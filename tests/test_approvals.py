"""审批的持久边界判据（2026-10-20 P0）：跨重启、跨进程、跨租户。

背景：审批此前只活在内存（`PipelineTask.awaiting_approval`），进程一重启
`approve()` 就找不到任务——"人审"变成一次性演示。本文件钉住修复后的契约：

  1. 台账本身：请求复用、只有 pending 能被决策、决定消费、按 principal 过滤；
  2. **真重启**：子进程跑到闸门后 `os._exit`（无收尾、无内存），父进程用新
     orchestrator 批准 → 队列行回到 pending+resume → worker 从 checkpoint 续跑完成；
  2b. **真双进程（owner 还活着）**：子进程停在闸门上不退出，父进程批准 → 决定只经
     持久台账被观察，活 owner 的任务不被重排、已完成节点不重跑；
  2c. 暂停边界必须是**完整**检查点（浅存档会丢已完成节点，续跑整条重跑）；
  3. 驳回同理：重启后驳回 → 队列行 cancelled、worker 不再执行；
  4. 真 HTTP：收件箱按 token 隔离、admin 看全量；越权审批 404（不泄露存在性）；
     日志/质量/告警这些跨租户聚合面对非 admin 403；dashboard 任务列表按 principal 过滤。
  5. 真 Postgres（`LOOM_TEST_PG_URL` 未设则 skip）：同一份审批契约在 PG 方言下
     也成立——待批跨实例（= 跨进程视图）可见、决定幂等、消费落库、按 principal 隔离。

边界：活/死 owner 的判定按 pid 存活，只在单机成立；跨主机接管同一队列未覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from loom_core import PipelineOrchestrator  # noqa: E402
from loom_core.admin_api import AdminAPI, AdminHandler  # noqa: E402
from loom_core.approvals import (  # noqa: E402
    APPROVED,
    REJECTED,
    ApprovalStore,
)
from loom_core.run_store import RunStore  # noqa: E402
from loom_core.scheduler import Scheduler  # noqa: E402
from loom_core.task_queue import TaskQueue  # noqa: E402
from loom_core.worker import TaskWorker  # noqa: E402

GATE_YAML = """
name: gate_demo
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


# ─── 1. 台账单元 ───────────────────────────────────

class TestApprovalStore:
    """台账契约单元。Stage 3a 起事实源是 RunStore 的 `approvals` 投影 + `approval.*` 事件。

    下面这批断言钉的是"审批台账对外承诺的语义"（待办复用、只有 pending 可被决策、
    决定消费后不再越闸、按 principal 过滤、按节点集合绑定），换后端不该改变任何一条
    ——所以用例形状原样保留，只把数据来源换成 store 门面；`db_path` 指一个不存在的
    老台账，让这一组用例绝不靠回落通过。
    """

    @pytest.fixture
    def store(self, tmp_path):
        return ApprovalStore(run_store=RunStore(str(tmp_path / "runs.db")),
                             db_path=str(tmp_path / "approvals.db"))

    def test_request_is_reused_while_pending(self, store):
        first = store.request("r1", "p", ["g"], {"g": "ok"}, principal="alice")
        second = store.request("r1", "p", ["g"], {"g": "ok"}, principal="alice")
        assert first == second, "同一运行在决定前重复 arm 不能造出两条待办"
        assert len(store.list_pending()) == 1

    def test_only_pending_can_be_decided(self, store):
        rid = store.request("r1", "p", ["g"])
        assert store.decide(rid, APPROVED, by="alice") is True
        assert store.decide(rid, REJECTED, by="bob") is False, "已决行不能被二次覆盖"
        row = store.get(rid)
        assert row["status"] == APPROVED and row["decided_by"] == "alice"

    def test_open_decision_until_consumed(self, store):
        rid = store.request("r1", "p", ["g"])
        store.decide(rid, APPROVED, note="看过了")
        assert store.open_decision("r1")["request_id"] == rid
        store.consume(rid)
        assert store.open_decision("r1") is None, "已消费的决定不能再次越过闸门"

    def test_pending_filtered_by_principal(self, store):
        store.request("a", "p", ["g"], principal="alice")
        store.request("b", "p", ["g"], principal="bob")
        assert [r["run_id"] for r in store.list_pending("alice")] == ["a"]
        assert {r["run_id"] for r in store.list_pending(None)} == {"a", "b"}

    def test_unknown_decision_rejected(self, store):
        rid = store.request("r1", "p", ["g"])
        with pytest.raises(ValueError):
            store.decide(rid, "maybe")

    def test_open_decision_is_bound_to_the_gate_nodes(self, store):
        """多闸门共用 run_id：G2 的决定不得被 G1 借走（2026-10-20 复核确认）。"""
        first = store.request("r1", "p", ["g1"])
        store.decide(first, APPROVED, note="g1-note")
        store.consume(first)
        second = store.request("r1", "p", ["g2"])
        store.decide(second, APPROVED, note="g2-note")
        assert store.open_decision("r1", nodes=["g1"]) is None,             "G1 拿到了 G2 的决定"
        assert store.open_decision("r1", nodes=["g2"])["note"] == "g2-note"

    def test_history_is_newest_first(self, store):
        first = store.request("r1", "p", ["g1"])
        store.decide(first, APPROVED)
        store.consume(first)          # 第一道闸门已过，才轮到第二道
        store.request("r1", "p", ["g2"])
        rows = store.list_for_run("r1")
        assert len(rows) == 2 and rows[0]["nodes"] == ["g2"]


