"""TaskWorker — 常驻消费 `task_queue` 里 pending 的任务。

补齐的是队列的消费端。实测证据：`TaskQueue.acquire()` 自始至终没有任何调用方
（全仓 grep 只命中它自己的定义与 docstring 示例），所以 `POST /api/tasks` 把任务
写进队列之后，只有人再手动跑一次 `run.py --recover` 才会真的执行——队列只进不出，
"提交即排队"这句话在跨进程部署里并不成立。

职责边界：
  - claim：用 `acquire()` 的原子出队（条件 UPDATE + rowcount），同一任务不会被
    两个 worker 拿到；崩溃 worker 留下的 running 行由 `recover(stale_seconds=…)`
    按"租约过期 + owner_pid 已死"两个条件回收，活着的进程正在跑的任务不动。
  - execute：按流水线名解析 YAML → `orch.run_plan()`。锁文件漂移一律拒绝执行，
    并如实写成 failed，而不是退回 legacy 路径蒙混。
  - 收尾：流水线自己会在 `_finalize_plan_task` 写终态；这里只兜住"抛异常/崩溃在
    收尾之前"的情况。写终态用 `TaskQueue.finish`，取消过的行不会被洗成 done。

用法：
    python run.py --worker                      # 一直跑，Ctrl-C 优雅退出
    python run.py --worker --once               # 只消费一条（调试/ cron）
    python loom_core/worker.py              # 等价入口
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import paths
from .scheduler import LockfileMismatchError, Scheduler, installed_pipelines
from .task_queue import TaskQueue

logger = logging.getLogger("worker")

DEFAULT_POLL_INTERVAL = 1.0
#: 租约过期阈值：running 且 owner_pid 已死、且超过这个时长才回收
DEFAULT_LEASE_STALE_SECONDS = 900.0


class TaskWorker:
    """单进程 worker 循环。多个实例可以并发跑同一份 tasks.db。"""

    def __init__(self, orchestrator: Any = None, queue: TaskQueue | None = None,
                 scheduler: Scheduler | None = None, worker_id: str = "",
                 agents_dir: str = "", pipeline_dir: str = "",
                 lease_stale_seconds: float = DEFAULT_LEASE_STALE_SECONDS,
                 log: logging.Logger | None = None):
        self.agents_dir = agents_dir or str(paths.agents_dir())
        self.pipeline_dir = Path(pipeline_dir or str(paths.pipelines_dir()))
        self.queue = queue or TaskQueue()
        self.scheduler = scheduler or Scheduler(agents_dir=self.agents_dir,
                                                pipeline_dir=str(self.pipeline_dir))
        self.worker_id = worker_id or f"pid{os.getpid()}"
        self.lease_stale_seconds = lease_stale_seconds
        self.log = log or logger
        self._orch = orchestrator
        self._owns_orchestrator = orchestrator is None
        self.processed = 0
        self.failed = 0

    # ── 生命周期 ─────────────────────────────────────

    @property
    def orchestrator(self) -> Any:
        if self._orch is None:
            from .pipeline import PipelineOrchestrator

            self._orch = PipelineOrchestrator(agents_dir=self.agents_dir)
            self._orch.register_agents()
        return self._orch

    def shutdown(self) -> None:
        if self._owns_orchestrator and self._orch is not None:
            with contextlib.suppress(Exception):
                self._orch.shutdown()
            self._orch = None

    def run_forever(self, poll_interval: float = DEFAULT_POLL_INTERVAL,
                    stop_event: threading.Event | None = None,
                    idle_timeout: float | None = None) -> int:
        """消费直到 stop_event 置位或空转超过 idle_timeout。返回处理条数。"""
        stop_event = stop_event or threading.Event()
        idle_started: float | None = None
        ticks = 0
        while not stop_event.is_set():
            ticks += 1
            self.refresh_config()
            item = self.run_once()
            # A6：GC 与"这一轮有没有活儿"无关——原先它挂在下面的空转分支里，
            # 于是**持续有活的 worker 永远不回收**，而那正是 A6 要治的场景
            # （长跑且饱和的 worker 无限膨胀）。放在分支外，两者都按 tick 跑。
            if self._gc_interval_ticks() and ticks % self._gc_interval_ticks() == 0:
                self._maybe_gc()
            if item is None:
                if idle_timeout is not None:
                    idle_started = idle_started or time.time()
                    if time.time() - idle_started >= idle_timeout:
                        self.log.info("worker 空转到超时，退出")
                        break
                time.sleep(poll_interval)
            else:
                idle_started = None
        return self.processed

    # ── 保留期 GC（A6）────────────────────────

    def _gc_interval_ticks(self) -> int:
        """每 N 个轮询跑一次 GC（配置 `maintenance.gc_ticks`，默认 60）。"""
        orch = self._orch
        if orch is None:
            return 60
        try:
            return max(1, int(orch.config.get("maintenance.gc_ticks", 60) or 60))
        except Exception:                     # noqa: BLE001 — 配置读错不该让 worker 退出
            return 60

    def _maybe_gc(self) -> None:
        """跑一次保留期 GC。失败只告警不拖垮 worker；空转 worker 不构建
        orchestrator（与 refresh_config 的既有纪律一致）。"""
        orch = self._orch
        if orch is None:
            return
        try:
            from .maintenance import gc_from_orchestrator
            gc_from_orchestrator(orch, log=self.log.info)
        except Exception as e:               # noqa: BLE001
            self.log.warning(f"GC 失败（保留）：{e}")

    # ── 跨进程配置同步 ─────────────────────────────

    def refresh_config(self) -> bool:
        """P1 验收第三条：别的进程改过运行时配置（覆写文件 revision 变）就热加载。

        orchestrator 尚未构建时直接返回 False——首个任务构建 orchestrator 的
        那一刻会读到最新配置，无需也不该为一个空转 worker 提前加载全部 Agent。
        同步失败只告警不退出：拿不到新配置也不该让 worker 死。
        """
        orch = self._orch
        if orch is None:
            return False
        try:
            return bool(orch.refresh_config_if_changed())
        except Exception as e:  # noqa: BLE001  见 docstring：同步失败不该让 worker 退出
            self.log.warning(f"配置热加载失败，保留旧配置: {e}")
            return False

    # ── 单轮 ─────────────────────────────────────────

    def reclaim_stale(self) -> int:
        """回收崩溃 worker 留下的 running 行（租约过期 + owner 已死）。"""
        try:
            recovered = self.queue.recover(stale_seconds=self.lease_stale_seconds)
        except Exception as e:  # noqa: BLE001  回收失败不该让 worker 退出
            self.log.warning(f"recover 失败: {e}")
            return 0
        for row in recovered:
            self.log.info(f"回收过期任务 {row['task_id']}（原 worker 已不在执行）")
        return len(recovered)

    def run_once(self) -> dict | None:
        """处理至多一条任务；没有可做的返回 None。"""
        self.reclaim_stale()
        item = self.queue.acquire(worker_id=self.worker_id)
        if not item:
            return None
        task_id = item["task_id"]
        self.log.info(f"开始任务 {task_id} pipeline={item['pipeline_name']}")
        started = time.time()
        try:
            self._execute(item)
        except Exception as e:  # noqa: BLE001  worker 不能因为单任务异常而退出
            self.failed += 1
            self.log.error(f"任务 {task_id} 异常: {e}")
            # 收尾没跑成（崩溃在 _finalize 之前）时补写终态；取消过的行不动
            # —— 这条分支意味着引擎自己的投影盖章没跑到，标 incomplete 让缺口可见
            self.queue.finish(task_id, "failed", error=str(e),
                              projection="incomplete")
        else:
            self.processed += 1
            self.log.info(f"任务 {task_id} 结束，耗时 {time.time() - started:.1f}s")
        return item

    def _execute(self, item: dict) -> None:
        task_id = item["task_id"]
        name = item["pipeline_name"]
        # pack 感知解析：核心 pipelines/ 优先，其次物理 pack 的 pipelines/
        plan_path = self.scheduler._pipeline_path(name)
        if not plan_path.exists():
            available = ", ".join(installed_pipelines(self.pipeline_dir)) or "无"
            raise FileNotFoundError(
                f"流水线 '{name}' 不存在（可用: {available}）")

        config = item.get("config") or {}
        output = config.get("output") or f"output/{task_id}_result.md"
        try:
            plan = self.scheduler.parse_file(str(plan_path), verify_lock=True)
        except LockfileMismatchError as e:
            raise RuntimeError(f"锁文件校验失败，拒绝执行: {e}") from e
        plan.raw.setdefault("pipeline", {})["output"] = output

        task = self.orchestrator.run_plan(plan, input_file=item.get("input_file", ""),
                                          task_id=task_id, wait=True,
                                          resume=bool(item.get("resume")),
                                          owner=str(item.get("principal") or ""))
        if item.get("resume"):
            # 标记只在这一次认领生效：失败重试/手动重跑不该再复用旧断点
            self.queue.clear_resume(task_id)
        # _finalize_plan_task 已经写过终态；这里只兜住它没跑到的分支
        # （行已是终态时 finish 返回 False，什么都不改）。兜到的那条同样缺投影盖章。
        self.queue.finish(task_id, task.status.value, error=task.error,
                          projection="incomplete")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Loom 常驻 worker（消费任务队列）")
    ap.add_argument("--worker-id", default="", help="写进队列 worker_id 列，默认 pidN")
    ap.add_argument("--once", action="store_true", help="只消费一条任务后退出")
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    ap.add_argument("--idle-timeout", type=float, default=None,
                    help="空转多少秒后退出（默认一直跑）")
    ap.add_argument("--lease-stale", type=float, default=DEFAULT_LEASE_STALE_SECONDS,
                    help="回收 running 行的租约阈值（秒）")
    ap.add_argument("--agents-dir", default="")
    ap.add_argument("--pipeline-dir", default="")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [worker] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = build_arg_parser().parse_args(argv)
    worker = TaskWorker(worker_id=args.worker_id, agents_dir=args.agents_dir,
                        pipeline_dir=args.pipeline_dir, lease_stale_seconds=args.lease_stale)

    stop_event = threading.Event()

    def _stop(signum, _frame):
        stop_event.set()
        print(f"\n[worker] 收到信号 {signum}，处理完当前任务后退出")

    signal.signal(signal.SIGINT, _stop)
    with contextlib.suppress(AttributeError):
        signal.signal(signal.SIGTERM, _stop)

    print(f"[worker] id={worker.worker_id} pipelines={worker.pipeline_dir} "
          f"poll={args.poll_interval}s lease={args.lease_stale}s")
    try:
        if args.once:
            n = 1 if worker.run_once() else 0
        else:
            n = worker.run_forever(poll_interval=args.poll_interval,
                                   stop_event=stop_event, idle_timeout=args.idle_timeout)
    finally:
        worker.shutdown()
    print(f"[worker] 退出：处理 {n} 条，失败 {worker.failed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
