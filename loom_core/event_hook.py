"""
EventHook - 事件钩子系统
========================
供 PEV 框架或其他外部系统订阅 Loom 生命周期事件。

支持的事件类型：
  - task.created       任务创建
  - task.started       任务开始执行
  - task.completed     任务完成
  - task.failed        任务失败
  - task.cancelled     任务取消
  - agent.started      Agent 启动
  - agent.stopped      Agent 停止
  - agent.error        Agent 异常
  - quality_gate.evaluated  质量门控评分完成
  - quality_gate.regenerate 质量门控触发重做
  - circuit_breaker.open    熔断器打开
  - circuit_breaker.close   熔断器恢复

钩子触发方式：
  - webhook: HTTP POST 回调到指定 URL（全异步 aiohttp 发送，不阻塞调用方）
  - callback: Python 可调用对象（进程内）

线程安全：所有操作均通过 _lock 保护。
Webhook 异步架构：专用事件循环线程 + asyncio.Queue + aiohttp.ClientSession（连接池复用）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin

from artesian.url_guard import validate_public_http_url

_logger = logging.getLogger(__name__)


# ── 全异步 webhook 投递引擎 ────────────────────────────────────
# 专用事件循环运行在后台线程中，emit() 通过 run_coroutine_threadsafe 投递任务
_webhook_loop: asyncio.AbstractEventLoop | None = None
_webhook_thread: threading.Thread | None = None
_webhook_async_queue: asyncio.Queue | None = None
_webhook_session: Any | None = None  # aiohttp.ClientSession
_webhook_stop_event: asyncio.Event | None = None
_webhook_worker_future: Any | None = None  # concurrent.futures.Future for worker task
_webhook_pending_tasks: set = set()  # track in-flight _deliver_one tasks for graceful shutdown
_webhook_init_lock = threading.Lock()
_webhook_engine_ready = False  # True only when queue + worker fully initialized
_WEBHOOK_QUEUE_MAXSIZE = 10000
_WEBHOOK_TIMEOUT = 10  # seconds per HTTP POST
_WEBHOOK_MAX_CONCURRENCY = 50  # max simultaneous in-flight webhook requests
_WEBHOOK_MAX_REDIRECTS = 5  # 与 fetcher 对齐：重定向手动逐跳跟随，每跳重新过 SSRF 校验


async def _async_webhook_worker():
    """Async worker: drain queue and deliver webhooks via aiohttp with concurrency limit."""
    global _webhook_session
    import aiohttp

    _webhook_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_WEBHOOK_TIMEOUT),
        connector=aiohttp.TCPConnector(
            limit=_WEBHOOK_MAX_CONCURRENCY,
            limit_per_host=20,
            ttl_dns_cache=300,
        ),
    )
    _logger.info("[EventHook] aiohttp session started (async webhook engine)")

    semaphore = asyncio.Semaphore(_WEBHOOK_MAX_CONCURRENCY)

    try:
        while True:
            try:
                job = await asyncio.wait_for(_webhook_async_queue.get(), timeout=1.0)
            except TimeoutError:
                if _webhook_stop_event.is_set():
                    break
                continue

            if job is None:  # shutdown sentinel
                break

            task = asyncio.create_task(_deliver_one(semaphore, job))
            _webhook_pending_tasks.add(task)
            task.add_done_callback(_webhook_pending_tasks.discard)

        # 等待所有 in-flight 请求完成
        if _webhook_pending_tasks:
            _logger.info(f"[EventHook] Draining {len(_webhook_pending_tasks)} in-flight webhooks...")
            await asyncio.gather(*_webhook_pending_tasks, return_exceptions=True)
    finally:
        # P0 fix: ensure session is closed even if worker is cancelled during
        # shutdown timeout (task.cancel() at asyncio.gather would otherwise skip
        # the close() call below, leaking the aiohttp ClientSession).
        if _webhook_session is not None and not _webhook_session.closed:
            with contextlib.suppress(Exception):
                await _webhook_session.close()
        _logger.info("[EventHook] aiohttp session closed (async webhook engine stopped)")


async def _deliver_one(semaphore: asyncio.Semaphore, job: tuple):
    """Deliver a single webhook with concurrency control.

    SSRF 出网层兜底（补齐 admin_api 注册期仅拦裸 IP 的缺口）：
    aiohttp 关闭自动重定向，逐跳手动跟随，并对每一跳重新执行
    validate_public_http_url（DNS 全记录解析 + 私网/环回/链路本地/元数据段拒绝），
    封死"注册时公网域名 → 解析或 302 跳内网"的绕过路径。
    """
    hook_id, url, headers, body = job
    async with semaphore:
        current = url
        try:
            for hop in range(_WEBHOOK_MAX_REDIRECTS + 1):
                ok, reason = await asyncio.to_thread(validate_public_http_url, current)
                if not ok:
                    _logger.error(
                        f"[EventHook] webhook {url} 第{hop + 1}跳 SSRF 拒绝({reason}): {current}")
                    return
                async with _webhook_session.post(  # type: ignore[union-attr]
                    current, data=body, headers=headers, allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location", "")
                        if not loc:
                            _logger.error(
                                f"[EventHook] webhook {current} 返回 {resp.status} 但缺少 Location，放弃")
                            return
                        current = urljoin(current, loc)
                        continue
                    if resp.status >= 400:
                        _logger.warning(
                            f"[EventHook] webhook {url} returned {resp.status}"
                        )
                    return
            _logger.error(
                f"[EventHook] webhook {url} 重定向超过 {_WEBHOOK_MAX_REDIRECTS} 跳上限，放弃")
        except Exception as e:
            _logger.error(f"[EventHook] webhook {url} failed: {e}")


def _safe_enqueue(queue: asyncio.Queue, item):
    """Safely enqueue item, logging warning if queue is full."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        _logger.warning("[EventHook] async queue full, dropping webhook event")


