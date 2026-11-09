"""CostTracker — LLM 成本/配额追踪与预算熔断

特性：
  - 每次调用记录：供应商/token 数/花费/任务 ID
  - 实时统计：按供应商/任务/时间维度
  - 预算熔断：超预算时拒绝调用
  - 持久化：经 db_backend 走 SQLite/Postgres 双方言，重启不丢

用法：
    tracker = get_cost_tracker()
    tracker.set_budget(max_cost=10.0)  # 10 美元预算
    if not tracker.check_budget():
        raise RuntimeError("预算已耗尽")
    tracker.record(provider="cloudflare", prompt_tokens=500, completion_tokens=200,
                   cost=0.002, task_id="task-001")
    stats = tracker.stats()  # 按供应商汇总
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from . import state_paths
from .db_backend import resolve_backend

logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """预算熔断：已花费达到预算上限，拒绝 LLM 调用"""
    def __init__(self, message: str = "预算已耗尽"):
        super().__init__(message)
        self.total_cost: float | None = None
        self.budget: float | None = None


# 供应商定价表（USD per 1K tokens）— prompt / completion
# 来源：各供应商官方定价页 2026-07；标注「近似值」的条目为牌价折算，实际以账单为准
PRICING = {
    "cloudflare": (0.00063, 0.00252),    # Kimi K2.6 via CF
    "openai": (0.0025, 0.01),            # GPT-4o 牌价 $2.5/$10 每 1M tokens（近似值）
    "deepseek": (0.00027, 0.0011),       # DeepSeek-V3 chat 牌价 $0.27/$1.10 每 1M tokens（近似值）
    "moonshot": (0.0006, 0.0025),        # Kimi K2 官方牌价 $0.6/$2.5 每 1M tokens（近似值）
    "qwen": (0.0004, 0.0012),            # Qwen Plus 牌价 $0.4/$1.2 每 1M tokens，与 bailian 同源（近似值）
    "xiaomi_mimo": (0.00014, 0.00056),   # MiMo-7B
    "longcat": (0.00014, 0.00056),       # LongCat-Flash
    "sensenova": (0.001, 0.002),         # DeepSeek V4 Flash
    "glm": (0.001, 0.002),               # GLM 5.2
    "agnes": (0.0005, 0.0015),           # Agnes
    "nvidia": (0.0007, 0.0021),          # Llama 3.1 Nemotron
    "bailian": (0.0004, 0.0012),         # Qwen Plus
    "qianfan": (0.0008, 0.0024),         # ERNIE 4.0
    "dahl": (0.0005, 0.0015),            # Dahl
    "siliconflow": (0.00014, 0.00056),   # Qwen 2.5 7B
    "ollama": (0.0, 0.0),                # 本地模型免费
    "default": (0.001, 0.002),           # 无前缀通用供应商兜底，与 DEFAULT_PRICE 一致
}
DEFAULT_PRICE = (0.001, 0.002)  # 未知供应商默认定价


# ── 成本归属：节点执行期把 task_id 传播到每次 LLM 记录 ──────────────
# 归属不能靠"调用方记得传"：`llm_router.chat()` 的调用点散在 agents/ 与 packs/ 里
# （writer / fact_checker / requirements_analyzer …），逐个补参数既会漏也污染签名。
# 这里用 ContextVar 让**引擎在节点执行期**声明作用域，router 记录时自己带上。
# 边界：ContextVar 不跨线程自动继承，所以作用域必须设在真正干活的那个线程里
# （`DAGExecutor.execute_node_from_scheduler` 的入口，process 模式的 worker 同样经过它）。
_cost_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "loom_cost_task_id", default="")


def current_cost_task_id() -> str:
    """当前节点执行期声明的 task_id；不在作用域内时为空串。"""
    return _cost_task_id.get()


@contextlib.contextmanager
def cost_task_scope(task_id: str) -> Iterator[None]:
    """在 with 块内把成本记到 task_id 名下（嵌套按栈恢复，异常也恢复）。"""
    token = _cost_task_id.set(str(task_id or ""))
    try:
        yield
    finally:
        _cost_task_id.reset(token)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（英文 ~4 chars/token，中文 ~2 chars/token）"""
    if not text:
        return 0
    cn_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en_count = len(text) - cn_count
    return cn_count // 2 + en_count // 4 + 1


