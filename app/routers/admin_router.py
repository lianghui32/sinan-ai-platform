"""管理后台：用户管理、助手配置CRUD、使用情况统计、全部对话记录、Provider设置、审计查询。

安全基线：
- 用户部门走白名单（助手/知识库可见性按部门匹配）；
- 管理员重置口令后：目标用户被标记"下次登录必须改密"，且其会话全部吊销；
- openai_api_key 静态加密后入库（enc1: 前缀），接口永远只返回"是否已配置"；
- 敏感操作（用户/助手/知识库变更、模型设置、登录成败）写 audit_logs。
"""
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..access import check_departments_value
from ..auth import delete_user_sessions, hash_password
from ..config import ENV_OPENAI_API_KEY, ENV_OPENAI_BASE_URL, ENV_OPENAI_MODEL
from ..db import get_conn, now, record_audit
from ..secretbox import SecretBox, get_box
from .auth_router import check_department, client_ip


class CreateUserReq(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    role: str = "employee"          # admin / employee
    department: str = ""
    display_name: str = ""
    assistant_ids: list[int] = []


class UpdateUserReq(BaseModel):
    role: str | None = None
    department: str | None = None
    display_name: str | None = None
    # 与注册/新建保持一致的口令强度；None = 不修改密码
    password: str | None = Field(default=None, min_length=6, max_length=64)
    assistant_ids: list[int] | None = None


class AssistantReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)
    system_prompt: str = Field(default="", max_length=8000)
    kb_id: int | None = None
    provider: str = "mock"
    model: str = Field(default="mock-model", max_length=64)
    departments: str = Field(default="", max_length=256)


class SettingsReq(BaseModel):
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None


