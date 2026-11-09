"""RunStore + EventLog —— 一次运行的事实只有**一个写入口**（RunStore 设计 §3）。

治的是什么：运行事实此前散在五处写面（内存 `_running_tasks`、`task_queue`、
checkpoint JSON、`deliveries.db`、`approvals.db`），读侧 20 处各自回落，其中最坏的
几条把"进程没重启过"当前提（见 `docs/runstore-eventlog-design-2026-10-21.md` §1）。
本模块不搬旧数据，而是把**新运行**的每一个事实先落成一条不可变事件，再在
**同一个事务**里更新投影（`runs` / `run_nodes`）——恢复与审计因此共处同一个时间
坐标系，不会出现"事件说完成、投影说运行中"。

四张表 + 一张审批投影（Stage 3a 起，审批的事实源就是这里）：

| 表 | 角色 |
|---|---|
| `schema_version` | 结构版本；取代散落的 `add_column_if_missing` 临时迁移 |
| `events` | 追加式事实流，PK `(run_id, seq)`，`seq` 每运行单调 |
| `runs` | 运行投影（列表页与作用域判定的唯一来源），`last_seq` 让 SSE 游标跨进程续传 |
| `run_nodes` | 逐节点投影，把原先埋在 `deliveries.payload_json` 里的事实升为一等行
| `approvals` | 审批投影（结构版本 2 起）：待批/决定/消费是可变状态，所以要投影；每次变更同事务落一条事件
| `schedules` | 触发租约（结构版本 3 起）：cron 触发的"谁有权提交"判据，两进程共享一库只允许一个触发者；`last_fire`/`next_fire` 让排程在观测面可见

四条刻意留下的边界（对外要说清，不能读成"什么都齐了"）：

- **不 backfill**：老运行不进这里，仍按原路径经 `deliveries` 读得到终态。
- **有事件必有投影行**：`_append` 先 `INSERT OR IGNORE INTO runs`，所以不存在
  "events 里有 run_id、runs 里查不到"的半条事实。
- **`seq` 由事务内取号**：`MAX(seq)+1`，不用自增 id——多个进程共享一个库文件时，
  跨进程唯一不等于每运行单调，而 SSE 游标要的是后者。SQLite 侧靠 `BEGIN IMMEDIATE`
  把取号排队；Postgres 侧对应 `SELECT ... FOR UPDATE` 行锁。
- **禁用 `INSERT OR REPLACE`**：`db_backend` 的方言翻译表里没有它，写出来就是
  "SQLite 能跑、PG 直接报错"的假可用；这里的 upsert 一律 `INSERT OR IGNORE` + `UPDATE`。

**没有开关**（2026-10-21 移除）：写侧恒开，读侧仍按"投影没有这行就回落队列/账本"
的数据边界降级——这条回落与开关无关，所以删开关不改变任何读语义。写失败不再静默：
`PipelineOrchestrator.run_store_health()` 与 `/api/health/deep` 的 `runstore` 组件
会把"库能不能开、结构版本、最近一次失败"报出来。
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Literal

from artesian.fast_json import dumps as _fast_dumps
from artesian.fast_json import loads as _fast_loads

from . import db_backend

SCHEMA_VERSION = 3

#: 本批的形状化写口；其余事实走 `record()`，同一条流、不动投影
RUN_OPENED = "run.opened"
RUN_STARTED = "run.started"
NODE_FINISHED = "node.finished"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
RUN_CANCELLED = "run.cancelled"
#: 事后补账（投影写失败后按交付账本重放）——不是执行事实，必须与原始事件可区分
RUN_REPLAYED = "run.replayed"

#: 终态投影状态：`close_run` 用它做"至多一条终态事件"的原子裁决
TERMINAL_STATUSES = ("done", "failed", "cancelled")

#: 审批是一条可变状态（pending → approved/rejected → consumed），
#: 所以它既有投影又有事件：状态给收件箱读，事件给重放与审计读。
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
APPROVAL_CONSUMED = "approval.consumed"

#: 审批行状态字面量（`approvals.py` 从这里再导出，词汇表只有一份）
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

#: 终态投影状态 → 事件类型
_TERMINAL_EVENT = {"done": RUN_COMPLETED, "failed": RUN_FAILED,
                   "cancelled": RUN_CANCELLED}

_DDL = (
    """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS events (
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts           REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
)
""",
    """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    workflow       TEXT NOT NULL DEFAULT '',
    principal      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'running',
    trigger        TEXT NOT NULL DEFAULT '',
    inputs_json    TEXT NOT NULL DEFAULT '{}',
    started_at     REAL NOT NULL DEFAULT 0,
    finished_at    REAL NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    cost_usd       REAL NOT NULL DEFAULT 0,
    quality_score  REAL NOT NULL DEFAULT 0,
    last_seq       INTEGER NOT NULL DEFAULT 0,
    artifacts_json TEXT NOT NULL DEFAULT '[]'
)
""",
    """
