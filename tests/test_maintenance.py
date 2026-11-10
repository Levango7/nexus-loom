"""保留期与 GC（设计复核 2026-10-20 §3.1 A6）的判据。

覆盖四层：
  A. RunStore.cleanup：年龄判据 + 数量判据 + 联动删（events/nodes/approvals
     一起走）+ 进行中的行一行不碰 + 假时钟；
  B. 既有 store 的清理：task_queue / cost / quality_feedback 的双判据
     （0 的语义按历史保持：age=0 = 删光）；
  C. maintenance.gc_runs / gc_from_orchestrator：disabled 早退、失败不拖垮、
     汇总日志；
  D. worker 接线：按配置周期调用 GC（重启后继续——判据落在"不依赖内存态"
     这条性质上）、空转 worker 不构建 orchestrator、GC 失败不拖垮循环。
"""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("LOOM_STATE_DIR", str(d))
    return d


# ── A. RunStore 清理 ──────────────────────────────────

class TestRunStoreCleanup:
    def _seed(self, store, run_id, *, finished_at, status="done",
              with_nodes=True, with_approval=False):
        store.open_run(run_id, workflow="p", started_at=finished_at - 10)
        if with_nodes:
            store.record_node(run_id, "n1", "success", started_at=finished_at - 9,
                              finished_at=finished_at - 1)
        if with_approval:
            store.open_approval(run_id, f"{run_id}-ap", pipeline="p",
                                nodes=[{"name": "n1"}], principal="u")
        store.close_run(run_id, status, finished_at=finished_at)

    def _store(self, state):
        from loom_core.run_store import RunStore
        return RunStore()

    def test_age_deletes_terminal_with_full_cascade(self, state):
        store = self._store(state)
        now = 10_000_000.0
        self._seed(store, "old", finished_at=now - 100 * 86400, with_approval=True)
        self._seed(store, "fresh", finished_at=now - 10, with_approval=True)
        self._seed(store, "running", finished_at=0, status="running")
        result = store.cleanup(retention_days=30, now=now)
        assert result["runs"] == 1
        assert store.get_run("old") is None
        assert store.get_run("fresh") is not None
        assert store.get_run("running") is not None
        # 联动删：老运行的事件/节点/审批一行不留
        assert store.events("old") == []
        assert store.nodes("old") == []
        assert store.open_approvals("old") == []

    def test_max_runs_keeps_newest(self, state):
        store = self._store(state)
        now = 10_000_000.0
        for i, rid in enumerate(["r1", "r2", "r3", "r4"]):
            self._seed(store, rid, finished_at=now - (4 - i) * 86400)
        result = store.cleanup(max_runs=2, now=now)
        assert result["runs"] == 2
        assert {store.get_run(rid)["status"] for rid in ("r3", "r4")} == {"done"}
        assert store.get_run("r1") is None and store.get_run("r2") is None

    def test_age_applies_to_kept_set_too(self, state):
        """保留集内也过旧的行：年龄判据对保留集内同样生效。"""
        store = self._store(state)
        now = 10_000_000.0
        self._seed(store, "old", finished_at=now - 100 * 86400)
        self._seed(store, "mid", finished_at=now - 10)
        self._seed(store, "new", finished_at=now - 1)
        store.cleanup(retention_days=30, max_runs=2, now=now)
        assert store.get_run("old") is None
        assert store.get_run("mid") is not None
        assert store.get_run("new") is not None

    def test_disabled_is_all_zero(self, state):
        store = self._store(state)
        self._seed(store, "x", finished_at=1.0)
        result = store.cleanup(now=10_000_000.0)
        assert result == {"runs": 0, "run_nodes": 0, "events": 0, "approvals": 0,
                          "run_ids": []}
        assert store.get_run("x") is not None


# ── B. 既有 store 清理 ──────────────────────────────────

