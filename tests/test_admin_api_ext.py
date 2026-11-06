"""admin_api 补充测试 — 补齐覆盖率薄弱区（原 52%）。

与 test_admin_api.py 互补，覆盖：
- 安全校验纯函数：mask_token_query / _is_private_ip / _validate_webhook_url /
  _validate_output_path / _validate_task_id
- _StreamMetricsHub 聚合 + 模块级 track/untrack/snapshot
- do_GET / do_POST / do_DELETE / do_OPTIONS 路由分发
- 各 handler：health / metrics / list_tasks / get_task（含输出文件读取与过大省略）/
  submit_task 成功与失败路径 / rerun / dashboard / pipeline / agent_detail /
  config get/set/reload / health_deep / cache stats+clear / alerts / logs /
  versions list/diff/rollback/stats / hooks 注册与注销 / stream metrics
- _attach/_detach_stream_callback、_schedule_input_cleanup
"""
import io
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from loom_core.admin_api import (
    AdminHandler,
    _is_private_ip,
    _StreamMetricsHub,
    _validate_output_path,
    _validate_task_id,
    _validate_webhook_url,
    mask_token_query,
    stream_metrics_snapshot,
    track_stream_callback,
    untrack_stream_callback,
)


@pytest.fixture
def handler():
    h = AdminHandler.__new__(AdminHandler)
    h.orch = MagicMock()
    # 交付流水线名的解析归引擎：外壳只拿 (名字, 错误)
    h.orch.resolve_pipeline_name.return_value = ("docgen", "")
    h._json = MagicMock()
    h.headers = {}
    h.path = "/"
    h.api_key = None
    h.server_host = "127.0.0.1"
    h.dashboard_dir = None
    return h


# ─── 安全校验纯函数 ────────────────────────────────────────

class TestValidators:
    def test_mask_token_query(self):
        assert mask_token_query("GET /stream?token=secret123&x=1") == \
            "GET /stream?token=***&x=1"
        assert mask_token_query("no token here") == "no token here"

    def test_is_private_ip(self):
        assert _is_private_ip("127.0.0.1")
        assert _is_private_ip("10.1.2.3")
        assert _is_private_ip("192.168.0.1")
        assert _is_private_ip("169.254.169.254")  # 云元数据端点
        assert _is_private_ip("::1")
        assert _is_private_ip("fc00::1")
        assert not _is_private_ip("8.8.8.8")
        assert not _is_private_ip("example.com")  # 域名不在此判定

    def test_validate_webhook_url(self):
        ok, _ = _validate_webhook_url("https://hooks.example.com/cb")
        assert ok
        assert not _validate_webhook_url("")[0]
        assert not _validate_webhook_url("ftp://x.com")[0]
        assert not _validate_webhook_url("https://")[0]  # 无 host
        assert not _validate_webhook_url("http://127.0.0.1/x")[0]
        assert not _validate_webhook_url("http://169.254.169.254/meta")[0]

    def test_validate_output_path(self, tmp_path):
        ok, _ = _validate_output_path("", str(tmp_path))
        assert ok  # 空路径不校验
        ok, _ = _validate_output_path(str(tmp_path / "out.md"), str(tmp_path))
        assert ok
        ok, reason = _validate_output_path(str(tmp_path / "out.md"),
                                           str(tmp_path / "other"))
        assert not ok and "不在允许目录" in reason

    def test_validate_task_id(self):
        assert _validate_task_id("task-001_ABC")
        assert not _validate_task_id("")
        assert not _validate_task_id("a" * 129)
        assert not _validate_task_id("../etc/passwd")
        assert not _validate_task_id("id with space")


class TestStreamMetricsHub:
    def _cb(self, emitted=5, dropped=1, chunks=3, sections=2, latency=0.4):
        cb = MagicMock()
        cb.metrics.snapshot.return_value = {
            "events_emitted": emitted, "events_dropped": dropped,
            "chunks_emitted": chunks, "sections_emitted": sections,
            "avg_latency": latency,
        }
        return cb

    def test_snapshot_aggregates(self):
        hub = _StreamMetricsHub()
        hub.track("t1", self._cb())
        hub.track("t2", self._cb(emitted=3, latency=0.2))
        snap = hub.snapshot()
        assert snap["active_streams"] == 2
        assert snap["events_emitted"] == 8
        assert snap["events_dropped"] == 2
        assert snap["avg_latency"] == pytest.approx((0.4 * 5 + 0.2 * 3) / 8)
        assert snap["events_per_sec"] >= 0

    def test_snapshot_ignores_broken_callback(self):
        hub = _StreamMetricsHub()
        bad = MagicMock()
        bad.metrics.snapshot.side_effect = RuntimeError("broken")
        hub.track("bad", bad)
        snap = hub.snapshot()
        assert snap["active_streams"] == 1
        assert snap["events_emitted"] == 0

    def test_untrack(self):
        hub = _StreamMetricsHub()
        hub.track("t1", self._cb())
        hub.untrack("t1")
        hub.untrack("ghost")  # 不存在不报错
        assert hub.snapshot()["active_streams"] == 0

    def test_module_level_functions(self):
        cb = self._cb()
        track_stream_callback("mod-t1", cb)
        try:
            snap = stream_metrics_snapshot()
            assert snap["active_streams"] >= 1
        finally:
            untrack_stream_callback("mod-t1")


