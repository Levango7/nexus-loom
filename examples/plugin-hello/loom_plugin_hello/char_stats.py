"""char_stats —— 外部插件示例 Agent（entry_points 接入，见 ../README.md）。

与内置 `agents/*.py` 同构：模块级 `AGENT_NAME` + 一个 `BaseAgent` 子类。
本文件刻意保持"干净"——不碰加载器黑名单里的调用，因此**不需要**（也不应该）
声明 `SANDBOX_TRUSTED`：那个声明只留给随产品发布、由本仓维护的内置件。
"""
from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "char_stats"
AGENT_VERSION = "0.1.0"
AGENT_DESC = "外部插件示例：统计上游正文的字符数 / 行数 / 中文字符数"
AGENT_AUTHOR = "loom-example"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["char_stats.input", "writer.done"]
OUTPUT_TOPICS = ["char_stats.done"]


class CharStatsAgent(BaseAgent):
    """把 payload["content"] 的规模统计回传，供下游质量门/报告引用。"""

    def handle(self, msg: Message) -> dict | None:
        content = msg.payload.get("content") or ""
        self.report(AgentStatus.RUNNING, "统计中...")
        stats = {
            "chars": len(content),
            "lines": len(content.splitlines()),
            "cjk_chars": sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff"),
        }
        self.publish("char_stats.done",
                     {"task_id": msg.payload.get("task_id", ""), **stats})
        return {"status": "ok", **stats}
