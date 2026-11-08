"""控制台三态呈现：失败不许画成"没有数据"（F4）、首屏不许自称"没有了"（F4b）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告 §6 的 F4 / F4b（板面 §二十三 台账归 zcode C-24；
配色两条由 #112 承担）。**这两条是同一件事的两半**，报告本身也把 F4b「并入 F4」：

- **F4**（P2）：`fetchAll()` 里 `/health` 与 `/api/dashboard` 两源**绕过 `settle()`**，
  失败被直接折成 `null` ⇒ 渲染层只能画空态 ⇒ `/api/dashboard` 一挂，Tasks / Agents 面板就
  谎报「暂无活跃任务 / 0 / No agents registered」（实测：真话只剩卡头那行 0.75rem 小字
  "部分数据不可用"）。其余 11 个源都走了 `settle` + `renderPanelError`，**最核心的 Runs/Agents 两块漏了**
  —— 正是产品评审 §4.2 点名要修的三件事之三「每面板独立 loading/error（不要 allSettled 把失败折叠成空）」。
- **F4b**（P3）：`index.html` 里 7 块面板的静态占位是「暂无…」；首屏等服务端（含 4s 超时窗口）期间
  一律自称"没有了"，与 F4 叠加后**「没跑过任务」「服务端挂了」「还在加载」三态同形**。

判据全部用**绑定敏感**的写法（正则钉住"谁作用于谁、赋值给谁"），不用全文子串包含 ——
后者会被"改个名字/加一处恒假短路"蒙混（同批第 1 组 PR #113 的变异电池实测逃逸过两处，见 §二十六）。
每条都带**前提可复核**：判据依赖的"外部事实"（`index.html` 的静态文案、`renderPanelError` 的契约）
单独断言，前提变了就明确报出来，而不是让判据悄悄变恒真。
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"
APP_JS = DASH / "app.js"
INDEX_HTML = DASH / "index.html"

#: 首屏静态占位是"暂无…"、因而需要被加载态顶掉的 7 块（报告 F4b 实测列表）。
STATIC_EMPTY_PANELS = [
    "tasksPanel", "agentsPanel", "qualityPanel", "costPanel",
    "logsPanel", "approvalsPanel", "deliveriesPanel",
]
#: 与上面一一对应的 index.html 静态文案（用来断言"前提仍成立"）。
STATIC_TEXTS = ["暂无任务", "暂无 Agent", "暂无质量数据", "暂无成本数据",
                "暂无日志", "没有等待审批的运行", "还没有交付记录"]


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """去掉整行 `//` 注释与 `/* ... */` 块（同批第 1 组的同款工具）。

    判据必须只看代码：修复处刻意留了注释解释旧写法，全文匹配会把注释当违规。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("//"))


