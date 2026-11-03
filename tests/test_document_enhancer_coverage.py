"""DocumentEnhancer 全路径覆盖补强（C-14，覆盖 69% → 目标 95%+）。

既有 `test_document_enhancer_fixes.py` 钉了原子写/清洗回退/LLM 失败三面；
本文件补齐其余路径：__init__ 装配、enhance 端到端、搜索路径、超长分块、
fence/H4 丢失回退、虚假标题全局清理、_guard_cleaned、_fix_ascii、get_stats。

mock 口径：`DocumentEnhancer.__new__` 跳过 __init__（沿用既有测试模式），
LLM 路由与搜索管理器直接 setattr 假对象——不触网、不触真 LLM。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import loom_doc.document_enhancer as de_module
from loom_doc.document_enhancer import ENHANCE_PROMPT, DocumentEnhancer

ENHANCE_PROMPT_SAMPLE = ENHANCE_PROMPT[: ENHANCE_PROMPT.index("## 严格禁止")]


def _make_enhancer():
    enh = DocumentEnhancer.__new__(DocumentEnhancer)
    enh._stats = {"sections": 0, "enhanced": 0, "searched": 0,
                  "ascii_fixed": 0, "fake_headings_removed": 0}
    return enh


class _Router:
    """可编程假路由：按调用序返回输出，或抛异常。"""

    def __init__(self, outputs, error=None):
        self.outputs = list(outputs)
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages[0]["content"])
        if self.error:
            raise self.error
        return self.outputs.pop(0), "mock"


class _Search:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    def is_available(self):
        return True

    def search(self, query, max_results=5):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results[:max_results]


class _NoSearch:
    def is_available(self):
        return False


SEARCH_HIT = {"title": "参考", "url": "https://example.com/a",
              "snippet": "一段补充资料" * 5}


# ─── __init__ 装配 ──────────────────────────────────────────

class TestInit:

    def test_init_wires_router_and_search_and_zeroes_stats(self, monkeypatch):
        router = object()
        sent = object()
        monkeypatch.setattr(de_module, "get_router", lambda: router)
        monkeypatch.setattr(de_module.SearchEngineManager, "from_env",
                            classmethod(lambda cls, *a, **k: sent))
        enh = DocumentEnhancer()
        assert enh._llm_router is router
        assert enh._search_mgr is sent
        assert enh.get_stats() == {"sections": 0, "enhanced": 0, "searched": 0,
                                   "ascii_fixed": 0, "fake_headings_removed": 0}


# ─── enhance() 端到端 ────────────────────────────────────────

class TestEnhanceEndToEnd:

    def test_happy_path_writes_output_and_stats(self, tmp_path, monkeypatch):
        enh = _make_enhancer()
        enh._llm_router = _Router(["增强后的第一节正文" * 20])
        enh._search_mgr = _NoSearch()
        inp = tmp_path / "doc.md"
        inp.write_text("# 文档\n\n## 第一节\n\n" + "原始内容" * 20 + "\n\n## 第二节\n\n短",
                       encoding="utf-8")
        result = enh.enhance(str(inp), output_dir=str(tmp_path / "out"),
                             with_search=False)
        assert result["status"] == "success"
        out = Path(result["output_path"])
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "# 文档" in text and "## 第一节" in text
        stats = result["stats"]
        assert stats["sections"] == 3  # 文档标题 + 两节
        assert stats["enhanced"] == 3
        # 短节/空节跳过：第二节"短"不足 50 字符，LLM 未被再次调用
        assert len(enh._llm_router.calls) == 1
        assert "第二节" in text  # 短节原样保留

    def test_search_results_feed_prompt_and_stats(self, tmp_path, monkeypatch):
        enh = _make_enhancer()
        router = _Router(["增强后正文" * 20])
        enh._llm_router = router
        enh._search_mgr = _Search([SEARCH_HIT])
        inp = tmp_path / "doc.md"
        inp.write_text("# 文档\n\n## 第一节\n\n" + "原始内容" * 20, encoding="utf-8")
        result = enh.enhance(str(inp), output_dir=str(tmp_path / "out"),
                             with_search=True, max_search_results=3)
        assert result["stats"]["searched"] == 1
        # 搜索结果进入提示词
        assert "https://example.com/a" in router.calls[0]
        assert "原始内容" in router.calls[0]

    def test_search_failure_degrades_to_no_search(self, tmp_path):
        enh = _make_enhancer()
        router = _Router(["增强后正文" * 20])
        enh._llm_router = router
        enh._search_mgr = _Search(error=RuntimeError("搜索服务不可用"))
        inp = tmp_path / "doc.md"
        inp.write_text("# 文档\n\n## 第一节\n\n" + "原始内容" * 20, encoding="utf-8")
        result = enh.enhance(str(inp), output_dir=str(tmp_path / "out"),
                             with_search=True)
        assert result["status"] == "success"
        assert result["stats"]["searched"] == 0  # 搜索失败不计入
        assert "增强后正文" in Path(result["output_path"]).read_text(encoding="utf-8")

    def test_fake_heading_from_llm_demoted_globally(self, tmp_path):
        """LLM 生成了原文没有的 ## 标题 → 全局清理降级为加粗文本。"""
        enh = _make_enhancer()
        enh._llm_router = _Router(["增强正文\n\n## 凭空出现的小节\n\n内容"])
        enh._search_mgr = _NoSearch()
        inp = tmp_path / "doc.md"
        inp.write_text("# 文档\n\n## 唯一节\n\n" + "正文" * 30, encoding="utf-8")
        result = enh.enhance(str(inp), output_dir=str(tmp_path / "out"),
                             with_search=False)
        text = Path(result["output_path"]).read_text(encoding="utf-8")
        assert "## 凭空出现的小节" not in text
        assert "**凭空出现的小节**" in text
        assert result["stats"]["fake_headings_removed"] == 1

    def test_enhance_failure_bubbles_up_and_cleans_tmp(self, tmp_path, monkeypatch):
        """文档读取失败：异常上抛，不留临时文件。"""
        enh = _make_enhancer()
        enh._llm_router = _Router([])
        enh._search_mgr = _NoSearch()
        monkeypatch.setattr(Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
        with pytest.raises(IOError):
            enh.enhance(str(tmp_path / "no.md"), output_dir=str(tmp_path / "out"))
        leftovers = list(tmp_path.glob(".doc_enhanced_*"))
        assert not leftovers, "临时文件必须被清理"


# ─── _parse_sections 边界 ────────────────────────────────────

class TestParseSections:

    def test_no_h1_title_still_splits_h2(self):
        enh = _make_enhancer()
        sections = enh._parse_sections("## A\n\n内容A\n\n## B\n\n内容B")
        assert ("## A", "内容A") in sections
        assert ("## B", "内容B") in sections
        assert all(not t.startswith("# ") or t.startswith("## ") for t, _ in sections)


# ─── 超长分块 ────────────────────────────────────────────────

class TestEnhanceLongSection:

    def _long_body_with_subs(self):
        sub_a = "### 子节A\n\n" + "甲" * 1500
        sub_b = "### 子节B\n\n" + "乙" * 1500
        return sub_a + "\n\n" + sub_b

    def test_chunked_by_h3_and_reassembled(self):
        enh = _make_enhancer()
        router = _Router(["甲增强" * 40, "乙增强" * 40])
        enh._llm_router = router
        enh._search_mgr = _NoSearch()
        body = self._long_body_with_subs()
        assert len(body) > enh.MAX_CHUNK_SIZE
        out = enh._enhance_section("## 长节", body, with_search=False, max_results=3)
        assert "### 子节A" in out and "### 子节B" in out
        assert "甲增强" in out and "乙增强" in out
        assert len(router.calls) == 2  # 逐子节各调一次

    def test_overlong_sub_chunk_truncated_before_llm(self):
        """单个子节超限：截断送 LLM（防止超上下文），截断标记保留。"""
        enh = _make_enhancer()
        router = _Router([" whatever " * 10])
        enh._llm_router = router
        enh._search_mgr = _NoSearch()
        body = "### 巨型子节\n\n" + "字" * (enh.MAX_CHUNK_SIZE + 500)
        enh._enhance_section("## 长节", body, with_search=False, max_results=3)
        sent = router.calls[0]
        # sent 是完整提示词（模板 + 截断后的内容）：断言截断标记在、且比未截断短
        assert "[内容过长，已截断]" in sent
        assert len(sent) < len("### 巨型子节\n\n") + len(body) + 500

    def test_llm_failure_on_sub_chunk_keeps_original_chunk(self):
        """分块路径下单次 LLM 失败：该子节保留原文，不丢内容。"""
        enh = _make_enhancer()
        router = _Router([], error=RuntimeError("llm down"))
        enh._llm_router = router
        enh._search_mgr = _NoSearch()
        body = self._long_body_with_subs()
        out = enh._enhance_section("## 长节", body, with_search=False, max_results=3)
        assert "甲" * 100 in out and "乙" * 100 in out  # 原文原样

    def test_no_h3_subtitles_truncates_whole_body(self):
        """超长但无 ### 子标题：整体截断送 LLM；失败回退完整原文而非截断版。"""
        enh = _make_enhancer()
        router = _Router([], error=RuntimeError("llm down"))
        enh._llm_router = router
        enh._search_mgr = _NoSearch()
        body = "长" * (enh.MAX_CHUNK_SIZE + 200)
        out = enh._enhance_section("## 长节", body, with_search=False, max_results=3)
        assert out == body  # 失败回退**完整**原文，不是截断版
        # 成功路径：截断版送 LLM，正常返回
        router2 = _Router(["增强后的长文"])
        enh._llm_router = router2
        out2 = enh._enhance_section("## 长节", body, with_search=False, max_results=3)
        assert "增强后的长文" in out2
        # sent 是完整提示词：断言其中的内容段带截断标记（模板里没有"已截断"字样，
        # 标记出现即证明送 LLM 的是截断版）
        assert "[内容过长，已截断]" in router2.calls[0]

    def test_empty_and_short_sections_skip_llm(self):
        enh = _make_enhancer()
        router = _Router([])
        enh._llm_router = router
        enh._search_mgr = _NoSearch()
        assert enh._enhance_section("## 空节", "   \n", False, 3) == "   \n"
        assert enh._enhance_section("## 短节", "很短", False, 3) == "很短"
        assert router.calls == []


# ─── fence / H4 丢失回退 ─────────────────────────────────────

class TestRestoreGuards:

    def test_code_fence_loss_falls_back_to_original(self):
        enh = _make_enhancer()
        original = "前文\n\n```python\nprint(1)\n```\n\n后文"
        enhanced = "前文（代码没了）"
        assert enh._restore_code_fences(original, enhanced) == original

    def test_code_fence_count_match_keeps_enhanced(self):
        enh = _make_enhancer()
        original = "前文\n\n```python\nprint(1)\n```\n\n后文"
        enhanced = "前文增强\n\n```python\nprint(2)\n```\n\n后文增强"
        assert enh._restore_code_fences(original, enhanced) == enhanced

    def test_h4_loss_falls_back_to_original(self):
        enh = _make_enhancer()
        original = "#### 细节一\n\n内容一\n\n#### 细节二\n\n内容二"
        enhanced = "#### 细节一\n\n内容一（细节二被吞了）"
        assert enh._restore_missing_h4(original, enhanced) == original

    def test_h4_intact_keeps_enhanced(self):
        enh = _make_enhancer()
        original = "#### 细节一\n\n内容一\n\n#### 细节二\n\n内容二"
        enhanced = "#### 细节一\n\n内容一增强\n\n#### 细节二\n\n内容二增强"
        assert enh._restore_missing_h4(original, enhanced) == enhanced


# ─── _call_llm_enhance 与 _guard_cleaned ─────────────────────

class TestCallAndGuard:

    def test_llm_exception_returns_content_unchanged(self):
        enh = _make_enhancer()
        enh._llm_router = _Router([], error=RuntimeError("boom"))
        enh._search_mgr = _NoSearch()
        content = "原始内容"
        assert enh._call_llm_enhance(content, "") is content

    def test_guard_empty_cleaned_returns_llm_output(self):
        enh = _make_enhancer()
        llm_out = "有内容的输出"
        assert enh._guard_cleaned(llm_out, "   ") is llm_out

    def test_guard_over_shrink_returns_llm_output(self):
        """清洗移除超过一半：判定为误删，放弃清洗。"""
        enh = _make_enhancer()
        llm_out = "保留行\n" + "待删行\n" * 10
        cleaned = "保留行"
        assert enh._guard_cleaned(llm_out, cleaned) is llm_out

    def test_guard_mild_clean_passes(self):
        enh = _make_enhancer()
        llm_out = "保留行\n杂行"
        cleaned = "保留行"
        assert enh._guard_cleaned(llm_out, cleaned) == cleaned


# ─── _clean_fake_headings 与 _clean_llm_output 边界 ──────────

class TestCleanBoundaries:

    def test_clean_llm_output_keeps_h3_and_h4(self):
        enh = _make_enhancer()
        raw = "## 该删\n### 该留\n#### 也该留\n正文"
        assert enh._clean_llm_output(raw) == "### 该留\n#### 也该留\n正文"

    def test_clean_llm_output_keeps_fence_content_verbatim(self):
        enh = _make_enhancer()
        raw = "## 该删\n```text\n## fence 里不删\n```\n正文"
        assert enh._clean_llm_output(raw) == "```text\n## fence 里不删\n```\n正文"

    def test_clean_llm_output_strips_meta_descriptions(self):
        enh = _make_enhancer()
        raw = "增强后的 Markdown 内容\n\n实际正文"
        assert enh._clean_llm_output(raw).strip() == "实际正文"

    def test_clean_fake_headings_converts_to_bold(self):
        enh = _make_enhancer()
        content = "## 原始节\n\n**凭空节**前的正文\n\n## 凭空节\n\n内容"
        out, removed = enh._clean_fake_headings(content, {"## 原始节"})
        assert removed == 1
        assert "## 凭空节" not in out
        assert "**凭空节**" in out
        assert "## 原始节" in out


# ─── _fix_ascii 与 get_stats ─────────────────────────────────

class TestFixAsciiAndStats:

    def test_fix_ascii_delegates_and_counts(self, monkeypatch):
        fake = types.ModuleType("scripts.convert_ascii")
        fake.convert_ascii_in_text = lambda text: (text + "\n[fixed]", 2)
        monkeypatch.setitem(sys.modules, "scripts.convert_ascii", fake)
        monkeypatch.setitem(sys.modules, "scripts", types.ModuleType("scripts"))
        enh = _make_enhancer()
        out = enh._fix_ascii("图")
        assert "[fixed]" in out
        assert enh._stats["ascii_fixed"] == 2

    def test_fix_ascii_import_error_is_non_fatal(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "scripts.convert_ascii", None)  # None ⇒ ImportError
        enh = _make_enhancer()
        assert enh._fix_ascii("原图") == "原图"  # 原样返回，不炸
        assert enh._stats["ascii_fixed"] == 0

    def test_fix_ascii_generic_error_is_non_fatal(self, monkeypatch):
        fake = types.ModuleType("scripts.convert_ascii")

        def boom(text):
            raise RuntimeError("转换器炸了")

        fake.convert_ascii_in_text = boom
        monkeypatch.setitem(sys.modules, "scripts.convert_ascii", fake)
        monkeypatch.setitem(sys.modules, "scripts", types.ModuleType("scripts"))
        enh = _make_enhancer()
        assert enh._fix_ascii("原图") == "原图"  # 非致命：原样返回
