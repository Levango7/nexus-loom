"""可达性与动效：账本行键盘可达（F7）、a11y 结构（F14）、按下态（F15）、减弱动效（F17）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告 §9 的 F14 / §4 的 F15 / §11 邻域的 F17 /
§9 的 F7（板面 §二十三 台账归 zcode C-24）。

- **F7**：交付账本行可点（`tr.onclick = () => openDelivery(...)`）却**不可键盘达**——
  没有 `tabindex`、没有键盘触发（实测 `tabIndex:-1`、Tab 走查 22 次焦点里没有账本行）。
  讽刺的是 CSS 早为它写了 `.row-clickable:focus-visible` 焦点环 ⇒ 元素不可聚焦时那条规则永不生效。
- **F14**（结构性四项）：① `<tr>` 上挂 `role="button"`（实测 4 行）把"这是一行"的隐式语义抹掉；
  ② `<th>` 无 `scope`（`thWithScope:0/thCount:4`），屏幕阅读器不知道每格属于哪一列；
  ③ 提交反馈 `#nt-msg` 无 `aria-live`（写了用户也不被告知）；④ Token 对话框无 `aria-modal`。
- **F15**：全站 `:active` 计数为 **0** —— 最重的两个审批按钮（放行/驳回）连 hover 都没有，
  按下去没有任何视觉变化。
- **F17**：无 `@media (prefers-reduced-motion: reduce)`（`grep -c` → 0）：刷新指示点的 2s
  无限脉冲与进度条宽度过渡在 reduce 下实测都不变；运维台是常开页面，影响被放大。

判据用**绑定敏感**写法（钉住"谁作用于谁、赋值给谁"），并**剥注释后**判定 —— 修复处的注释刻意
引用了旧写法来解释历史（同批第 1/3/4 组的变异电池都踩过"注释被当违规"这类假红）。
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"
APP_JS = DASH / "app.js"
STYLE_CSS = DASH / "style.css"
INDEX_HTML = DASH / "index.html"


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("//"))


def _js() -> str:
    return _strip_js_comments(APP_JS.read_text(encoding="utf-8"))


def _fn_body(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"app.js 里找不到函数 {name} —— 本文件的解析需要改写法"
    rest = src[m.start():]
    nxt = re.search(r"\n  (?:/\* ----|function )", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


# ─── 射程自证 ────────────────────────────────────────────────

def test_extraction_is_not_vacuous():
    js = _js()
    for name in ("makeKeyboardActivatable", "thCell", "renderDeliveries", "renderTasks"):
        assert len(_fn_body(js, name)) > 80, f"{name} 的函数体取不到，解析器失效"
    css = _css()
    assert ".refresh-indicator" in css and ".progress-fill" in css, "CSS 锚点漂了"


# ─── F7：账本行键盘可达 ─────────────────────────────────────

def test_delivery_rows_are_keyboard_reachable():
    """账本行必须与任务行同口径地可键盘达（F7）。"""
    body = _fn_body(_js(), "renderDeliveries")
    assert re.search(r"elt\(\s*'tr'\s*,\s*'row-clickable'\s*\)", body), (
        "找不到账本行构造 —— 判据需要改写法")
    assert re.search(r"makeKeyboardActivatable\(\s*tr\s*,", body), (
        "账本行没有 makeKeyboardActivatable —— 鼠标能点、键盘不可达（F7 回归）")
    assert "openDelivery(" in body.split("makeKeyboardActivatable")[1][:160], (
        "账本行的键盘触发没有指向 openDelivery（触发了别的动作）")


def test_row_activation_does_not_set_button_role():
    """`<tr>` 的键盘激活**不得**再挂 `role="button"`（F14①：会抹掉隐式 row 语义）。"""
    body = _fn_body(_js(), "makeKeyboardActivatable")
    assert re.search(r"if\s*\(\s*role\s*\)\s*el\.setAttribute\(\s*'role'", body), (
        "makeKeyboardActivatable 没有把 role 变成可空参数 —— 行上仍会挂 role=button")
    js = _js()
    # 计数等式：每个 `tr` 调用点都必须**同时**匹配「以 `, null)` 收尾」的形态。
    # （第一版用 `[^)]*` 收尾匹配，被箭头函数里的 `()` 截断了 —— 正则到第一个右括号就停，
    #   `, null)` 根本没进匹配串，于是"没传 null"也会通过。）
    all_calls = re.findall(r"makeKeyboardActivatable\(\s*tr\s*,", js)
    assert all_calls, "找不到任何 tr 上的键盘激活调用 —— 判据需要改写法"
    with_null = re.findall(r"makeKeyboardActivatable\(\s*tr\s*,.*?,\s*null\s*\)", js, re.S)
    assert len(with_null) == len(all_calls), (
        f"{len(all_calls)} 处 tr 键盘激活里只有 {len(with_null)} 处传了 role=null —— "
        "其余仍会挂 role=button，抹掉行的隐式语义（F14①）")


# ─── F14②：表头 scope ───────────────────────────────────────

def test_all_table_headers_go_through_scoped_helper():
    """所有 `<th>` 必须经由 `thCell()`（带 `scope="col"`）—— 不许有旁路。"""
    body = _fn_body(_js(), "thCell")
    assert re.search(r"scope\s*=\s*'col'", body), (
        "thCell 没有设 scope='col' —— 屏幕阅读器不知道每格属于哪一列（F14②）")
    js = _js()
    raw = re.findall(r"elt\(\s*'th'", js)
    assert len(raw) == 1, (
        f"除了 thCell 内部，还有 {len(raw) - 1} 处直接构造 `<th>` —— 那些表头不会有 scope")


# ─── F14③④：提交反馈与对话框 ───────────────────────────────

def test_submit_feedback_is_announced():
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'id="nt-msg"[^>]*', html)
    assert m, "找不到 #nt-msg —— 判据需要改写法"
    assert "aria-live" in m.group(0), "提交反馈没有 aria-live —— 屏幕阅读器不会告知结果（F14③）"


def test_token_dialog_is_modal():
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'id="token-prompt"[^>]*', html)
    assert m, "找不到 #token-prompt —— 判据需要改写法"
    assert 'aria-modal="true"' in m.group(0), "Token 对话框没有 aria-modal（F14④）"


# ─── F15：按下态 ───────────────────────────────────────────

def test_pressed_and_hover_states_exist():
    """`:active` 必须存在，且覆盖最重的两个审批按钮（含 hover）。"""
    css = _css()
    assert ":active" in css, "全站又没有 `:active` 了 —— 按下去毫无反馈（F15 回归）"
    assert re.search(r"\.approval-actions button:active", css), (
        "最重的两个审批按钮没有 `:active`（运行详情里放行/驳回）")
    assert re.search(r"\.approval-actions button:hover", css), (
        "审批按钮连 hover 都没有（F15 原文）")
    assert re.search(r"\.btn-reject:hover", css), "驳回按钮没有 hover 态"


# ─── F17：减弱动效 ─────────────────────────────────────────

def _reduced_motion_block() -> str:
    css = _css()
    m = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
    assert m, (
        "style.css 里没有 `@media (prefers-reduced-motion: reduce)` —— 两处动效在 reduce 下照播（F17）")
    return m.group(1)


def test_reduced_motion_disables_both_animations():
    block = _reduced_motion_block()
    assert re.search(r"\.refresh-indicator\s*\{\s*animation:\s*none", block), (
        "减弱动效下刷新指示点仍在无限脉冲")
    assert re.search(r"\.progress-fill\s*\{\s*transition:\s*none", block), (
        "减弱动效下进度条仍在做宽度过渡")


def test_reduced_motion_premise_holds():
    """前提：那两处动效**确实存在**（否则"禁用"就是对着空气写的，判据会变成恒真）。"""
    css = _css()
    assert re.search(r"\.refresh-indicator\s*\{[^}]*animation:", css, re.S), (
        "刷新指示点已经没有 animation 了 —— 若动效已移除，请连同 F17 的处置一并复审")
    assert re.search(r"\.progress-fill\s*\{[^}]*transition:", css, re.S), (
        "进度条已经没有 transition 了 —— 同上，请复审 F17")
