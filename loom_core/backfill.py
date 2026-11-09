"""补账：把 `projection='incomplete'` 的运行按交付账本重放一次终态写入。

为什么需要它：Stage 3b 的口径是"投影写失败标 incomplete 后继续"——观测面故障不拖垮
执行，但缺口必须能被**处理**，否则那个计数只会在健康检查里烂着。这个工具就是那个
"处理"：读交付账本（`deliveries.db`，唯一持久且完整的运行事实），把漏写的
`runs`/`run_nodes` 补回去，把队列行升格回 `ok`，并在事件流里留下一条 `run.replayed`
（事后补的必须与原始执行可区分）。

可靠性边界（这些是行为，不只是文档）：

  - **只碰 `marker=='incomplete'` 且队列已终态的行**。运行中的行不该被补账：它的投影
    还可能由执行方自己写成功；
  - **口径冲突不猜**：队列终态与账本状态不一致 ⇒ 拒绝并报两侧值，交给人判断；
  - **只补缺**：已存在的 `run_nodes` 行不覆盖（原始数据比事后重建的更细），
    `runs` 已是终态就不再 `close_run`；
  - **终态事件至多一条**由 `RunStore.close_run` 的条件 UPDATE 原子保证——带 `WHERE
    status NOT IN (终态)`，两步并发补账不会各写一条；
  - **不编数据**：账本只存节点的 `duration_sec`，没有绝对时间 ⇒ 补写的节点行
    `started_at`/`finished_at` 留 0（读侧因此不显示时长），事件里记
    `"times": "unavailable"`；
  - **失败就停在 incomplete**：marker 最后才升格，中途炸掉重跑即可（重跑是幂等的：
    缺的行补、已有的跳过、终态事件不会再发）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import run_store as _rs

#: 队列行状态里哪些算"运行已经结束"（补账的前提）
TERMINAL = _rs.TERMINAL_STATUSES


class RunBackfill:
    """补账器。三个依赖都是读口/窄写口，不持有任何缓存。"""

    def __init__(self, queue: Any, deliveries: Any,
                 store_factory: Callable[[], Any]):
        self._queue = queue
        self._deliveries = deliveries
        self._store_factory = store_factory

    # ── 入口 ─────────────────────────────────────────

    def replay_incomplete(self, limit: int = 200, dry_run: bool = False) -> dict:
        """把当前所有 `incomplete` 运行补一遍（按完成时间从旧到新）。"""
        rows = self._queue.list_by_projection("incomplete", limit=limit)
        results = [self.replay(str(row.get("task_id") or ""), dry_run=dry_run)
                   for row in rows]
        return {
            "checked": len(rows),
            "replayed": sum(1 for r in results if r["action"] in ("replayed", "dry-run")),
            "refused": [r for r in results if r["action"] == "refused"],
            "failed": [r for r in results if r["action"] == "failed"],
            "results": results,
        }

    def replay(self, run_id: str, dry_run: bool = False) -> dict:
        """补一条运行。返回 `action`：`replayed` / `dry-run` / `refused` / `failed`。"""
        result: dict[str, Any] = {
            "run_id": run_id, "action": "refused", "reason": "",
            "dry_run": dry_run, "created_run": False, "nodes_written": 0,
            "closed": False, "marker": "",
        }

        def _refuse(reason: str) -> dict:
            result["reason"] = reason
            return result

        row = self._queue.get(run_id) if run_id else None
        if not isinstance(row, dict):
            return _refuse("队列里没有这行（不是 worker 跑的运行，或 id 不对）")

        marker = str(row.get("projection") or "ok")
        result["marker"] = marker
        if marker != "incomplete":
            return _refuse(f"健康位是 {marker!r}，不需要补账")

        status = str(row.get("status") or "")
        if status not in TERMINAL:
            return _refuse(f"运行尚未终态（status={status!r}），补账会与执行方抢写")

        rec = self._deliveries.get(run_id)
        if not isinstance(rec, dict):
            return _refuse("交付账本里没有这条运行——没有可取的事实来源")
        ledger_status = str(rec.get("status") or "")
        if ledger_status and ledger_status != status:
            return _refuse(
                f"口径冲突：队列 status={status!r}，账本 status={ledger_status!r}；"
                "不猜哪个对，交给人判断")

        store = self._store_factory()
        if store is None:
            result["action"] = "failed"
            result["reason"] = "RunStore 不可用（写口建不起来），本次未补"
            return result

        existing = store.get_run(run_id)
        if (existing is not None and str(existing.get("status") or "") in TERMINAL
                and str(existing.get("status") or "") != status):
            return _refuse(
                f"口径冲突：投影 status={existing.get('status')!r}，"
                f"队列 status={status!r}")

        present = {str(n.get("node") or "") for n in store.nodes(run_id)}
        missing = [n for n in (rec.get("nodes") or [])
                   if str(n.get("node") or "") and str(n.get("node")) not in present]
        artifacts = sorted({str(k) for n in (rec.get("nodes") or [])
                            for k in (n.get("artifact_keys") or [])})

        # "已终态就别再盖章"只由存储层的条件 UPDATE 裁决：这里再判一次就是两套等价守卫，
        # 迟早分家。`will_close` 只用于 dry-run 的预告，真实结果看 close_run 的返回。
        will_close = not (existing is not None
                          and str(existing.get("status") or "") in TERMINAL)
        result["created_run"] = existing is None
        result["nodes_written"] = len(missing)
        result["closed"] = will_close

        if dry_run:
            result["action"] = "dry-run"
            result["reason"] = (f"将补写 runs 行={existing is None}、"
                                f"节点 {len(missing)} 条、终态事件={will_close}")
            return result

        try:
            if existing is None:
                store.open_run(run_id, workflow=str(rec.get("pipeline") or ""),
                               started_at=_float(rec.get("started_at")))
            if missing:
                store.record_nodes(run_id, [self._node_row(n) for n in missing])
            # 交付账本只存指纹，但成本与质量各有自己的持久化真值来源——补账
            # 同样要把它们捞回来，否则"补过的运行"和"活路径收尾的运行"在
            # `runs.cost_usd/quality_score` 上永远不是一个口径。
            from .run_metrics import collect_run_metrics
            cost_usd, quality_score = collect_run_metrics(run_id)
            seq = store.close_run(
                run_id, status,
                finished_at=_float(row.get("finished_at")) or _float(rec.get("finished_at")),
                error=str(row.get("error") or rec.get("error") or ""),
                artifacts=artifacts,
                cost_usd=cost_usd, quality_score=quality_score)
            result["closed"] = seq is not None
            store.record(run_id, _rs.RUN_REPLAYED, {
                "source": "deliveries",
                "created_run": existing is None,
                "nodes_written": len(missing),
                "closed": result["closed"],
                # 账本没有节点的绝对时间：补写行的时间列留 0，读侧不显示时长
                "times": "unavailable",
            })
        except Exception as e:  # noqa: BLE001  补账失败要能被重跑，不抛给调用方
            result["action"] = "failed"
            result["reason"] = f"写入失败（健康位保持 incomplete，可重跑）：{e}"
            return result

        upgraded = self._queue.set_projection(run_id, "ok")
        if not upgraded:
            result["action"] = "failed"
            result["reason"] = "投影已补，但队列健康位没能升格（行已不在？）"
            return result
        result["action"] = "replayed"
        result["reason"] = "已按交付账本补齐投影"
        return result

    # ── 形状转换 ─────────────────────────────────────

    @staticmethod
    def _node_row(node: dict) -> dict:
        """账本节点 → `record_nodes` 的行。不传 `result`：账本从没有正文。"""
        return {
            "node": str(node.get("node") or ""),
            "status": str(node.get("status") or ""),
            "attempts": int(node.get("attempts") or 1),
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": str(node.get("error") or ""),
            "artifact_keys": [str(k) for k in (node.get("artifact_keys") or [])],
        }


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
