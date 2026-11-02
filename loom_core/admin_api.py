"""
Admin API v1 - 轻量级 REST API（零外部依赖）
=============================================
端点：
  GET  /health              → 总线 + Registry 健康状态
  GET  /api/health/deep     → 全组件深度健康检查（LLM/搜索/缓存/检查点）
  GET  /metrics             → Prometheus 格式指标
  GET  /tasks               → 所有任务列表
  GET  /tasks/<id>          → 单任务详情（含 result/output_content）
  POST /api/tasks           → 提交新文档生成任务（body: query/title/pipeline/wait/output）
  POST /tasks/<id>/cancel   → 取消任务
  POST /tasks/<id>/rerun    → 重跑流水线（复用上一次 plan + 输入）
  GET  /agents              → 已注册 agent 列表
  GET  /api/agents/<name>   → 单个 agent 详情（含 stats/meta）
  GET  /api/config          → 运行时配置快照
  POST /api/config          → 运行时配置更新（body: {"key": "llm.model", "value": "xxx"}）
  GET  /api/cache           → 缓存统计
  POST /api/cache/clear     → 清空所有缓存
  GET  /api/events/hooks    → 列出已注册事件钩子
  POST /api/events/hooks    → 注册事件钩子（body: {"event": "task.completed", "url": "https://..."}）
  DELETE /api/events/hooks/<id> → 注销事件钩子
  GET  /dlq                 → 死信队列
  POST /dlq/<id>/replay     → 重放死信
  GET  /stream?task_id=<id>&query=...  → SSE 流式推送文档生成进度

鉴权：
  - 通过环境变量 ADMIN_API_KEY 启用（非空时开启）
  - 客户端需在 Header 携带 `Authorization: Bearer <key>`
  - /health 和静态资源免鉴权

危险操作二次确认：
  - POST /api/config、/api/cache/clear、/api/versions/rollback、/dlq/<id>/replay
    （GET 同）除鉴权外必须携带请求头 `X-Confirm: yes`，缺失返回
    428 Precondition Required；执行时输出结构化审计日志
    （时间/key 身份/操作/参数摘要）
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

from artesian.fast_json import dumps as _fast_dumps
from artesian.fast_json import loads as _fast_loads

from . import scheduler as _scheduler_mod
from .artifacts import task_output
from .ids import new_task_id
from .input_schema import InputValidationError, gate_freeform, prepare_inputs
from .scheduler import resolve_pipeline_name
from .tokens import LOCAL_PRINCIPAL, Principal, TokenConfigError, TokenStore, parse_bearer, token_from_query

_logger = logging.getLogger(__name__)


# ─── 安全防护辅助 ─────────────────────────────
# 安全修复 (P0): 集中定义 SSRF 防护与路径白名单校验，供各端点复用

# task_id 仅允许字母、数字、下划线、连字符（防止路径遍历 ../../）
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# 访问日志脱敏：?token= 查询串凭证打码为 ***（SSE 场景才建议 query token）
_QS_TOKEN_RE = re.compile(r"(token=)[^&\s]+")

# 请求体大小上限（S4）：防 Content-Length 任意值直接 read 的内存 DoS。
# 合法请求（任务提交/回滚/钩子注册均为 JSON 文本）远低于 1 MB，取 10 MB 留余量。
_MAX_BODY_BYTES = 10 * 1024 * 1024


def mask_token_query(text: str) -> str:
    return _QS_TOKEN_RE.sub(r"\1***", text)


def _is_private_ip(host: str) -> bool:
    """判断 host 是否为私有/保留 IP 地址（SSRF 防护）。

    覆盖：私有段（10/8、172.16/12、192.168/16）、环回（127/8、::1）、
    链路本地（169.254/16、fe80::/10）、元数据端点（169.254.169.254）、
    未分配/组播/保留段、IPv6 私有（fc00::/7）。
    """
    try:
        # 解析为 IP 地址对象（自动区分 v4/v6）
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 非 IP 字面量（可能是域名），交由调用方决定是否放行
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _validate_webhook_url(url: str) -> tuple[bool, str]:
    """校验 webhook URL 安全性（SSRF 防护）。

    Returns:
        (ok, reason): ok=True 表示通过；ok=False 时 reason 为拒绝原因。
    """
    if not url or not isinstance(url, str):
        return False, "url 为空"
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"url 解析失败: {e}"
    # 仅允许 http/https 协议（拒绝 file://、gopher://、ftp:// 等）
    if parsed.scheme not in ("http", "https"):
        return False, f"协议 {parsed.scheme!r} 不允许，仅支持 http/https"
    host = parsed.hostname or ""
    if not host:
        return False, "url 缺少 host"
    # 拒绝裸 IP 形式的私有/保留地址（域名解析后的 SSRF 由出网层兜底，此处先拦裸 IP）
    if _is_private_ip(host):
        return False, f"host {host!r} 为私有/保留 IP，已拒绝（SSRF 防护）"
    return True, ""


def _default_pipeline_from(orch) -> str:
    """运行期配置里的 default_pipeline——只认真字符串（配置对象可能是 mock）。"""
    cfg = getattr(orch, "config", None)
    value = cfg.get("default_pipeline", "") if cfg is not None and hasattr(cfg, "get") else ""
    return value if isinstance(value, str) else ""


def _validate_output_path(path_str: str, base_dir: str | None = None) -> tuple[bool, str]:
    """校验输出路径必须落在 base_dir（默认 cwd）范围内（防止任意文件写入）。

    Returns:
        (ok, reason): ok=True 表示通过；ok=False 时 reason 为拒绝原因。
    """
    if not path_str:
        return True, ""  # 空路径不校验（交由默认逻辑）
    base = Path(base_dir or os.getcwd()).resolve()
    try:
        target = Path(path_str).resolve()
    except (OSError, ValueError) as e:
        return False, f"路径解析失败: {e}"
    try:
        target.relative_to(base)
    except ValueError:
        return False, f"路径 {path_str!r} 不在允许目录 {base!s} 内"
    return True, ""


def _validate_task_id(task_id: str) -> bool:
    """校验 task_id 仅含字母/数字/下划线/连字符（防止路径遍历）。"""
    if not task_id or len(task_id) > 128:
        return False
    return bool(_TASK_ID_RE.match(task_id))


def _workflow_route(path: str) -> tuple[str, str] | None:
    """解析 `/api/workflows/{name}/{sub}` → `(name, sub)`，形状不对返回 None。

    流水线名沿用 `task_id` 那套字符白名单（随包 20 条流水线名全在其中），目的是只拿
    **一段**、不含分隔符/遍历的名字；真正的存在性判断仍交给 `resolve_pipeline_name`，
    这里不做第二套名单，免得两处白名单各漂各的。
    """
    parts = path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4 or parts[0] != "api" or parts[1] != "workflows":
        return None
    name, sub = parts[2], parts[3]
    if not _validate_task_id(name):
        return None
    return name, sub


# ─── 流式指标聚合 ─────────────────────────────
# streaming.py 仅提供单回调粒度的 StreamMetrics，未提供全局聚合接口；
# 此处在 admin_api 侧维护 task_id -> callback 注册表并汇总快照。

class _StreamMetricsHub:
    """聚合所有活跃 StreamCallback 的流式指标（线程安全）"""

    _COUNTER_KEYS = ("events_emitted", "events_dropped", "chunks_emitted", "sections_emitted")

    def __init__(self):
        self._lock = threading.Lock()
        self._callbacks: dict[str, Any] = {}
        self._started_at = time.time()

    def track(self, task_id: str, callback: Any) -> None:
        with self._lock:
            self._callbacks[task_id] = callback

    def untrack(self, task_id: str) -> None:
        with self._lock:
            self._callbacks.pop(task_id, None)

    @staticmethod
    def _cb_snapshot(cb: Any) -> dict:
        try:
            return dict(cb.metrics.snapshot())
        except Exception:
            return {}

    def snapshot(self) -> dict:
        with self._lock:
            callbacks = list(self._callbacks.values())
        totals = {k: 0 for k in self._COUNTER_KEYS}
        latency_weighted = 0.0
        for cb in callbacks:
            snap = self._cb_snapshot(cb)
            if not snap:
                continue
            for k in self._COUNTER_KEYS:
                totals[k] += int(snap.get(k, 0) or 0)
            emitted = int(snap.get("events_emitted", 0) or 0)
            if emitted > 0:
                latency_weighted += float(snap.get("avg_latency", 0.0) or 0.0) * emitted
        elapsed = time.time() - self._started_at
        total_events = totals["events_emitted"]
        return {
            **totals,
            "active_streams": len(callbacks),
            "elapsed": elapsed,
            "events_per_sec": (total_events / elapsed) if elapsed > 0 else 0.0,
            "avg_latency": (latency_weighted / total_events) if total_events > 0 else 0.0,
        }


_STREAM_METRICS_HUB = _StreamMetricsHub()

_EXTRA_CORS_ORIGINS = tuple(
    o.strip() for o in os.environ.get("ADMIN_CORS_ORIGINS", "").split(",") if o.strip()
)


def track_stream_callback(task_id: str, callback: Any) -> None:
    """登记活跃流回调（供 /stream/metrics 聚合）"""
    _STREAM_METRICS_HUB.track(task_id, callback)


def untrack_stream_callback(task_id: str) -> None:
    _STREAM_METRICS_HUB.untrack(task_id)


def stream_metrics_snapshot() -> dict:
    """全部活跃流的指标快照"""
    return _STREAM_METRICS_HUB.snapshot()



DEFAULT_MAX_CONCURRENCY = 32
DEFAULT_SOCKET_TIMEOUT = 30.0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """线程-per-连接 + **有界并发**（spec §3 P1 点名要替换的旧形态的替代品）。

    旧形态两个缺陷：① stdlib `HTTPServer` 默认 HTTP/1.0——无 keep-alive，
    dashboard 每 5 秒轮询就是每 5 秒新建 TCP + 新线程；② `ThreadingMixIn`
    线程-per-连接且**无上限**——每个提交一个数分钟 LLM 流水线的请求占一条线程，
    慢客户端/半开连接可以无限占线程直到进程崩。

    做法（2026-10-13 决策 A：保留 stdlib，消灭两个缺陷，不引入外部框架）：
    - 请求处理提交给**有界线程池**（`max_workers`，池满即 503 + Retry-After，
      不排队不炸线程数）；
    - HTTP/1.1 keep-alive 在 `AdminHandler.protocol_version` 上开（配套：
      所有响应带 `Content-Length`、SSE 用完即 `close_connection`）；
    - 读/写 socket 超时在 `AdminHandler.timeout` 上设（半开连接不再永久占线程）。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    #: 拒答时回收接收队列的字节上限（只取已在队列里的，不为读全请求体而等）
    DRAIN_LIMIT = 65536
    #: 单次拒答允许等待请求字节落地的上限（秒）
    REJECT_WAIT = 0.05
    #: 等待配额的重置周期（秒）：每周期 accept 线程总共只允许牺牲 REJECT_WAIT
    REJECT_BUDGET_WINDOW = 1.0

    def __init__(self, *args, max_workers: int = DEFAULT_MAX_CONCURRENCY, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers,
                                        thread_name_prefix="admin-api")
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._budget_left = self.REJECT_WAIT
        self._budget_at = time.monotonic()

    # ── 有界派发 ──────────────────────────────────────

    def process_request(self, request, client_address):
        """accept 线程只做派发：池满直接 503，否则提交池里执行。"""
        with self._inflight_lock:
            saturated = self._inflight >= self.max_workers
            if not saturated:
                self._inflight += 1
        if saturated:
            self._reject_saturated(request)
            return
        try:
            self._pool.submit(self._process_pooled, request, client_address)
        except RuntimeError:  # 池已关闭（stop 之后迟到的连接）
            with self._inflight_lock:
                self._inflight -= 1
            self._reject_saturated(request)

    def _process_pooled(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    def _budget_now(self) -> float:
        """本次拒答最多可以等多久（秒）——accept 线程的等待配额。

        只在 accept 线程里调用（`serve_forever` 单线程派发），无需加锁。
        """
        now = time.monotonic()
        if now - self._budget_at >= self.REJECT_BUDGET_WINDOW:
            self._budget_left = self.REJECT_WAIT
            self._budget_at = now
        return min(self._budget_left, self.REJECT_WAIT)

    def _drain_received(self, request, wait: float) -> float:
        """收回即将随 close 一起被 RST 掉的请求字节，返回实际等待秒数。

        TCP 通则（Windows/Linux 一致）：close 时接收队列非空 ⇒ 内核发 RST ⇒ 刚写进去的
        503 在客户端被丢弃，`recv` 报 10053/10054。池满时这条连接压根没被服务，客户端
        的请求字节要么还在路上、要么就躺在队列里，所以回收是"503 能送达"的前提而非收尾。
        先等 `wait` 让字节落地（本机实测正常客户端 8µs~24ms），再非阻塞取空。
        """
        t0 = time.perf_counter()
        with contextlib.suppress(OSError):
            if wait > 0:
                select.select([request], [], [], wait)
            request.setblocking(False)
            drained = 0
            while drained < self.DRAIN_LIMIT:
                chunk = request.recv(4096)
                if not chunk:
                    break
                drained += len(chunk)
        return time.perf_counter() - t0

    def _reject_saturated(self, request):
        """池满：回收请求字节 → 写 503 → 补收一次 → 优雅关闭。"""
        body = b'{"error": "server busy"}'
        spent = self._drain_received(request, self._budget_now())
        self._budget_left = max(0.0, self._budget_left - spent)
        with contextlib.suppress(OSError):
            # 回收步骤把套接字调成了非阻塞；写 503 必须恢复阻塞写，否则发送缓冲
            # 一满就 BlockingIOError，被上面的 suppress 咽掉 ⇒ 又变成"响应送不到"。
            request.setblocking(True)
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Retry-After: 1\r\n"
                b"Connection: close\r\n\r\n" + body)
        # sendall 期间可能又有字节落地，再补收一次（非阻塞，配额已用掉）
        self._drain_received(request, 0.0)
        self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False)

    @property
    def inflight(self) -> int:
        with self._inflight_lock:
            return self._inflight


#: 旧名保留（内部/外部可能 import）：线程-per-连接的语义由有界版接管
ThreadingHTTPServer = BoundedThreadingHTTPServer


