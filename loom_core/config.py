"""
统一配置中心 —— 级联配置：默认值 → 配置文件 → 环境变量 → 代码覆盖

跨进程热更新（P1 验收第三条）：运行时 `set` 的键值可写入**覆写文件**
`<状态目录>/config_overrides.json`（revision + 平铺点号键），其他进程
（worker / 第二个 Admin API）轮询 revision 即可拿到同一份运行时配置。
覆写层位于环境变量**之后**——与既往"运行时 set 胜过一切"的内存语义一致。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .state_paths import state_root

logger = logging.getLogger(__name__)

#: 覆写写口的 CAS 重试上限：读 → 写 → 读回验证，冲突则重读重写。
#: 10 次还冲突说明有进程持续抢跑——如实报错，不无限重试。
CAS_MAX_RETRIES = 10


def overlay_path() -> Path:
    """运行时配置覆写文件（跨进程共享，见模块 docstring）。"""
    return state_root() / "config_overrides.json"


def _read_overlay(path: Path) -> tuple[dict, int, str]:
    """读覆写文件 → (平铺键值, revision, 错误串)。文件不存在 = 空覆写，不是错误。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, 0, ""
    except Exception as e:  # noqa: BLE001 —— 损坏文件必须如实报错，调用方决定是否保留旧配置
        return {}, 0, f"{type(e).__name__}: {e}"
    if not isinstance(raw, dict):
        return {}, 0, "overlay 顶层不是对象"
    values = raw.get("values") or {}
    if not isinstance(values, dict):
        return {}, 0, "overlay values 不是对象"
    try:
        revision = int(raw.get("revision") or 0)
    except (TypeError, ValueError):
        return {}, 0, "overlay revision 不是整数"
    return values, revision, ""


@contextlib.contextmanager
def _overlay_file_lock(path: Path, timeout: float = 15.0):
    """跨进程文件锁——CAS 的读-改-写-读回必须**整体**串行化。

    为什么需要它（CI 抓到的真竞态，本地没暴露）：没有锁时两个进程可以
    这样交错——P1 写 rev1{a.k} → P1 读回验证通过（那一刻文件确实是 P1 的）
    → P2 写 rev1{b.k}（同 revision 整份覆盖）→ P2 读回通过。P1 的验证
    已经过期，最终文件只剩 P2 的键，而**两边都报成功**。读回验证挡不住
    "验证之后才发生的覆盖"——这正是文件锁存在的理由。

    实现：POSIX 走 fcntl.flock；Windows 走 O_CREAT|O_EXCL 自旋锁，崩溃
    留下的锁文件按 mtime 超 30s 判定陈旧并抢占（抢占有一个极小的双持有
    窗口，与 git 锁文件同款取舍，照实写在这里，不装作没有）。
    """
    lock_path = path.with_name(path.name + ".lock")
    try:
        import fcntl
    except ImportError:
        # Windows：O_EXCL 自旋
        deadline = time.time() + timeout
        fd = None
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = timeout + 1
                if age > 30:
                    with contextlib.suppress(OSError):
                        os.unlink(lock_path)
                    continue
                if time.time() > deadline:
                    raise TimeoutError(
                        f"配置覆写锁等待超时（{timeout}s）：{lock_path}") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
        return
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                os.close(fd)
                raise TimeoutError(
                    f"配置覆写锁等待超时（{timeout}s）：{lock_path}") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_overlay(path: Path, revision: int, values: dict) -> str | None:
    """原子写覆写文件（临时文件 + os.replace）。失败返回错误串，不静默。

    临时名带 PID：两个进程（两个 Admin 实例）同时写不会抢同一个 .tmp。
    """
    payload = {"revision": revision,
               "updated": datetime.now().isoformat(timespec="seconds"),
               "values": values}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return None
    except Exception as e:  # noqa: BLE001 —— 调用方要把失败回报给请求方
        return str(e)


