"""LLM 网关层：重试退避、失败降级、每用户限流、token 计量与调用审计。

为什么独立成网关层而不是各路由直接调 Provider：
1. 重试/退避/超时是横切逻辑，散在各调用点必然口径不一；
2. 成本防护（限流）与用量审计（llm_calls 表）只有在统一入口才能全覆盖；
3. 降级链（openai_compatible 调用失败 → mock + 明确标记）是平台可用性的兜底，
   降级必须"可被看见"——回复带 degraded 标记、审计表记 degraded，而不是静默替换。

错误语义（与现有"未配置 → 502"约定兼容）：
- Provider 未配置（base_url/api_key 缺失）→ 配置错误，不重试、不降级，
  抛 GatewayError（消息含"模型调用失败"），由路由映射为 502 明确暴露；
- 已配置但调用失败（网络错误/超时/5xx）→ 指数退避重试 max_attempts 次，
  仍失败则按 allow_degrade 决定：降级 mock（标记 degraded）或抛 GatewayError。
"""
import json
import math
import os
import random
import time

from .config import LLM_MAX_ATTEMPTS_CEILING
from .db import now
from .providers import MockProvider, get_provider

CONFIG_MISSING_MARK = "未配置"


def _resolve_attempts(max_attempts: int | None) -> int:
    """重试次数：env/入参均可调，但不得超过上限——过大的重试数叠加退避会长期占住线程。"""
    raw = max_attempts or os.environ.get("AIP_LLM_MAX_ATTEMPTS", "3")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 3
    return min(max(1, n), LLM_MAX_ATTEMPTS_CEILING)


class GatewayError(RuntimeError):
    """不可恢复的模型调用错误（配置缺失 / 关闭降级后仍失败）。不降级，直接暴露给调用方。"""


