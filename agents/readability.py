"""readability -- built-in agent (2026-10-11 folded in from examples/plugin-*).

Same shape as built-in agents/*.py: module-level AGENT_NAME plus one
BaseAgent subclass. Nothing from the loader AST blacklist is touched, so
no SANDBOX_TRUSTED declaration is needed or allowed.

Purpose: score how readable a Chinese/English mixed document is (sentence
length, sentence-count, and average characters per sentence), for the
quality gate to cite as a structural dimension. Pure stdlib, offline,
reproducible.
"""
from loom_core.base_agent import AgentStatus, BaseAgent, Message

SANDBOX_TRUSTED = True
AGENT_NAME = "readability"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "built-in agent: readability dimension (avg sentence length etc.) for quality gates"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["readability.input", "writer.done"]
OUTPUT_TOPICS = ["readability.done"]

_SENTENCE_ENDINGS = ("。", "！", "？", "!", "?", ".", ";", "；")


class ReadabilityAgent(BaseAgent):
    """Report avg sentence length / sentence count / long-sentence ratio."""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        cfg = msg.payload.get("config") or {}
        long_threshold = int(cfg.get("long_sentence_chars", 80) or 80)
        sentences = [s for s in self._split(content) if s.strip()]
        total_chars = sum(len(s) for s in sentences)
        avg = (total_chars / len(sentences)) if sentences else 0.0
        long_ratio = (sum(1 for s in sentences if len(s) > long_threshold)
                      / len(sentences)) if sentences else 0.0
        result = {
            "sentences": len(sentences),
            "avg_sentence_chars": round(avg, 2),
            "long_sentence_ratio": round(long_ratio, 4),
        }
        self.report(AgentStatus.RUNNING, f"{len(sentences)} sentences")
        self.publish("readability.done", {
            "task_id": msg.payload.get("task_id", ""), **result})
        return {"status": "ok", **result}

    @staticmethod
    def _split(text: str) -> list[str]:
        """Split on Chinese/ASCII sentence endings, keeping separators out."""
        current: list[str] = []
        out: list[str] = []
        for ch in text:
            current.append(ch)
            if ch in _SENTENCE_ENDINGS:
                out.append("".join(current))
                current = []
        if current:
            out.append("".join(current))
        return out
