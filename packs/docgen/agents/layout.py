"""Layout Agent v3.1 - 排版优化插件"""
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "layout"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "style": ('str', 'markdown'),
}
AGENT_VERSION = "3.1"
AGENT_DESC = "排版优化 Agent - 智能图表修复、表格对齐（不直接写文件，由 safe_writer 统一写入）"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 70
INPUT_TOPICS = ["checker.done", "layout.optimize", "layout.input"]
OUTPUT_TOPICS = ["layout.done"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：排版后重新导出正文，下游取最近产出者
PRODUCES = {"content": "last"}
CONSUMES = ["content"]
DEPENDENCIES = ["checker"]
CACHE_TTL = 0
RESPAWN = False

class LayoutAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._dry_run = config.get("dry_run", False)
        self.log_info(f"Layout v{AGENT_VERSION} 初始化完成")

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "开始排版优化...")
        payload = msg.payload
        content = payload.get("content", "")
        target = payload.get("target") or payload.get("file") or payload.get("target_file")

        if content:
            result = self._optimize_content(content)
            result["content"] = result.get("optimized", content)
            if target:
                result["target"] = target
            return result

        if not target:
            return {"status": "error", "message": "未指定文件或内容"}
        return self._optimize_file(target, content)

    def _optimize_content(self, content: str) -> dict:
        """直接优化 content 字符串（不依赖文件，DAG 流式传递模式）"""
        try:
            from scripts.layout_optimizer import LayoutOptimizer
            opt = LayoutOptimizer(content)
            opt.run()
            fixed = opt.get_result_stats().get("borders_fixed", 0) if hasattr(opt, "get_result_stats") else 0
            self.report(AgentStatus.RUNNING, f"修复 {fixed} 处边框")
            if not self._dry_run:
                return {"status": "ok", "optimized": opt.get_result(), "fixed": fixed}
            return {"status": "ok", "optimized": content, "fixed": fixed}
        except Exception as e:
            self.log_warning(f"排版优化失败（保留原文）: {e}")
            return {"status": "ok", "optimized": content, "fixed": 0}

    def _optimize_file(self, target: str, content: str = "") -> dict:
        """文件模式：读取文件、优化、返回结果（不直接写入，由 safe_writer 统一写入）"""
        import os
        target = str(Path(target).resolve())
        if not content:
            if not os.path.exists(target):
                return {"status": "error", "message": f"文件不存在: {target}"}
            try:
                with open(target, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                return {"status": "error", "message": f"读取文件失败: {e}"}
        try:
            from scripts.layout_optimizer import LayoutOptimizer
            opt = LayoutOptimizer(content)
            opt.run()
            fixed = opt.get_result_stats().get("borders_fixed", 0) if hasattr(opt, "get_result_stats") else 0
            self.report(AgentStatus.RUNNING, f"修复 {fixed} 处边框")
            optimized = opt.get_result() if not self._dry_run else content
            return {
                "status": "ok",
                "target": target,
                "content": optimized,
                "optimized": optimized,
                "fixed": fixed,
                "has_changes": fixed > 0,
            }
        except Exception as e:
            self.log_error(f"排版优化失败: {e}")
            return {"status": "error", "message": str(e)}

    def handle_checker_done(self, msg: Message):
        payload = msg.payload
        target = payload.get("target")
        content = payload.get("content", "")
        if target or content:
            self.report(AgentStatus.RUNNING, f"排版优化 {Path(target).name if target else 'content'}")
            if content:
                result = self._optimize_content(content)
                if target:
                    result["target"] = target
            else:
                result = self._optimize_file(target)  # type: ignore[arg-type]
            self.publish("layout.done", result)
