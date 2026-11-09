"""L4-01：`runs.cost_usd` / `runs.quality_score` 从「恒空字段」变成真值。

审计证据（板面 §27.1）：`close_run()` 收这两个参数并写进列，但**两个生产调用点
都不传**（`pipeline._runstore_close` 终态投影、`backfill.replay` 补账）⇒ 列永远
停在 `DEFAULT 0`；而 `run_handle.py` 把这两列读给消费者（`source:"runstore"`）。

本文件的判据分三层，缺一层修复就等于没修：

1. **真值来源**：成本在 `cost_log.task_id`（`CostTracker`），质量总分在
   `quality_runs`（本次新增，`QualityFeedback`）。没有来源，盖章只是把 0 换个地方写。
2. **归属传播**：`llm_router` 记成本时调用方拿不到 task 上下文，靠
   `cost_task_scope`（ContextVar）在**节点执行线程内部**声明；`llm_router` 两次
   记录（有 usage / 估算）都必须带上 `task_id`。
3. **盖章**：`_runstore_close` 与 `backfill.replay` 都要把取回的两个值交给 `close_run`；
   补账路径同样要捞（成本/质量各有自己的持久化真值，与「账本只存指纹」不冲突）。

判据全部**绑定形态**：谁写给谁、哪个 kwarg、哪一列。变异电池见 PR 描述。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from loom_core import run_metrics
from loom_core.cost_tracker import (
    CostTracker,
    cost_task_scope,
    current_cost_task_id,
)
from loom_core.pipeline import PipelineOrchestrator
from loom_core.quality_feedback import QualityFeedback, record_quality
from loom_core.run_store import RunStore

PROJECT = Path(__file__).resolve().parent.parent

# ─── 射程自证 ────────────────────────────────────────────────

def test_targets_exist():
    """前提：被测符号真的在（否则整文件会退化成 import 层就红的假门）。"""
    for name in ("collect_run_metrics", "_cost_of", "_quality_of"):
        assert hasattr(run_metrics, name), f"run_metrics.{name} 不在了——判据要改写法"
    assert callable(cost_task_scope) and callable(current_cost_task_id)


# ─── 1. 质量总分：写入 + 读最新 + 0 是合法分 ─────────────────

@pytest.fixture
def feedback(tmp_path):
    return QualityFeedback(db_path=str(tmp_path / "quality.db"))


def test_overall_roundtrip_latest_wins(feedback):
    feedback.record_overall("t1", 70.0)
    feedback.record_overall("t1", 82.5)   # 质量重做后的第二次评分
    assert feedback.overall_for("t1") == pytest.approx(82.5)


def test_overall_for_never_scored_is_none_not_zero(feedback):
    """从未评过分 ⇒ None。**0.0 是合法总分**，不能拿 0 当「没有」。"""
    assert feedback.overall_for("never") is None
    feedback.record_overall("zero", 0.0)
    assert feedback.overall_for("zero") == 0.0


def test_record_quality_without_overall_writes_no_runs_row(feedback):
    """只给维度分时不写总分行——总分要减 style/citation 扣分，不能从维度重算。"""
    feedback.record("t2", {"completeness": 80})
    assert feedback.overall_for("t2") is None
    assert feedback.stats()["total_records"] == 1   # 维度行照旧


def test_record_quality_with_overall_writes_both(monkeypatch, tmp_path):
    fb = QualityFeedback(db_path=str(tmp_path / "q2.db"))
    monkeypatch.setattr("loom_core.quality_feedback.get_quality_feedback", lambda: fb)
    record_quality(task_id="t3", scores={"structure": 91.0}, pipeline="docgen",
                   overall=88.5)
    assert fb.overall_for("t3") == pytest.approx(88.5)
    assert fb.stats()["total_records"] == 1


def test_cleanup_ages_out_overall_rows(feedback):
    feedback.record_overall("old", 60.0)
    # 把这一行的时间压到 100 天前，再按 90 天保留期清理
    with feedback._conn() as conn:
        conn.execute("UPDATE quality_runs SET timestamp = timestamp - ?", (100 * 86400,))
    assert feedback.cleanup(max_age_days=90) >= 1
    assert feedback.overall_for("old") is None


# ─── 2. run_metrics：两个来源都读、单侧坏不连坐、空 id 不碰库 ──
# 读侧按当前 env 的规范路径短构造 store（#120：单例会把路径冻死在出生
# 那一刻的 LOOM_STATE_DIR 上，tmp 被 pytest 清退后整条进程都读不到库），
# 所以判据钉的是「构造时传的 db_path」和「绝不碰进程级单例」。

class _Cost:
    def __init__(self, value=0.0, boom=False):
        self.value, self.boom, self.seen = value, boom, []
        self.closed = False

    def stats_by_task(self, task_id):
        self.seen.append(task_id)
        if self.boom:
            raise RuntimeError("cost.db 打不开")
        return {"task_id": task_id, "total_cost": self.value, "by_provider": {}}

    def close(self):
        self.closed = True


class _Qual:
    def __init__(self, value=None, boom=False):
        self.value, self.boom, self.seen = value, boom, []
        self.closed = False

    def overall_for(self, task_id):
        self.seen.append(task_id)
        if self.boom:
            raise RuntimeError("quality.db 打不开")
        return self.value

    def close(self):
        self.closed = True


class _FakePaths:
    """记下每次 store_path 调用，证明读侧按当前环境取规范路径。"""

    def __init__(self):
        self.seen: list[str] = []

    def store_path(self, name: str) -> str:
        self.seen.append(name)
        return f"/fake/state/{name}"


def test_collect_reads_both_sources(monkeypatch):
    cost, qual = _Cost(0.0342), _Qual(82.0)
    monkeypatch.setattr("loom_core.cost_tracker.CostTracker", lambda db_path="": cost)
    monkeypatch.setattr("loom_core.quality_feedback.QualityFeedback", lambda db_path="": qual)
    assert run_metrics.collect_run_metrics("run-1") == (pytest.approx(0.0342), 82.0)
    assert cost.seen == ["run-1"] and qual.seen == ["run-1"]
    assert cost.closed and qual.closed   # 短构造读完即关，不留连接到进程末


def test_collect_isolates_a_broken_source(monkeypatch):
    """成本库坏了不该把质量分也吞掉——两个 try 是分开的。"""
    monkeypatch.setattr("loom_core.cost_tracker.CostTracker", lambda db_path="": _Cost(boom=True))
    monkeypatch.setattr("loom_core.quality_feedback.QualityFeedback", lambda db_path="": _Qual(77.0))
    assert run_metrics.collect_run_metrics("run-2") == (0.0, 77.0)


def test_collect_empty_task_id_never_touches_stores(monkeypatch):
    built: list[str] = []

    def _cost_cls(db_path=""):
        built.append("cost")
        return _Cost(1.0)

    def _qual_cls(db_path=""):
        built.append("quality")
        return _Qual(90.0)

    monkeypatch.setattr("loom_core.cost_tracker.CostTracker", _cost_cls)
    monkeypatch.setattr("loom_core.quality_feedback.QualityFeedback", _qual_cls)
    assert run_metrics.collect_run_metrics("") == (0.0, 0.0)
    assert built == []   # 空 id 短路在构造之前，连库都不开


def test_collect_uses_float_for_sqlite_text_safety(monkeypatch):
    """`total_cost` 若被上游写成 int/字符串，盖章前必须归一成 float。"""
    monkeypatch.setattr("loom_core.cost_tracker.CostTracker", lambda db_path="": _Cost("0.5"))
    monkeypatch.setattr("loom_core.quality_feedback.QualityFeedback", lambda db_path="": _Qual(None))
    cost, quality = run_metrics.collect_run_metrics("run-3")
    assert isinstance(cost, float) and cost == pytest.approx(0.5)
    assert quality == 0.0


def test_collect_resolves_the_canonical_path_at_call_time(monkeypatch):
    """db_path 必须来自**调用时刻**的 state_paths.store_path，而不是任何
    出生时刻冻住的路径——#120 的炸法就是后者。"""
    paths = _FakePaths()
    monkeypatch.setattr(run_metrics, "state_paths", paths)
    seen_db: list[str] = []

    def _cost_cls(db_path=""):
        seen_db.append(db_path)
        return _Cost(1.5)

    monkeypatch.setattr("loom_core.cost_tracker.CostTracker", _cost_cls)
    monkeypatch.setattr("loom_core.quality_feedback.QualityFeedback", lambda db_path="": _Qual(60.0))
    cost, quality = run_metrics.collect_run_metrics("run-4")
    assert paths.seen == ["cost.db", "quality.db"]
    assert seen_db == ["/fake/state/cost.db"]
    assert cost == pytest.approx(1.5) and quality == pytest.approx(60.0)


