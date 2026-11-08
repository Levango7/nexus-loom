"""仪表盘配色对比度门禁（WCAG 2.1 AA，实算，不目测）。

背景（2026-11-08，L2-01 / L2-02）：默认主题是浅色，而浅色状态色系此前成片低于
4.5:1——`.badge-status.running` 实测 2.96、`.pending` 2.86、实心「放行」按钮 3.30；
另有 7 处硬编码旧主题色号（`rgba(76,175,80,.15)` / `rgba(233,69,96,.15)`）不随主题
切换，前景用新 token、底色用旧色号 ⇒ 配对值是偶然的。

本文件把这两类回归钉成判据，而不是只把颜色改一遍：

1. **对比度**：每个承载语义色文本的规则，按它**实际所在的面**（卡片 / 次级面 /
   raised / tint 叠底）解析 token、合成 alpha，实算比率，对照 WCAG 阈值
   （正文 4.5:1；≥18.66px 且 ≥700 粗体、或 ≥24px 走 3:1）。
2. **覆盖面**：枚举 `style.css` 里所有把 `color` 设成语义色 token 的规则，每条
   必须在表里或显式登记为非文本——漏一条就红，新规则不能悄悄绕过门。
3. **同源**：`--tint-X` 的 RGB 必须与 `--status-X` 同源（`--tint-highlight` 对
   `--highlight`）。这正是 F9 那类漂移的判据。
4. **硬编码普查**：两个主题 token 块之外不许出现 hex/rgb/rgba 字面量。
5. **反死判据**：表里每个选择器必须真的存在；字体大小取"规则自己声明的"或显式
   指定的来源规则，两者都没有就红——否则"把字号缩小到大字阈以下"会漏过门。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

DASHBOARD = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"
CSS_RAW = (DASHBOARD / "style.css").read_text(encoding="utf-8")
APP_JS = (DASHBOARD / "app.js").read_text(encoding="utf-8")

#: `html { font-size: 14px }`——rem 换算的唯一根据；改了它所有 rem 阈值都要重算。
FONT_BASE_PX = 14.0

#: WCAG 2.1「大字号」：18pt（24px）/ 14pt 粗体（18.66px）
LARGE_PX = 18.66
LARGE_WHEN_ANY_PX = 24.0

_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _blank_comments(text: str) -> str:
    """把注释替换成等长空白：偏移量保持不变，后面按偏移判 token 块才准。"""
    return _COMMENT.sub(lambda m: " " * len(m.group(0)), text)


CSS = _blank_comments(CSS_RAW)


class Rule(NamedTuple):
    selectors: tuple[str, ...]
    decls: dict[str, str]
    span: tuple[int, int]  # 声明块（含花括号内文）在 CSS 里的绝对偏移


def _iter_rules(text: str, base: int = 0):
    i = 0
    while True:
        brace = text.find("{", i)
        if brace < 0:
            return
        prelude = text[i:brace].strip()
        depth, j = 1, brace + 1
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth:
            raise AssertionError(f"style.css 花括号不配对（prelude={prelude!r}）")
        body = text[brace + 1:j - 1]
        body_start = base + brace + 1
        if prelude.startswith("@"):
            yield from _iter_rules(body, body_start)
        else:
            selectors = tuple(s.strip() for s in prelude.split(",") if s.strip())
            yield Rule(selectors, _decls(body), (body_start, base + j - 1))
        i = j


def _decls(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        name = name.strip().lower()
        if name:
            out[name] = value.strip()
    return out


RULES: list[Rule] = list(_iter_rules(CSS))


def _tokens(selector: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for rule in RULES:
        if selector in rule.selectors:
            merged.update(rule.decls)
    assert merged, f"style.css 里找不到 {selector} 的 token 块"
    return merged


LIGHT = _tokens(":root")
DARK = _tokens('[data-theme="dark"]')

REQUIRED_TOKENS = (
    "--bg-body", "--bg-card", "--bg-accent",
    "--status-ok", "--status-warn", "--status-error", "--status-done", "--status-info",
    "--tint-ok", "--tint-warn", "--tint-error", "--tint-info", "--tint-highlight",
    "--on-solid-ok", "--on-highlight", "--highlight", "--highlight-strong",
    "--surface-raised",
)


class RGBA(NamedTuple):
    r: int
    g: int
    b: int
    a: float


_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FUNC = re.compile(r"^rgba?\(([^)]*)\)$")
_VAR = re.compile(r"^var\((--[\w-]+)\)$")


def _rgb_value(value: str, tokens: dict[str, str], depth: int = 0) -> RGBA:
    if depth > 4:
        raise AssertionError(f"token 解析层级过深：{value!r}")
    v = value.strip()
    if v == "transparent":
        return RGBA(0, 0, 0, 0.0)
    var = _VAR.match(v)
    if var:
        name = var.group(1)
        assert name in tokens, f"{name} 在 token 块里没有定义"
        return _rgb_value(tokens[name], tokens, depth + 1)
    hex_match = _HEX.match(v)
    if hex_match:
        h = hex_match.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return RGBA(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    func = _FUNC.match(v)
    if func:
        parts = [p.strip() for p in func.group(1).split(",")]
        assert len(parts) in (3, 4), f"颜色函数参数个数异常：{value!r}"
        r, g, b = (int(round(float(p))) for p in parts[:3])
        a = float(parts[3]) if len(parts) == 4 else 1.0
        return RGBA(r, g, b, a)
    raise AssertionError(f"无法解析的颜色值：{value!r}")


def _over(top: RGBA, bottom: tuple[int, int, int]) -> tuple[int, int, int]:
    return (
        int(round(top.r * top.a + bottom[0] * (1 - top.a))),
        int(round(top.g * top.a + bottom[1] * (1 - top.a))),
        int(round(top.b * top.a + bottom[2] * (1 - top.a))),
    )


def _flatten(layers: list[RGBA]) -> tuple[int, int, int]:
    base = layers[0]
    assert base.a == 1.0, "最底层必须不透明（--bg-card / --bg-accent / --bg-body）"
    current = (base.r, base.g, base.b)
    for layer in layers[1:]:
        current = _over(layer, current)
    return current


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


#: 元素实际坐落的"面"。层序＝从最底层到最上层，alpha 逐层合成。
#: 名称对应 DOM：卡片 / 卡头次级面 / 底布；`*_on_card` 表示父容器自己先铺了
#: `--surface-raised`（`.agent-item`、`.metric-card`、`.stat-item` 等）。
SURFACES = {
    "card": ("--bg-card",),
    "accent": ("--bg-accent",),
    "body": ("--bg-body",),
    "raised_on_card": ("--bg-card", "--surface-raised"),
    "raised_on_accent": ("--bg-accent", "--surface-raised"),
    "approval_bar": ("--bg-card", "--tint-warn"),
}


class Pair(NamedTuple):
    selector: str              # style.css 里的选择器
    surface: str               # SURFACES 的键；规则自己的 background 会叠在它上面
    px: float                  # 字号（px）
    weight: int                # 字重
    size_source: str | None    # 规则自己没声明字号时，字号取自哪个规则
    note: str


def _font_px(value: str) -> float:
    v = value.strip().lower()
    if v.endswith("rem"):
        return float(v[:-3]) * FONT_BASE_PX
    if v.endswith("px"):
        return float(v[:-2])
    raise AssertionError(f"只支持 rem/px 字号：{value!r}")


def _font_weight(value: str) -> int:
    v = value.strip().lower()
    return {"normal": 400, "bold": 700}.get(v, int(v))


def _threshold(px: float, weight: int) -> float:
    large = px >= LARGE_WHEN_ANY_PX or (px >= LARGE_PX and weight >= 700)
    return 3.0 if large else 4.5


def _prop(selector: str, *names: str) -> str | None:
    found = None
    for rule in RULES:
        if selector in rule.selectors:
            for name in names:
                if name in rule.decls:
                    found = rule.decls[name]
    return found


PAIRS = [
    # ---- 状态徽标 / 状态牌（自己的 tint 叠在所在面上） ----
    Pair(".badge-status.running", "card", 9.1, 600, ".badge-status", "Tasks 状态徽标"),
    Pair(".badge-status.pending", "card", 9.1, 600, ".badge-status", "Tasks 状态徽标"),
    Pair(".badge-status.done", "card", 9.1, 600, ".badge-status", "Tasks 状态徽标"),
    Pair(".badge-status.failed", "card", 9.1, 600, ".badge-status", "Tasks 状态徽标"),
    Pair(".status-pill.ok", "card", 11.2, 600, ".status-pill", "Pipeline Status 徽标"),
    Pair(".status-pill.error", "card", 11.2, 600, ".status-pill", "Pipeline Status 徽标"),
    Pair(".agent-status-tag.ok", "raised_on_card", 9.1, 600, ".agent-status-tag", "Agent 状态牌"),
    Pair(".agent-status-tag.warn", "raised_on_card", 9.1, 600, ".agent-status-tag", "Agent 状态牌"),
    Pair(".agent-status-tag.error", "raised_on_card", 9.1, 600, ".agent-status-tag", "Agent 状态牌"),
    # ---- 大号数字（3.0 阈）与指标值（16.8px 未达大字阈 ⇒ 4.5） ----
    Pair(".stat-item .stat-value.ok", "raised_on_card", 21.0, 700, ".stat-item .stat-value", "状态大数字"),
    Pair(".stat-item .stat-value.warn", "raised_on_card", 21.0, 700, ".stat-item .stat-value", "状态大数字"),
    Pair(".stat-item .stat-value.error", "raised_on_card", 21.0, 700, ".stat-item .stat-value", "状态大数字"),
    # ---- 日志级别 / 提交反馈 / 实时进度 ----
    Pair(".logs-table .log-level.info", "card", 9.1, 700, ".logs-table .log-level", "日志级别"),
    Pair(".logs-table .log-level.warning", "card", 9.1, 700, ".logs-table .log-level", "日志级别"),
    Pair(".logs-table .log-level.error", "card", 9.1, 700, ".logs-table .log-level", "日志级别"),
    Pair(".newtask-msg", "card", 10.5, 400, None, "提交成功反馈"),
    Pair(".newtask-msg.is-error", "card", 10.5, 400, ".newtask-msg", "提交失败反馈"),
    Pair(".live-label", "raised_on_card", 9.8, 400, None, "实时进度标题"),
    # ---- 任务 / 运行详情 ----
    Pair(".task-id-link", "card", 11.2, 400, ".tasks-table", "任务 ID 链接"),
    Pair(".row-clickable:hover .task-id-link", "card", 11.2, 400, ".tasks-table", "任务 ID 链接 hover"),
    Pair(".run-error", "card", 11.2, 400, None, "运行错误"),
    Pair(".dag-node-err", "accent", 10.08, 400, None, "DAG 节点错误"),
    # ---- 节点检查器 ----
    Pair(".insp-ok", "accent", 10.64, 400, ".insp-row", "检查器 ok 值"),
    Pair(".insp-warn", "accent", 10.64, 400, ".insp-row", "检查器 warn 值"),
    Pair(".insp-error", "accent", 10.36, 400, None, "检查器错误"),
    Pair(".insp-issue", "accent", 10.36, 400, None, "检查器问题"),
    Pair(".insp-key", "accent", 10.08, 400, None, "幂等键"),
    Pair(".insp-art-type", "accent", 9.24, 400, None, "产物类型"),
    Pair(".deliv-sha", "card", 11.2, 400, ".tasks-table", "交付账本 sha"),
    # ---- 审批 / 新建任务 ----
    Pair(".decision-approved", "card", 9.52, 400, ".decision", "审批决定"),
    Pair(".decision-rejected", "card", 9.52, 400, ".decision", "审批决定"),
    Pair(".decision-pending", "card", 9.52, 400, ".decision", "审批决定"),
    Pair(".btn-approve", "approval_bar", 11.2, 600, ".approval-actions button", "实心放行按钮"),
    Pair(".btn-reject", "approval_bar", 11.2, 600, ".approval-actions button", "驳回按钮"),
    Pair("#nt-submit", "card", 11.9, 600, None, "提交任务主按钮"),
    # ---- DAG 角标 ----
    Pair(".dag-retried-flag", "accent", 8.68, 400, None, "重试旗"),
    Pair(".dag-checkpoint-badge", "accent", 9.8, 400, None, "断点角标"),
    Pair(".dag-checkpoint-resumed", "accent", 9.8, 400, ".dag-checkpoint-badge", "断点续跑角标"),
    # ---- 页头 / 离线覆盖层 ----
    Pair(".header h1 .accent", "card", 18.2, 600, ".header h1", "页头强调词"),
    Pair(".theme-toggle:hover", "card", 10.5, 400, ".theme-toggle", "主题切换钮 hover"),
    Pair(".error-overlay h2", "body", 18.2, 400, None, "离线覆盖层标题"),
    Pair(".error-overlay .retry-btn", "body", 11.9, 600, None, "离线覆盖层重试"),
]

#: 只承担图形语义（WCAG 1.4.11 的 3:1 另立），不是文本判据。
NON_TEXT_COLOR_RULES = {
    ".err-icon .ico": "SVG 图标，图形对比度另立（L2-01 非文本项）",
}

#: app.js 用内联样式给指标值上色（CSS-only 普查会漏掉这一族）。
INLINE_PAIRS = [
    Pair(".metric-value", "raised_on_card", 16.8, 700, None, "指标值 ok"),
    Pair(".metric-value", "raised_on_card", 16.8, 700, None, "指标值 warn"),
    Pair(".metric-value", "raised_on_card", 16.8, 700, None, "指标值 error"),
]
INLINE_STATUS_TOKENS = ("--status-ok", "--status-warn", "--status-error")


def _evaluate(selector: str, surface: str, tokens: dict[str, str],
              color_value: str | None = None) -> tuple[float, tuple, tuple]:
    fg_value = color_value if color_value is not None else _prop(selector, "color")
    assert fg_value, f"{selector} 没有 color 声明"
    fg = _rgb_value(fg_value, tokens)
    layers = [_rgb_value(tokens[name], tokens) for name in SURFACES[surface]]
    own = _prop(selector, "background-color", "background")
    if own and not own.startswith("linear-gradient"):
        layers.append(_rgb_value(own, tokens))
    bg = _flatten(layers)
    fg_rgb = (fg.r, fg.g, fg.b) if fg.a == 1.0 else _over(fg, bg)
    return _ratio(fg_rgb, bg), fg_rgb, bg


def _resolved_font(pair: Pair) -> tuple[float, int]:
    own_px = _prop(pair.selector, "font-size")
    own_weight = _prop(pair.selector, "font-weight")
    if own_px is not None:
        assert abs(_font_px(own_px) - pair.px) < 0.01, (
            f"{pair.selector} 实际字号 {_font_px(own_px):.2f}px 与表里的 {pair.px}px 不一致——"
            "字号变了阈值就可能变，先复核口径再改表"
        )
        weight = _font_weight(own_weight) if own_weight else pair.weight
        assert weight == pair.weight, f"{pair.selector} 字重 {weight} 与表里的 {pair.weight} 不一致"
        return pair.px, pair.weight
    assert pair.size_source, (
        f"{pair.selector} 自己没声明 font-size，表里也没给 size_source——"
        "字号来源不明时阈值就是猜的"
    )
    source_px = _prop(pair.size_source, "font-size")
    assert source_px is not None, f"size_source={pair.size_source} 没有 font-size 声明"
    source_weight = _prop(pair.size_source, "font-weight")
    weight = _font_weight(source_weight) if source_weight else pair.weight
    assert abs(_font_px(source_px) - pair.px) < 0.01, (
        f"{pair.selector} 的字号来自 {pair.size_source}，实际 {_font_px(source_px):.2f}px，"
        f"与表里的 {pair.px}px 不一致"
    )
    assert weight == pair.weight, (
        f"{pair.selector} 的字重来自 {pair.size_source}，实际 {weight}，与表里的 {pair.weight} 不一致"
    )
    return pair.px, pair.weight


def _semantic_color_rules() -> list[tuple[str, str]]:
    """枚举所有把 color 设成语义色 token 的 (选择器, 值)。"""
    found: list[tuple[str, str]] = []
    for rule in RULES:
        value = rule.decls.get("color")
        if not value:
            continue
        if not any(token in value for token in ("--status-", "--highlight", "--on-")):
            continue
        for selector in rule.selectors:
            found.append((selector, value))
    return found


class TestTokenBlocks:
    def test_both_themes_define_every_required_token(self):
        for selector, tokens in ((":root", LIGHT), ('[data-theme="dark"]', DARK)):
            missing = [name for name in REQUIRED_TOKENS if name not in tokens]
            assert not missing, f"{selector} 缺少 token：{missing}"

    def test_tints_are_same_source_as_status_colors(self):
        """`--tint-X` 的 RGB 必须与 `--status-X` 同源——F9 那类"前景新 token、底色旧色号"
        的漂移会让对比度变成偶然值，且底色不随主题切换。"""
        pairs = [("--tint-ok", "--status-ok"), ("--tint-warn", "--status-warn"),
                 ("--tint-error", "--status-error"), ("--tint-info", "--status-info"),
                 ("--tint-highlight", "--highlight")]
        for theme, tokens in (("light", LIGHT), ("dark", DARK)):
            for tint, status in pairs:
                tint_rgb = _rgb_value(tokens[tint], tokens)
                status_rgb = _rgb_value(tokens[status], tokens)
                assert (tint_rgb.r, tint_rgb.g, tint_rgb.b) == (
                    status_rgb.r, status_rgb.g, status_rgb.b), (
                    f"{theme}：{tint} 与 {status} 不同源（"
                    f"{tint_rgb[:3]} vs {status_rgb[:3]}）——底色不随主题/语义变化"
                )

    def test_no_hardcoded_color_outside_token_blocks(self):
        """两个主题 token 块之外不许出现 hex/rgb/rgba 字面量（L2-02）。"""
        token_spans = [
            rule.span for rule in RULES
            if ":root" in rule.selectors or '[data-theme="dark"]' in rule.selectors
        ]
        assert len(token_spans) == 2, f"期望恰好两个主题 token 块，实际 {len(token_spans)}"
        literal = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")
        offenders: list[str] = []
        for match in literal.finditer(CSS):
            if any(start <= match.start() < end for start, end in token_spans):
                continue
            line = CSS.count("\n", 0, match.start()) + 1
            offenders.append(f"style.css:{line} {match.group(0)}")
        assert not offenders, (
            "token 块外出现硬编码颜色（不随主题切换、且与 tint 不同源）：" + "; ".join(offenders)
        )

    def test_dead_waiting_badge_rule_is_gone(self):
        """`statusClass()` 把 waiting 并进 pending ⇒ `.badge-status.waiting` 永远命中不到。"""
        assert ".badge-status.waiting" not in CSS, "死规则又回来了：没有任何代码路径产出 waiting 类"
        waiting_branch = re.search(r"case 'waiting':", APP_JS)
        assert waiting_branch, "statusClass 的 waiting→pending 映射被删了？那这条判据的前提就变了"
        assert re.search(r"case 'waiting':\s*return 'pending'", APP_JS), (
            "waiting 不再映射到 pending——若它真的成了独立状态，得先把样式补回来"
        )


class TestContrast:
    def _check(self, theme: str, tokens: dict[str, str]) -> list[str]:
        failures: list[str] = []
        for pair in PAIRS:
            px, weight = _resolved_font(pair)
            ratio, fg, bg = _evaluate(pair.selector, pair.surface, tokens)
            need = _threshold(px, weight)
            if ratio < need:
                failures.append(
                    f"{pair.selector}（{pair.note}）{ratio:.2f} < {need}:1 "
                    f"fg={fg} bg={bg} [{px:.1f}px/{weight}]"
                )
        return failures

    def test_light_theme_text_meets_wcag_aa(self):
        failures = self._check("light", LIGHT)
        assert not failures, "浅色（默认主题）文本对比度不达标：\n" + "\n".join(failures)

    def test_dark_theme_text_meets_wcag_aa(self):
        failures = self._check("dark", DARK)
        assert not failures, "深色主题文本对比度不达标：\n" + "\n".join(failures)

    def test_inline_metric_colors_meet_wcag_aa(self):
        """指标值的颜色写在 app.js 的内联样式里——CSS-only 普查看不见这一族。"""
        sites = re.findall(r"\.style\.color = 'var\(--status-' \+", APP_JS)
        assert len(sites) == 3, (
            f"app.js 里内联上色的位置从 3 处变成了 {len(sites)} 处——新增的位置没进本判据"
        )
        classes: set[str] = set()
        for expr in re.findall(r"cls:\s*([^,\n]+)", APP_JS) + \
                re.findall(r"remainingCls = ([^;\n]+)", APP_JS):
            classes.update(re.findall(r"'([a-z]+)'", expr))
        assert classes == {"ok", "warn", "error"}, (
            f"内联上色实际用到的状态类变成了 {sorted(classes)}——本判据只算了 ok/warn/error"
        )
        for theme, tokens in (("light", LIGHT), ("dark", DARK)):
            for pair, token in zip(INLINE_PAIRS, INLINE_STATUS_TOKENS, strict=True):
                ratio, fg, bg = _evaluate(pair.selector, pair.surface, tokens,
                                          color_value=f"var({token})")
                need = _threshold(pair.px, pair.weight)
                assert ratio >= need, (
                    f"{theme}：{pair.note} {ratio:.2f} < {need}:1（fg={fg} bg={bg}）"
                )


class TestCoverage:
    def test_every_semantic_color_rule_is_judged(self):
        """新增一条语义色规则却没人判它 ⇒ 红。这是门的自我保护：不许静默跳过。"""
        covered = {pair.selector for pair in PAIRS} | set(NON_TEXT_COLOR_RULES)
        uncovered = [
            f"{selector} {{ color: {value} }}"
            for selector, value in _semantic_color_rules()
            if selector not in covered
        ]
        assert not uncovered, (
            "这些规则用了语义色但没有对比度判据（加进 PAIRS，或登记为非文本）：\n"
            + "\n".join(uncovered)
        )

    def test_every_pair_selector_exists(self):
        """反死判据：表里点了名却在 CSS 里不存在的选择器，说明表本身过期了。"""
        all_selectors = {selector for rule in RULES for selector in rule.selectors}
        missing = [pair.selector for pair in PAIRS if pair.selector not in all_selectors]
        assert not missing, f"表里的选择器在 style.css 里不存在：{missing}"

    def test_pairs_are_unique(self):
        seen = [pair.selector for pair in PAIRS]
        duplicates = {name for name in seen if seen.count(name) > 1}
        assert not duplicates, f"同一个选择器登记了两次：{duplicates}"
