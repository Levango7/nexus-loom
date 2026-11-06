"""路由层契约护栏：spec 声明了 query 参数的端点，带 query 串必须仍然可达。

起因（F1，2026-11-07 审计）：`/api/alerts` 的分发写的是精确比较
`self.path == "/api/alerts"`，而 spec 给它声明了 level/category/limit 三个
query 参数——客户端按规范带上 `?level=warning&limit=10`，请求就掉进通用 404。
同批的姊妹路由（`/api/deliveries`、`/api/locks`、`/api/logs`…）都写成了
`self.path.split("?", 1)[0] == ...`，只有 `/api/alerts` 漏了。

旧测试为什么没逮住：`tests/test_admin_api.py` 与 `test_admin_api_ext.py` 都是
**直接调** `_handle_alerts()`，绕开了分发层。handler 层是对的、路由层是错的，
缺陷就从两层测试的缝里滑过去了。

本文件的护栏刻意**从 spec 反推**而不是把路径表写死：

- 行为层：spec 里每个声明了 query 参数的 GET 端点，带 query 串都必须路由到
  某个 handler，且不得回落到通用 404；
- 结构层：`do_GET` 里不得对这些路径做精确相等比较（`self.path == "/x"`）——
  精确匹配在带 query 时必 404，是这类缺陷的同款写法。

新增带 query 的端点时不需要改这个文件：spec 一变，护栏自动跟着走。
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import AdminHandler

ROOT = Path(__file__).parent.parent
ADMIN_API = ROOT / "loom_core" / "admin_api.py"


def _spec_paths() -> dict:
    from loom_core.openapi_spec import generate_spec
    return generate_spec().get("paths", {})


_SPEC_PATHS = _spec_paths()


def _query_get_paths() -> list[str]:
    """spec 里声明了 query 参数的 GET 端点。

    从 spec 反推而不是写死路径表：新增端点后护栏自动覆盖，
    漏改本文件不会变成"漏测"。
    """
    out = []
    for path, methods in _SPEC_PATHS.items():
        op = methods.get("get")
        if not isinstance(op, dict):
            continue
        params = op.get("parameters") or []
        if any(isinstance(p, dict) and p.get("in") == "query" for p in params):
            out.append(path)
    return sorted(out)


QUERY_GET_PATHS = _query_get_paths()


def _sample_value(name: str) -> str:
    """给 query 参数造一个能被 handler 接受的样本值。"""
    if name in ("v1", "v2", "limit", "since"):
        return "1"
    if name == "file":
        return "report.md"
    return "sample"


def _url_with_query(path: str) -> str:
    op = _SPEC_PATHS[path]["get"]
    names = [p["name"] for p in (op.get("parameters") or [])
             if isinstance(p, dict) and p.get("in") == "query"]
    qs = "&".join(f"{n}={_sample_value(n)}" for n in names)
    return f"{path}?{qs}" if qs else path


def _make_handler(path: str) -> AdminHandler:
    """一台只够跑分发层的 handler。

    鉴权、共享配置同步、静态资源全部短路掉：本文件要验的是"路由到哪"，
    不是"有没有鉴权"。
    """
    h = AdminHandler.__new__(AdminHandler)
    h.orch = MagicMock()
    h.orch.resolve_pipeline_name.return_value = ("docgen", "")
    h._json = MagicMock()
    h.headers = {}
    h.path = path
    h.api_key = None
    h.server_host = "127.0.0.1"
    h.dashboard_dir = None
    h._serve_static = MagicMock(return_value=False)
    h._check_auth = MagicMock(return_value=True)
    h._sync_shared_config = MagicMock()
    h._require_admin = MagicMock(return_value=True)
    h._require_confirm = MagicMock(return_value=True)
    return h


def _stub_handlers(h: AdminHandler) -> list[str]:
    """把 AdminHandler 上所有 `_handle_*` 方法换成记录器。

    实例属性会遮蔽类属性，所以分发层里的 `self._handle_xxx()` 会打到记录器上：
    真实 handler 一个都不执行（不碰文件系统、不读状态库），但"路由到没有"
    这件事被完整记录下来。
    """
    called: list[str] = []

    def _recorder(name: str):
        def _inner(*_args, **_kwargs):
            called.append(name)
        return _inner

    for name in dir(AdminHandler):
        if name.startswith("_handle_"):
            setattr(h, name, _recorder(name))
    return called


def _dispatch(url: str) -> tuple[AdminHandler, list[str]]:
    h = _make_handler(url)
    called = _stub_handlers(h)
    h.do_GET()
    return h, called


def _exact_path_literals_in_do_get() -> dict[str, list[int]]:
    """`do_GET` 里形如 `self.path == "/x"` 的精确比较路径 → 行号列表。"""
    tree = ast.parse(ADMIN_API.read_text(encoding="utf-8"))
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "do_GET"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Compare) and len(sub.ops) == 1
                    and isinstance(sub.ops[0], ast.Eq)):
                continue
            left, right = sub.left, sub.comparators[0]
            if not (isinstance(left, ast.Attribute) and left.attr == "path"
                    and isinstance(left.value, ast.Name) and left.value.id == "self"
                    and isinstance(right, ast.Constant) and isinstance(right.value, str)):
                continue
            found.setdefault(right.value, []).append(sub.lineno)
    return found


# ─── 护栏 ──────────────────────────────────────────────────

def test_spec_has_query_param_get_paths():
    """护栏本身不许空转。

    同时把 `/api/alerts` 钉住：它一旦从 spec 里被摘掉 query 参数，
    行为层护栏就会少覆盖一条路径——那时必须先想清楚为什么，而不是让红变绿。
    """
    assert QUERY_GET_PATHS, (
        "spec 里一个带 query 参数的 GET 端点都没有了——要么 spec 变了，"
        "要么本护栏该退役了；别让它悄悄变成永远绿的空断言")
    assert "/api/alerts" in QUERY_GET_PATHS, (
        "/api/alerts 的 query 参数从 spec 里消失了——spec 与实现必须同步，"
        "不能靠削规范来让测试变绿")


@pytest.mark.parametrize("path", QUERY_GET_PATHS)
def test_query_param_get_path_is_reachable(path):
    """带 query 串的请求必须仍然路由到某个 handler，不得掉进通用 404。"""
    url = _url_with_query(path)
    h, called = _dispatch(url)
    assert called, (
        f"{url} 没有路由到任何 handler——带 query 串的请求掉进了通用 404 分支")
    for call in h._json.call_args_list:
        assert call.args[0] != {"error": "not found"}, (
            f"{url} 回落到通用 404（带 query 的请求不可达）")


def test_alerts_query_route_reaches_alerts_handler():
    """F1 回归：`/api/alerts` 带上 spec 声明的 query 参数必须仍然到 `_handle_alerts`。"""
    for url in ("/api/alerts", "/api/alerts?level=warning&category=quality&limit=10"):
        h, called = _dispatch(url)
        assert "_handle_alerts" in called, (
            f"{url} 未路由到 _handle_alerts（实际命中={called}）")
        h._json.assert_not_called()


def test_do_get_does_not_exactly_match_spec_query_paths():
    """结构层护栏：`do_GET` 不得对这些路径做精确相等比较。

    精确比较 `self.path == "/x"` 在带 query 时必 404——这类缺陷的同款写法
    必须在这一层就被挡住，而不是等某个客户端真的带上 ? 才炸。
    """
    exact = _exact_path_literals_in_do_get()
    bad = {p: exact[p] for p in QUERY_GET_PATHS if p in exact}
    assert not bad, (
        f"do_GET 里对这些带 query 参数的端点做了精确相等比较，带 query 必 404：{bad}")
