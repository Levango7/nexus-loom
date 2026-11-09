"""
QualityGate Agent v2 - 质量门禁（Profile 模板驱动）
=================================================
评分维度:
  - completeness:   文档结构完整度（必需章节、目录、参考资料）
  - structure:      标题层级合理性（H1→H2→H3 递进）
  - readability:    内容可读性（段落长度、语言风格）
  - citation:       引用可追溯性（URL 有效性、来源一致性）
  - depth:          内容深度（字数、段落数、信息密度）

特点:
  - 从 YAML Quality Profile 加载评分配置（可插拔）
  - 风格规则从 Profile 加载，不硬编码
  - 引用检查可配置启用/禁用
  - 扣分上限可配置
"""
import contextlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from loom_core.base_agent import AgentStatus, BaseAgent, Message
from loom_core.pack_manifest import quality_dirs as _quality_dirs
from loom_doc import degradation

AGENT_NAME = "quality_gate"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "quality_profile": ('str', 'technical-doc'),
    "threshold": (['int', 'float'], 70),
    "max_regenerations": ('int', 3),
    "min_output_chars": ('int', 120),
    "max_placeholder_section_ratio": (['int', 'float'], 0.34),
    "allow_raw_fetch_blocks": ('bool', False),
}
AGENT_VERSION = "2.0"
AGENT_DESC = "质量门禁 Agent v2 - Profile 模板驱动、可插拔评分"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 40
INPUT_TOPICS = ["writer.done", "quality_gate.check", "quality_gate.input"]
OUTPUT_TOPICS = ["quality_gate.done", "quality_gate.failed"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：只做判定，不重新导出正文（评分细节走 dependencies_results）
PRODUCES: dict = {}
CONSUMES = ["content"]
DEPENDENCIES = ["writer"]
CACHE_TTL = 0
RESPAWN = False
SUPPORTS_REGENERATION = True
REGENERATION_TARGET = "writer"
REGENERATION_RECHECK = "quality_gate"
AGENT_TAGS = ["quality", "gate"]

# 产出保真底线（先于评分维度判定）
DEFAULT_MIN_OUTPUT_CHARS = 120
# "没内容"的标志语与占位章节占比阈值。这些字符串是 writer ↔ 门禁之间的契约，
# 单处定义在 loom_doc.degradation —— 两处各写一份时漂移过：writer 改发
# 「降级声明」和「（暂无可用的相关内容）」，门禁名单里没有，于是 4/5 章节为
# 占位符的文档被判 98.8 pass 并正常落盘（2026-10-06 实测）。
PLACEHOLDER_MARKERS = degradation.PLACEHOLDER_MARKERS
#: 占位章节占比超过此值即判"没有产出"。取 1/3：一份 6 节的文档空 1 节走评分，
#: 空 4 节（实测样本 80%）不再有机会被当成成品出厂。
DEFAULT_MAX_PLACEHOLDER_RATIO = 0.34

# 默认配置文件路径：pack 感知解析——核心 pipelines/quality 优先，其次本 pack 的 quality/
QUALITY_DIRS = _quality_dirs()
QUALITY_DIR = next((d for d in QUALITY_DIRS if d.is_dir()), QUALITY_DIRS[0])
DEFAULT_PROFILE = "technical-doc.yaml"

# 英文功能词/泛文档词表：这些词即使首字母大写也不是专名，不计入 mandatory
_TOPIC_FN_WORDS = frozenset({
    "how", "what", "why", "when", "where", "who", "whose", "whom", "which",
    "whether", "the", "and", "or", "but", "nor", "so", "yet", "if", "then",
    "this", "that", "these", "those", "there", "here", "it", "its",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "done", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "use", "used", "using", "usage", "user",
    "best", "top", "guide", "tutorial", "introduction", "overview",
    "summary", "review", "basics", "basic", "example", "examples",
    "step", "steps", "tips", "setup", "install", "installing",
    "getting", "started", "learn", "learning", "with", "without",
    "within", "into", "onto", "about", "from", "for", "over", "under",
    "again", "all", "any", "both", "each", "more", "most", "other",
    "some", "such", "only", "same", "than", "very", "just", "also",
})
_TOPIC_MIN_WORD_LEN = 4


def load_profile(profile_name: str) -> dict:
    """加载 Quality Profile YAML"""
    import logging
    _log = logging.getLogger(__name__)
    try:
        path = Path(profile_name)
        if not path.exists():
            for qdir in QUALITY_DIRS:
                cand = qdir / profile_name
                if not cand.suffix:
                    cand = cand.with_suffix(".yaml")
                if cand.exists():
                    path = cand
                    break
        if not path.exists():
            for qdir in QUALITY_DIRS:
                if (qdir / DEFAULT_PROFILE).exists():
                    path = qdir / DEFAULT_PROFILE
                    break
        if not path.exists():
            _log.warning("QualityGate: 未找到 profile 文件")
            return {}

        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        _log.warning(f"QualityGate: 加载 profile 失败: {e}")
        return {}


def validate_style_rules(style_rules, profile_name: str) -> None:
    """校验 style_rules 必填键（name/pattern），缺失抛 ValueError 并定位到具体条目"""
    if style_rules is None:
        return
    if not isinstance(style_rules, list):
        raise ValueError(
            f"Quality Profile '{profile_name}': style_rules 必须为列表，实际为 "
            f"{type(style_rules).__name__}"
        )
    for idx, rule_cfg in enumerate(style_rules):
        if not isinstance(rule_cfg, dict):
            raise ValueError(
                f"Quality Profile '{profile_name}': style_rules[{idx}] 必须为映射，"
                f"实际为 {type(rule_cfg).__name__}"
            )
        missing = [key for key in ("name", "pattern") if key not in rule_cfg]
        if missing:
            raise ValueError(
                f"Quality Profile '{profile_name}': style_rules[{idx}] 缺少必填键: "
                f"{', '.join(missing)}"
            )


@dataclass(frozen=True)
class _RunCfg:
    """单次质量评估的运行配置（只读快照）。

    handle() 是并发入口，配置按请求解析、不再写回实例属性，
    避免共享单例在不同 profile 的请求间互相污染。
    """
    profile_name: str
    weights: dict
    threshold: float
    max_regenerations: int
    max_penalty: float
    citation_cfg: dict
    style_rules: list


class QualityGateAgent(BaseAgent):
    """质量门禁 Agent（Profile 驱动）"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)

        # 加载 Quality Profile
        profile_name = config.get("quality_profile", "technical-doc")
        self._profile = load_profile(profile_name)
        self._profile_name = self._profile.get("name", profile_name)
        # 请求键（文件名/路径，区别于 profile 内部 name 字段），用作运行期解析的缓存键
        self._profile_key = profile_name

        # 从 Profile 加载配置（fallback 到 config → 默认值）
        self._weights = {**self._profile.get("weights", {}), **config.get("weights", {})}
        self._threshold = config.get("threshold", self._profile.get("threshold", 70))
        self._max_regenerations = config.get("max_regenerations",
                                              self._profile.get("max_regenerations", 3))
        self._max_penalty = config.get("max_penalty", self._profile.get("max_penalty", 40))
        self._citation_cfg = {**self._profile.get("citation", {}),
                              **config.get("citation", {})}

        # 编译风格规则（从 Profile 加载，不硬编码）
        self._style_rules = self._compile_style_rules(self._profile, profile_name)

        # 初始化时的默认运行配置（已合并 agent config 覆盖）；
        # 实例属性保留作为默认值快照，供 _overall_score/_check_style 等直调兼容
        self._default_run_cfg = _RunCfg(
            profile_name=self._profile_name,
            weights=self._weights,
            threshold=self._threshold,
            max_regenerations=self._max_regenerations,
            max_penalty=self._max_penalty,
            citation_cfg=self._citation_cfg,
            style_rules=self._style_rules,
        )

        # Profile 缓存：请求键 → (profile dict, 编译后风格规则)。
        # 并发 handle() 共享只读条目，加载/编译在锁外完成（失败不进缓存）
        self._profile_cache: dict[str, tuple[dict, list[dict]]] = {
            profile_name: (self._profile, self._style_rules),
        }
        self._profile_cache_lock = threading.Lock()

        self.log_info(f"QualityGate v{AGENT_VERSION} (profile={self._profile_name}, "
                      f"threshold={self._threshold})")

    def _compile_style_rules(self, profile: dict, source: str) -> list[dict]:
        """校验并编译 Profile 风格规则，缺 name/pattern 键时抛 ValueError"""
        rules_cfg = profile.get("style_rules", [])
        validate_style_rules(rules_cfg, source)
        rules = []
        for rule_cfg in rules_cfg:
            if rule_cfg.get("enabled", True):
                rules.append({
                    "name": rule_cfg["name"],
                    "pattern": re.compile(rule_cfg["pattern"]),
                    "message": rule_cfg.get("message", ""),
                    "penalty": rule_cfg.get("penalty", 0),
                })
        return rules

    def _get_cached_profile(self, profile_key: str) -> tuple[dict, list[dict]]:
        """按请求键加载并编译 profile（带线程安全缓存）；坏规则抛 ValueError 且不进缓存"""
        with self._profile_cache_lock:
            cached = self._profile_cache.get(profile_key)
        if cached is not None:
            return cached
        profile = load_profile(profile_key)
        style_rules = self._compile_style_rules(profile, profile_key)
        with self._profile_cache_lock:
            self._profile_cache.setdefault(profile_key, (profile, style_rules))
        return profile, style_rules

    def _fidelity_violations(self, content: str, run_config: dict) -> list[str]:
        """产出保真底线判据：内容量 + 已知占位语。

        与评分维度的分工：评分量的是"写得好不好"，可以被风格/引用扣分
        拉低后仍放行；底线量的是"有没有真实产出"，命中即判失败，
        不重做也不 accepted_with_warnings。
        """
        text = (content or "").strip()
        violations: list[str] = []
        try:
            min_chars = int(run_config.get("min_output_chars",
                                           DEFAULT_MIN_OUTPUT_CHARS))
        except (TypeError, ValueError):
            self.log_warning(f"config.min_output_chars 无效: "
                             f"{run_config.get('min_output_chars')!r}，用默认值")
            min_chars = DEFAULT_MIN_OUTPUT_CHARS
        if len(text) < min_chars:
            violations.append(f"内容过短（{len(text)} < {min_chars} 字符）")
        markers = run_config.get("placeholder_markers")
        if not isinstance(markers, list):
            markers = list(PLACEHOLDER_MARKERS)
        for marker in markers:
            if marker and str(marker) in text:
                violations.append(f"产出为占位内容（命中“{marker}”）")
        # 逐节占位：writer 每有一节提取不到内容就交出一行 SECTION_PLACEHOLDER。
        # 只看"整份有没有那两个字"会漏掉"有字的垃圾"——实测样本 3065 字节、
        # 4/6 章节是占位符，却因长度过底线而拿到 98.8 pass。
        try:
            max_ratio = float(run_config.get("max_placeholder_section_ratio",
                                             DEFAULT_MAX_PLACEHOLDER_RATIO))
        except (TypeError, ValueError):
            self.log_warning(f"config.max_placeholder_section_ratio 无效: "
                             f"{run_config.get('max_placeholder_section_ratio')!r}，用默认值")
            max_ratio = DEFAULT_MAX_PLACEHOLDER_RATIO
        ratio = degradation.placeholder_ratio(text)
        if ratio > max_ratio:
            violations.append(
                f"{degradation.placeholder_section_count(text)}/"
                f"{degradation.section_count(text)} 个章节是占位符"
                f"（{ratio:.0%} > {max_ratio:.0%}）")
        # 抓取层中间格式泄漏：成品里出现「下载时间:」或 60 连等号分隔线，说明
        # writer 把素材块原样粘进来了 —— 这类文档字数充足、无占位语，光靠占比拦不住。
        if not self._flag_enabled(run_config.get("allow_raw_fetch_blocks")):
            leaked = degradation.raw_fetch_block_count(text)
            if leaked:
                violations.append(
                    f"正文泄漏 {leaked} 块抓取层原始素材（「下载时间:」/60 连等号分隔线），"
                    "那是素材的磁盘格式而非成品内容；确有需要请用 allow_raw_fetch_blocks 显式放行")
        return violations

    @staticmethod
    def _flag_enabled(value: object) -> bool:
        """只认显式的真值。YAML 里写成字符串 "false" 时不能被 bool("false") 判成 True。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _resolve_run_cfg(self, run_config: dict) -> _RunCfg:
        """解析本次请求的运行配置：profile（run_config 指定，否则沿用初始化 profile）+ 覆盖项。

        返回只读快照，不修改实例状态，并发 handle() 互不污染。
        """
        profile_key = run_config.get("quality_profile") or self._profile_key
        if profile_key == self._profile_key:
            # 与初始化同 profile：在初始化配置（已含 agent config 覆盖）之上叠加本次覆盖
            base = self._default_run_cfg
            return _RunCfg(
                profile_name=base.profile_name,
                weights={**base.weights, **run_config.get("weights", {})},
                threshold=run_config.get("threshold", base.threshold),
                max_regenerations=run_config.get("max_regenerations", base.max_regenerations),
                max_penalty=run_config.get("max_penalty", base.max_penalty),
                citation_cfg={**base.citation_cfg, **run_config.get("citation", {})},
                style_rules=base.style_rules,
            )
        profile, style_rules = self._get_cached_profile(profile_key)
        return _RunCfg(
            profile_name=profile.get("name", profile_key),
            weights={**profile.get("weights", {}), **run_config.get("weights", {})},
            threshold=run_config.get("threshold", profile.get("threshold", 70)),
            max_regenerations=run_config.get("max_regenerations",
                                             profile.get("max_regenerations", 3)),
            max_penalty=run_config.get("max_penalty", profile.get("max_penalty", 40)),
            citation_cfg={**profile.get("citation", {}), **run_config.get("citation", {})},
            style_rules=style_rules,
        )

    def handle(self, msg: Message) -> dict | None:
        """处理质量检查请求"""
        self.report(AgentStatus.RUNNING, "开始质量评估...")
        payload = msg.payload
        content = payload.get("content", "")
        task_id = payload.get("task_id", "")
        generation_count = payload.get("generation_count", 0)

        if not content:
            return {"status": "error", "message": "内容为空", "score": 0}

        # 支持从流水线配置覆盖 Quality Profile（按请求解析，不修改共享实例状态）
        run_config = payload.get("config", {})
        cfg = self._resolve_run_cfg(run_config)

        # ── 产出保真底线（先于评分）──
        # 评分维度回答"写得好不好"，底线回答"到底有没有内容"。
        # 此前无底线：writer 缺素材时写的占位文档（"未采集到可整合的搜索结果"，
        # 实测 99 字节）能一路走到落盘并把整条流水线报成成功。
        violations = self._fidelity_violations(content, run_config)
        if violations:
            hard_fail = {
                "status": "fail",
                "task_id": task_id,
                "hard_floor": True,
                "overall_score": 0,
                "violations": violations,
                "profile": cfg.profile_name,
                "needs_regenerate": False,
                "can_regenerate": False,
                "generation_count": generation_count,
            }
            self.log_error(
                f"产出未过保真底线，判定失败（不重做、不放行）: {'；'.join(violations)}")
            self.publish("quality_gate.failed", hard_fail)
            # 底线失败也要进反馈闭环与事件钩子：跳过记录会让"没产出"这种
            # 最需要学习的样本从历史统计里消失（且下游契约依赖这两次调用）。
            self._notify_feedback(task_id, cfg, {}, 0.0, False, False, generation_count)
            return hard_fail

        # 多维度评分（按 cfg weights）
        queries = payload.get("queries", []) or []
        scores = self._score_all(content, queries)
        overall = self._overall_score(scores, cfg.weights)

        # 风格检查（从 cfg 规则）
        style_issues = self._check_style(content, cfg.style_rules)
        style_penalty = sum(i.get("penalty", 0) for i in style_issues)

        # 引用检查（可禁用）
        citation_penalty = 0
        citation_report = {"total_refs": 0, "issues": []}
        if cfg.citation_cfg.get("enabled", True):
            citation_report = self._check_citations(content, cfg.citation_cfg)
            citation_penalty = len(citation_report.get("issues", [])) * cfg.citation_cfg.get("penalty_per_issue", 5)  # type: ignore[arg-type]

        # 总扣分
        total_penalty = min(style_penalty + citation_penalty, cfg.max_penalty)
        overall = max(0, overall - total_penalty)

        needs_regenerate = overall < cfg.threshold
        can_regenerate = generation_count < cfg.max_regenerations

        result = {
            "status": "pass" if not needs_regenerate else "fail",
            "task_id": task_id,
            "overall_score": round(overall, 1),
            "scores": {k: round(v, 1) for k, v in scores.items()},
            "profile": cfg.profile_name,
            "style_issues": style_issues,
            "citation_report": citation_report,
            "penalty": {"style": style_penalty, "citation": citation_penalty, "total": total_penalty},
            "needs_regenerate": needs_regenerate,
            "can_regenerate": can_regenerate,
            "generation_count": generation_count,
        }

        if needs_regenerate:
            info = f" (扣分: {total_penalty})" if total_penalty > 0 else ""
            self.log_warning(
                f"质量分 {overall:.1f} < {cfg.threshold}{info}"
                f"({'可重做' if can_regenerate else '已达上限'}) "
                f"问题: {self._score_breakdown(scores)}"
            )
        else:
            self.log_info(f"质量分 {overall:.1f}/{cfg.threshold} 通过 (profile={cfg.profile_name})")

        self.publish("quality_gate.done" if not needs_regenerate else "quality_gate.failed", result)

        self._notify_feedback(task_id, cfg, scores, overall,
                              needs_regenerate, can_regenerate, generation_count)

        return result

    def _notify_feedback(self, task_id: str, cfg: _RunCfg, scores: dict,
                         overall: float, needs_regenerate: bool,
                         can_regenerate: bool, generation_count: int) -> None:
        """质量结果进反馈闭环 + 事件钩子（两条判定路径共用，语义必须一致）。"""
        with contextlib.suppress(Exception):
            from loom_core.quality_feedback import record_quality
            record_quality(task_id=task_id, scores={k: round(v, 1) for k, v in scores.items()},
                           pipeline=cfg.profile_name, overall=round(overall, 1))

        with contextlib.suppress(Exception):
            from loom_core.event_hook import emit_event
            emit_event("quality_gate.evaluated", {"task_id": task_id, "score": round(overall, 1),
                       "threshold": cfg.threshold, "passed": not needs_regenerate,
                       "profile": cfg.profile_name})
            if needs_regenerate and can_regenerate:
                emit_event("quality_gate.regenerate", {"task_id": task_id, "score": round(overall, 1),
                           "generation_count": generation_count, "target": "writer"})

    # ── 多维度评分 ─────────────────────

    def _score_all(self, content: str, queries: list[str] = None) -> dict[str, float]:
        return {
            "completeness": self._score_completeness(content),
            "structure": self._score_structure(content),
            "readability": self._score_readability(content),
            "citation": self._score_citations(content),
            "depth": self._score_depth(content),
            "substance": self._score_substance(content),
            "topic_relevance": self._score_topic_relevance(content, queries),  # type: ignore[arg-type]
        }

    def _score_topic_relevance(self, content: str, queries: list[str]) -> float:
        """主题相关度：文档是否真的在讲 query 主题（而非跑题）

        - 英文专名候选（非句首大写词或全大写缩略语、排除功能词表）为 mandatory token，缺失过多则归零
        - 中文按 2-gram 拆分，避免整句不匹配
        无 query 时返回中性分（不惩罚）。
        """
        if not queries:
            return 70.0  # 无 query 时中性，不阻断
        # 提取所有 query 的核心词（去噪音）
        stop = {"的", "了", "是", "在", "我", "有", "和", "与", "及", "一个", "这份",
                "介绍", "简单", "基本", "概念", "生成", "一份", "文档", "技术", "测试",
                "这是", "用于", "验证", "流水线", "是否", "正常", "工作", "a", "the",
                "of", "to", "and", "is", "for", "this", "that", "with", "in", "on"}
        tokens: set[str] = set()
        mandatory: set[str] = set()  # 专有名词：缺失则归零
        for q in queries:
            # 句首词集合：仅句首出现的大写词多为普通词引导（How/What...），不算专名
            sent_initial: set[str] = set()
            for seg in re.split(r"[。？！；.?!;\n]", q):
                m_first = re.search(r"[A-Za-z][A-Za-z0-9]*", seg)
                if m_first:
                    sent_initial.add(m_first.group(0).lower())
            # 英文专有名词候选：首字母大写且长度>=4、排除功能词表；
            # 非句首位置出现或全大写缩略语（API/K8s）才计入 mandatory
            for m_prop in re.finditer(r"\b[A-Z][a-zA-Z0-9]{2,}\b", q):
                prop = m_prop.group(0)
                pl = prop.lower()
                if len(pl) < _TOPIC_MIN_WORD_LEN or pl in _TOPIC_FN_WORDS:
                    continue
                if pl in sent_initial and not prop.isupper():
                    continue
                mandatory.add(pl)
                tokens.add(pl)
            # 普通英文词
            for w in re.findall(r"[a-zA-Z]{2,}", q.lower()):
                if w not in stop:
                    tokens.add(w)
            # 中文 2-gram 滑动窗口拆分（避免整句不匹配）
            for zh in re.findall(r"[一-鿿]{2,}", q):
                if len(zh) <= 3:
                    if zh not in stop:
                        tokens.add(zh)
                else:
                    for i in range(len(zh) - 1):
                        gram = zh[i:i+2]
                        if gram not in stop:
                            tokens.add(gram)
        if not tokens:
            return 70.0

        text = content.lower()
        head = "\n".join(content.split("\n")[:30]).lower()  # 标题+目录+前两章

        # mandatory token 检查（修复 P0：原实现任一专有名词缺失即归零，过严格。
        # 例如 query="Apache Kafka 核心架构" 提取 mandatory={apache, kafka}，
        # 若文档只提到 Kafka 未提 Apache 即归零，导致评分失效。
        # 现改为覆盖率阈值：缺失专有名词超过 50% 才归零，否则按缺失比例扣分。）
        missing_mandatory = [t for t in mandatory if t not in text]
        if mandatory and missing_mandatory:
            missing_ratio = len(missing_mandatory) / len(mandatory)
            if missing_ratio > 0.5:
                # 超过半数专有名词缺失 → 严重跑题，归零
                self.log_debug(
                    f"topic_relevance 归零: 缺失 {len(missing_mandatory)}/{len(mandatory)} "
                    f"专有名词 {missing_mandatory}"
                )
                return 0.0
            else:
                # 少量缺失 → 按缺失比例扣分（不归零）
                self.log_debug(
                    f"topic_relevance 部分缺失: {missing_mandatory} "
                    f"({len(missing_mandatory)}/{len(mandatory)})"
                )

        hit = sum(1 for t in tokens if t.lower() in text)
        hit_head = sum(1 for t in tokens if t.lower() in head)
        coverage = hit / len(tokens)
        # 头部命中加权（跑题文档头部往往没有关键词）
        base_score = min(100.0, coverage * 70 + hit_head / len(tokens) * 30)
        # 对少量 mandatory 缺失施加额外扣分（每缺失一个扣 10 分）
        if mandatory and missing_mandatory:
            base_score = max(0.0, base_score - 10.0 * len(missing_mandatory))
        return round(base_score, 1)

    def _score_substance(self, content: str) -> float:
        """内容实质度：检测水话 / 车轱辘话 / 空洞，而非仅看格式"""
        score = 100.0
        paras = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 30]
        if not paras:
            return 0.0

        # 1. 信息密度：实词（长度>=2 的词）占比
        all_words = re.findall(r'[\w\u4e00-\u9fff]{2,}', content)
        if all_words:
            # 高频虚词（中英文停用词）占比高 = 信息密度低
            stop = set(["的", "了", "是", "在", "和", "与", "及", "也", "都", "就", "而", "等", "我们", "可以", "这个", "那个", "一种", "以及", "通过", "对于", "由于", "因此", "但是", "因为", "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are", "be"])
            content_words = [w for w in all_words if w.lower() not in stop]
            density = len(content_words) / len(all_words)
            if density < 0.4:
                score -= 25 * (0.4 - density) / 0.4

        # 2. 相邻段落重复率（Jaccard），过高 = 车轱辘话
        max_sim = 0.0
        for i in range(1, len(paras)):
            a = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', paras[i-1]))
            b = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', paras[i]))
            if a and b:
                sim = len(a & b) / len(a | b)
                max_sim = max(max_sim, sim)
        if max_sim > 0.6:
            score -= 30 * (max_sim - 0.6) / 0.4

        # 3. 实质信号缺失：无数字、无代码块、无列表/定义
        has_number = bool(re.search(r'\d', content))
        has_code = '```' in content
        has_list = bool(re.search(r'^\s*[-*]\s', content, re.MULTILINE))
        signals = sum([has_number, has_code, has_list])
        if signals == 0:
            score -= 20

        return max(0, score)

    def _score_completeness(self, content: str) -> float:
        score = 100.0
        if not re.search(r"^#\s", content, re.MULTILINE):
            score -= 25
        if "## 目录" not in content and "目录" not in content[:500]:
            score -= 15
        if "## 参考资料" not in content and "## 参考" not in content:
            score -= 20
        paragraphs = [p for p in content.split("\n\n") if len(p.strip()) > 30]
        if len(paragraphs) < 3:
            score -= 20 * (3 - len(paragraphs))
        return max(0, score)

    def _score_structure(self, content: str) -> float:
        score = 100.0
        headings = re.findall(r"^(#+)\s", content, re.MULTILINE)
        if not headings:
            return 30
        h1_count = headings.count("#")
        if h1_count == 0:
            score -= 20
        elif h1_count > 1:
            score -= 10
        levels = [len(h) for h in headings]
        for i in range(1, len(levels)):
            if levels[i] > levels[i - 1] + 1:
                score -= 5
        h2_count = headings.count("##")
        if h2_count < 2:
            score -= 15 * (2 - h2_count)
        return max(0, score)

    def _score_readability(self, content: str) -> float:
        score = 100.0
        lines = content.split("\n")
        long_lines = sum(1 for line in lines if len(line) > 120)
        score -= long_lines * 2
        if "```" not in content and "> " not in content:
            score -= 10
        if "- " not in content and "* " not in content:
            score -= 5
        return max(0, score)

    def _score_citations(self, content: str) -> float:
        score = 100.0
        refs = re.findall(r"\[([^\]]*)\]\(([^)]*)\)", content)
        if not refs:
            return 50
        for _title, url in refs:
            if not url or url.strip() == "":
                score -= 15
            elif url.startswith("https://example.com"):
                score -= 10
            elif not url.startswith(("http://", "https://", "#")):
                score -= 5
        return max(0, score)

    def _score_depth(self, content: str) -> float:
        score = 100.0
        word_count = len(content)
        para_count = len([p for p in content.split("\n\n") if len(p.strip()) > 30])
        if word_count < 200:
            score = 30
        elif word_count < 500:
            score = 50
        elif word_count < 1000:
            score = 70
        elif word_count > 5000:
            score = 95
        if para_count < 3:
            score = min(score, 40)
        return score

    def _overall_score(self, scores: dict[str, float],
                       weights: dict | None = None) -> float:
        weights = self._weights if weights is None else weights
        if not weights:
            return sum(scores.values()) / max(len(scores), 1)
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return sum(scores.values()) / max(len(scores), 1)
        total = sum(scores.get(k, 0) * w for k, w in weights.items())
        return total / total_weight  # type: ignore[no-any-return]

    def _score_breakdown(self, scores: dict[str, float]) -> str:
        parts = [f"{k}={v:.0f}" for k, v in sorted(scores.items())]
        return ", ".join(parts)

    # ── 风格检查（从 Profile 规则） ─────

    def _check_style(self, content: str,
                     rules: list[dict] | None = None) -> list[dict]:
        issues = []
        for rule in (self._style_rules if rules is None else rules):
            matches = rule["pattern"].findall(content)
            if matches:
                issues.append({
                    "rule": rule["name"],
                    "message": rule["message"],
                    "count": len(matches),
                    "penalty": rule["penalty"],
                })
        return issues

    # ── 引用验证（可禁用） ─────

    def _check_citations(self, content: str,
                         citation_cfg: dict | None = None) -> dict:
        cfg = self._citation_cfg if citation_cfg is None else citation_cfg
        refs = re.findall(r"\[([^\]]*)\]\(([^)]*)\)", content)
        seen_urls = {}  # type: ignore[var-annotated]
        issues = []
        for title, url in refs:
            if url in seen_urls:
                seen_urls[url]["count"] += 1
            else:
                seen_urls[url] = {"title": title, "count": 1}
            if not url:
                continue
            if cfg.get("check_url_format", True) and not url.startswith(
                ("http://", "https://", "#", "/")
            ):
                issues.append(f"非标准 URL: {url[:50]}")
            if cfg.get("check_empty_title", True) and not title.strip():
                issues.append("存在无标题链接")
        return {
            "total_refs": len(refs),
            "unique_urls": len(seen_urls),
            "duplicates": sum(1 for v in seen_urls.values() if v["count"] > 1),
            "issues": issues,
        }

    def handle_writer_done(self, msg: Message):
        payload = msg.payload
        content = payload.get("content", "")
        if content:
            self.publish("quality_gate.check", {
                "task_id": payload.get("task_id", ""),
                "content": content,
                "generation_count": payload.get("generation_count", 0),
            })
