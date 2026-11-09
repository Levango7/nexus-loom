"""离线覆盖层：英文技术串 + 把 HTML 里的中文指引顶掉（L2 F5）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告 §6 的 F5（板面 §二十三 台账归 zcode C-24）。

**F5（P2，真缺陷）**：全站最关键的失败路径（整站离线）上有两处问题——
① 文案是**英文技术串**（`All API endpoints unreachable` / `Unable to connect to the Loom API at …`
   以及覆盖层标题 `Dashboard Offline`）；
② 更糟的是 `renderError` 用 `els.errorMsg.textContent = …` **整个替换**了 `index.html:52` 里那句
   中文指引（「无法连接到 Admin API。请确保 pipeline 已在 `--admin`/`--dashboard` 模式下运行。」）
   ⇒ 实测 `hintInHtmlStillThere:false`：**"该怎么办"被抹掉**，用户只剩一句英文。
评审 §4.2 把「统一中文文案」列为控制台必补三件事之一（`docs/product-design-review-2026-10-20.md:172`），
且本仓已把 `timeAgo`、`<title>` 中文化 —— 这里漏了两处（另一处 `renderAgents` 的空态英文
`No agents registered`，F4 的实测里也顺带出现）。

判据用**绑定敏感**正则（钉住"谁写谁、往哪插"），并**剥注释后**判定 —— 修复处的注释里刻意写了
旧英文串来解释历史，全文匹配会把注释当违规（同批第 1/3 组的变异电池都踩过这类假红）。
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"
APP_JS = DASH / "app.js"
INDEX_HTML = DASH / "index.html"

#: 修复前的英文串（也是"不得回潮"的清单）。
ENGLISH_STRINGS = ["All API endpoints unreachable",
                   "Unable to connect to the Loom API at",
                   "Dashboard Offline",
                   "No agents registered"]


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


# ─── 射程自证 ────────────────────────────────────────────────

def test_extraction_is_not_vacuous():
    js = _js()
    for name in ("renderError", "renderAgents"):
        body = _fn_body(js, name)
        assert len(body) > 100, f"{name} 的函数体只取到 {len(body)} 字节，解析器失效"
    assert "所有 API 端点均不可达" in js, (
        "剥注释后的 app.js 里找不到那句中文离线文案 —— 解析或修复丢了")


# ─── ① 英文串不得回潮（代码层，不看注释）────────────────────

def test_no_english_technical_strings_in_code():
    """四个英文串都不得回到**代码**里（注释可留作历史说明）。"""
    js = _js()
    html = INDEX_HTML.read_text(encoding="utf-8")
    bad = []
    for s in ENGLISH_STRINGS:
        if s == "Dashboard Offline":
            if s in html:
                bad.append(f"index.html: {s}")
        elif s in js:
            bad.append(f"app.js: {s}")
    assert not bad, f"英文技术串回潮：{bad}（评测 §4.2 要求统一中文文案）"


def test_chinese_replacements_are_in_place():
    """替换后的中文串必须在场（不是"删掉英文就算修好"）。"""
    js = _js()
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "所有 API 端点均不可达" in js, "离线错误串没有中文替换"
    assert "无法连接到 Loom API" in js, "覆盖层详情的兜底文案没有中文替换"
    assert "暂无已注册 Agent" in js, "Agents 空态没有中文替换"
    assert "控制台已离线" in html, "覆盖层标题没有中文替换"


# ─── ② 核心缺陷：中文指引不得被覆盖 ─────────────────────────

def test_render_error_does_not_clobber_the_guidance():
    """`renderError` **不得**给 `els.errorMsg.textContent` 赋值。

    这条是 F5 的核心：原实现那一行就是"把 '该怎么办' 抹掉"的动作本身。
    判据钉住赋值形态（谁被写），而不是"文件里没有 errorMsg"这种宽匹配。
    """
    body = _fn_body(_js(), "renderError")
    assert not re.search(r"els\.errorMsg\.textContent\s*=", body), (
        "renderError 又在覆盖 `els.errorMsg.textContent` —— index.html 里那句中文指引"
        "（含 --admin/--dashboard 怎么起）会被顶掉（F5 回归）")
    assert "insertBefore(" in body and "els.errorMsg" in body, (
        "详情没有挂在指引节点旁边：指引要常驻、详情另起一个节点")


def test_detail_node_is_reused_not_appended_per_render():
    """详情节点必须复用（`els.errorDetail`），否则每次渲染都往覆盖层里塞一个 `<p>`。"""
    body = _fn_body(_js(), "renderError")
    assert re.search(r"=\s*els\.errorDetail\b", body), "没有复用已存在的详情节点（els.errorDetail）"
    assert re.search(r"els\.errorDetail\s*=\s*detail", body), "详情节点没有登记回 els"
    m = re.search(r"if\s*\(\s*!\s*detail\s*\)", body)
    assert m, "没有 `if (!detail)` 守卫 —— 会在每次渲染时重复插入详情节点"


# ─── 前提：被保护的那句指引确实在 HTML 里 ───────────────────

def test_guidance_premise_holds():
    """前提：`index.html` 里那句中文指引（含 `--admin`）与 `role="alert"` 仍在。

    若哪天指引被移走/改写，本组判据（"指引不得被覆盖"）的前提就变了 ⇒ 应当复审，
    而不是让判据悄悄变成对空字符串的保护。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "--admin" in html and "--dashboard" in html, (
        "HTML 里那句'怎么起服务'的指引不在了 —— 请连同 F5 的处置一并复审")
    m = re.search(r'id="error-overlay"[^>]*role="alert"', html)
    assert m, "离线覆盖层不再是 role=\"alert\"（可达性前提变了）"


# ─── 前提：详情节点继承既有样式，无需新增 CSS ──────────────

def test_detail_paragraph_inherits_existing_overlay_style():
    """前提：`.error-overlay p` 有样式 ⇒ 新增的详情 `<p>` 免写 CSS。"""
    css = (DASH / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.error-overlay\s+p\s*\{", css), (
        "`.error-overlay p` 的样式没了 —— 详情节点会退化成无样式文本，请复核 F5 的处置")