class TestQueueCleanup:
    def test_max_rows_cap_keeps_newest(self, state):
        from loom_core.task_queue import TaskQueue
        q = TaskQueue()
        for i in range(5):
            q.submit(f"t{i}", "p", "a.md")
            q.complete(f"t{i}")
        # complete() 的时间戳是 now——直接 SQL 钉成递增值保证确定性
        with q._lock, q._get_conn() as conn:
            for i in range(5):
                conn.execute("UPDATE task_queue SET finished_at = ? WHERE task_id = ?",
                             (1000.0 + i, f"t{i}"))
        dropped = q.cleanup(max_age_days=10 ** 9, max_rows=2)
        assert dropped == 3
        assert q.get("t3") is not None and q.get("t4") is not None
        assert q.get("t0") is None

    def test_age_zero_still_deletes_all(self, state):
        """历史语义：max_age_days=0 = 删光全部终态行（旧判据钉着这条）。"""
        from loom_core.task_queue import TaskQueue
        q = TaskQueue()
        q.submit("t1", "p", "a.md")
        q.complete("t1")
        assert q.cleanup(max_age_days=0) == 1
        assert q.get("t1") is None


class TestCostCleanup:
    def test_max_rows_and_age(self, state, monkeypatch):
        """单例污染的教训：get_cost_tracker 是进程级单例，绑定的是**首次构造时**
        的 LOOM_STATE_DIR——全量跑里更早的测试先构造了它，本用例就会操作别人的库。
        每次用前重置（monkeypatch 负责还原）。"""
        from loom_core import cost_tracker
        monkeypatch.setattr(cost_tracker, "_tracker_instance", None)
        t = cost_tracker.get_cost_tracker()
        for i in range(5):
            t.record("cloudflare", 10, 10, cost=0.01, task_id=f"t{i}")
        # record 的时间戳是 now——直接 SQL 钉成递增值保证确定性
        with t._lock, t._conn() as conn:
            for i in range(5):
                conn.execute("UPDATE cost_log SET timestamp = ? WHERE task_id = ?",
                             (2000.0 + i, f"t{i}"))
        dropped = t.cleanup(max_age_days=10 ** 9, max_rows=2)
        assert dropped == 3
        assert t.total_cost() == pytest.approx(0.02)


class TestFeedbackCleanup:
    def test_default_disabled_and_explicit(self, state, monkeypatch):
        from loom_core import quality_feedback
        monkeypatch.setattr(quality_feedback, "_instance", None)
        f = quality_feedback.get_quality_feedback()
        f.record("t1", {"completeness": 0.8}, ["core"], "p")
        f.record("t2", {"completeness": 0.9}, ["core"], "p")
        # record 的时间戳是 now——直接 SQL 钉成一旧一新保证确定性
        now = time.time()
        with f._lock, f._conn() as conn:
            conn.execute("UPDATE quality_history SET timestamp = ? WHERE task_id = ?",
                         (now - 30 * 86400, "t1"))
            conn.execute("UPDATE quality_history SET timestamp = ? WHERE task_id = ?",
                         (now - 10, "t2"))
        # 默认 0 = 不启用：一条都不删
        assert f.cleanup() == 0
        assert f.stats()["total_records"] == 2
        # 显式启用（7 天）：t1 过旧删、t2 保留
        assert f.cleanup(max_age_days=7) == 1
        assert f.stats()["total_records"] == 1


# ── C. maintenance 模块 ──────────────────────────────────

