"""A7 只读对账报告判据（板面 §三十四 A7 规格）。

钉三件事：
1. 三类漂移逐行命中（stale-owner-dead / projection-running /
   projection-unstamped），字段口径正确（last_event_ts 取最后事件、
   owner_pid=0/已死都算无人认领）；
2. 负控不报（新鲜 running、过期但 pid 存活、阈值边界、队列×投影终态一致、
   中间态、孤儿投影）；
3. 只读性实证：跑前后库内容零变化（逐表全量 dump + 文件清单/大小 + 主库
   字节哈希），以及 CLI 端到端（空库 0 行 / --json-output 单行 JSON 通道）。

漂移侧一律字面量 SQL 直插——公共 API 的 open_run 用 time.time() 盖 ts，会污染
「最后事件时间」判据；正常态夹具才走公共 API（_ensure_schema 建表）。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent

NOW = 1_000_000.0
STALE = 900.0


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """LOOM_STATE_DIR 指到 tmp：tasks.db / runs.db 都跟它走，并预建表。"""
    d = tmp_path / "state"
    monkeypatch.setenv("LOOM_STATE_DIR", str(d))
    _ensure_schema()
    return d


def _ensure_schema():
    from loom_core.run_store import RunStore
    from loom_core.task_queue import TaskQueue

    TaskQueue()
    RunStore()


def _dead_pid():
    """真死 pid：起一个秒退子进程并等它收尸，取其 pid。

    与 tests/test_p2_infra_fixes.py 同手法（Popen + wait 后 pid 必死）；
    双平台都成立：Windows 上 OpenProcess 对已退出 pid 失败，POSIX 上
    wait() 已 reap，os.kill(pid, 0) 报 ProcessLookupError。
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"])
    child.wait()
    return child.pid


def _queue_row(state_dir, *, task_id, status, started_at=0.0, finished_at=0.0,
               owner_pid=0):
    conn = sqlite3.connect(state_dir / "tasks.db")
    try:
        conn.execute(
            "INSERT INTO task_queue "
            "(task_id, pipeline_name, input_file, status, config_json, projection, "
            "error, created_at, started_at, finished_at, worker_id, owner_pid) "
            "VALUES (?, ?, ?, ?, '{}', 'ok', '', ?, ?, ?, '', ?)",
            (task_id, "docgen", "in.md", status, NOW - 10000.0,
             started_at, finished_at, owner_pid),
        )
        conn.commit()
    finally:
        conn.close()


def _run_row(state_dir, *, run_id, status, started_at=NOW - 5000.0,
             finished_at=0.0):
    conn = sqlite3.connect(state_dir / "runs.db")
    try:
        conn.execute(
            "INSERT INTO runs "
            "(run_id, workflow, principal, status, trigger, inputs_json, "
            "started_at, finished_at, error, cost_usd, quality_score, last_seq, "
            "artifacts_json) "
            "VALUES (?, ?, '', ?, '', '{}', ?, ?, '', 0, 0, 0, '[]')",
            (run_id, "docgen", status, started_at, finished_at),
        )
        conn.commit()
    finally:
        conn.close()


