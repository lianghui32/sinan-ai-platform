"""模型接入抽象层：ModelProvider 接口 + MockProvider + OpenAICompatibleProvider。

体现"模型扩展能力"：新增模型渠道只需实现 ModelProvider 接口并注册到 PROVIDER_REGISTRY。
"""
import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from .config import ENV_OPENAI_API_KEY, ENV_OPENAI_BASE_URL, ENV_OPENAI_MODEL


class ModelProvider(ABC):
    """模型提供方接口：输入 OpenAI 风格 messages，输出回复文本。"""

    name: str = "base"

    @abstractmethod
    def chat(self, messages: list[dict], model: str | None = None) -> str:
        ...

    def chat_with_usage(self, messages: list[dict], model: str | None = None) -> dict:
        """返回 {text, usage}；默认无 usage（网关层会做估算）。"""
        return {"text": self.chat(messages, model=model), "usage": None}

    def chat_stream(self, messages: list[dict], model: str | None = None):
        """流式输出分片的默认实现：整段生成后按 8 字符切片模拟流。

        子类可覆写为真流式（OpenAICompatibleProvider 用 SSE 逐 token 解析）；
        网关层对"未实现真流式"的 Provider 也能统一提供流式体验。
        """
        text = self.chat(messages, model=model)
        for i in range(0, len(text), 8):
            yield text[i:i + 8]


class MockProvider(ModelProvider):
    """确定性模拟模型：同一输入永远得到同一输出，便于测试与离线演示。"""

    name = "mock"

    def chat(self, messages: list[dict], model: str | None = None) -> str:
        model = model or "mock-model"
        last_user = ""
        has_kb_context = False
        for m in messages:
            if m.get("role") == "user":
                last_user = m.get("content", "")
            if m.get("role") == "system" and "[知识库上下文]" in m.get("content", ""):
                has_kb_context = True
        digest = hashlib.sha256(
            (model + "|" + last_user).encode("utf-8")
        ).hexdigest()[:8]
        excerpt = last_user.strip().replace("\n", " ")
        if len(excerpt) > 60:
            excerpt = excerpt[:60] + "…"
        turn_count = sum(1 for m in messages if m.get("role") == "user")
        parts = [f"【Mock模型 {model}】已收到你的第{turn_count}轮提问：\u201c{excerpt}\u201d。"]
        if has_kb_context:
            parts.append("我已结合企业知识库中的相关内容进行回答，具体引用见消息下方来源列表。")
        parts.append(
            "这是本地确定性模拟回复，未调用真实大模型；可在后台将本助手的 Provider 切换为 "
            "openai_compatible 并配置 base_url/api_key/model 接入真实模型。"
        )
        parts.append(f"(校验码:{digest})")
        return "\n".join(parts)

    def chat_stream(self, messages: list[dict], model: str | None = None):
        """模拟逐段生成：切片间加极短停顿，让前端打字机效果肉眼可见。"""
        text = self.chat(messages, model=model)
        for i in range(0, len(text), 8):
            yield text[i:i + 8]
            time.sleep(0.012)


