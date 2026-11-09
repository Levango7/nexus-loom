"""RunMetrics — 收尾时把「成本 / 质量」两个真值来源读成一对指标。

`runs.cost_usd` 与 `runs.quality_score` 两列长期恒为 `DEFAULT 0`：`close_run()`
收这两个参数，但**两个生产调用点都不传**（`pipeline.py` 终态投影、
`backfill.py` 补账）——列在那里、读侧（`run_handle.py`）也读它，写侧却空着。

这里只做**读**，不做写：
- 成本的真值在 `CostTracker`（`cost.db` 的 `cost_log.task_id` 列）——由
  `llm_router` 在每次调用后记；归属靠 `cost_task_scope` 在节点执行期传播。
- 质量的真值在 `QualityFeedback`（`quality.db` 的 `quality_runs`）——由
  quality_gate 打分后经 `record_quality(overall=...)` 落库。

两者都是**独立持久化**的，所以补账路径也能补回真值（与交付账本只存指纹不同）。

读侧**不碰进程级单例**：`get_cost_tracker()` / `get_quality_feedback()` 把
store 绑定在「进程里第一次调用」时的 `LOOM_STATE_DIR` 上。测试常把该环境变量
指到 pytest tmp_path 再真跑流水线，tmp 目录随 pytest 清退后单例就冻死在一条
不存在的路上，此后任何 `record()`/读取都报 `unable to open database file`
（#120 CI 实证）。这里每次调用按**当前**环境变量的规范路径短构造
`CostTracker`/`QualityFeedback`、读完即 `close()`：读到的永远是当前状态目录
里的真值，与单例谁先出生、何时 reset 无关；生产路径上 env 稳定时两者同文件。

任一来源不可用（库坏了、还没建、目录没了）⇒ 该项返回 0.0 并记 warning，
**绝不让一次收尾因为指标读不到而失败**。
"""
from __future__ import annotations

import logging
from typing import Any

from . import state_paths

logger = logging.getLogger(__name__)


def _cost_of(task_id: str) -> float:
    from .cost_tracker import CostTracker
    tracker = CostTracker(db_path=state_paths.store_path("cost.db"))
    try:
        data: dict[str, Any] = tracker.stats_by_task(task_id)
    finally:
        tracker.close()
    return float(data.get("total_cost") or 0.0)


def _quality_of(task_id: str) -> float:
    from .quality_feedback import QualityFeedback
    feedback = QualityFeedback(db_path=state_paths.store_path("quality.db"))
    try:
        score = feedback.overall_for(task_id)
    finally:
        feedback.close()
    return float(score) if score is not None else 0.0


def collect_run_metrics(task_id: str) -> tuple[float, float]:
    """返回 ``(cost_usd, quality_score)``；任一项读不到按 0.0 处理。"""
    cost = 0.0
    quality = 0.0
    if task_id:
        try:
            cost = _cost_of(task_id)
        except Exception as e:  # noqa: BLE001  指标读不到不能让收尾失败
            logger.warning("读取运行成本失败（按 0 处理）: %s", e)
        try:
            quality = _quality_of(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取运行质量分失败（按 0 处理）: %s", e)
    return cost, quality
