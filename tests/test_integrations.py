"""预留企业系统集成接口（企业微信 / ERP / 亚马逊）：占位路由返回 501 与接入方案。"""
from conftest import auth, login


def test_integrations_list_is_public(client):
    r = client.get("/api/integrations")
    assert r.status_code == 200
    names = {i["name"] for i in r.json()}
    assert {"wecom", "erp", "amazon"} <= names
    for i in r.json():
        assert i["status"] == "planned" and i["plan"]


def test_webhooks_return_501_with_plan(client):
    for path, key in (("/api/integrations/wecom/webhook", "企业微信"),
                      ("/api/integrations/erp/webhook", "ERP"),
                      ("/api/integrations/amazon/webhook", "亚马逊")):
        r = client.post(path, json={"event": "test"})
        assert r.status_code == 501, path
        body = r.json()
        assert body["integration"] == key.split("（")[0] or key in body["detail"]
        assert body["接入方案"]


def test_health_and_mock_endpoints(client, admin_token):
    # mock 端点已纳入鉴权基线：未登录 401，登录后可访问
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/mock/ads-data").status_code == 401
    ads = client.get("/api/mock/ads-data", params={"campaign": "测试活动"},
                     headers=auth(admin_token)).json()
    assert ads["campaign"] == "测试活动" and ads["acos"] > 0
    inv = client.get("/api/mock/inventory", headers=auth(admin_token)).json()
    assert inv["sellable_days"] > 0
