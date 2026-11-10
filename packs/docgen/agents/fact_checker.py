"""
FactChecker Agent - 轻量事实核查插件（MVP）
=============================================
职责：
  - 从上游最终文档中提取"可验证声明"（数字/百分比/年份/版本号等客观陈述）
  - 对照检索源内容做一致性核查：
      · 无 LLM：归一化字符串包含匹配（零成本基线）
      · 有 LLM：批量语义判定（supported / refuted / unverifiable），失败回退字符串匹配
  - 在文档尾部附加「事实核查附注」，并把核查报告写入节点结果

设计边界（如实声明）：
  - 这是启发式核查而非完整性验证：只能发现"来源不支持的数字类声明"，
    不能保证文档事实全量正确。unverifiable ≠ 错误。
"""

import re

from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "fact_checker"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True
AGENT_VERSION = "1.0"
AGENT_DESC = "事实核查 Agent - 可验证声明提取 + 来源一致性核查"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 45
INPUT_TOPICS = ["checker.done", "fact_checker.check", "fact_checker.input"]
OUTPUT_TOPICS = ["fact_checker.done"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：核查后可能补注来源，重新导出正文
PRODUCES = {"content": "last"}
CONSUMES = ["content", "results"]
DEPENDENCIES = ["checker"]
CACHE_TTL = 0
RESPAWN = False
AGENT_TAGS = ["check", "facts"]

# 声明提取上限（控制 LLM 成本）
MAX_CLAIMS = 30
# 句子最小长度（过滤碎片）
_MIN_SENT_LEN = 12

# 数字类声明模式：百分比、带单位数值、年份、版本号、倍数/规模
# 版本号模式（C14 收紧）：裸小数（如"延迟 0.5 秒"的 0.5、"准确率 99.5%"）
# 不再误判为版本声明——要求 v 前缀或前置英文词（Kafka 3.5 / Redis 5.0），
# 中文单位数值已由第 2 条模式覆盖，不会漏提取
_CLAIM_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?%"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:万|亿|千万|百万|k|K|M|GB|MB|TB|ms|秒|分钟|小时|天|年|人|台|次|元|美元|美金)"),
    re.compile(r"\b(?:19|20)\d{2}\s*年"),
    re.compile(r"\b[Vv]\d+\.\d+(?:\.\d+)?"),
    # 尾部用 (?!\d) 而非 \b：CJK 字符也是 \w，"Python 3.11版本" 的 "3.11版"
    # 无词边界，尾部 \b 会导致整句漏提取
    re.compile(r"\b[A-Za-z]{2,}\s+\d+\.\d+(?:\.\d+)?(?!\d)"),
    re.compile(r"\d+\s*(?:倍|个并发|万级|亿级)"),
]

_SENT_SPLIT = re.compile(r"[。！？!?\n]")


def _normalize(text: str) -> str:
    """归一化：去空白/中英文标点差异，用于包含匹配。

    C14：数字间小数点必须保留——原实现把 '3.5' 去点成 '35'，来源中真实的
    '35' 会与之匹配，产生跨值误判 supported。先用占位符保护 \\d.\\d，
    剥离标点后还原，声明与语料两侧归一化保持一致。
    """
    protected = re.sub(r"(?<=\d)\.(?=\d)", "\x00", text)
    stripped = re.sub(
        r"[\s,，。.、;；:：!！?？'\"“”‘’()（）\[\]【】《》<>〈〉\-—~～·…]+",
        "", protected)
    return stripped.replace("\x00", ".")