# ─── do_GET 路由 ───────────────────────────────────────────

class TestDoGetRouting:
    def _get(self, handler, path):
        handler.path = path
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock(return_value=True)
        handler._handle_stream = MagicMock()
        handler.do_GET()

    def test_static_short_circuits(self, handler):
        handler.path = "/index.html"
        handler._serve_static = MagicMock(return_value=True)
        handler._check_auth = MagicMock()
        handler.do_GET()
        handler._check_auth.assert_not_called()

    def test_health_exempt_from_auth(self, handler):
        handler.path = "/health"
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock()
        handler._handle_health = MagicMock()
        handler.do_GET()
        handler._check_auth.assert_not_called()
        handler._handle_health.assert_called_once()

    def test_unauthorized_returns_401(self, handler):
        handler.path = "/tasks"
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock(return_value=False)
        handler.do_GET()
        handler._json.assert_called_once_with({"error": "unauthorized"}, 401)

    def test_stream_route(self, handler):
        self._get(handler, "/stream?task_id=x")
        handler._handle_stream.assert_called_once()

    def test_routes_dispatch(self, handler):
        cases = [
            ("/metrics", "_handle_metrics"),
            ("/tasks", "_handle_list_tasks"),
            ("/agents", "_handle_list_agents"),
            ("/api/config", "_handle_config_get"),
            ("/api/health/deep", "_handle_health_deep"),
            ("/api/cache", "_handle_cache_stats"),
            ("/api/cost", "_handle_cost_stats"),
            ("/api/events/hooks", "_handle_list_hooks"),
            ("/dlq", "_handle_list_dlq"),
            ("/api/dashboard", "_handle_dashboard"),
            ("/api/pipeline", "_handle_pipeline"),
            ("/api/alerts", "_handle_alerts"),
            ("/api/logs", "_handle_logs"),
        ]
        for path, method in cases:
            handler._json.reset_mock()
            with patch.object(AdminHandler, method, autospec=True) as mock_m:
                self._get(handler, path)
                assert mock_m.called, f"{path} 未路由到 {method}"

    def test_task_detail_route(self, handler):
        """`/tasks/<id>` 详情仍然是 GET（动作端点的 GET 已改 405，见下一例）"""
        with patch.object(AdminHandler, "_handle_get_task", autospec=True) as mock_m:
            self._get(handler, "/tasks/t-001")
            assert mock_m.called

    #: F2 覆盖的动作端点（spec 里只有 post）
    _ACTIONS = ("cancel", "rerun", "pause", "resume", "approve", "reject")
    _ACTION_HANDLERS = ("_handle_cancel_task", "_handle_rerun_task", "_handle_pause_task",
                        "_handle_resume_task", "_handle_approve_task", "_handle_reject_task")

    def test_task_action_routes_reject_get(self, handler):
        """F2（2026-11-07）：动作端点 spec 里只有 post ⇒ GET 必须 405 + `Allow: POST`。

        本用例原先断言 GET 会路由到 `_handle_cancel_task` 等——**那正是缺陷本身**：
        GET 会静默执行状态变更（`/rerun` 真的再跑一遍 = 花钱），而 `approve`/`reject`
        更隐蔽，会落到 `/tasks/<id>` 详情分支返回 200 + 任务详情，让误用 GET 的调用方
        以为"已放行"。断言方向因此整体反过来：不改状态、不给假成功、明确说清该用什么动词。
        """
        for action in self._ACTIONS:
            handler._json.reset_mock()
            for method in self._ACTION_HANDLERS + ("_handle_get_task",):
                setattr(handler, method, MagicMock())
            self._get(handler, f"/tasks/t-001/{action}")
            handler._json.assert_called_once()
            _, kwargs = handler._json.call_args
            assert kwargs.get("status") == 405, \
                f"/{action} 未回 405（实际 {handler._json.call_args}）"
            assert (kwargs.get("headers") or {}).get("Allow") == "POST", \
                f"/{action} 的 405 缺 `Allow: POST`"
            for method in self._ACTION_HANDLERS + ("_handle_get_task",):
                assert not getattr(handler, method).called, \
                    f"/{action} 仍被 GET 执行或回落到详情（{method}）"

    def test_invalid_task_id_400(self, handler):
        self._get(handler, "/tasks/../../etc")
        handler._json.assert_called_once_with({"error": "invalid task id"}, 400)

    def test_agent_detail_route(self, handler):
        with patch.object(AdminHandler, "_handle_agent_detail", autospec=True) as m:
            self._get(handler, "/api/agents/writer")
            m.assert_called_once()

    def test_dlq_replay_route_requires_confirm(self, handler):
        handler.headers = {}  # 无 X-Confirm
        self._get(handler, "/dlq/5/replay")
        handler._json.assert_called_once_with({"error": "missing X-Confirm header"}, 428)

    def test_dlq_replay_invalid_id(self, handler):
        self._get(handler, "/dlq/abc/replay")
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    def test_versions_routes(self, handler):
        with patch.object(AdminHandler, "_handle_versions_stats", autospec=True) as m:
            self._get(handler, "/api/versions/stats")
            m.assert_called_once()
        with patch.object(AdminHandler, "_handle_versions_list", autospec=True) as m:
            self._get(handler, "/api/versions?file=a.md")
            m.assert_called_once()
        # 缺 file 参数
        handler._json.reset_mock()
        self._get(handler, "/api/versions")
        handler._json.assert_called_once_with({"error": "缺少 file 参数"}, 400)
        # diff 参数非法
        handler._json.reset_mock()
        self._get(handler, "/api/versions/diff?file=a.md&v1=x&v2=2")
        handler._json.assert_called_once_with({"error": "v1/v2 必须为整数"}, 400)
        with patch.object(AdminHandler, "_handle_versions_diff", autospec=True) as m:
            self._get(handler, "/api/versions/diff?file=a.md&v1=1&v2=2")
            m.assert_called_once()

    def test_openapi_and_root_and_404(self, handler):
        self._get(handler, "/api/openapi.json")
        assert handler._json.called
        handler._json.reset_mock()
        handler._text = MagicMock()
        self._get(handler, "/")
        handler._text.assert_called_once()
        handler._json.reset_mock()
        self._get(handler, "/no-such-endpoint")
        handler._json.assert_called_once_with({"error": "not found"}, 404)

    def test_handler_exception_returns_500(self, handler):
        handler.path = "/metrics"
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock(return_value=True)
        handler._handle_metrics = MagicMock(side_effect=RuntimeError("boom"))
        handler.do_GET()
        handler._json.assert_called_once_with({"error": "internal server error"}, 500)


