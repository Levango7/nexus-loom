"""方法契约护栏：spec 里**只声明 post** 的路径，GET 必须被拒。

起因（F2，2026-11-07 审计）：`do_GET` 的 `/tasks/<id>/<动作>` 分支把
cancel / rerun / pause / resume **用 GET 执行**了，而 `openapi_spec.py` 只给它们
声明了 `post` —— 未声明的第二扇门。其中：

- `/rerun` 会真的再跑一遍流水线（**真花钱**）；
- `/approve`、`/reject` 更隐蔽：GET 命中的不是审批 handler，而是落到 `/tasks/<id>`
  的**详情**分支，返回 **200 + 任务详情** —— 误用 GET 的调用方会当成"已放行"，
  实际什么都没发生（**静默假成功**，比报错更难查）。

分类与处置见协调板 §二十三台账 F2（板面裁决：GET 改 405 + `Allow: POST`，并补
spec method 覆盖门防止再漂移）。

护栏从 spec 反推，不写死路径表：

- 行为层（断言 A）：spec 里每个"声明了 post、没声明 get"的路径，GET 都不得被任何
  handler 接走、也不得回 200 —— 只能是 404/405 之一；
- 可诊断层（断言 B，仅 `/tasks` 家族）：它们必须回 **405 + `Allow: POST`**。
  因为 `/tasks/<id>` 本身有 GET 路由，回 404 等于说"路径不存在"，是假话；
- 结构层：这些路径不许出现"GET 被 handler 接走"的路由形态（同款写法拦截）。

`/dlq/{dlq_id}/replay` **不在**覆盖集内：它的 GET 形式是文档化的设计意图
（模块 docstring 与 `docs/api.md:28/116` 都对用户写明"GET 形式同样要求
`X-Confirm: yes`"），已按实情把 `get` 补进 spec —— 于是它自然脱离本门覆盖，
而不是靠一份豁免名单藏起来。
"""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import AdminAPI, AdminHandler
from loom_core.openapi_spec import generate_spec

_PATHS = generate_spec().get("paths", {})


def _post_only_paths() -> list[str]:
    """spec 里声明了 `post`、没有 `get` 的路径（按名排序，稳定）。"""
    return sorted(p for p, ms in _PATHS.items()
                  if "post" in ms and "get" not in ms)


POST_ONLY_PATHS = _post_only_paths()

#: F2 台账登记的动作端点：不只要求在覆盖集里，还要求"405 + Allow"这种拒绝形态。
#: 动作段的来源是 spec（`/tasks/{task_id}/<动作>` 的形状），不是这里再抄一份名单。
TASK_ACTION_PATHS = sorted(p for p in POST_ONLY_PATHS
                           if p.startswith("/tasks/{task_id}/"))


@pytest.fixture(autouse=True)
def _reset_handler_class_state():
    """`AdminAPI.start` 会写 `AdminHandler` 的**类属性**；用完还原，别污染其它文件。"""
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None
    AdminHandler.tokens = MagicMock(enabled=False)


def _concrete(path: str) -> str:
    return (path.replace("{task_id}", "t-001")
                .replace("{dlq_id}", "5")
                .replace("{name}", "demo"))


def _handler(method_path: str) -> tuple[AdminHandler, list[str]]:
    """一台只够跑分发层的 handler：鉴权/静态/确认全部短路。

    所有 `_handle_*` 被实例属性遮蔽为记录器 ⇒ 真实 handler 一个都不执行
    （不碰文件系统、不读状态库、不起新运行），但"路由到没有"被完整记录。
    """
    h = AdminHandler.__new__(AdminHandler)
    h.orch = MagicMock()
    h.orch.resolve_pipeline_name.return_value = ("docgen", "")
    h._json = MagicMock()
    h._text = MagicMock()
    h.headers = {"Content-Length": "0"}
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.path = method_path
    h.api_key = None
    h.server_host = "127.0.0.1"
    h.dashboard_dir = None
    h._serve_static = MagicMock(return_value=False)
    h._check_auth = MagicMock(return_value=True)
    h._sync_shared_config = MagicMock()
    h._require_admin = MagicMock(return_value=True)
    h._require_confirm = MagicMock(return_value=True)
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    called: list[str] = []
    for name in dir(AdminHandler):
        if name.startswith("_handle_"):
            setattr(h, name, (lambda n: (lambda *a, **k: called.append(n)))(name))
    return h, called


