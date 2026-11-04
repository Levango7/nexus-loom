"""config.py 分支补全（C-19）。

既有 `test_config_overlay.py` / `test_config.py` / `test_config_audit.py` 钉了
级联合并、覆写文件本体、跨进程 CAS（真子进程）、损坏文件保留旧配置与审计；
本文件补齐**分支级**缺口：

- `_overlay_file_lock`：Windows O_EXCL 自旋的**陈旧锁抢占**与**超时如实报错**；
  POSIX fcntl 分支（Windows 上天然不可达——用假 fcntl 模块行使它，含加锁成功、
  解锁失败被抑制、超时三态）；Windows 分支则在非 Windows 上用
  `sys.modules["fcntl"]=None` 强制（分派就在函数内 `try: import fcntl`，
  O_EXCL/stat/unlink 本身跨平台，Linux CI 也能如实行使）；
- `_read_overlay` 形状错误三族：顶层非对象 / values 非对象（既有已覆盖）/
  revision 非整数——错因必须进 `last_overlay_error` 而不是静默；
- CAS 三个错误分支（真实覆盖不到的形状）：损坏覆写真上拒绝增量写 / 写盘失败
  如实返回 / 写后读回失败；`_cas_publish` 同族；两条锁超时转错误串；
- `_check_reload`：配置文件变更热加载、损坏文件保留旧配置且 mtime 记账；
- `poll_overlay`：auto_reload 关闭 / 覆写文件不存在 / 同 revision 不重载；
- `_set_dotted`：中间层是标量时被替换成 dict（不能抛也不能吞）。

mock 口径：锁与 CAS 的失败注入全在 `loom_core.config` 模块面（monkeypatch
`_overlay_file_lock` / `_read_overlay` / `_write_overlay`）；假时钟替
`loom_core.config.time`，避免真等 15 秒。状态目录一律 LOOM_STATE_DIR 进 tmp。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import loom_core.config as cfg
from loom_core.config import ConfigCenter


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("LOOM_STATE_DIR", str(d))
    return d


def _overlay_path(state_dir: Path) -> Path:
    return state_dir / "config_overrides.json"


def _write_overlay_file(state_dir: Path, payload) -> None:
    _overlay_path(state_dir).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _FakeClock:
    """每次调用前进 step 秒——把 15 秒超时压成毫秒。"""

    def __init__(self, step=5.0):
        self.now = 1000.0
        self.step = step

    def time(self):
        self.now += self.step
        return self.now


def _patch_clock(monkeypatch, step=5.0):
    clock = _FakeClock(step)
    monkeypatch.setattr(cfg.time, "time", clock.time)
    monkeypatch.setattr(cfg.time, "sleep", lambda s: None)
    return clock


@pytest.fixture()
def windows_lock_branch(monkeypatch):
    """强制锁函数走 Windows O_EXCL 分支：分派就在函数内 `try: import fcntl`，
    sys.modules["fcntl"]=None 让该 import 抛 ImportError；O_EXCL/stat/unlink
    本身跨平台，故 Linux CI 上也能如实行使（此前这些行在非 Windows 上恒不可达）。"""
    monkeypatch.setitem(sys.modules, "fcntl", None)


# ─── 文件锁：Windows 自旋分支 ────────────────────────────────

class TestWindowsFileLock:

    def test_stale_lock_is_taken_over(self, state_dir, windows_lock_branch):
        """崩溃留下的锁文件（mtime 超 30s）被抢占：写照样成功。"""
        import os
        import time as _t
        lock_path = _overlay_path(state_dir).with_name(
            "config_overrides.json.lock")
        lock_path.write_text("", encoding="utf-8")
        stale = _t.time() - 60
        os.utime(lock_path, (stale, stale))

        center = ConfigCenter()
        assert center.set_persisted("llm.model", "m-stale-test") is None
        assert not lock_path.exists(), "写完必须释放锁文件"
        assert json.loads(_overlay_path(state_dir).read_text(
            encoding="utf-8"))["values"]["llm.model"] == "m-stale-test"

    def test_lock_timeout_reported_not_raised(self, state_dir, monkeypatch,
                                             windows_lock_branch):
        """锁始终被占：超时如实转错误串，调用方能回报给请求方。"""
        _patch_clock(monkeypatch)
        lock_path = _overlay_path(state_dir).with_name(
            "config_overrides.json.lock")
        lock_path.write_text("", encoding="utf-8")  # 新鲜锁，不会陈旧
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "配置覆写锁等待超时" in err


# ─── 文件锁：POSIX fcntl 分支（Windows 上借假 fcntl 行使） ────

class TestPosixFileLockBranch:

    def _fake_fcntl(self, monkeypatch, flock_impl):
        fake = SimpleNamespace(
            LOCK_EX=1, LOCK_NB=2, LOCK_UN=4,
            flock=flock_impl,
        )
        monkeypatch.setitem(sys.modules, "fcntl", fake)
        return fake

    def test_acquire_and_release(self, state_dir, monkeypatch):
        calls = []

        def flock(fd, op):
            calls.append(op)

        self._fake_fcntl(monkeypatch, flock)
        center = ConfigCenter()
        assert center.set_persisted("llm.model", "m-posix") is None
        fake = sys.modules["fcntl"]
        assert fake.LOCK_EX | fake.LOCK_NB in calls
        assert fake.LOCK_UN in calls

    def test_unlock_failure_suppressed(self, state_dir, monkeypatch):
        def flock(fd, op):
            if op == 4:  # LOCK_UN
                raise OSError("unlock 失败")

        self._fake_fcntl(monkeypatch, flock)
        center = ConfigCenter()
        assert center.set_persisted("llm.model", "m-u") is None, \
            "解锁失败不该把已成功的写入报成失败"

    def test_timeout_reported(self, state_dir, monkeypatch):
        _patch_clock(monkeypatch)

        def flock(fd, op):
            if op != 4:
                raise OSError("busy")

        self._fake_fcntl(monkeypatch, flock)
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "配置覆写锁等待超时" in err


# ─── 覆写文件形状错误 ───────────────────────────────────────

class TestOverlayShapeErrors:

    def test_top_level_not_object(self, state_dir):
        _write_overlay_file(state_dir, [1, 2, 3])
        center = ConfigCenter()
        assert "overlay 顶层不是对象" in center.last_overlay_error
        assert center.get("llm.model")  # 保留默认配置

    def test_revision_not_int(self, state_dir):
        _write_overlay_file(state_dir, {"revision": "abc", "values": {"a": 1}})
        center = ConfigCenter()
        assert "overlay revision 不是整数" in center.last_overlay_error
        assert center.get("a") is None, "损坏覆写不得渗入配置"


# ─── CAS 错误分支 ───────────────────────────────────────────

class TestCasErrorBranches:

    def test_refuses_incremental_write_on_corrupt_overlay(self, state_dir, monkeypatch):
        monkeypatch.setattr(cfg, "_read_overlay",
                            lambda p: ({}, 0, "JSONDecodeError: bad"))
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "拒绝在损坏的覆写文件上增量写" in err

    def test_write_failure_returned(self, state_dir, monkeypatch):
        monkeypatch.setattr(cfg, "_write_overlay",
                            lambda p, rev, values: "disk full")
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "x")
        assert err == "disk full"

    def test_backread_failure_after_write(self, state_dir, monkeypatch):
        center = ConfigCenter()  # 构造期自己会读一次覆写——先构造再打点
        calls = iter([({}, 0, ""), None, ({}, 0, "back bad")])
        monkeypatch.setattr(cfg, "_read_overlay", lambda p: next(calls))
        monkeypatch.setattr(cfg, "_write_overlay",
                            lambda p, rev, values: next(calls))
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "写后读回验证失败" in err

    def test_publish_error_branches(self, state_dir, monkeypatch):
        monkeypatch.setattr(cfg, "_read_overlay",
                            lambda p: ({}, 0, "读不动"))
        center = ConfigCenter()
        assert center.publish_reload() == "读不动"

    def test_publish_backread_failure(self, state_dir, monkeypatch):
        center = ConfigCenter()  # 构造期已读过一次覆写
        calls = iter([({}, 0, ""), None, ({}, 0, "back bad")])
        monkeypatch.setattr(cfg, "_read_overlay", lambda p: next(calls))
        monkeypatch.setattr(cfg, "_write_overlay",
                            lambda p, rev, values: next(calls))
        assert center.publish_reload() == "back bad"

    def test_publish_lock_timeout(self, state_dir, monkeypatch):
        def boom(path, timeout=15.0):
            raise TimeoutError("t")

        monkeypatch.setattr(cfg, "_overlay_file_lock", boom)
        center = ConfigCenter()
        err = center.publish_reload()
        assert err is not None and "配置发布锁等待超时" in err

    def test_persist_lock_timeout(self, state_dir, monkeypatch):
        def boom(path, timeout=15.0):
            raise TimeoutError("t")

        monkeypatch.setattr(cfg, "_overlay_file_lock", boom)
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "配置覆写锁等待超时" in err


class TestLowLevelAccess:

    def test_lock_stat_race_not_stale_then_timeout(self, state_dir, monkeypatch,
                                                   windows_lock_branch):
        """锁文件在 open 与 stat 之间消失（竞态）：stat 失败时 age=timeout+1=16，
        **不满足 >30 的陈旧线**——真实行为是继续自旋直到超时如实报错。
        （若把 16 当陈旧去抢，会让并发写者在锁存续期间闯入，比超时更糟。）"""
        _patch_clock(monkeypatch)
        lock_path = _overlay_path(state_dir).with_name(
            "config_overrides.json.lock")
        lock_path.write_text("", encoding="utf-8")

        orig_stat = Path.stat

        def fake_stat(self, *a, **kw):
            if self.name.endswith(".lock"):
                raise OSError("between check and stat")
            return orig_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", fake_stat)
        center = ConfigCenter()
        err = center.set_persisted("llm.model", "m-race")
        assert err is not None and "配置覆写锁等待超时" in err

    def test_write_overlay_replace_failure_reported(self, state_dir, monkeypatch):
        center = ConfigCenter()
        monkeypatch.setattr(cfg.os, "replace",
                            MagicMock(side_effect=OSError("disk full")))
        err = center.set_persisted("llm.model", "x")
        assert err is not None and "disk full" in err

    def test_yaml_missing_raises_actionable_and_keeps_defaults(self, state_dir, tmp_path, monkeypatch):
        """PyYAML 缺失：构造不炸（保留缺省）+ 错误串带安装指引。"""
        import sys as _sys
        f = tmp_path / "conf.yaml"
        f.write_text("""llm:
  model: x
