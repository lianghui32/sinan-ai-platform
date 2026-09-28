"""工作流引擎：JSON 定义的 DAG，按依赖顺序执行，记录每节点输入输出。

节点类型：
- start            起始节点，输出 = 本次运行的输入（如解析后的 CSV 行）
- llm              调用 ModelProvider 生成文本
- knowledge_search 在指定知识库中做 TF-IDF 余弦检索
- http_api         用 httpx 调用外部 HTTP API（测试时指向本服务 8005 端口内置 mock 路由）
- code             受限 Python 表达式计算（AST 白名单，见 safe_eval.py）
- condition        布尔判断，出边按 "true"/"false" 分支
- end              结束节点，输出运行结果

模板语法：配置里的字符串可含 {{节点id.路径}} 或 {{input.路径}}，
若整个字符串恰好是一个模板，则替换后保留原始类型（dict/list/数值），否则做字符串插值。
支持 {{路径|默认值}}：路径不存在时用默认值（不写默认值则节点失败，避免静默传空）。
默认值做轻量类型推断（5 / 1.5 / true / null / [] / "带引号字符串"），其余按文本处理；
对象型默认值不支持（默认值里不允许出现大括号）。
http_api 节点可用 params 对象传查询参数（由 httpx 正确编码），内联进 url 的模板值也会做百分号转义。
"""
import ipaddress
import json
import re
import socket
from collections import deque
from urllib.parse import quote, urlsplit

import httpx

from .config import HTTP_API_ALLOW_PRIVATE, HTTP_API_TIMEOUT_MAX, HTTP_API_TIMEOUT_MIN
from .safe_eval import UnsafeExpressionError, check_expression, safe_eval

NODE_TYPES = {"start", "llm", "knowledge_search", "http_api", "code", "condition", "end"}
# {{节点id.路径}} 或 {{路径|默认值}}（路径不存在时用默认值，常用于可选运行参数）
TEMPLATE_RE = re.compile(r"\{\{\s*([\w\.\[\]]+)(?:\|([^{}]*?))?\s*\}\}")


# ---------------------------------------------------------------- 定义校验
class WorkflowDefinitionError(ValueError):
    pass


def validate_definition(defn: dict) -> None:
    """校验 DAG：节点/边合法性、唯一 start、无环。"""
    if not isinstance(defn, dict):
        raise WorkflowDefinitionError("定义必须是 JSON 对象")
    nodes = defn.get("nodes") or []
    edges = defn.get("edges") or []
    if not nodes:
        raise WorkflowDefinitionError("nodes 不能为空")
    ids = []
    start_count = 0
    for nd in nodes:
        nid = nd.get("id")
        if not nid or not isinstance(nid, str):
            raise WorkflowDefinitionError("每个节点必须有字符串 id")
        if nid in ids:
            raise WorkflowDefinitionError(f"节点 id 重复: {nid}")
        ids.append(nid)
        if nd.get("type") not in NODE_TYPES:
            raise WorkflowDefinitionError(f"节点 {nid} 类型非法: {nd.get('type')}")
        if nd["type"] == "start":
            start_count += 1
    if start_count != 1:
        raise WorkflowDefinitionError("必须有且仅有一个 start 节点")
    if "input" in ids:
        # 执行器的 context 用 "input" 存本次运行输入，节点 id 同名会静默覆盖它
        raise WorkflowDefinitionError("节点 id 不能使用保留字 input")
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            raise WorkflowDefinitionError(f"边引用了不存在的节点: {e}")

    # Kahn 拓扑排序检测环
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    q = deque([i for i in ids if indeg[i] == 0])
    seen = 0
    while q:
        cur = q.popleft()
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
    if seen != len(ids):
        raise WorkflowDefinitionError("工作流存在环，必须是 DAG")


_TEMPLATE_ONLY = re.compile(r"\{\{.*\}\}", re.S)


def _expression_ok(expr: str) -> bool:
    """表达式里还留有未展开的模板占位符时无法静态检查，交给执行期求值。"""
    return not _TEMPLATE_ONLY.search(expr)


