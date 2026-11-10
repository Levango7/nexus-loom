"""运行维护：保留期与 GC（设计复核 2026-10-20 §3.1 A6）。

治的是什么：队列 / cost / quality feedback / checkpoint / RunStore 此前全部
无界——`task_queue.cleanup` 与 `cost_tracker.cleanup` 是**死代码**（实现过、
有判据、从未被调度），quality_feedback 与 RunStore 连清理面都没有。一个长跑
的 worker 会无限膨胀。

本模块把"删除过期运行"做成一条统一口径：

- **双判据**：`retention_days`（年龄）与 `max_runs`（数量），任一 > 0 即执行
  （0 = 保守默认，不启用时行为与今天完全一致）；
- **接线**：worker 循环按配置周期调用——它不依赖任何内存态，进程重启后
  下一轮循环照常跑（"重启后仍执行"这条判据就落在这条性质上）；
- **失败面**：单个 store 清理失败只告警不拖垮维护——但报错必须吵，
  静默一半等于把这个功能也变成死代码；
- **假时钟**：`now` 参数可注入，判据测试不依赖真实时间。

配置层（config.json / 覆写文件，点号键）：`retention.runs_days`、
`retention.max_runs`、`maintenance.gc_ticks`（worker 每 N 个轮询跑一次
GC，默认 60）。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: retention 配置的键名（点号，供 ConfigCenter.get 直接读）
RETENTION_DAYS_KEY = "retention.runs_days"
RETENTION_MAX_RUNS_KEY = "retention.max_runs"
GC_TICKS_KEY = "maintenance.gc_ticks"
#: 常驻服务（`--daemon`/`--admin`）没有"轮询 tick"可数，只能按秒计；
#: 默认 600s（10 分钟），0 = 服务模式不跑 GC（显式关掉，而不是"忘了接"）。
GC_SECONDS_KEY = "maintenance.gc_seconds"

#: "年龄不启用"的传参值：age=0 在既有语义里是"删光"（历史判据钉着），
#: 所以"不启用"要显式传一个不会删到任何行的天数——10^9 天。
AGE_DISABLED = 10 ** 9


@dataclass
class Retention:
    """保留期判据。两个字段都是 0 = 不启用（保守默认）。"""

    days: int = 0
    max_runs: int = 0

    @property
    def enabled(self) -> bool:
        return self.days > 0 or self.max_runs > 0

    def age_days(self) -> int:
        """给各 store cleanup 的年龄参数：0 = 不启用 → 10^9 天（不删任何行）。"""
        return self.days if self.days > 0 else AGE_DISABLED


def retention_from_config(config: Any) -> Retention:
    """从 ConfigCenter（或 dict）读 retention 段。非正数一律视为 0——
    配置写错不该悄悄变成"删光一切"。"""
    days = 0
    cap = 0
    if config is not None:
        try:
            days = int(config.get(RETENTION_DAYS_KEY, 0) or 0)
            cap = int(config.get(RETENTION_MAX_RUNS_KEY, 0) or 0)
        except (TypeError, ValueError):
            pass
    return Retention(days=max(days, 0), max_runs=max(cap, 0))


def gc_runs(*, queue: Any = None, cost: Any = None, feedback: Any = None,
            run_store: Any = None, checkpoint_mgr: Any = None,
            deliveries: Any = None, dlq: Any = None,
            retention: Retention | None = None,
            now: float | None = None,
            log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """按保留期判据清理所有 store。返回 {store_name: 删除行数 / 失败说明}。

    `now` 只传给 RunStore.cleanup 与 DeliveryLedger.cleanup（它们有假时钟参数）
    ——其余 store 的清理语义是"以现在为界"，注入时钟没有意义。

    任一 store 的清理失败只记 `failed` 计数与告警，不影响其余 store——
    删掉一半永远比不删更糟吗？不，但**删失败报出来**必须成立：静默的
    清理功能就是下一个死代码。

    RunStore 先清：它的 cleanup 回被删 run_id 集合，cost/quality 两库按它
    联动删（跨库无 FK，不联动就留指向已删运行的悬空行——实证 C1）。
    """
    if retention is None:
        retention = Retention()
    if not retention.enabled:
        return {"enabled": False}

    age = retention.age_days()
    rs_args: tuple = (age, retention.max_runs)
    if now is not None:
        rs_args = (*rs_args, now)
    summary: dict[str, Any] = {"enabled": True}
    rs_result = _run_cleanup(run_store, "cleanup", rs_args, "run_store")
    run_ids: set[str] = set()
    if isinstance(rs_result, dict):
        run_ids = {str(x) for x in (rs_result.get("run_ids") or [])}
    summary["run_store"] = rs_result
    summary["queue"] = _run_cleanup(queue, "cleanup", (age, retention.max_runs),
                                    "task_queue")
    # 双判据对全部 store 成立：max_runs 过去只接到 queue/run_store/deliveries/
    # dlq 四面，cost/quality 两库漏接 ⇒ max_runs-only 配置下两库无界（实证 B1）。
    summary["cost"] = _run_cleanup(cost, "cleanup", (age, retention.max_runs),
                                   "cost_tracker", {"run_ids": run_ids})
    summary["feedback"] = _run_cleanup(feedback, "cleanup",
                                       (age, retention.max_runs),
                                       "quality_feedback",
                                       {"run_ids": run_ids})
    summary["deliveries"] = _run_cleanup(deliveries, "cleanup", rs_args,
                                         "delivery_ledger")
    # 死信表此前只进不出（A6 漏掉的那一张）：判据形状与其他 store 相同。
    summary["dlq"] = _run_cleanup(dlq, "prune_dlq", (age, retention.max_runs),
                                  "dlq")
    if retention.days > 0:
        summary["checkpoints"] = _run_cleanup(
            checkpoint_mgr, "cleanup_old", (age,), "checkpoint_manager")
    if log:
        stamp = now if now is not None else time.time()
        log(f"[maintenance] GC 执行（{stamp}，保留 {retention.days} 天 / 最近 "
            f"{retention.max_runs} 条）：删除 {count_dropped(summary)} 行")
    return summary


def count_dropped(summary: dict[str, Any]) -> int:
    """把汇总里的"删除行数"加总——只数真行数。

    这里原先写的是 `sum(int(v) for v in summary.values() if isinstance(v, int))`，
    两头都错：`enabled: True` 是 int 的子类，被算成"删了 1 行"；而 `run_store`
    返回的是逐表 dict（真删了几十行），因为不是 int 被整个跳过。本机实测复现过
    "删 18 行、日志写删除 1 行"——一条会让人以为核对过的假账，比没有日志更糟。
    """
    total = 0
    for name, value in summary.items():
        if name == "enabled":
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            total += value
        elif isinstance(value, dict):
            total += sum(v for v in value.values()
                         if isinstance(v, int) and not isinstance(v, bool))
    return total


def format_summary(summary: dict[str, Any], retention: Retention | None = None
                   ) -> str:
    """把 GC 汇总打成人能核对的几行（`loom --gc` 与 daemon 日志用）。

    打原始 dict 的话，读的人得把 `{'run_store': {'runs': 3, ...}}` 心算成
    "到底删了多少、哪张表删的"——一张要人背锅的维护动作，账要一眼能对上。
    """
    if not summary.get("enabled"):
        return ("[gc] retention 未启用（retention.runs_days / retention.max_runs "
                "全 0）——未删任何行；worker 循环同样如此")
    lines: list[str] = []
    if retention is not None:
        lines.append(f"[gc] 保留 {retention.days} 天 / 最近 {retention.max_runs} 条")
    for name, value in summary.items():
        if name == "enabled" or value is None:
            continue
        if isinstance(value, dict) and value.get("failed"):
            lines.append(f"  {name}: 失败（{value.get('message', '')}）")
        elif isinstance(value, dict):
            # run_ids 是 list（被删 run 集合），进日志只会刷屏；计数在
            # count_dropped 里已按表入账，这里只打 int 面。
            parts = " ".join(f"{k}={v}" for k, v in sorted(value.items())
                             if isinstance(v, int) and not isinstance(v, bool))
            lines.append(f"  {name}: {parts}")
        else:
            lines.append(f"  {name}: {value} 行")
    lines.append(f"  合计删除 {count_dropped(summary)} 行")
    return "\n".join(lines)


def _dlq_store(orch: Any) -> Any:
    """取持久化总线里的死信表（内存总线没有 store，返回 None = 这一面跳过）。

    走 `bus._store` 与 `admin_api._node_idempotency` 同一条访问路径：另开一个
    `PersistentStore` 会连到另一个连接池，读到的不是同一个库。
    """
    store = getattr(getattr(orch, "bus", None), "_store", None)
    return store if hasattr(store, "prune_dlq") else None


def gc_interval_seconds(config: Any) -> int:
    """常驻服务模式两次 GC 之间的秒数（`maintenance.gc_seconds`，默认 600）。

    0（或读成非正数）= 服务模式不跑 GC——那是一个**显式选择**，不是忘了接。
    写成空值（YAML 里 `gc_seconds:` 不给值，读回来是 None）**不算显式选择**：
    它落进默认 600，否则"填了键却没填值"会把回收静默关掉，正是 #79 修过的那类坑。
    """
    if config is None:
        return 600
    raw = config.get(GC_SECONDS_KEY, 600)
    if raw is None:
        return 600
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 600
    return max(value, 0)


def _run_cleanup(store: Any, method: str, args: tuple, label: str,
                 kwargs: dict[str, Any] | None = None) -> Any:
    """调用一个 store 的清理方法；失败返回 {"failed": True, ...}（不抛）。"""
    if store is None:
        return None
    fn = getattr(store, method, None)
    if fn is None:
        return {"failed": True, "store": label, "message": f"{label} 无 {method}"}
    try:
        result = fn(*args, **(kwargs or {}))
    except Exception as e:                   # noqa: BLE001 — 清理失败不该拖垮维护
        return {"failed": True, "store": label, "message": str(e)}
    # run_store.cleanup 返回 dict（按表计数）；其余返回行数 int。
    if isinstance(result, dict):
        return result
    return int(result or 0)


def gc_from_orchestrator(orch: Any, now: float | None = None,
                         log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """从 orchestrator 组装各 store 并执行一次 GC（worker 接线用）。

    orchestrator 只暴露 `task_queue` / `_checkpoint`（ApprovalStore 只包
    RunStore 的读面）；cost / feedback 走模块级 getter；RunStore 经
    `_runstore()` 惰性拿——拿不到就跳过并告警（不因为一个观测面缺了就
    整个 GC 失败）。DeliveryLedger 是独立 SQLite 账本，直接构造即可。
    """
    from .cost_tracker import get_cost_tracker
    from .deliveries import DeliveryLedger
    from .quality_feedback import get_quality_feedback

    retention = retention_from_config(getattr(orch, "config", None))
    run_store = None
    getter = getattr(orch, "_runstore", None)
    if callable(getter):
        try:
            run_store = getter()
        except Exception as e:               # noqa: BLE001
            if log:
                log(f"[maintenance] RunStore 取用失败，RunStore 面跳过: {e}")
    return gc_runs(
        queue=getattr(orch, "task_queue", None),
        cost=get_cost_tracker(),
        feedback=get_quality_feedback(),
        run_store=run_store,
        checkpoint_mgr=getattr(orch, "_checkpoint", None),
        deliveries=DeliveryLedger(),
        dlq=_dlq_store(orch),
        retention=retention,
        now=now,
        log=log,
    )
