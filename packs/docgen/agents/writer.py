"""
Writer Agent v2 - 增强型内容整合插件
==================================
改进点：
  - 多模板支持（可配置模板）
  - 智能章节分类（NLP 关键词提取）
  - 内容去重和合并
  - 自动目录生成
  - 引用追踪
  - 文档骨架规划 + 从完整文章填充内容（配合 fetcher）
  - TF-IDF 向量语义匹配（替代关键词计数）
"""
import asyncio
import contextlib
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

from artesian.fast_json import dumps as _fast_dumps
from artesian.fast_json import loads as _fast_loads
from loom_core.base_agent import AgentStatus, BaseAgent, Message
from loom_core.cache_manager import CacheManager
from loom_core.streaming import StreamCallback
from loom_doc import degradation

# ─── 章节级并行 LLM 润色 ──────────────────────────────
# 当 LLM 可用时，多章节并行调用 LLM 润色（ThreadPoolExecutor），
# 减少串行等待时间。单章节降级为同步调用。
PARALLEL_POLISH_WORKERS = 4


AGENT_NAME = "writer"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "prompt_profile": ('str', 'generic-tech'),
    "pending_expire_secs": ('int', 300),
    "polish_cache_ttl": ('int', 3600),
}
AGENT_VERSION = "2.0"
AGENT_DESC = "增强型内容整合 Agent - 多模板、智能分类、引用追踪、TF-IDF 语义匹配"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 30
INPUT_TOPICS = ["writer.input", "researcher.done", "researcher.partial", "fetcher.done"]
OUTPUT_TOPICS = ["writer.done", "writer.progress"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：产出文档正文
PRODUCES = {"content": "last"}
CONSUMES = ["articles", "results", "spec"]
DEPENDENCIES = ["researcher", "fetcher"]
CACHE_TTL = 0
RESPAWN = False

# ─── TF-IDF 分词（中文 2-gram）──────────────────────────
# 中文无空格分隔：连续汉字若只按整段取 token，查询侧短词与文档侧长段
# 在词面上永不相交，余弦相似度恒为 0（_semantic_rank 退化为无排序 fallback）。
# 英文按单词、中文 run 整段 + 额外 2-gram，与 researcher._relevance_score 同一策略。
_RE_TFIDF_EN_WORD = re.compile(r"[a-zA-Z]{2,}")
_RE_TFIDF_CN_RUN = re.compile(r"[一-鿿]{2,}")


def _tokenize_for_tfidf(text: str) -> list[str]:
    """TF-IDF 分词：英文按单词；中文 run 保留整段并额外生成 2-gram。"""
    tokens: list[str] = [w for w in _RE_TFIDF_EN_WORD.findall(text.lower())]
    for run in _RE_TFIDF_CN_RUN.findall(text):
        tokens.append(run)
        if len(run) > 2:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
    return tokens


@dataclass
class ContentChunk:
    """内容块"""
    title: str
    content: str
    source: str
    url: str
    section: str = ""
    keywords: list = field(default_factory=list)
    relevance_score: float = 0.0


def _md_escape(text: str) -> str:
    """Escape markdown special chars inside link text/url."""
    return text.replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)").replace("*", "\\*").replace("_", "\\_")


class TemplateManager:
    """模板管理器"""

    def __init__(self):
        self._templates = {
            "default": {
                "header": "# {title}\n\n> 生成时间: {time}\n\n",
                "section": "## {section_title}\n\n{content}\n\n",
                "footer": "\n---\n\n## 参考资料\n\n{references}\n",
            }
        }

    def render(self, template_name: str, title: str, sections: list,
               references: list, **kwargs) -> str:
        template = self._templates.get(template_name, self._templates["default"])
        parts = [template["header"].format(title=title, time=time.strftime('%Y-%m-%d %H:%M:%S'))]
        for sec in sections:
            parts.append(template["section"].format(
                section_title=sec.get("title", ""), content=sec.get("content", "")))
        refs = "\n".join(
            f"- [{_md_escape(r.get('title', ''))}]({_md_escape(r.get('url', ''))})"
            for r in references
        ) if references else ""
        parts.append(template["footer"].format(references=refs))
        return "\n".join(parts)


