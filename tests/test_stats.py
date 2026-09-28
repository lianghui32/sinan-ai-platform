"""后台使用情况统计接口。"""
from datetime import date

from conftest import auth


def _chat(client, token, assistant_name, content):
    a = next(x for x in client.get("/api/assistants", headers=auth(token)).json()
             if x["name"] == assistant_name)
    conv = client.post("/api/conversations", headers=auth(token), json={"assistant_id": a["id"]}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth(token),
                    json={"content": content})
    assert r.status_code == 200


def test_stats_requires_admin(client, emp_token):
    assert client.get("/api/admin/stats", headers=auth(emp_token)).status_code == 403


def test_stats_numbers(client, admin_token, emp_token):
    # emp001 提3个问题；admin 提1个
    for q in ("FBA标签要求", "主图规范", "退货处理流程"):
        _chat(client, emp_token, "亚马逊运营助手", q)
    _chat(client, admin_token, "数据分析助手", "什么是ACOS")

    s = client.get("/api/admin/stats", headers=auth(admin_token)).json()

    # 总量：4个会话、8条消息（4问+4答）、种子用户2个、内置助手3个、种子文档2个
    assert s["totals"]["users"] == 2
    assert s["totals"]["assistants"] == 3
    assert s["totals"]["conversations"] == 4
    assert s["totals"]["messages"] == 8
    assert s["totals"]["documents"] == 2

    # 每用户消息数（含提问与回复）
    by_user = {u["username"]: u["message_count"] for u in s["per_user"]}
    assert by_user["emp001"] == 6
    assert by_user["admin"] == 2

    # 每助手消息数
    by_asst = {a["name"]: a["message_count"] for a in s["per_assistant"]}
    assert by_asst["亚马逊运营助手"] == 6
    assert by_asst["数据分析助手"] == 2
    assert by_asst["产品开发助手"] == 0

    # 近7日活跃：今天有2个活跃用户、8条消息
    today = date.today().isoformat()
    daily = {d["date"]: d for d in s["daily_active_7d"]}
    assert today in daily
    assert daily[today]["active_users"] == 2
    assert daily[today]["messages"] == 8


def test_stats_includes_workflow_runs(client, admin_token):
    wfs = client.get("/api/workflows", headers=auth(admin_token)).json()
    wf = next(w for w in wfs if w["name"] == "销售数据分析")
    import io
    csv_bytes = io.BytesIO(b"month,product,sales\n2025-05,A,100\n")
    r = client.post(f"/api/workflows/{wf['id']}/run", headers=auth(admin_token),
                    files={"file": ("s.csv", csv_bytes, "text/csv")}, data={"params": "{}"})
    assert r.json()["status"] == "success"
    s = client.get("/api/admin/stats", headers=auth(admin_token)).json()
    assert s["totals"]["workflow_runs"] == 1
