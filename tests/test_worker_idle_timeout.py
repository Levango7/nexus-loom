"""C-21 钉子：run_forever 空转超时的确定性行为（假时钟 + 注入 sleep）。

判据钉行为不钉行号，四条各杀一类变异：
  A. >= 边界：elapsed 恰等于 idle_timeout 即退出（杀 ``>`` 与 ``*2`` 两变异）；
  B. 有活一轮重置计时：工作轮后 idle_started 归零（杀漏重置变异）；
  C. idle_timeout=None：空转分支整体跳过，只靠 stop_event 退出；
  D. 首钟 0.0：``or`` 真值判定会把 0.0 当未初始化、每轮重置计时永不退出
     （杀 ``idle_started or clock()`` 回归）。
线程 + join 兜底：变异体若不死循环，测试也在 5 秒内干净失败。
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from loom_core.worker import TaskWorker


class FakeClock:
    """每次调用返回当前值并 +step；首值可配（0.0 用于杀 ``or`` 回归）。"""

    def __init__(self, start: float = 1000.0, step: float = 1.0) -> None:
        self.value = start
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        now = self.value
        self.value += self.step
        return now


def _run_in_thread(worker: TaskWorker, **kwargs) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def _target() -> None:
        result["n"] = worker.run_forever(**kwargs)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(5.0)
    return t, result


def _make_worker(queue: MagicMock) -> TaskWorker:
    queue.recover.return_value = []
    return TaskWorker(orchestrator=MagicMock(), queue=queue,
                      scheduler=MagicMock(), worker_id="t")


def test_idle_exits_at_ge_boundary() -> None:
    clock = FakeClock(start=1000.0)
    queue = MagicMock()
    queue.acquire.return_value = None
    worker = _make_worker(queue)
    t, res = _run_in_thread(worker, poll_interval=0, idle_timeout=5,
                            clock=clock, sleep_fn=lambda _s: None)
    assert not t.is_alive()
    assert res["n"] == 0
    # tick1 设初值+检查 2 次，其后每轮只查 1 次；第 5 轮 elapsed==5 触发退出
    assert clock.calls == 6


def test_work_round_resets_idle_clock() -> None:
    clock = FakeClock(start=1000.0)
    queue = MagicMock()
    item = {"task_id": "t1", "pipeline_name": "p", "config": {}}
    seen = {"n": 0}
    stop = threading.Event()

    def acquire(**_kw):
        seen["n"] += 1
        if seen["n"] == 3:
            return item
        if seen["n"] >= 12:  # 安全阀：变异体不死循环
            stop.set()
        return None

    queue.acquire.side_effect = acquire
    worker = _make_worker(queue)
    t, res = _run_in_thread(worker, poll_interval=0, idle_timeout=5,
                            stop_event=stop, clock=clock,
                            sleep_fn=lambda _s: None)
    assert not t.is_alive()
    assert res["n"] == 1
    # tick1 设+查(2)、tick2 查(1)、tick3 有活重置(0)、tick4 设+查(2)、
    # tick5-8 各查(1)：第 8 轮 elapsed==5 退出，共 9 次
    assert clock.calls == 9


def test_no_idle_timeout_exits_only_via_stop_event() -> None:
    clock = FakeClock(start=1000.0)
    queue = MagicMock()
    stop = threading.Event()
    seen = {"n": 0}

    def acquire(**_kw):
        seen["n"] += 1
        if seen["n"] >= 3:
            stop.set()
        return None

    queue.acquire.side_effect = acquire
    worker = _make_worker(queue)
    t, res = _run_in_thread(worker, poll_interval=0, idle_timeout=None,
                            stop_event=stop, clock=clock,
                            sleep_fn=lambda _s: None)
    assert not t.is_alive()
    assert res["n"] == 0
    # idle_timeout=None 时钟零消费：空转分支整体跳过
    assert clock.calls == 0


def test_zero_valued_clock_still_times_out() -> None:
    clock = FakeClock(start=0.0)
    queue = MagicMock()
    stop = threading.Event()

    def acquire(**_kw):
        if clock.calls > 20:  # 安全阀：``or`` 回归变异体不死循环
            stop.set()
        return None

    queue.acquire.side_effect = acquire
    worker = _make_worker(queue)
    t, res = _run_in_thread(worker, poll_interval=0, idle_timeout=5,
                            stop_event=stop, clock=clock,
                            sleep_fn=lambda _s: None)
    assert not t.is_alive()
    assert res["n"] == 0
    assert clock.calls == 6