def validate_expressions(defn: dict) -> None:
    """保存/更新定义时静态校验 code / condition 节点与边上的表达式。

    执行期本来就会被 safe_eval 的 AST 白名单拦住，这里提前拒绝是为了让
    配置者在保存时立刻看到"哪个节点的表达式非法"，而不是跑完才发现。
    """
    for nd in (defn.get("nodes") or []):
        if nd.get("type") not in ("code", "condition"):
            continue
        expr = (nd.get("config") or {}).get("expression")
        if expr is None or not _expression_ok(str(expr)):
            continue
        try:
            check_expression(str(expr))
        except UnsafeExpressionError as exc:
            raise WorkflowDefinitionError(
                f"节点 {nd.get('id')} 的表达式不安全或语法错误: {exc}") from exc

    for e in (defn.get("edges") or []):
        cond = e.get("condition")
        if cond in (None, "") or str(cond).strip().lower() in ("true", "false"):
            continue
        if not _expression_ok(str(cond)):
            continue
        try:
            check_expression(str(cond))
        except UnsafeExpressionError as exc:
            raise WorkflowDefinitionError(
                f"边 {e.get('from')}→{e.get('to')} 的条件表达式非法: {exc}") from exc


# ---------------------------------------------------------------- 模板解析
def _resolve_path(context: dict, path: str):
    """按 a.b[0].c 形式在 context 中取值。"""
    parts = re.split(r"\.(?![^\[]*\])", path)
    cur = context
    for part in parts:
        m = re.match(r"^([^\[\]]+)((\[\d+\])*)$", part)
        if not m:
            raise KeyError(f"非法路径片段: {part}")
        key = m.group(1)
        if isinstance(cur, dict):
            if key not in cur:
                raise KeyError(f"路径不存在: {path}（缺少 {key}）")
            cur = cur[key]
        else:
            raise KeyError(f"路径不存在: {path}")
        for idx in re.findall(r"\[(\d+)\]", m.group(2) or ""):
            cur = cur[int(idx)]
    return cur


def _coerce_default(raw: str):
    """默认值字面量的轻量类型推断。

    '5'->5，'1.5'->1.5，'true'->True，'null'->None，'[]'->[]，'"文本"'->文本，其余原样为字符串。
    数值只在字面量以数字/正负号/点开头时才尝试转换，否则 'nan'、'inf' 这类词会被 Python 的
    宽松字面量解析悄悄变成 float('nan')/float('inf')，语义与配置者写下的文本不一致。
    对象型默认值（`|{}`）不支持——模板正则不允许默认值里出现大括号，见 render_template。
    """
    s = raw.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if s[:1] in ("-", "+", ".") or s[:1].isdigit():
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s
    if s[:1] in ("[", '"', "'"):        # 列表 / 带引号字符串（空文件、空列表这类"真的想要空值"）
        try:
            return json.loads(s)
        except ValueError:
            return s
    return s


def _lookup(context: dict, path: str, default):
    """取路径值；缺失时：写了默认值用默认值，没写则原样抛错（让节点失败而不是静默传空）。"""
    try:
        return _resolve_path(context, path)
    except (KeyError, IndexError, TypeError):
        if default is None:
            raise
        return _coerce_default(default)


def render_template(value, context: dict):
    """递归渲染配置中的 {{path}} / {{path|默认值}} 模板。整串单模板保留原始类型。"""
    if isinstance(value, str):
        full = TEMPLATE_RE.fullmatch(value.strip())
        if full:
            return _lookup(context, full.group(1), full.group(2))

        def repl(m):
            try:
                v = _lookup(context, m.group(1), m.group(2))
            except (KeyError, IndexError, TypeError):
                return ""
            if isinstance(v, (dict, list)):
                return json.dumps(v, ensure_ascii=False)
            if v is None:
                return ""
            return str(v) if not isinstance(v, str) else v

        return TEMPLATE_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, context) for v in value]
    return value


# ---------------------------------------------------------------- URL 处理
# 保留所有 RFC3986 结构字符（含 %，避免二次编码），只转义空格/中文/控制字符
_URL_SAFE = ":/?#[]@!$&'()*+,;=%-._~"
_SECRET_KEYS = ("api_key", "apikey", "access_token", "token", "secret", "password")


