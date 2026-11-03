"""TaskWorker 生命周期路径补全（C-16）。

既有 `test_worker.py` 钉了出队互斥 / 租约回收 / run_once 主干 / 取消不被洗回 /
run_forever 三种退出；本文件补齐循环体内的辅助域与执行侧分支：

- A6 GC 门控：`_gc_interval_ticks` 三分支（无 orch / 读配置 / 配置读错）、
  `_maybe_gc`（无 orch 跳过 / 失败只告警）、run_forever 的 tick 取模门——
  **忙时也 GC 是 A6 的本意**（持续饱和的 worker 才是最需要回收的场景），
  间隔未到则绝不提前；
- `refresh_config`：无 orch False / 热加载 True / 失败告警不退出；
- orchestrator 懒构建（构建一次 + 注册一次）与 shutdown 所有权
  （注入的不关、自建的关且幂等）；
- `_execute` 分支：resume 一次性认领（跑完即清位）、principal 透传成 owner、
  output 缺省、`LockfileMismatchError` 如实转 RuntimeError 拒绝执行；
- CLI：`build_arg_parser` 缺省面 + `main --once` 空库进程内返回 0。

mock 口径：`gc_from_orchestrator` / `PipelineOrchestrator` 都在**函数体内**
import，故 monkeypatch `loom_core.maintenance.gc_from_orchestrator` 与
`loom_core.pipeline.PipelineOrchestrator` 打点。
"""
from __future__ import annotations

import signal
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from loom_core.pipeline import TaskStatus
from loom_core.scheduler import LockfileMismatchError
from loom_core.task_queue import TaskQueue
from loom_core.worker import DEFAULT_LEASE_STALE_SECONDS, DEFAULT_POLL_INTERVAL, TaskWorker, build_arg_parser, main

PROJECT = Path(__file__).resolve().parent.parent


class _RecordingOrch:
    """带 config 与 reload 面的最小替身：GC 门控/热加载/执行记录都够用。"""

    def __init__(self, config=None, status=TaskStatus.DONE, error="",
                 raises=None, reload_returns=False, reload_raises=None):
        self.config = config if config is not None else {}
        self._status = status
        self._error = error
        self._raises = raises
        self._reload_returns = reload_returns
        self._reload_raises = reload_raises
        self.calls = []
        self.refresh_calls = 0
        self.shutdown_calls = 0

    def run_plan(self, plan, input_file="", task_id=None, wait=True, owner="",
                 resume=False):
        self.calls.append({"task_id": task_id, "input_file": input_file,
                           "owner": owner, "resume": resume,
                           "output": plan.raw.get("pipeline", {}).get("output")})
        if self._raises:
            raise self._raises
        return SimpleNamespace(status=self._status, result={}, error=self._error)

    def refresh_config_if_changed(self):
        self.refresh_calls += 1
        if self._reload_raises:
            raise self._reload_raises
        return self._reload_returns

    def register_agents(self):
        pass

    def shutdown(self):
        self.shutdown_calls += 1


class _RaisingConfig:
    """config.get 一碰就炸：证明配置读错只回落缺省，不拖垮 worker。"""

    def get(self, *a, **kw):
        raise RuntimeError("配置库坏了")


def _queue(tmp_path) -> TaskQueue:
    return TaskQueue(str(tmp_path / "tasks.db"))