def _fn_body(src: str, name: str) -> str:
    """取一个函数的函数体（到下一个顶层 `function ` 或 `/* ----` 段注释为止）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"app.js 里找不到函数 {name} —— 本文件的解析需要改写法"
    rest = src[m.start():]
    nxt = re.search(r"\n  (?:/\* ----|function )", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


# ─── 射程自证 ────────────────────────────────────────────────

def test_extraction_is_not_vacuous():
    """三个被审函数都要真取到，且取到的是函数体而不是空串。"""
    js = _strip_js_comments(_js())
    for name in ("fetchAll", "renderTasks", "renderAgents", "renderStatus", "renderFirstPaint"):
        body = _fn_body(js, name)
        assert len(body) > 80, f"{name} 的函数体只取到 {len(body)} 字节，解析器失效"


def test_render_panel_error_contract_premise():
    """前提：`renderPanelError` 认的是 `__forbidden` / `__error` 两个标记（失败态的公共机制）。"""
    body = _fn_body(_strip_js_comments(_js()), "renderPanelError")
    assert "__forbidden" in body and "__error" in body, (
        "renderPanelError 的标记契约变了 —— 本文件关于「失败携带什么」的判据需要一并复审")


# ─── F4：失败必须带标记，不许折成 null ─────────────────────────

def test_health_and_dashboard_sources_go_through_settle():
    """`/health` 与 `/api/dashboard` 必须走 `settle(...)`（绑定形态，不是"用到过 settle"）。"""
    body = _fn_body(_strip_js_comments(_js()), "fetchAll")
    assert re.search(r"const\s+health\s*=\s*settle\(results\[0\]\)", body), (
        "`/health` 没有走 `settle(results[0])` —— 失败会被折成空态（F4 回归）")
    assert re.search(r"const\s+dash\s*=\s*settle\(results\[1\]\)", body), (
        "`/api/dashboard` 没有走 `settle(results[1])` —— Tasks/Agents 会谎报「暂无」（F4 回归）")


def test_no_source_folds_failure_into_null():
    """`... ? value : null` 这种把失败折成 null 的写法不得回潮。"""
    body = _strip_js_comments(_fn_body(_js(), "fetchAll"))
    bad = re.findall(r"results\[\d+\]\.status\s*===\s*'fulfilled'\s*\?\s*results\[\d+\]\.value\s*:\s*null", body)
    assert not bad, f"又有源把失败折成 null 了：{bad}"


def test_list_panels_render_failure_before_empty_state():
    """三个渲染器必须先画失败、再考虑空态（顺序也要断：先 error 后 empty）。"""
    js = _strip_js_comments(_js())
    for name, panel in (("renderTasks", "els.tasksPanel"),
                        ("renderAgents", "els.agentsPanel"),
                        ("renderStatus", "els.statusPanel")):
        body = _fn_body(js, name)
        m_err = re.search(r"renderPanelError\(", body)
        assert m_err, f"{name} 没有接 renderPanelError —— 取不到数据时会画成空态（F4）"
        assert panel in body[:m_err.end() + 120], (
            f"{name} 的 renderPanelError 没有传 {panel}（画到别的面板上了）")
        m_empty = re.search(r"emptyState\(", body)
        assert m_empty, f"{name} 里没有空态分支了 —— 判据需要复审"
        assert m_err.start() < m_empty.start(), (
            f"{name} 把空态画在了失败之前 —— 失败仍会被「暂无…」顶掉（F4 的核心症状）")


# ─── F4b：首屏必须先画加载态 ─────────────────────────────────

def test_first_paint_replaces_the_seven_static_panels():
    """`renderFirstPaint` 必须覆盖那 7 块，且文案是「加载中…」。"""
    body = _fn_body(_strip_js_comments(_js()), "renderFirstPaint")
    missing = [p for p in STATIC_EMPTY_PANELS if f"els.{p}" not in body]
    assert not missing, f"renderFirstPaint 漏了这些面板：{missing}"
    assert "加载中…" in body, "首屏占位文案不是「加载中…」"


def test_first_paint_runs_before_the_first_fetch():
    """必须在 `update()` **之前**调用（否则首屏窗口内仍是静态"暂无…"）。"""
    js = _strip_js_comments(_js())
    init = _fn_body(js, "init")
    m_fp = re.search(r"renderFirstPaint\(\)", init)
    assert m_fp, "init 里没有调用 renderFirstPaint —— 首屏仍是静态「暂无…」（F4b 回归）"
    m_up = re.search(r"\bupdate\(\)", init)
    assert m_up, "init 里找不到 update() —— 判据需要改写法"
    assert m_fp.start() < m_up.start(), "renderFirstPaint 调用在 update() 之后，起不到首屏作用"


def test_static_empty_text_premise_still_holds():
    """前提复核：`index.html` 里这 7 块的静态文案**仍是**"暂无…"。

    若哪天 index.html 自己改成加载文案，本组判据（"静态占位需要被顶掉"）的前提就变了 ——
    那时应当复审 renderFirstPaint 是否还必要，而不是让它悄悄恒真。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    missing = [t for t in STATIC_TEXTS if t not in html]
    assert not missing, (
        f"index.html 里这些静态占位文案不在了：{missing} —— 若已改成加载态，"
        "请连同 F4b 的处置与 renderFirstPaint 的必要性一并复审")
