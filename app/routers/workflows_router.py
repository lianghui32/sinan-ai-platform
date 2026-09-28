"""工作流：DAG 定义 CRUD(admin)、触发执行（可上传 CSV）、查看每节点执行详情。

安全基线：
- 执行入口有独立频控（LLM 成本入口之二，默认 20 次/分钟/用户）；
- node_runs 执行记录写入前对节点配置脱敏（headers/body/url 中的密钥打 ***）；
- 员工视角的工作流定义同样脱敏，仅 admin 编辑时能拿到原文；
- 上传文件分块限量读取，防止"先读后校验"把超大请求整体拉进内存。
"""
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..config import WF_RUN_FILE_MAX_BYTES
from ..db import get_conn, now, record_audit
from ..uploads import read_sync
from ..workflows import (
    WorkflowDefinitionError,
    execute_workflow,
    parse_csv_text,
    redact_config,
    redact_definition,
    validate_definition,
    validate_expressions,
)
from .auth_router import client_ip


class WorkflowReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)
    definition: dict


def _row(w) -> dict:
    return {
        "id": w["id"], "name": w["name"], "description": w["description"],
        "definition": json.loads(w["definition_json"]),
        "is_builtin": bool(w["is_builtin"]), "created_by": w["created_by"],
        "created_at": w["created_at"],
    }


def _check_definition(defn: dict) -> None:
    """保存前校验：DAG 结构 + code/condition 表达式安全性（配置期即拒绝，不必跑一遍才知道）。"""
    try:
        validate_definition(defn)
        validate_expressions(defn)
    except WorkflowDefinitionError as exc:
        raise HTTPException(400, f"工作流定义非法: {exc}")


