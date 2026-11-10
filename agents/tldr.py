"""tldr -- built-in agent (2026-10-11 folded in from examples/plugin-*).

Built-in: declares SANDBOX_TRUSTED like the other shipped agents
(pure stdlib, nothing on the loader AST blacklist).

Purpose: an offline, deterministic TL;DR -- the section headings plus the
first sentence of each section. This is not an LLM summary and does not
pretend to be; it exists so the pipeline has a reproducible "quick read"
node for tests and non-LLM runs.
"""
import re

from loom_core.base_agent import AgentStatus, BaseAgent, Message

SANDBOX_TRUSTED = True
AGENT_NAME = "tldr"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "built-in agent: offline deterministic TL;DR (headings + first sentence)"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["tldr.input", "writer.done"]
OUTPUT_TOPICS = ["tldr.done"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]{4,}")


class TldrAgent(BaseAgent):
    """Build a deterministic TL;DR from headings and each section's opening."""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        cfg = msg.payload.get("config") or {}
        max_items = int(cfg.get("max_items", 8) or 8)
        items: list[str] = []
        matches = list(_HEADING_RE.finditer(content))
        for idx, match in enumerate(matches):
            level = len(match.group(1))
            title = match.group(2).strip()
            if level >= 2 and title:
                items.append(title)
            if len(items) >= max_items:
                break
            if level < 2:
                continue
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            body = content[start:end].strip()
            first = _SENTENCE_RE.match(body)
            if first and first.group(0).strip():
                items.append(first.group(0).strip())
        items = items[:max_items]
        summary = "\n".join(f"- {item}" for item in items)
        self.report(AgentStatus.RUNNING, f"{len(items)} items")
        self.publish("tldr.done", {
            "task_id": msg.payload.get("task_id", ""),
            "summary": summary,
            "items": items,
        })
        return {"status": "ok", "summary": summary, "items": items}
