from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom_core.pipeline import PipelineOrchestrator, PipelineTask, TaskStatus


def _mk_task(tid="t1", status=TaskStatus.RUNNING):
    t = PipelineTask(id=tid, pipeline_name="p", input_file="in.md", config={})
    t.status = status
    t.started_at = 1000.0
    t.finished_at = 1010.0
    return t


@pytest.fixture()
def orch(tmp_path):
    o = PipelineOrchestrator(checkpoint_dir=str(tmp_path / "ck"))
    o._logger = MagicMock()
    o.task_queue = MagicMock()
    o.approvals = MagicMock()
    return o


def _levels(o):
    return [(c.args[0], c.args[1]) for c in o._logger.log.call_args_list
            if len(c.args) >= 2]


def _plan(name="demo"):
    return SimpleNamespace(pipeline_name=name, plan_id="plan-" + name,
                           levels=[], node_count=0, raw={"pipeline": {}},
                           checkpoint={})


class TestRunStoreDegrade:
    def test_build_failure_marks_degraded(self, orch, monkeypatch):
        import loom_core.run_store as rs

        def boom():
            raise RuntimeError("no db file")

        monkeypatch.setattr(rs, "RunStore", boom)
        orch._run_store = None
        orch._run_store_failed = False
        orch._run_store_error = ""
        assert orch._runstore() is None
        assert orch._run_store_failed is True
        assert "no db file" in orch._run_store_error
        errs = [m for lv, m in _levels(orch) if lv == "error"]
        assert errs, "runstore build failure must be loud"

    def test_build_failure_logs_only_once(self, orch, monkeypatch):
        import loom_core.run_store as rs

        def boom():
            raise RuntimeError("still broken")

        monkeypatch.setattr(rs, "RunStore", boom)
        orch._run_store = None
        orch._run_store_failed = False
        orch._run_store_error = ""
        assert orch._runstore() is None
        assert orch._runstore() is None
        errs = [m for lv, m in _levels(orch) if lv == "error"]
        assert len(errs) == 1

    def test_build_success_is_cached(self, orch, monkeypatch):
        import loom_core.run_store as rs

        calls = []

        def build():
            calls.append(1)
            return SimpleNamespace(dialect="sqlite", schema_version=lambda: 3)

        monkeypatch.setattr(rs, "RunStore", build)
        orch._run_store = None
        orch._run_store_failed = False
        a = orch._runstore()
        b = orch._runstore()
        assert a is b
        assert len(calls) == 1

    def test_health_degraded_reports_reason(self, orch, monkeypatch):
        import loom_core.run_store as rs

        def boom():
            raise RuntimeError("db locked")

        monkeypatch.setattr(rs, "RunStore", boom)
        orch._run_store = None
        orch._run_store_failed = False
        orch._run_store_error = ""
        orch.task_queue.count_projection.return_value = 2
        health = orch.run_store_health()
        assert health["status"] == "degraded"
        assert health["available"] is False
        assert "db locked" in health["error"]
        assert health["incomplete_runs"] == 2

    def test_health_count_failure_does_not_break(self, orch):
        orch.task_queue.count_projection.side_effect = RuntimeError("queue gone")
        orch._run_store_failed = False
        orch._run_store = None
        health = orch.run_store_health()
        assert health["incomplete_runs"] is None
        assert any(lv == "warning" for lv, _ in _levels(orch))


class TestTerminalEventOnce:
    def test_first_claim_wins_second_refused(self, orch):
        task = _mk_task()
        assert orch._claim_terminal_event(task, "task.completed") is True
        assert task.terminal_event == "task.completed"
        assert orch._claim_terminal_event(task, "task.failed") is False
        assert task.terminal_event == "task.completed"

    def test_empty_name_never_claims(self, orch):
        task = _mk_task()
        assert orch._claim_terminal_event(task, "") is False
        assert task.terminal_event == ""

    def test_name_mapping(self, orch):
        assert orch._terminal_event_name(TaskStatus.DONE) == "task.completed"
        assert orch._terminal_event_name(TaskStatus.FAILED) == "task.failed"
        assert orch._terminal_event_name(TaskStatus.CANCELLED) == "task.cancelled"
        assert orch._terminal_event_name(TaskStatus.RUNNING) == ""

    def test_failed_payload_carries_error(self, orch, monkeypatch):
        task = _mk_task(status=TaskStatus.FAILED)
        task.error = "boom"
        plan = _plan()
        seen = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: seen.append((n, p)))
        orch._emit_plan_terminal(task, plan)
        assert seen[0][0] == "task.failed"
        assert seen[0][1]["error"] == "boom"
        assert seen[0][1]["duration"] == 10.0

    def test_cancelled_payload_has_no_duration(self, orch, monkeypatch):
        task = _mk_task(status=TaskStatus.CANCELLED)
        seen = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: seen.append((n, p)))
        orch._emit_plan_terminal(task, _plan())
        assert seen[0][0] == "task.cancelled"
        assert "duration" not in seen[0][1]
        assert "error" not in seen[0][1]

    def test_emit_is_at_most_once(self, orch, monkeypatch):
        task = _mk_task(status=TaskStatus.DONE)
        seen = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: seen.append(n))
        orch._emit_plan_terminal(task, _plan())
        orch._emit_plan_terminal(task, _plan())
        assert seen == ["task.completed"]


class TestRecordPlanTerminalState:
    def test_finish_failure_is_logged_not_raised(self, orch):
        orch.task_queue.finish.side_effect = RuntimeError("queue dead")
        orch._runstore_close = MagicMock(return_value=False)
        orch._record_plan_terminal_state(_mk_task(status=TaskStatus.DONE), _plan())
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_projection_upgrade_failure_keeps_incomplete(self, orch):
        orch._runstore_close = MagicMock(return_value=True)
        orch.task_queue.set_projection.side_effect = RuntimeError("no row")
        orch._record_plan_terminal_state(_mk_task(status=TaskStatus.DONE), _plan())
        orch.task_queue.finish.assert_called_once()
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_projection_upgraded_to_ok(self, orch):
        orch._runstore_close = MagicMock(return_value=True)
        orch._record_plan_terminal_state(_mk_task(status=TaskStatus.DONE), _plan())
        orch.task_queue.set_projection.assert_called_once()
        args = orch.task_queue.set_projection.call_args.args
        assert args[1] == "ok"


