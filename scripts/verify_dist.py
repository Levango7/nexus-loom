"""发布前对**产物**的校验（不是对源码）——包内容、流水线数据、依赖闭合、版本一致。

为什么单独一个脚本而不是"能 pip install 就行"：这些缺陷都不会让源码测试变红，
只在装出来的包上才现形，而现形时已经在用户机器上了。

- 内容对齐源码目录：新增模块忘了进包（packages/package-data 口径漂移）当场红；
- **流水线数据两处都在**：`pipelines/*.yaml|*.lock|quality|prompts` 与
  `scripts/checker_rules.yaml` 丢了，装出来的引擎"一条流水线都没有"
  （2026-10-09 真踩过，PR #13）；
- **依赖闭合**：产品源码 import 的第三方顶层包，要么在 `Requires-Dist`，
  要么在 `EXTERNAL` 登记表明文认领（`artesian` 未上 PyPI，写版本约束会让
  `pip install` 直接装不上——这是发布形态问题，登记表负责说明）；
- 版本三处一致：产物文件名、`pyproject.toml` 的 `project.version`、
  装好之后 `loom_core.__version__` 与包元数据版本。

用法：
    python scripts/verify_dist.py [dist目录] [期望版本] [--installed]
`--installed` 只有在把产物装进当前环境后才该加（发布流水线里由干净 venv 那步带上）。
"""
from __future__ import annotations

import ast
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 打进 wheel 的 Python 包（与 [tool.setuptools] packages 同源）
PACKAGES = ["loom_core", "loom_doc", "agents", "scripts", "artesian",
            "packs", "packs.docgen", "packs.docgen.agents",
            "docpipeline", "pipeline_core"]  # 兼容 shim（旧名转会别名，安装态仍可 import）


def pkg_rel_path(pkg: str) -> str:
    """点号包名 → 仓库/wheel 相对路径（两处都用斜杠）。

    2026-10-12 v3.10.0 首发实证：此前把 `packs.docgen` 直接当路径前缀查
    wheel 条目（`n.startswith(pkg + "/")`），嵌套包永远零命中——wheel 本身
    合规却被门禁误拦；同一处"点号当路径"也让依赖闭合扫描对嵌套包静默空转。
    """
    return pkg.replace(".", "/")


def packages_missing_init(names: set[str]) -> list[str]:
    """声明的包里，哪些在给定条目集合里缺少**自己那一层**的 `__init__.py`。

    按精确路径判（`packs/docgen/__init__.py`），不是前缀——前缀匹配会让
    子包的 `__init__.py`（`packs/docgen/agents/__init__.py`）冒充满足父包，
    父包文件缺失时门禁静默放行（与点号/斜杠是同一族"看着像在检查"的洞）。
    """
    missing: list[str] = []
    for pkg in PACKAGES:
        if pkg_rel_path(pkg) + "/__init__.py" not in names:
            missing.append(pkg)
    return missing

#: wheel 与 sdist 里都必须有的数据文件（相对仓库根）
REQUIRED_DATA = (
    "pipelines/api-report.yaml",
    "pipelines/api-report.lock",
    "packs/docgen/pipelines/docgen.yaml",
    "packs/docgen/pipelines/docgen.lock",
    "packs/docgen/quality/technical-doc.yaml",
    "packs/docgen/prompts/default.yaml",
    "packs/docgen/pack.yaml",
    "scripts/checker_rules.yaml",
    # 控制台随包发布（2026-10-20）：3.12.0 及以前的 wheel 里一个 dashboard
    # 资源都没有，pip 装出来的用户打开 / 只看到一行纯文本。
    "loom_core/dashboard/index.html",
    "loom_core/dashboard/app.js",
    "loom_core/dashboard/style.css",
)