CREATE TABLE IF NOT EXISTS run_nodes (
    run_id             TEXT NOT NULL,
    node               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT '',
    attempts           INTEGER NOT NULL DEFAULT 1,
    started_at         REAL NOT NULL DEFAULT 0,
    finished_at        REAL NOT NULL DEFAULT 0,
    error              TEXT NOT NULL DEFAULT '',
    result_json        TEXT NOT NULL DEFAULT '{}',
    artifact_keys_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, node)
)
""",
    """
CREATE TABLE IF NOT EXISTS approvals (
    request_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    pipeline      TEXT NOT NULL DEFAULT '',
    principal     TEXT NOT NULL DEFAULT '',
    nodes_json    TEXT NOT NULL DEFAULT '[]',
    messages_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    requested_at  REAL NOT NULL DEFAULT 0,
    decided_at    REAL NOT NULL DEFAULT 0,
    decided_by    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    consumed      INTEGER NOT NULL DEFAULT 0
)
""",
    """
CREATE TABLE IF NOT EXISTS schedules (
    name        TEXT PRIMARY KEY,
    pipeline    TEXT NOT NULL DEFAULT '',
    cron        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    next_fire   REAL NOT NULL DEFAULT 0,
    last_fire   REAL NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0
)
""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_runs_principal ON runs(principal, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, finished_at)",
    "CREATE INDEX IF NOT EXISTS idx_run_nodes_run ON run_nodes(run_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(status, requested_at)",
)

