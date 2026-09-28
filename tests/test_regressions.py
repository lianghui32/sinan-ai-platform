"""深度回归测试：本轮排查出的 bug 与修复的针对性用例。

覆盖：会话 updated_at 每轮刷新（列表排序）、运行列表不被他人挤掉、
http_api 查询参数的百分号编码与密钥脱敏、模板 {{路径|默认值}} 语法（含 |[] 列表默认值不得褪成字符串）、
内置广告演示不传参数也能跑、节点 id 保留字、危险表达式在保存期即拒绝、
运行记录不卡 running、上传体积硬上限、params 必须是 JSON 对象、
后台改密码的口令强度一致性、显式分配的助手必须存在、后台读不存在会话 404、会话归属校验。
"""
import io
import json
import sqlite3

import pytest

from app.workflows import (
    WorkflowDefinitionError,
    quote_url,
    redact_url,
    validate_definition,
    validate_expressions,
)
from conftest import auth, login

SIMPLE_DEF = {
    "nodes": [{"id": "start", "type": "start"},
              {"id": "end", "type": "end", "config": {"output": {"ok": 1}}}],
    "edges": [{"from": "start", "to": "end"}],
}


def _assistant_id(client, token, name):
    return next(a["id"] for a in client.get("/api/assistants", headers=auth(token)).json()
                if a["name"] == name)


def _create_wf(client, token, defn, name="测试流"):
    r = client.post("/api/workflows", headers=auth(token), json={"name": name, "definition": defn})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _run(client, token, wf_id, params=None, csv_name=None, csv_bytes=None):
    data = {"params": json.dumps(params or {}, ensure_ascii=False)}
    files = None
    if csv_bytes is not None:
        files = {"file": (csv_name or "s.csv", io.BytesIO(csv_bytes), "text/csv")}
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(token), data=data, files=files)
    return r


# ---------------------------------------------------------------- 会话排序
def test_conversation_updated_at_bumps_on_every_message(client, emp_token, tmp_path):
    """修复：标题只在首问改名，updated_at 之前被同一条 SQL 的标题条件挡住，
    导致"我的会话"按 updated_at 倒序时活跃会话不冒泡。"""
    aid = _assistant_id(client, emp_token, "数据分析助手")
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": aid}).json()
    for q in ("第一问AAA", "第二问BBB"):
        assert client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                           json={"content": q}).status_code == 200

    db = sqlite3.connect(str(tmp_path / "test.db"))
    db.execute("UPDATE conversations SET updated_at='2020-01-01 00:00:00' WHERE id=?", (conv["id"],))
    db.commit()
    db.close()

    assert client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                       json={"content": "第三问CCC"}).status_code == 200

    row = next(c for c in client.get("/api/conversations", headers=auth(emp_token)).json()
               if c["id"] == conv["id"])
    assert row["updated_at"] > "2020-01-01 00:00:00", "每轮对话都应刷新 updated_at"
    assert row["title"].startswith("第一问AAA"), "标题保持首问命名，不被后续消息覆盖"


def test_message_history_keeps_turn_order(client, emp_token):
    aid = _assistant_id(client, emp_token, "数据分析助手")
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": aid}).json()
    for q in ("甲", "乙"):
        client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token),
                    json={"content": q})
    msgs = client.get(f"/api/conversations/{conv['id']}/messages", headers=auth(emp_token)).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 2
    assert msgs[-1]["content"].count("第2轮") == 1


# ---------------------------------------------------------------- 运行列表
def test_employee_run_list_not_starved_by_other_users(client, admin_token, emp_token):
    """修复：员工运行列表原先取全局最新 50 条再按人过滤，热门工作流会把自己挤没。"""
    wf_id = _create_wf(client, admin_token, SIMPLE_DEF, "最简流")
    mine = _run(client, emp_token, wf_id).json()["run_id"]
    for _ in range(55):
        _run(client, admin_token, wf_id)
    runs = client.get(f"/api/workflows/{wf_id}/runs", headers=auth(emp_token)).json()
    assert [r["id"] for r in runs] == [mine]
    admin_runs = client.get(f"/api/workflows/{wf_id}/runs", headers=auth(admin_token)).json()
    assert len(admin_runs) == 50 and all(r["user_id"] == 1 for r in admin_runs)