def quote_url(url: str) -> str:
    """转义已渲染的 URL 中必须转义的字符（空格、中文等），结构字符原样保留。"""
    return quote(str(url), safe=_URL_SAFE)


def redact_url(url: str) -> str:
    """把 URL 查询串里像密钥的参数值替换成 ***，用于节点执行记录的展示。"""
    head, sep, query = str(url).partition("?")
    if not sep:
        return head
    kept = []
    for pair in query.split("&"):
        key, eq, _val = pair.partition("=")
        if eq and key.lower() in _SECRET_KEYS:
            pair = f"{key}=***"
        kept.append(pair)
    return head + "?" + "&".join(kept)


# headers/body 的敏感键名提示词：命中即整值打码（authorization / cookie 等不在 _SECRET_KEYS 里）
_SECRET_KEY_HINTS = _SECRET_KEYS + ("authorization", "cookie", "x-aip-internal")


def _looks_secret_key(key) -> bool:
    k = str(key).lower()
    return any(hint in k for hint in _SECRET_KEY_HINTS)


def redact_config(config: dict) -> dict:
    """深拷贝并脱敏节点配置，用于 node_runs 执行记录与员工视角的定义展示。

    URL 查询串走 redact_url；headers / body(dict) 中键名含敏感词的值整体打码。
    明文密钥不应进执行记录——配置里有密钥是常态，记录里必须是 ***。
    """
    if not isinstance(config, dict):
        return config
    try:
        out = json.loads(json.dumps(config, ensure_ascii=False))
    except (TypeError, ValueError):
        return dict(config)
    if isinstance(out.get("url"), str):
        out["url"] = redact_url(out["url"])
    if isinstance(out.get("headers"), dict):
        out["headers"] = {k: ("***" if _looks_secret_key(k) else v)
                          for k, v in out["headers"].items()}
    if isinstance(out.get("body"), dict):
        out["body"] = {k: ("***" if _looks_secret_key(k) else v)
                       for k, v in out["body"].items()}
    return out


def redact_definition(defn: dict) -> dict:
    """对整个 DAG 定义的各节点 config 脱敏（员工视角展示用，admin 编辑仍拿原文）。"""
    if not isinstance(defn, dict):
        return defn
    out = json.loads(json.dumps(defn, ensure_ascii=False))
    for nd in out.get("nodes") or []:
        if isinstance(nd.get("config"), dict):
            nd["config"] = redact_config(nd["config"])
    return out


# ---------------------------------------------------------------- http_api URL 安全校验（防 SSRF）
_BLOCKED_HOSTNAMES = ("metadata.google.internal",)