def _ensure_webhook_engine():
    """Start the async webhook engine (dedicated loop thread + aiohttp session) if not running."""
    global _webhook_thread, _webhook_loop, _webhook_async_queue, _webhook_stop_event, _webhook_worker_future, _webhook_engine_ready

    with _webhook_init_lock:
        if _webhook_thread is not None and _webhook_thread.is_alive():
            return

        # Create event loop in main thread (safe), run it in background thread
        _webhook_loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(_webhook_loop)
            _webhook_loop.run_forever()

        _webhook_thread = threading.Thread(
            target=_run_loop, name="event-hook-async-loop", daemon=True
        )
        _webhook_thread.start()

        # Now _webhook_loop is set, safe to schedule coroutines on it
        async def _init():
            global _webhook_async_queue, _webhook_stop_event
            _webhook_async_queue = asyncio.Queue(maxsize=_WEBHOOK_QUEUE_MAXSIZE)
            _webhook_stop_event = asyncio.Event()

        future = asyncio.run_coroutine_threadsafe(_init(), _webhook_loop)
        future.result(timeout=5)

        # Start the worker coroutine on the dedicated loop and track its future
        _webhook_worker_future = asyncio.run_coroutine_threadsafe(
            _async_webhook_worker(), _webhook_loop
        )
        _webhook_engine_ready = True
        _logger.info("[EventHook] Async webhook engine started (asyncio + aiohttp)")


@dataclass
class Hook:
    """事件钩子定义"""
    id: str
    event: str                          # 订阅的事件类型（支持通配符 * ）
    url: str | None = None           # webhook URL（None 则为 callback 模式）
    headers: dict = field(default_factory=dict)  # webhook 自定义 header
    callback: Callable | None = None # 进程内回调函数
    created_at: float = field(default_factory=time.time)
    call_count: int = 0                # 触发次数
    last_called: float = 0             # 最后触发时间
    enabled: bool = True

    def matches(self, event: str) -> bool:
        """检查事件是否匹配此钩子"""
        if not self.enabled:
            return False
        if self.event == "*" or self.event == event:
            return True
        # 支持前缀通配: "task.*" 匹配 "task.completed"
        if self.event.endswith(".*"):
            prefix = self.event[:-2]
            return event.startswith(prefix + ".")
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("callback", None)  # callback 不可序列化
        return d


