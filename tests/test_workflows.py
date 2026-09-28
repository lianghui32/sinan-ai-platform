"""工作流引擎：安全表达式、DAG校验、各节点类型、条件分支、CSV销售分析端到端。"""
import json

import pytest

from app.safe_eval import UnsafeExpressionError, safe_eval
from app.workflows import (
    WorkflowDefinitionError,
    execute_workflow,
    parse_csv_text,
    validate_definition,
)
from conftest import auth, login, make_csv


# ---------------------------------------------------------------- safe_eval
class TestSafeEval:
    def test_allowed_expressions(self):
        assert safe_eval("1 + 2 * 3") == 7
        assert safe_eval("len(rows)", {"rows": [1, 2, 3]}) == 3
        assert safe_eval("[x * 2 for x in range(3)]") == [0, 2, 4]
        assert safe_eval("max(pairs, key=lambda p: p[1])", {"pairs": [("a", 1), ("b", 9)]}) == ("b", 9)
        assert safe_eval("{'sum': sum(v), 'avg': round(sum(v) / len(v), 2)}", {"v": [1, 2, 4]}) == \
            {"sum": 7, "avg": 2.33}
        assert safe_eval("a if a > b else b", {"a": 3, "b": 5}) == 5
        assert safe_eval("rows[0]['sales'] > 100", {"rows": [{"sales": 120.5}]}) is True

    @pytest.mark.parametrize("expr", [
        "__import__('os').system('dir')",          # 下划线名称 + 属性访问
        "open('secret.txt')",                       # 非白名单函数
        "x.__class__",                              # 属性访问
        "().__class__.__bases__",                   # 属性链
        "rows.clear()",                             # 方法调用（属性）
        "eval('1+1')",                              # 非白名单函数
        "exec('import os')",                        # 非白名单函数
        "_secret",                                  # 下划线变量名
        "9 ** 9 ** 9",                              # 资源耗尽
        "2 ** 100000",                              # 指数过大
        "import os",                                # 语句而非表达式（语法错误）
        "a := 5",                                   # 海象赋值（非白名单节点）
    ])
    def test_rejects_dangerous(self, expr):
        with pytest.raises((UnsafeExpressionError, ValueError)):
            safe_eval(expr, {"x": 1, "rows": [1, 2], "a": 1, "b": 2, "_secret": 1, "pairs": []})

    def test_runtime_error_wrapped(self):
        with pytest.raises(ValueError):
            safe_eval("1 / 0")
        with pytest.raises(ValueError):
            safe_eval("undefined_var + 1")


# ---------------------------------------------------------------- DAG 校验
class TestValidation:
    def test_rejects_cycle(self):
        with pytest.raises(WorkflowDefinitionError):
            validate_definition({"nodes": [
                {"id": "s", "type": "start"}, {"id": "a", "type": "code"}, {"id": "b", "type": "code"}],
                "edges": [{"from": "s", "to": "a"}, {"from": "a", "to": "b"}, {"from": "b", "to": "a"}]})

    def test_rejects_bad_defs(self):
        with pytest.raises(WorkflowDefinitionError):
            validate_definition({"nodes": [], "edges": []})
        with pytest.raises(WorkflowDefinitionError):  # 没有 start
            validate_definition({"nodes": [{"id": "a", "type": "code"}], "edges": []})
        with pytest.raises(WorkflowDefinitionError):  # 非法节点类型
            validate_definition({"nodes": [{"id": "s", "type": "start"}, {"id": "x", "type": "shell"}],
                                 "edges": []})
        with pytest.raises(WorkflowDefinitionError):  # 边引用不存在的节点
            validate_definition({"nodes": [{"id": "s", "type": "start"}],
                                 "edges": [{"from": "s", "to": "ghost"}]})

    def test_api_rejects_invalid_definition(self, client, admin_token):
        r = client.post("/api/workflows", headers=auth(admin_token), json={
            "name": "环形流", "definition": {
                "nodes": [{"id": "s", "type": "start"}, {"id": "a", "type": "code"}],
                "edges": [{"from": "s", "to": "a"}, {"from": "a", "to": "s"}]}})
        assert r.status_code == 400 and "环" in r.json()["detail"]


# ---------------------------------------------------------------- CSV 解析
def test_parse_csv_text():
    rows = parse_csv_text("month,product,sales\n2025-05,保温杯,1500\n2025-05,榨汁杯,900.5\n")
    assert rows == [{"month": "2025-05", "product": "保温杯", "sales": 1500},
                    {"month": "2025-05", "product": "榨汁杯", "sales": 900.5}]


# ---------------------------------------------------------------- 端到端：销售数据分析
def _builtin_wf(client, token, name):
    wfs = client.get("/api/workflows", headers=auth(token)).json()
    return next(w for w in wfs if w["name"] == name)


