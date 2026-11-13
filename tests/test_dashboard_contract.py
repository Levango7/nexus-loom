"""控制台端点契约（产品评审 2026-10-20 §4.3 判据 1）：app.js 请求 → OpenAPI 收录。

零浏览器：解析 `dashboard/app.js` 里作为请求路径出现的 `/` 开头字符串字面量，
断言每个都能在 `openapi_spec.generate_spec()` 的 paths 里找到落点——
防"控制台调了一个没有任何接口契约收录的端点"。这类漂移真实发生过：
`/tasks/<id>/nodes/<name>` 实装后长期不在 spec 里（本批补齐时发现）。

判据自带射程自证：解析器对构造的坏 token 必须能判出 uncovered，
否则"提取不到任何 token"也会一直绿（假阴性陷阱）。
"""
from __future__ import annotations

import re
from pathlib import Path

from loom_core.openapi_spec import generate_spec

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"

#: 拼接链里的续段：单独出现时不是完整端点（完整路径由前后段拼出，
#: 例如 '/tasks/' + id + '/nodes/' + name）。续段的落点由专门判据钉住。
CONTINUATION_TOKENS = {"/nodes/", "/inputs", "/download"}


def _request_tokens(js: str) -> set[str]:
    """收集 JS 里 `'/xxx'` 形态的路径字面量（含拼接链上的每一段）。"""
    return set(re.findall(r"'((?:/[A-Za-z0-9_.-]*)+/?)'", js))


def _uncovered_tokens(tokens: set[str], paths: set[str]) -> set[str]:
    """tokens 里找不到落点的集合：精确命中 / 前缀命中（如 '/tasks/'）/ 续段白名单。"""
    uncovered = set()
    for tok in tokens:
        if tok in paths:
            continue
        if any(p.startswith(tok) for p in paths):
            continue
        if tok in CONTINUATION_TOKENS:
            continue
        uncovered.add(tok)
    return uncovered


def _spec_paths() -> set[str]:
    return set(generate_spec()["paths"].keys())


def test_every_console_request_maps_to_openapi():
    js = (DASH / "app.js").read_text(encoding="utf-8")
    tokens = _request_tokens(js)
    assert len(tokens) >= 8, f"解析器疑似失效，只提取到: {sorted(tokens)}"
    uncovered = _uncovered_tokens(tokens, _spec_paths())
    assert not uncovered, (
        f"控制台调了未收录进 OpenAPI 的端点: {sorted(uncovered)}——"
        "补 openapi_spec 或在 app.js 去掉该调用")


def test_concatenated_endpoints_are_documented():
    """拼接链的完整落点：每条续段对应的整条端点必须在 spec 里。

    S3 检查器（'/nodes/'）、T6 声明读取（'/inputs'）与交付物下载（'/download'）。
    """
    paths = _spec_paths()
    assert "/tasks/{task_id}/nodes/{node_name}" in paths
    assert "/api/workflows/{name}/inputs" in paths
    assert "/api/deliveries/{run_id}/download" in paths


def test_checker_can_fail_on_a_bogus_token():
    """射程自证：坏 token 必须被判 uncovered——否则本判据可能永远绿。"""
    bogus = _request_tokens("fetchJson(API_BASE + '/api/nonexistent-thing')")
    assert bogus == {"/api/nonexistent-thing"}
    assert _uncovered_tokens(bogus, _spec_paths()) == {"/api/nonexistent-thing"}


def test_checker_accepts_prefix_and_continuation():
    paths = _spec_paths()
    assert not _uncovered_tokens({"/tasks/"}, paths)        # 前缀命中
    assert not _uncovered_tokens({"/nodes/"}, paths)        # 续段白名单
    assert not _uncovered_tokens({"/health"}, paths)        # 精确命中
