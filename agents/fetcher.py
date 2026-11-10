"""
Fetcher Agent v1 - 知识内容获取器
=================================
功能：
  - 从搜索结果 URL 下载完整文章内容
  - 提取正文（去 HTML 标签、导航、广告）
  - 保存到本地临时文本文件
  - 内容质量识别：可用 vs 不可用
  - 返回处理后内容的元数据
"""
import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from artesian import selectolax_compat
from artesian.url_guard import validate_public_http_url
from loom_core.base_agent import AgentStatus, BaseAgent, Message

# Async I/O 支持（可选）
try:
    import aiohttp
    USE_ASYNC = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    USE_ASYNC = False

AGENT_NAME = "fetcher"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "max_downloads": ('int', 15),
    "temp_dir": ('str', 'tmp_fetcher'),
    "download_workers": ('int', 5),
}
AGENT_VERSION = "1.0"
AGENT_DESC = "知识内容获取器 - 下载、提取正文、保存到本地、质量识别"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 20
INPUT_TOPICS = ["fetcher.input", "researcher.done"]
OUTPUT_TOPICS = ["fetcher.done", "fetcher.progress"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：把每条检索结果抓成完整文章
PRODUCES = {"articles": "list"}
CONSUMES = ["results"]
DEPENDENCIES = ["researcher"]
CACHE_TTL = 0
RESPAWN = False

# 不可用内容的关键词/模式
BAD_PATTERNS = [
    r"404|not found|page not found|页面不存在",
    r"captcha|验证码|security check|安全验证",
    r"access denied|access forbidden|403|access blocked",
    r"please enable javascript|请启用 javascript",
    r"too many requests|请求过于频繁",
    r"under maintenance|维护中",
]
# 预编译：_extract_text / _extract_text_regex 每页面必经，段落循环内
# 逐块调用密度/链接正则，内联 re.sub/findall 每次都过模块缓存查找
_BAD_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in BAD_PATTERNS]
_RE_STRIP_TAGS = re.compile(r"<[^>]+>")
_RE_NAMED_ENTITY = re.compile(r"&[a-zA-Z]+;")
_RE_NUM_ENTITY = re.compile(r"&#\d+;")
_RE_HEX_ENTITY = re.compile(r"&#x[0-9a-fA-F]+;")
_RE_NOISE_CHARS = re.compile(r"[¶§†‡•·]")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_WHITESPACE_ALL = re.compile(r"\s")
_RE_URL_SCHEME = re.compile(r"https?://")
_RE_BLOCK_SPLIT = re.compile(
    r"</?(p|div|section|li|td|article|main|blockquote|h[1-6])\b[^>]*>",
    re.IGNORECASE)
