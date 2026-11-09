"""通用 HTTP 请求 Agent —— 让引擎能对接任意 API，而不只是文档流水线。

放在这里的原因是"能力边界"：引擎此前只能跑检索/写作/质检那套领域 Agent，
一个"调外部接口 → 转换 → 通知"的普通工作流表达不出来。本模块与
`transform` / `webhook_notify` 一起构成通用件，任何领域都能用。

安全三条硬规矩（这一条 Agent 是全引擎唯一主动出网的地方，默认收紧）：

1. **URL 必须过 SSRF 校验**，复用 `artesian.url_guard.validate_public_http_url`
   （DNS 全记录解析 + 私网/环回/链路本地/元数据段拒绝）。URL 可以来自配置，也可以
   来自上游产物——工作流引擎必须由数据决定调用哪个接口，但**两条路都走同一个校验**，
   没有"关掉校验"的配置项。要访问内网服务只能显式列 `allow_hosts`，命中时结果里
   带 `guard: "allowlist"` 并在日志里说明，不做无声放行。
2. **不跟随重定向**（`allow_redirects=False`）。跟着跳一次就把"校验过的 URL"换成
   "没校验过的 URL"，3xx 一律如实返回给下游判断。
3. **响应体有上限**，超了就是 `blocked`（业务失败），不静默截断——截断后的 JSON
   会让下游拿到看起来合法的残缺数据。

载荷键 `response` 是声明过的产物（`PRODUCES`），下游按契约取用。
"""

import json
from urllib.parse import urlparse

import requests

from artesian.url_guard import validate_public_http_url
from loom_core.base_agent import BaseAgent, Message

AGENT_NAME = "http_request"
AGENT_VERSION = "1.0"
AGENT_DESC = "通用 HTTP 请求 Agent（SSRF 校验默认开启，产物为 response）"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 50

CONFIG_SCHEMA = {
    # 留空则从载荷取 payload["url"]（由上游产物决定调用哪个接口）
    "url": ("str", ""),
    "method": ("str", "GET"),
    "headers": ("dict", {}),
    # 用哪个上游产物当请求体；留空表示不带体
    "body_artifact": ("str", ""),
    "timeout_s": (["int", "float"], 15.0),
    "max_bytes": ("int", 200_000),
    # json | text —— json 解析失败会如实报错，不会退化成"字符串也算成功"
    "expect": ("str", "json"),
    # 显式内网放行名单（精确主机名）。默认空 == 只允许公网。
    "allow_hosts": ("list", []),
}

INPUT_TOPICS = ["http_request.input"]
OUTPUT_TOPICS = ["http_request.done", "http_request.failed"]
PRODUCES: dict = {"response": "last"}
CONSUMES: list = []
DEPENDENCIES: list = []
# 不进 legacy 自动图：legacy 不读 YAML，拿不到节点级 url，在这里它必然失败。
# 声明式流水线（pipelines/api-report.yaml）照旧能调它。
LEGACY_AUTO = False
CACHE_TTL = 0
RESPAWN = False
AGENT_TAGS = ["generic", "io", "http"]

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_SECRET_HEADER_HINTS = ("authorization", "token", "secret", "cookie", "api-key", "apikey")


def _host_allowed(url: str, allow_hosts: list) -> bool:
    """内网放行只看精确主机名，不做前缀/后缀匹配。

    `allow_hosts: ["db.internal"]` 不该顺带放行 `evil-db.internal.attacker.com`，
    所以比对的是 urlparse 的 hostname 全等（大小写不敏感）。
    """
    host = (urlparse(url).hostname or "").lower()
    lowered = {str(h).strip().lower() for h in (allow_hosts or []) if str(h).strip()}
    return bool(host) and host in lowered