def _dispatch(verb: str, path: str) -> tuple[list[str], int | None, dict | None]:
    """返回 (命中的 handler 名, 状态码, `_json` 的 headers 参数)。"""
    h, called = _handler(path)
    getattr(h, {"get": "do_GET", "post": "do_POST"}[verb])()
    status = None
    headers = None
    if h._json.called:
        _args, kwargs = h._json.call_args
        status = kwargs.get("status")
        headers = kwargs.get("headers")
        if status is None and len(_args) > 1:
            status = _args[1]
    elif h.send_response.called:
        status = h.send_response.call_args[0][0]
    return called, status, headers


# ─── 非空转：护栏本身要有射程 ──────────────────────────────────

def test_gate_scope_is_not_vacuous():
    """覆盖集非空，且 F2 的动作端点确实在里面。

    没有这一条，spec 被削平（或路径改名）之后本门会**静默变绿**——
    那正是这类护栏最常见的失效方式。
    """
    assert POST_ONLY_PATHS, (
        "spec 里已经没有「只声明 post」的路径了——要么 spec 大改，要么本门该退役，"
        "别让它变成永远绿的空断言")
    assert "/tasks/{task_id}/cancel" in TASK_ACTION_PATHS
    for action in ("cancel", "rerun", "pause", "resume", "approve", "reject"):
        assert f"/tasks/{{task_id}}/{action}" in TASK_ACTION_PATHS, (
            f"spec 里 `/tasks/{{task_id}}/{action}` 不再是「只有 post」——"
            "spec 与实现必须同步，不能靠改 spec 让测试变绿")


def test_dlq_replay_get_is_declared_intentionally():
    """`/dlq/{dlq_id}/replay` 的 GET 是**文档化的**，所以它应同时声明 get 与 post。

    这条把一个决定钉住：它**不是**豁免名单里的一条，而是"实现、spec、docs 三处
    同口径"的结果。谁要删掉 GET 能力，就得同时改 docs 两处与模块 docstring，
    而不是悄悄从 spec 里删一行让门变绿。
    """
    entry = _PATHS.get("/dlq/{dlq_id}/replay", {})
    assert "get" in entry and "post" in entry, (
        "`/dlq/{dlq_id}/replay` 的动词声明变了：GET 形式是 docs/api.md:28/116 与模块 "
        "docstring 都写明的设计意图；要改就三处一起改，别只改 spec")


# ─── 断言 A：spec 只声明 post 的路径，GET 必须被拒 ─────────────

@pytest.mark.parametrize("path", POST_ONLY_PATHS)
def test_post_only_path_does_not_serve_get(path):
    """GET 既不得被 handler 接走，也不得回 200（只能是 404/405）。"""
    called, status, _headers = _dispatch("get", _concrete(path))
    assert not called, (
        f"GET {path} 被 handler 接走了（{called}）——spec 只声明 post，"
        "这是未声明的第二扇门")
    assert status != 200, f"GET {path} 回了 200（静默假成功），应为 404/405"


# ─── 断言 B：`/tasks` 家族要给出可诊断的 405 + Allow ────────────

@pytest.mark.parametrize("path", TASK_ACTION_PATHS)
def test_task_action_get_is_405_with_allow_post(path):
    """`/tasks/<id>/<动作>` 的 GET 必须是 405 + `Allow: POST`。

    这里用 405 而不是通用 404：`/tasks/<id>` 本身有 GET 路由，说"路径不存在"是假话；
    405 才能让调用方（含 SDK/脚本）直接把动词改成 POST。
    """
    called, status, headers = _dispatch("get", _concrete(path))
    assert not called, f"GET {path} 仍被 handler 接走（{called}）"
    assert status == 405, f"GET {path} 状态码 {status}，应为 405"
    assert (headers or {}).get("Allow") == "POST", (
        f"GET {path} 的 405 没带 `Allow: POST`（headers={headers}）")


