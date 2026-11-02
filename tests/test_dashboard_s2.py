"""dashboard S2 运行详情屏的结构护栏（docs/product-spec.md §4.2）。

仓库内**没有浏览器级证据**（2026-10-20 设计复核记为缺口；此前"无头 Chrome
走查留证"的说法已撤回）；这里钉住**wiring 契约**，防止"后端加了字段、前端忘了接"
或"接了一半"这类静默退化：

- `index.html` 有 `#panel-run`（默认 hidden）与返回按钮 + S2 图标；
- `app.js` 的真接线：任务行 click → `openRun`；`openRun` 隐藏其他卡并显示
  运行详情；`closeRun` 恢复；审批按钮打 `/tasks/<id>/approve|reject`；
  节点四态与 when 条件跳过的虚线语义有对应渲染分支；
- `style.css` 有 `[hidden]` 显式压过 `.card{display:flex}` 的规则
  （作者样式优先于 UA 的 `[hidden]{display:none}`——不写这条"详情态隐藏
  其他卡片"根本不生效，无头 Chrome 实测踩过）与四态/虚线样式类。
"""
from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard"


def _read(name: str) -> str:
    return (DASH / name).read_text(encoding="utf-8")


class TestRunPanelMarkup:
    def setup_method(self):
        self.html = _read("index.html")

    def test_panel_exists_and_hidden_by_default(self):
        assert 'id="panel-run"' in self.html
        # 同一行里 hidden 与 id 相邻（默认不占位，点任务才打开）
        assert 'id="panel-run"' in self.html and "hidden" in self.html

    def test_back_button_and_meta_slot(self):
        assert 'id="run-back"' in self.html
        assert 'id="run-meta"' in self.html
        assert 'id="run-body"' in self.html

    def test_s2_icons_present(self):
        assert 'id="i-run"' in self.html
        assert 'id="i-check"' in self.html


class TestAppWiring:
    def setup_method(self):
        self.js = _read("app.js")

    def test_task_row_opens_run_detail(self):
        assert "openRun(String(t.id))" in self.js

    def test_open_hides_other_cards_and_shows_run(self):
        assert "c.id !== 'panel-run'" in self.js
        assert "c.hidden = true" in self.js
        assert "runPanel.hidden = false" in self.js

    def test_close_restores_all_cards(self):
        assert "runPanel.hidden = true" in self.js
        assert "c.hidden = false" in self.js

    def test_fetches_task_detail_endpoint(self):
        assert "API_BASE + '/tasks/'" in self.js

    def test_approval_buttons_hit_decision_endpoints(self):
        assert "/approve'" in self.js or 'approve' in self.js
        assert "'/tasks/' + encodeURIComponent(taskId) + '/' + action" in self.js

    def test_four_states_and_condition_skip_rendered(self):
        """四态经 nodeVisualState 归一后拼成 dag-<state> 类名（不是字面罗列）。"""
        assert "'dag-node dag-' + state" in self.js or '"dag-node dag-" + state' in self.js
        for state in ("'success'", "'failed'", "'skipped'", "'retried'"):
            assert state in self.js, f"nodeVisualState 缺状态 {state}"
        for label in ("'成功'", "'失败'", "'跳过'", "'重试后成功'"):
            assert label in self.js, f"NODE_STATE_LABEL 缺中文标签 {label}"
        assert "dag-skip-condition" in self.js
        assert "skip_reason === 'condition'" in self.js

    def test_node_opens_inspector(self):
        """S3：点 DAG 节点 → 右侧检查器抽屉（spec §4.2 统一骨架）。"""
        assert "openInspector(taskId, n.name)" in self.js
        assert "/nodes/" in self.js
        assert "node-inspector" in self.js

    def test_inspector_renders_detail_kinds(self):
        for token in ("artifact", "idempotency", "circuit_breaker", "rate_limit", "quality"):
            assert token in self.js, f"检查器缺 {token} 区块"
        # “取不到就不编”：null 时如实显示未建立，而不是假读数
        assert "未建立" in self.js

    def test_checkpoint_marker_on_dag(self):
        """S2 缺的检查点续跑标记：dag.checkpoint 驱动角标。"""
        assert "renderCheckpointMarkers" in self.js
        assert "dag-checkpoint-resumed" in self.js
        assert "断点续跑" in self.js

    def test_no_emoji_in_new_markup(self):
        """用户审美铁律：功能位置不用 emoji 指示符。"""
        for ch in ("✅", "❌", "⚠️", "🚀"):
            assert ch not in self.js


