"""SSE 流式对话端点测试：事件序列、落库、降级事件、限流。"""
import json

from conftest import SyncClient, auth, login


def _parse_events(text):
    """把 SSE 响应文本解析为 [(event, data_dict)]。"""
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        ev, data = "message", None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if data is not None:
            events.append((ev, data))
    return events


def test_stream_full_flow(client, admin_token):
    h = auth(admin_token)
    cid = client.post("/api/conversations", json={"assistant_id": 1}, headers=h).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages/stream",
                    json={"content": "保温杯是什么材质"}, headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "error" not in kinds

    meta = events[0][1]
    assert meta["degraded"] is False
    assert meta["provider"] == "mock"
    assert len(meta["refs"]) >= 1          # 助手1绑定了SOP知识库，RAG引用随 meta 下发

    deltas = "".join(d["text"] for k, d in events if k == "delta")
    assert deltas.strip()

    done = next(d for k, d in events if k == "done")
    assert done["usage"]["completion_tokens"] > 0
    assert done["latency_ms"] >= 0

    # 消息已落库：一轮 = user + assistant，且内容与流式拼接一致
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=h).json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == deltas
    assert msgs[1]["refs"][0]["filename"] == meta["refs"][0]["filename"]


def test_stream_audit_recorded(client, admin_token):
    h = auth(admin_token)
    cid = client.post("/api/conversations", json={"assistant_id": 1}, headers=h).json()["id"]
    client.post(f"/api/conversations/{cid}/messages/stream",
                json={"content": "FBA 外箱要求"}, headers=h)
    stats = client.get("/api/admin/stats", headers=h).json()["llm"]
    assert stats["calls"] >= 1
    assert stats["success"] >= 1


def test_stream_error_when_provider_misconfigured(client, admin_token, monkeypatch):
    """未配置的 openai_compatible：流式路径以 error 事件收尾且不落库（502 语义的对等物）。"""
    h = auth(admin_token)
    aid = client.post("/api/admin/assistants", headers=h, json={
        "name": "坏配置助手", "provider": "openai_compatible", "model": "gpt-4o-mini",
        "kb_id": None, "departments": ""}).json()["id"]
    cid = client.post("/api/conversations", json={"assistant_id": aid}, headers=h).json()["id"]
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.post(f"/api/conversations/{cid}/messages/stream",
                    json={"content": "hi"}, headers=h)
    assert r.status_code == 200   # SSE 本身建连成功
    events = _parse_events(r.text)
    assert [k for k, _ in events][-1] == "error"
    assert "未配置" in next(d for k, d in events if k == "error")["message"]
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=h).json()
    assert msgs == []             # 未落库


def test_stream_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("AIP_CHAT_RATE_PER_MIN", "1")
    from app.main import create_app
    app = create_app(str(tmp_path / "srl.db"))
    c = SyncClient(app)
    h = auth(login(c, "admin", "admin123"))
    cid = c.post("/api/conversations", json={"assistant_id": 1}, headers=h).json()["id"]
    assert c.post(f"/api/conversations/{cid}/messages/stream",
                  json={"content": "a"}, headers=h).status_code == 200
    r = c.post(f"/api/conversations/{cid}/messages/stream", json={"content": "b"}, headers=h)
    assert r.status_code == 429