def _no_proxy_for(url: str) -> bool:
    """回环地址的请求不走系统代理。

    httpx trust_env 会继承系统代理，但不识别 Windows 的 ProxyOverride 本地绕过规则——
    本机开着代理（如 127.0.0.1:7897）时，连 127.0.0.1 也会被转发给代理返回 502。
    回环地址语义上永远不该出本机，这里显式绕过（工作流 http_api 节点调本服务 mock
    路由、本地 vLLM/Ollama 端点都依赖这一点）。
    """
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "::1", "localhost") or host.endswith(".localhost")


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI 兼容 Provider：可指向 OpenAI / DeepSeek / Moonshot / vLLM / Ollama 等任意兼容端点。

    base_url 形如 https://api.deepseek.com/v1 ，调用 {base_url}/chat/completions。
    """

    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, default_model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages: list[dict], model: str, stream: bool) -> dict:
        payload = {"model": model, "messages": messages}
        if stream:
            payload["stream"] = True
        return payload

    def _client_kwargs(self, url: str) -> dict:
        return {"trust_env": not _no_proxy_for(url)}

    def _check_config(self):
        if not self.base_url or not self.api_key:
            raise RuntimeError(
                "openai_compatible 未配置：请在后台设置或环境变量中提供 base_url 与 api_key"
            )

    @staticmethod
    def _content_from_choice(data: dict) -> str:
        try:
            delta = data["choices"][0]
            msg = delta.get("message") or {}
            return msg.get("content") or delta.get("text") or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def chat(self, messages: list[dict], model: str | None = None) -> str:
        return self.chat_with_usage(messages, model=model)["text"]

    def chat_with_usage(self, messages: list[dict], model: str | None = None) -> dict:
        self._check_config()
        model = model or self.default_model or "gpt-4o-mini"
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, model, stream=False)
        with httpx.Client(timeout=self.timeout, **self._client_kwargs(url)) as client:
            resp = client.post(url, json=payload, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        text = self._content_from_choice(data)
        if not text:
            raise RuntimeError(f"模型返回格式异常: {json.dumps(data, ensure_ascii=False)[:200]}")
        usage = data.get("usage") or {}
        return {"text": text,
                "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                          "completion_tokens": usage.get("completion_tokens", 0)}}

    def chat_stream(self, messages: list[dict], model: str | None = None):
        """真流式：解析上游 SSE（data: {...} 行），逐 content 增量 yield。"""
        self._check_config()
        model = model or self.default_model or "gpt-4o-mini"
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, model, stream=True)
        with httpx.Client(timeout=self.timeout, **self._client_kwargs(url)) as client:
            with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"上游模型返回 {resp.status_code}: {body[:200]}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except ValueError:
                        continue
                    piece = self._content_from_choice(data)
                    if piece:
                        yield piece


def get_settings_dict(conn) -> dict:
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _db_dir(conn) -> str:
    """sqlite 主库文件所在目录（密钥文件与之同目录）。内存库回退当前目录。"""
    try:
        for r in conn.execute("PRAGMA database_list").fetchall():
            if r["name"] == "main" and r["file"]:
                return str(Path(r["file"]).parent)
    except Exception:
        pass
    return "."


def get_decrypted_api_key(conn, value: str) -> str:
    """settings 里的 API Key：enc1: 前缀走 SecretBox 解密；历史明文原样返回（下次保存会加密）。"""
    from .secretbox import SecretBox, get_box

    if not value:
        return ""
    if not SecretBox.is_encrypted(value):
        return value
    try:
        # _db_dir 返回库文件所在目录 = 密钥文件目录，与 admin_router 加密侧一致
        return get_box(_db_dir(conn)).decrypt(value)
    except Exception:
        # 解密失败（密钥文件丢失/更换）：宁可返回空让调用方报"未配置"，也不能把密文当明文用
        return ""


def get_provider(provider_name: str, conn=None) -> ModelProvider:
    """按名称获取 Provider 实例。openai_compatible 的配置优先级：数据库 settings > 环境变量。"""
    settings: dict = {}
    if conn is not None:
        settings = get_settings_dict(conn)
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "openai_compatible":
        base_url = settings.get("openai_base_url") or os.environ.get(ENV_OPENAI_BASE_URL, "")
        db_key = get_decrypted_api_key(conn, settings.get("openai_api_key", "")) if conn is not None else ""
        api_key = db_key or os.environ.get(ENV_OPENAI_API_KEY, "")
        model = settings.get("openai_model") or os.environ.get(ENV_OPENAI_MODEL, "")
        return OpenAICompatibleProvider(base_url, api_key, model)
    raise ValueError(f"未知的 Provider: {provider_name}")


# Provider 注册表：扩展新模型渠道时在此注册
PROVIDER_REGISTRY = {
    "mock": MockProvider,
    "openai_compatible": OpenAICompatibleProvider,
}