def test_collect_never_touches_the_process_global_singletons(monkeypatch, tmp_path):
    """#120 回归门：单例换成**读数故意错误**的毒丸（旧码走单例 ⇒ 拿到 999.0；
    不能换成「一调就炸」——collect 的 per-source try/except 会把异常吞成 warning，
    判据就失明了）。环境变量指到真实 tmp 目录：新码 scoped 读真库 ⇒ (0.0, 0.0)。"""
    import loom_core.cost_tracker as ct
    import loom_core.quality_feedback as qf

    called: list[str] = []

    class _PoisonCost:
        def stats_by_task(self, task_id):
            return {"task_id": task_id, "total_cost": 999.0, "by_provider": {}}

    class _PoisonQual:
        def overall_for(self, task_id):
            return 999.0

    def _poison_cost():
        called.append("cost")
        return _PoisonCost()

    def _poison_qual():
        called.append("quality")
        return _PoisonQual()

    monkeypatch.setattr(ct, "get_cost_tracker", _poison_cost)
    monkeypatch.setattr(qf, "get_quality_feedback", _poison_qual)
    monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path))
    assert run_metrics.collect_run_metrics("run-5") == (0.0, 0.0)
    assert called == []   # 毒丸一被取用就是红：读侧碰了进程级单例


