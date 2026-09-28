"""注册 / 登录 / 当前用户 / 登出 / 自助改密。

安全基线：
- 登录按 用户名+IP 做失败锁定（默认 5 次失败锁 15 分钟），防爆破；
- 用户不存在时也执行一次等价代价的 pbkdf2，抹平时序侧信道；
- 注册部门走白名单（助手/知识库可见性按部门匹配，开放自填等于自助提权）；
- 登录成败写审计日志。
"""
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import (
    create_session,
    delete_user_sessions,
    hash_password,
    purge_expired_sessions,
    verify_dummy,
    verify_password,
)
from ..config import ALLOWED_DEPARTMENTS
from ..db import get_conn, now, record_audit

# 用户名：字母/数字/下划线/-/.@ 与中文（展示层已统一转义，这里再从源头收敛字符集）
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-.@\u4e00-\u9fff]{2,32}$")


class RegisterReq(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    department: str = ""
    display_name: str = ""


class LoginReq(BaseModel):
    username: str
    password: str


class ChangePasswordReq(BaseModel):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=6, max_length=64)


class LoginThrottle:
    """进程内登录失败锁定：同一 用户名+IP 在窗口期内连续失败达上限后锁定一段时间。

    单实例内存实现，口径与 UserRateLimiter 一致；横向扩容时换 Redis 即可。
    """

    def __init__(self, max_failures: int, lockout_seconds: int):
        self.max_failures = max(1, max_failures)
        self.lockout_seconds = max(1, lockout_seconds)
        self._fails: dict[str, list[float]] = {}
        # 键是"用户名|IP"（用户名攻击者可控）：不给上限会被海量随机用户名撑爆内存
        self.max_keys = 10_000

    def _window(self, key: str) -> list[float]:
        ts = time.monotonic()
        if len(self._fails) > self.max_keys:
            self._prune(ts)
        window = self._fails.setdefault(key, [])
        while window and ts - window[0] > self.lockout_seconds:
            window.pop(0)
        return window

    def _prune(self, ts: float) -> None:
        """先清掉窗口外全部过期键；仍超限则丢最旧的键（ 攻击面优先保可用性）。"""
        for k in [k for k, w in self._fails.items() if not w or ts - w[-1] > self.lockout_seconds]:
            del self._fails[k]
        while len(self._fails) > self.max_keys:
            self._fails.pop(next(iter(self._fails)))

    def locked_for(self, key: str) -> int:
        """返回剩余锁定秒数（0 = 未锁定）。"""
        window = self._window(key)
        if len(window) >= self.max_failures:
            return max(1, int(self.lockout_seconds - (time.monotonic() - window[0])))
        return 0

    def record_failure(self, key: str) -> None:
        self._window(key).append(time.monotonic())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


def check_department(department: str) -> str:
    """部门白名单校验（空字符串=未分配，合法）。"""
    dept = (department or "").strip()
    if dept and dept not in ALLOWED_DEPARTMENTS:
        raise HTTPException(400, f"部门必须是: {'/'.join(ALLOWED_DEPARTMENTS)}，或留空")
    return dept


def client_ip(request: Request | None) -> str:
    try:
        return (request.client.host if request and request.client else "") or "unknown"
    except Exception:
        return "unknown"


def build_router(deps: dict) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    get_db_path = deps["get_db_path"]
    user_dep = deps["user_dep"]
    throttle: LoginThrottle = deps["login_throttle"]

    @router.post("/register")
    def register(req: RegisterReq, request: Request):
        username = req.username.strip()
        if not _USERNAME_RE.match(username):
            raise HTTPException(400, "用户名只能包含中英文、数字和 _ - . @，长度 2-32")
        department = check_department(req.department)
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                raise HTTPException(status_code=400, detail="用户名已存在")
            salt, phash = hash_password(req.password)
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,salt,role,department,display_name,"
                "must_change_password,created_at) VALUES(?,?,?,?,?,?,0,?)",
                (username, phash, salt, "employee", department,
                 req.display_name.strip() or username, now()),
            )
            record_audit(conn, user_id=cur.lastrowid, username=username, action="register",
                         detail=f"department={department or '未分配'}", ip=client_ip(request))
            conn.commit()
            return {"id": cur.lastrowid, "username": username, "role": "employee"}
        finally:
            conn.close()

    @router.post("/login")
    def login(req: LoginReq, request: Request):
        username = req.username.strip()
        ip = client_ip(request)
        key = f"{username.lower()}|{ip}"
        remain = throttle.locked_for(key)
        if remain:
            raise HTTPException(429, f"失败次数过多，账号已临时锁定，请 {remain} 秒后再试",
                                headers={"Retry-After": str(remain)})
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                # 时序均衡：用户不存在也做一次等价代价的哈希，避免响应时间差异枚举用户名
                verify_dummy(req.password)
                ok = False
            else:
                ok = verify_password(req.password, row["salt"], row["password_hash"])
            if not ok:
                throttle.record_failure(key)
                record_audit(conn, user_id=row["id"] if row else None, username=username,
                             action="login_fail", detail="口令错误或用户不存在", ip=ip)
                conn.commit()
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            throttle.clear(key)
            purge_expired_sessions(conn)
            token = create_session(conn, row["id"])
            record_audit(conn, user_id=row["id"], username=username, action="login_ok", ip=ip)
            conn.commit()
            return {
                "token": token,
                "user": {
                    "id": row["id"], "username": row["username"], "role": row["role"],
                    "department": row["department"], "display_name": row["display_name"],
                    "must_change_password": bool(row["must_change_password"]),
                },
            }
        finally:
            conn.close()

    @router.get("/me")
    def me(user: dict = Depends(user_dep)):
        user = dict(user)
        user.pop("db_path", None)
        user.pop("token", None)
        conn = get_conn(deps["get_db_path"]())
        try:
            grants = [r["assistant_id"] for r in conn.execute(
                "SELECT assistant_id FROM user_assistants WHERE user_id=?", (user["id"],))]
        finally:
            conn.close()
        user["granted_assistant_ids"] = grants
        return user

    @router.post("/change-password")
    def change_password(req: ChangePasswordReq, request: Request, user: dict = Depends(user_dep)):
        """自助改密：校验旧口令 → 更新哈希 → 清除强制改密标记 → 踢掉其它设备（保留当前会话）。"""
        if req.new_password == req.old_password:
            # 强制改密的语义是"真正换掉初始口令"：原样重提交视为未轮换，拒绝
            raise HTTPException(400, "新密码不能与当前密码相同")
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if row is None or not verify_password(req.old_password, row["salt"], row["password_hash"]):
                raise HTTPException(400, "旧密码不正确")
            salt, phash = hash_password(req.new_password)
            conn.execute(
                "UPDATE users SET salt=?, password_hash=?, must_change_password=0 WHERE id=?",
                (salt, phash, user["id"]))
            delete_user_sessions(conn, user["id"], keep_token=user.get("token"))
            record_audit(conn, user_id=user["id"], username=user["username"],
                         action="change_password", ip=client_ip(request))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.post("/logout")
    def logout(user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            delete_user_sessions(conn, user["id"])
            purge_expired_sessions(conn)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    return router
