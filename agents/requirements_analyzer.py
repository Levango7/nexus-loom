"""
Requirements Analyzer Agent v1.0
=================================
职责：
  - 解析用户输入（主题/文档/自由文本），生成结构化 DocumentSpec
  - 识别意图（doc_type）、目标读者（audience）、范围（scope）、深度（depth）
  - 歧义检测 + 置信度评分：confidence < threshold 时输出追问建议
  - 有 LLM 时用一次小调用生成 JSON；无 LLM 时走规则模板路径
  - 输出 requirements_analyzer.done，供 downstream researcher/writer 消费
"""

import contextlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "requirements_analyzer"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True
AGENT_VERSION = "1.0"
AGENT_DESC = "需求分析器 - 意图识别 + 结构化 DocumentSpec 生成"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 5
# 仅订阅自己的输入主题；不可订阅 researcher.input 等下游节点主题，
# 否则会经总线收到发给其他 agent 的请求
INPUT_TOPICS = ["requirements_analyzer.input"]
OUTPUT_TOPICS = ["requirements_analyzer.done"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：结构化 DocumentSpec，供 writer 决定体裁
PRODUCES = {"spec": "first"}
CONSUMES: list = []
DEPENDENCIES = []  # type: ignore[var-annotated]
CACHE_TTL = 0
RESPAWN = False
AGENT_TAGS = ["analysis", "requirements"]

# ─── DocumentSpec 数据结构 ─────────────────────────────

DOC_TYPE_ENUM = ["报告", "方案", "手册", "API文档", "白皮书", "教程", "综述", "其他"]
DEPTH_ENUM = ["quick", "standard", "deep-research"]
AUDIENCE_LEVELS = ["非技术", "入门", "中级", "高级", "专家"]


@dataclass
class DocumentSpec:
    """结构化文档规格说明"""
    doc_type: str = "其他"              # 文档类型
    scope: list[str] = field(default_factory=list)   # 主题范围（关键词/领域）
    audience: str = "中级"              # 目标读者技术水平
    depth: str = "standard"             # quick / standard / deep-research
    constraints: dict = field(default_factory=dict)  # 篇幅、风格、合规等
    sources: list[str] = field(default_factory=list) # 指定参考源
    template: str = ""                  # 模板 ID（如有）
    language: str = "zh"               # 输出语言
    confidence: float = 1.0            # 置信度（0~1）
    ambiguities: list[dict] = field(default_factory=list)  # 待澄清问题
    raw_input: str = ""                # 原始输入文本（用于追溯）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentSpec":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})  # type: ignore[attr-defined]


# ─── 规则引擎：无 LLM 时的兜底路径 ──────────────────────

DOC_TYPE_HINTS = {
    "api": ["api", "接口", "接口文档", "sdk", "rest", "graphql"],
    "手册": ["手册", "指南", "guide", "how-to", "步骤", "教程", "tutorial"],
    "方案": ["方案", "设计", "architecture", "架构", "设计文档", "propos"],
    "报告": ["报告", "report", "调研", "调查", "分析", "研究"],
    "白皮书": ["白皮书", "whitepaper", "趋势", "展望"],
    "综述": ["综述", "survey", "review", "综述"],
    "教程": ["教程", "学习", "入门", "beginner"],
}

DEPTH_HINTS = {
    "quick": ["简述", "概要", "overview", "简介", "快速"],
    "deep-research": ["深度", "深入", "comprehensive", "详尽", "full"],
}

AUDIENCE_HINTS = {
    "非技术": ["通俗", "non-tech", "小白"],
    "入门": ["入门", "beginner", "新手", "初学者"],
    "专家": ["专家", "expert", "advanced", "资深"],
    "高级": ["高级", "advanced", "资深"],
}


def _rule_based_analysis(raw: str, config: dict) -> DocumentSpec:
    """基于规则的文本分析（无 LLM 时的兜底路径）"""
    text_lower = raw.lower()

    # 推断 doc_type
    doc_type = "其他"
    for dt, hints in DOC_TYPE_HINTS.items():
        if any(h in text_lower for h in hints):
            doc_type = dt
            break

    # 推断 depth
    depth = "standard"
    for d, hints in DEPTH_HINTS.items():
        if any(h in text_lower for h in hints):
            depth = d
            break

    # 推断 audience
    audience = "中级"
    for a, hints in AUDIENCE_HINTS.items():
        if any(h in text_lower for h in hints):
            audience = a
            break

    # 提取 scope（从输入中提取中文词组和英文术语）
    scope = _extract_keywords(raw)

    # 检测 ambiguity：输入长度 < 20 且未明确 doc_type
    ambiguities = []
    confidence = 1.0
    if len(raw.strip()) < 20:
        ambiguities.append({
            "field": "scope",
            "question": "输入较简短，请补充文档需要覆盖的具体主题或范围",
            "suggestion": "例如：'Kafka 的生产者消费者模型和集群部署'",
        })
        confidence = max(0.3, confidence - 0.3)
    if doc_type == "其他":
        ambiguities.append({
            "field": "doc_type",
            "question": "未明确文档类型，请确认期望的输出类型",
            "suggestion": f"可选：{', '.join(DOC_TYPE_ENUM[:5])}",
        })
        confidence = max(0.4, confidence - 0.2)
    if not any(h in text_lower for h in ["读者", "面向", "受众", "target"]):
        ambiguities.append({
            "field": "audience",
            "question": "未指定目标读者，将使用默认（中级技术水平）",
            "suggestion": "例如：'面向运维工程师' 或 '面向初学者'",
        })
        confidence = max(0.6, confidence - 0.1)

    # 提取 sources（如果输入中有 URL 或文件引用）
    sources = list({m.group() for m in re.finditer(
        r'https?://[^\s<>"\')]+' , raw) if len(m.group()) < 200})
    file_refs = re.findall(r"[(（]([^)）]+\.(?:md|txt|pdf|docx))[)）]", raw)
    sources.extend(file_refs)

    return DocumentSpec(
        doc_type=doc_type,
        scope=scope[:10],
        audience=audience,
        depth=depth,
        constraints=config.get("constraints", {}),
        sources=sources[:5],
        template=config.get("template", ""),
        language=config.get("language", "zh"),
        confidence=round(confidence, 2),
        ambiguities=ambiguities,
        raw_input=raw[:500],
    )


def _extract_keywords(text: str, max_keywords: int = 10) -> list[str]:
    """从文本中提取关键词（中文 2-gram + 英文单词）"""
    keywords = []
    # 中文：提取 2-6 字的连续片段（过滤停用词）
    stop_words = {"的", "了", "是", "在", "我", "有", "和", "与", "及", "一个",
                  "这份", "介绍", "简单", "基本", "概念", "生成", "一份", "文档",
                  "技术", "测试", "这是", "用于", "验证", "流水线", "是否", "正常",
                  "工作", "如何", "什么", "哪个", "这个", "那个", "以及", "关于"}
    cn_runs = re.findall(r"[一-鿿]{2,}", text)
    for run in cn_runs:
        if run not in stop_words and len(run) >= 2:
            keywords.append(run)
    # 英文术语
    en = re.findall(r"[a-zA-Z]{3,}", text)
    keywords.extend(w.lower() for w in en if w.lower() not in stop_words)
    # 去重并保持顺序
    seen = set()
    unique = []
    for kw in keywords:
        k = kw.lower()
        if k not in seen and len(k) >= 2:
            seen.add(k)
            unique.append(kw)
        if len(unique) >= max_keywords:
            break
    return unique


# ─── LLM 路径 ──────────────────────────────

_LLM_PROMPT_TEMPLATE = """你是一个文档需求分析师。根据用户输入，生成结构化的文档规格说明。

用户输入：
{input}

请严格按以下 JSON 格式输出（不要其他文字）：
{{
  "doc_type": "报告|方案|手册|API文档|白皮书|教程|综述|其他",
  "scope": ["关键词1", "关键词2"],
  "audience": "非技术|入门|中级|高级|专家",
  "depth": "quick|standard|deep-research",
  "constraints": {{}},
  "sources": [],
  "template": "",
  "language": "zh",
  "confidence": 0.95,
  "ambiguities": [{{"field": "xxx", "question": "...", "suggestion": "..."}}]
}}

要求：
- doc_type 根据主题推断最合适类型
- scope 提取 3-8 个核心关键词
- confidence 反映分析的确定程度（0.3~1.0）
- ambiguities 列出所有不确定的维度及追问建议
- 严格遵守 JSON 格式，不要 markdown 代码块"""


def _llm_analysis(raw: str, config: dict) -> DocumentSpec:
    """调用 LLM 生成 DocumentSpec"""
    from loom_core.llm_router import get_router
    router = get_router()
    if not router or not router.get_active_providers():
        raise RuntimeError("LLM 不可用")

    prompt = _LLM_PROMPT_TEMPLATE.format(input=raw[:2000])
    try:
        content, _provider = router.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.1,
            timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"LLM 调用失败: {e}") from e

    # 提取 JSON（可能包裹在 markdown 代码块中）
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError(f"LLM 未返回有效 JSON，原始响应: {content[:200]}")

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回的 JSON 解析失败: {e}") from e

    # 枚举校验：LLM 输出不可信，非法值回落到规则路径默认值
    if data.get("doc_type") not in DOC_TYPE_ENUM:
        data["doc_type"] = "其他"
    if data.get("depth") not in DEPTH_ENUM:
        data["depth"] = "standard"
    if data.get("audience") not in AUDIENCE_LEVELS:
        data["audience"] = "中级"
    try:
        data["confidence"] = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        data["confidence"] = 0.5

    spec = DocumentSpec.from_dict(data)
    spec.raw_input = raw[:500]
    spec.constraints = {**config.get("constraints", {}), **(data.get("constraints") or {})}
    return spec


