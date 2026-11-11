"""空计划（node_count=0）结算守卫判据（§三十五 留档项的修复钉）。

退化 plan（node_count=0 但 levels 非空）在结算臂 ``completed / total_nodes``
处 ZeroDivisionError。守卫形态：total_nodes==0 时 progress 直接结算为 100
并记 WARN；非退化路径数学不变（原表达式逐字节保留）。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom_core.pipeline import PipelineOrchestrator, PipelineTask, TaskStatus


@pytest.fixture()
def orch(tmp_path):
    o = PipelineOrchestrator(checkpoint_dir=str(tmp_path / "ck"))
    o._logger = MagicMock()
    o.task_queue = MagicMock()
    o.approvals = MagicMock()
    return o


def _plan(levels):
    from loom_core.scheduler import ExecutionPlan

    return ExecutionPlan(plan_id="plan-empty", pipeline_name="demo",
                         levels=levels,
                         node_count=sum(len(x) for x in levels),
                         raw={"pipeline": {}})


def _node(name):
    from loom_core.scheduler import AgentConfig, ExecutionNode

    return ExecutionNode(agent_name=name, agent_config=AgentConfig(),
                         dependencies=[])


def _prep_async(o):
    """async 执行循环公共 mocks：闸门/暂停/结算 identity，收尾全 Mock。"""
    o.bus = MagicMock()
    o._executor = MagicMock()
    o._executor.execute_level_async = AsyncMock(return_value=True)
    o._arm_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
    o._settle_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
    o._pause_boundary_snapshot = MagicMock()
    o._observe_external_decision = MagicMock(return_value="waiting")
    o._runstore_open = MagicMock()
    o._runstore_nodes = MagicMock()
    o._save_checkpoint = MagicMock()
    o._merge_pooled_results = MagicMock()
    o._finalize_plan_task_async = MagicMock()


def _warned(o):
    return any(c.args and c.args[0] == "warning" and "空计划" in str(c.args[1])
               for c in o._logger.log.call_args_list)


def test_async_empty_level_settles_done_progress_100(orch):
    """node_count=0 + 空层：异步路径不炸，progress 结算为 100 并记 WARN。"""
    _prep_async(orch)
    task = asyncio.run(orch.run_plan_async(_plan([[]]), input_file="in.md",
                                           task_id="e1"))
    assert task.status == TaskStatus.DONE
    assert task.progress == 100
    assert task.current_step == 0
    assert _warned(orch)


def test_async_no_levels_list_is_done_with_warn(orch):
    """levels=[] 的退化 plan：入口即记 WARN，收尾 DONE。"""
    _prep_async(orch)
    task = asyncio.run(orch.run_plan_async(_plan([]), input_file="in.md",
                                           task_id="e2"))
    assert task.status == TaskStatus.DONE
    assert _warned(orch)


def test_sync_empty_level_settles_done_progress_100(orch):
    """同步内核 _execute_plan 同一守卫：空层结算臂不再除零。"""
    orch._arm_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
    orch._settle_approval_gate = MagicMock(side_effect=lambda t, p, lv: lv)
    orch._pause_boundary_snapshot = MagicMock()
    orch._observe_external_decision = MagicMock(return_value="waiting")
    orch._runstore_nodes = MagicMock()
    orch._save_checkpoint = MagicMock()
    task = PipelineTask(id="s1", pipeline_name="demo", input_file="in.md",
                        config={}, status=TaskStatus.RUNNING)
    orch._execute_plan(task, _plan([[]]), "in.md")
    assert task.status == TaskStatus.DONE
    assert task.progress == 100
    assert _warned(orch)


def test_non_degenerate_math_unchanged(orch):
    """非退化路径数学不变：两层各一节点，current_step=2、progress=100。"""
    _prep_async(orch)
    orch.registry = MagicMock()
    orch.registry.get_meta.return_value = None
    task = asyncio.run(orch.run_plan_async(
        _plan([[_node("a")], [_node("b")]]), input_file="in.md",
        task_id="e3"))
    assert task.status == TaskStatus.DONE
    assert task.current_step == 2
    assert task.progress == 100