def test_sales_analysis_end_to_end(client, emp_token):
    wf = _builtin_wf(client, emp_token, "销售数据分析")
    assert {"start", "code", "llm", "condition", "end"} <= set(wf["node_types"])

    r = client.post(f"/api/workflows/{wf['id']}/run", headers=auth(emp_token),
                    files={"file": ("sales.csv", make_csv(), "text/csv")},
                    data={"params": "{}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success", body["error"]
    assert body["order"] == ["start", "check", "summary", "report", "end"]

    summary = body["output"]["summary"]
    assert summary["total_sales"] == 4700.75
    assert summary["record_count"] == 5
    assert summary["monthly_sales"] == {"2025-04": 2000.5, "2025-05": 2700.25}
    assert summary["top_products"][0] == {"product": "保温杯", "sales": 2700.5}
    assert [p["product"] for p in summary["top_products"]] == ["保温杯", "榨汁杯", "手机壳"]
    assert summary["mom_growth_pct"] == 34.98  # (2700.25-2000.5)/2000.5*100
    assert "Mock模型" in body["output"]["report"] and "mock-data-analysis" in body["output"]["report"]

    # 节点级执行详情：每个节点都记录了输入输出
    detail = client.get(f"/api/workflows/runs/{body['run_id']}/detail", headers=auth(emp_token)).json()
    assert [n["node_id"] for n in detail["nodes"]] == body["order"]
    by_id = {n["node_id"]: n for n in detail["nodes"]}
    assert by_id["start"]["node_type"] == "start"
    assert by_id["check"]["output"]["result"] is True
    assert by_id["summary"]["output"]["result"]["total_sales"] == 4700.75
    assert by_id["report"]["node_type"] == "llm"
    assert all(n["status"] == "success" for n in detail["nodes"])


def test_sales_analysis_empty_csv_takes_false_branch(client, emp_token):
    wf = _builtin_wf(client, emp_token, "销售数据分析")
    r = client.post(f"/api/workflows/{wf['id']}/run", headers=auth(emp_token),
                    files={"file": ("empty.csv", make_csv(rows=("month,product,sales",)), "text/csv")},
                    data={"params": "{}"}).json()
    assert r["status"] == "success"
    assert r["order"] == ["start", "check", "empty_end"]   # condition 走 false 分支
    assert "没有数据" in r["output"]["error"]


def test_run_detail_isolation(client, emp_token):
    client.post("/api/auth/register", json={"username": "emp002", "password": "pass123456",
                                            "department": "运营部"})
    tok_b = login(client, "emp002", "pass123456")
    wf = _builtin_wf(client, emp_token, "销售数据分析")
    run = client.post(f"/api/workflows/{wf['id']}/run", headers=auth(emp_token),
                      files={"file": ("s.csv", make_csv(), "text/csv")}, data={"params": "{}"}).json()
    # 他人不能查看运行详情，但能看到自己的运行列表
    assert client.get(f"/api/workflows/runs/{run['run_id']}/detail", headers=auth(tok_b)).status_code == 403
    assert client.get(f"/api/workflows/{wf['id']}/runs", headers=auth(tok_b)).json() == []
    assert len(client.get(f"/api/workflows/{wf['id']}/runs", headers=auth(emp_token)).json()) == 1


# ---------------------------------------------------------------- http_api 节点（8005 内 mock 路由）
def test_http_api_node_against_local_mock(client, admin_token, live_server):
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "api", "type": "http_api", "config": {
                "method": "GET",
                "url": live_server + "/api/mock/ads-data?campaign={{input.params.campaign}}"}},
            {"id": "calc", "type": "code", "config": {
                "vars": {"spend": "{{api.data.spend}}", "sales": "{{api.data.sales}}"},
                "expression": "{'acos_pct': round(spend / sales * 100, 2), 'suggested_daily_budget': round(spend / 30 * 1.25, 2)}"}},
            {"id": "end", "type": "end", "config": {"output": {
                "metrics": "{{api.data}}", "analysis": "{{calc.result}}"}}},
        ],
        "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "calc"}, {"from": "calc", "to": "end"}],
    }
    wf_id = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "广告数据分析(http_api演示)", "definition": defn}).json()["id"]

    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                    data={"params": json.dumps({"campaign": "夏季大促"})}).json()
    assert r["status"] == "success", r["error"]
    assert r["order"] == ["start", "api", "calc", "end"]
    assert r["output"]["metrics"]["orders"] == 187
    assert r["output"]["analysis"]["acos_pct"] == round(1562.5 / 5609.06 * 100, 2)

    detail = client.get(f"/api/workflows/runs/{r['run_id']}/detail", headers=auth(admin_token)).json()
    api_node = next(n for n in detail["nodes"] if n["node_id"] == "api")
    assert api_node["output"]["status_code"] == 200
    assert api_node["output"]["data"]["campaign"] == "夏季大促"