""", encoding="utf-8")
        monkeypatch.setitem(_sys.modules, "yaml", None)  # import yaml ⇒ ImportError
        center = ConfigCenter(config_file=str(f))
        assert "PyYAML" in center.last_reload_error
        assert center.get("llm.model"), "缺省配置必须仍在"

    def test_publish_write_error_returned(self, state_dir, monkeypatch):
        center = ConfigCenter()
        calls = iter([({}, 0, ""), "disk full"])
        monkeypatch.setattr(cfg, "_read_overlay", lambda p: next(calls))
        monkeypatch.setattr(cfg, "_write_overlay",
                            lambda p, rev, values: next(calls))
        assert center.publish_reload() == "disk full"

    def test_publish_conflict_retries_exhausted(self, state_dir, monkeypatch):
        """读回 revision 永远追不上写入值：10 次后如实报冲突，不无限重试。"""
        center = ConfigCenter()
        monkeypatch.setattr(cfg, "_read_overlay", lambda p: ({}, 0, ""))
        monkeypatch.setattr(cfg, "_write_overlay", lambda p, rev, values: None)
        err = center.publish_reload()
        assert err is not None and "配置发布冲突" in err


# ─── 热加载与轮询 ───────────────────────────────────────────

class TestReloadBranches:

    def _config_file(self, tmp_path: Path, model: str) -> Path:
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"llm": {"model": model}}), encoding="utf-8")
        return f

    def test_file_change_hot_reloaded_on_get(self, tmp_path):
        import os
        import time as _t
        f = self._config_file(tmp_path, "old-model")
        center = ConfigCenter(config_file=str(f))
        assert center.get("llm.model") == "old-model"

        f.write_text(json.dumps({"llm": {"model": "new-model"}}), encoding="utf-8")
        future = _t.time() + 10
        os.utime(f, (future, future))
        assert center.get("llm.model") == "new-model", "mtime 前进必须触发热加载"
        assert center.last_reload_error == ""

    def test_corrupt_config_keeps_previous_and_records_error(self, tmp_path):
        import os
        import time as _t
        f = self._config_file(tmp_path, "keep-me")
        center = ConfigCenter(config_file=str(f))
        assert center.get("llm.model") == "keep-me"

        f.write_text("{broken json", encoding="utf-8")
        future = _t.time() + 10
        os.utime(f, (future, future))
        assert center.get("llm.model") == "keep-me", "坏文件不得冲掉旧配置"
        assert center.last_reload_error, "错因必须留痕（不许静默）"

    def test_poll_overlay_disabled_when_auto_reload_off(self, state_dir):
        center = ConfigCenter(auto_reload=False)
        assert center.poll_overlay() is False

    def test_poll_overlay_missing_file(self, state_dir):
        center = ConfigCenter()  # 从未写覆写文件
        assert center.poll_overlay() is False

    def test_poll_same_revision_does_not_reload(self, state_dir):
        import os
        import time as _t
        center = ConfigCenter()
        assert center.set_persisted("llm.model", "same-rev") is None
        rev = center.revision
        # 只改 mtime 不改 revision：模拟同内容重写，轮询不该重载
        p = _overlay_path(state_dir)
        future = _t.time() + 10
        os.utime(p, (future, future))
        center._overlay_mtime = 0.0  # 迫使 mtime 检查通过
        assert center.poll_overlay() is False
        assert center.revision == rev


# ─── 点键写入与导出 ─────────────────────────────────────────

class TestDottedWrites:

    def test_intermediate_scalar_replaced_by_dict(self):
        center = ConfigCenter()
        center.set("execution", "scalar")          # 把整块压成标量
        center.set("execution.max_workers", 3)     # 再写子键：中间层必须换回 dict
        assert center.get("execution.max_workers") == 3
        assert center.get("execution") == {"max_workers": 3}

    def test_to_dict_is_deep_copy(self):
        center = ConfigCenter()
        snapshot = center.to_dict()
        snapshot["llm"]["model"] = "hacked"
        assert center.get("llm.model") != "hacked"

    def test_revision_floor_is_zero(self):
        center = ConfigCenter()
        assert center.revision == 0, "从未见覆写时 revision 报 0 而非 -1"