class EventHookManager:
    """事件钩子管理器（线程安全单例）"""

    def __init__(self):
        self._hooks: dict[str, Hook] = {}
        self._lock = threading.RLock()

    def register(
        self,
        event: str,
        url: str | None = None,
        headers: dict | None = None,
        callback: Callable | None = None,
    ) -> str:
        """注册事件钩子，返回 hook_id"""
        hook_id = str(uuid.uuid4())[:8]
        hook = Hook(
            id=hook_id,
            event=event,
            url=url,
            headers=headers or {},
            callback=callback,
        )
        with self._lock:
            self._hooks[hook_id] = hook
        _logger.info(f"[EventHook] 注册钩子: {event} -> {url or 'callback'} (id={hook_id})")
        return hook_id

    def unregister(self, hook_id: str) -> bool:
        """注销事件钩子"""
        with self._lock:
            if hook_id in self._hooks:
                del self._hooks[hook_id]
                return True
            return False

    def enable(self, hook_id: str) -> bool:
        """启用钩子"""
        with self._lock:
            hook = self._hooks.get(hook_id)
            if hook:
                hook.enabled = True
                return True
            return False

    def disable(self, hook_id: str) -> bool:
        """禁用钩子"""
        with self._lock:
            hook = self._hooks.get(hook_id)
            if hook:
                hook.enabled = False
                return True
            return False

    def list_hooks(self) -> list[dict]:
        """列出所有钩子"""
        with self._lock:
            return [h.to_dict() for h in self._hooks.values()]

    def emit(self, event: str, payload: dict) -> int:
        """触发事件，返回成功调用的钩子数"""
        with self._lock:
            matched = [h for h in self._hooks.values() if h.matches(event)]

        if not matched:
            return 0

        count = 0
        for hook in matched:
            try:
                if hook.callback:
                    hook.callback(event, payload)
                elif hook.url:
                    self._fire_webhook(hook, event, payload)
                hook.call_count += 1
                hook.last_called = time.time()
                count += 1
            except Exception as e:
                _logger.error(f"[EventHook] 钩子 {hook.id} 触发失败: {e}")
        return count

    def _fire_webhook(self, hook: Hook, event: str, payload: dict):
        """Schedule async webhook HTTP POST via aiohttp (non-blocking, thread-safe)."""
        _ensure_webhook_engine()
        if not _webhook_engine_ready:
            _logger.warning("[EventHook] webhook engine not ready, dropping event")
            return
        # W6：worker 协程若已静默死亡（异常退出），事件会持续入队但永不被投递，
        # 表现为"传输层存活、事件全部丢失"。投递前检测存活，死亡则重新拉起
        # 投递协程（旧 session 由进程退出兜底回收，重启属罕见路径）。
        global _webhook_worker_future
        fut = _webhook_worker_future
        if fut is not None and fut.done():
            with contextlib.suppress(BaseException):
                exc = fut.exception()
            _logger.error(
                f"[EventHook] webhook worker 异常退出(exception={exc})，重新拉起投递协程")
            _webhook_worker_future = asyncio.run_coroutine_threadsafe(
                _async_webhook_worker(), _webhook_loop  # type: ignore[arg-type]
            )
        body = json.dumps({
            "event": event,
            "payload": payload,
            "hook_id": hook.id,
            "timestamp": time.time(),
        }, ensure_ascii=False, default=str)
        headers = {"Content-Type": "application/json"}
        headers.update(hook.headers)
        try:
            # P1 修复: 删除冗余的二次 _ensure_webhook_engine 调用
            # （已在方法开头调用过，重复调用既无必要也增加锁竞争）
            # put_nowait is sync but must run on the loop thread
            _webhook_loop.call_soon_threadsafe(  # type: ignore[union-attr]
                _safe_enqueue, _webhook_async_queue, (hook.id, hook.url, headers, body)  # type: ignore[arg-type]
            )
        except Exception as e:
            _logger.error(f"[EventHook] failed to enqueue webhook: {e}")

    def clear(self):
        """清空所有钩子"""
        with self._lock:
            self._hooks.clear()


def shutdown_webhook(timeout_s: float = 10.0):
    """Gracefully shut down the async webhook engine."""
    global _webhook_thread, _webhook_loop, _webhook_async_queue, _webhook_stop_event, _webhook_worker_future, _webhook_session, _webhook_engine_ready

    if _webhook_loop is None or _webhook_thread is None:
        return

    if not _webhook_thread.is_alive():
        _webhook_engine_ready = False
        return

    # Signal stop + enqueue sentinel
    async def _signal_stop():
        _webhook_stop_event.set()
        if _webhook_async_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                _webhook_async_queue.put_nowait(None)

    with contextlib.suppress(Exception):
        fut = asyncio.run_coroutine_threadsafe(_signal_stop(), _webhook_loop)
        fut.result(timeout=5)

    # Wait for worker to drain in-flight requests and close session
    if _webhook_worker_future is not None:
        try:
            _webhook_worker_future.result(timeout=timeout_s)
        except Exception:
            # Timeout: cancel any remaining in-flight tasks before stopping loop
            if _webhook_pending_tasks:
                _logger.warning(
                    f"[EventHook] Shutdown timeout, cancelling {len(_webhook_pending_tasks)} in-flight webhooks"
                )
                for task in list(_webhook_pending_tasks):
                    if not task.done():
                        task.cancel()
                _webhook_pending_tasks.clear()
            # P0 fix: explicitly close aiohttp session on shutdown timeout.
            # The worker coroutine may have been cancelled at asyncio.gather()
            # before reaching its finally block, so we close the session here
            # via run_coroutine_threadsafe to avoid "Unclosed client session" warning.
            if _webhook_session is not None and not _webhook_session.closed:
                with contextlib.suppress(Exception):
                    close_fut = asyncio.run_coroutine_threadsafe(
                        _webhook_session.close(), _webhook_loop
                    )
                    close_fut.result(timeout=3)

    # Now safe to stop the event loop and join thread
    _webhook_loop.call_soon_threadsafe(_webhook_loop.stop)
    _webhook_thread.join(timeout=5)

    # Cleanup globals
    _webhook_loop = None
    _webhook_thread = None
    _webhook_async_queue = None
    _webhook_stop_event = None
    _webhook_worker_future = None
    _webhook_session = None
    _logger.info("[EventHook] Async webhook engine stopped")


# ── 全局单例 ──────────────────────────────────────

_global_manager: EventHookManager | None = None
_singleton_lock = threading.Lock()


def get_hook_manager() -> EventHookManager:
    """获取全局 EventHookManager 单例"""
    global _global_manager
    if _global_manager is None:
        with _singleton_lock:
            if _global_manager is None:
                _global_manager = EventHookManager()
    return _global_manager


def emit_event(event: str, payload: dict) -> int:
    """便捷函数：触发事件"""
    return get_hook_manager().emit(event, payload)
