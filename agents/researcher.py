"""
Researcher Agent v2 - 增强型内容检索插件
======================================
改进点：
  - 多搜索引擎支持（可配置）
  - 智能缓存策略（LRU + TTL）
  - 搜索结果质量评分
  - 并行搜索控制（并发限制）
  - 搜索历史记录
"""
import asyncio
import contextlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message
from loom_core.cache_manager import CacheManager

AGENT_NAME = "researcher"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "search_engines": ("list", ["bing"]),
    "max_results": ('int', 10),
    "cache_size": ('int', 1000),
    "max_workers": ('int', 3),
    "min_score": ('float', 0.3),
    "max_history": ('int', 100),
}
AGENT_VERSION = "2.0"
AGENT_DESC = "增强型内容检索 Agent - 多引擎、智能缓存、质量评分"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 10
INPUT_TOPICS = ["researcher.input", "researcher.search"]
OUTPUT_TOPICS = ["researcher.done", "researcher.progress", "researcher.partial"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：检索结果列表；查询词由引擎从输入文件解析，不属于产物
PRODUCES = {"results": "list"}
CONSUMES: list = []
DEPENDENCIES = []  # type: ignore[var-annotated]
CACHE_TTL = 86400  # 24小时
RESPAWN = True
EXTRACTS_QUERIES = True
RESULTS_MERGE = "extend"

# 预编译热路径正则（_clean_queries / _relevance_score 每查询必经；
# _relevance_score 对每条搜索结果调用一次）
_NOISE_PATTERNS_COMPILED = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^这是一个测试", r"^用于验证", r"验证流水线", r"是否正常工作",
        r"^test\b", r"^测试\b", r"^\s*$", r"请生成", r"帮我写",
    )]
_RE_MEANINGFUL = re.compile(r"[一-鿿]{2,}|[a-zA-Z]{2,}")
_RE_CN_RUNS = re.compile(r"[一-鿿]{2,}")
_RE_CN_CONNECTORS = re.compile(r"[与和及的等在对于以]")
_RE_EN_WORDS = re.compile(r"[a-zA-Z]{2,}")


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str
    source: str
    query: str
    score: float = 0.0  # 质量评分
    relevance: float = 0.0  # query 相关性评分
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "query": self.query,
            "score": self.score,
            "relevance": self.relevance,
            "fetched_at": self.fetched_at,
        }


