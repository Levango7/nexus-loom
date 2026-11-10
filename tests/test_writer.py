"""WriterAgent 单元测试 — 补齐覆盖率薄弱区（原 32%）。

覆盖范围：
- TemplateManager 渲染 + markdown 转义
- LLM 调用链：_llm_chat（router/单 LLM/CF 格式）、_llm_chat_stream（SSE/重试/回退）、
  _llm_chat_async / _llm_chat_stream_async（aiohttp 与降级路径）
- 章节并行润色 _polish_sections_parallel、段落润色 _polish_with_llm（缓存/质量门控/过渡句）
- handle() 全部分支（占位文档 / pending / 搜索摘要 / 文章模式 / spec 增强 / 运行时配置）
- 文章 → 骨架 → TF-IDF 填充：_build_from_articles / _plan_skeleton / _fill_section / _semantic_rank
- 搜索摘要模式：_to_chunks / _extract_keywords / _classify_chunks / _deduplicate_chunks / _generate_document
- Mermaid 修复 / 参考资料修复 / 截断块删除 / _assemble_sections / _restructure_document
- prompt 模板加载、质量闭环增强、handle_streaming 流式回调
"""
import asyncio
import io
import json
import sys
import time
import types
from unittest.mock import MagicMock, patch

from loom_core.base_agent import AgentMeta, Message
from loom_core.streaming import StreamCallback
from loom_doc import degradation
from packs.docgen.agents.writer import ContentChunk, TemplateManager, WriterAgent, _md_escape


def _make_writer(tmp_path, **cfg) -> WriterAgent:
    config = {
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "quiet": True,
    }
    config.update(cfg)
    w = WriterAgent(
        name="writer",
        meta=AgentMeta(name="writer", version="2.0"),
        config=config,
        message_bus=None,
        registry=None,
    )
    # 环境可能带真实 LLM_API_KEY —— 默认强制关闭，各用例按需打开
    w._llm_api_key = ""
    return w


def _msg(payload: dict, topic: str = "writer.input") -> Message:
    return Message(topic=topic, payload=payload, from_agent="test")


# ─── TemplateManager / 转义 ────────────────────────────────

class TestTemplateManager:
    def test_render_default_template(self):
        mgr = TemplateManager()
        out = mgr.render("default", "我的标题",
                         [{"title": "S1", "content": "C1"}],
                         [{"title": "Ref", "url": "https://x.com"}])
        assert "# 我的标题" in out
        assert "## S1" in out and "C1" in out
        assert "## 参考资料" in out
        assert "[Ref](https://x.com)" in out.replace("\\", "")

    def test_render_unknown_template_falls_back_to_default(self):
        mgr = TemplateManager()
        out = mgr.render("nonexistent", "T", [], [])
        assert "# T" in out

    def test_render_empty_references(self):
        mgr = TemplateManager()
        out = mgr.render("default", "T", [{"title": "A", "content": "B"}], [])
        assert "## A" in out

    def test_md_escape(self):
        assert _md_escape("a[b](c)*d_e") == "a\\[b\\]\\(c\\)\\*d\\_e"


# ─── _llm_chat：router 优先 + 单 LLM 回退 ──────────────────