class TestGcRuns:
    def test_disabled_returns_early(self):
        from loom_core.maintenance import gc_runs
        calls: list = []
        result = gc_runs(log=calls.append)
        assert result == {"enabled": False}
        assert calls == [], "未启用不该产生任何日志（静默不删是刻意的）"

    def test_calls_through_with_counts(self):
        from loom_core.maintenance import Retention, gc_runs
        q = SimpleNamespace(cleanup=lambda days, rows: 2)
        seen: dict = {}

        def cost_fn(days, rows, run_ids=None):
            seen["cost"] = (days, rows, run_ids)
            return 1

        def fb_fn(days, rows, run_ids=None):
            seen["feedback"] = (days, rows, run_ids)
            return 0

        rs = SimpleNamespace(cleanup=lambda days, rows: {"runs": 1})
        logs: list = []
        result = gc_runs(queue=q, cost=SimpleNamespace(cleanup=cost_fn),
                         feedback=SimpleNamespace(cleanup=fb_fn), run_store=rs,
                         retention=Retention(days=30, max_runs=10),
                         log=logs.append)
        assert result["queue"] == 2 and result["cost"] == 1
        assert result["run_store"] == {"runs": 1}
        # 双判据 parity：cost/quality 两库现在也收 max_runs（过去漏接，
        # max_runs-only 配置下两库无界）；run_store 先行回 run_ids，
        # 以关键字落位（第三个位置参是 now，位置传会把 set 塞进时钟槽）
        age = Retention(days=30, max_runs=10).age_days()
        assert seen["cost"] == (age, 10, set())
        assert seen["feedback"] == (age, 10, set())
        assert any("GC 执行" in m for m in logs)

    def test_store_failure_isolated(self):
        from loom_core.maintenance import Retention, gc_runs

        def boom(days, rows):
            raise RuntimeError("db down")

        result = gc_runs(queue=SimpleNamespace(cleanup=boom),
                         retention=Retention(days=30))
        assert result["queue"]["failed"] is True
        assert "db down" in result["queue"]["message"]

    def test_retention_from_config(self):
        from loom_core.maintenance import retention_from_config
        fake_cfg = SimpleNamespace(get=lambda key, default=0:
                                   {"retention.runs_days": 30,
                                    "retention.max_runs": 500}[key])
        r = retention_from_config(fake_cfg)
        assert (r.days, r.max_runs) == (30, 500)
        # 非正数 / 读错 → 0（保守，不悄悄变成"删光"）
        r2 = retention_from_config(SimpleNamespace(
            get=lambda key, default=0: -5))
        assert (r2.days, r2.max_runs) == (0, 0)
        r3 = retention_from_config(None)
        assert r3.enabled is False


class TestCrossStoreGc:
    """GC 的跨库口径：cost.db / quality.db 与 runs.db 是各自的库、无 FK。

    实证（2026-11-10 prove_l4.py）：
    - B1/B2：max_runs-only 配置下两库过去漏接数量判据 ⇒ 无界增长；
    - C1：runs.db 已删的 run，两库里指向它的行过去原样留着 ⇒ 悬空 task_id。
    """

    def test_max_runs_only_prunes_cost_and_quality(self, state):
        from loom_core.cost_tracker import CostTracker
        from loom_core.maintenance import Retention, gc_runs
        from loom_core.quality_feedback import QualityFeedback
        ct = CostTracker(db_path=str(state / "cost.db"))
        qf = QualityFeedback(db_path=str(state / "quality.db"))
        for i in range(4):
            ct.record("cloudflare", 10, 10, cost=0.01, task_id=f"t{i}")
            qf.record_overall(f"t{i}", 0.8)
            qf.record(f"t{i}", {"completeness": 0.8}, ["core"], "p")
        result = gc_runs(cost=ct, feedback=qf,
                         retention=Retention(days=0, max_runs=2))
        assert result["cost"] == 2, result
        assert result["feedback"] == 4, result      # history 裁 2 + 总分表裁 2
        assert ct.total_cost() == pytest.approx(0.02), "只剩最新两条成本"
        ct.close()
        qf.close()

    def test_gc_links_cost_and_quality_to_deleted_runs(self, state):
        import sqlite3

        from loom_core.cost_tracker import CostTracker
        from loom_core.maintenance import Retention, gc_runs
        from loom_core.quality_feedback import QualityFeedback
        from loom_core.run_store import RunStore
        rs = RunStore()
        ct = CostTracker(db_path=str(state / "cost.db"))
        qf = QualityFeedback(db_path=str(state / "quality.db"))
        now = 10_000_000.0
        rs.open_run("old", "p", started_at=now - 10)
        rs.close_run("old", "done", finished_at=now - 9)
        rs.open_run("keep", "p", started_at=now - 8)
        rs.close_run("keep", "done", finished_at=now - 8)
        ct.record("cloudflare", 10, 10, cost=0.01, task_id="old")
        ct.record("cloudflare", 10, 10, cost=0.01, task_id="keep")
        qf.record_overall("old", 0.8)
        qf.record_overall("keep", 0.9)
        result = gc_runs(cost=ct, feedback=qf, run_store=rs,
                         retention=Retention(days=0, max_runs=1), now=now)
        assert result["run_store"]["runs"] == 1
        assert result["cost"] == 1, result          # old 的成本行联动删
        assert result["feedback"] == 1, result      # old 的总分行联动删
        conn = sqlite3.connect(str(state / "cost.db"))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM cost_log WHERE task_id=?",
                ("old",)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM cost_log WHERE task_id=?",
                ("keep",)).fetchone()[0] == 1
        finally:
            conn.close()
        conn = sqlite3.connect(str(state / "quality.db"))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM quality_runs WHERE task_id=?",
                ("old",)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM quality_runs WHERE task_id=?",
                ("keep",)).fetchone()[0] == 1
        finally:
            conn.close()
        ct.close()
        qf.close()


