"""A7 只读对账报告：任务队列 × 运行投影双源的 stale-running 漂移列示。

双源模型（板面 §三十四 A7 规格）：
- tasks.db 的 task_queue 行是**执行侧真源**（status/started_at/finished_at/owner_pid）；
- runs.db 的 runs 行 + events 表是**交付投影**（open_run 盖 running、close_run 盖终态章）。
正常收尾链是 队列终态 → 终态事件 → 投影盖章 → 交付账本；任何一环中断都会留下
双源不一致。本模块把不一致**列出来**——只报告、不修复：不自动杀进程、不写任何库，
与 task_queue.recover() 的「回收写库」语义刻意分开（人在圈内看了报告再决定动什么）。

三类漂移（互斥划分，每行恰好归一类）：
1. stale-owner-dead：队列仍 running，但 started_at 缺失/过期（超过 stale_seconds）
   且 owner_pid 缺失/已死（新鲜 running 或存活 pid 视为仍在执行，不报）；
2. projection-running：队列已终态（done/failed/cancelled）而投影仍 running；
3. projection-unstamped：队列已终态而投影行缺失，或投影状态非 running 也非终态
   （盖过头寸/异常态，即「未盖章」）。
一致行跳过；投影有行而队列无对应行（孤儿投影）不在本报告三类内，不报。
"""
from __future__ import annotations

import time
from typing import Any

from .run_store import TERMINAL_STATUSES, RunStore
from .task_queue import TaskQueue, _pid_alive

#: 三类漂移的 category 取值（判据钉死：CLI 人类输出 / JSON / 测试同源）。
CAT_STALE_OWNER_DEAD = "stale-owner-dead"
CAT_PROJECTION_RUNNING = "projection-running"
CAT_PROJECTION_UNSTAMPED = "projection-unstamped"

#: 每行输出的键集合（JSON 键序即插入序，不作契约）。
ROW_KEYS = (
    "task_id", "category", "reason", "queue_status", "queue_started_at",
    "queue_finished_at", "owner_pid", "run_status", "last_event_ts",
)


def _last_event_ts(run_store: RunStore, run_id: str) -> float | None:
    """该 run 最后一条事件的 ts；无事件 / 无该 run → None。

    events() 按 seq ASC 返回，取 max 即最后事件时间。limit 给足（事件量由保留期
    GC 有界），避免截断把「最后事件」读成「第 1000 条」。
    """
    events = run_store.events(run_id, after_seq=0, limit=1_000_000)
    if not events:
        return None
    return float(max(e["ts"] for e in events))


def _row(task_id: str, category: str, reason: str, q: dict, owner_pid: int,
         run_status: str | None, last_event_ts: float | None) -> dict[str, Any]:
    """按 ROW_KEYS 顺序组装一行（键序稳定，人类输出与 JSON 同源）。"""
    return {
        "task_id": task_id,
        "category": category,
        "reason": reason,
        "queue_status": q["status"],
        "queue_started_at": q["started_at"],
        "queue_finished_at": q["finished_at"],
        "owner_pid": owner_pid,
        "run_status": run_status,
        "last_event_ts": last_event_ts,
    }


def stale_running_report(task_queue: TaskQueue, run_store: RunStore, *,
                         now: float | None = None,
                         stale_seconds: float = 900.0) -> list[dict[str, Any]]:
    """对账队列与投影，返回 stale-running 漂移行（只读，零写库）。

    Args:
        task_queue: 任务队列（tasks.db）读取侧。
        run_store: 运行投影（runs.db）读取侧。
        now: 当前时间（秒）；None = time.time()。测试注入以钉死判据。
        stale_seconds: running 行超过该阈值算过期（与 --lease-stale 同口径默认值）。

    Returns:
        漂移行列表，按 task_id 排序（稳定输出）；每行键见 ROW_KEYS。
    """
    if now is None:
        now = time.time()
    cutoff = now - stale_seconds

    # owner_pid 全量映射：list_all 的行形状（_ROW_COLS）不含 owner_pid，
    # 由 TaskQueue.owner_pids() 单独读（不改全量行形状，免牵连所有消费者）。
    owner_pids = task_queue.owner_pids()

    rows: list[dict[str, Any]] = []
    for q in sorted(task_queue.list_all(limit=100_000),
                    key=lambda r: r["task_id"]):
        task_id = q["task_id"]
        q_status = q["status"]
        owner_pid = owner_pids.get(task_id, 0)
        run = run_store.get_run(task_id)
        run_status = run["status"] if run else None

        if q_status == "running":
            # ① 陈旧且无人认领：started_at 缺失/过期 + owner_pid 缺失/已死。
            #    owner_pid 先判 falsy 再调 _pid_alive：posix 上 os.kill(0, 0)
            #    命中进程组，会把「无主」误判成「存活」。
            stale_started = not q["started_at"] or q["started_at"] < cutoff
            pid_dead = not owner_pid or not _pid_alive(owner_pid)
            if stale_started and pid_dead:
                rows.append(_row(
                    task_id, CAT_STALE_OWNER_DEAD,
                    "queue running past stale_seconds with dead/absent owner_pid",
                    q, owner_pid, run_status, _last_event_ts(run_store, task_id)))
            continue
        if q_status not in TERMINAL_STATUSES:
            # pending / claimed 等中间态：不在本报告口径内，跳过。
            continue
        # 队列已终态：投影必须跟着盖章，否则就是漂移。
        last_ts = _last_event_ts(run_store, task_id)
        if run is None:
            rows.append(_row(task_id, CAT_PROJECTION_UNSTAMPED,
                             "projection row missing", q, owner_pid,
                             run_status, last_ts))
        elif run_status == "running":
            rows.append(_row(task_id, CAT_PROJECTION_RUNNING,
                             "projection running while queue terminal", q,
                             owner_pid, run_status, last_ts))
        elif run_status not in TERMINAL_STATUSES:
            rows.append(_row(task_id, CAT_PROJECTION_UNSTAMPED,
                             f"projection not stamped terminal (status={run_status})",
                             q, owner_pid, run_status, last_ts))
        # 队列终态 + 投影终态 = 一致行，跳过。
    return rows