# ---------------------------------------------------------------- http_api 编码
def test_http_api_node_params_are_percent_encoded(client, admin_token, live_server):
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "api", "type": "http_api", "config": {
                "method": "GET", "url": live_server + "/api/mock/ads-data",
                "params": {"campaign": "{{input.params.campaign}}"}}},
            {"id": "end", "type": "end", "config": {
                "output": {"campaign": "{{api.data.campaign}}", "url": "{{api.url}}"}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
    }
    wf_id = _create_wf(client, admin_token, defn, "广告参数编码")
    tricky = "双11 & 618#大促 汇总"
    r = _run(client, admin_token, wf_id, {"campaign": tricky})
    body = r.json()
    assert body["status"] == "success", body["error"]
    assert body["output"]["campaign"] == tricky, "params 必须完整round-trip，含 & # 空格"
    recorded = body["output"]["url"]
    assert " " not in recorded and "%26" in recorded, "空格与 & 必须被转义后再发送"


def test_http_api_inline_url_template_escapes_space(client, admin_token, live_server):
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "api", "type": "http_api", "config": {
                "method": "GET",
                "url": live_server + "/api/mock/inventory?sku={{input.params.sku}}"}},
            {"id": "end", "type": "end", "config": {"output": {"sku": "{{api.data.sku}}"}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
    }
    wf_id = _create_wf(client, admin_token, defn, "内联URL模板")
    body = _run(client, admin_token, wf_id, {"sku": "保温杯 A-01"}).json()
    assert body["status"] == "success", body["error"]
    assert body["output"]["sku"] == "保温杯 A-01"


def test_http_api_url_secret_redacted_in_node_record(client, admin_token, live_server):
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "api", "type": "http_api", "config": {
                "method": "GET",
                "url": live_server + "/api/mock/ads-data?api_key=SEC3T&campaign=x"}},
            {"id": "end", "type": "end", "config": {"output": {"done": True}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
    }
    wf_id = _create_wf(client, admin_token, defn, "带密钥的API")
    body = _run(client, admin_token, wf_id).json()
    detail = client.get(f"/api/workflows/runs/{body['run_id']}/detail", headers=auth(admin_token)).json()
    api_node = next(n for n in detail["nodes"] if n["node_id"] == "api")
    assert "SEC3T" not in json.dumps(api_node["output"]), "密钥不能进节点执行记录"
    assert "api_key=***" in api_node["output"]["url"]


def test_quote_url_and_redact_url_helpers():
    assert quote_url("http://x/a b/中文?k=值 z") == \
        "http://x/a%20b/%E4%B8%AD%E6%96%87?k=%E5%80%BC%20z"
    assert quote_url("http://x/y?kept=%E5%B7%B2%E7%BC%96%E7%A0%81") == \
        "http://x/y?kept=%E5%B7%B2%E7%BC%96%E7%A0%81"          # 不二次编码
    assert redact_url("http://h?api_key=abc&x=1").endswith("api_key=***&x=1")
    assert redact_url("http://h/path") == "http://h/path"


def test_template_default_value_syntax():
    """{{路径|默认值}}：可选运行参数不必再靠"插成空串"糊过去。"""
    from app.workflows import render_template

    assert render_template("{{a.b|默认}}", {}) == "默认"
    assert render_template("{{a.b|5}}", {}) == 5                       # 数字默认值保类型
    assert render_template("{{a.b|true}}", {}) is True
    assert render_template("{{a.b|null}}", {}) is None
    assert render_template("前缀 {{a.b|7}} 后缀", {}) == "前缀 7 后缀"
    assert render_template("{{a.b}}", {"a": {"b": {"k": 1}}}) == {"k": 1}
    assert render_template("{{a.b|忽略}}", {"a": {"b": 9}}) == 9        # 有值时默认值失效
    # 列表/带引号字符串默认值：不能退化成字面文本，否则 len("[]") == 2 会骗过条件判断
    assert render_template("{{a.rows|[]}}", {}) == []
    assert render_template('{{a.x|["保温杯","榨汁杯"]}}', {}) == ["保温杯", "榨汁杯"]
    assert render_template('{{a.x|"双11大促"}}', {}) == "双11大促"
    assert render_template("{{a.x|非数字}}", {}) == "非数字"             # 普通词保持文本
    assert render_template("{{a.x|nan}}", {}) == "nan"                  # 不被 float() 吃成 NaN
    assert render_template("{{a.x|inf}}", {}) == "inf"
    assert render_template("{{a.x|-1.5}}", {}) == -1.5
    # 没写默认值时行为不变：整串模板报错、插值降级为空串
    with pytest.raises(KeyError):
        render_template("{{a.b}}", {})
    assert render_template("值:{{a.b}}", {}) == "值:"


def test_list_default_keeps_condition_branch_correct(client, admin_token):
    """路径缺失时用 {{路径|[]}} 兜底：默认值必须是真空列表。
    修复前 '[]' 会退化成两个字符，len(rows) > 0 因此成立，无数据也走 true 分支，
    紧接着下游 sum(r['v'] for r in rows) 拿到字符串直接抛错，整条运行 failed。"""
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "check", "type": "condition", "config": {
                "expression": "len(rows) > 0", "vars": {"rows": "{{input.params.files|[]}}"}}},
            {"id": "calc", "type": "code", "config": {
                "vars": {"rows": "{{input.params.files|[]}}"},
                "expression": "sum(r['v'] for r in rows)"}},
            {"id": "has_end", "type": "end", "config": {"output": {"total": "{{calc.result}}"}}},
            {"id": "no_end", "type": "end", "config": {"output": {"error": "没有数据行"}}},
        ],
        "edges": [{"from": "start", "to": "check"},
                  {"from": "check", "to": "calc", "condition": "true"},
                  {"from": "calc", "to": "has_end"},
                  {"from": "check", "to": "no_end", "condition": "false"}],
    }
    wf_id = _create_wf(client, admin_token, defn, "空列表默认值")

    # params 里没有 files → 取默认值 [] → 走 false 分支（而不是 failed）
    body = _run(client, admin_token, wf_id).json()
    assert body["status"] == "success", body["error"]
    assert body["order"] == ["start", "check", "no_end"]
    assert body["output"] == {"error": "没有数据行"}

    # 传了列表 → 走 true 分支并正常聚合
    body = _run(client, admin_token, wf_id, {"files": [{"v": 3}, {"v": 9}]}).json()
    assert body["status"] == "success", body["error"]
    assert body["order"] == ["start", "check", "calc", "has_end"]
    assert body["output"] == {"total": 12}


