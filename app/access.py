"""可见性与访问控制规则：助手与知识库统一按「部门匹配 + 显式授权」判定。

- admin 全量可见；
- 配置的 departments 为空 = 对全部门开放；
- 否则要求用户部门（可多个，逗号分隔）与配置部门有交集；
- 助手额外支持 user_assistants 显式授权（部门规则之外的补充授权）。

聊天发消息、RAG 检索、工作流 knowledge_search 节点都必须走这里的同一套判定，
避免"列表处校验、使用处漏校验"的横向越权。
"""


def split_depts(raw: str | None) -> list[str]:
    return [d.strip() for d in (raw or "").split(",") if d.strip()]


def _overlaps(user_dept: str, allowed: list[str]) -> bool:
    return any(d in allowed for d in split_depts(user_dept))


def assistant_accessible(conn, assistant_row, user: dict) -> bool:
    """岗位助手可见性。assistant_row 需含 id/departments 字段。"""
    if user.get("role") == "admin":
        return True
    allowed = split_depts(assistant_row["departments"])
    # departments 为空 = 全部门可用
    if not allowed or _overlaps(user.get("department", ""), allowed):
        return True
    granted = conn.execute(
        "SELECT 1 FROM user_assistants WHERE user_id=? AND assistant_id=?",
        (user["id"], assistant_row["id"]),
    ).fetchone()
    return granted is not None


def kb_accessible(conn, kb_row, user: dict) -> bool:
    """知识库可见性（kb_row 需含 departments 字段；旧库迁移后该列必有值）。"""
    if user.get("role") == "admin":
        return True
    allowed = split_depts(kb_row["departments"] if "departments" in kb_row.keys() else "")
    if not allowed:
        return True
    return _overlaps(user.get("department", ""), allowed)


def get_user_row(conn, user_id) -> dict | None:
    """按 id 取用户基础信息（工作流节点执行时用 user_id 复查权限）。"""
    if not user_id:
        return None
    row = conn.execute("SELECT id,username,role,department FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def check_departments_value(raw: str) -> str:
    """校验 departments 配置串（助手/知识库通用）：逗号分隔，每项必须在部门白名单内。
    返回规范化后的串（去空格、去重、保序）；非法项抛 ValueError，调用方映射 400。"""
    from .config import ALLOWED_DEPARTMENTS

    seen: list[str] = []
    for d in split_depts(raw):
        if d not in ALLOWED_DEPARTMENTS:
            raise ValueError(f"部门必须是: {'/'.join(ALLOWED_DEPARTMENTS)}，非法值: {d}")
        if d not in seen:
            seen.append(d)
    return ",".join(seen)
