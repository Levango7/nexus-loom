"""浏览器门的覆盖度守卫 + mock 与后端契约的同形判据 + CI 接线判据。

`tests/check_dashboard_browser.py` 用真实 Chrome 跑渲染判据（pytest 不收集它）。它给出的是
"渲染真的对"的证据，但历史上只靠人肉执行——用例被删 / 从没加上时 CI 一条都不会红；
#93 那次前端新增 `/api/schedules` 而门里没补 mock，页面降级成"部分数据不可用"，门照样绿。
C-7（账本面板）和 C-10（节点检查器投影态）补的都是"扫源码 token 看不出来、只有渲染才看得见"的缺口，
而那个文件在 CI 里此前**没有任何守卫**。

本文件守三件事：

1. **覆盖度**：把门里每条 `print("PASS: ...")` 当成一条已交付用例，断言 ①主题一条不缺、
   ②每条用例在自己之前真有 `expect(`/`assert `（只有 print 没有断言＝假用例）、
   ③投影态那条的正反两面对照都在。
2. **载荷同形**：门里 mock 的投影响应必须与后端 `_node_inspector_from_projection` 的
   真产出逐字段一致——mock 漂移了，门就在验一个不存在的契约，而这种漂移在手动门里
   永远看不出来。
3. **接线**：`ci.yml` 必须真有一步在跑这个脚本并交代浏览器来源；否则前两层只在有人
   手动跑时才生效，"有门"和"门被跑"是两件事。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from tests.check_dashboard_browser import IN_MEMORY_NODE_PAYLOAD, PROJECTION_NODE_PAYLOAD

GATE = Path(__file__).resolve().parent / "check_dashboard_browser.py"
CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

PASS_RE = re.compile(r'print\("PASS: ([^"]+)"\)')

#: 门里必须存在的用例主题（按 PASS 文案的子串认）。删掉整块用例时，主题与它的
#: PASS 行一起消失，CI 这里就会红。
REQUIRED_PASS_TOPICS = {
    "cost, quality and logs": "S1 成本/质量/日志三块面板的渲染",
    "delivery ledger list and detail": "交付账本列表与明细（C-7）",
    "projection inspector swaps copy": "节点检查器投影态换文案（C-10）",
    "in-memory inspector keeps the detail copy": "同节点的内存态反向对照（C-10）",
    "settings face renders four sources": "Settings 面四源渲染 / 掩码 / 只读边界（PR #90）",
    "workflows & schedules panel renders rows": "Workflows & Schedules 面排程行与租约三态（PR #93）",
    "denied access": "403 与空数据必须可区分",
    "failed requests": "503 不能伪装成零成本或空收件箱",
    "successful empty responses": "成功空响应仍渲染为空",
    "cancelling rejection": "取消驳回不发决策",
    "explicit confirmation": "确认之后才发驳回",
    "saving credentials": "凭证保存后重载流水线目录",
    "schema-driven form": "声明式输入表单渲染与 typed 提交（T6）",
    "undeclared pipeline keeps the freeform": "无声明流水线保持自由文本（T6）",
    "delivery detail downloads": "交付物下载（T6）",
    "palettes meet WCAG AA": "浅/深主题对比度真浏览器复核（L2-01/L2-02）",
}


#: 用例收尾的事件解绑/收尾语句：它们不是判据，回溯"最后一句断言"时跳过。
CLEANUP_PREFIXES = ("page.remove_listener(", "page.close(", "browser.close(")


class TestGateCoverage:
    def setup_method(self):
        self.src = GATE.read_text(encoding="utf-8")
        self.passes = list(PASS_RE.finditer(self.src))

    def test_every_required_topic_has_a_case(self):
        messages = [m.group(1) for m in self.passes]
        missing = {
            needle: why
            for needle, why in REQUIRED_PASS_TOPICS.items()
            if not any(needle in msg for msg in messages)
        }
        assert not missing, f"手动门缺这些用例（被删了？从没加？）：{missing}"

    def test_every_case_actually_asserts(self):
        """每条 PASS 的**上一行可执行语句**必须是断言。

        判"两个 PASS 之间有 expect"太松：前置的 setup 行（`reject = page.locator(...)` 后
        常跟着一句 `expect(reject).to_be_enabled()`）会替被删掉的断言兜底，删了照样绿。
        这里的判据是"用例最后一句就是断言"——删掉最后一条断言立刻红。
        """
        assert len(self.passes) >= len(REQUIRED_PASS_TOPICS), (
            f"只解析出 {len(self.passes)} 条 PASS，比主题还少——解析器或文件结构坏了"
        )
        for match in self.passes:
            lines = self.src[:match.start()].rstrip().splitlines()
            previous = next(
                (line.strip() for line in reversed(lines)
                 if line.strip()
                 and not line.strip().startswith("#")
                 and not line.strip().startswith(CLEANUP_PREFIXES)),
                "",
            )
            assert previous.startswith(("expect(", "assert ")), (
                f"用例「{match.group(1)}」的 PASS 之前最后一句话不是断言（是 {previous!r}）——"
                "删了最后一条断言还能绿"
            )
        total = self.src.count("expect(") + self.src.count("assert ")
        assert total >= 30, f"门里断言总数只有 {total}，少得不像真的"

    def test_projection_case_has_both_sides(self):
        """正向判"不编"的文案，反向判内存态文案不出现——只判一边，把两句都印出来也能过。"""
        assert 'to_contain_text("投影不存正文（跨进程运行）")' in self.src
        assert 'not_to_contain_text("该节点没有产出")' in self.src
        assert "count() == 3" in self.src, "幂等/熔断/限流三处都要换文案，只判一处会漏"

    def test_gate_is_not_pytest_collected(self):
        """门靠真浏览器跑；pytest 默认 python_files=test_*.py / *_test.py 不收集它。

        改名成 test_*.py 会让 CI 直接把它当单测跑（缺 playwright 就红、有 playwright 就
        在无头环境里连浏览器），那不是这个文件想要的形态。
        """
        name = GATE.name
        assert not name.startswith("test_") and not name.endswith("_test.py")


class TestGateWiring:
    """这门自己有没有被 CI 接上——"人记得跑才算数"的门等于没有门。"""

    def test_ci_invokes_the_gate_with_a_real_browser(self):
        import yaml

        ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        commands = []
        for job in ci["jobs"].values():
            for step in job.get("steps", []):
                for line in str(step.get("run", "")).splitlines():
                    parts = line.strip().split()
                    if len(parts) > 1 and parts[0].startswith("python") \
                            and GATE.name in (p.rsplit("/", 1)[-1] for p in parts):
                        commands.append(line.strip())
        assert commands, (
            f"ci.yml 里没有任何一步真在跑 {GATE.name}——浏览器门退回手动执行，"
            "前端换数据源忘补 mock 又会静默降级（#93 那次就是这个形状）"
        )
        job_text = CI_YML.read_text(encoding="utf-8")
        for command in commands:
            tokens = command.split()
            browser_source = ("--channel" in tokens) or ("playwright install" in job_text)
            assert browser_source, (
                f"{command!r} 没交代浏览器从哪来：runner 上没装浏览器时这一步会直接报错，"
                "而不是静默跳过——但如果它被改成条件跳过，门就又没人守了"
            )



class _FakeRunHandle:
    """只回答投影构造需要的两个问题：台账行 + 节点摘要。"""

    def record(self):
        return {"status": "paused", "principal": "admin"}

    def nodes(self):
        return [{
            "node": "alpha", "status": "done", "attempts": 1, "error": "",
            "started_at": 1700000000.0, "finished_at": 1700000000.0,
            "artifact_keys": ["doc"],
        }]


def _projection_from_backend(task_id: str, node_name: str) -> dict:
    """用真的 handler 方法算一遍投影响应，不碰 socket：只截 `_json` 的载荷。"""
    from loom_core.admin_api import AdminHandler

    probe = object.__new__(AdminHandler)
    probe.captured = None
    probe.orch = SimpleNamespace(run_handle=lambda _task_id: _FakeRunHandle())
    probe._json = lambda data, status=200: setattr(probe, "captured", data)
    probe._principal = lambda: SimpleNamespace(admin=True, id="admin")
    probe._node_inspector_from_projection(task_id, node_name)
    assert probe.captured is not None, "投影构造器没走到 _json，探针本身失效了"
    return probe.captured


class TestProjectionPayloadParity:
    def test_gate_mock_matches_backend_projection(self):
        """门里 mock 的投影响应 == 后端真产出：否则门验的是一个不存在的契约。"""
        actual = _projection_from_backend("sample", "alpha")
        assert actual == PROJECTION_NODE_PAYLOAD, (
            "手动门的投影 mock 与后端 _node_inspector_from_projection 不一致；"
            f"后端={actual!r}，门={PROJECTION_NODE_PAYLOAD!r}"
        )

    def test_projection_mock_marks_unavailable_details_as_none(self):
        assert PROJECTION_NODE_PAYLOAD["source"] == "runstore"
        for kind in ("artifact", "idempotency", "circuit_breaker", "rate_limit", "quality", "checkpoint"):
            assert PROJECTION_NODE_PAYLOAD[kind] is None, f"{kind} 必须显式 None（'取不到就不编'）"
        node = PROJECTION_NODE_PAYLOAD["node"]
        assert node["max_retries"] is None, "投影不存重试上限：给 0 会被前端读成'不许重试'"
        assert node["artifact_keys"] == ["doc"], "投影存 artifact_keys，前端据此渲染'不存正文'行"

    def test_in_memory_mock_keeps_the_four_details(self):
        assert "source" not in IN_MEMORY_NODE_PAYLOAD, "带 source 就变成投影态了，反向对照失效"
        for kind in ("artifact", "idempotency", "circuit_breaker", "rate_limit"):
            assert IN_MEMORY_NODE_PAYLOAD[kind] is not None, f"{kind} 是反向对照的前提"


#: 与门里 WORKFLOW_INPUTS_PAYLOAD 同形的声明（T6 同形判据的真解析输入）。
_TYPED_YAML = """
name: demo
pipeline:
  inputs:
    type: object
    required: [topic]
    additionalProperties: false
    properties:
      topic: {type: str, minLength: 2, maxLength: 40}
      depth: {type: int, minimum: 1, maximum: 5, default: 3}
      mode: {enum: [fast, full]}