class FactCheckerAgent(BaseAgent):
    """事实核查 Agent（MVP）"""

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
        self._max_claims = int(config.get("max_claims", MAX_CLAIMS))
        # LLM 核查总时长预算（秒）：必须显著小于节点 timeout，
        # 防止多供应商 failover 叠加导致 bus.request 整体超时而丢失核查结果
        self._llm_timeout = int(config.get("llm_timeout", 30))

    # ─── 主入口 ──────────────────────────────

    def handle(self, msg: Message) -> dict:
        payload = getattr(msg, "payload", {}) or {}
        content = payload.get("content", "") or ""
        sources = self._collect_sources(payload.get("dependencies_results") or {})

        if not content:
            return {"status": "skip", "message": "无上游内容，跳过核查"}

        claims = self._extract_claims(content)
        if not claims:
            self.report(AgentStatus.RUNNING, "未发现可验证的数字类声明")
            return {
                "status": "ok",
                "content": content,
                "claims": [],
                "summary": {"total": 0, "verified": 0, "unverified": 0},
                "report_markdown": "",
            }

        verdicts = self._verify_claims(claims, sources)

        verified = sum(1 for v in verdicts if v["verdict"] == "supported")
        unverified = sum(1 for v in verdicts if v["verdict"] != "supported")

        report_md = self._render_report(verdicts)
        annotated = content + "\n\n## 事实核查附注\n\n" + report_md if unverified else content

        self.log_info(
            f"事实核查完成: {len(claims)} 条声明, "
            f"{verified} 条来源支持, {unverified} 条未能核实")

        return {
            "status": "ok",
            "content": annotated,
            "claims": verdicts,
            "summary": {
                "total": len(claims),
                "verified": verified,
                "unverified": unverified,
            },
            "report_markdown": report_md,
        }

    # ─── 声明提取 ──────────────────────────────

    def _extract_claims(self, content: str) -> list[str]:
        """提取含数字特征的句子（去重、截断到 max_claims）"""
        claims: list[str] = []
        seen: set[str] = set()
        # 剔除代码块与引用链接行，避免误报
        cleaned = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        cleaned = re.sub(r"^>.*$", "", cleaned, flags=re.MULTILINE)

        for raw in _SENT_SPLIT.split(cleaned):
            sent = raw.strip()
            if len(sent) < _MIN_SENT_LEN or len(sent) > 300:
                continue
            if any(p.search(sent) for p in _CLAIM_PATTERNS):
                key = _normalize(sent)[:80]
                if key and key not in seen:
                    seen.add(key)
                    claims.append(sent)
                    if len(claims) >= self._max_claims:
                        break
        return claims

    # ─── 来源收集 ──────────────────────────────

    def _collect_sources(self, dep_results: dict) -> list[str]:
        """从依赖节点结果中收集检索源文本（results/articles/text/snippet/content）"""
        texts: list[str] = []

        def _walk(obj, depth: int = 0):
            if depth > 4 or obj is None:
                return
            if isinstance(obj, dict):
                for key in ("text", "snippet", "content", "summary"):
                    val = obj.get(key)
                    if isinstance(val, str) and len(val) > 50:
                        texts.append(val)
                for val in obj.values():
                    if isinstance(val, (list, dict)):
                        _walk(val, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item, depth + 1)

        _walk(dep_results)
        return texts

    # ─── 核查 ──────────────────────────────

    def _verify_claims(self, claims: list[str], sources: list[str]) -> list[dict]:
        """先尝试 LLM 语义判定，失败回退归一化包含匹配"""
        if sources and self._llm_available():
            try:
                return self._verify_with_llm(claims, sources)
            except Exception as e:
                self.log_warning(f"LLM 核查失败，回退字符串匹配: {e}")
        return self._verify_by_matching(claims, sources)

    def _llm_available(self) -> bool:
        try:
            from loom_core.llm_router import get_router
            router = get_router()
            return bool(router and router.get_active_providers())
        except Exception:
            return False

    def _verify_by_matching(self, claims: list[str], sources: list[str]) -> list[dict]:
        """零成本基线：声明归一化后在来源全文中查找数字特征串"""
        corpus = _normalize(" ".join(sources))
        results = []
        for claim in claims:
            # 取声明中的数字特征片段作为锚点
            anchors = [m.group() for p in _CLAIM_PATTERNS for m in p.finditer(claim)]
            supported = bool(anchors) and all(
                _normalize(anchor) in corpus for anchor in anchors[:3])
            results.append({
                "claim": claim,
                "verdict": "supported" if supported else "unverified",
                "method": "string-match",
            })
        return results

    def _verify_with_llm(self, claims: list[str], sources: list[str]) -> list[dict]:
        """LLM 批量语义判定（带总时长预算，防止多供应商 failover 超过节点 timeout）"""
        import time

        from loom_core.llm_router import get_router
        router = get_router()

        deadline = time.time() + self._llm_timeout
        source_excerpt = "\n---\n".join(sources)[:12000]
        claims_text = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
        prompt = (
            "你是事实核查员。根据以下检索来源资料，逐条判断声明的真实性。\n"
            f"来源资料:\n{source_excerpt}\n\n待核查声明:\n{claims_text}\n\n"
            '严格输出 JSON（不要其他文字）:\n'
            '{"verdicts": [{"index": 1, "verdict": "supported|refuted|unverifiable", '
            '"note": "一句话依据"}]}'
        )
        remaining = deadline - time.time()
        if remaining <= 1:
            raise ValueError("LLM 核查预算已耗尽")
        content, _provider = router.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=2048, temperature=0.1,
            timeout=max(5, int(remaining)))

        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError("LLM 未返回 JSON")
        data = __import__("json").loads(match.group())

        by_index = {v.get("index"): v for v in data.get("verdicts", [])}
        # C14：索引全体无效（LLM 用 0 基编号、整体偏移或空 verdicts）时，
        # 不能静默把全部声明降级为 unverifiable——抛错走 _verify_claims
        # 的既有回退路径（字符串匹配），保持核查可用
        valid = sum(1 for idx in by_index
                    if isinstance(idx, int) and 1 <= idx <= len(claims))
        if not by_index or valid == 0:
            raise ValueError("LLM 返回的 verdicts 索引全部无效")
        results = []
        for i, claim in enumerate(claims):
            v = by_index.get(i + 1, {})
            verdict = v.get("verdict", "unverifiable")
            if verdict not in ("supported", "refuted", "unverifiable"):
                verdict = "unverifiable"
            results.append({
                "claim": claim,
                "verdict": verdict,
                "note": v.get("note", ""),
                "method": "llm",
            })
        return results

    # ─── 报告 ──────────────────────────────

    def _render_report(self, verdicts: list[dict]) -> str:
        lines = [
            f"> 本附注由 fact_checker 自动生成（启发式核查，unverifiable 不代表错误）。"
            f"共核查 {len(verdicts)} 条数字类声明。",
            "",
        ]
        for v in verdicts:
            icon = {"supported": "✅", "refuted": "❌", "unverifiable": "❓"}.get(
                v["verdict"], "❓")
            note = f" — {v['note']}" if v.get("note") else ""
            lines.append(f"- {icon} `{v['verdict']}` {v['claim']}{note}")
        return "\n".join(lines)
