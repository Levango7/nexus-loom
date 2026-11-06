"""跨源写操作闸（F3，2026-11-07 审计）：写操作必须在**服务端**按 Origin 自己拦。

起因：CORS（`_apply_cors_headers`）只回 ACAO 头，**从不拒绝请求**——CORS 的语义是
"浏览器读不读得到响应"，与"请求会不会被执行"无关。跨站页面用 `<form>` / `<img>` /
`fetch(..., {mode:'no-cors'})` 发一个**简单请求**，请求照常到达、照常生效（默认部署是
本机信任模式，连凭据都不需要），攻击者只是读不到响应。台账 F3 列出的可跨站写面：

- `POST /api/tasks`、`POST /api/workflows/{name}/runs`（起跑 = 花费）
- `POST /tasks/{id}/{cancel,rerun,pause,resume,approve,reject}`
- `POST /api/config`、`POST /api/config/reload`、`POST /api/cost/budget`
- `POST /api/cache/clear`、`POST /api/versions/rollback`
- `POST /api/events/hooks`（**可把数据外发到攻击者 URL**）、`DELETE /api/events/hooks/{id}`

判据（板面 §二十三 F3）：`Origin` 存在且不在白名单 ⇒ **403**；无 `Origin` 头 ⇒ 放行
（SDK / curl / 脚本等非浏览器客户端）；`/api/webhooks` 入站端点**豁免**（外部服务直连）；
本闸置于鉴权**之后** ⇒ 未过鉴权仍先回 401（不改既有鉴权契约）。

这些用例尽量走**真 HTTP**：本闸的价值就在"请求真的到达了服务端"这一层，
只对 mock 断言 `_require_same_origin` 被调用，证明不了"跨站请求真被挡住了"。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import AdminAPI, AdminHandler

EVIL = "https://evil.example"


@pytest.fixture(autouse=True)
def _reset_handler_class_state():
    """`AdminAPI.start` 会写 `AdminHandler` 的**类属性**；用完还原，别污染其它文件。"""
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None
    AdminHandler.tokens = MagicMock(enabled=False)


def _start(api_key: str = ""):
    """起一台真 server，返回 (base_url, port, orch_mock, api)。"""
    orch = MagicMock()
    orch.list_tasks.return_value = []
    orch.registry.list.return_value = []
    orch.bus.health.return_value = {"status": "ok", "subscribers": 0}
    orch.get_task.return_value = None
    orch.bus._store = None
    orch._cb_registry.get.return_value = None
    orch._rate_limiters.get.return_value = None
    api = AdminAPI(host="127.0.0.1", port=0, api_key=api_key)
    assert api.start(orch) is True
    port = api._server.server_address[1]
    return f"http://127.0.0.1:{port}", port, orch, api


def _reached_write_path(orch) -> bool:
    """orch 上是否出现了**写路径**的调用（配置同步不算，它每个请求都会跑一次）。

    这是本文件的反向对照判据：同样一个 `POST /api/tasks`，加跨源 Origin 就 403 且
    这里为假（上一条用例），不加 Origin 就为真 ⇒ 挡住它的确实是 Origin 判据本身。
    """
    return any(c[0] != "refresh_config_if_changed" for c in orch.method_calls)


def _req(url: str, method: str = "POST", origin: str | None = None,
         body: bytes | None = None, token: str | None = None):
    """返回 (status, headers, parsed_body)。"""
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            return r.status, dict(r.headers), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, dict(e.headers), json.loads(raw)
        except ValueError:
            return e.code, dict(e.headers), {"raw": raw}


# ─── 核心判据：跨源写操作被拒，且**真没执行** ──────────────────

def test_cross_origin_post_rejected_and_no_side_effect():
    """`POST /api/tasks` 带跨源 Origin ⇒ 403，且 orchestrator **一次都没被碰**。

    只看状态码不够：必须证明"没有副作用"，否则 403 可能只是回包错误、活儿已经干了。
    这里允许的唯一一次 orch 调用是**配置同步**（`_sync_shared_config` 在闸之前跑，
    与本次写操作无关）；除此之外任何 orch 调用都说明提交路径已经进去了。
    """
    base, _port, orch, api = _start()
    try:
        status, _headers, body = _req(base + "/api/tasks", origin=EVIL,
                                      body=json.dumps({"query": "hi"}).encode())
        assert status == 403, f"跨源 POST /api/tasks 状态码 {status}，应为 403"
        assert body.get("error") == "cross-origin request rejected"
        write_like = [c[0] for c in orch.method_calls
                      if c[0] != "refresh_config_if_changed"]
        assert write_like == [], (
            f"被拒的跨源请求仍然走了提交路径：{write_like}")
    finally:
        api.stop()


def test_no_origin_post_still_reaches_handler():
    """无 Origin 头（SDK / curl / 脚本）必须照常放行——否则就是把工具链一起锁死。

    这一条同时是本闸的**反向对照**：同样的请求、同样的 body，加上跨源 Origin 就 403
    （上一条）、不带就到达 handler ⇒ 挡住它的确实是 Origin 判据，而不是别的什么。
    """
    base, _port, orch, api = _start()
    try:
        status, _headers, _body = _req(base + "/api/tasks",
                                       body=json.dumps({"query": "hi"}).encode())
        assert status != 403, "无 Origin 的请求被误拦了"
        assert _reached_write_path(orch), "无 Origin 的请求没有到达 handler（反向对照失效）"
    finally:
        api.stop()


def test_loopback_same_port_origin_allowed():
    """dashboard 场景：控制台由本服务同端口提供 ⇒ Origin 回环同端口必须放行。

    放行规则与 CORS 白名单同源（`_allowed_origin`），所以"浏览器读得到"与
    "服务端肯执行"两套判据不会各说各话。
    """
    base, port, orch, api = _start()
    try:
        status, _headers, _body = _req(base + "/api/tasks", origin=f"http://127.0.0.1:{port}",
                                       body=json.dumps({"query": "hi"}).encode())
        assert status != 403, f"回环同端口 Origin 被误拦（{status}）——dashboard 会挂"
        assert _reached_write_path(orch), "同源请求没有到达 handler"
    finally:
        api.stop()


def test_whitelisted_origin_allowed(monkeypatch):
    """`ADMIN_CORS_ORIGINS` 显式白名单里的 Origin 必须放行（远程控制台部署）。"""
    monkeypatch.setattr("loom_core.admin_api._EXTRA_CORS_ORIGINS",
                        ("https://dashboard.example.com",))
    base, _port, orch, api = _start()
    try:
        status, _headers, _body = _req(
            base + "/api/tasks", origin="https://dashboard.example.com",
            body=json.dumps({"query": "hi"}).encode())
        assert status != 403, f"白名单 Origin 被误拦（{status}）"
        assert _reached_write_path(orch), "白名单 Origin 的请求没有到达 handler"
    finally:
        api.stop()


def test_cross_origin_delete_rejected():
    """DELETE 同样受闸（预检只保证"浏览器会先问"，不保证非浏览器客户端守规矩）。"""
    base, _port, _orch, api = _start()
    try:
        status, _headers, body = _req(base + "/api/events/hooks/h1",
                                      method="DELETE", origin=EVIL)
        assert status == 403, f"跨源 DELETE 状态码 {status}，应为 403"
        assert body.get("error") == "cross-origin request rejected"
        status2, _h2, _b2 = _req(base + "/api/events/hooks/h1", method="DELETE")
        assert status2 != 403, "无 Origin 的 DELETE 被误拦（反向对照失效）"
    finally:
        api.stop()


def test_webhook_inbound_exempt_from_origin_gate():
    """`/api/webhooks` 入站端点豁免：外部服务直连，不该受浏览器同源规则约束。

    用**裸路径** `/api/webhooks`（不带 name）探：它必然回 400「webhook name required」
    ——说明请求到达了入站 handler；若被本闸拦住会是 403。用 400/404 而不是"配一个真
    webhook"是为了不碰全局 hook manager，判据一样硬。
    """
    base, _port, _orch, api = _start()
    try:
        status, _headers, body = _req(base + "/api/webhooks", origin=EVIL, body=b"{}")
        assert status != 403, (
            f"入站 webhook 被跨源闸误拦（{status}）——外部服务直连会被打断")
        assert "webhook" in json.dumps(body).lower(), \
            f"未到达入站 handler（响应 {body}）"
    finally:
        api.stop()


def test_auth_401_precedes_origin_gate():
    """本闸在鉴权之后：未过鉴权一律先 401，不改既有鉴权契约。

    顺序是有意的——把 403 提到 401 之前会向未认证的调用方泄露"这个 Origin 有没有
    被拒绝"，而 401 是既有测试与 SDK 都依赖的契约。
    """
    base, _port, _orch, api = _start(api_key="secret-key")
    try:
        status, _headers, _body = _req(base + "/api/tasks", origin=EVIL,
                                       body=json.dumps({"query": "hi"}).encode())
        assert status == 401, f"未带凭据的跨源请求状态码 {status}，应为 401（鉴权优先）"
    finally:
        api.stop()


def test_cross_origin_get_is_not_gated():
    """读操作不受本闸影响：跨源 GET 仍然执行，只是**读不到**响应（CORS 管的正是这个）。

    这条把边界写清楚：本闸只堵"执行"，读的可见性交给 CORS 白名单，两件事不混。
    """
    base, _port, _orch, api = _start()
    try:
        status, headers, _body = _req(base + "/health", method="GET", origin=EVIL)
        assert status == 200, f"跨源 GET /health 状态码 {status}，读操作不该被本闸拦"
        assert "Access-Control-Allow-Origin" not in headers, \
            "非白名单跨源 GET 不该回 ACAO（那才是真敞口）"
    finally:
        api.stop()


# ─── 台账点名的写面逐条扫 ──────────────────────────────────────

_DANGEROUS_WRITES = [
    "/api/tasks",
    "/api/workflows/demo/runs",
    "/tasks/t-001/cancel",
    "/tasks/t-001/rerun",
    "/tasks/t-001/pause",
    "/tasks/t-001/resume",
    "/tasks/t-001/approve",
    "/tasks/t-001/reject",
    "/api/config",
    "/api/config/reload",
    "/api/cost/budget",
    "/api/cache/clear",
    "/api/versions/rollback",
    "/api/events/hooks",
]


@pytest.mark.parametrize("path", _DANGEROUS_WRITES)
def test_dangerous_write_paths_reject_cross_origin(path):
    """台账点名的每一条写面，跨源 POST 都必须 403。

    注意其中 `/api/cache/clear` 与 `/api/versions/rollback` 另有 `X-Confirm` 二次确认门：
    本闸在它**之前**生效 ⇒ 仍然 403（不靠"还有别的门"兜底，也不泄露二次确认的存在）。
    """
    base, _port, _orch, api = _start()
    try:
        status, _headers, _body = _req(base + path, origin=EVIL, body=b"{}")
        assert status == 403, f"跨源 POST {path} 状态码 {status}，应为 403"
    finally:
        api.stop()


# ─── mock 层：闸的位置与短路行为 ──────────────────────────────

def _bare_handler(path: str, origin: str | None):
    h = AdminHandler.__new__(AdminHandler)
    h.orch = MagicMock()
    h.orch.resolve_pipeline_name.return_value = ("docgen", "")
    h._json = MagicMock()
    h.headers = {"Content-Length": "4"}
    h.rfile = MagicMock()
    h.wfile = MagicMock()
    h.path = path
    h.api_key = None
    h.server_host = "127.0.0.1"
    h.dashboard_dir = None
    if origin is not None:
        h.headers["Origin"] = origin
    h._serve_static = MagicMock(return_value=False)
    h._check_auth = MagicMock(return_value=True)
    h._sync_shared_config = MagicMock()
    return h


def test_post_short_circuits_before_body_read():
    """被拒的 POST **不读请求体**（跨源大 body 也不该被吃进内存）。

    这也顺带证明闸的位置在"读 Content-Length/读 body"之前。
    """
    h = _bare_handler("/api/tasks", EVIL)
    h.do_POST()
    h.rfile.read.assert_not_called()
    h._json.assert_called_once()
    args, kwargs = h._json.call_args
    status = args[1] if len(args) > 1 else kwargs.get("status")
    assert status == 403


def test_delete_short_circuits_on_rejection():
    """被拒的 DELETE 不得触碰任何 handler。"""
    h = _bare_handler("/api/events/hooks/h1", EVIL)
    calls: list[str] = []
    for name in dir(AdminHandler):
        if name.startswith("_handle_"):
            setattr(h, name, (lambda n: (lambda *a, **k: calls.append(n)))(name))
    h.do_DELETE()
    assert calls == [], f"被拒的跨源 DELETE 仍然调了 {calls}"
    _args, kwargs = h._json.call_args
    status = _args[1] if len(_args) > 1 else kwargs.get("status")
    assert status == 403


def test_gate_wired_into_both_write_methods_but_not_get():
    """结构层：`_require_same_origin` 必须同时接在 do_POST 与 do_DELETE 上，且不在 do_GET。

    没有这一条，将来谁把 DELETE 那一行删掉，只要没有别的用例踩到就永远不红。
    用 AST 查"这两个方法体内确实调用了它"，而不是 grep 字符串。
    """
    import ast
    import inspect

    src = inspect.getsource(AdminHandler)
    tree = ast.parse(src)
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("do_POST", "do_DELETE", "do_GET"):
            body_src = ast.unparse(node)
            found[node.name] = "_require_same_origin" in body_src
    assert found.get("do_POST") is True, "do_POST 上没有接跨源闸（F3 回归）"
    assert found.get("do_DELETE") is True, "do_DELETE 上没有接跨源闸（F3 回归）"
    assert found.get("do_GET") is False, (
        "do_GET 上出现了跨源闸：读操作不该被它拦（可见性归 CORS），"
        "真要拦读请另立一条并说明理由")