def validate_http_url(url: str, allow_private: bool | None = None) -> None:
    """执行期校验 http_api 目标地址，防 SSRF：
    - scheme 仅 http/https；禁止 URL 内嵌用户凭证（user:pass@host）；
    - 云元数据（169.254.0.0/16 链路本地、metadata.google.internal）任何情况禁止；
    - 私网/回环默认允许（内置 demo 调本机 mock、本地 vLLM 依赖 127.0.0.1），
      生产对接外网系统时置 AIP_HTTP_API_ALLOW_PRIVATE=0 收紧。
    校验后立即发起请求，接受 DNS rebinding 的残余风险（两者间隔毫秒级）。
    """
    allow_private = HTTP_API_ALLOW_PRIVATE if allow_private is None else allow_private
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"URL 非法: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"http_api 仅支持 http/https，当前 scheme: {parts.scheme or '(空)'}")
    if parts.username or parts.password:
        raise ValueError("URL 不允许内嵌用户凭证（user:pass@host）")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("URL 缺少主机名")
    if host in _BLOCKED_HOSTNAMES:
        raise ValueError(f"禁止访问云元数据地址: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"无法解析主机 {host}: {exc}") from exc
    checked: set[str] = set()
    for info in infos:
        ip_str = str(info[4][0]).split("%")[0]
        if ip_str in checked:
            continue
        checked.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_link_local:
            raise ValueError(f"禁止访问链路本地/云元数据地址: {ip}")
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError(f"禁止访问保留/组播地址: {ip}")
        if not allow_private and (ip.is_loopback or ip.is_private):
            raise ValueError(
                f"已禁止访问私网/回环地址: {ip}（生产环境请通过公网域名访问外部 API，"
                "或显式设置 AIP_HTTP_API_ALLOW_PRIVATE=1）")


# ---------------------------------------------------------------- 节点执行
def _exec_node(nd: dict, config: dict, context: dict, services: dict) -> dict:
    """执行单个节点，返回其输出（写入 context[node_id]）。"""
    ntype = nd["type"]

    if ntype == "start":
        return dict(context.get("input") or {})

    if ntype == "end":
        out = config.get("output")
        return {"result": out if out is not None else {}}

    if ntype == "llm":
        from . import llm_gateway

        prompt = config.get("prompt", "")
        system = config.get("system", "")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": str(prompt)})
        # 工作流节点走同一网关：重试瞬时故障 + 写 llm_calls 审计；
        # 但不降级——工作流语义是显式的，模型不可用就该让节点失败并记录原因。
        result = llm_gateway.chat_with_gateway(
            services.get("conn"), provider_name=config.get("provider", "mock"),
            model=config.get("model"), messages=messages, allow_degrade=False,
            user_id=services.get("user_id"), run_id=services.get("run_id"))
        return {"text": result["text"], "provider": result["provider"],
                "model": config.get("model"), "attempts": result["attempts"]}

    if ntype == "knowledge_search":
        from .access import get_user_row, kb_accessible
        from .knowledge import search_kb

        conn = services.get("conn")
        kb_id = int(config.get("kb_id", 0))
        query = str(config.get("query", ""))
        top_k = int(config.get("top_k", 3))
        results: list = []
        if conn and kb_id and query.strip():
            # 知识库部门隔离：运行者无权访问该库时一律返回空，不泄漏任何内容
            user = get_user_row(conn, services.get("user_id"))
            kb = conn.execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
            if kb is not None and user is not None and kb_accessible(conn, kb, user):
                results = search_kb(conn, kb_id, query, top_k)
        return {"results": results, "count": len(results)}

    if ntype == "http_api":
        method = str(config.get("method", "GET")).upper()
        url = str(config.get("url", "")).strip()
        if not url:
            raise ValueError("http_api 节点缺少 url")
        # 推荐用 params 传查询参数（由 httpx 正确百分号编码）；
        # 直接把值插进 url 时也要转义空格/中文，否则请求会被截断或乱码。
        query = config.get("params")
        if query is not None and not isinstance(query, dict):
            raise ValueError("http_api 节点 params 必须是对象")
        url = quote_url(url)
        # SSRF 防护：scheme/凭证/元数据/私网校验（渲染完模板后的最终 URL 也要过一遍，
        # 防止 {{input.params.x}} 之类的用户输入把请求目标带偏）
        validate_http_url(url)
        try:
            timeout = float(config.get("timeout", 15))
        except (TypeError, ValueError):
            timeout = 15.0
        # timeout 钳制：配置成超大值会长期占住线程池线程（同步 def 端点），等效 DoS
        timeout = min(max(timeout, HTTP_API_TIMEOUT_MIN), HTTP_API_TIMEOUT_MAX)
        headers = dict(config.get("headers") or {})
        # 回环地址（本服务 mock 路由 / 本机 vLLM）绝不走系统代理：
        # 开着代理的机器上 httpx 会把 127.0.0.1 转发给代理返回 502（trust_env 默认开）。
        from .providers import _no_proxy_for

        # 本服务 mock 路由需要鉴权：回环请求自动附带进程级内部密钥（不出本机、不进执行记录）
        internal_secret = services.get("internal_secret")
        if internal_secret and _no_proxy_for(url):
            headers.setdefault("X-AIP-Internal", internal_secret)

        with httpx.Client(timeout=timeout, trust_env=not _no_proxy_for(url)) as client:
            resp = client.request(method, url, params=query or None,
                                  headers=headers, json=config.get("body"))
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return {"status_code": resp.status_code, "url": redact_url(str(resp.url)), "data": data}

    if ntype == "code":
        expression = config.get("expression", "")
        variables = config.get("vars") or {}
        if not isinstance(variables, dict):
            raise ValueError("code 节点 vars 必须是对象")
        result = safe_eval(expression, variables)
        return {"result": result}

    if ntype == "condition":
        expression = config.get("expression", "")
        variables = config.get("vars") or {}
        result = safe_eval(expression, variables)
        return {"result": bool(result)}

    raise WorkflowDefinitionError(f"未知节点类型: {ntype}")  # pragma: no cover


def _edge_pass(edge: dict, source_output: dict, context: dict) -> bool:
    """判断边是否可走：无条件=恒真；true/false 匹配 condition 节点结果；其他按安全表达式求值。"""
    cond = edge.get("condition")
    if cond in (None, ""):
        return True
    cond_str = str(cond).strip().lower()
    if cond_str in ("true", "false"):
        return str(bool(source_output.get("result"))).lower() == cond_str
    try:
        return bool(safe_eval(str(cond), dict(context)))
    except (UnsafeExpressionError, ValueError):
        return False


# ---------------------------------------------------------------- 执行器
def execute_workflow(defn: dict, run_input: dict, services: dict,
                     on_node=None) -> dict:
    """按依赖顺序执行 DAG。

    services: {"conn": sqlite连接}（供 llm/knowledge_search 节点使用）
    on_node(node_id, node_type, status, node_input, node_output, error, started, finished):
        节点执行记录回调（用于写 node_runs 表）。
    返回 {"status": "success"/"failed", "output": {...}, "error": str,
          "order": [已执行节点id]}
    """
    validate_definition(defn)
    nodes = {nd["id"]: nd for nd in defn["nodes"]}
    edges_by_src: dict[str, list[dict]] = {}
    for e in defn.get("edges", []):
        edges_by_src.setdefault(e["from"], []).append(e)

    context: dict = {"input": run_input or {}}
    start_id = next(i for i, nd in nodes.items() if nd["type"] == "start")
    queue = deque([start_id])
    visited: set[str] = set()
    order: list[str] = []
    final_output: dict = {}

    while queue:
        nid = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        nd = nodes[nid]
        from .db import now as _now

        started = _now()
        raw_config = nd.get("config") or {}
        try:
            config = render_template(raw_config, context)
            output = _exec_node(nd, config if isinstance(config, dict) else {}, context, services)
        except Exception as exc:  # 节点失败 → 整个运行失败
            error = f"{type(exc).__name__}: {exc}"
            if on_node:
                on_node(nid, nd["type"], "failed", raw_config, {}, error, started, _now())
            return {"status": "failed", "output": final_output, "error": f"节点 {nid} 执行失败: {error}", "order": order}

        context[nid] = output
        order.append(nid)
        if on_node:
            on_node(nid, nd["type"], "success", raw_config, output, "", started, _now())
        if nd["type"] == "end":
            final_output = output.get("result") or {}

        for edge in edges_by_src.get(nid, []):
            if _edge_pass(edge, output, context):
                queue.append(edge["to"])

    if not final_output:
        # 没有显式 end 输出时，取最后一个执行节点的输出
        last = order[-1] if order else None
        final_output = context.get(last, {}) if last else {}
    return {"status": "success", "output": final_output, "error": "", "order": order}


# ---------------------------------------------------------------- 内置示例：销售数据分析
SALES_ANALYSIS_DEFINITION = {
    "nodes": [
        {"id": "start", "type": "start", "name": "开始(上传CSV)"},
        {
            "id": "check", "type": "condition", "name": "数据校验",
            "config": {"expression": "len(rows) > 0", "vars": {"rows": "{{start.rows}}"}},
        },
        {
            "id": "summary", "type": "code", "name": "销售汇总计算",
            "config": {
                "vars": {"rows": "{{start.rows}}"},
                "expression": (
                    "{"
                    "'total_sales': round(sum(float(r['sales']) for r in rows), 2),"
                    "'record_count': len(rows),"
                    "'months': sorted(set(str(r['month']) for r in rows)),"
                    "'monthly_sales': {m: round(sum(float(r['sales']) for r in rows if str(r['month']) == m), 2) for m in sorted(set(str(r['month']) for r in rows))},"
                    "'top_products': [{"
                    "'product': p,"
                    "'sales': round(sum(float(r['sales']) for r in rows if r['product'] == p), 2)"
                    "} for p in sorted(set(r['product'] for r in rows), key=lambda x: -sum(float(r['sales']) for r in rows if r['product'] == x))[:3]],"
                    "'mom_growth_pct': (lambda ms: round((ms[-1][1] - ms[-2][1]) / ms[-2][1] * 100, 2) if len(ms) >= 2 and ms[-2][1] else None)("
                    "[(m, sum(float(r['sales']) for r in rows if str(r['month']) == m)) for m in sorted(set(str(r['month']) for r in rows))])"
                    "}"
                ),
            },
        },
        {
            "id": "report", "type": "llm", "name": "AI生成分析报告",
            "config": {
                "provider": "mock", "model": "mock-data-analysis",
                "system": "你是资深电商数据分析师，请根据汇总数据输出结构化分析报告（中文）。",
                "prompt": "以下是销售数据汇总JSON，请生成分析报告，包含总体表现、TOP产品、环比趋势与建议：\n{{summary.result}}",
            },
        },
        {
            "id": "empty_end", "type": "end", "name": "无数据结束",
            "config": {"output": {"error": "CSV 中没有数据行，请检查上传文件"}},
        },
        {
            "id": "end", "type": "end", "name": "输出报告",
            "config": {"output": {"summary": "{{summary.result}}", "report": "{{report.text}}"}},
        },
    ],
    "edges": [
        {"from": "start", "to": "check"},
        {"from": "check", "to": "summary", "condition": "true"},
        {"from": "check", "to": "empty_end", "condition": "false"},
        {"from": "summary", "to": "report"},
        {"from": "report", "to": "end"},
    ],
}


# ---------------------------------------------------------------- 内置示例：广告数据分析（http_api 节点演示）
AD_ANALYSIS_DEFINITION = {
    "nodes": [
        {"id": "start", "type": "start", "name": "开始"},
        {
            "id": "api", "type": "http_api", "name": "拉取广告数据(模拟API)",
            "config": {
                "method": "GET",
                "url": "http://127.0.0.1:8005/api/mock/ads-data",
                # params 由 httpx 做百分号编码；|双11大促 是未传参数时的默认值
                "params": {"campaign": "{{input.params.campaign|双11大促}}"},
            },
        },
        {
            "id": "calc", "type": "code", "name": "指标计算",
            "config": {
                "vars": {"spend": "{{api.data.spend}}", "sales": "{{api.data.sales}}",
                          "clicks": "{{api.data.clicks}}", "orders": "{{api.data.orders}}"},
                "expression": (
                    "{'acos_pct': round(spend / sales * 100, 2),"
                    " 'cvr_pct': round(orders / clicks * 100, 2),"
                    " 'suggested_daily_budget': round(spend / 30 * 1.25, 2)}"
                ),
            },
        },
        {
            "id": "report", "type": "llm", "name": "AI广告分析",
            "config": {
                "provider": "mock", "model": "mock-data-analysis",
                "system": "你是亚马逊广告优化师，请根据指标给出优化建议（中文）。",
                "prompt": "广告指标：{{calc.result}}，原始数据：{{api.data}}，请给出分析结论。",
            },
        },
        {
            "id": "end", "type": "end", "name": "输出",
            "config": {"output": {"metrics": "{{api.data}}", "analysis": "{{calc.result}}",
                                   "report": "{{report.text}}"}},
        },
    ],
    "edges": [
        {"from": "start", "to": "api"},
        {"from": "api", "to": "calc"},
        {"from": "calc", "to": "report"},
        {"from": "report", "to": "end"},
    ],
}


# ---------------------------------------------------------------- CSV 解析工具
def parse_csv_text(text: str) -> list[dict]:
    """解析 CSV 文本为 list[dict]，数字列自动转 int/float。"""
    import csv as _csv
    import io

    reader = _csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        row = {}
        for k, v in raw.items():
            if k is None:
                continue
            v = (v or "").strip()
            try:
                row[k] = int(v)
            except ValueError:
                try:
                    row[k] = float(v)
                except ValueError:
                    row[k] = v
        if any(str(x) != "" for x in row.values()):
            rows.append(row)
    return rows
