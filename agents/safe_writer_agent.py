"""SafeWriter Agent v2 - 安全写入插件"""
import contextlib
import hashlib
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "safe_writer"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True

# 配置契约：类型名用字符串写，好让 Scheduler 用 AST 读取而不必执行本模块
# （类型名表见 loom_core/config_schema.py）
CONFIG_SCHEMA = {
    "backup_dir": ('str', 'backups'),
    "atomic": ('bool', True),
}
AGENT_VERSION = "2.0"
AGENT_DESC = "安全写入 Agent - 备份+验证+原子替换+过期清理"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 99
INPUT_TOPICS = ["writer.done", "layout.done", "safe_writer.write", "safe_writer.input", "safewriter.input"]
OUTPUT_TOPICS = ["safe_writer.done", "safe_writer.failed"]
# 产物契约（引擎按此声明组装下游载荷，见 loom_core/artifacts.py）：终态落盘，无下游产物
# 本节点是流水线的落盘终点：引擎据此把交付物挂到 task.output_*
WRITES_OUTPUT = True
PRODUCES: dict = {}
CONSUMES = ["content"]
DEPENDENCIES = ["layout"]
CACHE_TTL = 0
RESPAWN = False


class SafeWriterAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self.pending: dict = {}
        self._default_backup_dir = config.get("backup_dir", "backups")
        self._manifest_lock = threading.Lock()
        self.log_info(f"SafeWriter v{AGENT_VERSION} 初始化完成")

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "准备写入...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        content = payload.get("content", "")
        target = payload.get("target", payload.get("target_file", ""))
        backup_dir = payload.get("backup_dir", self._default_backup_dir)
        reason = payload.get("reason", "plugin")
        quality_score = payload.get("quality_score", 0.0)
        if not target:
            return {"status": "error", "message": "未指定目标文件"}
        if not content:
            return {"status": "error", "message": "内容为空"}
        result = self._safe_write(target, content, backup_dir, reason, task_id,
                                  quality_score=quality_score)
        self.publish("safe_writer.done" if result.get("status") == "ok" else "safe_writer.failed",
                     {"task_id": task_id, "target": target, "result": result})
        return result

    def _safe_write(self, target: str, content: str, backup_dir: str, reason: str,
                    task_id: str, quality_score: float = 0.0) -> dict:
        import os
        target = str(Path(target).resolve())
        backup_dir = str(Path(backup_dir).resolve())
        manifest_path = Path(backup_dir) / "manifest.json"
        info = self._get_info(target)
        self.log_info(f"目标: {target}")
        backup_path = None
        if info.get("exists"):
            backup_path = str(Path(backup_dir) / f"{Path(target).stem}_{datetime.now():%Y%m%d_%H%M%S}{Path(target).suffix}")
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            # 用 copy 而非 copy2：copy2 会把目标文件的 mtime 一并复制到备份，
            # 而 _cleanup 按备份文件自身 mtime 判 TTL——目标文件超过
            # backup_ttl_days 未修改时，刚创建的备份会立即被清理误删。
            # copy() 不复制 mtime，备份 mtime = 创建时刻，TTL 语义正确。
            shutil.copy(target, backup_path)
            self.log_info(f"备份: {Path(backup_path).name} ({info['size']:,} bytes)")
        enc = "utf-8-sig" if Path(target).suffix.lower() in {".csv", ".tsv"} else "utf-8"
        # 确保目标目录存在
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = str(Path(target).parent or ".")
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=Path(target).suffix, prefix=".tmp_", dir=tmp_dir)
        os.close(fd)
        try:
            try:
                content_bytes = content.encode(enc)
                with open(tmp_path, "wb") as f:
                    f.write(content_bytes)
                new_size = len(content_bytes)
                new_lines = content.count("\n") + 1
                self.log_info(f"写入临时: {new_size:,} bytes, {new_lines} 行")
            except Exception as e:
                return {"status": "error", "message": f"写入失败: {e}"}
            issues = []
            if len(content.strip()) == 0:
                issues.append("P0: 内容为空")
            if info.get("exists") and new_size > 0:
                r = new_size / info["size"]
                if r < 0.5:
                    issues.append(f"P1: 文件<50% ({info['size']:,}->{new_size:,})")
                if r > 5.0:
                    issues.append(f"P1: 文件>5倍 ({info['size']:,}->{new_size:,})")
            if issues:
                for iss in issues:
                    self.log_warning(f"验证失败: {iss}")
                return {"status": "error", "issues": issues, "backup": backup_path}
            try:
                os.replace(tmp_path, target)
                tmp_path = None  # type: ignore[assignment]
                with self._manifest_lock:
                    man = self._load_manifest(manifest_path)
                    rel = target
                    if rel not in man["files"]:
                        man["files"][rel] = {"backups": [], "latest": None}
                    man["files"][rel]["backups"].append({
                        "path": backup_path,
                        "timestamp": datetime.now().isoformat(),
                        "size": info.get("size", 0),
                        "reason": reason,
                        "agent": self.name,
                        "task_id": task_id,
                    })
                    man["files"][rel]["latest"] = man["files"][rel]["backups"][-1]
                    self._save_manifest(manifest_path, man)
                self._cleanup(backup_dir, manifest_path, target)
                # ── 版本管理：写入成功后自动提交版本 ──
                version_info = None
                try:
                    from loom_core.version_manager import get_version_manager
                    vm = get_version_manager()
                    version_info = vm.commit(
                        target, content,
                        task_id=task_id,
                        quality_score=quality_score,
                        message=f"SafeWriter 自动提交 (reason={reason})",
                    )
                    self.log_info(f"版本: v{version_info['version']}")
                except Exception as e:
                    self.log_warning(f"版本管理提交失败（不影响写入）: {e}")
                self.log_info(f"完成: {target} ({new_size:,} bytes)")
                result = {"status": "ok", "backup": backup_path, "size": new_size, "lines": new_lines}
                if version_info:
                    result["version"] = version_info["version"]
                return result
            except Exception as e:
                self.log_error(f"替换失败: {e}")
                return {"status": "error", "message": str(e)}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(Exception):
                    os.remove(tmp_path)

    def _get_info(self, path):
        if not Path(path).exists():
            return {"exists": False}
        st = os.stat(path)
        size = st.st_size
        sha256 = hashlib.sha256()
        lines = 1
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                sha256.update(chunk)
                lines += chunk.count(b"\n")
        return {"exists": True, "size": size, "lines": lines, "sha256": sha256.hexdigest()}

    def _load_manifest(self, path):
        if not Path(path).exists():
            return {"files": {}, "version": "2.0"}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            bak = Path(str(path) + ".bak")
            if bak.exists():
                try:
                    with open(bak, encoding="utf-8") as f:
                        restored = json.load(f)
                    self.log_warning(f"manifest 损坏，已从 {bak.name} 恢复: {e}")
                    return restored
                except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e2:
                    self.log_warning(f"manifest 备份 {bak.name} 也无法恢复: {e2}")
            self.log_warning(f"manifest 损坏且无可用备份，使用空 manifest 全新写入: {e}")
            return {"files": {}, "version": "2.0"}

    def _save_manifest(self, path, data):
        manifest = Path(path)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if manifest.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(str(manifest), str(manifest) + ".bak")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _cleanup(self, backup_dir: str, manifest_path: Path, target: str):
        ttl_days = self.config.get("backup_ttl_days", 7)
        max_backups = self.config.get("max_backups", 20)
        cutoff = time.time() - ttl_days * 86400

        def mtime_of(entry: dict) -> float:
            with contextlib.suppress(OSError):
                return Path(entry["path"]).stat().st_mtime
            return 0.0

        try:
            with self._manifest_lock:
                man = self._load_manifest(manifest_path)
                info = man.get("files", {}).get(target)
                if not info:
                    return
                backups = [b for b in info.get("backups", [])
                           if isinstance(b, dict) and b.get("path")]
                fresh = [b for b in backups
                         if Path(b["path"]).exists() and mtime_of(b) >= cutoff]
                drop_ids = {id(b) for b in backups} - {id(b) for b in fresh}
                if len(fresh) > max_backups:
                    excess = sorted(fresh, key=mtime_of)[: len(fresh) - max_backups]
                    drop_ids.update(id(b) for b in excess)
                kept = [b for b in backups if id(b) not in drop_ids]
                if kept != backups:
                    for b in backups:
                        if id(b) in drop_ids:
                            with contextlib.suppress(OSError):
                                os.remove(b["path"])
                                self.log_info(f"清理备份: {Path(b['path']).name}")
                    info["backups"] = kept
                    info["latest"] = kept[-1] if kept else None
                    self._save_manifest(manifest_path, man)
        except Exception as e:
            self.log_error(f"清理失败: {e}")

    def handle_writer_done(self, msg: Message):
        """Writer 完成后触发安全写入。

        修复 P0：原实现只把消息缓存到 self.pending，从不消费 pending，
        导致 pending 无限增长（内存泄漏）且文档永远不写入。
        现改为：缓存后立即执行写入并从 pending 移除（消费）。
        """
        payload = msg.payload
        task_id = payload.get("task_id", "")
        content = payload.get("content", "")
        target = payload.get("target_file", "") or payload.get("target", "")
        if not target:
            target = self.config.get("default_target", "")

        # 缓存（保留原语义，供合并/查询/调试）
        self.pending[task_id] = {"content": content, "target": target, "timestamp": time.time()}

        # 实际执行写入并消费 pending（修复 P0：原实现到此就返回，pending 无限增长）
        if not target or not content:
            self.log_warning(
                f"handle_writer_done 跳过写入: task_id={task_id} "
                f"target={'有' if target else '无'} content={'有' if content else '无'}"
            )
            # 无效消息仍从 pending 移除，避免堆积
            self.pending.pop(task_id, None)
            return

        quality_score = payload.get("quality_score", 0.0)
        backup_dir = payload.get("backup_dir", self._default_backup_dir)
        reason = payload.get("reason", "writer_done")
        result = self._safe_write(target, content, backup_dir, reason, task_id,
                                  quality_score=quality_score)

        # 消费 pending：写入完成后移除，避免无限增长
        self.pending.pop(task_id, None)

        self.publish(
            "safe_writer.done" if result.get("status") == "ok" else "safe_writer.failed",
            {"task_id": task_id, "target": target, "result": result},
        )
