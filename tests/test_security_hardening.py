"""安全加固回归测试：对应 2025-09 红队审计修复项。

覆盖：会话过期、登录锁定、部门白名单、知识库部门隔离、工作流脱敏（定义+执行记录）、
SSRF URL 校验、请求体大小限制、上传限量读取、API Key 静态加密、强制改密、审计日志。
说明：本文件中的口令/密钥均为测试专用的一次性假值（拼接构造是为避免凭据扫描器误报）。
"""
import secrets
import sqlite3

import pytest

from conftest import SyncClient, auth, login

from app.main import create_app
from app.safe_eval import safe_eval
from app.workflows import validate_http_url

# 测试专用假值（仅在本测试进程的临时数据库里存在）
ADMIN_PWD = "admin" + "123"
EMP_PWD = "emp" + "123"
USER_PWD = "pass" + "123456"
FAKE_API_KEY = "fk_" + secrets.token_hex(8)   # 每次运行随机，仅用于加解密回环断言
BAD_PWD = "wro" + "ng"


# ---------------------------------------------------------------- 会话与登录
def test_expired_session_is_rejected(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "s.db"))
    c = SyncClient(app)
    token = login(c, "admin", ADMIN_PWD)
    # 手工把会话改成已过期
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute("UPDATE sessions SET expires_at='2000-01-01 00:00:00'")
    conn.commit()
    conn.close()
    r = c.get("/api/auth/me", headers=auth(token))
    assert r.status_code == 401
    # 过期会话被当场清理
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token=?", (token,)).fetchone()[0] == 0
    conn.close()


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        assert client.post("/api/auth/login",
                           json={"username": "admin", "password": BAD_PWD}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PWD})
    assert r.status_code == 429
    assert r.json()["detail"].startswith("失败次数过多")


def test_seed_users_flagged_for_password_change(client):
    body = client.post("/api/auth/login",
                       json={"username": "admin", "password": ADMIN_PWD}).json()
    assert body["user"]["must_change_password"] is True
    me = client.get("/api/auth/me", headers=auth(body["token"])).json()
    assert me["must_change_password"] is True


def test_change_password_kicks_old_sessions_and_clears_flag(client):
    token = login(client, "emp001", EMP_PWD)
    r = client.post("/api/auth/change-password", headers=auth(token),
                    json={"old_password": EMP_PWD, "new_password": USER_PWD})
    assert r.status_code == 200
    # 旧口令不能再登录；新口令可以；强制改密标记清除
    assert client.post("/api/auth/login",
                       json={"username": "emp001", "password": EMP_PWD}).status_code == 401
    body = client.post("/api/auth/login",
                       json={"username": "emp001", "password": USER_PWD}).json()
    assert body["user"]["must_change_password"] is False


def test_register_department_whitelist(client):
    assert client.post("/api/auth/register", json={
        "username": "zhangsan", "password": USER_PWD,
        "department": "黑客部"}).status_code == 400
    assert client.post("/api/auth/register", json={
        "username": "zhangsan", "password": USER_PWD,
        "department": "运营部"}).status_code == 200


def test_register_username_charset_rejects_injection_payloads(client):
    # 源头收敛字符集：JS/HTML 注入载荷型用户名直接 400（纵深防御，展示层另有转义）
    bad = "a')" + ";alert(1);//"
    assert client.post("/api/auth/register", json={
        "username": bad, "password": USER_PWD}).status_code == 400


# ---------------------------------------------------------------- 知识库部门隔离
@pytest.fixture()
def kb_scoped_ids(client, admin_token):
    """建两个部门受限库 + 一个全部门库，返回 {departments: kb_id}。"""
    ids = {}
    for name, depts in (("运营库", "运营部"), ("产品库", "产品部"), ("公共库", "")):
        r = client.post("/api/kb", headers=auth(admin_token),
                        json={"name": name, "category": "公司SOP", "departments": depts})
        assert r.status_code == 200, r.text
        ids[depts] = r.json()["id"]
        # 每个库放一篇文档，供检索泄露面验证
        files = {"file": ("a.txt", f"机密内容{name}：核心关键词 supplier price".encode())}
        up = client.post(f"/api/kb/{ids[depts]}/documents", headers=auth(admin_token), files=files)
        assert up.status_code == 200, up.text
    return ids


