"""KnowledgeBase Agent v1.0 - 个人知识库（索引 + 检索）

把"喂资料 → 存下来 → 以后能检索到"这条链路接进流水线。

与摄入层的组合是这里的重点：`ingest` 负责把 PDF/图片/文本变成 Markdown，
本 Agent 再切块向量化入库 —— 于是**扫描件里的内容也能被检索到**，
而不只是纯文本。

两个动作（由 payload 的 action 字段或存在哪些键推断）：
- `index`：把文件/内容切块入库
- `search`：按查询检索相关切块

设计要点：
- 库路径默认 `knowledge_base.db`（与 message_store/task_queue 同级约定）
- 嵌入器默认 auto（语义优先、自动回落），可显式指定
- 索引时如实回报每份资料的成败与所用后端，不静默吞错
"""
from pathlib import Path

from artesian import embeddings as emb_core
from artesian.knowledge_base import KnowledgeBase
from loom_core.base_agent import AgentStatus, BaseAgent, Message
from loom_doc import ingest as ingest_core

AGENT_NAME = "knowledge_base"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "action": ('str', ''),
    "db_path": ('str', 'knowledge_base.db'),
    "embedder": ('str', 'auto'),
    "top_k": ('int', 5),
}
AGENT_VERSION = "1.0"
AGENT_DESC = "知识库 Agent - 资料索引与向量检索（支持 PDF/图片经摄入层）"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 15
INPUT_TOPICS = ["knowledge_base.input", "kb.index", "kb.search", "kb.input",
                "ingest.done"]
