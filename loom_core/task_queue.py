"""TaskQueue — SQLite 持久化任务队列

解决核心问题：进程重启后内存任务丢失。

特性：
  - 任务持久化（SQLite WAL，与 message_store 同模式）
  - 原子 acquire/release（多 worker 安全）
  - 自动恢复：重启时 running → pending
  - 任务历史查询

用法：
    q = TaskQueue("bus_data/tasks.db")
    q.submit("task-001", "docgen", "input.md", {"key": "val"})
    task = q.acquire(worker_id="w1")      # 原子出队
    q.complete("task-001")           # 完成（结果正文不在队列，见 run_nodes 投影）
    q.recover()                            # 重启恢复
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from artesian.fast_json import dumps as _fast_dumps
from artesian.fast_json import loads as _fast_loads

from .db_backend import resolve_backend

#: `get()` / `list_all()` 的行形状，列顺序与 `_row_to_dict` 严格一一对应。
#: 这份清单原先在三个查询里各抄一遍——Stage 3b 把 `result_json` 换成 `projection`
#: 时任何一处漏改都会**静默**把错位的结果交给调用方（列是按位置取的），所以收成常量。
_ROW_COLS = (
    "SELECT task_id, pipeline_name, input_file, status, config_json, "
    "COALESCE(projection, 'ok') AS projection, error, created_at, started_at, "
    "finished_at, worker_id "
    "FROM task_queue"
)

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(Path(__file__).parent.parent.absolute(), "bus_data", "tasks.db")


def _pid_alive(pid: int) -> bool:
    """跨平台进程存活探测（Windows 上禁用 os.kill(pid, 0)：它会 TerminateProcess）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid & 0xFFFFFFFF)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class TaskQueue:
    """SQLite 持久化任务队列（线程安全，WAL 模式）"""

    def __init__(self, db_path: str = ""):
        # 后端工厂：db_path 为空读 LOOM_DATABASE_URL（旧名回退），仍空落
        # SQLite 默认路径——SQLite 场景行为零变化（WAL/忙等重试由
        # db_backend.SqlitePool 负责）。
        self._backend = resolve_backend(db_path, "tasks.db")
        self._lock = threading.Lock()
        # thread-local 连接缓存 / 弱引用登记 / 自愈逻辑全部在 pool（P1-B 抽象），
        # 业务侧只拿 pool.conn()。
        self._init_db()

    def _get_conn(self):
        """获取当前线程缓存的连接（复用；失效自愈）。方言翻译在包装层。"""
        return self._backend.conn()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_queue (
                    task_id       TEXT PRIMARY KEY,
                    pipeline_name TEXT NOT NULL,
                    input_file    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    config_json   TEXT DEFAULT '{}',
                    projection    TEXT DEFAULT 'ok',
                    error         TEXT DEFAULT '',
                    created_at    REAL NOT NULL,
                    started_at    REAL DEFAULT 0,
                    finished_at   REAL DEFAULT 0,
                    worker_id     TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON task_queue(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON task_queue(created_at)")
            # 幂等加列：并发首建时 check-then-act 会撞 "duplicate column name"，
            # 目标状态已达成，不算故障（同 sqlite_util.add_column_if_missing）。
            self._backend.add_column_if_missing(
                conn, "task_queue", "owner_pid", "INTEGER DEFAULT 0")
            # token 身份（spec §5.4）：任务归属的 Principal.id（""=无身份世界）
            self._backend.add_column_if_missing(
                conn, "task_queue", "principal", "TEXT DEFAULT ''")
            # 续跑标记（2026-10-20 审批跨重启）：批准一个"进程已死"的闸门任务时
            # 把行放回 pending 并置 1，worker 据此走 run_plan(resume=True)。
            self._backend.add_column_if_missing(
                conn, "task_queue", "resume", "INTEGER DEFAULT 0")
            # 投影健康位（Stage 3b，2026-10-24）：这次运行的 RunStore 投影到底落成没有。
            # `ok` = 投影写了；`incomplete` = 想写但写失败（要补账）；`legacy` = 走的是
            # 从不写投影的 legacy 路径（按设计缺，不是故障）。队列是投影写不进去时
            # **唯一还活着的一行**，所以这个标记只能住在这里。老库里的行补列后按 'ok'
            # 读（COALESCE），它们的真实历史仍由 deliveries/投影各自回答，不回填。
            self._backend.add_column_if_missing(
                conn, "task_queue", "projection", "TEXT DEFAULT 'ok'")

    def submit(self, task_id: str, pipeline_name: str, input_file: str,
               config: dict = None, principal: str = "") -> bool:
        """入队新任务。已存在则忽略（幂等）。principal = 提交者身份（可空）。"""
        with self._lock, self._get_conn() as conn:
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO task_queue "
                    "(task_id, pipeline_name, input_file, status, config_json, created_at, principal) "
                    "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                    (task_id, pipeline_name, input_file,
                     _fast_dumps(config or {}), time.time(), principal or ""),
                )
                # 修复 P0 回归：原用 conn.total_changes > 0 判断是否插入，
                # 但连接复用（threading.local 缓存）后 total_changes 累积历史变更，
                # 导致幂等重复 submit 仍返回 True。
                # 改用 cursor.rowcount：仅反映本次 INSERT 的行数（0=被 IGNORE，1=成功插入）。
                return bool(cursor.rowcount > 0)
            except sqlite3.IntegrityError:
                return False

    def acquire(self, worker_id: str = "") -> dict | None:
        """原子出队：取一条 pending 任务，标记为 running。

        多 worker 安全：SQLite 事务保证同一任务不会被两个 worker acquire。

        **抢输竞争 ≠ 没活**：`UPDATE ... AND status='pending'` 返回 rowcount=0
        只说明"这一条被别的 worker 先拿了"，队列里可能还有别的 pending。旧实现在
        这里直接 `return None`，调用方读到的是"队列空了"——齐射场景实测 12 进程
        抢 6 条任务，11/15 轮有任务没人认领（P7 的多进程判据在全量负载下偶发转红，
        根因即此）。现在改为**重试下一条**：rollback 结束本事务拿新快照，再判一次。
        推进性：每轮要么认领成功、要么把已被抢走的那条从候选里排除，有限步内终止
        （队列真空时 SELECT 无行 → 返回 None）。
        """
        with self._lock, self._get_conn() as conn:
            while True:
                row = conn.execute(
                    "SELECT task_id, pipeline_name, input_file, config_json, principal, "
                    "COALESCE(resume, 0) AS resume "
                    "FROM task_queue WHERE status = 'pending' "
                    "ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                task_id, pipeline_name, input_file, config_json, principal, resume = row
                now = time.time()
                cursor = conn.execute(
                    "UPDATE task_queue SET status = 'running', started_at = ?, worker_id = ?, "
                    "owner_pid = ? WHERE task_id = ? AND status = 'pending'",
                    (now, worker_id, os.getpid(), task_id),
                )
                # rowcount 而非 total_changes：连接复用后 total_changes 累积历史
                # 变更、永远 > 0，会把"被别人抢走"误判成"认领成功"（此前的 P0 回归）。
                if cursor.rowcount == 0:
                    # 该行已被别人改成 running，本事务快照已过期；必须回滚，
                    # 否则下一轮 SELECT 仍读旧快照、会在同一条上打转。
                    conn.rollback()
                    continue
                return {
                    "task_id": task_id,
                    "pipeline_name": pipeline_name,
                    "input_file": input_file,
                    "config": _fast_loads(config_json) if config_json else {},
                    "principal": principal or "",
                    "resume": bool(resume),
                }

    def complete(self, task_id: str, error: str = "") -> None:
        """标记任务完成或失败。

        Stage 3b 起不再收 `result`：节点结果正文的事实源是 RunStore 的
        `run_nodes.result_json`（以及本批之前的 `deliveries.payload_json`），
        队列这一列此前是**第三份拷贝**，而实测全仓没有任何读者——`RunHandle` 与
        `_queue_row` 只用 `status`。留着它只多付一次整份结果的写入（§5 警告的写放大）。
        """
        with self._lock, self._get_conn() as conn:
            status = "failed" if error else "done"
            conn.execute(
                "UPDATE task_queue SET status = ?, error = ?, finished_at = ? "
                "WHERE task_id = ?",
                (status, error, time.time(), task_id),
            )

    def cancel(self, task_id: str):
        """取消任务"""
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "UPDATE task_queue SET status = 'cancelled', finished_at = ? "
                "WHERE task_id = ? AND status IN ('pending', 'running')",
                (time.time(), task_id),
            )

    def claim_direct(self, task_id: str) -> bool:
        """直跑（run_plan 不经 worker）也占住队列行：记 owner_pid，别人不得重排。"""
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE task_queue SET status = 'running', owner_pid = ?, started_at = ? "
                "WHERE task_id = ? AND status = 'pending'",
                (os.getpid(), time.time(), task_id))
            return bool(cur.rowcount > 0)

    def mark_for_resume(self, task_id: str) -> bool:
        """把一行放回 pending 并置续跑标记（审批跨重启：批准一个进程已死的闸门）。

        接受 running/pending：进程被杀死时行停在 running（acquire 后没有收尾），
        闸门挂起并不会写终态。返回是否真的改了行（False = 行不存在或已是终态）。
        """
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT status, COALESCE(owner_pid, 0) FROM task_queue WHERE task_id = ?",
                (task_id,)).fetchone()
            if not row:
                return False
            status, owner_pid = row
            # 活着的 owner 还在跑：不能靠"别的进程内存里没有这个任务"就重排，
            # 否则同一任务会被两个执行者同时推进（2026-10-20 复核确认）。
            if (status == "running" and owner_pid
                    and int(owner_pid) not in (0, os.getpid())
                    and _pid_alive(int(owner_pid))):
                return False
            if status not in ("running", "pending"):
                return False
            cur = conn.execute(
                "UPDATE task_queue SET status = 'pending', worker_id = '', "
                "owner_pid = 0, started_at = 0, error = '', resume = 1, "
                "finished_at = 0 WHERE task_id = ? AND status IN ('running', 'pending')",
                (task_id,),
            )
            return bool(cur.rowcount > 0)

    def clear_resume(self, task_id: str) -> None:
        """worker 认领后清掉续跑标记——重试/重跑时不该再复用旧断点。"""
        with self._lock, self._get_conn() as conn:
            conn.execute("UPDATE task_queue SET resume = 0 WHERE task_id = ?",
                         (task_id,))

    def update_status(self, task_id: str, status: str, error: str = "") -> None:
        """通用状态更新（`error` 非空或状态进终态时一并盖章时间）。"""
        with self._lock, self._get_conn() as conn:
            if error or status in ("done", "failed", "cancelled"):
                conn.execute(
                    "UPDATE task_queue SET status = ?, error = ?, finished_at = ? "
                    "WHERE task_id = ?",
                    (status, error,
                     time.time() if status in ("done", "failed", "cancelled") else 0,
                     task_id),
                )
            else:
                conn.execute(
                    "UPDATE task_queue SET status = ? WHERE task_id = ?",
                    (status, task_id),
                )

    def set_projection(self, task_id: str, state: str) -> bool:
        """把这次运行的投影健康位写成 `ok` / `incomplete` / `legacy`。

        调用顺序是**先悲观后升格**：终态写入时先记 `incomplete`，投影确认落地后才
        升成 `ok`。反过来（先写 ok 再写投影）会留一个"队列说一切正常、投影里查无
        此运行"的窗口——而那正是这批要消灭的静默缺口。
        """
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE task_queue SET projection = ? WHERE task_id = ?",
                (state, task_id))
            return bool(cur.rowcount > 0)

    def count_projection(self, state: str) -> int:
        """按投影健康位统计行数（`run_store_health()` 用它报"有几条运行缺投影"）。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_queue WHERE COALESCE(projection, 'ok') = ?",
                (state,)).fetchone()
        return int(row[0]) if row else 0

    def list_by_projection(self, state: str, limit: int = 200) -> list[dict]:
        """按健康位取行（补账工具用它找 `incomplete` 的那批），按完成时间从旧到新。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                f"{_ROW_COLS} WHERE COALESCE(projection, 'ok') = ? "
                "ORDER BY COALESCE(finished_at, created_at) LIMIT ?",
                (state, int(limit))).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def finish(self, task_id: str, status: str, error: str = "",
               allow_states: tuple = ("running", "pending"),
               projection: str = "ok") -> bool:
        """写终态，但只在当前状态属于 allow_states 时才写。返回是否真的写了。

        为什么需要它：`update_status` 是无条件覆写，而 worker 化之后"用户取消"
        与"任务跑完"会并发发生——API 把行改成 cancelled，执行中的流水线收尾时
        会把同一行又写回 done，取消凭空消失。终态只应被写一次。

        不拼 IN 子句（bandit B608）：先读当前状态，再用常量 SQL 把那个观测值写进
        WHERE。观测值参与条件让"读到 cancelled 之后才被改"的竞态仍然安全——
        状态一变 rowcount 就归 0，不会覆写。

        `projection` 一起写进行里（见 `set_projection` 的先悲观后升格）；结果正文
        自 Stage 3b 起不再落这张表。
        """
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM task_queue WHERE task_id = ?", (task_id,),
            ).fetchone()
            if not row or row[0] not in allow_states:
                logger.info(
                    f"finish 跳过 task_id={task_id}：当前状态 {row[0] if row else '不存在'}"
                    f" 不在允许集合 {allow_states} 内（可能已被取消）")
                return False
            cursor = conn.execute(
                "UPDATE task_queue SET status = ?, error = ?, finished_at = ?, "
                "projection = ? WHERE task_id = ? AND status = ?",
                (status, error, time.time(), projection, task_id, row[0]),
            )
            return bool(cursor.rowcount > 0)

    def recover(self, stale_seconds: float | None = None) -> list[dict]:
        """重启恢复：把 running 状态的任务改回 pending。

        Args:
            stale_seconds: 陈旧阈值（秒）。

        - None（默认）：旧行为，回收全部 running 任务（单进程场景兼容）。
        - 传入数值：多进程共享 tasks.db 安全模式，仅回收同时满足以下条件的任务：
            1. started_at 早于 now - stale_seconds（新鲜的 running 视为仍在执行）；
            2. owner_pid 为空 / 本进程 pid / 已死亡的外部进程
              （存活外部进程正在执行的任务跳过，避免翻回 pending 导致双执行）。

        返回被恢复的任务列表，供调用方决定是否重新执行。
        """
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT task_id, pipeline_name, input_file, config_json, "
                "started_at, owner_pid FROM task_queue WHERE status = 'running'"
            ).fetchall()
            if not rows:
                return []
            my_pid = os.getpid()
            now = time.time()
            recovered = []
            skipped = 0
            for task_id, pipeline_name, input_file, config_json, started_at, owner_pid in rows:
                if stale_seconds is not None:
                    owner_pid = int(owner_pid) if owner_pid else 0
                    if owner_pid and owner_pid != my_pid and _pid_alive(owner_pid):
                        skipped += 1
                        continue
                    if started_at and now - started_at <= stale_seconds:
                        skipped += 1
                        continue
                conn.execute(
                    "UPDATE task_queue SET status = 'pending', worker_id = '' WHERE task_id = ?",
                    (task_id,),
                )
                recovered.append({
                    "task_id": task_id,
                    "pipeline_name": pipeline_name,
                    "input_file": input_file,
                    "config": _fast_loads(config_json) if config_json else {},
                })
            if skipped:
                logger.info(f"recover 跳过 {skipped} 个活跃/新鲜 running 任务")
            logger.info(f"恢复 {len(recovered)} 个中断任务")
            return recovered

    def get(self, task_id: str) -> dict | None:
        """查询单个任务"""
        with self._get_conn() as conn:
            row = conn.execute(_ROW_COLS + " WHERE task_id = ?",
                               (task_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_all(self, status: str = None, limit: int = 100) -> list[dict]:
        """列出任务（可按状态过滤）"""
        with self._get_conn() as conn:
            if status:
                rows = conn.execute(
                    _ROW_COLS + " WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    _ROW_COLS + " ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def owner_pids(self) -> dict[str, int]:
        """task_id → owner_pid 全量映射（只读）。

        list_all() 的行形状（_ROW_COLS）不含 owner_pid；对账报告（A7）要按
        owner_pid 判存活，单独读这一列，避免为了一列改全量行形状牵连所有
        消费者。
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT task_id, owner_pid FROM task_queue").fetchall()
        return {r[0]: int(r[1] or 0) for r in rows}

    def list_pending(self) -> list[dict]:
        """列出所有 pending 任务"""
        return self.list_all(status="pending", limit=1000)

    def cleanup(self, max_age_days: int = 30, max_rows: int = 0) -> int:
        """清理过期已完成任务（双判据并集，与 `RunStore.cleanup` 同口径）。

        年龄判据：终态行 `finished_at` 早于 `cutoff` 的删；数量判据：只保留
        最近 `max_rows` 条终态行（按 finished_at 新→旧）。两条同时启用时
        年龄判据对保留集内同样生效。

        **边界（0 的语义按历史保持）**：`max_age_days=0` 仍按旧语义 = 删光
        全部终态行（`cutoff=now`；tests/test_task_queue.py::test_cleanup 钉着
        这条）。要"年龄不启用"必须显式传大数——`maintenance.gc_runs` 就是这么
        调的（0 → 10^9 天，等于不删）。`max_rows=0` = 数量判据不启用。
        """
        cutoff = time.time() - max_age_days * 86400
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM task_queue "
                "WHERE status IN ('done', 'failed', 'cancelled') "
                "AND finished_at < ?", (cutoff,))
            dropped = cur.rowcount
            if max_rows > 0:
                cur = conn.execute(
                    "DELETE FROM task_queue "
                    "WHERE status IN ('done', 'failed', 'cancelled') "
                    "AND task_id NOT IN (SELECT task_id FROM task_queue "
                    "WHERE status IN ('done', 'failed', 'cancelled') "
                    "ORDER BY finished_at DESC LIMIT ?)",
                    (max_rows,))
                dropped += cur.rowcount
        return int(dropped)

    def stats(self) -> dict:
        """队列统计"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"
            ).fetchall()
            return {status: cnt for status, cnt in rows}

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "task_id": row[0],
            "pipeline_name": row[1],
            "input_file": row[2],
            "status": row[3],
            "config": _fast_loads(row[4]) if row[4] else {},
            "projection": row[5] or "ok",
            "error": row[6] or "",
            "created_at": row[7],
            "started_at": row[8],
            "finished_at": row[9],
            "worker_id": row[10] or "",
        }

    def close(self):
        """显式关闭当前线程缓存的连接（修复 P0：原为空实现，连接永不关闭）。"""
        self._backend.close()

    def close_all(self):
        """使本实例登记的全部 thread-local 连接失效（长驻进程/测试关停用）。

        安全性（P0，2026-08 CI 段错误修复，与 message_store.PersistentStore 同款）：
        绝不跨线程 close()——另一线程正在执行语句时跨线程关连接会在 C 层
        竞争触发 SIGSEGV。只关调用线程自己的连接，其余仅移出登记表；
        拥有线程下次 _get_conn() 探测失效自动重建（自愈）。失效连接由 GC
        在其线程结束后回收。
        """
        self._backend.close_all()