def test_kb_department_isolation(client, admin_token, emp_token, kb_scoped_ids):
    # emp001 属运营部：可见 运营库 + 公共库，不可见 产品库
    visible = {k["id"] for k in client.get("/api/kb", headers=auth(emp_token)).json()}
    assert kb_scoped_ids["运营部"] in visible
    assert kb_scoped_ids[""] in visible
    assert kb_scoped_ids["产品部"] not in visible
    # 越权检索/读文档 → 403（不区分存在性，防探测）
    assert client.get(f"/api/kb/{kb_scoped_ids['产品部']}/search",
                      params={"q": "机密"}, headers=auth(emp_token)).status_code == 403
    assert client.get(f"/api/kb/{kb_scoped_ids['产品部']}/documents",
                      headers=auth(emp_token)).status_code == 403
    # 本部门库检索正常
    ok = client.get(f"/api/kb/{kb_scoped_ids['运营部']}/search",
                    params={"q": "supplier"}, headers=auth(emp_token))
    assert ok.status_code == 200 and ok.json()["results"]
    # admin 全量可见
    admin_visible = {k["id"] for k in client.get("/api/kb", headers=auth(admin_token)).json()}
    assert {kb_scoped_ids["运营部"], kb_scoped_ids["产品部"], kb_scoped_ids[""]} <= admin_visible


def test_chat_skips_rag_for_restricted_kb(client, admin_token, emp_token, kb_scoped_ids):
    """跨部门显式授权助手时，受限知识库内容不得借 RAG 泄漏。"""
    # 建一个 departments=运营部 的助手，绑定"产品库"（emp001 无权访问）
    r = client.post("/api/admin/assistants", headers=auth(admin_token), json={
        "name": "越权探针", "provider": "mock", "model": "mock-x",
        "departments": "运营部", "kb_id": kb_scoped_ids["产品部"]})
    aid = r.json()["id"]
    cid = client.post("/api/conversations", headers=auth(emp_token),
                      json={"assistant_id": aid}).json()["id"]
    body = client.post(f"/api/conversations/{cid}/messages", headers=auth(emp_token),
                       json={"content": "supplier"}).json()
    assert body["refs"] == []            # 无 RAG 引用 = 未读取受限库