# ─── Agent 实现 ──────────────────────────────

class RequirementsAnalyzerAgent(BaseAgent):
    """需求分析器 Agent"""

    AGENT_NAME = AGENT_NAME
    AGENT_VERSION = AGENT_VERSION
    AGENT_DESC = AGENT_DESC
    AGENT_AUTHOR = AGENT_AUTHOR
    AGENT_PRIORITY = AGENT_PRIORITY
    INPUT_TOPICS = INPUT_TOPICS
    OUTPUT_TOPICS = OUTPUT_TOPICS
    DEPENDENCIES = DEPENDENCIES
    CACHE_TTL = CACHE_TTL
    RESPAWN = RESPAWN
    AGENT_TAGS = AGENT_TAGS

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._confidence_threshold = float(config.get("confidence_threshold", 0.7))
        self._llm_enabled = config.get("llm_enabled", True)
        self._max_questions = int(config.get("max_questions", 3))
        self.log_info(
            f"需求分析器初始化完成 "
            f"(llm={'启用' if self._llm_enabled else '禁用'}, "
            f"threshold={self._confidence_threshold})"
        )

    def handle(self, msg: Message) -> dict | None:
        payload = getattr(msg, "payload", {}) or {}
        task_id = payload.get("task_id", "")
        raw_input = self._resolve_raw_input(payload)

        if not raw_input:
            return {"status": "skip", "message": "无输入内容"}

        # 节点级配置按请求解析：DAG 路径把 YAML 节点的 config 放在
        # payload["config"]，构造期拿到的是空 dict —— 不读它 = YAML 里写的
        # `llm_enabled` / `confidence_threshold` / `max_questions` 被静默忽略
        # （实测：声明 llm_enabled: false 仍会尝试 LLM 路径）。只解析成局部量、
        # 不改实例状态：同一实例可能被多条流水线并发复用。
        node_cfg = payload.get("config") or {}
        llm_enabled = node_cfg.get("llm_enabled", self._llm_enabled)
        threshold = float(node_cfg.get("confidence_threshold",
                                       self._confidence_threshold))
        max_questions = int(node_cfg.get("max_questions", self._max_questions))
        # 规则/LLM 路径共用的领域配置（constraints/template/language）也按
        # 节点级覆盖合并，避免同一路径上出现"一半配置生效一半不生效"。
        merged_cfg = {**self.config, **node_cfg}

        self.report(AgentStatus.RUNNING, f"分析需求: {raw_input[:60]}...")

        try:
            if llm_enabled:
                spec = self._analyze_with_llm(raw_input, merged_cfg)
            else:
                spec = _rule_based_analysis(raw_input, merged_cfg)
        except Exception as e:
            self.log_warning(f"LLM 分析失败，回退到规则路径: {e}")
            spec = _rule_based_analysis(raw_input, merged_cfg)

        # 置信度低于阈值时追加追问建议
        if spec.confidence < threshold:
            spec.ambiguities = spec.ambiguities[:max_questions]
            self.log_warning(
                f"置信度 {spec.confidence:.2f} 低于阈值 {threshold}，"
                f"生成 {len(spec.ambiguities)} 条追问建议"
            )

        result = {
            "status": "ok",
            "task_id": task_id,
            "spec": spec.to_dict(),
            "needs_clarification": spec.confidence < threshold,
            "confidence": spec.confidence,
        }

        self.report(AgentStatus.LOADED, f"分析完成 confidence={spec.confidence:.2f}")
        self.publish("requirements_analyzer.done", result)
        return result

    def _analyze_with_llm(self, raw: str, config: dict | None = None) -> DocumentSpec:
        return _llm_analysis(raw, config if config is not None else self.config)

    @staticmethod
    def _resolve_raw_input(payload: dict) -> str:
        """解析原始需求文本：直接字段 > 输入文件 > queries 拼接。

        DAG 模式下 dag_executor 不传 input/query，需求原文来自
        input_file 内容或从输入文件提取的 queries 列表。
        """
        raw = payload.get("input", "") or payload.get("query", "") or payload.get("topic", "")
        if raw:
            return str(raw)
        input_file = payload.get("input_file", "")
        if input_file and Path(input_file).exists():
            with contextlib.suppress(OSError):
                return Path(input_file).read_text(encoding="utf-8")[:2000]
        queries = payload.get("queries") or []
        joined = " ".join(q for q in queries if isinstance(q, str) and q)
        return joined[:2000]


# ─── 公开工具函数（供外部脚本直接调用）──────────────────

def analyze(input_text: str, config: dict | None = None) -> DocumentSpec:
    """便捷函数：直接分析文本并返回 DocumentSpec（不经过 Agent 总线）"""
    cfg = config or {}
    llm_enabled = cfg.get("requirements_analyzer", {}).get("llm_enabled", True)
    with contextlib.suppress(Exception):
        if llm_enabled:
            from loom_core.llm_router import get_router
            router = get_router()
            if router and router.get_active_providers():
                return _llm_analysis(input_text, cfg)
    return _rule_based_analysis(input_text, cfg)
