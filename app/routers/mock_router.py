"""本地 Mock 外部 API（挂在 8005 同一服务内），供工作流 http_api 节点测试/演示使用。

真实场景中 http_api 节点的 url 指向外部系统（ERP、亚马逊SP-API、广告平台等），
这里为了不依赖外网、不另开端口，在本服务内提供确定性的模拟端点。

鉴权：端点返回的是确定性假数据，但与全站"默认需要登录"的基线保持一致——
- 工作流 http_api 节点的回环请求由执行器自动附带 X-AIP-Internal（进程级随机密钥）；
- 浏览器/外部调用必须携带有效 Bearer Token；两者都不满足返回 401。
"""
import secrets

from fastapi import APIRouter, Header, HTTPException

from ..db import get_conn, now


def build_router(deps: dict | None = None) -> APIRouter:
    deps = deps or {}
    get_db_path = deps.get("get_db_path")
    internal_secret = deps.get("internal_secret", "")

    def _authorized(authorization: str, x_aip_internal: str) -> bool:
        # 路径一：工作流执行器注入的进程级内部密钥（只存在于同进程回环请求里）
        if internal_secret and x_aip_internal and secrets.compare_digest(x_aip_internal, internal_secret):
            return True
        # 路径二：正常登录态（Bearer Token 存在且未过期）
        if get_db_path and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            conn = get_conn(get_db_path())
            try:
                row = conn.execute(
                    "SELECT expires_at FROM sessions WHERE token=?", (token,)).fetchone()
            finally:
                conn.close()
            if row is None:
                return False
            expires = row["expires_at"] if "expires_at" in row.keys() else ""
            return bool(expires) and expires > now()
        return False

    def _guard(authorization: str, x_aip_internal: str) -> None:
        if not _authorized(authorization, x_aip_internal):
            raise HTTPException(401, "未登录或缺少内部调用凭证")

    router = APIRouter(prefix="/api/mock", tags=["mock"])

    @router.get("/ads-data")
    def ads_data(campaign: str = "夏季大促", authorization: str = Header(default=""),
                 x_aip_internal: str = Header(default="")):
        """模拟广告数据 API：返回确定性的广告指标。"""
        _guard(authorization, x_aip_internal)
        return {
            "campaign": campaign,
            "currency": "USD",
            "impressions": 125000,
            "clicks": 3125,
            "ctr": 0.025,
            "spend": 1562.5,
            "orders": 187,
            "sales": 5609.06,
            "acos": round(1562.5 / 5609.06 * 100, 2),
            "cvr": round(187 / 3125 * 100, 2),
        }

    @router.get("/inventory")
    def inventory(sku: str = "CUP-500-BLK", authorization: str = Header(default=""),
                  x_aip_internal: str = Header(default="")):
        """模拟库存 API。"""
        _guard(authorization, x_aip_internal)
        return {"sku": sku, "fba_available": 860, "fba_inbound": 200,
                "daily_sales_avg": 42, "sellable_days": round(860 / 42, 1)}

    return router
