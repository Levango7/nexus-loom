"""SQLite 并发首建的 WAL 切换重试（底座层本土实现）。

`PRAGMA journal_mode=WAL` 切换需要短暂独占锁，且**不走 busy handler**：
两进程同时首建同一库时，裸切会直接拿 SQLITE_BUSY（Python 文案
`database is locked`），与连接的 timeout 无关。所以必须自己退避重试。

这个竞态此前只在 loom_core 的连接池里修过；artesian 的知识库首建同样
裸切 WAL、重演过同一故障。修法不是让 artesian 指向 loom_core 的辅助
——分层门禁禁止底座反向依赖主仓包（artesian 不得 import
loom_core/loom_doc/agents/scripts，见 tests/test_layering）——而是把
辅助**下沉到本层**：实现住在这里，loom_core.sqlite_util 保留同名
re-export，上层调用面不变。
"""
from __future__ import annotations

import sqlite3
import time

#: WAL 重试次数与退避（首建期竞争窗口极短，8 次 × 0.05s 起步足够）
WAL_RETRY_ATTEMPTS = 8
WAL_RETRY_BASE_DELAY = 0.05
WAL_RETRY_MAX_DELAY = 1.0


def enable_wal(conn: sqlite3.Connection, attempts: int = WAL_RETRY_ATTEMPTS) -> None:
    """带重试地切换到 WAL 模式。

    非锁类错误（如库损坏）立即抛出；锁类错误退避重试，耗尽后仍抛
    ——"没切成 WAL"必须是可见故障，不许静默降级。
    """
    delay = WAL_RETRY_BASE_DELAY
    for attempt in range(1, attempts + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, WAL_RETRY_MAX_DELAY)