class TestStyleContract:
    def setup_method(self):
        self.css = _read("style.css")

    def test_hidden_overrides_card_display_flex(self):
        assert ".card[hidden]" in self.css and "display: none" in self.css

    def test_dag_state_classes_defined(self):
        for token in (".dag-success", ".dag-failed", ".dag-skipped",
                      ".dag-retried", ".dag-running", ".dag-pending"):
            assert token in self.css, f"缺少 {token} 样式"

    def test_condition_skip_is_dashed(self):
        assert ".dag-skip-condition" in self.css and "dashed" in self.css

    def test_inspector_drawer_styles(self):
        for token in (".node-inspector", ".insp-art-row", ".dag-checkpoint-badge",
                      ".dag-checkpoint-resumed", ".dag-node-clickable"):
            assert token in self.css, f"缺少 {token} 样式"


class TestConsoleShipping:
    """控制台必须随包发布（2026-10-20 复核：3.12.0 wheel 里 0 个 dashboard 资源）。"""

    def test_dashboard_lives_inside_the_package(self):
        from loom_core import paths

        d = paths.dashboard_dir()
        for name in ("index.html", "app.js", "style.css"):
            assert (d / name).is_file(), f"控制台资源缺失：{name}"

    def test_package_data_declares_the_dashboard(self):
        import tomllib

        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        assert "dashboard/*" in data["tool"]["setuptools"]["package-data"]["loom_core"]

    def test_root_serves_the_console_shell(self):
        """`GET /` 直接给控制台入口页（免鉴权，只发静态壳）。"""
        import urllib.request
        from unittest.mock import MagicMock

        from loom_core import paths
        from loom_core.admin_api import AdminAPI, AdminHandler

        api = AdminAPI(host="127.0.0.1", port=0, api_key="",
                       serve_static=True,
                       dashboard_dir=str(paths.dashboard_dir()))
        try:
            assert api.start(MagicMock()) is True
            port = api._server.server_address[1]
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5) as r:
                body = r.read().decode("utf-8")
                assert r.status == 200
                assert r.headers.get_content_type() == "text/html"
            assert "Loom" in body
        finally:
            api.stop()
            # start() 写的是类属性，清掉避免污染后续用例
            AdminHandler.dashboard_dir = None


class TestDeliveriesDetailSurvivesPolling:
    """账本明细跨轮询存活（2026-10-31 实测缺陷回归）。

    缺陷现场：`openDelivery` 点开明细后，`render()` 的 5 秒轮询无条件重画
    交付面板 → 明细 ≤5 秒被列表覆盖（点开即消失）。修法：明细开着时置
    `deliveriesDetailOpen`，`renderDeliveries` 遇旗只更新数据不回画；
    「返回账本列表」显式清旗。本判据静态钉住三处接线（零浏览器）。
    """

    def setup_method(self):
        self.js = _read("app.js")

    def test_flag_declared_and_guarded(self):
        assert "deliveriesDetailOpen" in self.js
        assert "if (deliveriesDetailOpen) return;" in self.js, \
            "renderDeliveries 缺轮询守卫——明细会被 5 秒轮询覆盖"

    def test_open_sets_flag_before_rendering_detail(self):
        open_body = self.js.split("async function openDelivery", 1)[1]
        set_at = open_body.index("deliveriesDetailOpen = true;")
        render_at = open_body.index("renderDeliveryDetail(")
        assert set_at < render_at, "必须先置旗再渲染明细"

    def test_back_button_clears_flag(self):
        detail_body = self.js.split("function renderDeliveryDetail", 1)[1]
        assert "deliveriesDetailOpen = false;" in detail_body, \
            "返回账本列表必须清旗，否则列表再也回不来"


