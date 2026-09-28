"""FastAPI 应用入口：create_app 工厂 + 静态前端挂载。

启动（端口固定 8005）：
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8005
    或  python -m app.main
"""
import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .auth import make_admin_dependency, make_user_dependency
from .config import MAX_BODY_BYTES, PORT, WF_RATE_PER_MIN, resolve_db_path
from .db import init_db
from .llm_gateway import UserRateLimiter
from .routers import (
    admin_router,
    auth_router,
    chat_router,
    integrations_router,
    kb_router,
    mock_router,
    workflows_router,
)
from .routers.auth_router import LoginThrottle
from .config import LOGIN_LOCKOUT_SECONDS, LOGIN_MAX_FAILURES

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 安全响应头：connect-src 限制为同源，外传 token/数据的 XSS 代价显著提高；
# script/style 允许 inline（前端原生 JS 使用内联事件处理器与首帧主题脚本）
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


def create_app(db_path: str | None = None) -> FastAPI:
    """应用工厂：db_path 参数 > 环境变量 AIP_DB_PATH > 默认 data/platform.db。"""
    resolved_db_path = resolve_db_path(db_path)
    init_db(resolved_db_path)

    app = FastAPI(
        title="司南 Sinan — 跨境电商企业级AI中台",
        description="多用户 + 岗位助手(流式对话/知识库RAG) + LLM网关 + JSON-DAG工作流自动化 + 使用审计",
        version="1.2.0",
    )
    app.state.db_path = resolved_db_path
    # 进程级内部密钥：工作流 http_api 节点回调本服务 mock 路由时使用，
    # 只存在于同进程内存中，外部调用者拿不到。多 worker 部署时用 AIP_INTERNAL_SECRET
    # 统一各进程的密钥，否则 A 进程发起的回环请求可能落到 B 进程被 401。
    env_secret = os.environ.get("AIP_INTERNAL_SECRET", "").strip()
    app.state.internal_secret = env_secret or secrets.token_hex(32)

    def get_db_path(request=None) -> str:  # noqa: ARG001 — 兼容依赖注入签名
        return resolved_db_path

    def _env_rate(name: str, default: str) -> int:
        try:
            return int(os.environ.get(name, default).strip())
        except ValueError:
            return int(default)

    chat_rate = _env_rate("AIP_CHAT_RATE_PER_MIN", "30")
    wf_rate = _env_rate("AIP_WF_RATE_PER_MIN", str(WF_RATE_PER_MIN))
    deps = {
        "get_db_path": get_db_path,
        "user_dep": make_user_dependency(get_db_path),
        "admin_dep": None,  # 下面填充
        # LLM 成本入口限流：每用户每分钟消息数 / 工作流执行数，<=0 表示不限制
        "rate_limiter": UserRateLimiter(chat_rate) if chat_rate > 0 else None,
        "wf_rate_limiter": UserRateLimiter(wf_rate) if wf_rate > 0 else None,
        "login_throttle": LoginThrottle(LOGIN_MAX_FAILURES, LOGIN_LOCKOUT_SECONDS),
        "internal_secret": app.state.internal_secret,
    }
    deps["admin_dep"] = make_admin_dependency(deps["user_dep"])

    @app.get("/api/health", tags=["meta"])
    def health():
        return {"status": "ok", "service": "sinan-ai-platform", "port": PORT}

    for mod in (auth_router, chat_router, kb_router, workflows_router,
                admin_router, integrations_router, mock_router):
        app.include_router(mod.build_router(deps))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # 统一中间件：
    # 1) 请求体大小上限——uvicorn 默认不限制 body，先看 Content-Length 直接 413，
    #    防止"先整读进内存再校验"的内存 DoS（上传另有分块限量读取兜底）；
    # 2) 安全响应头（CSP / nosniff / DENY / Referrer-Policy）覆盖所有响应；
    # 3) 静态资源 no-cache——每次协商缓存（ETag 未变返回 304）：前端迭代刷新即生效，
    #    又避免启发式缓存导致"改了 JS 刷新还在跑旧代码"。
    @app.middleware("http")
    async def common_headers_and_body_limit(request, call_next):
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            response = JSONResponse({"detail": f"请求体过大，上限 {MAX_BODY_BYTES // 1024 // 1024}MB"},
                                    status_code=413)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)