def _resume_bit(db_path: Path, task_id: str) -> int:
    """q.get() 的投影不含 resume 列，直接对库读。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT resume FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _worker(tmp_path, orch=None, **kw) -> TaskWorker:
    return TaskWorker(orchestrator=orch, queue=_queue(tmp_path),
                      agents_dir=str(PROJECT / "agents"),
                      pipeline_dir=str(PROJECT / "pipelines"), worker_id="w-c16", **kw)


# ─── A6 GC 门控 ──────────────────────────────────────────────

class TestGcIntervalTicks:

    def test_defaults_to_60_without_orchestrator(self, tmp_path):
        """空转 worker 未构建 orchestrator：回落缺省 60，不去碰配置。"""
        worker = _worker(tmp_path, None)
        assert worker._gc_interval_ticks() == 60

    def test_reads_gc_ticks_from_config(self, tmp_path):
        orch = _RecordingOrch(config={"maintenance.gc_ticks": 3})
        assert _worker(tmp_path, orch)._gc_interval_ticks() == 3

    def test_falsy_value_falls_back_to_60(self, tmp_path):
        """`gc_ticks: 0`（或 None）不合法：`or 60` 兜底，而不是每 tick 都 GC。"""
        orch = _RecordingOrch(config={"maintenance.gc_ticks": 0})
        assert _worker(tmp_path, orch)._gc_interval_ticks() == 60

    def test_config_error_falls_back_instead_of_crashing(self, tmp_path):
        """配置读错不该让 worker 循环退出——这是该分支存在的唯一理由。"""
        orch = _RecordingOrch()
        orch.config = _RaisingConfig()
        assert _worker(tmp_path, orch)._gc_interval_ticks() == 60


class TestMaybeGc:

    def test_noop_without_orchestrator(self, tmp_path, monkeypatch):
        """空转 worker 不为 GC 构建 orchestrator（与 refresh_config 同纪律）。"""
        fired = MagicMock()
        monkeypatch.setattr("loom_core.maintenance.gc_from_orchestrator", fired)
        worker = _worker(tmp_path, None)
        worker._maybe_gc()
        fired.assert_not_called()

    def test_failure_warns_but_keeps_worker_alive(self, tmp_path, monkeypatch):
        fired = MagicMock(side_effect=RuntimeError("GC 表锁死"))
        monkeypatch.setattr("loom_core.maintenance.gc_from_orchestrator", fired)
        worker = _worker(tmp_path, _RecordingOrch(), log=MagicMock())
        worker._maybe_gc()
        fired.assert_called_once()
        worker.log.warning.assert_called()  # 告警而非静默，也非异常外抛


class TestGcTickGate:
    """run_forever 的取模门：忙时也 GC（A6 回归），间隔未到绝不提前。"""

    def test_busy_ticks_still_gc(self, tmp_path, monkeypatch):
        """A6 修的正是『持续有活的 worker 永远不回收』——GC 在分支外按 tick 跑，
        忙轮次也必须命中；若挂回空转分支，gc_calls 会停在 0。"""
        fired = MagicMock()
        monkeypatch.setattr("loom_core.maintenance.gc_from_orchestrator", fired)
        q = _queue(tmp_path)
        orch = _RecordingOrch(config={"maintenance.gc_ticks": 1})
        worker = _worker(tmp_path, orch)
        for i in range(3):
            q.submit(f"t-gc-{i}", "test_pipeline", "input.md", {})

        assert worker.run_forever(poll_interval=0.01, idle_timeout=0.05) == 3
        assert fired.call_count >= 3, "忙轮次没跑 GC ⇒ A6 回归"
        assert fired.call_args[0][0] is orch  # 传的是这个 orchestrator 本尊

    def test_gc_does_not_fire_before_interval(self, tmp_path, monkeypatch):
        """3 条任务 + 几个空转 tick 远不到 60：一次 GC 都不该有。"""
        fired = MagicMock()
        monkeypatch.setattr("loom_core.maintenance.gc_from_orchestrator", fired)
        q = _queue(tmp_path)
        worker = _worker(tmp_path, _RecordingOrch(config={"maintenance.gc_ticks": 60}))
        for i in range(3):
            q.submit(f"t-quiet-{i}", "test_pipeline", "input.md", {})

        assert worker.run_forever(poll_interval=0.01, idle_timeout=0.05) == 3
        assert fired.call_count == 0


# ─── 配置热加载 ──────────────────────────────────────────────

class TestRefreshConfig:

    def test_without_orchestrator_returns_false(self, tmp_path):
        """首个任务构建 orchestrator 时自然读到新配置，空转不必提前加载。"""
        assert _worker(tmp_path, None).refresh_config() is False

    def test_hot_reload_true_when_revision_changed(self, tmp_path):
        orch = _RecordingOrch(reload_returns=True)
        worker = _worker(tmp_path, orch)
        assert worker.refresh_config() is True
        assert orch.refresh_calls == 1

    def test_reload_failure_warns_and_returns_false(self, tmp_path):
        """同步失败只告警不退出：拿不到新配置也不该让 worker 死。"""
        orch = _RecordingOrch(reload_raises=RuntimeError("覆写文件读不动"))
        worker = _worker(tmp_path, orch, log=MagicMock())
        assert worker.refresh_config() is False
        worker.log.warning.assert_called()


# ─── orchestrator 懒构建与 shutdown 所有权 ────────────────────

class TestOrchestratorLifecycle:

    def test_built_once_and_agents_registered(self, tmp_path, monkeypatch):
        built = []

        class _FakeOrchClass(_RecordingOrch):
            def __init__(self, *, agents_dir, **kw):
                super().__init__(**kw)
                self.agents_dir = agents_dir
                self.registered = 0
                built.append(self)

            def register_agents(self):
                self.registered += 1

        monkeypatch.setattr("loom_core.pipeline.PipelineOrchestrator", _FakeOrchClass)
        worker = _worker(tmp_path, None)  # 未注入 ⇒ 属性首次访问时懒构建

        first = worker.orchestrator
        assert built and built[0] is first
        assert first.registered == 1
        assert first.agents_dir == worker.agents_dir
        assert worker.orchestrator is first  # 第二次访问复用同一实例
        assert first.registered == 1

    def test_shutdown_respects_ownership(self, tmp_path):
        """注入的 orchestrator 归调用方所有：worker 退出时不得替人关闭。"""
        injected = _RecordingOrch()
        worker = _worker(tmp_path, injected)
        worker.shutdown()
        assert injected.shutdown_calls == 0
        assert worker._orch is injected

    def test_shutdown_owned_orchestrator_and_idempotent(self, tmp_path, monkeypatch):
        class _FakeOrchClass(_RecordingOrch):
            def __init__(self, *, agents_dir, **kw):
                super().__init__(**kw)

        monkeypatch.setattr("loom_core.pipeline.PipelineOrchestrator", _FakeOrchClass)
        worker = _worker(tmp_path, None)
        owned = worker.orchestrator
        worker.shutdown()
        assert owned.shutdown_calls == 1
        assert worker._orch is None
        worker.shutdown()  # 已置 None：二次关闭是无害空操作
        assert owned.shutdown_calls == 1


# ─── _execute 分支 ───────────────────────────────────────────

class TestExecuteBranches:

    def test_resume_flag_cleared_after_single_claim(self, tmp_path):
        """断点续跑只对这一次认领生效：失败重试/手动重跑不得复用旧断点。"""
        q = _queue(tmp_path)
        q.submit("t-resume", "test_pipeline", "input.md", {})
        assert q.mark_for_resume("t-resume") is True
        orch = _RecordingOrch()
        worker = _worker(tmp_path, orch)

        assert worker.run_once() is not None
        assert orch.calls == [{"task_id": "t-resume", "input_file": "input.md",
                               "owner": "", "resume": True,
                               "output": "output/t-resume_result.md"}]  # config 空 ⇒ 缺省 output
        assert _resume_bit(tmp_path / "tasks.db", "t-resume") == 0, "resume 位必须清零"

    def test_non_resume_task_never_touches_resume_bit(self, tmp_path):
        q = _queue(tmp_path)
        q.submit("t-fresh", "test_pipeline", "input.md", {})
        orch = _RecordingOrch()
        worker = _worker(tmp_path, orch)
        worker.run_once()
        assert orch.calls[0]["resume"] is False
        assert _resume_bit(tmp_path / "tasks.db", "t-fresh") == 0

    def test_principal_is_passed_through_as_owner(self, tmp_path):
        q = _queue(tmp_path)
        q.submit("t-principal", "test_pipeline", "input.md", {}, principal="alice")
        orch = _RecordingOrch()
        _worker(tmp_path, orch).run_once()
        assert orch.calls[0]["owner"] == "alice"

    def test_lockfile_mismatch_fails_honestly(self, tmp_path):
        """锁漂移一律拒绝执行并如实写 failed，而不是退回 legacy 路径蒙混。"""
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text("steps: []", encoding="utf-8")

        class _MismatchScheduler:
            def _pipeline_path(self, name):
                return plan_path

            def parse_file(self, path, verify_lock=True):
                raise LockfileMismatchError("test_pipeline", ["hash 不一致"])

        q = _queue(tmp_path)
        q.submit("t-lock", "test_pipeline", "input.md", {})
        worker = _worker(tmp_path, _RecordingOrch(), scheduler=_MismatchScheduler())

        worker.run_once()
        row = q.get("t-lock")
        assert row["status"] == "failed"
        assert "锁文件校验失败" in row["error"], row["error"]
        assert worker.failed == 1


# ─── CLI 面 ──────────────────────────────────────────────────

class _BrokenQueue:
    """recover 一碰就炸：回收失败不该让 worker 循环退出。"""

    def recover(self, stale_seconds):
        raise RuntimeError("队列库锁死")


class TestCli:

    def test_reclaim_failure_warns_and_returns_zero(self, tmp_path):
        """recover 失败：告警 + 返回 0，绝不让 run_once 的第一步炸掉 worker。"""
        worker = TaskWorker(orchestrator=None, queue=_BrokenQueue(),
                            agents_dir=str(PROJECT / "agents"),
                            pipeline_dir=str(PROJECT / "pipelines"),
                            log=MagicMock())
        assert worker.reclaim_stale() == 0
        worker.log.warning.assert_called()

    def test_arg_parser_defaults(self):
        args = build_arg_parser().parse_args([])
        assert args.worker_id == ""
        assert args.once is False
        assert args.poll_interval == DEFAULT_POLL_INTERVAL
        assert args.idle_timeout is None
        assert args.lease_stale == DEFAULT_LEASE_STALE_SECONDS
        assert args.agents_dir == "" and args.pipeline_dir == ""

    def test_arg_parser_accepts_all_flags(self):
        args = build_arg_parser().parse_args(
            ["--once", "--worker-id", "w9", "--poll-interval", "0.5",
             "--idle-timeout", "7", "--lease-stale", "30",
             "--agents-dir", "a", "--pipeline-dir", "p"])
        assert (args.once, args.worker_id, args.poll_interval,
                args.idle_timeout, args.lease_stale, args.agents_dir,
                args.pipeline_dir) == (True, "w9", 0.5, 7.0, 30.0, "a", "p")

    def test_main_once_empty_queue_returns_zero(self, tmp_path, monkeypatch):
        """进程内跑一遍 main 的 --once 空库路径：装 handler → 消费 0 条 → 收尾退出。"""
        monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "state"))
        assert main(["--once", "--worker-id=probe"]) == 0

    def test_main_forever_branch_and_signal_handler(self, tmp_path, monkeypatch):
        """不带 --once 走 run_forever 分支（idle_timeout 快速退出）；
        退出后 SIGINT handler 仍在——直接调用它验证信号路径不炸、只置位。"""
        monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "state"))
        assert main(["--idle-timeout", "0.05", "--poll-interval", "0.01",
                     "--worker-id=probe"]) == 0
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)  # 主进程自调 handler：置 stop_event + 打印，不抛错