class RunStore:
    """运行事实的唯一写入口 + 读接口。

    写方法一律"事件 + 投影"同事务同提交：要么整套落下去，要么什么都没落。
    读侧因此永远看不到"事件在、投影没跟上"的中间态。
    """

    def __init__(self, db_path: str = ""):
        self._pool = db_backend.resolve_backend(db_path, default_name="runs.db")
        self._init_schema()

    @property
    def dialect(self) -> str:
        return self._pool.dialect

    def _conn(self):
        return self._pool.conn()

    def schema_version(self) -> int:
        """库里的结构版本；读不到给 0（健康检查用它识别"库是老的/空的"）。"""
        with contextlib.suppress(Exception):
            row = self._conn().execute(
                "SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0] or 0) if row else 0
        return 0

    def _init_schema(self) -> None:
        conn = self._conn()
        with _Txn(self._pool, conn):
            for ddl in _DDL:
                conn.execute(ddl)
            for sql in _INDEXES:
                conn.execute(sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (?, ?)", (SCHEMA_VERSION, time.time()))

    # ── 写入 ───────────────────────────────────────────────
    def open_run(self, run_id: str, workflow: str = "", principal: str = "",
                 trigger: str = "", inputs: dict | None = None,
                 started_at: float | None = None) -> int:
        """登记一次运行的开始，返回 `run.started` 的 seq。

        投影行幂等（同一 run_id 重复 open 不覆写已有列），但**事件照样追加**：
        重放与续跑本身就是事实，抹掉它等于把"这个运行被重开过"从审计里删了。
        """
        ts = started_at or time.time()
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, workflow, principal, status, "
                "trigger, inputs_json, started_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (run_id, workflow, principal, trigger, _fast_dumps(inputs or {}), ts))
            self._append(conn, run_id, RUN_OPENED,
                         {"workflow": workflow, "principal": principal,
                          "trigger": trigger}, ts, txn)
            return self._append(conn, run_id, RUN_STARTED, {"started_at": ts},
                                ts, txn)

    def record_nodes(self, run_id: str, rows: list[dict]) -> int:
        """一层节点一次事务（§5 的写放大缓解）。

        逐节点各开一次事务是这个模块最容易写错的地方：事务本身没问题，问题在
        `BEGIN IMMEDIATE` 是**整库写锁**——一层 3 个节点就排队 3 回，并发运行时
        纯付锁切换的钱。这里把一批 upsert + 一批事件收进同一个事务。
        """
        if not rows:
            return 0
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            seq = 0
            for row in rows:
                cols = (str(row.get("status") or ""), int(row.get("attempts") or 1),
                        float(row.get("started_at") or 0.0),
                        float(row.get("finished_at") or 0.0),
                        str(row.get("error") or ""),
                        _fast_dumps(row.get("result") or {}),
                        _fast_dumps(row.get("artifact_keys") or []))
                conn.execute(
                    "INSERT OR IGNORE INTO run_nodes (run_id, node, status, attempts, "
                    "started_at, finished_at, error, result_json, artifact_keys_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, str(row.get("node") or "")) + cols)
                conn.execute(
                    "UPDATE run_nodes SET status = ?, attempts = ?, started_at = ?, "
                    "finished_at = ?, error = ?, result_json = ?, artifact_keys_json = ? "
                    "WHERE run_id = ? AND node = ?",
                    cols + (run_id, str(row.get("node") or "")))
                seq = self._append(conn, run_id, NODE_FINISHED,
                                   {"node": row.get("node"), "status": cols[0],
                                    "attempts": cols[1], "error": cols[4]},
                                   cols[3] or time.time(), txn)
            return seq

    def record_node(self, run_id: str, node: str, status: str, *,
                    attempts: int = 1, started_at: float = 0.0,
                    finished_at: float = 0.0, error: str = "",
                    result: Any = None,
                    artifact_keys: list | None = None) -> int:
        """单节点写法（`record_nodes` 的一元特例，留给非层循环的调用方）。"""
        return self.record_nodes(run_id, [{
            "node": node, "status": status, "attempts": attempts,
            "started_at": started_at, "finished_at": finished_at, "error": error,
            "result": result, "artifact_keys": artifact_keys}])

    def close_run(self, run_id: str, status: str, *, finished_at: float = 0.0,
                  error: str = "", cost_usd: float = 0.0,
                  quality_score: float = 0.0,
                  artifacts: list | None = None) -> int | None:
        """终态：投影盖章 + 一条终态事件，同一事务。返回事件 seq（没写则 None）。

        **"每个运行至多一条终态事件"由这里的条件 UPDATE 原子裁决**：已经是终态的行
        不再被覆写、也不再追加事件。此前这条约束写在调用方（`_claim_terminal_event`），
        那是**进程内**的去重——补账、重启后的重放、两个进程同时收尾都绕得过它；而事件流
        一旦多出一条终态，SSE 重放与审计时间线就多一个无法解释的节点。

        投影行不存在时同样返回 None（`UPDATE` 影响 0 行）：没有行却有终态事件，
        正是补账要消灭的那种半条事实。

        收尾同时**结清该运行的待批**（rejected + by=system + reason，与
        `pipeline.cancel` 的 `_retire_pending_approvals` 同语义、同幂等守卫）：
        cancel 会先结再走这里（守卫使其 0 行），backfill/崩溃恢复路径没有
        cancel 那一步，这里补上——否则收件箱会留一条点不掉的待办（实证 A6）。
        结清同时置 `consumed = 1`（与 `consume_approval` 同语义）：系统驳回不是
        给执行器的指令，不留消费位会让同 run_id 的新执行经 `open_decision`
        把这条旧裁决当闸门信号吃掉，闸门永久武装不上。
        """
        ts = finished_at or time.time()
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            cur = conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, "
                "cost_usd = ?, quality_score = ?, artifacts_json = ? "
                "WHERE run_id = ? AND status NOT IN (?, ?, ?)",
                (status, ts, error, float(cost_usd or 0), float(quality_score or 0),
                 _fast_dumps(artifacts or []), run_id) + TERMINAL_STATUSES)
            if cur.rowcount <= 0:
                return None
            # 结清待批：与 cancel 的 _retire_pending_approvals 同语义。
            # `WHERE status = ?` 是幂等守卫——cancel 已结过的行这里 0 行，
            # approval.decided 事件也不会二发（与 decide_approval 同一守卫）。
            # 同一 UPDATE 置 consumed = 1：系统驳回不留消费位，会被同 run_id
            # 的新执行经 open_decision 当闸门裁决吃掉（与 consume_approval 同语义）。
            pending = conn.execute(
                "SELECT request_id, nodes_json FROM approvals "
                "WHERE run_id = ? AND status = ?", (run_id, PENDING)).fetchall()
            for request_id, nodes_json in pending:
                conn.execute(
                    "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
                    "note = ?, reason = ?, consumed = 1 "
                    "WHERE request_id = ? AND status = ?",
                    (REJECTED, ts, "system", "", f"运行已收尾（{status}）",
                     request_id, PENDING))
                self._append(
                    conn, run_id, APPROVAL_DECIDED,
                    {"request_id": request_id, "decision": REJECTED,
                     "by": "system", "note": "", "reason": f"运行已收尾（{status}）",
                     "nodes": _fast_loads(nodes_json) or []}, ts, txn)
                self._append(
                    conn, run_id, APPROVAL_CONSUMED,
                    {"request_id": request_id}, ts, txn)
            return self._append(conn, run_id,
                                _TERMINAL_EVENT.get(status, RUN_COMPLETED),
                                {"status": status, "error": error}, ts, txn)

    def record(self, run_id: str, type: str, payload: dict | None = None,
               ts: float | None = None) -> int:
        """不属于上述三种形状的事实（条件跳过……）：只追加事件，不动投影。

        Stage 3 把审批升成了投影（`open_approval` 等，见下）——它是可变状态，
        收件箱要按 status 查；其余仍在"只进事件流"这一档，免得再添一面总线。
        """
        with _Txn(self._pool, self._conn()) as txn:
            return self._append(txn.conn, run_id, type, payload or {},
                                ts or time.time(), txn)

    # ── 审批：投影 + 事件同事务（Stage 3a 起审批的唯一写口）──────

    def open_approval(self, run_id: str, request_id: str, pipeline: str = "",
                      nodes: list | None = None, messages: dict | None = None,
                      principal: str = "", ts: float | None = None) -> int:
        """登记一次待批：投影行 + `approval.requested`，同一事务。"""
        stamp = ts or time.time()
        node_list = list(nodes or [])
        message_map = dict(messages or {})
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            conn.execute(
                "INSERT INTO approvals (request_id, run_id, pipeline, principal, "
                "nodes_json, messages_json, status, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, run_id, pipeline, principal or "",
                 _fast_dumps(node_list), _fast_dumps(message_map), PENDING, stamp))
            return self._append(
                conn, run_id, APPROVAL_REQUESTED,
                {"request_id": request_id, "nodes": node_list,
                 "messages": message_map, "pipeline": pipeline,
                 "principal": principal or ""}, stamp, txn)

    def decide_approval(self, request_id: str, decision: str, by: str = "",
                        note: str = "", reason: str = "",
                        ts: float | None = None) -> dict | None:
        """作出决定。只有 pending 行可被决策（幂等：第二次给 None）。

        返回 None 的含义是"这个决定没落下去"——调用方必须据此拒绝放行，
        不能声称已批准。返回的 `decided_at` 是这次决定的**唯一时间源**：
        审计、事件、内存任务历史都读它。此前是"台账自己取一次 time.time()、
        调用方再取一次"，同一个决定因此有两个时刻（差几十微秒到几秒，
        取决于台账有没有排队），对账时只能靠容差糊过去——现在没得糊。

        幂等守卫只有一条：那句 `WHERE ... AND status = ?` 加 `rowcount`。
        先在 Python 里读一次 status 再判是重复的守卫（读到的值到 UPDATE 时
        已被 `BEGIN IMMEDIATE` 锁住，两种写法同一个事务）——留两条就会有一条
        是死的，而变异测试证明死的是哪条只取决于谁真的挡住写入。
        """
        stamp = ts or time.time()
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            row = conn.execute(
                "SELECT run_id, nodes_json FROM approvals "
                "WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
                "note = ?, reason = ? WHERE request_id = ? AND status = ?",
                (decision, stamp, by or "", note or "", reason or "",
                 request_id, PENDING))
            if cur.rowcount <= 0:
                return None
            seq = self._append(
                conn, str(row[0]), APPROVAL_DECIDED,
                {"request_id": request_id, "decision": decision,
                 "by": by or "", "note": note or "", "reason": reason or "",
                 "nodes": _fast_loads(row[1]) or []}, stamp, txn)
            return {"request_id": request_id, "run_id": str(row[0]),
                    "status": decision, "decided_at": stamp, "decided_by": by or "",
                    "note": note or "", "reason": reason or "",
                    "nodes": list(_fast_loads(row[1]) or []), "seq": seq}

    def consume_approval(self, request_id: str) -> int:
        """标记决定已被执行器消费（续跑越过闸门后调用）。"""
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            row = conn.execute("SELECT run_id FROM approvals WHERE request_id = ?",
                               (request_id,)).fetchone()
            if row is None:
                return 0
            conn.execute("UPDATE approvals SET consumed = 1 WHERE request_id = ?",
                         (request_id,))
            return self._append(conn, str(row[0]), APPROVAL_CONSUMED,
                                {"request_id": request_id}, time.time(), txn)

    def _append(self, conn, run_id: str, type: str, payload: dict,
                ts: float, txn: _Txn) -> int:
        """事务内取号并追加。见模块头对 `seq` 的说明。

        注意 PG 上的行锁顺序：必须先 ``INSERT OR IGNORE`` 让行存在，
        再 ``acquire_seq_lock``（``SELECT ... FOR UPDATE``）。否则对
        不存在的行 ``FOR UPDATE`` 锁不住任何东西，并发下 seq 仍会撞 PK。
        """
        conn.execute("INSERT OR IGNORE INTO runs (run_id) VALUES (?)", (run_id,))
        txn.acquire_seq_lock(run_id)
        row = conn.execute("SELECT MAX(seq) FROM events WHERE run_id = ?",
                           (run_id,)).fetchone()
        seq = int(row[0] if row and row[0] is not None else 0) + 1
        conn.execute(
            "INSERT INTO events (run_id, seq, type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, type, _fast_dumps(payload), ts))
        conn.execute("UPDATE runs SET last_seq = ? "
                     "WHERE run_id = ? AND last_seq < ?", (seq, run_id, seq))
        return seq

    # ── 读取 ───────────────────────────────────────────────
    def get_run(self, run_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT run_id, workflow, principal, status, trigger, inputs_json, "
            "started_at, finished_at, error, cost_usd, quality_score, last_seq, "
            "artifacts_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "workflow": row[1], "principal": row[2],
                "status": row[3], "trigger": row[4],
                "inputs": _fast_loads(row[5]) or {}, "started_at": row[6],
                "finished_at": row[7], "error": row[8], "cost_usd": row[9],
                "quality_score": row[10], "last_seq": row[11],
                "artifacts": _fast_loads(row[12]) or []}

    def nodes(self, run_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT node, status, attempts, started_at, finished_at, error, "
            "result_json, artifact_keys_json FROM run_nodes WHERE run_id = ? "
            "ORDER BY node", (run_id,)).fetchall()
        return [{"node": r[0], "status": r[1], "attempts": r[2],
                 "started_at": r[3], "finished_at": r[4], "error": r[5],
                 "result": _fast_loads(r[6]) or {},
                 "artifact_keys": _fast_loads(r[7]) or []} for r in rows]

    def events(self, run_id: str, after_seq: int = 0,
               limit: int = 1000) -> list[dict]:
        rows = self._conn().execute(
            "SELECT seq, type, payload_json, ts FROM events "
            "WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, int(after_seq), int(limit))).fetchall()
        return [{"seq": r[0], "type": r[1], "payload": _fast_loads(r[2]) or {},
                 "ts": r[3]} for r in rows]

    def list_runs(self, principal: str | None = None,
                  limit: int = 100) -> list[dict]:
        """作用域读法：`principal=None` 给全量（admin 面），给了只回自己名下。"""
        cols = ("SELECT run_id, workflow, principal, status, started_at, finished_at, "
                "error ")
        conn = self._conn()
        if principal is None:
            rows = conn.execute(cols + "FROM runs ORDER BY started_at DESC LIMIT ?",
                                (int(limit),)).fetchall()
        else:
            rows = conn.execute(cols + "FROM runs WHERE principal = ? "
                                "ORDER BY started_at DESC LIMIT ?",
                                (principal, int(limit))).fetchall()
        return [{"run_id": r[0], "workflow": r[1], "principal": r[2],
                 "status": r[3], "started_at": r[4], "finished_at": r[5],
                 "error": r[6] or ""}
                for r in rows]

    # ── 审批读口 ──────────────────────────────────────
    # 列一律显式列出、按位置建字典：`SELECT *` + `sqlite3.Row` 是 approvals.db
    # 老实现里唯一依赖 SQLite 的地方（PG 给的是元组），这里不把它带进来。
    _APPROVAL_COLS = (
        "SELECT request_id, run_id, pipeline, principal, nodes_json, messages_json, "
        "status, requested_at, decided_at, decided_by, note, reason, consumed ")

    @staticmethod
    def _approval_from_row(r: Any) -> dict:
        return {"request_id": r[0], "run_id": r[1], "pipeline": r[2],
                 "principal": r[3], "nodes": list(_fast_loads(r[4]) or []),
                 "messages": dict(_fast_loads(r[5]) or {}), "status": r[6],
                 "requested_at": r[7], "decided_at": r[8], "decided_by": r[9],
                 "note": r[10], "reason": r[11], "consumed": int(r[12] or 0)}

    def get_approval(self, request_id: str) -> dict | None:
        row = self._conn().execute(
            self._APPROVAL_COLS + "FROM approvals WHERE request_id = ?",
            (request_id,)).fetchone()
        return self._approval_from_row(row) if row else None

    def pending_approval(self, run_id: str) -> dict | None:
        """该运行当前**唯一**的待批行（没有则 None）。"""
        row = self._conn().execute(
            self._APPROVAL_COLS + "FROM approvals WHERE run_id = ? AND status = ? "
            "ORDER BY requested_at DESC LIMIT 1", (run_id, PENDING)).fetchone()
        return self._approval_from_row(row) if row else None

    def open_approvals(self, run_id: str) -> list[dict]:
        """该运行已作出但尚未消费的决定（新→旧），节点集合的匹配由调用方判。"""
        rows = self._conn().execute(
            self._APPROVAL_COLS + "FROM approvals WHERE run_id = ? "
            "AND status IN (?, ?) AND consumed = 0 ORDER BY decided_at DESC",
            (run_id, APPROVED, REJECTED)).fetchall()
        return [self._approval_from_row(r) for r in rows]

    def approvals_for_run(self, run_id: str) -> list[dict]:
        """该运行的全部审批历史（新→旧），供详情页/对账。"""
        rows = self._conn().execute(
            self._APPROVAL_COLS + "FROM approvals WHERE run_id = ? "
            "ORDER BY requested_at DESC", (run_id,)).fetchall()
        return [self._approval_from_row(r) for r in rows]

    def list_pending_approvals(self, principal: str | None = None,
                               limit: int = 200) -> list[dict]:
        """待批收件箱。`principal=None` 不过滤（admin 面），给了只看自己名下。"""
        sql = self._APPROVAL_COLS + "FROM approvals WHERE status = ?"
        args: list[Any] = [PENDING]
        if principal is not None:
            sql += " AND principal = ?"
            args.append(principal)
        sql += " ORDER BY requested_at ASC LIMIT ?"
        args.append(int(limit))
        rows = self._conn().execute(sql, args).fetchall()
        return [self._approval_from_row(r) for r in rows]

    # ── 维护：保留期清理（A6：RunStore 此前也无界）───────────
    def cleanup(self, retention_days: int = 0, max_runs: int = 0,
                now: float | None = None) -> dict[str, int | list[str]]:
        """删除过期运行：`runs`/`run_nodes`/`events`/`approvals` 一次删干净。

        只碰**终态**（done/failed/cancelled）的运行——进行中的行一行都不碰
        （把活运行从观测面抹掉，比不删更糟，且它还在写投影）。判删两条：

        - `retention_days` > 0：`finished_at` 早于 `now - retention_days` 的
          删（年龄判据，0 = 不启用）；
        - `max_runs` > 0：按 `finished_at` 新→旧排序后，超出最近 N 条的删
          （数量判据，0 = 不启用）。

        两者同时启用时，年龄判据对**保留集内**的行也生效（一条 90 天前的
        行哪怕在最近 500 条里也删）。删的是 run_id 集合联动删——
        `events`/`run_nodes`/`approvals` 里挂着同一 run 的行必须一起走，
        否则留下"运行没了、事件还在"的半条事实（§3 的边界句说的正是这种
        形状）。两次都不启用返回全 0（幂等，也方便健康检查区分"没启用"和
        "跑了但没删到"）。
        返回 dict 的 `run_ids` 是**被删 run 的集合**：cost.db / quality.db 与
        runs.db 是各自的库、无 FK，调用方（maintenance.gc_runs）拿它做跨库
        联动删，不留指向已删运行的悬空行。
        """
        stamp = time.time() if now is None else now
        cutoff = stamp - retention_days * 86400.0
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            rows = conn.execute(
                "SELECT run_id, finished_at FROM runs "
                "WHERE status IN (?, ?, ?) ORDER BY finished_at DESC",
                TERMINAL_STATUSES).fetchall()
            # 判删是两条判据的并集：超出最近 N 条 + 年龄过旧（含保留集内）。
            to_delete: list[str] = []
            if max_runs > 0:
                to_delete += [r[0] for r in rows[max_runs:]]
            if retention_days > 0:
                to_delete += [r[0] for r in rows
                              if float(r[1] or 0) < cutoff]
            to_delete = sorted(set(to_delete))
            if not to_delete:
                return {"runs": 0, "run_nodes": 0, "events": 0, "approvals": 0,
                        "run_ids": []}
            # 逐行 DELETE 而非 executemany：psycopg3 的 Connection 没有
            # executemany（只有 Cursor 有），而 _Conn 包装的 execute 在两边
            # 都能用。参数化占位符 ?→%s 由方言翻译层处理，SQL 文本零插值
            # （bandit B608 的判定点就是 f-string 拼查询）。
            n_nodes = n_events = n_approvals = n_runs = 0
            for rid in to_delete:
                n_nodes += conn.execute(
                    "DELETE FROM run_nodes WHERE run_id = ?", (rid,)).rowcount
                n_events += conn.execute(
                    "DELETE FROM events WHERE run_id = ?", (rid,)).rowcount
                n_approvals += conn.execute(
                    "DELETE FROM approvals WHERE run_id = ?", (rid,)).rowcount
                n_runs += conn.execute(
                    "DELETE FROM runs WHERE run_id = ?", (rid,)).rowcount
        return {"runs": n_runs, "run_nodes": n_nodes, "events": n_events,
                "approvals": n_approvals, "run_ids": to_delete}

    # ── schedules：触发租约（两进程共享一库只允许一个触发者）──────
    # cron 触发此前只有进程内的 tick 循环，两个 `loom --triggers` 指向同一状态
    # 目录会把同一窗口各提交一遍。租约和队列租约是同一套语义：条件 UPDATE +
    # rowcount 原子裁决（SQLite 侧 BEGIN IMMEDIATE 排队，PG 侧天然串行），
    # 不引入新锁机制，也就不需要新的变异面。

    def register_schedule(self, name: str, pipeline: str = "", cron: str = "",
                          enabled: bool = True) -> None:
        """触发配置进表（幂等）：新名字插入；老名字只更新配置列，租约列不动。

        crontab 改了就该覆盖；但 `UPDATE` 若把租约列一并写掉，轮换配置的瞬间
        会把别的进程正持有的租约清空——两个触发者就有两个了。
        """
        with _Txn(self._pool, self._conn()) as txn:
            conn = txn.conn
            conn.execute(
                "INSERT OR IGNORE INTO schedules (name, pipeline, cron, enabled) "
                "VALUES (?, ?, ?, ?)",
                (name, pipeline, cron, int(bool(enabled))))
            conn.execute(
                "UPDATE schedules SET pipeline = ?, cron = ?, enabled = ? "
                "WHERE name = ?", (pipeline, cron, int(bool(enabled)), name))

    def acquire_schedule_lease(self, name: str, owner: str, ttl: float,
                               now: float | None = None) -> bool:
        """抢/续租约。到期、无人持有或本人持有三种情形才成功，其余 False。

        同一个条件 UPDATE 承担两件事：跨进程的互斥（新持有者必须等旧租约
        过期）和本进程的续租（持有者每轮把 `lease_until` 推远 TTL）。行不存在
        返回 False——没登记过的触发不该被"顺手"建出来，登记是配置侧的事。
        """
        stamp = time.time() if now is None else now
        with _Txn(self._pool, self._conn()) as txn:
            cur = txn.conn.execute(
                "UPDATE schedules SET lease_owner = ?, lease_until = ? "
                "WHERE name = ? AND (lease_until <= ? OR lease_owner = ?)",
                (owner, stamp + float(ttl), name, stamp, owner))
            return bool(cur.rowcount > 0)

    def release_schedule_lease(self, name: str, owner: str) -> bool:
        """主动放掉本人持有的租约（优雅退出时调用；只放自己的，别人的不动）。"""
        with _Txn(self._pool, self._conn()) as txn:
            cur = txn.conn.execute(
                "UPDATE schedules SET lease_owner = '', lease_until = 0 "
                "WHERE name = ? AND lease_owner = ?", (name, owner))
            return bool(cur.rowcount > 0)

    def record_schedule_fire(self, name: str, fired_at: float,
                             next_fire: float) -> None:
        """记一次已发生的触发（last_fire/next_fire 是观测面，不参与裁决）。"""
        with _Txn(self._pool, self._conn()) as txn:
            txn.conn.execute(
                "UPDATE schedules SET last_fire = ?, next_fire = ? "
                "WHERE name = ?", (float(fired_at), float(next_fire), name))

    def list_schedules(self) -> list[dict]:
        """全部排程（按名字），供观测面与排障；列显式列出，不依赖 sqlite3.Row。"""
        rows = self._conn().execute(
            "SELECT name, pipeline, cron, enabled, next_fire, last_fire, "
            "lease_owner, lease_until FROM schedules ORDER BY name").fetchall()
        return [{"name": r[0], "pipeline": r[1], "cron": r[2],
                 "enabled": bool(r[3]), "next_fire": r[4], "last_fire": r[5],
                 "lease_owner": r[6], "lease_until": r[7]} for r in rows]


class _Txn:
    """`with _Txn(pool, conn):` —— 成功提交、异常回滚；SQLite 侧提前拿写锁。

    为什么 SQLite 要自己发 `BEGIN IMMEDIATE`：python-sqlite3 的隐式事务只在 **DML**
    之前开启，而取号是 `SELECT MAX(seq)`——读操作不触发 BEGIN，两个进程就可能读到
    同一个 `MAX` 再各自 +1，然后撞主键（或被 busy_timeout 挡下，取决于时序）。
    IMMEDIATE 让"取号 + 插入"整体排队。

    非 SQLite 方言不能照抄这句（`BEGIN IMMEDIATE` 是 SQLite 语法），所以走驱动自带的
    `with conn:` 事务边界 + `SELECT ... FOR UPDATE` 行锁。
    """

    def __init__(self, pool, conn):
        self._pool = pool
        self.conn = conn
        self._sqlite = getattr(pool, "dialect", "sqlite") == "sqlite"

    def __enter__(self) -> _Txn:
        if self._sqlite:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.__enter__()
        return self

    def acquire_seq_lock(self, run_id: str) -> None:
        """取号前的写锁：SQLite 已在 `BEGIN IMMEDIATE` 里，这里只处理其余方言。"""
        if self._sqlite:
            return
        self.conn.execute("SELECT 1 FROM runs WHERE run_id = ? FOR UPDATE", (run_id,))

    def __exit__(self, *exc) -> Literal[False]:
        """永远返回 False：本上下文管理器不吞异常。

        标注成 `bool` 会让 mypy 认为"异常可能被咽掉、控制流落到 with 之后"，
        于是每个 `with _Txn(...): return …` 的写口都报 Missing return——
        而这里确实不该咽异常（提交失败必须让调用方看到）。
        """
        if self._sqlite:
            if exc[0] is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        else:
            self.conn.__exit__(*exc)
        return False
