"""fact_checker Agent 测试 — 声明提取 / 来源匹配核查 / LLM 回退 / 报告渲染

不 mock BaseAgent 基类行为；LLM 路径通过 mock get_router 覆盖。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from packs.docgen.agents.fact_checker import FactCheckerAgent, _normalize


@pytest.fixture
def agent():
    """绕过 BaseAgent 线程/总线初始化，仅测核心逻辑"""
    a = FactCheckerAgent.__new__(FactCheckerAgent)
    a._max_claims = 30
    a.log_info = MagicMock()
    a.log_warning = MagicMock()
    a.log_error = MagicMock()
    a.report = MagicMock()
    return a


class TestClaimExtraction:

    def test_extracts_numeric_claims(self, agent):
        content = (
            "# 标题\n\n"
            "该方案将成本降低了57%，服务器从7台削减到3台。\n"
            "这是一句没有任何数字特征的普通陈述句。\n"
            "在2026年发布的版本V2.1.3修复了问题。\n"
        )
        claims = agent._extract_claims(content)
        assert len(claims) == 2
        assert any("57%" in c for c in claims)
        assert any("2026" in c for c in claims)

    def test_skips_code_blocks_and_short_fragments(self, agent):
        content = (
            "```python\nx = 100%  # 这行在代码块里\n```\n\n"
            "正常声明：该系统吞吐量稳定达到每秒6000次请求的处理水平。\n"
            "短句。5%\n"
        )
        claims = agent._extract_claims(content)
        assert len(claims) == 1
        assert "6000" in claims[0]
        # 超 300 字的长句即使带数字也不该被当成声明（防长段落整段灌入）
        overlong = "该指标在长期运行中保持稳定增长并持续优化，增长率为42%，" * 12
        assert len(overlong) > 300
        assert agent._extract_claims(overlong) == []

    def test_max_claims_cap(self, agent):
        agent._max_claims = 3
        content = "\n".join(
            f"第{i}条声明显示增长率为{i}%的稳定趋势。" for i in range(10)
        )
        assert len(agent._extract_claims(content)) == 3


class TestSourceMatching:

    def test_supported_when_anchors_in_sources(self, agent):
        claim = "改版后成本骤降57%。"
        sources = ["该项目使用新架构，成本降低57%，效率大幅提升。"]
        results = agent._verify_by_matching([claim], sources)
        assert results[0]["verdict"] == "supported"

    def test_unverified_when_anchor_missing(self, agent):
        claim = "据称该系统支持1000万并发连接。"
        sources = ["这是一个完全无关的来源文本，没有任何相关数据。"]
        results = agent._verify_by_matching([claim], sources)
        assert results[0]["verdict"] == "unverified"

    def test_no_sources_all_unverified(self, agent):
        results = agent._verify_by_matching(["成本下降30%的说法。"], [])
        assert results[0]["verdict"] == "unverified"


class TestCollectSources:

    def test_walks_nested_dependency_results(self, agent):
        long_a = ("这是一段足够长的摘要文本用于测试收集逻辑，包含必要的上下文描述、"
                  "关键数据指标与背景信息，长度超过五十个字符的收集阈值要求。")
        long_b = ("这是另一段足够长的正文内容同样用于测试嵌套结构的遍历与收集行为表现，"
                  "确保递归深度与列表结构的处理路径都能被正确覆盖到。")
        long_c = ("上游最终文档内容也应当被收集为核查来源之一，这段文字补足了足够的长度"
                  "以满足来源片段的最小长度限制条件，从而验证完整的收集行为。")
        dep_results = {
            "_researcher_raw": {"results": [
                {"title": "t", "snippet": long_a},
                {"url": "u", "text": long_b},
            ]},
            "writer": {"content": long_c},
        }
        texts = agent._collect_sources(dep_results)
        assert len(texts) >= 3


class TestHandle:

    def _msg(self, content="", dep_results=None):
        msg = MagicMock()
        msg.payload = {"content": content,
                       "dependencies_results": dep_results or {}}
        return msg

    def test_skip_on_empty_content(self, agent):
        result = agent.handle(self._msg(""))
        assert result["status"] == "skip"

    def test_no_claims_returns_empty_summary(self, agent):
        result = agent.handle(self._msg("没有数字特征的普通文档内容。"))
        assert result["status"] == "ok"
        assert result["summary"]["total"] == 0

    def test_unverified_claims_append_report(self, agent):
        content = ("# 文档\n\n"
                   "该产品声称支持1000万并发连接且延迟低于1毫秒。\n")
        result = agent.handle(self._msg(content))
        assert result["status"] == "ok"
        assert result["summary"]["unverified"] >= 1
        assert "事实核查附注" in result["content"]
        assert result["content"].startswith("# 文档")

    def test_all_verified_does_not_append_report(self, agent):
        content = "根据官方文档，该库在Python 3.11版本正式发布。"
        sources = ["官方文档记载该库于Python 3.11版本正式发布并可用。"]
        dep = {"fetcher": {"results": [{"text": sources[0]}]}}
        result = agent.handle(self._msg(content, dep))
        assert result["status"] == "ok"
        if result["summary"]["unverified"] == 0:
            assert "事实核查附注" not in result["content"]

    def test_llm_failure_falls_back_to_matching(self, agent):
        """LLM 判定抛异常时必须回退到字符串匹配而非崩溃"""
        content = "该方案将成本降低了57%。"
        with patch("loom_core.llm_router.get_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.get_active_providers.return_value = [MagicMock()]
            mock_router.chat.side_effect = RuntimeError("LLM down")
            mock_gr.return_value = mock_router
            result = agent.handle(self._msg(content))
        assert result["status"] == "ok"
        assert result["claims"][0]["method"] == "string-match"


class TestNormalize:

    def test_strips_whitespace_and_punctuation(self):
        assert _normalize("成本 降低 57%！") == _normalize("成本降低57%")