def _event(state_dir, *, run_id, seq, type_, ts):
    conn = sqlite3.connect(state_dir / "runs.db")
    try:
        conn.execute(
            "INSERT INTO events (run_id, seq, type, payload_json, ts) "
            "VALUES (?, ?, ?, '{}', ?)",
            (run_id, seq, type_, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _report(state_dir, **kw):
    from loom_core.reconcile import stale_running_report
    from loom_core.run_store import RunStore
    from loom_core.task_queue import TaskQueue

    kw.setdefault("now", NOW)
    kw.setdefault("stale_seconds", STALE)
    return stale_running_report(TaskQueue(), RunStore(), **kw)


def _snapshot(state_dir):
    """库内容的全量指纹：逐表全量 dump + 文件清单/大小 + 主库字节哈希。

    -wal/-shm 是 SQLite 自己的中间态文件，内容字节不可复现，只记名与大小；
    主库 .db 的字节哈希 + 全表 dump 双通道，任何逻辑写都无处遁形。
    SQL 全字面量（表名写死），不拼标识符。
    """
    files = {}
    for p in sorted(state_dir.rglob("*")):
        if p.is_file():
            files[str(p.relative_to(state_dir))] = p.stat().st_size
    hashes = {}
    for name in ("tasks.db", "runs.db"):
        db = state_dir / name
        if db.is_file():
            hashes[name] = hashlib.sha256(db.read_bytes()).hexdigest()
    dumps = {}
    conn = sqlite3.connect(state_dir / "tasks.db")
    try:
        dumps["tasks.db:task_queue"] = repr(
            conn.execute(
                "SELECT * FROM task_queue ORDER BY rowid").fetchall())
    finally:
        conn.close()
    conn = sqlite3.connect(state_dir / "runs.db")
    try:
        dumps["runs.db:runs"] = repr(
            conn.execute("SELECT * FROM runs ORDER BY rowid").fetchall())
        dumps["runs.db:events"] = repr(
            conn.execute("SELECT * FROM events ORDER BY rowid").fetchall())
    finally:
        conn.close()
    return {"files": files, "hashes": hashes, "dumps": dumps}


# ─── 三类漂移逐行命中 ─────────────────────────────

def test_three_categories_each_hit(state_dir):
    dead = _dead_pid()
    # ① 队列 running + started_at 过期 + owner_pid 已死
    _queue_row(state_dir, task_id="t-stale", status="running",
               started_at=NOW - STALE - 1, owner_pid=dead)
    # ② 队列终态 + 投影仍 running
    _queue_row(state_dir, task_id="t-projrun", status="done",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="t-projrun", status="running")
    # ③a 队列终态 + 投影行缺失
    _queue_row(state_dir, task_id="t-norun", status="failed",
               started_at=NOW - 100, finished_at=NOW - 90)
    # ③b 队列终态 + 投影非终态非 running
    _queue_row(state_dir, task_id="t-unstamped", status="cancelled",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="t-unstamped", status="replayed")

    rows = _report(state_dir)

    assert [r["task_id"] for r in rows] == sorted(r["task_id"] for r in rows)
    cats = {c: sorted(r["task_id"] for r in rows if r["category"] == c)
            for c in ("stale-owner-dead", "projection-running",
                      "projection-unstamped")}
    assert cats["stale-owner-dead"] == ["t-stale"]
    assert cats["projection-running"] == ["t-projrun"]
    assert cats["projection-unstamped"] == ["t-norun", "t-unstamped"]
    assert len(rows) == 4


def test_category_one_field_detail(state_dir):
    from loom_core.reconcile import ROW_KEYS

    dead = _dead_pid()
    _queue_row(state_dir, task_id="t1", status="running",
               started_at=NOW - STALE - 5, owner_pid=dead)
    # 事件序列 1000/1500/2000 → last_event_ts 必须取 max=2000.0
    _run_row(state_dir, run_id="t1", status="running",
             started_at=NOW - STALE - 5)
    for seq, ts in ((1, 1000.0), (2, 1500.0), (3, 2000.0)):
        _event(state_dir, run_id="t1", seq=seq, type_="node_done", ts=ts)

    rows = _report(state_dir)
    assert len(rows) == 1
    r = rows[0]
    assert list(r.keys()) == list(ROW_KEYS)
    assert r["category"] == "stale-owner-dead"
    assert r["task_id"] == "t1"
    assert r["queue_status"] == "running"
    assert r["queue_started_at"] == NOW - STALE - 5
    assert r["queue_finished_at"] == 0.0
    assert r["owner_pid"] == dead
    assert r["run_status"] == "running"
    assert r["last_event_ts"] == 2000.0
    assert "stale_seconds" in r["reason"]

    # ① 的两个变体：owner_pid 缺失(0) / started_at 缺失(0) 同样算无人认领的陈旧行
    _queue_row(state_dir, task_id="t1b", status="running",
               started_at=NOW - STALE - 5, owner_pid=0)
    _queue_row(state_dir, task_id="t1c", status="running",
               started_at=0.0, owner_pid=dead)
    rows = _report(state_dir)
    assert [r["task_id"] for r in rows
            if r["category"] == "stale-owner-dead"] == ["t1", "t1b", "t1c"]


def test_category_two_projection_running(state_dir):
    _queue_row(state_dir, task_id="t2", status="done",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="t2", status="running",
             started_at=NOW - 100, finished_at=0.0)

    rows = _report(state_dir)
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "projection-running"
    assert r["reason"] == "projection running while queue terminal"
    assert r["queue_status"] == "done"
    assert r["run_status"] == "running"


def test_category_three_variants(state_dir):
    # ③a 投影行缺失
    _queue_row(state_dir, task_id="t3a", status="failed",
               started_at=NOW - 10, finished_at=NOW - 5)
    # ③b 投影状态非终态非 running
    _queue_row(state_dir, task_id="t3b", status="done",
               started_at=NOW - 10, finished_at=NOW - 5)
    _run_row(state_dir, run_id="t3b", status="replayed")

    rows = _report(state_dir)
    assert sorted(r["task_id"] for r in rows) == ["t3a", "t3b"]
    assert all(r["category"] == "projection-unstamped" for r in rows)
    ra = next(r for r in rows if r["task_id"] == "t3a")
    assert ra["reason"] == "projection row missing"
    assert ra["run_status"] is None
    assert ra["last_event_ts"] is None
    rb = next(r for r in rows if r["task_id"] == "t3b")
    assert "not stamped terminal" in rb["reason"]
    assert rb["run_status"] == "replayed"


# ─── 负控：这些形态一律不报 ─────────────────────────────

def test_negative_controls_not_reported(state_dir):
    alive = os.getpid()
    # 新鲜 running（未过期）
    _queue_row(state_dir, task_id="fresh", status="running",
               started_at=NOW - 10, owner_pid=alive)
    # 过期但 pid 存活（仍在执行，不算漂移）
    _queue_row(state_dir, task_id="stale-alive", status="running",
               started_at=NOW - STALE - 1, owner_pid=alive)
    # 阈值边界：started_at 恰为 cutoff，不算过期（严格 <）
    _queue_row(state_dir, task_id="boundary", status="running",
               started_at=NOW - STALE, owner_pid=_dead_pid())
    # 队列终态 + 投影终态 = 一致行
    _queue_row(state_dir, task_id="consistent", status="done",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="consistent", status="failed",
             started_at=NOW - 100, finished_at=NOW - 90)
    # 中间态 pending
    _queue_row(state_dir, task_id="pending1", status="pending")
    # 孤儿投影（runs 有行、队列无对应行）：规格外，不报
    _run_row(state_dir, run_id="orphan", status="running")

    assert _report(state_dir) == []


# ─── 只读性实证 ─────────────────────────────

def test_read_only_no_writes(state_dir):
    dead = _dead_pid()
    _queue_row(state_dir, task_id="t-stale", status="running",
               started_at=NOW - STALE - 1, owner_pid=dead)
    _queue_row(state_dir, task_id="t-projrun", status="done",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="t-projrun", status="running")

    before = _snapshot(state_dir)
    rows = _report(state_dir)
    after = _snapshot(state_dir)

    assert rows  # 有漂移可报（否则下面比对是空转）
    assert after == before


# ─── CLI 端到端 ─────────────────────────────

def test_cli_empty_db_clean(state_dir):
    proc = subprocess.run(
        [sys.executable, "run.py", "reconcile"],
        cwd=str(PROJECT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "LOOM_STATE_DIR": str(state_dir)},
    )
    assert proc.returncode == 0
    assert "[reconcile] 无 stale-running 漂移" in proc.stdout
    assert "共 0 行漂移" in proc.stdout
    assert "未写库" in proc.stdout  # 人类输出自带只读声明
    assert "Agent 工作流引擎" in proc.stderr  # banner 走 stderr


def test_cli_json_drift(state_dir):
    dead = _dead_pid()
    # ① stale + 死 pid + 已知事件序列
    _queue_row(state_dir, task_id="j1", status="running",
               started_at=NOW - STALE - 1, owner_pid=dead)
    _run_row(state_dir, run_id="j1", status="running")
    _event(state_dir, run_id="j1", seq=1, type_="run.started", ts=1000.0)
    _event(state_dir, run_id="j1", seq=2, type_="node_done", ts=2000.0)
    # ② 队列终态 + 投影 running
    _queue_row(state_dir, task_id="j2", status="done",
               started_at=NOW - 100, finished_at=NOW - 90)
    _run_row(state_dir, run_id="j2", status="running")

    proc = subprocess.run(
        [sys.executable, "run.py", "reconcile", "--json-output"],
        cwd=str(PROJECT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "LOOM_STATE_DIR": str(state_dir)},
    )
    assert proc.returncode == 0
    # json_output 契约：stdout 恰一行 JSON（过程输出全改接 stderr）
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert isinstance(data, list) and len(data) == 2
    by_id = {r["task_id"]: r for r in data}
    assert by_id["j1"]["category"] == "stale-owner-dead"
    assert by_id["j1"]["last_event_ts"] == 2000.0
    assert by_id["j2"]["category"] == "projection-running"
    assert "Agent 工作流引擎" in proc.stderr  # banner 没污染 JSON 通道


# ─── 动词翻译层 ─────────────────────────────

def test_split_verb_reconcile():
    run = importlib.import_module("run")
    verb, action, rest = run._split_verb(["reconcile"])
    assert (verb, action, rest) == ("reconcile", "reconcile", [])


def test_split_verb_reconcile_rejects_positional():
    run = importlib.import_module("run")
    with pytest.raises(SystemExit) as ei:
        run._split_verb(["reconcile", "extra"])
    assert ei.value.code == 2


def test_split_verb_reconcile_passes_flags():
    """flag 不是位置参数，必须放行给 argparse（--stale-seconds/--json-output）。"""
    run = importlib.import_module("run")
    verb, action, rest = run._split_verb(["reconcile", "--json-output"])
    assert (verb, action) == ("reconcile", "reconcile")
    assert rest == ["--json-output"]


def test_reconcile_stale_seconds_flag():
    run = importlib.import_module("run")
    verb, action, argv = run._split_verb(["reconcile", "--stale-seconds", "5"])
    args = run.build_arg_parser().parse_args(argv)
    assert args.stale_seconds == 5.0
    assert args.stale_seconds != args.lease_stale  # 两个阈值互相独立