def estimate_tokens(text: str) -> int:
    """无 usage 字段时的粗略 token 估算：CJK 字符约 0.6 token/字，其余约 0.28/字符。

    只用于 mock Provider 与流式增量；真实调用优先取接口返回的 usage 字段。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, round(cjk * 0.6 + other * 0.28))


def messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


class UserRateLimiter:
    """进程内每用户滑动窗口限流（默认 N 次/分钟）。

    单实例部署够用且零依赖；横向扩容时换 Redis + Lua 计数即可，allow/retry_after 接口不变。
    只挂在会触发 LLM 调用的消息端点上（成本入口），普通读接口不限。
    limit_per_min <= 0 视为不限制（便于测试与运维开关）。
    """

    def __init__(self, limit_per_min: int):
        self.limit = int(limit_per_min)
        self._hits: dict[int, list[float]] = {}

    def allow(self, user_id: int) -> bool:
        if self.limit <= 0:
            return True
        ts = time.monotonic()
        window = self._hits.setdefault(user_id, [])
        while window and ts - window[0] > 60.0:
            window.pop(0)
        if len(window) >= self.limit:
            return False
        window.append(ts)
        return True

    def retry_after(self, user_id: int) -> int:
        window = self._hits.get(user_id) or []
        if not window:
            return 60
        return max(1, math.ceil(60.0 - (time.monotonic() - window[0])))


def _backoff_sleep(attempt: int) -> None:
    """指数退避 + 抖动：0.4s, 0.8s, 1.6s...（抖动防惊群）。"""
    time.sleep(0.4 * (2 ** (attempt - 1)) + random.uniform(0, 0.2))


def _record(conn, *, user_id, assistant_id, run_id, provider_name, model,
            prompt_tokens, completion_tokens, latency_ms, attempts, status, error):
    if conn is None:
        return
    conn.execute(
        "INSERT INTO llm_calls(user_id,assistant_id,run_id,provider,model,prompt_tokens,"
        "completion_tokens,latency_ms,attempts,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, assistant_id, run_id, provider_name, model, prompt_tokens,
         completion_tokens, latency_ms, attempts, status, (error or "")[:1000], now()))
    conn.commit()


def chat_with_gateway(conn, *, provider_name: str, model: str | None, messages: list[dict],
                      user_id: int | None = None, assistant_id: int | None = None,
                      run_id: int | None = None, allow_degrade: bool = True,
                      max_attempts: int | None = None) -> dict:
    """同步调用：重试 + 按需降级 + 审计落库。

    返回 {text, provider, model, degraded, degrade_reason, usage, latency_ms, attempts}。
    """
    attempts_total = _resolve_attempts(max_attempts)
    provider = get_provider(provider_name, conn=conn)
    started = time.monotonic()
    ptoks = messages_tokens(messages)
    last_exc: Exception | None = None

    for attempt in range(1, attempts_total + 1):
        try:
            if hasattr(provider, "chat_with_usage"):
                out = provider.chat_with_usage(messages, model=model)
                text, usage = out["text"], out.get("usage") or {}
            else:
                text, usage = provider.chat(messages, model=model), {}
            usage = {"prompt_tokens": usage.get("prompt_tokens", ptoks),
                     "completion_tokens": usage.get("completion_tokens", estimate_tokens(text))}
            latency = int((time.monotonic() - started) * 1000)
            _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=run_id,
                    provider_name=provider_name, model=model,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    latency_ms=latency, attempts=attempt, status="success", error="")
            return {"text": text, "provider": provider.name, "model": model,
                    "degraded": False, "degrade_reason": "", "usage": usage,
                    "latency_ms": latency, "attempts": attempt}
        except Exception as exc:
            last_exc = exc
            # 配置缺失：重试无意义，立即失败（保持"未配置 → 502 明确报错"的语义）
            if CONFIG_MISSING_MARK in str(exc):
                break
            if attempt < attempts_total:
                _backoff_sleep(attempt)

    latency = int((time.monotonic() - started) * 1000)
    err_text = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"
    # 配置缺失属"必须人工修配置"的错误：无论是否允许降级都不降级，直接暴露（与 502 语义一致）
    if last_exc is not None and CONFIG_MISSING_MARK in str(last_exc):
        _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=run_id,
                provider_name=provider_name, model=model, prompt_tokens=ptoks,
                completion_tokens=0, latency_ms=latency, attempts=1,
                status="failed", error=err_text)
        raise GatewayError(f"模型调用失败: {err_text}")
    if allow_degrade:
        fallback = MockProvider()
        text = fallback.chat(messages, model=model)
        _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=run_id,
                provider_name=provider_name, model=model, prompt_tokens=ptoks,
                completion_tokens=estimate_tokens(text), latency_ms=latency,
                attempts=attempts_total, status="degraded", error=err_text)
        return {"text": text, "provider": provider_name, "model": model,
                "degraded": True, "degrade_reason": err_text,
                "usage": {"prompt_tokens": ptoks, "completion_tokens": estimate_tokens(text)},
                "latency_ms": latency, "attempts": attempts_total}
    _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=run_id,
            provider_name=provider_name, model=model, prompt_tokens=ptoks,
            completion_tokens=0, latency_ms=latency, attempts=attempts_total,
            status="failed", error=err_text)
    raise GatewayError(f"模型调用失败: {err_text}")


def stream_chat_with_gateway(conn, *, provider_name: str, model: str | None, messages: list[dict],
                             user_id: int | None = None, assistant_id: int | None = None,
                             allow_degrade: bool = True, max_attempts: int | None = None):
    """流式调用生成器：yield ("meta"|"delta"|"done"|"error", payload)。

    - meta 在第一个 delta 前产出，携带 degraded/degrade_reason（降级决策发生在开流阶段）；
    - 重试粒度 = "打开流并拿到第一个分片"。已开始输出后再失败无法安全重试
      （重放会造成内容重复），直接 error 结束，由前端提示"本条未保存"；
    - 配置缺失不降级（保持 502 语义）→ error 结束；重试耗尽按 allow_degrade 降级 mock 流。
    """
    attempts_total = _resolve_attempts(max_attempts)
    provider = get_provider(provider_name, conn=conn)
    ptoks = messages_tokens(messages)

    # 配置预检：openai_compatible 缺 base_url/api_key 时报配置错误（不重试、不降级）
    if provider_name == "openai_compatible" and (not provider.base_url or not provider.api_key):
        err = "openai_compatible 未配置：请在后台设置或环境变量中提供 base_url 与 api_key"
        _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=None,
                provider_name=provider_name, model=model, prompt_tokens=ptoks,
                completion_tokens=0, latency_ms=0, attempts=1, status="failed", error=err)
        yield ("error", f"模型调用失败: {err}")
        return

    started = time.monotonic()
    stream_iter = None
    first_chunk = ""
    last_exc: Exception | None = None
    attempts_used = 0

    for attempt in range(1, attempts_total + 1):
        attempts_used = attempt
        try:
            it = provider.chat_stream(messages, model=model)
            first_chunk = next(it)          # 拿到第一个分片才算流真正建立
            stream_iter = it
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt < attempts_total:
                _backoff_sleep(attempt)

    degraded = stream_iter is None
    degrade_reason = f"{type(last_exc).__name__}: {last_exc}" if last_exc else ""

    if degraded:
        if not allow_degrade:
            _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=None,
                    provider_name=provider_name, model=model, prompt_tokens=ptoks,
                    completion_tokens=0, latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts_used, status="failed", error=degrade_reason)
            yield ("error", f"模型调用失败: {degrade_reason}")
            return
        mock_text = MockProvider().chat(messages, model=model)
        chunks = [mock_text[i:i + 8] for i in range(0, len(mock_text), 8)]
    else:
        chunks = None

    yield ("meta", {"provider": provider_name, "model": model,
                    "degraded": degraded, "degrade_reason": degrade_reason})

    text_parts: list[str] = [first_chunk] if not degraded else []
    try:
        if degraded:
            for c in chunks:
                yield ("delta", c)
                text_parts.append(c)
        else:
            if first_chunk:
                yield ("delta", first_chunk)
            for c in stream_iter:
                yield ("delta", c)
                text_parts.append(c)
    except Exception as exc:
        # 已输出一半再失败：不能安全重试，如实报错（本条未保存）
        err = f"{type(exc).__name__}: {exc}"
        _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=None,
                provider_name=provider_name, model=model, prompt_tokens=ptoks,
                completion_tokens=estimate_tokens("".join(text_parts)),
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts_used, status="failed", error=err)
        yield ("error", f"模型输出中断: {err}（本条未保存）")
        return

    reply = "".join(text_parts)
    latency = int((time.monotonic() - started) * 1000)
    usage = {"prompt_tokens": ptoks, "completion_tokens": estimate_tokens(reply)}
    _record(conn, user_id=user_id, assistant_id=assistant_id, run_id=None,
            provider_name=provider_name, model=model,
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            latency_ms=latency, attempts=attempts_used,
            status="degraded" if degraded else "success",
            error=degrade_reason if degraded else "")
    yield ("done", {"reply": reply, "usage": usage, "latency_ms": latency,
                    "degraded": degraded, "attempts": attempts_used})


def sse_event(kind: str, payload: dict) -> str:
    """编码为一条 SSE 帧。data 一行 JSON，前端按 event 分发。"""
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