# ─── 3. 归属传播：ContextVar 作用域 + router 真的带上 task_id ──

def test_cost_scope_sets_and_restores():
    assert current_cost_task_id() == ""
    with cost_task_scope("task-a"):
        assert current_cost_task_id() == "task-a"
        with cost_task_scope("task-b"):
            assert current_cost_task_id() == "task-b"
        assert current_cost_task_id() == "task-a"   # 嵌套按栈恢复
    assert current_cost_task_id() == ""


def test_cost_scope_restores_on_exception():
    with pytest.raises(RuntimeError), cost_task_scope("task-c"):
        raise RuntimeError("节点炸了")
    assert current_cost_task_id() == ""


def test_router_records_task_id_from_scope(monkeypatch):
    """`llm_router` 的两条记录路径都要带 task_id（这里走估算分支）。"""
    from loom_core.llm_router import LLMProvider, LLMRouter

    recorded: dict = {}

    class _Tracker:
        def ensure_budget(self):
            return None

        def record(self, **kw):
            recorded.update(kw)

        def record_call(self, **kw):
            recorded.update(kw)

    router = LLMRouter([LLMProvider(name="p0", api_url="http://h0/v1",
                                    api_key="sk-0", model="m0", priority=0)])
    monkeypatch.setattr("loom_core.cost_tracker.get_cost_tracker", lambda: _Tracker())
    monkeypatch.setattr("loom_core.llm_router._call_llm", lambda *a, **k: "ok")

    with cost_task_scope("run-42"):
        router.chat([{"role": "user", "content": "hi"}])
    assert recorded.get("task_id") == "run-42"

    recorded.clear()
    router.chat([{"role": "user", "content": "hi"}])
    assert recorded.get("task_id") == ""


def test_executor_wrapper_enters_scope(monkeypatch):
    """作用域必须设在 worker 线程入口——否则线程里读到空串。"""
    from loom_core.dag_executor import DAGExecutor

    ex = DAGExecutor.__new__(DAGExecutor)
    seen: dict = {}

    def _body(task, node, input_file, plan):
        seen["inside"] = current_cost_task_id()
        return {"ok": True}

    ex._execute_node_from_scheduler = _body   # type: ignore[method-assign]

    class _Task:
        id = "task-77"

    assert ex.execute_node_from_scheduler(_Task(), object(), "", None) == {"ok": True}
    assert seen["inside"] == "task-77"
    assert current_cost_task_id() == ""       # 出了节点作用域就恢复


# ─── 4. 盖章：两条收尾路径都把值交给 close_run ────────────────

class _Store:
    def __init__(self):
        self.closed: dict = {}

    def get_run(self, run_id):
        return None

    def nodes(self, run_id):
        return []

    def open_run(self, run_id, **kw):
        return None

    def record_nodes(self, run_id, rows):
        return None

    def close_run(self, run_id, status, **kw):
        self.closed.update(run_id=run_id, status=status, **kw)
        return 7

    def record(self, run_id, type, payload=None, ts=None):
        return 8


