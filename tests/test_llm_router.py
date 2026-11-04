"""LLMRouter — 多供应商 fallback / 重试 / token 估算 / 健康检查

测试原则：
  - 用 unittest.mock 模拟 HTTP 调用，不实际请求网络
  - 每个测试方法聚焦一个行为
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loom_core.cost_tracker import BudgetExceededError, estimate_tokens
from loom_core.llm_router import (
    LLMProvider,
    LLMRouter,
    _call_llm,
    _load_env,
    get_router,
    reset_router,
)


@pytest.fixture
def cost_tracker(tmp_path):
    """临时 DB 的全局成本追踪器（测试后还原单例）"""
    import loom_core.cost_tracker as ct_mod
    from loom_core.cost_tracker import CostTracker
    old = ct_mod._tracker_instance
    tracker = CostTracker(db_path=str(tmp_path / "cost.db"))
    ct_mod._tracker_instance = tracker
    yield tracker
    ct_mod._tracker_instance = old


def _make_single_router() -> LLMRouter:
    return LLMRouter([
        LLMProvider(name="p0", api_url="http://h0/v1", api_key="k", model="m0"),
    ])


def _mock_http_response(body: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode()
    mock_resp.__enter__ = lambda self: mock_resp
    mock_resp.__exit__ = lambda *a: False
    return mock_resp


def _fetch_cost_rows(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT provider, model, prompt_tokens, completion_tokens FROM cost_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

# ─── LLMProvider 单元测试 ────────────────────────────

class TestLLMProvider:
    """LLMProvider 数据类行为"""

    def _make_provider(self, **kwargs):
        defaults = dict(
            name="test", api_url="http://localhost/v1",
            api_key="sk-test", model="test-model",
        )
        defaults.update(kwargs)
        return LLMProvider(**defaults)

    def test_default_healthy(self):
        """新建供应商默认健康"""
        p = self._make_provider()
        assert p.healthy is True
        assert p._fail_count == 0

    def test_mark_failed_increments_count(self):
        """mark_failed 累加失败计数"""
        p = self._make_provider()
        p.mark_failed("timeout")
        assert p._fail_count == 1
        assert p.healthy is True  # 1 次不标记不健康

    def test_mark_failed_unhealthy_after_3(self):
        """连续 3 次失败标记为不健康"""
        p = self._make_provider()
        for i in range(3):
            p.mark_failed(f"err{i}")
        assert p.healthy is False
        assert p._fail_count == 3

    def test_mark_success_resets(self):
        """mark_success 重置失败计数和健康状态"""
        p = self._make_provider()
        p.mark_failed("err")
        p.mark_failed("err")
        p.mark_failed("err")
        assert p.healthy is False
        p.mark_success()
        assert p.healthy is True
        assert p._fail_count == 0
        assert p._last_error == ""

    def test_check_health_cooldown_skips_recent_unhealthy(self):
        """冷却期内跳过已不健康供应商的健康检查"""
        p = self._make_provider()
        p._healthy = False
        p._last_check = time.time()  # 刚检查过
        # 冷却期内应直接返回 False，不发起请求
        result = p.check_health(cooldown=60.0)
        assert result is False

    def test_check_health_success_marks_healthy(self):
        """健康检查成功后标记为健康"""
        p = self._make_provider()
        p._healthy = False
        p._last_check = time.time() - 120  # 超过冷却期
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            result = p.check_health(cooldown=60.0)
        assert result is True
        assert p.healthy is True
        # 刚过冷却期（90s > 60s，但 < 2×60s）也必须真去检查：窗口不能被放大
        p._healthy = False
        p._last_check = time.time() - 90
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            assert p.check_health(cooldown=60.0) is True

    def test_check_health_failure_marks_unhealthy(self):
        """健康检查失败后累计失败计数（3 次后才标记不健康）"""
        p = self._make_provider()
        with patch("loom_core.llm_router._call_llm", side_effect=Exception("conn refused")):
            result = p.check_health(cooldown=0)
        assert result is False
        # 1 次失败不标记不健康，但累计失败计数
        assert p._fail_count == 1
        # 连续 3 次后才标记不健康
        with patch("loom_core.llm_router._call_llm", side_effect=Exception("conn refused")):
            p.check_health(cooldown=0)
        with patch("loom_core.llm_router._call_llm", side_effect=Exception("conn refused")):
            p.check_health(cooldown=0)
        assert p.healthy is False


# ─── LLMRouter fallback 测试 ────────────────────────────

class TestLLMRouterFallback:
    """多供应商 fallback 行为"""

    def _make_router(self, n=3):
        providers = []
        for i in range(n):
            providers.append(LLMProvider(
                name=f"p{i}", api_url=f"http://h{i}/v1",
                api_key=f"sk-{i}", model=f"m{i}",
                priority=i * 10,
            ))
        return LLMRouter(providers)

    def test_sort_by_priority(self):
        """供应商按 priority 升序排序"""
        router = self._make_router()
        names = [p.name for p in router._providers]
        assert names == ["p0", "p1", "p2"]

    def test_get_active_providers_filters_disabled(self):
        """get_active_providers 过滤禁用的供应商"""
        router = self._make_router()
        router._providers[1].enabled = False
        active = router.get_active_providers()
        assert len(active) == 2
        assert all(p.enabled for p in active)

    def test_get_active_providers_filters_unhealthy(self):
        """get_active_providers 过滤不健康的供应商"""
        router = self._make_router()
        router._providers[0]._healthy = False
        active = router.get_active_providers()
        assert len(active) == 2
        assert all(p.healthy for p in active)

    def test_get_best_provider_returns_highest_priority(self):
        """get_best_provider 返回最高优先级（数字最小）"""
        router = self._make_router()
        best = router.get_best_provider()
        assert best.name == "p0"

    def test_get_best_provider_none_when_all_unhealthy(self):
        """所有供应商不健康时返回 None"""
        router = self._make_router()
        for p in router._providers:
            p._healthy = False
        assert router.get_best_provider() is None

    def test_chat_falls_back_on_failure(self):
        """第一个供应商失败时 fallback 到第二个"""
        router = self._make_router()
        # p0 失败，p1 成功
        call_count = [0]
        def mock_call(provider, messages, *args, **kwargs):
            call_count[0] += 1
            if provider.name == "p0":
                raise Exception("p0 down")
            return "response from p1"
        with patch("loom_core.llm_router._call_llm", side_effect=mock_call):
            content, provider_name = router.chat([{"role": "user", "content": "hi"}])
        assert provider_name == "p1"
        assert content == "response from p1"
        assert call_count[0] == 2  # 调用了 2 次

    def test_chat_raises_when_all_fail(self):
        """所有供应商都失败时抛 RuntimeError"""
        router = self._make_router()
        with patch("loom_core.llm_router._call_llm", side_effect=Exception("all down")), \
                pytest.raises(RuntimeError, match="所有 LLM 供应商调用失败"):
            router.chat([{"role": "user", "content": "hi"}])

    def test_chat_raises_when_no_providers(self):
        """无供应商时抛 RuntimeError"""
        router = LLMRouter(providers=[])
        with pytest.raises(RuntimeError, match="所有 LLM 供应商不可用"):
            router.chat([{"role": "user", "content": "hi"}])

    def test_chat_preferred_provider_used_first(self):
        """指定 preferred 供应商时优先使用"""
        router = self._make_router()
        seen = []
        def mock_call(provider, messages, *args, **kwargs):
            seen.append(provider.name)
            return "ok"
        with patch("loom_core.llm_router._call_llm", side_effect=mock_call):
            router.chat([{"role": "user", "content": "hi"}], preferred="p2")
        assert seen[0] == "p2"

    def test_chat_success_marks_provider_healthy(self):
        """调用成功后供应商标记为健康"""
        router = self._make_router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            router.chat([{"role": "user", "content": "hi"}])
        assert router._providers[0].healthy is True
        assert router._providers[0]._fail_count == 0


# ─── LLMRouter 健康检查 ────────────────────────────

class TestLLMRouterHealthCheck:
    """health_check_all 行为"""

    def test_health_check_all_returns_all_providers(self):
        """health_check_all 返回所有供应商状态"""
        providers = [
            LLMProvider(name="p0", api_url="http://h0", api_key="k0", model="m0"),
            LLMProvider(name="p1", api_url="http://h1", api_key="k1", model="m1", enabled=False),
        ]
        router = LLMRouter(providers)
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            result = router.health_check_all()
        assert "p0" in result
        assert "p1" in result
        assert result["p1"]["status"] == "disabled"

    def test_status_summary(self):
        """status 返回路由器摘要"""
        router = LLMRouter([
            LLMProvider(name="p0", api_url="http://h0", api_key="k0", model="m0"),
        ])
        s = router.status()
        assert s["total_providers"] == 1
        assert s["active"] == 1
        assert len(s["providers"]) == 1


# ─── from_env / from_dict ────────────────────────────

class TestLLMRouterConfig:
    """配置加载"""

    def test_from_dict_creates_providers(self):
        """from_dict 从字典创建路由器"""
        config = {
            "providers": [
                {"name": "a", "api_url": "http://a", "api_key": "k", "model": "m", "priority": 10},
                {"name": "b", "api_url": "http://b", "api_key": "k", "model": "m", "priority": 20},
            ]
        }
        router = LLMRouter.from_dict(config)
        assert len(router._providers) == 2
        assert router._providers[0].name == "a"

    def test_from_env_with_empty_env_returns_empty_router(self):
        """空 env 返回空路由器"""
        with patch("loom_core.llm_router._load_env", return_value={}):
            router = LLMRouter.from_env()
        assert len(router._providers) == 0

    def test_from_env_loads_provider(self):
        """from_env 加载单个供应商"""
        env = {
            "LLM_API_KEY": "sk-test",
            "LLM_API_URL": "http://localhost/v1",
            "LLM_MODEL": "test-model",
        }
        with patch("loom_core.llm_router._load_env", return_value=env):
            router = LLMRouter.from_env()
        assert len(router._providers) == 1
        assert router._providers[0].name == "cloudflare"

    def test_load_env_reads_file(self, tmp_path):
        """_load_env 读取 .env 文件"""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "KEY1=value1\n"
            'KEY2="value2"\n'
            "KEY3='value3'\n"
            "INVALID_LINE\n"
        )
        result = _load_env(str(env_file))
        assert result.get("KEY1") == "value1"
        assert result.get("KEY2") == "value2"
        assert result.get("KEY3") == "value3"
        assert "INVALID_LINE" not in result


# ─── 全局单例 ────────────────────────────

class TestRouterSingleton:
    """get_router / reset_router 单例行为"""

    def test_get_router_returns_same_instance(self):
        """get_router 返回同一实例"""
        reset_router()
        r1 = get_router()
        r2 = get_router()
        assert r1 is r2

    def test_reset_router_clears_instance(self):
        """reset_router 清除单例"""
        r1 = get_router()
        reset_router()
        r2 = get_router()
        assert r1 is not r2


# ─── _call_llm Cloudflare 格式适配 ────────────────────────────

class TestCallLLM:
    """_call_llm 底层调用"""

    def test_cloudflare_format(self):
        """Cloudflare Workers AI 格式自动适配"""
        provider = LLMProvider(
            name="cf", api_url="http://cf/ai/run/@cf/model",
            api_key="k", model="m", is_cloudflare=True,
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"result":{"choices":[{"message":{"content":"hello"}}]}}'
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            content = _call_llm(provider, [{"role": "user", "content": "hi"}], max_tokens=10)
        assert content == "hello"

    def test_standard_format(self):
        """标准 OpenAI 格式"""
        provider = LLMProvider(
            name="std", api_url="http://std/v1/chat",
            api_key="k", model="m",
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices":[{"message":{"content":"world"}}]}'
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            content = _call_llm(provider, [{"role": "user", "content": "hi"}])
        assert content == "world"

    def test_think_tag_filtered(self):
        """<think>...</think> 标签被过滤"""
        provider = LLMProvider(
            name="dahl", api_url="http://dahl/v1", api_key="k", model="m",
        )
        raw_content = "<think>reasoning here</think>actual answer"
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"choices":[{"message":{"content":"' + raw_content.encode() + b'"}}]}'
        )
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            content = _call_llm(provider, [{"role": "user", "content": "hi"}])
        assert content == "actual answer"

    def test_empty_content_raises(self):
        """空内容抛 ValueError"""
        provider = LLMProvider(
            name="empty", api_url="http://e/v1", api_key="k", model="m",
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices":[{"message":{"content":""}}]}'
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp), \
                pytest.raises(ValueError, match="返回空内容"):
            _call_llm(provider, [{"role": "user", "content": "hi"}])


# ─── chat/chat_async 成本记录与预算熔断 ────────────────────────────

class TestChatCostRecording:
    """成功路径写入 cost_log"""

    def test_chat_async_records_cost_row_aligned_with_sync(self, cost_tracker):
        """chat_async 成功后 cost_log 新增一行，token 口径与同步版一致"""
        router = _make_single_router()
        messages = [{"role": "user", "content": "写一篇关于 Python 的文章"}]
        with patch("loom_core.llm_router._call_llm_async",
                   new_callable=AsyncMock, return_value="同一份回复内容"):
            content, name = asyncio.run(router.chat_async(messages))
        assert name == "p0"
        assert content == "同一份回复内容"
        rows_after_async = _fetch_cost_rows(cost_tracker._db_path)
        assert len(rows_after_async) == 1

        with patch("loom_core.llm_router._call_llm", return_value="同一份回复内容"):
            router.chat(messages)
        rows = _fetch_cost_rows(cost_tracker._db_path)
        assert len(rows) == 2
        assert rows[0][0] == rows[1][0] == "p0"
        assert rows[0][1] == rows[1][1] == "m0"
        assert rows[0][2:] == rows[1][2:]  # 同字符数 → 同 token 估算

    def test_chat_records_cost_row(self, cost_tracker):
        """同步 chat 成功后 cost_log 新增一行（既有行为回归）"""
        router = _make_single_router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            router.chat([{"role": "user", "content": "hi"}])
        rows = _fetch_cost_rows(cost_tracker._db_path)
        assert len(rows) == 1
        assert rows[0][0] == "p0"
        assert cost_tracker.total_cost() > 0

    def test_record_failure_does_not_break_chat(self, cost_tracker):
        """记录成本异常不影响主流程"""
        router = _make_single_router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"), \
                patch("loom_core.cost_tracker.CostTracker.record_call",
                      side_effect=RuntimeError("db broken")):
            content, name = router.chat([{"role": "user", "content": "hi"}])
        assert name == "p0"


class TestBudgetEnforcement:
    """预算耗尽时拒绝调用且不发起底层请求"""

    def _exhaust_budget(self, tracker):
        tracker.set_budget(0.005)
        tracker.record("p0", 1000, 500, cost=0.01)

    def test_chat_raises_budget_exceeded_and_skips_call(self, cost_tracker):
        self._exhaust_budget(cost_tracker)
        router = _make_single_router()
        with patch("loom_core.llm_router._call_llm") as mock_call, \
                pytest.raises(BudgetExceededError):
            router.chat([{"role": "user", "content": "hi"}])
        mock_call.assert_not_called()

    def test_chat_async_raises_budget_exceeded_and_skips_call(self, cost_tracker):
        self._exhaust_budget(cost_tracker)
        router = _make_single_router()
        mock_async = AsyncMock(return_value="should not happen")
        with patch("loom_core.llm_router._call_llm_async", mock_async), \
                pytest.raises(BudgetExceededError):
            asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        mock_async.assert_not_called()

    def test_no_budget_passes_through(self, cost_tracker):
        """未设置预算时行为不变放行"""
        router = _make_single_router()
        with patch("loom_core.llm_router._call_llm", return_value="ok") as mock_call:
            content, name = router.chat([{"role": "user", "content": "hi"}])
        assert name == "p0"
        mock_call.assert_called_once()

    def test_zero_budget_behaves_as_unlimited(self, cost_tracker):
        cost_tracker.set_budget(0)
        router = _make_single_router()
        with patch("loom_core.llm_router._call_llm", return_value="ok"):
            _, name = router.chat([{"role": "user", "content": "hi"}])
        assert name == "p0"

    def test_budget_check_failure_does_not_break_chat(self, cost_tracker):
        """检查器故障（DB 损坏）不影响主流程"""
        router = _make_single_router()
        with patch("loom_core.cost_tracker.CostTracker.total_cost",
                   side_effect=RuntimeError("db broken")), \
                patch("loom_core.llm_router._call_llm", return_value="ok"):
            _, name = router.chat([{"role": "user", "content": "hi"}])
        assert name == "p0"


class TestUsageBasedCosting:
    """响应含 usage 字段时优先精确计费，否则回退字符估算"""

    def test_call_llm_fills_usage_out(self):
        provider = LLMProvider(name="std", api_url="http://std/v1",
                               api_key="k", model="m")
        usage: dict = {}
        resp = _mock_http_response({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 22},
        })
        with patch("urllib.request.urlopen", return_value=resp):
            content = _call_llm(provider, [{"role": "user", "content": "hi"}],
                                usage_out=usage)
        assert content == "hello"
        assert usage == {"prompt_tokens": 111, "completion_tokens": 22}

    def test_call_llm_usage_out_untouched_without_usage(self):
        provider = LLMProvider(name="std", api_url="http://std/v1",
                               api_key="k", model="m")
        usage: dict = {}
        resp = _mock_http_response({"choices": [{"message": {"content": "hello"}}]})
        with patch("urllib.request.urlopen", return_value=resp):
            _call_llm(provider, [{"role": "user", "content": "hi"}], usage_out=usage)
        assert usage == {}

    def test_chat_prefers_usage_tokens(self, cost_tracker):
        router = _make_single_router()
        resp = _mock_http_response({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 22},
        })
        with patch("urllib.request.urlopen", return_value=resp):
            router.chat([{"role": "user", "content": "hi"}])
        s = cost_tracker.stats()["p0"]
        assert s["calls"] == 1
        assert s["prompt_tokens"] == 111
        assert s["completion_tokens"] == 22

    def test_chat_falls_back_to_char_estimate_without_usage(self, cost_tracker):
        router = _make_single_router()
        resp = _mock_http_response({"choices": [{"message": {"content": "hello"}}]})
        with patch("urllib.request.urlopen", return_value=resp):
            router.chat([{"role": "user", "content": "hi"}])
        s = cost_tracker.stats()["p0"]
        assert s["prompt_tokens"] == estimate_tokens("hi")
        assert s["completion_tokens"] == estimate_tokens("hello")

    def test_chat_async_prefers_usage_tokens(self, cost_tracker):
        router = _make_single_router()

        async def _no_session():
            return None

        resp = _mock_http_response({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 22},
        })
        with patch("loom_core.llm_router._get_shared_session", side_effect=_no_session), \
                patch("urllib.request.urlopen", return_value=resp):
            asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
        s = cost_tracker.stats()["p0"]
        assert s["prompt_tokens"] == 111
        assert s["completion_tokens"] == 22
