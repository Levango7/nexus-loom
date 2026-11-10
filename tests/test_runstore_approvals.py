"""RunStore Stage 3a：审批的事实源从 `approvals.db` 换成 `runs.db` 的投影 + 事件。

本文件的判据只回答四件事：

1. **同事务**：投影行与 `approval.*` 事件一起落；事件写不下去时投影不动——
   读侧永远看不到"投影已决、审计里没有这次决策"的半条事实；
2. **单点时刻与单一形状**：台账行就是审计记录、内存历史、事件的来源（`nodes` /
   `by` / `at` / `decision` 逐字段相等）。此前调用方自己 `time.time()` 一次、
   台账再取一次，同一个决定因此有两个时刻；
3. **数据边界，不是开关**：老台账里已有的行仍读得到、仍批得掉（升级前停在闸门上
   的运行不能升级后永远批不掉），但**新事实一律不落老台账**，读路径也不许把库建出来；
4. **fail-closed**：RunStore 不可用时登记报错、`approve()` 拒绝放行，不静默换
   一面可写的墙。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from loom_core.approvals import (
    APPROVED,
    PENDING,
    REJECTED,
    ApprovalLedgerUnavailable,
    ApprovalStore,
)
from loom_core.pipeline import PipelineOrchestrator
from loom_core.run_store import RunStore
from loom_core.scheduler import Scheduler

PROJECT = Path(__file__).resolve().parent.parent

GATE_YAML = """
name: gate_3a
agents:
  - name: tldr
    version: "0.1"
    dependencies: []
    config: {max_sentences: 2}
  - name: release_gate
    approval:
      message: "确认对外发布"
    dependencies: ["tldr"]
topology:
  levels: [["tldr"], ["release_gate"]]
  edges: [["tldr", "release_gate"]]
