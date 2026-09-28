"""LLM 网关层测试：限流 429、重试退避、降级标记、审计落库、配置缺失不降级。"""
import httpx
import pytest

from app import llm_gateway
from app.db import get_conn, init_db
from app.main import create_app
from conftest import SyncClient, auth, login


@pytest.fixture()
def gconn(tmp_path):
    db = str(tmp_path / "gw.db")
    init_db(db)
    conn = get_conn(db)
    yield conn
    conn.close()


class FlakyProvider:
    """前 fail_times 次调用抛网络错误，之后成功。"""

    name = "flaky"

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def chat(self, messages, model=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("connection refused（模拟网络抖动）")
        return f"ok-第{self.calls}次调用"


def _stub_provider(monkeypatch, provider):
    monkeypatch.setattr(llm_gateway, "get_provider", lambda name, conn=None: provider)


def test_retry_then_success_records_attempts(gconn, monkeypatch):
    _stub_provider(monkeypatch, FlakyProvider(fail_times=2))
    monkeypatch.setenv("AIP_LLM_MAX_ATTEMPTS", "3")
    r = llm_gateway.chat_with_gateway(
        gconn, provider_name="flaky", model="m",
        messages=[{"role": "user", "content": "hi"}], user_id=1)
    assert r["text"] == "ok-第3次调用"
    assert r["attempts"] == 3 and r["degraded"] is False
    row = gconn.execute("SELECT attempts,status,prompt_tokens,completion_tokens FROM llm_calls").fetchone()
    assert row["attempts"] == 3 and row["status"] == "success"
    assert row["prompt_tokens"] > 0 and row["completion_tokens"] > 0


def test_degrade_on_persistent_failure_is_marked(gconn, monkeypatch):
    _stub_provider(monkeypatch, FlakyProvider(fail_times=99))
    monkeypatch.setenv("AIP_LLM_MAX_ATTEMPTS", "2")
    r = llm_gateway.chat_with_gateway(
        gconn, provider_name="flaky", model="m",
        messages=[{"role": "user", "content": "hi"}], allow_degrade=True)
    assert r["degraded"] is True
    assert "ConnectError" in r["degrade_reason"]
    row = gconn.execute("SELECT status,error FROM llm_calls").fetchone()
    assert row["status"] == "degraded" and "ConnectError" in row["error"]


def test_no_degrade_raises_gateway_error_and_records_failed(gconn, monkeypatch):
    _stub_provider(monkeypatch, FlakyProvider(fail_times=99))
    monkeypatch.setenv("AIP_LLM_MAX_ATTEMPTS", "2")
    with pytest.raises(llm_gateway.GatewayError, match="模型调用失败"):
        llm_gateway.chat_with_gateway(
            gconn, provider_name="flaky", model="m",
            messages=[{"role": "user", "content": "hi"}], allow_degrade=False)
    row = gconn.execute("SELECT status FROM llm_calls").fetchone()
    assert row["status"] == "failed"


def test_config_missing_fails_fast_without_retry_or_degrade(gconn, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm_gateway.GatewayError, match="未配置"):
        llm_gateway.chat_with_gateway(
            gconn, provider_name="openai_compatible", model="m",
            messages=[{"role": "user", "content": "hi"}], allow_degrade=True)
    row = gconn.execute("SELECT attempts,status FROM llm_calls").fetchone()
    assert row["attempts"] == 1 and row["status"] == "failed"


def test_rate_limit_429_with_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("AIP_CHAT_RATE_PER_MIN", "3")
    app = create_app(str(tmp_path / "rl.db"))
    c = SyncClient(app)
    h = auth(login(c, "admin", "admin123"))
    cid = c.post("/api/conversations", json={"assistant_id": 1}, headers=h).json()["id"]
    for _ in range(3):
        assert c.post(f"/api/conversations/{cid}/messages",
                      json={"content": "你好"}, headers=h).status_code == 200
    r = c.post(f"/api/conversations/{cid}/messages", json={"content": "你好"}, headers=h)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "太频繁" in r.json()["detail"]


def test_rate_limit_isolated_between_users(monkeypatch, tmp_path):
    """限流按用户隔离：emp001 不受 admin 用量影响。"""
    monkeypatch.setenv("AIP_CHAT_RATE_PER_MIN", "1")
    app = create_app(str(tmp_path / "rl2.db"))
    c = SyncClient(app)
    ah = auth(login(c, "admin", "admin123"))
    eh = auth(login(c, "emp001", "emp123"))
    acid = c.post("/api/conversations", json={"assistant_id": 1}, headers=ah).json()["id"]
    ecid = c.post("/api/conversations", json={"assistant_id": 3}, headers=eh).json()["id"]
    assert c.post(f"/api/conversations/{acid}/messages", json={"content": "a"}, headers=ah).status_code == 200
    assert c.post(f"/api/conversations/{acid}/messages", json={"content": "b"}, headers=ah).status_code == 429
    # 员工在自己的配额内（助手3 = 数据分析助手，全部门可用）
    r = c.post(f"/api/conversations/{ecid}/messages", json={"content": "c"}, headers=eh)
    assert r.status_code == 200
