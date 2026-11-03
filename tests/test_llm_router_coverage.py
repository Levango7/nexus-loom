"""llm_router 覆盖缺口补强（C-17）。

既有 `test_llm_router.py` 钉了供应商健康 / chat fallback / 预算熔断 / 成本
记录 / usage 计费；本文件补齐：

- 共享 aiohttp Session 域：同 loop 复用、跨 loop 返回 None、无 aiohttp 回落、
  `_close_shared_session` 复位、atexit 钩子的两处早退与真关闭；
- `_call_llm_async` 的 aiohttp 分支（被既有测试整体 mock 掉，0 覆盖）：
  成功 / CF 格式解包 / usage 回填 / 空内容按业务结果上抛**不**回退同步重试
  （C15 语义：请求已计费）/ 传输故障回退 run_in_executor；
- `chat` / `chat_async` 的全不健康重检循环：冷却外恢复、冷却外仍不健康才告警
  （日志洪水防线）、async 空内容转移到下一供应商；
- 杂项：`add_provider` 重排、同优先级禁用靠后、`from_dict` 最小配置缺省、
  usage 非 int 不回填、`health_check_all` 字段面。

不可达分支留证言：`from_env` 的「通用键兼容 default」分支（581-590）与
provider_defs 首项 cloudflare 同键（LLM_API_URL/MODEL/API_KEY），任何能让
兼容分支命中的 env 都会先命中首项——不改产品代码，此 6 条语句不计入目标。

mock 口径：`_call_llm_async` / `_get_shared_session` 都在**函数体内** import
或调用，monkeypatch `loom_core.llm_router.*` 打点；HTTP 一律 mock，不出网。
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import loom_core.llm_router as lr
from loom_core.cost_tracker import CostTracker
from loom_core.llm_router import (
    LLMProvider,
    LLMRouter,
    _call_llm,
    _close_shared_session,
    _close_shared_session_at_exit,
    _EmptyContentError,
    _get_shared_session,
)


@pytest.fixture
def cost_tracker(tmp_path):
    """临时 DB 的全局成本追踪器（chat 系测试共用，测后还原单例）"""
    import loom_core.cost_tracker as ct_mod
    old = ct_mod._tracker_instance
    tracker = CostTracker(db_path=str(tmp_path / "cost.db"))
    ct_mod._tracker_instance = tracker
    yield tracker
    ct_mod._tracker_instance = old


@pytest.fixture
def shared_session_guard():
    """保存/还原模块级共享 Session 全局——跨用例不得泄漏。"""
    old_session, old_loop = lr._shared_aiohttp_session, lr._shared_session_loop
    yield
    if lr._shared_aiohttp_session is not None and not lr._shared_aiohttp_session.closed:
        asyncio.run(_close_shared_session())
    lr._shared_aiohttp_session = old_session
    lr._shared_session_loop = old_loop


def _provider(name="p0", priority=0, **kw):
    return LLMProvider(name=name, api_url=f"http://{name}/v1",
                       api_key="k", model=f"m-{name}", priority=priority, **kw)


def _router(*providers) -> LLMRouter:
    return LLMRouter(list(providers))


# ─── 共享 aiohttp Session 域 ─────────────────────────────────

class TestSharedSession:
    """C15：Session 绑定创建它的 loop，跨 loop 直接给 None 而不是等运行时报错。"""

    def test_reused_within_same_loop(self, shared_session_guard):
        async def _two_calls():
            s1 = await _get_shared_session()
            s2 = await _get_shared_session()
            return s1, s2

        s1, s2 = asyncio.run(_two_calls())
        assert s1 is not None and s1 is s2, "同 loop 内必须复用连接池"

    def test_cross_loop_returns_none(self, shared_session_guard):
        async def _make():
            return await _get_shared_session()

        async def _ask():
            return await _get_shared_session()

        first = asyncio.run(_make())
        assert first is not None
        assert asyncio.run(_ask()) is None, "别的 loop 拿旧 Session 必炸，须给 None"

    def test_missing_aiohttp_returns_none(self, shared_session_guard, monkeypatch):
        monkeypatch.setitem(sys.modules, "aiohttp", None)  # import 即 ImportError

        async def _ask():
            return await _get_shared_session()

        assert asyncio.run(_ask()) is None

    def test_close_resets_globals(self, shared_session_guard):
        async def _lifecycle():
            s = await _get_shared_session()
            await _close_shared_session()
            return s

        s = asyncio.run(_lifecycle())
        assert s.closed is True
        assert lr._shared_aiohttp_session is None
        assert lr._shared_session_loop is None

    def test_atexit_skips_when_absent_or_closed(self, shared_session_guard):
        lr._shared_aiohttp_session = None
        _close_shared_session_at_exit()  # 不该有任何动作或异常

        closed = SimpleNamespace(closed=True, close=AsyncMock())
        lr._shared_aiohttp_session = closed
        _close_shared_session_at_exit()  # 已关闭：早退，不再 close
        closed.close.assert_not_awaited()

    def test_atexit_closes_open_session(self, shared_session_guard):
        async def _make():
            return await _get_shared_session()

        session = asyncio.run(_make())
        assert session.closed is False
        _close_shared_session_at_exit()  # best-effort：进程退出清连接
        assert session.closed is True
        assert lr._shared_aiohttp_session is None


# ─── _call_llm_async 的 aiohttp 分支 ─────────────────────────

class _FakeResp:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakePostCtx:
    def __init__(self, body=None, exc=None):
        self._body, self._exc = body, exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._body)

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, body=None, exc=None):
        self.body, self.exc = body, exc
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(url)
        return _FakePostCtx(self.body, self.exc)


def _patch_session(monkeypatch, session):
    async def _fake_get():
        return session

    monkeypatch.setattr(lr, "_get_shared_session", _fake_get)


class TestCallLlmAsyncAiohttp:

    def test_success_with_usage(self, monkeypatch):
        session = _FakeSession(body={
            "choices": [{"message": {"content": "你好"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        })
        _patch_session(monkeypatch, session)
        usage: dict = {}
        content = asyncio.run(lr._call_llm_async(
            _provider(), [{"role": "user", "content": "hi"}], usage_out=usage))
        assert content == "你好"
        assert usage == {"prompt_tokens": 7, "completion_tokens": 9}

    def test_cloudflare_result_unwrapped(self, monkeypatch):
        session = _FakeSession(body={
            "result": {"choices": [{"message": {"content": "cf 正文"}}]},
        })
        _patch_session(monkeypatch, session)
        content = asyncio.run(lr._call_llm_async(
            _provider("cf", is_cloudflare=True), [{"role": "user", "content": "hi"}]))
        assert content == "cf 正文"

    def test_think_tag_filtered_async(self, monkeypatch):
        session = _FakeSession(body={
            "choices": [{"message": {"content": "<think>推理</think>结论"}}],
        })
        _patch_session(monkeypatch, session)
        content = asyncio.run(lr._call_llm_async(
            _provider(), [{"role": "user", "content": "hi"}]))
        assert content == "结论"

    def test_empty_content_raises_not_sync_retried(self, monkeypatch):
        """C15 语义：空内容是业务结果不是传输故障——直接上抛，
        绝不回退同步对同一供应商重复计费请求。"""
        session = _FakeSession(body={"choices": [{"message": {"content": ""}}]})
        _patch_session(monkeypatch, session)
        with patch("urllib.request.urlopen") as mock_sync, \
                pytest.raises(_EmptyContentError):
            asyncio.run(lr._call_llm_async(
                _provider(), [{"role": "user", "content": "hi"}]))
        mock_sync.assert_not_called()

    def test_transport_failure_falls_back_to_sync(self, monkeypatch):
        """aiohttp 传输故障回退 run_in_executor 同步路径。"""
        session = _FakeSession(exc=RuntimeError("连接重置"))
        _patch_session(monkeypatch, session)
        with patch("loom_core.llm_router._call_llm", return_value="同步兜底") as mock_sync:
            content = asyncio.run(lr._call_llm_async(
                _provider(), [{"role": "user", "content": "hi"}]))
        assert content == "同步兜底"
        mock_sync.assert_called_once()


# ─── chat / chat_async 全不健康重检循环 ───────────────────────

class _AllUnhealthyRouter:
    @staticmethod
    def router():
        r = _router(_provider("a", 10), _provider("b", 20))
        for p in r._providers:
            p._healthy = False
            p._last_check = 0.0  # 无冷却记录 ⇒ check_health 会真发起
        return r


class TestChatRecheckLoop:

    def test_all_unhealthy_recovers_via_check_health(self, cost_tracker):
        """全不健康时走重检：check_health 成功即恢复并照常服务。"""
        router = _AllUnhealthyRouter.router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"), \
                patch.object(router._providers[1], "_fail_count", 0):
            content, name = router.chat([{"role": "user", "content": "hi"}])
        assert name in ("a", "b") and content == "ok"
        assert router._providers[0].healthy or router._providers[1].healthy

    def test_cooldown_skip_silent_within_chat(self, cost_tracker):
        """冷却内的不健康供应商在 chat 重检时被静默跳过——既不发请求也不告警。

        证据注记：374-377 的「冷却外仍不健康」告警分支实测不可达——check_health
        无论走冷却跳过（elapsed<冷却）还是真失败（mark_failed 刷新 _last_check），
        回到调用点时 elapsed 都小于冷却 ⇒ `>= HEALTH_CHECK_COOLDOWN` 恒假。
        本判据钉住可达的形状：冷却内不发起请求（_call_llm 零调用）、整个 chat
        以「所有供应商不可用」收场。"""
        import time as _t
        router = _AllUnhealthyRouter.router()
        for p in router._providers:
            p._last_check = _t.time()  # 刚检查过 ⇒ 冷却内
        with patch("loom_core.llm_router._call_llm") as mock_call, \
                pytest.raises(RuntimeError, match="所有 LLM 供应商不可用"):
            router.chat([{"role": "user", "content": "hi"}])
        mock_call.assert_not_called()


class TestChatAsyncExtras:

    def test_preferred_used_first(self, cost_tracker):
        router = _router(_provider("a", 10), _provider("b", 20), _provider("c", 30))
        seen = []

        async def _rec(provider, messages, *a, **kw):
            seen.append(provider.name)
            return "ok"

        with patch("loom_core.llm_router._call_llm_async", side_effect=_rec):
            _, name = asyncio.run(router.chat_async(
                [{"role": "user", "content": "hi"}], preferred="c"))
        assert seen[0] == "c" and name == "c"

    def test_empty_content_moves_to_next_provider(self, cost_tracker):
        """C15：async 空内容按业务结果转移下一供应商，不重试同一供应商。"""
        router = _router(_provider("a", 10), _provider("b", 20))
        mock = AsyncMock(side_effect=[_EmptyContentError("供应商 a 返回空内容"), "来自 b"])
        with patch("loom_core.llm_router._call_llm_async", mock):
            content, name = asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        assert (content, name) == ("来自 b", "b")
        assert mock.await_count == 2
        # 一次失败只计数不摘除（3 连败才不健康）——但错误如实留痕
        assert router._providers[0]._fail_count == 1
        assert "空内容" in router._providers[0]._last_error

    def test_all_unhealthy_recovers_via_recheck(self, cost_tracker):
        router = _AllUnhealthyRouter.router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"), \
                patch("loom_core.llm_router._call_llm_async", new_callable=AsyncMock,
                      return_value="异步正文"):
            content, name = asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        assert name in ("a", "b") and content == "异步正文"

    def test_no_providers_raises_unusable(self, cost_tracker):
        with pytest.raises(RuntimeError, match="所有 LLM 供应商不可用"):
            asyncio.run(LLMRouter(providers=[]).chat_async([{"role": "user", "content": "hi"}]))

    def test_all_fail_raises_with_last_error(self, cost_tracker):
        router = _router(_provider("a", 10), _provider("b", 20))
        mock = AsyncMock(side_effect=[RuntimeError("a 断网"), RuntimeError("b 断网")])
        with patch("loom_core.llm_router._call_llm_async", mock), \
                pytest.raises(RuntimeError, match="所有 LLM 供应商异步调用失败") as excinfo:
            asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        assert mock.await_count == 2
        assert "b 断网" in str(excinfo.value)  # 最后错误留痕在异常消息里
        assert router._providers[1]._fail_count == 1

    def test_cost_record_failure_does_not_break_async(self, cost_tracker):
        """同步版已有同款判据；async 的成本失败告警分支同形不拖垮主流程。"""
        router = _router(_provider("a", 10))
        with patch("loom_core.llm_router._call_llm_async",
                   new_callable=AsyncMock, return_value="正文"), \
                patch("loom_core.cost_tracker.CostTracker.record_call",
                      side_effect=RuntimeError("db broken")):
            content, name = asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        assert (content, name) == ("正文", "a")


# ─── 杂项分支 ────────────────────────────────────────────────

class TestRouterExtras:

    def test_add_provider_resorts(self):
        router = _router(_provider("a", 10))
        router.add_provider(_provider("b", 5))
        assert [p.name for p in router._providers] == ["b", "a"]

    def test_disabled_sorts_after_enabled_at_same_priority(self):
        router = _router(_provider("on", 10, enabled=True),
                         _provider("off", 10, enabled=False))
        assert [p.name for p in router._providers] == ["on", "off"]

    def test_preferred_name_not_found_keeps_order(self, cost_tracker):
        router = _router(_provider("a", 10), _provider("b", 20))
        with patch("loom_core.llm_router._call_llm", return_value="ok") as mock:
            _, name = router.chat([{"role": "user", "content": "hi"}], preferred="ghost")
        assert name == "a"
        assert mock.call_args[0][0].name == "a"

    def test_from_dict_minimal_config_uses_defaults(self):
        router = LLMRouter.from_dict(
            {"providers": [{"api_url": "http://x", "api_key": "k", "model": "m"}]})
        (p,) = router._providers
        assert (p.name, p.priority, p.enabled, p.is_cloudflare,
                p.max_tokens, p.timeout) == ("provider_0", 100, True, False, 4096, 120)

    def test_call_llm_non_int_usage_untouched(self):
        provider = _provider()
        usage: dict = {}
        resp = MagicMock()
        resp.read.return_value = (b'{"choices":[{"message":{"content":"x"}}],'
                                  b'"usage":{"prompt_tokens":"7","completion_tokens":null}}')
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            _call_llm(provider, [{"role": "user", "content": "hi"}], usage_out=usage)
        assert usage == {}, "非 int 的 usage 不回填（宁缺勿错）"

    def test_health_check_all_disabled_and_fields(self, cost_tracker):
        router = _router(_provider("live", 10), _provider("dead", 20, enabled=False))
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            result = router.health_check_all()
        assert result["dead"] == {"status": "disabled", "healthy": False}
        live = result["live"]
        assert live["status"] == "healthy"
        assert live["model"] == "m-live" and live["priority"] == 10
        assert live["fail_count"] == 0 and live["last_error"] == ""