def _redacted(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        key = str(k).lower()
        out[k] = "***" if any(h in key for h in _SECRET_HEADER_HINTS) else v
    return out


class HttpRequestAgent(BaseAgent):
    """按配置或上游产物发起一次 HTTP 请求，产出 `response`。"""

    def handle(self, msg: Message) -> dict:
        payload = msg.payload or {}
        # 节点级配置从载荷进来（Scheduler 把 YAML 的 config 与构造期配置合并后
        # 放进 payload["config"]）；实例配置只作兜底，否则 YAML 里的 url 会被忽略。
        cfg = {**(self.config or {}), **(msg.payload or {}).get("config", {})}

        url = str(cfg.get("url") or payload.get("url") or "").strip()
        if not url:
            return {"status": "error", "error": "未给出 url（config.url 或上游产物 url）"}

        method = str(cfg.get("method") or "GET").upper()
        if method not in _ALLOWED_METHODS:
            return {"status": "error",
                    "error": f"method 非法: {method}（可用 {', '.join(sorted(_ALLOWED_METHODS))}）"}
        expect = str(cfg.get("expect") or "json").lower()
        if expect not in ("json", "text"):
            return {"status": "error", "error": f"expect 非法: {expect}（只支持 json/text）"}

        guard, reason = validate_public_http_url(url)
        allowlisted = _host_allowed(url, cfg.get("allow_hosts") or [])
        if not guard and not allowlisted:
            # 这是本 Agent 唯一的安全边界：拒绝就是业务失败，绝不"降级为直接请求"
            self.log_warning(f"出站请求被拒绝: {reason}")
            return {"status": "blocked", "error": f"SSRF 校验未通过: {reason}", "url": url}

        headers = dict(cfg.get("headers") or {})
        body = None
        want_body = str(cfg.get("body_artifact") or "")
        if want_body:
            artifacts = payload.get("upstream") or {}
            if want_body not in artifacts:
                return {"status": "error",
                        "error": f"body_artifact={want_body!r} 不在上游产物里"
                                 f"（可用: {sorted(k for k in artifacts if isinstance(k, str))}）"}
            body = artifacts[want_body]

        timeout = float(cfg.get("timeout_s") or 15.0)
        max_bytes = int(cfg.get("max_bytes") or 200_000)
        try:
            resp = requests.request(method=method, url=url, headers=headers, json=body,
                                    timeout=timeout, allow_redirects=False, stream=True)
        except Exception as e:  # 网络异常如实上报，不伪装成空响应
            self.log_error(f"请求失败 {method} {url}: {e}")
            return {"status": "error", "error": f"{type(e).__name__}: {e}", "url": url}

        try:
            raw = b""
            too_large = False
            for chunk in resp.iter_content(chunk_size=8192):
                raw += chunk
                if len(raw) > max_bytes:
                    too_large = True
                    break
        finally:
            resp.close()

        meta = {"url": url, "method": method, "http_status": resp.status_code,
                "headers": _redacted(dict(resp.headers)),
                "bytes": len(raw), "guard": "allowlist" if allowlisted and not guard else "public"}
        if too_large:
            over = (f"响应超过 max_bytes={max_bytes}，已中止读取"
                    "（不截断：残缺 JSON 会让下游拿到看似合法的坏数据）")
            meta["status"] = "blocked"
            meta["error"] = over
            self.log_warning(over)
            return meta

        # 3xx 先返回：跟不跟随由下游决定，引擎不代替作者绕过出网校验。
        # 这一步放在解析之前是有意的——重定向响应体经常是空的，
        # 让"不是合法 JSON"抢先把结果判死，作者就看不到 Location 了。
        if 300 <= resp.status_code < 400:
            meta["status"] = "ok"
            meta["redirect_to"] = resp.headers.get("location", "")
            self.log_warning(f"收到 {resp.status_code} 重定向，未跟随: {meta['redirect_to']!r}")
            return meta

        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        if expect == "json":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                meta["status"] = "error"
                meta["error"] = f"响应不是合法 JSON: {e}"
                meta["response"] = text[:500]
                return meta
            meta["response"] = parsed
        else:
            meta["response"] = text

        if resp.status_code >= 400:
            # 4xx/5xx 是业务失败：让节点失败，而不是把错误页当成正常产物往下传
            meta["status"] = "error"
            http_err = f"HTTP {resp.status_code}"
            meta["error"] = http_err
            self.log_warning(http_err)
        else:
            meta["status"] = "ok"
        return meta