def _task(task_id="run-close"):
    class _Status:
        value = "done"

    class _Dag:
        result = {"output": "x"}

    class _Task:
        id = task_id
        status = _Status()
        finished_at = 0.0
        error = ""
        dag_nodes = {"n1": _Dag()}

    return _Task()


def test_runstore_close_stamps_collected_metrics(monkeypatch):
    store = _Store()
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch._runstore = lambda: store          # type: ignore[method-assign]
    monkeypatch.setattr("loom_core.run_metrics.collect_run_metrics",
                        lambda tid: (0.25, 82.0))

    assert orch._runstore_close(_task(), None) is True
    assert store.closed["cost_usd"] == pytest.approx(0.25)
    assert store.closed["quality_score"] == pytest.approx(82.0)
    assert store.closed["run_id"] == "run-close"


def test_backfill_close_stamps_collected_metrics(monkeypatch):
    """补账路径同样要盖章——否则「补过的运行」与「活路径收尾」不是一个口径。"""
    from loom_core.backfill import RunBackfill

    store = _Store()
    ledger = {"status": "done", "pipeline": "bf", "finished_at": 100.0,
              "nodes": [{"node": "n1", "artifact_keys": []}]}
    queue_row = {"task_id": "run-bf", "status": "done", "projection": "incomplete",
                 "finished_at": 100.0, "error": ""}

    class _Queue:
        def get(self, run_id):
            return queue_row

        def set_projection(self, run_id, marker):
            return True

    class _Ledger:
        def get(self, run_id):
            return ledger

    monkeypatch.setattr("loom_core.run_metrics.collect_run_metrics",
                        lambda tid: (0.000123, 91.0))
    bf = RunBackfill(_Queue(), _Ledger(), lambda: store)
    result = bf.replay("run-bf")
    assert result["action"] == "replayed", result
    assert store.closed["cost_usd"] == pytest.approx(0.000123)
    assert store.closed["quality_score"] == pytest.approx(91.0)


def test_close_run_persists_both_columns(tmp_path, monkeypatch):
    """落库层：两个 kwarg 真的进了 `runs` 两列，读回来不是 0。"""
    monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "state"))
    store = RunStore()
    store.open_run("run-persist", workflow="plain")
    assert store.close_run("run-persist", "done",
                           cost_usd=0.0123, quality_score=82.0) is not None
    row = store.get_run("run-persist")
    assert row is not None
    assert float(row["cost_usd"]) == pytest.approx(0.0123)
    assert float(row["quality_score"]) == pytest.approx(82.0)


def test_cost_tracker_task_scoped_sum_is_the_source(tmp_path):
    """端到端最小闭环：写两条 cost_log（一条本 task、一条别的 task）再按 task 汇总。"""
    tracker = CostTracker(db_path=str(tmp_path / "cost.db"))
    tracker.record(provider="p", prompt_tokens=1000, completion_tokens=0,
                   cost=0.01, task_id="run-x")
    tracker.record(provider="p", prompt_tokens=1000, completion_tokens=0,
                   cost=0.02, task_id="run-other")
    assert tracker.stats_by_task("run-x")["total_cost"] == pytest.approx(0.01)


# ─── 5. 上游写侧：评分方必须把总分交给反馈闭环 ────────────────

def test_quality_gate_passes_overall_to_feedback():
    """`record_quality(overall=...)` 是总分进 `quality_runs` 的唯一入口。

    少传 `overall` ⇒ `quality_runs` 永远没有行 ⇒ `runs.quality_score` 又回到恒 0。
    这是**结构判据**（评分方在 packs 里，起一个真 gate 成本过高），所以钉的是
    调用形态：同一个调用里必须同时出现 `task_id=task_id` 与 `overall=round(overall`。
    """
    src = (PROJECT / "packs" / "docgen" / "agents" / "quality_gate.py").read_text(
        encoding="utf-8")
    calls = re.findall(r"record_quality\((?:[^()]|\([^()]*\))*\)", src, re.S)
    assert len(calls) == 1, f"record_quality 调用点数量变了（{len(calls)}）——判据要改写法"
    call = calls[0]
    assert re.search(r"task_id\s*=\s*task_id", call), "没把 task_id 传下去"
    assert re.search(r"overall\s*=\s*round\(overall", call), (
        "没把总分传下去 —— quality_runs 不会有行，runs.quality_score 又恒 0")