class ResearcherAgent(BaseAgent):
    """增强型内容检索 Agent"""

    DEFAULT_SPAM_DOMAINS = {
        "yuanbao.tencent.com", "wb.admaster.com.cn", "click.admaster.com.cn",
        "admaster.com.cn", "adsensor.org", "duomai.com", "tanx.com",
        "ex.bidswitch.com", "ib.adnxs.com", "nexage.com", "pubmatic.com",
        "adzerk.net", "doubleclick.net", "googleadservices.com",
        "googlesyndication.com", "amazon-adsystem.com",
    }

    DEFAULT_LOW_QUALITY_DOMAINS = set()  # type: ignore[var-annotated]

    # prosearch.cjs 脚本路径不内置默认值（机器相关路径不属于仓库）：
    # 通过环境变量 PROSEARCH_SCRIPT_PATH 或 agent config 的 prosearch_paths 提供；
    # 未提供时 _prosearch 自动跳过该引擎（优雅降级）
    DEFAULT_PROSEARCH_PATHS: list[str] = []

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)

        cache_size = config.get("cache_size", 1000)
        self._cache = CacheManager(
            name="researcher", max_size=cache_size,
            ttl=self.meta.cache_ttl or CACHE_TTL,
        )
        self._search_manager = None  # 缓存 SearchEngineManager 实例（避免每次查询重建）

        self.dedup_set: set = set()
        self._dedup_lock = threading.Lock()

        self._search_history: list[dict] = []
        self._history_lock = threading.Lock()
        self._max_history = config.get("max_history", 100)

        self._max_workers = config.get("max_workers", 3)

        self._search_engines = config.get("search_engines", ["bing", "sogou", "360"])
        # 处理嵌套配置：若 config 是完整的流水线配置（含 researcher 子键），提取该子配置
        if "researcher" in config and isinstance(config["researcher"], dict):
            research_cfg = config["researcher"]
            self._search_engines = research_cfg.get("search_engines", self._search_engines)

        from loom_core.rate_limiter import RateLimiterRegistry
        self._rate_limiters = RateLimiterRegistry()

        self._min_score = config.get("min_score", 0.25)

        self._spam_domains = set(config.get("spam_domains", [])) or self.DEFAULT_SPAM_DOMAINS
        config_lq = config.get("low_quality_domains", None)
        if config_lq is not None:
            self._low_quality_domains = set(config_lq)
        else:
            self._low_quality_domains = set(self.DEFAULT_LOW_QUALITY_DOMAINS)

        self._tracking_url_patterns = config.get("tracking_url_patterns", [
            "/evt/", "/click/", "/rd/", "/goto?", "redirect?",
            "adclick", "adredirect", "union", "trace",
        ])

        self._prosearch_paths = []
        env_path = os.environ.get("PROSEARCH_SCRIPT_PATH", "")
        if env_path:
            self._prosearch_paths.append(env_path)
        config_paths = config.get("prosearch_paths", [])
        if config_paths:
            self._prosearch_paths.extend(config_paths)
        self._prosearch_paths.extend(self.DEFAULT_PROSEARCH_PATHS)

        self._title_min_pattern = re.compile(r"[一-鿿]{2,}|[a-zA-Z]{3,}")

        self.log_info(f"Researcher v{AGENT_VERSION} 初始化完成 (engines={self._search_engines})")

    def on_config_update(self, changed_keys: list[str]):
        """配置热更新：重新加载搜索引擎配置"""
        super().on_config_update(changed_keys)
        self._search_manager = None
        self.log_info("搜索引擎配置已刷新，下次搜索将重新初始化")

    def handle(self, msg: Message) -> dict | None:
        """处理检索请求"""
        self.report(AgentStatus.RUNNING, "开始检索...")

        payload = msg.payload
        task_id = payload.get("task_id", "")
        queries = payload.get("queries", [])
        parallel = payload.get("parallel", True)
        max_results = payload.get("max_results", 50)

        # 从 DocumentSpec 增强查询：如果 spec.scope 存在，将其作为补充查询词
        spec = payload.get("spec") or {}
        if isinstance(spec, dict) and spec.get("scope"):
            spec_scope = [s for s in spec["scope"] if s and len(s) >= 2]
            if spec_scope and (not queries or len(queries) < 3):
                queries = list(dict.fromkeys(spec_scope + (queries or [])))
                self.log_info(f"从 DocumentSpec 提取 {len(spec_scope)} 个 scope 关键词作为查询词")

        # 从流水线配置覆盖运行时参数
        cfg = payload.get("config", {})
        engines_used = self._search_engines
        if isinstance(cfg, dict):
            # mock 模式下 agent 已显式配置仅 mock，忽略 payload 携带的真实引擎列表
            if "search_engines" in cfg and not all(e == "mock" for e in self._search_engines):
                engines_used = cfg["search_engines"]
            if "max_results" in cfg:
                max_results = cfg["max_results"]

        if not queries:
            return {"status": "ok", "task_id": task_id, "total": 0,
                    "results": [], "query_count": 0, "engines_used": engines_used}

        # 清洗 query：过滤噪音/验证性/无意义的行
        queries = self._clean_queries(queries)
        # 清洗 query 文本：去 markdown 格式、序号前缀
        queries = [self._clean_query_text(q) for q in queries]
        # 去重清洗后的查询
        queries = list(dict.fromkeys(q for q in queries if q))
        if not queries:
            return {"status": "ok", "task_id": task_id, "total": 0,
                    "results": [], "query_count": 0, "engines_used": engines_used}

        self.log_info(f"任务 {task_id}: {len(queries)} 个查询")

        results = []

        if parallel and len(queries) > 1:
            results = self._parallel_search(queries, task_id, max_results, engines_used)
        else:
            for i, query in enumerate(queries):
                self.report(AgentStatus.RUNNING, f"[{i+1}/{len(queries)}] {query[:40]}...")
                try:
                    r = self._search(query, task_id, engines_used)
                    results.extend(r)
                except Exception as e:
                    self.log_error(f"搜索失败 {query}: {e}")

        results = self._score_and_filter(results)
        results = self._deduplicate(results)
        results = results[:max_results]

        self._record_history(task_id, queries, len(results))

        self.report(AgentStatus.RUNNING, f"检索完成，共 {len(results)} 条结果")
        self.log_info(f"最终返回 {len(results)} 条搜索结果")

        return {
            "status": "ok",
            "task_id": task_id,
            "total": len(results),
            "results": [r.to_dict() for r in results],
            "query_count": len(queries),
            "engines_used": engines_used,
        }

    def _parallel_search(self, queries: list[str], task_id: str, max_results: int, engines: list[str]) -> list[SearchResult]:
        """并行搜索"""
        results = []
        completed = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_query = {
                executor.submit(self._search, query, task_id, engines): query
                for query in queries
            }

            for future in as_completed(future_to_query):
                query = future_to_query[future]
                completed += 1
                self.report(AgentStatus.RUNNING, f"[{completed}/{len(queries)}] 完成: {query[:40]}...")

                try:
                    r = future.result()
                    results.extend(r)

                    # 发送部分结果（流式）
                    if completed % 3 == 0:
                        self.publish("researcher.partial", {
                            "task_id": task_id,
                            "completed": completed,
                            "total": len(queries),
                            "partial_count": len(results),
                        })

                except Exception as e:
                    self.log_error(f"并行搜索失败 {query}: {e}")

        return results

    def _search(self, query: str, task_id: str, engines: list[str] = None) -> list[SearchResult]:
        """执行单次搜索（含缓存）"""
        if engines is None:
            engines = self._search_engines
        # C13：缓存 key 不含 task_id——README 宣传"LRU+TTL 跨任务缓存"，
        # 混入 task_id 后每个任务的同一查询必然 miss。key 覆盖 query + 引擎集
        # （不同引擎集结果不同），跨任务、跨引擎组合均正确隔离。
        engines_sig = ",".join(engines) if engines else ""
        cache_key = f"{query}|engines={engines_sig}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.log_debug(f"缓存命中: {query[:40]}")
            return [SearchResult(**r) for r in cached]

        all_results = []

        # 离线演示 / 测试路径：配置仅 mock 引擎时，直接短路返回，完全跳过
        # SearchEngineManager（from_env 会无条件注册 bing 等真实引擎并触发网络抓取）
        if engines and all(e == "mock" for e in engines):
            results = self._mock_search(query, "mock")
            self._cache.set(cache_key, [r.to_dict() for r in results])
            return results

        # 优先尝试 SearchEngineManager（多引擎统一接口 + Metaso API）
        # 性能优化：缓存 manager 实例，避免每次查询都重建（含引擎初始化、环境变量读取）
        try:
            from artesian.search_engines import SearchEngineManager
            if self._search_manager is None:
                self._search_manager = SearchEngineManager.from_env()
            manager = self._search_manager
            if manager.is_available():
                self.log_debug(f"SearchEngineManager 可用: {manager.get_engine_names()}")
                items = manager.search_with_sites(query, max_results=10)
                if items:
                    for item in items:
                        all_results.append(SearchResult(
                            title=item.title,
                            url=item.url,
                            snippet=item.snippet,
                            source=item.source,
                            query=query,
                        ))
                    self.log_info(f"SearchEngineManager 返回 {len(all_results)} 条结果")
        except Exception as e:
            self.log_debug(f"SearchEngineManager 不可用，回退到内置引擎: {e}")

        # 如果 SearchEngineManager 已返回足够结果，跳过内置引擎
        if len(all_results) >= 5:
            self._cache.set(cache_key, [r.to_dict() for r in all_results])
            return all_results

        # 回退：内置引擎串行搜索
        for engine in engines:
            try:
                # 每引擎独立限流
                rate_cfg = {"duckduckgo": (3, 5), "prosearch": (5, 10), "mock": (100, 100),
                            "sogou": (10, 20), "360": (10, 20)}
                rate, burst = rate_cfg.get(engine, (10, 20))
                limiter = self._rate_limiters.get_or_create(
                    f"search_{engine}", rate=rate, burst=burst,
                )
                if not limiter.acquire(block=True, timeout=30):
                    self.log_warning(f"{engine} 限流等待超时，跳过")
                    continue

                if engine == "prosearch":
                    results = self._prosearch(query)
                elif engine == "duckduckgo":
                    results = self._duckduckgo_search(query)
                elif engine == "bing":
                    results = self._bing_search(query)
                elif engine == "sogou":
                    results = self._sogou_search(query)
                elif engine == "360":
                    results = self._360_search(query)
                elif engine == "mock":
                    results = self._mock_search(query, engine)
                else:
                    # 名字不在内置实现里 ≠ 可以拿 mock 顶上：CI 实测过一次 351 字节
                    # 的 bocha mock 占位稿被当成功出厂（docgen 配了 bocha/tavily/serper，
                    # 这三个在本文件里没有实现分支，原本全部静默退化成假摘要）。
                    # mock 必须是显式选择，不能是兜底。
                    self.log_warning(f"未知检索引擎 {engine!r}：跳过，不回落 mock")
                    results = []
                all_results.extend(results)
            except Exception as e:
                self.log_error(f"{engine} 搜索失败: {e}")

        # 缓存结果（仅非空结果）：全引擎失败/反爬零结果时若照常写缓存，
        # 24h TTL 内同查询即使引擎恢复也持续返回空，静默劣化为占位文档
        if all_results:
            self._cache.set(cache_key, [r.to_dict() for r in all_results])

        return all_results

    def _prosearch(self, query: str) -> list[SearchResult]:
        """调用元宝搜索"""
        import subprocess

        script = None
        for path in self._prosearch_paths:
            if Path(path).exists():
                script = path
                break

        if not script:
            self.log_debug("prosearch.cjs 未找到，跳过 prosearch 引擎")
            return []

        try:
            result = subprocess.run(
                ["node", script, json.dumps({"keyword": query})],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return self._normalize_results(data, query)
        except Exception as e:
            self.log_error(f"prosearch 调用失败: {e}")

        return []

    def _html_search(self, query: str, engine_name: str, url: str,
                     params: dict, skip_domains: list[str],
                     max_results: int = 10) -> list[SearchResult]:
        """通用 HTML 搜索引擎抓取（Bing/Sogou/360 共用）"""
        import re

        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            with requests.get(url, params=params, headers=headers, timeout=10) as r:
                if r.status_code != 200:
                    self.log_warning(f"{engine_name} 返回 {r.status_code}")
                    return []
                html_text = r.text
            results = []
            seen_urls = set()
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', html_text):
                url_found = m.group(1)
                if url_found in seen_urls:
                    continue
                seen_urls.add(url_found)
                if any(d in url_found for d in skip_domains):
                    continue
                if any(url_found.endswith(ext) for ext in (".css", ".js", ".svg", ".png", ".ico")):
                    continue
                start = max(0, m.start() - 200)
                ctx = html_text[start:m.end()]
                title_match = re.search(r'(?:title|aria-label)="([^"]+)"', ctx)
                title = title_match.group(1) if title_match else url_found.split("/")[-1][:60]
                title = re.sub(r'<[^>]+>', '', title).strip()
                snippet_start = m.end()
                snippet_end = min(snippet_start + 500, len(html_text))
                snippet = re.sub(r'<[^>]+>', '', html_text[snippet_start:snippet_end]).strip()[:200]
                results.append(SearchResult(
                    title=title[:120] if len(title) > 120 else title,
                    url=url_found,
                    snippet=snippet,
                    source=engine_name,
                    query=query,
                    score=0.8,
                ))
                if len(results) >= max_results:
                    break
            self.log_info(f"{engine_name} [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"{engine_name} 搜索失败 ({e})，返回空结果")
            return []

    def _bing_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 Bing 搜索 — 只从 b_algo 容器提取真实搜索结果"""
        import re

        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            with requests.get("https://cn.bing.com/search",
                             params={"q": query, "mkt": "zh-CN"},
                             headers=headers, timeout=10) as r:
                if r.status_code != 200:
                    self.log_warning(f"Bing 返回 {r.status_code}")
                    return []
                html_text = r.text

            results = []
            seen_urls = set()
            for algo in re.finditer(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                                    html_text, re.DOTALL):
                block = algo.group(1)
                link = re.search(r'href="(https?://[^"]+)"', block)
                if not link:
                    continue
                url = link.group(1)
                if any(d in url for d in ("bing.com", "microsoft.com", "live.com",
                                           "w3.org", "schema.org")):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title_m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
                title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""

                snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()[:200] if snippet_m else ""

                results.append(SearchResult(
                    title=title[:120], url=url, snippet=snippet,
                    source="bing", query=query, score=0.8,
                ))
                if len(results) >= max_results:
                    break

            self.log_info(f"Bing [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"Bing 搜索失败 ({e})，返回空结果")
            return []

    def _sogou_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 Sogou 搜索（搜狗，国内可访问，免费，无需 API Key）"""
        return self._html_search(
            query=query,
            engine_name="sogou",
            url="https://www.sogou.com/web",
            params={"query": query},
            skip_domains=["sogou.com", "weixin.sogou.com", "w3.org", "schema.org"],
            max_results=max_results,
        )

    def _360_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 360 搜索（好搜，国内可访问，免费，无需 API Key）"""
        return self._html_search(
            query=query,
            engine_name="360",
            url="https://www.so.com/s",
            params={"q": query},
            skip_domains=["so.com", "360.cn", "haosou.com", "w3.org", "schema.org"],
            max_results=max_results,
        )


    def _duckduckgo_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 DuckDuckGo 实时搜索（免费，无需 API Key）"""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                from duckduckgo_search import DDGS
                results = []
                with DDGS(timeout=10) as ddgs:
                    for r in ddgs.text(query, max_results=max_results):
                        results.append(SearchResult(
                            title=r.get("title", ""),
                            url=r.get("href", ""),
                            snippet=r.get("body", ""),
                            source="duckduckgo",
                            query=query,
                            score=0.7,
                        ))
                if not results:
                    self.log_info(f"DuckDuckGo 返回空结果: {query[:40]}")
                self.log_info(f"DuckDuckGo [{query[:30]}]: {len(results)} 条结果")
                return results
            except ImportError:
                self.log_warning("duckduckgo_search 未安装，返回空结果")
                return []
            except Exception as e:
                self.log_warning(f"DuckDuckGo 不可用 ({e})，返回空结果")
                return []

    def _mock_search(self, query: str, engine: str) -> list[SearchResult]:
        """模拟搜索（用于测试）"""
        return [
            SearchResult(
                title=f"{engine} 搜索结果: {query}",
                url=f"https://example.com/search?q={query}",
                snippet=f"这是 {engine} 的搜索结果摘要...",
                source=engine,
                query=query,
                score=0.5,
            )
        ]

    def _normalize_results(self, data: dict, query: str) -> list[SearchResult]:
        """标准化搜索结果"""
        results = []
        # 修复：原写法 `a or b if cond else c` 优先级等价于 `(a or b) if cond else c`，
        # 导致 dict 输入永远得到空列表；按意图显式分支
        if isinstance(data, dict):
            items = data.get("results", [])
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for item in items[:20]:  # 最多20条
            if isinstance(item, dict):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", item.get("content", "")),  # type: ignore[arg-type]
                    source=item.get("source", ""),
                    query=query,
                ))

        return results

    def _clean_queries(self, queries: list[str]) -> list[str]:
        """清洗查询：过滤噪音/验证性/无实质意义的行"""
        cleaned = []
        for q in queries:
            q = q.strip()
            if len(q) < 4:
                continue
            if any(rx.search(q) for rx in _NOISE_PATTERNS_COMPILED):
                continue
            # 过滤纯标点/纯停用词
            meaningful = _RE_MEANINGFUL.findall(q)
            if not meaningful:
                continue
            cleaned.append(q)
        return cleaned

    @staticmethod
    def _clean_query_text(query: str) -> str:
        """清洗查询文本：去 markdown 格式、序号前缀、特殊符号"""
        import re
        q = query.strip()
        # 去 markdown 加粗/斜体
        q = re.sub(r"\*\*(.+?)\*\*", r"\1", q)
        q = re.sub(r"\*(.+?)\*", r"\1", q)
        # 去 markdown 链接文本 [text](url) → text
        q = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", q)
        # 去编号前缀 "1. **xxx** → xxx"
        q = re.sub(r"^\d+[\.、]\s*", "", q)
        # 去列表前缀
        q = re.sub(r"^[-*]\s+", "", q)
        # 统一中文破折号
        q = re.sub(r"[—–]+", " - ", q)
        return q.strip()

    def _is_spam_url(self, url: str) -> bool:
        """检查 URL 是否属于垃圾域名"""
        import urllib.parse
        try:
            domain = urllib.parse.urlparse(url).hostname or ""
            # 精确匹配
            if domain in self._spam_domains:
                return True
            # 后缀匹配
            return any(domain and domain.endswith("." + spam) for spam in self._spam_domains)
        except Exception:
            return False

    def _normalize_url(self, url: str) -> str:
        """URL 归一化：去跟踪参数，用于更准确的去重"""
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(url)
            # 丢弃常见跟踪参数
            track_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                           "utm_content", "fbclid", "gclid", "ref", "source",
                           "trid", "tridChannel", "spm", "scm"}
            clean_query = "&".join(
                f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query)
                if k.lower() not in track_params
            )
            return urllib.parse.urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, clean_query, parsed.fragment
            )).rstrip("?&") or url
        except Exception:
            return url

    def _score_and_filter(self, results: list[SearchResult]) -> list[SearchResult]:
        """质量评分和过滤（含域名黑名单 + query 相关性 + 标题质量）"""
        filtered = []
        for r in results:
            # 0a. 垃圾域名直接丢弃
            if self._is_spam_url(r.url):
                continue

            # 0b. 跟踪/重定向 URL 丢弃
            url_lower = r.url.lower()
            if any(p in url_lower for p in self._tracking_url_patterns):
                continue

            # 0c. 标题质量检查：至少包含一个真正的内容词
            if not self._title_min_pattern.search(r.title):
                continue

            # 1. 基础质量分（结构完整度）
            struct = 0.0
            if 10 <= len(r.title) <= 120:
                struct += 0.15
            if len(r.snippet) > 50:
                struct += 0.15
            if r.source and r.source in ("bing", "sogou", "360", "prosearch"):
                struct += 0.1
            if r.url and r.url.startswith("http"):
                struct += 0.1

            # 域名质量加成
            import urllib.parse
            try:
                domain = urllib.parse.urlparse(r.url).hostname or ""
                for lq in self._low_quality_domains:
                    if lq in domain:
                        struct -= 0.2
                        break
            except Exception as e:
                self.log_warning(f"域名质量检查失败: {e}")

            # 2. query 相关性分
            rel = self._relevance_score(r, r.query)
            r.relevance = rel

            # 综合分：相关性占主导（65%），结构占 35%
            r.score = min(max(struct, 0) * 0.35 + rel * 0.65, 1.0)

            if r.score >= self._min_score and r.relevance > 0:
                filtered.append(r)

        # 按评分排序
        filtered.sort(key=lambda x: x.score, reverse=True)
        return filtered

    def _relevance_score(self, r: SearchResult, query: str) -> float:
        """计算搜索结果与 query 的相关性（0~1）

        策略：从 query 提取关键词（去停用词），统计在标题+摘要中的命中比例。
        完全无命中 → 0；关键词全部命中 → 1。
        中文用 2-gram 滑动窗口拆分，避免长中文串作为一个 token 永远不匹配。
        """
        if not query:
            return 0.5
        # query 关键词：去标点、去停用词，取中文 2-gram 词和英文单词
        stop = {"的", "了", "是", "在", "我", "有", "和", "与", "及", "一个", "这份",
                "介绍", "简单", "基本", "概念", "生成", "一份", "文档", "技术", "测试",
                "这是", "用于", "验证", "流水线", "是否", "正常", "工作", "a", "the",
                "of", "to", "and", "is", "for", "this", "that", "with", "in", "on"}
        tokens: list[str] = []
        # 中文：先按连接词/介词拆分，再取 2+ 字片段作为 token
        # "核心架构与生产实践" → "核心架构", "生产实践" → 各自作为 token
        cn_runs = _RE_CN_RUNS.findall(query)
        for run in cn_runs:
            # 按连接词拆分
            segments = _RE_CN_CONNECTORS.split(run)
            for seg in segments:
                seg = seg.strip()
                if len(seg) >= 2 and seg not in stop:
                    tokens.append(seg)
                # 长段额外取 2-gram 补充匹配能力
                if len(seg) > 4:
                    for i in range(len(seg) - 1):
                        gram = seg[i:i+2]
                        if gram not in stop:
                            tokens.append(gram)
        # 英文：单词
        en = _RE_EN_WORDS.findall(query.lower())
        for w in en:
            if w not in stop:
                tokens.append(w)
        if not tokens:
            return 0.5

        text = f"{r.title} {r.snippet}".lower()
        hit = 0
        for t in tokens:
            if t.lower() in text:
                hit += 1
        return hit / len(tokens)

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """基于归一化 URL + 标题去重"""
        with self._dedup_lock:
            seen_urls = set()
            seen_titles = set()
            deduped = []

            for r in results:
                # URL 归一化去重（去跟踪参数后的裸 URL）
                clean_url = self._normalize_url(r.url or "")
                url_sig = clean_url.split("?")[0] if "?" in clean_url else clean_url
                if url_sig in seen_urls:
                    continue
                seen_urls.add(url_sig)

                # 标题去重（取前 30 字，忽略大小写）
                title_sig = (r.title or "").strip().lower()[:30]
                if title_sig and title_sig in seen_titles:
                    continue
                if title_sig:
                    seen_titles.add(title_sig)

                deduped.append(r)

            return deduped

    def _record_history(self, task_id: str, queries: list[str], result_count: int):
        """记录搜索历史"""
        with self._history_lock:
            self._search_history.append({
                "task_id": task_id,
                "queries": queries,
                "result_count": result_count,
                "timestamp": time.time(),
            })

            if len(self._search_history) > self._max_history:
                self._search_history = self._search_history[-self._max_history:]

    def get_search_history(self) -> list[dict]:
        """获取搜索历史"""
        with self._history_lock:
            return list(self._search_history)

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self.log_info("缓存已清空")

    async def search_async(self, query: str, engines: list[str] = None,
                           task_id: str = "") -> list[SearchResult]:
        """异步搜索 —— 优先用 aiohttp 真异步 HTTP，回退到 run_in_executor 包装同步。

        当搜索引擎支持 HTTP API 时，直接用 aiohttp 发起异步请求，
        避免线程池开销；不支持时回退到同步 _search + run_in_executor。
        """
        # 尝试 aiohttp 真异步路径（仅对 HTTP 搜索引擎生效）
        with contextlib.suppress(Exception):
            results = await self._search_http_async(query, engines or self._search_engines)
            if results is not None:
                return results

        # 回退：同步搜索 + run_in_executor
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._search, query, task_id, engines or self._search_engines,
        )

    async def _search_http_async(self, query: str, engines: list[str]) -> list[SearchResult] | None:
        """使用 aiohttp 发起真异步 HTTP 搜索请求。

        仅处理有 HTTP API 的搜索引擎，不支持的引擎返回 None 以触发回退。
        """
        try:
            import aiohttp
        except ImportError:
            return None

        # 查找有 API URL 的搜索引擎
        http_engines = []
        # 修复 P0：原代码引用 self._search_mgr，实际属性名为 self._search_manager
        # （__init__ 第 92 行初始化为 self._search_manager）。属性名不匹配导致
        # hasattr 永远 False，异步 HTTP 路径永远不生效，回退到同步线程池。
        if hasattr(self, '_search_manager') and hasattr(self._search_manager, '_engines'):
            for eng_name in engines:
                eng = self._search_manager._engines.get(eng_name)
                if eng and hasattr(eng, 'api_url') and eng.api_url:
                    http_engines.append((eng_name, eng))

        if not http_engines:
            return None  # 无 HTTP 引擎，触发回退

        all_results = []  # type: ignore[var-annotated]
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for eng_name, eng in http_engines:
                try:
                    url = eng.api_url
                    params = eng.build_params(query) if hasattr(eng, 'build_params') else {"q": query}
                    headers = getattr(eng, 'headers', {})
                    async with session.get(url, params=params, headers=headers) as resp:
                        data = await resp.json()
                        parsed = eng.parse_response(data, query) if hasattr(eng, 'parse_response') else []
                        all_results.extend(parsed)
                except Exception as e:
                    self.log_warning(f"异步搜索 {eng_name} 失败: {e}")

        return all_results

    async def parallel_search_async(self, queries: list[str], task_id: str,
                                     engines: list[str] = None) -> list[SearchResult]:
        """异步并行多 query 搜索 —— 用 asyncio.gather 替代 ThreadPoolExecutor。
        比 _parallel_search 更高效：无线程开销，纯 I/O 并发。"""
        tasks = [self.search_async(q, engines, task_id) for q in queries]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)
        all_results = []
        for r in results_nested:
            if isinstance(r, list):
                all_results.extend(r)
            else:
                self.log_warning(f"异步搜索异常: {r}")
        return all_results

    def is_healthy(self) -> bool:
        """健康检查"""
        # 检查缓存是否正常
        try:
            test_key = "health_check"
            self._cache.set(test_key, [])
            self._cache.get(test_key)
            return True
        except Exception:
            return False