# ── D. worker 接线 ───────────────────────────────────────

class TestWorkerGcHook:
    def _worker(self, tmp_path, monkeypatch, retention_days=0, gc_ticks=60):
        from loom_core.worker import TaskWorker
        monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "state"))
        fake_cfg = SimpleNamespace(
            get=lambda key, default=0:
            {"maintenance.gc_ticks": gc_ticks,
             "retention.runs_days": retention_days}[key])
        orch = SimpleNamespace(config=fake_cfg, task_queue=SimpleNamespace(),
                               refresh_config_if_changed=lambda: False)
        w = TaskWorker(queue=SimpleNamespace(recover=lambda **kw: [],
                                             acquire=lambda **kw: None),
                       scheduler=SimpleNamespace())
        w._orch = orch
        return w

    def test_gc_called_periodically(self, tmp_path, monkeypatch):
        w = self._worker(tmp_path, monkeypatch, retention_days=30, gc_ticks=2)
        calls: list = []
        import loom_core.maintenance as m

        def fake_gc(orch, now=None, log=None):
            calls.append(orch)
            return {"enabled": True}

        monkeypatch.setattr(m, "gc_from_orchestrator", fake_gc)
        import threading

        stop = threading.Event()
        import time as _t

        def stop_later():
            _t.sleep(0.6)
            stop.set()

        t = threading.Thread(target=stop_later, daemon=True)
        t.start()
        w.run_forever(poll_interval=0.05, stop_event=stop)
        assert len(calls) >= 2, f"GC 每 {2} 轮应至少跑 2 次，实际 {len(calls)}"

    def test_idle_worker_does_not_build_orchestrator_for_gc(self, tmp_path, monkeypatch):
        w = self._worker(tmp_path, monkeypatch)
        w._orch = None
        import loom_core.maintenance as m

        called: list = []
        monkeypatch.setattr(m, "gc_from_orchestrator",
                            lambda orch, now=None, log=None: called.append(1))
        import threading
        import time as _t

        stop = threading.Event()
        t = threading.Thread(target=lambda: (_t.sleep(0.4), stop.set()), daemon=True)
        t.start()
        w.run_forever(poll_interval=0.05, stop_event=stop)
        assert called == [], "空转 worker 不该为 GC 构建 orchestrator"
        assert w._orch is None

    def test_gc_failure_does_not_kill_worker(self, tmp_path, monkeypatch):
        w = self._worker(tmp_path, monkeypatch, retention_days=30, gc_ticks=1)
        import loom_core.maintenance as m

        def boom(orch, now=None, log=None):
            raise RuntimeError("gc boom")

        monkeypatch.setattr(m, "gc_from_orchestrator", boom)
        import threading
        import time as _t

        stop = threading.Event()
        t = threading.Thread(target=lambda: (_t.sleep(0.3), stop.set()), daemon=True)
        t.start()
        processed = w.run_forever(poll_interval=0.05, stop_event=stop)
        assert processed == 0, "GC 失败后 worker 循环必须继续"