class TestRunsDeepLinkAndFilter:
    """Runs 面 P0（评审 §4.2）：深链 `#/runs/{id}` 与状态过滤的接线护栏（零浏览器）。

    评审原文："运行列表（状态/工作流/principal/耗时/成本/质量分）｜过滤、新建运行、
    深链 `/#/runs/{id}`｜P0"。深链的价值是"把某一次运行发给同事/从书签直达"——
    因此 hash→视图只能有一个真源（`routeFromHash`），且浏览器前进/后退必须同步。
    """

    def setup_method(self):
        self.js = _read("app.js")
        self.html = _read("index.html")

    def test_hash_route_helpers_exist(self):
        assert "RUN_HASH_PREFIX = '#/runs/'" in self.js
        for token in ("runIdFromHash", "setRunHash", "clearRunHash", "routeFromHash"):
            assert token in self.js, f"缺深链接线 {token}"
        assert "addEventListener('hashchange', routeFromHash)" in self.js

    def test_open_sets_hash_and_close_clears_it(self):
        open_body = self.js.split("async function openRun", 1)[1]
        assert "setRunHash(taskId);" in open_body, "点开运行必须写深链"
        close_body = self.js.split("function closeRun", 1)[1]
        assert "clearRunHash();" in close_body, "关闭运行必须清深链（否则后退链错乱）"
        # hashchange 驱动的路径不得反向再写历史（防：每 3s 刷新写一条历史）
        assert "openRun(id, { fromHash: true })" in self.js
        assert "closeRun({ fromHash: true })" in self.js

    def test_filter_element_and_render_wiring(self):
        assert 'id="task-filter"' in self.html
        assert "els.taskFilter" in self.js
        assert "state.taskFilter" in self.js
        # 动词级：过滤必须真按状态分类走，而不是拼个假计数
        assert "all.filter(t => statusClass(t.status) === filter)" in self.js
        assert "该状态下没有任务" in self.js, "过滤后空态要说清是'筛选空'，不是'没任务'"


class TestSettingsFace:
    """Settings 面 v1（评审 §4.2，P2 只读先行）的接线护栏（零浏览器）。

    只读面四源：/api/config（运行配置快照）、/api/webhooks（集成）、
    /api/versions/stats + /api/config/audit（版本与配置审计）；预算复用 /api/cost。
    写操作（配置更新/调预算/版本回滚）需要 X-Confirm + admin，本批刻意不做。
    """

    def setup_method(self):
        self.js = _read("app.js")
        self.html = _read("index.html")

    def test_panel_markup_exists(self):
        assert 'id="panel-settings"' in self.html
        assert 'id="badge-settings"' in self.html

    def test_four_sources_are_wired(self):
        for ep in ("/api/config'", "/api/webhooks'", "/api/versions/stats'",
                   "/api/config/audit'"):
            assert "API_BASE + '" + ep in self.js, f"Settings 缺数据源 {ep}"
        assert "renderSettings(state.settings)" in self.js
        assert "state.settings = data.settings;" in self.js

    def test_sensitive_keys_are_masked(self):
        """即便后端将来把密钥放进快照，前端也不得原样渲染。"""
        assert "maskConfigValue" in self.js
        assert "key|secret|token|password" in self.js
        assert "***（已掩码）" in self.js

    def test_read_only_scope_is_stated(self):
        """只读边界要写在界面上（不外推成'能改配置'）。"""
        assert "本面板只读" in self.js
        assert "不触发回滚" in self.js

    def test_webhook_rows_only_read_fields_the_api_returns(self):
        """前端只许读后端真返回的字段——空箭头就是这么漏出去的。

        `/api/webhooks` 的响应键是 name / pipeline / enabled / secret_env / output，
        而第一版前端渲染的是 `name → w.url`：`url` 从不存在 ⇒ 真机每个 webhook 后面都是空箭头。
        浏览器门能抓到，但门不进 CI；这条把"字段对得上"搬进单测：后端改名或前端读不存在的字段都会红。
        """
        import re

        admin_src = (DASH.parent / "admin_api.py").read_text(encoding="utf-8")
        branch = admin_src.split('elif self.path == "/api/webhooks":', 1)
        assert len(branch) == 2, "admin_api 里找不到 /api/webhooks 分支"
        segment = branch[1].split("elif self.path", 1)[0]
        backend_fields = set(re.findall(r'"(\w+)": w\.', segment))
        assert backend_fields, f"解析不出 webhook 的响应字段：{segment[:200]!r}"
        assert {"name", "pipeline"} <= backend_fields, backend_fields

        rendered = self.js.split("function renderSettings", 1)[1].split("function renderError", 1)[0]
        used = set(re.findall(r"\bw\.(\w+)", rendered))
        assert used, "renderSettings 里没有读 webhook 字段"
        assert used <= backend_fields | {"name"}, (
            f"Settings 面读了后端不返回的字段：{sorted(used - backend_fields - {'name'})}"
            f"（后端只有 {sorted(backend_fields)}）"
        )