# ─── 2/3. 真重启：子进程死在闸门上 ─────────────────

CHILD_SCRIPT = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from loom_core import PipelineOrchestrator
    from loom_core.scheduler import Scheduler

    yaml_path, agents_dir, ckpt, inp, task_id = sys.argv[2:7]
    orch = PipelineOrchestrator(agents_dir=agents_dir, checkpoint_dir=ckpt)
    orch.register_agents()
    plan = Scheduler(agents_dir=agents_dir,
                     pipeline_dir=str(__import__("pathlib").Path(yaml_path).parent)
                     ).parse_file(yaml_path, verify_lock=False)
    task = orch.run_plan(plan, input_file=inp, task_id=task_id, wait=False)
    deadline = time.time() + 40
    while time.time() < deadline and not task.awaiting_approval \\
            and task.status.value not in ("done", "failed", "cancelled"):
        time.sleep(0.05)
    print("GATE" if task.awaiting_approval
          else "NOSTATE:%s:%s" % (task.status.value, task.error))
    sys.stdout.flush()
    os._exit(0)  # 硬退出：不跑收尾、不留内存态——等价于进程被杀死
""")


def _pipeline_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pipes"
    d.mkdir(exist_ok=True)
    (d / "gate_demo.yaml").write_text(GATE_YAML, encoding="utf-8")
    return d


def _run_child_to_gate(tmp_path: Path, task_id: str) -> None:
    pipes = _pipeline_dir(tmp_path)
    inp = tmp_path / "in.md"
    inp.write_text("# 标题\n\n这是测试输入内容，用于触发摘要链路。\n", encoding="utf-8")
    script = tmp_path / "child_to_gate.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(PROJECT), str(pipes / "gate_demo.yaml"),
         str(PROJECT / "agents"), str(tmp_path / "ckpt"), str(inp), task_id],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )
    assert "GATE" in proc.stdout, \
        f"子进程没停在闸门上：{proc.stdout!r} / {proc.stderr[-800:]!r}"


def _new_orch(tmp_path: Path) -> PipelineOrchestrator:
    orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                checkpoint_dir=str(tmp_path / "ckpt"))
    orch.register_agents()
    return orch


def _worker(tmp_path: Path, orch: PipelineOrchestrator) -> TaskWorker:
    pipes = _pipeline_dir(tmp_path)
    return TaskWorker(orchestrator=orch, queue=TaskQueue(),
                      scheduler=Scheduler(agents_dir=str(PROJECT / "agents"),
                                          pipeline_dir=str(pipes)),
                      agents_dir=str(PROJECT / "agents"),
                      pipeline_dir=str(pipes))


class TestDurableApprovalAcrossRestart:
    def test_approve_after_restart_resumes_and_completes(self, tmp_path, monkeypatch):
        task_id = "restart_ok"
        _run_child_to_gate(tmp_path, task_id)

        orch = _new_orch(tmp_path)
        try:
            assert orch.get_task(task_id) is None, "前提：新进程内存里没有这个任务"
            pending = orch.approvals.pending_for_run(task_id)
            assert pending is not None and pending["nodes"] == ["release_gate"]

            ok, err = orch.approve(task_id, note="重启后批准", by="alice")
            assert ok and err == "", err
            assert orch.task_queue.get(task_id)["status"] == "pending", \
                "批准后队列行必须放回 pending 才会被 worker 续跑"

            monkeypatch.chdir(tmp_path)  # worker 的 output/ 落点别写进仓库
            item = _worker(tmp_path, orch).run_once()
            assert item and item["task_id"] == task_id
            assert item["resume"] is True, "worker 必须拿到续跑标记"

            task = orch.get_task(task_id)
            assert task is not None
            deadline = time.time() + 30
            while time.time() < deadline and task.status.value not in ("done", "failed", "cancelled"):
                time.sleep(0.05)
            assert task.status.value == "done", f"续跑未完成：{task.status.value} / {task.error}"
            gate = task.dag_nodes["release_gate"]
            assert gate.status == "success"
            assert gate.result["status"] == "approved"
            assert gate.result["note"] == "重启后批准"
            assert orch.approvals.open_decision(task_id) is None, "决定应已被消费"
            assert orch.approvals.list_for_run(task_id)[0]["status"] == APPROVED
            assert orch.task_queue.get(task_id)["status"] == "done"
        finally:
            orch.shutdown()

    def test_reject_after_restart_blocks_resume(self, tmp_path):
        task_id = "restart_no"
        _run_child_to_gate(tmp_path, task_id)

        orch = _new_orch(tmp_path)
        try:
            ok, err = orch.reject(task_id, reason="内容不合规", by="alice")
            assert ok and err == "", err
            assert orch.task_queue.get(task_id)["status"] == "cancelled"
            assert orch.approvals.list_for_run(task_id)[0]["status"] == REJECTED
            assert _worker(tmp_path, orch).run_once() is None, "驳回过的任务不能再被执行"
        finally:
            orch.shutdown()


# ─── 3. 真双进程：owner 还活着时的外部批准 ──────────

LIVE_CHILD_SCRIPT = textwrap.dedent("""
    import json, os, sys, threading, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from loom_core import PipelineOrchestrator
    from loom_core.scheduler import Scheduler

    yaml_path, agents_dir, ckpt, inp, task_id, out_path = sys.argv[2:8]
    orch = PipelineOrchestrator(agents_dir=agents_dir, checkpoint_dir=ckpt)
    orch.register_agents()
    plan = Scheduler(agents_dir=agents_dir,
                     pipeline_dir=str(Path(yaml_path).parent)
                     ).parse_file(yaml_path, verify_lock=False)

    # 计数探针挂在真实执行漏斗（dag_executor.execute_node_from_scheduler）上：
    # 它记的是"本进程真的跑了这个节点几次"。重跑会让计数涨起来，
    # 而挂在 checkpoint/内存状态上的断言看不见这件事。
    counts = {}
    lock = threading.Lock()
    real = orch._executor.execute_node_from_scheduler

    def counted(task, node, input_file, plan):
        # 提交侧用的是关键字参数（dag_executor._submit_level_futures），
        # 形参名必须对齐，否则探针在 future 里炸、断言变成空集自证失败
        with lock:
            key = node.agent_name
            counts[key] = counts.get(key, 0) + 1
        return real(task=task, node=node, input_file=input_file, plan=plan)

    orch._executor.execute_node_from_scheduler = counted

    task = orch.run_plan(plan, input_file=inp, task_id=task_id, wait=False)

    # 观察点必须是**终态**，不是"此刻还挂在暂停上"。外部批准可能落在两次 50ms 采样之间：
    # 子进程确实挂起过（父进程看到台账 pending 行才会继续），但等这个主线程回头看时，
    # awaiting_approval 已被清空、运行已经跑完 ⇒ 把"没观察到暂停"误报成"没暂停"。
    # "挂起发生过"这件事由父进程侧的台账判据负责，不靠子进程采样。
    deadline = time.time() + 150
    while time.time() < deadline and task.status.value not in ("done", "failed", "cancelled"):
        time.sleep(0.05)
    status = task.status.value
    if status not in ("done", "failed", "cancelled"):
        Path(out_path).write_text(json.dumps(
            {"phase": "timeout", "status": status, "error": task.error,
             "counts": dict(counts), "pid": os.getpid()},
            ensure_ascii=False, default=str), encoding="utf-8")
        os._exit(9)

    # 关键：进程活到终态才退出——这就是"暂停中的真 worker"被外部决定唤醒的完整过程
    time.sleep(0.5)  # 等 finally 链把队列终态/账本写完（父进程仍按等待条件复核）
    gate = task.dag_nodes.get("release_gate")
    Path(out_path).write_text(json.dumps({
        "phase": "terminal",
        "status": status,
        "error": task.error,
        "pid": os.getpid(),
        "counts": dict(counts),
        "gate_status": getattr(gate, "status", None),
        "gate_result": gate.result if gate else None,
    }, ensure_ascii=False, default=str), encoding="utf-8")
    os._exit(0 if status == "done" else 8)
