"""Ingest Agent v1.0 - 本地资料摄入插件（PDF/图片/文本 → Markdown）

摄入层的 Agent 包装：把本地已有资料（PDF、扫描件、图片、文本）
转成结构化 Markdown，喂给下游 writer / quality_gate / renderer。

定位：这是渲染层的对偶能力。渲染解决"产出什么格式"，
摄入解决"已有资料怎么进来"——知识库、报告生成都以摄入为前提。

设计要点：
- **单文件失败不中断批量**：一份资料解析失败不应拖垮整批
- **OCR 缺失如实回报**：扫描件/图片需要 OCR 后端，未安装时明确
  告知该装什么，而不是静默返回空字符串
- 产物写入本地文件供下游节点读取，不塞进消息总线正文（避免撑爆）
"""
import json
import logging
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message
from loom_doc import ingest as ingest_core

logger = logging.getLogger("agent.ingest")

AGENT_NAME = "ingest"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "output_dir": ('str', 'output/ingested'),
    "ocr_enabled": ('bool', True),
    "files_from_input": ('bool', True),
}
AGENT_VERSION = "1.0"
AGENT_DESC = "资料摄入 Agent - PDF/图片/文本 → 结构化 Markdown"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 10
# 只订阅自己的输入主题。历史上这里多挂了一个 `researcher.input`，
# 于是发给 researcher 的定向 RPC 被本 Agent 接走并回了"未指定待摄入文件"，
# docgen 因此全线拿到空 results（见 tests/test_kb_pipeline_wiring.py::TestBusAddressing）。
INPUT_TOPICS = ["ingest.input", "ingest.files"]
OUTPUT_TOPICS = ["ingest.done", "ingest.failed"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：摄入产物由 knowledge_base 经 dependencies_results 读取
PRODUCES: dict = {}
CONSUMES: list = []
DEPENDENCIES: list[str] = []
CACHE_TTL = 0
RESPAWN = False


class IngestAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._output_dir = config.get("output_dir", "output/ingested")
        self._ocr_enabled = config.get("ocr_enabled", True)
        self.log_info(
            f"Ingest v{AGENT_VERSION} 初始化完成"
            f"（数字版后端: {','.join(ingest_core.available_backends(include_ocr=False)) or 'pymupdf 缺失'}；"
            f"OCR 按需探测）"
        )

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "开始摄入资料...")
        payload = msg.payload
        task_id = payload.get("task_id", "")

        files = self._collect_files(payload)
        if not files:
            # 无事可做 ≠ 失败，也 ≠ 成功：如实标 skipped，引擎会把它记成
            # skipped 步骤而不是"已交付"。（历史包袱：legacy run() 会给每个
            # 已注册 Agent 都发一次 RPC，摄入没有语料是常态而非错误。）
            no_files: dict = {"status": "skipped", "task_id": task_id,
                              "message": "未指定待摄入文件"}
            self.publish("ingest.failed", no_files)
            return no_files

        self.log_info(f"待摄入 {len(files)} 个文件")

        batch = ingest_core.ingest_many(list(files))
        documents = batch.get("documents", [])
        ok_docs = [d for d in documents if d.get("status") == "ok" and d.get("markdown")]
        failed = [d for d in documents if d not in ok_docs]

        for d in failed:
            self.log_warning(
                f"摄入失败 {Path(d.get('file', '?')).name}: {d.get('message', '')}"
            )
        needs_ocr = [d for d in documents if d.get("needs_ocr")]
        for d in needs_ocr:
            self.log_warning(
                f"{Path(d.get('file', '?')).name} 需要 OCR 后端：{d.get('message', '')}"
            )

        merged = batch.get("markdown", "")
        artifact = self._save_artifact(task_id, merged, ok_docs) if merged else ""

        result: dict = {
            "status": "ok" if ok_docs else "error",
            "task_id": task_id,
            "succeeded": len(ok_docs),
            "failed": len(failed),
            "chars": len(merged),
            "artifact": artifact,
            "backends": batch.get("backends", []),
            "needs_ocr": bool(batch.get("needs_ocr")),
            "documents": [
                {"file": d.get("file"),
                 "backend": d.get("backend"),
                 "chars": d.get("chars", 0),
                 "pages": d.get("pages"),
                 "tables": d.get("tables"),
                 "message": d.get("message", "")}
                for d in documents
            ],
        }
        if failed:
            result["failures"] = [
                {"file": d.get("file"), "message": d.get("message", "")}
                for d in failed
            ]
        if needs_ocr and self._ocr_enabled and not ingest_core.ocr_backend():
            result["ocr_hint"] = (
                "扫描件/图片需要 OCR 后端。推荐 pip install paddlepaddle "
                "paddlex[ocr] paddleocr（PP-StructureV3，中文版面与表格"
                "还原最好）或 mineru；注意 paddlepaddle 需与本机 Python "
                "版本匹配"
            )

        self.report(AgentStatus.RUNNING,
                    f"摄入完成 {len(ok_docs)}/{len(files)}，{len(merged)} 字符")
        self.publish("ingest.done" if ok_docs else "ingest.failed", result)
        return result

    @staticmethod
    def _collect_files(payload: dict) -> list[str]:
        """从payload 里收齐文件路径，兼容多种传参形态。

        流水线编排时没有 RPC 那样的 `files=` 入参，资料清单来自三处：
        节点 config.files、节点 config.input_dir（目录批量），
        或 CLI 输入 Markdown 里的路径行（每行一个，允许 `-`/`*` 项目符号）。
        """
        files: list[str] = []
        cfg = payload.get("config") or {}

        single = payload.get("file") or payload.get("path") or payload.get("input")
        if single:
            files.append(str(single))

        listed = payload.get("files") or payload.get("paths") or cfg.get("files") or []
        if isinstance(listed, str):
            files.append(listed)
        else:
            files.extend(str(f) for f in listed)

        input_dir = cfg.get("input_dir")
        if input_dir:
            base = Path(str(input_dir))
            if not base.is_dir():
                raise FileNotFoundError(f"config.input_dir 不是目录: {base}")
            pattern = str(cfg.get("pattern", "*"))
            files.extend(str(p) for p in sorted(base.glob(pattern)) if p.is_file())

        if not files and cfg.get("files_from_input", True):
            files.extend(IngestAgent._paths_from_input(str(payload.get("input_file", ""))))

        # 去重保序（set.add 返回 None，不能用在条件表达式里）
        seen: set[str] = set()
        unique: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique

    @staticmethod
    def _paths_from_input(input_file: str) -> list[str]:
        """把输入文件里的路径行解析为磁盘上真实存在的资料文件。

        主题描述之类的散文行解析不出路径就跳过；输入文件自身不算资料，
        否则"吃自己的清单"会把提示词当成语料摄入。
        """
        if not input_file:
            return []
        src = Path(input_file)
        if not src.is_file():
            return []
        out: list[str] = []
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"读取资料清单失败 {input_file}: {e}")
            return []
        for raw in text.splitlines():
            line = raw.strip().lstrip("-*•># ").strip().strip("`")
            if not line or line == src.name:
                continue
            for cand in (Path(line), src.parent / line):
                try:
                    if cand.is_file():
                        out.append(str(cand))
                        break
                except OSError:
                    continue
        return out

    def _save_artifact(self, task_id: str, markdown: str,
                       docs: list[dict]) -> str:
        """把摄入结果落盘。

        内容进文件而非消息总线正文：批量资料可达 MB 级，
        塞进消息会撑爆总线内存（LARGE_ARTICLE_THRESHOLD 同理）。
        """
        out_dir = Path(self._output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (task_id or "ingest") if c.isalnum() or c in "-_")[:60]
        path = out_dir / f"{safe or 'ingest'}.md"

        header = [f"<!-- 由 ingest Agent 生成：{len(docs)} 份资料 -->", ""]
        path.write_text("\n".join(header) + markdown, encoding="utf-8")
        self.log_info(f"摄入结果已保存: {path} ({path.stat().st_size:,} B)")
        return str(path)

    def on_snapshot(self) -> dict:
        return {"output_dir": self._output_dir, "ocr_enabled": self._ocr_enabled}

    def get_info(self) -> dict:
        """汇报摄入能力。基类无同名方法，这里独立提供。"""
        return {
            "backends": ingest_core.available_backends(),
            "ocr_backend": ingest_core.ocr_backend(),
            "supported": sorted(ingest_core.SUPPORTED_SUFFIXES),
            "output_dir": self._output_dir,
            "ocr_enabled": self._ocr_enabled,
        }

    @staticmethod
    def _dumps(obj) -> str:      # pragma: no cover - 调试辅助
        return json.dumps(obj, ensure_ascii=False, default=str)
