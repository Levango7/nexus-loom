"""文档口径与代码实态一致性护栏。

为什么需要这一族测试：本仓的文档一直领先或落后于代码而无人发现——
README 长期写"1854 passed"而实测早已 2200+；`docs/deployment.md` 教用户
`POST /tasks` 提交任务，但该路由只有 GET（`POST` 在 `/api/tasks`）；
`llm_router` 的模块注释写"10 个供应商"而定义表里是 16 条。
此前 `tests/` 里**没有任何一条**测试引用 README 或 docs，所以这些数字
只能靠人肉发现。本文件把三处最容易腐烂的口径改成从代码取数。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ─── 1. README 的测试数必须等于实际收集数 ───────────────────

def _readme_counts() -> tuple[int, int] | None:
    """README 那一行里的一对数：声明通过数 + 声明跳过数。

    跳过数也读出来，是因为它才是"允许差多少"的依据——容差写死成 10 的时候，
    `requires_pg` 那批环境门涨到 21 条，护栏就把一个**正确**的数字判成了过期。
    容差改成「README 自报的 skipped + 平台差」，两个数同源就不会各说各话。
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+)\s*个测试本机全绿\*\*（`\1 passed", text)
    if not m:
        return None
    passed = int(m.group(1))
    m2 = re.search(r"`\d+ passed, (\d+) skipped", text)
    if not m2:
        return None
    return passed, int(m2.group(1))


#: Linux CI 比 Windows 本机**多收集**的用例数（2026-10-28 在 CI 上实测：3208 vs 3200 = 8，
#: 多为 POSIX-only 的路径/权限类用例）。README 那个 passed 数是 Windows 实测值，
#: 所以在 Linux 上允许它和 collected 差出这 8 条；Windows 上不放宽（精确到 skipped）。
#: 2026-10-29 头（含 #78）上绝对数变了、这个差额没变：Windows 收集 3209 / skipped 24 /
#: passed 3185 ⇒ Linux 预期组合 3217，预算 24 + 8 = 32 恰好等于 3217 − 3185。这个"恰好"
#: 是有意的：Linux 侧能容忍的只有环境门 skip 与平台差，再多一条就该有人来解释。
#: 2026-11-07 复核订正：这个 8 如今只是**预算余量**，不是实测平台差。同一 SHA 在 CI 四条腿
#: 实测 Linux 收集 3717 / 选中 3711，比 Windows（3718 / 3712）**少 1**；差的那条已归因到
#: tests/test_html_backend.py:94 —— 它按环境里真能导入的 selectolax 内核动态参数化
#: （本机 modest + lexbor 两个、CI 只有 lexbor 一个）。常量不动：放宽方向无害，收紧反而会让
#: 这类环境波动假红；但别再把它读成"Linux 比 Windows 多收集 8 条 POSIX-only 用例"。
LINUX_COLLECT_DELTA = 8


@pytest.mark.slow
def test_readme_test_count_matches_collection():
    """README 声明的通过数 == 实际收集数（减去环境门 skipif 的那些）。

    数字来自 `pytest --co`，不是写死的常量——加测试时忘了改 README 会直接红。
    """
    counts = _readme_counts()
    assert counts is not None, "README 的测试数句式被改坏了，护栏读不到数字"
    declared, declared_skipped = counts

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--co", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode in (0, 5), f"收集失败：{proc.stdout[-800:]}{proc.stderr[-800:]}"
    tail = proc.stdout.strip().splitlines()[-1]
    m = re.search(r"(\d+)/(\d+)\s+tests? collected", tail)
    if m:
        collected = int(m.group(1))               # 已排除 deselected 的 e2e
    else:
        m2 = re.search(r"(\d+)\s+tests? collected", tail)
        assert m2, f"读不懂 --co 的汇总行：{tail!r}"
        collected = int(m2.group(1))

    # skipif 的用例被收集但不执行，所以 declared 允许比 collected 小的部分只能
    # 是这些 skip；上界 = README 自报的 skipped 数（Windows 侧精确），Linux 上再
    # 加上 LINUX_COLLECT_DELTA 的预算余量（8，见常量处 2026-11-07 的订正：今天实测是
    # Linux 少 1 条，不是多 8 条）。不写死一个"宽到不会红"的常量：环境门或平台差变了，
    # 这里就该有人来解释一句，而不是悄悄放进一个更大的数里。
    import sys as _sys

    platform_delta = 0 if _sys.platform.startswith("win") else LINUX_COLLECT_DELTA
    budget = declared_skipped + platform_delta
    assert 0 <= collected - declared <= budget, (
        f"README 写 {declared} passed / {declared_skipped} skipped，而实际收集 "
        f"{collected} 条；差值 {collected - declared} 超出「仅环境门 skip」的上界 "
        f"{budget}，请更新 README 的测试一节")


