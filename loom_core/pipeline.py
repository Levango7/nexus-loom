"""
PipelineOrchestrator v3.1 - 增强型流水线编排器
=============================================
核心特性：
  - DAG 并行执行（拓扑排序层级调度）
  - 断点续传支持（checkpoint + 恢复）
  - 自动重做（QualityGate 反馈循环）
  - Per-agent 熔断器 + 令牌桶限流
  - 指数退避重试（带 jitter）
  - 结构化日志 + Prometheus 指标 + 审计轨迹
  - 临时文件自动清理
  - Admin REST API + Dashboard
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .event_hook import emit_event
from .executor_factory import create_executor, shutdown_executor
from .naming import agent_of, family_of
from .observability import get_logger, get_metrics

if TYPE_CHECKING:  # pragma: no cover
    from .backfill import RunBackfill
    from .registry import AgentMeta
    from .scheduler import ExecutionNode, ExecutionPlan


_REDACT_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)")


def _redact_config(config):
    """递归脱敏配置副本：敏感键的叶子值替换为 ***redacted***，不影响原 config 对象"""
    if not isinstance(config, dict):
        return config
    redacted = {}
    for key, value in config.items():
        if isinstance(value, dict):
            redacted[key] = _redact_config(value)
        elif isinstance(value, list):
            redacted[key] = [
                _redact_config(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif _REDACT_KEY_RE.search(str(key)):
            redacted[key] = "***redacted***"
        else:
            redacted[key] = value
    return redacted


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"      # 新增：暂停状态（断点续传）
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepResult:
    """步骤执行结果"""
    step_name: str
    agent_name: str
    status: str  # success / failed / skipped
    started_at: float
    finished_at: float = 0  # 默认 0，finally 中会更新
    result: dict = field(default_factory=dict)
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000


@dataclass
class NodeConfig:
    """节点配置（替代 SimpleNamespace，提供类型安全的 agent_config）"""
    config: dict = field(default_factory=dict)
    pool_size: int = 1
    rate_limit: dict = field(default_factory=dict)
    circuit_breaker: dict = field(default_factory=dict)
    parallelism: dict = field(default_factory=dict)


@dataclass
class TaskNode:
    """DAG 任务节点（统一模型，兼容 ExecutionNode 接口）

    合并了原 TaskNode（运行时状态）和 ExecutionNode（计划配置）的职责，
    消除 SimpleNamespace 中间转换层。
    """
    name: str                          # 节点名称（对应 agent 名称）
    agent_name: str                    # 执行的 Agent
    dependencies: list[str] = field(default_factory=list)  # 依赖的节点名
    payload: dict = field(default_factory=dict)             # 执行载荷
    max_workers: int = 1               # 并行度（>1 时可拆分子任务）
    retry_count: int = 0               # 当前重试次数
    max_retries: int = 3               # 最大重试次数
    timeout: float = 300               # 超时时间（秒）
    rate_limit: dict = field(default_factory=dict)  # 限流配置
    # 原 ExecutionNode 字段（统一后无需 SimpleNamespace 转换）
    backoff: str = "exponential"       # 退避策略: exponential | linear | fixed
    initial_delay: float = 1.0         # 初始退避延迟（秒）
    agent_config: NodeConfig = field(default_factory=lambda: NodeConfig())

    # 运行时状态
    status: str = "pending"            # pending/running/success/failed/skipped
    #: 跳过原因："condition"=when 不成立（作者声明的可选分支，下游不受影响）；
    #: 空串 + status=skipped 表示因依赖失败被级联跳过。
    skip_reason: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""
    started_at: float = 0
    finished_at: float = 0
    attempts: int = 0


@dataclass
class PipelineTask:
    """增强型流水线任务"""
    id: str
    pipeline_name: str
    input_file: str
    config: dict
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    steps: list[StepResult] = field(default_factory=list)
    current_step: int = 0
    result: dict = field(default_factory=dict)
    # 落盘终点（由声明 WRITES_OUTPUT 的节点在执行成功时写入）：
    # 接口层直接读这两个字段，不再靠猜 agent 名去 result 里翻找
    output_path: str = ""
    output_content: str = ""
    started_at: float = 0
    finished_at: float = 0
    error: str = ""
    checkpoint_file: str | None = None  # 断点文件路径
    #: 任务归属（token 身份的 Principal.id；空串 = 无身份世界，如本机信任模式）。
    #: 作用域隔离由接口层（Admin API）按它过滤；引擎自身保持中立，
    #: CLI/MCP 单机场景不传即空。触发类提交记 "trigger:<名>"/"webhook:<名>"/"mcp"。
    owner: str = ""
    # DAG 执行相关
    dag_nodes: dict[str, TaskNode] = field(default_factory=dict)  # name -> TaskNode
    execution_order: list[list[str]] = field(default_factory=list)  # 拓扑层级
    #: 审批闸门（approval 节点）等待中的信息：{"nodes": [...], "messages": {...},
    #: "pipeline": ...}。非 None = 任务停在闸门前，只认 approve/reject（resume 被拒）。
    awaiting_approval: dict | None = None
    #: 已落定的审批决策流水：[{"nodes": [...], "decision": "approved"/"rejected",
    #: "note": ..., "reason": ..., "at": ts, "by": <客户端标识>}, ...]
    approvals: list = field(default_factory=list)
    #: 已发过的终态事件名（空串 = 还没发）。`cancel()` 先把状态置 cancelled 并即时发事件，
    #: 收尾链随后又按状态发一次 ⇒ 同一次运行出现两条 `task.cancelled`，钩子/审计看到两次收尾。
    #: 有了这个字段，"每个运行至多一条终态事件"成为**属性**而不是调用顺序的运气。
    terminal_event: str = ""
    # Per-task 取消事件（替代全局 _stop_event 实现精确取消）
    stop_event: threading.Event = field(default_factory=threading.Event)
    # Per-task 结果锁（保护 result dict 的多线程写入）
    result_lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pipeline": self.pipeline_name,
            "input": self.input_file,
            "status": self.status.value,
            "progress": self.progress,
            "awaiting_approval": dict(self.awaiting_approval) if self.awaiting_approval else None,
            "approvals": [dict(a) for a in self.approvals],
            "steps": [
                {
                    "name": s.step_name,
                    "agent": s.agent_name,
                    "status": s.status,
                    "duration_ms": round(s.duration_ms, 2),
                    "error": s.error,
                }
                for s in self.steps
            ],
            "owner": self.owner,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": round(self.finished_at - self.started_at, 2) if self.finished_at else None,
            "error": self.error,
        }

    # pickle 支持：PipelineTask 含 threading.Event / threading.Lock（均不可序列化），
    # __getstate__ 剥离同步原语，__setstate__ 重建全新实例。
    # 注意：重建的 stop_event 是反序列化侧的独立副本——父进程 cancel() 设置的信号
    # 不会传播到子进程（Python 多进程同步原语只能通过继承共享，无法经 pickle 传递）。
    # 进程模式（executor_type=process）下节点在子进程中会因 registry/bus 未重建
    # （DAGExecutor.from_config 未接线）而失败，重试在父进程执行、stop_event 有效；
    # 见 dag_executor._execute_node_worker 与 executor_factory.create_executor 的告警。
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state["stop_event"] = None
        state["result_lock"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        # 反序列化时重建同步原语（新进程内独立实例）
        if self.__dict__.get("stop_event") is None:
            self.__dict__["stop_event"] = threading.Event()
        if self.__dict__.get("result_lock") is None:
            self.__dict__["result_lock"] = threading.Lock()


class PipelineOrchestrator:
    """增强型流水线编排器"""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "bus" and hasattr(self, "_executor"):
            self._executor.bus = value

    def __init__(self, agents_dir: str = "agents", checkpoint_dir: str = "checkpoints"):
        # 统一配置中心
        from .config import ConfigCenter
        self.config = ConfigCenter(
            config_file=str(Path(__file__).parent.parent / "config.json"),
            auto_reload=True,
        )

        # 延迟导入避免循环依赖
        from .agent_loader import AgentLoader
        from .checkpoint_manager import CheckpointManager
        from .circuit_breaker import CircuitBreakerRegistry
        from .dag_executor import DAGExecutor
        from .message_bus_v3 import MessageBus
        from .rate_limiter import RateLimiterRegistry
        from .registry import Registry

        self.bus = MessageBus()
        self.registry = Registry()
        # 显式参数优先于 config 默认值。ConfigCenter 内置了 agents.dir='agents' /
        # checkpoint.dir='checkpoints'，用 get(key, 参数) 取等于永远取到默认值，
        # 于是 self.agents_dir 与真正喂给 AgentLoader 的目录可以不是同一个：
        # 父进程按参数注册、子进程 child_context 按 'agents' 重建，process 模式
        # 直接报"子进程上下文重建失败：agents 下未加载到任何 Agent"。
        # admin_api 还会把这个属性当诊断信息返回，两套值会让排查完全走偏。
        self.agents_dir = Path(agents_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        self._running_tasks: dict[str, PipelineTask] = {}
        self._task_callbacks: dict[str, list[Callable]] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        # 可观测性
        self._logger = get_logger()
        self._metrics = get_metrics()

        # ── 组件化 ──
        self._loader = AgentLoader(self.registry, self.bus, agents_dir, self._logger)
        self._checkpoint = CheckpointManager(checkpoint_dir, self._logger)

        # 熔断器注册中心
        self._cb_registry = CircuitBreakerRegistry()

        # 限流器注册中心
        self._rate_limiters = RateLimiterRegistry()

        def _locked_checkpoint_save(task: PipelineTask, *args, **kwargs):
            """checkpoint 保存必须持 task.result_lock：
            as_completed 逐节点保存时兄弟节点仍在 worker 线程经 _set_task_output
            写 task.result，无锁的 to_dict() 遍历可撞上 dict 并发变更
            （RuntimeError: dictionary changed size / 撕裂快照）"""
            lock = getattr(task, "result_lock", None)
            if lock is not None:
                with lock:
                    return self._checkpoint.save(task, *args, **kwargs)
            return self._checkpoint.save(task, *args, **kwargs)

        self._executor = DAGExecutor(
            self.registry, self.bus,
            self._cb_registry, self._rate_limiters,
            self._metrics, self._logger,
            stop_event=self._stop_event,
            checkpoint_save_fn=_locked_checkpoint_save,
            audit_log_fn=self._audit_log,
        )

        self._admin_api = None

        # 持久化任务队列
        from .task_queue import TaskQueue
        self.task_queue = TaskQueue()

        # 审批台账（跨重启/跨进程）：闸门待批与决策不再只活在内存。
        # Stage 3a 起事实源是 RunStore（approvals 投影 + approval.* 事件）。
        # 传的是取写口的闭包而不是实例：①建口惰性，构造引擎不该顺手建库；
        # ②`self._runstore` 是属性查找，测试与子类替换它之后审批立刻跟着换——
        #   绑成方法引用会把那一刻的绑定永久抄走（fail-closed 判据就测不到）。
        from .approvals import ApprovalStore
        self.approvals = ApprovalStore(run_store=lambda: self._runstore())

        # RunStore 写口（Stage 1 起）：恒开，没有开关（2026-10-21 移除退路）。
        # 建口是惰性的；失败仍只让本运行少一个观测面，但**不静默**——
        # `run_store_health()` 与 /api/health/deep 会报出来。
        # 属性名带下划线后缀区别（`_run_store`）：早先叫 `_runstore` 时它把同名方法
        # 遮住了，实例属性优先于类方法 ⇒ 每次调用都是 `'NoneType' object is not callable`。
        self._run_store: Any = None
        self._run_store_failed = False
        self._run_store_error: str = ""

        # 性能统计
        self._execution_stats: list[dict] = []
        self._max_task_history: int = 100

        # 上次执行的计划和输入（用于 rerun）
        self.last_plan = None
        self.last_input = None

    # ─── 委托：限流 ─────────────────────────────

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: dict | None = None,
                             timeout: float = 30.0) -> bool:
        return self._executor._acquire_rate_limit(agent_name, rate_limit_cfg, timeout)

    # ─── 委托：Agent 加载 ─────────────────────────────

    def discover_agents(self) -> list[str]:
        return self._loader.discover()

    def register_agents(self, agent_names: list[str] | None = None, config: dict | None = None) -> list[str]:
        loaded = self._loader.register(agent_names, config)
        if loaded:
            # 记录注册信息，供 process 执行模式在子进程内重建上下文使用
            # （见 dag_executor._get_child_context）
            self._executor.child_context = {
                "agents_dir": str(self.agents_dir),
                "agent_names": list(loaded),
                "config": config or {},
            }
        return loaded

    def _extract_meta(self, cls) -> AgentMeta:
        return self._loader._extract_meta(cls)

    # ─── 执行计划 ─────────────────────────────

    def _legacy_agent_order(self) -> list[str]:
        """legacy 自动图的准入：全体注册件，除去声明 `LEGACY_AUTO = False` 的那些。

        legacy 按设计不读 `pipelines/*.yaml`，因此没有 per-node config；任何"没配置就
        跑不出东西"的件（出网要 url、转换要模板）在这张图里必然业务失败——加一个这样的
        通用件就会打挂所有 legacy 跑。它们照旧注册、照旧被声明式流水线调用。
        """
        order = self.registry.deps_order()
        return [name for name in order
                if getattr(self.registry.get_meta(name), "legacy_auto", True)]

    def plan(self, pipeline_name: str, input_file: str,
             config: dict | None = None) -> list[dict]:
        """生成执行计划（预览）"""
        agent_order = self._legacy_agent_order()
        plan = []

        for i, name in enumerate(agent_order):  # type: ignore[var-annotated]
            meta = self.registry.get_meta(name)
            if meta:
                desc = meta.description or ""
                plan.append({
                    "step": i + 1,
                    "agent": name,
                    "priority": meta.priority,
                    "dependencies": meta.dependencies,
                    "description": desc[:50],
                })

        return plan

    def visualize_plan(self, plan: list[dict]) -> str:
        """可视化执行计划"""
        lines = ["执行计划:", "=" * 60]

        for step in plan:
            deps = ", ".join(step["dependencies"]) if step["dependencies"] else "-"
            lines.append(f"  Step {step['step']}: {step['agent']}")
            lines.append(f"    优先级: {step['priority']}")
            lines.append(f"    依赖: {deps}")
            lines.append(f"    描述: {step['description']}")
            lines.append("")

        return "\n".join(lines)

    # ─── 委托：DAG 构建与执行（通过 _executor 直接访问） ─────────────────────────────
    # 注：内部方法通过 self._executor 直接调用，无需逐一包装

    def _build_dag(self, agent_order: list[str], config: dict | None = None) -> tuple[dict[str, TaskNode], list[list[str]]]:
        return self._executor.build_dag(agent_order, config)

    def _execute_level(self, task: PipelineTask, level: list, input_file: str,
                       plan, executor) -> bool:
        return self._executor.execute_level(task, level, input_file, plan, executor)

    def _get_task_output(self, task: PipelineTask, key: str, default=None):
        return self._executor._get_task_output(task, key, default)

    def _set_task_output(self, task: PipelineTask, key: str, value, dag_node: bool = True):
        self._executor._set_task_output(task, key, value, dag_node)

    def _execute_node(self, task: PipelineTask, node: TaskNode, input_file: str, config: dict) -> dict:
        return self._executor.execute_node(task, node, input_file, config)

    def _execute_node_from_scheduler(self, task: PipelineTask, node: ExecutionNode,
                                     input_file: str, plan: ExecutionPlan) -> dict:
        return self._executor.execute_node_from_scheduler(task, node, input_file, plan)

    def _extract_queries(self, input_file: str, node: ExecutionNode) -> list[str]:
        return self._executor._extract_queries(input_file, node)

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        return self._executor._backoff_delay(backoff, initial_delay, attempt)

    def _circuit_breaker(self, node: ExecutionNode, task: PipelineTask) -> bool:
        return self._executor._circuit_breaker(node, task)

    def _circuit_breaker_success(self, node):
        self._executor._circuit_breaker_success(node)

    # ─── 流水线执行 ─────────────────────────────

    def _claim_terminal_event(self, task: PipelineTask, name: str) -> bool:
        """终态事件先到先得：同一次运行至多发一条收尾事件。

        `cancel()` 在把状态置 cancelled 的当下就发事件，收尾链随后按状态再发一次 ⇒
        钩子与审计看到两次收尾（谁先谁后取决于调用顺序）。各写入点保留自己的载荷，
        只由这里裁决"是否还发"。
        """
        if task.terminal_event or not name:
            return False
        task.terminal_event = name
        return True

    @staticmethod
    def _terminal_event_name(status: TaskStatus) -> str:
        """终态 → 事件名；非终态给空串（空串不参与"至多一次"的裁决）。"""
        return {TaskStatus.DONE: "task.completed",
                TaskStatus.FAILED: "task.failed",
                TaskStatus.CANCELLED: "task.cancelled"}.get(status, "")

    def _record_plan_terminal_state(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """声明式路径收尾里"运行事实"的两笔写入：队列终态 + 终态事件 + 投影盖章。

        同步与异步两条收尾共用这一个口——这两笔正是历史上异步版整段缺掉的部分
        （SSE 发起的运行因此在持久队列里查不到、事件面一条不发）。

        投影写失败时的口径（2026-10-24 拍板：标 incomplete 后继续）：队列终态照写、
        worker 继续跑，但这次运行在队列里留下 `projection='incomplete'`。顺序是
        **先悲观后升格**——先按 incomplete 写队列行，投影确认落地才升成 ok；反过来
        会留一个"队列说一切正常、投影里查无此运行"的窗口，而那正是静默丢运行的形状。
        """
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
        # 用 finish 而不是 update_status：终态只应写一次。worker 化之后，API 取消
        # （行已 cancelled）与流水线收尾会并发，无条件覆写会把取消洗成 done。
        try:
            self.task_queue.finish(task.id, status, error=task.error,
                                   projection="incomplete")
        except Exception as e:
            self._logger.log("warning", "task_queue.finish 失败",
                             task_id=task.id, error=str(e))

        self._emit_plan_terminal(task, plan)
        if self._runstore_close(task, plan):
            try:
                self.task_queue.set_projection(task.id, "ok")
            except Exception as e:  # noqa: BLE001
                # 升格失败 ⇒ 这行停在 incomplete，而投影其实是好的：偏保守，
                # 宁可让人多看一条"待补账"，也不要反过来把缺投影的运行报成正常。
                self._logger.log("warning", "投影健康位升格失败",
                                 task_id=task.id, error=str(e))

    def _emit_plan_terminal(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """声明式路径的终态事件——同步与异步收尾调同一个口，载荷逐字段一致。

        载荷形状是既有对外契约（`completed`: task_id/pipeline/duration/plan_id；
        `failed` 多一个 error；`cancelled` 无 duration/error），这里只做
        "至多一次"的裁决，不扩键、不改名。
        """
        name = self._terminal_event_name(task.status)
        if not self._claim_terminal_event(task, name):
            return
        payload: dict = {"task_id": task.id, "pipeline": plan.pipeline_name,
                         "plan_id": plan.plan_id}
        if name != "task.cancelled":
            payload["duration"] = task.finished_at - task.started_at
        if name == "task.failed":
            payload["error"] = task.error
        emit_event(name, payload)

    # ─── RunStore 双写（Stage 1：事件 + 投影同事务，旧写面一律不动） ───

    def _runstore(self):
        """惰性拿写口；建不起或此前已失败返回 None（失败原因留给健康检查）。

        本方法**自己兜住一切异常**：调用点在 try 之外，如果它能抛，"新观测面写坏就
        只降级不判红"这条承诺就成了空话（第一轮就撞在 `'NoneType' object is not callable`
        上——属性遮蔽同名方法，8 条用例直接 ERROR、6 条 FAILED）。

        没有开关之后，失败必须**吵**：`_run_store_error` 会被 `run_store_health()`
        报给 `/api/health/deep`，日志也从 warning 提为 error（只记首次，避免刷屏）。
        """
        if self._run_store_failed:
            return None
        try:
            from . import run_store

            if self._run_store is None:
                self._run_store = run_store.RunStore()
            return self._run_store
        except Exception as e:  # noqa: BLE001
            if not self._run_store_failed:
                self._run_store_failed = True
                self._run_store_error = str(e)
                self._log("error", "RunStore 建口失败：这次运行没有投影可读", error=str(e))
            return None

    def run_store_health(self) -> dict:
        """RunStore 的可观测状态（健康检查与排障用）。

        `status=degraded` 不是"功能关闭"，而是"这次进程写不出投影"——
        读侧仍会按数据边界回落到队列/账本，但新运行在投影里会缺席。

        两个计数来自队列（投影写不进去时唯一还活着的那一行），回答的是同一个问题的
        两面：`incomplete_runs` = 想写投影但没写成（要补账，是故障）；
        `legacy_runs` = 走的是从不写投影的 legacy 路径（按设计缺，不是故障）。
        读的人把它们混成一个数，就会把 deprecated 兜底路径报成数据丢失。
        """
        counts: dict[str, int | None] = {"incomplete_runs": None, "legacy_runs": None}
        try:
            counts["incomplete_runs"] = self.task_queue.count_projection("incomplete")
            counts["legacy_runs"] = self.task_queue.count_projection("legacy")
        except Exception as e:  # noqa: BLE001  健康检查不许因为队列读不动就整体 500
            self._logger.log("warning", "投影健康位计数失败", error=str(e))
        store = self._runstore()
        if store is None:
            return {"status": "degraded", "available": False,
                    "error": self._run_store_error, **counts}
        return {"status": "healthy", "available": True,
                "dialect": store.dialect,
                "schema_version": store.schema_version(), **counts}

    # ── 补账（投影写失败后的重放）────────────────────────

    def _backfill(self) -> RunBackfill:
        from .backfill import RunBackfill
        from .deliveries import DeliveryLedger

        return RunBackfill(self.task_queue, DeliveryLedger(), self._runstore)

    def replay_run(self, run_id: str, dry_run: bool = False) -> dict:
        """把一个 `incomplete` 运行按交付账本重投影（语义与边界见 `backfill.py`）。"""
        return self._backfill().replay(run_id, dry_run=dry_run)

    def replay_incomplete_runs(self, limit: int = 200, dry_run: bool = False) -> dict:
        """批量补账：当前所有 `incomplete` 运行。"""
        return self._backfill().replay_incomplete(limit=limit, dry_run=dry_run)

    def _runstore_open(self, task: PipelineTask, plan: ExecutionPlan,
                       input_file: str, trigger: str) -> None:
        store = self._runstore()
        if store is None:
            return
        try:
            store.open_run(
                task.id, workflow=plan.pipeline_name,
                # principal 取引擎当前知道的归属：SSE 与直跑今天传不出 owner，
                # 于是这里就是空串——列先有，身份由调用方补（设计 §2 末记的账）。
                principal=str(getattr(task, "owner", "") or ""),
                trigger=trigger,
                inputs={"input_file": input_file, "plan_id": plan.plan_id},
                started_at=task.started_at or None)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "RunStore open 失败", task_id=task.id, error=str(e))

    def _runstore_nodes(self, task: PipelineTask, level: list) -> None:
        """层跑完就落该层节点的投影。

        读的是**当时**的 `dag_node`，不借 `_build_delivery_record` 的形状——那样
        Stage 1 的 parity 门禁就变成"拿一份抄来的数据跟自己比"，射程为零。
        """
        store = self._runstore()
        if store is None:
            return
        rows: list[dict] = []
        for node in level:
            dag = task.dag_nodes.get(node.agent_name)
            if dag is None:
                continue
            result = dag.result or {}
            rows.append({
                "node": dag.name, "status": str(dag.status or ""),
                "attempts": int(dag.attempts or 1),
                "started_at": float(dag.started_at or 0.0),
                "finished_at": float(dag.finished_at or 0.0),
                "error": dag.error or "",
                # 只存摘要，不存正文：整篇文档进 result_json 是 §5 警告的写放大
                "result": {"keys": sorted(str(k) for k in result),
                           "chars": {str(k): len(str(v)) for k, v in result.items()}},
                "artifact_keys": sorted(str(k) for k in result)})
        try:
            store.record_nodes(task.id, rows)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "RunStore 节点投影失败",
                      task_id=task.id, nodes=[r["node"] for r in rows], error=str(e))

    def _runstore_close(self, task: PipelineTask, plan: ExecutionPlan) -> bool:
        """给投影盖章，返回**这次盖章是否真的落了**。

        调用方拿这个布尔决定队列行的 `projection` 标记升不升成 ok。False 有三种：
        写口整个建不起来（`runs.db` 打不开）、`close_run` 抛了、或投影里没有这条
        运行的行（`close_run` 的条件 UPDATE 影响 0 行 ⇒ 返回 None）。三种都意味着
        "这次运行在投影里查不到或停在 running"——必须能被发现，而不是只进日志。
        """
        store = self._runstore()
        if store is None:
            return False
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
        keys: set[str] = set()
        for dag in task.dag_nodes.values():
            keys.update(str(k) for k in (dag.result or {}))
        try:
            # 成本与质量的真值都不在引擎手里：成本在 `cost_log`（按 task_id 归集，
            # 靠节点执行期的 cost_task_scope 归属），质量总分在 `quality_runs`
            # （评分方写入）。这里只负责取回盖章；取不到按 0（见 run_metrics）。
            from .run_metrics import collect_run_metrics
            cost_usd, quality_score = collect_run_metrics(task.id)
            seq = store.close_run(task.id, status,
                                  finished_at=task.finished_at or None,
                                  error=task.error or "", artifacts=sorted(keys),
                                  cost_usd=cost_usd, quality_score=quality_score)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "RunStore 终态投影失败", task_id=task.id, error=str(e))
            return False
        return seq is not None

    def _legacy_plan_from_config(self, pipeline_name: str, config: dict) -> ExecutionPlan:
        """从 legacy config 构建 ExecutionPlan（B8 引擎收敛：legacy run() → run_plan()）。

        legacy 路径不读 pipelines/*.yaml，DAG 由 registry.deps_order() 构建。
        这里把 legacy 的 TaskNode + execution_order 转换为 ExecutionPlan，
        让 run_plan() 的统一引擎执行——legacy 因此获得 RunStore 投影、交付契约
        检查、审批闸门支持等全部声明式能力。

        配置入口统一：plan.raw = {"pipeline": config}，使得 _execute_plan 里的
        ``plan.raw.get("pipeline", {}).get("executor_type")`` 与 legacy 的
        ``config.get("executor_type")`` 等价。
        """
        from .scheduler import AgentConfig, ExecutionNode, ExecutionPlan

        agent_order = self._legacy_agent_order()
        nodes, execution_order = self._build_dag(agent_order, config)

        cb_cfg = config.get("circuit_breaker", {}) if config else {}
        levels: list[list[ExecutionNode]] = []
        for level_names in execution_order:
            level: list[ExecutionNode] = []
            for name in level_names:
                tn = nodes.get(name)
                if tn is None:
                    continue
                ac = AgentConfig(
                    name=name,
                    config=config or {},
                    pool_size=1,
                    rate_limit=tn.rate_limit,
                    circuit_breaker=cb_cfg,
                    timeout=tn.timeout,
                    retry={"max_retries": tn.max_retries} if tn.max_retries else {},
                    dependencies=list(tn.dependencies),
                )
                level.append(ExecutionNode(
                    agent_name=name,
                    agent_config=ac,
                    dependencies=list(tn.dependencies),
                    timeout=tn.timeout,
                    max_retries=tn.max_retries,
                    backoff=tn.backoff,
                    initial_delay=tn.initial_delay,
                ))
            levels.append(level)

        return ExecutionPlan(
            plan_id=f"legacy-{pipeline_name}",
            pipeline_name=pipeline_name,
            node_count=len(nodes),
            levels=levels,
            raw={"pipeline": config or {}},
            fail_fast=config.get("fail_fast", True) if config else True,
            checkpoint=config or {},
        )

    def run(self, task_id: str | None = None, pipeline_name: str = "",
            input_file: str = "", config: dict | None = None,
            wait: bool = True, resume: bool = False, owner: str = "") -> PipelineTask:
        """执行流水线（统一入口：YAML 声明式优先，legacy config 兜底）。

        B8 引擎收敛后，本方法是唯一入口：
        1. 有 ``config`` → 走 legacy 注册表依赖序（不读 pipelines/*.yaml）
        2. 无 ``config`` 但有 ``pipeline_name`` → 尝试 Scheduler.parse() 加载 YAML
        3. YAML 加载失败 → 回落到 legacy ``_legacy_plan_from_config()``

        两条路径最终都委托给 ``run_plan()`` 统一引擎执行。
        """
        plan = None
        if config is not None:
            # 显式 config → legacy 路径
            plan = self._legacy_plan_from_config(pipeline_name, config)
        elif pipeline_name:
            # 无 config 但有 pipeline_name → 尝试 YAML
            try:
                from .scheduler import Scheduler
                scheduler = Scheduler()
                plan = scheduler.parse(pipeline_name)
            except Exception as e:
                self._log("info", "YAML 加载失败，回落到 legacy 注册表依赖序",
                          pipeline=pipeline_name, error=str(e))
                plan = self._legacy_plan_from_config(pipeline_name, {})
        else:
            # 既无 config 也无 pipeline_name → 空 plan 走 legacy
            plan = self._legacy_plan_from_config("", {})

        return self.run_plan(plan, input_file=input_file, task_id=task_id,
                              wait=wait, resume=resume, owner=owner)

    def pause(self, task_id: str) -> bool:
        """暂停任务（在层级边界生效，支持断点续传）。

        语义说明：正在执行的 level 内节点不会被中断，执行器推进到下一 level 前
        检测到 PAUSED 即阻塞等待；边界 checkpoint 由执行循环在进入等待时保存
        （此时无并发节点写入，避免撕裂快照），pause() 本身不落盘。
        """
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.PAUSED
                # C7：不向 registry 广播 on_pause——agent 实例是共享单例，
                # 全量广播会让暂停任务 A 连带影响任务 B 的执行。基类 on_pause
                # 目前为 no-op 且无 agent 覆写；暂停语义由执行器在层边界按
                # per-task 状态（PAUSED → 阻塞等待）实现，与 cancel 的
                # stop_event 模式一致。
                return True
        return False

    def cancel(self, task_id: str) -> bool:
        """取消任务（per-task，不影响其他正在运行的任务）"""
        cancelled = False
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED]:
                task.status = TaskStatus.CANCELLED
                task.stop_event.set()  # per-task 取消信号
                if self._claim_terminal_event(task, "task.cancelled"):
                    emit_event("task.cancelled", {"task_id": task_id,
                                                   "pipeline": task.pipeline_name})
                cancelled = True
        if cancelled:
            # 台账写不能拖着锁做（收件箱是另一条查询路径）
            self._retire_pending_approvals(task_id, "运行已取消")
        return cancelled

    def _retire_pending_approvals(self, task_id: str, reason: str) -> None:
        """取消一个停在闸门上的运行时，把它的待批记录就地结掉。

        不结的后果是收件箱里留一条**点不掉的待办**：运行已经取消，approve 只会得到
        "未在审批等待中"，而人在收件箱里看到的仍是"等你确认"。结批用 `rejected`
        （不新增词汇）、`by=system` 且 reason 写明来由——事件流里仍是一条
        `approval.decided`，审计能看出这是系统结的而不是人批的。decide 落下去后
        立刻 `consume`：系统驳回同样不是给执行器的指令，不置消费位会被同 task_id
        的重跑经 `open_decision` 当闸门裁决吃掉（与 close_run 的结清同语义）。

        控制动作不能因为台账不可用而失败：这里只告警，取消本身照常生效。
        """
        try:
            row = self.approvals.pending_for_run(task_id)
            if not row:
                return
            # decide 返回 False = 行已被别人裁决（并发人工点了收件箱），不动它的
            # 消费位；只有系统亲手结掉的才消费，避免吃掉人的并发决定。
            if self.approvals.decide(str(row["request_id"]), "rejected",
                                     by="system", note="", reason=reason):
                self.approvals.consume(str(row["request_id"]))
            self._log("info", f"取消时结清待批：{reason}", task_id=task_id)
        except Exception as e:  # noqa: BLE001
            self._logger.log("warning", "取消时结清待批失败",
                             task_id=task_id, error=str(e))

    def resume(self, task_id: str) -> bool:
        """恢复暂停的任务（从断点续传）。

        审批闸门等待中的任务**拒绝 resume**：那种 PAUSED 不是"用户按了暂停"，
        而是流水线声明的人工关卡；resume 直通等于绕过审批，只认 approve/reject。
        """
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.awaiting_approval:
                self._log("warning", "任务在审批等待中：请用 approve/reject（resume 被拒）",
                          task_id=task_id)
                return False
            if task and task.status == TaskStatus.PAUSED:
                task.status = TaskStatus.RUNNING
                # C7：同 pause()——不向 registry 广播 on_resume（共享单例跨任务污染）
                self._log("info", "Pipeline resumed", task_id=task_id)
                return True
        return False

    def approve(self, task_id: str, note: str = "", by: str = "") -> tuple[bool, str]:
        """审批放行：等待中的闸门任务 → RUNNING，决策进台账与审计。

        两种形态都受理：
        - 任务还在本进程内存里 → 唤醒暂停线程（原路径）；
        - 进程已重启、任务不在内存 → 决定写进持久台账，并把队列行放回
          pending + `resume=1`，由 worker 从 checkpoint 续跑越过闸门。
          这是"人审"不再是一次性演示的关键：重启后仍可批准。

        返回 (ok, error)；error 非空时 ok=False 且任务状态未动——
        "没等在审批上的任务"不允许被"批准"，否则审计里会出现无中生有的决策。
        """
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task is not None:
                if not task.awaiting_approval or task.status != TaskStatus.PAUSED:
                    return False, (f"task {task_id} 未在审批等待中"
                                   f"（status={task.status.value}）")
                # fail-closed：先落台账再唤醒。写不进去就不能声称已批准——
                # 否则执行了却没有持久授权，重启后台账仍显示待批（复核确认）。
                recorded = self._decide_in_store(task_id, "approved", note, "", by)
                if recorded is None:
                    return False, (f"task {task_id} 审批台账不可用或决定冲突，"
                                   "拒绝放行")
                decision = self._decision_from_record(recorded)
                task.approvals.append(decision)
                task.awaiting_approval = None
                task.status = TaskStatus.RUNNING
                self._log("info", f"审批放行：{decision['nodes']}", task_id=task_id)
            else:
                row = self._pending_row(task_id)
                if row is None:
                    queue_row = self._queue_row(task_id)
                    if queue_row is None:
                        return False, f"task not found: {task_id}"
                    return False, (f"task {task_id} 未在审批等待中"
                                   f"（status={queue_row.get('status')}）")
                recorded = self._decide_in_ledger(str(row["request_id"]), "approved",
                                                  note, "", by)
                if recorded is None:
                    return False, f"task {task_id} 未在审批等待中（决定已被处理）"
                decision = self._decision_from_record(recorded)
                nodes = list(decision["nodes"])
                resumed = self.task_queue.mark_for_resume(task_id)
                self._log(
                    "info",
                    (f"审批放行（持久台账）：已放回队列续跑 {nodes}" if resumed else
                     f"审批放行（持久台账）：队列行不可续跑，仅记录决定 {nodes}"),
                    task_id=task_id)
        self._audit_approval(task_id, decision)
        emit_event("task.approved", {"task_id": task_id, "nodes": decision["nodes"]})
        return True, ""

    def _pending_row(self, task_id: str) -> dict | None:
        try:
            row = self.approvals.pending_for_run(task_id)
        except Exception:  # noqa: BLE001
            return None
        return row if isinstance(row, dict) else None

    def reject(self, task_id: str, reason: str = "", by: str = "") -> tuple[bool, str]:
        """审批驳回：任务终止（CANCELLED），原因进错误信息、台账与审计。

        与 approve 同样受理两种形态：内存里的线程被取消；已不在内存的任务
        只把决定写进台账并把队列行标记为 cancelled（下次续跑不会再执行）。

        最小闭环不做"驳回到另一分支"：驳回就是终止并可注明原因——
        分支语义需要一个尚不存在的评审工作流，先不猜（spec §9 决策 C）。
        """
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task is not None:
                if not task.awaiting_approval or task.status != TaskStatus.PAUSED:
                    return False, (f"task {task_id} 未在审批等待中"
                                   f"（status={task.status.value}）")
                recorded = self._decide_in_store(task_id, "rejected", "", reason, by)
                if recorded is None:
                    return False, (f"task {task_id} 审批台账不可用或决定冲突，"
                                   "拒绝驳回")
                decision = self._decision_from_record(recorded)
                task.approvals.append(decision)
                task.awaiting_approval = None
                task.error = (f"审批被驳回: {reason}" if reason else "审批被驳回")
                task.status = TaskStatus.CANCELLED
                task.stop_event.set()  # 与 cancel() 同款：执行循环见 CANCELLED/事件即退出
                self._log("warning",
                          f"审批驳回：{decision['nodes']} —— {reason or '无原因'}",
                          task_id=task_id)
            else:
                row = self._pending_row(task_id)
                if row is None:
                    queue_row = self._queue_row(task_id)
                    if queue_row is None:
                        return False, f"task not found: {task_id}"
                    return False, (f"task {task_id} 未在审批等待中"
                                   f"（status={queue_row.get('status')}）")
                recorded = self._decide_in_ledger(str(row["request_id"]), "rejected",
                                                  "", reason, by)
                if recorded is None:
                    return False, f"task {task_id} 未在审批等待中（决定已被处理）"
                decision = self._decision_from_record(recorded)
                self.task_queue.cancel(task_id)
                self._log("warning",
                          f"审批驳回（持久台账）：{decision['nodes']} —— "
                          f"{reason or '无原因'}",
                          task_id=task_id)
        self._audit_approval(task_id, decision)
        emit_event("task.rejected", {"task_id": task_id, "nodes": decision["nodes"],
                                     "reason": reason})
        return True, ""

    def _audit_approval(self, task_id: str, decision: dict) -> None:
        """审批决策落审计（复用 audit 通道约定；失败不炸调用方，记 warning）。"""
        try:
            from .audit import write_audit

            task = self._running_tasks.get(task_id)
            err = write_audit("approvals", {
                "task_id": task_id,
                "pipeline": task.pipeline_name if task else "",
                **decision,
            })
            if err:
                self._log("warning", "审批审计写盘失败", task_id=task_id, error=err)
        except Exception as e:  # noqa: BLE001  审计不许拖垮审批动作
            self._log("warning", "审批审计异常", task_id=task_id, error=str(e))

    # ─── 委托：断点续传 ─────────────────────────────

    def _save_checkpoint(self, task: PipelineTask, full_state: bool = False):
        # 收集 agent 快照
        agent_snapshots = {}
        for name in self.registry.list_agent_names():  # type: ignore[attr-defined]
            inst = self.registry.get_instance(name)
            if inst:
                try:
                    agent_snapshots[name] = inst.on_snapshot()
                except Exception as e:
                    self._log("warning", f"agent {name} on_snapshot 失败", error=str(e))
        self._checkpoint.save(task, full_state, agent_snapshots)

    def _load_checkpoint(self, task_id: str) -> PipelineTask | None:
        task, agent_snapshots = self._checkpoint.load(task_id)
        if task is not None and agent_snapshots:
            # 恢复 agent 快照
            for name, state in agent_snapshots.items():
                inst = self.registry.get_instance(name)
                if inst:
                    try:
                        inst.on_restore(state)
                    except Exception as e:
                        self._log("warning", f"agent {name} on_restore 失败", error=str(e))
        return task  # type: ignore[no-any-return]

    def _resume_snapshots_for(self, plan: ExecutionPlan, task_id: str) -> dict | None:
        """声明式路径的断点取数：返回可合并的节点快照；无断点/归属不符返回 None。

        归属校验是必须的：声明式调用方会自己拼 task_id，而断点文件属于哪条
        流水线只有文件自己知道。把别的流水线的快照合进来，节点名对不上时
        静默全量重跑、对上了就是用错结果——两种都不是"续传"。
        """
        loaded = self._load_checkpoint(task_id)
        if loaded is None:
            self._log("info", "未找到断点，按全新任务执行", task_id=task_id)
            return None
        if loaded.pipeline_name != plan.pipeline_name:
            self._log("warning", "断点属于另一条流水线，忽略断点",
                      task_id=task_id, checkpoint_pipeline=loaded.pipeline_name,
                      pipeline=plan.pipeline_name)
            return None
        snaps = getattr(loaded, "_resumed_node_snapshots", None) or {}
        self._log("info", "从断点恢复", task_id=task_id,
                  pipeline=plan.pipeline_name, restored_nodes=len(snaps))
        return snaps or None

    def _remove_checkpoint(self, task_id: str):
        self._checkpoint.remove(task_id)

    def _cleanup_old_checkpoints(self, max_age_days: int = 7):
        self._checkpoint.cleanup_old(max_age_days)

    # ─── 报告和回调 ─────────────────────────────

    def _merge_pooled_results(self, task: PipelineTask) -> None:
        """池化节点结果合并：将 *_pool_* 实例键的结果按节点家族聚合回基础名

        分组按 `family_of`（保留内联别名）而不是 `agent_of`：内联进来的
        `writer_pool_0__review` 与父图的 `writer_pool_0` 是两个不同节点，
        按 Agent 归并会把两段不同子流程的结果混成一团。
        """
        merged = set()
        for key in list(task.result.keys()):
            if "_pool_" in key:
                base = family_of(key)
                if base not in merged:
                    merged.add(base)
                    # 从所有 pool 实例合并结果
                    pooled_results = [
                        self._get_task_output(task, k) for k in task.result
                        if family_of(k) == base and self._get_task_output(task, k)
                    ]
                    if pooled_results:
                        # 合并 researcher 的 results 列表
                        meta = self.registry.get_meta(agent_of(base))
                        if getattr(meta, "results_merge", "") == "extend":
                            combined: dict = {"status": "ok", "task_id": task.id,
                                              "total": 0, "results": [],
                                              "query_count": 0, "engines_used": []}
                            for pr in pooled_results:
                                if isinstance(pr, dict):
                                    combined["results"].extend(pr.get("results", []))
                                    combined["total"] = len(combined["results"])
                                    combined["query_count"] += pr.get("query_count", 0)
                                    eng = pr.get("engines_used", [])
                                    combined["engines_used"] = list(
                                        set(combined["engines_used"]) | set(eng)
                                    )
                            self._set_task_output(task, base, combined)
                        else:
                            # 非 researcher 池：取第一个非空结果
                            for pr in pooled_results:
                                if pr:
                                    self._set_task_output(task, base, pr)
                                    break

    def _finalize_plan_task(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """计划任务收尾：进度/时间戳、队列状态、事件钩子、报告、临时文件与 checkpoint 清理"""
        task.progress = 100 if task.status == TaskStatus.DONE else task.progress
        task.finished_at = time.time()

        # update_status 必须有异常保护：本方法在 execute() 的 finally 链中执行，
        # TaskQueue 异常若外抛会中断后续报告/回调/清理（对照 _finalize_task_queue 的同款防护）
        self._record_plan_terminal_state(task, plan)

        self._log("info", f"Pipeline {task.status.value}",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  status=task.status.value, duration_sec=round(task.finished_at - task.started_at, 2),
                  error=task.error or "",
                  steps=len(task.steps))
        self._generate_report(task)
        self._notify_callbacks(task)
        self._deliver(task, plan)

        self.bus.publish("pipeline.finished", "orchestrator", {
            "task_id": task.id,
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
            "duration": task.finished_at - task.started_at,
            "plan_id": plan.plan_id,
        })

        self._cleanup_task_temp(task)

        if task.status == TaskStatus.DONE and not plan.checkpoint.get("keep_on_success", False):
            self._remove_checkpoint(task.id)

        with self._lock:
            self._trim_task_history()

    # ─── 审批闸门（approval 节点，spec §9 决策 C 最小闭环）────────

    def _consume_open_decision(self, task: PipelineTask,
                               nodes: list[str] | None = None) -> dict | None:
        """取该运行已作出、尚未消费的审批决定（续跑用）；取到即标记已消费。

        `nodes` 绑定当前闸门的节点集合：同一运行的多道闸门不能互借决定。
        """
        try:
            decision = self.approvals.open_decision(task.id, nodes=nodes)
        except Exception as e:  # noqa: BLE001  台账读失败按"未决定"处理，闸门照常挂起
            self._log("warning", "审批台账读取失败（按未决定处理）",
                      task_id=task.id, error=str(e))
            return None
        if decision is None:
            return None
        try:
            self.approvals.consume(str(decision["request_id"]))
        except Exception as e:  # noqa: BLE001
            self._log("warning", "审批决定消费标记写入失败", task_id=task.id,
                      error=str(e))
        task.approvals.append(self._decision_from_record(decision))
        return decision

    @staticmethod
    def _decision_from_record(record: dict) -> dict:
        """台账行 → 审计 / 内存历史用的决定形状（字段全部来自台账）。

        这几个字段此前是调用方各自 `time.time()` 拼出来的，于是同一个决定在台账、
        事件、审计里可能有两个时刻，对账只能靠容差；现在台账单点取值，没得糊。
        """
        return {"nodes": list(record.get("nodes") or []),
                "decision": str(record.get("status") or ""),
                "note": str(record.get("note") or ""),
                "reason": str(record.get("reason") or ""),
                "at": float(record.get("decided_at") or 0.0),
                "by": str(record.get("decided_by") or "")}

    def _decide_in_ledger(self, request_id: str, decision: str, note: str,
                          reason: str, by: str) -> dict | None:
        """决策落台账并取回权威行；None = 没落下去（已被决策 / 台账不可用）。

        返回 None 时调用方必须 fail-closed：台账不可用却回答"已批准"，等于
        执行了一个没有持久授权的决定（重启后台账仍显示待批）。
        """
        try:
            return self.approvals.decide_recorded(request_id, decision, by=by,
                                                  note=note, reason=reason)
        except Exception as e:  # noqa: BLE001
            self._log("error", "审批台账写入失败", error=str(e))
            return None

    def _decide_in_store(self, task_id: str, decision: str, note: str,
                         reason: str, by: str) -> dict | None:
        """内存路径上的决定同样落台账（同一张投影，不是另一面墙）。"""
        row = self._pending_row(task_id)
        if row is None:
            return None
        return self._decide_in_ledger(str(row["request_id"]), decision, note,
                                      reason, by)

    def _queue_row(self, task_id: str) -> dict | None:
        try:
            return self.task_queue.get(task_id)
        except Exception:  # noqa: BLE001
            return None

    # ─── 暂停边界与外部决定（同步/异步两条执行循环共用） ───────

    def _pause_boundary_snapshot(self, task: PipelineTask) -> None:
        """暂停边界必须是完整状态：浅检查点会覆盖掉 `_dag_nodes`/`_result`，
        续跑时已完成节点全部重跑（2026-10-20 复核确认，判据
        `tests/test_approvals.py::TestPauseCheckpointIsFullState`）。"""
        try:
            self._save_checkpoint(task, full_state=True)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "暂停边界 checkpoint 保存失败",
                      task_id=task.id, error=str(e))

    def _observe_external_decision(self, task: PipelineTask) -> str:
        """在暂停等待里观察**别的进程/别的调用方**写下的审批决定。

        返回 "waiting"（还没决定）/ "running"（已放行，继续执行）/
        "cancelled"（被驳回，终止）。两条执行循环都必须调它——只让线程版看到
        外部决定，异步版（SSE 起的那条）就会挂在闸门前等一个永远不会来的信号。
        """
        gate_nodes = list((task.awaiting_approval or {}).get("nodes") or [])
        decision = (self._consume_open_decision(task, gate_nodes)
                    if gate_nodes else None)
        if decision is None:
            return "waiting"
        if str(decision.get("status")) == "rejected":
            reason = str(decision.get("reason") or "")
            task.error = f"审批被驳回: {reason}" if reason else "审批被驳回"
            task.status = TaskStatus.CANCELLED
            task.stop_event.set()
            return "cancelled"
        task.awaiting_approval = None
        task.status = TaskStatus.RUNNING
        self._log("info", "审批放行（他进程批准）：继续执行", task_id=task.id)
        return "running"

    def _arm_approval_gate(self, task: PipelineTask, plan: ExecutionPlan,
                           level: list) -> list:
        """闸门预处理：为闸门建 dag_node；有"待批"闸门时把任务置 PAUSED。

        置 PAUSED 后由执行循环既有的暂停边界等待接管（含 checkpoint 保存、
        stop_event 取消），approve/reject 才有东西可作用。返回本层节点列表
        **原样**——过滤留给 `_settle_approval_gate`，计数与 checkpoint 语义
        都按原始层级走（闸门也是节点，放行即算完成）。
        """
        gates = [n for n in level if getattr(n, "approval", None) is not None]
        if not gates:
            return level
        pending: list[str] = []
        messages: dict[str, str] = {}
        for node in gates:
            dag_node = TaskNode(
                name=node.agent_name,
                agent_name=node.agent_name,
                dependencies=node.dependencies,
                timeout=node.timeout,
                max_retries=node.max_retries,
            )
            dag_node.attempts = 0
            task.dag_nodes[node.agent_name] = dag_node
            # when 不成立的闸门不挂起（结算时直接记 skipped，和普通条件节点同款）
            if self._executor._should_skip_by_condition(task, node, plan):
                continue
            pending.append(node.agent_name)
            messages[node.agent_name] = str((node.approval or {}).get("message", ""))
        # 闸门节点从"挂起"那一刻起就是运行事实的一部分。只在放行后的层尾登记的话，
        # 停在闸门上就被取消的运行会留下"账本有这一行、投影没有"的缺行
        # ——Stage 1 的三方对账门禁实测抓到过一次（run_nodes=['tldr'] vs 账本 ['release_gate','tldr']）。
        self._runstore_nodes(task, gates)
        if pending:
            decision = self._consume_open_decision(task, pending)
            if decision is not None:
                # 进程重启后续跑：决定来自持久台账，闸门不再二次挂起
                if str(decision.get("status")) == "rejected":
                    reason = str(decision.get("reason") or "")
                    task.error = f"审批被驳回: {reason}" if reason else "审批被驳回"
                    task.status = TaskStatus.CANCELLED
                    task.stop_event.set()
                    self._log("warning", "审批驳回（持久台账）：续跑终止",
                              task_id=task.id)
                else:
                    self._log("info", "审批放行（持久台账）：续跑越过闸门",
                              task_id=task.id)
                return level
            existing = self.approvals.pending_for_run(task.id)
            if existing is None:
                try:
                    self.approvals.request(
                        task.id, plan.pipeline_name, pending, messages,
                        principal=str(getattr(task, "owner", "") or ""))
                except Exception as e:  # noqa: BLE001  台账写失败不该让运行直接失败，
                    # 但必须响亮：内存路径仍然可用，只是重启后不可批
                    self._log("warning", "审批台账写入失败（重启后将无法审批）",
                              task_id=task.id, error=str(e))
            else:
                # 续跑在决定之前重新 arm：复用已有待批行，收件箱不出现重复待办
                pending = list(existing.get("nodes") or pending)
                messages = dict(existing.get("messages") or messages)
            task.awaiting_approval = {"nodes": pending, "messages": messages,
                                      "pipeline": plan.pipeline_name}
            task.status = TaskStatus.PAUSED
            self._log("info", f"审批闸门等待人工放行：{pending}", task_id=task.id)
        return level

    def _settle_approval_gate(self, task: PipelineTask, plan: ExecutionPlan,
                              level: list) -> list:
        """闸门结算（放行后）：标记闸门节点，返回仍需执行的节点列表。

        - 放行的闸门 → success，决策（含 note）进节点结果，下游可读
          `upstream.<gate>.status == "approved"`；
        - when 不成立的闸门 → skipped（condition），与普通条件节点同语义。
        """
        gates = [n for n in level if getattr(n, "approval", None) is not None]
        if not gates:
            return level
        note = ""
        for d in reversed(task.approvals):
            if d.get("decision") == "approved":
                note = str(d.get("note") or "")
                break
        for node in gates:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
                continue
            reason = self._executor._should_skip_by_condition(task, node, plan)
            if reason:
                self._executor._skip_node_by_condition(task, node, dag_node, plan, reason)
            else:
                self._executor.settle_approval_node(task, node, dag_node, plan, note=note)
        if task.awaiting_approval:
            task.awaiting_approval = None
        # 闸门已经过：决定不再需要被续跑消费（内存路径在 approve 时只落了台账）
        try:
            open_decision = self.approvals.open_decision(task.id)
            if open_decision is not None:
                self.approvals.consume(str(open_decision["request_id"]))
        except Exception as e:  # noqa: BLE001
            self._log("warning", "审批决定消费标记写入失败", task_id=task.id,
                      error=str(e))
        return [n for n in level if getattr(n, "approval", None) is None]

    def _execute_plan(self, task: PipelineTask, plan: ExecutionPlan, input_file: str) -> None:
        """执行计划的所有层级（run_plan 的内核，拆分为独立方法便于测试）。"""
        total_nodes = plan.node_count
        if total_nodes == 0:
            self._log("warning", "空计划（node_count=0）：无节点可执行，按空转处理直接结算",
                      task_id=task.id, pipeline=plan.pipeline_name)
        completed = 0

        for _level_idx, level in enumerate(plan.levels):
            if task.stop_event.is_set() or self._stop_event.is_set():
                task.status = TaskStatus.CANCELLED
                return

            # 审批闸门（approval 节点）：先把任务置 PAUSED 并记录待批信息，
            # 随后的暂停边界等待（下面的 while 循环）同时服务它——checkpoint
            # 语义、外部 pause、reject 取消都走同一条既有路径。
            level = self._arm_approval_gate(task, plan, level)

            # 暂停检查点（与异步版共用 `_pause_boundary_snapshot` + `_observe_external_decision`，
            # 两条循环的差异只剩"怎么睡"）
            paused_saved = False
            while task.status == TaskStatus.PAUSED:
                if not paused_saved:
                    self._pause_boundary_snapshot(task)
                    paused_saved = True
                if task.stop_event.is_set() or self._stop_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    return
                outcome = self._observe_external_decision(task)
                if outcome == "cancelled":
                    return
                if outcome == "running":
                    break
                task.stop_event.wait(0.5)
            if task.status == TaskStatus.CANCELLED:
                return

            # 闸门放行后结算（标记闸门节点），并把闸门从待执行集合里滤掉。
            # 计数仍按本层**原始**节点数推进（闸门也是节点，放行即算完成）。
            # `level_all` 同样按原始集合留档：RunStore 的节点投影必须覆盖闸门，
            # 否则"整层都是闸门"那一层一个节点都不会落，三方对账必然缺行。
            level_total = len(level)
            level_all = list(level)
            level = self._settle_approval_gate(task, plan, level)
            if not level:
                # 整层都是闸门（已放行）：本层无事可执行，直接推进
                completed += level_total
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100) if total_nodes else 100
                self._runstore_nodes(task, level_all)
                self._save_checkpoint(task, full_state=True)
                continue

            max_workers = max(
                (node.agent_config.parallelism.get("max_workers", 1) for node in level),
                default=1,
            )
            executor_type = plan.raw.get("pipeline", {}).get("executor_type", "thread")
            # 不用 with：进程池的 __exit__ 是 shutdown(wait=True)，子进程卡死时
            # 它会一直等下去，把宿主进程（CLI/daemon/pytest）一起拖住。
            self._executor.stuck_detected = False
            executor = create_executor(max_workers=max_workers, executor_type=executor_type)
            try:
                for node in level:
                    dag_node = TaskNode(
                        name=node.agent_name,
                        agent_name=node.agent_name,
                        dependencies=node.dependencies,
                        timeout=node.timeout,
                        max_retries=node.max_retries,
                    )
                    dag_node.attempts = 0
                    task.dag_nodes[node.agent_name] = dag_node

                level_ok = self._execute_level(task, level, input_file, plan, executor)
            finally:
                shutdown_executor(executor, force=self._executor.stuck_detected)

            if not level_ok:
                return

            completed += level_total
            task.current_step = completed
            task.progress = int((completed / total_nodes) * 100) if total_nodes else 100
            self._merge_pooled_results(task)
            self._runstore_nodes(task, level_all)
            self._save_checkpoint(task, full_state=True)

            if task.status == TaskStatus.FAILED:
                return

        if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
            # 交付契约：声明了落盘节点的流水线，必须真拿出交付物才算 done。
            # `fail_fast: false` 让末端节点保持 RUNNING，收尾就无条件盖成 DONE——
            # 实测 kb-docgen 摄入被跳过、知识库失败、writer 之后从未执行，
            # 却报 done + exit 0 且没有产物文件。没交付就不是 done。
            if self._sink_declared(plan) and not self._delivered(task, plan):
                reason = "声明了落盘节点却没有交付物（无 output_path/内容）"
                task.error = f"{task.error}；{reason}" if task.error else reason
                task.status = TaskStatus.FAILED
                self._log("error", "交付契约未满足：没有交付物，不能算 done",
                          task_id=task.id, error=task.error)
                return
            task.status = TaskStatus.DONE

    def _sink_node_names(self, plan: ExecutionPlan) -> list[str]:
        """计划里所有声明了 WRITES_OUTPUT 的节点名（落盘节点）。"""
        names: list[str] = []
        for level in getattr(plan, "levels", []) or []:
            for node in level:
                base = agent_of(node.agent_name)
                meta = self.registry.get_meta(base)
                if meta is not None and getattr(meta, "writes_output", False):
                    names.append(node.agent_name)
        return names

    def _sink_declared(self, plan: ExecutionPlan) -> bool:
        """计划里是否存在声明 WRITES_OUTPUT 的落盘节点。"""
        return bool(self._sink_node_names(plan))

    def _delivered(self, task: PipelineTask, plan: ExecutionPlan | None = None) -> bool:
        """交付物是否真的落地：文件存在，或有非空内联内容。

        例外：落盘节点全部被 `when` 条件主动跳过时，"这轮不需要交付物"是作者
        声明的意图，不算失败（但下面的收尾会留一条 warning 日志，别让它无声）。
        """
        path = str(getattr(task, "output_path", "") or "")
        if path and Path(path).exists():
            return True
        if str(getattr(task, "output_content", "") or "").strip():
            return True
        if plan is not None:
            sinks = self._sink_node_names(plan)
            nodes = getattr(task, "dag_nodes", {}) or {}
            skipped_by_condition = [n for n in sinks
                                    if getattr(nodes.get(n), "status", "") == "skipped"
                                    and getattr(nodes.get(n), "skip_reason", "") == "condition"]
            if sinks and len(skipped_by_condition) == len(sinks):
                self._log("warning", "落盘节点被条件跳过，本次无交付物（按声明）",
                          task_id=task.id, nodes=skipped_by_condition)
                return True
        return False

    def run_plan(self, plan: ExecutionPlan, input_file: str = "",
                task_id: str | None = None, wait: bool = True,
                resume: bool = False, owner: str = "") -> PipelineTask:
        """按 Scheduler 生成的 ExecutionPlan 执行流水线

        resume=True 时从 task_id 对应的断点恢复：已完成节点直接复用产物注入
        下游（DAGExecutor._reuse_completed_node），未完成节点重跑。此前
        `--resume` 只在 legacy 分支生效，声明式路径静默变成全新执行。
        """
        self.last_plan = plan  # type: ignore[assignment]
        self.last_input = input_file  # type: ignore[assignment]

        from .scheduler import ExecutionPlan as EP
        if not isinstance(plan, EP):
            raise TypeError("run_plan 需要 ExecutionPlan 实例")

        task_id = task_id or str(uuid.uuid4())[:8]
        resume_snaps = self._resume_snapshots_for(plan, task_id) if resume else None

        task = PipelineTask(
            id=task_id,
            pipeline_name=plan.pipeline_name,
            input_file=input_file,
            config=plan.raw.get("pipeline", {}),
            status=TaskStatus.RUNNING,
            owner=owner,
        )
        task.checkpoint_file = str(self.checkpoint_dir / f"{task_id}.json")
        if resume_snaps:
            task._resumed_node_snapshots = resume_snaps

        with self._lock:
            self._running_tasks[task.id] = task

        self.task_queue.submit(task.id, plan.pipeline_name, input_file,
                               plan.raw.get("pipeline", {}),
                               principal=str(getattr(task, "owner", "") or ""))
        try:
            self.task_queue.claim_direct(task.id)  # 直跑也占住行，防被 worker 重排
        except Exception as e:  # noqa: BLE001
            self._log("warning", "队列行占位失败", task_id=task.id, error=str(e))

        task.started_at = time.time()
        total_nodes = plan.node_count
        self._runstore_open(task, plan, input_file,
                            trigger="resume" if resume else "direct")

        emit_event("task.created", {"task_id": task.id, "pipeline": plan.pipeline_name,
                     "input_file": input_file, "plan_id": plan.plan_id, "total_nodes": total_nodes})
        emit_event("task.started", {"task_id": task.id, "pipeline": plan.pipeline_name, "started_at": task.started_at})

        self._log("info", "Pipeline started",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  input_file=input_file, plan_id=plan.plan_id,
                  total_nodes=total_nodes)

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": plan.pipeline_name,
            "input_file": input_file,
            "plan_id": plan.plan_id,
            "config": _redact_config(plan.raw.get("pipeline", {})),
        })

        def execute():
            try:
                self._execute_plan(task, plan, input_file)
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
            finally:
                self._finalize_plan_task(task, plan)

        if wait:
            execute()
        else:
            threading.Thread(target=execute, daemon=True).start()

        return task

    def _finalize_plan_task_async(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """async 任务收尾：与 `_finalize_plan_task` 共用同一笔"队列终态 + 终态事件"写入。

        2026-10-21 之前这里整段不写队列、不发事件，导致 SSE 发起的运行在持久队列里
        没有行、事件面一条不发——"UI 看得见、接口查不到"的根因。
        """
        task.progress = 100 if task.status == TaskStatus.DONE else task.progress
        task.finished_at = time.time()
        self._record_plan_terminal_state(task, plan)
        self._log("info", f"Pipeline {task.status.value} (async)",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  status=task.status.value, duration_sec=round(task.finished_at - task.started_at, 2),
                  error=task.error or "",
                  steps=len(task.steps))
        self._generate_report(task)
        self._notify_callbacks(task)
        self._deliver(task, plan)

        self.bus.publish("pipeline.finished", "orchestrator", {
            "task_id": task.id,
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
            "duration": task.finished_at - task.started_at,
            "plan_id": plan.plan_id,
        })

        self._cleanup_task_temp(task)

        if task.status == TaskStatus.DONE and not plan.checkpoint.get("keep_on_success", False):
            self._remove_checkpoint(task.id)

        with self._lock:
            self._trim_task_history()

    async def run_plan_async(self, plan: ExecutionPlan, input_file: str = "",
                             task_id: str | None = None,
                             resume: bool = False, owner: str = "") -> PipelineTask:
        """async 版 run_plan：在已有事件循环中直接 await，消除 asyncio.run 嵌套开销。

        与 run_plan(wait=True) 的区别：
        - 用 asyncio.gather + asyncio.to_thread 替代 ThreadPoolExecutor
        - 可在 async 上下文（如 SSE handler）中直接 await，无需 asyncio.run()
        - 逐层执行逻辑、池化合并、断点续传、报告生成均与 run_plan 一致
        """
        self.last_plan = plan  # type: ignore[assignment]
        self.last_input = input_file  # type: ignore[assignment]

        from .scheduler import ExecutionPlan as EP
        if not isinstance(plan, EP):
            raise TypeError("run_plan_async 需要 ExecutionPlan 实例")

        task_id = task_id or str(uuid.uuid4())[:8]
        resume_snaps = self._resume_snapshots_for(plan, task_id) if resume else None

        task = PipelineTask(
            id=task_id,
            pipeline_name=plan.pipeline_name,
            input_file=input_file,
            config=plan.raw.get("pipeline", {}),
            status=TaskStatus.RUNNING,
            owner=owner,
        )
        task.checkpoint_file = str(self.checkpoint_dir / f"{task_id}.json")
        if resume_snaps:
            task._resumed_node_snapshots = resume_snaps

        with self._lock:
            self._running_tasks[task.id] = task

        # 队列行与创建/启动事件：与同步版同一条登记（此前异步版什么都不写，
        # 于是 SSE 发起的运行在持久队列里查不到、事件面一条不发）
        self.task_queue.submit(task.id, plan.pipeline_name, input_file,
                               plan.raw.get("pipeline", {}),
                               principal=str(getattr(task, "owner", "") or ""))
        try:
            self.task_queue.claim_direct(task.id)  # 直跑也占住行，防被 worker 重排
        except Exception as e:  # noqa: BLE001
            self._log("warning", "队列行占位失败", task_id=task.id, error=str(e))

        task.started_at = time.time()
        total_nodes = plan.node_count
        if total_nodes == 0:
            self._log("warning", "空计划（node_count=0）：无节点可执行，按空转处理直接结算",
                      task_id=task.id, pipeline=plan.pipeline_name)
        self._runstore_open(task, plan, input_file,
                            trigger="stream-resume" if resume else "stream")

        emit_event("task.created", {"task_id": task.id, "pipeline": plan.pipeline_name,
                     "input_file": input_file, "plan_id": plan.plan_id, "total_nodes": total_nodes})
        emit_event("task.started", {"task_id": task.id, "pipeline": plan.pipeline_name, "started_at": task.started_at})

        self._log("info", "Pipeline started (async)",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  input_file=input_file, plan_id=plan.plan_id,
                  total_nodes=total_nodes)

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": plan.pipeline_name,
            "input_file": input_file,
            "plan_id": plan.plan_id,
            "config": _redact_config(plan.raw.get("pipeline", {})),
        })

        try:
            completed = 0
            for _level_idx, level in enumerate(plan.levels):
                if task.stop_event.is_set() or self._stop_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    break

                # 审批闸门：与同步版同一条机制（置 PAUSED → 走下面的暂停等待）
                level = self._arm_approval_gate(task, plan, level)

                # 暂停边界：与同步版共用同一对判据——完整状态快照 + 观察他进程的决定。
                # 缺后半段时，异步运行会挂在闸门前等一个"已经有人批了"的信号（无限等）。
                paused_saved = False
                while task.status == TaskStatus.PAUSED:
                    if not paused_saved:
                        self._pause_boundary_snapshot(task)
                        paused_saved = True
                    if task.stop_event.is_set() or self._stop_event.is_set():
                        task.status = TaskStatus.CANCELLED
                        break
                    outcome = self._observe_external_decision(task)
                    if outcome != "waiting":
                        break
                    await asyncio.sleep(0.5)
                if task.status == TaskStatus.CANCELLED:
                    break

                # 闸门放行后结算（计数按本层原始节点数推进）
                level_total = len(level)
                level_all = list(level)
                level = self._settle_approval_gate(task, plan, level)
                if not level:
                    completed += level_total
                    task.current_step = completed
                    task.progress = int((completed / total_nodes) * 100) if total_nodes else 100
                    self._runstore_nodes(task, level_all)
                    self._save_checkpoint(task, full_state=True)
                    continue

                for node in level:
                    dag_node = TaskNode(
                        name=node.agent_name,
                        agent_name=node.agent_name,
                        dependencies=node.dependencies,
                        timeout=node.timeout,
                        max_retries=node.max_retries,
                    )
                    dag_node.attempts = 0
                    task.dag_nodes[node.agent_name] = dag_node

                if not await self._executor.execute_level_async(task, level, input_file, plan):
                    break

                completed += level_total
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100) if total_nodes else 100

                # 池化节点结果合并（与 run_plan 共用同一实现）
                self._merge_pooled_results(task)
                self._runstore_nodes(task, level_all)

                self._save_checkpoint(task, full_state=True)

                if task.status == TaskStatus.FAILED:
                    break

            if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
                # 交付契约与同步版一致：声明了落盘节点就必须真拿出交付物，
                # 否则 SSE 触发的任务在没有产物时报 done，报表与用户都被骗过
                if self._sink_declared(plan) and not self._delivered(task, plan):
                    reason = "声明了落盘节点却没有交付物（无 output_path/内容）"
                    task.error = f"{task.error}；{reason}" if task.error else reason
                    task.status = TaskStatus.FAILED
                    self._log("error", "交付契约未满足：没有交付物，不能算 done",
                              task_id=task.id, error=task.error)
                else:
                    task.status = TaskStatus.DONE

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)

        finally:
            self._finalize_plan_task_async(task, plan)

        return task

    # ── 交付（结构化 JSON 落库 + 声明式 JSON 文件 / HTML 站点） ─────────────

    def _build_delivery_record(self, task: PipelineTask,
                               plan: ExecutionPlan) -> dict[str, Any]:
        """一轮运行的结构化记录：节点级状态/耗时 + 产物路径 + 指纹。

        这是交付账本（deliveries.db）与 deliver.json 文件共用的唯一形状。
        """
        # 指纹（A5）：topology_hash + 逐 agent config_hash
        # 复用 Scheduler 的静态方法，保证与 lockfile 生成/校验同一实现——
        # 两处各算一遍必然漂，锁就失去了意义。
        from .content_digest import artifact_digests
        from .scheduler import Scheduler
        topology_hash = Scheduler._topology_hash(plan)
        config_hashes: dict[str, str] = {}
        for level in getattr(plan, "levels", []) or []:
            for node in level:
                ac = getattr(node, "agent_config", None)
                if ac is not None:
                    config_hashes[node.agent_name] = Scheduler._node_config_hash(ac)

        nodes: list[dict[str, Any]] = []
        for level in getattr(plan, "levels", []) or []:
            for node in level:
                dag_node = task.dag_nodes.get(node.agent_name)
                if dag_node is None:
                    continue
                duration = None
                if dag_node.finished_at and dag_node.started_at:
                    duration = round(dag_node.finished_at - dag_node.started_at, 3)
                nodes.append({
                    "node": dag_node.name,
                    "agent": agent_of(dag_node.agent_name),
                    "status": dag_node.status,
                    "attempts": dag_node.attempts,
                    "duration_sec": duration,
                    "error": dag_node.error or "",
                    # 产物键也进账本：内存里的 task 会被 _trim_task_history 淘汰
                    # （只留最近 100 个），届时 RunHandle.artifacts() 只能从账本重建。
                    # 没产出的节点给空表——"这一节点跑过但没交付东西"与"不知道"要可区分。
                    "artifact_keys": sorted(str(k) for k in (dag_node.result or {})),
                    # 内容级摘要（A2 追溯面的补充）：只存键名的话，"当时交付的
                    # 到底是哪一份内容"事后无法核对——产物值活到进程结束，
                    # 这一份 sha256 是唯一的证据。
                    "artifacts": artifact_digests(dag_node.result),
                })
        finished = float(task.finished_at or 0.0)
        started = float(task.started_at or 0.0)

        # 产物 sha256（A2）：输出文件的内容哈希。文件不存在时给空串——
        # "没产出"与"产出了但文件丢了"要可区分。
        # output_bytes 沿用读侧 fmtBytes 的三态：None=没记录（无 output_path），
        # -1=声明了落盘路径但文件读不到，>=0=收尾时实测字节数。
        output_sha256 = ""
        output_bytes = None
        if task.output_path:
            output_bytes = -1
            try:
                from pathlib import Path
                p = Path(task.output_path)
                if p.is_file():
                    import hashlib
                    data = p.read_bytes()
                    output_sha256 = hashlib.sha256(data).hexdigest()
                    output_bytes = len(data)
            except OSError:
                pass

        return {
            "run_id": task.id,
            "pipeline": plan.pipeline_name,
            "plan_id": plan.plan_id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "input_file": task.input_file,
            "started_at": started,
            "finished_at": finished,
            "duration_sec": round(finished - started, 3) if finished else None,
            "output_path": task.output_path,
            "output_sha256": output_sha256,
            "output_bytes": output_bytes,
            "error": task.error or "",
            "topology_hash": topology_hash,
            "config_hashes": config_hashes,
            "nodes": nodes,
        }

    def _deliver(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """交付收尾（绝不外抛：交付失败记 warning，不把运行判红）。

        1. **落库**（默认开）：完整结构化记录进 deliveries.db（含失败运行——
           账本的意义就是失败也可查）；
        2. `pipeline.deliver.json: <path>` 声明时：同一记录写成 JSON 文件
           （仅 DONE——失败运行没有"交付物"可声明）；
        3. `pipeline.deliver.site: <dir>` 声明时：把本次产物构建成静态站点
           （仅 DONE 且有产物内容）。
        """
        try:
            rec = self._build_delivery_record(task, plan)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "构建交付记录失败", task_id=task.id, error=str(e))
            return

        # 1) 落库：DONE 与 FAILED 都记（账本要能查失败）
        try:
            from .deliveries import DeliveryLedger
            DeliveryLedger().record(rec)
        except Exception as e:  # noqa: BLE001
            self._log("warning", "交付落库失败", task_id=task.id, error=str(e))

        if task.status != TaskStatus.DONE:
            return
        deliver_cfg: dict[str, Any] = {}
        raw = getattr(plan, "raw", None)
        if isinstance(raw, dict):
            deliver_cfg = raw.get("pipeline", {}).get("deliver") or {}
        if not isinstance(deliver_cfg, dict):
            deliver_cfg = {}

        # 2) JSON 文件（声明式）
        json_path = str(deliver_cfg.get("json") or "").strip()
        if json_path:
            try:
                target = Path(json_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                self._log("info", "交付 JSON 已写入", task_id=task.id, path=str(target))
            except Exception as e:  # noqa: BLE001
                self._log("warning", "交付 JSON 写入失败",
                          task_id=task.id, error=str(e))

        # 3) HTML 站点（声明式）
        site_dir = str(deliver_cfg.get("site") or "").strip()
        if site_dir:
            try:
                markdown = ""
                output = str(task.output_path or "")
                if output and Path(output).suffix.lower() in (".md", ".markdown") \
                        and Path(output).exists():
                    markdown = Path(output).read_text(encoding="utf-8")
                elif str(task.output_content or "").strip():
                    markdown = task.output_content
                if not markdown.strip():
                    self._log("warning", "声明了 site 但没有 Markdown 产物可建站",
                              task_id=task.id)
                    return
                from .html_export import build_site
                info = build_site(
                    [{"title": f"{plan.pipeline_name} 交付",
                      "markdown": markdown, "slug": task.id}],
                    site_dir, site_title=f"{plan.pipeline_name} 交付站点")
                self._log("info", "交付站点已构建", task_id=task.id,
                          index=info["index"], pages=info["page_count"])
            except Exception as e:  # noqa: BLE001
                self._log("warning", "交付站点构建失败", task_id=task.id, error=str(e))

    def _generate_report(self, task: PipelineTask):
        """生成执行报告"""
        report_file = self.checkpoint_dir / f"report_{task.id}.json"
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._logger.log("error", "生成报告失败", error=str(e))

    def on_complete(self, task_id: str, callback: Callable[[PipelineTask], None]):
        """注册完成回调"""
        with self._lock:
            if task_id not in self._task_callbacks:
                self._task_callbacks[task_id] = []
            self._task_callbacks[task_id].append(callback)

    def _notify_callbacks(self, task: PipelineTask):
        """通知回调"""
        with self._lock:
            callbacks = self._task_callbacks.get(task.id, [])

        for callback in callbacks:
            try:
                callback(task)
            except Exception as e:
                self._logger.log("error", "回调执行失败", error=str(e))

    # ─── Audit Log ─────────────────────────────

    def _audit_log(self, task_id: str, agent_name: str,
                   input_summary: dict, output_summary: dict,
                   duration_ms: float, status: str, error: str = ""):
        """记录 agent 调用的完整审计轨迹"""
        try:
            entry = {
                "task_id": task_id,
                "agent": agent_name,
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "error": error,
                "input_keys": list(input_summary.keys()) if input_summary else [],
                "output_keys": list(output_summary.keys()) if output_summary else [],
                "input_size": len(json.dumps(input_summary, default=str)) if input_summary else 0,
                "output_size": len(json.dumps(output_summary, default=str)) if output_summary else 0,
                "timestamp": time.time(),
            }
            # 将审计记录 publish 到 audit topic（可通过订阅者持久化）
            self.bus.publish("pipeline.audit", "orchestrator", entry)
        except Exception as e:
            self._logger.log("error", "Audit log 失败", error=str(e))

    # ─── 查询 ─────────────────────────────

    def resolve_pipeline_name(self, requested: str = "") -> tuple[str, str]:
        """跑哪条流水线：默认值来自 config.default_pipeline，歧义不猜。

        `installed_pipelines` 是 `scheduler` 的**模块级**函数（类上没有同名方法），
        与 admin_api / mcp_server 的口径保持一致。
        """
        from .scheduler import installed_pipelines, resolve_pipeline_name
        return resolve_pipeline_name(
            requested,
            installed_pipelines(),
            str(self.config.get("default_pipeline", "") or ""),
        )

    def get_task(self, task_id: str) -> PipelineTask | None:
        return self._running_tasks.get(task_id)

    def run_handle(self, task_id: str):
        """取得一次运行的句柄：`status / wait / cancel / resume / artifacts`（spec §6 API 原语）。

        任务不存在时**也返回句柄**——`status()` 给 "unknown"、`artifacts()` 给空表。
        调用方因此只有一种对象形态要处理，不必"先判存在再分支"；但代价是
        `exists` 这件事得自己看 `status()`，所以别把句柄当持久收据用。
        """
        from .run_handle import RunHandle
        return RunHandle(self, task_id)

    def run_store(self):
        """RunStore 读口（与写侧同一个实例；开关关掉/建不起来给 None）。"""
        return self._runstore()

    def list_runs(self, principal: str | None = None,
                  limit: int = 100) -> list[dict]:
        """运行列表：内存缓存 ∪ RunStore 投影，按开始时间倒序。

        合并规则是"缓存补字段、投影保存在"：同一 run_id 以内存的为准（它比投影新
        ——比如刚取消、终态还没落完的那一瞬），但**投影里有而内存淘汰了的运行照样
        列出来**——这正是 Stage 2 要拆掉的旧前提（"内存里没有 = 不存在"）。

        `principal=None` 给全量（admin 面）；给了就只留自己名下。空串归属的行
        （无身份世界/历史遗留）对具名 principal 不可见，与 `/tasks` 既有口径一致。
        """
        rows: dict[str, dict] = {}
        store = self._runstore()
        if store is not None:
            try:
                for rec in store.list_runs(principal=principal, limit=max(limit, 100)):
                    rows[rec["run_id"]] = {
                        "id": rec["run_id"],
                        "status": rec["status"],
                        "pipeline": rec["workflow"],
                        "principal": rec.get("principal") or "",
                        "started_at": rec.get("started_at") or 0.0,
                        "created_at": rec.get("started_at") or None,
                        "finished_at": rec.get("finished_at") or None,
                        "error": rec.get("error") or None,
                        "progress": 0,
                        "steps": 0,
                        "source": "runstore",
                    }
            except Exception as e:  # noqa: BLE001 —— 投影读不动不该让列表接口整页 500
                self._log("warning", "RunStore 列表读取失败", error=str(e))
        for task in list(self._running_tasks.values()):
            owner = str(getattr(task, "owner", "") or "")
            if principal is not None and owner != principal:
                continue
            rows[task.id] = {
                "id": task.id,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "pipeline": task.pipeline_name,
                "principal": owner,
                "started_at": float(task.started_at or 0.0),
                "created_at": getattr(task, "created_at", None) or task.started_at or None,
                "finished_at": getattr(task, "finished_at", None) or None,
                "error": getattr(task, "error", None) or None,
                "progress": int(getattr(task, "progress", 0) or 0),
                "steps": len(getattr(task, "steps", None) or []),
                "source": "memory",
            }
        ordered = sorted(rows.values(), key=lambda r: r["started_at"] or 0.0, reverse=True)
        return ordered[:int(limit)]

    def recover_tasks(self) -> list[dict]:
        """恢复中断的任务：重启时把 running 状态改回 pending。

        返回被恢复的任务列表。调用方可选择重新执行：
            recovered = orch.recover_tasks()
            for t in recovered:
                plan = scheduler.parse(t["pipeline_name"])
                orch.run_plan(plan, input_file=t["input_file"], task_id=t["task_id"])

        内存侧同步：_running_tasks 中同 id 且仍为 RUNNING 的任务一并置为 PENDING，
        避免 get_task()/list_tasks() 返回与持久化队列不一致的旧状态
        （PAUSED/DONE 等其他状态不动）。
        """
        recovered = self.task_queue.recover()
        if not recovered:
            return recovered
        with self._lock:
            for entry in recovered:
                task = self._running_tasks.get(entry.get("task_id", ""))
                if task is not None and task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
        return recovered

    def list_queued_tasks(self, status: str = None) -> list[dict]:
        """列出持久化队列中的任务"""
        return self.task_queue.list_all(status=status)

    def replay_dlq(self, dlq_id: int) -> dict | None:
        """重放一条死信：重新执行故障 node 并回填结果

        支持两种死信：
        - REQUEST 消息（agent 故障）：重新 bus.request 执行该 node，回填 task.result
        - EVENT 消息：直接广播回原 topic
        返回重放结果 dict；若 dlq_id 不存在返回 None。
        """
        if not self.bus._store:
            return None
        entry = self.bus._store.get_dlq_entry(dlq_id)
        if not entry:
            return None

        payload = entry["payload"]          # Message.to_dict()
        topic = entry["topic"]               # 外层 topic（如 writer.input）
        inner = payload.get("payload", {})   # 原消息的业务 payload

        # 更新重放计数
        self.bus._store.replay_dlq(dlq_id)

        # 判断消息类型：REQUEST 由 orchestrator 重执行，EVENT 由总线广播
        msg_type = payload.get("msg_type", "event")
        if msg_type == "request" or topic.endswith(".input"):
            node_name = inner.get("node") or payload.get("to_agent") or topic.replace(".input", "")
            task_id = inner.get("task_id", "")
            # 真实自愈：直接重新执行故障 node 的 agent（与正常执行路径一致）
            result = None
            instance = self.registry.get_instance(node_name)
            if instance is not None:
                try:
                    from .message_bus_v3 import Message, MessageType
                    msg = Message(
                        topic=topic,
                        payload=inner,
                        msg_type=MessageType.REQUEST,
                        from_agent=payload.get("from_agent", "orchestrator.replay"),
                        to_agent=node_name,
                        correlation_id=payload.get("correlation_id", ""),
                        trace_id=payload.get("trace_id", ""),
                    )
                    result = instance.handle(msg)
                except Exception as e:
                    self._logger.error(f"replay node {node_name} 失败: {e}")
                    return {"node": node_name, "task_id": task_id, "error": str(e)}
            else:
                # fallback：路由到总线（若已注册 subscriber）
                result = self.bus.request(
                    topic=topic, from_a="orchestrator.replay",
                    to_a=node_name, payload=inner, timeout=300,
                )
            # 回填 task.result（若 task 仍在内存）
            with self._lock:
                task = self._running_tasks.get(task_id)
                if task and isinstance(result, dict):
                    self._set_task_output(task, node_name, result)
            # 广播完成事件，让依赖节点可继续
            self.bus.publish(f"{node_name}.done", "orchestrator.replay", result)
            return {"node": node_name, "task_id": task_id, "result": result}

        # EVENT 类型：直接广播回原 topic
        original_payload = payload.get("payload", payload)
        from_agent = payload.get("from_agent", "dlq_replay")
        self.bus.publish(topic, from_agent, original_payload)
        return {"topic": topic, "replayed": True}

    def list_tasks(self, status: TaskStatus | None = None) -> list[PipelineTask]:
        tasks = list(self._running_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_execution_stats(self) -> list[dict]:
        return self._execution_stats

    def refresh_config_if_changed(self) -> bool:
        """别的进程改过运行时配置（覆写文件 revision 变）就热加载并通知 Agent。

        P1 验收第三条「/api/config 热更新不再只对本进程生效」的消费端：
        worker 循环与 Admin API 各请求入口都调它。返回 True = 确实发生了热加载。
        """
        if not getattr(self, "config", None):
            return False
        if not self.config.poll_overlay():
            return False
        self._log("info", "运行时配置已被其他进程修改，本进程热加载",
                  revision=self.config.revision)
        self.notify_config_update(["*"])
        return True

    def notify_config_update(self, changed_keys: list[str] | None = None) -> int:
        """把配置变更通知给已注册且实现 on_config_update 的 Agent。返回通知数。"""
        notified = 0
        for agent_info in self.registry.list():
            name = agent_info.get("name", "")
            instance = self.registry.get_instance(name)
            if instance and hasattr(instance, "on_config_update"):
                try:
                    instance.on_config_update(changed_keys=changed_keys or ["*"])
                    notified += 1
                except Exception as e:  # noqa: BLE001  单个 Agent 的通知失败不该中断其余
                    self._log("warning", f"agent {name} on_config_update 失败", error=str(e))
        return notified

    def rerun(self, pipeline_name: str = "", input_file: str = "",
              task_id: str | None = None) -> PipelineTask:
        """用上一次执行的 plan + 输入重新跑一遍"""
        if self.last_plan is None:
            # 没有内存中的 plan，尝试从 pipeline_name 重建
            if not pipeline_name:
                raise RuntimeError("没有可重用的执行计划，请指定 pipeline_name")
            from .scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse(pipeline_name)
            input_file = input_file or "test_input.md"
        else:
            plan = self.last_plan
            input_file = input_file or self.last_input

        self._logger.log("info", "重新执行流水线", pipeline=plan.pipeline_name, input=input_file)
        return self.run_plan(plan, input_file=input_file, task_id=task_id, wait=False)

    def shutdown(self):
        """关闭编排器"""
        self._stop_event.set()
        self._cleanup_all_stale_temp(max_age_hours=24)
        self.bus.shutdown()
        self.task_queue.close_all()
        self.registry.shutdown()
        self.stop_admin_api()
        self._cleanup_old_checkpoints(max_age_days=7)

    def _trim_task_history(self):
        """限制 _running_tasks 历史大小，防止内存无限增长。
        仅在锁内调用。保留正在运行的任务和最近完成的任务。"""
        if len(self._running_tasks) <= self._max_task_history:
            return
        completed = sorted(
            [(tid, t) for tid, t in self._running_tasks.items()
             if t.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)],
            key=lambda x: x[1].finished_at or 0
        )
        excess = len(self._running_tasks) - self._max_task_history
        for tid, _ in completed[:excess]:
            self._running_tasks.pop(tid, None)

    def _cleanup_task_temp(self, task: PipelineTask):
        """任务完成后清理各 agent 产生的临时文件。

        动态遍历所有已注册 agent，凡实现 cleanup_task_temp 的均调用
        （契约见 BaseAgent.cleanup_task_temp），不再硬编码单个 agent 名。
        """
        try:
            cleaned = 0
            for agent_name in self.registry.list_agent_names():
                instance = self.registry.get_instance(agent_name)
                if instance is None or not hasattr(instance, "cleanup_task_temp"):
                    continue
                try:
                    cleaned += int(instance.cleanup_task_temp(task.id) or 0)
                except Exception as e:
                    self._log("warning", f"清理 {agent_name} 临时文件失败",
                              task_id=task.id, error=str(e))
            if cleaned:
                self._log("info", "任务临时文件已清理", task_id=task.id, removed=cleaned)
        except Exception as e:
            self._log("warning", "任务临时文件清理异常", task_id=task.id, error=str(e))

    def _cleanup_all_stale_temp(self, max_age_hours: int = 24):
        """清理所有 agent 的过期临时文件（动态遍历所有已注册 agent）。"""
        try:
            cleaned = 0
            for agent_name in self.registry.list_agent_names():
                instance = self.registry.get_instance(agent_name)
                if instance is None or not hasattr(instance, "cleanup_stale_temp"):
                    continue
                try:
                    cleaned += int(instance.cleanup_stale_temp(max_age_hours) or 0)
                except Exception as e:
                    self._log("warning", f"清理 {agent_name} 过期临时文件失败", error=str(e))
            if cleaned:
                self._log("info", "过期临时文件已清理",
                          removed=cleaned, max_age_hours=max_age_hours)
        except Exception as e:
            self._log("warning", "过期临时文件清理异常", error=str(e))

    def start_admin_api(self, host: str = "127.0.0.1", port: int = 8910,
                         serve_static: bool = False,
                         dashboard_dir: str | None = None,
                         tokens_file: str | None = None) -> bool:
        """启动管理 API"""
        from .admin_api import AdminAPI
        self._admin_api = AdminAPI(host=host, port=port,  # type: ignore[assignment]
                                   serve_static=serve_static,
                                   dashboard_dir=dashboard_dir,
                                   tokens_file=tokens_file)
        return self._admin_api.start(self)  # type: ignore[no-any-return,attr-defined]

    def stop_admin_api(self):
        if self._admin_api:
            self._admin_api.stop()
            self._admin_api = None

    def _log(self, level: str, msg: str, **kw):
        """结构化日志记录"""
        self._logger.log(level, msg, **kw)

    def __repr__(self):
        agents = self.registry.list()
        tasks = len(self._running_tasks)
        return f"<PipelineOrchestrator agents={len(agents)} tasks={tasks}>"
