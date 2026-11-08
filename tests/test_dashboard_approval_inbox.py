"""审批收件箱的两个真缺陷：按钮无样式（F2）与动作无防重/无 loading/失败无留痕（F16）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告（`%TEMP%/nexusloom-audit/L2-frontend.md`
§4 F2 与 F16），板面 §二十三 台账归 zcode C-24（qoder 只取 L2-01/L2-02 配色两条）。

- **F2**（P2）：收件箱的「放行 / 驳回」按钮用的是 `retry-btn`，而 `style.css` 里
  `.retry-btn` **只在 `.error-overlay` 作用域下有声明** ⇒ 收件箱里一条都不命中，两个主操作
  按钮退化成浏览器原生灰按钮（实测 computed style：`borderRadius 0px / padding 0px /
  fontFamily Arial / cursor default / borderColor rgb(0,0,0)`）。
- **F16**（P3）：`decideApproval` 无防重、无 loading，失败只 `alert` —— 关掉弹窗即无痕，
  且无法回看是哪一条失败；而"点下去没反应"又反过来诱使用户连点（连点＝多个请求）。

本文件用**文本断言**而不是浏览器：这两条的性质是"接线接错了/漏接了"，静态可判定；
真浏览器复核另有 `check_dashboard_browser.py` 的一条主题项承载视觉面（对比度那组）。
每条判据都带**射程自证**：先证明解析器真能取到目标片段，否则"取不到就恒绿"。
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"
APP_JS = DASH / "app.js"
STYLE_CSS = DASH / "style.css"


def _approvals_region() -> str:
    """`app.js` 里审批收件箱那一块（从 Approvals 段注释到下一段注释）。

    只取这一段而不是全文 grep：同一个类名可能在别处（例如 `.error-overlay` 的重试按钮）
    合法出现，全文查找会让"接错地方"蒙混过关——F2 正是错在"类名存在、只是接错了作用域"。
    """
    js = APP_JS.read_text(encoding="utf-8")
    start = js.find("/* ---- Approvals")
    assert start != -1, "app.js 里找不到 Approvals 段注释 —— 本文件的解析需要改写法"
    end = js.find("/* ---- Deliveries", start)
    assert end != -1 and end > start, "Approvals 段的结束锚点（Deliveries）找不到了"
    return js[start:end]


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释后的源码。

    "不得再出现 `retry-btn`" 这类判据必须**只看代码**：修复处刻意留了一条注释
    写明"此处曾写作 `retry-btn`"（解释为什么不能那样写），全文匹配会把这条注释
    当成违规——判据的第一版就是这样误红自己的，故补这一步。
    只处理**整行** `//` 注释与 `/* ... */` 块：本仓注释都在独立行上，
    这样不会误伤字符串里的 `http://`。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("//"))


def _fn_body(js: str, name: str) -> str:
    """粗略取一个函数的函数体（同名函数在本文件里只有一个）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", js)
    assert m, f"app.js 里找不到函数 {name} —— 本文件的解析需要改写法"
    # 从函数签名起，取到下一个顶层 `function ` 或 `/* ----` 段注释之前
    rest = js[m.start():]
    nxt = re.search(r"\n  (?:/\* ----|function )", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


# ─── 射程自证 ────────────────────────────────────────────────

def test_region_extraction_is_not_vacuous():
    """解析器必须真取到目标片段，且取到的是"审批收件箱"而不是别处。"""
    region = _approvals_region()
    assert len(region) > 500, f"Approvals 段只取到 {len(region)} 字节，解析器失效了"
    assert "renderApprovals" in region, "取到的片段里没有 renderApprovals —— 锚点漂了"
    assert "decideApproval" in region, "取到的片段里没有 decideApproval —— 锚点漂了"
    body = _fn_body(region, "decideApproval")
    assert "fetch(" in body, "decideApproval 的函数体取错了"


# ─── F2：按钮必须用共享的审批按钮类 ──────────────────────────

def test_inbox_buttons_use_shared_approval_button_classes():
    """收件箱两个按钮必须挂 `btn-approve` / `btn-reject`（有站内声明的类）。"""
    region = _approvals_region()
    assert "'btn-approve'" in region, (
        "收件箱的「放行」按钮没有用 `btn-approve` —— 站内该类的底座与配色就白定义了")
    assert "'btn-reject'" in region, "收件箱的「驳回」按钮没有用 `btn-reject`"


def test_inbox_does_not_reuse_the_error_overlay_retry_class():
    """不得再退回 `retry-btn`（F2 回归锁）。

    `retry-btn` 不是"错类名"这么简单：它**存在**，只是全部声明都在 `.error-overlay` 里，
    放在收件箱里等于零命中 —— 这个坑的形状就是"类名存在但接错作用域"，所以两件事都要断：
    ① 收件箱不用它；② 它确实只被 `.error-overlay` 限定（否则本判据的前提就变了，该复审）。
    """
    code = _strip_js_comments(_approvals_region())
    assert "retry-btn" not in code, (
        "审批收件箱的**代码**里又用回了 `retry-btn`：该类只在 `.error-overlay` 作用域下有样式，"
        "在收件箱里会退化成浏览器原生按钮（F2 回归）")
    # 射程自证：剥离注释后仍要能看见真代码，否则"注释全被剥掉"也会恒绿。
    assert "elt(" in code and "decideApproval" in code, (
        "剥离注释后取不到任何代码 —— 剥离器写坏了，本判据会变成恒真")

    css = STYLE_CSS.read_text(encoding="utf-8")
    selectors = [s.strip() for s in re.findall(r"([^{}]*)\{", css)]
    retry_rules = [s for s in selectors if ".retry-btn" in s]
    assert retry_rules, (
        "style.css 里已经没有 `.retry-btn` 的任何规则了 —— 本判据的'作用域限定'前提失效，"
        "请连同 F2 的处置一并复审（别让判据悄悄变成恒真）")
    unscoped = [s for s in retry_rules if ".error-overlay" not in s]
    assert not unscoped, (
        f"`.retry-btn` 出现了不受 `.error-overlay` 限定的规则：{unscoped} —— ")


def test_inbox_buttons_are_wrapped_in_approval_actions():
    """两个按钮要包在 `.approval-actions` 里：**底座**（padding/圆角/光标/字号）来自
    `.approval-actions button`，只换配色类是不够的。"""
    region = _approvals_region()
    assert "'approval-actions'" in region, (
        "收件箱的操作按钮没有 `approval-actions` 容器 —— 底座规则（`.approval-actions button`）"
        "不会命中，按钮会「有色无壳」（无内边距/无圆角/默认光标）")
    # 结构顺序：容器 → btn-approve → btn-reject → append 进容器
    assert region.index("'approval-actions'") < region.index("'btn-approve'"), \
        "approval-actions 容器应在按钮之前创建"


# ─── F16：防重 / loading / 失败留痕 ──────────────────────────

def test_decide_approval_guards_against_duplicate_submits():
    """在途同动作必须被忽略（防重）。

    此前无防重：连点会打出多个 `POST /tasks/<id>/{approve,reject}`。而"点下去没反应"
    正是诱使用户连点的原因 —— 两条要一起修才有意义。
    """
    body = _fn_body(_approvals_region(), "decideApproval")
    assert "approvalInFlight" in body, (
        "decideApproval 没有防重集合（`approvalInFlight`）—— 连点会重复提交")
    assert re.search(r"if\s*\(\s*approvalInFlight\.has\(", body), (
        "防重只登记不清查：入口必须写成 `if (approvalInFlight.has(key)) return;`。"
        "判据要求 `if` **直接**作用于 `has(...)` —— 写成 `if (false && has(...))` 这类"
        "恒假短路会被判红（第一版判据就是被它逃逸的）")
    assert "approvalInFlight.add(" in body and "approvalInFlight.delete(" in body, (
        "防重集合没有成对 add/delete（delete 必须在 finally 里，异常路径也要放行）")


def test_decide_approval_shows_in_flight_state():
    """在途期间按钮要 `disabled` 且文案改成「处理中…」，让"点了没反应"不再出现。"""
    body = _fn_body(_approvals_region(), "decideApproval")
    assert "disabled = true" in body, "在途期间没有禁用按钮（无 loading 反馈）"
    assert "处理中…" in body, "在途期间按钮文案没有变化（用户看不出正在提交）"


def test_approval_failure_is_rendered_in_panel_not_only_alert():
    """失败必须落进 `state.approvalError` 并由面板就地呈现。

    原实现只有 `alert(...)`：弹窗一关就没有痕迹，也回看不了是哪一条失败；
    而面板每 5s 整块重画 ⇒ 提示必须存在 state 里才活得过一轮。
    """
    region = _approvals_region()
    body = _fn_body(region, "decideApproval")
    assert re.search(r"\bstate\.approvalError\s*=\s*\{", body), (
        "失败没有**赋值**给 `state.approvalError` —— 面板重画后提示会消失，等于只留一个弹窗。"
        "判据钉住赋值形态（`state.approvalError = {`）：第一版只做子串匹配，"
        "把字段改名成 `approvalError_` 也能蒙混过关，故收紧")
    assert "alert('审批操作失败" not in body, (
        "失败仍在用 alert 作为**唯一**反馈：关掉即无痕（若保留 alert 作为附加提示，"
        "请改成同时写入 state.approvalError 并调整本判据的措辞）")
    assert "approvalErrorNotice" in region, (
        "没有 approvalErrorNotice 把 state.approvalError 渲染出来 —— 存了不显示等于没修")
    # 通告要进 replaceChildren 的两个分支（有待批项 / 空态都带上）
    assert region.count("notice") >= 3, (
        "通告只在其中一个分支接线：空态或列表态会把它吞掉")


def test_approval_notice_survives_re_render():
    """通告的载体是 state（而不是挂在已渲染的节点上）—— 这是它能扛过 5s 轮询的原因。"""
    js = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"let state = \{(.*?)\n  \};", js, re.S)
    assert m, "找不到 state 对象字面量 —— 解析需要改写法"
    assert "approvalError" in m.group(1), (
        "state 里没有 approvalError 字段：轮询重画会把就地提示抹掉")
