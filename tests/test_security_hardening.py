"""接口面安全收尾验证：危险操作 X-Confirm 二次确认 + 审计日志 /
访问日志 ?token= 打码 / initialize protocolVersion 回显 /
new_task_id 统一（长度/字符集/唯一性/三处接线）。
"""
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loom_core.admin_api import AdminAPI, AdminHandler, mask_token_query
from loom_core.ids import new_task_id


@pytest.fixture(autouse=True)
def reset_handler_state():
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None


def _start(api_key="", orch=None):
    api = AdminAPI(host="127.0.0.1", port=0, api_key=api_key)
    assert api.start(orch if orch is not None else MagicMock()) is True
    port = api._server.server_address[1]
    return f"http://127.0.0.1:{port}", api


def _request(url, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


CONFIRM = {"X-Confirm": "yes", "Content-Type": "application/json"}


@pytest.fixture
def audit_capture():
    with patch("loom_core.observability.get_logger") as mock_get:
        logger = MagicMock()
        mock_get.return_value = logger
        yield logger


# ─── 1. 危险操作 X-Confirm 二次确认 ──────────────────────────

class TestDangerousOpsConfirmGate:

    def test_cache_clear_without_confirm_428(self):
        with patch("loom_core.cache_manager.clear_all_caches") as mock_clear:
            base, api = _start()
            try:
                status, data = _request(base + "/api/cache/clear", method="POST")
                assert status == 428
                assert data == {"error": "missing X-Confirm header"}
                mock_clear.assert_not_called()
            finally:
                api.stop()

    def test_cache_clear_with_confirm_200_and_audit(self, audit_capture):
        with patch("loom_core.cache_manager.clear_all_caches") as mock_clear:
            base, api = _start()
            try:
                status, data = _request(base + "/api/cache/clear", method="POST",
                                        headers={"X-Confirm": "yes"})
                assert status == 200
                assert data == {"cleared": True}
                mock_clear.assert_called_once()
                audit_capture.info.assert_called_once()
                kwargs = audit_capture.info.call_args.kwargs
                assert kwargs["action"] == "cache_clear"
                assert "key_id" in kwargs and "params" in kwargs and "client" in kwargs
            finally:
                api.stop()

    def test_config_set_without_confirm_428(self):
        orch = MagicMock()
        base, api = _start(orch=orch)
        try:
            status, data = _request(
                base + "/api/config", method="POST",
                data=json.dumps({"key": "llm.model", "value": "x"}).encode(),
                headers={"Content-Type": "application/json"})
            assert status == 428
            assert data["error"] == "missing X-Confirm header"
            orch.config.set.assert_not_called()
        finally:
            api.stop()

    def test_config_set_with_confirm_200_and_audit_params_summary(self, audit_capture):
        orch = MagicMock()
        base, api = _start(orch=orch)
        try:
            status, data = _request(
                base + "/api/config", method="POST",
                data=json.dumps({"key": "llm.model", "value": "x"}).encode(),
                headers=CONFIRM)
            assert status == 200
            assert data["key"] == "llm.model"
            audit_capture.info.assert_called_once()
            params = json.loads(audit_capture.info.call_args.kwargs["params"])
            assert params == {"key": "llm.model"}
        finally:
            api.stop()

    def test_versions_rollback_without_confirm_428(self):
        base, api = _start()
        try:
            status, data = _request(
                base + "/api/versions/rollback", method="POST",
                data=json.dumps({"file": "output/a.md", "version": 2}).encode(),
                headers={"Content-Type": "application/json"})
            assert status == 428
            assert data == {"error": "missing X-Confirm header"}
        finally:
            api.stop()

    def test_versions_rollback_with_confirm_200_and_audit(self, audit_capture):
        vm = MagicMock()
        vm.rollback.return_value = {"status": "ok", "restored_version": 2}
        base, api = _start()
        try:
            with patch("loom_core.version_manager.get_version_manager",
                       return_value=vm):
                status, data = _request(
                    base + "/api/versions/rollback", method="POST",
                    data=json.dumps({"file": "output/a.md", "version": 2}).encode(),
                    headers=CONFIRM)
            assert status == 200
            assert data["status"] == "ok"
            audit_capture.info.assert_called_once()
            kwargs = audit_capture.info.call_args.kwargs
            assert kwargs["action"] == "versions_rollback"
            assert json.loads(kwargs["params"]) == {
                "file": "output/a.md", "version": 2}
        finally:
            api.stop()

    def test_dlq_replay_post_without_confirm_428(self):
        orch = MagicMock()
        base, api = _start(orch=orch)
        try:
            status, data = _request(base + "/dlq/3/replay", method="POST")
            assert status == 428
            assert data == {"error": "missing X-Confirm header"}
            orch.replay_dlq.assert_not_called()
        finally:
            api.stop()

    def test_dlq_replay_post_with_confirm_200_and_audit(self, audit_capture):
        orch = MagicMock()
        orch.replay_dlq.return_value = {"id": 3}
        base, api = _start(orch=orch)
        try:
            status, data = _request(base + "/dlq/3/replay", method="POST",
                                    headers={"X-Confirm": "yes"})
            assert status == 200
            assert data["replayed"] is True
            orch.replay_dlq.assert_called_once_with(3)
            kwargs = audit_capture.info.call_args.kwargs
            assert kwargs["action"] == "dlq_replay"
            assert json.loads(kwargs["params"]) == {"dlq_id": 3}
        finally:
            api.stop()

    def test_dlq_replay_get_also_gated(self):
        orch = MagicMock()
        base, api = _start(orch=orch)
        try:
            status, data = _request(base + "/dlq/5/replay")
            assert status == 428
            orch.replay_dlq.assert_not_called()
            status, _ = _request(base + "/dlq/5/replay",
                                 headers={"X-Confirm": "YES"})
            assert status == 200
        finally:
            api.stop()

    def test_wrong_confirm_value_still_428(self):
        base, api = _start()
        try:
            status, _ = _request(base + "/api/cache/clear", method="POST",
                                 headers={"X-Confirm": "no"})
            assert status == 428
        finally:
            api.stop()

    def test_bad_dlq_id_keeps_400_before_confirm_gate(self):
        """结构校验（400）先于二次确认门，既有错误语义不变"""
        base, api = _start()
        try:
            status, data = _request(base + "/dlq/bad/replay", method="POST")
            assert status == 400
            assert "invalid dlq id" in data["error"]
        finally:
            api.stop()


# ─── 2. 访问日志 token 打码 ─────────────────────────────────

class TestAccessLogTokenMasking:

    def test_mask_token_query_string(self):
        out = mask_token_query("GET /stream?task_id=t1&token=supersecret HTTP/1.1")
        assert "supersecret" not in out
        assert "token=***" in out
        assert "task_id=t1" in out

    def test_mask_leaves_non_token_params_intact(self):
        out = mask_token_query("/stream?query=a&title=b&access_token=x&tok=1")
        assert "query=a" in out and "title=b" in out
        assert "x" not in out.replace("***", "")

    def test_log_message_masks_requestline(self):
        h = AdminHandler.__new__(AdminHandler)
        h.client_address = ("127.0.0.1", 45000)
        requestline = 'GET /stream?query=q&token=abc123secret HTTP/1.1'
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            h.log_message('"%s" %s %s', requestline, "200", "15")
        out = err.getvalue()
        assert "abc123secret" not in out
        assert "token=***" in out

    def test_real_request_log_does_not_leak_query_token(self):
        """?token= 鉴权成功的请求，stderr 访问日志不落明文凭证"""
        base, api = _start(api_key="sekrit-key")
        err = io.StringIO()
        try:
            with patch.object(sys, "stderr", err):
                status, data = _request(base + "/api/versions/stats?token=sekrit-key")
            assert status == 200
            assert "status" in data
            assert "sekrit-key" not in err.getvalue()
            assert "token=***" in err.getvalue()
        finally:
            api.stop()


# ─── 3. MCP initialize protocolVersion 回显 ─────────────────

class TestMcpInitializeEcho:
    def _server(self):
        from loom_core.mcp_server import MCPServer
        return MCPServer(orch=MagicMock())

    def test_initialize_echoes_client_requested_version(self):
        resp = self._server()._handle_request({
            "jsonrpc": "2.0", "id": 30, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        assert resp["result"]["protocolVersion"] == "2025-06-18"

    def test_initialize_missing_version_falls_back_to_supported(self):
        from loom_core.mcp_server import PROTOCOL_VERSION
        resp = self._server()._handle_request({
            "jsonrpc": "2.0", "id": 31, "method": "initialize", "params": {},
        })
        assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION

    def test_generate_document_task_id_comes_from_new_task_id(self):
        import inspect

        from loom_core.mcp_server import MCPServer
        # task_id 的生成点已收口到共享提交尾 `_submit_plan_run`（generate_document
        # 与 run_workflow 共用）——守卫跟着生成点走，并钉住两条工具都必须经过它，
        # 不然 id 来源就分叉了（原守卫只盯 _tool_generate_document，重构后失焦）。
        submit_src = inspect.getsource(MCPServer._submit_plan_run)
        assert "new_task_id()" in submit_src
        assert "uuid.uuid4" not in submit_src
        for tool in (MCPServer._tool_generate_document, MCPServer._tool_run_workflow):
            assert "_submit_plan_run" in inspect.getsource(tool), (
                f"{tool.__name__} 必须经 _submit_plan_run 提交任务")


# ─── 4. new_task_id 统一生成 ────────────────────────────────

class TestNewTaskId:

    def test_length_is_16_hex_chars(self):
        tid = new_task_id()
        assert len(tid) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", tid)

    def test_uniqueness_sample_10k(self):
        ids = {new_task_id() for _ in range(10000)}
        assert len(ids) == 10000

    def test_all_three_call_sites_wired(self):
        import inspect

        import loom_core.admin_api as aa
        import run as run_mod
        assert "new_task_id()" in inspect.getsource(run_mod._run_single_task)
        assert "uuid.uuid4" not in inspect.getsource(run_mod._run_single_task)
        # #7 之后 Admin 的两个提交口共用一条尾巴，task_id 只在尾巴里铸一次。
        # 判据跟着结构改，但一条都没放宽：入口必须把活交给尾巴（否则等于没接线），
        # 且入口自己不得再铸 id——两处各铸一个会让临时输入文件与 task_id 错位。
        tail = inspect.getsource(aa.AdminHandler._launch_from_doc)
        assert "new_task_id()" in tail, "共享提交尾没铸 task_id"
        assert "uuid.uuid4" not in tail
        for entry in ("_handle_submit_task", "_handle_create_run"):
            src = inspect.getsource(getattr(aa.AdminHandler, entry))
            assert "_launch_from_doc(" in src, f"{entry} 绕过了共享提交尾"
            assert "new_task_id()" not in src, f"{entry} 自己铸了第二个 id"
        # 第三条口（MCP）也仍在同一形状上：单处铸 id、单处写临时文件
        mcp_tail = inspect.getsource(
            __import__("loom_core.mcp_server", fromlist=["MCPServer"]).MCPServer
            ._submit_plan_run)
        assert mcp_tail.count("new_task_id()") == 1, mcp_tail

    def test_dashboard_token_moved_to_session_storage(self):
        """凭据只进 sessionStorage；localStorage 只许存**非凭据**偏好（如主题）。

        原断言是 `"localStorage" not in js`——禁整个 API。视觉改版 v1 需要把主题
        偏好（浅/深）持久化到 localStorage（2026-10-31，C-12），字符串级封禁会把
        它一起打死。这里把判据收紧到**语义**而不是 API 名：所有 localStorage 调用
        的键必须是非凭据的白名单键，凭据键（TOKEN_KEY）逐个验仍在 sessionStorage。
        """
        import re

        dash = Path(__file__).parent.parent / "loom_core" / "dashboard"
        js = (dash / "app.js").read_text(encoding="utf-8")
        # 凭据路径：读写都在 sessionStorage（关标签页即失效）
        assert js.count("sessionStorage") >= 2
        assert "sessionStorage.setItem(TOKEN_KEY" in js
        assert "sessionStorage.getItem(TOKEN_KEY" in js
        # localStorage：每次调用的键必须是白名单里的非凭据键
        non_credential_keys = ("THEME_KEY",)
        calls = re.findall(r"localStorage\.(?:get|set)Item\(([^)]*)\)", js)
        assert calls, "app.js 里没有 localStorage 调用——白名单形同虚设，删掉本段"
        for arg in calls:
            assert any(k in arg for k in non_credential_keys), \
                f"localStorage 只许存非凭据偏好，发现键表达式: {arg!r}"
        # 反向：凭据键绝不能出现在 localStorage 调用里
        for bad in ("localStorage.setItem(TOKEN_KEY", "localStorage.getItem(TOKEN_KEY"):
            assert bad not in js