_RE_PRE_BLOCK = re.compile(
    r"<pre\b[^>]*>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
_RE_CODE_BLOCK_LANG = re.compile(
    r"<code\b[^>]*\bclass\s*=\s*[\"'][^\"']*"
    r"(?:language|lang)-([a-zA-Z0-9_+#-]+)[^\"']*[\"'][^>]*>",
    re.IGNORECASE)
_RE_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_FENCE_IN_CODE = re.compile(r"^[ \t]*`{3,}[ \t]*\w*$", re.MULTILINE)
_RE_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_REMOVE_AREAS = {tag: re.compile(
    rf"<{tag}[^>]*>.*?</{tag}>", re.DOTALL | re.IGNORECASE)
    for tag in ("script", "style", "noscript", "svg", "head")}
_RE_MAIN_CONTAINER = re.compile(
    r"<(article|main)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
# _is_content_usable / _content_relevance 的查询侧 token 提取
_RE_EN_WORD = re.compile(r"[a-zA-Z]{3,}")
_RE_QUERY_TOKEN = re.compile(r"[\w\u4e00-\u9fff]{2,}")

# 最小可用内容长度
MIN_CONTENT_LENGTH = 200

# 标题标签：按结构信号处理，不受正文长度/密度门槛约束
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# 标题长度上限：超过此长度多半是被误标的长段落（如整段正文塞进 h1），丢弃
_MAX_HEADING_LEN = 120


def _as_markdown_heading(tag: str, text: str) -> str:
    """把 <hN> 文本转成 Markdown 标题，保留层级语义。

    标题原先与普通段落同等对待，短标题（<30 字符）被直接丢弃，
    导致 h1/h2 语义在提取阶段就丢失——下游渲染层再强也拿不到标题。
    转成 Markdown 标记后，标题语义可穿透到 writer / quality_gate / 渲染层。
    """
    level = int(tag[1]) if tag[1:].isdigit() else 1
    return f"{'#' * min(max(level, 1), 6)} {text}"
# 大文章阈值：超过此长度的内容改为文件引用传递（减少消息总线内存压力）
LARGE_ARTICLE_THRESHOLD = 50000  # 50KB
# 最大下载数
MAX_DOWNLOADS = 20
# 单页面超时
PAGE_TIMEOUT = 10
# 手动重定向上限（每跳重新过 SSRF 校验）
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class _DownloadAbort(Exception):
    """不可恢复的下载终止（SSRF 拒绝 / 终态 HTTP 状态），不重试"""


class _DownloadRetry(Exception):
    """临时性下载失败，可重试"""


def _unescape_html(text: str) -> str:
    """HTML 实体反转义（命名/数字/十六进制三类）。

    代码块里&nbsp;/&lt;/&gt;/&amp; 很常见，不还原会让代码完全不可读
    （`if a&lt;b` 这种）。只做这四类常见实体，避免引入 html.unescape
    对 &copy 等符号的展开污染正文。
    """
    def _num(m):
        try:
            return chr(int(m.group(1), 10))
        except (ValueError, OverflowError):
            return m.group(0)

    def _hex(m):
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    text = re.sub(r"&#x([0-9a-fA-F]+);", _hex, text)
    text = re.sub(r"&#(\d+);", _num, text)
    for entity, char in (
        ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
        ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "),
        ("&amp;", "&"),
    ):
        text = text.replace(entity, char)
    # 残余命名实体（&copy; 等）→ 去掉分号留裸文本，避免污染代码
    text = re.sub(r"&[a-zA-Z]{2,8};", "", text)
    return text


def _extract_pre_code(raw: str) -> str:
    """把一个 <pre> 块的内层 HTML 转成 Markdown 围栏代码块。

    关键点：**保留换行与缩进**。原实现用 separator=" " / \\s+→" " 把
    <pre> 和普通段落同等对待，导致 `asyncio.run(main())` 被压成
    `asyncio . run ( main ( ) )`，代码彻底不可读，且下游任何格式
    转换（docx/pdf）都无法还原排版。
    """
    inner = raw

    # <br> 在 <pre> 里是显式换行
    inner = _RE_BR_TAG.sub("\n", inner)
    # 保留 <br> 之外的标签作为分隔（span/div 在 <pre> 里常用于语法高亮分段）
    inner = _RE_STRIP_TAGS.sub("", inner)
    inner = _unescape_html(inner)

    # 只做行尾空白清理，保留行首缩进（缩进即语义）
    inner = _RE_TRAILING_WS.sub("", inner)
    inner = inner.replace("\r\n", "\n").replace("\r", "\n").strip("\n")

    if not inner.strip():
        return ""

    # 语言标注：<pre><code class="language-python">
    lang = ""
    m = _RE_CODE_BLOCK_LANG.search(raw)
    if m:
        lang = re.sub(r"[^a-zA-Z0-9_+#-]", "", m.group(1))[:20]

    # 代码自身含 ``` 围栏时，用更长的围栏避免提前闭合
    longest = max(
        (len(m.group(1)) for m in re.finditer(r"^([`]+)", inner, re.MULTILINE)),
        default=0,
    )
    fence = "`" * max(3, longest + 1)

    return f"{fence}{lang}\n{inner}\n{fence}"


def _harvest_pre_blocks(html: str) -> tuple[str, str]:
    """摘出所有 <pre> 代码块并转成 Markdown，同时把它们从 HTML 中移除。

    返回 (代码块文本, 剩余HTML)。代码块不再参与后续的段落密度筛选
    （代码的 text_ratio 天然偏低，且会被 _RE_WHITESPACE 压平）。
    """
    blocks: list[str] = []
    seen: set[str] = set()

    def _take(m):
        code = _extract_pre_code(m.group(1))
        if code and code not in seen:      # 去重：同一段代码常被多处嵌入
            seen.add(code)
            blocks.append(code)
        return " "  # 原位留空，避免相邻文本粘连成一个词

    rest = _RE_PRE_BLOCK.sub(_take, html)
    return ("\n\n".join(blocks), rest) if blocks else ("", html)


def _dedupe_blocks(blocks: list[str]) -> list[str]:
    """去掉"父块文本被子块完全包含"的冗余块。

    CSS 选择器里同时匹配了容器标签（article/section/div）和它们的子节点
    （p/li），父节点的 text() 本身就是所有子节点文本的拼接，
    于是同一段文字会被收集两遍（表现为正文中段内容重复）。
    这里按长度降序保留，被更长块完全包含的短块直接丢弃；
    容器节点文本通常更长，故保留容器、丢弃子节点，语义等价。

    以 '#' 开头的 Markdown 标题块不参与去重：短标题（"## 概述"）很容易
    被正文里的同名片段包含（例如正文提到"…概述…"），但标题是结构信号，
    删掉就丢了层级语义。
    """
    headings = [b for b in blocks if b.startswith("#")]
    bodies = [b for b in blocks if not b.startswith("#")]

    kept: list[str] = []
    kept_pos: dict[str, int] = {}
    for idx, blk in sorted(
        ((i, b) for i, b in enumerate(bodies) if not b.startswith("#")),
        key=lambda pair: len(pair[1]), reverse=True,
    ):
        if any(blk in k for k in kept):
            continue
        kept.append(blk)
        kept_pos[blk] = idx

    kept.sort(key=lambda b: kept_pos[b])

    # 标题与正文按各自在原文中的位置交织回单序列
    merged: list[str] = []
    hi = 0
    bi = 0
    h_pos = [i for i, b in enumerate(blocks) if b.startswith("#")]
    b_pos = [kept_pos[b] for b in kept]
    while hi < len(headings) or bi < len(kept):
        h = h_pos[hi] if hi < len(h_pos) else len(blocks) + 1
        b = b_pos[bi] if bi < len(b_pos) else len(blocks) + 1
        if h <= b:
            merged.append(headings[hi])
            hi += 1
        else:
            merged.append(kept[bi])
            bi += 1
    return merged


def _merge_code(text: str, code_text: str) -> str:
    """把代码块拼回正文尾部。

    放尾部而非原位插入：代码块在原文中的位置需要完整的 DOM/流上下文才能
    准确还原（<pre> 常嵌在 <li>/<td> 里），而下游（writer 的 TF-IDF 匹配、
    quality_gate 的评分）只关心内容完整性，不关心代码在正文中的精确位置。
    """
    if not code_text:
        return text
    if not text.strip():
        return code_text
    return f"{text.rstrip()}\n\n{code_text}"


class FetcherAgent(BaseAgent):
    """知识内容获取器"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._temp_dir = Path(config.get("temp_dir", tempfile.gettempdir())) / "loom_fetcher"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        # 多 UA 池：规避基础反爬
        self._ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        ]
        self._base_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._retry = int(config.get("retry", 2))
        # 下载量统计
        self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}
        self._stats_lock = threading.Lock()
        # aiohttp session 按调用创建/关闭（不跨事件循环复用），此字段仅保留兼容引用
        self._aio_session: aiohttp.ClientSession | None = None
        # HTML 解析后端：C 内核（modest/lexbor/quick）可用则用它，否则正则启发式。
        # 降级必须是可见信号（_parser_fallbacks + warning），不能再静默换路径。
        self._parser_backend = selectolax_compat.resolve_backend() or "regex"
        self._parser_fallbacks = 0
        # Firecrawl 网页提取增强（可选，优先于 HTML 下载 + 正则提取）
        from artesian.search_engines import FirecrawlExtractor
        self._firecrawl = FirecrawlExtractor(
            api_key=config.get("firecrawl_api_key", "")
        )
        self.log_info(f"Fetcher v{AGENT_VERSION} 初始化完成，临时目录: {self._temp_dir}"
                      f" | Async I/O: {'启用' if USE_ASYNC else '未安装 aiohttp，使用同步模式'}"
                      f" | HTML解析: {self._parser_backend}"
                      f" | Firecrawl: {'启用' if self._firecrawl.is_available() else '未配置'}"
                      f" | UA池: {len(self._ua_pool)} | 重试: {self._retry}")

    def handle(self, msg: Message) -> dict | None:
        """处理搜索结果，下载文章内容"""
        self.report(AgentStatus.RUNNING, "开始获取文章内容...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        query = payload.get("query", payload.get("queries", [""])[0] if payload.get("queries") else "")
        results = payload.get("results", [])
        max_downloads = payload.get("max_downloads", MAX_DOWNLOADS)

        if not results:
            self.log_info(f"任务 {task_id}: 无搜索结果")
            self.cleanup_task_temp(task_id)
            return {"status": "ok", "task_id": task_id, "articles": [], "query": query}

        results = results[:max_downloads]
        self.log_info(f"任务 {task_id}: 开始下载 {len(results)}/{len(results)} 个页面")

        # 为这个任务创建独立的子目录
        task_dir = self._temp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        with self._stats_lock:
            self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}

        use_async = USE_ASYNC
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop()
            use_async = False
        if use_async:
            articles = asyncio.run(self._fetch_all_async(results, query, task_id, task_dir))
        else:
            articles = self._fetch_all_sync(results, query, task_id, task_dir)

        with self._stats_lock:
            stats_snapshot = dict(self._stats)

        self.log_info(f"任务 {task_id}: 下载完成 ({stats_snapshot['success']}成功/{stats_snapshot['failed']}失败/{stats_snapshot['filtered']}过滤)")

        result = {
            "status": "ok",
            "task_id": task_id,
            "query": query,
            "articles": articles,
            "stats": {
                "total": len(results),
                "attempted": stats_snapshot["attempted"],
                "success": stats_snapshot["success"],
                "failed": stats_snapshot["failed"],
                "filtered": stats_snapshot["filtered"],
            },
        }
        # 注意：不在此处清理临时文件，因为下游 agent (writer) 需要读取 local_path。
        # 临时文件由 orchestrator 在流水线完成后统一清理（cleanup_stale_temp）。
        return result

    async def handle_async(self, msg: Message) -> dict | None:
        """异步处理入口 —— 供异步调用方直接 await，避免 asyncio.run 嵌套。

        与 handle() 逻辑相同，但在已有事件循环中使用。
        """
        self.report(AgentStatus.RUNNING, "开始获取文章内容...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        query = payload.get("query", payload.get("queries", [""])[0] if payload.get("queries") else "")
        results = payload.get("results", [])
        max_downloads = payload.get("max_downloads", MAX_DOWNLOADS)

        if not results:
            self.log_info(f"任务 {task_id}: 无搜索结果")
            self.cleanup_task_temp(task_id)
            return {"status": "ok", "task_id": task_id, "articles": [], "query": query}

        results = results[:max_downloads]
        self.log_info(f"任务 {task_id}: 开始下载 {len(results)} 个页面")

        task_dir = self._temp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        with self._stats_lock:
            self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}

        if USE_ASYNC:
            articles = await self._fetch_all_async(results, query, task_id, task_dir)
        else:
            articles = await asyncio.to_thread(
                self._fetch_all_sync, results, query, task_id, task_dir
            )

        with self._stats_lock:
            stats_snapshot = dict(self._stats)

        self.log_info(f"任务 {task_id}: 下载完成 ({stats_snapshot['success']}成功/{stats_snapshot['failed']}失败/{stats_snapshot['filtered']}过滤)")

        return {
            "status": "ok",
            "task_id": task_id,
            "query": query,
            "articles": articles,
            "stats": {
                "total": len(results),
                "attempted": stats_snapshot["attempted"],
                "success": stats_snapshot["success"],
                "failed": stats_snapshot["failed"],
                "filtered": stats_snapshot["filtered"],
            },
        }

    def _fetch_all_sync(self, results: list, query: str, task_id: str, task_dir: Path) -> list:
        """同步模式：线程池并发下载"""
        from concurrent.futures import ThreadPoolExecutor

        workers = self.config.get("download_workers", 5)
        articles = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(self._fetch_article, r, query, task_id, task_dir, i)
                for i, r in enumerate(results)
            ]
            for i, fut in enumerate(futures):
                self.report(AgentStatus.RUNNING, f"[{i+1}/{len(results)}] 下载中...")
                article = fut.result()
                if article:
                    articles.append(article)
        return articles

    async def _fetch_all_async(self, results: list, query: str, task_id: str, task_dir: Path) -> list:
        """异步模式：aiohttp 并发下载（session 每次调用内创建，finally 关闭，避免跨 loop 失效）"""
        workers = self.config.get("download_workers", 5)
        semaphore = asyncio.Semaphore(workers)
        timeout = aiohttp.ClientTimeout(total=PAGE_TIMEOUT)
        connector = aiohttp.TCPConnector(limit=20, limit_per_host=5)
        session = aiohttp.ClientSession(
            headers=self._base_headers, timeout=timeout, connector=connector
        )
        try:
            tasks = [
                self._fetch_article_async(session, semaphore, r, query, task_id, task_dir, idx)
                for idx, r in enumerate(results)
            ]
            articles = await asyncio.gather(*tasks)
        finally:
            await session.close()
        return [a for a in articles if a]

    async def _fetch_article_async(self, session, semaphore, result, query, task_id, task_dir, idx=0):
        """异步下载单页（带 UA 轮换 + 重试）

        提取策略（优先级从高到低）：
          1. Firecrawl API（若已配置）— URL → Markdown，JS 渲染，质量最好
          2. aiohttp 下载 HTML → selectolax/正则 提取正文
        """
        url = result.get("url", "") if isinstance(result, dict) else getattr(result, "url", "")
        title = result.get("title", "") if isinstance(result, dict) else getattr(result, "title", "")
        if not url:
            return None
        ok, reason = validate_public_http_url(url)
        if not ok:
            self.log_debug(f"  跳过不安全 URL({reason}): {str(url)[:60]}")
            return None

        with self._stats_lock:
            self._stats["attempted"] += 1

        # ── 优先：Firecrawl 提取 ──
        if self._firecrawl.is_available():
            try:
                # W10：调用方 gather 全量并发且仅在 _download_html_async 内限流，
                # Firecrawl 调用若不持信号量会绕过并发上限（打爆 API 速率/成本）
                async with semaphore:
                    fc_result = await asyncio.to_thread(self._firecrawl.scrape, url, 30)
                if fc_result["success"]:
                    plain_text = fc_result["markdown"]
                    if fc_result["title"] and not title:
                        title = fc_result["title"]
                    if self._is_content_usable(plain_text, url, query):
                        article = self._save_article(
                            title, url, plain_text, query, task_dir
                        )
                        if article:
                            with self._stats_lock:
                                self._stats["success"] += 1
                            return article
                    else:
                        with self._stats_lock:
                            self._stats["filtered"] += 1
                        self.log_debug(f"  Firecrawl 内容不可用: {url[:60]}")
                        # 继续尝试 HTML 下载
                else:
                    self.log_debug(f"  Firecrawl 失败({fc_result['error'][:40]}): {url[:60]}")
            except Exception as e:
                self.log_debug(f"  Firecrawl 异常: {e}")

        # ── 回退：aiohttp HTML 下载 + 正文提取 ──
        last_err = None
        for attempt in range(self._retry + 1):
            ua = self._ua_pool[(idx + attempt) % len(self._ua_pool)]
            try:
                headers = {**self._base_headers, "User-Agent": ua}
                html = await self._download_html_async(session, semaphore, url, headers)
                plain_text = self._extract_text(html)

                if not self._is_content_usable(plain_text, url, query):
                    with self._stats_lock:
                        self._stats["filtered"] += 1
                    self.log_debug(f"  过滤不可用内容: {url[:60]}")
                    return None

                article = self._save_article(title, url, plain_text, query, task_dir)
                if article:
                    with self._stats_lock:
                        self._stats["success"] += 1
                    return article
            except _DownloadAbort as e:
                with self._stats_lock:
                    self._stats["failed"] += 1
                self.log_debug(f"  下载中止(安全拒绝): {url[:60]} - {e}")
                return None
            except TimeoutError:
                last_err = "超时"
                continue
            except Exception as e:
                last_err = str(e)
                continue
        with self._stats_lock:
            self._stats["failed"] += 1
        self.log_debug(f"  下载失败(重试{self._retry}次): {url[:60]} - {last_err}")
        return None

    async def _download_html_async(self, session, semaphore, url: str, headers: dict) -> str:
        """异步 GET：手动跟随重定向（≤MAX_REDIRECTS 跳），每跳重新过 SSRF 校验"""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ok, reason = validate_public_http_url(current)
            if not ok:
                raise _DownloadAbort(f"SSRF 拒绝({reason}): {current[:80]}")
            async with semaphore, session.get(current, headers=headers, allow_redirects=False) as resp:
                if resp.status in _REDIRECT_STATUSES:
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        raise _DownloadAbort(f"HTTP {resp.status} 缺少 Location")
                    nxt = urljoin(current, loc)
                    if urlparse(nxt).scheme.lower() not in ("http", "https"):
                        raise _DownloadAbort(f"重定向到非 http/https 已中止: {nxt[:80]}")
                    current = nxt
                    continue
                if resp.status != 200:
                    if resp.status in (403, 429, 503):
                        raise _DownloadRetry(f"HTTP {resp.status}")
                    raise _DownloadAbort(f"HTTP {resp.status}")
                text: str = await resp.text()
                return text
        raise _DownloadAbort(f"重定向超过 {MAX_REDIRECTS} 跳上限")

    def _fetch_article(self, result, query: str, task_id: str, task_dir: Path, idx=0) -> dict | None:
        """下载单个页面（同步），提取正文，保存到本地（带 UA 轮换 + 重试）

        提取策略：Firecrawl 优先 → HTML 下载回退
        """
        import requests

        url = result.get("url", "") if isinstance(result, dict) else getattr(result, "url", "")
        title = result.get("title", "") if isinstance(result, dict) else getattr(result, "title", "")
        if not url:
            return None
        ok, reason = validate_public_http_url(url)
        if not ok:
            self.log_debug(f"  跳过不安全 URL({reason}): {str(url)[:60]}")
            return None

        with self._stats_lock:
            self._stats["attempted"] += 1

        # ── 优先：Firecrawl 提取 ──
        if self._firecrawl.is_available():
            try:
                fc_result = self._firecrawl.scrape(url, 30)
                if fc_result["success"]:
                    plain_text = fc_result["markdown"]
                    if fc_result["title"] and not title:
                        title = fc_result["title"]
                    if self._is_content_usable(plain_text, url, query):
                        article = self._save_article(
                            title, url, plain_text, query, task_dir
                        )
                        if article:
                            with self._stats_lock:
                                self._stats["success"] += 1
                            return article
                    else:
                        with self._stats_lock:
                            self._stats["filtered"] += 1
                        self.log_debug(f"  Firecrawl 内容不可用: {url[:60]}")
                else:
                    self.log_debug(f"  Firecrawl 失败({fc_result['error'][:40]}): {url[:60]}")
            except Exception as e:
                self.log_debug(f"  Firecrawl 异常: {e}")

        # ── 回退：HTML 下载 + 正文提取 ──
        last_err = None
        for attempt in range(self._retry + 1):
            ua = self._ua_pool[(idx + attempt) % len(self._ua_pool)]
            try:
                headers = {**self._base_headers, "User-Agent": ua}
                html = self._download_html_sync(url, headers)
                plain_text = self._extract_text(html)

                if not self._is_content_usable(plain_text, url, query):
                    with self._stats_lock:
                        self._stats["filtered"] += 1
                    self.log_debug(f"  过滤不可用内容: {url[:60]}")
                    return None

                article = self._save_article(title, url, plain_text, query, task_dir)
                if article:
                    with self._stats_lock:
                        self._stats["success"] += 1
                    return article
            except _DownloadAbort as e:
                with self._stats_lock:
                    self._stats["failed"] += 1
                self.log_debug(f"  下载中止(安全拒绝): {url[:60]} - {e}")
                return None
            except requests.Timeout:
                last_err = "超时"
                continue
            except Exception as e:
                last_err = str(e)
                continue
        with self._stats_lock:
            self._stats["failed"] += 1
        self.log_debug(f"  下载失败(重试{self._retry}次): {url[:60]} - {last_err}")
        return None

    def _download_html_sync(self, url: str, headers: dict) -> str:
        """同步 GET：手动跟随重定向（≤MAX_REDIRECTS 跳），每跳重新过 SSRF 校验"""
        import requests

        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ok, reason = validate_public_http_url(current)
            if not ok:
                raise _DownloadAbort(f"SSRF 拒绝({reason}): {current[:80]}")
            with requests.get(current, timeout=PAGE_TIMEOUT, headers=headers,
                              allow_redirects=False) as r:
                if r.status_code in _REDIRECT_STATUSES:
                    loc = r.headers.get("Location", "")
                    if not loc:
                        raise _DownloadAbort(f"HTTP {r.status_code} 缺少 Location")
                    nxt = urljoin(current, loc)
                    if urlparse(nxt).scheme.lower() not in ("http", "https"):
                        raise _DownloadAbort(f"重定向到非 http/https 已中止: {nxt[:80]}")
                    current = nxt
                    continue
                if r.status_code != 200:
                    if r.status_code in (403, 429, 503):
                        raise _DownloadRetry(f"HTTP {r.status_code}")
                    raise _DownloadAbort(f"HTTP {r.status_code}")
                return r.text
        raise _DownloadAbort(f"重定向超过 {MAX_REDIRECTS} 跳上限")

    def _save_article(self, title: str, url: str, plain_text: str,
                      query: str, task_dir: Path) -> dict | None:
        """将提取的正文保存到本地文件，返回文章元数据（同目录临时文件 + os.replace 原子落盘）"""
        safe_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff-]', '_', title)[:60] or hashlib.sha256(url.encode()).hexdigest()[:12]
        file_path = task_dir / f"{safe_name}.txt"
        if file_path.exists():
            file_path = task_dir / f"{safe_name}_{secrets.token_hex(4)}.txt"
        file_content = (
            f"标题: {title}\n"
            f"来源: {url}\n"
            f"下载时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'='*60}\n\n"
            f"{plain_text}"
        )
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(task_dir), prefix=f"{safe_name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(file_content)
            os.replace(str(tmp_path), str(file_path))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise
        return {
            "title": title,
            "url": url,
            "local_path": str(file_path),
            "content_length": len(plain_text),
            "source": urlparse(url).netloc,
            "relevance": round(self._content_relevance(plain_text, query), 3),
        }

    def _extract_text(self, html: str) -> str:
        """从 HTML 中提取可读正文（优先 selectolax C-bindings 解析，回退到正则启发式）"""
        if not html:
            return ""

        # ── 先摘出 <pre> 代码块（保留换行/缩进），再对剩余内容做正文提取 ──
        code_text, html = _harvest_pre_blocks(html)

        # ── 优先：selectolax 解析（C-bindings，比正则快 5-10x）──
        parsed = selectolax_compat.get_parser(html)
        if parsed is None:
            return self._extract_text_regex(html, code_text=code_text)
        tree, backend = parsed
        try:
            # 移除无正文节点
            for tag in ("script", "style", "noscript", "svg", "head", "nav", "footer", "aside"):
                for node in tree.css(tag):
                    tree.decompose(node)

            # 优先 <article> / <main> 容器
            container = tree.css_first("article") or tree.css_first("main") or tree

            # 按块级标签提取段落，用文本密度筛选（pre 已在上面摘走）
            scored = []
            for node in container.css(
                "p, div, section, li, td, blockquote, article, h1, h2, h3, h4, h5, h6"
            ):
                blk = node.text(separator=" ", strip=True)
                if not blk:
                    continue
                # 标题是结构信号而非正文：不受正文长度/密度/链接密度门槛约束，
                # 否则短标题（如 <h2>总结</h2>）会被整段丢弃、语义丢失。
                # 仍然要求非纯数字，避免把表格里的数字当标题。
                if node.tag in _HEADING_TAGS:
                    if len(blk) > _MAX_HEADING_LEN:
                        continue
                    scored.append(_as_markdown_heading(node.tag, blk))
                    continue
                if len(blk) < 30:
                    continue
                # 文本密度：可见字符占比
                text_ratio = len(_RE_WHITESPACE_ALL.sub("", blk)) / max(len(blk), 1)
                if text_ratio < 0.4:
                    continue
                # 惩罚纯链接行
                link_density = len(_RE_URL_SCHEME.findall(blk)) / max(len(blk) // 50, 1)
                if link_density > 0.5:
                    continue
                scored.append(blk)

            text = '\n'.join(_dedupe_blocks(scored))

            # 兜底：若密度法提取过少，退回到全文档纯文本
            if len(text) < MIN_CONTENT_LENGTH:
                fallback = tree.text(separator=" ", strip=True)
                # 清理 Unicode 噪声字符
                fallback = _RE_NOISE_CHARS.sub(" ", fallback)
                fallback = _RE_WHITESPACE.sub(" ", fallback).strip()
                # 只在兜底确实更长时替换：全文含 <article>/<main> 容器时，
                # tree.text() 会把容器内所有段落再拼一遍（与 scored 重复），
                # 直接赋值会导致正文重复输出。
                if len(fallback) > len(text) * 1.5:
                    text = fallback

            text = _merge_code(text, code_text)
            if text:
                return text[:80000]  # 单页上限 80KB
        except Exception as e:
            # 不再静默吞异常：计数 + 首次告警，然后走正则回退
            self._parser_fallbacks += 1
            if self._parser_fallbacks == 1:
                self.log_warning(
                    f"HTML 解析后端 {backend} 提取失败，已降级为正则启发式"
                    f"（本页之后同类失败不再重复告警）: {type(e).__name__}: {e}")

        # ── 回退：正则启发式提取（无外部依赖）──
        return self._extract_text_regex(html, code_text=code_text)

    def _extract_text_regex(self, html: str, code_text: str = "") -> str:
        """正则启发式 HTML 正文提取（selectolax 不可用时的 fallback）

        code_text: 调用方（_extract_text）已摘出的代码块文本，用于复用避免重复解析。
        **无论是否传入，本方法都会重新从 html 摘除 <pre>**——否则传入
        code_text 却不移除 <pre>，代码会以压平形态混进正文，
        造成同一份代码出现两次（一次保真、一次塌陷）。
        """
        # 0. 摘出 <pre> 代码块（保留换行/缩进），避免被 _RE_WHITESPACE 压平
        harvested, html = _harvest_pre_blocks(html)
        code_text = code_text or harvested

        # 1. 移除明显无正文区域
        for _rx in _RE_REMOVE_AREAS.values():
            html = _rx.sub(" ", html)

        # 2. 优先尝试 <article> / <main> 容器
        main_match = _RE_MAIN_CONTAINER.search(html)
        region = main_match.group(2) if main_match else html

        # 3. 按块级标签切分为候选段落，用文本密度筛选
        #    用 finditer 而非 split：split 拿不到标签名，无法区分标题与正文
        scored = []
        for m in _RE_BLOCK_SPLIT.finditer(region):
            tag = (m.group(1) or "").lower()
            end = m.end()
            nxt = _RE_BLOCK_SPLIT.search(region, end)
            raw = region[end: nxt.start() if nxt else len(region)]

            blk = _RE_STRIP_TAGS.sub("", raw)       # 去标签
            blk = _RE_NAMED_ENTITY.sub(" ", blk)     # 去命名 HTML 实体（含大写）
            blk = _RE_NUM_ENTITY.sub(" ", blk)       # 去数字实体
            blk = _RE_HEX_ENTITY.sub(" ", blk)       # 去十六进制实体
            blk = _RE_NOISE_CHARS.sub(" ", blk)      # 去常见 Unicode 噪声字符
            blk = _RE_WHITESPACE.sub(" ", blk).strip()
            if not blk:
                continue

            # 标题是结构信号，不受正文长度门槛约束（与 selectolax 路径一致）
            if tag in _HEADING_TAGS:
                if len(blk) <= _MAX_HEADING_LEN:
                    scored.append(_as_markdown_heading(tag, blk))
                continue

            if len(blk) < 30:
                continue
            # 文本密度：可见字符占比
            text_ratio = len(_RE_WHITESPACE_ALL.sub("", blk)) / max(len(blk), 1)
            if text_ratio < 0.4:   # 多半是标签/链接噪声
                continue
            # 惩罚纯链接行（链接文字占比过高）
            link_density = len(_RE_URL_SCHEME.findall(blk)) / max(len(blk) // 50, 1)
            if link_density > 0.5:
                continue
            scored.append(blk)

        text = '\n'.join(_dedupe_blocks(scored))

        # 4. 兜底：若密度法提取过少，退回到全文档去标签
        if len(text) < MIN_CONTENT_LENGTH:
            fallback = _RE_STRIP_TAGS.sub(" ", html)
            fallback = _RE_NAMED_ENTITY.sub(" ", fallback)     # 命名实体（含大写）
            fallback = _RE_NUM_ENTITY.sub(" ", fallback)       # 数字实体
            fallback = _RE_HEX_ENTITY.sub(" ", fallback)       # 十六进制实体
            fallback = _RE_NOISE_CHARS.sub(" ", fallback)      # Unicode 噪声字符
            fallback = _RE_WHITESPACE.sub(" ", fallback).strip()
            # 同上：全文去标签会重复包含已提取的段落，仅在明显更长时替换
            if len(fallback) > len(text) * 1.5:
                text = fallback

        text = _merge_code(text, code_text)
        return text[:80000]  # 单页上限 80KB

    def _is_content_usable(self, text: str, url: str, query: str) -> bool:
        """识别内容是否可用（宽松策略：宁可保留，下游质量门控再筛）"""
        # 1. 长度下限
        if len(text) < MIN_CONTENT_LENGTH:
            return False

        # 2. 硬拒绝：明确的错误/拦截页
        for rx in _BAD_PATTERNS_COMPILED:
            if rx.search(text):
                return False

        # 3. 内容级主题相关性：query 中的英文专有名词必须在正文中出现
        #    例如 query="Apache Kafka 核心架构" → "kafka" 必须在正文中出现
        #    这能过滤掉搜索结果中"Apache"匹配但实际是 Apache HTTP Server 的无关页面
        query_en_tokens = _RE_EN_WORD.findall(query)
        # 排除通用词
        generic_en = {"apache", "the", "and", "for", "with", "from", "that", "this", "are", "was"}
        specific_en = [t.lower() for t in query_en_tokens if t.lower() not in generic_en]
        if specific_en:
            text_lower = text.lower()
            # 至少 1 个专有名词要在正文中出现
            if not any(tok in text_lower for tok in specific_en):
                return False

        return True

    def _content_relevance(self, text: str, query: str) -> float:
        """计算正文与查询的相关度（0~1），供质量评估与排序使用"""
        if not query or not text:
            return 0.0
        q_tokens = set(_RE_QUERY_TOKEN.findall(query.lower()))
        if not q_tokens:
            return 0.0
        t = text.lower()
        hit = sum(1 for tok in q_tokens if tok in t)
        return min(hit / len(q_tokens), 1.0)

    def cleanup_task_temp(self, task_id: str) -> int:
        """清理指定任务的临时文件，返回清理的文件数量"""
        import shutil
        count = 0
        if not task_id:
            return 0
        if not self._temp_dir.exists():
            return 0
        for item in self._temp_dir.iterdir():
            if item.name == task_id or item.name.startswith(task_id + "_"):
                with contextlib.suppress(OSError):
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    count += 1
        if count:
            self.log_info(f"清理任务 {task_id} 的临时文件: {count} 个")
        return count

    def cleanup_stale_temp(self, max_age_hours: int = 24) -> int:
        """清理过期的临时文件（默认 24 小时前）"""
        import shutil
        import time as _time
        cutoff = _time.time() - max_age_hours * 3600
        count = 0
        if not self._temp_dir.exists():
            return 0
        for item in self._temp_dir.iterdir():
            with contextlib.suppress(OSError):
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    count += 1
        if count:
            self.log_info(f"清理过期临时文件: {count} 个（>{max_age_hours}h）")
        return count

    def on_stop(self):
        """Agent 停止钩子 —— aiohttp session 已改为按调用创建/关闭，此处仅兜底清理残留引用"""
        super().on_stop()
        if self._aio_session is not None and not self._aio_session.closed:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self._aio_session.close())
                loop.close()
                self.log_info("aiohttp session 已关闭")
            except Exception as e:
                self.log_info(f"关闭 aiohttp session 异常（将由 GC 回收）: {e}")
        self._aio_session = None
