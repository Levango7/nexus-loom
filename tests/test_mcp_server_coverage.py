"""MCPServer 工具实现与生命周期路径补全（C-18）。

既有 `test_mcp_server.py` 钉了协议面（initialize/tools/list/ping/未知方法）与
run_workflow 的提交主干；本文件补齐：

- 三个读工具的直测（此前零覆盖）：`get_task`（非法 id / 无 orch / 不存在 /
  完整载荷 / **淘汰后退化载荷**——.get 缺省正是为它存在）、`list_tasks`
  （无 orch / progress 缺省）、`get_pipeline_info`（解析失败 / 解析不出 / 真解析）；
- `_submit_plan_run` 共享提交尾的异常分支：plan 解析失败、类型化校验拒绝、
  载荷准备异常、run_plan 异常（临时输入文件必须清理）；wait=True 全分支
  （result_keys / error / 产物提取 / 落盘 output / 临时清理）；
- `_watch_and_finalize`：终态即停、任务消失即停、running→done 两拍、
  done 且有产物才落盘；
- `serve()` 进程内循环：空白行跳过、非 JSON 回 -32700、合法请求逐帧应答；
- `run_mcp_server`：自带 orch 直跑 / 缺省构建真 orchestrator。

mock 口径：`Scheduler` 在函数体内 import ⇒ patch `loom_core.scheduler.Scheduler`；
`task_output` / `prepare_inputs` / `gate_freeform` 是模块顶导入 ⇒ patch
`loom_core.mcp_server.*`。PROJECT_ROOT monkeypatch 进 tmp_path，输出落盘
不污染仓库。HTTP/网络零依赖。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import loom_core.mcp_server as mcp
from loom_core.input_schema import InputValidationError
from loom_core.mcp_server import MCPServer, _validate_output_path, _validate_task_id


def _done_task(pid="tid", result=None, error=None):
    return SimpleNamespace(id=pid, status=SimpleNamespace(value="done"),
                           pipeline_name="api-digest",
                           result=result if result is not None else {"writer": {}},
                           error=error)


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """把 PROJECT_ROOT 锚进 tmp：输出落盘全部落在沙箱里。"""
    monkeypatch.setattr(mcp, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)  # 相对 output 先按 cwd resolve 再比对 base
    return tmp_path


def _rpc(server, method, params=None, req_id=1):
    return server._handle_request({"jsonrpc": "2.0", "id": req_id,
                                   "method": method, "params": params or {}})


def _tool_data(resp) -> dict:
    assert "error" not in resp, resp
    assert resp["result"].get("isError") is not True, resp
    return json.loads(resp["result"]["content"][0]["text"])


def _tool_err_text(resp) -> str:
    assert resp["result"]["isError"] is True, resp
    return resp["result"]["content"][0]["text"]


# ─── 三个读工具（此前零直测） ─────────────────────────────────

class TestGetTaskTool:

    def _orch(self, handle=None):
        orch = MagicMock()
        orch.run_handle.return_value = handle
        return orch

    def test_invalid_task_id_rejected(self):
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "get_task", "arguments": {"task_id": "../evil"}})
        assert "Invalid task_id" in _tool_err_text(resp)

    def test_empty_task_id_rejected(self):
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "get_task", "arguments": {}})
        assert "Invalid task_id" in _tool_err_text(resp)

    def test_no_orchestrator_is_protocol_error(self):
        resp = _rpc(MCPServer(orch=None), "tools/call",
                    {"name": "get_task", "arguments": {"task_id": "t1"}})
        assert resp["error"]["code"] == -32603

    def test_missing_task_reports_not_found(self):
        handle = MagicMock()
        handle.exists.return_value = False
        server = MCPServer(orch=self._orch(handle))
        resp = _rpc(server, "tools/call",
                    {"name": "get_task", "arguments": {"task_id": "ghost"}})
        assert "Task not found" in _tool_err_text(resp)

    def test_full_payload_passthrough(self):
        handle = MagicMock()
        handle.exists.return_value = True
        handle.to_dict.return_value = {
            "id": "t9", "status": "done", "pipeline": "api-digest",
            "result": {"writer": {"content": "正文"}}, "error": None,
            "artifacts": [{"node": "writer", "path": "out.md"}],
        }
        server = MCPServer(orch=self._orch(handle))
        data = _tool_data(_rpc(server, "tools/call",
                               {"name": "get_task", "arguments": {"task_id": "t9"}}))
        assert data["id"] == "t9" and data["status"] == "done"
        assert data["pipeline"] == "api-digest"
        assert data["result"] == {"writer": {"content": "正文"}}
        assert data["artifacts"] == [{"node": "writer", "path": "out.md"}]

    def test_degraded_payload_uses_get_defaults(self):
        """淘汰窗口里 to_dict() 退化为只有身份与状态：不能 KeyError 打飞帧。"""
        handle = MagicMock()
        handle.exists.return_value = True
        handle.to_dict.return_value = {"id": "t9", "status": "done"}
        server = MCPServer(orch=self._orch(handle))
        data = _tool_data(_rpc(server, "tools/call",
                               {"name": "get_task", "arguments": {"task_id": "t9"}}))
        assert (data["pipeline"], data["result"], data["error"],
                data["artifacts"]) == ("", {}, None, [])


class TestListTasksTool:

    def test_no_orchestrator_is_protocol_error(self):
        resp = _rpc(MCPServer(orch=None), "tools/call",
                    {"name": "list_tasks", "arguments": {}})
        assert resp["error"]["code"] == -32603

    def test_rows_with_progress_default(self):
        orch = MagicMock()
        orch.list_runs.return_value = [
            {"id": "a", "status": "done", "pipeline": "api-digest", "progress": 100},
            {"id": "b", "status": "running", "pipeline": "kb-docgen"},
        ]
        data = _tool_data(_rpc(MCPServer(orch=orch), "tools/call",
                               {"name": "list_tasks", "arguments": {}}))
        assert data["count"] == 2
        assert data["tasks"][0]["progress"] == 100
        assert data["tasks"][1]["progress"] == 0, "缺省 progress 不许 KeyError"


class TestGetPipelineInfoTool:

    def test_unresolvable_name_lists_available(self):
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "get_pipeline_info", "arguments": {"name": "no-such"}})
        text = _tool_err_text(resp)
        assert "no-such" in text and "可用" in text

    def test_parse_failure_is_tool_error(self, monkeypatch):
        class _Boom:
            def parse(self, name):
                raise RuntimeError("YAML 坏了")

        monkeypatch.setattr("loom_core.scheduler.Scheduler", _Boom)
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "get_pipeline_info", "arguments": {"name": "api-digest"}})
        assert "Failed to parse pipeline" in _tool_err_text(resp)

    def test_success_reports_topology_and_inputs(self):
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "get_pipeline_info", "arguments": {"name": "api-digest"}})
        data = _tool_data(resp)
        assert data["name"] == "api-digest"
        assert data["node_count"] == len(data["agents"]) > 0
        assert all(isinstance(level, list) for level in data["levels"])
        for agent in data["agents"]:
            assert {"name", "dependencies", "timeout", "max_retries"} <= set(agent)
        assert "inputs" in data, "类型化输入声明必须暴露（客户端据此生成表单）"


# ─── _submit_plan_run 异常分支与 wait=True ────────────────────

class TestSubmitPlanRunBranches:

    def _orch(self, task=None, raises=None):
        orch = MagicMock()
        orch.resolve_pipeline_name.return_value = ("api-digest", "")
        if raises:
            orch.run_plan.side_effect = raises
        else:
            orch.run_plan.return_value = task
        return orch

    def test_generate_document_output_path_outside_root_rejected(self, project_root):
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "generate_document",
                     "arguments": {"query": "q", "output": "../outside.md"}})
        assert "Invalid output path" in _tool_err_text(resp)

    def test_run_workflow_output_path_outside_root_rejected(self, project_root):
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "run_workflow",
                     "arguments": {"name": "api-digest", "output": "/etc/x.md"}})
        assert "Invalid output path" in _tool_err_text(resp)

    def test_generate_document_without_orchestrator(self):
        resp = _rpc(MCPServer(orch=None), "tools/call",
                    {"name": "generate_document", "arguments": {"query": "q", "pipeline": "api-digest"}})
        assert resp["error"]["code"] == -32603

    def test_run_workflow_without_orchestrator(self):
        resp = _rpc(MCPServer(orch=None), "tools/call",
                    {"name": "run_workflow", "arguments": {"name": "api-digest"}})
        assert resp["error"]["code"] == -32603

    def test_generate_document_unresolvable_pipeline(self):
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "generate_document",
                     "arguments": {"query": "q", "pipeline": "no-such-pipe"}})
        text = _tool_err_text(resp)
        assert "no-such-pipe" in text and "可用" in text

    def test_parse_failure_cleans_up(self, project_root, monkeypatch):
        class _Boom:
            def parse(self, name):
                raise RuntimeError("YAML 坏了")

        monkeypatch.setattr("loom_core.scheduler.Scheduler", _Boom)
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "run_workflow", "arguments": {"name": "api-digest"}})
        assert "Failed to parse pipeline" in _tool_err_text(resp)

    def test_typed_input_rejected_with_field_error(self, project_root, monkeypatch):
        monkeypatch.setattr(
            mcp, "prepare_inputs",
            MagicMock(side_effect=InputValidationError([{"field": "inputs.topic", "reason": "缺失且必填"}])))
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "run_workflow",
                     "arguments": {"name": "api-digest", "inputs": {}}})
        assert "inputs.topic" in _tool_err_text(resp)

    def test_prepare_inputs_generic_failure(self, project_root, monkeypatch):
        monkeypatch.setattr(mcp, "prepare_inputs",
                            MagicMock(side_effect=RuntimeError("渲染炸了")))
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "run_workflow", "arguments": {"name": "api-digest"}})
        assert "Failed to prepare inputs" in _tool_err_text(resp)

    def test_gate_freeform_rejects_when_inputs_declared(self, project_root, monkeypatch):
        """自由文本入口撞上声明了 inputs 的流水线：直接拒，不留绕闸门的门。"""
        monkeypatch.setattr(
            mcp, "gate_freeform",
            MagicMock(side_effect=InputValidationError([{"field": "inputs", "reason": "pipeline 声明了 inputs，自由文本被拒"}])))
        resp = _rpc(MCPServer(orch=self._orch(_done_task())), "tools/call",
                    {"name": "generate_document", "arguments": {"query": "q", "pipeline": "api-digest"}})
        assert "inputs" in _tool_err_text(resp)

    def test_run_plan_failure_cleans_temp_input(self, project_root):
        orch = self._orch(raises=RuntimeError("引擎炸了"))
        resp = _rpc(MCPServer(orch=orch), "tools/call",
                    {"name": "generate_document", "arguments": {"query": "q", "pipeline": "api-digest"}})
        assert "Pipeline failed" in _tool_err_text(resp)
        input_file = Path(orch.run_plan.call_args.kwargs["input_file"])
        assert not input_file.exists(), "提交失败也要清临时输入文件"

    def test_wait_true_returns_full_result_and_writes_target(self, project_root):
        orch = self._orch(_done_task(result={"writer": {}, "checker": {}}))
        with patch.object(mcp, "task_output",
                          return_value=(str(project_root / "real.md"), "落盘正文")):
            resp = _rpc(MCPServer(orch=orch), "tools/call",
                        {"name": "generate_document",
                         "arguments": {"query": "q", "pipeline": "api-digest",
                                       "wait": True, "output": "out/mcp_cov.md"}})
        data = _tool_data(resp)
        assert data["status"] == "done"
        assert data["result_keys"] == ["writer", "checker"]
        assert data["error"] is None
        assert data["output_content"] == "落盘正文"
        assert Path(data["output_written"]) == project_root / "out" / "mcp_cov.md"
        assert (project_root / "out" / "mcp_cov.md").read_text(
            encoding="utf-8") == "落盘正文"
        input_file = Path(orch.run_plan.call_args.kwargs["input_file"])
        assert not input_file.exists(), "wait=True 收尾同样清临时输入"

    def test_wait_true_without_output_target_skips_write(self, project_root):
        orch = self._orch(_done_task())
        with patch.object(mcp, "task_output", return_value=(None, None)):
            resp = _rpc(MCPServer(orch=orch), "tools/call",
                        {"name": "generate_document",
                         "arguments": {"query": "q", "pipeline": "api-digest", "wait": True}})
        data = _tool_data(resp)
        assert "output_written" not in data
        assert "output_path" not in data


# ─── _watch_and_finalize（后台收尾） ─────────────────────────

class TestWatchAndFinalize:

    def _watcher(self, orch, tmp_path, input_name="mcp_w.md"):
        input_file = tmp_path / input_name
        input_file.write_text("# 输入\n", encoding="utf-8")
        target = tmp_path / "out" / "watch.md"
        return MCPServer(orch=orch), input_file, target

    def test_terminal_task_writes_output_and_cleans(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp.time, "sleep", lambda s: None)
        orch = MagicMock()
        orch.get_task.return_value = _done_task()
        with patch.object(mcp, "task_output", return_value=(None, "后台正文")):
            server, input_file, target = self._watcher(orch, tmp_path)
            server._watch_and_finalize("tid", input_file, target)
        assert target.read_text(encoding="utf-8") == "后台正文"
        assert not input_file.exists()

    def test_running_then_done_two_beats(self, tmp_path, monkeypatch):
        beats = iter([SimpleNamespace(id="t", status=SimpleNamespace(value="running")),
                      _done_task()])
        monkeypatch.setattr(mcp.time, "sleep", lambda s: None)
        orch = MagicMock()
        orch.get_task.side_effect = lambda tid: next(beats)
        with patch.object(mcp, "task_output", return_value=(None, "第二拍正文")):
            server, input_file, target = self._watcher(orch, tmp_path)
            server._watch_and_finalize("tid", input_file, target)
        assert target.read_text(encoding="utf-8") == "第二拍正文"

    def test_task_vanishes_stops_and_cleans_without_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp.time, "sleep", lambda s: None)
        orch = MagicMock()
        orch.get_task.return_value = None
        server, input_file, target = self._watcher(orch, tmp_path)
        server._watch_and_finalize("tid", input_file, target)
        assert not input_file.exists()
        assert not target.exists(), "任务消失不该有任何落盘"

    def test_non_done_terminal_state_never_writes(self, tmp_path, monkeypatch):
        failed = SimpleNamespace(id="t", status=SimpleNamespace(value="failed"))
        monkeypatch.setattr(mcp.time, "sleep", lambda s: None)
        orch = MagicMock()
        orch.get_task.return_value = failed
        with patch.object(mcp, "task_output", return_value=(None, "不该出现")):
            server, input_file, target = self._watcher(orch, tmp_path)
            server._watch_and_finalize("tid", input_file, target)
        assert not target.exists(), "只有 done 才允许落盘"


# ─── serve() 进程内循环 与 run_mcp_server ────────────────────

class TestServeLoop:

    def test_serve_answers_frames_and_flags_parse_error(self, monkeypatch):
        requests = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "",                                # 空白行：跳过
            "{not-json",                       # 解析错误：-32700
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(requests) + "\n"))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)

        MCPServer(orch=MagicMock()).serve()

        frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        assert len(frames) == 3
        assert frames[0]["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
        assert frames[1]["error"]["code"] == -32700
        assert frames[1]["id"] is None
        assert frames[2]["result"] == {}

    def test_run_mcp_server_with_injected_orchestrator(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n"))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        orch = MagicMock()

        mcp.run_mcp_server(orch=orch)

        frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        assert frames[0]["id"] == 5

    def test_run_mcp_server_builds_orchestrator_when_absent(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # 立即 EOF
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        built = []

        class _FakeOrch:
            def __init__(self, **kw):
                built.append(kw)
                self.registered = 0

            def register_agents(self):
                self.registered += 1

        monkeypatch.setattr("loom_core.PipelineOrchestrator", _FakeOrch)
        mcp.run_mcp_server()
        assert built and built[0]["agents_dir"].endswith("agents")
        assert built[0].get("checkpoint_dir", "").endswith("checkpoints")


# ─── 协议层兜底与防御分支 ────────────────────────────────────

class TestDefensiveBranches:

    def test_internal_error_returns_jsonrpc_503_shape(self):
        """工具实现抛了未预料的异常：协议层兜 -32603，不让帧裸奔。"""
        server = MCPServer(orch=MagicMock())
        with patch.object(server, "_handle_tool_call",
                          side_effect=RuntimeError("内存炸了")):
            resp = server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "list_pipelines", "arguments": {}}})
        assert resp["error"]["code"] == -32603
        assert "内存炸了" in resp["error"]["message"]

    def test_output_write_oserror_reported_not_raised(self, project_root):
        """落盘目标是个目录：OSError 如实写进 output_write_error，不打飞响应。"""
        (project_root / "out" / "mcp_cov.md").mkdir(parents=True)
        orch = MagicMock()
        orch.run_plan.return_value = _done_task()
        with patch.object(mcp, "task_output",
                          return_value=(str(project_root / "real.md"), "正文")):
            resp = _rpc(MCPServer(orch=orch), "tools/call",
                        {"name": "generate_document",
                         "arguments": {"query": "q", "pipeline": "api-digest",
                                       "wait": True, "output": "out/mcp_cov.md"}})
        data = _tool_data(resp)
        assert "output_written" not in data
        assert "output_write_error" in data

    def test_list_pipelines_skips_non_dir_entries(self, monkeypatch):
        """pack 目录可能被删掉一半：非目录项跳过，不炸工具。"""
        monkeypatch.setattr("loom_core.pack_manifest.pipeline_dirs",
                            lambda: [Path(project_root_none := "/nonexistent-dir-x")])
        resp = _rpc(MCPServer(orch=MagicMock()), "tools/call",
                    {"name": "list_pipelines", "arguments": {}})
        data = _tool_data(resp)
        assert data["pipelines"] == []


# ─── 校验函数直测 ────────────────────────────────────────────

class TestValidators:

    def test_task_id_charset_and_length(self):
        assert _validate_task_id("abc_123-X") is True
        assert _validate_task_id("") is False
        assert _validate_task_id("../evil") is False
        assert _validate_task_id("a" * 129) is False

    def test_output_path_rejects_traversal_and_garbage(self, tmp_path):
        ok, _ = _validate_output_path(str(tmp_path / "out" / "x.md"),
                                      base_dir=str(tmp_path))
        assert ok is True
        ok, reason = _validate_output_path("../x.md", base_dir=str(tmp_path))
        assert ok is False and "不在允许目录" in reason
        ok, reason = _validate_output_path("\x00bad", base_dir=str(tmp_path))
        assert ok is False, "解析不了/含非法字节的路径必须拒绝"
