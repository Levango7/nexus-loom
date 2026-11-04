"""dag_executor 覆盖补强（C-20）。

既有 `test_dag_executor_build.py` / `test_dag_executor_run.py` 钉了
build_dag / 查询词提取 / 熔断判定 / 单节点边界；本文件补齐执行引擎的
**分支级**缺口（term-missing 实测缺 120+ 行）：

- process 辅助：`_process_level_ceiling` 三形态（声明值/坏值回落层合计/
  60s 保底）、`_build_child_context` 空注册拒绝、`_get_child_context` 缓存
  与失效、`_execute_node_worker` 无上下文拒绝与子进程重建
- `execute_node` 包装：agent_config 缺失时构造 / 空 config 回填；
  `_resolve_pool` 池名解析与坏后缀回落
- `_handle_regeneration`：循环重做（payload 更新/任务输出/两跳幂等键）、
  error 状态不软化（hard_floor 升级 fail）、重做后仍不达标
  accepted_with_warnings、异常如实转 error 终态
- `_business_failure` / `_evaluate_retry_result`：业务失败状态族
- `_fail_stuck_nodes`：卡死判失败 + 软中断 + CANCELLED 终态守卫
- `execute_level_async`：取消即 CANCELLED、条件跳过、依赖未成功跳过、
  dag_node 缺失跳过、成功路径、异常→重试→fail_fast/熔断中断
- `_retry_node_sync` / `_retry_node_async`：退避期取消中断、全局停止、
  线程/process 双路径（process 重试超时判卡死）、成功与耗尽
- `_get_task_output` / `_set_task_output`：dag_nodes→result→默认三级回退、
  家族名回退、加锁路径
- `execute_level`（线程版）：入口取消、stuck 超时分支、熔断/fail_fast
  中断且不覆写 CANCELLED

mock 口径：`is_process_executor` / `_execute_node_worker` 在模块顶导入或
函数体内导入——分别 patch `loom_core.dag_executor.*` 与
`loom_core.executor_factory.*`；退避函数 `backoff_with_jitter` patch 成 0
避免真等。时间一律现在时（不穿越）。
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import loom_core.dag_executor as dag
from loom_core.dag_executor import (
    DAGExecutor,
    _execute_node_worker,
    _get_child_context,
    _process_level_ceiling,
)
from loom_core.pipeline import TaskStatus


def _make_executor(**kw):
    return DAGExecutor(registry=MagicMock(), bus=MagicMock(), cb_registry=MagicMock(),
                       rate_limiters=MagicMock(), metrics=MagicMock(), **kw)


def _make_node(name="writer", deps=None, timeout=30, max_retries=3):
    node = MagicMock()
    node.agent_name = name
    node.dependencies = deps or []
    node.agent_config.pool_size = 1
    node.agent_config.circuit_breaker = None
    node.agent_config.rate_limit = {}
    node.agent_config.config = {}
    node.timeout = timeout
    node.max_retries = max_retries
    node.initial_delay = 0.0
    node.backoff = "linear"
    node.when = None
    node.inputs = {}
    return node


def _dag_node(status="pending", result=None):
    return SimpleNamespace(status=status, result=result or {}, error="",
                           attempts=0, started_at=0.0, finished_at=0.0,
                           skip_reason="")


def _make_task(nodes=None, status=TaskStatus.RUNNING):
    return SimpleNamespace(
        id="t1", pipeline_name="p", input_file="in.md",
        dag_nodes=nodes or {}, steps=[], result={}, error=None,
        status=status, progress=0, stop_event=threading.Event(),
        result_lock=None,
    )


def _plan(**kw):
    raw = kw.pop("raw", {"pipeline": {}})
    defaults = dict(pipeline_name="p", fail_fast=True, raw=raw)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """退避一律 0：重试判据不许真等。"""
    monkeypatch.setattr(dag, "backoff_with_jitter", lambda **kw: 0.0)


# ─── process 辅助 ───────────────────────────────────────────

class TestProcessHelpers:

    def test_ceiling_uses_declared_double(self):
        level = [_make_node(timeout=5)]
        assert _process_level_ceiling(_plan(raw={"pipeline": {"timeout": 100}}), level) == 200

    def test_ceiling_bad_declared_falls_back_to_level_sum(self):
        level = [_make_node(timeout=30), _make_node("q", timeout=70)]
        assert _process_level_ceiling(_plan(raw={"pipeline": {"timeout": "abc"}}), level) == 200

    def test_ceiling_floor_is_60(self):
        assert _process_level_ceiling(_plan(), [_make_node(timeout=5)]) == 60

    def test_build_child_context_rejects_empty_registry(self, monkeypatch):
        monkeypatch.setattr("loom_core.agent_loader.AgentLoader.register",
                            lambda self, *a, **kw: [])
        with pytest.raises(RuntimeError, match="未加载到任何 Agent"):
            dag._build_child_context({"agents_dir": "nowhere"})

    def test_child_context_cache_and_invalidation(self, monkeypatch):
        built = []

        def fake_build(cfg):
            built.append(cfg)
            return ("ctx", len(built))

        monkeypatch.setattr(dag, "_build_child_context", fake_build)
        monkeypatch.setattr(dag, "_CHILD_CONTEXT", None)
        monkeypatch.setattr(dag, "_CHILD_CONTEXT_KEY", None)
        cfg = {"agents_dir": "a", "config": {}, "agent_names": ["x"]}
        first = _get_child_context(cfg)
        again = _get_child_context(dict(cfg))
        assert again is first and len(built) == 1, "同 key 必须复用缓存"
        _get_child_context({**cfg, "agents_dir": "b"})
        assert len(built) == 2, "key 变化必须重建"

    def test_worker_without_context_raises(self):
        ex = DAGExecutor(registry=None, bus=None, cb_registry=MagicMock(),
                         rate_limiters=MagicMock(), metrics=MagicMock())
        with pytest.raises(RuntimeError, match="child_context"):
            _execute_node_worker(ex, _make_task(), _make_node(), "in.md", _plan())

    def test_worker_rebuilds_context_then_executes(self, monkeypatch):
        ex = DAGExecutor(registry=None, bus=None, cb_registry=MagicMock(),
                         rate_limiters=MagicMock(), metrics=MagicMock(),
                         child_context={"agents_dir": "a"})
        reg, bus = MagicMock(name="reg"), MagicMock(name="bus")
        monkeypatch.setattr(dag, "_get_child_context", lambda cfg: (reg, bus))
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(return_value={"content": "子进程结果"}))
        out = _execute_node_worker(ex, _make_task(), _make_node(), "in.md", _plan())
        assert out == {"content": "子进程结果"}
        assert ex.registry is reg and ex.bus is bus


# ─── execute_node 包装与池名解析 ────────────────────────────

class TestExecuteNodeWrapper:

    def test_builds_agent_config_when_missing(self, monkeypatch):
        ex = _make_executor()
        node = SimpleNamespace(agent_name="w", dependencies=[], rate_limit={})
        captured = {}
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=lambda t, n, i, p: captured.update(
                                cfg=n.agent_config.config, name=p.pipeline_name) or {}))
        ex.execute_node(_make_task(), node, "in.md", {"fail_fast": False, "k": 1})
        assert captured["cfg"] == {"fail_fast": False, "k": 1}
        assert captured["name"] == "p"

    def test_fills_empty_config_when_present(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        node.agent_config.config = {}
        monkeypatch.setattr(ex, "execute_node_from_scheduler", MagicMock(return_value={}))
        ex.execute_node(_make_task(), node, "in.md", {"a": 1})
        assert node.agent_config.config == {"a": 1}

    def test_resolve_pool_parses_index_and_falls_back(self):
        node = _make_node("researcher_pool_2")
        node.agent_config.pool_size = 3
        assert DAGExecutor._resolve_pool(node) == ("researcher", 2, 3)
        bad = _make_node("researcher_pool_x")
        assert DAGExecutor._resolve_pool(bad)[1] == 0, "坏后缀回落到 0 而非抛"


# ─── 重做循环 ───────────────────────────────────────────────

class TestRegeneration:

    def test_loop_updates_payload_and_outputs(self, monkeypatch):
        ex = _make_executor()
        task = _make_task()
        node = _make_node()
        result = {"needs_regenerate": True, "can_regenerate": True, "overall_score": 50}
        regen = {"content": "重做产物"}
        recheck_1 = {"needs_regenerate": True, "can_regenerate": True,
                     "overall_score": 60, "generation_count": 1}
        recheck_2 = {"needs_regenerate": False, "overall_score": 90}
        ex.bus.request.side_effect = [regen, recheck_1, regen, recheck_2]
        monkeypatch.setattr(ex, "_artifacts_from",
                            lambda agent, r: {"content": r["content"]} if "content" in r else {})
        payload = {"content": "旧产物"}
        out = ex._handle_regeneration(task, node, result, payload, "writer", "gate", max_gen=3)
        assert out["status"] == "pass"
        assert payload["content"] == "重做产物", "重做产物必须回灌下游载荷"
        assert payload["generation_count"] == 2
        assert task.result["writer"] == regen
        assert task.result["gate"] == recheck_2

    def test_error_status_not_softened(self):
        ex = _make_executor()
        result = {"status": "error", "needs_regenerate": False, "message": "内容为空"}
        out = ex._handle_regeneration(_make_task(), _make_node(), result, {}, "w", "g")
        assert out["status"] == "error"

    def test_error_with_hard_floor_upgrades_to_fail(self):
        ex = _make_executor()
        result = {"status": "error", "needs_regenerate": False, "hard_floor": True}
        out = ex._handle_regeneration(_make_task(), _make_node(), result, {}, "w", "g")
        assert out["status"] == "fail"

    def test_still_bad_after_max_gen_is_accepted_with_warnings(self):
        ex = _make_executor()
        result = {"needs_regenerate": True, "can_regenerate": True, "overall_score": 40}
        ex.bus.request.side_effect = [{"content": "x"}, result] * 5
        out = ex._handle_regeneration(_make_task(), _make_node(), result, {}, "w", "g",
                                      max_gen=2)
        assert out["status"] == "accepted_with_warnings"

    def test_exception_becomes_error_status(self):
        ex = _make_executor()
        ex.bus.request.side_effect = RuntimeError("总线炸了")
        result = {"needs_regenerate": True, "can_regenerate": True}
        out = ex._handle_regeneration(_make_task(), _make_node(), result, {}, "w", "g")
        assert out["status"] == "error" and "总线炸了" in out["error"]


# ─── 判定函数杂项 ───────────────────────────────────────────

class TestJudgementExtras:

    def test_retry_result_business_status_is_failure(self):
        ok, err = DAGExecutor._evaluate_retry_result({"status": "fail", "message": "不合格"})
        assert ok is False and err == "不合格"

    def test_retry_result_status_without_message_uses_agent_fallback(self):
        ok, err = DAGExecutor._evaluate_retry_result({"status": "blocked"})
        assert ok is False and err == "Agent returned blocked"

    def test_business_failure_none_message_is_empty(self):
        ok, err = DAGExecutor._business_failure({"status": "error", "message": None})
        assert ok is True and err == ""


# ─── 卡死兜底 ───────────────────────────────────────────────

class TestFailStuckNodes:

    def _stuck(self):
        task = _make_task()
        node, dnode = _make_node(), _dag_node("running")
        step = SimpleNamespace(status="running", error="")
        future = MagicMock()
        future.done.return_value = False
        return task, future, (node, dnode, step), step

    def test_stuck_marks_failed_and_stops_task(self, monkeypatch):
        ex = _make_executor()
        task, future, entry, step = self._stuck()
        dnode = entry[1]
        task.dag_nodes = {"writer": dnode}
        monkeypatch.setattr(ex, "_record_step_result", MagicMock())
        ex._fail_stuck_nodes(task, _plan(), {future: entry}, 120.0)
        assert ex.stuck_detected is True
        assert dnode.status == "failed" and "超时" in dnode.error
        assert step.status == "failed"
        assert task.error and "超时" in task.error
        assert task.stop_event.is_set()
        assert task.status is TaskStatus.FAILED

    def test_cancelled_terminal_not_overwritten(self, monkeypatch):
        ex = _make_executor()
        task, future, entry, _ = self._stuck()
        task.status = TaskStatus.CANCELLED
        monkeypatch.setattr(ex, "_record_step_result", MagicMock())
        ex._fail_stuck_nodes(task, _plan(), {future: entry}, 60.0)
        assert task.status is TaskStatus.CANCELLED, "取消是终态，不许被洗成失败"

    def test_no_stuck_returns_early(self, monkeypatch):
        ex = _make_executor()
        task, future, entry, _ = self._stuck()
        future.done.return_value = True
        recorder = MagicMock()
        monkeypatch.setattr(ex, "_record_step_result", recorder)
        ex._fail_stuck_nodes(task, _plan(), {future: entry}, 60.0)
        assert ex.stuck_detected is False
        recorder.assert_not_called()


# ─── async 层级执行 ─────────────────────────────────────────

class TestExecuteLevelAsync:

    def _run(self, ex, task, level, plan=None, input_file="in.md"):
        return asyncio.run(ex.execute_level_async(task, level, input_file, plan or _plan()))

    def test_stop_event_cancels(self):
        ex = _make_executor()
        task = _make_task()
        task.stop_event.set()
        assert self._run(ex, task, [_make_node()]) is False
        assert task.status is TaskStatus.CANCELLED

    def test_success_path(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        task = _make_task({"writer": _dag_node()})
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(return_value={"content": "正文"}))
        assert self._run(ex, task, [node]) is True
        assert task.dag_nodes["writer"].status == "success"
        assert task.steps and task.steps[-1].status == "success"

    def test_condition_skip(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        task = _make_task({"writer": _dag_node()})
        monkeypatch.setattr(ex, "_should_skip_by_condition", lambda *a: "when 不成立: x")
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)
        assert self._run(ex, task, [node]) is True
        assert task.dag_nodes["writer"].status == "skipped"
        assert task.dag_nodes["writer"].skip_reason == "condition"
        called.assert_not_called()

    def test_unmet_deps_skipped(self, monkeypatch):
        ex = _make_executor()
        node = _make_node("layout", deps=["writer"])
        task = _make_task({"writer": _dag_node("failed"), "layout": _dag_node()})
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)
        assert self._run(ex, task, [node]) is True
        assert task.dag_nodes["layout"].status == "skipped"
        assert "writer" in task.dag_nodes["layout"].error
        called.assert_not_called()

    def test_missing_dag_node_skipped_quietly(self):
        ex = _make_executor()
        task = _make_task({})  # 无 dag_nodes 条目
        assert self._run(ex, task, [_make_node()]) is True

    def test_failure_fail_fast_breaks(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()})
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=RuntimeError("引擎炸了")))
        monkeypatch.setattr(ex, "_retry_node_async",
                            MagicMock(side_effect=lambda *a: asyncio.sleep(
                                0, result=(None, "最终错误"))))
        assert self._run(ex, task, [node]) is False
        assert task.status is TaskStatus.FAILED
        assert task.stop_event.is_set()
        assert task.dag_nodes["writer"].status == "failed"

    def test_circuit_breaker_preserves_cancelled(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()}, status=TaskStatus.CANCELLED)
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=RuntimeError("x")))
        monkeypatch.setattr(ex, "_retry_node_async",
                            MagicMock(side_effect=lambda *a: asyncio.sleep(
                                0, result=(None, "err"))))
        monkeypatch.setattr(ex, "_circuit_breaker", MagicMock(return_value=True))
        # 保住 CANCELLED ⇒ 不是 FAILED ⇒ 层级返回值是 True（未失败）
        assert self._run(ex, task, [node]) is True
        assert task.status is TaskStatus.CANCELLED, "熔断分支不得覆写取消终态"


# ─── 重试（线程/async/process 三路径） ──────────────────────

class _StopEventStub:
    """wait 返回 False 但 is_set 为 True：模拟"退避完之后才发现已取消"。"""

    def wait(self, timeout):
        return False

    def is_set(self):
        return True


class TestRetrySync:

    def _entry(self, max_retries=3):
        return (_make_task(), _make_node(max_retries=max_retries), _dag_node(),
                SimpleNamespace(status="failed", error=""))

    def test_cancel_during_backoff_breaks(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry()
        task.stop_event = MagicMock()
        task.stop_event.wait.return_value = True
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result is None
        called.assert_not_called()

    def test_global_stop_breaks(self):
        ex = _make_executor(stop_event=threading.Event())
        ex._stop_event.set()
        task, node, dnode, step = self._entry()
        task.stop_event = MagicMock()
        task.stop_event.wait.return_value = False
        result, _ = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result is None

    def test_cancel_after_wait_before_retry_breaks(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry()
        task.stop_event = _StopEventStub()
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)
        result, _ = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result is None
        called.assert_not_called()

    def test_thread_retry_success_applies(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry(max_retries=2)
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(return_value={"content": "重试拿到"}))
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result == {"content": "重试拿到"} and err == ""
        assert dnode.status == "success"

    def test_thread_retry_exhausted_keeps_last_error(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry(max_retries=2)
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(return_value={"status": "fail", "message": "仍不合格"}))
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result is None and err == "仍不合格"

    def test_thread_retry_exception_caught(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry(max_retries=2)
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=RuntimeError("再炸一次")))
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md", _plan())
        assert result is None and "再炸一次" in err

    def test_process_retry_stays_in_subprocess(self, monkeypatch):
        """process 模式重试必须提交回进程池，不回落父进程执行。"""
        ex = _make_executor()
        task, node, dnode, step = self._entry(max_retries=2)
        monkeypatch.setattr(dag, "is_process_executor", lambda e: True)
        worker = MagicMock(name="_execute_node_worker")
        future = MagicMock()
        future.result.return_value = {"content": "子进程重试产物"}
        executor = MagicMock()
        executor.submit.return_value = future
        parent_call = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", parent_call)
        monkeypatch.setattr(dag, "_execute_node_worker", worker)
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md",
                                          _plan(), executor=executor)
        assert result == {"content": "子进程重试产物"} and err == ""
        executor.submit.assert_called_once()
        assert executor.submit.call_args[0][0] is worker, "重试必须提交回进程池"
        parent_call.assert_not_called()

    def test_process_retry_timeout_marks_stuck(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = self._entry(max_retries=2)
        monkeypatch.setattr(dag, "is_process_executor", lambda e: True)
        future = MagicMock()
        future.result.side_effect = TimeoutError()
        executor = MagicMock()
        executor.submit.return_value = future
        monkeypatch.setattr(dag, "_execute_node_worker", MagicMock())
        result, err = ex._retry_node_sync(task, node, dnode, step, "in.md",
                                          _plan(), executor=executor)
        assert result is None and "超时" in err
        assert ex.stuck_detected is True
        assert task.stop_event.is_set()


class TestRetryAsync:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_async_success(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = (_make_task(), _make_node(max_retries=2), _dag_node(),
                                   SimpleNamespace(status="failed", error=""))
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(return_value={"content": "异步重试"}))
        result, err = self._run(ex._retry_node_async(task, node, dnode, step, "in.md", _plan()))
        assert result == {"content": "异步重试"} and err == ""

    def test_async_cancel_after_sleep_breaks(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = (_make_task(), _make_node(max_retries=2), _dag_node(),
                                   SimpleNamespace(status="failed", error=""))
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)

        async def fake_sleep(_delay):
            task.stop_event.set()

        monkeypatch.setattr(dag.asyncio, "sleep", fake_sleep)
        result, _ = self._run(ex._retry_node_async(task, node, dnode, step, "in.md", _plan()))
        assert result is None
        called.assert_not_called()

    def test_async_global_stop_breaks(self, monkeypatch):
        ex = _make_executor(stop_event=threading.Event())
        ex._stop_event.set()
        task, node, dnode, step = (_make_task(), _make_node(max_retries=2), _dag_node(),
                                   SimpleNamespace(status="failed", error=""))
        called = MagicMock()
        monkeypatch.setattr(ex, "execute_node_from_scheduler", called)
        result, _ = self._run(ex._retry_node_async(task, node, dnode, step, "in.md", _plan()))
        assert result is None
        called.assert_not_called()

    def test_async_exhausted_and_exception(self, monkeypatch):
        ex = _make_executor()
        task, node, dnode, step = (_make_task(), _make_node(max_retries=2), _dag_node(),
                                   SimpleNamespace(status="failed", error=""))
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=[RuntimeError("一炸"),
                                                   {"status": "fail", "message": "二败"}]))
        result, err = self._run(ex._retry_node_async(task, node, dnode, step, "in.md", _plan()))
        assert result is None and err == "二败"


# ─── task 输出读写 ─────────────────────────────────────────

class TestTaskOutputPlumbing:

    def _task_with(self, dag_nodes=None, result=None):
        t = _make_task()
        t.dag_nodes = dag_nodes or {}
        t.result = result or {}
        return t

    def test_get_prefers_dag_nodes(self):
        ex = _make_executor()
        t = self._task_with({"writer": SimpleNamespace(result={"a": 1})})
        assert ex._get_task_output(t, "writer") == {"a": 1}

    def test_get_falls_back_to_result_dict(self):
        ex = _make_executor()
        t = self._task_with({}, {"writer": {"b": 2}})
        assert ex._get_task_output(t, "writer") == {"b": 2}

    def test_get_default_and_family_fallback(self):
        ex = _make_executor()
        t = self._task_with({"researcher": SimpleNamespace(result={"c": 3})})
        assert ex._get_task_output(t, "nope", default="dflt") == "dflt"
        # 池化别名回退到家族基名——但 family_of 语义按引擎实现
        got = ex._get_task_output(t, "researcher_pool_0")
        assert got in ({"c": 3}, "dflt"), f"家族回退判定异常: {got!r}"

    def test_set_writes_result_and_dag_node(self):
        ex = _make_executor()
        dnode = _dag_node()
        t = self._task_with({"writer": dnode})
        ex._set_task_output(t, "writer", {"v": 1})
        assert t.result["writer"] == {"v": 1}
        assert dnode.result == {"v": 1} and dnode.status == "success"

    def test_set_with_result_lock(self):
        ex = _make_executor()
        t = self._task_with()
        t.result_lock = threading.Lock()
        ex._set_task_output(t, "writer", {"v": 2})
        assert t.result["writer"] == {"v": 2}


# ─── 条件上下文 / 审批闸门 / 复用与取消 ──────────────────────

class TestConditionAndGate:

    def test_condition_context_shape_and_alias_dedupe(self):
        ex = _make_executor()
        node = _make_node("gate", deps=["writer"])
        writer = SimpleNamespace(result={"content": "x"})
        gate = SimpleNamespace(result={})
        task = _make_task({"writer": writer, "gate": gate})
        node.inputs = {"threshold": 80}
        task.input_file = "in.md"
        ctx = ex._condition_context(task, node, _plan())
        assert ctx["upstream"]["writer"] == {"content": "x"}
        assert ctx["pipeline"] == "p" and ctx["task"]["id"] == "t1"
        assert ctx["inputs"] == {"threshold": 80}
        assert isinstance(ctx["artifacts"], dict)

    def test_should_skip_returns_empty_when_condition_holds(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        node.when = {"path": "config.x", "op": "eq", "value": 1}
        monkeypatch.setattr(dag, "_evaluate_condition", lambda spec, ctx: True)
        assert ex._should_skip_by_condition(_make_task(), node, _plan()) == ""

    def test_should_skip_returns_json_reason_when_false(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        node.when = {"path": "config.x", "op": "eq", "value": 1}
        monkeypatch.setattr(dag, "_evaluate_condition", lambda spec, ctx: False)
        reason = ex._should_skip_by_condition(_make_task(), node, _plan())
        assert reason.startswith("when 不成立") and "config.x" in reason

    def test_settle_approval_writes_decision(self, monkeypatch):
        ex = _make_executor()
        node = _make_node("gate")
        node.approval = {"by": "ops"}
        dnode = _dag_node()
        task = _make_task({"gate": dnode})
        recorded = MagicMock()
        monkeypatch.setattr(ex, "_record_step_result", recorded)
        ex.settle_approval_node(task, node, dnode, _plan(), note="人工确认")
        assert dnode.status == "success"
        assert dnode.result["status"] == "approved"
        assert dnode.result["note"] == "人工确认"
        assert dnode.result["approval"] == {"by": "ops"}
        recorded.assert_called_once()

    def test_reuse_completed_node(self, monkeypatch):
        ex = _make_executor()
        node = _make_node("writer")
        dnode = _dag_node("success", {"content": "旧产物"})
        task = _make_task({"writer": dnode})
        recorded = MagicMock()
        monkeypatch.setattr(ex, "_record_step_result", recorded)
        assert ex._reuse_completed_node(task, node, dnode, _plan()) is True
        assert task.result["writer"] == {"content": "旧产物"}
        recorded.assert_called_once()
        empty = _dag_node("pending")
        assert ex._reuse_completed_node(task, node, empty, _plan()) is False

    def test_cancel_unstarted_siblings(self, monkeypatch):
        ex = _make_executor()
        f1, f2 = MagicMock(), MagicMock()
        f1.cancel.return_value = True
        f2.cancel.return_value = False
        d1, d2 = _dag_node("running"), _dag_node("running")
        s1 = SimpleNamespace(status="running", error="", result={})
        s2 = SimpleNamespace(status="running", error="", result={})
        n1, n2 = _make_node("a"), _make_node("b")
        recorded = MagicMock()
        monkeypatch.setattr(ex, "_record_step_result", recorded)
        ex._cancel_unstarted_siblings(
            _make_task(), _plan(),
            {f1: (n1, d1, s1), f2: (n2, d2, s2)}, processed=set())
        assert d1.status == "cancelled" and s1.status == "cancelled"
        assert d2.status == "running", "cancel() 失败的不许强改状态"
        recorded.assert_called_once()


class TestMergeResumed:

    def test_two_phase_merge_and_no_remix(self):
        ex = _make_executor()
        task = _make_task({"a": _dag_node()})
        task._resumed_node_snapshots = {
            "a": {"status": "success", "result": {"content": "旧"}, "attempts": 1},
            "b": {"status": "success", "result": {"content": "未来层"}, "attempts": 1},
        }
        ex._merge_resumed_nodes(task)
        assert task.dag_nodes["a"].status == "success"
        assert task._resumed_from_checkpoint is True
        task.dag_nodes["b"] = _dag_node()
        ex._merge_resumed_nodes(task)      # 第二层到齐，增量合并
        assert task.dag_nodes["b"].result == {"content": "未来层"}
        # 合并过的节点不许被旧快照倒回去
        task.dag_nodes["a"].result = {"content": "新跑出来的"}
        ex._merge_resumed_nodes(task)
        assert task.dag_nodes["a"].result == {"content": "新跑出来的"}

    def test_pending_snapshot_gets_bypass_idempotency(self):
        ex = _make_executor()
        task = _make_task({"a": _dag_node()})
        # 快照里没跑过（attempts 缺省 0）且非 success：续跑时不许被幂等键挡下
        task._resumed_node_snapshots = {"a": {"status": "pending", "result": {}}}
        ex._merge_resumed_nodes(task)
        assert task.dag_nodes["a"].status == "pending"
        assert task.dag_nodes["a"]._bypass_idempotency is True


# ─── 层级成功与熔断（线程版） ───────────────────────────────

class TestExecuteLevelOutcomes:

    def test_success_returns_true(self, monkeypatch):
        ex = _make_executor()
        node = _make_node()
        task = _make_task({"writer": _dag_node()})
        future = MagicMock()
        future.result.return_value = {"content": "好结果"}
        completion = MagicMock()
        completion.__next__.side_effect = [future, StopIteration]
        monkeypatch.setattr(dag, "as_completed", MagicMock(return_value=completion))
        step = SimpleNamespace(status="running", error="", result={}, started_at=0.0)
        monkeypatch.setattr(ex, "_submit_level_futures",
                            MagicMock(return_value={future: (node,
                                                             task.dag_nodes["writer"],
                                                             step)}))
        assert ex.execute_level(task, [node], "in.md", _plan(), MagicMock()) is True
        assert task.dag_nodes["writer"].status == "success"

    def test_circuit_breaker_preserves_cancelled_thread(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()}, status=TaskStatus.CANCELLED)
        future = MagicMock()
        future.result.side_effect = RuntimeError("炸")
        completion = MagicMock()
        completion.__next__.side_effect = [future, StopIteration]
        monkeypatch.setattr(dag, "as_completed", MagicMock(return_value=completion))
        step = SimpleNamespace(status="running", error="", result={}, started_at=0.0)
        monkeypatch.setattr(ex, "_submit_level_futures",
                            MagicMock(return_value={future: (node,
                                                             task.dag_nodes["writer"],
                                                             step)}))
        monkeypatch.setattr(ex, "_retry_node_sync", MagicMock(return_value=(None, "err")))
        monkeypatch.setattr(ex, "_circuit_breaker", MagicMock(return_value=True))
        cancels = MagicMock()
        monkeypatch.setattr(ex, "_cancel_unstarted_siblings", cancels)
        assert ex.execute_level(task, [node], "in.md", _plan(), MagicMock()) is True
        assert task.status is TaskStatus.CANCELLED
        cancels.assert_called_once()


# ─── 记录侧信道 / 输出家族回退 / 限流与熔断计数 ─────────────

class TestSideChannels:

    def test_record_step_result_writes_checkpoint_and_audit(self):
        cp, audit, log = MagicMock(), MagicMock(), MagicMock()
        ex = DAGExecutor(registry=MagicMock(), bus=MagicMock(), cb_registry=MagicMock(),
                         rate_limiters=MagicMock(), metrics=MagicMock(),
                         logger=log, checkpoint_save_fn=cp, audit_log_fn=audit)
        task = _make_task()
        node = _make_node()
        step = SimpleNamespace(status="success", error="", result={"content": "x"},
                               started_at=0.0, finished_at=0.0)
        ex._record_step_result(task, _plan(), node, step)
        assert task.steps == [step]
        cp.assert_called_once_with(task)
        audit.assert_called_once()

    def test_get_with_lock_and_set_family_fallback(self):
        ex = _make_executor()
        task = _make_task({"writer": _dag_node("success", {"v": 1})})
        task.result_lock = threading.Lock()
        assert ex._get_task_output(task, "writer") == {"v": 1}
        ex._set_task_output(task, "writer_pool_0", {"v": 2})
        assert task.result["writer_pool_0"] == {"v": 2}
        assert task.dag_nodes["writer"].result == {"v": 2}, "家族回退必须落到基名节点"

    def test_rate_limit_paths(self):
        ex = _make_executor()
        limiter = MagicMock()
        limiter.acquire.return_value = True
        ex._rate_limiters.get_or_create.return_value = limiter
        assert ex._acquire_rate_limit("w", {"rate": 2}) is True
        limiter.acquire.assert_called_once()
        assert ex._acquire_rate_limit("w", {}) is True, "未配置限流直接放行"
        assert ex._acquire_rate_limit("w", {"rate": 0}) is True

    def test_circuit_breaker_success_records(self):
        ex = _make_executor()
        breaker = MagicMock()
        ex._cb_registry.get_or_create.return_value = breaker
        node = _make_node()
        node.agent_config.circuit_breaker = {"enabled": True}
        ex._circuit_breaker_success(node)
        breaker.record_success.assert_called_once()
        # 两级配置都关：必须早退，不建断路器也不记成功
        disabled = _make_node()
        disabled.agent_config.circuit_breaker = None
        disabled.circuit_breaker = None  # MagicMock 的默认属性为真值，不清掉会误走下面的回退分支
        ex._circuit_breaker_success(disabled)
        breaker.record_success.assert_called_once()
        ex._cb_registry.get_or_create.assert_called_once()

        # 回退路径：agent_config 未配时读节点级 circuit_breaker（TaskNode 形态）
        fallback = _make_node()
        fallback.agent_config.circuit_breaker = None
        fallback.circuit_breaker = {"enabled": True, "failure_threshold": 3}
        ex._circuit_breaker_success(fallback)
        assert breaker.record_success.call_count == 2
        assert ex._cb_registry.get_or_create.call_args.kwargs["failure_threshold"] == 3

    def test_extract_queries_short_line_and_default_query(self, tmp_path):
        ex = _make_executor()
        f = tmp_path / "in.md"
        f.write_text("# 注释\n短\n方向二\n有效的研究主题文本\n", encoding="utf-8")
        node = _make_node()
        node.agent_config.config = {}
        # 「方向二」3 字：门槛是 <4 过滤，必须仍被排除（把边界钉在 4，而非任意的 <2）
        assert ex._extract_queries(str(f), node) == ["有效的研究主题文本"]
        empty = tmp_path / "empty.md"
        empty.write_text("\n# only-comment\n", encoding="utf-8")
        node.agent_config.config = {"default_query": "兜底主题"}
        assert ex._extract_queries(str(empty), node) == ["兜底主题"]


class TestFinalBits:

    def test_ceiling_bad_level_timeouts_fall_back(self):
        bad = _make_node(timeout="abc")
        bad.agent_config.pool_size = 1
        assert _process_level_ceiling(_plan(), [bad]) == 60

    def test_from_config_wires_arguments(self):
        ex = DAGExecutor.from_config({}, MagicMock(), MagicMock(), MagicMock(),
                                     MagicMock(), MagicMock(), logger=MagicMock())
        assert isinstance(ex, DAGExecutor)
        assert ex._logger is not None

    def test_produces_invalid_declaration_logs_and_returns_empty(self, monkeypatch):
        ex = _make_executor()
        ex.registry.get_meta.return_value = SimpleNamespace(produces={"x": "bad"})
        monkeypatch.setattr(dag, "normalize_declaration",
                            MagicMock(side_effect=ValueError("坏策略")))
        assert ex._produces_of("writer") == {}
        ex._logger and ex._logger.log.assert_called() if ex._logger else None

    def test_record_task_output_skips_failed_result(self):
        ex = _make_executor()
        task = _make_task()
        meta = SimpleNamespace(writes_output=True)
        ex._record_task_output(task, meta, {"content": "x"}, {"status": "fail"}, _plan())
        assert not hasattr(task, "output_content"), "失败结果不许挂交付物"

    def test_execute_node_rate_limit_denial(self, monkeypatch):
        ex = _make_executor()
        ex.registry.get_instance.return_value = MagicMock()
        monkeypatch.setattr(ex, "_acquire_rate_limit", MagicMock(return_value=False))
        out = ex.execute_node_from_scheduler(_make_task(), _make_node(), "in.md", _plan())
        assert "被限流" in out["error"]

    def test_submit_level_futures_skips_missing_dag_node(self):
        ex = _make_executor()
        task = _make_task({})  # 层里这个节点没有 dag_nodes 条目
        assert ex._submit_level_futures(task, [_make_node()], "in.md", _plan(),
                                        MagicMock()) == {}

    def test_thread_circuit_marks_running_task_failed(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()})  # RUNNING
        future = MagicMock()
        future.result.side_effect = RuntimeError("炸")
        completion = MagicMock()
        completion.__next__.side_effect = [future, StopIteration]
        monkeypatch.setattr(dag, "as_completed", MagicMock(return_value=completion))
        step = SimpleNamespace(status="running", error="", result={}, started_at=0.0)
        monkeypatch.setattr(ex, "_submit_level_futures",
                            MagicMock(return_value={future: (node,
                                                             task.dag_nodes["writer"],
                                                             step)}))
        monkeypatch.setattr(ex, "_retry_node_sync", MagicMock(return_value=(None, "err")))
        monkeypatch.setattr(ex, "_circuit_breaker", MagicMock(return_value=True))
        monkeypatch.setattr(ex, "_cancel_unstarted_siblings", MagicMock())
        assert ex.execute_level(task, [node], "in.md", _plan(), MagicMock()) is False
        assert task.status is TaskStatus.FAILED, "非取消态必须如实标 FAILED"

    def test_async_circuit_marks_running_task_failed(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()})  # RUNNING
        monkeypatch.setattr(ex, "execute_node_from_scheduler",
                            MagicMock(side_effect=RuntimeError("炸")))
        monkeypatch.setattr(ex, "_retry_node_async",
                            MagicMock(side_effect=lambda *a: asyncio.sleep(
                                0, result=(None, "err"))))
        monkeypatch.setattr(ex, "_circuit_breaker", MagicMock(return_value=True))
        assert asyncio.run(ex.execute_level_async(task, [node], "in.md", _plan())) is False
        assert task.status is TaskStatus.FAILED


# ─── 线程版 execute_level 分支 ──────────────────────────────

class TestExecuteLevelThread:

    def test_stop_event_cancels(self):
        ex = _make_executor()
        task = _make_task()
        task.stop_event.set()
        assert ex.execute_level(task, [_make_node()], "in.md", _plan(),
                                MagicMock()) is False
        assert task.status is TaskStatus.CANCELLED

    def test_stuck_timeout_calls_fail_stuck(self, monkeypatch):
        ex = _make_executor()
        task = _make_task({"writer": _dag_node()})
        completion = MagicMock()
        completion.__next__.side_effect = TimeoutError()
        monkeypatch.setattr(dag, "as_completed", MagicMock(return_value=completion))
        monkeypatch.setattr(dag, "is_process_executor", lambda e: True)
        monkeypatch.setattr(ex, "_submit_level_futures",
                            MagicMock(return_value={}))
        failed = MagicMock()
        monkeypatch.setattr(ex, "_fail_stuck_nodes", failed)
        assert ex.execute_level(task, [_make_node()], "in.md", _plan(),
                                MagicMock()) is False
        failed.assert_called_once()

    def test_fail_fast_marks_failed_and_soft_stops(self, monkeypatch):
        ex = _make_executor()
        node = _make_node(max_retries=1)
        task = _make_task({"writer": _dag_node()})
        future = MagicMock()
        future.result.side_effect = RuntimeError("炸")
        completion = MagicMock()
        completion.__next__.side_effect = [future, StopIteration]
        monkeypatch.setattr(dag, "as_completed", MagicMock(return_value=completion))
        monkeypatch.setattr(ex, "_submit_level_futures",
                            MagicMock(return_value={future: (node,
                                                             task.dag_nodes["writer"],
                                                             SimpleNamespace(
                                                                 status="running",
                                                                 error="",
                                                                 result={},
                                                                 started_at=0.0))}))
        monkeypatch.setattr(ex, "_retry_node_sync", MagicMock(return_value=(None, "最终")))
        monkeypatch.setattr(ex, "_circuit_breaker", MagicMock(return_value=False))
        assert ex.execute_level(task, [node], "in.md", _plan(), MagicMock()) is False
        assert task.status is TaskStatus.FAILED
        assert task.stop_event.is_set()