def build_router(deps: dict) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])
    get_db_path = deps["get_db_path"]
    admin_dep = deps["admin_dep"]

    def _check_provider(p: str):
        if p not in ("mock", "openai_compatible"):
            raise HTTPException(400, "provider 必须是 mock 或 openai_compatible")

    def _check_grants(conn, assistant_ids) -> list[int]:
        """显式分配的助手必须真实存在，否则会留下永远不生效的幽灵授权。"""
        ids = sorted(set(assistant_ids or []))
        if not ids:
            return []
        # 助手总量很小，直接取全量 id 在 Python 侧比对，避免动态构造 IN 占位符
        have = {r["id"] for r in conn.execute("SELECT id FROM assistants")}
        missing = [i for i in ids if i not in have]
        if missing:
            raise HTTPException(400, f"助手不存在，无法分配: {missing}")
        return ids

    # ---------------- 用户管理 ----------------
    @router.get("/users")
    def list_users(admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute("SELECT id,username,role,department,display_name,must_change_password,created_at FROM users ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["must_change_password"] = bool(d["must_change_password"])
                d["assistant_ids"] = [g["assistant_id"] for g in conn.execute(
                    "SELECT assistant_id FROM user_assistants WHERE user_id=?", (r["id"],))]
                d["message_count"] = conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE c.user_id=? AND m.role='user'", (r["id"],)).fetchone()[0]
                out.append(d)
            return out
        finally:
            conn.close()

    @router.post("/users")
    def create_user(req: CreateUserReq, request: Request, admin: dict = Depends(admin_dep)):
        if req.role not in ("admin", "employee"):
            raise HTTPException(400, "role 必须是 admin 或 employee")
        department = check_department(req.department)
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM users WHERE username=?", (req.username.strip(),)).fetchone():
                raise HTTPException(400, "用户名已存在")
            grants = _check_grants(conn, req.assistant_ids)
            salt, phash = hash_password(req.password)
            # 管理员代设的初始口令：强制用户首次登录自行修改
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,salt,role,department,display_name,"
                "must_change_password,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (req.username.strip(), phash, salt, req.role, department,
                 req.display_name.strip() or req.username.strip(), now()))
            uid = cur.lastrowid
            for aid in grants:
                conn.execute("INSERT OR IGNORE INTO user_assistants(user_id,assistant_id) VALUES(?,?)", (uid, aid))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="user_create",
                         detail=f"new_uid={uid} role={req.role} department={department or '未分配'}",
                         ip=client_ip(request))
            conn.commit()
            return {"id": uid}
        finally:
            conn.close()

    @router.put("/users/{uid}")
    def update_user(uid: int, req: UpdateUserReq, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if row is None:
                raise HTTPException(404, "用户不存在")
            changes: list[str] = []
            if req.role is not None:
                if req.role not in ("admin", "employee"):
                    raise HTTPException(400, "role 非法")
                if uid == admin["id"] and req.role != "admin":
                    raise HTTPException(400, "不能降级自己的角色")
                conn.execute("UPDATE users SET role=? WHERE id=?", (req.role, uid))
                changes.append(f"role={req.role}")
            if req.department is not None:
                department = check_department(req.department)
                conn.execute("UPDATE users SET department=? WHERE id=?", (department, uid))
                changes.append(f"department={department or '未分配'}")
            if req.display_name is not None:
                conn.execute("UPDATE users SET display_name=? WHERE id=?", (req.display_name.strip(), uid))
                changes.append("display_name")
            if req.password:
                salt, phash = hash_password(req.password)
                conn.execute(
                    "UPDATE users SET salt=?, password_hash=?, must_change_password=1 WHERE id=?",
                    (salt, phash, uid))
                # 重置口令后吊销目标用户全部会话：旧登录态不能带着新口令继续用
                delete_user_sessions(conn, uid)
                changes.append("password_reset(会话已吊销)")
            if req.assistant_ids is not None:
                grants = _check_grants(conn, req.assistant_ids)
                conn.execute("DELETE FROM user_assistants WHERE user_id=?", (uid,))
                for aid in grants:
                    conn.execute("INSERT OR IGNORE INTO user_assistants(user_id,assistant_id) VALUES(?,?)", (uid, aid))
                changes.append(f"grants={grants}")
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="user_update",
                         detail=f"uid={uid} {';'.join(changes)}", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.delete("/users/{uid}")
    def delete_user(uid: int, request: Request, admin: dict = Depends(admin_dep)):
        if uid == admin["id"]:
            raise HTTPException(400, "不能删除自己")
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
            if row is None:
                raise HTTPException(404, "用户不存在")
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM user_assistants WHERE user_id=?", (uid,))
            for c in conn.execute("SELECT id FROM conversations WHERE user_id=?", (uid,)).fetchall():
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (c["id"],))
            conn.execute("DELETE FROM conversations WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="user_delete",
                         detail=f"uid={uid} username={row['username']}", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    # ---------------- 助手配置 CRUD ----------------
    @router.get("/assistants")
    def list_assistants(admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute("SELECT * FROM assistants ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @router.post("/assistants")
    def create_assistant(req: AssistantReq, request: Request, admin: dict = Depends(admin_dep)):
        _check_provider(req.provider)
        try:
            departments = check_departments_value(req.departments)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        conn = get_conn(get_db_path())
        try:
            if req.kb_id and conn.execute("SELECT id FROM knowledge_bases WHERE id=?", (req.kb_id,)).fetchone() is None:
                raise HTTPException(400, "绑定的知识库不存在")
            cur = conn.execute(
                "INSERT INTO assistants(name,description,system_prompt,kb_id,provider,model,departments,is_builtin,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (req.name.strip(), req.description.strip(), req.system_prompt.strip(),
                 req.kb_id, req.provider, req.model.strip() or "mock-model",
                 departments, 0, now()))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="assistant_create",
                         detail=f"assistant={cur.lastrowid} name={req.name.strip()}",
                         ip=client_ip(request))
            conn.commit()
            return {"id": cur.lastrowid}
        finally:
            conn.close()

    @router.put("/assistants/{aid}")
    def update_assistant(aid: int, req: AssistantReq, request: Request, admin: dict = Depends(admin_dep)):
        _check_provider(req.provider)
        try:
            departments = check_departments_value(req.departments)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM assistants WHERE id=?", (aid,)).fetchone() is None:
                raise HTTPException(404, "助手不存在")
            if req.kb_id and conn.execute("SELECT id FROM knowledge_bases WHERE id=?", (req.kb_id,)).fetchone() is None:
                raise HTTPException(400, "绑定的知识库不存在")
            conn.execute(
                "UPDATE assistants SET name=?,description=?,system_prompt=?,kb_id=?,provider=?,model=?,departments=? WHERE id=?",
                (req.name.strip(), req.description.strip(), req.system_prompt.strip(),
                 req.kb_id, req.provider, req.model.strip() or "mock-model",
                 departments, aid))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="assistant_update",
                         detail=f"assistant={aid} provider={req.provider}",
                         ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.delete("/assistants/{aid}")
    def delete_assistant(aid: int, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise HTTPException(404, "助手不存在")
            conn.execute("DELETE FROM user_assistants WHERE assistant_id=?", (aid,))
            for c in conn.execute("SELECT id FROM conversations WHERE assistant_id=?", (aid,)).fetchall():
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (c["id"],))
            conn.execute("DELETE FROM conversations WHERE assistant_id=?", (aid,))
            conn.execute("DELETE FROM assistants WHERE id=?", (aid,))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="assistant_delete",
                         detail=f"assistant={aid} name={row['name']}", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    # ---------------- 使用情况统计 ----------------
    @router.get("/stats")
    def stats(admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            per_user = [dict(r) for r in conn.execute(
                "SELECT u.id AS user_id, u.username, u.department, COUNT(m.id) AS message_count "
                "FROM users u LEFT JOIN conversations c ON c.user_id=u.id "
                "LEFT JOIN messages m ON m.conversation_id=c.id "
                "GROUP BY u.id ORDER BY message_count DESC")]
            per_assistant = [dict(r) for r in conn.execute(
                "SELECT a.id AS assistant_id, a.name, COUNT(m.id) AS message_count "
                "FROM assistants a LEFT JOIN conversations c ON c.assistant_id=a.id "
                "LEFT JOIN messages m ON m.conversation_id=c.id "
                "GROUP BY a.id ORDER BY message_count DESC")]
            daily_active = [dict(r) for r in conn.execute(
                "SELECT substr(m.created_at,1,10) AS date, COUNT(DISTINCT c.user_id) AS active_users, "
                "COUNT(m.id) AS messages FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE date(m.created_at) >= date('now','localtime','-6 days') "
                "GROUP BY date ORDER BY date")]
            totals = {
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "assistants": conn.execute("SELECT COUNT(*) FROM assistants").fetchone()[0],
                "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "workflow_runs": conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0],
            }
            # LLM 网关用量（成本与稳定性视角）：调用数、成功率、降级数、token、平均延迟
            llm_row = conn.execute(
                "SELECT COUNT(*) AS calls,"
                " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,"
                " SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degraded,"
                " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
                " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                " COALESCE(AVG(CASE WHEN status != 'failed' THEN latency_ms END),0) AS avg_latency_ms,"
                " COALESCE(AVG(attempts),1) AS avg_attempts"
                " FROM llm_calls").fetchone()
            llm_calls = dict(llm_row) if llm_row else {}
            llm_by_assistant = [dict(r) for r in conn.execute(
                "SELECT COALESCE(a.name, '工作流节点') AS name, COUNT(*) AS calls,"
                " SUM(l.completion_tokens) AS completion_tokens,"
                " SUM(CASE WHEN l.status='degraded' THEN 1 ELSE 0 END) AS degraded"
                " FROM llm_calls l LEFT JOIN assistants a ON a.id = l.assistant_id"
                " GROUP BY l.assistant_id ORDER BY calls DESC LIMIT 10")]
            return {"totals": totals, "per_user": per_user,
                    "per_assistant": per_assistant, "daily_active_7d": daily_active,
                    "llm": {"calls": llm_calls.get("calls", 0),
                            "success": llm_calls.get("success", 0) or 0,
                            "degraded": llm_calls.get("degraded", 0) or 0,
                            "failed": llm_calls.get("failed", 0) or 0,
                            "prompt_tokens": llm_calls.get("prompt_tokens", 0),
                            "completion_tokens": llm_calls.get("completion_tokens", 0),
                            "avg_latency_ms": round(llm_calls.get("avg_latency_ms", 0) or 0),
                            "avg_attempts": round(llm_calls.get("avg_attempts", 1) or 1, 2),
                            "by_assistant": llm_by_assistant}}
        finally:
            conn.close()

    # ---------------- 全部对话记录（后台） ----------------
    @router.get("/conversations")
    def all_conversations(user_id: int | None = None, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            sql = ("SELECT c.*, u.username, a.name AS assistant_name, "
                   "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
                   "FROM conversations c JOIN users u ON u.id=c.user_id "
                   "JOIN assistants a ON a.id=c.assistant_id")
            args: list = []
            if user_id:
                sql += " WHERE c.user_id=?"
                args.append(user_id)
            sql += " ORDER BY c.updated_at DESC LIMIT 200"
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    @router.get("/conversations/{cid}/messages")
    def conversation_messages(cid: int, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone() is None:
                raise HTTPException(404, "会话不存在")
            rows = conn.execute(
                "SELECT id,role,content,refs_json,created_at FROM messages WHERE conversation_id=? ORDER BY id",
                (cid,)).fetchall()
            return [{**dict(r), "refs": json.loads(r["refs_json"] or "[]")} for r in rows]
        finally:
            conn.close()

    # ---------------- Provider / 模型设置 ----------------
    @router.get("/settings")
    def get_settings(admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute("SELECT key,value FROM settings").fetchall()
            s = {r["key"]: r["value"] for r in rows}
        finally:
            conn.close()
        return {
            "providers": ["mock", "openai_compatible"],
            "openai_base_url": s.get("openai_base_url", "") or os.environ.get(ENV_OPENAI_BASE_URL, ""),
            "openai_api_key_set": bool(s.get("openai_api_key") or os.environ.get(ENV_OPENAI_API_KEY, "")),
            "openai_model": s.get("openai_model", "") or os.environ.get(ENV_OPENAI_MODEL, ""),
            "from_env": not s.get("openai_base_url"),
        }

    @router.put("/settings")
    def put_settings(req: SettingsReq, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            changed: list[str] = []
            for key, val in (("openai_base_url", req.openai_base_url),
                             ("openai_api_key", req.openai_api_key),
                             ("openai_model", req.openai_model)):
                if val is None:
                    continue
                store = val.strip()
                if key == "openai_api_key" and store and not SecretBox.is_encrypted(store):
                    # API Key 静态加密后入库（历史明文在下次保存时会被加密覆盖）；
                    # 密钥文件与库文件同目录，与 providers 解密侧共用同一把钥匙
                    store = get_box(str(Path(get_db_path()).parent)).encrypt(store)
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, store))
                changed.append(key)
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="settings_update",
                         detail=f"keys={','.join(changed)}",
                         ip=client_ip(request))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    # ---------------- 安全审计查询 ----------------
    @router.get("/audit")
    def audit_logs(limit: int = 200, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute(
                "SELECT id,user_id,username,action,detail,ip,created_at FROM audit_logs "
                "ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return router
