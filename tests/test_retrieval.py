"""混合检索接口测试：三种模式可用、结果带模式标记、非法模式 400。"""
import json

from conftest import auth

DOC = (
    "产品档案：平台型号 HX-2081 是不锈钢保温杯 500ml，304食品级不锈钢材质，保温12小时。"
    "供应商为永康恒暖五金制品厂，最小起订量500个，交期15天。"
)


def _make_kb(client, admin_token):
    h = auth(admin_token)
    kb = client.post("/api/kb", headers=h,
                     json={"name": "检索测试库", "category": "产品资料"}).json()["id"]
    r = client.post(f"/api/kb/{kb}/documents", headers=h,
                    files={"file": ("产品档案.txt", DOC.encode("utf-8"), "text/plain")})
    assert r.status_code == 200, r.text
    return h, kb


def test_search_three_modes(client, admin_token):
    h, kb = _make_kb(client, admin_token)
    for mode in ("tfidf", "bm25", "hybrid"):
        r = client.get(f"/api/kb/{kb}/search", headers=h,
                       params={"q": "HX-2081 的供应商是谁", "top_k": 3, "mode": mode})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == mode
        assert body["results"], f"模式 {mode} 应有命中"
        assert body["results"][0]["score"] > 0
        assert body["results"][0]["mode"] == mode
        assert "HX-2081" in body["results"][0]["text"]


def test_search_invalid_mode_400(client, admin_token):
    h, kb = _make_kb(client, admin_token)
    r = client.get(f"/api/kb/{kb}/search", headers=h,
                   params={"q": "保温杯", "mode": "向量数据库"})
    assert r.status_code == 400


def test_default_mode_is_hybrid(client, admin_token):
    """不带 mode 参数时默认 hybrid（与对话 RAG 的默认一致）。"""
    h, kb = _make_kb(client, admin_token)
    r = client.get(f"/api/kb/{kb}/search", headers=h, params={"q": "保温杯 材质"})
    assert r.status_code == 200
    assert r.json()["mode"] == "hybrid"