#: 非 PyPI 分发的第三方依赖登记表：源码真的 import，但不以版本约束出现。
EXTERNAL: dict[str, str] = {
    "opentelemetry": (
        "可观测性（telemetry.py try-import，未装时降级 no-op）；"
        "可选依赖不进 Requires-Dist——要追踪时显式 pip install opentelemetry-sdk。"),
    "huggingface_hub": (
        "artesian.embeddings 的可选路径（try_to_load_from_cache 探测本地权重，"
        "未装时按无缓存处理）；sentence-transformers 传递依赖，不单列。"),
    "mineru": (
        "重型 OCR 解析（ingest.py 按需探测，未装时 PDF 走 pymupdf 路线）；"
        "GB 级依赖，与 paddleocr 同属 RUNTIME_PROBED，刻意不进 requirements。"),
    "safe_writer": (
        "scripts/layout_optimizer.py 独立入口经 sys.path 注入 scripts/ 后按裸模块名"
        " import 的第一方件（products 打包时 scripts 包内已有同名模块），非第三方。"),
}

#: 标准库补充名单（verify_dist 自己的口径；与 test_layering 的 _stdlib_names 不同源）：
#: py3.11+ 的 tomllib / 常见网络与运行时件。fcntl 是 POSIX-only 的标准库——
#: config.py 的跨进程文件锁用它做 flock，Windows 侧走 O_EXCL 自旋回退，
#: 不依赖任何第三方包，故登记在此而不是 EXTERNAL（EXTERNAL 是第三方件）。
STDLIB_EXTRA = frozenset({
    "atexit", "contextvars", "fcntl", "ipaddress", "socketserver", "struct",
    "tomllib", "zlib",
})


def fail(msg: str) -> None:
    raise SystemExit(f"[verify_dist] 失败：{msg}")


def pyproject_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver: str = data["project"]["version"]
    return ver


def check(dist: Path, want: str | None) -> str:
    ver = pyproject_version()
    if want is not None and want != ver:
        fail(f"tag 要发 {want}，但 pyproject 的 version 是 {ver}（先改 pyproject 再打 tag）")

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1:
        fail(f"期望 dist/ 里恰好 1 个 wheel，实际 {len(wheels)} 个：{[w.name for w in wheels]}")
    if len(sdists) != 1:
        fail(f"期望 dist/ 里恰好 1 个 sdist，实际 {len(sdists)} 个：{[s.name for s in sdists]}")
    wheel, sdist = wheels[0], sdists[0]
    for art in (wheel, sdist):
        if ver not in art.name:
            fail(f"产物文件名里没有 version {ver}：{art.name}")

    names = set(zipfile.ZipFile(wheel).namelist())
    # 包内容：每个声明的包里至少要有 __init__（新增子包忘了进 packages 当场红）
    missing = packages_missing_init(names)
    if missing:
        fail("wheel 里没有 " + "、".join(p + "/" for p in missing)
             + " 的任何 __init__.py——包清单与 pyproject 漂移")

    # 流水线数据两处都在（wheel + sdist）：丢了它，装出来的引擎没有流水线
    with tarfile.open(sdist) as t:
        members = [m.name for m in t.getmembers() if m.isfile()]

    def has_wheel(rel: str) -> bool:
        return any(n == rel or n.endswith("/" + rel) for n in names)

    def has_sdist(rel: str) -> bool:
        return any(m.endswith(rel) for m in members)

    for rel in REQUIRED_DATA:
        if not has_wheel(rel):
            fail(f"wheel 里缺数据文件 {rel}（装出来的引擎会没有流水线，PR #13 真踩过）")
        if not has_sdist(rel):
            fail(f"sdist 里缺数据文件 {rel}")

    for req in ("PKG-INFO", "README.md", "pyproject.toml"):
        if not has_sdist(req):
            fail(f"sdist 里没有 {req}（发布页说明与许可就来自这些文件）")
    return ver


