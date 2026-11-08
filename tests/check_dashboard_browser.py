"""Run dashboard browser regressions with Playwright and an installed browser."""
from __future__ import annotations

import argparse
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: `/tasks/<id>/nodes/<node>` 在内存里查不到那次运行（被淘汰 / 别的进程跑的）时后端的
#: 投影响应。字段与 `admin_api.AdminHandler._node_inspector_from_projection` 逐字段一致，
#: `tests/test_browser_gate.py` 会把它与后端真产出对账——mock 漂移了这门就在验一个
#: 不存在的契约，而那种漂移在手动门里永远看不出来。
PROJECTION_NODE_PAYLOAD = {
    "task_id": "sample",
    "task_status": "paused",
    "source": "runstore",
    "node": {
        "name": "alpha",
        "agent": "alpha",
        "status": "done",
        "attempts": 1,
        "skip_reason": "",
        "error": "",
        "dependencies": [],
        "max_retries": None,
        "duration_ms": None,
        "artifact_keys": ["doc"],
    },
    "artifact": None,
    "idempotency": None,
    "circuit_breaker": None,
    "rate_limit": None,
    "quality": None,
    "checkpoint": None,
}

#: 同一节点走**内存态**时的响应（没有 `source`）：四类明细齐全，用来做反向对照——
#: 换了文案的那几行在这里必须还是原文案，否则"按 source 换文案"只是把两套字都印出来。
IN_MEMORY_NODE_PAYLOAD = {
    "task_id": "sample",
    "task_status": "paused",
    "node": {
        "name": "beta",
        "agent": "writer",
        "status": "done",
        "attempts": 1,
        "skip_reason": "",
        "error": "",
        "dependencies": ["alpha"],
        "max_retries": 2,
    },
    "artifact": {
        "keys": ["doc"],
        "size": 5,
        "preview": {"doc": {"type": "str", "value": "hello", "length": 5}},
        "truncated": False,
    },
    "idempotency": {"key": "beta:doc", "recorded": False},
    "circuit_breaker": {"state": "CLOSED", "failure_count": 0},
    "rate_limit": {"rate": 2, "burst": 4, "available_tokens": 3,
                   "total_acquired": 1, "total_blocked": 0},
    "quality": None,
    "checkpoint": None,
}