class TestRunStoreWritesDegrade:
    def test_open_skipped_when_store_missing(self, orch):
        store = MagicMock()
        orch._runstore = MagicMock(return_value=None)
        orch._runstore_open(_mk_task(), _plan(), "in.md", "direct")
        assert not store.open_run.called

    def test_open_failure_logged(self, orch):
        store = MagicMock()
        store.open_run.side_effect = RuntimeError("insert refused")
        orch._runstore = MagicMock(return_value=store)
        orch._runstore_open(_mk_task(), _plan(), "in.md", "direct")
        assert any(lv == "warning" for lv, _ in _levels(orch))
        assert "insert refused" in str(store.open_run.side_effect)

    def test_nodes_skip_missing_dag(self, orch):
        store = MagicMock()
        orch._runstore = MagicMock(return_value=store)
        task = _mk_task()
        task.dag_nodes = {}
        orch._runstore_nodes(task, [SimpleNamespace(agent_name="ghost")])
        rows = store.record_nodes.call_args.args[1]
        assert rows == []

    def test_nodes_record_failure_logged(self, orch):
        store = MagicMock()
        store.record_nodes.side_effect = RuntimeError("poison row")
        orch._runstore = MagicMock(return_value=store)
        task = _mk_task()
        task.dag_nodes = {}
        orch._runstore_nodes(task, [SimpleNamespace(agent_name="w")])
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_close_returns_false_when_missing_store(self, orch):
        orch._runstore = MagicMock(return_value=None)
        assert orch._runstore_close(_mk_task(), _plan()) is False

    def test_close_zero_rows_is_not_success(self, orch):
        store = MagicMock()
        store.close_run.return_value = None
        orch._runstore = MagicMock(return_value=store)
        assert orch._runstore_close(_mk_task(), _plan()) is False

    def test_close_failure_logged(self, orch):
        store = MagicMock()
        store.close_run.side_effect = RuntimeError("locked")
        orch._runstore = MagicMock(return_value=store)
        assert orch._runstore_close(_mk_task(), _plan()) is False
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_close_success_collects_artifact_keys(self, orch):
        store = MagicMock()
        store.close_run.return_value = 1
        orch._runstore = MagicMock(return_value=store)
        task = _mk_task()
        task.dag_nodes = {"w": SimpleNamespace(result={"b.md": "1", "a.md": "2"})}
        assert orch._runstore_close(task, _plan()) is True
        arts = store.close_run.call_args.kwargs["artifacts"]
        assert arts == ["a.md", "b.md"]


class TestApprovalLedgerFailsClosed:
    def test_record_maps_fields(self):
        rec = {"nodes": ["g1"], "status": "approved", "note": "n",
               "reason": "r", "decided_at": 12.5, "decided_by": "alice"}
        d = PipelineOrchestrator._decision_from_record(rec)
        assert d == {"nodes": ["g1"], "decision": "approved", "note": "n",
                     "reason": "r", "at": 12.5, "by": "alice"}

    def test_record_tolerates_missing_fields(self):
        d = PipelineOrchestrator._decision_from_record({})
        assert d["nodes"] == [] and d["decision"] == "" and d["at"] == 0.0

    def test_ledger_write_failure_returns_none(self, orch):
        orch.approvals.decide_recorded.side_effect = RuntimeError("ledger down")
        assert orch._decide_in_ledger("r1", "approved", "", "", "bob") is None
        assert any(lv == "error" for lv, _ in _levels(orch))

    def test_decide_in_store_without_pending_row(self, orch):
        orch.approvals.pending_for_run.return_value = None
        assert orch._decide_in_store("t1", "approved", "", "", "bob") is None

    def test_pending_row_swallows_ledger_error(self, orch):
        orch.approvals.pending_for_run.side_effect = RuntimeError("db gone")
        assert orch._pending_row("t1") is None

    def test_pending_row_rejects_non_dict(self, orch):
        orch.approvals.pending_for_run.return_value = "not-a-row"
        assert orch._pending_row("t1") is None

    def test_queue_row_swallows_error(self, orch):
        orch.task_queue.get.side_effect = RuntimeError("queue gone")
        assert orch._queue_row("t1") is None


class TestPauseCancelResume:
    def test_pause_unknown_returns_false(self, orch):
        assert orch.pause("nope") is False

    def test_pause_running_flips_to_paused(self, orch):
        task = _mk_task()
        orch._running_tasks = {"t1": task}
        assert orch.pause("t1") is True
        assert task.status == TaskStatus.PAUSED

    def test_pause_finished_is_refused(self, orch):
        task = _mk_task(status=TaskStatus.DONE)
        orch._running_tasks = {"t1": task}
        assert orch.pause("t1") is False
        assert task.status == TaskStatus.DONE

    def test_cancel_unknown_returns_false(self, orch):
        assert orch.cancel("nope") is False

    def test_cancel_sets_stop_event_and_emits_once(self, orch, monkeypatch):
        seen = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: seen.append((n, p)))
        task = _mk_task()
        orch._running_tasks = {"t1": task}
        assert orch.cancel("t1") is True
        assert task.status == TaskStatus.CANCELLED
        assert task.stop_event.is_set()
        assert [n for n, _ in seen] == ["task.cancelled"]
        assert orch.cancel("t1") is False
        assert [n for n, _ in seen] == ["task.cancelled"]

    def test_cancel_retires_pending_approvals(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda n, p: None)
        task = _mk_task()
        orch._running_tasks = {"t1": task}
        orch.approvals.pending_for_run.return_value = {"request_id": "req-9"}
        orch.cancel("t1")
        kwargs = orch.approvals.decide.call_args.kwargs
        assert kwargs["by"] == "system"
        assert "取消" in kwargs["reason"]

    def test_retire_failure_does_not_block_cancel(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda n, p: None)
        task = _mk_task()
        orch._running_tasks = {"t1": task}
        orch.approvals.pending_for_run.side_effect = RuntimeError("ledger gone")
        assert orch.cancel("t1") is True
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_resume_refused_while_awaiting_approval(self, orch):
        task = _mk_task(status=TaskStatus.PAUSED)
        task.awaiting_approval = {"nodes": ["g"]}
        orch._running_tasks = {"t1": task}
        assert orch.resume("t1") is False
        assert task.status == TaskStatus.PAUSED

    def test_resume_paused_task(self, orch):
        task = _mk_task(status=TaskStatus.PAUSED)
        orch._running_tasks = {"t1": task}
        assert orch.resume("t1") is True
        assert task.status == TaskStatus.RUNNING

    def test_resume_unknown_returns_false(self, orch):
        assert orch.resume("nope") is False

    def test_resume_running_returns_false(self, orch):
        orch._running_tasks = {"t1": _mk_task()}
        assert orch.resume("t1") is False


def _gate(name="gate", approval=None):
    return SimpleNamespace(agent_name=name, dependencies=[], timeout=10,
                           max_retries=0, approval=approval or {"message": "ok"})


def _wnode(name="w"):
    return SimpleNamespace(
        agent_name=name, dependencies=[], timeout=10, max_retries=0,
        approval=None, agent_config=SimpleNamespace(parallelism={}))