# ---------------------------------------------------------------- knowledge_search + llm 节点
def test_knowledge_search_and_llm_nodes(client, admin_token):
    kbs = client.get("/api/kb", headers=auth(admin_token)).json()
    sop_kb = next(k for k in kbs if k["name"] == "公司SOP库")["id"]
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "ks", "type": "knowledge_search", "config": {
                "kb_id": sop_kb, "query": "{{input.params.q}}", "top_k": 2}},
            {"id": "answer", "type": "llm", "config": {
                "provider": "mock", "model": "mock-sop-qa",
                "system": "根据检索到的知识回答。\n[知识库上下文]\n{{ks.results}}",
                "prompt": "{{input.params.q}}"}},
            {"id": "end", "type": "end", "config": {"output": {
                "hits": "{{ks.count}}", "answer": "{{answer.text}}"}}},
        ],
        "edges": [{"from": "start", "to": "ks"}, {"from": "ks", "to": "answer"},
                  {"from": "answer", "to": "end"}],
    }
    wf_id = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "SOP知识问答", "definition": defn}).json()["id"]
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                    data={"params": json.dumps({"q": "FBA发货外箱重量限制"})}).json()
    assert r["status"] == "success", r["error"]
    assert r["output"]["hits"] >= 1
    assert "mock-sop-qa" in r["output"]["answer"]

    detail = client.get(f"/api/workflows/runs/{r['run_id']}/detail", headers=auth(admin_token)).json()
    ks = next(n for n in detail["nodes"] if n["node_id"] == "ks")
    assert any("22.7" in x["text"] or "重量" in x["text"] for x in ks["output"]["results"])


# ---------------------------------------------------------------- condition 节点独立测试 + 节点失败
def test_condition_expression_and_failed_node(client, admin_token):
    # condition 依据参数走不同分支
    defn = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "c", "type": "condition", "config": {
                "expression": "score >= 60", "vars": {"score": "{{input.params.score}}"}}},
            {"id": "pass_end", "type": "end", "config": {"output": {"level": "合格"}}},
            {"id": "fail_end", "type": "end", "config": {"output": {"level": "不合格"}}},
        ],
        "edges": [{"from": "start", "to": "c"},
                  {"from": "c", "to": "pass_end", "condition": "true"},
                  {"from": "c", "to": "fail_end", "condition": "false"}],
    }
    wf_id = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "条件分支演示", "definition": defn}).json()["id"]

    r1 = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                     data={"params": json.dumps({"score": 85})}).json()
    assert r1["order"] == ["start", "c", "pass_end"] and r1["output"]["level"] == "合格"
    r2 = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token),
                     data={"params": json.dumps({"score": 30})}).json()
    assert r2["order"] == ["start", "c", "fail_end"] and r2["output"]["level"] == "不合格"

    # 危险 code 表达式：保存定义时即被拒绝（配置期 fail-fast，不必跑一遍才发现）
    bad = {"nodes": [{"id": "start", "type": "start"},
                     {"id": "hack", "type": "code", "config": {"expression": "__import__('os')"}}],
           "edges": [{"from": "start", "to": "hack"}]}
    r = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "危险流", "definition": bad})
    assert r.status_code == 400 and "hack" in r.json()["detail"]

    # 执行层仍独立兜底：绕过 API 直接执行同一定义也不允许求值危险表达式
    res = execute_workflow(bad, {"params": {}}, {})
    assert res["status"] == "failed" and "hack" in res["error"]

    # 合法但运行期出错的表达式 → 运行 failed，且节点级详情记录错误
    boom = {"nodes": [{"id": "start", "type": "start"},
                      {"id": "calc", "type": "code", "config": {"expression": "1 / 0", "vars": {}}},
                      {"id": "end", "type": "end", "config": {"output": {"r": "{{calc.result}}"}}}],
            "edges": [{"from": "start", "to": "calc"}, {"from": "calc", "to": "end"}]}
    wf_id = client.post("/api/workflows", headers=auth(admin_token), json={
        "name": "除零流", "definition": boom}).json()["id"]
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(admin_token), data={"params": "{}"}).json()
    assert r["status"] == "failed" and "calc" in r["error"]
    detail = client.get(f"/api/workflows/runs/{r['run_id']}/detail", headers=auth(admin_token)).json()
    calc = next(n for n in detail["nodes"] if n["node_id"] == "calc")
    assert calc["status"] == "failed" and "表达式执行出错" in calc["error"]


def test_workflow_crud_permissions(client, admin_token, emp_token):
    defn = {"nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end", "config": {"output": {"ok": 1}}}],
            "edges": [{"from": "start", "to": "end"}]}
    assert client.post("/api/workflows", headers=auth(emp_token),
                       json={"name": "x", "definition": defn}).status_code == 403
    wf_id = client.post("/api/workflows", headers=auth(admin_token),
                        json={"name": "最简流", "description": "d", "definition": defn}).json()["id"]
    assert client.put(f"/api/workflows/{wf_id}", headers=auth(admin_token),
                      json={"name": "最简流v2", "description": "", "definition": defn}).status_code == 200
    r = client.post(f"/api/workflows/{wf_id}/run", headers=auth(emp_token), data={"params": "{}"}).json()
    assert r["status"] == "success" and r["output"] == {"ok": 1}
    assert client.delete(f"/api/workflows/{wf_id}", headers=auth(admin_token)).status_code == 200
    assert client.get(f"/api/workflows/{wf_id}", headers=auth(emp_token)).status_code == 404