def check_dependency_closure() -> None:
    """源码 import 的第三方顶层包必须要么在 Requires-Dist，要么在 EXTERNAL。"""
    import importlib.metadata as _im

    declared: set[str] = set()
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"].get("dependencies", []))
    for group in data["project"].get("optional-dependencies", {}).values():
        specs.extend(group)
    for spec in specs:
        head = re.split(r"[<>=!;\[ ]", spec)[0].strip()
        if head:
            try:
                declared.add(_im.metadata(head)["Name"].lower().replace("-", "_"))
            except Exception:
                declared.add(head.lower().replace("-", "_"))

    found: set[str] = set()
    for pkg in PACKAGES:
        for py in (REPO / pkg_rel_path(pkg)).rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        found.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call):
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name in ("import_module", "__import__") and node.args:
                        first = node.args[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            found.add(first.value.split(".")[0])

    STDLIB = {
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "concurrent",
        "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime", "decimal",
        "difflib", "enum", "errno", "functools", "glob", "hashlib", "hmac", "html",
        "http", "importlib", "inspect", "io", "itertools", "json", "logging",
        "math", "mimetypes", "multiprocessing", "os", "pathlib", "pickle", "platform",
        "pprint", "queue", "random", "re", "select", "secrets", "shlex", "shutil", "signal",
        "socket", "sqlite3", "stat", "string", "subprocess", "sys", "tarfile",
        "tempfile", "textwrap", "threading", "time", "tokenize", "traceback", "types",
        "typing", "unicodedata", "unittest", "urllib", "uuid", "warnings",
        "weakref", "xml", "zipfile", "zoneinfo", "__future__",
    }
    import_to_dist = {
        "yaml": "pyyaml", "docx": "python_docx", "pptx": "python_pptx",
        "fitz": "pymupdf", "duckduckgo_search": "duckduckgo_search",
    }
    unclaimed = []
    for mod in sorted(found):
        if mod in STDLIB or mod in STDLIB_EXTRA or mod in PACKAGES or mod in EXTERNAL:
            continue
        dist_name = import_to_dist.get(mod, mod).lower().replace("-", "_")
        if dist_name in declared:
            continue
        unclaimed.append(mod)
    if unclaimed:
        fail("源码 import 了未声明的第三方包（要么进 pyproject 依赖，"
             "要么在 EXTERNAL 登记表说明）：" + ", ".join(unclaimed))


def check_installed(ver: str) -> None:
    import importlib.metadata

    import loom_core
    if getattr(loom_core, "__version__", None) != ver:
        fail(f"装出来的 loom_core.__version__="
             f"{getattr(loom_core, '__version__', None)} 与 pyproject 的 {ver} 不符")
    # 旧名 shim：安装态旧 import 必须命中**同一模块对象**（MAOP 零改动承诺）
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore", DeprecationWarning)
        import pipeline_core.scheduler as _legacy_sched  # noqa: F401

        import pipeline_core as _legacy
    if _legacy is not loom_core:
        fail("安装态的 pipeline_core shim 与 loom_core 不是同一模块对象")
    meta = importlib.metadata.version("nexusloom")
    if meta != ver:
        fail(f"包元数据版本 {meta} 与 pyproject 的 {ver} 不符")
    # 安装态冒烟：流水线清册非空（wheel 数据文件的最终判据）
    from loom_core.pack_manifest import discover
    packs = [p for p in discover() if p.source in ("builtin", "pack")]
    if not packs:
        fail("装出来的包里流水线清册为空——pipelines/ 数据没有随 wheel 走（PR #13 真踩过）")
    docgen = next((p for p in packs if p.name == "docgen" and p.is_physical), None)
    if docgen is None:
        fail("装出来的包里没有物理 pack docgen——packs/ 数据没有随 wheel 走")


def main(argv: list[str]) -> int:
    installed = "--installed" in argv
    pos = [a for a in argv[1:] if not a.startswith("--")]
    dist = Path(pos[0]) if pos else REPO / "dist"
    # 空串要当"没给"：CI 里 dispatch 没有 tag，`${WANT}` 展开成空参数——
    # 当成给了就会报"tag 要发 ，但 pyproject 是 0.1.0"（本机测时传了显式版本，
    # 正好绕过这条，第一次在 runner 上跑才现形）。
    want: str | None = re.sub(r"^v", "", pos[1]) if len(pos) > 1 else ""
    want = want or None
    if not dist.is_dir():
        fail(f"dist 目录不存在：{dist}（先跑 python -m build）")

    ver = check(dist, want)
    check_dependency_closure()
    if installed:
        check_installed(ver)
    print(f"[verify_dist] OK：wheel/sdist 内容与数据齐全、依赖闭合、版本={ver}"
          + ("；安装态三项一致" if installed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