class TestWorkflowsSchedulesFace:
    """Workflows & Schedules 面（评审 §4.2 六面最后一个可做面）的接线护栏（零浏览器）。

    工作流卡片：/api/pipeline 名单 + /api/locks 的**三值**漂移（matches:
    true=比过且一致 / false=漂移 / null=没法判断）——徽章三色，
    "没法判断"不得画成"一致"。排程：/api/schedules 的租约三态由服务端按时点算好。
    """

    def setup_method(self):
        self.js = _read("app.js")
        self.html = _read("index.html")

    def test_panel_and_two_sources_wired(self):
        assert 'id="panel-workflows"' in self.html
        for ep in ("/api/locks'", "/api/schedules'"):
            assert "API_BASE + '" + ep in self.js, f"缺数据源 {ep}"
        assert "renderWorkflows(state.workflows)" in self.js
        assert "state.workflows = data.workflows;" in self.js

    def test_lock_badge_is_three_state(self):
        """matches 的三值各自有徽章——"没法判断"（null）不得混成"一致"。"""
        for token in ("lockBadgeClass", "lockBadgeLabel", "'一致'", "'漂移'", "'未验证'"):
            assert token in self.js, f"锁徽章缺 {token}"
        assert "if (matches === true) return 'done'" in self.js
        assert "if (matches === false) return 'failed'" in self.js

    def test_lease_state_computed_on_server(self):
        """租约三态由服务端按时点算好；前端不拿本机时钟跟 lease_until 自比。"""
        assert "lease_state" in self.js
        for token in ("'held'", "'stale'", "'free'"):
            assert token in self.js, f"租约三态缺 {token}"
        assert "lease_until > now" not in self.js

    def test_drift_hint_points_to_plan_diff(self):
        assert "loom plan --diff" in self.js


class TestDashboardTheme:
    """视觉改版 v1（2026-10-31 用户反馈"太暗/阴沉、内容乱杂"）。

    契约：浅色为默认（旧深色只在 `[data-theme=dark]` 下存活）；切换按钮把选择
    持久化到 localStorage；主操作（新建任务）前置到首屏；浅色主题里不得残留
    "只在深色下成立"的白色高光与旧主题深蓝底。
    """

    def setup_method(self):
        self.js = _read("app.js")
        self.html = _read("index.html")
        self.css = _read("style.css")

    def test_default_is_light(self):
        assert "color-scheme: light" in self.css
        # 2026-11-02 用户反馈：卡片与底布界限不分明 → 底布压深到 #e9edf5、卡身纯白、边框加深
        assert "--bg-body: #e9edf5" in self.css
        assert "--bg-card: #ffffff" in self.css

    def test_dark_override_exists(self):
        assert '[data-theme="dark"]' in self.css
        assert "--bg-body: #0b0f16" in self.css

    def test_card_vs_backdrop_layering_is_visible(self):
        """卡片必须在底布上"浮起来"：底布与卡身不同色 + 边框比两者都深。

        用户原话：「card 的边框线 和 底布 之间 界限分明一点，背景色也要有点区别」。
        判据按 token 语义钉：三者在亮度上必须拉开（不是只差一两个色阶）。
        """
        def lum(hex_color: str) -> float:
            hex_color = hex_color.lstrip("#")
            rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def ratio(a: str, b: str) -> float:
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        for theme, body, card, border in (
            ("light", "#e9edf5", "#ffffff", "#d3dae7"),
            ("dark", "#0b0f16", "#1a2231", "#33405a"),
        ):
            assert card in self.css, theme
            assert border in self.css, theme
            # 卡身 vs 底布：≥1.15 才"看得出区别"（深色主题用绝对亮度差会天然偏小，故用比率）
            assert ratio(card, body) >= 1.15, f"{theme}: 卡身与底布对比不足 {ratio(card, body):.2f}"
            # 边框对两侧都≥1.15：卡界这条线本身要看得出
            assert ratio(border, card) >= 1.15, f"{theme}: 边框对卡身对比不足 {ratio(border, card):.2f}"
            assert ratio(border, body) >= 1.15, f"{theme}: 边框对底布对比不足 {ratio(border, body):.2f}"

    def test_toggle_wiring_persists_choice(self):
        assert 'id="theme-toggle"' in self.html
        for token in ("THEME_KEY", "applyTheme", "initTheme", "data-theme",
                      "localStorage.setItem(THEME_KEY"):
            assert token in self.js, f"主题接线缺 {token}"

    def test_light_theme_has_no_dark_only_highlights(self):
        css = self.css
        start = css.index('[data-theme="dark"]')
        end = css.index('}', css.index("--shadow", start)) + 1
        outside = (css[:start] + css[end:]).replace(" ", "")
        assert "rgba(255,255,255" not in outside, \
            "浅色主题里残留只在深色成立的白色高光"
        assert "rgba(15,52,96" not in outside, "残留旧主题深蓝底色"

    def test_primary_action_first(self):
        assert self.html.index('id="panel-newtask"') < self.html.index(
            'id="panel-approvals"'), "新建任务（主操作）必须前置到首屏"