""")


class TestLiveWorkerObservesExternalApproval:
    def test_paused_worker_resumes_on_other_process_decision(self, tmp_path):
        """批准方在另一个进程、执行方还活着：台账是唯一通路，且不得重排活 owner。

        分工写死，避免再把瞬时状态当信号：
        - "闸门确实挂起过" = 父进程读到 pending 台账行的那一刻 `proc.poll()` 仍是 None；
        - "被外部决定唤醒并跑完" = 子进程终态落盘（闸门节点 success + 批准人写的 note）；
        - 子进程**不**报告"此刻是否还挂在暂停上"——批准可能落在两次采样之间，
          等它回头读 `awaiting_approval` 时已被清空（CI 3.13 腿实测红在 no_gate）。
        """
        task_id = "live_worker"
        pipes = _pipeline_dir(tmp_path)
        inp = tmp_path / "in.md"
        inp.write_text("# 标题\n\n这是测试输入内容，用于触发摘要链路。\n", encoding="utf-8")
        script = tmp_path / "child_live.py"
        script.write_text(LIVE_CHILD_SCRIPT, encoding="utf-8")
        out = tmp_path / "child_state.json"
        logs = tmp_path / "child.log"

        orch = _new_orch(tmp_path)
        with logs.open("w", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                [sys.executable, str(script), str(PROJECT), str(pipes / "gate_demo.yaml"),
                 str(PROJECT / "agents"), str(tmp_path / "ckpt"), str(inp),
                 task_id, str(out)],
                stdout=log_fh, stderr=subprocess.STDOUT, text=True, cwd=str(tmp_path))
        try:
            # 握手用台账而不是 stdout：证明"闸门挂着、owner 还活着"这个前提。
            # 超时按"父进程启动子进程"起算，含子进程的 import + 20 只 Agent 注册，
            # 所以不能照搬子进程内部那几个 40s 判据（23:31 那次就是这么超时的）。
            pending = _wait_for(lambda: None if proc.poll() is not None
                                else orch.approvals.pending_for_run(task_id),
                                timeout=150)
            assert pending is not None, f"子进程没把闸门落到台账：{logs.read_text()[-800:]}"
            assert pending["nodes"] == ["release_gate"]
            assert proc.poll() is None, "前提：执行方进程还活着（不是重启后的续跑）"

            # 决定还没写，子进程不可能自己走——此刻的行状态是稳定读数，
            # 用它钉"外部批准不得重排活 owner"（批准之后行会很快转终态，那时再读就是竞态）
            row = orch.task_queue.get(task_id)
            assert row["status"] == "running", f"前提：活 owner 的行应在 running：{row}"
            assert not row.get("resume"), f"活 owner 的行不该带续跑标记：{row}"
            assert orch.task_queue.mark_for_resume(task_id) is False, \
                "活 owner 被重排——同一任务会有两个执行者"
            assert orch.task_queue.get(task_id)["status"] == "running"

            ok, err = orch.approve(task_id, note="外部进程批准", by="ops")
            assert ok and err == "", err

            state = _wait_for(lambda: _read_json(out), timeout=210)
            assert state is not None, f"子进程没跑完：{logs.read_text()[-800:]}"
            assert state["phase"] == "terminal", state
            assert state["status"] == "done", f"外部批准后没继续执行：{state}"
            assert state["counts"] == {"tldr": 1}, \
                f"已完成节点被重跑：{state['counts']}"
            assert state["gate_status"] == "success"
            assert state["gate_result"]["note"] == "外部进程批准", state["gate_result"]

            terminal = _wait_for(
                lambda: (lambda r: r if r and r["status"] in ("done", "failed", "cancelled")
                         else None)(orch.task_queue.get(task_id)), timeout=30)
            assert terminal["status"] == "done", terminal
            assert not terminal.get("resume"), \
                f"终态行带续跑标记说明它被重排过：{terminal}"
            assert orch.approvals.open_decision(task_id) is None, "决定没被消费（会被二次越闸）"
            assert orch.approvals.list_for_run(task_id)[0]["status"] == APPROVED
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            orch.shutdown()


def _wait_for(probe, timeout: float = 60.0, interval: float = 0.1):
    """按判据轮询（不是固定 sleep）：探到值即返回，超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(interval)
    return None


