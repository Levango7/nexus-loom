"""fact_coverage -- built-in agent (2026-10-11 folded in from examples/plugin-*).

Built-in: declares SANDBOX_TRUSTED like the other shipped agents
(pure stdlib, nothing on the loader AST blacklist).

Purpose: measure how much of a document is backed by concrete evidence --
digits, code blocks, tables, links -- versus pure prose. Quality gates can
cite it as a dimension: an all-prose doc reads low, which is exactly the
FP-1 shape this repo fought before.
"""
import re

from loom_core.base_agent import AgentStatus, BaseAgent, Message

SANDBOX_TRUSTED = True
AGENT_NAME = "fact_coverage"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "built-in agent: evidence-density dimension for quality gates"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["fact_coverage.input", "writer.done"]
OUTPUT_TOPICS = ["fact_coverage.done"]

_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")
_CODE_FENCE_RE = re.compile(r"```")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_LINK_RE = re.compile(r"https?://[^\s)\]]+")


class FactCoverageAgent(BaseAgent):
    """Report evidence-density signals over payload['content']."""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        chars = len(content) or 1
        numbers = len(_DIGIT_RE.findall(content))
        fences = len(_CODE_FENCE_RE.findall(content)) // 2
        table_rows = len(_TABLE_ROW_RE.findall(content))
        links = len(_LINK_RE.findall(content))
        result = {
            "chars": len(content),
            "numbers": numbers,
            "numbers_per_1kb": round(numbers / chars * 1000, 2),
            "code_blocks": fences,
            "table_rows": table_rows,
            "links": links,
        }
        self.report(AgentStatus.RUNNING, f"{numbers} numbers, {fences} code blocks")
        self.publish("fact_coverage.done", {
            "task_id": msg.payload.get("task_id", ""), **result})
        return {"status": "ok", **result}
