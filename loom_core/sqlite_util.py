"""SQLite 并发初始化收口：加列竞态的幂等处理。

两个都是**首建期**竞态，只在"两个进程同时第一次建同一批库"时现形，
因此单进程测试与隔离跑都看不见——CI 的 3.14 腿在全量负载下命中过
（`e2e-0 终态是 failed: database is locked`）。本机探针复现口径：
两进程 × 80 轮同建四库，命中 3 次 `database is locked` + 1 次
`duplicate column name: owner_pid`。

两处竞态的分工：

1. **WAL 切换不走 busy handler**（切模式需短暂独占锁，裸切遇竞争直接
   SQLITE_BUSY，与 timeout 无关）——实现已下沉到 artesian.sqlite_util
   （底座层本土：分层门禁禁 artesian 反向依赖主仓包，见
   tests/test_layering）；本模块保留同名 re-export，db_backend /
   message_store 等调用面不变。
2. **`ALTER TABLE ... ADD COLUMN` 的 check-then-act**：两个进程都按
   `PRAGMA table_info` 判断"列不存在"，然后都执行 ALTER，后到者拿
   "duplicate column name"。目标状态其实已达成，吞掉这个错误即可。
"""
from __future__ import annotations

import re
import sqlite3

from artesian.sqlite_util import enable_wal as enable_wal  # 侧重导出：调用面不变

#: 标识符白名单：PRAGMA/ALTER 的插值点无法参数化（SQL 标识符不支持占位符），
#: 这里把"调用方只传字面常量"从约定升级为强制——动态拼出的表名/列名在这里
#: 直接抛 ValueError，而不是等 SQLite 原样执行。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: DDL 类型片段白名单：字母/数字/下划线/空格/单引号/逗号/括号——够表达
#: "INTEGER DEFAULT 0"、"TEXT DEFAULT ''" 这类字面量，拒掉 ; -- /* 等结构字符。
_DDL_TYPE_RE = re.compile(r"^[A-Za-z0-9_ '(),]+$")


def _checked(kind: str, value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.match(value):
        raise ValueError(
            f"sqlite_util: {kind} 必须是字面常量，不接受动态拼出的值: {value!r}")
    return value


def column_names(conn: sqlite3.Connection, table: str) -> set:
    """表的现有列名集合。"""
    _checked("表名", table, _IDENTIFIER_RE)
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                          ddl_type: str) -> bool:
    """幂等加列。返回是否真的执行了 ALTER。

    并发下 check-then-act 会撞 "duplicate column name"，此时目标状态已达成，
        视为成功（返回 False）；其他 OperationalError 照抛。
    """
    _checked("表名", table, _IDENTIFIER_RE)
    _checked("列名", column, _IDENTIFIER_RE)
    _checked("DDL 类型", ddl_type, _DDL_TYPE_RE)
    if column in column_names(conn, table):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            return False
        raise
