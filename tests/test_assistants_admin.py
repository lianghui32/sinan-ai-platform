"""助手配置 CRUD（admin）、模型 Provider 切换。"""
from conftest import auth


def _create(client, token, **over):
    body = {"name": "供应链助手", "description": "供应链与库存", "system_prompt": "你是供应链专家。",
            "kb_id": None, "provider": "mock", "model": "mock-scm", "departments": "运营部"}
    body.update(over)
    r = client.post("/api/admin/assistants", headers=auth(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_employee_cannot_manage_assistants(client, emp_token):
    assert client.post("/api/admin/assistants", headers=auth(emp_token),
                       json={"name": "x"}).status_code == 403


def test_assistant_crud(client, admin_token, emp_token):
    # 创建：运营部员工立即可见（后续加供应链/财务助手 = 加一条配置）
    aid = _create(client, admin_token)
    names = {a["name"] for a in client.get("/api/assistants", headers=auth(emp_token)).json()}
    assert "供应链助手" in names

    # 读取
    r = client.get(f"/api/assistants/{aid}", headers=auth(emp_token))
    assert r.status_code == 200
    assert r.json()["provider"] == "mock" and r.json()["model"] == "mock-scm"

    # 更新：换 provider/model、绑定知识库、改部门
    kbs = client.get("/api/kb", headers=auth(admin_token)).json()
    kb_id = kbs[0]["id"]
    r = client.put(f"/api/admin/assistants/{aid}", headers=auth(admin_token), json={
        "name": "供应链助手Pro", "description": "升级版", "system_prompt": "新的提示词",
        "kb_id": kb_id, "provider": "openai_compatible", "model": "deepseek-chat",
        "departments": "财务部"})
    assert r.status_code == 200
    detail = client.get(f"/api/assistants/{aid}", headers=auth(admin_token)).json()
    assert detail["provider"] == "openai_compatible"
    assert detail["model"] == "deepseek-chat"
    assert detail["kb_id"] == kb_id
    # 运营部员工不再可见
    names = {a["name"] for a in client.get("/api/assistants", headers=auth(emp_token)).json()}
    assert "供应链助手Pro" not in names

    # 非法 provider / 不存在的知识库
    assert client.put(f"/api/admin/assistants/{aid}", headers=auth(admin_token), json={
        "name": "x", "provider": "bad_provider", "model": "m", "departments": ""}).status_code == 400
    assert client.post("/api/admin/assistants", headers=auth(admin_token), json={
        "name": "y", "kb_id": 99999, "provider": "mock", "model": "m", "departments": ""}).status_code == 400

    # 删除
    assert client.delete(f"/api/admin/assistants/{aid}", headers=auth(admin_token)).status_code == 200
    assert client.get(f"/api/assistants/{aid}", headers=auth(admin_token)).status_code == 404


def test_openai_provider_without_config_fails_gracefully(client, admin_token, emp_token):
    """切到 openai_compatible 但未配置 base_url/api_key 时，对话返回 502 而不是崩溃。"""
    aid = _create(client, admin_token, name="外部模型助手", departments="")
    client.put(f"/api/admin/assistants/{aid}", headers=auth(admin_token), json={
        "name": "外部模型助手", "description": "", "system_prompt": "你是助手",
        "kb_id": None, "provider": "openai_compatible", "model": "gpt-4o-mini", "departments": ""})
    conv = client.post("/api/conversations", headers=auth(emp_token), json={"assistant_id": aid}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                    json={"content": "你好"})
    assert r.status_code == 502
    assert "模型调用失败" in r.json()["detail"]

    # 后台可查看/修改 Provider 设置
    s = client.get("/api/admin/settings", headers=auth(admin_token)).json()
    assert set(s["providers"]) == {"mock", "openai_compatible"}
    assert client.put("/api/admin/settings", headers=auth(admin_token), json={
        "openai_base_url": "https://api.example.com/v1", "openai_model": "some-model"}).status_code == 200
    s = client.get("/api/admin/settings", headers=auth(admin_token)).json()
    assert s["openai_base_url"] == "https://api.example.com/v1"


def test_builtin_assistants_present(client, admin_token):
    rows = client.get("/api/admin/assistants", headers=auth(admin_token)).json()
    builtin = {r["name"] for r in rows if r["is_builtin"]}
    assert builtin == {"亚马逊运营助手", "产品开发助手", "数据分析助手"}
