"""打磨批：日志撑破卡片（F6）、Metrics 假徽标（F8）、原生校验抢跑（F10）、流水线失败静默（F11）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告 §2 F6 / §6 F8 / §8 F10 / §8 F11（板面 §二十三
台账归 zcode C-24）。另含 **F14② 的另一半**：6 张表补 `aria-label`（第 5 组只做了表头 `scope`）。

- **F6**：`.logs-table .log-msg` 注释写「配合 table 布局让 ellipsis 生效」，规则里却**没有**
  `white-space/overflow/text-overflow` ⇒ 长消息把卡片撑成横向滚动（实测 160 字符无空格消息：
  `cardBodyScrollW 1836 > clientW 1246`）。
- **F8**：卡头 `#badge-metrics` 是**永久占位符**「▲」——`els.badgeMetrics` 只在 cacheDom 里被赋值，
  **没有任何 renderer 写过它**；全站另外 11 个徽标都有真实语义。
- **F10**：`<form>` 无 `novalidate` + `textarea required` ⇒ 浏览器原生校验先拦，`#nt-msg` 里那段
  带 `.is-error` 的中文文案**永不出现**（实测空提交后 `msgHidden:true`、`posted:0`）。
- **F11**：`/api/pipeline` 失败只 `console.warn`，`#nt-pipeline` 为空时 `submitTask` **静默省略**
  `pipeline` 字段 ⇒ 用户以为选了默认，实际看到空下拉且零提示。

判据用**绑定敏感**写法并**剥注释**：修复处刻意在注释里引用了旧写法（`.retry-btn`、`▲` 等），
全文匹配会把注释当违规——同批已踩过两次这类假红。
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


def _css_block(selector_pattern: str) -> str:
    css = STYLE_CSS.read_text(encoding="utf-8")
    m = re.search(selector_pattern + r"\s*\{([^}]*)\}", css)
    assert m, f"style.css 里找不到 {selector_pattern} 的规则块 —— 判据需要改写法"
    return m.group(1)


# ─── 射程自证 ────────────────────────────────────────────────

def test_extraction_is_not_vacuous():
    js = _js()
    for name in ("renderMetrics", "loadPipelines", "submitTask", "tableEl"):
        assert len(_fn_body(js, name)) > 80, f"{name} 的函数体取不到，解析器失效"


# ─── F6：日志单元格必须完整省略号三件套 ────────────────────

def test_log_message_cell_has_full_ellipsis_trio():
    """`max-width: 0` 只是配合 table 布局；**真正让省略号生效的是后三行**。"""
    block = _css_block(r"\.logs-table\s+\.log-msg")
    missing = [p for p in ("white-space", "overflow", "text-overflow")
               if not re.search(p, block)]
    assert not missing, (
        f"`.logs-table .log-msg` 缺少 {missing} —— 长日志会把卡片撑成横向滚动（F6 回归）")
    assert re.search(r"text-overflow\s*:\s*ellipsis", block), "不是省略号而是别的截断方式"
    assert re.search(r"white-space\s*:\s*nowrap", block), "没有 nowrap，长消息仍会换行撑高"


# ─── F8：Metrics 徽标必须有真实写入方 ──────────────────────

def test_metrics_badge_is_written_by_a_renderer():
    js = _js()
    assert "els.badgeMetrics" in _fn_body(js, "renderMetrics"), (
        "renderMetrics 没有写 `els.badgeMetrics` —— 徽标仍是永久占位符（F8 回归）")
    assert re.search(r"els\.badgeMetrics\.textContent\s*=", js), "徽标只取了节点却没赋值"
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'id="badge-metrics"[^>]*>([^<]*)<', html)
    assert m, "找不到 #badge-metrics 的初始文本"
    assert "▲" not in m.group(1), (
        "初始占位仍是「▲」——那是无解释的符号，且与「用 SVG 图标替代功能性符号」的既定方向相反")
    assert "title=" in re.search(r'<span id="badge-metrics"[^>]*', html).group(0), (
        "徽标没有 title 说明它是什么数（全站其它 11 个徽标都有明确语义）")


# ─── F10：原生校验不得抢跑 ─────────────────────────────────

def test_form_opts_out_of_native_validation():
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<form id="newtask-form"[^>]*', html)
    assert m, "找不到 #newtask-form —— 判据需要改写法"
    assert "novalidate" in m.group(0), (
        "表单没有 novalidate —— 浏览器原生气泡会先拦，`#nt-msg` 的中文文案永不出现（F10 回归）")
    assert re.search(r'id="nt-query"[^>]*aria-describedby="nt-msg"', html), (
        "Query 字段没有 aria-describedby 指向提示节点 —— 屏幕阅读器读不到校验文案")


def test_custom_validation_message_still_exists():
    """前提：自定义校验分支仍在（否则去掉 novalidate 就是删掉了唯一校验）。"""
    body = _fn_body(_js(), "submitTask")
    assert re.search(r"if\s*\(\s*!query\s*\)", body), "submitTask 里没有空值校验了"
    assert "showTaskMsg(" in body, "submitTask 不再走应用内提示"


# ─── F11：流水线列表失败不许静默 ───────────────────────────

def test_pipeline_load_failure_is_visible():
    body = _fn_body(_js(), "loadPipelines")
    assert re.search(r"catch\s*\(err\)\s*\{", body), "loadPipelines 没有 catch —— 判据需要改写法"
    assert "showTaskMsg(" in body, (
        "加载失败只留 console —— 用户看到的是一个空下拉且零提示（F11 回归）")


def test_submit_does_not_silently_drop_pipeline():
    """列表为空时不得**静默**省略 `pipeline` 字段。"""
    body = _fn_body(_js(), "submitTask")
    m = re.search(r'if\s*\(\s*els\.ntPipeline\s*&&\s*els\.ntPipeline\.value\s*\)', body)
    assert m, "submitTask 里没有 pipeline 取值分支 —— 判据需要改写法"
    tail = body[m.end(): m.end() + 400]
    assert "showTaskMsg(" in tail, (
        "没有后续分支在列表为空时告知用户「按服务端默认提交」（F11：静默省略 pipeline 字段）")


# ─── F14② 剩余半：6 张表都要 aria-label ────────────────────

def test_every_table_goes_through_labeled_helper():
    js = _js()
    raw = re.findall(r"elt\(\s*'table'", js)
    assert len(raw) == 1, (
        f"除了 tableEl 内部，还有 {len(raw) - 1} 处直接构造 `<table>` —— 那些表没有 aria-label")
    body = _fn_body(js, "tableEl")
    assert re.search(r"setAttribute\(\s*'aria-label'", body), "tableEl 没有设 aria-label"
    calls = re.findall(r"tableEl\(\s*'([^']+)'", js)
    assert len(calls) == 6, f"只给 {len(calls)} 张表设了 label，应为 6 张：{calls}"
    assert len(set(calls)) == 6, f"有重复的表名：{calls}"
