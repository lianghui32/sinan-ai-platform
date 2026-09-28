"""账号与鉴权：pbkdf2_hmac 密码哈希 + secrets token 会话，纯标准库实现。

会话安全基线：
- token 带 expires_at（默认 7 天，AIP_SESSION_TTL_HOURS 可调），鉴权时校验，过期即删即 401；
- 鉴权依赖把 token 一并放进 user dict（供"改密后保留当前会话、踢掉其它设备"用），
  路由对外返回用户信息前必须 pop 掉 token / db_path。
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException

from .config import SESSION_TTL_HOURS
from .db import get_conn, now

PBKDF2_ITERATIONS = 120_000

# 时序均衡用的假口令参数：用户不存在时也跑一次等价 pbkdf2，避免响应时间差异枚举用户名
_DUMMY_SALT = "0123456789abcdef0123456789abcdef"
_DUMMY_HASH = "00" * 32


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt, hex_hash)。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, h = hash_password(password, salt)
    return secrets.compare_digest(h, expected_hash)


def verify_dummy(password: str) -> None:
    """用户不存在时做一次等价代价的哈希计算，抹平登录接口的时序侧信道。"""
    hash_password(password, _DUMMY_SALT)


def session_expiry() -> str:
    return (datetime.now() + timedelta(hours=SESSION_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")


def create_session(conn, user_id: int) -> str:
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, now(), session_expiry()),
    )
    conn.commit()
    return token


def delete_user_sessions(conn, user_id: int, keep_token: str | None = None) -> None:
    """删除某用户的会话；keep_token 用于改密场景保留当前设备登录态。"""
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, keep_token))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions(conn) -> int:
    """清理已过期会话，返回清理行数。登录/登出时顺带执行，避免 sessions 表无限膨胀。"""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at<>'' AND expires_at<=?", (now(),))
    return cur.rowcount


def _row_to_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "department": row["department"],
        "display_name": row["display_name"],
        "must_change_password": bool(row["must_change_password"]) if "must_change_password" in row.keys() else False,
        "created_at": row["created_at"],
    }


def make_user_dependency(get_db_path):
    """构造绑定到具体 app 的当前用户依赖。get_db_path(request)->str"""
    from fastapi import Request

    def dep(request: Request, authorization: str = Header(default="")) -> dict:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="未登录或缺少 Token")
        token = authorization.split(" ", 1)[1].strip()
        conn = get_conn(get_db_path(request))
        try:
            row = conn.execute(
                "SELECT u.*, s.expires_at AS session_expires_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
            if row is not None:
                expires_at = row["session_expires_at"] or ""
                if not expires_at or expires_at <= now():
                    # 过期会话：当场删除，返回 401
                    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                    conn.commit()
                    row = None
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
        user = _row_to_user(row)
        user["db_path"] = get_db_path(request)
        user["token"] = token   # 内部字段：改密后保留当前会话用；对外返回前必须 pop
        return user

    return dep


def make_admin_dependency(user_dep):
    """在 user_dep 基础上要求 admin 角色。"""

    def dep(user: dict = Depends(user_dep)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    return dep