# ─── 2. 文档里教的 METHOD+路径必须真存在 ────────────────────

#: 规范端点本身不出现在它自己生成的 paths 里，改查 admin_api 源码是否真的处理它
SPEC_SELF = {"/api/openapi.json"}


def _spec_paths() -> dict:
    from loom_core.openapi_spec import generate_spec
    return generate_spec().get("paths", {})


def _served_by_admin(route: str) -> bool:
    src = (ROOT / "loom_core" / "admin_api.py").read_text(encoding="utf-8")
    return f'"{route}"' in src or f"'{route}'" in src


@pytest.mark.parametrize("doc", ["docs/deployment.md", "docs/api.md", "README.md"])
def test_documented_endpoints_exist(doc):
    """文档里反引号包住的 `METHOD /路径` 必须在实码里存在且支持该动词。

    只匹配反引号形式，是为了不把散文里的斜杠误当成路由。曾经的事故是
    `docs/deployment.md` 教用户用 POST 打 `/tasks`，而那条路由只有 GET。
    """
    paths = _spec_paths()
    text = (ROOT / doc).read_text(encoding="utf-8")
    refs = re.findall(r"`(GET|POST|DELETE)\s+(/[A-Za-z0-9_/{}.-]*)`", text)
    assert refs, f"{doc} 里没抓到任何端点引用，护栏需要改写法"

    missing = []
    for method, route in refs:
        if route in SPEC_SELF:
            if not _served_by_admin(route):
                missing.append(f"{method} {route}（admin_api 不再处理它）")
            continue
        norm = re.sub(r"<[^>]+>", "{x}", route)
        hit = norm if norm in paths else None
        if hit is None:
            missing.append(f"{method} {route}（规范里没有这个路径）")
        elif method.lower() not in {m.lower() for m in paths[hit]}:
            missing.append(f"{method} {route}（该路径只支持 {sorted(paths[hit])}）")
    assert not missing, f"{doc} 教了代码里不存在的端点：{missing}"


def test_tasks_is_get_only_and_docs_do_not_teach_post_tasks():
    """`/tasks` 只有 GET —— 文档不得把它写成提交任务的入口。

    写法上刻意不在 deployment.md 里留下反引号形式的 `POST /tasks`（连"别这么用"
    的提醒都不行），因为关键字扫描会命中这种自我引用；文档改用散文表述。
    """
    paths = _spec_paths()
    assert "post" not in {m.lower() for m in paths.get("/tasks", {})}, \
        "/tasks 若新增了 POST，这条护栏与 deployment.md 的说明都要同步改"
    deploy = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "`POST /tasks`" not in deploy
    assert "POST /api/tasks" in deploy, "提交任务的正确入口必须在文档里写明"


# ─── 3. LLM 供应商条数从代码取，不许文档写死 ─────────────────