class TestLLMChat:
    def test_router_path_used_when_available(self, tmp_path):
        w = _make_writer(tmp_path)
        router = MagicMock()
        router.get_active_providers.return_value = ["p1"]
        router.chat.return_value = ("router-answer", "p1")
        with patch("loom_core.llm_router.get_router", return_value=router):
            assert w._llm_chat([{"role": "user", "content": "hi"}]) == "router-answer"
        router.chat.assert_called_once()

    def test_fallback_to_single_llm_openai_format(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_api_url = "https://api.example.com/v1/chat/completions"
        body = json.dumps({"choices": [{"message": {"content": "single"}}]}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("loom_core.llm_router.get_router", return_value=None), \
                patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            assert w._llm_chat([{"role": "user", "content": "hi"}]) == "single"
        req = mock_open.call_args[0][0]
        assert json.loads(req.data)["messages"][0]["content"] == "hi"

    def test_fallback_cloudflare_format(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_api_url = "https://acc.workers.dev/ai/run/@cf/model"
        body = json.dumps({"result": {"choices": [{"message": {"content": "cf"}}]}}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("loom_core.llm_router.get_router", side_effect=RuntimeError("no")), \
                patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            assert w._llm_chat([]) == "cf"
        req = mock_open.call_args[0][0]
        assert "input" in json.loads(req.data)  # CF 格式用 input.messages


# ─── _llm_chat_stream：SSE 流式 ────────────────────────────

class _FakeSSEResponse:
    """模拟 urlopen 返回的 SSE 流响应"""

    def __init__(self, lines, sock_error=False):
        self._lines = lines
        self._sock_error = sock_error
        self.closed = False

    @property
    def fp(self):
        if self._sock_error:
            raise AttributeError("no fp")
        return self

    @property
    def raw(self):
        return self

    @property
    def _sock(self):
        return self

    def settimeout(self, t):
        pass

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class TestLLMChatStream:
    def test_sse_chunks_yielded(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        lines = [
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b'\n',
            b'event: ping\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
            b'data: [DONE]\n',
        ]
        with patch("urllib.request.urlopen", return_value=_FakeSSEResponse(lines)):
            chunks = list(w._llm_chat_stream([]))
        assert chunks == ["Hel", "lo"]

    def test_bad_json_and_missing_delta_skipped(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        lines = [
            b'data: {not-json}\n',
            b'data: {"choices":[{"finish_reason":"stop"}]}\n',
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: [DONE]\n',
        ]
        with patch("urllib.request.urlopen", return_value=_FakeSSEResponse(lines)):
            assert list(w._llm_chat_stream([])) == ["ok"]

    def test_connect_failure_retries_then_fallback(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(return_value="fallback-answer")
        with patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
            chunks = list(w._llm_chat_stream([], max_retries=1))
        assert chunks == ["fallback-answer"]
        w._llm_chat.assert_called_once()

    def test_empty_stream_falls_back_to_non_stream(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(return_value="non-stream")
        with patch("urllib.request.urlopen", return_value=_FakeSSEResponse([b'data: [DONE]\n'])):
            assert list(w._llm_chat_stream([])) == ["non-stream"]

    def test_read_timeout_keeps_partial_result(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"

        class _TimeoutResp(_FakeSSEResponse):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"part"}}]}\n'
                raise TimeoutError("read timeout")

        resp = _TimeoutResp([])
        w._llm_chat = MagicMock(return_value="SHOULD-NOT-BE-USED")
        with patch("urllib.request.urlopen", return_value=resp):
            chunks = list(w._llm_chat_stream([]))
        assert chunks == ["part"]  # 保留已收到部分，不重复回退
        w._llm_chat.assert_not_called()
        assert resp.closed

    def test_cloudflare_stream_format(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_api_url = "https://x.workers.dev/ai/run/@cf/m"
        lines = [b'data: {"result":{"choices":[{"delta":{"content":"cf!"}}]}}\n',
                 b'data: [DONE]\n']
        with patch("urllib.request.urlopen", return_value=_FakeSSEResponse(lines)):
            assert list(w._llm_chat_stream([])) == ["cf!"]


# ─── 异步 LLM 调用 ─────────────────────────────────────────

def _fake_aiohttp_module(post_result=None, post_error=None, stream_lines=None):
    """构造可注入 sys.modules 的假 aiohttp 模块"""
    mod = types.ModuleType("aiohttp")

    class _Resp:
        def __init__(self):
            self._lines = stream_lines or []

        async def json(self):
            if post_error:
                raise post_error
            return post_result

        @property
        def content(self):
            lines = self._lines

            class _Iter:
                def __init__(self):
                    self._i = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._i >= len(lines):
                        raise StopAsyncIteration
                    line = lines[self._i]
                    self._i += 1
                    return line
            return _Iter()

    class _PostCM:
        async def __aenter__(self):
            if post_error:
                raise post_error
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, timeout=None, headers=None, connector=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return _PostCM()

        def get(self, url, params=None, headers=None):
            return _PostCM()

    mod.ClientSession = _Session
    mod.ClientTimeout = lambda total=None: ("timeout", total)
    mod.TCPConnector = lambda **kw: ("connector", kw)
    return mod


class TestLLMChatAsync:
    def test_no_key_returns_empty(self, tmp_path):
        w = _make_writer(tmp_path)
        assert asyncio.run(w._llm_chat_async([])) == ""

    def test_aiohttp_success(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        body = {"choices": [{"message": {"content": "async-ok"}}]}
        fake = _fake_aiohttp_module(post_result=body)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(w._llm_chat_async([])) == "async-ok"

    def test_aiohttp_error_falls_back_to_sync(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(return_value="sync-fallback")
        fake = _fake_aiohttp_module(post_error=OSError("boom"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(w._llm_chat_async([])) == "sync-fallback"

    def test_import_error_falls_back_to_executor(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(return_value="executor-fallback")
        with patch.dict(sys.modules, {"aiohttp": None}):
            assert asyncio.run(w._llm_chat_async([])) == "executor-fallback"


class TestLLMChatStreamAsync:
    def test_no_key_yields_nothing(self, tmp_path):
        w = _make_writer(tmp_path)

        async def _collect():
            return [c async for c in w._llm_chat_stream_async([])]
        assert asyncio.run(_collect()) == []

    def test_aiohttp_stream_chunks(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        lines = [
            b'data: {"choices":[{"delta":{"content":"A"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"B"}}]}\n',
            b'data: [DONE]\n',
        ]
        fake = _fake_aiohttp_module(stream_lines=lines)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            async def _collect():
                return [c async for c in w._llm_chat_stream_async([])]
            assert asyncio.run(_collect()) == ["A", "B"]

    def test_import_error_falls_back_to_sync_stream(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat_stream = MagicMock(return_value=iter(["x", "y"]))
        with patch.dict(sys.modules, {"aiohttp": None}):
            async def _collect():
                return [c async for c in w._llm_chat_stream_async([])]
            assert asyncio.run(_collect()) == ["x", "y"]

    def test_aiohttp_error_falls_back_to_sync_stream(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat_stream = MagicMock(return_value=iter(["z"]))
        fake = _fake_aiohttp_module(post_error=OSError("net down"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            async def _collect():
                return [c async for c in w._llm_chat_stream_async([])]
            assert asyncio.run(_collect()) == ["z"]


# ─── 润色 ──────────────────────────────────────────────────

class TestPolishSectionsParallel:
    def test_single_section_passthrough(self, tmp_path):
        w = _make_writer(tmp_path)
        secs = [{"title": "A", "content": "x" * 200}]
        assert w._polish_sections_parallel(secs) is secs

    def test_no_llm_key_skips(self, tmp_path):
        w = _make_writer(tmp_path)
        secs = [{"title": f"S{i}", "content": "x" * 200} for i in range(3)]
        assert w._polish_sections_parallel(secs) is secs

    def test_parallel_polish_with_llm(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(return_value="P" * 120)
        secs = [
            {"title": "S1", "content": "a" * 200},
            {"title": "S2", "content": "short"},  # 过短不润色
            {"title": "S3", "content": "c" * 200},
        ]
        out = w._polish_sections_parallel(secs, context="ctx", task_id="t1")
        assert len(out) == 3
        assert out[0].get("polished") is True
        assert out[0]["content"] == "P" * 120
        assert "polished" not in out[1]  # 短内容保持原样
        assert out[2].get("polished") is True

    def test_polish_exception_keeps_original(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(side_effect=RuntimeError("llm down"))
        secs = [{"title": f"S{i}", "content": "a" * 200} for i in range(2)]
        out = w._polish_sections_parallel(secs)
        assert all("polished" not in s for s in out)


class TestPolishWithLLM:
    def test_no_key_returns_content(self, tmp_path):
        w = _make_writer(tmp_path)
        assert w._polish_with_llm("some content", "q") == "some content"

    def test_cache_hit(self, tmp_path):
        import hashlib
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        content = "## A\n\n" + "x" * 150
        key = hashlib.sha256((content[:200] + "q").encode()).hexdigest()
        w._polish_cache.set(key, "CACHED")
        assert w._polish_with_llm(content, "q") == "CACHED"

    def test_quality_gate_skips_complete_doc(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock()
        content = (
            "## 第一章\n\n" + "字" * 200 + "\n\n"
            "## 第二章\n\n" + "字" * 200 + "\n\n"
            "## 第三章\n\n" + "字" * 200 + "\n\n"
            "[ref](https://example.com/a)\n"
        )
        assert w._polish_with_llm(content, "q") == content
        w._llm_chat.assert_not_called()

    def test_segments_polished_with_transitions(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(side_effect=lambda msgs, **kw: "POLISHED")
        content = (
            "## 简介\n\n" + "甲" * 120 + "\n\n"
            "## 核心概念\n\n" + "乙" * 120 + "\n\n"
            "## 实践\n\n" + "丙" * 120
        )
        out = w._polish_with_llm(content, "q")
        assert "POLISHED" in out
        assert "> " in out  # 规则过渡句已插入

    def test_llm_exception_keeps_segment(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._llm_chat = MagicMock(side_effect=RuntimeError("boom"))
        seg = "字" * 150
        content = f"## 简介\n\n{seg}"
        out = w._polish_with_llm(content, "q")
        assert seg in out


# ─── handle() 分支 ─────────────────────────────────────────

class TestHandle:
    def test_placeholder_doc_when_nothing_available(self, tmp_path):
        w = _make_writer(tmp_path)
        res = w.handle(_msg({"task_id": "t1", "title": "空任务"}))
        assert res["status"] == "ok"
        assert "未采集到可整合的搜索结果" in res["content"]
        assert "# 空任务" in res["content"]

    def test_spec_enhances_title_query_audience(self, tmp_path):
        w = _make_writer(tmp_path)
        res = w.handle(_msg({
            "task_id": "t2",
            "title": "Kafka",
            "spec": {"doc_type": "技术文档", "scope": ["架构", "部署"], "audience": "高级"},
        }))
        assert res["status"] == "ok"
        assert w._audience_level == "高级"
        assert "技术文档" in res["content"]  # 标题被 doc_type 前缀增强

    def test_queries_fallback(self, tmp_path):
        w = _make_writer(tmp_path)
        res = w.handle(_msg({"task_id": "t3", "queries": ["主题甲"], "results": []}))
        assert res["status"] == "ok"

    def test_input_file_query_fallback(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="LLM-DOC")
        inp = tmp_path / "in.md"
        inp.write_text("# 注释行\n实际主题内容行\n", encoding="utf-8")
        res = w.handle(_msg({"task_id": "t4", "input_file": str(inp)}))
        assert res["status"] == "ok"
        # 输入文件首行非注释内容被提取为 query 并传给重构
        assert w._restructure_document.call_args[0][2] == "实际主题内容行"

    def test_pending_results_consumed(self, tmp_path):
        w = _make_writer(tmp_path)
        results = [{"title": "PendT", "snippet": "PendS " * 20, "url": "https://p.com",
                    "source": "bing"}]
        w.pending_results["t5"] = (time.time(), results)
        res = w.handle(_msg({"task_id": "t5", "query": "PendT"}))
        assert res["status"] == "ok"
        # 治本批（2026-11-10）：keyless 拒绝拼贴，只交降级自述，原文不得进正文
        assert degradation.KEYLESS_REFUSAL in res["content"]
        assert "PendS" not in res["content"]
        assert res["stats"]["keyless_refused"] is True
        assert "t5" not in w.pending_results  # 消费后弹出

    def test_results_mode_generates_sections_and_stats(self, tmp_path):
        w = _make_writer(tmp_path)
        results = [
            {"title": "Kafka 教程指南", "snippet": "安装配置使用教程 " * 10,
             "url": "https://a.com", "source": "bing"},
            {"title": "Kafka 概念原理", "snippet": "架构设计机制 " * 10,
             "url": "https://b.com", "source": "sogou"},
            {"title": "Kafka 教程指南", "snippet": "重复标题应被去重",
             "url": "https://c.com", "source": "360"},
        ]
        res = w.handle(_msg({"task_id": "t6", "query": "Kafka", "results": results}))
        assert res["status"] == "ok"
        assert res["stats"]["total_results"] == 3
        assert res["stats"]["unique_chunks"] == 2  # 标题去重
        # 治本批（2026-11-10）：keyless 拒绝拼贴，参考资料清单不再代替交付物
        assert degradation.KEYLESS_REFUSAL in res["content"]
        assert "## 参考资料" not in res["content"]

    def test_results_mode_with_llm_restructure(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="RESTRUCTURED")
        results = [{"title": "T", "snippet": "S" * 60, "url": "https://a.com",
                    "source": "bing"}]
        res = w.handle(_msg({"task_id": "t7", "query": "q", "results": results}))
        assert res["content"] == "RESTRUCTURED"
        w._restructure_document.assert_called_once()

    def test_results_mode_with_llm_but_restructure_unavailable_refuses_collage(self, tmp_path):
        """窄边界（2026-11-10 第二批）：检索摘要路径同理——有 Key 但重构不可用
        时 _generate_document 的拼贴不得出厂。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._prompt_profile = "__no_such_template__"
        results = [
            {"title": "Kafka 教程指南", "snippet": "安装配置使用教程 " * 10,
             "url": "https://a.com", "source": "bing"},
            {"title": "Kafka 概念原理", "snippet": "架构设计机制 " * 10,
             "url": "https://b.com", "source": "sogou"},
        ]
        res = w.handle(_msg({"task_id": "t6", "query": "Kafka", "results": results}))
        assert res["status"] == "ok"
        assert degradation.RESTRUCTURE_UNAVAILABLE in res["content"]
        assert degradation.KEYLESS_REFUSAL not in res["content"]
        assert "安装配置使用教程" not in res["content"]
        assert "## 参考资料" not in res["content"]
        assert res["stats"]["restructure_refused"] is True

    def test_results_mode_stats_carry_source_urls(self, tmp_path):
        """检索摘要路径同约：stats 报告查询与用过的素材 URL 集合。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="RESTRUCTURED")
        results = [
            {"title": "A", "snippet": "x" * 60, "url": "https://a.com",
             "source": "bing"},
            {"title": "B", "snippet": "y" * 60, "url": "https://b.com",
             "source": "sogou"},
        ]
        res = w.handle(_msg({"task_id": "t11", "query": "Kafka",
                             "results": results}))
        assert res["stats"]["query"] == "Kafka"
        assert res["stats"]["source_urls"] == ["https://a.com", "https://b.com"]

    def test_results_mode_empty_query_with_key_refuses_collage(self, tmp_path):
        """第三批裁决：检索摘要路径同拒——空查询时重构没有目标，摘要拼贴
        不得出厂（keyless 同形状已拒绝）。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        results = [{"title": "A", "snippet": "安装配置使用教程 " * 10,
                    "url": "https://a.com", "source": "bing"}]
        res = w.handle(_msg({"task_id": "t12", "query": "", "results": results}))
        assert res["status"] == "ok"
        assert degradation.RESTRUCTURE_UNAVAILABLE in res["content"]
        assert "安装配置使用教程" not in res["content"]
        assert "## 参考资料" not in res["content"]
        assert res["stats"]["restructure_refused"] is True

    def test_articles_dispatch(self, tmp_path):
        w = _make_writer(tmp_path)
        w._build_from_articles = MagicMock(return_value={"status": "ok", "task_id": "t8"})
        res = w.handle(_msg({"task_id": "t8", "query": "q",
                             "articles": [{"title": "a", "url": "u", "text": "t"}]}))
        assert res["task_id"] == "t8"
        w._build_from_articles.assert_called_once()

    def test_runtime_config_refresh(self, tmp_path):
        w = _make_writer(tmp_path)
        w.handle(_msg({
            "task_id": "t9",
            "config": {"prompt_profile": "pro", "llm_api_url": "http://u",
                       "llm_api_key": "kk", "llm_model": "m1"},
        }))
        assert w._prompt_profile == "pro"
        assert w._llm_api_url == "http://u"
        assert w._llm_api_key == "kk"
        assert w._llm_model == "m1"

    def test_no_results_with_llm_generates_via_restructure(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="LLM-DOC")
        res = w.handle(_msg({"task_id": "t10", "query": "主题"}))
        assert res["content"] == "LLM-DOC"


class TestExpireStalePending:
    def test_stale_removed_fresh_kept(self, tmp_path):
        w = _make_writer(tmp_path)
        w._pending_expire_secs = 100
        now = time.time()
        w.pending_results["stale"] = (now - 999, [{"x": 1}])
        w.pending_results["fresh"] = (now, [{"y": 1}])
        w.pending_results["bad_entry"] = "not-a-tuple"
        w._expire_stale_pending()
        assert "stale" not in w.pending_results
        assert "fresh" in w.pending_results
        assert "bad_entry" in w.pending_results


# ─── 文章 → 骨架 → TF-IDF 填充 ─────────────────────────────

class TestBuildFromArticles:
    def _write_articles(self, tmp_path):
        a1 = tmp_path / "a1.txt"
        a1.write_text("标题: Kafka 架构\n\n" + "Kafka 是一个分布式消息系统，架构设计非常优秀。" * 20,
                      encoding="utf-8")
        a2 = tmp_path / "a2.txt"
        a2.write_text("标题: 部署实践\n\n" + "生产环境部署 Kafka 需要关注分区与副本配置。" * 20,
                      encoding="utf-8")
        return [
            {"title": "Kafka 架构", "url": "https://a1.com", "source": "bing",
             "local_path": str(a1), "relevance": 0.9},
            {"title": "部署实践", "url": "https://a2.com", "source": "bing",
             "local_path": str(a2), "relevance": 0.5},
            {"title": "重复 URL", "url": "https://a1.com", "source": "bing",
             "local_path": str(a1), "relevance": 0.1},
            {"title": "缺失文件", "url": "https://gone.com", "source": "bing",
             "local_path": str(tmp_path / "missing.txt"), "relevance": 0.05},
        ]

    def test_build_without_llm_refuses_raw_collage(self, tmp_path):
        """治本批（2026-11-10）：keyless 拿到素材也不得拼贴交付，只交拒绝自述。"""
        w = _make_writer(tmp_path)
        articles = self._write_articles(tmp_path)
        res = w._build_from_articles(articles, "Kafka 架构", "Kafka 文档", "default", "t1")
        assert res["status"] == "ok"
        content = res["content"]
        assert degradation.KEYLESS_REFUSAL in content
        # 素材正文一个字都不得进成品（FP-1b 的源头形状）
        assert "架构设计非常优秀" not in content
        assert "## 参考资料" not in content
        assert res["stats"]["articles_used"] == 2  # 重复 URL + 缺失文件被剔除
        assert res["stats"]["keyless_refused"] is True

    def test_build_with_llm_restructure(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="LLM-RESTRUCTURED")
        articles = self._write_articles(tmp_path)
        res = w._build_from_articles(articles, "Kafka", "T", "default", "t2")
        assert res["content"] == "LLM-RESTRUCTURED"

    def test_build_with_llm_but_restructure_unavailable_refuses_collage(self, tmp_path):
        """窄边界（2026-11-10 第二批）：有 Key 但模板为空时重构返回 None，
        骨架+TF-IDF 的拼贴不得出厂，同 keyless 一样只交拒绝自述。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._prompt_profile = "__no_such_template__"  # _load_prompt_template → {}
        articles = self._write_articles(tmp_path)
        res = w._build_from_articles(articles, "Kafka 架构", "Kafka 文档", "default", "t1")
        assert res["status"] == "ok"
        content = res["content"]
        assert degradation.RESTRUCTURE_UNAVAILABLE in content
        assert degradation.KEYLESS_REFUSAL not in content  # 不是 keyless 的措辞
        assert "架构设计非常优秀" not in content
        assert "## 参考资料" not in content
        assert res["stats"]["articles_used"] == 2
        assert res["stats"]["restructure_refused"] is True

    def test_empty_query_with_key_refuses_collage(self, tmp_path):
        """第三批裁决（2026-11-10）：空查询 + 有 Key + 有素材 ⇒ 重构没有目标，
        与 keyless / 重构不可用同拒，骨架+TF-IDF 拼贴不得出厂。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        articles = self._write_articles(tmp_path)
        res = w._build_from_articles(articles, "", "T", "default", "t4")
        assert res["status"] == "ok"
        content = res["content"]
        assert degradation.RESTRUCTURE_UNAVAILABLE in content
        assert degradation.KEYLESS_REFUSAL not in content
        assert "架构设计非常优秀" not in content
        assert "## 参考资料" not in content
        assert res["stats"]["restructure_refused"] is True

    def test_stats_carry_query_and_source_urls(self, tmp_path):
        """门禁对账的信号源：query + 实际读入素材的 URL 集合。"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._restructure_document = MagicMock(return_value="LLM-RESTRUCTURED")
        articles = self._write_articles(tmp_path)
        res = w._build_from_articles(articles, "Kafka 架构", "T", "default", "t5")
        assert res["stats"]["query"] == "Kafka 架构"
        assert res["stats"]["source_urls"] == ["https://a1.com", "https://a2.com"]

    def test_empty_query_skeleton_used(self, tmp_path):
        w = _make_writer(tmp_path)
        res = w._build_from_articles([], "", "空文章", "default", "t3")
        assert res["status"] == "ok"
        assert "概述" in res["content"]


class TestStampTopicHeader:
    """重构输出是 LLM 新生文档，`> 主题:` 声明行由 writer 补回（门禁对账的锚）。"""

    def test_adds_topic_line_after_title(self):
        out = WriterAgent._stamp_topic_header("# T\n\nbody", "Kafka")
        assert out.split("\n")[1] == "> 主题: Kafka"

    def test_keeps_existing_line(self):
        doc = "# T\n\n> 主题: Kafka\n\nbody"
        assert WriterAgent._stamp_topic_header(doc, "Kafka") == doc

    def test_empty_query_is_noop(self):
        doc = "# T\n\nbody"
        assert WriterAgent._stamp_topic_header(doc, "") == doc


class TestPlanSkeleton:
    def test_no_query_default_skeleton(self, tmp_path):
        w = _make_writer(tmp_path)
        sk = w._plan_skeleton("", "T", [])
        assert [s["heading"] for s in sk] == ["概述", "详细内容", "总结"]

    def test_query_skeleton_with_article_keywords(self, tmp_path):
        w = _make_writer(tmp_path)
        articles = [{"text": ("Kafka 的副本机制和分区策略是分布式系统的核心组成部分，"
                              "它直接决定了整个系统的吞吐能力与数据可靠性表现。")} for _ in range(2)]
        sk = w._plan_skeleton("Kafka 核心架构", "T", articles)
        headings = [s["heading"] for s in sk]
        assert "简介" in headings
        assert "核心概念" in headings
        assert "详细分析" in headings  # 从文章中提取到关键词
        assert "实践与应用" in headings
        assert "总结" in headings


class TestFillSection:
    def test_paragraph_extraction_and_filters(self, tmp_path):
        w = _make_writer(tmp_path)
        good_para = "Kafka 的架构设计包含 broker、topic、partition 等核心组件，缺一不可。"
        text = (
            "短段落\n\n"                       # <40 字，过滤
            "navigation 首页 导航 栏\n\n"       # 导航前缀，过滤
            + good_para + "\n\n"
            + "x" * 4000 + "\n"                # 超长段落被截断
        )
        articles = [{"title": "T1", "url": "https://x.com", "text": text}]
        filled = w._fill_section({"keywords": ["Kafka", "架构"]}, articles)
        assert any(good_para[:20] in p for p in filled["paragraphs"])
        assert all(len(p) <= 3003 + 3 for p in filled["paragraphs"])
        assert filled["references"] == [{"title": "T1", "url": "https://x.com"}]

    def test_global_dedup_across_sections(self, tmp_path):
        w = _make_writer(tmp_path)
        para = ("同一段落内容在多个章节之间只应该出现一次，这是全局去重机制的基本语义，"
                "重复出现会显著降低文档的整体质量。")
        articles = [{"title": "T", "url": "https://x.com", "text": para}]
        seen: set = set()
        f1 = w._fill_section({"keywords": ["段落"]}, articles, seen)
        f2 = w._fill_section({"keywords": ["段落"]}, articles, seen)
        assert f1["paragraphs"] and not f2["paragraphs"]

    def test_no_paragraphs_returns_empty(self, tmp_path):
        w = _make_writer(tmp_path)
        filled = w._fill_section({"keywords": ["k"]}, [{"text": "short"}])
        assert filled == {"paragraphs": [], "references": []}


class TestSemanticRank:
    def test_no_keywords_returns_first_8(self, tmp_path):
        w = _make_writer(tmp_path)
        paras = [{"text": f"p{i}"} for i in range(10)]
        assert w._semantic_rank(paras, []) == paras[:8]

    def test_keyword_paragraphs_ranked_first(self, tmp_path):
        w = _make_writer(tmp_path)
        paras = [
            {"text": "完全无关的内容段落，讨论的是烹饪菜谱和烘焙技巧。" * 3},
            {"text": "Kafka 消息队列的架构设计与性能优化实践总结。" * 3},
        ]
        ranked = w._semantic_rank(paras, ["Kafka", "架构"])
        assert ranked and "Kafka" in ranked[0]["text"]
        assert all(p.get("score", 0) > 0.01 for p in ranked)

    def test_no_match_returns_fallback_three(self, tmp_path):
        w = _make_writer(tmp_path)
        paras = [{"text": "甲乙丙丁戊己庚辛壬癸子丑寅卯。" * 5} for _ in range(5)]
        out = w._semantic_rank(paras, ["zzz_not_present"])
        assert out == paras[:3]

    def test_empty_docs(self, tmp_path):
        w = _make_writer(tmp_path)
        assert w._semantic_rank([], ["k"]) == []


# ─── 搜索摘要模式 ──────────────────────────────────────────

class TestChunksPipeline:
    def test_to_chunks_and_extract_keywords(self, tmp_path):
        w = _make_writer(tmp_path)
        results = [{"title": "Kafka 入门", "snippet": "kafka kafka 教程",
                    "url": "https://x.com", "source": "bing"}]
        chunks = w._to_chunks(results)
        assert len(chunks) == 1
        assert isinstance(chunks[0], ContentChunk)
        assert "kafka" in chunks[0].keywords

    def test_classify_chunks(self, tmp_path):
        w = _make_writer(tmp_path)
        chunks = [
            ContentChunk(title="安装指南教程", content="配置与使用入门", source="s", url="u"),
            ContentChunk(title="A vs B 对比", content="区别与比较", source="s", url="u"),
            ContentChunk(title="随便聊聊", content="无关键词", source="s", url="u"),
        ]
        out = w._classify_chunks(chunks)
        assert out[0].section == "技术教程"
        assert out[1].section == "对比分析"
        assert out[2].section == "其他"

    def test_deduplicate_chunks(self, tmp_path):
        w = _make_writer(tmp_path)
        chunks = [
            ContentChunk(title="Same Title", content="1", source="s", url="u1"),
            ContentChunk(title="same title", content="2", source="s", url="u2"),
            ContentChunk(title="", content="3", source="s", url="u3"),
            ContentChunk(title="Other", content="4", source="s", url="u4"),
        ]
        out = w._deduplicate_chunks(chunks)
        assert [c.title for c in out] == ["Same Title", "Other"]

    def test_generate_document(self, tmp_path):
        w = _make_writer(tmp_path)
        chunks = w._classify_chunks([
            ContentChunk(title="教程甲", content="内容甲", source="s", url="https://a.com"),
            ContentChunk(title="原理乙", content="内容乙", source="s", url="https://b.com"),
        ])
        doc = w._generate_document(chunks, "default", "总文档")
        assert "# 总文档" in doc
        assert "### 教程甲" in doc
        assert "[教程甲](https://a.com)" in doc.replace("\\", "")


# ─── Mermaid / 参考资料修复 ────────────────────────────────

class TestMermaidAndRefFixes:
    def test_clean_mermaid_adds_graph_and_removes_orphan_end(self, tmp_path):
        w = _make_writer(tmp_path)
        text = (
            "前文\n"
            "```mermaid\n"
            "A --> B\n"      # 首行无关键字 → 补 graph TD
            "subgraph S\n"
            "C --> D\n"
            "end\n"
            "end\n"          # 孤立 end → 删除
            "```\n"
            "后文\n"
        )
        out = w._clean_mermaid(text)
        assert "graph TD" in out
        assert out.count("end") == 1  # 仅保留 subgraph 配对的 end

    def test_clean_mermaid_fixes_br(self, tmp_path):
        w = _make_writer(tmp_path)
        text = "```mermaid\ngraph TD\nA<br/>B\n```\n"
        assert "<br/>" not in w._clean_mermaid(text)

    def test_fix_references_removes_empty_section(self, tmp_path):
        assert "参考资料" not in WriterAgent._fix_references("正文\n## 参考资料\n   \n")
        assert "## 下一章" in WriterAgent._fix_references(
            "正文\n## 参考资料\n\n## 下一章\n内容")

    def test_remove_truncated_mermaid_blocks(self, tmp_path):
        ok_block = "```mermaid\ngraph TD\nA-->B\n```"
        truncated = "```mermaid\ngraph TD\nA-->B"
        text = f"保留\n{ok_block}\n截断\n{truncated}"
        out = WriterAgent._remove_truncated_mermaid_blocks(text)
        assert ok_block in out
        assert "截断" in out
        assert out.count("```mermaid") == 1


class TestAssembleSections:
    def test_assemble_sorted_and_title_prepended(self, tmp_path):
        w = _make_writer(tmp_path)
        cb = StreamCallback()
        results = [(1, "S2", "第二部分"), (0, "S1", "第一部分"), (2, "S3", "")]
        out = w._assemble_sections(results, "标题", cb)
        assert out.startswith("# 标题")
        assert out.index("第一部分") < out.index("第二部分")
        assert "S3" not in out  # 空 part 被跳过

    def test_all_failed_returns_none(self, tmp_path):
        w = _make_writer(tmp_path)
        assert w._assemble_sections([(0, "S1", "")], "T", None) is None


class TestRestructureDocument:
    def test_no_key_returns_none(self, tmp_path):
        w = _make_writer(tmp_path)
        assert w._restructure_document("c", [], "q", "t") is None

    def test_empty_template_returns_none(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._load_prompt_template = MagicMock(return_value={})
        assert w._restructure_document("c", [], "q", "t") is None

    def test_full_path_and_cache(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._load_prompt_template = MagicMock(return_value={
            "system_prompt": "sys",
            "sections": [{"name": "S1", "prompt": "写 {title} 的 {query}"}],
        })

        async def _fake_gen(idx, sec_name, sec_prompt, system_prompt, context):
            return (idx, sec_name, f"# 标题\n\n生成内容-{sec_name}")

        w._generate_section_async = _fake_gen
        out1 = w._restructure_document("c", [{"title": "a", "url": "u", "text": "t"}],
                                       "q", "标题")
        assert out1 and "生成内容-S1" in out1
        # 第二次同输入命中缓存（不再生成）。C11 修复后缓存 key 覆盖
        # query+title+system_prompt+素材 context，第二次必须传相同素材
        w._generate_section_async = MagicMock(side_effect=AssertionError("不应再调用"))
        out2 = w._restructure_document("c", [{"title": "a", "url": "u", "text": "t"}],
                                       "q", "标题")
        assert out2 == out1

    def test_cache_miss_on_different_articles(self, tmp_path):
        """C11 回归守卫：素材不同 → 缓存 key 不同，不得返回旧文档"""
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._load_prompt_template = MagicMock(return_value={
            "system_prompt": "sys",
            "sections": [{"name": "S1", "prompt": "写 {title} 的 {query}"}],
        })

        async def _fake_gen(idx, sec_name, sec_prompt, system_prompt, context):
            return (idx, sec_name, f"# 标题\n\n生成内容-{sec_name}-{context}")

        w._generate_section_async = _fake_gen
        out1 = w._restructure_document("c", [{"title": "a", "url": "u", "text": "素材甲"}],
                                       "q", "标题")
        out2 = w._restructure_document("c", [{"title": "a", "url": "u", "text": "素材乙"}],
                                       "q", "标题")
        assert out1 and out2
        assert out1 != out2

    def test_stream_callback_picked_by_task_id(self, tmp_path):
        w = _make_writer(tmp_path)
        w._llm_api_key = "k"
        w._load_prompt_template = MagicMock(return_value={
            "system_prompt": "sys",
            "sections": [{"name": "S1", "prompt": "p"}],
        })

        async def _fake_gen(idx, sec_name, sec_prompt, system_prompt, context):
            return (idx, sec_name, "内容块")

        w._generate_section_async = _fake_gen
        cb = StreamCallback()
        w._register_stream_callback("task-X", cb)
        out = w._restructure_document("c", [], "q", "T", task_id="task-X")
        assert out is not None
        events = cb.get_events()
        assert any(e.event_type == "section" for e in events)


class TestLoadPromptTemplate:
    def test_missing_yaml_returns_empty(self, tmp_path):
        w = _make_writer(tmp_path)
        w._prompts_dirs = [tmp_path]
        assert w._load_prompt_template("ghost") == {}

    def test_valid_yaml_loaded_and_cached(self, tmp_path):
        w = _make_writer(tmp_path)
        w._prompts_dirs = [tmp_path]
        (tmp_path / "myprofile.yaml").write_text(
            "system_prompt: you are x\nsections:\n  - name: S1\n    prompt: p1\n",
            encoding="utf-8")
        t1 = w._load_prompt_template("myprofile")
        assert t1["system_prompt"] == "you are x"
        assert len(t1["sections"]) == 1
        # 缓存命中（改文件也不应重读）
        (tmp_path / "myprofile.yaml").write_text("system_prompt: changed\n", encoding="utf-8")
        t2 = w._load_prompt_template("myprofile")
        assert t2["system_prompt"] == "you are x"

    def test_broken_yaml_returns_empty(self, tmp_path):
        w = _make_writer(tmp_path)
        w._prompts_dirs = [tmp_path]
        (tmp_path / "bad.yaml").write_text("a:\n\t- broken\n  -: [", encoding="utf-8")
        assert w._load_prompt_template("bad") == {}


class TestQualityFeedbackEnhancement:
    def test_recommendations_appended(self, tmp_path):
        w = _make_writer(tmp_path)
        with patch("loom_core.quality_feedback.get_quality_feedback") as mock_gqf:
            mock_gqf.return_value.get_recommendations.return_value = ["加强引用", "改善结构"]
            out = w._enhance_system_prompt_with_feedback("原始 prompt")
        assert "质量改进提醒" in out
        assert "加强引用" in out

    def test_no_recommendations_unchanged(self, tmp_path):
        w = _make_writer(tmp_path)
        with patch("loom_core.quality_feedback.get_quality_feedback") as mock_gqf:
            mock_gqf.return_value.get_recommendations.return_value = []
            assert w._enhance_system_prompt_with_feedback("原始") == "原始"

    def test_exception_returns_original(self, tmp_path):
        w = _make_writer(tmp_path)
        with patch("loom_core.quality_feedback.get_quality_feedback",
                   side_effect=RuntimeError("db down")):
            assert w._enhance_system_prompt_with_feedback("原始") == "原始"


class TestGenerateTextAsync:
    def test_router_async_path(self, tmp_path):
        w = _make_writer(tmp_path)
        router = MagicMock()
        router.get_active_providers.return_value = ["p"]

        async def _chat_async(messages, **kw):
            return ("router-async-content", "p")
        router.chat_async = _chat_async
        with patch("loom_core.llm_router.get_router", return_value=router):
            out = asyncio.run(w._generate_text_async("sp", "sys", "ctx"))
        assert out == "router-async-content"

    def test_fallback_to_llm_chat_async(self, tmp_path):
        w = _make_writer(tmp_path)

        async def _fake(messages, **kw):
            return "  fallback-content  "
        w._llm_chat_async = _fake
        with patch("loom_core.llm_router.get_router", return_value=None):
            out = asyncio.run(w._generate_text_async("sp", "sys", "ctx"))
        assert out == "fallback-content"

    def test_all_fail_returns_empty(self, tmp_path):
        w = _make_writer(tmp_path)

        async def _boom(messages, **kw):
            raise RuntimeError("no llm")
        w._llm_chat_async = _boom
        with patch("loom_core.llm_router.get_router", return_value=None):
            assert asyncio.run(w._generate_text_async("sp", "sys", "ctx")) == ""


class TestHandleStreaming:
    def test_streaming_lifecycle(self, tmp_path):
        w = _make_writer(tmp_path)
        w._load_prompt_template = MagicMock(return_value={
            "sections": [{"name": "S1"}, {"name": "S2"}]})
        cb = StreamCallback()
        msg = _msg({"task_id": "ts1", "query": "q", "title": "流式标题"})
        res = w.handle_streaming(msg, cb)
        assert res["status"] == "ok"
        events = [e.event_type for e in cb.get_events()]
        assert events[0] == "start"
        assert events[-1] == "complete"
        # 回调已注销
        assert w._get_stream_callback("ts1") is None

    def test_streaming_error_reported(self, tmp_path):
        w = _make_writer(tmp_path)
        w._load_prompt_template = MagicMock(return_value={"sections": []})
        w.handle = MagicMock(side_effect=RuntimeError("boom"))
        cb = StreamCallback()
        res = w.handle_streaming(_msg({"task_id": "ts2"}), cb)
        assert res["status"] == "error"
        assert "boom" in res["error"]
        events = [e.event_type for e in cb.get_events()]
        assert "error" in events