class TestArmApprovalGate:
    def test_no_gates_returns_level_unchanged(self, orch):
        node = SimpleNamespace(agent_name="w", approval=None)
        assert orch._arm_approval_gate(_mk_task(), _plan(), [node]) == [node]

    def test_skipped_gate_not_pending(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value="cond")
        orch._runstore_nodes = MagicMock()
        task = _mk_task()
        level = orch._arm_approval_gate(task, _plan(), [_gate()])
        assert len(level) == 1
        assert task.status != TaskStatus.PAUSED
        assert task.dag_nodes["gate"].attempts == 0
        orch.approvals.request.assert_not_called()

    def test_pending_gate_pauses_and_requests_ledger(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._runstore_nodes = MagicMock()
        orch._consume_open_decision = MagicMock(return_value=None)
        orch.approvals.pending_for_run.return_value = None
        task = _mk_task()
        orch._arm_approval_gate(task, _plan(), [_gate()])
        assert task.status == TaskStatus.PAUSED
        assert task.awaiting_approval["nodes"] == ["gate"]
        orch.approvals.request.assert_called_once()

    def test_ledger_request_failure_still_waits(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._runstore_nodes = MagicMock()
        orch._consume_open_decision = MagicMock(return_value=None)
        orch.approvals.pending_for_run.return_value = None
        orch.approvals.request.side_effect = RuntimeError("ledger down")
        task = _mk_task()
        orch._arm_approval_gate(task, _plan(), [_gate()])
        assert task.status == TaskStatus.PAUSED
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_existing_pending_row_is_reused(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._runstore_nodes = MagicMock()
        orch._consume_open_decision = MagicMock(return_value=None)
        orch.approvals.pending_for_run.return_value = {
            "nodes": ["old-gate"], "messages": {"old-gate": "m"}}
        task = _mk_task()
        orch._arm_approval_gate(task, _plan(), [_gate()])
        assert task.awaiting_approval["nodes"] == ["old-gate"]
        orch.approvals.request.assert_not_called()

    def test_rejected_decision_cancels_run(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._runstore_nodes = MagicMock()
        orch._consume_open_decision = MagicMock(
            return_value={"status": "rejected", "reason": "no"})
        task = _mk_task()
        orch._arm_approval_gate(task, _plan(), [_gate()])
        assert task.status == TaskStatus.CANCELLED
        assert "no" in task.error
        assert task.stop_event.is_set()
        assert task.status != TaskStatus.PAUSED

    def test_approved_decision_does_not_pause(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._runstore_nodes = MagicMock()
        orch._consume_open_decision = MagicMock(return_value={"status": "approved"})
        task = _mk_task()
        orch._arm_approval_gate(task, _plan(), [_gate()])
        assert task.status == TaskStatus.RUNNING
        assert task.awaiting_approval is None


class TestSettleApprovalGate:
    def test_no_gates_returns_level(self, orch):
        node = SimpleNamespace(agent_name="w", approval=None)
        assert orch._settle_approval_gate(_mk_task(), _plan(), [node]) == [node]

    def test_gate_without_dag_node_is_skipped(self, orch):
        task = _mk_task()
        task.dag_nodes = {}
        out = orch._settle_approval_gate(task, _plan(), [_gate()])
        assert out == []

    def test_skip_reason_marks_condition_skip(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value="cond")
        orch._executor._skip_node_by_condition = MagicMock()
        task = _mk_task()
        task.dag_nodes = {"gate": SimpleNamespace()}
        orch._settle_approval_gate(task, _plan(), [_gate()])
        orch._executor._skip_node_by_condition.assert_called_once()

    def test_settle_passes_latest_note(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._executor.settle_approval_node = MagicMock()
        task = _mk_task()
        task.dag_nodes = {"gate": SimpleNamespace()}
        task.approvals = [{"decision": "approved", "note": "first"},
                          {"decision": "approved", "note": "latest"}]
        orch._settle_approval_gate(task, _plan(), [_gate()])
        kwargs = orch._executor.settle_approval_node.call_args.kwargs
        assert kwargs["note"] == "latest"

    def test_awaiting_cleared_after_settle(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._executor.settle_approval_node = MagicMock()
        task = _mk_task()
        task.dag_nodes = {"gate": SimpleNamespace()}
        task.awaiting_approval = {"nodes": ["gate"]}
        orch._settle_approval_gate(task, _plan(), [_gate()])
        assert task.awaiting_approval is None

    def test_consume_failure_logged_not_raised(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._executor.settle_approval_node = MagicMock()
        orch.approvals.open_decision.side_effect = RuntimeError("ledger gone")
        task = _mk_task()
        task.dag_nodes = {"gate": SimpleNamespace()}
        orch._settle_approval_gate(task, _plan(), [_gate()])
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_returns_only_non_gate_nodes(self, orch):
        orch._executor._should_skip_by_condition = MagicMock(return_value=None)
        orch._executor.settle_approval_node = MagicMock()
        task = _mk_task()
        task.dag_nodes = {"gate": SimpleNamespace()}
        worker = SimpleNamespace(agent_name="w", approval=None)
        out = orch._settle_approval_gate(task, _plan(), [_gate(), worker])
        assert out == [worker]


class TestObserveExternalDecision:
    def test_no_awaiting_is_waiting(self, orch):
        task = _mk_task()
        task.awaiting_approval = None
        assert orch._observe_external_decision(task) == "waiting"

    def test_no_decision_is_waiting(self, orch):
        orch._consume_open_decision = MagicMock(return_value=None)
        task = _mk_task()
        task.awaiting_approval = {"nodes": ["g"]}
        assert orch._observe_external_decision(task) == "waiting"

    def test_rejected_cancels_with_reason(self, orch):
        orch._consume_open_decision = MagicMock(
            return_value={"status": "rejected", "reason": "not ok"})
        task = _mk_task()
        task.awaiting_approval = {"nodes": ["g"]}
        assert orch._observe_external_decision(task) == "cancelled"
        assert task.status == TaskStatus.CANCELLED
        assert "not ok" in task.error
        assert task.stop_event.is_set()

    def test_rejected_without_reason(self, orch):
        orch._consume_open_decision = MagicMock(return_value={"status": "rejected"})
        task = _mk_task()
        task.awaiting_approval = {"nodes": ["g"]}
        assert orch._observe_external_decision(task) == "cancelled"
        assert task.error == "审批被驳回"

    def test_approved_resumes(self, orch):
        orch._consume_open_decision = MagicMock(return_value={"status": "approved"})
        task = _mk_task(status=TaskStatus.PAUSED)
        task.awaiting_approval = {"nodes": ["g"]}
        assert orch._observe_external_decision(task) == "running"
        assert task.status == TaskStatus.RUNNING
        assert task.awaiting_approval is None


def _awaiting_task(status=TaskStatus.PAUSED):
    t = _mk_task(status=status)
    t.awaiting_approval = {"nodes": ["gate"], "messages": {}, "pipeline": "p"}
    return t


class TestApproveReject:
    def test_approve_unknown_task(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._pending_row = MagicMock(return_value=None)
        orch._queue_row = MagicMock(return_value=None)
        ok, err = orch.approve("ghost")
        assert ok is False and "not found" in err

    def test_approve_unknown_but_queued_reports_status(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._pending_row = MagicMock(return_value=None)
        orch._queue_row = MagicMock(return_value={"status": "done"})
        ok, err = orch.approve("t9")
        assert ok is False and "done" in err

    def test_approve_refuses_non_awaiting_task(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._running_tasks = {"t1": _mk_task()}
        ok, err = orch.approve("t1")
        assert ok is False and "未在审批等待中" in err

    def test_approve_fails_closed_when_ledger_unavailable(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        task = _awaiting_task()
        orch._running_tasks = {"t1": task}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = None
        ok, err = orch.approve("t1")
        assert ok is False and "台账不可用" in err
        assert task.status == TaskStatus.PAUSED

    def test_approve_in_memory_wakes_task(self, orch, monkeypatch):
        audits = []
        monkeypatch.setattr("loom_core.audit.write_audit",
                            lambda topic, entry: audits.append(entry) or None)
        events = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: events.append((n, p)))
        task = _awaiting_task()
        orch._running_tasks = {"t1": task}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = {
            "nodes": ["gate"], "status": "approved", "note": "ok",
            "decided_at": 1.0, "decided_by": "bob"}
        ok, err = orch.approve("t1", note="ok", by="bob")
        assert ok is True and err == ""
        assert task.status == TaskStatus.RUNNING
        assert task.awaiting_approval is None
        assert task.approvals[-1]["decision"] == "approved"
        assert audits and events and events[0][0] == "task.approved"

    def test_approve_persistent_path_marks_resume(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._running_tasks = {}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = {
            "nodes": ["gate"], "status": "approved", "decided_at": 1.0}
        orch.task_queue.mark_for_resume.return_value = True
        ok, err = orch.approve("t1")
        assert ok is True and err == ""
        orch.task_queue.mark_for_resume.assert_called_once_with("t1")

    def test_approve_persistent_decision_conflict(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._running_tasks = {}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = None
        ok, err = orch.approve("t1")
        assert ok is False and "决定已被处理" in err

    def test_reject_in_memory_cancels(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        events = []
        monkeypatch.setattr("loom_core.pipeline.emit_event",
                            lambda n, p: events.append((n, p)))
        task = _awaiting_task()
        orch._running_tasks = {"t1": task}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = {
            "nodes": ["gate"], "status": "rejected", "reason": "bad"}
        ok, err = orch.reject("t1", reason="bad")
        assert ok is True and err == ""
        assert task.status == TaskStatus.CANCELLED
        assert "bad" in task.error
        assert task.stop_event.is_set()
        assert events[0][0] == "task.rejected"

    def test_reject_persistent_path_cancels_queue(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._running_tasks = {}
        orch._pending_row = MagicMock(return_value={"request_id": "r"})
        orch.approvals.decide_recorded.return_value = {
            "nodes": ["gate"], "status": "rejected"}
        ok, err = orch.reject("t1", reason="bad")
        assert ok is True
        orch.task_queue.cancel.assert_called_once_with("t1")

    def test_reject_non_awaiting_refused(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a, **k: None)
        monkeypatch.setattr("loom_core.pipeline.emit_event", lambda *a: None)
        orch._running_tasks = {"t1": _mk_task()}
        ok, err = orch.reject("t1")
        assert ok is False and "未在审批等待中" in err


class TestAuditApprovalDegrade:
    def test_write_error_is_logged(self, orch, monkeypatch):
        monkeypatch.setattr("loom_core.audit.write_audit", lambda *a: "disk full")
        orch._audit_approval("t1", {"nodes": ["g"], "decision": "approved"})
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_write_exception_is_logged(self, orch, monkeypatch):
        def boom(*a):
            raise RuntimeError("audit down")

        monkeypatch.setattr("loom_core.audit.write_audit", boom)
        orch._audit_approval("t1", {"nodes": ["g"], "decision": "approved"})
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_success_records_pipeline_name(self, orch, monkeypatch):
        seen = []
        monkeypatch.setattr("loom_core.audit.write_audit",
                            lambda topic, entry: seen.append(entry) or None)
        orch._running_tasks = {"t1": _mk_task()}
        orch._audit_approval("t1", {"nodes": ["g"], "decision": "approved"})
        assert seen and seen[0]["pipeline"] == "p"


class TestCheckpointDegrade:
    def test_snapshot_failure_does_not_block_others(self, orch):
        bad = MagicMock()
        bad.on_snapshot.side_effect = RuntimeError("snapshot boom")
        good = MagicMock()
        good.on_snapshot.return_value = {"k": 1}
        orch.registry.list_agent_names = MagicMock(return_value=["bad", "good"])
        orch.registry.get_instance = MagicMock(side_effect=lambda n: {"bad": bad,
                                                                       "good": good}[n])
        orch._checkpoint = MagicMock()
        orch._save_checkpoint(_mk_task())
        saved = orch._checkpoint.save.call_args.args[2]
        assert saved == {"good": {"k": 1}}
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_restore_failure_is_logged(self, orch):
        inst = MagicMock()
        inst.on_restore.side_effect = RuntimeError("restore boom")
        orch.registry.get_instance = MagicMock(return_value=inst)
        orch._checkpoint = MagicMock()
        task = _mk_task()
        orch._checkpoint.load.return_value = (task, {"a": {"k": 1}})
        assert orch._load_checkpoint("t1") is task
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_resume_without_checkpoint_logs_info(self, orch):
        orch._load_checkpoint = MagicMock(return_value=None)
        assert orch._resume_snapshots_for(_plan(), "t1") is None
        assert any(lv == "info" for lv, _ in _levels(orch))

    def test_resume_pipeline_mismatch_is_refused(self, orch):
        other = _mk_task()
        other.pipeline_name = "other"
        orch._load_checkpoint = MagicMock(return_value=other)
        assert orch._resume_snapshots_for(_plan("demo"), "t1") is None
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_resume_returns_snapshots(self, orch):
        loaded = _mk_task()
        loaded.pipeline_name = "demo"
        loaded._resumed_node_snapshots = {"a": {"content": "x"}}
        orch._load_checkpoint = MagicMock(return_value=loaded)
        snaps = orch._resume_snapshots_for(_plan("demo"), "t1")
        assert snaps == {"a": {"content": "x"}}

    def test_pause_boundary_save_failure_swallowed(self, orch):
        orch._save_checkpoint = MagicMock(side_effect=RuntimeError("disk full"))
        orch._pause_boundary_snapshot(_mk_task())
        assert any(lv == "warning" for lv, _ in _levels(orch))


class TestPooledMerge:
    def test_extend_strategy_merges_results(self, orch):
        meta = SimpleNamespace(results_merge="extend")
        orch.registry.get_meta = MagicMock(return_value=meta)
        orch._set_task_output = MagicMock()
        task = _mk_task()
        task.result = {
            "researcher_pool_0": {"results": [1], "query_count": 2,
                                   "engines_used": ["a"]},
            "researcher_pool_1": {"results": [2], "query_count": 3,
                                   "engines_used": ["b"]},
        }
        orch._get_task_output = MagicMock(side_effect=lambda t, k, d=None: t.result[k])
        orch._merge_pooled_results(task)
        merged = orch._set_task_output.call_args.args[2]
        assert merged["total"] == 2
        assert merged["query_count"] == 5
        assert set(merged["engines_used"]) == {"a", "b"}

    def test_first_non_empty_for_other_pools(self, orch):
        meta = SimpleNamespace(results_merge="")
        orch.registry.get_meta = MagicMock(return_value=meta)
        orch._set_task_output = MagicMock()
        task = _mk_task()
        task.result = {"writer_pool_0": {}, "writer_pool_1": {"x": 1}}
        orch._get_task_output = MagicMock(side_effect=lambda t, k, d=None: t.result[k])
        orch._merge_pooled_results(task)
        assert orch._set_task_output.call_args.args[2] == {"x": 1}


class TestDeliveredContract:
    def test_path_exists_counts_as_delivered(self, orch, tmp_path):
        out = tmp_path / "o.md"
        out.write_text("x", encoding="utf-8")
        task = _mk_task()
        task.output_path = str(out)
        assert orch._delivered(task) is True

    def test_inline_content_counts(self, orch):
        task = _mk_task()
        task.output_content = " hello "
        assert orch._delivered(task) is True

    def test_all_sinks_skipped_by_condition(self, orch, monkeypatch):
        meta = SimpleNamespace(writes_output=True)
        orch.registry.get_meta = MagicMock(return_value=meta)
        plan = _plan("demo")
        plan.levels = [[SimpleNamespace(agent_name="writer")]]
        task = _mk_task()
        task.dag_nodes = {"writer": SimpleNamespace(status="skipped",
                                                    skip_reason="condition")}
        assert orch._delivered(task, plan) is True
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_nothing_delivered(self, orch):
        assert orch._delivered(_mk_task()) is False

    def test_sink_declared_only_when_writes_output(self, orch):
        orch.registry.get_meta = MagicMock(return_value=SimpleNamespace(writes_output=False))
        plan = _plan("demo")
        plan.levels = [[_wnode()]]
        assert orch._sink_declared(plan) is False
        orch.registry.get_meta = MagicMock(return_value=SimpleNamespace(writes_output=True))
        assert orch._sink_declared(plan) is True


class TestConfigRefreshAndRerun:
    def test_refresh_without_config_is_noop(self, orch):
        orch.config = None
        assert orch.refresh_config_if_changed() is False

    def test_refresh_no_change(self, orch):
        orch.config = MagicMock()
        orch.config.poll_overlay.return_value = False
        assert orch.refresh_config_if_changed() is False

    def test_refresh_notifies_agents(self, orch):
        orch.config = MagicMock()
        orch.config.poll_overlay.return_value = True
        orch.config.revision = 7
        orch.notify_config_update = MagicMock(return_value=1)
        assert orch.refresh_config_if_changed() is True
        orch.notify_config_update.assert_called_once_with(["*"])

    def test_notify_counts_only_implementors(self, orch):
        impl = MagicMock()
        bare = SimpleNamespace()
        orch.registry.list = MagicMock(return_value=[{"name": "a"}, {"name": "b"},
                                                      {"name": "c"}])
        orch.registry.get_instance = MagicMock(
            side_effect=lambda n: {"a": impl, "b": bare, "c": None}[n])
        assert orch.notify_config_update() == 1

    def test_notify_swallows_agent_error(self, orch):
        bad = MagicMock()
        bad.on_config_update.side_effect = RuntimeError("boom")
        ok = MagicMock()
        orch.registry.list = MagicMock(return_value=[{"name": "bad"}, {"name": "ok"}])
        orch.registry.get_instance = MagicMock(
            side_effect=lambda n: {"bad": bad, "ok": ok}[n])
        assert orch.notify_config_update() == 1
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_rerun_without_plan_raises(self, orch):
        orch.last_plan = None
        with pytest.raises(RuntimeError):
            orch.rerun()

    def test_rerun_parses_named_pipeline(self, orch, monkeypatch):
        import loom_core.scheduler as sch
        orch.last_plan = None
        plan = _plan("named")
        monkeypatch.setattr(sch.Scheduler, "parse", lambda self, name: plan)
        orch.run_plan = MagicMock()
        orch.rerun(pipeline_name="named")
        orch.run_plan.assert_called_once()

    def test_rerun_reuses_last_plan(self, orch):
        orch.last_plan = _plan("cached")
        orch.last_input = "old.md"
        orch.run_plan = MagicMock()
        orch.rerun()
        assert orch.run_plan.call_args.args[0].pipeline_name == "cached"
        assert orch.run_plan.call_args.kwargs["input_file"] == "old.md"


class TestCleanupAndShutdown:
    def test_trim_keeps_running_drops_finished(self, orch):
        orch._max_task_history = 2
        running = _mk_task("r1")
        done1 = _mk_task("d1", TaskStatus.DONE)
        done1.finished_at = 1.0
        done2 = _mk_task("d2", TaskStatus.DONE)
        done2.finished_at = 2.0
        orch._running_tasks = {"r1": running, "d1": done1, "d2": done2}
        orch._trim_task_history()
        assert set(orch._running_tasks) == {"r1", "d2"}

    def test_trim_noop_under_limit(self, orch):
        orch._max_task_history = 10
        orch._running_tasks = {"a": _mk_task("a")}
        orch._trim_task_history()
        assert set(orch._running_tasks) == {"a"}

    def test_task_temp_cleanup_sums_counts(self, orch):
        inst = MagicMock()
        inst.cleanup_task_temp.return_value = 3
        orch.registry.list_agent_names = MagicMock(return_value=["a"])
        orch.registry.get_instance = MagicMock(return_value=inst)
        orch._cleanup_task_temp(_mk_task())
        assert any("已清理" in m for _, m in _levels(orch))

    def test_task_temp_cleanup_swallows_error(self, orch):
        inst = MagicMock()
        inst.cleanup_task_temp.side_effect = RuntimeError("busy")
        orch.registry.list_agent_names = MagicMock(return_value=["a"])
        orch.registry.get_instance = MagicMock(return_value=inst)
        orch._cleanup_task_temp(_mk_task())
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_stale_temp_cleanup_skips_missing_method(self, orch):
        inst = SimpleNamespace()
        orch.registry.list_agent_names = MagicMock(return_value=["a"])
        orch.registry.get_instance = MagicMock(return_value=inst)
        orch._cleanup_all_stale_temp()
        assert not [m for _, m in _levels(orch) if "已清理" in m]

    def test_shutdown_closes_everything(self, orch):
        orch.bus = MagicMock()
        orch.registry = MagicMock()
        orch.registry.shutdown = MagicMock()
        orch.task_queue = MagicMock()
        orch._cleanup_old_checkpoints = MagicMock()
        orch._cleanup_all_stale_temp = MagicMock()
        orch._admin_api = None
        orch.shutdown()
        assert orch._stop_event.is_set()
        orch.bus.shutdown.assert_called_once()
        orch.task_queue.close_all.assert_called_once()
        orch.registry.shutdown.assert_called_once()

    def test_stop_admin_api_without_instance(self, orch):
        orch._admin_api = None
        orch.stop_admin_api()

    def test_stop_admin_api_calls_stop(self, orch):
        api = MagicMock()
        orch._admin_api = api
        orch.stop_admin_api()
        api.stop.assert_called_once()
        assert orch._admin_api is None

    def test_repr_mentions_counts(self, orch):
        orch.registry.list = MagicMock(return_value=[1, 2])
        assert "agents=2" in repr(orch)


class TestEntryBranches:
    def test_run_with_config_uses_legacy(self, orch):
        orch._legacy_plan_from_config = MagicMock(return_value=_plan("legacy"))
        orch.run_plan = MagicMock(return_value=_mk_task())
        orch.run(pipeline_name="p", config={"x": 1}, wait=True)
        orch._legacy_plan_from_config.assert_called_once_with("p", {"x": 1})

    def test_run_yaml_failure_falls_back_to_legacy(self, orch, monkeypatch):
        import loom_core.scheduler as sch
        monkeypatch.setattr(sch.Scheduler, "parse",
                            MagicMock(side_effect=RuntimeError("no yaml")))
        orch._legacy_plan_from_config = MagicMock(return_value=_plan("p"))
        orch.run_plan = MagicMock(return_value=_mk_task())
        orch.run(pipeline_name="p", wait=True)
        orch._legacy_plan_from_config.assert_called_once_with("p", {})
        assert any(lv == "info" for lv, _ in _levels(orch))

    def test_run_blank_builds_empty_legacy_plan(self, orch):
        orch._legacy_plan_from_config = MagicMock(return_value=_plan(""))
        orch.run_plan = MagicMock(return_value=_mk_task())
        orch.run(wait=True)
        orch._legacy_plan_from_config.assert_called_once_with("", {})

    def test_run_plan_rejects_non_plan(self, orch):
        with pytest.raises(TypeError):
            orch.run_plan("not-a-plan")

    @pytest.mark.asyncio
    async def test_run_plan_async_rejects_non_plan(self, orch):
        with pytest.raises(TypeError):
            await orch.run_plan_async("not-a-plan")


class TestLegacyPlanFromConfig:
    def test_missing_dag_node_is_skipped(self, orch):
        orch._legacy_agent_order = MagicMock(return_value=["a", "ghost"])
        nodes = {"a": SimpleNamespace(rate_limit={}, timeout=5, max_retries=0,
                                       dependencies=[], backoff="linear",
                                       initial_delay=1.0)}
        orch._build_dag = MagicMock(return_value=(nodes, [["a", "ghost"]]))
        plan = orch._legacy_plan_from_config("p", {})
        names = [n.agent_name for lv in plan.levels for n in lv]
        assert names == ["a"]

    def test_retry_only_when_max_retries(self, orch):
        orch._legacy_agent_order = MagicMock(return_value=["a"])
        node = SimpleNamespace(rate_limit={}, timeout=5, max_retries=3,
                               dependencies=[], backoff="linear", initial_delay=1.0)
        orch._build_dag = MagicMock(return_value=({"a": node}, [["a"]]))
        plan = orch._legacy_plan_from_config("p", {"fail_fast": False})
        built = plan.levels[0][0]
        assert built.agent_config.retry == {"max_retries": 3}
        assert plan.fail_fast is False


class TestListRunsMerge:
    def test_memory_only_when_store_missing(self, orch):
        orch._runstore = MagicMock(return_value=None)
        task = _mk_task()
        orch._running_tasks = {"t1": task}
        rows = orch.list_runs()
        assert rows[0]["id"] == "t1"
        assert rows[0]["source"] == "memory"

    def test_store_listing_failure_is_logged(self, orch):
        store = MagicMock()
        store.list_runs.side_effect = RuntimeError("bad db")
        orch._runstore = MagicMock(return_value=store)
        orch._running_tasks = {}
        assert orch.list_runs() == []
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_principal_filter_hides_others(self, orch):
        orch._runstore = MagicMock(return_value=None)
        mine = _mk_task("m")
        mine.owner = "alice"
        other = _mk_task("o")
        other.owner = "bob"
        orch._running_tasks = {"m": mine, "o": other}
        rows = orch.list_runs(principal="alice")
        assert [r["id"] for r in rows] == ["m"]

    def test_store_rows_merge_with_memory(self, orch):
        store = MagicMock()
        store.list_runs.return_value = [
            {"run_id": "s1", "status": "done", "workflow": "p",
             "principal": "", "started_at": 5.0}]
        orch._runstore = MagicMock(return_value=store)
        mem = _mk_task("m1")
        mem.started_at = 9.0
        orch._running_tasks = {"m1": mem}
        rows = orch.list_runs()
        assert [r["id"] for r in rows] == ["m1", "s1"]
        assert rows[1]["source"] == "runstore"


class TestReplayDlq:
    def test_no_store_returns_none(self, orch):
        orch.bus = MagicMock()
        orch.bus._store = None
        assert orch.replay_dlq(1) is None

    def test_missing_entry_returns_none(self, orch):
        store = MagicMock()
        store.get_dlq_entry.return_value = None
        orch.bus = MagicMock()
        orch.bus._store = store
        assert orch.replay_dlq(1) is None

    def test_request_replay_reruns_agent(self, orch):
        store = MagicMock()
        store.get_dlq_entry.return_value = {
            "topic": "writer.input",
            "payload": {"msg_type": "request", "payload": {"task_id": "t1",
                                                           "node": "writer"}}}
        orch.bus = MagicMock()
        orch.bus._store = store
        inst = MagicMock()
        inst.handle.return_value = {"ok": True}
        orch.registry.get_instance = MagicMock(return_value=inst)
        orch._set_task_output = MagicMock()
        task = _mk_task("t1")
        orch._running_tasks = {"t1": task}
        out = orch.replay_dlq(7)
        assert out["result"] == {"ok": True}
        orch._set_task_output.assert_called_once()
        store.replay_dlq.assert_called_once_with(7)

    def test_request_replay_agent_error_is_reported(self, orch):
        store = MagicMock()
        store.get_dlq_entry.return_value = {
            "topic": "writer.input",
            "payload": {"msg_type": "request", "payload": {"task_id": "t1",
                                                           "node": "writer"}}}
        orch.bus = MagicMock()
        orch.bus._store = store
        inst = MagicMock()
        inst.handle.side_effect = RuntimeError("agent boom")
        orch.registry.get_instance = MagicMock(return_value=inst)
        out = orch.replay_dlq(7)
        assert "agent boom" in out["error"]

    def test_request_replay_falls_back_to_bus(self, orch):
        store = MagicMock()
        store.get_dlq_entry.return_value = {
            "topic": "writer.input",
            "payload": {"msg_type": "request", "payload": {"task_id": "t1",
                                                           "node": "writer"}}}
        orch.bus = MagicMock()
        orch.bus._store = store
        orch.registry.get_instance = MagicMock(return_value=None)
        orch.bus.request.return_value = {"ok": True}
        orch._set_task_output = MagicMock()
        out = orch.replay_dlq(7)
        orch.bus.request.assert_called_once()
        assert out["result"] == {"ok": True}

    def test_event_replay_rebroadcasts(self, orch):
        store = MagicMock()
        store.get_dlq_entry.return_value = {
            "topic": "writer.done",
            "payload": {"msg_type": "event", "payload": {"x": 1},
                        "from_agent": "w"}}
        orch.bus = MagicMock()
        orch.bus._store = store
        out = orch.replay_dlq(7)
        orch.bus.publish.assert_called_once()
        assert out == {"topic": "writer.done", "replayed": True}


class TestDeliveryChain:
    def _record_task(self):
        task = _mk_task(status=TaskStatus.DONE)
        task.output_path = ""
        task.dag_nodes = {"w": SimpleNamespace(
            name="w", agent_name="w", status="success", attempts=1,
            started_at=1000.0, finished_at=1002.0, error="", result={"a.md": "hi"})}
        return task

    def test_record_hashes_output_file(self, orch, tmp_path, monkeypatch):
        import loom_core.scheduler as sch
        monkeypatch.setattr(sch.Scheduler, "_topology_hash",
                            staticmethod(lambda p: "top-hash"))
        monkeypatch.setattr(sch.Scheduler, "_node_config_hash",
                            staticmethod(lambda ac: "cfg-hash"))
        out = tmp_path / "o.md"
        out.write_text("body", encoding="utf-8")
        task = self._record_task()
        task.output_path = str(out)
        plan = _plan("demo")
        plan.levels = [[SimpleNamespace(agent_name="w", dependencies=[],
                                        agent_config=SimpleNamespace(config={}))]]
        rec = orch._build_delivery_record(task, plan)
        assert rec["output_sha256"]
        assert rec["topology_hash"] == "top-hash"
        assert rec["config_hashes"] == {"w": "cfg-hash"}
        assert rec["nodes"][0]["artifact_keys"] == ["a.md"]
        assert rec["duration_sec"] == 10.0

    def test_record_tolerates_unreadable_output(self, orch, tmp_path):
        task = self._record_task()
        task.output_path = str(tmp_path)  # a directory, not a file
        rec = orch._build_delivery_record(task, _plan())
        assert rec["output_sha256"] == ""

    def test_deliver_build_failure_is_swallowed(self, orch):
        orch._build_delivery_record = MagicMock(side_effect=RuntimeError("bad"))
        orch._deliver(_mk_task(status=TaskStatus.DONE), _plan())
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_deliver_ledger_failure_is_swallowed(self, orch, monkeypatch):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record",
                            MagicMock(side_effect=RuntimeError("ledger down")))
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        orch._deliver(_mk_task(status=TaskStatus.DONE), _plan())
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_deliver_failed_run_skips_file_outputs(self, orch, monkeypatch, tmp_path):
        import loom_core.deliveries as dv
        rec = MagicMock()
        monkeypatch.setattr(dv.DeliveryLedger, "record", rec)
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        target = tmp_path / "failed.json"
        plan = _plan()
        plan.raw = {"pipeline": {"deliver": {"json": str(target)}}}
        orch._deliver(_mk_task(status=TaskStatus.FAILED), plan)
        rec.assert_called_once()
        assert not target.exists(), "失败运行不得产出交付 JSON"

    def test_deliver_writes_json(self, orch, monkeypatch, tmp_path):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record", MagicMock())
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        target = tmp_path / "rec" / "out.json"
        plan = _plan()
        plan.raw = {"pipeline": {"deliver": {"json": str(target)}}}
        orch._deliver(_mk_task(status=TaskStatus.DONE), plan)
        assert target.exists()

    def test_deliver_json_write_failure_logged(self, orch, monkeypatch):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record", MagicMock())
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        plan = _plan()
        plan.raw = {"pipeline": {"deliver": {"json": "bad\u0000name.json"}}}
        orch._deliver(_mk_task(status=TaskStatus.DONE), plan)
        assert any(lv == "warning" for lv, _ in _levels(orch))

    def test_deliver_site_without_markdown_warns(self, orch, monkeypatch, tmp_path):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record", MagicMock())
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        plan = _plan()
        plan.raw = {"pipeline": {"deliver": {"site": str(tmp_path / "site")}}}
        orch._deliver(_mk_task(status=TaskStatus.DONE), plan)
        assert any("没有 Markdown" in m for _, m in _levels(orch))

    def test_deliver_site_from_inline_content(self, orch, monkeypatch, tmp_path):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record", MagicMock())
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        plan = _plan("demo")
        plan.raw = {"pipeline": {"deliver": {"site": str(tmp_path / "site")}}}
        task = _mk_task(status=TaskStatus.DONE)
        task.output_content = "# hello"
        orch._deliver(task, plan)
        assert (tmp_path / "site").exists()

    def test_deliver_cfg_not_dict_is_ignored(self, orch, monkeypatch):
        import loom_core.deliveries as dv
        monkeypatch.setattr(dv.DeliveryLedger, "record", MagicMock())
        orch._build_delivery_record = MagicMock(return_value={"run_id": "t1"})
        plan = _plan()
        plan.raw = {"pipeline": {"deliver": "nonsense"}}
        orch._deliver(_mk_task(status=TaskStatus.DONE), plan)
        assert not [lv for lv, _ in _levels(orch) if lv == "error"]


class TestExecutePlanLoop:
    def _prep(self, orch):
        orch._arm_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
        orch._settle_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
        orch._runstore_nodes = MagicMock()
        orch._save_checkpoint = MagicMock()
        orch._merge_pooled_results = MagicMock()
        orch._execute_level = MagicMock(return_value=True)
        return orch

    def test_stop_event_cancels_before_level(self, orch):
        self._prep(orch)
        task = _mk_task()
        task.stop_event.set()
        plan = _plan()
        plan.levels = [[_wnode()]]
        orch._execute_plan(task, plan, "in.md")
        assert task.status == TaskStatus.CANCELLED

    def test_all_gate_level_advances_without_execution(self, orch):
        self._prep(orch)
        orch._settle_approval_gate = MagicMock(return_value=[])
        task = _mk_task()
        plan = _plan()
        plan.node_count = 1
        plan.levels = [[SimpleNamespace(agent_name="gate")]]
        orch._execute_plan(task, plan, "in.md")
        assert task.progress == 100
        assert task.status == TaskStatus.DONE
        orch._execute_level.assert_not_called()
        orch._save_checkpoint.assert_called_once()

    def test_level_failure_stops_loop(self, orch):
        self._prep(orch)
        orch._execute_level = MagicMock(return_value=False)
        task = _mk_task()
        task.status = TaskStatus.RUNNING
        plan = _plan()
        plan.node_count = 1
        plan.levels = [[_wnode()]]
        orch._execute_plan(task, plan, "in.md")
        assert task.current_step == 0
        assert task.status == TaskStatus.RUNNING

    def test_task_failed_after_level_returns(self, orch):
        self._prep(orch)
        task = _mk_task()
        plan = _plan()
        plan.node_count = 1
        level = [_wnode()]
        plan.levels = [level]

        def mark(t, lv, *a):
            t.status = TaskStatus.FAILED
            return True

        orch._execute_level = MagicMock(side_effect=mark)
        orch._execute_plan(task, plan, "in.md")
        assert task.status == TaskStatus.FAILED

    def test_sink_declared_but_not_delivered_fails(self, orch):
        self._prep(orch)
        orch._sink_declared = MagicMock(return_value=True)
        orch._delivered = MagicMock(return_value=False)
        task = _mk_task()
        plan = _plan()
        plan.levels = []
        orch._execute_plan(task, plan, "in.md")
        assert task.status == TaskStatus.FAILED
        assert "没有交付物" in task.error

    def test_plain_done_when_no_sink(self, orch):
        self._prep(orch)
        orch._sink_declared = MagicMock(return_value=False)
        task = _mk_task()
        plan = _plan()
        plan.levels = []
        orch._execute_plan(task, plan, "in.md")
        assert task.status == TaskStatus.DONE

    def test_paused_boundary_observes_external_cancel(self, orch):
        self._prep(orch)

        def arm(t, p, lv):
            t.status = TaskStatus.PAUSED
            return lv

        orch._arm_approval_gate = MagicMock(side_effect=arm)
        orch._observe_external_decision = MagicMock(return_value="cancelled")
        task = _mk_task()
        plan = _plan()
        plan.levels = [[_wnode()]]
        orch._execute_plan(task, plan, "in.md")
        orch._execute_level.assert_not_called()
        assert task.status == TaskStatus.PAUSED

    def test_paused_boundary_resumes_on_external_approval(self, orch):
        self._prep(orch)
        calls = {"n": 0}

        def arm(t, p, lv):
            if calls["n"] == 0:
                t.status = TaskStatus.PAUSED
            calls["n"] += 1
            return lv

        orch._arm_approval_gate = MagicMock(side_effect=arm)
        orch._observe_external_decision = MagicMock(return_value="running")
        task = _mk_task()
        plan = _plan()
        plan.node_count = 1
        plan.levels = [[_wnode()]]
        orch._execute_plan(task, plan, "in.md")
        orch._execute_level.assert_called_once()

    def test_executor_type_from_plan(self, orch, monkeypatch):
        self._prep(orch)
        import loom_core.pipeline as pm
        made = []

        def fake_create(max_workers, executor_type):
            made.append(executor_type)
            return MagicMock()

        monkeypatch.setattr(pm, "create_executor", fake_create)
        monkeypatch.setattr(pm, "shutdown_executor", lambda ex, force=False: None)
        task = _mk_task()
        plan = _plan()
        plan.node_count = 1
        plan.raw = {"pipeline": {"executor_type": "process"}}
        plan.levels = [[SimpleNamespace(agent_name="w", dependencies=[],
                                        timeout=5, max_retries=0,
                                        agent_config=SimpleNamespace(
                                            parallelism={"max_workers": 3}))]]
        orch._execute_plan(task, plan, "in.md")
        assert made == ["process"]


class TestDelegatesAndQueries:
    def test_visualize_plan_renders_steps(self, orch):
        text = orch.visualize_plan([
            {"step": 1, "agent": "writer", "priority": 10,
             "dependencies": ["a", "b"], "description": "写"},
            {"step": 2, "agent": "gate", "priority": 5,
             "dependencies": [], "description": ""},
        ])
        assert "writer" in text and "a, b" in text and "依赖: -" in text

    def test_discover_agents_delegates(self, orch):
        orch._loader = MagicMock()
        orch._loader.discover.return_value = ["a"]
        assert orch.discover_agents() == ["a"]

    def test_extract_meta_delegates(self, orch):
        orch._loader = MagicMock()
        orch._loader._extract_meta.return_value = "meta"
        assert orch._extract_meta(object) == "meta"

    def test_execute_node_delegates(self, orch):
        orch._executor = MagicMock()
        orch._executor.execute_node.return_value = {"ok": True}
        assert orch._execute_node(_mk_task(), object(), "i", {}) == {"ok": True}

    def test_extract_queries_delegates(self, orch):
        orch._executor = MagicMock()
        orch._executor._extract_queries.return_value = ["q"]
        assert orch._extract_queries("i", object()) == ["q"]

    def test_backoff_delay_delegates(self, orch):
        orch._executor = MagicMock()
        orch._executor._backoff_delay.return_value = 1.5
        assert orch._backoff_delay("linear", 1.0, 2) == 1.5

    def test_list_queued_tasks(self, orch):
        orch.task_queue.list_all.return_value = [{"id": "t"}]
        assert orch.list_queued_tasks(status="pending") == [{"id": "t"}]

    def test_list_tasks_filters_status(self, orch):
        running = _mk_task("r")
        done = _mk_task("d", TaskStatus.DONE)
        orch._running_tasks = {"r": running, "d": done}
        assert [t.id for t in orch.list_tasks(TaskStatus.DONE)] == ["d"]
        assert len(orch.list_tasks()) == 2

    def test_execution_stats_returns_list(self, orch):
        orch._execution_stats = [{"x": 1}]
        assert orch.get_execution_stats() == [{"x": 1}]

    def test_resolve_pipeline_name_delegates(self, orch, monkeypatch):
        orch.config = MagicMock()
        orch.config.get.return_value = "default-p"
        monkeypatch.setattr("loom_core.scheduler.installed_pipelines",
                            lambda: ["req", "default-p"])
        # 命中请求名
        assert orch.resolve_pipeline_name("req") == ("req", "")
        # 空请求 ⇒ 落到 config.default_pipeline
        assert orch.resolve_pipeline_name("") == ("default-p", "")
        # 不存在 ⇒ 返回空名 + 可读错误（不猜）
        name, err = orch.resolve_pipeline_name("nope")
        assert name == ""
        assert "nope" in err


class TestCallbacksAndAuditLog:
    def test_on_complete_appends_callbacks(self, orch):
        cb1, cb2 = MagicMock(), MagicMock()
        orch.on_complete("t1", cb1)
        orch.on_complete("t1", cb2)
        assert orch._task_callbacks["t1"] == [cb1, cb2]

    def test_notify_callbacks_runs_all(self, orch):
        seen = []
        orch._task_callbacks = {"t1": [lambda t: seen.append(1),
                                        lambda t: seen.append(2)]}
        orch._notify_callbacks(_mk_task())
        assert seen == [1, 2]

    def test_callback_exception_is_logged(self, orch):
        bad = MagicMock(side_effect=RuntimeError("boom"))
        orch._task_callbacks = {"t1": [bad]}
        orch._notify_callbacks(_mk_task())
        assert any(lv == "error" for lv, _ in _levels(orch))

    def test_audit_log_publishes_entry(self, orch):
        orch.bus = MagicMock()
        orch._audit_log("t1", "writer", {"a": 1}, {"b": 2}, 12.5, "success")
        topic, _from, entry = orch.bus.publish.call_args.args
        assert topic == "pipeline.audit"
        assert entry["agent"] == "writer"
        assert entry["input_keys"] == ["a"]

    def test_audit_log_swallows_publish_error(self, orch):
        orch.bus = MagicMock()
        orch.bus.publish.side_effect = RuntimeError("bus down")
        orch._audit_log("t1", "writer", {}, {}, 1.0, "success")
        assert any(lv == "error" for lv, _ in _levels(orch))

    def test_start_admin_api_delegates(self, orch, monkeypatch):
        import loom_core.admin_api as aa
        api = MagicMock()
        api.start.return_value = True
        monkeypatch.setattr(aa, "AdminAPI", MagicMock(return_value=api))
        assert orch.start_admin_api(port=9999) is True
        assert orch._admin_api is api

    def test_cleanup_task_temp_outer_exception(self, orch):
        orch.registry.list_agent_names = MagicMock(
            side_effect=RuntimeError("registry down"))
        orch._cleanup_task_temp(_mk_task())
        assert any("异常" in m for _, m in _levels(orch))

    def test_cleanup_stale_temp_outer_exception(self, orch):
        orch.registry.list_agent_names = MagicMock(
            side_effect=RuntimeError("registry down"))
        orch._cleanup_all_stale_temp()
        assert any("异常" in m for _, m in _levels(orch))

    def test_checkpoint_save_without_task_lock(self, orch):
        orch._checkpoint = MagicMock()
        orch._checkpoint.save.return_value = "saved"
        lockless = SimpleNamespace(id="t1")
        assert orch._executor._checkpoint_save(lockless) == "saved"
        orch._checkpoint.save.assert_called_once_with(lockless)
