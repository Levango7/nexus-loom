"""
loom_core v3 - 工作流引擎层（领域无关）
=========================================
DAG 编排、消息总线、重试/熔断/限流、检查点与 Agent 加载。
文档领域能力（渲染/摄入/增强）在 `loom_doc/`，本包不得反向依赖它
（依赖方向由 tests/test_layering.py 把关）。
取数层（JSON 序列化、HTML 解析内核、嵌入、知识库、搜索引擎）在独立库
`artesian`，登记为该测试里的 LOCAL_LIBRARY 一类。

核心特性：
  - 消息总线支持异步广播和死信队列 (v3)
  - Registry 支持健康检查和自动恢复
  - BaseAgent 支持结构化日志和性能统计
  - PipelineOrchestrator 支持 DAG 并行、断点续传、自动重做
  - Per-agent 熔断器 + 令牌桶限流
  - QualityGate 多维度评分 + 自动反馈循环
  - 临时文件自动清理
"""

# 版本必须在子模块导入之前绑定：`.mcp_server` 用 `from . import __version__`
# 取它，而本包在第 31 行就导入该子模块 —— 定义放在下面时那次导入发生在
# 包初始化中途，ImportError 被 except 吞掉，MCP 的 serverInfo.version 因此
# 长期报 "unknown"（而本模块第 18 行的示例文档写的是真实版本号）。
__version__ = "3.17.0"

from .agent_loader import AgentLoader
from .alert_manager import alert, clear_alerts, get_alerts
from .base_agent import AgentLogger, BaseAgent
from .bootstrap import StartupReport, quick_check, run_startup_check
from .cache_manager import CacheManager, all_stats, clear_all_caches, get_cache
from .checkpoint_manager import CheckpointManager
from .circuit_breaker import CircuitBreakerRegistry, backoff_with_jitter
from .client import LoomClient, LoomError, LoomTimeoutError
from .config import ConfigCenter
from .cost_tracker import CostTracker, calc_cost, estimate_tokens, get_cost_tracker
from .dag_executor import DAGExecutor
from .event_hook import EventHookManager, emit_event, get_hook_manager
from .executor_factory import SmartExecutor, create_executor, is_process_executor
from .llm_router import LLMRouter, get_router, reset_router
from .mcp_server import MCPServer, run_mcp_server
from .message_bus_v3 import (
    Message,
    MessageBus,
    MessageMetrics,
    MessageType,
)
from .message_store import MessagePriority, PersistentStore
from .openapi_spec import generate_spec
from .pipeline import NodeConfig, PipelineOrchestrator, PipelineTask, StepResult, TaskStatus
from .quality_feedback import QualityFeedback, get_quality_feedback, record_quality
from .registry import AgentMeta, AgentPriority, AgentStats, AgentStatus, Registry
from .scheduler import AgentConfig, ExecutionNode, ExecutionPlan, Scheduler
from .streaming import (
    StreamCallback,
    StreamEvent,
    StreamMetrics,
    get_callback,
    register_callback,
    unregister_callback,
)
from .task_queue import TaskQueue
from .version_manager import VersionEntry, VersionManager, get_version_manager

__all__ = [
    "ConfigCenter",
    "MessageBus", "Message", "MessageType", "MessagePriority", "MessageMetrics",
    "PersistentStore",
    "Registry", "AgentMeta", "AgentStatus", "AgentStats", "AgentPriority",
    "BaseAgent", "AgentLogger",
    "PipelineOrchestrator", "PipelineTask", "TaskStatus", "StepResult", "NodeConfig",
    "Scheduler", "ExecutionPlan", "ExecutionNode", "AgentConfig",
    "AgentLoader", "DAGExecutor", "CheckpointManager",
    "LLMRouter", "get_router", "reset_router",
    "CacheManager", "get_cache", "clear_all_caches", "all_stats",
    "StreamEvent", "StreamCallback", "StreamMetrics",
    "register_callback", "get_callback", "unregister_callback",
    "create_executor", "is_process_executor", "SmartExecutor",
    "run_startup_check", "quick_check", "StartupReport",
    "EventHookManager", "get_hook_manager", "emit_event",
    "VersionManager", "VersionEntry", "get_version_manager",
    "MCPServer", "run_mcp_server",
    "TaskQueue",
    "CostTracker", "get_cost_tracker", "estimate_tokens", "calc_cost",
    "alert", "clear_alerts", "get_alerts",
    "CircuitBreakerRegistry", "backoff_with_jitter",
    "generate_spec",
    "QualityFeedback", "get_quality_feedback", "record_quality",
    "LoomClient", "LoomError", "LoomTimeoutError",
]
