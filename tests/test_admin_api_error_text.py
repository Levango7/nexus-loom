"""文案契约：同一个条件只许有一种说法，且内部文字必须与对外文档同口径。

两条都来自 2026-11-07 生产力审计（板面 §二十三 台账）：

- **F4**（P3，真缺陷）：同一个「请求体不是合法 JSON」条件有两种文案 ——
  `invalid JSON body`（4 处：/api/tasks、/api/workflows/{name}/runs、/api/config、
  /api/events/hooks）与 `请求体不是合法 JSON`（3 处：/api/versions/rollback、approve、
  reject）。前端两串都不匹配（只能原样透传 `error`），同一个用户动作在不同端点看到不同
  话术。收敛到 `CANONICAL_INVALID_JSON` 一个常量。
- **F5**（P3，真缺陷·文档层）：`output` 字段的内部 docstring 写「输出文件路径」，而
  `docs/api.md` 与 `_launch_from_doc` docstring 对用户写明「只做**白名单校验，不参与
  落盘**」——**行为本身是设计意图**（对外文档一致），说谎的是内部文档。

两条合在一起的理由：都是「第二种说法」问题，判据也就同一种形状 —— 找出所有措辞，
钉住唯一的那一种。
"""
from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import CANONICAL_INVALID_JSON, AdminAPI, AdminHandler

ROOT = Path(__file__).parent.parent
ADMIN_API = ROOT / "loom_core" / "admin_api.py"

#: 除规范文案之外，**允许存在的**含 "JSON" 的 400 文案 —— 它们说的是别的条件。
#: 新增第三个条件时必须显式登记在这里，并在表里写清它和既有条件的区别：
#: 这张表的意义是"同一条件不许有两种说法"，不是"不许有新文案"。
REGISTERED_JSON_400 = {
    "body 必须是 JSON 对象",                    # 解析成功但不是对象
    '需要 JSON 体 {"file": <path>, "version": <int>}',  # 体合规但缺字段
}


@pytest.fixture(autouse=True)
def _reset_handler_class_state():
    """`AdminAPI.start` 会写 `AdminHandler` 的**类属性**；用完还原，别污染其它文件。"""
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None
    AdminHandler.tokens = MagicMock(enabled=False)


def _start():
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
    return f"http://127.0.0.1:{api._server.server_address[1]}", api


def _post(base: str, path: str, body: bytes, confirm: bool = False):
    headers = {"X-Confirm": "yes"} if confirm else {}
    req = urllib.request.Request(base + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


# ─── F4：非法 JSON 的 400 文案必须就是规范那一条 ────────────────

BROKEN_JSON = b"{not json"

#: 四年前写下 `invalid JSON body` 的端点 + 一个原本就规范的端点做对照。
#: `/api/config` 的解析在 X-Confirm 之后，所以要带上确认头才能走到解析那一步。
_INVALID_JSON_CASES = [
    ("/api/tasks", False),
    ("/api/workflows/demo/runs", False),
    ("/api/events/hooks", False),
    ("/api/versions/rollback", False),
    ("/api/config", True),
]


@pytest.mark.parametrize("path,confirm", _INVALID_JSON_CASES)
def test_invalid_json_returns_canonical_message(path, confirm):
    """走真 HTTP：非法 JSON ⇒ 400 + 规范文案（不是 `invalid JSON body`）。"""
    base, api = _start()
    try:
        status, body = _post(base, path, BROKEN_JSON, confirm=confirm)
        assert status == 400, f"POST {path} 非法 JSON 状态码 {status}，应为 400"
        assert body.get("error") == CANONICAL_INVALID_JSON, (
            f"POST {path} 的 400 文案是 {body.get('error')!r}，"
            f"应为 {CANONICAL_INVALID_JSON!r}")
    finally:
        api.stop()


def test_canonical_message_matches_spec_declaration():
    """规范文案必须等于 spec 给 approve/reject 写下的 400 描述（契约向 spec 对齐）。

    这是"为什么会选这一条"，而不是"哪边字数多"——spec 是权威 HTTP 契约。
    """
    from loom_core.openapi_spec import generate_spec

    spec = generate_spec()
    for path in ("/tasks/{task_id}/approve", "/tasks/{task_id}/reject"):
        desc = spec["paths"][path]["post"]["responses"]["400"]["description"]
        assert desc == CANONICAL_INVALID_JSON, (
            f"spec 里 {path} 的 400 描述是 {desc!r}，与实现用的规范文案 "
            f"{CANONICAL_INVALID_JSON!r} 不一致")


def _json_related_400_literals() -> dict[str, list[int]]:
    """源码里所有**字面量**形式的、含 JSON 的 400 error 文案 → 行号列表。

    只扫字面量：规范文案现在走 `CANONICAL_INVALID_JSON` 常量（`ast.Name`），
    因此它的调用点不会出现在结果里 —— 这正是"只剩一种写法"的机械判据。
    """
    tree = ast.parse(ADMIN_API.read_text(encoding="utf-8"))
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "_json"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        status = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            status = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "status" and isinstance(kw.value, ast.Constant):
                status = kw.value.value
        if status != 400:
            continue
        for key, value in zip(node.args[0].keys, node.args[0].values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "error"):
                continue
            if isinstance(value, ast.Constant) and "json" in str(value.value).lower():
                found.setdefault(str(value.value), []).append(node.lineno)
    return found


def test_no_second_spelling_for_invalid_json():
    """同一个条件不许有第二种拼法：含 JSON 的 400 文案集合必须恰好是登记表。

    没有这一条，将来谁在一个新端点上顺手写下第三种说法都没人发现 ——
    F4 修的就是这个（`invalid JSON body` 曾经活在 4 个端点上）。
    """
    found = _json_related_400_literals()
    unregistered = {m: lines for m, lines in found.items() if m not in REGISTERED_JSON_400}
    assert not unregistered, (
        f"出现了未登记的、含 JSON 的 400 文案：{unregistered}。\n"
        f"若它就是「请求体不是合法 JSON」这个条件，请改用 CANONICAL_INVALID_JSON 常量"
        f"（不要写字面量）；若是**另一个**条件，请登记到 REGISTERED_JSON_400 并说明"
        f"它和既有条件的区别")
    assert CANONICAL_INVALID_JSON not in found, (
        "规范文案被写成了字面量 —— 请改用 CANONICAL_INVALID_JSON 常量，"
        "这样'只有一种写法'才是结构性保证而不是约定")


# ─── F5：内部 docstring 必须与对外文档同口径 ────────────────────

def test_output_field_docstring_matches_user_docs():
    """`output` 字段：内部 docstring 与 `docs/api.md` 必须说同一件事（F5）。

    行为是设计意图（只校验不落盘，docs/api.md:188 对用户写明）；此前 `/api/tasks` 与
    `/api/workflows/{name}/runs` 的 docstring 写的是「输出文件路径」，等于让下一个读代码的
    人以为它会落盘。
    """
    src = ADMIN_API.read_text(encoding="utf-8")
    assert "输出文件路径" not in src, (
        "内部 docstring 又出现「输出文件路径」了 —— 与 docs/api.md 的"
        "「只做白名单校验，不参与落盘」矛盾（F5 回归）")

    docs = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    assert "不参与落盘" in docs, "用户文档的 output 口径变了，请核对实现与测试"
    # 两个入口 docstring + `_launch_from_doc` 的实现说明，共三处同口径
    assert src.count("不参与落盘") >= 3, (
        f"admin_api.py 里只有 {src.count('不参与落盘')} 处说明 output 不落盘，"
        "应为 3 处（/api/tasks、/api/workflows/{name}/runs、_launch_from_doc）")
