"""KnowledgeBase 首建切 WAL 的并发判据：裸 PRAGMA 的锁竞态不许回潮。

背景：loom_core/sqlite_util 模块头已记载 WAL 切换的首建期竞态（切模式
需短暂独占锁、不走 busy handler，竞争直接 SQLITE_BUSY，与 timeout 无关；
CI 3.14 腿 e2e-0 实证）。该竞态此前在 artesian 侧重演——
KnowledgeBase._get_conn 直接裸切 WAL，同样脆。现由 artesian/sqlite_util
的 enable_wal 收口（带退避重试，与 db_backend 同款口径；实现住底座层是
因为分层门禁禁 artesian 反向依赖 loom_core）。本文件钉四件事：

1. 确定性持锁竞争下 KnowledgeBase 首建不抛 + 终态 journal_mode=wal；
2. 护栏正例：同场景裸 PRAGMA journal_mode=WAL 必抛 locked（证明区分力，
   否则第 1 条的绿是空转）；
3. 形态钉死：artesian 内全文 AST 扫描不得 import loom_core；WAL 切换走
   artesian 本土 enable_wal（相对 import 在模块顶层、函数体内无裸
   PRAGMA），且实现唯一（loom_core 侧只有同名 re-export）；
4. 双进程并发首建同一 knowledge_base.db 零异常（真实首建现场，概率性补充）。
"""
from __future__ import annotations

import ast
import contextlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent

KB_INIT_SCRIPT = """
import json, sys
sys.path.insert(0, {project!r})
errors = []
try:
    from artesian.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(db_path={db!r})
    kb.add_document("wal probe line one\\n\\nwal probe line two\\n\\nwal probe line three", "src", "title")
    hits = kb.search("wal probe", top_k=1)
    if not hits:
        errors.append("empty search after index")
    kb.close_all()
except Exception as e:
    errors.append(type(e).__name__ + ": " + str(e))
print(json.dumps(errors))
"""


class _Holder:
    """持 BEGIN EXCLUSIVE 一段固定时长，模拟另一进程首建抢锁。"""

    HOLD_SECONDS = 1.2  # 必须 > SQLite 内部重试窗口（约 0.45s）才区分新旧写法

    def __init__(self, db_path: str):
        self.holder = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.holder.execute("PRAGMA journal_mode")  # 触发建库
        self.holder.execute("BEGIN EXCLUSIVE")

    def release_after(self):
        def _release():
            time.sleep(self.HOLD_SECONDS)
            self.holder.rollback()

        threading.Thread(target=_release, daemon=True).start()

    def close(self):
        with contextlib.suppress(sqlite3.Error):
            self.holder.rollback()
        self.holder.close()


class TestKnowledgeBaseWalInit:
    def test_first_init_under_exclusive_lock_retries_into_wal(self, tmp_path):
        """持锁 1.2s 下首建 KnowledgeBase：enable_wal 退避跨过，终态 wal。"""
        from artesian.knowledge_base import KnowledgeBase

        db = str(tmp_path / "kb.db")
        holder = _Holder(db)
        try:
            holder.release_after()
            kb = KnowledgeBase(db_path=db)  # 不得抛 database is locked
            try:
                mode = kb._get_conn().execute("PRAGMA journal_mode").fetchone()[0]
                assert mode == "wal"
            finally:
                kb.close_all()
        finally:
            holder.close()

    def test_old_style_pragma_fails_under_same_lock(self, tmp_path):
        """护栏正例：同场景裸 PRAGMA 必抛 locked（否则上条绿是空转）。"""
        db = str(tmp_path / "fresh.db")
        holder = _Holder(db)
        try:
            other = sqlite3.connect(db, timeout=0.1)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("PRAGMA journal_mode=WAL")
            finally:
                other.close()
        finally:
            holder.close()

    def test_wal_switch_uses_artesian_local_helper(self):
        """形态钉死：artesian 内不得 import loom_core；走本土 enable_wal；实现唯一。"""
        src = (PROJECT / "artesian" / "knowledge_base.py").read_text(
            encoding="utf-8")
        # 分层门禁禁 artesian（底座）-> loom_core（主仓包）；这里把口径再钉
        # 一层：AST 全文件扫描（含函数体内的 deferred import），任何形态的
        # loom_core 导入都不许出现（提顶层/留函数体同样红）。
        tree = ast.parse(src)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert not m.startswith("loom_core"), (
                    f"artesian 不得依赖 loom_core：{m}")

        # 开关必须经由本土辅助：相对 import 在模块顶层（无环），函数体内
        # 不许再裸切 PRAGMA（持锁竞争下必抛 locked，区分力见第 2 条）
        assert "from .sqlite_util import enable_wal" in src
        fn = src.split("def _get_conn", 1)[1].split("\n    def ", 1)[0]
        assert "enable_wal(conn)" in fn
        assert "PRAGMA journal_mode=WAL" not in src

        # 单一实现：enable_wal 只住在 artesian/sqlite_util；loom_core 侧
        # 只有同名 re-export，不许有第二份实现（口径归一）
        art_util = (PROJECT / "artesian" / "sqlite_util.py").read_text(
            encoding="utf-8")
        core_util = (PROJECT / "loom_core" / "sqlite_util.py").read_text(
            encoding="utf-8")
        assert "def enable_wal" in art_util
        assert "def enable_wal" not in core_util
        assert "from artesian.sqlite_util import enable_wal" in core_util

    def test_two_processes_first_build_without_lock_errors(self, tmp_path):
        """双进程并发首建同一 knowledge_base.db：零异常 + 事后真可读写。"""
        ROUNDS = 6
        for rnd in range(ROUNDS):
            state = tmp_path / ("r" + str(rnd))
            state.mkdir()
            db = str(state / "knowledge_base.db")
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", KB_INIT_SCRIPT.format(
                        project=str(PROJECT), db=db)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for _ in range(2)
            ]
            for proc in procs:
                out, _ = proc.communicate(timeout=120)
                assert proc.returncode == 0, out[-1500:]
                last = out.strip().splitlines()[-1] if out.strip() else "[]"
                errors = json.loads(last)
                assert errors == [], f"第 {rnd} 轮并发首建报错: {errors}"