def test_builtin_ad_workflow_uses_params_with_default(client, admin_token, live_server):
    """内置广告工作流改为 params + 默认值：不传参数能跑，特殊字符不丢。"""
    wfs = client.get("/api/workflows", headers=auth(admin_token)).json()
    wf = next(w for w in wfs if w["name"].startswith("广告数据分析"))["id"]

    body = _run(client, admin_token, wf).json()
    assert body["status"] == "success", body["error"]
    assert body["output"]["metrics"]["campaign"] == "双11大促"     # 未传参数 → 默认值
    assert body["output"]["analysis"]["acos_pct"] == 27.86

    tricky = "双11 & 618#大促 汇总"
    body = _run(client, admin_token, wf, {"campaign": tricky}).json()
    assert body["status"] == "success", body["error"]
    assert body["output"]["metrics"]["campaign"] == tricky          # & # 空格 原样送达


# ---------------------------------------------------------------- 定义校验
def test_reserved_node_id_input_rejected(client, admin_token):
    defn = {"nodes": [{"id": "start", "type": "start"},
                      {"id": "input", "type": "code", "config": {"expression": "1"}},
                      {"id": "end", "type": "end", "config": {"output": {"v": "{{input.params.x}}"}}}],
            "edges": [{"from": "start", "to": "input"}, {"from": "input", "to": "end"}]}
    with pytest.raises(WorkflowDefinitionError):
        validate_definition(defn)
    r = client.post("/api/workflows", headers=auth(admin_token), json={"name": "保留字", "definition": defn})
    assert r.status_code == 400 and "input" in r.json()["detail"]