OUTPUT_TOPICS = ["kb.done", "kb.failed", "kb.results", "kb.indexed"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：检索命中经 dependencies_results 交给 writer，避免与 researcher.results 同名合并
PRODUCES: dict = {}
CONSUMES: list = []
DEPENDENCIES: list[str] = []
CACHE_TTL = 0
RESPAWN = False


class KnowledgeBaseAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._db_path = config.get("db_path", "knowledge_base.db")
        self._embedder_name = config.get("embedder", "auto")
        # 哈希后端需要显式维度；不传就会被静默忽略（配了 256 维实际用 1024）
        self._dim = int(config["dim"]) if config.get("dim") else None
        self._default_top_k = int(config.get("top_k", 5))
        self._max_per_doc = int(config.get("max_per_doc", 0))
        self._use_ingest = bool(config.get("use_ingest_layer", True))
        self._kb: KnowledgeBase | None = None
        self.log_info(
            f"KnowledgeBase v{AGENT_VERSION} 初始化完成"
            f"（库: {self._db_path}，嵌入器: {self._embedder_name}）"
        )

    @property
    def kb(self) -> KnowledgeBase:
        """惰性建库：避免 Agent 加载时就创建/打开数据库文件。"""
        if self._kb is None:
            self._kb = KnowledgeBase(
                self._db_path,
                embedder_name=self._embedder_name,
                dim=self._dim,
            )
            reasons = emb_core.auto_fallback_reasons()
            if reasons:
                for cand, why in reasons.items():
                    self.log_warning(f"嵌入后端 {cand} 不可用，已回落: {why}")
            self.log_info(f"知识库就绪（实际嵌入器: {self._kb._embedder.name}）")
        return self._kb

    def handle(self, msg: Message) -> dict | None:
        payload = msg.payload
        cfg = payload.get("config") or {}
        self._apply_node_config(cfg)
        # 流水线里动作写在节点 config（payload 不带 action），RPC 调用仍可直传
        action = str(payload.get("action") or cfg.get("action") or "").strip().lower()
        if not action:
            # 推断：有查询词就是检索，否则是索引
            action = "search" if (payload.get("query") or payload.get("queries")) else "index"

        if action in ("index_and_search", "rag"):
            result = self._do_rag(payload)
            self.publish("kb.done" if result.get("status") == "ok"
                         else "kb.failed", result)
        elif action == "search":
            # DAG 节点拿到的是 `queries` 列表（引擎自有键），单条 `query` 只在
            # 显式 RPC 时出现。此前这里一律走 _do_search，于是带 queries 的节点
            # 被回以"未指定查询词"——旧引擎把 error 当成功吞了，改判失败后才暴露。
            result = (self._do_search_multi(payload) if payload.get("queries")
                      else self._do_search(payload))
            self.publish("kb.results" if result.get("status") == "ok"
                         else "kb.failed", result)
        elif action == "index":
            result = self._do_index(payload)
            self.publish("kb.indexed" if result.get("status") == "ok"
                         else "kb.failed", result)
        else:
            result = {"status": "error",
                      "message": f"未知动作: {action}（支持 index / search / index_and_search）"}
            self.publish("kb.failed", result)
        return result

    # ── 一站式 RAG 动作（流水线用）──

    def _do_rag(self, payload: dict) -> dict:
        """先索引上游资料，再按查询词检索 —— 供 DAG 单节点完成接地准备。

        索引与检索拆成两个节点需要同一 Agent 在 DAG 里出现两次，而当前
        Scheduler 的节点名即 Agent 名（无别名机制），因此合成为一个动作。
        """
        index_res = self._do_index(payload)
        search_res = self._do_search_multi(payload)
        merged = dict(search_res)
        merged["action"] = "index_and_search"
        merged["task_id"] = payload.get("task_id", "")
        merged["indexed"] = index_res.get("indexed", 0)
        merged["index_failed"] = index_res.get("failed", 0)
        if index_res.get("status") != "ok":
            merged["index_error"] = index_res.get("message", "")
        # 一份都没索引成功且检索也无结果：如实报错，别让下游以为"库里没有"
        if merged.get("status") != "ok":
            return merged
        if not merged.get("results") and not merged["indexed"]:
            return {"status": "error", "results": [], "action": "index_and_search",
                    "task_id": payload.get("task_id", ""),
                    "indexed": 0, "hits": 0,
                    "message": "既未索引到资料也无检索命中"
                               f"（index_error={merged.get('index_error', '')}）"}
        return merged

    def _do_search_multi(self, payload: dict) -> dict:
        """按多个查询词检索并按切块去重合并（保留每个切块的最高分）。

        DAG 节点拿到的是 `queries` 列表而非单条 `query`。
        """
        queries = [str(q).strip() for q in (payload.get("queries") or []) if str(q).strip()]
        single = str(payload.get("query", "")).strip()
        if single and single not in queries:
            queries.insert(0, single)
        # 资料清单行（同一输入文件里既写主题也写语料路径）不是查询词
        queries = [q for q in queries if not self._is_material_line(q)]
        if not queries:
            # 作为 DAG 节点被调度却没有查询词 = 无事可做，不是检索失败；
            # 显式 kb.search 请求缺 query 仍然算错误（见 _do_search）。
            return {"status": "skipped", "message": "未指定查询词", "results": []}

        merged: dict[str, dict] = {}
        errors: list[str] = []
        last_ok: dict = {}
        for q in queries:
            res = self._do_search({**payload, "query": q})
            if res.get("status") != "ok":
                errors.append(f"{q}: {res.get('message', '')}")
                continue
            last_ok = res
            for hit in res.get("results", []):
                key = str(hit.get("chunk_id") or f"{hit.get('doc_id')}:{hit.get('ordinal')}")
                prev = merged.get(key)
                if prev is None or hit.get("score", 0) > prev.get("score", 0):
                    merged[key] = hit

        top_k = int(payload.get("top_k", self._default_top_k))
        results = sorted(merged.values(),
                         key=lambda h: h.get("score", 0), reverse=True)[:top_k]
        if not last_ok and errors:
            return {"status": "error", "results": [], "queries": queries,
                    "message": "；".join(errors)}
        return {"status": "ok", "results": results, "hits": len(results),
                "queries": queries, "scanned": last_ok.get("scanned", 0),
                "embedder": last_ok.get("embedder", self.kb._embedder.name),
                **({"query_errors": errors} if errors else {})}

    # ── 索引 ──

    def _do_index(self, payload: dict) -> dict:
        self.report(AgentStatus.RUNNING, "索引资料...")
        docs: list[dict] = []

        # 直接给内容
        content = payload.get("content", "")
        if content:
            res = self.kb.add_document(
                content,
                source=payload.get("source", ""),
                title=payload.get("title", ""),
                meta=payload.get("meta"),
            )
            docs.append({"source": payload.get("source", "(inline)"), **res})

        # 给文件（走摄入层，支持 PDF/图片）
        for path in self._collect_files(payload):
            docs.append(self._index_one(path, payload))

        if not docs:
            return {"status": "error", "task_id": payload.get("task_id", ""),
                    "message": "未指定 content 或 files"}

        ok = [d for d in docs if d.get("status") == "ok"]
        failed = [d for d in docs if d.get("status") != "ok"]
        for d in failed:
            self.log_warning(
                f"索引失败 {Path(str(d.get('source', '?'))).name}: "
                f"{d.get('message', '')}")

        return {
            "status": "ok" if ok else "error",
            "task_id": payload.get("task_id", ""),
            "indexed": len(ok),
            "failed": len(failed),
            "chunks": sum(d.get("chunks", 0) for d in ok),
            "documents": docs,
            "stats": self.kb.stats(),
        }

    def _index_one(self, path: str, payload: dict) -> dict:
        """索引单个文件：非文本格式先经摄入层转 Markdown。"""
        p = Path(path)
        if not p.exists():
            return {"source": path, "status": "error",
                    "message": f"文件不存在: {p}"}

        text = ""
        via = "direct"

        if self._use_ingest and p.suffix.lower() in ingest_core.SUPPORTED_SUFFIXES:
            ing = ingest_core.ingest(p)
            if ing.get("status") == "ok" and ing.get("markdown"):
                text = ing["markdown"]
                via = f"ingest:{ing.get('backend', '?')}"
            elif ing.get("needs_ocr"):
                # 扫描件/图片需要 OCR —— 如实回报而非静默跳过
                return {"source": path, "status": "error",
                        "message": ing.get("message", "需要 OCR 后端"),
                        "needs_ocr": True}
            # 摄入失败则继续走下面的直读兜底

        if not text:
            try:
                raw = p.read_bytes()
            except OSError as e:
                return {"source": path, "status": "error", "message": str(e)}
            for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

        if not text.strip():
            return {"source": path, "status": "error", "message": "内容为空"}

        res = self.kb.add_document(
            text,
            source=str(p),
            title=payload.get("title") or p.stem,
            meta={"via": via},
        )
        res["via"] = via
        return {"source": path, **res}

    # ── 检索 ──

    def _do_search(self, payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()
        if not query:
            return {"status": "error", "message": "未指定查询词"}

        self.report(AgentStatus.RUNNING, f"检索: {query}")
        # artesian 未安装/未带类型时自定义导入是 Any；显式标注钉住契约形状
        res: dict = self.kb.search(
            query,
            top_k=int(payload.get("top_k", self._default_top_k)),
            min_score=float(payload.get("min_score", 0.0)),
            max_per_doc=int(payload.get("max_per_doc", self._max_per_doc)),
            source_prefix=str(payload.get("source_prefix", "")),
        )
        res["task_id"] = payload.get("task_id", "")
        res["query"] = query
        if res.get("status") == "ok":
            res["hits"] = len(res.get("results", []))
            self.report(AgentStatus.RUNNING, f"命中 {res['hits']} 个切块")
        else:
            self.log_warning(f"检索未完成: {res.get('message', '')}")
        return res

    # ── 工具 ──

    def _apply_node_config(self, cfg: dict) -> None:
        """把 DAG 节点 config 落到实例上。

        Agent 经 registry 加载时构造函数拿到的是空 config，节点级
        `db_path / embedder / top_k` 只在 payload 里 —— 不应用的话
        YAML 里写的 `embedder: hash` 会静默无效（实测就是这样）。
        """
        if not cfg:
            return
        fields = (("db_path", "_db_path", str, True),
                  ("embedder", "_embedder_name", str, True),
                  ("top_k", "_default_top_k", int, False),
                  ("max_per_doc", "_max_per_doc", int, False),
                  ("use_ingest_layer", "_use_ingest", bool, False))
        rebuild = False
        for key, attr, cast, affects_db in fields:
            if cfg.get(key) is None:
                continue
            try:
                value = cast(cfg[key])
            except (TypeError, ValueError):
                self.log_warning(f"config.{key} 值无效: {cfg[key]!r}，已忽略")
                continue
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                rebuild = rebuild or affects_db
        if cfg.get("dim") and self._dim != int(cfg["dim"]):
            self._dim = int(cfg["dim"])
            rebuild = True
        if rebuild and self._kb is not None:
            # 库句柄是按旧配置建的，换 db_path/embedder 必须重开
            self._kb.close_all()
            self._kb = None

    @staticmethod
    def _collect_files(payload: dict) -> list[str]:
        files: list[str] = []
        single = payload.get("file") or payload.get("path")
        if single:
            files.append(str(single))
        listed = payload.get("files") or payload.get("paths") or []
        if isinstance(listed, str):
            files.append(listed)
        else:
            files.extend(str(f) for f in listed)
        # 流水线里没有 RPC 那样的 files= 参数：语料来自上游 ingest 节点的产出
        if not files:
            files.extend(KnowledgeBaseAgent._upstream_materials(payload))
        seen: set[str] = set()
        unique: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique

    @staticmethod
    def _upstream_materials(payload: dict) -> list[str]:
        """取上游 ingest 节点报告的资料清单。

        优先原始文件（检索结果才能溯源到用户自己的 PDF/MD 而非中间产物），
        没有 documents 时退回合并后的 artifact。
        """
        dep = (payload.get("dependencies_results") or {}).get("ingest") or {}
        paths = [str(d.get("file")) for d in (dep.get("documents") or [])
                 if d.get("file")]
        if paths:
            return paths
        artifact = dep.get("artifact")
        return [str(artifact)] if artifact else []

    @staticmethod
    def _is_material_line(query: str) -> bool:
        """判定"这行其实是语料文件路径"而不是检索意图。

        kb-docgen 的输入文件同时承载主题与资料清单，若不过滤，
        `corpus/x.md` 这类路径行会当成查询词挤占 top_k 命中位。
        """
        cand = query.strip().strip("`").lstrip("-*•> ").strip()
        if not cand:
            return False
        if Path(cand).suffix.lower() in ingest_core.SUPPORTED_SUFFIXES:
            return True
        try:
            return Path(cand).is_file()
        except OSError:  # 非法路径字符（Windows 会抛 ValueError/OSError）
            return False

    def get_info(self) -> dict:
        return {
            "db_path": self._db_path,
            "requested_embedder": self._embedder_name,
            "requested_dim": self._dim,
            "active_embedder": (self._kb._embedder.name if self._kb else None),
            "candidate_embedders": emb_core.available_embedders(),
            "fallback_reasons": emb_core.auto_fallback_reasons(),
            "use_ingest_layer": self._use_ingest,
        }

    def on_snapshot(self) -> dict:
        return {"db_path": self._db_path,
                "embedder": self._embedder_name,
                "dim": self._dim,
                "default_top_k": self._default_top_k}

    def on_stop(self):
        if self._kb is not None:
            self._kb.close_all()
            self._kb = None
