"""
Checker Agent - 结构检查插件（改进版）
=========================================
改进点：
  - P0 失败不再 raise，改为返回状态码，避免破坏 checkpoint
  - 检查结果附带修复建议
  - 支持从消息 payload 传入 content（不必须读文件）
"""

import os
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "checker"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "fail_fast": ('bool', False),
    "block_on_p1": ('bool', False),
}
AGENT_VERSION = "1.1"
AGENT_DESC = "结构检查 Agent - P0/P1/P2/P3 分级质检"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 50
INPUT_TOPICS = ["writer.done", "checker.check", "checker.input"]
OUTPUT_TOPICS = ["checker.done", "checker.failed"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：只做结构检查，产出问题清单
PRODUCES: dict = {}
CONSUMES = ["content"]
DEPENDENCIES = ["quality_gate"]
CACHE_TTL = 0
RESPAWN = False
AGENT_TAGS = ["check", "quality"]


class CheckerAgent(BaseAgent):
    """
    结构检查 Agent

    P0 = 阻断（流水线终止）
    P1 = 严重（需修复后重检）
    P2 = 警告（记录，继续执行）
    P3 = 建议（仅记录）
    """

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
        # 修复 P0：移除手动重复订阅。BaseAgent.__init__ 已通过 INPUT_TOPICS
        # 自动订阅 writer.done / checker.check / checker.input 到 _wrapped_handle。
        # 原 __init__ 再次手动订阅导致每条消息被处理两次：
        #   - checker.check 被 handle 处理两次（一次经 _wrapped_handle，一次直接 handle）
        #   - writer.done 被 handle + _on_writer_done 各处理一次
        # 现改为：所有 topic 统一经 _wrapped_handle -> handle 分发，
        # handle 内部根据 msg.topic 路由到 _on_writer_done（保留 publish 协议）。

    def _on_writer_done(self, msg: Message):
        """Writer 完成后，自动触发检查"""
        payload = msg.payload
        target = payload.get("target") or payload.get("output")
        content = payload.get("content", "")
        task_id = payload.get("task_id", "")

        if not target and not content:
            return

        self.report(AgentStatus.RUNNING, f"自动检查 {Path(target).name if target else '内容'}")
        result = self._check(target, content=content)
        self.publish("checker.done", {**result, "task_id": task_id})

    def handle(self, msg: Message) -> dict | None:
        """处理消息：根据 topic 分发到自动检查或主动检查"""
        # writer.done 触发自动检查（保留原 _on_writer_done 的 publish("checker.done") 协议）
        if getattr(msg, "topic", "") == "writer.done":
            self._on_writer_done(msg)
            return None

        # 主动检查请求
        self.report(AgentStatus.RUNNING, "开始检查...")
        payload = msg.payload
        target = payload.get("target") or payload.get("file")
        content = payload.get("content", "")
        fix = payload.get("fix", False)

        if not target and not content:
            return {"status": "error", "message": "未指定文件或内容"}

        result = self._check(target, content=content, fix=fix,
                             block_on_p1=self._block_on_p1(payload))

        # P0 问题需要阻断，但不 raise，避免破坏 checkpoint
        if result.get("P0", 0) > 0:
            result["status"] = "blocked"
            self.log_error(f"P0 阻断: {result['P0']} 个问题，返回状态码供上游处理")
            self.publish("checker.failed", result)
            return result

        return result

    def _block_on_p1(self, payload: dict) -> bool:
        """节点 config 优先于构造期 config（DAG 里 YAML 写的值必须生效）。"""
        cfg = payload.get("config") or {}
        value = cfg.get("block_on_p1")
        if value is None:
            value = self.config.get("block_on_p1", False)
        return bool(value)

    def _check(self, target: str = None, content: str = "",
               fix: bool = False, block_on_p1: bool | None = None) -> dict:
        """调用 markdown_checker 核心逻辑"""
        # 读文件内容
        if not content and target:
            if not os.path.exists(target):
                return {"status": "error", "message": f"文件不存在: {target}"}
            with open(target, encoding="utf-8", errors="replace") as f:
                content = f.read()

        if not content:
            return {"status": "error", "message": "内容为空"}

        try:
            from scripts.markdown_checker import Checker

            filepath = target or "<content>"
            checker = Checker(content, filepath, fix=fix)
            result = checker.run()

            p0 = result["summary"].get("P0_blocking", 0)
            p1 = result["summary"].get("P1_severe", 0)
            p2 = result["summary"].get("P2_warning", 0)
            p3 = result["summary"].get("P3_suggestion", 0)

            # 契约分层：status 表示"这次检查跑没跑成"，verdict 表示"文档有没有问题"。
            # 以前 P1>0 直接把 status 写成 fail，而引擎把 fail 当业务失败 →
            # 下游 layout/safe_writer 全部被跳过。检查器完成了工作却因"发现了问题"
            # 而被判死，这与 pipeline 里 `checker.fail_fast: false` 的意图相反。
            # 需要"P1 也阻断"的场景请显式开 block_on_p1。
            verdict = "pass" if p0 == 0 and p1 == 0 else "fail"
            if block_on_p1 is None:
                block_on_p1 = bool(self.config.get("block_on_p1", False))
            status = "fail" if (verdict == "fail" and block_on_p1) else "ok"

            self.log_info(f"检查结果: P0={p0} P1={p1} P2={p2} P3={p3}  "
                          f"[verdict={verdict}{', block_on_p1' if block_on_p1 else ''}]")

            return {
                "status": status,
                "verdict": verdict,
                "target": target or "<content>",
                "P0": p0,
                "P1": p1,
                "P2": p2,
                "P3": p3,
                "total_issues": p0 + p1 + p2 + p3,
                "by_level": result.get("by_level", {}),
            }

        except ImportError:
            self.log_warning("markdown_checker 未找到，跳过检查")
            return {"status": "skip", "message": "checker 模块未找到"}
        except Exception as e:
            self.log_error(f"检查异常: {e}")
            return {"status": "error", "message": str(e)}