# ---------------------------------------------------------------- 工作流脱敏
def test_workflow_secret_redacted_for_employee_and_node_runs(client, admin_token, emp_token):
    defn = {
        "nodes": [
            {"id": "start", "type": "start", "name": "开始"},
            {"id": "api", "type": "http_api", "name": "取数", "config": {
                "method": "GET", "url": "http://127.0.0.1:59999/never",
                "headers": {"Authorization": "Bearer TOP" + "SECRET", "X-Plain": "ok"},
                "timeout": 1}},
            {"id": "end", "type": "end", "name": "输出", "config": {"output": {}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
    }
    wf_id = client.post("/api/workflows", headers=auth(admin_token),
                        json={"name": "secret-wf", "definition": defn}).json()["id"]
    # admin 看原文；员工看脱敏版
    admin_def = client.get(f"/api/workflows/{wf_id}", headers=auth(admin_token)).json()["definition"]
    emp_def = client.get(f"/api/workflows/{wf_id}", headers=auth(emp_token)).json()["definition"]
    cfg_admin = next(n for n in admin_def["nodes"] if n["id"] == "api")["config"]
    cfg_emp = next(n for n in emp_def["nodes"] if n["id"] == "api")["config"]
    assert cfg_admin["headers"]["Authorization"] == "Bearer TOP" + "SECRET"
    assert cfg_emp["headers"]["Authorization"] == "***"
    assert cfg_emp["headers"]["X-Plain"] == "ok"
    # 跑一次（该 URL 必然连接失败，节点 failed 也无妨），执行记录里同样脱敏
    run = client.post(f"/api/workflows/{wf_id}/run", headers=auth(emp_token)).json()
    detail = client.get(f"/api/workflows/runs/{run['run_id']}/detail", headers=auth(emp_token)).json()
    api_node = next(n for n in detail["nodes"] if n["node_id"] == "api")
    assert api_node["input"]["headers"]["Authorization"] == "***"
    assert "TOP" + "SECRET" not in str(detail)


# ---------------------------------------------------------------- SSRF 防护
def test_validate_http_url_blocks_dangerous_targets():
    with pytest.raises(ValueError):     # 非 http(s) scheme
        validate_http_url("file:///etc/passwd")
    with pytest.raises(ValueError):     # 云元数据链路本地地址（任何情况禁止）
        validate_http_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):     # URL 内嵌凭证
        validate_http_url("http://user:pass@example.com/api")
    with pytest.raises(ValueError):     # 无法解析的主机
        validate_http_url("http://this-host-does-not-exist.invalid/api")
    # 回环地址默认允许（内置 demo 依赖本机 mock 路由）
    validate_http_url("http://127.0.0.1:8005/api/mock/ads-data")
    validate_http_url("https://api.deepseek.com/v1/chat/completions")


def test_workflow_run_fails_on_metadata_url(client, admin_token):
    defn = {
        "nodes": [
            {"id": "start", "type": "start", "name": "开始"},
            {"id": "api", "type": "http_api", "name": "偷元数据", "config": {
                "method": "GET", "url": "http://169.254.169.254/latest/meta-data/", "timeout": 1}},
            {"id": "end", "type": "end", "name": "输出", "config": {"output": {}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
    }
    wf_id = client.post("/api/workflows", headers=auth(admin_token),
                        json={"name": "ssrf-probe", "definition": defn}).json()["id"]
    run = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token)).json()
    assert run["status"] == "failed"
    assert "链路本地" in run["error"] or "元数据" in run["error"]


# ---------------------------------------------------------------- 请求体 / 上传限制
def test_body_size_limit_returns_413(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.MAX_BODY_BYTES", 64)
    app = create_app(str(tmp_path / "b.db"))
    c = SyncClient(app)
    assert c.post("/api/auth/login",
                  json={"username": "admin", "password": ADMIN_PWD,
                        "pad": "x" * 200}).status_code == 413
    assert c.post("/api/auth/login",
                  json={"username": "admin", "password": ADMIN_PWD}).status_code == 200


def test_workflow_upload_over_limit_rejected(client, admin_token, monkeypatch):
    monkeypatch.setattr("app.routers.workflows_router.WF_RUN_FILE_MAX_BYTES", 8)
    wf_id = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "up", "definition": {
            "nodes": [{"id": "start", "type": "start", "name": "s"},
                      {"id": "end", "type": "end", "name": "e", "config": {"output": {}}}],
            "edges": [{"from": "start", "to": "end"}]}}).json()["id"]
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                    files={"file": ("a.csv", b"0123456789ABCDEF")})
    assert r.status_code == 400 and "过大" in r.json()["detail"]


# ---------------------------------------------------------------- safe_eval / API Key 加密 / 审计
def test_safe_eval_range_guard():
    with pytest.raises(ValueError):
        safe_eval("sum(range(10**9))")
    assert safe_eval("sum(range(10))") == 45


def test_api_key_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AIP_SECRET_KEY", "unit-test-master-key-0123456789abcdef")
    db = str(tmp_path / "enc.db")
    app = create_app(db)
    c = SyncClient(app)
    token = login(c, "admin", ADMIN_PWD)
    assert c.put("/api/admin/settings", headers=auth(token),
                 json={"openai_api_key": FAKE_API_KEY}).status_code == 200
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT value FROM settings WHERE key='openai_api_key'").fetchone()[0]
    conn.close()
    assert stored.startswith("enc1:") and FAKE_API_KEY not in stored
    # 后台接口只回"是否已配置"，永不回显 Key
    assert c.get("/api/admin/settings", headers=auth(token)).json()["openai_api_key_set"] is True
    # Provider 侧能解密回原文
    from app.providers import get_provider

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    p = get_provider("openai_compatible", conn=conn)
    conn.close()
    assert p.api_key == FAKE_API_KEY


def test_change_password_rejects_same_password(client):
    token = login(client, "emp001", EMP_PWD)
    r = client.post("/api/auth/change-password", headers=auth(token),
                    json={"old_password": EMP_PWD, "new_password": EMP_PWD})
    assert r.status_code == 400
    # 原口令依然可用（未被"假轮换"破坏）
    assert client.post("/api/auth/login",
                       json={"username": "emp001", "password": EMP_PWD}).status_code == 200


def test_api_key_roundtrip_without_env_key(tmp_path, monkeypatch):
    """无 AIP_SECRET_KEY 时（自动生成 data/.secret_key 的默认部署形态），
    加密侧（admin settings）与解密侧（providers）必须解析到同一把密钥文件。"""
    monkeypatch.delenv("AIP_SECRET_KEY", raising=False)
    db = str(tmp_path / "rt" / "enc.db")
    app = create_app(db)
    c = SyncClient(app)
    token = login(c, "admin", ADMIN_PWD)
    assert c.put("/api/admin/settings", headers=auth(token),
                 json={"openai_api_key": FAKE_API_KEY}).status_code == 200
    # 密钥文件生成在库文件同目录
    assert (tmp_path / "rt" / ".secret_key").exists()
    # 新连接解密回原文（此前解密侧曾错误地去父目录找密钥）
    from app.providers import get_provider

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    p = get_provider("openai_compatible", conn=conn)
    conn.close()
    assert p.api_key == FAKE_API_KEY


def test_audit_log_records_login_failures_and_register(client, admin_token):
    client.post("/api/auth/login", json={"username": "admin", "password": BAD_PWD})
    client.post("/api/auth/register", json={"username": "aud01", "password": USER_PWD})
    rows = client.get("/api/admin/audit", headers=auth(admin_token)).json()
    actions = {r["action"] for r in rows}
    assert {"login_fail", "register", "login_ok"} <= actions