# ─── do_POST / do_DELETE / do_OPTIONS 路由 ─────────────────

class TestDoPostRouting:
    def _post(self, handler, path, body=b"", headers=None):
        handler.path = path
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock(return_value=True)
        handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
        handler.rfile = io.BytesIO(body)
        handler.do_POST()

    def test_submit_task_route(self, handler):
        with patch.object(AdminHandler, "_handle_submit_task", autospec=True) as m:
            self._post(handler, "/api/tasks", b'{"query":"q"}')
            m.assert_called_once()

    def test_task_action_routes(self, handler):
        for suffix, method in [("/cancel", "_handle_cancel_task"),
                               ("/rerun", "_handle_rerun_task"),
                               ("/pause", "_handle_pause_task"),
                               ("/resume", "_handle_resume_task")]:
            with patch.object(AdminHandler, method, autospec=True) as m:
                self._post(handler, f"/tasks/t-1{suffix}")
                assert m.called

    def test_invalid_task_id_400(self, handler):
        self._post(handler, "/tasks/..%2f/cancel")
        handler._json.assert_called_once_with({"error": "invalid task id"}, 400)

    def test_rollback_bad_json(self, handler):
        self._post(handler, "/api/versions/rollback", b"{not json")
        handler._json.assert_called_once_with({"error": "请求体不是合法 JSON"}, 400)

    def test_rollback_missing_fields(self, handler):
        self._post(handler, "/api/versions/rollback", b'{"file": "a.md"}')
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    def test_rollback_requires_confirm(self, handler):
        body = json.dumps({"file": "a.md", "version": 1}).encode()
        self._post(handler, "/api/versions/rollback", body)
        handler._json.assert_called_once_with({"error": "missing X-Confirm header"}, 428)

    def test_rollback_success(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        body = json.dumps({"file": "a.md", "version": 1}).encode()
        with patch("loom_core.version_manager.get_version_manager") as mock_vm:
            mock_vm.return_value.rollback.return_value = {"status": "ok"}
            self._post(handler, "/api/versions/rollback", body,
                       headers={"X-Confirm": "yes"})
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 200

    def test_dlq_replay_post_route(self, handler):
        with patch.object(AdminHandler, "_handle_replay_dlq", autospec=True) as m:
            self._post(handler, "/dlq/3/replay", headers={"X-Confirm": "yes"})
            m.assert_called_once()

    def test_config_set_route_requires_confirm(self, handler):
        self._post(handler, "/api/config", b'{"key":"a"}')
        handler._json.assert_called_once_with({"error": "missing X-Confirm header"}, 428)

    def test_config_reload_route(self, handler):
        with patch.object(AdminHandler, "_handle_config_reload", autospec=True) as m:
            self._post(handler, "/api/config/reload")
            m.assert_called_once()

    def test_cache_clear_route(self, handler):
        with patch.object(AdminHandler, "_handle_cache_clear", autospec=True) as m:
            self._post(handler, "/api/cache/clear", headers={"X-Confirm": "yes"})
            m.assert_called_once()

    def test_set_budget_route(self, handler):
        with patch.object(AdminHandler, "_handle_set_budget", autospec=True) as m:
            self._post(handler, "/api/cost/budget", b'{"max_cost": 1.5}')
            m.assert_called_once()

    def test_register_hook_route(self, handler):
        with patch.object(AdminHandler, "_handle_register_hook", autospec=True) as m:
            self._post(handler, "/api/events/hooks", b'{"event":"e","url":"u"}')
            m.assert_called_once()

    def test_unknown_post_route_405(self, handler):
        self._post(handler, "/unknown")
        handler._json.assert_called_once_with({"error": "method not allowed"}, 405)

    def test_post_exception_500(self, handler):
        handler.path = "/api/tasks"
        handler._serve_static = MagicMock(return_value=False)
        handler._check_auth = MagicMock(return_value=True)
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        with patch.object(AdminHandler, "_handle_submit_task",
                          side_effect=RuntimeError("boom")):
            handler.do_POST()
        handler._json.assert_called_once_with({"error": "internal server error"}, 500)


class TestDoDeleteAndOptions:
    def test_delete_hook_route(self, handler):
        handler.path = "/api/events/hooks/abc123"
        handler._check_auth = MagicMock(return_value=True)
        with patch.object(AdminHandler, "_handle_unregister_hook", autospec=True) as m:
            handler.do_DELETE()
            m.assert_called_once()

    def test_delete_unauthorized(self, handler):
        handler.path = "/api/events/hooks/x"
        handler._check_auth = MagicMock(return_value=False)
        handler.do_DELETE()
        handler._json.assert_called_once_with({"error": "unauthorized"}, 401)

    def test_delete_unknown_405(self, handler):
        handler.path = "/whatever"
        handler._check_auth = MagicMock(return_value=True)
        handler.do_DELETE()
        handler._json.assert_called_once_with({"error": "method not allowed"}, 405)

    def test_delete_exception_500(self, handler):
        handler.path = "/api/events/hooks/x"
        handler._check_auth = MagicMock(side_effect=RuntimeError("boom"))
        handler.do_DELETE()
        handler._json.assert_called_once_with({"error": "internal server error"}, 500)

    def _options(self, handler, origin):
        handler.headers = {"Origin": origin} if origin else {}
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()
        handler.do_OPTIONS()

    def test_options_allowed_origin(self, handler):
        handler._allowed_origin = MagicMock(return_value="http://x.com")
        self._options(handler, "http://x.com")
        handler.send_response.assert_called_once_with(204)

    def test_options_rejected_origin(self, handler):
        handler._allowed_origin = MagicMock(return_value=None)
        self._options(handler, "http://evil.com")
        handler.send_response.assert_called_once_with(403)


# ─── 各 handler 行为 ───────────────────────────────────────

class TestBasicHandlers:
    def test_handle_health(self, handler):
        handler.orch.bus.health.return_value = {"running": True}
        handler.orch.registry.list.return_value = [{"name": "a"}]
        handler.orch.list_tasks.return_value = []
        handler._handle_health()
        data = handler._json.call_args[0][0]
        assert data["running"] is True
        assert data["registry_agents"] == 1

    def test_handle_metrics_prometheus(self, handler):
        handler.orch._metrics.to_prometheus.return_value = "# metrics\n"
        handler._prometheus = MagicMock()
        handler._handle_metrics()
        handler._prometheus.assert_called_once_with("# metrics\n")

    def test_handle_metrics_no_metrics(self, handler):
        handler.orch._metrics = None
        handler._text = MagicMock()
        handler._handle_metrics()
        handler._text.assert_called_once()

    def test_handle_list_tasks(self, handler):
        # Stage 2 起 /tasks 读的是引擎列表读口（内存缓存 ∪ RunStore 投影），
        # 行是 dict 而不是 PipelineTask——这里判 handler 把行映射成既有载荷形状。
        handler.orch.list_runs.return_value = [{
            "id": "t1", "status": "done", "pipeline": "docgen",
            "created_at": 0, "finished_at": 0, "error": None,
        }]
        handler._handle_list_tasks()
        data = handler._json.call_args[0][0]
        assert data["count"] == 1 and data["tasks"][0]["id"] == "t1"
        assert data["tasks"][0]["pipeline"] == "docgen"

    def test_handle_get_task_with_output_file(self, handler, tmp_path):
        out_file = tmp_path / "doc.md"
        out_file.write_text("# 文档内容", encoding="utf-8")
        task = MagicMock()
        task.id = "t1"
        task.status.value = "done"
        task.pipeline_name = "docgen"
        task.output_path = str(out_file)
        task.output_content = ""
        handler.orch.get_task.return_value = task
        handler._handle_get_task("t1")
        data = handler._json.call_args[0][0]
        assert data["output_path"] == str(out_file)
        assert data["output_content"] == "# 文档内容"

    def test_handle_get_task_falls_back_to_inline_content(self, handler):
        """落盘文件不在时（如容器内路径丢失）退回引擎记录的内存产出。"""
        task = MagicMock()
        task.id = "t2"
        task.status.value = "done"
        task.pipeline_name = "p"
        task.output_path = "output/gone.md"
        task.output_content = "# 内存里的最终稿"
        handler.orch.get_task.return_value = task
        handler._handle_get_task("t2")
        data = handler._json.call_args[0][0]
        assert data["output_content"] == "# 内存里的最终稿"

    def test_handle_rerun_task(self, handler):
        new_task = MagicMock()
        new_task.id = "t-new"
        new_task.pipeline_name = "docgen"
        handler.orch.rerun.return_value = new_task
        handler._handle_rerun_task("t-old")
        data = handler._json.call_args[0][0]
        assert data["new_task_id"] == "t-new"

    def test_handle_rerun_task_error(self, handler):
        handler.orch.rerun.side_effect = RuntimeError("no plan")
        handler._handle_rerun_task("t-old")
        assert handler._json.call_args[0][1] == 500

    def test_handle_list_agents(self, handler):
        handler.orch.registry.list.return_value = [{"name": "writer"}]
        handler._handle_list_agents()
        assert handler._json.call_args[0][0]["count"] == 1

    def test_handle_list_dlq(self, handler):
        handler.orch.bus.list_dlq.return_value = [{"id": 1}]
        handler._handle_list_dlq()
        assert handler._json.call_args[0][0]["count"] == 1

    def test_handle_replay_dlq(self, handler):
        handler.orch.replay_dlq.return_value = {"ok": True}
        handler._handle_replay_dlq(7)
        data = handler._json.call_args[0][0]
        assert data["replayed"] is True and data["dlq_id"] == 7


class TestDashboardAndPipeline:
    def test_handle_dashboard(self, handler):
        handler.orch.list_runs.return_value = [{
            "id": "t1", "status": "running", "pipeline": "docgen",
            "progress": 50, "steps": 2, "created_at": 0, "finished_at": None,
            "error": None,
        }]
        handler.orch.registry.list.return_value = [
            {"name": "writer", "version": "2.0", "status": "idle"}]
        handler._handle_dashboard()
        data = handler._json.call_args[0][0]
        assert data["status"] == "ok"
        assert data["tasks"][0]["progress"] == 50
        assert data["agents"][0]["name"] == "writer"

    def test_handle_pipeline(self, handler, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (tmp_path / "pipelines").mkdir()
        (tmp_path / "pipelines" / "docgen.yaml").write_text("x", encoding="utf-8")
        handler.orch.agents_dir = str(agents_dir)
        handler.orch.checkpoint_dir = str(tmp_path / "ckpt")
        handler.orch.registry.list.return_value = []
        handler.orch.list_tasks.return_value = []
        handler._handle_pipeline()
        data = handler._json.call_args[0][0]
        assert "docgen.yaml" in data["pipeline_files"]
        assert "version" in data

    def test_handle_agent_detail(self, handler):
        agent = MagicMock()
        agent.status.value = "idle"
        agent.meta.version = "2.0"
        agent.meta.description = "d"
        agent.meta.priority = 30
        agent.meta.input_topics = []
        agent.meta.output_topics = []
        agent.meta.dependencies = []
        agent.meta.cache_ttl = 0
        agent.meta.respawn = False
        agent.meta.health_check_interval = 0
        agent.meta.tags = []
        agent.stats.start_count = 1
        handler.orch.registry._agents = {"writer": agent}
        cb = MagicMock()
        cb.state.name = "CLOSED"
        cb.failure_count = 0
        handler.orch._cb_registry.get.return_value = cb
        handler._handle_agent_detail("writer")
        data = handler._json.call_args[0][0]
        assert data["name"] == "writer"
        assert data["meta"]["version"] == "2.0"
        assert data["circuit_breaker"]["state"] == "CLOSED"

    def test_handle_agent_detail_not_found(self, handler):
        handler.orch.registry._agents = {}     # 详情端点按 meta 表查
        handler._handle_agent_detail("ghost")
        assert handler._json.call_args[0][1] == 404


class TestConfigHandlers:
    def test_config_get(self, handler):
        handler.orch.config.to_dict.return_value = {"a": 1}
        handler._handle_config_get()
        handler._json.assert_called_once_with({"a": 1})

    def test_config_set_success(self, handler):
        handler.orch.config.get.return_value = "old"
        handler.orch.config.set_persisted = MagicMock(return_value=None)
        handler._handle_config_set(b'{"key": "llm.model", "value": "m2"}')
        # 跨进程生效（续44/P1-C）：set → set_persisted（内存 + 覆写文件）
        handler.orch.config.set_persisted.assert_called_once_with("llm.model", "m2")
        data = handler._json.call_args[0][0]
        assert data["applied"] is True and data["old_value"] == "old"
        assert data["propagated"] is True

    def test_config_set_bad_json(self, handler):
        handler._handle_config_set(b"{bad")
        assert handler._json.call_args[0][1] == 400

    def test_config_set_missing_key(self, handler):
        handler._handle_config_set(b'{"value": 1}')
        assert handler._json.call_args[0][1] == 400

    def test_config_reload_notifies_agents(self, handler):
        handler.orch.notify_config_update = MagicMock(return_value=1)
        handler.orch.config.publish_reload = MagicMock(return_value=None)
        handler._handle_config_reload()
        # 遍历注册表通知 Agent 的逻辑在 orchestrator.notify_config_update（续44 抽出），
        # 这里只钉住 handler 的委托关系与广播
        handler.orch.notify_config_update.assert_called_once_with(["*"])
        handler.orch.config.publish_reload.assert_called_once()
        data = handler._json.call_args[0][0]
        assert data["reloaded"] is True and data["agents_notified"] == 1
        assert data["propagated"] is True
        assert data["reloaded"] is True and data["agents_notified"] == 1

    def test_config_reload_error(self, handler):
        handler.orch.config.reload.side_effect = RuntimeError("bad config")
        handler._handle_config_reload()
        assert handler._json.call_args[0][1] == 500


class TestHealthDeepAndCaches:
    def test_health_deep(self, handler, tmp_path):
        handler.orch.bus.health.return_value = {"running": True}
        handler.orch.registry.list.return_value = [{"name": "writer"}]
        handler.orch.checkpoint_dir = tmp_path
        handler.orch._cb_registry.list.return_value = []
        router = MagicMock()
        router.list_providers.return_value = ["p1"]
        router.get_active.return_value = "p1"
        mgr = MagicMock()
        mgr.list_engines.return_value = []
        with patch("loom_core.llm_router.get_router", return_value=router), \
                patch("artesian.search_engines.SearchEngineManager.from_env",
                      return_value=mgr):
            handler._handle_health_deep()
        data = handler._json.call_args[0][0]
        assert data["components"]["message_bus"]["status"] == "healthy"
        assert data["components"]["llm_router"]["status"] == "healthy"
        assert data["components"]["checkpoint"]["status"] == "healthy"
        assert data["overall"] in ("healthy", "degraded")

    def test_health_deep_component_errors(self, handler, tmp_path):
        handler.orch.bus.health.return_value = {"running": False}
        handler.orch.registry.list.return_value = []
        handler.orch.checkpoint_dir = tmp_path / "missing"
        handler.orch._cb_registry.list.return_value = []
        with patch("loom_core.llm_router.get_router",
                   side_effect=RuntimeError("no router")), \
                patch("artesian.search_engines.SearchEngineManager.from_env",
                      side_effect=RuntimeError("no mgr")):
            handler._handle_health_deep()
        data = handler._json.call_args[0][0]
        assert data["components"]["llm_router"]["status"] == "unknown"
        assert data["overall"] == "degraded"
        assert data["unhealthy_components"]

    def test_cache_stats(self, handler):
        handler._handle_cache_stats()
        assert "caches" in handler._json.call_args[0][0]

    def test_cache_clear(self, handler):
        handler._handle_cache_clear()
        handler._json.assert_called_once_with({"cleared": True})

    def test_cost_stats_and_budget(self, handler):
        handler._handle_cost_stats()
        assert handler._json.called
        handler._json.reset_mock()
        handler._handle_set_budget(b'{"max_cost": 2.5}')
        data = handler._json.call_args[0][0]
        assert data["budget_set"] is True and data["max_cost"] == 2.5
        handler._json.reset_mock()
        handler._handle_set_budget(b'{"max_cost": "abc"}')
        assert handler._json.call_args[0][1] == 400


class TestAlertsAndLogs:
    def test_alerts_bad_limit(self, handler):
        handler.path = "/api/alerts?limit=abc"
        handler._handle_alerts()
        assert handler._json.call_args[0][1] == 400

    def test_alerts_ok(self, handler):
        handler.path = "/api/alerts?level=error&limit=10"
        with patch("loom_core.alert_manager.get_alerts", return_value=[]) as m:
            handler._handle_alerts()
        m.assert_called_once()
        assert handler._json.call_args[0][0]["alerts"] == []

    def test_logs_no_dir(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        handler.path = "/api/logs"
        handler._handle_logs()
        data = handler._json.call_args[0][0]
        assert data == {"logs": [], "count": 0}

    def test_logs_filters(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        now = time.time()
        entries = [
            {"timestamp": now, "level": "error", "agent": "writer", "msg": "e1"},
            {"timestamp": now, "level": "info", "agent": "fetcher", "msg": "i1"},
            {"timestamp": now - 99999, "level": "error", "agent": "writer", "msg": "old"},
        ]
        (log_dir / "a.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries) + "\nnot-json\n",
            encoding="utf-8")
        handler.path = "/api/logs?level=error&agent=writer&since=3600&limit=10"
        handler._handle_logs()
        data = handler._json.call_args[0][0]
        assert data["count"] == 1
        assert data["logs"][0]["msg"] == "e1"

    def test_logs_bad_params(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        handler.path = "/api/logs?since=abc"
        handler._handle_logs()
        assert handler._json.call_args[0][1] == 400

    def test_logs_stale_file_skipped_by_mtime(self, handler, tmp_path, monkeypatch):
        """性能优化回归：mtime 早于时间窗的文件整文件跳过（不读内容），
        glob 逆序（真实命名 app_YYYYMMDD[.N].jsonl 字母序=时间序）遇过期文件即停"""
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        now = time.time()
        # 逆序遍历顺序：今日 → 昨日 → 前日
        fresh = log_dir / "loom_20260829.jsonl"
        fresh.write_text(
            json.dumps({"timestamp": now, "level": "info", "msg": "fresh"}) + "\n",
            encoding="utf-8")
        stale = log_dir / "loom_20260828.jsonl"
        stale.write_text(
            json.dumps({"timestamp": now, "level": "info", "msg": "stale"}) + "\n",
            encoding="utf-8")
        older = log_dir / "loom_20260827.jsonl"
        older.write_text(
            json.dumps({"timestamp": now, "level": "info", "msg": "older"}) + "\n",
            encoding="utf-8")
        # 把昨日/前日文件 mtime 回拨出窗（模拟按天滚动的旧日志；
        # 内容时间戳故意留在窗内，验证跳过依据是 mtime 而非内容）
        past = now - 7200
        import os as _os
        _os.utime(stale, (past, past))
        _os.utime(older, (past, past))
        handler.path = "/api/logs?since=3600"
        handler._handle_logs()
        data = handler._json.call_args[0][0]
        msgs = [e["msg"] for e in data["logs"]]
        # stale/older 内容在窗内但 mtime 过期 → 不出现；older 更不会被遍历到
        assert msgs == ["fresh"]


class TestVersionsHandlers:
    def test_versions_list(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "doc.md").write_text("x", encoding="utf-8")
        with patch("loom_core.version_manager.get_version_manager") as mock_vm:
            mock_vm.return_value.history.return_value = [{"version": 1}]
            handler._handle_versions_list("doc.md")
        data = handler._json.call_args[0][0]
        assert data["count"] == 1

    def test_versions_list_path_rejected(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # 越界样本必须跨平台：Windows 路径在 Linux 上被 resolve 成 cwd 内的
        # 相对路径而通过校验；/etc/passwd 在两侧都落在 cwd 之外
        handler._handle_versions_list("/etc/passwd")
        assert handler._json.call_args[0][1] == 400

    def test_versions_diff(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("loom_core.version_manager.get_version_manager") as mock_vm:
            mock_vm.return_value.diff.return_value = "--- a\n+++ b"
            handler._handle_versions_diff("d.md", 1, 2)
        assert handler._json.call_args[0][0]["diff"].startswith("---")

    def test_versions_rollback_not_found(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("loom_core.version_manager.get_version_manager") as mock_vm:
            mock_vm.return_value.rollback.return_value = {"status": "error"}
            handler._handle_versions_rollback("d.md", 99)
        assert handler._json.call_args[0][1] == 404

    def test_versions_stats(self, handler):
        with patch("loom_core.version_manager.get_version_manager") as mock_vm:
            mock_vm.return_value.stats.return_value = {"total_files": 3}
            handler._handle_versions_stats()
        assert handler._json.call_args[0][0]["total_files"] == 3


class TestHookHandlers:
    def test_register_hook_success_and_unregister(self, handler):
        body = json.dumps({"event": "task.completed",
                           "url": "https://hooks.example.com/cb"}).encode()
        handler._handle_register_hook(body)
        data = handler._json.call_args[0][0]
        assert data["registered"] is True
        hook_id = data["hook_id"]
        handler._json.reset_mock()
        handler._handle_unregister_hook(hook_id)
        assert handler._json.call_args[0][0]["unregistered"] is True

    def test_register_hook_ssrf_rejected(self, handler):
        body = json.dumps({"event": "e", "url": "http://127.0.0.1:9/x"}).encode()
        handler._handle_register_hook(body)
        assert handler._json.call_args[0][1] == 400

    def test_register_hook_missing_fields(self, handler):
        handler._handle_register_hook(b'{"event": "e"}')
        assert handler._json.call_args[0][1] == 400

    def test_register_hook_bad_json(self, handler):
        handler._handle_register_hook(b"{bad")
        assert handler._json.call_args[0][1] == 400

    def test_list_hooks(self, handler):
        handler._handle_list_hooks()
        assert "hooks" in handler._json.call_args[0][0]


class TestSubmitTask:
    def _body(self, **kw):
        base = {"query": "Kafka 架构", "pipeline": "docgen"}
        base.update(kw)
        return json.dumps(base).encode()

    def test_submit_success_wait_true(self, handler, tmp_path):
        out_file = tmp_path / "out.md"
        out_file.write_text("# 输出", encoding="utf-8")
        task = MagicMock()
        task.id = "t-100"
        task.status.value = "done"
        task.pipeline_name = "docgen"
        task.output_path = str(out_file)
        task.output_content = "# 输出"
        task.error = None
        handler.orch.run_plan.return_value = task
        handler._find_streaming_agent = MagicMock(return_value=None)
        with patch("loom_core.scheduler.Scheduler") as mock_sched:
            mock_sched.return_value.parse.return_value = MagicMock()
            handler._handle_submit_task(self._body(wait=True))
        data = handler._json.call_args[0][0]
        assert data["task_id"] == "t-100"
        assert data["output_content"] == "# 输出"

    def test_submit_success_wait_false(self, handler):
        task = MagicMock()
        task.id = "t-101"
        task.status.value = "running"
        task.pipeline_name = "docgen"
        handler.orch.run_plan.return_value = task
        handler._find_streaming_agent = MagicMock(return_value=None)
        handler._schedule_input_cleanup = MagicMock()
        with patch("loom_core.scheduler.Scheduler") as mock_sched:
            mock_sched.return_value.parse.return_value = MagicMock()
            handler._handle_submit_task(self._body())
        data = handler._json.call_args[0][0]
        assert "message" in data
        handler._schedule_input_cleanup.assert_called_once()

    def test_submit_bad_pipeline_400(self, handler):
        with patch("loom_core.scheduler.Scheduler") as mock_sched:
            mock_sched.return_value.parse.side_effect = RuntimeError("no yaml")
            handler._handle_submit_task(self._body(pipeline="ghost"))
        assert handler._json.call_args[0][1] == 400

    def test_submit_run_plan_error_500(self, handler):
        handler.orch.run_plan.side_effect = RuntimeError("exec failed")
        handler._find_streaming_agent = MagicMock(return_value=None)
        with patch("loom_core.scheduler.Scheduler") as mock_sched:
            mock_sched.return_value.parse.return_value = MagicMock()
            handler._handle_submit_task(self._body(wait=True))
        assert handler._json.call_args[0][1] == 500

    def test_submit_output_path_rejected(self, handler, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # 同 test_versions_list_path_rejected：跨平台越界路径
        handler._handle_submit_task(self._body(output="/etc/passwd"))
        assert handler._json.call_args[0][1] == 400


class TestStreamCallbackHelpers:
    def test_attach_and_detach_with_writer(self, handler, tmp_path):
        from loom_core.base_agent import AgentMeta
        from packs.docgen.agents.writer import WriterAgent
        writer = WriterAgent(
            name="writer", meta=AgentMeta(name="writer", version="2.0"),
            config={"quiet": True}, message_bus=None, registry=None)
        handler.orch.registry.list_agent_names.return_value = ["writer"]
        handler.orch.registry.get_instance.return_value = writer

        cb = handler._attach_stream_callback("t-200")
        assert cb is not None
        assert writer._get_stream_callback("t-200") is cb
        handler._detach_stream_callback("t-200", cb)
        assert writer._get_stream_callback("t-200") is None
        assert cb.is_closed()

    def test_attach_without_writer_returns_none(self, handler):
        handler.orch.registry.list_agent_names.return_value = []
        assert handler._attach_stream_callback("t-201") is None
        handler._detach_stream_callback("t-201", None)  # None 回调直接返回

    def test_schedule_input_cleanup_reaps_after_terminal(self, handler, tmp_path):
        input_file = tmp_path / "in.md"
        input_file.write_text("x", encoding="utf-8")
        task = MagicMock()
        task.status = "done"  # 无 .value → str() 后为 "done"
        handler.orch.get_task.return_value = task
        handler._find_streaming_agent = MagicMock(return_value=None)
        handler._schedule_input_cleanup("t-300", input_file, None, max_wait_seconds=5)
        deadline = time.monotonic() + 5
        while input_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not input_file.exists()

    def test_stream_metrics_endpoint(self, handler):
        handler.path = "/stream/metrics"
        handler._handle_stream()
        data = handler._json.call_args[0][0]
        assert "active_streams" in data

    def test_stream_invalid_task_id(self, handler):
        handler.path = "/stream?task_id=../evil"
        handler._handle_stream()
        assert handler._json.call_args[0][1] == 400


class TestCheckAuth:
    def test_no_key_loopback_trusted(self, handler):
        handler.api_key = None
        handler.server_host = "127.0.0.1"
        assert handler._check_auth() is True

    def test_no_key_non_loopback_rejected(self, handler):
        handler.api_key = None
        handler.server_host = "0.0.0.0"
        assert handler._check_auth() is False

    def test_bearer_token(self, handler):
        handler.api_key = "sekret"
        handler.headers = {"Authorization": "Bearer sekret"}
        assert handler._check_auth() is True
        handler.headers = {"Authorization": "Bearer wrong"}
        assert handler._check_auth() is False

    def test_query_token(self, handler):
        handler.api_key = "sekret"
        handler.headers = {}
        handler.path = "/tasks?token=sekret"
        assert handler._check_auth() is True
        handler.path = "/tasks?token=nope"
        assert handler._check_auth() is False
        handler.path = "/tasks"
        assert handler._check_auth() is False


class TestServeStatic:
    def test_no_dashboard_dir(self, handler):
        handler.dashboard_dir = None
        assert handler._serve_static("/index.html") is False

    def test_non_static_ext(self, handler, tmp_path):
        handler.dashboard_dir = str(tmp_path)
        assert handler._serve_static("/api/tasks") is False

    def test_serves_existing_file(self, handler, tmp_path):
        (tmp_path / "index.html").write_text("<html>hi</html>", encoding="utf-8")
        handler.dashboard_dir = str(tmp_path)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()
        handler._apply_cors_headers = MagicMock()
        assert handler._serve_static("/index.html") is True
        assert b"hi" in handler.wfile.getvalue()

    def test_missing_file(self, handler, tmp_path):
        handler.dashboard_dir = str(tmp_path)
        assert handler._serve_static("/ghost.html") is False