class WriterAgent(BaseAgent):
    """增强型内容整合 Agent"""

    SECTION_TITLES = ["概念原理", "技术教程", "实践应用", "对比分析", "最佳实践", "其他"]

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self.pending_results: dict = {}
        self._pending_lock = threading.Lock()
        self._template_mgr = TemplateManager()
        self._pending_expire_secs = config.get("pending_expire_secs", 300)
        self._section_scores = {
            "技术教程": ["教程", "指南", "入门", "安装", "配置", "使用"],
            "概念原理": ["概念", "原理", "机制", "架构", "设计", "模式"],
            "实践应用": ["实践", "应用", "案例", "示例", "场景", "项目"],
            "对比分析": ["对比", "区别", "比较", "优缺点", "vs", "versus"],
            "最佳实践": ["最佳实践", "建议", "技巧", "推荐", "优化"],
        }
        self.log_info(f"Writer v{AGENT_VERSION} 初始化完成")

        # LLM 润色配置（支持任意 OpenAI 兼容接口）
        self._llm_api_url = config.get("llm_api_url",
            os.environ.get("LLM_API_URL", "https://api.siliconflow.cn/v1/chat/completions"))
        self._llm_api_key = config.get("llm_api_key",
            os.environ.get("LLM_API_KEY", "") or
            os.environ.get("SILICONFLOW_API_KEY", ""))
        self._llm_model = config.get("llm_model",
            os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash"))
        if self._llm_api_key:
            self.log_info("LLM 润色已启用")

        self._polish_cache = CacheManager(
            name="writer_polish", max_size=config.get("polish_cache_max", 200),
            ttl=config.get("polish_cache_ttl", 3600),
        )
        self._cache_lock = threading.Lock()  # 保留用于 pending_results

        # Prompt 模板配置
        self._prompt_profile = config.get("prompt_profile", "default")
        from loom_core.pack_manifest import prompts_dirs as _prompts_dirs
        self._prompts_dirs = _prompts_dirs()
        self._prompt_template_cache = CacheManager(
            name="writer_prompt", max_size=50, ttl=0,
        )
        self._restructure_cache = CacheManager(
            name="writer_restructure", max_size=config.get("restructure_cache_max", 200),
            ttl=config.get("restructure_cache_ttl", 7200),
        )
        self.log_info(f"Prompt profile: {self._prompt_profile}")
        # P2 修复：类级单槽 _active_stream_callback 在并发两条流时互踩。
        # 改为实例级 dict[task_id]->callback 注册表，按 task_id 路由分发；
        # 旧单槽语义经属性 property 映射到 "" 键保持兼容（task_id 缺省流）。
        self._stream_callbacks: dict = {}
        self._stream_callbacks_lock = threading.Lock()
        self._audience_level = "中级"  # 默认读者水平，可被 spec.audience 覆盖

        # 规则过渡句模板（0 成本兜底）
        self._transition_templates = [
            ("简介", "核心概念", "了解了基本概念后，下面我们来深入分析其核心原理。"),
            ("核心概念", "详细分析", "理解了核心概念后，我们进一步探讨具体的技术细节。"),
            ("核心概念", "实践", "理论之外，实践应用同样值得关注。"),
            ("详细分析", "实践", "上面的分析为我们打下了基础，接下来看看实际应用。"),
            ("详细分析", "实践与应用", "技术原理之外，实际应用场景同样重要。"),
            ("实践", "总结", "综合以上讨论，我们可以做出以下总结。"),
            ("实践与应用", "总结", "回顾以上的实践内容，可以得出以下结论。"),
            ("", "", "接下来，我们继续探讨相关内容。"),  # 通用回退
        ]

    # ─── 流式回调注册表（P2：按 task_id 路由，旧单槽经 property 兼容） ──────

    _LEGACY_STREAM_KEY = ""

    @property
    def _active_stream_callback(self):
        """旧单槽兼容读取（等价 task_id 缺省 "" 键）"""
        with self._stream_callbacks_lock:
            return self._stream_callbacks.get(self._LEGACY_STREAM_KEY)

    @_active_stream_callback.setter
    def _active_stream_callback(self, callback):
        with self._stream_callbacks_lock:
            if callback is None:
                self._stream_callbacks.pop(self._LEGACY_STREAM_KEY, None)
            else:
                self._stream_callbacks[self._LEGACY_STREAM_KEY] = callback

    def _register_stream_callback(self, task_id: str, callback: StreamCallback) -> None:
        with self._stream_callbacks_lock:
            self._stream_callbacks[task_id or self._LEGACY_STREAM_KEY] = callback

    def _unregister_stream_callback(self, task_id: str) -> None:
        with self._stream_callbacks_lock:
            self._stream_callbacks.pop(task_id or self._LEGACY_STREAM_KEY, None)

    def _get_stream_callback(self, task_id: str) -> StreamCallback | None:
        """按 task_id 取回调；未命中时回退旧单槽键（兼容缺省流）"""
        with self._stream_callbacks_lock:
            cb = self._stream_callbacks.get(task_id or self._LEGACY_STREAM_KEY)
            if cb is None and task_id:
                cb = self._stream_callbacks.get(self._LEGACY_STREAM_KEY)
            return cb

    def _llm_chat(self, messages: list[dict], max_tokens: int = 4096,
                  temperature: float = 0.3, timeout: int = 120) -> str:
        """统一 LLM 调用（优先用 LLMRouter 多供应商 fallback，回退到单 LLM）"""
        # 优先尝试 LLMRouter（多供应商自动 fallback）
        try:
            from loom_core.llm_router import get_router
            router = get_router()
            if router and router.get_active_providers():
                content, provider = router.chat(messages, max_tokens=max_tokens,
                                                temperature=temperature, timeout=timeout)
                self.log_debug(f"LLM 调用成功: {provider}")
                return content
        except Exception as e:
            self.log_debug(f"LLMRouter 不可用，回退到单 LLM: {e}")

        # 回退：原有单 LLM 调用（自动适配 Cloudflare Workers AI 格式）
        is_cf = "/ai/run" in self._llm_api_url
        if is_cf:
            payload = {"model": self._llm_model, "input": {"messages": messages},
                       "max_tokens": max_tokens, "temperature": temperature}
        else:
            payload = {"model": self._llm_model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}
        data = _fast_dumps(payload).encode()
        req = urllib.request.Request(
            self._llm_api_url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._llm_api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _fast_loads(resp.read())
        if is_cf:
            body = body.get("result", body)
        return body["choices"][0]["message"]["content"]  # type: ignore[no-any-return]

    async def _llm_chat_async(self, messages: list[dict], max_tokens: int = 4096,
                              temperature: float = 0.3, timeout: int = 120) -> str:
        """异步 LLM 调用 —— 使用 aiohttp 真异步 HTTP，不阻塞事件循环。

        优先使用 aiohttp（零线程开销），失败时回退到 run_in_executor 包装同步方法。
        """
        if not self._llm_api_key:
            return ""
        try:
            import aiohttp
        except ImportError:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._llm_chat, messages, max_tokens, temperature, timeout,
            )

        is_cf = "/ai/run" in self._llm_api_url
        if is_cf:
            payload = {"model": self._llm_model, "input": {"messages": messages},
                       "max_tokens": max_tokens, "temperature": temperature}
        else:
            payload = {"model": self._llm_model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._llm_api_key}"}

        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
                session.post(self._llm_api_url, json=payload, headers=headers) as resp,
            ):
                body = await resp.json()
                if is_cf:
                    body = body.get("result", body)
                return body["choices"][0]["message"]["content"]  # type: ignore[no-any-return]
        except Exception as e:
            self.log_warning(f"aiohttp LLM 调用失败，回退同步: {e}")
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._llm_chat, messages, max_tokens, temperature, timeout,
            )

    async def _llm_chat_stream_async(self, messages: list[dict], max_tokens: int = 4096,
                                     temperature: float = 0.3, timeout: int = 120):
        """异步流式 LLM 调用 —— 使用 aiohttp 逐 chunk yield。

        比同步 _llm_chat_stream 更高效：不阻塞事件循环，可与其他 async 任务并发。
        """
        if not self._llm_api_key:
            return
        try:
            import aiohttp
        except ImportError:
            for chunk in self._llm_chat_stream(messages, max_tokens, temperature, timeout):
                yield chunk
            return

        is_cf = "/ai/run" in self._llm_api_url
        if is_cf:
            payload = {"model": self._llm_model, "input": {"messages": messages},
                       "max_tokens": max_tokens, "temperature": temperature, "stream": True}
        else:
            payload = {"model": self._llm_model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature, "stream": True}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._llm_api_key}",
                   "Accept": "text/event-stream"}

        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
                session.post(self._llm_api_url, json=payload, headers=headers) as resp,
            ):
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str or not line_str.startswith("data:"):
                        continue
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_data = _fast_loads(data_str)
                        if is_cf:
                            chunk_data = chunk_data.get("result", chunk_data)
                        delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            self.log_warning(f"aiohttp 流式 LLM 失败，回退同步: {e}")
            for chunk in self._llm_chat_stream(messages, max_tokens, temperature, timeout):
                yield chunk

    def _llm_chat_stream(self, messages: list[dict], max_tokens: int = 4096,
                         temperature: float = 0.3, timeout: int = 120,
                         read_timeout: int = 30, max_retries: int = 2):
        """流式 LLM 调用 —— 逐 chunk yield，降低首字延迟。

        使用 SSE (Server-Sent Events) streaming response：
        - 请求中设置 stream=True
        - 响应为 text/event-stream 格式，每行 data: {...}
        - 逐行读取并 yield delta content

        增强特性：
        - read_timeout: socket 级读取超时，防止服务端 hang 住不发送数据
        - max_retries: 连接建立失败时的最大重试次数
        - 连接中断时保留已 yield 的部分结果（不产生重复内容）
        - 无任何数据时自动回退到非流式 _llm_chat

        用法:
            for chunk in writer._llm_chat_stream(messages):
                print(chunk, end="", flush=True)  # 实时输出

        若 LLM 端点不支持 streaming，自动回退到非流式 _llm_chat。
        """

        # 构建流式请求 payload
        is_cf = "/ai/run" in self._llm_api_url
        if is_cf:
            payload = {"model": self._llm_model, "input": {"messages": messages},
                       "max_tokens": max_tokens, "temperature": temperature,
                       "stream": True}
        else:
            payload = {"model": self._llm_model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature,
                       "stream": True}
        data = _fast_dumps(payload).encode()
        req = urllib.request.Request(
            self._llm_api_url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._llm_api_key}",
                     "Accept": "text/event-stream"})

        # 连接建立重试
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                break
            except Exception as e:
                if attempt < max_retries:
                    self.log_warning(f"SSE 连接失败 (attempt {attempt+1}/{max_retries+1}): {e}")
                    continue
                # 所有重试失败，回退到非流式
                result = self._llm_chat(messages, max_tokens, temperature, timeout)
                yield result
                return

        # 逐行读取 SSE 流
        buffer = ""
        try:
            # 设置 socket 级读取超时，防止服务端 hang
            try:
                sock = resp.fp.raw._sock  # type: ignore[union-attr]
                sock.settimeout(read_timeout)
            except (AttributeError, OSError):
                pass  # 无法设置 socket 超时，继续用默认行为

            for raw_line in resp:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                elif line.startswith("data:"):
                    data_str = line[5:]
                else:
                    continue

                if data_str == "[DONE]":
                    break

                try:
                    chunk_json = _fast_loads(data_str)
                except json.JSONDecodeError:
                    continue

                # 适配不同 API 格式
                if is_cf:
                    chunk_json = chunk_json.get("result", chunk_json)

                # 提取 delta content
                try:
                    delta = chunk_json["choices"][0]["delta"].get("content", "")
                except (KeyError, IndexError):
                    delta = ""

                if delta:
                    buffer += delta
                    yield delta

        except TimeoutError as e:
            # 读取超时：已 yield 的部分结果保留给调用方，不重试（避免重复内容）
            self.log_warning(f"SSE 读取超时，已收到 {len(buffer)} 字符: {e}")
        except (ConnectionError, OSError) as e:
            # 连接中断：已 yield 的部分结果保留给调用方
            self.log_warning(f"SSE 连接中断，已收到 {len(buffer)} 字符: {e}")
        finally:
            if resp:
                with contextlib.suppress(Exception):
                    resp.close()

        # 若流式返回为空（未收到任何 chunk），回退到非流式
        if not buffer:
            try:
                result = self._llm_chat(messages, max_tokens, temperature, timeout)
                yield result
            except Exception as e:
                self.log_warning(f"SSE 流式回退非流式失败: {e}")

    def _polish_sections_parallel(self, sections: list[dict], context: str = "",
                                   task_id: str = "") -> list[dict]:
        """章节级并行 LLM 润色 - 多章节同时调用 LLM，减少串行等待。

        当 LLM 不可用或章节数 <= 1 时降级为串行处理。
        使用 PARALLEL_POLISH_WORKERS 控制并发度。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if len(sections) <= 1:
            return sections

        if not self._llm_api_key:
            return sections  # LLM 不可用，跳过润色

        polished = [None] * len(sections)

        def _polish_one(idx: int, section: dict) -> tuple:
            """润色单个章节"""
            sec_title = section.get("title", "")
            sec_content = section.get("content", "")
            if not sec_content or len(sec_content) < 100:
                return idx, section  # 内容过短不润色

            messages = [
                {"role": "system", "content": "你是技术文档润色专家。保持专业性和准确性，改善行文流畅度。"},
                {"role": "user", "content": (
                    f"请润色以下技术文档章节。\n"
                    f"章节标题: {sec_title}\n"
                    f"文档主题: {context}\n\n"
                    f"{sec_content[:3000]}"
                )},
            ]
            try:
                result = self._llm_chat(messages, max_tokens=2048, temperature=0.3)
                if result and len(result) > 50:
                    section = dict(section)
                    section["content"] = result
                    section["polished"] = True
            except Exception as e:
                self.log_debug(f"章节润色失败 [{sec_title}]: {e}")
            return idx, section

        workers = min(PARALLEL_POLISH_WORKERS, len(sections))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_polish_one, i, sec): i
                for i, sec in enumerate(sections)
            }
            for fut in as_completed(futures):
                try:
                    idx, result = fut.result(timeout=90)
                    polished[idx] = result
                except Exception:
                    idx = futures[fut]
                    polished[idx] = sections[idx]  # type: ignore[call-overload]

        # 填充未完成的（超时等异常情况）
        for i in range(len(polished)):
            if polished[i] is None:
                polished[i] = sections[i]  # type: ignore[call-overload]

        polished_count = sum(1 for p in polished if p.get("polished"))  # type: ignore[misc,attr-defined]
        if polished_count:
            self.log_info(f"任务 {task_id}: 并行润色 {polished_count}/{len(sections)} 个章节")

        return polished  # type: ignore[return-value]

    def _polish_with_llm(self, content: str, query: str) -> str:
        """用 LLM 润色文档段落衔接（含缓存+质量门控+分段+规则兜底）"""
        if not self._llm_api_key or not content.strip():
            return content

        cache_key = hashlib.sha256((content[:200] + query).encode()).hexdigest()
        now = time.time()
        cached = self._polish_cache.get(cache_key)
        if cached is not None:
            self.log_info("LLM 润色缓存命中，跳过")
            return cached  # type: ignore[no-any-return]

        # ── Layer 1: 质量门控 ──
        # 如果文档已完整（3+ 章节、有引用、500+ 字），跳过 LLM
        section_count = len(re.findall(r'^##\s+\S+', content, re.MULTILINE))
        ref_count = len(re.findall(r'\[([^\]]+)\]\(https?://[^)]+\)', content))
        if section_count >= 3 and ref_count >= 1 and len(content) >= 500:
            self.log_info(f"质量门控跳过 LLM ({section_count}章, {ref_count}引用, {len(content)}字)")
            return content

        # ── Layer 2: 分段润色 ──
        segments = re.split(r'\n(?=##\s)', content)
        polished_segments = []

        for i, seg in enumerate(segments):
            seg = seg.strip()
            if not seg or len(seg) < 100:
                polished_segments.append(seg)
                continue

            # 段落间插规则过渡句（Layer 3）
            if i > 0:
                prev_heading = re.search(r'^##\s+(.+)', segments[i - 1], re.MULTILINE)
                curr_heading = re.search(r'^##\s+(.+)', seg, re.MULTILINE)
                prev = prev_heading.group(1).strip() if prev_heading else ""
                curr = curr_heading.group(1).strip() if curr_heading else ""

                # 找匹配的过渡模板
                transition = ""
                for t_prev, t_curr, t_text in self._transition_templates:
                    if (t_prev in prev or prev in t_prev) and (t_curr in curr or curr in t_curr):
                        transition = t_text
                        break
                if not transition:
                    for tp, tc, tt in self._transition_templates:
                        if not tp and not tc:
                            transition = tt  # 通用回退
                            break

                if transition:
                    polished_segments.append(f"> {transition}\n")

            # 小段 fallback: LLM 润色
            seg_prompt = f"润色以下段落，改善语句流畅性，保持原意不变：\n\n{seg[:2000]}"
            try:
                t0 = time.time()
                polished = self._llm_chat(
                    [{"role": "user", "content": seg_prompt}],
                    max_tokens=1024, temperature=0.3, timeout=30)
                # 防御空响应：_llm_chat 返回空串/纯空白时不抛异常，
                # 直接 append 会把整段替换为空并随 result 缓存 1 小时
                # （polish_cache_ttl=3600），造成静默内容丢失。空响应一律保留原文。
                if polished and polished.strip():
                    polished_segments.append(polished)
                else:
                    self.log_debug(f"  段{i}: LLM 空响应，保留原文")
                    polished_segments.append(seg)
                self.log_debug(f"  段{i}: {len(seg)}→{len(polished)} 字, {time.time()-t0:.1f}s")
            except Exception:
                self.log_debug(f"  段{i} 润色失败, 保留原文")
                polished_segments.append(seg)

        result = "\n\n".join(polished_segments)
        elapsed = time.time() - now

        self._polish_cache.set(cache_key, result)
        self.log_info(f"LLM 润色完成 ({len(segments)}段, {elapsed:.1f}s, {len(content)}→{len(result)} 字)")
        return result

    def handle(self, msg: Message) -> dict | None:
        """处理整合请求（支持 fetcher 文章 + 文档骨架规划）"""
        self.report(AgentStatus.RUNNING, "开始整合内容...")

        payload = msg.payload
        task_id = payload.get("task_id", "")

        # 从 per-agent config 刷新运行时配置（__init__ 时 config 为空，需在此更新）
        runtime_cfg = payload.get("config", {})
        if runtime_cfg:
            if "prompt_profile" in runtime_cfg:
                self._prompt_profile = runtime_cfg["prompt_profile"]
            if "llm_api_url" in runtime_cfg:
                self._llm_api_url = runtime_cfg["llm_api_url"]
            if "llm_api_key" in runtime_cfg:
                self._llm_api_key = runtime_cfg["llm_api_key"]
            if "llm_model" in runtime_cfg:
                self._llm_model = runtime_cfg["llm_model"]
            self.log_info(f"运行时配置已刷新: prompt_profile={self._prompt_profile}, model={self._llm_model}")

        template_name = payload.get("template", "default")
        title = payload.get("title", "自动生成文档")
        query = payload.get("query", "")

        # 从 DocumentSpec 增强标题和查询：优先使用 spec.doc_type 和 spec.scope
        spec = payload.get("spec") or {}
        if isinstance(spec, dict):
            if spec.get("doc_type") and spec["doc_type"] != "其他":
                title = f"{spec['doc_type']}：{title}"
            if spec.get("scope") and not query:
                query = " ".join(spec["scope"][:3])
            if spec.get("audience"):
                self._audience_level = spec["audience"]

        if not query:
            queries = payload.get("queries", [])
            query = queries[0] if queries else ""
        if not query:
            # 从输入文件取首行非注释内容作为主题
            input_file = payload.get("input_file", "")
            if input_file and os.path.exists(input_file):
                try:
                    with open(input_file, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                query = line[:80]
                                break
                except Exception as e:
                    self.log_warning(f"读取输入文件失败: {e}")

        self._expire_stale_pending()

        # ── 优先使用 fetcher 的完整文章 ──
        articles = payload.get("articles", [])
        kb_hits = self._kb_hits(payload)
        if articles and kb_hits:
            self.log_warning(
                f"任务 {task_id}: 同时存在网络文章({len(articles)})与知识库命中"
                f"({len(kb_hits)})，当前优先使用网络文章，KB 接地未生效")
        if articles:
            return self._build_from_articles(articles, query, title, template_name, task_id)

        # ── 本地知识库命中：用自己的资料接地（kb-docgen 流水线）──
        if kb_hits:
            return self._build_from_kb(kb_hits, query, title, task_id)

        # ── 次选：搜索摘要（旧模式） ──
        results = payload.get("results", [])
        if not results:
            with self._pending_lock:
                pending_entry = self.pending_results.get(task_id)
                if pending_entry:
                    results = pending_entry[1]

        if not results:
            # 无搜索结果也无文章：若 LLM 可用，仍尝试基于 query 生成文档
            if query and self._llm_api_key:
                self.log_info(f"任务 {task_id}: 无文章无摘要，用 LLM 基于主题生成文档")
                restructured = self._restructure_document("", [], query, title, task_id=task_id)
                if restructured:
                    return {"status": "ok", "task_id": task_id, "content": restructured}
            return {
                "status": "ok",
                "task_id": task_id,
                "message": "无待整合内容，生成占位文档",
                "content": f"# {title}\n\n> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{degradation.EMPTY_RESULT_DOC}。\n",
            }

        self.log_info(f"任务 {task_id}: 整合 {len(results)} 条搜索摘要")
        chunks = self._to_chunks(results)
        chunks = self._classify_chunks(chunks)
        chunks = self._deduplicate_chunks(chunks)
        content = self._generate_document(chunks, template_name, title)
        with self._pending_lock:
            self.pending_results.pop(task_id, None)

        # 治本（2026-11-10）：检索摘要路径同理，无 Key 不拼贴。
        if not self._llm_api_key:
            content = self._keyless_refusal_content(
                title, query, f"已检索 {len(results)} 条结果，无法整合成文")
            self.log_warning(
                f"任务 {task_id}: 无 LLM API Key，{len(results)} 条检索摘要"
                "无法整合成文，拒绝输出原始拼贴，只交降级自述")
            return {"status": "ok", "task_id": task_id, "content": content,
                    "stats": {"total_results": len(results),
                              "unique_chunks": len(chunks),
                              "sections": len({c.section for c in chunks}),
                              "word_count": len(content),
                              "char_count": len(content),
                              "keyless_refused": True}}

        # LLM 重构：即使只有搜索摘要，也用 LLM 生成完整技术文档
        if query and self._llm_api_key:
            # 将搜索摘要转为伪文章上下文
            snippet_articles = []
            for r in results[:5]:
                snippet_articles.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "text": r.get("snippet", ""),
                })
            restructured = self._restructure_document(content, snippet_articles, query, title, task_id=task_id)
            if restructured:
                content = restructured
            else:
                # 窄边界（2026-11-10 第二批）：有 Key 但重构返回 None（模板为空/
                # 加载失败/章节生成全灭）时，_generate_document 的拼贴不得出厂——
                # 与 keyless 拼贴同一个病（FP-1b 同源），拒绝之。
                content = self._restructure_refusal_content(
                    title, query, f"已检索 {len(results)} 条结果，重构不可用")
                self.log_warning(
                    f"任务 {task_id}: 有 Key 但 LLM 重构不可用，{len(results)} 条检索摘要"
                    "无法整合成文，拒绝输出原始拼贴，只交降级自述")
                return {"status": "ok", "task_id": task_id, "content": content,
                        "stats": {"total_results": len(results),
                                  "unique_chunks": len(chunks),
                                  "sections": len({c.section for c in chunks}),
                                  "word_count": len(content),
                                  "char_count": len(content),
                                  "restructure_refused": True}}

        self.report(AgentStatus.RUNNING, f"整合完成，生成 {len(content)} 字符")
        return {
            "status": "ok",
            "task_id": task_id,
            "content": content,
            "stats": {
                "total_results": len(results),
                "unique_chunks": len(chunks),
                "sections": len(set(c.section for c in chunks)),
                "word_count": len(content),
                "char_count": len(content),
            }
        }

    # ═══════════════════════════════════════════════
    # 知识库接地：上游 knowledge_base 命中 → 抽取式草稿 → LLM 重构
    # ═══════════════════════════════════════════════

    @staticmethod
    def _kb_hits(payload: dict) -> list[dict]:
        """从上游 knowledge_base 节点结果里取检索命中。"""
        dep = (payload.get("dependencies_results") or {}).get("knowledge_base") or {}
        hits = dep.get("results") or []
        return [h for h in hits
                if isinstance(h, dict) and str(h.get("content", "")).strip()]

    @staticmethod
    def _kb_articles(hits: list[dict]) -> list[dict]:
        """把切块伪装成"文章"，复用既有的素材上下文与缓存键（C11）。"""
        return [{
            "title": str(h.get("heading_path") or h.get("title") or ""),
            "url": f"kb://{h.get('source', '?')}#{h.get('chunk_id', '')}",
            "text": str(h.get("content", "")),
            "relevance": float(h.get("score", 0) or 0),
        } for h in hits]

    def _build_from_kb(self, hits: list[dict], query: str, title: str,
                        task_id: str) -> dict:
        """基于本地知识库切块产出文档。

        先做抽取式草稿（无 LLM 也能交付，且内容全部来自用户资料），
        有 LLM 时再走既有的重构路径 —— 素材上下文进 prompt 也进缓存键。
        """
        self.log_info(f"任务 {task_id}: 使用知识库命中 {len(hits)} 个切块接地")

        parts = [f"# {title}", "",
                 f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        if query:
            parts.append(f"> 主题: {query}")
        parts.append("")

        groups: dict[str, list[dict]] = {}
        for h in hits:
            key = str(h.get("heading_path") or h.get("title") or "相关资料")
            groups.setdefault(key, []).append(h)
        for heading, items in groups.items():
            parts.extend([f"## {heading}", ""])
            for h in items:
                parts.extend([str(h["content"]).strip(), ""])

        parts.extend(["## 参考资料（本地知识库）", ""])
        for i, h in enumerate(hits, 1):
            parts.append(
                f"{i}. {h.get('source', '?')} · "
                f"{h.get('heading_path') or h.get('title') or '正文'}"
                f"（相关度 {float(h.get('score', 0) or 0):.2f}）")

        draft = "\n".join(parts).strip()
        sources = sorted({str(h.get("source", "?")) for h in hits})
        stats = {"kb_hits": len(hits), "kb_sources": len(sources),
                 "char_count": len(draft), "llm_used": False}

        if query and self._llm_api_key:
            restructured = self._restructure_document(
                draft, self._kb_articles(hits), query, title, task_id=task_id)
            if restructured:
                stats["llm_used"] = True
                stats["char_count"] = len(restructured)
                return {"status": "ok", "task_id": task_id,
                        "content": restructured, "stats": stats}

        return {"status": "ok", "task_id": task_id, "content": draft, "stats": stats}

    # ═══════════════════════════════════════════════
    # 新流程：文章 → 骨架 → TF-IDF 填充
    # ═══════════════════════════════════════════════

    def _keyless_refusal_content(self, title: str, query: str,
                                  material_note: str) -> str:
        """无 LLM Key 时 web 素材路径的拒绝稿：只交自述，不交拼贴。

        web 素材的"整合"本身就是 LLM 的活；keyless 拼贴=把检索原文当成品出厂，
        正是 FP-1b 的源头。KB 抽取式草稿是另一条约（内容本就叫摘录），不在此列。
        """
        return (
            f"# {title}\n\n"
            f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + (f"> 主题: {query}\n" if query else "")
            + f"\n{degradation.KEYLESS_REFUSAL}：{material_note}\n"
            "配置 LLM API Key（复制 .env.example 为 .env）后重新生成。\n"
        )

    def _restructure_refusal_content(self, title: str, query: str,
                                      material_note: str) -> str:
        """有 Key 但 LLM 重构不可用/失败时 web 素材路径的拒绝稿：同一约定。

        与 keyless 拒绝稿是同一个病的两副面孔：web 素材的"整合"是 LLM 的活，
        重构不可用时把骨架/摘要拼贴当成品出厂同样是 FP-1b。KB 抽取式草稿
        不在此列（内容本就叫摘录）。
        """
        return (
            f"# {title}\n\n"
            f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + (f"> 主题: {query}\n" if query else "")
            + f"\n{degradation.RESTRUCTURE_UNAVAILABLE}：{material_note}\n"
            "请检查 prompt 模板（pipelines/prompts/）与 LLM 服务可用性后重新生成。\n"
        )

    def _build_from_articles(self, articles: list[dict], query: str,
                              title: str, template_name: str,
                              task_id: str) -> dict:
        """从完整文章构建文档（先骨架，再填内容）"""
        self.log_info(f"任务 {task_id}: 从 {len(articles)} 篇文章构建文档")

        # Step 1: 读取所有本地文章内容（按相关性降序，优先用相关素材）
        sorted_articles = sorted(
            articles,
            key=lambda a: a.get("relevance", 0) or 0,
            reverse=True,
        )
        article_contents = []
        seen_urls = set()  # 按 URL 去重，避免 pool 研究者返回相同 URL 导致重复
        for art in sorted_articles:
            art_url = art.get("url", "")
            if art_url and art_url in seen_urls:
                continue
            local_path = art.get("local_path", "")
            if local_path and os.path.exists(local_path):
                try:
                    with open(local_path, encoding="utf-8") as f:
                        text = f.read()
                    article_contents.append({
                        "title": art.get("title", ""),
                        "url": art.get("url", ""),
                        "source": art.get("source", ""),
                        "text": text,
                    })
                    if art_url:
                        seen_urls.add(art_url)
                except Exception as e:
                    self.log_warning(f"读取文章失败 {local_path}: {e}")
                    continue

        self.log_info(f"  成功读取 {len(article_contents)}/{len(articles)} 篇文章")

        # 治本（2026-11-10）：无 Key 时 web 素材拼贴=把检索原文当成品出厂
        # （FP-1b 的源头），宁可不交；KB 抽取式草稿是另一条约，不在此列。
        if not self._llm_api_key and article_contents:
            content = self._keyless_refusal_content(
                title, query, f"已读取 {len(article_contents)} 篇素材，无法整合成文")
            self.log_warning(
                f"任务 {task_id}: 无 LLM API Key，{len(article_contents)} 篇素材"
                "无法整合成文，拒绝输出原始拼贴，只交降级自述")
            return {"status": "ok", "task_id": task_id, "content": content,
                    "stats": {"articles_used": len(article_contents), "sections": 0,
                              "word_count": len(content),
                              "char_count": len(content),
                              "empty_sections": [], "keyless_refused": True}}

        # Step 2: 创建文档骨架
        skeleton = self._plan_skeleton(query, title, article_contents)

        # Step 3: 构建最终文档
        content_parts = [f"# {title}", ""]
        content_parts.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if query:
            content_parts.append(f"> 主题: {query}")
        content_parts.append("")

        # 目录
        content_parts.append("## 目录")
        content_parts.append("")
        for sec in skeleton:
            content_parts.append(f"1. [{sec['heading']}](#{sec['anchor']})")
        content_parts.append("")
        content_parts.append("---")
        content_parts.append("")

        # 每个章节填充内容（TF-IDF 语义匹配）
        all_refs = []
        global_seen_paras: set[str] = set()  # 全局段落去重，避免同一段落在多个章节中重复出现
        empty_sections: list[str] = []  # 未能填充内容的章节（降级声明用）
        for sec in skeleton:
            content_parts.append(f"## {sec['heading']}")
            content_parts.append("")
            filled = self._fill_section(sec, article_contents, global_seen_paras)
            if filled["paragraphs"]:
                for para in filled["paragraphs"]:
                    content_parts.append(para)
                    content_parts.append("")
            else:
                content_parts.append(degradation.SECTION_PLACEHOLDER)
                content_parts.append("")
                empty_sections.append(sec.get("heading", ""))
            all_refs.extend(filled["references"])
            content_parts.append("---")
            content_parts.append("")

        # 参考资料
        if all_refs:
            seen_refs = set()
            content_parts.append("## 参考资料")
            content_parts.append("")
            for ref in all_refs:
                if ref["url"] not in seen_refs:
                    seen_refs.add(ref["url"])
                    content_parts.append(f"- [{ref['title']}]({ref['url']})")
            content_parts.append("")

        content = "\n".join(content_parts)

        # 降级声明：无 LLM 时空章节不再静默交付，在文档头部显式标记
        if empty_sections and not self._llm_api_key:
            warn_block = (
                degradation.DEGRADE_BANNER + "：以下章节未能从检索素材中提取到足够内容（当前为占位符）："
                + "、".join(s for s in empty_sections if s)
                + "。建议配置 LLM API Key（复制 .env.example 为 .env）后重新生成。\n\n"
            )
            content = content.replace("## 目录", warn_block + "## 目录", 1)
            self.log_warning(
                f"任务 {task_id}: {len(empty_sections)} 个章节内容不足（降级），已在文档头部声明")

        # LLM 重构：生成严谨技术文档结构（篇-章-节 + mermaid + 表格 + 代码块）
        if query and self._llm_api_key:
            restructured = self._restructure_document(content, article_contents, query, title, task_id=task_id)
            if restructured:
                content = restructured
            else:
                # 窄边界（2026-11-10 第二批）：有 Key 但重构返回 None（模板为空/
                # 加载失败/章节生成全灭）时，骨架+TF-IDF 填充的拼贴不得出厂——
                # 与 keyless 拼贴同一个病（FP-1b 同源），拒绝之。
                content = self._restructure_refusal_content(
                    title, query, f"已读取 {len(article_contents)} 篇素材，重构不可用")
                self.log_warning(
                    f"任务 {task_id}: 有 Key 但 LLM 重构不可用，{len(article_contents)} 篇素材"
                    "无法整合成文，拒绝输出原始拼贴，只交降级自述")
                return {"status": "ok", "task_id": task_id, "content": content,
                        "stats": {"articles_used": len(article_contents), "sections": 0,
                                  "word_count": len(content),
                                  "char_count": len(content),
                                  "empty_sections": [], "restructure_refused": True}}

        # LLM 润色段落衔接（当重构未运行时）
        if query and not content.startswith("# ") and content.count("##") < 2:
            polished = self._polish_with_llm(content, query)
        else:
            polished = content

        self.report(AgentStatus.RUNNING, f"骨架+TF-IDF填充完成，{len(skeleton)} 章节，{len(polished)} 字符")

        return {
            "status": "ok",
            "task_id": task_id,
            "content": polished,
            "stats": {
                "articles_used": len(article_contents),
                "sections": len(skeleton),
                "word_count": len(content),
                "char_count": len(content),
                "empty_sections": empty_sections,
            }
        }

    def _load_prompt_template(self, profile_name: str) -> dict:
        """从 pipelines/prompts/ 加载 YAML prompt 模板"""
        cached = self._prompt_template_cache.get(profile_name)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        yaml_path = next((d / f"{profile_name}.yaml" for d in self._prompts_dirs
                          if (d / f"{profile_name}.yaml").exists()), None)
        if yaml_path is None:
            self.log_warning(f"Prompt 模板不存在: {profile_name}.yaml（搜索 {self._prompts_dirs}），使用内置默认")
            return {}

        try:
            import yaml
            with open(yaml_path, encoding="utf-8") as f:
                template = yaml.safe_load(f)
            self._prompt_template_cache.set(profile_name, template)
            n_sections = len(template.get("sections", []))
            self.log_info(f"加载 prompt 模板: {profile_name} ({n_sections} sections)")
            return template  # type: ignore[no-any-return]
        except Exception as e:
            self.log_error(f"加载 prompt 模板失败: {e}")
            return {}

    # ─── 多轮 LLM 重构：辅助方法（供 _restructure_document 调用） ──────────

    def _enhance_system_prompt_with_feedback(self, system_prompt: str) -> str:
        """质量闭环：读取历史低分模式，附加改进提醒到 system prompt"""
        with contextlib.suppress(Exception):
            from loom_core.quality_feedback import get_quality_feedback
            recs = get_quality_feedback().get_recommendations()
            if recs:
                enhancement = "\n\n【质量改进提醒】基于历史评分数据，以下维度需要加强：\n"
                for r in recs[:3]:
                    enhancement += f"- {r}\n"
                self.log_info(f"质量闭环：应用 {len(recs[:3])} 条改进建议")
                return system_prompt + enhancement
        return system_prompt

    @staticmethod
    def _build_article_context(articles: list[dict]) -> str:
        """构建文章素材摘要（前 3 篇，每篇截断 3000 字符）"""
        summaries = []
        for art in articles[:3]:
            text = (art.get("text") or "")[:3000]
            summaries.append(
                f"--- 文章: {art.get('title','')} ---\n"
                f"来源: {art.get('url','')}\n{text}\n"
            )
        return "\n".join(summaries)

    async def _generate_section_async(self, idx: int, sec_name: str, sec_prompt: str,
                                       system_prompt: str, context: str) -> tuple[int, str, str]:
        """异步生成单个 section，返回 (序号, 章节名, 内容)"""
        part = await self._generate_text_async(sec_prompt, system_prompt, context,
                                                max_tok=4096, time_out=180)
        return (idx, sec_name, part)

    async def _generate_text_async(self, section_prompt: str, system_prompt: str,
                                    context: str, max_tok: int = 4096,
                                    time_out: int = 120) -> str:
        """异步 LLM 生成（优先 LLMRouter 异步接口，回退 _llm_chat_async）"""
        prompt = (
            f"{system_prompt}\n\n"
            f"{section_prompt}\n\n"
            f"素材文章参考（如有）:\n{context[:2000]}\n"
        )
        try:
            try:
                from loom_core.llm_router import get_router
                router = get_router()
                if router and router.get_active_providers():
                    content, provider = await router.chat_async(
                        [{"role": "user", "content": prompt}],
                        max_tokens=max_tok, temperature=0.3, timeout=time_out,
                    )
                    return content.strip() if content else ""
            except Exception as e:
                self.log_debug(f"LLMRouter 异步不可用，回退: {e}")

            text = await self._llm_chat_async(
                [{"role": "user", "content": prompt}],
                max_tokens=max_tok, temperature=0.3, timeout=time_out,
            )
            return text.strip()
        except Exception as e:
            self.log_warning(f"LLM 异步生成异常: {e}")
            return ""

    def _clean_mermaid(self, text: str) -> str:
        """修复常见的 Mermaid 语法问题 + 删除孤立的 end"""
        def fix_mermaid_block(m):
            block = m.group(0)
            lines = block.split("\n")
            in_block = False
            for i, line in enumerate(lines):
                if "```mermaid" in line:
                    in_block = True
                    continue
                if in_block and "```" in line:
                    break
                if in_block and line.strip():
                    first = line.strip()
                    if not any(first.startswith(kw) for kw in
                            ["graph", "sequenceDiagram", "flowchart", "classDiagram",
                             "stateDiagram", "erDiagram", "gantt", "pie"]):
                        lines[i] = "graph TD  " + first
                    break
            subgraph_count = 0
            cleaned = []
            for line in lines:
                s = line.strip()
                if s.startswith("subgraph"):
                    subgraph_count += 1
                    cleaned.append(line)
                elif s == "end":
                    if subgraph_count > 0:
                        subgraph_count -= 1
                        cleaned.append(line)
                else:
                    cleaned.append(line)
            while subgraph_count > 0:
                cleaned.append("    end")
                subgraph_count -= 1
            return "\n".join(cleaned)
        text = re.sub(r"```mermaid.*?```", fix_mermaid_block, text, flags=re.DOTALL)
        def fix_br(m):
            return m.group(0).replace("<br/>", "<br")
        text = re.sub(r"```mermaid.*?```", fix_br, text, flags=re.DOTALL)
        return text

    @staticmethod
    def _fix_references(text: str) -> str:
        """删除空的参考资料段落"""
        text = re.sub(r"\n## 参考资料\n\s*$", "", text)
        text = re.sub(r"\n## 参考资料\s*\n(?=## )", "\n", text)
        return text.strip()

    @staticmethod
    def _remove_truncated_mermaid_blocks(text: str) -> str:
        """删除截断的 Mermaid 块（只有开标签没闭标签）"""
        lines = text.split("\n")
        in_mermaid = False
        final_lines = []
        for line in lines:
            if "```mermaid" in line:
                in_mermaid = True
                final_lines.append(line)
            elif in_mermaid and "```" in line:
                in_mermaid = False
                final_lines.append(line)
            elif in_mermaid:
                final_lines.append(line)
            else:
                final_lines.append(line)
        if in_mermaid:
            for i in range(len(final_lines) - 1, -1, -1):
                if "```mermaid" in final_lines[i]:
                    final_lines = final_lines[:i]
                    break
        return "\n".join(final_lines)

    def _assemble_sections(self, results_parallel: list[tuple[int, str, str]],
                            title: str,
                            stream_callback: StreamCallback | None) -> str | None:
        """按序号拼接各 section，做 Mermaid/参考资料修复，返回最终文档（全失败返回 None）"""
        parts = []
        for idx, sec_name, part in sorted(results_parallel, key=lambda x: x[0]):
            if part:
                self.log_info(f"R{idx+1} [{sec_name}] 完成: {len(part)} 字符")
                parts.append(part)
                if stream_callback:
                    stream_callback.on_section(idx, sec_name, part)
            else:
                self.log_info(f"R{idx+1} [{sec_name}] 失败，跳过")

        if not parts:
            return None

        final = parts[0]
        for p in parts[1:]:
            final += "\n\n" + p

        final = self._remove_truncated_mermaid_blocks(final)
        final = self._clean_mermaid(final)
        final = self._fix_references(final)

        if not final.startswith(f"# {title}"):
            final = f"# {title}\n\n" + final

        self.log_info(f"多轮拼接完成: {len(final)} 字符")
        return final

    def _restructure_document(self, content: str, articles: list[dict],
                               query: str, title: str,
                               stream_callback: StreamCallback | None = None,
                               task_id: str = "") -> str | None:
        """多轮 LLM 生成：每轮一篇，逐步拼接成完整文档（prompt 从外部模板加载）"""
        if not self._llm_api_key:
            return None
        # P2 修复：按 task_id 从注册表拾取回调（并发流互不串扰），未命中回退旧单槽
        if stream_callback is None:
            stream_callback = self._get_stream_callback(task_id)

        # C11：缓存 key 必须覆盖影响产出的全部输入。原 key 只含 query+title，
        # 同题不同素材（articles）、不同 prompt profile / 质量反馈状态都会命中
        # 错误缓存，返回与当前检索结果无关的旧文档。key 覆盖：
        # query + title + 增强后的 system_prompt（含 profile 与质量反馈）+ 素材 context。
        template = self._load_prompt_template(self._prompt_profile)
        base_system_prompt = template.get("system_prompt", "")
        sections = template.get("sections", [])

        if not sections:
            self.log_warning("Prompt 模板为空或加载失败，跳过 LLM 重构")
            return None

        # 素材摘要（并入缓存 key）
        context = self._build_article_context(articles)

        # 质量闭环增强 prompt（随反馈状态变化，并入缓存 key）
        system_prompt = self._enhance_system_prompt_with_feedback(base_system_prompt)

        cache_key_short = hashlib.sha256(
            (query + "\x00" + title[:50] + "\x00" + system_prompt + "\x00" + context).encode()
        ).hexdigest()
        cached = self._restructure_cache.get(cache_key_short)
        if cached is not None:
            self.log_info("LLM 重构缓存命中，跳过")
            return cached  # type: ignore[no-any-return]

        # 并行生成：真异步并发所有 sections（aiohttp + asyncio.gather）
        sec_specs = []
        for i, sec in enumerate(sections):
            sec_name = sec.get("name", f"Section {i+1}")
            sec_prompt = sec.get("prompt", "")
            sec_prompt = sec_prompt.replace("{title}", title).replace("{query}", query)
            sec_specs.append((i, sec_name, sec_prompt))

        # 使用 asyncio 真并行（创建新事件循环运行）
        # 注意：gather 必须在运行中的 loop 内调用 —— 在 loop 外调用时
        # ensure_future 会因主/工作线程无 current event loop 抛 RuntimeError
        async def _gather_all() -> list[tuple[int, str, str]]:
            return await asyncio.gather(*[
                self._generate_section_async(i, sn, sp, system_prompt, context)
                for i, sn, sp in sec_specs
            ])

        loop = asyncio.new_event_loop()
        try:
            results_parallel = loop.run_until_complete(_gather_all())
        finally:
            loop.close()

        final = self._assemble_sections(results_parallel, title, stream_callback)
        if final is None:
            return None
        self._restructure_cache.set(cache_key_short, final)
        return final

    def _plan_skeleton(self, query: str, title: str,
                       articles: list[dict]) -> list[dict]:
        """根据查询和文章内容规划文档骨架"""
        if not query:
            return [
                {"heading": "概述", "anchor": "概述", "keywords": []},
                {"heading": "详细内容", "anchor": "详细内容", "keywords": []},
                {"heading": "总结", "anchor": "总结", "keywords": []},
            ]

        query_kws = re.findall(r'[\w\u4e00-\u9fff]+', query)
        skeleton = []

        skeleton.append({
            "heading": "简介",
            "anchor": "简介",
            "keywords": query_kws[:3] + ["介绍", "概述", "什么是"],
        })

        core_kws = [kw for kw in query_kws if len(kw) > 1 and kw not in ("的", "是", "在", "了")]
        if core_kws:
            skeleton.append({
                "heading": "核心概念",
                "anchor": "核心概念",
                "keywords": core_kws + ["概念", "定义", "原理", "机制"],
            })

        # 从文章中自动提取主题
        content_kws = set()
        for art in articles:
            text = art.get("text", "")
            paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]
            for p in paras[:30]:
                if any(kw.lower() in p.lower() for kw in query_kws):
                    extra = re.findall(r'[\w\u4e00-\u9fff]{2,}', p)
                    content_kws.update(extra[:5])
        if content_kws:
            skeleton.append({
                "heading": "详细分析",
                "anchor": "详细分析",
                "keywords": list(content_kws)[:8],
            })

        skeleton.append({
            "heading": "实践与应用",
            "anchor": "实践与应用",
            "keywords": ["实践", "应用", "示例", "用法", "教程", "案例"],
        })
        skeleton.append({
            "heading": "总结",
            "anchor": "总结",
            "keywords": ["总结", "结论", "未来", "展望", "发展趋势"],
        })

        return skeleton

    def _fill_section(self, section: dict,
                      articles: list[dict],
                      global_seen: set | None = None) -> dict:
        """从文章中提取与章节相关的段落（TF-IDF 语义匹配）"""
        keywords = section.get("keywords", [])
        paragraphs = []
        seen_para_hashes = global_seen if global_seen is not None else set()

        for art in articles:
            text = art.get("text", "")
            title = art.get("title", "")
            url = art.get("url", "")

            raw_paras = re.split(r'\n\s*\n', text)
            for para in raw_paras:
                p = para.strip()
                if len(p) < 40:
                    continue
                if len(p) > 3000:
                    p = p[:3000] + "..."
                if re.search(r'^(navigation|menu|footer|copyright|©|广告|推荐|热门|相关文章)',
                             p, re.IGNORECASE):
                    continue
                # 段落内容去重：取前 200 字的 hash 作为签名
                para_sig = hashlib.sha256(p[:200].encode()).hexdigest()
                if para_sig in seen_para_hashes:
                    continue
                seen_para_hashes.add(para_sig)
                paragraphs.append({"text": p, "title": title, "url": url})

        if not paragraphs:
            return {"paragraphs": [], "references": []}

        # TF-IDF 向量语义匹配
        scored = self._semantic_rank(paragraphs, keywords)
        top = scored[:8]

        result_paras = [p["text"] for p in top]
        result_refs = []
        seen_urls = set()
        for p in top:
            if p["url"] and p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                result_refs.append({"title": p["title"], "url": p["url"]})

        return {"paragraphs": result_paras, "references": result_refs}

    def _semantic_rank(self, paragraphs: list[dict],
                       keywords: list[str]) -> list[dict]:
        """用 TF-IDF + 余弦相似度对段落排序（稀疏向量优化版）

        性能优化：
          - 稀疏字典表示替代密集列表（500 段 x 700 词 → 仅存非零项）
          - 单次遍历构建 TF + DF（减少一轮 O(n_docs * n_terms) 循环）
          - 查询向量仅含 keywords 对应的非零项（点积只遍历稀疏项）
        """
        import math
        if not keywords:
            return paragraphs[:8]

        # 1. 分词（英文单词 + 中文 run/2-gram，见 _tokenize_for_tfidf）
        all_docs = []
        for p in paragraphs:
            all_docs.append(_tokenize_for_tfidf(p["text"]))

        # 查询侧同样展开为 token 集合：中文关键词（如"核心架构"）需生成
        # 2-gram 才能与文档侧 2-gram 命中
        query_words: set[str] = set()
        for kw in keywords:
            if len(kw) > 1:
                query_words.update(_tokenize_for_tfidf(kw.lower()))
        n_docs = len(all_docs)
        if n_docs == 0:
            return paragraphs[:8]

        # 2. 稀疏 TF + 文档频率（单次遍历）
        sparse_tf: list[dict[str, float]] = []  # 每篇文档的非零 TF
        df: dict[str, int] = {}         # word -> 出现在多少篇文档中

        for doc in all_docs:
            if not doc:
                sparse_tf.append({})
                continue
            tf_counter: dict[str, int] = {}
            for w in doc:
                tf_counter[w] = tf_counter.get(w, 0) + 1
            max_tf = max(tf_counter.values())
            normalized = {w: cnt / max_tf for w, cnt in tf_counter.items()}
            sparse_tf.append(normalized)
            for w in normalized:
                df[w] = df.get(w, 0) + 1

        # 3. IDF（仅计算出现在文档中的词）
        idf = {w: math.log((n_docs + 1) / (freq + 1)) + 1 for w, freq in df.items()}

        # 4. TF-IDF 稀疏向量 + 文档范数
        sparse_tfidf = []
        doc_norms = []
        for tf_dict in sparse_tf:
            tfidf_dict = {}
            norm_sq = 0.0
            for w, tf_val in tf_dict.items():
                val = tf_val * idf.get(w, 1.0)
                tfidf_dict[w] = val
                norm_sq += val * val
            sparse_tfidf.append(tfidf_dict)
            doc_norms.append(math.sqrt(norm_sq) if norm_sq > 0 else 0.0)

        # 5. 余弦相似度（仅遍历查询词对应的稀疏项）
        similarities = []
        for i in range(n_docs):
            if doc_norms[i] == 0:
                similarities.append(0.0)
                continue
            dot = sum(sparse_tfidf[i].get(w, 0.0) for w in query_words)
            # 查询向量范数 = sqrt(len(query_words))（每个权重为 1）
            query_norm = math.sqrt(len(query_words))
            similarities.append(dot / (doc_norms[i] * query_norm) if query_norm > 0 else 0.0)

        # 6. 排序
        sorted_indices = sorted(range(n_docs), key=lambda x: -similarities[x])
        scored = []
        for idx in sorted_indices:
            score = similarities[idx]
            if score > 0.01:
                scored.append({**paragraphs[idx], "score": score})

        if not scored:
            return paragraphs[:3]

        return scored

    # ═══════════════════════════════════════════════
    # 原有逻辑：搜索摘要模式
    # ═══════════════════════════════════════════════

    def _to_chunks(self, results: list[dict]) -> list[ContentChunk]:
        """转换为内容块"""
        chunks = []
        for r in results:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            url = r.get("url", "")
            source = r.get("source", "")
            keywords = self._extract_keywords(title + " " + snippet)
            chunks.append(ContentChunk(
                title=title, content=snippet, source=source,
                url=url, keywords=keywords,
            ))
        return chunks

    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词（简单实现）"""
        text = re.sub(r'[^\w\s]', ' ', text)
        words = text.lower().split()
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                     "的", "是", "在", "和", "了", "有", "与", "及", "等"}
        word_freq = defaultdict(int)  # type: ignore[var-annotated]
        for word in words:
            if len(word) > 2 and word not in stopwords:
                word_freq[word] += 1
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_words[:10]]

    def _classify_chunks(self, chunks: list[ContentChunk]) -> list[ContentChunk]:
        """将内容块分类到合适的章节"""
        for chunk in chunks:
            text = (chunk.title + " " + chunk.content).lower()
            best_section = "其他"
            best_score = 0
            for section, kws in self._section_scores.items():
                score = sum(1 for kw in kws if kw.lower() in text)
                if score > best_score:
                    best_score = score
                    best_section = section
            chunk.section = best_section
            chunk.relevance_score = best_score
        return chunks

    def _deduplicate_chunks(self, chunks: list[ContentChunk]) -> list[ContentChunk]:
        """去重"""
        seen = set()
        unique = []
        for chunk in chunks:
            key = chunk.title.strip().lower()[:50]
            if key and key not in seen:
                seen.add(key)
                unique.append(chunk)
        return unique

    def _generate_document(self, chunks: list[ContentChunk],
                           template_name: str, title: str) -> str:
        """生成文档"""
        sections = defaultdict(list)
        for chunk in chunks:
            sections[chunk.section].append(chunk)

        section_data = []
        all_refs = []
        for sec_title in self.SECTION_TITLES:
            sec_chunks = sections.get(sec_title, [])
            if not sec_chunks:
                continue
            sec_content_parts = []
            for c in sec_chunks:
                sec_content_parts.append(f"### {c.title}\n\n{c.content}")
                if c.url and c.title:
                    all_refs.append({"title": c.title, "url": c.url})
            section_data.append({"title": sec_title, "content": "\n\n".join(sec_content_parts)})

        return self._template_mgr.render(template_name, title, section_data, all_refs)  # type: ignore[no-any-return]

    def _expire_stale_pending(self):
        """清理过期的 pending 结果"""
        now = time.time()
        with self._pending_lock:
            stale = []
            for tid, entry in self.pending_results.items():
                if isinstance(entry, tuple) and len(entry) == 2:
                    ts, _ = entry
                    if now - ts > self._pending_expire_secs:
                        stale.append(tid)
            for tid in stale:
                self.pending_results.pop(tid, None)

    def handle_streaming(self, msg: Message, callback: StreamCallback) -> dict:
        """流式处理 —— 逐章节通过 callback 推送内容，最终返回完整结果。

        与 handle() 的区别：生成过程中通过 callback 实时推送每个章节，
        而非等全部生成完才一次性返回。适用于 SSE / WebSocket 场景。
        """
        payload = msg.payload
        task_id = payload.get("task_id", "")
        query = payload.get("query", "")
        if not query:
            queries = payload.get("queries", [])
            query = queries[0] if queries else ""
        title = payload.get("title", "自动生成文档")

        # 通知开始
        template = self._load_prompt_template(self._prompt_profile)
        sections = template.get("sections", [])
        callback.on_start(len(sections), title)

        # P2：注册回调供 _restructure_document 按 task_id 拾取（并发流各自命中）
        self._register_stream_callback(task_id, callback)

        try:
            # 调用 handle 执行正常流程（_restructure_document 内部会触发 callback）
            result = self.handle(msg)
            content = result.get("content", "") if result else ""

            # 通知完成
            callback.on_complete(content, result.get("stats", {}) if result else {})
            return result or {"status": "ok", "task_id": task_id, "content": content}

        except Exception as e:
            callback.on_error(str(e))
            return {"status": "error", "task_id": task_id, "error": str(e)}
        finally:
            self._unregister_stream_callback(task_id)