class AdminHandler(BaseHTTPRequestHandler):
    """REST API 请求处理"""

    #: HTTP/1.1 keep-alive（2026-10-13 P1 决策 A）。前提：**每个响应都必须带
    #: Content-Length**——否则客户端永远等不到响应体结束。三处写响应的地方
    #: （_json/_text/_prometheus）+ do_OPTIONS 的 403 分支都已补齐；SSE 是长
    #: 响应流，用完显式 `close_connection = True`，不参与复用。
    protocol_version = "HTTP/1.1"
    #: socket 读/写超时沿用基类 StreamRequestHandler.timeout 属性（秒）：
    #: 半开连接不再永久占着一条（有界）线程。None = 不限；
    #: 由 AdminAPI.start 从 LOOM_ADMIN_SOCKET_TIMEOUT 注入（此处不重新声明，
    #: 否则 mypy 判"与基类属性冲突"）。

    orch: Any = None  # 由 AdminAPI 注入
    dashboard_dir: str | None = None  # 静态文件目录
    api_key: str | None = None  # 由 AdminAPI 注入
    server_host: str = "127.0.0.1"  # 由 AdminAPI 注入（本机信任模式判定）
    #: 入站 webhook 配置（webhooks.load_webhooks 的产物），由 AdminAPI 注入；
    #: 空 dict = 未配置（所有 /api/webhooks/* 请求 404）
    webhooks: dict = {}

    def _parse_query(self) -> dict:
        """解析 URL 查询串为 dict（不含 URL 解码以外的处理）"""
        if "?" not in self.path:
            return {}
        qs = self.path.split("?", 1)[1]
        return dict(
            kv.split("=", 1) for kv in qs.split("&") if "=" in kv
        )

    def _allowed_origin(self, origin: str | None) -> str | None:
        """CORS 白名单判定。

        仅放行：
          - ADMIN_CORS_ORIGINS 环境变量显式配置的 Origin
          - 与 Host 头完全一致的同源请求
          - 回环地址（127.0.0.1/localhost/::1）且端口与本服务一致
        其余一律拒绝（修复：原先无条件回 ACAO:* 导致任意网页可跨站调用本机 API）。
        """
        if not origin:
            return None
        origin = origin.strip()
        if origin in _EXTRA_CORS_ORIGINS:
            return origin
        try:
            parsed = urlparse(origin)
        except Exception:
            return None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        host_header = (self.headers.get("Host") or "").strip()
        if host_header and origin == f"{parsed.scheme}://{host_header}":
            return origin
        try:
            server_port = self.server.server_address[1]  # type: ignore[index]
        except Exception:
            server_port = None
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = parsed.hostname.lower()
        if hostname in ("127.0.0.1", "localhost", "::1") \
                and server_port is not None and origin_port == server_port:
            return origin
        return None

    def _apply_cors_headers(self):
        """按白名单回显 Origin；非白名单请求不带 ACAO 头（浏览器侧拦截响应）"""
        allowed = self._allowed_origin(self.headers.get("Origin"))
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def _json(self, data: dict, status: int = 200):
        body = _fast_dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._apply_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _prometheus(self, text: str):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = 200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self._apply_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard_index(self) -> bool:
        """`GET /` → 控制台入口页（`loom_core/dashboard/index.html`）。

        没有 dashboard_dir 时返回 False，调用方退回纯文本 API 索引。
        """
        if not self.dashboard_dir:
            return False
        path = Path(self.dashboard_dir) / "index.html"
        try:
            body = path.read_bytes()
        except OSError:
            return False
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._apply_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _serve_static(self, path: str) -> bool:
        """尝试从 dashboard_dir 提供静态文件"""
        if not self.dashboard_dir:
            return False
        # 只处理 .html/.js/.css 文件
        if not any(path.endswith(ext) for ext in (".html", ".js", ".css", ".png", ".svg", ".ico", ".json")):
            return False
        # 安全处理：防止路径遍历。
        # 关键：file_path 必须先 resolve 再做 relative_to —— relative_to 是词法
        # 前缀比较，未 resolve 的路径中 `../` 不会被消解，"dashboard/../../x.json"
        # 能通过校验而文件系统按真实路径读取（已实测可绕过）。resolve 后比较
        # 同时兜住绝对路径拼接（Windows 下 Path / "C:/x" 会丢弃基路径）。
        clean_path = path.lstrip("/")
        file_path = (Path(self.dashboard_dir) / clean_path).resolve()
        if not file_path.exists() or not file_path.is_file():
            return False
        # 确保文件在 dashboard_dir 内
        try:
            file_path.relative_to(Path(self.dashboard_dir).resolve())
        except ValueError:
            return False
        try:
            content = file_path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(file_path))
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self._apply_cors_headers()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return True
        except Exception:
            return False


    # ─── 版本管理端点 ─────────────────────────────

    def _handle_versions_list(self, file_path: str):
        """GET /api/versions?file=<path> — 获取文件版本历史"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件读取
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            history = vm.history(file_path, limit=50)
            self._json({"status": "ok", "file": file_path, "versions": history, "count": len(history)})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_diff(self, file_path: str, v1: int, v2: int):
        """GET /api/versions/diff?file=<path>&v1=N&v2=M — 对比两版本"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件读取
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            diff_text = vm.diff(file_path, v1, v2)
            self._json({"status": "ok", "file": file_path, "v1": v1, "v2": v2, "diff": diff_text})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_rollback(self, file_path: str, version: int):
        """POST /api/versions/rollback — 回滚到指定版本"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件写入
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            result = vm.rollback(file_path, version)
            status_code = 200 if result["status"] == "ok" else 404
            self._json(result, status_code)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_stats(self):
        """GET /api/versions/stats — 版本管理统计"""
        try:
            from .version_manager import get_version_manager
            vm = get_version_manager()
            self._json({"status": "ok", **vm.stats()})
        except Exception as e:
            self._json({"error": str(e)}, 500)


    # 回环地址集合：未配置 API key 时仅对这些绑定地址放行（本机信任模式）。
    # 注意不含 ""：socket 语义中 host="" 绑定所有网卡，属于非回环绑定，
    # 必须要求 ADMIN_API_KEY（否则空串可静默绕过启动安全门与运行时鉴权）
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

    #: token 身份（spec §5.4）：TokenStore | None。
    #: None / 未启用 = "无身份世界"——行为与既往完全一致（单 key 或本机信任）。
    tokens: TokenStore | None = None
    #: 本请求解析出的 Principal（_resolve_principal 填充；鉴权通过后必有值）
    principal: Principal | None = None

    def _resolve_principal(self) -> Principal | None:
        """凭证 → Principal（鉴权矩阵，spec §5.4）。

        优先序与兼容承诺：
          1. Bearer/?token= 命中 `ADMIN_API_KEY` → admin Principal（旧部署零改动）；
          2. tokens.yaml 启用（TokenStore.enabled）→ 命中任一 token 得对应
             Principal；**未命中/无凭证一律 None（回环也不例外）**——启用身份
             却对回环免检等于没启用；
          3. 未启用 tokens.yaml：有 api_key 时同 1（无凭证 None）；无 api_key 且
             回环 → LOCAL_PRINCIPAL（本机信任模式，与既往一致）。
        """
        token = parse_bearer(self.headers.get("Authorization", "")) or token_from_query(self.path)
        if self.api_key and token and hmac.compare_digest(token, self.api_key):
            return Principal(id="admin", admin=True, source="admin_key")
        store = getattr(self, "tokens", None)
        if store is not None and store.enabled:
            return store.resolve(token) if token else None
        if self.api_key:
            return None
        if getattr(self, "server_host", "127.0.0.1") in self._LOOPBACK_HOSTS:
            return LOCAL_PRINCIPAL
        return None

    def _check_auth(self) -> bool:
        """校验凭证并缓存本请求的 Principal（_resolve_principal 的薄封装）。

        无凭证失败时 principal=None（调用方给 401）；/health 与静态资源已在
        路由层豁免。
        """
        self.principal = self._resolve_principal()
        return self.principal is not None

    # ── 作用域（token 身份，spec §5.4）──

    def _principal(self) -> Principal:
        """当前请求的 Principal（鉴权通过后必有；兜底给 LOCAL 防漏网）。"""
        return self.principal or LOCAL_PRINCIPAL

    def _scoped_task(self, task_id: str):
        """按作用域取**内存里**的任务；不可见（不存在或属他人）返回 None。

        **越权统一 404 而不是 403**：403 会泄露"这个任务存在、只是不给你"——
        跨租户场景里存在性本身就是信息。非 admin 且 owner 不匹配 → 视同不存在。
        内存只是缓存：要判"这次运行是否存在"请用 `_scoped_run`（投影层）。
        """
        task = self.orch.get_task(task_id) if self.orch else None
        if task is None:
            return None
        p = self._principal()
        if p.admin:
            return task
        if str(getattr(task, "owner", "") or "") == p.id:
            return task
        return None

    def _scoped_run(self, task_id: str) -> dict | None:
        """按作用域取 RunStore 投影；不可见（不存在或属他人）返回 None。

        与 `_scoped_task` 同一条口径：非 admin 且 principal 不匹配 ⇒ 视同不存在。
        空串归属的行（无身份世界/历史遗留）只对 admin 可见——与 `/tasks` 的过滤
        规则一致，否则"具名 token 看不到空串任务"会在列表与详情之间自相矛盾。
        """
        if not self.orch:
            return None
        try:
            rec = self.orch.run_handle(task_id).record()
        except Exception:  # noqa: BLE001 —— 投影读不动按不存在处理，不打 500
            return None
        if not isinstance(rec, dict):
            return None
        p = self._principal()
        if p.admin:
            return rec
        return rec if str(rec.get("principal") or "") == p.id else None

    def _require_admin(self) -> bool:
        """运维级只读面（日志/质量/告警）只对 admin 开放：跨租户聚合会泄露任务内容。"""
        if self._principal().admin:
            return True
        self._json({"error": "admin required"}, 403)
        return False

    def _can_approve(self, task_id: str) -> bool:
        """审批写操作的可见性：内存任务沿用 `_scoped_task`；已不在内存的
        运行看持久台账里的 principal（admin 全见）。越权同样按不存在处理。"""
        if self._scoped_task(task_id) is not None:
            return True
        if not self.orch:
            return False
        try:
            row = self.orch.approvals.pending_for_run(task_id)
        except Exception:  # noqa: BLE001
            return False
        # 只认台账真给出来的 dict：替身/损坏的 store 返回别的东西时不能当成"有待办"
        if not isinstance(row, dict):
            return False
        p = self._principal()
        return bool(p.admin) or str(row.get("principal") or "") == p.id

    def log_message(self, fmt, *args):
        """访问日志拼装点：requestline 中的 ?token= 凭证打码后再输出"""
        masked = tuple(mask_token_query(a) if isinstance(a, str) else a for a in args)
        sys.stderr.write(
            f"{self.address_string()} - - "
            f"[{self.log_date_time_string()}] {fmt % masked}\n")

    def _require_confirm(self, action: str, params: dict | None = None) -> bool:
        """危险操作二次确认门：X-Confirm: yes 放行并落审计日志，缺失返回 428"""
        if (self.headers.get("X-Confirm") or "").strip().lower() != "yes":
            self._json({"error": "missing X-Confirm header"}, 428)
            return False
        self._audit_dangerous_op(action, params or {})
        return True

    def _audit_dangerous_op(self, action: str, params: dict) -> None:
        with contextlib.suppress(Exception):
            from .observability import get_logger
            get_logger().info(
                f"[AdminAPI] 危险操作审计: {action}",
                action=action,
                key_id=self._key_identity(),
                client=self.client_address[0],
                params=_fast_dumps(params, default=str),
            )

    def _key_identity(self) -> str:
        """凭证指纹（sha256 前 12 位），避免审计日志落明文 key"""
        candidates: list[str] = []
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[len("Bearer "):].strip())
        if "?" in getattr(self, "path", ""):
            for kv in self.path.split("?", 1)[1].split("&"):
                if kv.startswith("token="):
                    candidates.append(kv[len("token="):])
        for tok in candidates:
            if tok:
                return "sha256:" + hashlib.sha256(tok.encode()).hexdigest()[:12]
        return "trust" if not self.api_key else "anonymous"

    def _sync_shared_config(self) -> None:
        """别的进程（worker / CLI / 另一个 Admin API）改过的运行时配置，本进程跟着生效。

        每个请求入口鉴权后调一次（覆写文件一次 stat，无变更即返回）。失败只记
        日志保留旧配置——配置同步失败不该把请求打成 500。
        """
        orch = self.orch
        if not orch or not hasattr(orch, "refresh_config_if_changed"):
            return
        try:
            orch.refresh_config_if_changed()
        except Exception as e:  # noqa: BLE001 —— 同步失败不该炸请求
            _logger.warning("[AdminAPI] 共享配置同步失败，保留旧配置: %s", e)

    def do_GET(self):
        try:
            # 静态资源免鉴权
            if self._serve_static(self.path):
                return
            # 控制台入口 `/`：只发静态壳，数据端点各自鉴权
            # （浏览器要先拿到页面才谈得上填 token；壳里没有任何租户数据）
            if self.path == "/" and self._serve_dashboard_index():
                return
            # 健康端点免鉴权（只读，供监控探针使用）
            if self.path == "/health":
                self._handle_health()
                return
            # 安全修复 (P0): /stream 触发文档生成会消耗 LLM 配额并执行流水线，
            # 必须鉴权；原先置于 _check_auth() 之前导致任意未授权客户端可滥用
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return
            self._sync_shared_config()
            wf_route = _workflow_route(self.path)
            if self.path.startswith("/stream"):
                self._handle_stream()
                return

            if self.path == "/metrics":
                self._handle_metrics()
            elif self.path == "/tasks":
                self._handle_list_tasks()
            elif self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if not _validate_task_id(task_id):
                    return self._json({"error": "invalid task id"}, 400)
                # S3 节点检查器：/tasks/<id>/nodes/<name>（静态段优先于动作段匹配）
                if "/nodes/" in self.path:
                    node_name = self.path.split("/nodes/", 1)[1].split("?", 1)[0]
                    return self._handle_node_inspector(task_id, node_name)
                if self.path.endswith("/cancel"):
                    self._handle_cancel_task(task_id)
                elif self.path.endswith("/rerun"):
                    self._handle_rerun_task(task_id)
                elif self.path.endswith("/pause"):
                    self._handle_pause_task(task_id)
                elif self.path.endswith("/resume"):
                    self._handle_resume_task(task_id)
                else:
                    self._handle_get_task(task_id)
            elif self.path == "/agents":
                self._handle_list_agents()
            elif self.path.startswith("/api/agents/"):
                agent_name = self.path.split("/api/agents/")[1].split("/")[0]
                self._handle_agent_detail(agent_name)
            elif self.path == "/api/config":
                self._handle_config_get()
            elif self.path.split("?", 1)[0] == "/api/config/audit":
                self._handle_config_audit()
            elif self.path == "/api/health/deep":
                self._handle_health_deep()
            elif self.path == "/api/cache":
                self._handle_cache_stats()
            elif self.path == "/api/cost":
                self._handle_cost_stats()
            elif self.path == "/api/events/hooks":
                self._handle_list_hooks()
            elif self.path == "/api/webhooks":
                configured = getattr(self, "webhooks", {}) or {}
                self._json({"webhooks": [
                    {"name": w.name, "pipeline": w.pipeline, "enabled": w.enabled,
                     "secret_env": w.secret_env, "output": w.output}
                    for w in sorted(configured.values(), key=lambda x: x.name)]})
            elif self.path == "/dlq":
                self._handle_list_dlq()
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                raw_id = self.path.split("/dlq/")[1].split("/")[0]
                try:
                    dlq_id = int(raw_id)
                except ValueError:
                    return self._json({"error": f"invalid dlq id: {raw_id!r}"}, 400)
                if not self._require_confirm("dlq_replay", {"dlq_id": dlq_id}):
                    return
                self._handle_replay_dlq(dlq_id)
            elif self.path.split("?", 1)[0] == "/api/versions/stats":
                self._handle_versions_stats()
            elif self.path.split("?", 1)[0] == "/api/versions/diff":
                qs = self._parse_query()
                file_path = qs.get("file", "")
                try:
                    v1, v2 = int(qs.get("v1", "")), int(qs.get("v2", ""))
                except ValueError:
                    return self._json({"error": "v1/v2 必须为整数"}, 400)
                self._handle_versions_diff(file_path, v1, v2)
            elif self.path.split("?", 1)[0] == "/api/versions":
                qs = self._parse_query()
                file_path = qs.get("file", "")
                if not file_path:
                    return self._json({"error": "缺少 file 参数"}, 400)
                self._handle_versions_list(file_path)
            elif self.path == "/api/dashboard":
                self._handle_dashboard()
            elif self.path == "/api/approvals":
                self._handle_approvals()
            elif self.path.split("?", 1)[0] == "/api/deliveries":
                self._handle_deliveries()
            elif self.path.startswith("/api/deliveries/"):
                run_id = self.path.split("/api/deliveries/", 1)[1].split("/")[0].split("?")[0]
                if not _validate_task_id(run_id):
                    return self._json({"error": "invalid run id"}, 400)
                self._handle_delivery_detail(run_id)
            elif self.path.split("?", 1)[0] == "/api/locks":
                self._handle_locks()
            elif self.path.split("?", 1)[0] == "/api/schedules":
                self._handle_schedules()
            elif wf_route and wf_route[1] == "inputs":
                self._handle_workflow_inputs(wf_route[0])
            elif self.path == "/api/pipeline":
                self._handle_pipeline()
            elif self.path == "/api/openapi.json":
                from .openapi_spec import generate_spec
                self._json(generate_spec())
            elif self.path == "/api/alerts":
                if not self._require_admin():
                    return
                self._handle_alerts()
            elif self.path == "/api/quality/feedback":
                if not self._require_admin():
                    return
                from .quality_feedback import get_quality_feedback
                self._json(get_quality_feedback().stats())
            elif self.path.startswith("/api/logs"):
                if not self._require_admin():
                    return
                self._handle_logs()
            elif self.path == "/":
                self._text("Loom Admin API v1\n"
                           "Endpoints: /health /metrics /tasks /agents /dlq /api/dashboard /api/pipeline /stream")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            _logger.exception("[AdminAPI] GET %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def do_POST(self):
        try:
            # 静态资源/健康免鉴权
            if self._serve_static(self.path):
                return
            if self.path == "/health":
                self._handle_health()
                return
            # 入站 webhook 自携密钥（HMAC/Token）且全程审计，不走 ADMIN_API_KEY——
            # 外部服务不该持有管理端凭据。置于管理鉴权门之前是刻意的。
            if self.path.startswith("/api/webhooks"):
                self._handle_webhook_inbound()
                return
            # 鉴权前置：未通过前不读取请求体（HTTP/1.0 无 keep-alive，可安全直接响应）
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return
            self._sync_shared_config()

            # 读取请求体（已通过鉴权）；超限直接 413，不读入内存（S4 内存 DoS 防护）
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return self._json({"error": "invalid Content-Length"}, 400)
            if content_length > _MAX_BODY_BYTES:
                self._json({"error": "request body too large"}, 413)
                return
            body = self.rfile.read(content_length) if content_length > 0 else b""

            # 处理 POST 专属路由
            if self.path == "/api/tasks":
                self._handle_submit_task(body)
                return
            wf_route = _workflow_route(self.path)
            if wf_route and wf_route[1] == "runs":
                # POST /api/workflows/{name}/runs —— 类型化输入的通用提交口（P1 #7）
                self._handle_create_run(wf_route[0], body)
                return
            if self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if not _validate_task_id(task_id):
                    return self._json({"error": "invalid task id"}, 400)
                if self.path.endswith("/cancel"):
                    self._handle_cancel_task(task_id)
                    return
                elif self.path.endswith("/rerun"):
                    self._handle_rerun_task(task_id)
                    return
                elif self.path.endswith("/pause"):
                    self._handle_pause_task(task_id)
                    return
                elif self.path.endswith("/resume"):
                    self._handle_resume_task(task_id)
                    return
                elif self.path.endswith("/approve"):
                    self._handle_approve_task(task_id, body)
                    return
                elif self.path.endswith("/reject"):
                    self._handle_reject_task(task_id, body)
                    return
            elif self.path == "/api/versions/rollback":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (ValueError, UnicodeDecodeError):
                    return self._json({"error": "请求体不是合法 JSON"}, 400)
                file_path = payload.get("file", "")
                version = payload.get("version")
                if not file_path or not isinstance(version, int):
                    return self._json(
                        {"error": '需要 JSON 体 {"file": <path>, "version": <int>}'}, 400)
                if not self._require_confirm(
                        "versions_rollback", {"file": file_path, "version": version}):
                    return
                self._handle_versions_rollback(file_path, version)
                return
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                raw_id = self.path.split("/dlq/")[1].split("/")[0]
                try:
                    dlq_id = int(raw_id)
                except ValueError:
                    return self._json({"error": f"invalid dlq id: {raw_id!r}"}, 400)
                if not self._require_confirm("dlq_replay", {"dlq_id": dlq_id}):
                    return
                self._handle_replay_dlq(dlq_id)
                return
            elif self.path == "/api/config":
                summary: dict = {}
                with contextlib.suppress(Exception):
                    data = _fast_loads(body) if body else {}
                    if isinstance(data, dict):
                        summary = {"key": data.get("key")}
                if not self._require_confirm("config_set", summary):
                    return
                self._handle_config_set(body)
                return
            elif self.path == "/api/config/reload":
                self._handle_config_reload()
                return
            elif self.path == "/api/cache/clear":
                if not self._require_confirm("cache_clear"):
                    return
                self._handle_cache_clear()
                return
            elif self.path == "/api/cost/budget":
                self._handle_set_budget(body)
                return
            elif self.path == "/api/events/hooks":
                self._handle_register_hook(body)
                return

            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            _logger.exception("[AdminAPI] POST %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def do_OPTIONS(self):
        """CORS 预飞：仅白名单 Origin 放行（同源 / 回环同端口 / ADMIN_CORS_ORIGINS）"""
        origin = self.headers.get("Origin")
        allowed = self._allowed_origin(origin)
        if origin and not allowed:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b'{"error": "origin not allowed"}')))
            self.end_headers()
            self.wfile.write(b'{"error": "origin not allowed"}')
            return
        self.send_response(204)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_DELETE(self):
        try:
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return
            self._sync_shared_config()
            if self.path.startswith("/api/events/hooks/"):
                hook_id = self.path.split("/api/events/hooks/")[1]
                self._handle_unregister_hook(hook_id)
                return
            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            _logger.exception("[AdminAPI] DELETE %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def _handle_health(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 503)
        h = self.orch.bus.health()
        h["registry_agents"] = len(self.orch.registry.list())
        h["tasks_running"] = len(self.orch.list_tasks())
        self._json(h)

    def _handle_metrics(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 503)
        metrics = getattr(self.orch, "_metrics", None)
        if metrics and hasattr(metrics, "to_prometheus"):
            self._prometheus(metrics.to_prometheus())
        else:
            self._text("# no metrics available\n")

    def _handle_list_tasks(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        p = self._principal()
        # 内存缓存 ∪ RunStore 投影：被 _trim_task_history 淘汰的运行照样列出来。
        # 作用域在 list_runs 里按 principal 过滤（空串归属对具名 token 不可见）。
        runs = self.orch.list_runs(principal=None if p.admin else p.id)
        tasks_data = [
            {
                "id": r["id"],
                "status": r["status"],
                "pipeline": r["pipeline"],
                "created_at": r.get("created_at"),
                "finished_at": r.get("finished_at"),
                "error": r.get("error"),
            }
            for r in runs
        ]
        self._json({"tasks": tasks_data, "count": len(tasks_data)})

    def _handle_get_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        task = self._scoped_task(task_id)
        if not task:
            # 内存只是缓存：淘汰后仍从 RunStore 投影答（越权同样 404）
            return self._get_task_from_projection(task_id)
        result = dict(getattr(task, "result", {}))
        # 交付物由声明 WRITES_OUTPUT 的节点挂在任务上，接口层不再猜节点名
        output_path_found, output_content = task_output(task)
        self._json({
            "id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "input_file": getattr(task, "input_file", ""),
            "config": getattr(task, "config", {}),
            "result": result,
            "output_path": output_path_found,
            "output_content": output_content,
            "created_at": getattr(task, "created_at", None),
            "finished_at": getattr(task, "finished_at", None),
            "error": getattr(task, "error", None),
            # S2 运行详情屏（spec §4.2）：逐节点视图一次拿全，前端不再逐个猜。
            # 来源：TaskNode 运行态（status/skip_reason/attempts）+ execution_order
            # 拓扑层级 + dependencies 连边。任务没有 DAG 信息（legacy 路径）时
            # 给空结构而不是省略键——前端分支只写一次。
            "dag": self._task_dag_view(task),
            # 审批闸门（approval 节点）：等待中的闸门与决策流水。P1-C 之后
            # "人审"在控制台上有了落点，这里把它和 DAG 一起交给 S2 渲染。
            "awaiting_approval": getattr(task, "awaiting_approval", None),
            "approvals": list(getattr(task, "approvals", None) or []),
            # 产物清单（RunHandle.artifacts）：作用域已在上一步 _scoped_task 判过 404，
            # 这里读的是同一个已授权任务对象
            "artifacts": self.orch.run_handle(task_id).artifacts(),
        })

    def _get_task_from_projection(self, task_id: str):
        """内存里没有（被淘汰 / 别的进程跑完）时，从 RunStore 投影给出可读事实。

        能给的：状态 / 流水线 / 起止时间 / 错误 / 产物清单 / 逐节点摘要（status、
        attempts、duration、artifact_keys）。**给不出的**：节点 result 正文本、
        output_content、审批等待态、checkpoint 视图——这些从不落进投影（Stage 1
        只存摘要），所以对应键要么不出现、要么显式给 None，不编空值冒充"没有内容"。
        """
        run = self._scoped_run(task_id)
        if run is None:
            return self._json({"error": "task not found"}, 404)
        handle = self.orch.run_handle(task_id)
        nodes = handle.nodes()

        def _duration_ms(n: dict) -> float | None:
            n_started = float(n.get("started_at") or 0.0)
            n_finished = float(n.get("finished_at") or 0.0)
            return round((n_finished - n_started) * 1000, 1) if n_finished > n_started else None

        self._json({
            "id": run.get("run_id"),
            "status": run.get("status"),
            "pipeline": run.get("workflow") or "",
            "created_at": run.get("started_at") or None,
            "finished_at": run.get("finished_at") or None,
            "error": run.get("error") or None,
            "artifacts": handle.artifacts(),
            # 前端 S2 屏只依赖 nodes/layers/checkpoint 三个键；投影没有拓扑层级，
            # 逐节点一层是既有 _task_dag_view 在缺 layers 时的同一降级形态。
            "dag": {
                "nodes": [
                    {
                        "name": n.get("node"),
                        # 投影不存节点→Agent 的绑定（agent_name 只在内存态有），
                        # 这里给节点键本身——与 `_task_dag_view` 缺 agent_name 时的
                        # 回退同形，不另编一个 Agent 名
                        "agent": n.get("node"),
                        "status": n.get("status") or "",
                        "retried": int(n.get("attempts") or 0) > 1
                                   and str(n.get("status")) == "success",
                        "attempts": int(n.get("attempts") or 1),
                        "duration_ms": _duration_ms(n),
                        "skip_reason": "",
                        "error": n.get("error") or "",
                        "dependencies": [],
                        "artifact_keys": list(n.get("artifact_keys") or []),
                    }
                    for n in nodes
                ],
                "layers": [[n.get("node")] for n in nodes],
                "checkpoint": None,
            },
            "source": "runstore",
        })

    def _task_dag_view(self, task) -> dict:
        """任务的 DAG 视图（spec §4.2 S2）：节点四态 + 层级 + 连边 + 断点标记。

        四态 = success / skipped / failed / retried（retried 由 attempts>1 与
        最终 success 合成——engine 不另存"重试态"这个字段）。skipped 分两种：
        skip_reason="condition"（作者声明的 `when` 分支，下游不受影响，画虚线）
        与空串（依赖失败被级联跳过）。
        """
        dag_nodes: dict = getattr(task, "dag_nodes", None) or {}
        layers: list = [list(layer) for layer in (getattr(task, "execution_order", None) or [])]
        steps_by_node: dict[str, list] = {}
        for step in (getattr(task, "steps", None) or []):
            steps_by_node.setdefault(getattr(step, "agent_name", ""), []).append(step)

        # steps 里的 agent_name 与 dag_nodes 的 name 不一定同名（池化/别名），
        # 逐个 name 匹配后再回退 agent_name，避免节点状态全部落到 pending。
        nodes = []
        for name, node in dag_nodes.items():
            steps = steps_by_node.get(name) or steps_by_node.get(getattr(node, "agent_name", ""), [])
            attempts = int(getattr(node, "attempts", 0) or 0) or len(steps)
            status = str(getattr(node, "status", "pending") or "pending")
            started = float(getattr(node, "started_at", 0) or 0)
            finished = float(getattr(node, "finished_at", 0) or 0)
            nodes.append({
                "name": name,
                "agent": getattr(node, "agent_name", name),
                "status": status,
                # retried 是派生态：真跑过且最终成功，但不止一次尝试
                "retried": attempts > 1 and status == "success",
                "attempts": attempts,
                "duration_ms": round((finished - started) * 1000, 1) if finished > started else None,
                "skip_reason": str(getattr(node, "skip_reason", "") or ""),
                "error": str(getattr(node, "error", "") or ""),
                "dependencies": list(getattr(node, "dependencies", None) or []),
            })
        if not layers:
            layers = [[n["name"]] for n in nodes]
        # 断点续跑标记（S2 表里原缺的一项）：每节点标一个"已落断点"圆点，
        # 任务级再给 resumed/exists——同一份 _task_checkpoint_view 出数，
        # S3 检查器与 S2 绘图共用，不会两处漂移。
        checkpoint = self._task_checkpoint_view(task)
        return {"nodes": nodes, "layers": layers, "checkpoint": checkpoint}

    # ── S3 节点检查器（spec §4.2） ─────────────────────

    def _handle_node_inspector(self, task_id: str, node_name: str):
        """节点检查器：S2 DAG 里点一个节点 → 它的四类明细一次拿全。

        spec §4.2 S3 主区内容：**artifact 结构预览 / 幂等键 / 熔断限流态 /
        质量门明细**。约定：
        - 越权与不存在同形（404）——复用 `_scoped_task` / `_scoped_run`，不泄露存在性；
        - 各类明细**取不到就不编**：熔断器没建过、限流器没建过、质量门不是
          本节点的产物，对应字段给 None 而不是空对象或猜的值（防"显示假分"）；
        - artifact 预览是**形状 + 截断**，不是全量回传（节点结果可能很大）。
        - 内存只是缓存：跨进程跑完的运行（被淘汰 / 别的进程）从 RunStore 投影答
          ——与 `/tasks/<id>` 的回退同一口径，否则"列表能开详情、点节点必 404"。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if not node_name:
            return self._json({"error": "missing node name"}, 400)
        task = self._scoped_task(task_id)
        if not task:
            return self._node_inspector_from_projection(task_id, node_name)
        node = (getattr(task, "dag_nodes", None) or {}).get(node_name)
        if node is None:
            return self._json({"error": "node not found"}, 404)

        agent_name = getattr(node, "agent_name", node_name) or node_name
        self._json({
            "task_id": task.id,
            "task_status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "node": {
                "name": node_name,
                "agent": agent_name,
                "status": str(getattr(node, "status", "pending") or "pending"),
                "attempts": int(getattr(node, "attempts", 0) or 0),
                "skip_reason": str(getattr(node, "skip_reason", "") or ""),
                "error": str(getattr(node, "error", "") or ""),
                "dependencies": list(getattr(node, "dependencies", None) or []),
                "max_retries": int(getattr(node, "max_retries", 0) or 0),
            },
            "artifact": self._node_artifact_preview(node),
            "idempotency": self._node_idempotency(task, node_name, node),
            "circuit_breaker": self._agent_circuit_breaker(agent_name),
            "rate_limit": self._agent_rate_limit(agent_name),
            "quality": self._node_quality_detail(node),
            "checkpoint": self._task_checkpoint_view(task),
        })

    def _node_inspector_from_projection(self, task_id: str, node_name: str):
        """内存里没有（被淘汰 / 别的进程跑完）时的节点明细：从 RunStore 投影给可读事实。

        与 `_get_task_from_projection` 同一口径：越权与不存在同形（404，经 `_scoped_run`）。
        投影只存节点摘要（status/attempts/时间/artifact_keys），四类明细（artifact 正文
        预览 / 幂等键落库态 / 熔断限流 / 质量门明细）从不落进投影——按"取不到就不编"给
        None，并显式标 `source: "runstore"`，前端据此换文案而不是把 None 当"没有内容"。
        """
        run = self._scoped_run(task_id)
        if run is None:
            return self._json({"error": "task not found"}, 404)
        handle = self.orch.run_handle(task_id)
        node = next((n for n in handle.nodes() if n.get("node") == node_name), None)
        if node is None:
            return self._json({"error": "node not found"}, 404)
        n_started = float(node.get("started_at") or 0.0)
        n_finished = float(node.get("finished_at") or 0.0)
        duration_ms = (round((n_finished - n_started) * 1000, 1)
                       if n_finished > n_started else None)
        self._json({
            "task_id": task_id,
            "task_status": run.get("status"),
            "source": "runstore",
            "node": {
                "name": node_name,
                # 投影不存节点→Agent 绑定（同 _get_task_from_projection 的回退）
                "agent": node_name,
                "status": str(node.get("status") or "pending"),
                "attempts": int(node.get("attempts") or 0),
                "skip_reason": "",
                "error": str(node.get("error") or ""),
                "dependencies": [],
                # 投影不存重试上限：给 None 让前端显示 "?"，不拿 0 冒充"不许重试"
                "max_retries": None,
                "duration_ms": duration_ms,
                "artifact_keys": list(node.get("artifact_keys") or []),
            },
            "artifact": None,
            "idempotency": None,
            "circuit_breaker": None,
            "rate_limit": None,
            "quality": None,
            "checkpoint": None,
        })

    @staticmethod
    def _node_artifact_preview(node) -> dict:
        """artifact 结构预览：键 + 类型 + 标量值，长值截断（不整包回传）。"""
        result = getattr(node, "result", None)
        if not isinstance(result, dict) or not result:
            return {"keys": [], "size": 0, "preview": {}, "truncated": False}
        preview: dict = {}
        truncated = False
        for key, value in result.items():
            kind = type(value).__name__
            if isinstance(value, str):
                preview[key] = {"type": "str", "length": len(value),
                                "value": value[:200] + ("…" if len(value) > 200 else "")}
                truncated = truncated or len(value) > 200
            elif isinstance(value, (int, float, bool)) or value is None:
                preview[key] = {"type": kind, "value": value}
            elif isinstance(value, list):
                preview[key] = {"type": "list", "length": len(value)}
            elif isinstance(value, dict):
                preview[key] = {"type": "dict", "keys": sorted(value.keys())[:20]}
            else:
                preview[key] = {"type": kind}
        return {"keys": sorted(result.keys()), "size": len(result),
                "preview": preview, "truncated": truncated}

    def _node_idempotency(self, task, node_name: str, node) -> dict:
        """幂等键 `{task_id}:{node}:{attempts}` 与它是否已落 processed_keys。

        取的是 orchestrator 身上那条持久化总线内部的 PersistentStore
        （`orch.bus._store`）——processed_keys 表就在那里，另开一条连接会
        读到另一份库。取不到给 None（不猜"未处理"）。
        """
        attempts = int(getattr(node, "attempts", 0) or 0)
        key = f"{task.id}:{node_name}:{attempts}"
        seen = None
        try:
            store = getattr(getattr(self.orch, "bus", None), "_store", None)
            if store is not None and hasattr(store, "is_idempotent"):
                seen = bool(store.is_idempotent(key))
        except Exception as e:  # noqa: BLE001  取不到就如实说取不到
            _logger.debug("[AdminAPI] 幂等键查询失败（%s 节点）: %s", node_name, e)
            seen = None
        return {"key": key, "attempts": attempts, "recorded": seen}

    def _agent_circuit_breaker(self, agent_name: str):
        """该 Agent 的熔断器态；从未建过 = None（不是"closed"）。"""
        registry = getattr(self.orch, "_cb_registry", None)
        if registry is None:
            return None
        try:
            cb = registry.get(agent_name) if hasattr(registry, "get") else None
        except Exception:  # noqa: BLE001
            return None
        if not cb:
            return None
        return {
            "state": cb.state.name if hasattr(cb.state, "name") else str(cb.state),
            "failure_count": int(getattr(cb, "failure_count", 0) or 0),
        }

    def _agent_rate_limit(self, agent_name: str):
        """该 Agent 的限流态（令牌桶余量 / 累计放行与阻塞）；未建过 = None。"""
        registry = getattr(self.orch, "_rate_limiters", None)
        if registry is None or not hasattr(registry, "get"):
            return None
        try:
            limiter = registry.get(agent_name)
        except Exception:  # noqa: BLE001
            return None
        if not limiter:
            return None
        try:
            stats = limiter.stats()
        except Exception:  # noqa: BLE001
            return None
        return {"rate": stats.get("rate"), "burst": stats.get("burst"),
                "available_tokens": stats.get("available_tokens"),
                "total_acquired": stats.get("total_acquired"),
                "total_blocked": stats.get("total_blocked")}

    @staticmethod
    def _node_quality_detail(node) -> dict | None:
        """质量门明细：只认质量门形状的产物（overall_score + scores 维度分）。

        非质量门节点的 result 长得不像就给 None——**不把普通节点结果当成
        质量分展示**（FP-1"显示假分"同类错误）。
        """
        result = getattr(node, "result", None)
        if not isinstance(result, dict):
            return None
        scores = result.get("scores")
        if not isinstance(scores, dict) or "overall_score" not in result:
            return None
        return {
            "overall_score": result.get("overall_score"),
            "scores": {k: v for k, v in scores.items()
                       if isinstance(v, (int, float))},
            "profile": result.get("profile"),
            "status": result.get("status"),
            "threshold": result.get("threshold"),
            "style_issues": result.get("style_issues") if isinstance(
                result.get("style_issues"), list) else None,
            "needs_regenerate": result.get("needs_regenerate"),
        }

    @staticmethod
    def _task_checkpoint_view(task) -> dict:
        """断点视图：落点、是否已落盘、续跑标记（S2 缺的那一项）。

        `resumed` = 该任务是从断点恢复而来的（状态自 PAUSED 读回，且
        节点已完成数 > 0）；`exists` = 断点文件此刻真的在磁盘上。
        """
        path = str(getattr(task, "checkpoint_file", "") or "")
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
        done_nodes = sum(
            1 for n in (getattr(task, "dag_nodes", None) or {}).values()
            if str(getattr(n, "status", "")) == "success")
        return {
            "path": path,
            "exists": bool(path) and Path(path).exists(),
            "resumed": status == "paused" and done_nodes > 0,
            "completed_nodes": done_nodes,
        }

    def _attach_stream_callback(self, task_id: str):
        """为 API 提交的任务挂接流式回调（供 GET /stream?task_id= 订阅实时进度）"""
        try:
            from .streaming import StreamCallback, register_callback
            writer_agent = self._find_streaming_agent()
            if writer_agent is None:
                return None
            callback = StreamCallback()
            # 按 task_id 注册到 writer 的回调注册表（并发任务互不串扰；
            # 旧单槽 _active_stream_callback 在多任务并发时会互相覆盖）
            writer_agent._register_stream_callback(task_id, callback)
            register_callback(task_id, callback)
            track_stream_callback(task_id, callback)
            return callback
        except Exception:
            return None

    def _detach_stream_callback(self, task_id: str, callback) -> None:
        if callback is None:
            return
        with contextlib.suppress(Exception):
            from .streaming import unregister_callback
            unregister_callback(task_id)
        untrack_stream_callback(task_id)
        with contextlib.suppress(Exception):
            writer_agent = self._find_streaming_agent()
            if writer_agent is not None \
                    and writer_agent._get_stream_callback(task_id) is callback:
                writer_agent._unregister_stream_callback(task_id)
        with contextlib.suppress(Exception):
            callback.close()

    def _schedule_input_cleanup(self, task_id: str, input_file: Path,
                                callback=None, max_wait_seconds: int = 1800):
        """wait=false 时后台线程仍会读取输入文件，待任务终态后再清理临时文件。"""
        orch = self.orch

        def _reap():
            deadline = time.time() + max_wait_seconds
            active = True
            while time.time() < deadline:
                task = None
                with contextlib.suppress(Exception):
                    task = orch.get_task(task_id) if orch else None
                if task is None:
                    active = False
                    break
                status = str(getattr(task.status, "value", task.status))
                if status not in ("pending", "running", "paused"):
                    active = False
                    break
                time.sleep(2.0)
            self._detach_stream_callback(task_id, callback)
            if not active:
                with contextlib.suppress(OSError):
                    input_file.unlink()

        threading.Thread(target=_reap, daemon=True,
                         name=f"tmp-clean-{task_id}").start()

    def _handle_webhook_inbound(self) -> None:
        """POST /api/webhooks/<name> —— 入站触发（自携密钥 + 审计）。

        请求体在这里自行读取：HMAC 要用**原始字节**验签，且这条路不经过管理
        鉴权门（见 do_POST 的注释）。业务逻辑全在 loom_core.webhooks，
        这里只做 HTTP 层的取头/读数/回包。
        """
        from . import webhooks as _webhooks

        raw = self.path.split("?", 1)[0]
        m = re.fullmatch(r"/api/webhooks/([^/]+)", raw)
        if not m:
            self._json({"error": "webhook name required: POST /api/webhooks/<name>"}, 400)
            return
        name = m.group(1)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._json({"error": "invalid Content-Length"}, 400)
            return
        if length > _MAX_BODY_BYTES:
            self._json({"error": "request body too large"}, 413)
            return
        body = self.rfile.read(length) if length > 0 else b""
        remote = self.client_address[0] if self.client_address else "?"
        status, payload = _webhooks.handle_inbound(
            name, getattr(self, "webhooks", {}) or {}, body,
            headers=self.headers, remote=remote, orch=getattr(self, "orch", None))
        self._json(payload, status)

    def _handle_submit_task(self, body: bytes):
        """提交新文档生成任务

        body JSON:
          query (str, 与 inputs 二选一) — 文档主题/查询（自由文本形）
          inputs (obj, 与 query 二选一) — 类型化载荷，按流水线 `pipeline.inputs` 校验
          title (str, 可选) — 文档标题，默认同 query
          pipeline (str, 可选) — 流水线名，默认 docgen
          wait (bool, 可选) — 是否同步等待完成，默认 false
          output (str, 可选) — 输出文件路径
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            data = _fast_loads(body) if body else {}
        except Exception:
            return self._json({"error": "invalid JSON body"}, 400)

        query = str(data.get("query", "") or "").strip()
        typed_inputs = data.get("inputs")
        if typed_inputs is None and not query:
            return self._json({"error": "missing 'query' or 'inputs' field"}, 400)

        title = data.get("title", query)
        pipeline_name, pipeline_err = resolve_pipeline_name(
            str(data.get("pipeline", "")), _scheduler_mod.installed_pipelines(),
            _default_pipeline_from(self.orch))
        if pipeline_err:
            return self._json({"error": pipeline_err}, 400)
        wait = bool(data.get("wait", False))
        output_path = data.get("output", "")
        # 安全修复 (P0): output 路径白名单校验，防止任意文件写入
        if output_path:
            ok, reason = _validate_output_path(output_path)
            if not ok:
                return self._json({"error": f"output 路径校验失败: {reason}"}, 400)

        try:
            from .scheduler import Scheduler
            plan = Scheduler().parse(pipeline_name)
        except Exception as e:
            return self._json({"error": f"failed to parse pipeline '{pipeline_name}': {e}"}, 400)

        # 两条载荷形状过同一个闸门：typed 走逐字段校验，query 自由文本在流水线声明了
        # inputs 时直接拒（否则声明就成了装饰）。
        try:
            if typed_inputs is not None:
                input_text = prepare_inputs(plan, typed_inputs, title=pipeline_name)
            else:
                gate_freeform(plan, surface="POST /api/tasks")
                input_text = f"# {title}\n\n## 查询\n\n{query}\n"
        except InputValidationError as e:
            return self._json(e.to_dict(), 400)

        echo = {"query": query, "title": title} if typed_inputs is None \
            else {"inputs": typed_inputs}
        self._launch_from_doc(plan, input_text, wait=wait, echo=echo)

    def _handle_create_run(self, pipeline_name: str, body: bytes):
        """`POST /api/workflows/{name}/runs` —— 类型化输入的通用提交口（P1 #7）。

        body JSON:
          inputs (obj, 必填) — 按该流水线 `pipeline.inputs` 声明校验的载荷
          wait (bool, 可选) — 同步等待完成，默认 false
          output (str, 可选) — 输出文件路径（同 /api/tasks 的白名单校验）

        与 `/api/tasks` 的差别只在载荷形状：这里没有 `query` 语义，任何声明了 inputs
        的流水线都能用同一份契约提交；非法载荷 400 且错误带字段名（`fields`）。
        鉴权、10MiB 体积上限与 owner 口径由 do_POST 的统一入口提供，不在这里另立。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            data = _fast_loads(body) if body else {}
        except Exception:
            return self._json({"error": "invalid JSON body"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "body 必须是 JSON 对象"}, 400)

        resolved, err = resolve_pipeline_name(
            pipeline_name, _scheduler_mod.installed_pipelines())
        if err:
            return self._json({"error": err}, 400)
        wait = bool(data.get("wait", False))
        output_path = data.get("output", "")
        if output_path:
            ok, reason = _validate_output_path(output_path)
            if not ok:
                return self._json({"error": f"output 路径校验失败: {reason}"}, 400)

        try:
            from .scheduler import Scheduler
            plan = Scheduler().parse(resolved)
        except Exception as e:
            return self._json({"error": f"failed to parse pipeline '{resolved}': {e}"}, 400)

        try:
            input_text = prepare_inputs(plan, data.get("inputs") or {},
                                        title=resolved)
        except InputValidationError as e:
            return self._json(e.to_dict(), 400)

        self._launch_from_doc(plan, input_text, wait=wait,
                              echo={"inputs": data.get("inputs") or {}})

    def _handle_workflow_inputs(self, pipeline_name: str):
        """`GET /api/workflows/{name}/inputs` —— 读回类型化输入声明（表单/预估用）。

        返回的是流水线里那份声明的**副本**，没声明时 `{"declared": false}`：客户端据此
        决定生成表单还是退回自由文本，而不是靠猜键名。
        """
        resolved, err = resolve_pipeline_name(
            pipeline_name, _scheduler_mod.installed_pipelines())
        if err:
            return self._json({"error": err}, 404)
        try:
            from .scheduler import Scheduler
            plan = Scheduler().parse(resolved)
        except Exception as e:
            return self._json({"error": f"failed to parse pipeline '{resolved}': {e}"}, 500)
        from .input_schema import declaration
        schema = declaration(plan)
        self._json({"pipeline": resolved, "declared": bool(schema),
                    "inputs": schema})

    def _launch_from_doc(self, plan, input_text: str, *, wait: bool, echo: dict):
        """共享提交尾（Admin 侧）：临时输入文件 → run_plan(owner=principal) → 收尾。

        `/api/tasks` 与 `/api/workflows/{name}/runs` 共用这一段——wait 语义、SSE
        callback 的摘挂、异步输入文件的 reaper 只在一处维护；两处各写一份的话，
        "两个入口行为不同"这种漂移没有任何门禁会看见。

        注意 `output` 字段沿用了既有行为：**只做白名单校验，不参与落盘**（改它=开始
        按客户端给的路径写文件，是另一件事，不在这一条线里顺手做）。
        """
        import tempfile
        task_id = new_task_id()
        input_file = Path(tempfile.gettempdir()) / f"task_{task_id}.md"
        input_file.write_text(input_text, encoding="utf-8")

        callback = self._attach_stream_callback(task_id)

        try:
            task = self.orch.run_plan(plan, input_file=str(input_file),
                                      task_id=task_id, wait=wait,
                                      owner=self._principal().id)
        except Exception as e:
            self._detach_stream_callback(task_id, callback)
            with contextlib.suppress(OSError):
                input_file.unlink()
            return self._json({"error": f"pipeline execution failed: {e}"}, 500)

        response = {
            "task_id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            **echo,
        }

        if wait:
            result = dict(getattr(task, "result", {}))
            response["result"] = result
            response["error"] = getattr(task, "error", None)
            response["output_path"], response["output_content"] = task_output(task)

            self._detach_stream_callback(task_id, callback)
            with contextlib.suppress(OSError):
                input_file.unlink()
        else:
            response["message"] = "task started, poll GET /tasks/{task_id} for status"
            # 后台线程仍会读取输入文件 → 任务结束后由 reaper 清理
            self._schedule_input_cleanup(task_id, input_file, callback)

        self._json(response)

    def _handle_cancel_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self._scoped_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
        ok = self.orch.cancel(task_id)
        self._json({"cancelled": ok, "task_id": task_id})

    def _handle_rerun_task(self, task_id: str):
        """重跑流水线（复用上一次 plan + 输入）"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self._scoped_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
        try:
            new_task = self.orch.rerun(task_id=task_id)
            self._json({
                "status": "rerun_started",
                "new_task_id": new_task.id,
                "pipeline": new_task.pipeline_name,
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_pause_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self._scoped_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
        ok = self.orch.pause(task_id)
        self._json({"paused": ok, "task_id": task_id})

    def _handle_resume_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self._scoped_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
        ok = self.orch.resume(task_id)
        self._json({"resumed": ok, "task_id": task_id})

    def _handle_approve_task(self, task_id: str, body: bytes = b""):
        """审批放行（POST /tasks/<id>/approve）：body 可选 {"note": "..."}。

        只受理"审批等待中"的任务：不在等待中的批准会被 orch.approve 拒绝（409），
        审计里不出现无中生有的决策。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        # 已在内存的任务走 _scoped_task；进程重启后的待批走持久台账（同样按 principal）
        if not self._can_approve(task_id):
            return self._json({"error": "task not found"}, 404)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)
        note = str(payload.get("note", "") or "") if isinstance(payload, dict) else ""
        ok, err = self.orch.approve(task_id, note=note, by=self._approval_actor())
        if not ok:
            return self._json({"error": err, "task_id": task_id}, 409)
        self._json({"approved": True, "task_id": task_id, "note": note})

    def _handle_reject_task(self, task_id: str, body: bytes = b""):
        """审批驳回（POST /tasks/<id>/reject）：body 可选 {"reason": "..."}。

        驳回=终止（spec §9 决策 C：最小闭环不做分支）；原因进任务 error 与审计。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if not self._can_approve(task_id):
            return self._json({"error": "task not found"}, 404)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)
        reason = str(payload.get("reason", "") or "") if isinstance(payload, dict) else ""
        ok, err = self.orch.reject(task_id, reason=reason, by=self._approval_actor())
        if not ok:
            return self._json({"error": err, "task_id": task_id}, 409)
        self._json({"rejected": True, "task_id": task_id, "reason": reason})

    def _approval_actor(self) -> str:
        """审批人标识（最小闭环：客户端地址 + 凭证指纹——真名实姓等 P1 身份系统）。"""
        client = getattr(self, "client_address", None)
        host = client[0] if client else "?"
        try:
            return f"{host}|{self._key_identity()}"
        except Exception:
            return host

    def _handle_list_agents(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        agents = self.orch.registry.list()
        self._json({"agents": agents, "count": len(agents)})

    def _handle_list_dlq(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        dlq = self.orch.bus.list_dlq()
        self._json({"dlq": dlq, "count": len(dlq)})

    def _handle_replay_dlq(self, dlq_id: int):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        # 交由 orchestrator 真正重放（重新执行故障 node 并回填）
        result = self.orch.replay_dlq(dlq_id)
        self._json({"replayed": result is not None, "dlq_id": dlq_id,
                     "result": result})

    def _handle_dashboard(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        orch = self.orch
        # 与 /tasks 共用同一条读口径（内存缓存 ∪ 投影）；作用域在 list_runs 里过滤。
        # 2026-10-20 之前这里不隔离，普通 token 能看到全仓任务列表。
        p = self._principal()
        runs = orch.list_runs(principal=None if p.admin else p.id)
        tasks_data = [
            {
                "id": r["id"],
                "status": r["status"],
                "pipeline": r["pipeline"],
                "progress": r.get("progress", 0),
                "steps": r.get("steps", 0),
                "created_at": r.get("created_at"),
                "finished_at": r.get("finished_at"),
                "error": r.get("error"),
            }
            for r in runs
        ]
        agents_list = orch.registry.list()
        agents = []
        for a in agents_list:
            agents.append({
                "name": a.get("name", ""),
                "version": a.get("version", ""),
                "status": a.get("status", "unknown"),
            })
        self._json({
            "status": "ok",
            "tasks": tasks_data,
            "agents": agents,
            "task_count": len(tasks_data),
        })

    def _handle_approvals(self):
        """待批收件箱（跨运行、跨重启）：`GET /api/approvals`。

        非 admin 只看 principal 属于自己的待办；admin 看全部并附带归属。
        数据来自持久台账而不是内存任务表——进程重启后收件箱照常可读。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        p = self._principal()
        try:
            rows = self.orch.approvals.list_pending(
                principal=None if p.admin else p.id)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[AdminAPI] 读取审批台账失败: %s", e)
            return self._json({"error": "approvals unavailable"}, 503)
        now = time.time()
        items = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            item = {
                "request_id": r.get("request_id", ""),
                "run_id": r.get("run_id", ""),
                "pipeline": r.get("pipeline", ""),
                "nodes": list(r.get("nodes") or []),
                "messages": dict(r.get("messages") or {}),
                "requested_at": r.get("requested_at", 0),
                "waiting_sec": round(now - float(r.get("requested_at") or now), 1),
            }
            if p.admin:
                item["principal"] = r.get("principal", "")
            items.append(item)
        self._json({"approvals": items, "count": len(items)})

    # ─── 交付账本 / 锁巡检（P1 #8，只读）───────────────────

    #: 账本一行装的是整条运行的逐节点历史，一次给太多会把进程拖垮
    _DELIVERY_LIMIT_MAX = 200

    def _int_param(self, name: str, default: int, maximum: int) -> int:
        """读一个带上限的整数查询参数；坏值回落到默认，不 400。"""
        from urllib.parse import parse_qs, urlparse
        raw = (parse_qs(urlparse(self.path).query).get(name) or [""])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(1, min(value, maximum))

    def _delivery_item(self, rec: dict, p: Principal) -> dict | None:
        """账本一行 → 响应项，同时裁决可见性。

        账本自己没有归属列（`deliveries.py` 的表里就没有 principal），所以"这条你
        能不能看"只能问投影：走 `_scoped_run` 那条口径——越权按不存在处理，不给 403。
        顺带带上 Stage 3b 的健康位：`incomplete` 就是该跑 `--replay-run` 的那批，
        但**这里不替你判能不能补**（判据只有补账工具一处，抄一份就会分叉）。
        """
        run_id = str(rec.get("run_id") or "")
        if not p.admin and self._scoped_run(run_id) is None:
            return None
        nodes = [n for n in (rec.get("nodes") or []) if isinstance(n, dict)]
        return {
            "run_id": run_id,
            "pipeline": str(rec.get("pipeline") or ""),
            "status": str(rec.get("status") or ""),
            "finished_at": rec.get("finished_at") or 0,
            "duration_sec": rec.get("duration_sec"),
            "output_path": str(rec.get("output_path") or ""),
            "node_count": len(nodes),
            "artifact_count": sum(len(n.get("artifact_keys") or []) for n in nodes),
            "error": str(rec.get("error") or ""),
            # 交付物内容摘要（A2）：空串 = 没有落盘产物，或那一跑写在
            # output_sha256 落地之前——两种都在读侧显示为"未记录"，不冒充已核对。
            "output_sha256": str(rec.get("output_sha256") or ""),
            "projection": self.orch.run_handle(run_id).projection(),
        }

    def _handle_deliveries(self):
        """`GET /api/deliveries?limit=N`：最近交付（`finished_at` 降序）。"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        p = self._principal()
        limit = self._int_param("limit", 50, self._DELIVERY_LIMIT_MAX)
        try:
            from .deliveries import DeliveryLedger
            rows = DeliveryLedger().list(limit=limit)
        except Exception as e:  # noqa: BLE001 —— 账本读不动要报 503，不能当成"没有交付"
            _logger.warning("[AdminAPI] 读取交付账本失败: %s", e)
            return self._json({"error": "deliveries unavailable"}, 503)
        items = [item for item in (self._delivery_item(r, p) for r in rows
                                   if isinstance(r, dict)) if item is not None]
        self._json({"deliveries": items, "count": len(items), "limit": limit,
                    "truncated": len(rows) >= limit,
                    "scope": "all" if p.admin else "principal"})

    def _handle_delivery_detail(self, run_id: str):
        """`GET /api/deliveries/<run_id>`：整条结构化载荷（含逐节点）+ 健康位。

        "账本里没这条"和"这条不是你的"**同一个 404**：存在性本身就是信息。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        p = self._principal()
        try:
            from .deliveries import DeliveryLedger
            rec = DeliveryLedger().get(run_id)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[AdminAPI] 读取交付明细失败: %s", e)
            return self._json({"error": "deliveries unavailable"}, 503)
        if not isinstance(rec, dict) or str(rec.get("run_id") or "") != run_id:
            return self._json({"error": "delivery not found"}, 404)
        item = self._delivery_item(rec, p)
        if item is None:
            return self._json({"error": "delivery not found"}, 404)
        item["plan_id"] = str(rec.get("plan_id") or "")
        item["input_file"] = str(rec.get("input_file") or "")
        item["started_at"] = rec.get("started_at") or 0
        item["nodes"] = [n for n in (rec.get("nodes") or []) if isinstance(n, dict)]
        # 锁指纹（A5）随明细一起回：列表不需要，但"这一跑用的是哪把锁"必须可查。
        item["topology_hash"] = str(rec.get("topology_hash") or "")
        item["config_hashes"] = dict(rec.get("config_hashes") or {})
        self._json({"delivery": item})

    #: 全量锁巡检要解析每一条流水线：本机实测（Python 3.14.3、单进程、无并发负载）
    #: 19 条 = 冷 7.27s / 再跑一遍 11.23s。监控是**轮询**的，每次轮询付这个钱既不
    #: 合理也必然超时，所以按 (yaml, lock) 的 (mtime_ns, size) 记忆单条结论——
    #: 文件没动就复用，动过立刻重算；`scope: current files` 的语义不变。
    #: 带 `?pipeline=` 的显式单条**不读缓存**（"我刚改了，现在到底对不对"是那一问的
    #: 语义），它只多付一条的解析。
    _LOCKS_SURVEY_CACHE: dict[str, tuple[tuple, dict]] = {}

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int]:
        try:
            st = path.stat()
        except OSError:
            return (0, 0)
        return (int(st.st_mtime_ns), st.st_size)

    @staticmethod
    def _lock_entry(sched, name: str, yaml_path: Path, source_dir: Path) -> dict:
        try:
            plan = sched.parse_file(str(yaml_path), verify_lock=False)
            drift = sched.lock_drift(plan, source_dir=source_dir)
            # 三值而不是布尔：`matches: true` 要说的是"比过且一致"，
            # 没有锁可比时给 true 等于把"没发现问题"和"没法判断"混成一件事。
            return {"pipeline": name,
                    "lock_file": Path(drift["lock_file"]).name,
                    "lock_present": drift["lock_present"],
                    "matches": (not drift["issues"]) if drift["lock_present"] else None,
                    "issues": list(drift["issues"])}
        except Exception as e:  # noqa: BLE001 —— 解析炸了也要报，别把一条拖成全口 500
            return {"pipeline": name, "lock_file": "", "lock_present": None,
                    "matches": None, "issues": [f"解析失败: {e}"]}

    def _handle_schedules(self):
        """`GET /api/schedules`：cron 触发租约的观测面（Workflows & Schedules 屏）。

        只读：租约的裁决/释放归 triggers 域，这里不做任何写。
        `lease_active` 在服务端算好——"租约是否还在期内"是时点判断，口径放
        服务端，前端不拿本机时钟跟 `lease_until` 自比。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        store = self.orch.run_store() if self.orch else None
        rows = store.list_schedules() if store is not None else []
        import time as _time
        now = _time.time()
        schedules = []
        for r in rows:
            lease_until = r.get("lease_until") or 0
            schedules.append({
                "name": r.get("name", ""),
                "pipeline": r.get("pipeline", ""),
                "cron": r.get("cron", ""),
                "enabled": bool(r.get("enabled")),
                "last_fire": r.get("last_fire") or None,
                "next_fire": r.get("next_fire") or None,
                "lease_owner": r.get("lease_owner") or "",
                "lease_until": lease_until or None,
                # 租约三态：无主（''）= 空闲；有主且未到期 = 持有中；有主已到期 = 残留
                "lease_state": ("held" if r.get("lease_owner") and lease_until > now
                                else "stale" if r.get("lease_owner") else "free"),
            })
        self._json({"schedules": schedules, "count": len(schedules)})

    def _handle_locks(self):
        """`GET /api/locks[?pipeline=<name>]`：**当前文件 vs 当前锁**的漂移巡检。

        口径要说清，否则这口会变成假证据：报的是"现在这两份文件对不对得上"，
        **不是**"某次运行时用的锁对不对"——运行时的 `config_hash`/`topology_hash`
        今天不落库（只在 `verify_lockfile` 那一瞬存在于内存），所以后者无法在这里
        回答，也不拿今天的值冒充当时。响应里的 `scope` 就是给读的人钉这一点的。
        """
        from urllib.parse import parse_qs, urlparse

        from .pack_manifest import pipeline_dirs
        from .scheduler import Scheduler
        wanted = (parse_qs(urlparse(self.path).query).get("pipeline") or [""])[0]
        entries: list[dict] = []
        seen: set[str] = set()
        cached_hits = 0
        for d in pipeline_dirs():
            if not d.is_dir():
                continue
            # 解析顺序即优先级：核心 pipelines/ 在前，pack 里的同名片段不覆盖它
            sched = Scheduler(pipeline_dir=str(d))
            for name in sorted(sched.list_pipelines()):
                if name in seen or (wanted and name != wanted):
                    continue
                seen.add(name)
                yaml_path = d / f"{name}.yaml"
                key = (str(d), self._stamp(yaml_path),
                       self._stamp(sched.resolve_lock_path(name, source_dir=d)))
                if not wanted:
                    hit = AdminHandler._LOCKS_SURVEY_CACHE.get(name)
                    if hit is not None and hit[0] == key:
                        cached_hits += 1
                        entries.append(hit[1])
                        continue
                entry = self._lock_entry(sched, name, yaml_path, d)
                if not wanted:
                    AdminHandler._LOCKS_SURVEY_CACHE[name] = (key, entry)
                entries.append(entry)
        if wanted and not entries:
            return self._json({"error": "pipeline not found"}, 404)
        self._json({"scope": "current files", "pipelines": entries, "count": len(entries),
                    "cached": cached_hits,
                    "drifted": [e["pipeline"] for e in entries if e.get("matches") is False]})

    def _handle_pipeline(self):
        """返回流水线配置信息"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        orch = self.orch
        try:
            from loom_core import __version__ as _v
        except ImportError:
            _v = "unknown"
        pipeline_info = {
            "agents_dir": str(orch.agents_dir),
            "checkpoint_dir": str(orch.checkpoint_dir),
            "registered_agents": orch.registry.list(),
            "active_tasks": len(orch.list_tasks()),
            "version": _v,
        }
        from .pack_manifest import pipeline_dirs
        files: set[str] = set()
        for d in pipeline_dirs():
            if d.is_dir():
                files.update(p.name for p in d.glob("*.yaml"))
        pipeline_info["pipeline_files"] = sorted(files)
        self._json(pipeline_info)

    def _handle_agent_detail(self, agent_name: str):
        """单个 agent 详情（含 stats/meta）"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        agent = self.orch.registry._agents.get(agent_name)
        if not agent:
            return self._json({"error": f"agent '{agent_name}' not found"}, 404)
        detail = {  # type: ignore[var-annotated]
            "name": agent_name,
            "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
            "meta": {},
        }
        meta = getattr(agent, "meta", None)
        if meta:
            for attr in ("version", "description", "priority", "input_topics",
                         "output_topics", "dependencies", "cache_ttl", "respawn",
                         "health_check_interval", "tags"):
                detail["meta"][attr] = getattr(meta, attr, None)  # type: ignore[index]
        stats = getattr(agent, "stats", None)
        if stats:
            detail["stats"] = {
                "start_count": getattr(stats, "start_count", 0),
                "error_count": getattr(stats, "error_count", 0),
                "respawn_count": getattr(stats, "respawn_count", 0),
                "total_runtime_ms": getattr(stats, "total_runtime_ms", 0),
                "avg_processing_time_ms": getattr(stats, "avg_processing_time_ms", 0),
            }
        # 熔断器状态
        cb = self.orch._cb_registry.get(agent_name) if hasattr(self.orch, "_cb_registry") else None
        if cb:
            detail["circuit_breaker"] = {
                "state": cb.state.name if hasattr(cb.state, "name") else str(cb.state),
                "failure_count": cb.failure_count,
            }
        self._json(detail)

    def _handle_config_get(self):
        """运行时配置快照"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        self._json(self.orch.config.to_dict())

    #: 审计里对这些键做值脱敏：只记"变了"，不记明文（值可能带密钥/token）
    _AUDIT_REDACT_KEYS = ("token", "key", "secret", "password", "api_key")

    @staticmethod
    def _redact_value(key: str, value):
        """审计脱敏：命中敏感键名时只回形状（类型 + 长度），不回明文。"""
        if value is None:
            return None
        lowered = key.lower()
        if any(marker in lowered for marker in AdminHandler._AUDIT_REDACT_KEYS):
            return {"redacted": True, "type": type(value).__name__,
                    "length": len(value) if isinstance(value, (str, list, dict)) else None}
        return value

    def _handle_config_set(self, body: bytes):
        """运行时配置变更。body: {"key": "llm.model", "value": "xxx"}

        契约（docs/product-spec.md §5.4）：**每次变更有审计记录**。审计落
        `<状态目录>/audit/config.jsonl`（与入站 webhook 同一份实现），
        记录 key / 旧值 / 新值 / 客户端 / 凭证指纹；敏感键名的值只记脱敏形状。
        写盘失败不静默——响应里带 `audit_error`。

        跨进程（P1 验收第三条）：值同时写进覆写文件并递增 revision，
        worker / 其他 Admin API 进程轮询到后同样生效。覆写写盘失败时
        仅本进程内存生效——响应带 `propagation_error`，不静默。
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            data = _fast_loads(body) if body else {}
        except (json.JSONDecodeError, Exception):
            return self._json({"error": "invalid JSON body"}, 400)
        key = data.get("key")
        value = data.get("value")
        if not key:
            return self._json({"error": "missing 'key' field"}, 400)
        old_value = self.orch.config.get(key)
        propagation_error = self.orch.config.set_persisted(key, value)
        _logger.info(f"[AdminAPI] 配置更新: {key} = {value!r} (旧值: {old_value!r})"
                     f"{' [仅本进程]' if propagation_error else ''}")

        from . import audit as _audit

        # handler 可能由 __new__ 构造（测试夹具/嵌入方），client_address 未必存在
        client = getattr(self, "client_address", None)
        audit_error = _audit.write_audit("config", {
            "event": "config.set",
            "key": key,
            "old_value": self._redact_value(str(key), old_value),
            "new_value": self._redact_value(str(key), value),
            "key_id": self._key_identity(),
            "client": client[0] if client else "?",
        })
        payload: dict = {"key": key, "old_value": old_value,
                         "new_value": value, "applied": True,
                         "revision": self.orch.config.revision}
        if audit_error:
            payload["audit_error"] = audit_error
            _logger.error(f"[AdminAPI] 配置变更审计写盘失败: {audit_error}")
        else:
            payload["audit"] = "ok"
        if propagation_error:
            payload["propagation_error"] = propagation_error
            _logger.error(f"[AdminAPI] 配置覆写写盘失败（仅本进程生效）: {propagation_error}")
        else:
            payload["propagated"] = True
        self._json(payload)

    def _handle_config_audit(self):
        """GET /api/config/audit —— 最近的配置变更审计（新→旧，供 §5.4 判据查证）。"""
        from . import audit as _audit

        limit_raw = self._parse_query().get("limit", "50")
        try:
            limit = max(1, min(int(limit_raw), 500))
        except (TypeError, ValueError):
            limit = 50
        records = _audit.read_audit("config", limit=limit)
        self._json({"audit_file": str(_audit.audit_path("config")),
                    "count": len(records), "records": list(reversed(records))})

    def _handle_health_deep(self):
        """全组件深度健康检查"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        orch = self.orch
        result = {"timestamp": time.time(), "components": {}}

        # 1. 消息总线
        bus_h = orch.bus.health()
        result["components"]["message_bus"] = {
            "status": "healthy" if bus_h.get("running") else "unhealthy",
            "details": bus_h,
        }

        # 2. Registry
        agents = orch.registry.list()
        result["components"]["registry"] = {
            "status": "healthy" if len(agents) > 0 else "degraded",
            "agent_count": len(agents),
        }

        # 3. LLM Router
        try:
            from loom_core.llm_router import get_router
            router = get_router()
            providers = router.list_providers() if hasattr(router, "list_providers") else []
            active = router.get_active() if hasattr(router, "get_active") else None
            result["components"]["llm_router"] = {
                "status": "healthy" if active else "degraded",
                "providers": providers,
                "active": active,
            }
        except Exception as e:
            result["components"]["llm_router"] = {"status": "unknown", "error": str(e)}

        # 4. 搜索引擎
        try:
            from artesian.search_engines import SearchEngineManager
            mgr = SearchEngineManager.from_env()
            engines = mgr.list_engines() if hasattr(mgr, "list_engines") else []
            result["components"]["search_engines"] = {
                "status": "healthy" if engines else "degraded",
                "engines": engines,
            }
        except Exception as e:
            result["components"]["search_engines"] = {"status": "unknown", "error": str(e)}

        # 5. 缓存
        try:
            from loom_core.cache_manager import all_stats
            stats = all_stats()
            result["components"]["cache"] = {
                "status": "healthy",
                "stats": stats,
            }
        except Exception as e:
            result["components"]["cache"] = {"status": "unknown", "error": str(e)}

        # 6. 检查点目录
        ckpt_dir = orch.checkpoint_dir
        result["components"]["checkpoint"] = {
            "status": "healthy" if ckpt_dir.exists() else "degraded",
            "dir": str(ckpt_dir),
        }

        # 7. RunStore（Stage 1 起恒开，没有开关）：库能不能开、结构版本、最近一次失败
        store_h = (orch.run_store_health()
                   if callable(getattr(orch, "run_store_health", None)) else None)
        if not isinstance(store_h, dict):
            store_h = {"status": "unknown", "reason": "orchestrator 未提供 run_store_health"}
        result["components"]["runstore"] = {
            "status": store_h.get("status", "unknown"),
            "details": store_h,
        }

        # 8. 熔断器
        # `CircuitBreakerRegistry` 的读法是 `all() -> {name: to_dict()}`（没有 `list()`）：
        # 原写法在真编排器上恒抛 AttributeError ⇒ `/api/health/deep` 整个 500，
        # 而单测用的是 MagicMock（`hasattr` 恒真、`.list()` 随便返回），所以一直没暴露。
        cb_all = orch._cb_registry.all() if hasattr(orch, "_cb_registry") else {}
        if not isinstance(cb_all, dict):  # 替身/损坏的 registry：当成"没有熔断器"
            cb_all = {}
        open_cbs = [name for name, info in cb_all.items()
                    if str((info or {}).get("state", "")).upper() == "OPEN"]
        result["components"]["circuit_breakers"] = {
            "status": "healthy" if not open_cbs else "degraded",
            "total": len(cb_all),
            "open": len(open_cbs),
        }

        # 总体状态
        unhealthy = [k for k, v in result["components"].items()
                     if isinstance(v, dict) and v.get("status") not in ("healthy",)]
        result["overall"] = "healthy" if not unhealthy else "degraded"
        result["unhealthy_components"] = unhealthy
        self._json(result)

    def _handle_cache_stats(self):
        """缓存统计"""
        try:
            from loom_core.cache_manager import all_stats
            self._json({"caches": all_stats()})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_cost_stats(self):
        """LLM 成本统计（非 admin 只统计自己名下任务的成本，spec §5.4）。"""
        try:
            from loom_core.cost_tracker import get_cost_tracker
            tracker = get_cost_tracker()
            p = self._principal()
            if p.admin or not self.orch:
                return self._json(tracker.summary())
            # 作用域集合同样不能只取内存：淘汰/别的进程跑完的运行也属于这个
            # principal 的成本归因（这是设计 §1 点名"随淘汰丢"的那条）
            own_ids = {r["id"] for r in self.orch.list_runs(principal=p.id, limit=1000)}
            self._json(tracker.summary(task_ids=own_ids))
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_set_budget(self, body: bytes):
        """设置预算上限"""
        try:
            data = _fast_loads(body) if body else {}
            max_cost = float(data.get("max_cost", 0))
            from loom_core.cost_tracker import get_cost_tracker
            get_cost_tracker().set_budget(max_cost)
            self._json({"budget_set": True, "max_cost": max_cost})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _handle_config_reload(self):
        """热重载配置并通知所有 Agent；同时递增 revision 让其他进程也重读"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            self.orch.config.reload()
            notified = self.orch.notify_config_update(["*"])
            propagation_error = self.orch.config.publish_reload()
            payload: dict = {"reloaded": True, "agents_notified": notified,
                             "revision": self.orch.config.revision}
            if propagation_error:
                payload["propagation_error"] = propagation_error
                _logger.error(f"[AdminAPI] reload 广播失败（其他进程不会重读）: {propagation_error}")
            else:
                payload["propagated"] = True
            self._json(payload)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_alerts(self):
        """告警列表"""
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        level = params.get("level", [None])[0]
        category = params.get("category", [None])[0]
        try:
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            return self._json({"error": "limit 必须为整数"}, 400)
        from .alert_manager import get_alerts
        self._json({"alerts": get_alerts(level=level, category=category, limit=limit)})

    def _handle_logs(self):
        """结构化日志查询

        ?level=error&agent=writer&since=3600&limit=50
        """
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        level_filter = params.get("level", [None])[0]
        agent_filter = params.get("agent", [None])[0]
        try:
            since_seconds = int(params.get("since", ["3600"])[0])
            limit = int(params.get("limit", ["100"])[0])
        except ValueError:
            return self._json({"error": "since/limit 必须为整数"}, 400)

        log_dir = Path("logs")
        if not log_dir.exists():
            self._json({"logs": [], "count": 0})
            return

        cutoff = time.time() - since_seconds
        results = []
        # 性能：流式逐行读取（原 read_text 整文件载入，数百 MB 日志可瞬时打爆内存），
        # 且 JSONL 按天滚动、内部时间戳递增 → mtime 早于 cutoff（留 60s 尾部余量）
        # 的文件整文件早于时间窗，直接跳过（glob 已按文件名逆序 = 时间逆序）。
        for log_file in sorted(log_dir.glob("*.jsonl"), reverse=True):
            try:
                if log_file.stat().st_mtime < cutoff - 60:
                    break
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = _fast_loads(line)
                        except Exception:
                            continue
                        ts = entry.get("timestamp", 0)
                        if ts < cutoff:
                            continue
                        if level_filter and entry.get("level", "").lower() != level_filter.lower():
                            continue
                        if agent_filter and entry.get("agent", "") != agent_filter:
                            continue
                        results.append(entry)
                        if len(results) >= limit:
                            break
            except Exception:
                continue
            if len(results) >= limit:
                break
        self._json({"logs": results, "count": len(results)})

    def _handle_cache_clear(self):
        """清空所有缓存"""
        try:
            from loom_core.cache_manager import clear_all_caches
            clear_all_caches()
            _logger.info("[AdminAPI] 所有缓存已清空")
            self._json({"cleared": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_list_hooks(self):
        """列出已注册事件钩子"""
        from loom_core.event_hook import get_hook_manager
        mgr = get_hook_manager()
        self._json({"hooks": mgr.list_hooks()})

    def _handle_register_hook(self, body: bytes):
        """注册事件钩子。body: {"event": "task.completed", "url": "https://..."}"""
        from loom_core.event_hook import get_hook_manager
        try:
            data = _fast_loads(body) if body else {}
        except (json.JSONDecodeError, Exception):
            return self._json({"error": "invalid JSON body"}, 400)
        event = data.get("event")
        url = data.get("url")
        if not event or not url:
            return self._json({"error": "missing 'event' or 'url' field"}, 400)
        # 安全修复 (P0): webhook URL SSRF 防护，拒绝私有/保留 IP 与非 http(s) 协议
        ok, reason = _validate_webhook_url(url)
        if not ok:
            return self._json({"error": f"webhook url 校验失败: {reason}"}, 400)
        mgr = get_hook_manager()
        hook_id = mgr.register(event, url, data.get("headers", {}))
        _logger.info(f"[AdminAPI] 事件钩子已注册: {event} -> {url} (id={hook_id})")
        self._json({"hook_id": hook_id, "event": event, "url": url, "registered": True})

    def _handle_unregister_hook(self, hook_id: str):
        """注销事件钩子"""
        from loom_core.event_hook import get_hook_manager
        mgr = get_hook_manager()
        ok = mgr.unregister(hook_id)
        self._json({"unregistered": ok, "hook_id": hook_id})

    _SSE_HEARTBEAT_SECONDS = 15.0
    _BROKEN_PIPE_CANCEL_THRESHOLD = 3
    #: 跨进程重放没有回调可等，只能轮询事件表；0.5s 对进度条足够，也不至于打满库
    _RUN_TAIL_POLL = 0.5

    def _send_sse(self, event) -> bool:
        """写出一条 SSE 帧（统一走 StreamEvent.to_dict() 序列化，含 ts/section/total）。

        返回 False 表示客户端连接已断开（BrokenPipe/ConnectionReset）。
        """
        payload = _fast_dumps(event.to_dict(), default=str)
        frame = f"id: {event.event_id}\ndata: {payload}\n\n"
        try:
            self.wfile.write(frame.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _send_heartbeat(self) -> bool:
        """SSE 注释帧心跳（迭代空转时保活连接）"""
        try:
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _cancel_for_broken_pipe(self, task_id: str):
        """客户端连续断管 → 取消任务止损 LLM 配额"""
        _logger.warning("[AdminAPI] SSE 客户端连续断开，取消任务 %s 止损", task_id)
        cancel = getattr(self.orch, "cancel", None) if self.orch else None
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel(task_id)

    def _pump_sse(self, callback, task_id: str, start_cursor: int = 0) -> None:
        """推送回调事件流（游标式只读，多客户端安全；事件驱动 + 心跳保活）。

        每个客户端持独立 event_id 游标，从回调事件历史读取而非破坏性出队——
        多个 SSE 客户端并发订阅同一任务时各自拿到完整事件流，互不瓜分
        （修复：原 get_events() 出队语义下第二个客户端会抢走第一个的事件）。
        事件到达由 wait_for_events 的 Condition 立即唤醒推送（原 5Hz 轮询
        200ms 才轮到一次且空闲时持续空转）；空闲 15s 发送心跳帧；
        连续 BrokenPipe 达阈值则取消任务并停止。
        """
        broken = 0
        last_frame = time.time()
        cursor = start_cursor
        while True:
            events = callback.wait_for_events(cursor, timeout=1.0)
            for ev in events:
                cursor = ev.event_id
                if self._send_sse(ev):
                    broken = 0
                    last_frame = time.time()
                else:
                    broken += 1
                if ev.event_type in ("complete", "error"):
                    return
                if broken >= self._BROKEN_PIPE_CANCEL_THRESHOLD:
                    self._cancel_for_broken_pipe(task_id)
                    return
            if callback.is_closed():
                return
            if time.time() - last_frame >= self._SSE_HEARTBEAT_SECONDS:
                if self._send_heartbeat():
                    last_frame = time.time()
                else:
                    broken += 1
                    if broken >= self._BROKEN_PIPE_CANCEL_THRESHOLD:
                        self._cancel_for_broken_pipe(task_id)
                        return
                    last_frame = time.time()

    @staticmethod
    def _parse_last_event_id(raw: str | None) -> tuple[str, int]:
        """拆 Last-Event-ID 的两套命名空间。

        `r<seq>` = RunStore 的事件游标（跨进程有意义）；纯数字 = 进程内 StreamEvent
        计数器（**跨进程没有意义**：它只是本进程自增的序号）。把计数器当 seq 用会
        从错误的断点续传，所以这里必须分开，不能都 `int()` 掉。
        """
        if not raw:
            return ("none", 0)
        value = str(raw).strip()
        if value.startswith("r") and value[1:].isdigit():
            return ("run", int(value[1:]))
        if value.isdigit():
            return ("sse", int(value))
        return ("none", 0)

    def _send_run_event(self, ev: dict) -> bool:
        """写一条投影事件帧。**只有这类帧的 id 是运行级游标**（`r<seq>`）。"""
        payload = _fast_dumps({
            "type": "run_event",
            "data": {"seq": ev.get("seq"), "event": ev.get("type"),
                     "payload": ev.get("payload") or {}, "ts": ev.get("ts")},
            "ts": ev.get("ts"),
            "section": -1,
            "total": 0,
        }, default=str)
        frame = f"id: r{int(ev['seq'])}\ndata: {payload}\n\n"
        try:
            self.wfile.write(frame.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _send_run_progress(self, task_id: str) -> None:
        """重放没有 chunk/段落，进度只能由投影的节点行合成——不编百分比。"""
        from .streaming import StreamEvent
        nodes = self.orch.run_handle(task_id).nodes() if self.orch else []
        if not nodes:
            return
        finished = sum(1 for n in nodes
                       if str(n.get("status")) in ("success", "skipped", "failed"))
        self._send_sse(StreamEvent("progress", {
            "current": finished, "total": len(nodes),
            "message": f"{finished}/{len(nodes)} 节点", "source": "runstore"}))

    def _stream_replay_from_store(self, task_id: str, last_seq: int) -> bool:
        """从 RunStore 事件流重放（并尾随）一次运行；投影里没有这条运行给 False。

        这条路径治的是"跨进程/重启不可重放"：进程内的 `StreamCallback` 没了，
        只能读事件表。语义边界照实说：重放的是**运行级事实**
        （`run.opened/node.finished/run.completed/...`），不是逐 chunk 的正文流——
        后者从不落盘。
        """
        from .streaming import StreamEvent
        store = self.orch.run_store() if self.orch else None
        if store is None:
            return False
        run = self.orch.run_handle(task_id).record()
        if run is None:
            return False
        handle = self.orch.run_handle(task_id)
        self._send_sse(StreamEvent("connected", {
            "task_id": task_id, "reconnect": last_seq > 0,
            "resumed_from": last_seq, "source": "runstore"}))
        cursor = int(last_seq)
        replayed = 0
        last_frame = time.time()
        while True:
            page = store.events(task_id, after_seq=cursor, limit=200)
            for ev in page:
                cursor = int(ev["seq"])
                replayed += 1
                if not self._send_run_event(ev):
                    return True  # 客户端断了：响应已开始，静默收尾
            if page:
                last_frame = time.time()
                continue
            status = str(handle.status())
            if status in ("done", "failed", "cancelled"):
                break
            if time.time() - last_frame >= self._SSE_HEARTBEAT_SECONDS:
                if not self._send_heartbeat():
                    return True
                last_frame = time.time()
            time.sleep(self._RUN_TAIL_POLL)
        self._send_sse(StreamEvent("resumed", {
            "replayed": replayed, "last_seq": cursor, "source": "runstore"}))
        self._send_run_progress(task_id)
        if status == "done":
            self._send_sse(StreamEvent("complete", {
                "status": status, "last_seq": cursor, "source": "runstore"}))
        else:
            self._send_sse(StreamEvent("error", {
                "error": (run.get("error") or status),
                "status": status, "last_seq": cursor, "source": "runstore"}))
        return True

    def _stream_replay(self, task_id: str, query: str,
                       existing_callback, last_event_id: int) -> None:
        """SSE 重连：从 Last-Event-ID 游标继续推送（历史只读，无需二次入队）"""
        from .streaming import StreamEvent
        self._send_sse(StreamEvent("connected", {"task_id": task_id, "query": query,
                                                 "resumed_from": last_event_id,
                                                 "reconnect": True}))
        replayed = len(existing_callback.get_events_since(last_event_id))
        self._send_sse(StreamEvent("resumed", {"replayed": replayed}))
        self._pump_sse(existing_callback, task_id, start_cursor=last_event_id)

    def _find_streaming_agent(self):
        """按**能力**找支持流式生成的节点实例（不按名字猜）。

        旧实现遍历 `registry._agents.values()`——那存的是 AgentMeta.to_dict()
        出来的 dict，`hasattr(dict, "handle_streaming")` 恒为 False，
        于是本方法恒返回 None，API 提交的任务从来没能挂上流式回调
        （调用方拿到 None 就静默跳过，SSE 只剩进度没有增量内容）。
        """
        for name in self.orch.registry.list_agent_names():
            inst = self.orch.registry.get_instance(name)
            if hasattr(inst, "_register_stream_callback"):
                return inst
        return None

    def _start_stream_worker(self, writer_agent, task_id: str,
                             input_file: Path, callback,
                             owner: str = "") -> threading.Thread:
        """后台线程执行完整流水线（fetcher/researcher/writer 走 run_plan_async 真异步，
        替代旧的 writer_agent.handle_streaming 单 Agent 方式），完成后回调 complete/error"""
        orch = self.orch

        def _run():
            import asyncio as _aio

            from loom_core.scheduler import Scheduler
            try:
                sched = Scheduler()
                name, err = resolve_pipeline_name(
                    "", _scheduler_mod.installed_pipelines(), _default_pipeline_from(orch))
                if err:
                    callback.on_error(err)
                    return
                plan = sched.parse(name)
                # 按 task_id 注册回调，writer 的 _restructure_document 会按 task_id 拾取
                writer_agent._register_stream_callback(task_id, callback)
                task = _aio.run(orch.run_plan_async(
                    plan, input_file=str(input_file), task_id=task_id, owner=owner
                ))
                # 流水线完成后发送 complete 事件
                content = str(getattr(task, "output_content", "") or "") if task else ""
                callback.on_complete(content, {
                    "status": task.status.value if task else "unknown",
                    "task_id": task_id,
                })
            except Exception as e:
                callback.on_error(str(e))
            finally:
                writer_agent._unregister_stream_callback(task_id)
                with contextlib.suppress(OSError):
                    input_file.unlink()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        return worker

    def _handle_stream(self):
        """SSE 流式端点 —— 实时推送文档生成进度。

        用法: GET /stream?task_id=<id>&query=<topic>&title=<title>
        重连: 客户端断线后携带 Last-Event-ID header 重连，从断点继续推送。
        指标: GET /stream/metrics 获取流式指标快照。
        返回: text/event-stream 格式，每个事件含 id/type/data/section/total 字段。
        """
        # HTTP/1.1 keep-alive 下，流式响应不能参与连接复用（没有 Content-Length
        # 可依）；推完显式关闭连接，客户端重连即新连接。
        self.close_connection = True
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # 指标查询端点（聚合所有活跃流的 StreamMetrics，而非新建实例恒报零）
        if parsed.path == "/stream/metrics":
            self._json(stream_metrics_snapshot())
            return

        # S5：task_id 必填——缺省时所有连接共享 "stream" 键，回调注册表互相覆盖、
        # finally 注销误伤他人；订阅已提交任务或发起新流都必须携带唯一 task_id。
        if "task_id" not in params:
            self._json({"error": "missing 'task_id' parameter"}, 400)
            return
        task_id = params["task_id"][0]
        # task_id 会拼进临时输入文件名（stream_{task_id}.md），必须做字符白名单校验，
        # 防止 ../ 路径遍历把攻击者控制的内容写到任意路径（与 /tasks 路由同一防线）
        if not _validate_task_id(task_id):
            self._json({"error": "invalid task id"}, 400)
            return
        query = params.get("query", [""])[0]
        title = params.get("title", ["自动生成文档"])[0]

        # SSE 重连: 检查 Last-Event-ID header（两套命名空间，见 `_parse_last_event_id`）
        cursor_ns, cursor_value = self._parse_last_event_id(
            self.headers.get("Last-Event-ID"))
        last_event_id = cursor_value if cursor_ns == "sse" else 0
        run_cursor = cursor_value if cursor_ns == "run" else 0

        from loom_core.streaming import (
            StreamCallback,
            StreamEvent,
            get_callback,
            register_callback,
            unregister_callback,
        )

        # 已有活跃流：直接挂接订阅（replay 历史后续推），不再重复起流水线
        existing_callback = get_callback(task_id)

        # 投影里的这条运行在不在调用方的作用域内：越权与"不存在"同形（404），
        # 不能因为 SSE 走另一条路径就漏掉租户隔离。
        store_rec = self._scoped_run(task_id)
        raw_store_rec = self.orch.run_handle(task_id).record() if self.orch else None
        if raw_store_rec is not None and store_rec is None:
            self._json({"error": "task not found"}, 404)
            return
        # 运行是否"已经存在"：投影优先，其次持久队列（RunStore 关掉时仍成立）
        has_run = bool(store_rec is not None or (
            self.orch is not None and self.orch.run_handle(task_id).exists()))

        if not query and existing_callback is None and not has_run:
            self._json({"error": "missing 'query' parameter"}, 400)
            return

        # SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # 必须写 `close`：`send_header("Connection", …)` 会改写 close_connection，
        # 写 keep-alive 会把本方法开头那句 `close_connection = True` 翻回 False ⇒
        # 流推完后连接一直挂着，等 EOF 的客户端（http.client/urllib）只能超时，
        # 而 EventSource 看不出来（它自己会重连）。SSE 本就不该参与连接复用。
        self.send_header("Connection", "close")
        self._apply_cors_headers()
        self.end_headers()
        self.close_connection = True

        if existing_callback:
            self._stream_replay(task_id, query, existing_callback, last_event_id)
            return

        # 跨进程/重启：投影里有这条运行就重放事件流 + 尾随到终态，
        # **绝不因为查不到回调而重新起一条流水线**（那会把一次重连变成第二次运行）。
        if store_rec is not None and self._stream_replay_from_store(task_id, run_cursor):
            return
        if has_run:
            # 有运行、但没有可重放的事件（RunStore 被关掉 / 投影没写成功）：
            # 如实说"重放不了"，同样不回退到"再跑一遍"。
            self._send_sse(StreamEvent("error", {
                "error": "run has no replayable events "
                         "(RunStore unavailable? see /api/health/deep)",
                "task_id": task_id}))
            return

        # 首次连接
        self._send_sse(StreamEvent("connected", {"task_id": task_id, "query": query,
                                                 "resumed_from": 0, "reconnect": False}))

        if not self.orch:
            self._send_sse(StreamEvent("error", {"error": "orchestrator not set"}))
            return

        try:
            # 获取 writer agent 实例
            writer_agent = self._find_streaming_agent()
            if not writer_agent:
                self._send_sse(StreamEvent("error", {"error": "writer agent not found"}))
                return

            callback = StreamCallback()
            register_callback(task_id, callback)
            track_stream_callback(task_id, callback)

            # 加载 prompt 模板获取章节数，用于 on_start 通知
            try:
                template = writer_agent._load_prompt_template(
                    writer_agent._prompt_profile)
                sections = template.get("sections", [])
            except Exception:
                sections = []
            callback.on_start(len(sections), title)

            # 创建临时输入文件（供全流水线 fetcher→researcher→writer 使用）
            import tempfile
            input_file = Path(tempfile.gettempdir()) / f"stream_{task_id}.md"
            input_file.write_text(
                f"# {title}\n\n## 查询\n\n{query}\n", encoding="utf-8")

            worker = self._start_stream_worker(
                writer_agent, task_id, input_file, callback,
                owner=self._principal().id)

            # 流式推送事件（心跳 + 断管止损），直到 complete/error
            self._pump_sse(callback, task_id)

            # 等待后台生成线程完整结束，确保 complete/error 事件已推送给客户端；
            # timeout=120s 防止流水线挂死时 HTTP 线程永久阻塞（daemon 线程，
            # 超时后仅放弃等待并注销回调，不影响后台线程自行退出）。
            worker.join(timeout=120)
            if worker.is_alive():
                _logger.warning("[AdminAPI] 流式任务 %s 后台线程 %ss 未结束，提前断开",
                                task_id, 120)

        except Exception as e:
            self._send_sse(StreamEvent("error", {"error": "internal server error"}))
            _logger.exception("[AdminAPI] 流式任务 %s 异常: %s", task_id, e)
        finally:
            unregister_callback(task_id)
            untrack_stream_callback(task_id)


class AdminAPI:
    """管理 API 服务器"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8910,
                 serve_static: bool = False, dashboard_dir: str | None = None,
                 api_key: str | None = None, webhooks_path: str | None = None,
                 tokens_file: str | None = None):
        self.host = host
        self.port = port
        self.serve_static = serve_static
        self.dashboard_dir = dashboard_dir
        # 从环境变量或参数获取 API key
        self.api_key = api_key or os.environ.get("ADMIN_API_KEY", "")
        # token 身份配置（None = LOOM_TOKENS_FILE 或仓库根 tokens.yaml；
        # 文件不存在 = 无身份世界，行为与既往一致）
        self.tokens_file = tokens_file
        # 入站 webhook 配置路径（None = 用 LOOM_WEBHOOKS_FILE——旧名回退可读——或仓库根默认）
        self.webhooks_path = webhooks_path
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        #: 由 start() 从 env 解析并回写（便于测试/运维读回实际值）
        self.max_concurrency: int = DEFAULT_MAX_CONCURRENCY
        self.socket_timeout: float = DEFAULT_SOCKET_TIMEOUT

    def start(self, orch) -> bool:
        """在后台线程启动 API 服务器"""
        try:
            # 安全门：非回环绑定必须配置 API key（本机信任模式仅限回环地址）
            if not self.api_key and self.host not in AdminHandler._LOOPBACK_HOSTS:
                _logger.error(
                    f"[AdminAPI] 拒绝启动：绑定 {self.host} 但未设置 ADMIN_API_KEY。"
                    "公网/容器部署必须设置 ADMIN_API_KEY，或将 host 改为 127.0.0.1（仅本机）。"
                )
                return False
            # token 身份：装载 tokens.yaml（结构性错误 = 拒绝启动——身份配置
            # 写错还带病运行，等于隔离静默失效）
            try:
                AdminHandler.tokens = TokenStore.load(
                    Path(self.tokens_file) if self.tokens_file else None)
            except TokenConfigError as e:
                _logger.error("[AdminAPI] 拒绝启动：token 配置非法：%s", e)
                return False
            if AdminHandler.tokens.enabled:
                n = len(AdminHandler.tokens.entries)
                _logger.info(
                    "[AdminAPI] token 身份已启用：%d 个凭证（%s）——回环访问同样要求凭证",
                    n, AdminHandler.tokens.source)

            AdminHandler.orch = orch
            AdminHandler.api_key = self.api_key
            AdminHandler.server_host = self.host
            # 入站 webhook：默认读仓库根 webhooks.yaml（可用 LOOM_WEBHOOKS_FILE——
            # 旧名 DOC_PIPELINE_WEBHOOKS_FILE 过渡期仍可读——或显式路径覆盖）；
            # 未配置文件 = 能力休眠（不报错）。配置写坏则整体禁用并大声记日志——
            # 半个配置放行比全禁更危险。
            from .webhooks import WebhookConfigError, default_config_path, load_webhooks, webhooks_file_env
            wh_path = (self.webhooks_path or webhooks_file_env()
                       or str(default_config_path()))
            try:
                AdminHandler.webhooks = load_webhooks(wh_path)
                if AdminHandler.webhooks:
                    _logger.info(
                        f"[AdminAPI] 入站 webhook: {len(AdminHandler.webhooks)} 个（{wh_path}）")
            except WebhookConfigError as e:
                AdminHandler.webhooks = {}
                _logger.error(f"[AdminAPI] webhooks 配置非法，入站触发已禁用: {e}")
            if self.serve_static and self.dashboard_dir:
                AdminHandler.dashboard_dir = self.dashboard_dir
                _logger.info(f"[AdminAPI] 静态文件目录: {self.dashboard_dir}")
            if self.api_key:
                _logger.info("[AdminAPI] 已启用 API Key 鉴权")
            else:
                _logger.warning(
                    "[AdminAPI] 未配置 ADMIN_API_KEY：本机信任模式（仅回环访问免鉴权），"
                    "受保护端点对远程来源返回 401。"
                )
            # 并发模型（2026-10-13 P1 决策 A）：HTTP/1.1 keep-alive + 有界线程池
            # + socket 超时。spec §3 P1 原文点名要替换的"stdlib HTTP/1.0 +
            # 线程-per-连接、无并发上限"到此事实性关闭；env 可调，默认值见常量。
            self.max_concurrency = _int_env("LOOM_ADMIN_MAX_CONCURRENCY",
                                            DEFAULT_MAX_CONCURRENCY)
            self.socket_timeout = _float_env("LOOM_ADMIN_SOCKET_TIMEOUT",
                                             DEFAULT_SOCKET_TIMEOUT)
            AdminHandler.timeout = self.socket_timeout
            self._server = BoundedThreadingHTTPServer(
                (self.host, self.port), AdminHandler, max_workers=self.max_concurrency)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True,
                name="admin-api",
            )
            self._thread.start()
            _logger.info(f"[AdminAPI] 启动 http://{self.host}:{self.port}"
                         f"（HTTP/1.1 keep-alive，并发上限 {self.max_concurrency}，"
                         f"socket 超时 {self.socket_timeout:g}s）")
            return True
        except Exception as e:
            _logger.error(f"[AdminAPI] 启动失败: {e}")
            return False

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()  # 含 ThreadPoolExecutor.shutdown
            if self._thread:
                self._thread.join(timeout=2)
            self._server = None
            self._thread = None
