"""注册登录、角色权限、部门助手可见性、员工数据隔离。"""
from conftest import auth, login


def test_register_and_login(client):
    r = client.post("/api/auth/register", json={
        "username": "zhangsan", "password": "pass123456",
        "department": "产品部", "display_name": "张三"})
    assert r.status_code == 200
    assert r.json()["role"] == "employee"

    r = client.post("/api/auth/login", json={"username": "zhangsan", "password": "pass123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["department"] == "产品部"
    assert len(body["token"]) == 64


def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpass"})
    assert r.status_code == 401


def test_register_duplicate_username(client):
    r = client.post("/api/auth/register", json={"username": "admin", "password": "pass123456"})
    assert r.status_code == 400


def test_me_and_unauthorized(client, emp_token):
    assert client.get("/api/auth/me").status_code == 401
    r = client.get("/api/auth/me", headers=auth(emp_token))
    assert r.status_code == 200
    assert r.json()["username"] == "emp001"
    assert r.json()["role"] == "employee"


def test_employee_cannot_access_admin_api(client, emp_token):
    for url in ("/api/admin/users", "/api/admin/stats", "/api/admin/assistants",
                "/api/admin/conversations", "/api/admin/settings"):
        assert client.get(url, headers=auth(emp_token)).status_code == 403, url
    r = client.post("/api/admin/users", headers=auth(emp_token), json={
        "username": "hacker", "password": "pass123456"})
    assert r.status_code == 403


def test_admin_can_create_employee_and_grant_assistants(client, admin_token):
    # 建员工并显式分配助手2（产品开发助手，部门不匹配也能用）
    r = client.post("/api/admin/users", headers=auth(admin_token), json={
        "username": "lisi", "password": "pass123456", "role": "employee",
        "department": "客服部", "display_name": "李四", "assistant_ids": [2]})
    assert r.status_code == 200
    tok = login(client, "lisi", "pass123456")
    r = client.get("/api/assistants", headers=auth(tok))
    ids = {a["id"] for a in r.json()}
    assert 2 in ids          # 显式分配
    assert 1 not in ids      # 运营部助手，部门不匹配且未分配

    # 修改部门后按部门规则可见
    users = client.get("/api/admin/users", headers=auth(admin_token)).json()
    lisi = next(u for u in users if u["username"] == "lisi")
    r = client.put(f"/api/admin/users/{lisi['id']}", headers=auth(admin_token),
                   json={"department": "运营部", "assistant_ids": []})
    assert r.status_code == 200
    ids = {a["id"] for a in client.get("/api/assistants", headers=auth(tok)).json()}
    assert 1 in ids and 2 not in ids


def test_department_based_assistant_visibility(client, admin_token, emp_token):
    names = {a["name"] for a in client.get("/api/assistants", headers=auth(emp_token)).json()}
    assert "亚马逊运营助手" in names    # 运营部可用
    assert "数据分析助手" in names      # 全部门可用
    assert "产品开发助手" not in names  # 产品部/开发部专用

    all_names = {a["name"] for a in client.get("/api/assistants", headers=auth(admin_token)).json()}
    assert all_names == {"亚马逊运营助手", "产品开发助手", "数据分析助手"}

    # 直接访问无权限助手详情 → 403
    assts = client.get("/api/assistants", headers=auth(admin_token)).json()
    dev_id = next(a["id"] for a in assts if a["name"] == "产品开发助手")
    assert client.get(f"/api/assistants/{dev_id}", headers=auth(emp_token)).status_code == 403


def test_conversation_data_isolation(client, emp_token):
    # 第二个员工
    client.post("/api/auth/register", json={"username": "wangwu", "password": "pass123456", "department": "运营部"})
    tok_b = login(client, "wangwu", "pass123456")

    assts_a = client.get("/api/assistants", headers=auth(emp_token)).json()
    aid = assts_a[0]["id"]
    conv_a = client.post("/api/conversations", headers=auth(emp_token), json={"assistant_id": aid}).json()
    client.post(f"/api/conversations/{conv_a['id']}/messages", headers=auth(emp_token),
                json={"content": "A的私密问题"})

    conv_b = client.post("/api/conversations", headers=auth(tok_b), json={"assistant_id": aid}).json()

    # 各自只能看到自己的会话
    ids_a = {c["id"] for c in client.get("/api/conversations", headers=auth(emp_token)).json()}
    ids_b = {c["id"] for c in client.get("/api/conversations", headers=auth(tok_b)).json()}
    assert conv_a["id"] in ids_a and conv_b["id"] not in ids_a
    assert conv_b["id"] in ids_b and conv_a["id"] not in ids_b

    # B 访问 A 的会话 → 403
    assert client.get(f"/api/conversations/{conv_a['id']}/messages", headers=auth(tok_b)).status_code == 403
    r = client.post(f"/api/conversations/{conv_a['id']}/messages", headers=auth(tok_b), json={"content": "hi"})
    assert r.status_code == 403
    assert client.delete(f"/api/conversations/{conv_a['id']}", headers=auth(tok_b)).status_code == 403


def test_logout_invalidates_token(client, emp_token):
    assert client.post("/api/auth/logout", headers=auth(emp_token)).status_code == 200
    assert client.get("/api/auth/me", headers=auth(emp_token)).status_code == 401
