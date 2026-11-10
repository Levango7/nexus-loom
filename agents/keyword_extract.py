"""keyword_extract -- built-in agent (2026-10-11 folded in from examples/plugin-*).

Same shape as built-in agents/*.py: module-level AGENT_NAME plus one
BaseAgent subclass. Declares SANDBOX_TRUSTED like the other shipped built-ins
(pure stdlib, nothing on the loader AST blacklist).

Purpose: rank Chinese/English keywords from upstream text (frequency plus
stopword filtering) for downstream topic-coverage/quality gates. Pure
stdlib, offline, byte-for-byte reproducible.
"""
import re
from collections import Counter

from loom_core.base_agent import AgentStatus, BaseAgent, Message

SANDBOX_TRUSTED = True
AGENT_NAME = "keyword_extract"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "built-in agent: Chinese/English keyword extraction for topic coverage"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["keyword_extract.input", "writer.done"]
OUTPUT_TOPICS = ["keyword_extract.done"]

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+|[一-龥]{2,}")
_STOP_EN = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "you", "your", "not", "can", "will", "has", "have", "its", "our", "their",
    "use", "used", "using", "into", "out", "off", "over", "under", "then",
    "how", "what", "why", "when", "where", "which", "who", "all", "any",
})
_STOP_CN = frozenset({"我们", "你们", "他们", "以及", "因此", "但是", "如果", "所以", "一个",
                      "这个", "那个", "可以", "需要", "进行", "对于", "通过"})


class KeywordExtractAgent(BaseAgent):
    """Extract ranked keywords from payload["content"] per payload config."""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        cfg = msg.payload.get("config") or {}
        top_k = int(cfg.get("top_k", 20) or 20)
        min_len = int(cfg.get("min_len", 2) or 2)
        counts: Counter = Counter()
        for match in _TOKEN_RE.findall(content):
            token = match.strip().lower()
            if len(token) < min_len or token in _STOP_EN or token in _STOP_CN:
                continue
            counts[token] += 1
        ranked = [{"term": term, "count": n}
                  for term, n in counts.most_common(top_k)]
        self.report(AgentStatus.RUNNING, f"{len(ranked)} keywords")
        self.publish("keyword_extract.done", {
            "task_id": msg.payload.get("task_id", ""),
            "keywords": ranked,
            "unique_terms": len(counts),
        })
        return {"status": "ok", "keywords": ranked, "unique_terms": len(counts)}
