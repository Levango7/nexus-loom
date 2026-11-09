"""DAG 执行器 —— 负责 DAG 构建、节点调度、熔断器、限流、指标"""
from __future__ import annotations

import asyncio
import copy
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .artifacts import collect_artifacts, normalize_declaration, output_artifact_name
from .cache_manager import CacheManager
from .circuit_breaker import backoff_with_jitter
from .conditions import UNRESOLVED
from .conditions import evaluate as _evaluate_condition
from .conditions import resolve as _resolve_path
from .executor_factory import is_process_executor
from .naming import agent_of, family_of

# ─── 模块级函数：支持 ProcessPoolExecutor pickle ──────────────────

# 产出保真底线失败的前缀标记：错误串经重试路径只剩文本，用前把"硬失败"
# 与可容忍的软失败区分开，使 fail_fast=false 也不能把它兑成 done
_HARD_FLOOR_PREFIX = "HARD_FLOOR:"

# Agent 的业务失败语义。`status: "error"` 必须算失败：ingest 抢占
# researcher.input 那次，回执是 {"status":"error","message":"未指定待摄入文件"}
# ——没有 "error" 键，旧判据只看 blocked/fail 与 "error" 键，于是这份失败
# 被当成功记进 dag_node.result，下游拿到一份"成功"的空产物。
_BUSINESS_FAILURE_STATUSES = ("error", "blocked", "fail")

# 子进程上下文缓存（每个 worker 进程仅重建一次；Windows spawn 下模块级状态按进程隔离）
_CHILD_CONTEXT_LOCK = threading.Lock()
_CHILD_CONTEXT = None
_CHILD_CONTEXT_KEY = None

# 进程模式单层等待的保底上限（秒），见 _process_level_ceiling
_PROCESS_LEVEL_CEILING_MIN = 60.0


def _process_level_ceiling(plan, level: list) -> float:
    """process 模式下单层 futures 的等待上限（秒）。

    线程模式的节点在 bus 请求超时里自行收尾，父进程总能拿到返回；子进程不
    一样——一旦卡死（fork 继承到被别的线程持有的锁、子进程被信号打断等），
    父进程手里没有任何信号可等，`as_completed` 会无限阻塞，紧接着进程池
    `shutdown(wait=True)` 也一起挂住。CI 上实测过这一串：pytest 已经打印
    汇总，进程仍空转 47 分钟，直到整条 job 被 60 分钟上限杀掉。

    上限取流水线声明 timeout 的两倍（留出子进程冷启动与装载开销）；没声明
    就按本层节点 timeout 合计的两倍。保底 60 秒，避免极小的声明值把正常的
    冷启动误判成卡死。
    """
    raw = getattr(plan, "raw", None) or {}
    declared = (raw.get("pipeline") or {}).get("timeout")
    try:
        declared_f = float(declared or 0)
    except (TypeError, ValueError):
        declared_f = 0.0
    if declared_f <= 0:
        try:
            declared_f = sum(float(getattr(n, "timeout", 0) or 0) for n in level)
        except (TypeError, ValueError):
            declared_f = 0.0
    return max(declared_f * 2, _PROCESS_LEVEL_CEILING_MIN)


def _build_child_context(ctx_cfg: dict):
    """在子进程内重建最小可执行上下文：(registry, bus)。

    - Registry 关闭健康检查线程（子进程无需活性探测）
    - MessageBus 使用非持久化模式：节点执行所需输入来自任务载荷而非总线，
      子进程内的 publish 事件不回传父进程（已知限制，见 docs/architecture.md §5）
    """
    from .agent_loader import AgentLoader
    from .message_bus_v3 import MessageBus
    from .registry import Registry

    registry = Registry(enable_health_check=False)
    bus = MessageBus(enable_persistence=False)
    loader = AgentLoader(registry, bus, ctx_cfg.get("agents_dir", "agents"))
    loaded = loader.register(
        ctx_cfg.get("agent_names"),
        ctx_cfg.get("config") or {},
    )
    if not loaded:
        raise RuntimeError(
            f"子进程上下文重建失败：{ctx_cfg.get('agents_dir')} 下未加载到任何 Agent"
        )
    return registry, bus


def _get_child_context(ctx_cfg: dict):
    """获取（或构建）本 worker 进程的缓存上下文"""
    global _CHILD_CONTEXT, _CHILD_CONTEXT_KEY
    key = (
        str(ctx_cfg.get("agents_dir")),
        repr(sorted((ctx_cfg.get("config") or {}).items()))[:512],
        repr(ctx_cfg.get("agent_names")),
    )
    with _CHILD_CONTEXT_LOCK:
        if _CHILD_CONTEXT is None or key != _CHILD_CONTEXT_KEY:
            _CHILD_CONTEXT = _build_child_context(ctx_cfg)
            _CHILD_CONTEXT_KEY = key
    return _CHILD_CONTEXT


def _execute_node_worker(dag_executor, task, node, input_file, plan):
    """模块级节点执行函数 —— ProcessPoolExecutor 子进程入口。

    dag_executor 经 pickle 传入时非序列化属性为 None；若构造时携带
    child_context 配置，则在本进程内一次性重建 registry/bus 后执行节点，
    结果 dict 作为返回值 pickle 回父进程。
    """
    if dag_executor.registry is None or dag_executor.bus is None:
        ctx_cfg = getattr(dag_executor, "child_context", None)
        if not ctx_cfg:
            raise RuntimeError(
                "ProcessPoolExecutor 模式下 registry/bus 不可用，且 DAGExecutor "
                "未携带 child_context 配置（无法在子进程重建上下文）。"
                "请先通过 PipelineOrchestrator.register_agents() 注册 Agent，"
                "或使用 ThreadPoolExecutor（默认）模式。"
            )
        registry, bus = _get_child_context(ctx_cfg)
        dag_executor.registry = registry
        dag_executor.bus = bus
    return dag_executor.execute_node_from_scheduler(task, node, input_file, plan)