def _provider_defs_length() -> int:
    tree = ast.parse((ROOT / "loom_core" / "llm_router.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Name) and tgt.id == "provider_defs"
                        and isinstance(node.value, ast.List)):
                    return len(node.value.elts)
    raise AssertionError("找不到 provider_defs，护栏口径需重写")


def test_llm_router_docstring_matches_provider_table():
    src = (ROOT / "loom_core" / "llm_router.py").read_text(encoding="utf-8")
    n = _provider_defs_length()
    docstring = ast.get_docstring(ast.parse(src)) or ""
    m = re.search(r"供应商定义表\s*(\d+)\s*家", docstring)
    assert m, "llm_router 模块注释里得有「供应商定义表 N 家」这句，护栏才有效"
    assert int(m.group(1)) == n, f"注释说 {m.group(1)} 家，定义表实际 {n} 条"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{n} 供应商定价表" in readme or f"{n} 供应商" in readme, \
        f"README 的供应商口径应与定义表一致（当前 {n}）"


# ─── 4. CI 的产出判据必须与门禁契约同源 ─────────────────────

def test_ci_step_carries_the_output_fidelity_checks():
    """`.github/workflows/ci.yml` 的 docgen 步骤必须自己带着两条产出判据。

    那份 21,926 字节的素材直粘文档就是从这一步以 `OUTPUT FIDELITY OK` 的名义过的，
    所以这一步不能只依赖 quality_gate：判据若从 ci.yml 里消失（或被注释掉），
    等于门禁单方面失效，这里要红。字符串取自 loom_doc.degradation，
    不在测试里重抄一份字面量。
    """
    from loom_doc import degradation

    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert degradation.EMPTY_RESULT_DOC in text, \
        "ci.yml 不再检查占位语，交付物是否空洞就没人兜底了"
    assert r"^下载时间: [0-9]{4}" in text, \
        "ci.yml 不再检查抓取层原始素材签名（degradation.FETCH_TIMESTAMP_RE 的等价式）"
    assert "OUTPUT FIDELITY OK" in text, "护栏与被检对象一并失效，先确认这一步还在"


def test_ci_type_check_covers_run_py():
    """mypy 步骤必须把 run.py 一起查——它是 CLI 入口，也是覆盖率口径的一部分。

    长期只在 py_compile 里露面的后果是实测攒下两条 `no-any-return` 没人发现。
    只认真正的 `mypy ...` 命令行（按 token 判）：YAML 块标量里的注释也是字符串，
    拿整段 `run:` 做子串匹配会被一句解释性注释骗过去。
    """
    import yaml

    ci = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    runs = [step.get("run", "") for job in ci["jobs"].values()
            for step in job.get("steps", []) if "mypy" in str(step.get("name", "")).lower()]
    assert runs, "ci.yml 里找不到 mypy 步骤——类型门整个没了"
    commands = [line.strip() for run in runs for line in str(run).splitlines()
                if line.strip().startswith("mypy ")]
    assert commands, f"mypy 步骤里已经没在跑 mypy：{runs!r}"
    for command in commands:
        assert "run.py" in command.split(), \
            f"mypy 命令没带上 run.py，CLI 入口又回到类型门之外：{command!r}"


# ─── 5. E2E Nightly 的触发实态与文档口径必须同步 ─────────────

def _nightly_triggers(text: str) -> set:
    """从 YAML 解析出真实触发集合。

    必须用解析器而不是 grep：定时块现在是被注释掉的形态留在文件里
    （留着是为了"重启只需取消注释"），文本搜索会把注释当成触发，
    于是这条护栏会永远自我安慰成"定时还在"。
    """
    import yaml

    doc = yaml.safe_load(text) or {}
    # YAML 1.1 把裸 `on` 解析成布尔 True，GitHub 的 `on:` 因此有两种键形态。
    on = doc.get("on", doc.get(True, {}))
    if isinstance(on, str):
        return {on}
    return set(on or {})


def test_nightly_trigger_reader_sees_both_shapes():
    """先证明读取函数本身活着：注释掉的 schedule 不算触发，真写的算。

    双向对照——若哪天有人只改了注释而没改实触发，读取函数必须给出不同结论。
    """
    scheduled = _nightly_triggers("on:\n  schedule:\n    - cron: '0 18 * * *'\n")
    assert scheduled == {"schedule"}, f"读取函数漏掉了真定时：{scheduled}"
    dispatch_only = _nightly_triggers("on:\n  workflow_dispatch:\n")
    assert dispatch_only == {"workflow_dispatch"}, dispatch_only
    commented = _nightly_triggers(
        "on:\n  workflow_dispatch:\n  # schedule:\n  #   - cron: '0 18 * * *'\n")
    assert "schedule" not in commented, f"注释被当成了触发：{commented}"


def test_nightly_disabled_state_is_disclosed_in_docs():
    """E2E Nightly 有没有定时，README 与 CONTRIBUTING 的说法必须跟文件一致。

    2026-10-08 停用的那晚，问题不在判据而在文档：README 只写了"未配 Secret 会全部
    skip"，读者据此以为夜间回归还在跑。反向也成立——真要恢复定时，
    把 schedule 加回来的同一天这两处文档就得改，否则这里红。
    """
    text = (ROOT / ".github" / "workflows" / "e2e-nightly.yml").read_text(encoding="utf-8")
    triggers = _nightly_triggers(text)
    assert "workflow_dispatch" in triggers, \
        "e2e-nightly.yml 连手动触发都没了，Secret 配好后无法先手动验证再恢复定时"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    discloses_disabled = "已于 2026-10-08 停用" in readme

    if "schedule" in triggers:
        assert not discloses_disabled, \
            "定时触发已恢复，README 却仍说 E2E Nightly 处于停用状态"
    else:
        assert discloses_disabled, \
            "E2E Nightly 已无定时触发，README「测试」一节必须写明停用日期与重启条件"
        assert "重启条件" in readme, "停用要连同怎么恢复一起写，否则等于永久放弃"
        row = [line for line in contributing.splitlines() if "e2e-nightly.yml" in line]
        assert row, "CONTRIBUTING 的工作流表里读不到 e2e-nightly.yml 这一行，护栏需重写"
        assert "停用" in row[0], \
            f"CONTRIBUTING 仍按「定时照跑」介绍 e2e-nightly：{row[0]}"



# ─── 6. SDK 文档：方法表与 _ROUTES 双向一致 + 可发现性 ─────────

SDK_DOC = ROOT / "docs" / "sdk.md"


def _sdk_doc_routes() -> dict[str, tuple[str, str]]:
    """`docs/sdk.md` 方法表里的 {方法: (METHOD, path)}。

    只认 `| `name` | GET | `/path` | ...` 这种表格行——表头与分隔行自然不匹配，
    所以解析器不需要"跳过第几行"这类会随排版漂移的规则。
    """
    rows: dict[str, tuple[str, str]] = {}
    if not SDK_DOC.exists():
        return rows
    for line in SDK_DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`(\w+)`\s*\|\s*([A-Z]+)\s*\|\s*`([^`]+)`\s*\|", line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def test_sdk_doc_method_table_matches_client_routes():
    """文档表 ↔ `LoomClient._ROUTES` 双向相等，且 method+path 逐条一致。

    这是「同源」承诺在本仓的第三处落点（另两处：`_ROUTES` ↔ OpenAPI spec 由
    `test_client_sdk.py` 守，spec ↔ docs 由本文件 §2 守）。只改一边都会红。
    """
    from loom_core.client import routes

    documented = _sdk_doc_routes()
    actual = routes()
    # 先挡掉"解析器什么都不认"：16 个方法至少得解析出 10 行
    assert len(documented) >= 10, f"只从 docs/sdk.md 解析出 {len(documented)} 行，表格格式或文件坏了"
    assert set(documented) == set(actual), (
        f"文档多写了 {sorted(set(documented) - set(actual))}，"
        f"漏写了 {sorted(set(actual) - set(documented))}")
    wrong = {k: {"doc": documented[k], "code": actual[k]}
             for k in actual if documented[k] != actual[k]}
    assert not wrong, f"方法与路由/HTTP 动词对不上：{wrong}"


def test_sdk_doc_is_discoverable_from_readme():
    """SDK 必须在 README 有入口、在 doc 里有真实用法——否则等于"没交付"。

    起因：`LoomClient` 随 v3.16.0 一起发版，但用户文档里零命中，集成方只能读源码。
    这条只判"有人能找到 + 文档真的在教用法"，不判文案好坏。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/sdk.md" in readme, "README 里没有指向 docs/sdk.md 的链接，SDK 又变回不可发现"
    # 光"出现过"不够：要出现在文档索引表里（导航位置），否则正文提一句也算通过
    assert re.search(r"\|\s*\[Python SDK\]\(docs/sdk\.md\)\s*\|", readme), \
        "README 的文档索引表里没有 [Python SDK](docs/sdk.md) 这一行——只在正文提一句不叫入口"
    text = SDK_DOC.read_text(encoding="utf-8")
    assert "from loom_core import LoomClient" in text, \
        "docs/sdk.md 里没有可复制的导入示例，这份文档不是在教用法"
    assert "pip install nexusloom" in text, "安装方式没写，读者不知道装什么才会有这个 SDK"