class ConfigCenter:
    """统一配置中心 —— 级联配置：默认值 → 配置文件 → 环境变量 → 代码覆盖"""

    def __init__(self, config_file: str = None, auto_reload: bool = True):
        """
        config_file: 主配置文件路径（YAML/JSON），默认查找 loom/config.yaml
        auto_reload: 是否自动检测文件变化并热加载
        """
        self._config_file = Path(config_file) if config_file else None
        self._auto_reload = auto_reload
        self._data: dict = {}
        self._mtime: float = 0
        self._lock = threading.RLock()
        self._overrides: dict = {}
        self.last_reload_error: str = ""
        self.last_overlay_error: str = ""
        self._overlay_path = overlay_path()
        self._overlay_revision: int = -1
        self._overlay_mtime: float = 0.0
        self._load()

    def _load(self):
        """加载配置文件"""
        # 1. 加载默认值
        defaults = {
            "bus": {"db_path": "bus_data/message_bus.db", "persistence": True},
            "checkpoint": {"dir": "checkpoints", "max_age_days": 7},
            "agents": {"dir": "agents", "cache_dir": "cache", "log_dir": "logs"},
            "execution": {"max_workers": 8, "fail_fast": True, "max_retries": 3},
            "llm": {"api_key_env": "LLM_API_KEY", "model": "@cf/moonshotai/kimi-k2.6"},
        }
        previous = self._data
        self._data = defaults.copy()

        # 2. 加载配置文件
        if self._config_file and self._config_file.exists():
            try:
                self._merge_file(self._config_file)
                self.last_reload_error = ""
            except Exception as e:
                self._data = previous or defaults
                self.last_reload_error = f"{type(e).__name__}: {e}"
                logging.getLogger(__name__).warning(
                    "配置文件加载失败，保留旧配置: %s", e
                )
                with contextlib.suppress(OSError):
                    self._mtime = self._config_file.stat().st_mtime

        # 3. 环境变量覆盖（以 DOCPIPE_ 开头的环境变量）
        # 例如 DOCPIPE_LLM__API_KEY=xxx → data["llm"]["api_key"] = "xxx"
        self._merge_env()

        # 4. 运行时覆写层（跨进程共享；位于 env 之后=运行时 set 仍胜出）
        self._merge_overlay(previous)

    def _merge_overlay(self, previous: dict) -> None:
        """合并覆写文件的键值。文件不存在 = 空覆写；解析失败 = 整体保留旧配置。"""
        values, revision, error = _read_overlay(self._overlay_path)
        try:
            self._overlay_mtime = self._overlay_path.stat().st_mtime
        except OSError:
            self._overlay_mtime = 0.0
        if error:
            self._data = previous or self._data
            self.last_overlay_error = error
            self._overlay_revision = revision
            logger.warning("配置覆写文件解析失败，保留旧配置: %s", error)
            return
        self.last_overlay_error = ""
        self._overlay_revision = revision
        for key, value in values.items():
            self._set_dotted(key, value)

    def _set_dotted(self, key: str, value) -> None:
        """按点分隔键写入（如 'llm.model' → data['llm']['model']）。"""
        parts = key.split(".")
        target = self._data
        for part in parts[:-1]:
            nxt = target.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                target[part] = nxt
            target = nxt
        target[parts[-1]] = value

    def _merge_file(self, path: Path):
        """合并 YAML/JSON 配置文件"""
        import json
        with open(path, encoding="utf-8") as f:
            if path.suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    file_data = yaml.safe_load(f) or {}
                except ImportError:
                    raise ImportError(
                        f"PyYAML is required to load {path} but is not installed. "
                        "Install with: pip install pyyaml"
                    ) from None
            else:
                file_data = json.load(f) or {}
        self._deep_merge(self._data, file_data)

    def _merge_env(self):
        """合并 DOCPIPE_ 前缀的环境变量。双下划线表示嵌套：DOCPIPE_LLM__MODEL → llm.model"""
        for key, value in os.environ.items():
            if not key.startswith("DOCPIPE_"):
                continue
            parts = key[8:].lower().split("__")
            target = self._data
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = self._coerce(value)

    def _coerce(self, value: str):
        """类型转换：'true'→True, '123'→123, '3.14'→3.14, '[a,b]'→list, '{"k":"v"}'→dict"""
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        with contextlib.suppress(ValueError):
            return int(value)
        with contextlib.suppress(ValueError):
            return float(value)
        # JSON 容器：支持环境变量传入数组/对象，如 DOCPIPE_RESEARCHER__SEARCH_ENGINES='["mock"]'
        if value and value[0] in ("[", "{"):
            with contextlib.suppress(ValueError):
                import json as _json
                parsed = _json.loads(value)
                if isinstance(parsed, (list, dict)):
                    return parsed
        return value

    def _deep_merge(self, base: dict, override: dict):
        """深度合并字典"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _check_reload(self):
        """检查配置文件是否变更，自动热加载"""
        if not self._auto_reload or not self._config_file or not self._config_file.exists():
            return
        mtime = self._config_file.stat().st_mtime
        if mtime > self._mtime:
            with self._lock:
                if mtime > self._mtime:  # 双重检查
                    self._load()
                    self._mtime = mtime

    def get(self, key: str, default=None):
        """获取配置值。key 用点分隔：'llm.model' → data['llm']['model']"""
        self._check_reload()
        with self._lock:
            parts = key.split(".")
            target = self._data
            for part in parts:
                if isinstance(target, dict):
                    target = target.get(part)  # type: ignore[assignment]
                else:
                    return default
            return target if target is not None else default

    def set(self, key: str, value):
        """运行时覆盖配置（仅本进程内存；跨进程用 set_persisted）"""
        with self._lock:
            self._set_dotted(key, value)

    def set_persisted(self, key: str, value) -> str | None:
        """运行时覆盖配置并**写入覆写文件**——其他进程轮询 revision 后同样生效。

        返回 None = 已持久化；返回错误串 = 仅本进程内存生效，跨进程不会拿到
        （调用方必须把失败如实回报给请求方，不静默）。
        """
        with self._lock:
            self._set_dotted(key, value)
            return self._write_overlay_with(key, value)

    def _write_overlay_with(self, key: str, value) -> str | None:
        """读现覆写 → 改一个键 → 原子写回（revision +1）→ **读回验证**。持锁调用。

        B6：此前的写法是"读 revision → 写 revision+1"——多进程下两个进程
        都读到 N、都写 N+1，**后写者整份覆盖先写者**：先写者的键丢失，
        而两边都报成功（配置悄悄少了一个键，这正是 B6 点名的形状）。现在
        写后必须读回：revision 与 values 都与写入时一致才算成功；不一致
        说明有进程抢跑，**重读重写**（下一轮读到的是抢跑者的新值，把己方
        键并进去再写——所有键最终都落在文件里）。重试仍冲突则如实报错。

        **整段在跨进程文件锁内执行**（`_overlay_file_lock`）：读回验证挡不住
        "验证之后才发生的覆盖"——CI 上两进程并发写不同键实测抓到过这条
        （P1 验证通过后 P2 才覆盖，最终文件只剩一个键）。锁把读-改-写-读回
        串行化，覆盖竞态从机制上消失。
        """
        try:
            with _overlay_file_lock(self._overlay_path):
                return self._cas_write(key, value)
        except TimeoutError as e:
            return f"配置覆写锁等待超时：{e}"

    def _cas_write(self, key: str, value) -> str | None:
        """CAS 写循环本体（持文件锁调用）。"""
        for _attempt in range(CAS_MAX_RETRIES):
            values, revision, error = _read_overlay(self._overlay_path)
            if error:
                return f"{error}（拒绝在损坏的覆写文件上增量写）"
            new_rev = revision + 1
            new_values = dict(values)
            new_values[key] = value
            err = _write_overlay(self._overlay_path, new_rev, new_values)
            if err:
                return err
            back_values, back_rev, back_err = _read_overlay(self._overlay_path)
            if back_err:
                return f"{back_err}（写后读回验证失败）"
            if back_rev == new_rev and back_values == new_values:
                self._overlay_revision = new_rev
                with contextlib.suppress(OSError):
                    self._overlay_mtime = self._overlay_path.stat().st_mtime
                return None
            # 冲突：抢跑者的值已在文件里，下一轮并进来重写
        return (f"配置覆写冲突（{CAS_MAX_RETRIES} 次重试后仍有并发推进；"
                "请稍后重试）")

    def publish_reload(self) -> str | None:
        """递增 revision（值不变）——让其他进程重新读取全部配置层。

        用于配置文件被外部改动后的人工 reload：本进程 reload() 之外，也要
        把"重新读一次"这件事广播给其他进程。返回 None = 已广播。
        与 `_write_overlay_with` 同一套 CAS：写后读回验证，冲突重读重写
        （广播"重读"不能被并发发布抢掉——抢跑者的值才是文件里最新的值），
        同样整体在跨进程文件锁内执行。
        """
        with self._lock:
            try:
                with _overlay_file_lock(self._overlay_path):
                    return self._cas_publish()
            except TimeoutError as e:
                return f"配置发布锁等待超时：{e}"

    def _cas_publish(self) -> str | None:
        """发布循环本体（持文件锁调用）。"""
        for _attempt in range(CAS_MAX_RETRIES):
            values, revision, error = _read_overlay(self._overlay_path)
            if error:
                return error
            new_rev = revision + 1
            err = _write_overlay(self._overlay_path, new_rev, values)
            if err:
                return err
            back_values, back_rev, back_err = _read_overlay(self._overlay_path)
            if back_err:
                return back_err
            if back_rev == new_rev and back_values == values:
                self._overlay_revision = new_rev
                return None
        return (f"配置发布冲突（{CAS_MAX_RETRIES} 次重试后仍有并发推进；"
                "请稍后重试）")

    @property
    def revision(self) -> int:
        """当前已知的覆写 revision（进程启动/上次轮询读到的值）。"""
        return max(self._overlay_revision, 0)

    def poll_overlay(self) -> bool:
        """检查覆写文件是否被别的进程改过；改过则热加载并返回 True。

        每个轮询周期调一次即可（worker 循环/请求入口）。解析失败按既有
        "保留旧配置 + 不反复重解析"语义处理：记住坏 mtime，返回 False。
        """
        if not self._auto_reload:
            return False
        try:
            mtime = self._overlay_path.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._overlay_mtime:
            return False
        with self._lock:
            values, revision, error = _read_overlay(self._overlay_path)
            if error:
                self._overlay_mtime = mtime  # 记住坏版本，不反复重解析
                self.last_overlay_error = error
                logger.warning("配置覆写文件解析失败，保留旧配置: %s", error)
                return False
            if revision == self._overlay_revision:
                self._overlay_mtime = mtime
                return False  # 内容没变（例如 publish_reload 之外的同内容重写）
            self._load()
            self.last_overlay_error = ""
            return True

    def to_dict(self) -> dict:
        """导出全部配置"""
        self._check_reload()
        with self._lock:
            import copy
            return copy.deepcopy(self._data)

    def reload(self):
        """强制重新加载"""
        with self._lock:
            self._load()