class DAGExecutor:
    """DAG 构建和执行"""

    def __init__(self, registry, bus, cb_registry, rate_limiters, metrics, logger=None,
                 stop_event: threading.Event | None = None,
                 checkpoint_save_fn: Callable | None = None,
                 audit_log_fn: Callable | None = None,
                 child_context: dict | None = None):
        self.registry = registry
        self.bus = bus
        self._cb_registry = cb_registry
        self._rate_limiters = rate_limiters
        self._metrics = metrics
        self._logger = logger
        self._stop_event = stop_event
        self._checkpoint_save = checkpoint_save_fn
        self._audit_log = audit_log_fn
        # process 模式子进程上下文重建配置（可 pickle 的纯数据）：
        # {"agents_dir": str, "agent_names": list[str] | None, "config": dict}
        self.child_context = child_context
        self._execution_stats: list[dict] = []
        self._query_cache = CacheManager(name="dag_queries", max_size=100, ttl=3600)
        # 本轮是否出现过"子进程超极限未返回"——run_plan 据此决定进程池是
        # 正常 shutdown(wait=True) 还是强制回收（等它只会把宿主进程一起挂住）
        self.stuck_detected = False

    # ─── pickle 支持（ProcessPoolExecutor 兼容） ─────────────

    # 非可序列化属性列表：pickle 时置 None，子进程在 _execute_node_worker 中重建
    _NON_PICKLABLE_ATTRS = (
        "registry", "bus", "_logger", "_stop_event",
        "_checkpoint_save", "_audit_log",
        "_cb_registry", "_rate_limiters", "_metrics", "_query_cache",
    )

    def __getstate__(self):
        """pickle 时剥离非可序列化属性，其余正常序列化。"""
        state = self.__dict__.copy()
        for attr in self._NON_PICKLABLE_ATTRS:
            state[attr] = None
        return state

    def __setstate__(self, state):
        """从 pickle 恢复：非序列化组件按需重建（子进程独立实例）。"""
        self.__dict__.update(state)
        if self.__dict__.get("_cb_registry") is None:
            from .circuit_breaker import CircuitBreakerRegistry
            self._cb_registry = CircuitBreakerRegistry()
        if self.__dict__.get("_rate_limiters") is None:
            from .rate_limiter import RateLimiterRegistry
            self._rate_limiters = RateLimiterRegistry()
        if self.__dict__.get("_metrics") is None:
            from .observability import get_metrics
            self._metrics = get_metrics()
        if self.__dict__.get("_query_cache") is None:
            self._query_cache = CacheManager(name="dag_queries", max_size=100, ttl=3600)

    @classmethod
    def from_config(cls, config: dict, registry, bus, cb_registry, rate_limiters, metrics,
                    logger=None, stop_event=None, checkpoint_save_fn=None, audit_log_fn=None):
        """手动构建 DAGExecutor（低层入口）。

        常规 process 模式无需调用它：_execute_node_worker 会依据 child_context
        自动在子进程内重建 registry/bus。此方法保留给需要完全手工装配的高级场景。
        """
        return cls(
            registry=registry, bus=bus, cb_registry=cb_registry,
            rate_limiters=rate_limiters, metrics=metrics,
            logger=logger, stop_event=stop_event,
            checkpoint_save_fn=checkpoint_save_fn, audit_log_fn=audit_log_fn,
        )

    def _log(self, level: str, msg: str, **kw):
        """结构化日志记录"""
        if self._logger:
            self._logger.log(level, msg, **kw)

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: dict | None = None,
                             timeout: float = 30.0) -> bool:
        """获取限流令牌。未配置限流时直接放行。"""
        cfg = rate_limit_cfg or {}
        rate = float(cfg.get("rate", 0))
        if rate <= 0:
            return True  # 不限流
        burst = int(cfg.get("burst", rate * 2))
        limiter = self._rate_limiters.get_or_create(agent_name, rate=rate, burst=burst)
        return limiter.acquire(1, block=True, timeout=timeout)  # type: ignore[no-any-return]

    # ─── DAG 构建 ─────────────────────────────

    def build_dag(self, agent_order: list[str], config: dict | None = None) -> tuple[dict, list[list[str]]]:
        """从 agent_order 构建 DAG 节点和拓扑层级"""
        from .pipeline import TaskNode

        nodes = {}
        for name in agent_order:
            meta = self.registry.get_meta(name)
            if not meta:
                continue
            deps = meta.dependencies
            node = TaskNode(
                name=name,
                agent_name=name,
                dependencies=[d for d in deps if d in agent_order],
                timeout=config.get("timeout", 300) if config else 300,
                max_retries=config.get("max_retries", 3) if config else 3,
            )
            nodes[name] = node

        # Kahn 算法拓扑排序，得到层级
        in_degree = {name: len(node.dependencies) for name, node in nodes.items()}
        queue = [name for name, deg in in_degree.items() if deg == 0]
        execution_order = []

        while queue:
            level = queue
            execution_order.append(level)
            queue = []
            for name in level:
                for other_name, other_node in nodes.items():
                    if name in other_node.dependencies:
                        in_degree[other_name] -= 1
                        if in_degree[other_name] == 0:
                            queue.append(other_name)

        # 环检测：检查是否所有节点都被排序
        sorted_count = sum(len(level) for level in execution_order)
        if sorted_count != len(nodes):
            cycle_nodes = [name for name, deg in in_degree.items() if deg > 0]
            raise ValueError(f"DAG 中存在环，无法拓扑排序。环中节点: {cycle_nodes}")

        return nodes, execution_order

    # ─── DAG 执行 ─────────────────────────────

    def execute_node(self, task, node, input_file: str, config: dict) -> dict:
        """执行单个 DAG 节点（统一节点模型，TaskNode 已兼容 ExecutionNode 接口）"""
        from types import SimpleNamespace

        # 确保 node 有 agent_config（统一模型下 TaskNode 自带）
        if not hasattr(node, "agent_config") or node.agent_config is None:
            from .pipeline import NodeConfig
            node.agent_config = NodeConfig(
                config=config,
                pool_size=1,
                rate_limit=getattr(node, "rate_limit", {}),
                circuit_breaker=config.get("circuit_breaker", {}),
            )
        elif hasattr(node.agent_config, "config") and not node.agent_config.config:
            node.agent_config.config = config

        plan = SimpleNamespace(
            pipeline_name=task.pipeline_name,
            checkpoint=config if isinstance(config, dict) else {},
            fail_fast=config.get("fail_fast", True) if isinstance(config, dict) else True,
            raw=config if isinstance(config, dict) else {},
        )
        return self.execute_node_from_scheduler(task, node, input_file, plan)

    @staticmethod
    def _resolve_pool(node) -> tuple[str, int, int]:
        """解析池化节点名 → (base_agent, pool_idx, pool_size)。"""
        raw = node.agent_name
        base = agent_of(raw)
        idx = 0
        if "_pool_" in raw:
            try:
                idx = int(raw.split("_pool_")[1])
            except (ValueError, IndexError):
                idx = 0
        size = max(node.agent_config.pool_size, 1)
        return base, idx, size

    def _build_node_payload(self, task, node, input_file: str, plan,
                            base_agent: str, pool_idx: int, pool_size: int) -> dict:
        """构建节点执行的消息载荷（上游产物 + 配置 + 查询词）。

        上游传什么由各自 Agent 的 PRODUCES 声明决定（见 loom_core.artifacts），
        引擎不再持有 `articles` / `content` / `spec` 这类领域键名，也不再靠
        一份写死的 agent 名单决定"谁的内容更新"——顺序来自 DAG 层级。
        """
        all_queries = self._extract_queries(input_file, node)
        meta = self.registry.get_meta(base_agent)
        if getattr(meta, "extracts_queries", False) and pool_size > 1:
            # 分片而不是复制：`all_queries[pool_idx::pool_size]` 天然处理
            # "查询词比池实例少"——多余的实例拿到 []，如实回报零结果，
            # 而不是把同一条 query 再跑一遍（旧条件 len>=pool_size 会整体退回全量复制）。
            queries = all_queries[pool_idx::pool_size]
        else:
            queries = all_queries

        output_file = (
            node.agent_config.config.get("output")
            or plan.raw.get("pipeline", {}).get("output")
            or f"output/{task.id}_result.md"
        )

        dep_results_raw = {
            dep: task.dag_nodes[dep].result for dep in node.dependencies if dep in task.dag_nodes
        }
        # 再按 Agent 名补一份：Agent 里普遍写着 `dependencies_results.get("ingest")`
        # 这种按名取上游的写法，而内联（子流水线）会把节点名变成 `ingest__tail`。
        # 只在"该 Agent 在本节点上游只出现一次"时补名——出现两次就不猜，
        # 让按名字取的代码拿到空，而不是拿到错的那一份。
        groups: dict[str, list[str]] = {}
        for dep in dep_results_raw:
            groups.setdefault(agent_of(dep), []).append(dep)
        for agent, deps in groups.items():
            if len(deps) == 1 and agent not in dep_results_raw:
                dep_results_raw[agent] = dep_results_raw[deps[0]]
        artifacts = self._collect_upstream_artifacts(task, node, plan)

        ctor_config = getattr(meta, "config", None) or {}
        merged_config = {**ctor_config, **node.agent_config.config}

        payload = {
            "task_id": task.id,
            "input_file": input_file,
            "config": merged_config,
            "pipeline": plan.pipeline_name,
            "node": node.agent_name,
            "dependencies_results": dep_results_raw,
            "upstream": artifacts,
            "queries": queries,
            "target_file": output_file,
            "target": output_file,
        }
        # 声明过的产物同时铺到顶层：既有 Agent 直接读 payload["content"] 的写法
        # 不必改名；引擎自有键（queries/target/...）不会被上游覆盖。
        for name, value in artifacts.items():
            payload.setdefault(name, value)
        return payload

    def _produces_of(self, agent_name: str) -> dict[str, str]:
        """取某节点（含池化实例）声明的产物与合并策略。"""
        base = agent_of(agent_name)
        meta = self.registry.get_meta(base)
        if meta is None:
            return {}
        try:
            return normalize_declaration(getattr(meta, "produces", {}) or {})
        except ValueError as e:
            self._log("error", f"Agent {base} 的 PRODUCES 声明无效: {e}")
            return {}

    def _record_task_output(self, task, meta, payload: dict, result, plan) -> None:
        """声明 `WRITES_OUTPUT` 的节点成功后，把交付物挂到任务上。

        接口层（Admin API / MCP）过去各自扫一遍
        `("safe_writer","safewriter","layout","checker")` 去找产物路径——
        三份重复的猜测，且把领域节点名写进了引擎外壳。
        现在只有落盘节点自己声明终点，引擎按 `pipeline.output_artifact`
        （默认 `content`）取载荷里的交付物。
        """
        if not isinstance(result, dict) or not getattr(meta, "writes_output", False):
            return
        if result.get("status") in ("error", "blocked", "fail"):
            return
        artifact = output_artifact_name(getattr(plan, "raw", None))
        content = payload.get(artifact)
        path = payload.get("target_file") or payload.get("target") or ""
        if isinstance(content, str) and content:
            task.output_content = content
        if path:
            task.output_path = str(path)

    def _artifacts_from(self, agent_name: str, result: dict) -> dict[str, Any]:
        """按 Agent 声明挑出该结果里算作产物的键（引擎不再猜 `content`）。"""
        produces = self._produces_of(agent_name)
        return {name: result[name] for name in produces if name in result}

    def _upstream_closure(self, task, node) -> list[str]:
        """本节点的全部上游节点名，按"离本节点近的层级在前"排序。

        为什么要闭包而不是只看直接依赖：中间节点（如只做结构检查的
        checker）不重新导出正文，只看直接依赖会在它这里断链，
        下游就拿不到内容了——旧实现正是靠一份写死的优先级表跨过这一跳的。
        """
        dag_nodes = getattr(task, "dag_nodes", {}) or {}
        seen: set[str] = set()
        closure: list[str] = []
        frontier = list(node.dependencies or [])
        while frontier:
            nxt: list[str] = []
            for name in frontier:
                # 按"家族"归并，不是按 Agent：内联进来的 writer__review 与父图里的
                # writer 是两个逻辑节点，用 agent_of 归组会让前者从闭包里掉出去。
                base = family_of(name)
                if base in seen:
                    continue
                seen.add(base)
                # 池化兄弟实例一起纳入（同层并行，产物都是有效素材）
                for sibling in [k for k in dag_nodes if family_of(k) == base]:
                    if sibling not in closure:
                        closure.append(sibling)
                dep_node = dag_nodes.get(name) or dag_nodes.get(base)
                if dep_node is not None:
                    nxt.extend(getattr(dep_node, "dependencies", []) or [])
            frontier = nxt
        return closure

    def _collect_upstream_artifacts(self, task, node, plan) -> dict[str, Any]:
        """按声明收集上游产物；"最近的产出者"依据 DAG 层级判定。"""
        level_of: dict[str, int] = {}
        for idx, level in enumerate(getattr(plan, "levels", []) or []):
            for exec_node in level:
                level_of[exec_node.agent_name] = idx

        def depth(name: str) -> int:
            if name in level_of:
                return level_of[name]
            # 同样按家族回退：内联节点的层级不等于同名 Agent 的层级
            return level_of.get(family_of(name), -1)

        # 远 → 近 遍历：`last` 因此在最近产出者处收尾（谁离得近谁说了算），
        # `first` 因此在最远产出者处定格。方向只在这里定义一次。
        ordered = sorted(self._upstream_closure(task, node), key=depth)
        pairs: list[tuple[dict, dict[str, str]]] = []
        for name in ordered:
            result = getattr(task.dag_nodes.get(name), "result", None) if name in task.dag_nodes else None
            if isinstance(result, dict):
                pairs.append((result, self._produces_of(name)))
        return collect_artifacts(pairs)

    def execute_node_from_scheduler(self, task, node, input_file: str, plan) -> dict:
        """由 run_plan 调度：基于 ExecutionNode 执行单个节点。

        成本归属在这一层声明：`llm_router` 记成本时从 ContextVar 取 task_id，
        而 ContextVar **不跨线程继承**——作用域必须设在真正执行节点的 worker
        线程内部（process 模式的 `_execute_node_worker` 也经这条入口）。设在
        提交点与执行点之间的空档上，工作线程里读到的仍是空串，
        `cost_log.task_id` 就会一直为空、按运行汇总的成本恒为 0。
        """
        from .cost_tracker import cost_task_scope

        with cost_task_scope(str(getattr(task, "id", "") or "")):
            return self._execute_node_from_scheduler(task, node, input_file, plan)

    def _execute_node_from_scheduler(self, task, node, input_file: str, plan) -> dict:
        """节点执行本体（`execute_node_from_scheduler` 摘掉成本作用域后的实现）。"""
        from .registry import AgentStatus

        base_agent, pool_idx, pool_size = self._resolve_pool(node)

        instance = self.registry.get_instance(base_agent)
        if not instance:
            return {"error": f"Agent {base_agent} 未找到"}

        cb_cfg = node.agent_config.circuit_breaker if hasattr(node, "agent_config") and node.agent_config else None
        if cb_cfg and cb_cfg.get("enabled", False):
            breaker = self._cb_registry.get_or_create(
                name=base_agent,
                failure_threshold=cb_cfg.get("failure_threshold", 5),
                recovery_timeout=cb_cfg.get("recovery_timeout", 60),
            )
            if not breaker.allow_request():
                self._log("warning", f"{base_agent} 已熔断", reason="circuit_open")
                return {"error": "circuit open", "status": "blocked"}

        self.registry.set_status(base_agent, AgentStatus.RUNNING)

        # 限流：获取令牌（按 agent 级 rate_limit 配置）
        if not self._acquire_rate_limit(base_agent, node.agent_config.rate_limit,
                                        timeout=node.timeout or 30):
            self.registry.set_status(base_agent, AgentStatus.STOPPED)
            return {"error": f"Agent {base_agent} 被限流"}

        try:
            msg_payload = self._build_node_payload(task, node, input_file, plan,
                                                     base_agent, pool_idx, pool_size)
            # meta 上移到投递之前：foreach 分支要在发出任何一项之前就知道这个 Agent
            # 的声明（是否落盘、是否带质量重做），否则"发出去才发现语义冲突"就晚了。
            meta = self.registry.get_meta(base_agent)

            node_state = task.dag_nodes[node.agent_name]
            idempotency_key = f"{task.id}:{node.agent_name}:{node_state.attempts}"
            if getattr(node_state, "_bypass_idempotency", False):
                idempotency_key = f"{idempotency_key}:r{uuid.uuid4().hex[:8]}"

            # ── 投递：普通节点一跳；声明了 foreach 就按项展开成 N 跳再聚合 ──
            # 只认"真映射"：节点在这里是被鸭子类型的（既有测试与 process 模式都喂过
            # MagicMock 节点，其任意属性恒为真），判真值会把每一个 mock 节点都推进
            # 逐项展开，把不相干的用例改造成 foreach 用例。解析期产出的规格只会是
            # dict 或 None（见 scheduler._as_foreach），所以按类型判不损失任何声明。
            foreach_spec = getattr(node, "foreach", None)
            if isinstance(foreach_spec, dict) and foreach_spec:
                result = self._execute_foreach(
                    task, node, plan, msg_payload, base_agent, meta,
                    idempotency_key, foreach_spec)
            else:
                result = self._request_node_once(
                    task, node, base_agent, msg_payload,
                    timeout=node.timeout, idempotency_key=idempotency_key)

            # ── 质量重做循环（外提为独立方法）──
            # 必须按 Agent 名取 meta：内联进来的节点叫 `quality_gate__tail`，
            # 拿它查注册表会得到 None，于是 supports_regeneration / writes_output
            # 一起失效——质量门不再重做、交付契约认为没有产物，且一声不响。
            if getattr(meta, "supports_regeneration", False) and isinstance(result, dict):
                target = getattr(meta, "regeneration_target", "")
                # 复检目标也要还原成 Agent 名：topic 与 RPC 目标都按 base 寻址，
                # 内联节点的 `quality_gate__tail` 没有任何订阅者。
                recheck = agent_of(getattr(meta, "regeneration_recheck", "")
                                    or node.agent_name)
                if not target:
                    raise RuntimeError(
                        f"节点 {node.agent_name} 声明 supports_regeneration 却没给出 "
                        f"REGENERATION_TARGET：重做目标不能由引擎猜（旧实现默认 writer）")
                result = self._handle_regeneration(
                    task, node, result, msg_payload,
                    regenerate_agent=target,
                    recheck_agent=recheck,
                    max_gen=node.agent_config.config.get("max_regenerations", 3),
                )

            # 产出保真底线是"硬"失败：不允许因为 fail_fast=false 而被当成
            # 可容忍的软失败继续下发（历史上占位文档正是这样带着 done + exit 0 出厂的）
            if isinstance(result, dict) and result.get("hard_floor"):
                violations = "；".join(str(v) for v in result.get("violations", []))
                raise RuntimeError(
                    f"{_HARD_FLOOR_PREFIX} 产出未过保真底线: {violations}")

            self.registry.set_status(base_agent, AgentStatus.STOPPED)
            self._record_task_output(task, meta, msg_payload, result, plan)
            return result or {}
        except Exception:
            self.registry.set_status(base_agent, AgentStatus.ERROR)
            raise

    def _request_node_once(self, task, node, base_agent: str, payload: dict,
                           timeout: float, idempotency_key: str):
        """投递一跳，并判定两种"其实根本没执行"：幂等键撞历史、订阅者空响应。

        普通节点与 foreach 的每一项共用这里 —— 分成两处写就会长出
        "普通节点报错、逐项放行"这种分裂判据，而逐项放行正是静默少跑。
        """
        result = self.bus.request(
            topic=f"{base_agent}.input",
            from_a="orchestrator",
            to_a=base_agent,
            payload=payload,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )

        # 幂等键命中历史 → 订阅者根本没被触达，不能算执行成功
        if isinstance(result, dict) and \
                result.get("error") == "duplicate_idempotency_key":
            raise RuntimeError(
                f"节点 {node.agent_name} 未执行：幂等键 "
                f"{result.get('idempotency_key')} 已存在于消息库历史中。"
                f"常见原因是复用了历史 task_id，而 bus_data/ 的幂等记录"
                f"按 checkout 绝对路径共享（message_store.py:28）；"
                f"换新 task_id 或确认这是 --resume 场景")

        if result is None:
            self._log("warning",
                      f"节点 {node.agent_name} 返回空响应（订阅者返回 None 或超时）",
                      task_id=task.id, node=node.agent_name)
        return result

    def _execute_foreach(self, task, node, plan, msg_payload: dict, base_agent: str,
                          meta, idempotency_key: str, spec: dict) -> dict:
        """foreach：把 `over` 解析出的列表逐项交给同一个 Agent，再把结果聚合。

        展开发生在**一次节点执行内部**（同一个 DAG 节点、同一层级、同一 topic），
        所以 DAG 结构、锁文件、上游闭包都不用改。真正变的只有三件事：

        - 载荷：每项一份深拷贝 + 引擎注入的 `item`/`index`/`count`
          （它们是引擎自有键，见 artifacts.ENGINE_OWNED_KEYS）；
        - 幂等键：每项后缀 `#{i}`。不加后缀，第 2 项起全部命中第 1 项的缓存 ——
          表现是"展开了 N 次、只执行了 1 次"，而产物看起来是完整的；
        - 结果：按 Agent 的 PRODUCES 聚合，**任一项失败整个节点失败**。

        `max_items` 与空列表都是显式错误而不是"少跑一点"：作者要的是一整份，
        给一份短的都是静默缺失。
        """
        if getattr(meta, "supports_regeneration", False):
            raise RuntimeError(
                f"节点 {node.agent_name} 同时声明 foreach 与质量重做"
                f"（REGENERATION_TARGET）：重做的对象是“整份产物”，逐项展开里"
                f"没有这个东西；请把 foreach 放在质量门之前的节点上")
        if getattr(meta, "writes_output", False):
            raise RuntimeError(
                f"节点 {node.agent_name} 同时声明 foreach 与 WRITES_OUTPUT："
                f"每一项写的是同一个 target_file，最后只剩第 N 项（前 N-1 项静默蒸发）；"
                f"请把落盘节点放在 foreach 之后，让它拿聚合后的产物")

        over = str(spec.get("over") or "")
        ctx = self._condition_context(task, node, plan)
        items = _resolve_path(over, ctx)
        if items is UNRESOLVED:
            raise RuntimeError(
                f"节点 {node.agent_name} 的 foreach.over={over!r} 在上下文里取不到值"
                f"（路径语法与 when 同源，可用顶层键: {sorted(ctx)}）")
        if not isinstance(items, list):
            raise RuntimeError(
                f"节点 {node.agent_name} 的 foreach.over={over!r} 解析出 "
                f"{type(items).__name__}，不是列表：foreach 只按列表逐项展开")
        if not items:
            raise RuntimeError(
                f"节点 {node.agent_name} 的 foreach.over={over!r} 是空列表："
                f"零项展开只会产出一份“什么都没做”的空聚合，下游却照样报 done")
        max_items = int(spec.get("max_items") or 0)
        if len(items) > max_items:
            raise RuntimeError(
                f"节点 {node.agent_name} 的 foreach 要展开 {len(items)} 项，超过 "
                f"max_items={max_items}：超限报错而不是截断，截断等于静默少跑")

        timeout = node.timeout
        total = len(items)
        self._log("info", f"节点 {node.agent_name} foreach 展开 {total} 项",
                  task_id=task.id, node=node.agent_name, over=over)

        results: list = []
        for idx, one in enumerate(items):
            if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
                raise RuntimeError(
                    f"节点 {node.agent_name} 的 foreach 在第 {idx}/{total} 项处中止（任务已取消）")
            # 限流按项领令牌：一次节点执行现在等于 total 次对外调用，
            # 只领一次等于把作者设的速率整倍放大（真被打的是下游端点，不是引擎）
            if not self._acquire_rate_limit(base_agent, node.agent_config.rate_limit,
                                            timeout=timeout or 30):
                raise RuntimeError(
                    f"节点 {node.agent_name} 的 foreach 第 {idx}/{total} 项取限流令牌超时")

            per_payload = copy.deepcopy(msg_payload)
            per_payload["item"] = one
            per_payload["index"] = idx
            per_payload["count"] = total
            # 每项一个幂等键后缀：见 docstring —— 没有它，展开会退化成"只跑第一项"
            results.append(self._request_node_once(
                task, node, base_agent, per_payload,
                timeout=timeout, idempotency_key=f"{idempotency_key}#{idx}"))

        return self._aggregate_foreach(base_agent, results)

    def _aggregate_foreach(self, base_agent: str, results: list) -> dict:
        """把逐项结果合成一份节点结果。

        产物形状只按类型给确定规则：str 换行拼接、list 首尾相接、其余收成列表。
        不给"取最后一项"这类覆盖语义 —— 覆盖在逐项展开里等于"只留第 N 项"，
        而那正是这里要避免的那类静默缺失。
        `count`/`items` 是引擎聚合键，在产物之后写入：谁把它们声明成 PRODUCES
        也顶不掉（同 ENGINE_OWNED_KEYS 的道理）。
        """
        agg: dict[str, Any] = {}
        ok = [r for r in results if isinstance(r, dict)]

        produced: list[str] = []
        for r in ok:
            for name in self._artifacts_from(base_agent, r):
                if name not in produced:
                    produced.append(name)
        for name in produced:
            vals = [r[name] for r in ok
                    if name in r and r[name] not in (None, "", [], {})]
            if not vals:
                continue
            if all(isinstance(v, str) for v in vals):
                agg[name] = "\n".join(vals)
            elif all(isinstance(v, list) for v in vals):
                flat: list = []
                for v in vals:
                    flat.extend(v)
                agg[name] = flat
            else:
                agg[name] = vals

        # 保真底线按项累积：任何一项被判 hard_floor，整个节点就是硬失败
        floors = [r for r in ok if r.get("hard_floor")]
        if floors:
            agg["hard_floor"] = True
            agg["violations"] = [str(v) for r in floors for v in (r.get("violations") or [])]

        failed: list[str] = []
        for idx, one in enumerate(results):
            if not isinstance(one, dict):
                failed.append(f"#{idx} 空响应（订阅者返回 None 或超时）")
                continue
            is_fail, err = self._business_failure(one)
            if is_fail:
                failed.append(f"#{idx} {err or one.get('status')}")

        agg["count"] = len(results)
        agg["items"] = results
        if failed:
            agg["status"] = "error"
            agg["error"] = (f"foreach {len(failed)}/{len(results)} 项失败："
                            + "；".join(failed[:5]))
        else:
            agg["status"] = "ok"
        return agg

    def _handle_regeneration(self, task, node, result: dict,
                             msg_payload: dict,
                             regenerate_agent: str,
                             recheck_agent: str,
                             max_gen: int = 3) -> dict:
        """质量门控触发的重做循环 —— 只重跑受影响的两跳，不重新调度整个层级。

        领域中立化处理：
        - 反馈载荷不再拆成 `quality_scores`/`style_issues`/`citation_report`
          这些 gate 专属键（实测没有任何 Agent 读它们），改为整体转发
          `gate_feedback`，gate 想给什么由它自己的返回结构决定；
        - 重做结果不再靠字面量 `content` 回灌，而是按重做 Agent 声明的
          PRODUCES 合并进下游载荷。
        """
        try:
            generation = 0
            while (result.get("needs_regenerate") and result.get("can_regenerate")
                   and generation < max_gen and not task.stop_event.is_set()):
                generation += 1
                feedback = {
                    "gate_feedback": dict(result),
                    "generation_count": result.get("generation_count", 0) + 1,
                }
                self._log("info", "质量门控重做",
                          score=result.get('overall_score', 0), generation=generation)

                # deepcopy 防止 feedback 污染原 msg_payload
                regen_payload = copy.deepcopy(msg_payload)
                regen_payload.update(feedback)
                # 幂等 key 必须含节点维度 + 本次重做 attempt：
                # - 缺节点维度：同层并行池节点（writer_pool_0/1）同 agent 名 key 碰撞，
                #   bus.request 幂等缓存命中返回 None，后到节点的重做被静默吞掉
                # - 缺 attempt 维度：整节点重试时 generation 重新计数，跨物理执行
                #   的 key 相同，重试后的重做同样命中缓存不执行
                regen_attempt = uuid.uuid4().hex[:8]
                regen_result = self.bus.request(
                    topic=f"{regenerate_agent}.input", from_a="orchestrator", to_a=regenerate_agent,
                    payload=regen_payload, timeout=node.timeout,
                    idempotency_key=f"regenerate_{task.id}_{node.agent_name}_{regenerate_agent}_g{generation}_{regen_attempt}",
                )

                if isinstance(regen_result, dict) and regen_result:
                    fresh = self._artifacts_from(regenerate_agent, regen_result)
                    if fresh:
                        msg_payload.update(fresh)
                        msg_payload["generation_count"] = feedback["generation_count"]
                    self._set_task_output(task, regenerate_agent, regen_result)

                qg_payload = copy.deepcopy(msg_payload)
                qg_payload.update(feedback)
                result = self.bus.request(
                    topic=f"{recheck_agent}.input", from_a="orchestrator", to_a=recheck_agent,
                    payload=qg_payload, timeout=node.timeout,
                    idempotency_key=f"regenerate_{task.id}_{node.agent_name}_{recheck_agent}_g{generation}_{regen_attempt}",
                ) or {}
                if result:
                    self._set_task_output(task, recheck_agent, result)

            # 状态改写纪律：只有"重做后仍低于阈值"这一种情况可以软化。
            # 原实现在此无条件写回，把 needs_regenerate 为假的分叉全当成通过——
            # 于是 quality_gate 的 {"status":"error","message":"内容为空"}
            # 与保真底线失败都被洗成 pass/accepted_with_warnings（静默绿）。
            incoming = result.get("status")
            if incoming in ("error", "blocked", "fail") and not result.get("needs_regenerate"):
                final_status = "fail" if result.get("hard_floor") else incoming
                self._log("error", "质量门控判定失败，不做软化放行",
                          incoming=incoming, task_id=task.id,
                          violations=result.get("violations", []))
            else:
                final_status = ("accepted_with_warnings"
                                if result.get("needs_regenerate") else "pass")
            result["status"] = final_status
            if result.get("needs_regenerate"):
                self._log("warning", "质量分仍不达标",
                          generation=generation, score=result.get('overall_score', 0))
            else:
                self._log("info", "质量分通过",
                          generation=generation, score=result.get('overall_score', 0))
        except Exception as e:
            self._log("error", "质量重做异常", error=str(e))
            # C3：原 `result = result or {...}` 是空操作（入参 result 恒为 truthy
            # dict，调用方已 isinstance 校验），异常被吞后节点带着旧结果照常下发。
            # 改为显式 error 终态，由 _business_failure 判定节点失败。
            result = {**result, "status": "error", "error": str(e)}

        return result

    # ─── 层级执行：共享辅助方法 ─────────────────────

    @staticmethod
    def _business_failure(result) -> tuple[bool, str]:
        """Agent 返回业务失败（语义失败状态或携带 error 键）时返回 (True, 错误消息)"""
        if isinstance(result, dict):
            sem_status = result.get("status")
            if sem_status in _BUSINESS_FAILURE_STATUSES:
                raw = result.get("message", result.get("error", f"Agent returned {sem_status}"))
                return True, "" if raw is None else str(raw)
            if "error" in result:
                raw = result.get("error")
                return True, "" if raw is None else str(raw)
        return False, ""

    @staticmethod
    def _evaluate_retry_result(retry_result) -> tuple[bool, str]:
        """判定重试结果是否成功，返回 (是否成功, 失败原因)"""
        retry_ok = True
        retry_err = ""
        if not retry_result or "error" in retry_result:
            retry_ok = False
            raw = retry_result.get("error", "retry failed") if retry_result else "retry failed"
            retry_err = "" if raw is None else str(raw)
        elif isinstance(retry_result, dict):
            sem_status = retry_result.get("status")
            if sem_status in _BUSINESS_FAILURE_STATUSES:
                # 原实现只填了 retry_err 却没把 retry_ok 置假：重试拿到业务失败的
                # 结果仍被当成"重试成功"，节点于是带着一份失败产出被判 success
                # （quality_gate 的 fail 就是这样在重试后照样下发的）。
                retry_ok = False
                raw = retry_result.get("message",
                                       retry_result.get("error",
                                                        f"Agent returned {sem_status}"))
                retry_err = "" if raw is None else str(raw)
        return retry_ok, retry_err

    def _apply_node_success(self, task, node, dag_node, step_result, result) -> None:
        """节点成功：写入 dag_node/step_result/任务输出 + 熔断成功计数

        Agent 明说 `status: "skipped"`（无事可做）时，步骤如实记 skipped——
        既不是失败，也不算交付；此前这类返回会被写成 success，报表上就看不出
        这一格其实什么都没做。
        """
        skipped = isinstance(result, dict) and result.get("status") == "skipped"
        dag_node.result = result
        dag_node.status = "success"
        dag_node.finished_at = time.time()
        dag_node.error = ""
        step_result.status = "skipped" if skipped else "success"
        step_result.result = result
        step_result.error = ""
        self._set_task_output(task, node.agent_name, result)
        self._circuit_breaker_success(node)

    def _merge_resumed_nodes(self, task) -> None:
        """断点续传：把 checkpoint 恢复的节点状态合并进重建后的 DAG。

        合并是**按层增量**而不是"每任务一次"：`_execute_plan` 每层现场新建 TaskNode，
        一次性合并只能覆盖第一层建好的那几个节点，后面几层的已完成状态永远合不进来，
        resume 于是退化成"除了第一层，全部重跑"。已合并过的节点名记下来，
        免得节点重跑出新结果后被旧快照倒回去。
        """
        snaps = getattr(task, "_resumed_node_snapshots", None)
        if snaps is None:
            return
        merged = getattr(task, "_resume_merged_nodes", None)
        if merged is None:
            merged = task._resume_merged_nodes = set()
            task._resumed_from_checkpoint = True
        for name, snap in (snaps or {}).items():
            if name in merged:
                continue
            dag_node = task.dag_nodes.get(name)
            if dag_node is None:
                continue        # 这一层还没建出来，等它成为当前层时再合
            merged.add(name)
            dag_node.attempts = int(snap.get("attempts", 0) or 0)
            dag_node.error = str(snap.get("error", "") or "")
            dag_node.result = snap.get("result") or {}
            finished_at = snap.get("finished_at", 0) or 0
            if finished_at:
                dag_node.finished_at = float(finished_at)
            if snap.get("status") == "success" and dag_node.result:
                dag_node.status = "success"
            else:
                dag_node.status = "pending"
        for dag_node in task.dag_nodes.values():
            if dag_node.status != "success" and dag_node.attempts == 0:
                dag_node._bypass_idempotency = True

    # ── 条件节点（when） ────────────────────────────

    def _condition_context(self, task, node, plan) -> dict:
        """`when` 的求值上下文。只放只读数据：不传回调、不 exec、不 eval。"""
        dag_nodes = getattr(task, "dag_nodes", {}) or {}
        upstream = {}
        for dep in (node.dependencies or []):
            if dep in dag_nodes:
                upstream[dep] = getattr(dag_nodes[dep], "result", None)
        # 上游闭包也纳入：作者写 upstream.quality_gate.overall_score 时，
        # quality_gate 常常是祖先而非直接依赖（docgen-lean 就是这样）。只暴露
        # 直接依赖会让这种写法在运行期抛"路径取不到"，等于能力不可用。
        for name in self._upstream_closure(task, node):
            if name in dag_nodes and name not in upstream:
                upstream[name] = getattr(dag_nodes[name], "result", None)
        # 内联进来的节点带着别名（quality_gate__tail），子流水线里写条件的人并不
        # 知道自己被叫什么，只能按 Agent 名引用。于是补一份"去别名"的键：
        # 仅当该 Agent 在上游只出现一次时才补——出现两次还补就是猜，宁可让条件
        # 抛"路径取不到"，也不要读错那份结果（静默串台比失败更难查）。
        by_agent: dict[str, list[str]] = {}
        for name in upstream:
            by_agent.setdefault(agent_of(str(name)), []).append(str(name))
        for agent, names in by_agent.items():
            if len(names) == 1 and agent not in upstream:
                upstream[agent] = upstream[names[0]]
        return {
            # 上游按声明合并后的产物：artifacts.content / artifacts.results ...
            "artifacts": self._collect_upstream_artifacts(task, node, plan),
            # 某个具体上游节点的原始结果：upstream.quality_gate.overall_score
            "upstream": upstream,
            "config": dict(getattr(node.agent_config, "config", None) or {}),
            # call 传进来的参数：子流水线用 inputs.xxx 当阈值/开关，不必抄死数值
            "inputs": dict(getattr(node, "inputs", None) or {}),
            "pipeline": getattr(plan, "pipeline_name", ""),
            "task": {"id": getattr(task, "id", ""),
                     "input_file": getattr(task, "input_file", "")},
        }

    def _should_skip_by_condition(self, task, node, plan) -> str:
        """返回跳过原因；空串表示该节点应当执行。

        求值失败（路径拼错等）**直接抛**：把它当成"条件不成立"就是静默跳过，
        流水线会带着一个从未执行的分支报 done。
        """
        spec = getattr(node, "when", None)
        if not spec:
            return ""
        if _evaluate_condition(spec, self._condition_context(task, node, plan)):
            return ""
        import json as _json

        return f"when 不成立: {_json.dumps(spec, ensure_ascii=False)}"

    def _skip_node_by_condition(self, task, node, dag_node, plan, reason: str) -> None:
        """条件不成立：标记 skipped 并留痕，不投递消息。

        `skip_reason = "condition"` 是刻意的：它表示"作者声明这条分支这轮不需要"，
        因此下游不受影响；而依赖失败的级联跳过不带这个标记，下游照样被跳过。
        """
        from .pipeline import StepResult

        dag_node.status = "skipped"
        dag_node.skip_reason = "condition"
        dag_node.error = reason
        dag_node.finished_at = time.time()
        self._log("info", f"Node {node.agent_name} 按条件跳过：{reason}", task_id=task.id)
        step_result = StepResult(step_name=node.agent_name, agent_name=node.agent_name,
                                 status="skipped", started_at=time.time(), error="")
        step_result.result = {"status": "skipped", "reason": reason}
        self._record_step_result(task, plan, node, step_result)

    def settle_approval_node(self, task, node, dag_node, plan, note: str = "") -> None:
        """审批闸门放行：把闸门节点标记为 success 并留痕（决策进节点结果）。

        闸门不是 Agent——它的"执行结果"就是那次人工决定：`status=approved`
        连同可选的 note 一起写进 dag_node.result，下游能按
        `upstream.<gate>.status == "approved"` 读到这次决策。
        """
        from .pipeline import StepResult

        dag_node.result = {
            "status": "approved",
            "approval": dict(node.approval or {}),
            "note": note,
        }
        dag_node.status = "success"
        dag_node.skip_reason = ""
        dag_node.error = ""
        dag_node.finished_at = time.time()
        self._log("info", f"审批闸门 {node.agent_name} 已放行", task_id=task.id)
        step_result = StepResult(step_name=node.agent_name, agent_name=node.agent_name,
                                 status="success", started_at=time.time())
        step_result.result = dict(dag_node.result)
        self._record_step_result(task, plan, node, step_result)

    def _reuse_completed_node(self, task, node, dag_node, plan) -> bool:
        """断点续传：已完成且结果非空的节点直接复用结果注入下游，不提交 bus.request"""
        if dag_node.status != "success" or not dag_node.result:
            return False
        from .pipeline import StepResult

        started_at = dag_node.started_at or time.time()
        step_result = StepResult(
            step_name=node.agent_name,
            agent_name=node.agent_name,
            status="success",
            started_at=started_at,
            finished_at=time.time(),
            result=dict(dag_node.result),
        )
        self._set_task_output(task, node.agent_name, dag_node.result)
        self._record_step_result(task, plan, node, step_result)
        self._log("info", "断点续传：复用已完成节点", task_id=task.id, node=node.agent_name)
        return True

    def _cancel_unstarted_siblings(self, task, plan, futures: dict, processed: set) -> None:
        """fail_fast/熔断中断后：未启动的兄弟节点置 cancelled 并记录 step_result"""
        for future, (node, dag_node, step_result) in futures.items():
            if future in processed:
                continue
            if not future.cancel():
                continue
            dag_node.status = "cancelled"
            dag_node.error = "cancelled"
            dag_node.finished_at = time.time()
            step_result.status = "cancelled"
            step_result.error = "cancelled"
            self._record_step_result(task, plan, node, step_result)

    def _submit_level_futures(self, task, level: list, input_file: str,
                              plan, executor: ThreadPoolExecutor) -> dict:
        """提交层级内所有节点到执行器，返回 {future: (node, dag_node, step_result)}"""
        from .executor_factory import is_process_executor
        from .pipeline import StepResult

        futures = {}
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
                continue

            if self._reuse_completed_node(task, node, dag_node, plan):
                continue

            cond_reason = self._should_skip_by_condition(task, node, plan)
            if cond_reason:
                self._skip_node_by_condition(task, node, dag_node, plan, cond_reason)
                continue

            # fail_fast=False 时失败依赖的下游不应带缺失依赖继续执行：
            # 提交前检查依赖终态，任一依赖非 success 则跳过（skipped 级联到下游）
            # 例外：被 when 条件主动跳过的依赖不算"未成功"——那是作者声明的
            # 可选分支，整条下游因此停摆就不是"可选"了。
            unmet_deps = [d for d in node.dependencies
                          if d in task.dag_nodes
                          and task.dag_nodes[d].status != "success"
                          and getattr(task.dag_nodes[d], "skip_reason", "") != "condition"]
            if unmet_deps:
                dag_node.status = "skipped"
                dag_node.error = f"依赖未成功，已跳过: {', '.join(unmet_deps)}"
                dag_node.finished_at = time.time()
                self._log("warning", f"Node {node.agent_name} 跳过（依赖未成功: {', '.join(unmet_deps)}）",
                          task_id=task.id)
                continue

            dag_node.status = "running"
            dag_node.started_at = time.time()
            dag_node.attempts += 1

            step_result = StepResult(
                step_name=node.agent_name,
                agent_name=node.agent_name,
                status="running",
                started_at=time.time(),
            )

            if is_process_executor(executor):
                # ProcessPoolExecutor 模式：使用模块级函数（可 pickle）
                future = executor.submit(
                    _execute_node_worker, self, task, node, input_file, plan,
                )
            else:
                # ThreadPoolExecutor 模式：直接提交 bound method（零开销）
                future = executor.submit(
                    self.execute_node_from_scheduler,
                    task=task, node=node, input_file=input_file, plan=plan,
                )
            futures[future] = (node, dag_node, step_result)
        return futures

    def _retry_node_sync(self, task, node, dag_node, step_result,
                         input_file: str, plan, executor=None) -> tuple[object, str]:
        """失败节点重试循环（线程版：stop_event.wait 可中断退避）。
        返回 (重试成功的结果或 None, 最后错误)。

        process 模式下重试必须**留在子进程里**：早先这里无条件调
        `execute_node_from_scheduler`，等于第一次失败就把节点拉回父进程重跑，
        进程隔离在重试这一步悄悄失效——父进程被节点里的长阻塞卡住时，
        层循环的等待上限管不到它（实测：子进程空闲、父线程卡在 Agent.handle，
        整条流水线既不完成也不失败）。重试同样受层上限约束。
        """
        last_error = dag_node.error or ""
        retry_in_process = is_process_executor(executor)
        ceiling = _process_level_ceiling(plan, [node]) if retry_in_process else None
        while dag_node.attempts < node.max_retries:
            delay = backoff_with_jitter(
                base_delay=getattr(node, "initial_delay", 1.0),
                attempt=dag_node.attempts,
                strategy=getattr(node, "backoff", "exponential"),
            )
            self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                      task_id=task.id, error=str(dag_node.error)[:100])
            # 用 stop_event.wait 替代 time.sleep，支持取消中断
            if task.stop_event.wait(delay):
                # 任务被取消，中断重试
                break
            # 全局停止信号（shutdown）补充检查：上面的 wait 只监听 per-task
            # 事件，全局停止的感知最多延迟一个退避周期
            if self._stop_event is not None and self._stop_event.is_set():
                break
            # 重试执行前再查一次取消信号，避免取消后仍发起下一次节点调用
            if task.stop_event.is_set():
                break
            dag_node.attempts += 1
            dag_node.status = "pending"
            try:
                if retry_in_process:
                    # 与首次执行同一条入口（模块级函数，可 pickle），不回落父进程
                    retry_future = executor.submit(
                        _execute_node_worker, self, task, node, input_file, plan,
                    )
                    try:
                        retry_result = retry_future.result(timeout=ceiling)
                    except TimeoutError:
                        retry_future.cancel()
                        err = (
                            f"节点执行超时：process 模式子进程重试 {ceiling or 0:.0f}s 未返回"
                            "（子进程可能已卡死，进程池将被强制回收）"
                        )
                        self.stuck_detected = True
                        dag_node.error = err
                        step_result.error = err
                        task.error = err
                        task.stop_event.set()
                        return None, err
                else:
                    retry_result = self.execute_node_from_scheduler(
                        task, node, input_file, plan
                    )
                retry_ok, retry_err = self._evaluate_retry_result(retry_result)

                if retry_ok:
                    self._apply_node_success(task, node, dag_node, step_result, retry_result)
                    return retry_result, ""
                last_error = retry_err
                dag_node.error = retry_err
                step_result.error = retry_err
            except Exception as retry_e:
                last_error = str(retry_e)
                dag_node.error = str(retry_e)
                step_result.error = str(retry_e)
        return None, last_error

    async def _retry_node_async(self, task, node, dag_node, step_result,
                                input_file: str, plan) -> tuple[object, str]:
        """失败节点重试循环（async 版：asyncio.sleep 避免阻塞事件循环）。
        返回 (重试成功的结果或 None, 最后错误)。"""
        last_error = dag_node.error or ""
        while dag_node.attempts < node.max_retries:
            delay = backoff_with_jitter(
                base_delay=getattr(node, "initial_delay", 1.0),
                attempt=dag_node.attempts,
                strategy=getattr(node, "backoff", "exponential"),
            )
            self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                      task_id=task.id, error=str(dag_node.error)[:100])
            # 原实现用 task.stop_event.wait(delay) 会阻塞 asyncio 事件循环；
            # 改为 await asyncio.sleep 避免阻塞，随后再检查 stop 信号。
            await asyncio.sleep(delay)
            if task.stop_event.is_set():
                break
            # 全局停止信号补充检查（与线程版对齐）
            if self._stop_event is not None and self._stop_event.is_set():
                break
            dag_node.attempts += 1
            dag_node.status = "pending"
            try:
                retry_result = await asyncio.to_thread(
                    self.execute_node_from_scheduler, task, node, input_file, plan
                )
                retry_ok, retry_err = self._evaluate_retry_result(retry_result)

                if retry_ok:
                    self._apply_node_success(task, node, dag_node, step_result, retry_result)
                    return retry_result, ""
                last_error = retry_err
                dag_node.error = retry_err
                step_result.error = retry_err
            except Exception as retry_e:
                last_error = str(retry_e)
                dag_node.error = str(retry_e)
                step_result.error = str(retry_e)
        return None, last_error

    def _record_step_result(self, task, plan, node, step_result) -> None:
        """记录单节点步骤：checkpoint、审计日志、指标（as_completed 每节点 finally）"""
        step_result.finished_at = time.time()
        duration_ms = (step_result.finished_at - step_result.started_at) * 1000
        task.steps.append(step_result)
        if self._checkpoint_save:
            self._checkpoint_save(task)
        if self._audit_log:
            self._audit_log(
                task_id=task.id,
                agent_name=node.agent_name,
                input_summary=getattr(getattr(node, "agent_config", None), "config", {}),
                output_summary=step_result.result or {},
                duration_ms=duration_ms,
                status=step_result.status,
                error=step_result.error,
            )
        self._metrics.observe(
            "step_duration_ms", duration_ms,
            labels={"agent": node.agent_name, "status": step_result.status},
        )
        self._metrics.counter(
            "step_total", labels={"agent": node.agent_name},
        )
        if step_result.status != "success":
            self._metrics.counter(
                "step_failures", labels={"agent": node.agent_name, "status": step_result.status},
            )
        self._metrics.gauge(
            "pipeline_progress", task.progress,
            labels={"pipeline": getattr(plan, "pipeline_name", "")},
        )

    def execute_level(self, task, level: list, input_file: str,
                      plan, executor: ThreadPoolExecutor) -> bool:
        """执行一个 DAG 层级的所有节点，处理成功/失败/重试/熔断。
        返回 True 表示层级成功完成，False 表示需要中断（fail_fast）。"""
        from .pipeline import TaskStatus

        self._merge_resumed_nodes(task)

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        futures = self._submit_level_futures(task, level, input_file, plan, executor)
        processed: set = set()

        # process 模式给整层等待设上限（成因见 _process_level_ceiling）：
        # 子进程卡死时 as_completed 没有信号可等，只能由父进程判超时收尾。
        # 用显式迭代而不是 for，超时既能在原地判掉，又不必给整个循环体加深缩进。
        ceiling = _process_level_ceiling(plan, level) if is_process_executor(executor) else None
        completion = as_completed(futures, timeout=ceiling)
        while True:
            try:
                future = next(completion)
            except StopIteration:
                break
            except TimeoutError:
                self._fail_stuck_nodes(task, plan, futures, ceiling or 0.0)
                return False

            node, dag_node, step_result = futures[future]
            processed.add(future)
            try:
                result = future.result()

                is_business_fail, biz_err = self._business_failure(result)
                if is_business_fail:
                    raise Exception(f"节点 {node.agent_name} 业务失败: "
                                    f"{biz_err or 'Agent returned failure status'}")

                # 成功路径
                self._apply_node_success(task, node, dag_node, step_result, result)

            except Exception as e:
                dag_node.error = str(e)

                # while 循环重试直到达到 max_retries（取消/全局停止时提前中断）
                _, last_error = self._retry_node_sync(task, node, dag_node, step_result,
                                                      input_file, plan, executor)

                # 重试循环结束，检查是否最终失败
                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        # 修复：cancel 已置 CANCELLED 时不得覆写为 FAILED——
                        # 熔断/fail_fast 分支无条件赋值会把用户取消的任务
                        # 错报为失败，污染终态、exit code 与恢复逻辑
                        if task.status is not TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        # 修复 P0：f.cancel() 只能取消尚未启动的 future，
                        # 已在运行的节点不会被中断。设置 task.stop_event 软中断，
                        # 让运行中的节点在下次检查 stop_event 时主动退出。
                        task.stop_event.set()
                        self._cancel_unstarted_siblings(task, plan, futures, processed)
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    # 子进程被判定卡死并强制回收时，无论 fail_fast 是什么都必须
                    # 让整条流水线如实失败：worker 是被我们杀掉的，这一轮的结果
                    # 不可信，留下 done 就是把"没跑完"报成"跑完了"。
                    if fail_fast or self.stuck_detected \
                            or (last_error or "").startswith(_HARD_FLOOR_PREFIX):
                        if task.status is not TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        # 修复 P0：同上，设置 stop_event 软中断已运行节点。
                        task.stop_event.set()
                        self._cancel_unstarted_siblings(task, plan, futures, processed)
                        break

            finally:
                self._record_step_result(task, plan, node, step_result)

        return task.status != TaskStatus.FAILED  # type: ignore[no-any-return]

    def _fail_stuck_nodes(self, task, plan, futures: dict, ceiling: float) -> None:
        """超过等待上限仍未返回的节点判为失败（process 模式卡死兜底）。

        "没返回"必须当失败报出来，不能留一个 running 的节点让整条流水线
        永远停在中间：调用方（run_plan 的收尾、RunStore 的节点投影、交付账本）
        都靠终态判断该不该继续，一个永不终结的节点会让它们全部挂住。
        """
        from .pipeline import TaskStatus

        stuck = [f for f in futures if not f.done()]
        if not stuck:
            return
        self.stuck_detected = True
        err = (
            f"节点执行超时：process 模式子进程 {ceiling:.0f}s 未返回"
            "（子进程可能已卡死，进程池将被强制回收）"
        )
        for future in stuck:
            node, dag_node, step_result = futures[future]
            dag_node.status = "failed"
            dag_node.error = err
            dag_node.finished_at = time.time()
            step_result.status = "failed"
            step_result.error = err
            task.error = err
            future.cancel()
            self._record_step_result(task, plan, node, step_result)
        self._log("error", err, task_id=task.id, nodes=[futures[f][0].agent_name for f in stuck])
        task.stop_event.set()
        if task.status is not TaskStatus.CANCELLED:
            task.status = TaskStatus.FAILED

    async def execute_level_async(self, task, level: list, input_file: str,
                                  plan) -> bool:
        """async 版 execute_level：用 asyncio.gather + asyncio.to_thread 并发执行节点。

        与 execute_level 的区别：
        - 不需要 ThreadPoolExecutor，直接用 asyncio 事件循环调度
        - 每个节点的同步 execute_node_from_scheduler 通过 asyncio.to_thread 桥接
        - 消除 asyncio.run 嵌套开销，适合在已有事件循环中调用
        返回 True 表示层级成功完成，False 表示需要中断（fail_fast）。
        """
        from .pipeline import StepResult, TaskStatus

        self._merge_resumed_nodes(task)

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        # 准备节点元数据
        node_meta = []
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
                continue
            if self._reuse_completed_node(task, node, dag_node, plan):
                continue
            cond_reason = self._should_skip_by_condition(task, node, plan)
            if cond_reason:
                self._skip_node_by_condition(task, node, dag_node, plan, cond_reason)
                continue
            # 对齐线程版：fail_fast=False 时失败依赖的下游跳过执行
            # （被 when 条件主动跳过的依赖除外，见线程版注释）
            unmet_deps = [d for d in node.dependencies
                          if d in task.dag_nodes
                          and task.dag_nodes[d].status != "success"
                          and getattr(task.dag_nodes[d], "skip_reason", "") != "condition"]
            if unmet_deps:
                dag_node.status = "skipped"
                dag_node.error = f"依赖未成功，已跳过: {', '.join(unmet_deps)}"
                dag_node.finished_at = time.time()
                self._log("warning", f"Node {node.agent_name} 跳过（依赖未成功: {', '.join(unmet_deps)}）",
                          task_id=task.id)
                continue
            dag_node.status = "running"
            dag_node.started_at = time.time()
            dag_node.attempts += 1
            step_result = StepResult(
                step_name=node.agent_name,
                agent_name=node.agent_name,
                status="running",
                started_at=time.time(),
            )
            node_meta.append((node, dag_node, step_result))

        # 并发执行所有节点（asyncio.to_thread 桥接同步调用）
        coros = [
            asyncio.to_thread(self.execute_node_from_scheduler,
                              task, node, input_file, plan)
            for node, _, _ in node_meta
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # 处理结果（与 execute_level 相同的逻辑）
        for (node, dag_node, step_result), result in zip(node_meta, results, strict=False):
            try:
                if isinstance(result, Exception):
                    raise result

                is_business_fail, biz_err = self._business_failure(result)
                if is_business_fail:
                    raise Exception(f"节点 {node.agent_name} 业务失败: "
                                    f"{biz_err or 'Agent returned failure status'}")

                self._apply_node_success(task, node, dag_node, step_result, result)

            except Exception as e:
                dag_node.error = str(e)

                _, last_error = await self._retry_node_async(task, node, dag_node, step_result,
                                                              input_file, plan)

                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        # 对齐线程版：CANCELLED 不得被覆写为 FAILED（终态守卫）
                        if task.status is not TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        # 对齐线程版（execute_level 同分支）的软中断语义：设置
                        # stop_event 让后续重试/层级入口立即退出。async 下同层兄弟
                        # 已由 gather 启动，无法像线程版那样 cancel 未启动的 future。
                        task.stop_event.set()
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    if fail_fast or (last_error or "").startswith(_HARD_FLOOR_PREFIX):
                        # 对齐线程版：CANCELLED 不得被覆写为 FAILED（终态守卫）
                        if task.status is not TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        task.stop_event.set()
                        break

            finally:
                self._record_step_result(task, plan, node, step_result)

        return task.status != TaskStatus.FAILED  # type: ignore[no-any-return]

    # ─── 查询词提取 ─────────────────────────────

    def _extract_queries(self, input_file: str, node) -> list[str]:
        """从输入文件提取查询词（按行，过滤注释/空行/噪音行）。

        噪音行（如"这是一个测试，用于验证流水线是否正常工作"）只在存在其他有效行时
        才被过滤——若输入仅有这些行，它们本身就是用户给出的主题，必须原样作为查询词，
        不得替换成无关的默认主题（修复：含"请生成/帮我写"的真实主题被静默丢弃，
        导致 API/MCP 提交的任务跑偏到硬编码默认查询）。
        使用 per-file 缓存避免同一 level 内多个节点重复读取文件。"""
        cache_key = input_file
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        with open(input_file, encoding="utf-8") as f:
            content = f.read()
        noise = [r"^这是一个测试", r"^用于验证", r"验证流水线", r"是否正常工作",
                 r"^test\b", r"^测试\b", r"请生成", r"帮我写"]
        candidates = []
        for line in content.split("\n"):
            q = line.strip()
            if not q or q.startswith("#"):
                continue
            if len(q) < 4:
                continue
            candidates.append(q)
        queries = [q for q in candidates
                   if not any(re.search(p, q, re.I) for p in noise)]
        if not queries and candidates:
            # 所有行都命中噪音模式：它们是仅有的输入，直接作为查询词
            queries = candidates
        if not queries:
            default_query = node.agent_config.config.get("default_query")
            if default_query:
                queries = [str(default_query)]
            else:
                raise ValueError(
                    f"输入文件 {input_file!r} 未提取到任何有效检索词"
                    "（空文件/全部为注释或过短行）。请在输入文件首行写明文档主题，"
                    "或为节点配置 default_query。"
                )
        self._query_cache.set(cache_key, queries)
        return queries

    # ─── 任务输出读写 ─────────────────────────────

    def _get_task_output(self, task, key: str, default=None):
        """统一读取 task 输出（同时查 dag_nodes 和 result，加锁）。
        支持池化节点名（如 researcher_pool_0）自动解析为其家族基名。

        回退用 `family_of` 而不是 `agent_of`：`researcher_pool_0__review` 回退到
        "researcher" 会读到父图那份合并结果，两段内联子流程的产物就此互相串台。
        """
        base = family_of(key)
        lock = getattr(task, "result_lock", None)
        def _read():
            for lookup in (key, base):
                if hasattr(task, "dag_nodes") and lookup in task.dag_nodes:
                    val = task.dag_nodes[lookup].result
                    if val:
                        return val
            for lookup in (key, base):
                if hasattr(task, "result") and lookup in task.result:
                    val = task.result[lookup]
                    if val:
                        return val
            return default
        if lock:
            with lock:
                return _read()
        return _read()

    def _set_task_output(self, task, key: str, value, dag_node: bool = True):
        """统一写入 task 输出（同时写 dag_nodes 和 result，加锁）。"""
        lock = getattr(task, "result_lock", None)
        # 回退按家族：把内联池的合并结果写回 "researcher" 会覆写父图 researcher 节点
        # 的结果与状态（agent_of 剥别名，这里正是要保留别名）。
        base = family_of(key)
        def _write():
            if hasattr(task, "result"):
                task.result[key] = value
            if dag_node and hasattr(task, "dag_nodes"):
                if key in task.dag_nodes:
                    task.dag_nodes[key].result = value
                    task.dag_nodes[key].status = "success"
                elif base in task.dag_nodes:
                    task.dag_nodes[base].result = value
                    task.dag_nodes[base].status = "success"
        if lock:
            with lock:
                _write()
        else:
            _write()
        if self._logger:
            self._logger.log("debug", "set_task_output", key=key)

    def _circuit_breaker(self, node, task) -> bool:
        """Per-agent 熔断器：失败达到阈值后返回 True（表示应熔断/跳过）。
        池化节点（如 researcher_pool_0）共享 base agent 的熔断器。"""
        cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg or not cb_cfg.get("enabled", False):
            return False

        agent_name = agent_of(node.agent_name)
        breaker = self._cb_registry.get_or_create(
            name=agent_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            recovery_timeout=cb_cfg.get("recovery_timeout", 60),
        )

        breaker.record_failure()
        if not breaker.allow_request():
            self._log("warning", f"{agent_name} 已熔断")
            return True
        return False

    def _circuit_breaker_success(self, node):
        """node 成功时重置该 agent 的熔断计数（支持 ExecutionNode 和 TaskNode）"""
        agent_name = getattr(node, "agent_name", "")
        base_name = agent_of(agent_name)
        cb_cfg = None
        if hasattr(node, "agent_config") and node.agent_config:
            cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg:
            cb_cfg = getattr(node, "circuit_breaker", None)
        enabled = bool(cb_cfg and cb_cfg.get("enabled", False)) if cb_cfg else False
        if not enabled:
            return
        breaker = self._cb_registry.get_or_create(
            name=base_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5) if cb_cfg else 5,
            recovery_timeout=cb_cfg.get("recovery_timeout", 60) if cb_cfg else 60,
        )
        breaker.record_success()

    # ─── 退避 ─────────────────────────────

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        """计算重试退避时间（带 jitter）"""
        return backoff_with_jitter(
            base_delay=initial_delay,
            attempt=attempt,
            strategy=backoff,
        )
