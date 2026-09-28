"""预留企业系统集成接口（企业微信 / ERP webhook 骨架）。

当前返回 501 Not Implemented，仅约定路由与数据格式，接入方案见《项目原理.md》。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def _not_implemented(name: str, plan: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "detail": f"{name} 集成尚未接入",
            "integration": name,
            "接入方案": plan,
        },
    )


def build_router(deps: dict | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/integrations", tags=["integrations"])

    @router.get("")
    def list_integrations():
        """占位：返回规划中的企业系统集成清单与状态。"""
        return [
            {"name": "wecom", "display": "企业微信", "status": "planned",
             "webhook": "/api/integrations/wecom/webhook",
             "plan": "配置企业微信自建应用，接收消息回调 → 映射平台账号 → 调用助手对话接口 → 主动回复"},
            {"name": "erp", "display": "ERP系统", "status": "planned",
             "webhook": "/api/integrations/erp/webhook",
             "plan": "ERP 推送订单/库存变更事件 → 触发对应工作流（如库存分析）→ 结果回写或通知"},
            {"name": "amazon", "display": "亚马逊业务数据", "status": "planned",
             "webhook": "/api/integrations/amazon/webhook",
             "plan": "定时通过 SP-API 拉取销售/广告报告 → 存为CSV → 触发销售/广告分析工作流"},
        ]

    @router.post("/wecom/webhook")
    async def wecom_webhook(request: Request):
        return _not_implemented(
            "企业微信",
            "1) 企业微信管理后台创建自建应用并配置回调URL指向本路由；"
            "2) 实现URL验证(echostr解密)与消息接收；"
            "3) 将企业微信userid映射到平台users表(增加wecom_userid字段)；"
            "4) 收到消息后调用对应助手的对话逻辑并主动发送应用消息回复。",
        )

    @router.post("/erp/webhook")
    async def erp_webhook(request: Request):
        return _not_implemented(
            "ERP",
            "1) 约定ERP事件推送格式(订单创建/库存变更等)；"
            "2) 本路由校验签名后解析事件；"
            "3) 按事件类型触发 workflows 表中的对应工作流(如库存分析)；"
            "4) 执行结果通过ERP提供的回写接口或企业微信通知相关人员。",
        )

    @router.post("/amazon/webhook")
    async def amazon_webhook(request: Request):
        return _not_implemented(
            "亚马逊业务数据",
            "1) 注册亚马逊SP-API开发者应用并获取LWA授权；"
            "2) 定时任务拉取Reports API的销售/广告/库存报告；"
            "3) 转换为CSV后调用 POST /api/workflows/{id}/run 触发分析工作流。",
        )

    return router
