"""README 的「第一公里」：新机必须先能跑通一条**离线**闭环（L5-01 / L5-02）。

起因（L5-01，P1 真缺陷）：README「3 步快速体验」的第一步就是 `pip install` + 联网检索
（默认流水线 `docgen` = researcher + writer）——新机上要有网络和检索 Key 才能看到任何产出；
而仓库里唯一的离线闭环 `loom demo` 在 README 里**零提及**（`grep -c demo README.md` ⇒ 0）。
实测 `python run.py demo`：约 11s、exit 0、docx/html/md 三件交付物、质量门 82.0/100、
`state/deliveries.db` 落账 —— 它才是第一公里该给的东西：**装完依赖就有产出**。

L5-02（P2 真缺陷）：README「CLI 动词与参数」表漏了 `loom demo` 与 `loom pack new`
（`run.py` 的 `VERBS` 与 `_PACK_ACTIONS` 里都有）。

护栏从 **`run.py` 反推**，不写死动词表：`run.py` 新增动词而 README 没跟上，这里直接红。
最后一条走真进程（`python run.py demo`），因为 L5-01 要保证的是"新机真能跑出东西"，
文档里写了而实际跑不通，比不写更糟。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
RUN_PY = ROOT / "run.py"
DEMO_YAML = ROOT / "packs" / "docgen" / "pipelines" / "demo.yaml"

#: `demo` 流水线**承诺全离线**（demo.yaml 头部自述：不检索、不抓取、不调 LLM）。
#: 新增需要网络/LLM 的节点就必须同时改 README 的离线承诺，所以这里钉住集合。
OFFLINE_AGENTS = {"transform", "quality_gate", "checker", "layout", "safe_writer", "renderer"}


def _module_assign(name: str):
    """从 run.py 取一个模块级赋值的字面量值（VERBS / _PACK_ACTIONS）。"""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"run.py 里找不到模块级赋值 {name} —— 本文件的护栏需要改写法")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _cli_verb_section() -> str:
    """README 的「CLI 动词与参数」一节（到下一个二/三级标题为止）。

    只在**这一节**里找动词，而不是全 README 扫一遍：否则散文里顺口提一句
    `loom demo` 就能让「文档里有」成立 —— 那正是 L5-02 的缺陷形态
    （动词表缺行，别处却可能提到过）。判据必须落在表里。
    """
    text = _readme()
    start = text.find("### CLI 动词与参数")
    assert start != -1, "README 里找不到「CLI 动词与参数」一节 —— 护栏需要改写法"
    rest = text[start:]
    m = re.search(r"\n#{2,3} ", rest[1:])
    return rest[: m.start() + 1] if m else rest


# ─── L5-02：动词表必须覆盖 run.py 的真实动词 ────────────────────

def test_every_cli_verb_is_documented():
    """`run.py` 的每个动词都必须在 README 里出现（`loom <verb>`）。"""
    verbs = _module_assign("VERBS")
    assert verbs, "run.py 的 VERBS 是空的 —— 护栏需要改写法"
    section = _cli_verb_section()
    missing = [v for v in verbs if f"loom {v}" not in section]
    assert not missing, (
        f"「CLI 动词与参数」一节里找不到这些动词：{missing}"
        f"（run.py 的 VERBS={list(verbs)}）。新增动词后请同步那一节的动词表")


def test_every_pack_action_is_documented():
    """`pack` 的每个子动作也必须在 README 里出现（`loom pack <action>`）。

    `pack new` 就是这样漏掉的：动词表当时只写了 `pack list` / `pack check`。
    """
    actions = _module_assign("_PACK_ACTIONS")
    assert actions, "run.py 的 _PACK_ACTIONS 是空的 —— 护栏需要改写法"
    section = _cli_verb_section()
    missing = [a for a in actions if f"loom pack {a}" not in section]
    assert not missing, (
        f"「CLI 动词与参数」一节里找不到这些 pack 子动作：{missing}"
        f"（run.py 的 _PACK_ACTIONS={list(actions)}）")


# ─── L5-01：第一公里必须是离线的 ────────────────────────────────

def test_quickstart_leads_with_an_offline_path():
    """`loom demo` 必须在「走联网 + LLM 全路径」**之前**出现。

    这条钉的不是"README 里提到过 demo"，而是**顺序**：先说清"装完依赖就能跑出东西"，
    再讲需要网络与 Key 的那条路。反过来（先联网后离线）正是 L5-01 报的问题形态。
    """
    text = _readme()
    offline_at = text.find("run.py demo")
    assert offline_at != -1, (
        "README 里没有 `run.py demo` —— 离线闭环又变成零提及了（L5-01 回归）")
    networked_at = text.find("-o output/my_doc.md")
    assert networked_at != -1, (
        "README 里没有联网路径的示例命令了 —— 请核对快速开始那两节")
    assert offline_at < networked_at, (
        "README 把联网 + LLM 的路径排在了离线 `loom demo` 之前 "
        f"（离线 @{offline_at}，联网 @{networked_at}）—— 新机会先卡在没有 Key 上")


def test_demo_pipeline_uses_only_offline_agents():
    """`demo` 流水线的节点集合必须是全离线的那一套（README 的承诺有据可依）。"""
    src = DEMO_YAML.read_text(encoding="utf-8")
    found = set(re.findall(r"^\s*-\s*name:\s*([A-Za-z_][\w]*)\s*$", src, re.MULTILINE))
    assert found, f"{DEMO_YAML} 里没解析出 agents 的 name —— 护栏需要改写法"
    assert found == OFFLINE_AGENTS, (
        f"demo 流水线的节点集合变成 {sorted(found)}，而 README 承诺它"
        f"「不检索、不抓取、不调 LLM」（登记的全离线集合是 {sorted(OFFLINE_AGENTS)}）。"
        "若新增节点**确实**离线，请把名字加进 OFFLINE_AGENTS；"
        "若它需要网络/LLM，请同时改 README 的离线承诺")


def test_demo_runs_offline_end_to_end(tmp_path):
    """真跑一遍 `python run.py demo`：新机第一公里必须**真有产出**。

    只看文档不算数——L5-01 要保证的是"装完依赖就能看到交付物"。这里断言四件事：
    退出码 0、状态 done、三件交付物落盘、交付账本落行（`state/deliveries.db`）。
    """
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "demo"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"`loom demo` 退出码 {proc.returncode}：\n{out[-2000:]}"
    assert "状态: done" in out, f"`loom demo` 没跑到 done：\n{out[-2000:]}"

    sandboxes = sorted(tmp_path.glob("loom-demo-*"))
    assert sandboxes, f"沙盒目录没建出来（cwd={tmp_path}）：\n{out[-1000:]}"
    deliverables = sandboxes[-1] / "deliverables"
    for name in ("demo-report.md", "demo-report.html", "demo-report.docx"):
        path = deliverables / name
        assert path.exists() and path.stat().st_size > 0, \
            f"交付物 {name} 没落盘或为空（{deliverables}）：\n{out[-1000:]}"
    assert (sandboxes[-1] / "state" / "deliveries.db").exists(), (
        "交付账本 state/deliveries.db 没落行 —— 第一公里只跑到一半：\n"
        f"{out[-1000:]}")
