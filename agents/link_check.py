"""link_check -- built-in agent (2026-10-11 folded in from examples/plugin-*).

Built-in: declares SANDBOX_TRUSTED like the other shipped agents
(pure stdlib, nothing on the loader AST blacklist).

Purpose: extract the link inventory (markdown + bare URLs) from upstream
text so downstream fact checking can key off it. This agent never
dereferences URLs: fetching is the fetcher's job, and doing it here would
make the plugin network-bound and non-reproducible.
"""
import re

from loom_core.base_agent import AgentStatus, BaseAgent, Message

SANDBOX_TRUSTED = True
AGENT_NAME = "link_check"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "built-in agent: link inventory extraction (no fetch) for downstream fact checking"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["link_check.input", "writer.done"]
OUTPUT_TOPICS = ["link_check.done"]

_MD_LINK_RE = re.compile(r"\[([^\]]{0,200})\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![(\w])(https?://[^\s)\]]+)")


class LinkCheckAgent(BaseAgent):
    """Collect md-style and bare links, deduped, preserving first-seen order."""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        cfg = msg.payload.get("config") or {}
        limit = int(cfg.get("limit", 200) or 200)
        seen: set[str] = set()
        entries: list[dict] = []
        for label, url in _MD_LINK_RE.findall(content):
            if url in seen:
                continue
            seen.add(url)
            entries.append({"url": url, "label": label, "kind": "markdown"})
        for match in _BARE_URL_RE.finditer(content):
            url = match.group(1)
            if url in seen:
                continue
            seen.add(url)
            entries.append({"url": url, "label": "", "kind": "bare"})
        entries = entries[:limit]
        self.report(AgentStatus.RUNNING, f"{len(entries)} links")
        self.publish("link_check.done", {
            "task_id": msg.payload.get("task_id", ""),
            "links": entries,
            "unique_urls": len(entries),
        })
        return {"status": "ok", "links": entries, "unique_urls": len(entries)}