# ── E. CLI ──────────────────────────────────────────────

class TestGcCli:
    def test_flag_registered(self):
        import run as run_mod
        parser = run_mod.build_arg_parser()
        dests = {a.dest for a in parser._actions}
        assert "gc" in dests

    def test_gc_cli_end_to_end_disabled(self, tmp_path):
        """仓库默认配置 retention 未启用 → 如实报告"未启用"，退出 0。"""
        import subprocess
        import sys
        r = subprocess.run(
            [sys.executable, str(PROJECT / "run.py"), "--gc"],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr[-800:]
        assert "retention 未启用" in r.stdout


# ── F. DeliveryLedger.cleanup（A2：交付账本清理）─────────────

class TestDeliveryLedgerCleanup:
    """A2: DeliveryLedger.cleanup() 的双判据清理。

    与 RunStore.cleanup 同一口径：
      - retention_days > 0：删除 finished_at 早于 now - days*86400 的记录
      - max_runs > 0：只保留 finished_at 最新的 N 条
      - 两个判据可同时生效
      - 0/0 = 不清理（返回 0）
      - 只碰终态（done/failed/cancelled），进行中的行一行都不删
    """

    def _led(self, state):
        from loom_core.deliveries import DeliveryLedger
        return DeliveryLedger()

    def _seed(self, led, run_id, *, finished_at, status="done",
              output_path=None):
        led.record({"run_id": run_id, "pipeline": "p", "status": status,
                    "finished_at": float(finished_at),
                    "duration_sec": 1.0,
                    "output_path": output_path or f"{run_id}.md",
                    "nodes": []})

    def test_age_deletes_old(self, state):
        led = self._led(state)
        now = 10_000_000.0
        self._seed(led, "old", finished_at=now - 100 * 86400)
        self._seed(led, "fresh", finished_at=now - 10)
        deleted = led.cleanup(retention_days=30, now=now)
        assert deleted == 1
        assert led.get("old") is None
        assert led.get("fresh") is not None

    def test_max_runs_keeps_newest(self, state):
        led = self._led(state)
        now = 10_000_000.0
        for i, rid in enumerate(["r1", "r2", "r3", "r4"]):
            self._seed(led, rid, finished_at=now - (4 - i) * 86400)
        deleted = led.cleanup(max_runs=2, now=now)
        assert deleted == 2
        remaining = {r["run_id"] for r in led.list(limit=100)}
        assert "r3" in remaining and "r4" in remaining
        assert "r1" not in remaining and "r2" not in remaining

    def test_age_applies_to_kept_set_too(self, state):
        """保留集内也过旧的行：年龄判据对保留集内同样生效。"""
        led = self._led(state)
        now = 10_000_000.0
        self._seed(led, "old", finished_at=now - 100 * 86400)
        self._seed(led, "mid", finished_at=now - 10)
        self._seed(led, "new", finished_at=now - 1)
        led.cleanup(retention_days=30, max_runs=2, now=now)
        assert led.get("old") is None
        assert led.get("mid") is not None
        assert led.get("new") is not None

    def test_disabled_returns_zero(self, state):
        """0/0 = 不清理（与 RunStore.cleanup 口径一致）。"""
        led = self._led(state)
        self._seed(led, "x", finished_at=1.0)
        assert led.cleanup(now=10_000_000.0) == 0
        assert led.get("x") is not None

    def test_non_terminal_rows_are_untouched_by_age(self, state):
        """年龄判据也不碰进行中的行：活账本（finished_at=0）比过期记录更该留。"""
        led = self._led(state)
        now = 10_000_000.0
        self._seed(led, "old", finished_at=now - 100 * 86400)
        self._seed(led, "running", finished_at=0.0, status="running")
        deleted = led.cleanup(retention_days=30, now=now)
        assert deleted == 1
        assert led.get("old") is None
        assert led.get("running") is not None

    def test_non_terminal_rows_ignored_by_max_runs(self, state):
        """数量判据同样不碰进行中的行：它不占"最近 N 条"的名额。"""
        led = self._led(state)
        now = 10_000_000.0
        self._seed(led, "t1", finished_at=now - 30)
        self._seed(led, "t2", finished_at=now - 10)
        self._seed(led, "running", finished_at=0.0, status="running")
        deleted = led.cleanup(max_runs=1, now=now)
        assert deleted == 1
        assert led.get("t1") is None
        assert led.get("t2") is not None
        assert led.get("running") is not None

    def test_terminal_statuses_agree_with_run_store(self, state):
        """两处"终态"必须是同一套三值：判据漂移会让一张表删了、另一张没删。"""
        from loom_core import deliveries as deliveries_mod
        from loom_core import run_store

        assert tuple(deliveries_mod.TERMINAL_STATUSES) == tuple(
            run_store.TERMINAL_STATUSES)
        # 行为级钉面：run_store 认作终态的每种状态，账本清理都要敢删。
        now = 10_000_000.0
        led = self._led(state)
        for status in run_store.TERMINAL_STATUSES:
            self._seed(led, f"r-{status}", finished_at=0.0, status=status)
        self._seed(led, "alive", finished_at=0.0, status="running")
        assert led.cleanup(retention_days=1, now=now) == len(
            run_store.TERMINAL_STATUSES)
        for status in run_store.TERMINAL_STATUSES:
            assert led.get(f"r-{status}") is None
        assert led.get("alive") is not None

    def test_cleanup_never_deletes_the_delivered_file(self, state, tmp_path):
        """账本行走了，磁盘产物还在——清理只删行，不碰用户声明的输出。

        交付物的所有权不在 GC：output_path 指向的是用户仓库里的产物，
        删账本的保留期策略不该外溢成删文件。这条防的是将来有人把
        "清理过期记录"扩写成"清理过期产物"。
        """
        doc = tmp_path / "result.md"
        doc.write_text("# 交付\n正文", encoding="utf-8")
        led = self._led(state)
        now = 10_000_000.0
        self._seed(led, "with-file", finished_at=now - 100 * 86400,
                   output_path=str(doc))
        assert led.cleanup(retention_days=30, now=now) == 1
        assert doc.exists(), "GC 不该删磁盘上的交付物"
        assert doc.read_text(encoding="utf-8").startswith("# 交付")
        assert led.get("with-file") is None

    def test_gc_runs_includes_deliveries(self):
        """gc_runs 汇总里应含 deliveries 键（A2 接线判据）。"""
        from loom_core.maintenance import Retention, gc_runs
        led = SimpleNamespace(cleanup=lambda days, rows: 3)
        result = gc_runs(
            deliveries=led,
            retention=Retention(days=30, max_runs=10),
            log=lambda m: None,
        )
        assert result["deliveries"] == 3

    def test_gc_runs_deliveries_failure_isolated(self):
        """deliveries cleanup 失败不该拖垮其他 store。"""
        from loom_core.maintenance import Retention, gc_runs

        def boom(days, rows):
            raise RuntimeError("ledger down")

        result = gc_runs(
            deliveries=SimpleNamespace(cleanup=boom),
            retention=Retention(days=30),
            log=lambda m: None,
        )
        assert result["deliveries"]["failed"] is True
        assert "ledger down" in result["deliveries"]["message"]