def build_router(deps: dict) -> APIRouter:
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])
    get_db_path = deps["get_db_path"]
    user_dep = deps["user_dep"]
    admin_dep = deps["admin_dep"]
    wf_rate_limiter = deps.get("wf_rate_limiter")
    internal_secret = deps.get("internal_secret", "")

    @router.get("")
    def list_workflows(user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute("SELECT * FROM workflows ORDER BY id").fetchall()
            out = []
            for w in rows:
                d = json.loads(w["definition_json"])
                out.append({
                    "id": w["id"], "name": w["name"], "description": w["description"],
                    "is_builtin": bool(w["is_builtin"]), "created_at": w["created_at"],
                    "node_count": len(d.get("nodes", [])),
                    "node_types": sorted({n.get("type") for n in d.get("nodes", [])}),
                })
            return out
        finally:
            conn.close()

    @router.get("/{wf_id}")
    def get_workflow(wf_id: int, user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            w = conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
            if w is None:
                raise HTTPException(404, "工作流不存在")
            row = _row(w)
            if user["role"] != "admin":
                # 员工只需要看节点结构；http_api 的 headers/body 等配置可能含密钥，一律脱敏展示
                row["definition"] = redact_definition(row["definition"])
            return row
        finally:
            conn.close()

    @router.post("")
    def create_workflow(req: WorkflowReq, request: Request, admin: dict = Depends(admin_dep)):
        _check_definition(req.definition)
        conn = get_conn(get_db_path())
        try:
            cur = conn.execute(
                "INSERT INTO workflows(name,description,definition_json,is_builtin,created_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (req.name.strip(), req.description.strip(),
                 json.dumps(req.definition, ensure_ascii=False), 0, admin["id"], now()))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="workflow_create",
                         detail=f"wf={cur.lastrowid} name={req.name.strip()}",
                         ip=client_ip(request))
            conn.commit()
            return {"id": cur.lastrowid}
        finally:
            conn.close()

    @router.put("/{wf_id}")
    def update_workflow(wf_id: int, req: WorkflowReq, request: Request, admin: dict = Depends(admin_dep)):
        _check_definition(req.definition)
        conn = get_conn(get_db_path())
        try:
            w = conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
            if w is None:
                raise HTTPException(404, "工作流不存在")
            conn.execute(
                "UPDATE workflows SET name=?, description=?, definition_json=? WHERE id=?",
                (req.name.strip(), req.description.strip(),
                 json.dumps(req.definition, ensure_ascii=False), wf_id))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="workflow_update",
                         detail=f"wf={wf_id}", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.delete("/{wf_id}")
    def delete_workflow(wf_id: int, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            w = conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
            if w is None:
                raise HTTPException(404, "工作流不存在")
            conn.execute("DELETE FROM node_runs WHERE run_id IN (SELECT id FROM workflow_runs WHERE workflow_id=?)", (wf_id,))
            conn.execute("DELETE FROM workflow_runs WHERE workflow_id=?", (wf_id,))
            conn.execute("DELETE FROM workflows WHERE id=?", (wf_id,))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="workflow_delete",
                         detail=f"wf={wf_id}", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    # ---------------- 执行 ----------------
    # 注意：这里用同步 def（FastAPI 会放入线程池执行），保证工作流内 http_api 节点
    # 回调本服务 8005 端口的 mock 路由时不会阻塞事件循环造成自锁。
    @router.post("/{wf_id}/run")
    def run_workflow(
        wf_id: int,
        request: Request,
        file: UploadFile | None = File(default=None),
        params: str = Form(default="{}"),
        user: dict = Depends(user_dep),
    ):
        if wf_rate_limiter and not wf_rate_limiter.allow(user["id"]):
            retry = wf_rate_limiter.retry_after(user["id"])
            raise HTTPException(429, f"工作流执行太频繁，请 {retry} 秒后再试",
                                headers={"Retry-After": str(retry)})
        conn = get_conn(get_db_path())
        try:
            w = conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
            if w is None:
                raise HTTPException(404, "工作流不存在")
            defn = json.loads(w["definition_json"])

            # 组装运行输入：params JSON + 上传文件（CSV 自动解析为 rows）
            try:
                param_dict = json.loads(params) if params else {}
            except json.JSONDecodeError:
                raise HTTPException(400, "params 必须是合法 JSON")
            if not isinstance(param_dict, dict):
                raise HTTPException(400, "params 必须是 JSON 对象，如 {\"campaign\":\"双11\"}")
            run_input: dict = {"params": param_dict, "rows": [], "file_name": "", "file_text": ""}
            if file is not None:
                # 分块限量读取（先读后校验会让超大请求整体进内存）
                try:
                    raw = read_sync(file.file, WF_RUN_FILE_MAX_BYTES)
                except ValueError as exc:
                    raise HTTPException(400, str(exc))
                text = raw.decode("utf-8-sig", errors="replace")
                run_input["file_name"] = file.filename or "upload.csv"
                run_input["file_text"] = text
                if (file.filename or "").lower().endswith(".csv"):
                    run_input["rows"] = parse_csv_text(text)

            cur = conn.execute(
                "INSERT INTO workflow_runs(workflow_id,user_id,status,input_json,started_at)"
                " VALUES(?,?,?,?,?)",
                (wf_id, user["id"], "running",
                 json.dumps({k: v for k, v in run_input.items() if k != "file_text"},
                            ensure_ascii=False)[:20000], now()))
            run_id = cur.lastrowid
            record_audit(conn, user_id=user["id"], username=user["username"], action="workflow_run",
                         detail=f"wf={wf_id} run={run_id}", ip=client_ip(request))
            conn.commit()

            def on_node(node_id, node_type, status, node_input, node_output, error, started, finished):
                conn.execute(
                    "INSERT INTO node_runs(run_id,node_id,node_type,status,input_json,output_json,error,started_at,finished_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, node_id, node_type, status,
                     # 配置里可能带密钥（headers/URL query）：落库前统一脱敏
                     json.dumps(redact_config(node_input), ensure_ascii=False, default=str)[:20000],
                     json.dumps(redact_config(node_output) if isinstance(node_output, dict) else node_output,
                                ensure_ascii=False, default=str)[:20000],
                     error[:2000], started, finished))
                conn.commit()

            # 执行器内部已按节点捕获异常；这里再兜一层，保证运行记录不会卡在 running
            # （否则前端轮询永远等不到终态）
            try:
                result = execute_workflow(defn, run_input,
                                          {"conn": conn, "user_id": user["id"], "run_id": run_id,
                                           "internal_secret": internal_secret},
                                          on_node=on_node)
                final = {"status": result["status"], "output": result["output"],
                         "error": result["error"], "order": result["order"]}
            except Exception as exc:  # pragma: no cover — 定义损坏 / 写库失败等意外
                conn.rollback()
                final = {"status": "failed", "output": {},
                         "error": f"{type(exc).__name__}: {exc}", "order": []}

            conn.execute(
                "UPDATE workflow_runs SET status=?, output_json=?, error=?, finished_at=? WHERE id=?",
                (final["status"],
                 json.dumps(final["output"], ensure_ascii=False, default=str)[:50000],
                 final["error"][:2000], now(), run_id))
            conn.commit()
            return {"run_id": run_id, "status": final["status"],
                    "output": final["output"], "error": final["error"],
                    "order": final["order"]}
        finally:
            conn.close()

    @router.get("/{wf_id}/runs")
    def list_runs(wf_id: int, user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            # 员工只看自己的运行记录（必须在 SQL 里过滤，否则 LIMIT 50 会挤掉自己的记录）。
            # 两条固定 SQL 替代字符串拼接，参数全部走绑定。
            if user["role"] != "admin":
                rows = conn.execute(
                    "SELECT r.*, u.username FROM workflow_runs r JOIN users u ON u.id=r.user_id "
                    "WHERE r.workflow_id=? AND r.user_id=? ORDER BY r.id DESC LIMIT 50",
                    (wf_id, user["id"])).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.*, u.username FROM workflow_runs r JOIN users u ON u.id=r.user_id "
                    "WHERE r.workflow_id=? ORDER BY r.id DESC LIMIT 50",
                    (wf_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["output"] = json.loads(d.pop("output_json") or "{}")
                d.pop("input_json", None)
                out.append(d)
            return out
        finally:
            conn.close()

    @router.get("/runs/{run_id}/detail")
    def run_detail(run_id: int, user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            r = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if r is None:
                raise HTTPException(404, "运行记录不存在")
            if r["user_id"] != user["id"] and user["role"] != "admin":
                raise HTTPException(403, "无权查看他人运行记录")
            nodes = conn.execute(
                "SELECT * FROM node_runs WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            detail = dict(r)
            detail["output"] = json.loads(detail.pop("output_json") or "{}")
            detail["input"] = json.loads(detail.pop("input_json") or "{}")
            detail["nodes"] = [{
                **{k: n[k] for k in ("node_id", "node_type", "status", "error", "started_at", "finished_at")},
                "input": json.loads(n["input_json"] or "{}"),
                "output": json.loads(n["output_json"] or "{}"),
            } for n in nodes]
            return detail
        finally:
            conn.close()

    return router
