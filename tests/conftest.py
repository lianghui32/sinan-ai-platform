"""pytest 公共夹具。

本机 httpx 0.28 与 starlette TestClient 不兼容（Client.__init__ 移除了 app 参数），
因此用 httpx.ASGITransport 自建同步测试客户端；http_api 节点测试则在 8005 端口
起真实 uvicorn 线程服务（mock 路由挂在同一端口内，不另开端口）。
"""
import asyncio
import io
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试会话内所有 app 实例共用同一个内部密钥：client 应用（进程内）与
# live_server 应用（8005 端口独立实例）是两个 create_app 产物，
# 工作流 http_api 节点回环调 mock 路由时密钥必须一致，否则会被 401。
os.environ.setdefault("AIP_INTERNAL_SECRET", "test-internal-secret")

from app.main import create_app  # noqa: E402


class SyncClient:
    """基于 ASGITransport 的同步测试客户端（每请求独立事件循环，够用且隔离）。"""

    def __init__(self, app):
        self._transport = httpx.ASGITransport(app=app)

    def request(self, method, url, **kw):
        async def _do():
            async with httpx.AsyncClient(transport=self._transport, base_url="http://testserver") as ac:
                return await ac.request(method, url, **kw)

        return asyncio.run(_do())

    def get(self, url, **kw): return self.request("GET", url, **kw)
    def post(self, url, **kw): return self.request("POST", url, **kw)
    def put(self, url, **kw): return self.request("PUT", url, **kw)
    def delete(self, url, **kw): return self.request("DELETE", url, **kw)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试用例独立数据库的应用实例。

    常规用例把 LLM/工作流限流放到极高（限流行为由专项测试用自建 app 验证），
    避免任何测试因默认 30 条/分钟、20 次/分钟而偶发 429。
    """
    monkeypatch.setenv("AIP_CHAT_RATE_PER_MIN", "100000")
    monkeypatch.setenv("AIP_WF_RATE_PER_MIN", "100000")
    app = create_app(str(tmp_path / "test.db"))
    return SyncClient(app)


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client):
    return login(client, "admin", "admin123")


@pytest.fixture()
def emp_token(client):
    return login(client, "emp001", "emp123")


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """会话级真实 uvicorn 服务（127.0.0.1:8005），供 http_api 节点与冒烟使用。"""
    import socket

    import uvicorn

    # 端口预检：8005 被占用时（例如本服务正在运行）立即报错，
    # 否则下面的健康检查会"误连"到别的实例，用错误的数据库跑出一堆假失败。
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 8005))
    except OSError as exc:
        raise RuntimeError(
            "测试端口 8005 已被占用（例如本服务正在运行），请先停止后再跑测试") from exc
    finally:
        probe.close()

    db = str(tmp_path_factory.mktemp("live") / "live.db")
    app = create_app(db)
    config = uvicorn.Config(app, host="127.0.0.1", port=8005, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = "http://127.0.0.1:8005"
    # 就绪探测用 TCP 连接而不是 HTTP 请求：socket accept 只在 uvicorn 完成
    # startup（应用挂载完毕）后开始，TCP 通 = 服务就绪；且不经过 httpx，
    # 不存在系统代理把回环请求转发出去的问题（见 §10.1 的 502 教训）。
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", 8005), timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("uvicorn 未能在 8005 端口启动")
    yield base
    server.should_exit = True
    t.join(timeout=8)


def make_csv(rows=("month,product,sales", "2025-04,保温杯,1200.5", "2025-04,榨汁杯,800",
                   "2025-05,保温杯,1500", "2025-05,榨汁杯,900.25", "2025-05,手机壳,300")):
    return io.BytesIO(("\n".join(rows) + "\n").encode("utf-8"))