def _read_json(path: Path) -> dict | None:
    """读子进程落盘的终态；不存在或写了一半就继续等（由 _wait_for 重试）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ─── 4. 真 HTTP：收件箱作用域与 admin 面 ───────────

TOK_A = "tok-alice-approvals"
TOK_B = "tok-bob-approvals"
TOK_OPS = "tok-ops-approvals"


@pytest.fixture(autouse=True)
def reset_handler_state():
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.orch = None
    AdminHandler.tokens = None
    AdminHandler.principal = None
    AdminHandler.dashboard_dir = None


def _tokens_yaml(tmp_path: Path) -> Path:
    f = tmp_path / "tokens.yaml"
    f.write_text(f"""
tokens:
  - id: alice
    token_sha256: {hashlib.sha256(TOK_A.encode()).hexdigest()}
  - id: bob
    token_sha256: {hashlib.sha256(TOK_B.encode()).hexdigest()}
  - id: ops
    admin: true
    token_sha256: {hashlib.sha256(TOK_OPS.encode()).hexdigest()}
""", encoding="utf-8")
    return f


def _start(orch, tokens_file: Path):
    api = AdminAPI(host="127.0.0.1", port=0, api_key="",
                   tokens_file=str(tokens_file))
    assert api.start(orch) is True
    return f"http://127.0.0.1:{api._server.server_address[1]}", api


def _req(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


class TestApprovalHTTP:
    def test_inbox_scoped_and_durable_approve(self, tmp_path):
        orch = _new_orch(tmp_path)
        orch.approvals.request("run_a", "gate_demo", ["g"], {"g": "确认"}, principal="alice")
        orch.approvals.request("run_b", "gate_demo", ["g"], {"g": "确认"}, principal="bob")
        base, api = _start(orch, _tokens_yaml(tmp_path))
        try:
            code, data = _req("GET", base + "/api/approvals", TOK_A)
            assert code == 200, data
            assert [a["run_id"] for a in data["approvals"]] == ["run_a"]

            code, data = _req("GET", base + "/api/approvals", TOK_OPS)
            assert {a["run_id"] for a in data["approvals"]} == {"run_a", "run_b"}
            assert all("principal" in a for a in data["approvals"])

            # 越权审批按不存在处理（404，不泄露存在性）
            code, _ = _req("POST", base + "/tasks/run_a/approve", TOK_B, {})
            assert code == 404

            # 日志/质量/告警是跨租户聚合面：非 admin 403
            assert _req("GET", base + "/api/logs?limit=1", TOK_A)[0] == 403
            assert _req("GET", base + "/api/quality/feedback", TOK_A)[0] == 403
            assert _req("GET", base + "/api/logs?limit=1", TOK_OPS)[0] == 200

            # 自己的待办可以批（持久路径：任务不在内存）
            code, data = _req("POST", base + "/tasks/run_a/approve", TOK_A, {"note": "ok"})
            assert code == 200 and data["approved"] is True, data
            assert orch.approvals.pending_for_run("run_a") is None
            assert orch.approvals.list_for_run("run_a")[0]["status"] == APPROVED
        finally:
            api.stop()
            orch.shutdown()

    def test_dashboard_hides_other_tenants_tasks(self, tmp_path):
        from types import SimpleNamespace

        def _task(tid, owner):
            return SimpleNamespace(
                id=tid, owner=owner, pipeline_name="gate_demo",
                status=SimpleNamespace(value="done"),
                created_at=0, finished_at=0, started_at=0, error=None,
                result={}, output_path="", progress=100, steps=[],
            )

        orch = MagicMock()
        # 作用域过滤已收进引擎的 list_runs（内存缓存 ∪ 投影）；替身若直接返回
        # 过滤好的行，这条判据就没跑过被测逻辑——挂真实现，只伪造数据底座。
        from types import MethodType

        from loom_core.pipeline import PipelineOrchestrator
        tasks = {"t_a": _task("t_a", "alice"), "t_b": _task("t_b", "bob")}
        orch._running_tasks = dict(tasks)
        orch._runstore = lambda: None
        orch.list_runs = MethodType(PipelineOrchestrator.list_runs, orch)
        orch.registry.list.return_value = []
        base, api = _start(orch, _tokens_yaml(tmp_path))
        try:
            code, data = _req("GET", base + "/api/dashboard", TOK_A)
            assert code == 200 and [t["id"] for t in data["tasks"]] == ["t_a"]
            code, data = _req("GET", base + "/api/dashboard", TOK_OPS)
            assert {t["id"] for t in data["tasks"]} == {"t_a", "t_b"}
        finally:
            api.stop()


class TestPauseCheckpointIsFullState:
    def test_pause_boundary_keeps_completed_nodes(self, tmp_path):
        """暂停边界必须是完整检查点：浅存档会丢掉已完成节点（复核确认）。"""
        task_id = "cp_full"
        _run_child_to_gate(tmp_path, task_id)
        ckpt = tmp_path / "ckpt" / f"{task_id}.json"
        data = json.loads(ckpt.read_text(encoding="utf-8"))
        nodes = data.get("_dag_nodes") or {}
        assert "tldr" in nodes, f"暂停检查点没有已完成节点：{sorted(data)}"
        assert nodes["tldr"]["status"] == "success"
        assert data.get("_result"), "暂停检查点没有结果快照"


class TestLiveOwnerIsNotRequeued:
    def test_mark_for_resume_refuses_a_live_owner(self, tmp_path):
        """管理员进程内存里没有任务 ≠ owner 已死：活 owner 的任务不得重排。"""
        import subprocess
        import sys

        q = TaskQueue(str(tmp_path / "tasks.db"))
        q.submit("live_owner", "gate_demo", "in.md")
        assert q.claim_direct("live_owner") is True
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with q._lock, q._get_conn() as conn:
                conn.execute("UPDATE task_queue SET owner_pid = ? WHERE task_id = ?",
                             (sleeper.pid, "live_owner"))
            assert q.mark_for_resume("live_owner") is False, "活 owner 被重排"
            assert q.get("live_owner")["status"] == "running"
        finally:
            sleeper.kill()
            sleeper.wait(timeout=10)
        assert q.mark_for_resume("live_owner") is True
        assert q.get("live_owner")["status"] == "pending"


# ── 5. 真 Postgres：A1 判据「真 PG 同样通过」的落点 ──────────────
# 台账此前只有 SQLite 形态判据（2026-11-12 T7 核验如实标「未验」）。
# CI 的 test job 自带真 PG service（ci.yml services.postgres +
# LOOM_TEST_PG_URL），本类在 PG 方言下复钉同一份契约；本机无 PG 时
# 整类 skip，与 test_schedule_lease.py 的 PG 腿同规。
PG_URL = os.environ.get("LOOM_TEST_PG_URL", "").strip()

try:
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False


@pytest.mark.skipif(not (PG_URL and _HAS_PSYCOPG),
                    reason="LOOM_TEST_PG_URL 未设置或 psycopg 未安装（真 PG 判据）")
class TestApprovalDurableOnPostgres:
    def test_request_decide_consume_survive_store_reopen_on_real_pg(self):
        run_id = f"pg-t8-run-{uuid.uuid4().hex[:10]}"
        request_id = f"pg-t8-req-{uuid.uuid4().hex[:10]}"
        first = RunStore(PG_URL)
        first.open_approval(run_id, request_id, pipeline="gate_demo",
                            nodes=["release_gate"], principal="alice")

        # 「重启」= 同一 DSN 上的全新实例：内存一概没有，只认持久层
        second = RunStore(PG_URL)
        pending = second.pending_approval(run_id)
        assert pending is not None, "待批必须穿过实例边界活下来"
        assert pending["request_id"] == request_id
        assert pending["nodes"] == ["release_gate"]
        assert pending["principal"] == "alice"
        assert request_id in [r["request_id"]
                              for r in second.list_pending_approvals("alice")]
        assert second.list_pending_approvals("bob") == [], "跨租户隔离在 PG 方言下同义"

        decided = second.decide_approval(request_id, APPROVED, by="alice",
                                         note="重启后批准")
        assert decided is not None and decided["status"] == APPROVED
        assert second.decide_approval(request_id, REJECTED, by="bob") is None, \
            "已决行不能被二次覆盖"
        assert second.consume_approval(request_id) >= 1, "消费必须落库"

        third = RunStore(PG_URL)
        assert third.pending_approval(run_id) is None, "决定后不再占据待批"
        hist = third.approvals_for_run(run_id)
        assert hist[0]["status"] == APPROVED and hist[0]["consumed"] == 1
