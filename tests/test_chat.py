"""对话：多轮会话、RAG 知识注入与引用来源、Mock 模型确定性、会话管理。"""
from conftest import auth, login


def _get_assistant(client, token, name):
    return next(a for a in client.get("/api/assistants", headers=auth(token)).json() if a["name"] == name)


def test_chat_with_knowledge_references(client, emp_token):
    a = _get_assistant(client, emp_token, "亚马逊运营助手")
    assert a["kb_name"] and "公司SOP" in a["kb_name"]

    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": a["id"]}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                    json={"content": "FBA发货时外箱标签和重量有什么要求？"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]
    # 绑定了知识库 → 回复附带引用来源
    assert body["refs"], "应检索到SOP知识并附引用"
    assert any("亚马逊运营SOP手册.txt" in ref["filename"] for ref in body["refs"])
    joined = " ".join(ref["text"] for ref in body["refs"])
    assert "22.7" in joined or "热转印" in joined
    # Mock 回复中声明结合了知识库
    assert "企业知识库" in body["reply"]


def test_chat_multi_turn_and_history(client, emp_token):
    a = _get_assistant(client, emp_token, "数据分析助手")  # 未绑定知识库
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": a["id"]}).json()
    for q in ("帮我解读5月销售环比增长", "ACOS上升说明什么？", "库存周转天数如何计算？"):
        r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                        json={"content": q})
        assert r.status_code == 200
        assert r.json()["refs"] == []      # 未绑定知识库 → 无引用

    msgs = client.get(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token)).json()
    assert len(msgs) == 6
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    # Mock 模型能感知多轮（回复中标注第N轮）
    assert "第3轮" in msgs[-1]["content"]
    # 标题取首条消息
    convs = client.get("/api/conversations", headers=auth(emp_token)).json()
    assert next(c for c in convs if c["id"] == conv["id"])["title"].startswith("帮我解读5月销售")


def test_mock_provider_is_deterministic(client, emp_token):
    a = _get_assistant(client, emp_token, "数据分析助手")
    replies = []
    for _ in range(2):
        conv = client.post("/api/conversations", headers=auth(emp_token),
                           json={"assistant_id": a["id"]}).json()
        r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                        json={"content": "同样的问题：什么是ACOS？"})
        replies.append(r.json()["reply"])
    assert replies[0] == replies[1], "MockProvider 必须对相同输入返回相同输出"


def test_conversation_lifecycle(client, emp_token):
    a = _get_assistant(client, emp_token, "数据分析助手")
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": a["id"]}).json()
    assert client.delete(f"/api/conversations/{conv['id']}", headers=auth(emp_token)).status_code == 200
    msgs = client.get(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token))
    assert msgs.status_code == 404
    # 用不存在的助手建会话
    assert client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": 99999}).status_code == 404


def test_admin_views_all_conversations(client, admin_token, emp_token):
    a = _get_assistant(client, emp_token, "数据分析助手")
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": a["id"]}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                json={"content": "员工提问内容XYZ"})

    convs = client.get("/api/admin/conversations", headers=auth(admin_token)).json()
    row = next(c for c in convs if c["id"] == conv["id"])
    assert row["username"] == "emp001" and row["message_count"] == 2

    msgs = client.get(f"/api/admin/conversations/{conv['id']}/messages", headers=auth(admin_token)).json()
    assert any(m["content"] == "员工提问内容XYZ" for m in msgs)

    # 按用户过滤
    users = client.get("/api/admin/users", headers=auth(admin_token)).json()
    emp_id = next(u["id"] for u in users if u["username"] == "emp001")
    filtered = client.get("/api/admin/conversations", headers=auth(admin_token),
                          params={"user_id": emp_id}).json()
    assert filtered and all(c["user_id"] == emp_id for c in filtered)