"""

#: 老台账的形状（本批之前 `approvals.py` 自己建的那张表，列与 store 投影一一对应）。
#: 只在这里出现=只有测试还在造它，生产写口已经不存在了——这本身就是判据的一部分。
_LEGACY_DDL = """
CREATE TABLE approvals (
    request_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    pipeline      TEXT NOT NULL DEFAULT '',
    principal     TEXT NOT NULL DEFAULT '',
    nodes_json    TEXT NOT NULL DEFAULT '[]',
    messages_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    requested_at  REAL NOT NULL,
    decided_at    REAL NOT NULL DEFAULT 0,
    decided_by    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    consumed      INTEGER NOT NULL DEFAULT 0
)
"""


# ─── 夹具 ─────────────────────────────────────────

def _store(tmp_path: Path) -> RunStore:
    return RunStore(str(tmp_path / "runs.db"))


def _ledger(tmp_path: Path, store: RunStore | None = None) -> ApprovalStore:
    return ApprovalStore(run_store=store or _store(tmp_path),
                         db_path=str(tmp_path / "approvals.db"))


def _events_of(store: RunStore, run_id: str, type_: str) -> list[dict]:
    return [e for e in store.events(run_id) if e["type"] == type_]


def _legacy(tmp_path: Path) -> Path:
    """造一个"升级前"的老台账文件（本批的生产代码不会再建它）。"""
    path = tmp_path / "approvals.db"
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_DDL)
    conn.commit()
    conn.close()
    return path


def _legacy_insert(path: Path, request_id: str, run_id: str, nodes: list[str],
                   requested_at: float, status: str = PENDING,
                   principal: str = "alice", decided_at: float = 0.0,
                   consumed: int = 0, reason: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO approvals (request_id, run_id, pipeline, principal, nodes_json, "
        "messages_json, status, requested_at, decided_at, consumed, reason) "
        "VALUES (?, ?, 'gate_3a', ?, ?, '{}', ?, ?, ?, ?, ?)",
        (request_id, run_id, principal, json.dumps(nodes), status, requested_at,
         decided_at, consumed, reason))
    conn.commit()
    conn.close()


def _legacy_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM approvals").fetchall()]
    conn.close()
    return rows


def _set_state_dir(monkeypatch, tmp_path: Path) -> Path:
    d = tmp_path / "state"
    monkeypatch.setenv("LOOM_STATE_DIR", str(d))
    return d


def _write_gate_yaml(tmp_path: Path) -> Path:
    d = tmp_path / "pipe"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "gate_3a.yaml"
    f.write_text(GATE_YAML, encoding="utf-8")
    return f


def _orch(tmp_path: Path) -> PipelineOrchestrator:
    orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                checkpoint_dir=str(tmp_path / "ckpt"))
    orch.register_agents()
    return orch


def _run_to_gate(orch, tmp_path, task_id: str = "gate_3a_task"):
    plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                     pipeline_dir=str(tmp_path / "pipe")
                     ).parse_file(str(_write_gate_yaml(tmp_path)), verify_lock=False)
    inp = tmp_path / "in.md"
    inp.write_text("# 标题\n\n这是 Stage 3a 的测试输入，用于触发摘要链路。\n",
                   encoding="utf-8")
    task = orch.run_plan(plan, input_file=str(inp), task_id=task_id, wait=False)
    deadline = time.time() + 30
    while (time.time() < deadline and not task.awaiting_approval
           and task.status.value not in ("done", "failed", "cancelled")):
        time.sleep(0.1)
    return task


# ─── 1. 投影与事件同事务 ──────────────────────────

class TestProjectionAndEvents:
    def test_request_lands_projection_and_event_together(self, tmp_path):
        store = _store(tmp_path)
        ledger = _ledger(tmp_path, store)
        rid = ledger.request("r1", "gate_3a", ["release_gate"],
                             {"release_gate": "确认对外发布"}, principal="alice")

        row = store.get_approval(rid)
        assert row is not None, "投影行没落下去"
        assert (row["status"], row["principal"], row["run_id"],
                row["nodes"], row["messages"]) == (
            PENDING, "alice", "r1", ["release_gate"],
            {"release_gate": "确认对外发布"})

        events = _events_of(store, "r1", "approval.requested")
        assert len(events) == 1, f"待批事实应恰好一条事件，实得 {len(events)}"
        assert events[0]["payload"]["request_id"] == rid
        assert events[0]["payload"]["nodes"] == ["release_gate"]
        # 同一个事务里的同一个时刻：事件不许自己再取一次时间
        assert events[0]["ts"] == row["requested_at"], \
            f"事件 {events[0]['ts']} 与投影 {row['requested_at']} 分了家"

    def test_decision_lands_one_event_with_the_authoritative_row(self, tmp_path):
        store = _store(tmp_path)
        ledger = _ledger(tmp_path, store)
        rid = ledger.request("r1", "gate_3a", ["release_gate"], principal="alice")
        recorded = ledger.decide_recorded(rid, APPROVED, by="bob", note="看过了")

        assert recorded is not None
        row = store.get_approval(rid)
        assert row["status"] == APPROVED
        assert (row["decided_by"], row["note"], row["consumed"]) == ("bob", "看过了", 0)
        assert recorded["decided_at"] == row["decided_at"], "权威行必须就是台账那一行"
        assert recorded["nodes"] == ["release_gate"], "决定形状要能直接对上闸门"

        events = _events_of(store, "r1", "approval.decided")
        assert len(events) == 1
        assert events[0]["payload"]["decision"] == APPROVED
        assert events[0]["payload"]["by"] == "bob"
        assert events[0]["ts"] == row["decided_at"]

    def test_second_decision_writes_no_event_and_keeps_the_first(self, tmp_path):
        store = _store(tmp_path)
        ledger = _ledger(tmp_path, store)
        rid = ledger.request("r1", "gate_3a", ["release_gate"])
        assert ledger.decide(rid, APPROVED, by="bob") is True
        assert ledger.decide(rid, REJECTED, by="mallory", reason="改主意") is False

        row = store.get_approval(rid)
        assert row["status"] == APPROVED, "重复决策不许覆写历史"
        assert row["decided_by"] == "bob"
        assert len(_events_of(store, "r1", "approval.decided")) == 1, \
            "被拒的第二次决策不该留下一条事实"

    def test_consume_marks_projection_and_appends_event(self, tmp_path):
        store = _store(tmp_path)
        ledger = _ledger(tmp_path, store)
        rid = ledger.request("r1", "gate_3a", ["release_gate"])
        ledger.decide(rid, APPROVED, by="bob")
        assert ledger.open_decision("r1", ["release_gate"])["request_id"] == rid

        ledger.consume(rid)
        assert store.get_approval(rid)["consumed"] == 1
        assert ledger.open_decision("r1", ["release_gate"]) is None, \
            "已消费的决定不能再次越过闸门"
        assert len(_events_of(store, "r1", "approval.consumed")) == 1

    def test_append_failure_leaves_the_projection_pending(self, tmp_path, monkeypatch):
        """事件写不下去 ⇒ 投影也不动。半条事实正是"两套口径漂移"的起点。"""
        store = _store(tmp_path)
        ledger = _ledger(tmp_path, store)
        rid = ledger.request("r1", "gate_3a", ["release_gate"])

        def _boom(*args, **kwargs):
            raise RuntimeError("事件写不进去")

        monkeypatch.setattr(RunStore, "_append", _boom)
        with pytest.raises(RuntimeError):
            store.decide_approval(rid, APPROVED, by="bob")
        assert store.get_approval(rid)["status"] == PENDING, \
            "投影已决、事件没有：读侧与审计从此分叉"


# ─── 2. 引擎侧：台账是决定形状的唯一来源 ────────────

class TestLedgerIsAuthoritative:
    def test_gate_arms_into_the_store_projection(self, tmp_path, monkeypatch):
        state = _set_state_dir(monkeypatch, tmp_path)
        orch = _orch(tmp_path)
        try:
            task = _run_to_gate(orch, tmp_path)
            assert task.awaiting_approval, f"闸门没挂起（{task.status.value}）"
            row = orch.approvals.pending_for_run(task.id)
            assert row is not None and row["nodes"] == ["release_gate"]
            assert row["messages"] == {"release_gate": "确认对外发布"}
            store = orch._runstore()
            assert store is not None
            assert store.get_approval(row["request_id"]) is not None, \
                "待批行没落在 runs.db 的投影里"
            assert len(_events_of(store, task.id, "approval.requested")) == 1
            assert (state / "runs.db").exists(), \
                "审批事实要跟着 LOOM_STATE_DIR 走，不许散到别处"
        finally:
            orch.shutdown()

    def test_audit_event_and_memory_all_read_the_ledger_row(self, tmp_path,
                                                            monkeypatch):
        """nodes / by / at / decision 四个字段在四面墙上必须是同一个值。

        审计用替身接住（`write_audit` 是函数内 import，换模块属性即可），
        其余三面读真实产物。
        """
        state = _set_state_dir(monkeypatch, tmp_path)
        audits: list[tuple[str, dict]] = []
        from loom_core import audit as audit_mod

        def _spy(channel, payload):
            audits.append((channel, dict(payload)))
            return ""

        monkeypatch.setattr(audit_mod, "write_audit", _spy)
        orch = _orch(tmp_path)
        try:
            task = _run_to_gate(orch, tmp_path)
            pending = orch.approvals.pending_for_run(task.id)
            assert pending is not None, "前提：闸门已在 store 里登记待批"
            rid = str(pending["request_id"])
            ok, err = orch.approve(task.id, note="放行", by="tester")
            assert ok, err

            row = orch.approvals.get(rid)
            assert row is not None and row["status"] == APPROVED
            memory = task.approvals[-1]
            event = _events_of(orch._runstore(), task.id, "approval.decided")[0]
            audited = [p for c, p in audits if c == "approvals"][-1]

            assert memory["at"] == row["decided_at"] == event["ts"], \
                f"同一个决定有三个时刻：{memory} / {row} / {event}"
            for field, key in (("nodes", "nodes"), ("by", "decided_by"),
                               ("decision", "status"), ("note", "note")):
                assert memory[field] == row[key] == event["payload"][field], \
                    (field, memory, row, event)
            assert audited["at"] == row["decided_at"], "审计用的不是台账那一行"
            assert audited["by"] == "tester"
            # 决定离开待批面：收件箱不再有这一条
            assert orch.approvals.pending_for_run(task.id) is None
            assert (state / "approvals.db").exists() is False, \
                "引擎不许退回老台账写"
        finally:
            orch.shutdown()


# ─── 3. 老台账：数据边界，不是开关 ──────────────────

class TestLegacyBoundary:
    def test_legacy_pending_row_is_still_readable_and_approvable(self, tmp_path):
        """升级前挂在闸门上的运行必须还能批掉，否则升级把活人质扣下了。"""
        legacy = _legacy(tmp_path)
        _legacy_insert(legacy, "old-1", "old_run", ["release_gate"], 100.0)
        ledger = _ledger(tmp_path)

        row = ledger.pending_for_run("old_run")
        assert row is not None and row["request_id"] == "old-1"
        assert ledger.decide("old-1", APPROVED, by="bob") is True
        assert ledger.get("old-1")["status"] == APPROVED

        store = _store(tmp_path)
        assert store.get_approval("old-1") is None, "老行不许被抄进投影"
        assert _events_of(store, "old_run", "approval.decided") == [], \
            "老运行不进事件流（不 backfill，也不就地造伪事实）"

    def test_legacy_decided_row_can_still_be_consumed(self, tmp_path):
        """升级前已批准、还没跑过闸门的运行：续跑时决定要被消费掉，不能二次越闸。

        这条是"老台账只剩历史"那句的另一面——**已存在**的决定仍然活着，
        连它的消费标记也只能落在老台账上（新投影里根本没有这一行）。
        """
        legacy = _legacy(tmp_path)
        _legacy_insert(legacy, "old-1", "old_run", ["release_gate"], 100.0,
                       status=APPROVED, decided_at=200.0)
        ledger = _ledger(tmp_path)

        found = ledger.open_decision("old_run", ["release_gate"])
        assert found is not None and found["request_id"] == "old-1"
        ledger.consume("old-1")
        assert ledger.open_decision("old_run", ["release_gate"]) is None, \
            "消费没落到老台账：同一个决定可以反复越闸"
        assert _legacy_rows(legacy)[0]["consumed"] == 1

    def test_new_request_never_touches_the_legacy_file(self, tmp_path):
        legacy = tmp_path / "approvals.db"
        ledger = ApprovalStore(run_store=_store(tmp_path), db_path=str(legacy))
        ledger.request("r1", "gate_3a", ["release_gate"], principal="alice")

        assert not legacy.exists(), \
            "读路径/写路径都不许把老台账建出来——老库只剩历史，不再是写入面"
        assert _legacy_rows(legacy) == []

    def test_legacy_file_without_table_is_read_as_empty(self, tmp_path):
        """升级中途的半成品状态目录：文件在、表没建 ⇒ 按没有处理，不炸读侧。"""
        (tmp_path / "approvals.db").write_bytes(b"")
        sqlite3.connect(tmp_path / "approvals.db").close()
        ledger = _ledger(tmp_path)
        assert ledger.list_pending() == []
        assert ledger.pending_for_run("anything") is None

    def test_inbox_merges_both_sources_oldest_first(self, tmp_path):
        legacy = _legacy(tmp_path)
        _legacy_insert(legacy, "old-1", "old_run", ["g"], 100.0, principal="alice")
        _legacy_insert(legacy, "old-2", "old_run2", ["g"], 300.0, principal="bob")
        store = _store(tmp_path)
        ledger = ApprovalStore(run_store=store, db_path=str(legacy))
        new_id = ledger.request("new_run", "gate_3a", ["g"], principal="alice")

        all_rows = ledger.list_pending()
        assert [r["request_id"] for r in all_rows] == ["old-1", "old-2", new_id], \
            "两个来源要按同一个时间键并成一条队列"
        assert [r["request_id"] for r in ledger.list_pending("alice")] == \
            ["old-1", new_id], "principal 过滤对两个来源都得生效"
        assert len(ledger.list_pending(limit=2)) == 2

    def test_open_decision_merges_sources_and_keeps_node_binding(self, tmp_path):
        """跨来源并清单之后，"决定只结算同一节点集合的闸门"这条不能退化。"""
        legacy = _legacy(tmp_path)
        _legacy_insert(legacy, "old-1", "r1", ["gate_a"], 100.0, status=APPROVED,
                       decided_at=200.0)
        store = _store(tmp_path)
        ledger = ApprovalStore(run_store=store, db_path=str(legacy))
        new_id = ledger.request("r1", "gate_3a", ["gate_b"])
        assert ledger.decide(new_id, APPROVED, by="bob") is True

        # 新决定更晚，取 gate_b 要拿到 store 那条
        assert ledger.open_decision("r1", ["gate_b"])["request_id"] == new_id
        # 取 gate_a 只能拿老台账那条——不许把 gate_b 的决定结算给 gate_a
        assert ledger.open_decision("r1", ["gate_a"])["request_id"] == "old-1"
        assert ledger.open_decision("r1", ["gate_c"]) is None


# ─── 4. 写侧不可用：fail-closed ────────────────────

class TestLedgerUnavailable:
    def test_write_raises_when_there_is_no_store(self, tmp_path):
        legacy = tmp_path / "approvals.db"
        ledger = ApprovalStore(run_store=lambda: None, db_path=str(legacy))
        with pytest.raises(ApprovalLedgerUnavailable):
            ledger.request("r1", "gate_3a", ["g"])
        with pytest.raises(ApprovalLedgerUnavailable):
            ledger.decide("r1", APPROVED)
        assert not legacy.exists(), "不可用时要报错，不许静默落回老台账"

    def test_reads_still_work_without_a_store(self, tmp_path):
        legacy = _legacy(tmp_path)
        _legacy_insert(legacy, "old-1", "old_run", ["g"], 100.0)
        ledger = ApprovalStore(run_store=lambda: None, db_path=str(legacy))
        assert [r["request_id"] for r in ledger.list_pending()] == ["old-1"]

    def test_approve_is_refused_when_the_store_is_unavailable(self, tmp_path,
                                                              monkeypatch):
        """真闸门 + 库打不开：批准必须被拒，任务留在 PAUSED，不造无授权的放行。"""
        state = _set_state_dir(monkeypatch, tmp_path)
        orch = _orch(tmp_path)
        monkeypatch.setattr(orch, "_runstore", lambda: None)
        try:
            task = _run_to_gate(orch, tmp_path)
            assert task.awaiting_approval, "闸门照旧挂起（内存路径不依赖投影）"
            ok, err = orch.approve(task.id, note="批", by="tester")
            assert ok is False and "台账" in err, (ok, err)
            assert task.status.name == "PAUSED", "被拒的 approve 不许把任务放出去"
            assert task.approvals == [], "被拒的决定不许进内存历史"
            assert orch.run_store_health()["status"] == "degraded"
            assert not (state / "approvals.db").exists()
        finally:
            orch.shutdown()


# ─── 5. 收尾结清待批（close_run 同事务） ──────────────────

class TestCloseRetiresPendingApprovals:
    """close_run 盖章时同事务结清该运行的待批（实证 A6：cancel 会结，
    close/backfill 不会）——收件箱不许给已收尾的运行留点不掉的待办。"""

    def test_close_retires_pending_with_system_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.open_approval("r1", "req-1", pipeline="p", nodes=["n1"],
                            principal="u")
        store.close_run("r1", "done", finished_at=100.0)
        row = store.get_approval("req-1")
        assert row["status"] == REJECTED
        assert row["decided_by"] == "system"
        assert "收尾" in row["reason"]
        assert row["decided_at"] == 100.0
        types = [e["type"] for e in store.events("r1")]
        assert types.count("approval.decided") == 1
        assert types.count("approval.consumed") == 1
        assert types.index("approval.decided") < types.index("approval.consumed")
        assert types.index("approval.consumed") < types.index("run.completed")
        # 结清必须同事务置消费位：否则同 run_id 的新执行会经 open_decision
        # 吃到这条系统驳回，闸门永久武装不上（实证于 test_approval_gate）。
        assert row["consumed"] == 1
        assert store.open_approvals("r1") == []

    def test_close_never_touches_other_runs(self, tmp_path):
        store = _store(tmp_path)
        store.open_approval("r1", "req-1", pipeline="p")
        store.open_approval("r2", "req-2", pipeline="p")
        store.close_run("r1", "done", finished_at=100.0)
        assert store.get_approval("req-2")["status"] == PENDING

    def test_cancel_then_close_decides_once(self, tmp_path):
        """cancel 已结的行，close_run 的幂等守卫不让事件二发、reason 不被覆盖。"""
        store = _store(tmp_path)
        store.open_approval("r1", "req-1", pipeline="p")
        store.decide_approval("req-1", REJECTED, by="system",
                              reason="运行已取消")
        store.close_run("r1", "cancelled", finished_at=100.0)
        types = [e["type"] for e in store.events("r1")]
        assert types.count("approval.decided") == 1
        assert store.get_approval("req-1")["reason"] == "运行已取消"

    def test_cancel_retire_also_consumes(self, tmp_path):
        """cancel 路径同语义：_retire_pending_approvals 结清后同样置消费位。

        不置的后果与 close_run 结清相同——同 task_id 的重跑会经 open_decision
        把这条系统驳回当成闸门裁决，任务直接 cancelled 而非重新武装闸门。
        """
        orch = _orch(tmp_path)
        task = _run_to_gate(orch, tmp_path)
        assert task.awaiting_approval
        assert orch.cancel(task.id)
        store = orch._runstore()
        rows = store.approvals_for_run(task.id)
        assert len(rows) == 1
        row = rows[0]
        assert (row["status"], row["decided_by"], row["reason"],
                row["consumed"]) == (REJECTED, "system", "运行已取消", 1)
        types = [e["type"] for e in store.events(task.id)]
        assert types.count("approval.decided") == 1
        assert types.count("approval.consumed") == 1
        assert store.open_approvals(task.id) == []