def calc_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """计算花费（USD）"""
    p = PRICING.get(provider, DEFAULT_PRICE)
    return (prompt_tokens * p[0] + completion_tokens * p[1]) / 1000


class CostTracker:
    """LLM 成本追踪器（双方言持久化，pool 管理并发）"""

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or state_paths.store_path("cost.db")
        self._pool = resolve_backend(self._db_path, "cost.db",
                                    schema_init=self._ensure_schema)
        self._lock = threading.Lock()
        self._budget: float = 0  # 0 = 无限制
        self._init_db()

    @contextlib.contextmanager
    def _conn(self) -> Iterator[Any]:
        """当前线程连接（pool 管理生命周期与并发）。

        pool 的 thread-local 连接自带 WAL + busy_timeout（SQLite 侧），
        PG 侧由 MVCC 提供并发控制。事务由 ``with conn:`` 块管理。
        """
        with self._pool.conn() as conn:
            yield conn

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        """建表 + 索引（幂等；DDL 不可参数化，表名/列名全为常量）"""
        conn.execute(  # nosec B608
            "CREATE TABLE IF NOT EXISTS cost_log "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, provider TEXT NOT NULL, "
            "prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, "
            "cost REAL NOT NULL, task_id TEXT DEFAULT '', model TEXT DEFAULT '')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_provider ON cost_log(provider)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_log(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_log(timestamp)")

    def _init_db(self):
        self._ensure_schema(self._pool.conn())

    def set_budget(self, max_cost: float):
        """设置预算上限（USD）。0 = 无限制"""
        self._budget = max_cost

    def total_cost(self, task_ids: set[str] | None = None) -> float:
        """已花费总额（pool 连接互斥，读值原子）

        `task_ids` 非 None 时只统计这些任务（token 身份的作用域过滤，spec §5.4）。

        残余窗口：check_budget 的「读总额→比对」与并发 record 之间无在途预留，
        高并发下多个调用可同时通过检查，存在轻微超预算可能（check-then-act）。
        """
        with self._conn() as conn:
            if task_ids is None:
                row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM cost_log").fetchone()
                return row[0] if row else 0
            if not task_ids:
                return 0.0
            marks = ",".join("?" for _ in task_ids)
            # nosec B608：marks 是按集合长度生成的纯 "?" 占位符，值全部走参数
            # 绑定（tuple(task_ids)）——无任何用户输入拼进 SQL 文本。
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost), 0) FROM cost_log WHERE task_id IN ({marks})",  # nosec B608
                tuple(task_ids)).fetchone()
            return row[0] if row else 0

    def check_budget(self) -> bool:
        """检查是否在预算内（未设置预算或预算为 0 时恒为 True）"""
        if self._budget <= 0:
            return True
        return self.total_cost() < self._budget

    def ensure_budget(self) -> None:
        """预算熔断：超预算抛 BudgetExceededError；未设置预算(0)时放行"""
        total = self.total_cost()
        if not self.check_budget():
            err = BudgetExceededError(
                f"预算已耗尽: 已花费 {total:.4f} USD, 上限 {self._budget:.4f} USD"
            )
            err.total_cost = total
            err.budget = self._budget
            raise err

    def record(self, provider: str, prompt_tokens: int, completion_tokens: int,
               cost: float = None, task_id: str = "", model: str = ""):
        """记录一次 LLM 调用"""
        if cost is None:
            cost = calc_cost(provider, prompt_tokens, completion_tokens)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cost_log (timestamp, provider, prompt_tokens, completion_tokens, "
                "cost, task_id, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), provider, prompt_tokens, completion_tokens,
                 cost, task_id, model),
            )

    def record_call(self, provider: str, messages: list[dict], response: str,
                    task_id: str = "", model: str = ""):
        """便捷方法：从 messages + response 自动估算 token 并记录"""
        prompt_text = " ".join(m.get("content", "") for m in messages)
        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(response)
        self.record(provider, prompt_tokens, completion_tokens,
                    task_id=task_id, model=model)

    def stats(self, since: float = 0, task_ids: set[str] | None = None) -> dict:
        """按供应商汇总统计（task_ids 非 None 时只统计这些任务）。"""
        with self._conn() as conn:
            if task_ids is None:
                rows = conn.execute(
                    "SELECT provider, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cost) "
                    "FROM cost_log WHERE timestamp >= ? GROUP BY provider",
                    (since,),
                ).fetchall()
            else:
                if not task_ids:
                    return {}
                marks = ",".join("?" for _ in task_ids)
                # nosec B608：同 total_cost——marks 纯占位符，值全部参数绑定。
                rows = conn.execute(
                    "SELECT provider, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cost) "
                    f"FROM cost_log WHERE timestamp >= ? AND task_id IN ({marks}) GROUP BY provider",  # nosec B608
                    (since, *task_ids),
                ).fetchall()
            result = {}
            for provider, calls, pt, ct, cost in rows:
                result[provider] = {
                    "calls": calls,
                    "prompt_tokens": pt or 0,
                    "completion_tokens": ct or 0,
                    "total_tokens": (pt or 0) + (ct or 0),
                    "cost": round(cost or 0, 6),
                }
            return result

    def stats_by_task(self, task_id: str) -> dict:
        """按任务汇总"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT provider, COUNT(*), SUM(cost) "
                "FROM cost_log WHERE task_id = ? GROUP BY provider",
                (task_id,),
            ).fetchall()
            return {
                "task_id": task_id,
                "total_cost": round(sum(r[2] or 0 for r in rows), 6),
                "by_provider": {
                    r[0]: {"calls": r[1], "cost": round(r[2] or 0, 6)}
                    for r in rows
                },
            }

    def summary(self, task_ids: set[str] | None = None) -> dict:
        """总览（task_ids 非 None 时按任务集合过滤——token 身份的成本作用域）。"""
        total = self.total_cost(task_ids)
        data: dict = {
            "total_cost": round(total, 6),
            "budget": self._budget,
            "budget_remaining": round(self._budget - total, 6) if self._budget > 0 else None,
            "budget_exceeded": total >= self._budget if self._budget > 0 else False,
            "by_provider": self.stats(task_ids=task_ids),
        }
        if task_ids is not None:
            data["scope"] = "principal"
            data["task_count"] = len(task_ids)
        return data

    def cleanup(self, max_age_days: int = 90, max_rows: int = 0,
                now: float | None = None) -> int:
        """清理过期记录（双判据并集；0 的语义与 TaskQueue 同口径）。"""
        now_ts = now if now is not None else time.time()
        cutoff = now_ts - max_age_days * 86400
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM cost_log WHERE timestamp < ?",
                               (cutoff,))
            dropped: int = cur.rowcount
            if max_rows > 0:
                cur = conn.execute(
                    "DELETE FROM cost_log WHERE id NOT IN (SELECT id FROM cost_log "
                    "ORDER BY timestamp DESC LIMIT ?)", (max_rows,))
                dropped += cur.rowcount
        return dropped

    def close(self) -> None:
        """关闭当前线程的连接（测试清理用）。"""
        self._pool.close()


_tracker_instance: CostTracker | None = None
_tracker_lock = threading.Lock()


def get_cost_tracker() -> CostTracker:
    """获取全局 CostTracker 单例"""
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_lock:
            if _tracker_instance is None:
                _tracker_instance = CostTracker()
    return _tracker_instance


def reset_cost_tracker():
    """重置单例（测试用）"""
    global _tracker_instance
    with _tracker_lock:
        _tracker_instance = None