def test_task_action_get_with_query_does_not_slip_through():
    """带 query 串的动作 GET 也必须 405。

    原先判定写的是 `self.path.endswith("/cancel")` —— 带 query 时失配，
    于是 `/tasks/t-001/cancel?token=K` 会掉到**详情**分支返回 200（比不带 query
    时更糟：不带 query 会执行、带 query 会假成功）。剥 query 后两者同归 405。
    """
    for action in ("cancel", "rerun", "pause", "resume", "approve", "reject"):
        called, status, headers = _dispatch("get", f"/tasks/t-001/{action}?token=K")
        assert not called, f"GET /tasks/t-001/{action}?token=K 被 handler 接走（{called}）"
        assert status == 405, f"带 query 的 /{action} 状态码 {status}，应为 405"
        assert (headers or {}).get("Allow") == "POST"


def test_unknown_task_action_is_404_not_detail():
    """未知动作段不得再回落到 `/tasks/<id>` 详情（那是假成功的来源）。"""
    called, status, _headers = _dispatch("get", "/tasks/t-001/nope")
    assert not called, f"未知动作段被 handler 接走（{called}）"
    assert status == 404, f"未知动作段状态码 {status}，应为 404"


# ─── 反向：GET 的家底不能被我改坏 ──────────────────────────────

def test_task_detail_still_served_over_get():
    called, _status, _headers = _dispatch("get", "/tasks/t-001")
    assert "_handle_get_task" in called


def test_node_inspector_still_served_over_get():
    called, _status, _headers = _dispatch("get", "/tasks/t-001/nodes/writer")
    assert "_handle_node_inspector" in called


# ─── POST 侧：query token 是文档化的鉴权形式，不能被动词判定挡掉 ──

def test_post_action_with_query_token_still_dispatches():
    """`POST /tasks/<id>/cancel?token=K` 必须仍然派发到动作 handler。

    `?token=` 是文档化的鉴权形式（README/docs/api.md），而原先 `endswith` 判定
    在带 query 时失配 ⇒ 请求会掉到 do_POST 尾部的通用 405。这条把"鉴权形式"与
    "动词判定"解耦：剥 query 只用于切段，不做丢弃。
    """
    for action, handler in (("cancel", "_handle_cancel_task"),
                            ("rerun", "_handle_rerun_task"),
                            ("pause", "_handle_pause_task"),
                            ("resume", "_handle_resume_task"),
                            ("approve", "_handle_approve_task"),
                            ("reject", "_handle_reject_task")):
        called, status, _headers = _dispatch("post", f"/tasks/t-001/{action}?token=K")
        assert handler in called, (
            f"POST /tasks/t-001/{action}?token=K 未派发（命中={called}, status={status}）")


# ─── 真 HTTP：`Allow` 头确实出得来（校验 _json 的头透传） ────────

def _real_server():
    orch = MagicMock()
    orch.list_tasks.return_value = []
    orch.registry.list.return_value = []
    orch.bus.health.return_value = {"status": "ok", "subscribers": 0}
    orch.get_task.return_value = None
    orch.bus._store = None
    orch._cb_registry.get.return_value = None
    orch._rate_limiters.get.return_value = None
    api = AdminAPI(host="127.0.0.1", port=0, api_key="")
    assert api.start(orch) is True
    return api, api._server.server_address[1]


@pytest.mark.parametrize("action", ["cancel", "rerun", "pause", "resume", "approve", "reject"])
def test_real_http_get_action_returns_allow_header(action):
    """走真 HTTP：状态码 405 且响应头带 `Allow: POST`（不是只 mock 层对）。"""
    import urllib.error
    import urllib.request

    api, port = _real_server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/t-001/{action}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, headers, body = r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            status, headers, body = e.code, e.headers, e.read().decode()
        assert status == 405, f"GET /{action} 真 HTTP 状态码 {status}，应为 405"
        assert headers.get("Allow") == "POST", \
            f"GET /{action} 真 HTTP 响应头缺 `Allow: POST`（Allow={headers.get('Allow')!r}）"
        assert "post" in json.loads(body).get("error", "").lower(), \
            f"405 的错误文案应指路 POST，实际 {body!r}"
    finally:
        api.stop()