def test_dangerous_edge_condition_rejected_on_save(client, admin_token):
    defn = {"nodes": [{"id": "start", "type": "start"},
                      {"id": "c", "type": "condition", "config": {"expression": "1 > 0", "vars": {}}},
                      {"id": "e", "type": "end", "config": {"output": {"k": 1}}}],
            "edges": [{"from": "start", "to": "c"},
                      {"from": "c", "to": "e", "condition": "x.__class__ is not None"}]}
    with pytest.raises(WorkflowDefinitionError):
        validate_expressions(defn)
    r = client.post("/api/workflows", headers=auth(admin_token), json={"name": "危险边", "definition": defn})
    assert r.status_code == 400 and "条件表达式" in r.json()["detail"]


def test_safe_expressions_still_save(client, admin_token):
    """业务里合法的表达式（含属性外的推导式/带 key 的 max）不能被误伤。"""
    defn = {"nodes": [
        {"id": "start", "type": "start"},
        {"id": "calc", "type": "code", "config": {
            "vars": {"rows": "{{start.rows}}"},
            "expression": "{'n': len(rows), 'top': max(rows, key=lambda r: r['v'])['v']}"}},
        {"id": "c", "type": "condition", "config": {"expression": "len(rows) > 0", "vars": {"rows": "{{start.rows}}"}}},
        {"id": "e", "type": "end", "config": {"output": {"r": "{{calc.result}}"}}}],
        "edges": [{"from": "start", "to": "calc"}, {"from": "calc", "to": "c"},
                  {"from": "c", "to": "e", "condition": "true"}]}
    wf_id = _create_wf(client, admin_token, defn, "安全表达式")
    body = _run(client, admin_token, wf_id, csv_name="v.csv",
                csv_bytes="v\n3\n9\n1\n".encode("utf-8")).json()
    assert body["status"] == "success", body["error"]
    assert body["output"]["r"] == {"n": 3, "top": 9}


def test_run_never_left_stuck_in_running(client, admin_token, tmp_path):
    """定义在执行前就炸（历史脏数据/手工改坏）时，运行记录必须落到终态而不是永远 running。"""
    wf_id = _create_wf(client, admin_token, SIMPLE_DEF, "会被改坏")
    db = sqlite3.connect(str(tmp_path / "test.db"))
    broken = {"nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "code",
                                                                "config": {"expression": "1"}}],
              "edges": [{"from": "start", "to": "a"}, {"from": "a", "to": "start"}]}
    db.execute("UPDATE workflows SET definition_json=? WHERE id=?",
               (json.dumps(broken, ensure_ascii=False), wf_id))
    db.commit()
    db.close()

    r = _run(client, admin_token, wf_id)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and "环" in body["error"]
    row = client.get(f"/api/workflows/runs/{body['run_id']}/detail", headers=auth(admin_token)).json()
    assert row["status"] == "failed" and row["finished_at"], "运行记录必须写入结束时间"


# ---------------------------------------------------------------- 入参/上传约束
def test_run_params_must_be_json_object(client, admin_token):
    wf_id = _create_wf(client, admin_token, SIMPLE_DEF, "参数校验")
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                    data={"params": "[1,2]"})
    assert r.status_code == 400 and "JSON 对象" in r.json()["detail"]
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                    data={"params": "不是JSON"})
    assert r.status_code == 400


