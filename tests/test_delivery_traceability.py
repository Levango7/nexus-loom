"""交付追溯面的判据：内容摘要 / 逐节点产物 / 锁指纹，一路到 HTTP 与控制台。

v3.15.0 已经给了整篇交付物的 `output_sha256` 与 `topology_hash`/`config_hashes`，
但"逐节点分别产出了什么内容"和"这些东西在台面上看不看得见"仍是空的。本文件钉：

  A. 摘要实现本身（规范形态、键序无关、编不动的对象也不抛）；
  B. 真跑一遍：账本里逐节点产物摘要与内存里的结果逐字对得上，指纹是 12 位；
  C. 穿过 HTTP：列表给 `output_sha256`，明细给 `topology_hash`/`config_hashes`
     与逐节点 `artifacts`——写侧算对了但端点没带上，等于这一切不存在；
  D. 控制台接线：卡片在 DOM 里、真调两个端点、每轮刷新都渲染、点行开明细能返回；
  E. **跨端字段契约**：前端读的字段必须真的出现在 HTTP 响应里（后端改名/删字段，
     两端各自 grep 一遍都不会红，这条会）。
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path

from tests.test_delivery_forms import DEMO_YAML, _plan

PROJECT = Path(__file__).resolve().parent.parent
DASH = PROJECT / "loom_core" / "dashboard"


# ── A. 摘要实现 ─────────────────────────────────────────

class TestDigest:
    def test_text_is_digested_over_utf8_bytes(self):
        from loom_core.content_digest import digest_of

        value = "# 标题\n中文正文"
        got = digest_of(value)
        assert got["sha256"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert got["bytes"] == len(value.encode("utf-8"))
        assert got["kind"] == "text"

    def test_dict_digest_is_key_order_invariant(self):
        """同一个 dict 换个键序就换一个 sha，"追溯"当场失效。"""
        from loom_core.content_digest import digest_of

        a = digest_of({"x": 1, "y": {"p": 2, "q": 3}})
        b = digest_of({"y": {"q": 3, "p": 2}, "x": 1})
        assert a == b and a["kind"] == "json"

    def test_unserialisable_value_still_digests(self):
        """编不动的对象退到 repr 而不是抛：交付收尾那条路绝不外抛。"""
        from loom_core.content_digest import digest_of

        got = digest_of({(1, 2), 3})
        assert len(got["sha256"]) == 64 and got["bytes"] > 0

    def test_only_dict_results_yield_per_key_digests(self):
        from loom_core.content_digest import artifact_digests, digest_of

        assert artifact_digests(None) == {}
        assert artifact_digests("正文") == {}
        got = artifact_digests({"content": "abc", "meta": {"n": 1}})
        assert list(got) == ["content", "meta"]
        assert got["content"]["sha256"] == digest_of("abc")["sha256"]


# ── B. 真跑一遍 ─────────────────────────────────────────

def _run_demo(orch, tmp_path, task_id: str, name: str = "trace-demo"):
    plan, out = _plan(DEMO_YAML.format(out=str(tmp_path / "out")), tmp_path, name)
    inp = tmp_path / "in.md"
    inp.write_text("# 输入\n追溯判据。", encoding="utf-8")
    task = orch.run_plan(plan, input_file=str(inp), task_id=task_id, wait=True)
    assert task.status.value == "done", task.error
    return task, plan


class TestRecordCarriesTraceability:
    def test_node_artifact_digests_match_the_in_memory_results(self, orch, tmp_path):
        from loom_core.content_digest import digest_of

        task, plan = _run_demo(orch, tmp_path, "trace-rec-1")
        from loom_core.deliveries import DeliveryLedger

        rec = DeliveryLedger().get("trace-rec-1")
        assert rec is not None
        node_map = {n["node"]: n for n in rec["nodes"]}
        assert node_map, "账本里没有逐节点记录"
        checked = 0
        for name, node in node_map.items():
            assert set(node["artifacts"]) == set(node["artifact_keys"]), name
            dag = task.dag_nodes.get(name)
            for key, digest in node["artifacts"].items():
                assert digest["sha256"] == digest_of(dag.result[key])["sha256"]
                checked += 1
        assert checked, "没有任何产物被摘要，判据空转"

    def test_output_sha256_matches_the_file_on_disk(self, orch, tmp_path):
        task, _plan_obj = _run_demo(orch, tmp_path, "trace-rec-2")
        from loom_core.deliveries import DeliveryLedger

        rec = DeliveryLedger().get("trace-rec-2")
        assert rec["output_sha256"] == hashlib.sha256(
            Path(task.output_path).read_bytes()).hexdigest()

    def test_output_bytes_matches_the_file_on_disk(self, orch, tmp_path):
        task, _plan_obj = _run_demo(orch, tmp_path, "trace-rec-bytes")
        from loom_core.deliveries import DeliveryLedger

        rec = DeliveryLedger().get("trace-rec-bytes")
        assert rec["output_bytes"] == len(Path(task.output_path).read_bytes())

    def test_output_bytes_tristate_when_file_is_gone_or_never_declared(self, orch,
                                                                       tmp_path):
        """None=没记录（无 output_path）；-1=声明了路径但文件读不到。

        与读侧 fmtBytes 的三态同一口径：把"文件丢了"写成 0 或 None，
        下载面就会把"曾经有产物"说成"空文件/没产物"。
        """
        task, plan = _run_demo(orch, tmp_path, "trace-rec-tri")
        task.output_path = str(tmp_path / "gone.md")
        rec = orch._build_delivery_record(task, plan)
        assert rec["output_bytes"] == -1
        assert rec["output_sha256"] == "", "文件丢了时 sha 仍是空串（A2 口径不变）"
        task.output_path = ""
        rec = orch._build_delivery_record(task, plan)
        assert rec["output_bytes"] is None

    def test_fingerprint_fields_are_present_and_shaped(self, orch, tmp_path):
        _task, plan = _run_demo(orch, tmp_path, "trace-rec-3")
        from loom_core.deliveries import DeliveryLedger

        rec = DeliveryLedger().get("trace-rec-3")
        assert len(rec["topology_hash"]) == 12
        expected = {n.agent_name for level in plan.levels for n in level}
        assert set(rec["config_hashes"]) == expected
        assert all(len(h) == 12 for h in rec["config_hashes"].values())


# ── C. 穿过 HTTP ────────────────────────────────────────

class TestOverHTTP:
    def _serve(self, orch, monkeypatch):
        from loom_core.admin_api import AdminAPI

        # 端口只绑回环，但鉴权开关会被环境变量牵着走：删掉它，否则本机设过
        # ADMIN_API_KEY 时这几条会一起 401（那是环境噪声，不是契约变化）
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        api = AdminAPI(host="127.0.0.1", port=0, api_key="", tokens_file="")
        assert api.start(orch) is True
        return api, f"http://127.0.0.1:{api._server.server_address[1]}"

    def _stop(self, api):
        """停服务 + 复位 `AdminHandler` 的**类**属性（`stop()` 自己不清）。

        不复位就会把"我这台 orch / 空 api_key"留给后面所有起 AdminAPI 的用例，
        症状是它们在别的文件里莫名变红、单跑却全绿。
        """
        from loom_core.admin_api import AdminHandler

        api.stop()
        AdminHandler.orch = None
        AdminHandler.api_key = None
        AdminHandler.tokens = None
        AdminHandler.principal = None

    def _get(self, url: str):
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_list_and_detail_expose_the_traceability_fields(self, orch, tmp_path,
                                                           monkeypatch):
        task, _plan_obj = _run_demo(orch, tmp_path, "trace-http-1")
        expected = hashlib.sha256(Path(task.output_path).read_bytes()).hexdigest()
        api, base = self._serve(orch, monkeypatch)
        try:
            code, body = self._get(base + "/api/deliveries?limit=20")
            assert code == 200, body
            item = next(d for d in body["deliveries"]
                        if d["run_id"] == "trace-http-1")
            assert item["output_sha256"] == expected, item
            assert item["output_bytes"] == len(Path(task.output_path).read_bytes()), item

            code, detail = self._get(base + "/api/deliveries/trace-http-1")
            assert code == 200, detail
            delivery = detail["delivery"]
            assert delivery["output_sha256"] == expected
            assert delivery["output_bytes"] == item["output_bytes"]
            assert len(delivery["topology_hash"]) == 12
            assert delivery["config_hashes"], delivery
            with_digests = [n for n in delivery["nodes"] if n.get("artifacts")]
            assert with_digests, "逐节点产物摘要没穿过 HTTP"
        finally:
            self._stop(api)

    def test_a_record_without_a_digest_crosses_as_empty_not_as_zero(self, orch,
                                                                    tmp_path,
                                                                    monkeypatch):
        """没有落盘产物的运行：`output_sha256` 给空串（未记录），不是 0/64 个 0。"""
        from loom_core.deliveries import DeliveryLedger

        DeliveryLedger().record({
            "run_id": "trace-nodigest", "pipeline": "p", "status": "failed",
            "finished_at": 1_000_000.0, "output_path": "", "output_sha256": "",
            "nodes": [],
        })
        api, base = self._serve(orch, monkeypatch)
        try:
            code, body = self._get(base + "/api/deliveries?limit=50")
            assert code == 200, body
            item = next(d for d in body["deliveries"]
                        if d["run_id"] == "trace-nodigest")
            assert item["output_sha256"] == "", item
            assert item["output_bytes"] is None, "老行没有这键 ⇒ null（未记录），不是 0"
        finally:
            self._stop(api)


# ── D/E. 控制台接线与跨端字段契约 ────────────────────────

def _js() -> str:
    return (DASH / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (DASH / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (DASH / "style.css").read_text(encoding="utf-8")


def _console_region() -> str:
    """app.js 里账本面板那一段（从 fmtBytes 到 renderError 之前）。"""
    js = _js()
    start = js.index("function fmtBytes (n) {")
    end = js.index("function renderError ()")
    return js[start:end]


class TestConsoleWiring:
    def test_card_exists_before_the_run_detail_card(self):
        html = _html()
        assert '<div class="card wide" id="panel-deliveries">' in html
        assert 'id="badge-deliveries"' in html
        assert html.index('id="panel-deliveries"') < html.index('id="panel-run"'), \
            "交付卡排在运行详情之后会被压进默认隐藏的详情态里"

    def test_fetches_the_ledger_endpoints(self):
        js = _js()
        assert "fetchJson(API_BASE + '/api/deliveries?limit=' + DELIVERIES_LIMIT)" in js
        assert ("fetchJson(API_BASE + '/api/deliveries/' + "
                "encodeURIComponent(runId))") in js

    def test_renders_every_refresh_and_opens_detail(self):
        js = _js()
        # 逐行比而不是子串比：`back.onclick = () => renderDeliveries(...)` 也含这段
        # 子串，只在 render() 里把这一行注释掉的话，子串型断言会假绿（变异 M10 实测）。
        render_body = js[js.index("function render () {"):
                         js.index("/* ---- Approvals")]
        lines = [ln.strip() for ln in render_body.splitlines()]
        assert "renderDeliveries(state.deliveries);" in lines,             "render() 里没有调用账本面板（子串命中不算，必须是真语句）"
        assert "state.deliveries = data.deliveries;" in js
        assert "deliveries: settle(results[6])" in js
        region = _console_region()
        assert "openDelivery(String(item.run_id || ''))" in region
        assert "renderDeliveryDetail((data && data.delivery) || {})" in region
        # 返回按钮（2026-10-31 更新）：原先钉 `() => renderDeliveries(state.deliveries)`
        # 单行；该形态下明细会被 5s 轮询覆盖（点开即消失，浏览器实测复现），
        # 改为"先清明细持有旗、再回列表"。行为契约不变（回列表），新增清旗接线，
        # 对应判据见 test_dashboard_s2.TestDeliveriesDetailSurvivesPolling。
        assert "back.onclick = () => {" in region
        assert "deliveriesDetailOpen = false;" in region

    def test_missing_digest_has_two_separate_truths(self):
        region = _console_region()
        assert "未记录" in region          # 没有落盘产物 / 写在摘要面之前
        assert "这个节点没有产出物" in region
        assert "产物键有，摘要没记" in region

    def test_artifact_rows_reuse_the_inspector_row_style(self):
        region = _console_region()
        for token in ("artifactDigestBlock", "insp-art-row", "insp-art-key",
                      "insp-art-type", "insp-art-val"):
            assert token in region, f"控制台缺 {token}"

    def test_no_emoji_in_the_panel(self):
        for ch in ("\u2705", "\u274c", "\u26a0\ufe0f", "\U0001f680",
                   "\U0001f4e6", "\U0001f512"):
            assert ch not in _console_region()
            assert ch not in _html()

    def test_styles_exist_and_are_used(self):
        css = _css()
        region = _console_region()
        for cls in re.findall(r"^\.(deliv-[a-z-]+)", css, re.M):
            assert cls in region, f"样式 .{cls} 没有使用者（写了个死样式）"


class TestConsoleApiContract:
    """前端读的字段必须在**真响应**里存在。

    参照系不手抄：起真服务、拉真响应，把列表项与明细的键并起来，再和前端读到的
    字段求差集——后端改名/删字段时，这类跨端契约只有这一条会红。
    """

    NOT_FIELDS = {"run", "length", "map", "sort", "slice", "join", "push",
                  "toFixed", "toLocaleString", "toLocaleTimeString", "now",
                  "keys", "title"}

    def _fields_read(self, region: str) -> set[str]:
        found: set[str] = set()
        for pattern in (r"\bitem\.([a-z_][a-z_0-9]*)",
                        r"\bdelivery\.([a-z_][a-z_0-9]*)",
                        r"\bn\.([a-z_][a-z_0-9]*)",
                        r"\barts\[k\]\.([a-z_][a-z_0-9]*)"):
            found |= {m.group(1) for m in re.finditer(pattern, region)}
        return found - self.NOT_FIELDS

    def test_console_reads_only_fields_the_api_actually_returns(self, orch, tmp_path,
                                                               monkeypatch):
        from loom_core.admin_api import AdminAPI

        _run_demo(orch, tmp_path, "trace-contract")
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        api = AdminAPI(host="127.0.0.1", port=0, api_key="", tokens_file="")
        assert api.start(orch) is True
        base = f"http://127.0.0.1:{api._server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/deliveries?limit=20",
                                        timeout=20) as resp:
                listing = json.loads(resp.read().decode())
            with urllib.request.urlopen(base + "/api/deliveries/trace-contract",
                                        timeout=20) as resp:
                detail = json.loads(resp.read().decode())["delivery"]
        finally:
            from loom_core.admin_api import AdminHandler

            api.stop()
            AdminHandler.orch = None
            AdminHandler.api_key = None
            AdminHandler.tokens = None
            AdminHandler.principal = None

        item = next(d for d in listing["deliveries"]
                    if d["run_id"] == "trace-contract")
        readable = set(item) | set(detail)
        for node in detail.get("nodes") or []:
            readable |= set(node)
            for digest in (node.get("artifacts") or {}).values():
                readable |= set(digest)
        readable |= set(detail.get("config_hashes") or {})

        used = self._fields_read(_console_region())
        assert used, "判据没抓到任何字段引用，护栏需要改写法"
        missing = sorted(used - readable)
        assert missing == [], f"控制台读了响应里不存在的字段：{missing}"

    def test_traceability_fields_are_all_presented(self):
        region = _console_region()
        for token in ("item.output_sha256", "delivery.output_sha256",
                      "delivery.output_bytes",
                      "delivery.topology_hash", "delivery.config_hashes",
                      "n.artifacts", "arts[k].sha256", "arts[k].bytes"):
            assert token in region, f"控制台没呈现追溯字段 {token}"