#: 真浏览器侧的配色复核（L2-01/L2-02）。静态门禁 `test_dashboard_contrast.py`
#: 解析 CSS 后按模型实算；这里按 `getComputedStyle` 量同一批配对——静态模型与真级联
#: 一旦分叉（被更高优先级规则覆盖、内联样式、继承字号变化），只有这一面看得见。
CONTRAST_PROBE_JS = r"""
() => {
  const parse = (value) => {
    const m = value.match(/^rgba?\(([^)]+)\)$/);
    if (!m) return null;
    const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const over = (top, base) => ({
    r: Math.round(top.r * top.a + base.r * (1 - top.a)),
    g: Math.round(top.g * top.a + base.g * (1 - top.a)),
    b: Math.round(top.b * top.a + base.b * (1 - top.a)),
  });
  const lum = (c) => {
    const f = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };
  const backgroundOf = (el) => {
    const chain = [];
    for (let node = el; node; node = node.parentElement) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0) chain.push(bg);
      if (bg && bg.a === 1) break;
    }
    chain.push({ r: 255, g: 255, b: 255, a: 1 });
    let current = chain[chain.length - 1];
    for (let i = chain.length - 2; i >= 0; i -= 1) current = over(chain[i], current);
    return current;
  };
  const host = document.createElement('div');
  host.className = 'card';
  host.innerHTML = `
    <div class="card-body">
      <span class="badge-status running">running</span>
      <span class="badge-status pending">pending</span>
      <span class="badge-status done">done</span>
      <span class="badge-status failed">failed</span>
      <span class="status-pill ok">ok</span>
      <span class="status-pill error">error</span>
      <div class="agent-item ok"><span class="agent-status-tag ok">ok</span></div>
      <div class="agent-item error"><span class="agent-status-tag error">error</span></div>
      <div class="stat-item"><div class="stat-value ok">7</div></div>
      <div class="stat-item"><div class="stat-value warn">7</div></div>
      <div class="metric-card"><div class="metric-body"><div class="metric-value"
        style="color: var(--status-warn)">7</div></div></div>
      <div class="approval-bar">
        <button class="btn-approve">放行</button>
        <button class="btn-reject">驳回</button>
      </div>
      <div class="newtask-msg">已提交</div>
    </div>`;
  document.body.appendChild(host);
  try {
    const targets = Array.from(host.querySelectorAll(
      '.badge-status, .status-pill, .agent-status-tag, .stat-value, .metric-value,'
      + ' .btn-approve, .btn-reject, .newtask-msg'));
    const submit = document.querySelector('#nt-submit');
    if (submit) targets.push(submit);
    return targets.map((el) => {
      const style = getComputedStyle(el);
      const fg = parse(style.color);
      const bg = backgroundOf(el);
      const px = parseFloat(style.fontSize);
      const weight = parseInt(style.fontWeight, 10) || 400;
      const large = px >= 24 || (px >= 18.66 && weight >= 700);
      return {
        selector: el.id ? '#' + el.id : '.' + String(el.className).split(' ').join('.'),
        ratio: ratio(fg, bg),
        need: large ? 3 : 4.5,
      };
    });
  } finally {
    host.remove();
  }
}
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def check(browser, base: str) -> None:
    from playwright.sync_api import expect

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    responses = {
        "/health": {"status": "ok", "metrics": {}},
        "/api/dashboard": {"tasks": [{"id": "sample", "pipeline": "demo", "status": "paused"}], "agents": []},
        "/api/cost": {"total_cost": 0.125, "budget": 5, "budget_remaining": 4.875},
        "/api/quality/feedback": {"total_records": 1, "total_tasks": 1, "avg_score": 83.5},
        "/api/logs": {"logs": [{"level": "info", "message": "browser-log-sample"}]},
        "/api/approvals": {"approvals": []},
        "/api/pipeline": {"pipeline_files": ["demo.yaml"]},
        "/api/deliveries": {"deliveries": [{
            "run_id": "run-browser-1", "pipeline": "demo", "status": "done",
            "finished_at": 1700000000, "output_sha256": "a" * 64,
            "node_count": 2, "artifact_count": 3,
        }]},
        "/api/deliveries/run-browser-1": {"delivery": {
            "run_id": "run-browser-1", "pipeline": "demo", "status": "done",
            "output_path": "/tmp/out/runner-output.md", "output_sha256": "a" * 64,
            "node_count": 2, "artifact_count": 3, "duration_sec": 12,
            "topology_hash": "b" * 64, "config_hashes": {"alpha": "c" * 12},
            "nodes": [{
                "node": "alpha", "status": "done", "artifact_keys": ["doc"],
                "artifacts": {"doc": {"sha256": "d" * 64, "bytes": 2048, "kind": "str"}},
            }],
        }},
        "/tasks/sample": {
            "id": "sample", "pipeline": "demo", "status": "paused",
            "dag": {
                "nodes": [
                    {"name": "alpha", "status": "done", "attempts": 1},
                    {"name": "beta", "status": "done", "attempts": 1, "dependencies": ["alpha"]},
                ],
                "layers": [["alpha"], ["beta"]],
            },
            "awaiting_approval": {"nodes": ["gate"], "messages": {}},
        },
        "/tasks/sample/nodes/alpha": PROJECTION_NODE_PAYLOAD,
        "/tasks/sample/nodes/beta": IN_MEMORY_NODE_PAYLOAD,
        # Settings 面（评审 §4.2，只读 v1）的四源。**按后端真形状 mock**：
        # `/api/webhooks` 只给 name/pipeline/enabled/secret_env/output，没有 url——
        # 用作者想象的形状 mock，正好会放过"渲染一个不存在的字段"这类缺陷。
        "/api/config": {"llm": {"model": "demo-model"}, "security": {"api_key": "sk-very-secret"},
                        "runner": {"pool_size": 4}},
        "/api/webhooks": {"webhooks": [{"name": "demo-hook", "pipeline": "demo.yaml",
                                        "enabled": True, "secret_env": "HOOK_SECRET",
                                        "output": "out"}]},
        "/api/versions/stats": {"status": "ok", "tracked_files": 7, "total_versions": 21,
                               "total_size_bytes": 4096, "max_versions_per_file": 5},
        "/api/config/audit": {"audit_file": "audit/config.jsonl", "count": 2, "records": []},
        # Workflows & Schedules 面（#93）的两源。**必须一起 mock**：这两条一漏，
        # 请求会穿透到静态服务器变 404，页面只显示"部分数据不可用"——
        # 门看上去还在跑，其实已经少验了两块（下面 `unmocked` 那道断言就是为了不再静默发生）。
        "/api/schedules": {"schedules": [{
            "name": "nightly", "pipeline": "demo.yaml", "cron": "0 2 * * *",
            "enabled": True, "last_fire": 1700000000, "next_fire": 1700003600,
            "lease_owner": "worker-1", "lease_until": 1700000900, "lease_state": "held",
        }], "count": 1},
        "/api/locks": {"scope": "current files", "pipelines": [
            {"pipeline": "demo", "matches": True}], "count": 1, "cached": 0, "drifted": []},
    }
    statuses: dict[str, int] = {}
    posts: list[str] = []
    #: 前端真的去取、但门里没 mock 的 API 路径。以前这种请求会被 `continue_()` 放给静态
    #: 服务器，拿到 404 后页面只显示"部分数据不可用"——门照样绿，那两块等于没验。
    unmocked: list[str] = []

    def respond(route):
        from urllib.parse import urlsplit

        path = urlsplit(route.request.url).path
        if path not in responses and not path.startswith("/tasks/"):
            if path.startswith("/api/") or path == "/health":
                unmocked.append(path)
            route.continue_()
            return
        if route.request.method == "POST":
            posts.append(path)
        route.fulfill(status=statuses.get(path, 200), content_type="application/json",
                      body=json.dumps(responses.get(path, {})))

    page.route("**/*", respond)
    try:
        page.goto(base + "/index.html")
        expect(page.locator("#badge-cost")).to_have_text("$0.1250")
        expect(page.locator("#badge-quality")).to_have_text("avg 83.5")
        expect(page.locator("#panel-logs")).to_contain_text("browser-log-sample")
        print("PASS: cost, quality and logs reach the rendered panels")

        # 账本面板此前只有静态源断言，M10b 变异（入参被覆写成空态）0 failed。
        # 这里从真实渲染面判：列表行来自 /api/deliveries，明细来自 /api/deliveries/<run_id>。
        expect(page.locator("#badge-deliveries")).to_have_text("1")
        expect(page.locator("#panel-deliveries")).to_contain_text("run-browser-1")
        expect(page.locator("#panel-deliveries")).to_contain_text("2 / 3")
        page.locator("#panel-deliveries tr.row-clickable").click()
        expect(page.locator("#panel-deliveries")).to_contain_text("runner-output.md")
        expect(page.locator("#panel-deliveries")).to_contain_text("节点 alpha 的产物")
        expect(page.locator("#panel-deliveries")).to_contain_text("2.0 KB")
        page.locator("#panel-deliveries .run-back").click()
        expect(page.locator("#panel-deliveries")).to_contain_text("run-browser-1")
        print("PASS: delivery ledger list and detail come from the API payload")

        statuses.update({"/api/quality/feedback": 403, "/api/logs": 403})
        page.reload()
        for panel in ("quality", "logs"):
            expect(page.locator(f"#panel-{panel}")).to_contain_text("需要 admin 权限")
        expect(page.locator("#last-updated")).not_to_contain_text("部分数据不可用")
        print("PASS: denied access is distinct from empty data and service errors")

        statuses.clear()
        statuses.update({path: 503 for path in ("/api/cost", "/api/quality/feedback", "/api/logs", "/api/approvals")})
        page.reload()
        for panel in ("cost", "quality", "logs", "approvals"):
            expect(page.locator(f"#panel-{panel}")).to_contain_text("读取失败：HTTP 503")
            expect(page.locator(f"#badge-{panel}")).to_have_text("--")
        print("PASS: failed requests cannot masquerade as zero cost or an empty inbox")

        statuses.clear()
        responses["/api/cost"] = {"total_cost": 0}
        responses["/api/quality/feedback"] = {"total_records": 0, "avg_score": 0}
        responses["/api/logs"] = {"logs": []}
        page.reload()
        expect(page.locator("#badge-cost")).to_have_text("$0.0000")
        expect(page.locator("#badge-quality")).to_have_text("avg 0")
        expect(page.locator("#panel-logs")).to_contain_text("暂无日志")
        expect(page.locator("#panel-approvals")).to_contain_text("没有等待审批的运行")
        print("PASS: successful empty responses still render as empty")

        page.locator('tr[data-task-id="sample"]').click()
        reject = page.locator(".btn-reject")
        expect(reject).to_be_enabled()

        # 跨进程节点检查器（#87 的修复）：投影里没有的四类明细是 `None`，前端必须
        # 按 `source === "runstore"` 换成"不落投影"的说法。此前 `test_dashboard_s2.py`
        # 只按字符串 token 扫 app.js 源码，这个门里 inspector/投影 零命中 ⇒
        # "前端据此换文案"这句话没有任何证据。
        page.locator(".dag-node", has_text="alpha").click()
        body = page.locator("#inspector-body")
        expect(page.locator("#inspector-title")).to_have_text("alpha")
        expect(body).to_contain_text("跨进程运行：以下为 RunStore 投影摘要")
        expect(body).to_contain_text("投影不存正文（跨进程运行）")
        assert body.get_by_text("不落投影（跨进程运行）").count() == 3, "幂等/熔断/限流三处都要换文案"
        expect(body).not_to_contain_text("该节点没有产出")
        expect(body).not_to_contain_text("熔断器未建立")
        expect(body).not_to_contain_text("限流器未建立")
        expect(body).not_to_contain_text("查不到（store 未就绪）")
        # 质量门整块不渲染（投影里是 None，不是"没有质量门"）；说明文字本身含这四个字，
        # 所以按块标题判，不按整段文本判。
        assert body.locator("h4", has_text="质量门明细").count() == 0, "投影里 quality=None 就不该有质量门块"
        assert body.locator("h4", has_text="断点续跑").count() == 0
        attempts_row = body.locator(".insp-row").filter(has_text="尝试次数")
        expect(attempts_row).to_contain_text("1 / ?")
        print("PASS: projection inspector swaps copy instead of reading null as empty")

        page.locator("#inspector-close").click()
        page.locator(".dag-node", has_text="beta").click()
        expect(page.locator("#inspector-title")).to_have_text("beta")
        expect(body).to_contain_text("hello")
        expect(body).to_contain_text("否——首次执行")
        expect(body).to_contain_text("CLOSED（连续失败 0 次）")
        expect(body).not_to_contain_text("投影不存正文（跨进程运行）")
        expect(body).not_to_contain_text("不落投影（跨进程运行）")
        print("PASS: in-memory inspector keeps the detail copy (negative control)")
        page.locator("#inspector-close").click()
        dialogs: list[str] = []

        def dismiss(dialog):
            dialogs.append(dialog.type)
            dialog.dismiss()

        page.on("dialog", dismiss)
        reject.click()
        assert dialogs == ["prompt"], dialogs
        assert not posts, posts
        page.remove_listener("dialog", dismiss)
        print("PASS: cancelling rejection sends no decision and no confirmation")

        def accept(dialog):
            dialogs.append(dialog.type)
            dialog.accept("") if dialog.type == "prompt" else dialog.accept()

        page.on("dialog", accept)
        reject.click()
        expect(page.locator(".approval-bar")).to_be_visible()
        assert posts == ["/tasks/sample/reject"], posts
        page.remove_listener("dialog", accept)
        print("PASS: explicit confirmation still submits rejection")

        statuses["/api/pipeline"] = 401
        page.reload()
        expect(page.locator("#token-input")).to_be_visible()
        statuses.clear()
        responses["/api/pipeline"] = {"pipeline_files": ["after-login.yaml"]}
        page.locator("#token-input").fill("test-only-token")
        page.locator("#token-save").click()
        expect(page.locator("#nt-pipeline option")).to_have_text(["after-login"])
        print("PASS: saving credentials reloads the workflow catalog")

        # Settings 面（评审 §4.2，只读 v1）：四块真渲染 + 敏感键掩码 + 只读边界。
        # 这一步是 PR #90 声称"真 Chrome 11 项全过"的**可复现版本**——那次是一次性脚本，
        # 没进仓，别人没法重跑；而且它按想象的字段 mock，漏掉了 `/api/webhooks` 没有 url。
        panel = page.locator("#panel-settings")
        expect(panel).to_contain_text("运行配置")
        expect(panel).to_contain_text("llm.model")
        expect(panel).to_contain_text("***（已掩码）")
        expect(panel).not_to_contain_text("sk-very-secret")
        expect(panel).to_contain_text("demo-hook")
        expect(panel).to_contain_text("demo.yaml")
        expect(panel).to_contain_text("受跟踪文件")
        expect(panel).to_contain_text("配置审计记录")
        expect(panel).to_contain_text("不触发回滚")
        print("PASS: settings face renders four sources, masks secrets and states read-only")

        # Workflows & Schedules 面（#93）：排程行与租约三态从 /api/schedules 真渲染。
        # 这两条 mock 是并 main 后才发现漏的——漏了门也照样绿，只是页面多一行"部分数据不可用"。
        wf = page.locator("#panel-workflows")
        expect(wf).to_contain_text("nightly")
        expect(wf).to_contain_text("0 2 * * *")
        expect(wf).to_contain_text("持有中（worker-1）")
        expect(page.locator("#badge-workflows")).to_have_text("1 / 1")
        print("PASS: workflows & schedules panel renders rows and lease state")
        assert not unmocked, (
            "门里没 mock 的前端数据源：" + str(sorted(set(unmocked)))
            + "——它们会被放给静态服务器变 404，页面只显示'部分数据不可用'，那几块等于没验"
        )
        assert not errors, errors

        # 配色（L2-01/L2-02）：静态门禁实算的是"CSS 模型"，这里量的是浏览器真级联。
        # 两层都过才说明"改对了"，而不是"改得符合我对 CSS 的想象"。
        for theme in ("light", "dark"):
            page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            report = page.evaluate(CONTRAST_PROBE_JS)
            assert len(report) >= 15, f"{theme}: 探针只量到 {len(report)} 个元素，覆盖面缩水了"
            bad = [item for item in report if item["ratio"] < item["need"]]
            assert not bad, f"{theme} 主题真浏览器实算不达标：{bad}"
        page.evaluate("() => document.documentElement.removeAttribute('data-theme')")
        report = page.evaluate(CONTRAST_PROBE_JS)
        bad = [item for item in report if item["ratio"] < item["need"]]
        assert not bad, f"默认（浅色）主题真浏览器实算不达标：{bad}"
        print("PASS: light (default) and dark palettes meet WCAG AA in a real browser")
    finally:
        page.close()


def main() -> None:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=None, help="Installed browser channel, e.g. chrome or msedge")
    parser.add_argument("--dashboard-dir", type=Path, default=ROOT / "loom_core" / "dashboard")
    args = parser.parse_args()
    handler = functools.partial(QuietHandler, directory=str(args.dashboard_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=args.channel, headless=True)
            try:
                check(browser, f"http://127.0.0.1:{server.server_port}")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