agents:
  - name: tldr
    version: "0.1"
    dependencies: []
    config: {max_sentences: 2}
topology:
  levels: [["tldr"]]
  edges: []
"""


class TestInputsPayloadParity:
    """门里 inputs mock == 后端 `_handle_workflow_inputs` 的真产出（T6）。

    不手抄参照系：真 Scheduler 解析一份同形 YAML，探针截 `_json` 的载荷逐字段比。
    后端改了响应形状而 mock 没跟上时，门会绿着验一个不存在的契约——这条就是拦它的。
    """

    def test_gate_inputs_mock_matches_backend_declaration(self, tmp_path, monkeypatch):
        from loom_core import admin_api
        from loom_core.scheduler import Scheduler
        from tests.check_dashboard_browser import WORKFLOW_INPUTS_PAYLOAD

        d = tmp_path / "pipe"
        d.mkdir()
        f = d / "demo.yaml"
        f.write_text(_TYPED_YAML, encoding="utf-8")
        project = Path(__file__).resolve().parent.parent
        plan = Scheduler(agents_dir=str(project / "agents"),
                         pipeline_dir=str(d)).parse_file(str(f), verify_lock=False)
        monkeypatch.setattr(admin_api, "resolve_pipeline_name",
                            lambda name, installed: (name, None))
        monkeypatch.setattr(admin_api._scheduler_mod, "installed_pipelines",
                            lambda: ["demo"])
        monkeypatch.setattr(Scheduler, "parse", lambda self, name: plan)
        probe = object.__new__(admin_api.AdminHandler)
        captured: dict = {}
        probe._json = lambda data, status=200: captured.update(data)
        probe._handle_workflow_inputs("demo")
        assert captured == WORKFLOW_INPUTS_PAYLOAD, (
            "门的 inputs mock 与后端 _handle_workflow_inputs 不一致；"
            f"后端={captured!r}，门={WORKFLOW_INPUTS_PAYLOAD!r}")