def test_oversized_uploads_rejected(client, admin_token):
    kb_id = client.get("/api/kb", headers=auth(admin_token)).json()[0]["id"]
    big = "知".encode("utf-8") * (3 * 1024 * 1024)
    r = client.post(f"/api/kb/{kb_id}/documents", headers=auth(admin_token),
                    files={"file": ("大文档.txt", io.BytesIO(big), "text/plain")})
    assert r.status_code == 400 and "过大" in r.json()["detail"]

    wf_id = _create_wf(client, admin_token, SIMPLE_DEF, "CSV上限")
    r = _run(client, admin_token, wf_id, csv_bytes=b"x" * (6 * 1024 * 1024))
    assert r.status_code == 400 and "过大" in r.json()["detail"]

    # 只含空白的文档不给建空索引
    r = client.post(f"/api/kb/{kb_id}/documents", headers=auth(admin_token),
                    files={"file": ("空白.txt", io.BytesIO(b"  \n\t "), "text/plain")})
    assert r.status_code == 400


# ---------------------------------------------------------------- 后台策略一致性
def test_admin_password_reset_requires_min_length(client, admin_token):
    import secrets

    new_pwd = "Rst" + secrets.token_hex(6)   # 运行时随机，避免固定测试口令
    uid = client.post("/api/admin/users", headers=auth(admin_token), json={
        "username": "pwcheck", "password": ("pass" + "123456"), "department": "运营部"}).json()["id"]
    assert client.put(f"/api/admin/users/{uid}", headers=auth(admin_token),
                      json={"password": "123"}).status_code == 422
    assert client.put(f"/api/admin/users/{uid}", headers=auth(admin_token),
                      json={"password": new_pwd}).status_code == 200
    assert login(client, "pwcheck", new_pwd)
    # 不带 password 字段 = 不改密码
    assert client.put(f"/api/admin/users/{uid}", headers=auth(admin_token),
                      json={"department": "产品部"}).status_code == 200
    assert login(client, "pwcheck", new_pwd)


def test_granting_unknown_assistant_is_rejected(client, admin_token):
    r = client.post("/api/admin/users", headers=auth(admin_token), json={
        "username": "grantx", "password": "pass123456", "assistant_ids": [99998]})
    assert r.status_code == 400 and "助手不存在" in r.json()["detail"]

    uid = client.post("/api/admin/users", headers=auth(admin_token), json={
        "username": "grantok", "password": "pass123456", "department": "财务部",
        "assistant_ids": [_assistant_id(client, admin_token, "数据分析助手")]}).json()["id"]
    assert client.put(f"/api/admin/users/{uid}", headers=auth(admin_token),
                      json={"assistant_ids": [12345]}).status_code == 400
    row = next(u for u in client.get("/api/admin/users", headers=auth(admin_token)).json()
               if u["id"] == uid)
    assert row["assistant_ids"] != [12345], "拒绝的编辑不应改动已有授权"


def test_admin_conversation_messages_404_for_missing(client, admin_token):
    assert client.get("/api/admin/conversations/88888/messages",
                      headers=auth(admin_token)).status_code == 404


def test_employee_cannot_post_into_another_conversation(client, emp_token):
    client.post("/api/auth/register", json={"username": "peer01", "password": "pass123456",
                                            "department": "运营部"})
    peer = login(client, "peer01", "pass123456")
    aid = _assistant_id(client, emp_token, "数据分析助手")
    conv = client.post("/api/conversations", headers=auth(emp_token),
                       json={"assistant_id": aid}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(peer),
                    json={"content": "蹭别人的会话"})
    assert r.status_code == 403
    assert client.get(f"/api/conversations/{conv['id']}/messages", headers=auth(peer)).status_code == 403
