"""fact_checker LLM 核查路径全覆盖（C-15，覆盖 72% → 目标 95%+）。

既有 `test_fact_checker.py` 钉了声明提取/字符串匹配/来源收集/报告渲染；
本文件补齐 LLM 相关路径：`_llm_available` 三分支、`_verify_claims` LLM
分支与异常回退、`_verify_with_llm` 全身（预算耗尽/JSON 解析失败/索引无效/
verdict 归一化）、`_collect_sources` 深度护栏。

mock 口径：`get_router` 在**函数体内** import，故 monkeypatch
`loom_core.llm_router.get_router` 打点；agent 用 `__new__` 跳过基类初始化。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from packs.docgen.agents.fact_checker import FactCheckerAgent


@pytest.fixture
def agent():
    a = FactCheckerAgent.__new__(FactCheckerAgent)
    a._max_claims = 30
    a._llm_timeout = 30
    a.log_info = MagicMock()
    a.log_warning = MagicMock()
    a.log_error = MagicMock()
    a.report = MagicMock()
    return a


def _patch_router(monkeypatch, router):
    monkeypatch.setattr("loom_core.llm_router.get_router", lambda: router)


# ─── _llm_available 三分支 ───────────────────────────────────

class TestLLMAvailable:

    def test_true_when_router_has_active_providers(self, agent, monkeypatch):
        router = MagicMock()
        router.get_active_providers.return_value = ["openai"]
        _patch_router(monkeypatch, router)
        assert agent._llm_available() is True

    def test_false_when_no_active_providers(self, agent, monkeypatch):
        router = MagicMock()
        router.get_active_providers.return_value = []
        _patch_router(monkeypatch, router)
        assert agent._llm_available() is False

    def test_false_when_router_is_none(self, agent, monkeypatch):
        _patch_router(monkeypatch, None)
        assert agent._llm_available() is False

    def test_false_when_import_or_call_raises(self, agent, monkeypatch):
        def boom():
            raise RuntimeError("router init failed")
        monkeypatch.setattr("loom_core.llm_router.get_router", boom)
        assert agent._llm_available() is False


# ─── _verify_claims：LLM 分支与回退 ──────────────────────────

class TestVerifyClaimsDispatch:

    def test_uses_llm_when_available(self, agent, monkeypatch):
        verdicts = [{"claim": "增长57%", "verdict": "supported",
                     "note": "来源一致", "method": "llm"}]
        agent._llm_available = lambda: True
        agent._verify_with_llm = MagicMock(return_value=verdicts)
        sources = ["来源提到57%"]
        out = agent._verify_claims(["增长57%"], sources)
        agent._verify_with_llm.assert_called_once_with(["增长57%"], sources)
        assert out[0]["method"] == "llm"

    def test_skips_llm_when_no_sources(self, agent, monkeypatch):
        """无来源时即便 LLM 可用也走字符串匹配（LLM 没有可核查的依据）。"""
        agent._llm_available = lambda: True
        agent._verify_with_llm = MagicMock(
            side_effect=AssertionError("不应调用 LLM"))
        out = agent._verify_claims(["增长57%"], [])
        assert out[0]["method"] == "string-match"
        agent._verify_with_llm.assert_not_called()

    def test_falls_back_to_matching_on_llm_failure(self, agent, monkeypatch):
        """LLM 可用但核查抛异常 → 回退字符串匹配（不丢核查能力）。"""
        agent._llm_available = lambda: True
        agent._verify_with_llm = MagicMock(side_effect=RuntimeError("llm 炸了"))
        claim = "改版后成本骤降57%。"
        sources = ["该项目成本降低57%，效率提升。"]
        out = agent._verify_claims([claim], sources)
        assert out[0]["verdict"] == "supported"
        assert out[0]["method"] == "string-match"
        agent.log_warning.assert_called()  # 回退有告警，不是静默


# ─── _verify_with_llm 全身 ───────────────────────────────────

class _ChatRouter:
    """可编程 chat 假路由。"""

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.content, "mock"

    def get_active_providers(self):
        return ["mock"]


VALID_JSON = ('{"verdicts": [{"index": 1, "verdict": "supported", "note": "一致"},'
              ' {"index": 2, "verdict": "refuted", "note": "来源矛盾"}]}')


class TestVerifyWithLLM:

    def test_maps_verdicts_by_index(self, agent, monkeypatch):
        router = _ChatRouter(content=VALID_JSON)
        _patch_router(monkeypatch, router)
        claims = ["声明一", "声明二"]
        out = agent._verify_with_llm(claims, ["来源一", "来源二"])
        assert len(out) == 2
        assert out[0]["verdict"] == "supported"
        assert out[0]["method"] == "llm"
        assert out[1]["verdict"] == "refuted"
        assert out[1]["note"] == "来源矛盾"

    def test_budget_exhausted_raises(self, agent, monkeypatch):
        """剩余预算 ≤1 秒：抛错走回退，不带超时请求拖垮节点。"""
        router = _ChatRouter(content=VALID_JSON)
        _patch_router(monkeypatch, router)
        agent._llm_timeout = 0  # deadline = now + 0 → remaining ≤ 1
        with pytest.raises(ValueError, match="预算"):
            agent._verify_with_llm(["c"], ["s"])

    def test_non_json_response_raises(self, agent, monkeypatch):
        router = _ChatRouter(content="抱歉，我无法输出 JSON 格式。")
        _patch_router(monkeypatch, router)
        with pytest.raises(ValueError, match="JSON"):
            agent._verify_with_llm(["c"], ["s"])

    def test_invalid_indexes_raise_not_silently_downgrade(self, agent, monkeypatch):
        """LLM 用 0 基编号/整体偏移：索引全体无效时抛错走回退，不静默降级。"""
        router = _ChatRouter(content=(
            '{"verdicts": [{"index": 0, "verdict": "supported", "note": "0基"},'
            ' {"index": 5, "verdict": "refuted", "note": "越界"}]}'))
        _patch_router(monkeypatch, router)
        with pytest.raises(ValueError, match="索引"):
            agent._verify_with_llm(["声明一", "声明二"], ["来源"])

    def test_unknown_verdict_normalized(self, agent, monkeypatch):
        """verdict 值不在三值集内：归一化为 unverifiable，不透传脏值。"""
        router = _ChatRouter(content=(
            '{"verdicts": [{"index": 1, "verdict": "大概是真的", "note": "脏值"}]}'))
        _patch_router(monkeypatch, router)
        out = agent._verify_with_llm(["声明一"], ["来源"])
        assert out[0]["verdict"] == "unverifiable"

    def test_missing_verdict_defaults_to_unverifiable(self, agent, monkeypatch):
        """LLM 漏了某条声明的判定：缺省 unverifiable，不猜。"""
        router = _ChatRouter(content='{"verdicts": [{"index": 1, "verdict": "supported"}]}')
        _patch_router(monkeypatch, router)
        out = agent._verify_with_llm(["声明一", "声明二"], ["来源"])
        assert out[0]["verdict"] == "supported"
        assert out[1]["verdict"] == "unverifiable"
        assert out[1]["note"] == ""


# ─── _collect_sources 深度护栏 ───────────────────────────────

class TestCollectSourcesDepthGuard:

    def test_depth_beyond_four_not_collected(self, agent):
        """嵌套超过 4 层的来源不再收集——防深嵌套结构拖垮核查。"""
        long = "长" * 60
        deep = {"text": long}
        for _ in range(8):  # 一路套 8 层
            deep = {"inner": deep}
        texts = agent._collect_sources(deep)
        assert texts == []  # 深度护栏截断

    def test_depth_within_four_collected(self, agent):
        long = "长" * 60
        nested = {"a": {"b": {"c": {"d": {"text": long}}}}}  # 恰好 4 层 dict
        texts = agent._collect_sources(nested)
        assert texts == [long]


# ─── 真 _verify_with_llm 抛错的穿透回退 ──────────────────────

class TestRealPipelineFallback:

    def test_real_verify_llm_failure_falls_back_via_verify_claims(
        self, agent, monkeypatch,
    ):
        """真 `_verify_with_llm` 抛错（非 mock 该方法本身）：经 `_verify_claims`
        的 except 落回字符串匹配——穿透判据，钉住回退接线的完整性。"""
        agent._llm_available = lambda: True
        router = _ChatRouter(error=RuntimeError("chat 炸了"))
        _patch_router(monkeypatch, router)
        claim = "改版后成本骤降57%。"
        sources = ["该项目成本降低57%，效率提升。"]
        out = agent._verify_claims([claim], sources)
        assert out[0]["verdict"] == "supported"
        assert out[0]["method"] == "string-match"
        agent.log_warning.assert_called()  # 回退有告警，不是静默


# ─── 真构造（__init__ 91-95） ─────────────────────────────────

class TestRealInit:
    """真构造走 BaseAgent.__init__（其余测试用 __new__ 绕过）。

    meta 必须是 AgentMeta 数据类——基类读 meta.cache_ttl/input_topics/version
    属性；cache_dir/log_dir 落 tmp_path 避免向仓库写缓存与日志。
    """

    def test_init_reads_budget_from_config(self, tmp_path):
        """__init__ 91-95 行：max_claims/llm_timeout 从 config 读入，
        是全文件此前唯一未覆盖的路径。"""
        from loom_core.registry import AgentMeta

        meta = AgentMeta(name="fact_checker", version="2.1", cache_ttl=60,
                         input_topics=["doc.factcheck"])
        bus = MagicMock()
        agent = FactCheckerAgent(
            "fc-cov", meta,
            {"max_claims": 5, "llm_timeout": 15,
             "cache_dir": str(tmp_path / "cache"),
             "log_dir": str(tmp_path / "logs"), "quiet": True},
            bus, MagicMock(),
        )
        assert agent._max_claims == 5
        assert agent._llm_timeout == 15
        assert agent.status.name == "LOADED"
        # 基类按 meta.input_topics 自动订阅
        bus.subscribe.assert_called_once_with("doc.factcheck", agent._wrapped_handle)

    def test_init_defaults_when_config_empty(self, tmp_path):
        from loom_core.registry import AgentMeta

        meta = AgentMeta(name="fact_checker", version="2.1")
        agent = FactCheckerAgent(
            "fc-cov", meta,
            {"cache_dir": str(tmp_path / "cache"),
             "log_dir": str(tmp_path / "logs"), "quiet": True},
            None, None,
        )
        assert agent._max_claims == 30   # MAX_CLAIMS 缺省
        assert agent._llm_timeout == 30  # LLM_TIMEOUT 缺省
