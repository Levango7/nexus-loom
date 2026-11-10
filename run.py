#!/usr/bin/env python3
"""
run.py - Agent 工作流引擎入口 v3.1
===============================
动词（spec §5.3）：`run / plan / lock / pack / plugin`——
  loom run [input]     执行流水线（缺省动词：`loom input.md` 即 run）
  loom plan [name]     只预览执行计划，不执行
  loom lock [name]     生成/刷新流水线锁文件后退出
  loom pack list|check|new pack 清册 / 启动自检 / 生成新 pack 骨架
  loom plugin list     列出全部 Agent / 插件（含来源）
其余服务/运维开关（--check/--worker/--mcp/--admin/--triggers/--recover）保持为 flag。

核心特性：
  - 支持声明式流水线配置 (YAML) via Scheduler + run_plan
  - DAG 并行执行 + 自动重做循环 + 熔断器 + 指数退避重试
  - 完整审计日志 + 事务 checkpoint + 临时文件自动清理
"""
import argparse
import contextlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))


# ─── 优雅退出：SIGTERM (Docker/systemd) → 触发 KeyboardInterrupt ───
# 注意：handler 注册移入 main()，避免 import run 副作用（测试时 import 会劫持 SIGTERM）
def _sigterm_handler(signum, frame):
    raise KeyboardInterrupt


def _install_sigterm_handler():
    """注册 SIGTERM 处理器（仅在 main 中调用，避免 import 副作用）"""
    # 非主线程或非主解释器（如 worker 进程）无法注册信号，忽略
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _sigterm_handler)


def _load_dotenv():
    """从项目根目录 .env 文件加载环境变量（不覆盖已存在的）"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

from loom_core import PipelineOrchestrator, TaskStatus, __version__  # noqa: E402
from loom_core.ids import new_task_id  # noqa: E402

#: `--json-output` 期间的真实机器读通道。默认 None 表示"用当下的 sys.stdout"——
#: 不能直接在导入期绑死 `sys.stdout`，否则测试里 capsys 之类替换后就读不到输出。
_RESULT_OUT = None


def print_banner():
    # 走 stderr：stdout 是机器读的通道 —— `--mcp` 在上面跑 JSON-RPC 帧，
    # `--json-output` 让 wrapper 解析单行 JSON。banner 落 stdout 时实测会
    # 排在 initialize 响应之前，等于往协议通道里塞非 JSON 行。
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║          Loom v{__version__} - Agent 工作流引擎                     ║
║          声明式 DAG | 自动重做 | 熔断器 | 审计日志             ║
╚══════════════════════════════════════════════════════════════╝
""", file=sys.stderr)


def output_json_result(task, output_path, steps, status):
    """输出 JSON 结果供 wrapper 解析"""
    result = {
        "exit_code": 0 if task.status == TaskStatus.DONE else 1,
        "stdout": "",
        "stderr": task.error or "",
        "output_path": output_path,
        "status": status,
        "steps": steps
    }
    # 写 _RESULT_OUT 而不是 sys.stdout：json_output 模式下 sys.stdout 已被改接到
    # stderr（人类可读的过程输出不许混进 wrapper 要解析的那条通道）。
    target = _RESULT_OUT if _RESULT_OUT is not None else sys.stdout
    print(json.dumps(result, ensure_ascii=False), file=target)
    target.flush()


def _run_ascii_fix(output_path: str):
    """将文档中的 ASCII 图转换为 Mermaid 图"""
    try:
        from scripts.convert_ascii import AsciiConverter
        converter = AsciiConverter()
        text = Path(output_path).read_text(encoding="utf-8")
        converted = converter.detect_and_convert(text)
        if converted != text:
            Path(output_path).write_text(converted, encoding="utf-8")
            print(f"\n[ascii] ASCII 图已转换为 Mermaid，保存至: {output_path}")
        else:
            print("\n[ascii] 未检测到 ASCII 图")
    except Exception as e:
        print(f"\n[ascii] ASCII 转换失败: {e}")


def _run_export(md_path: str, fmt: str, export_path: str = None):
    """导出 Markdown 为其他格式"""
    try:
        from scripts.format_converter import FormatConverter
        converter = FormatConverter()
        if fmt == "html":
            out = export_path or md_path.replace(".md", ".html")
            converter.markdown_to_html(md_path, out)
            print(f"\n[export] HTML 已导出: {out}")
        elif fmt == "word":
            out = export_path or md_path.replace(".md", ".docx")
            converter.markdown_to_word(md_path, out)
            print(f"\n[export] Word 已导出: {out}")
        elif fmt == "png":
            out_dir = export_path or Path(md_path).parent / "images"
            converter.render_mermaid_in_markdown(md_path, str(out_dir))
            print(f"\n[export] Mermaid 图片已渲染至: {out_dir}")
    except Exception as e:
        print(f"\n[export] 导出失败: {e}")


def _get_orchestrator(project_root: Path) -> PipelineOrchestrator:
    """创建并注册 Agent 的编排器实例"""
    orch = PipelineOrchestrator(
        agents_dir=str(project_root / "agents"),
        checkpoint_dir=str(project_root / "checkpoints"),
    )
    return orch


def _load_config(args_args: argparse.Namespace, project_root: Path) -> dict:
    """加载项目配置：优先 --config，其次 config.json"""
    config_path = args_args.config or (project_root / "config.json")
    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


# 进程内缓存：流水线 YAML 集合在运行期视为静态（变更需重启进程），
# 避免每次参数解析/错误提示重复扫描 pipelines/ 目录
_PIPELINE_NAMES_CACHE: list[str] | None = None


def _pipelines_dir() -> Path:
    """流水线 YAML 根目录（经 loom_core.paths 收口）。

    模块级函数而非 import 期常量：安装态（wheel data-files）与开发态
    解析结果不同，且测试可通过 patch 它替换目录。"""
    from loom_core.paths import pipelines_dir as _dir

    return Path(_dir())


def _pack_pipeline_dirs() -> list[Path]:
    """物理 pack 的流水线目录（可 patch 缝隙；默认 pack 清单发现）。"""
    from loom_core.pack_manifest import physical_packs

    return [p.pipelines_dir for p in physical_packs()
            if p.pipelines_dir and p.pipelines_dir.is_dir()]


def _pipeline_names_from_dirs() -> list[str]:
    """核心 pipelines/ + 物理 pack 的 pipelines/ 全量候选（去重排序）。"""
    names: set[str] = set()
    core = _pipelines_dir()
    if core.is_dir():
        names.update(p.stem for p in core.glob("*.yaml")
                     if not p.name.startswith("_"))
    for d in _pack_pipeline_dirs():
        names.update(p.stem for p in d.glob("*.yaml")
                     if not p.name.startswith("_"))
    return sorted(names)


def _available_pipeline_names() -> list[str]:
    """列出 pipelines/ 目录下可用的流水线名（yaml 文件 stem）。

    结果进程内缓存一次，返回副本防止调用方修改污染缓存。
    """
    global _PIPELINE_NAMES_CACHE
    if _PIPELINE_NAMES_CACHE is not None:
        return list(_PIPELINE_NAMES_CACHE)
    # `_` 前缀是内部片段（子流水线 tail），只能被 call 引用：它们的输入是上游
    # 节点的产物，单独跑没有意义，所以不进候选清单。口径与
    # scheduler.installed_pipelines / Scheduler.list_pipelines 保持一致
    # （核心 pipelines/ + 物理 pack 的 pipelines/ 全量 union）。
    _PIPELINE_NAMES_CACHE = _pipeline_names_from_dirs()
    return list(_PIPELINE_NAMES_CACHE)


def _resolve_pipeline_files(pipeline: str, pipeline_file: str | None,
                            pipelines_dir: Path) -> list[Path]:
    """按名字解析出候选流水线 YAML 文件。

    精确名优先：`docgen` 必须命中 `docgen.yaml`。此前用
    `glob(f"{pipeline}*.yaml")` 前缀匹配 + 首个可解析即返回，按字母序
    `docgen-render.yaml` 排在 `docgen.yaml` 之前，导致默认流水线名永远取不到
    自己的定义文件。仅在不存在精确同名文件时才允许前缀匹配，且多义不再择一。
    """
    if pipeline_file:
        return [Path(pipeline_file)]
    for d in [pipelines_dir, *_pack_pipeline_dirs()]:
        exact = d / f"{pipeline}.yaml"
        if exact.exists():
            return [exact]
    matches: list[Path] = []
    seen: set[str] = set()
    for d in [pipelines_dir, *_pack_pipeline_dirs()]:
        for hit in sorted(d.glob(f"{pipeline}*.yaml")):
            if str(hit) not in seen:
                matches.append(hit)
                seen.add(str(hit))
    return matches


def _resolve_pipeline_plan(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                           config: dict) -> tuple[Any, bool]:
    """尝试从 YAML 文件解析 pipeline plan；失败时返回 (None, False) 表示应走 legacy 路径"""
    from loom_core.scheduler import LockfileMismatchError, Scheduler
    sched = Scheduler()
    pipeline_files = _resolve_pipeline_files(
        args_args.pipeline, args_args.pipeline_file, _pipelines_dir())
    if not pipeline_files:
        available = ", ".join(_available_pipeline_names())
        print(f"[run] ERROR: 未找到流水线 '{args_args.pipeline}'"
              f"（pipelines/ 下可用: {available or '无'}）", file=sys.stderr)
        sys.exit(2)
    if len(pipeline_files) > 1:
        candidates = ", ".join(pf.name for pf in pipeline_files)
        print(f"[run] ERROR: 流水线名 '{args_args.pipeline}' 有 {len(pipeline_files)} 个候选"
              f"（{candidates}），已拒绝猜测", file=sys.stderr)
        print("[run] 提示: 使用完整流水线名，或用 --pipeline-file 指定具体文件",
              file=sys.stderr)
        sys.exit(2)

    # --diff 要绕过锁校验（parse_file 不抛 LockfileMismatchError），
    # 但不能写锁文件——本 flag 的目的是报告漂移，不是生成锁。
    write_lock = bool(getattr(args_args, "write_lock", False))
    skip_lock_verify = write_lock or bool(getattr(args_args, "diff", False))
    for pf in pipeline_files:
        if pf.exists():
            try:
                plan = sched.parse_file(str(pf), verify_lock=not skip_lock_verify)
            except LockfileMismatchError as e:
                print(f"[run] ERROR: {pf.name} 与版本锁定不一致，已阻止执行:\n{e}", file=sys.stderr)
                print("[run] 提示: 若配置变更是有意的，使用 --write-lock 重新生成 lockfile 后再运行",
                      file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"[run] 加载 {pf.name} 失败: {e}")
                continue
            print(f"[run] 加载流水线配置: {pf.name}")
            if write_lock:
                # 锁写到解析出的 YAML 同目录（pack 流水线的锁进 packs/<name>/pipelines/）
                lock_path = sched.generate_lockfile(plan, output_dir=str(pf.parent))
                agent_count = len({n.agent_name.split("_pool_")[0]
                                   for level in plan.levels for n in level})
                print(f"[run] 已写入版本锁定: {lock_path}"
                      f"（pipeline={plan.pipeline_name}, agents={agent_count}, nodes={plan.node_count}）")
            return (plan, True)
    return (None, False)


_TASK_EXIT_CODES = {"failed": 1, "cancelled": 2}


def _task_exit_code(task) -> int:
    """按任务终态映射进程退出码：failed=1、cancelled=2、其余=0"""
    try:
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
    except Exception:
        return 0
    return _TASK_EXIT_CODES.get(str(status).lower(), 0)


def _print_task_summary(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                        task_id: str, use_legacy: bool,
                        input_file: str | None = None) -> None:
    """打印任务摘要横幅"""
    print(f"\n{'='*60}")
    print(f"任务: {task_id}")
    print(f"流水线: {args_args.pipeline}")
    # 类型化输入的载荷与渲染出的文档都要看得见：只印 "输入: None" 的横幅让人没法复盘
    if args_args.inputs_json or args_args.set_input:
        print(f"输入: {input_file}")
        print(f"类型化输入: {_cli_typed_inputs(args_args)}")
    else:
        print(f"输入: {args_args.input}")
    print(f"模式: {'声明式 DAG' if not use_legacy else 'Legacy'}")
    print(f"Agent: {', '.join(orch.registry.list_agent_names())}")
    if args_args.queries:
        print(f"查询: {args_args.queries}")
    if args_args.resume:
        print("模式: 断点续传")
    print(f"{'='*60}\n")


def _poll_task_progress(orch: PipelineOrchestrator, args_args: argparse.Namespace,
                        task, task_id: str) -> bool:
    """轮询任务进度直到结束。返回 False 表示被中断（已暂停/进入守护等待）。

    中断且 --daemon 时保持进程存活（Admin API 常驻），再次 Ctrl+C 退出。
    """
    import time as time_module
    try:
        from loom_core import TaskStatus
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
            time_module.sleep(1)
            print(f"[run] 进度: {task.progress}%  状态: {task.status.value}")
    except KeyboardInterrupt:
        print("\n\n[run] 收到中断信号，正在暂停任务...")
        orch.pause(task_id)
        print("[run] 任务已暂停，可使用 --resume 续传")
        if args_args.daemon:
            print("\n[run] 守护进程模式激活，按 Ctrl+C 再次退出")
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time_module.sleep(10)
        return False
    return True


def _result_for(task, agent: str) -> dict:
    """按 Agent 名取节点结果，兼容内联别名（`quality_gate__tail`）。

    报表里的告警是按 Agent 语义找的；写死节点名会在子流水线内联后静默失效
    ——质量分不达标的警告就此消失，用户只看到"执行完成"。
    同名出现多次时不猜：返回空，宁可缺警告也不报另一个实例的分数。
    """
    from loom_core.naming import agent_of

    results = getattr(task, "result", None) or {}
    direct = results.get(agent)
    if isinstance(direct, dict):
        return direct
    matches = [v for k, v in results.items() if agent_of(str(k)) == agent
               and isinstance(v, dict)]
    return matches[0] if len(matches) == 1 else {}


def _collect_steps(task) -> list[dict]:
    """将 StepResult 列表转为可 JSON 序列化的 dict 列表"""
    steps = []
    if task.steps:
        for step in task.steps:
            steps.append({
                "step_name": step.step_name,
                "agent_name": step.agent_name,
                "status": step.status,
                "duration_ms": step.duration_ms,
                "started_at": step.started_at,
                "finished_at": step.finished_at,
                "result": step.result if hasattr(step, 'result') else {}
            })
    return steps


def _resolve_output_path(args_args: argparse.Namespace, task) -> str:
    """确定输出文档路径：CLI 指定 > 任务结果 > 任务属性"""
    if args_args.output and Path(args_args.output).exists():
        return str(args_args.output)
    if task.result and isinstance(task.result, dict) and task.result.get("output_path"):
        return str(task.result["output_path"])
    if hasattr(task, 'output_path') and task.output_path:
        return str(task.output_path)
    return ""


def _render_task_result(args_args: argparse.Namespace, task, task_id: str) -> None:
    """渲染最终结果：JSON 输出或人类可读报告 + ASCII 修复 + 格式导出"""
    base_dir = Path(__file__).parent

    steps = _collect_steps(task)
    output_path = _resolve_output_path(args_args, task)

    if args_args.json_output:
        output_json_result(task, output_path, steps, task.status.value)
        return

    print(f"\n{'='*60}")
    print(f"流水线执行完成 | 状态: {task.status.value}")
    if task.finished_at and task.started_at:
        with contextlib.suppress(TypeError, ValueError):
            duration = float(task.finished_at) - float(task.started_at)
            print(f"耗时: {duration:.1f}s")

    if task.steps:
        print("\n执行步骤:")
        for step in task.steps:
            status_icon = "✅" if step.status == "success" else ("❌" if step.status == "failed" else "⏭️")
            print(f"  {status_icon} {step.agent_name:20s} {step.duration_ms:8.1f}ms")

    if task.error:
        print(f"\n错误: {task.error}")

    # 降级警告：writer 报告了内容不足的章节时，stderr 显式提示
    writer_result = _result_for(task, "writer")
    empty_sections = (writer_result.get("stats") or {}).get("empty_sections") or []
    if empty_sections:
        print(f"\n⚠️ WARNING: {len(empty_sections)} 个章节内容不足（降级占位）："
              f"{'、'.join(empty_sections)}；建议配置 LLM API Key 后重新生成",
              file=sys.stderr)

    # 质量门控警告：重做耗尽仍不达标时任务仍为 DONE，必须在此显式呈现，
    # 否则用户拿到低分文档却只看到"执行完成"（此前仅存在于日志）
    # 取法与 writer 同理走 _result_for：docgen 系列的 gate 现在在片段里，
    # 结果键是 `quality_gate__quality_tail`，直接 .get("quality_gate") 会静默拿空。
    qg_result = _result_for(task, "quality_gate")
    if isinstance(qg_result, dict) and qg_result.get("status") == "accepted_with_warnings":
        print(f"\n⚠️ WARNING: 质量门控经 {qg_result.get('generation_count', '?')} 轮重做后仍未达标"
              f"（得分 {qg_result.get('overall_score', '?')}，低于阈值），"
              f"文档以带警告状态发布。各维度得分: {qg_result.get('scores', {})}",
              file=sys.stderr)

    print(f"{'='*60}")

    if args_args.report:
        report_file = base_dir / "checkpoints" / f"report_{task_id}.json"
        if report_file.exists():
            print(f"\n详细报告已保存: {report_file}")

    if args_args.fix_ascii and output_path:
        _run_ascii_fix(output_path)

    if args_args.export and output_path:
        _run_export(output_path, args_args.export, args_args.export_output)


def _cli_typed_inputs(args_args: argparse.Namespace) -> dict:
    """把 `--inputs-json` / `--set-input` 合成一份载荷对象。

    这里**只解析不校验**：校验只有一条实现（`input_schema`），CLI 再造一套判据必然会和
    HTTP/MCP 漂移。`--set-input` 的值先按 JSON 解析，失败当字符串——`depth=3` 要落成
    整数，否则声明 `type: int` 的字段永远过不了门。
    """
    inputs: dict = {}
    if args_args.inputs_json:
        try:
            loaded = json.loads(args_args.inputs_json)
        except ValueError as e:
            print(f"[run] ✗ --inputs-json 不是合法 JSON: {e}")
            sys.exit(2)
        if not isinstance(loaded, dict):
            print("[run] ✗ --inputs-json 必须是 JSON 对象（键→值）")
            sys.exit(2)
        inputs.update(loaded)
    for pair in args_args.set_input or []:
        if "=" not in pair:
            print(f"[run] ✗ --set-input 需要 K=V 形式，实际 {pair!r}")
            sys.exit(2)
        key, _, raw = pair.partition("=")
        if not key.strip():
            print(f"[run] ✗ --set-input 的键为空: {pair!r}")
            sys.exit(2)
        try:
            inputs[key.strip()] = json.loads(raw)
        except ValueError:
            inputs[key.strip()] = raw
    return inputs


def _resolve_cli_input(args_args: argparse.Namespace, plan, task_id: str) -> str:
    """CLI 提交口的闸门，返回实际使用的 input_file 路径；没给输入文档时返回空串。

    空串是引擎约定的"无输入"形状（`run_plan(input_file="")` 即此意），
    `argparse` 的位置参数默认是 None，这里归一成空串再交出去。

    两种输入形状过同一个闸门口子（与 HTTP/MCP 同一实现）：typed 载荷走 `prepare_inputs`
    （逐字段校验 + 填默认值 + 渲染），位置参数 `input` 那份自由文本走 `gate_freeform`
    ——流水线一旦声明了类型化输入就拒绝，否则 CLI 会留一条绕过门的路。
    """
    from loom_core.input_schema import (
        InputValidationError,
        gate_freeform,
        prepare_inputs,
    )

    typed = bool(args_args.inputs_json or args_args.set_input)
    try:
        if typed:
            if args_args.input:
                print("[run] ✗ --inputs-json/--set-input 与位置参数 input 二选一")
                sys.exit(2)
            text = prepare_inputs(plan, _cli_typed_inputs(args_args),
                                  title=plan.pipeline_name)
            # 与 /api/tasks、MCP 同一形状：载荷渲染成临时 markdown 文档再交给引擎
            input_file = Path(tempfile.gettempdir()) / f"cli_{task_id}.md"
            input_file.write_text(text, encoding="utf-8")
            return str(input_file)
        gate_freeform(plan, surface="loom 的位置参数 input（输入文档）")
    except InputValidationError as e:
        print(f"[run] ✗ {e}")
        sys.exit(2)
    freeform_input = args_args.input
    if not freeform_input:
        return ""
    return str(freeform_input)


def _run_single_task(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                     config: dict):
    """执行单任务：构建 plan/task、等待结果、输出报告。返回终态 task（预览/中断返回 None）"""
    task_id = args_args.task_id or new_task_id()

    use_legacy = args_args.legacy
    plan = None

    if not use_legacy:
        plan, plan_loaded = _resolve_pipeline_plan(args_args, orch, config)
        if not plan_loaded:
            use_legacy = True

    # 预览模式
    if args_args.plan:
        if args_args.diff:
            # 锁指纹差异（A5）：比对当前流水线与 .lock 文件，输出人话差异项
            if plan:
                from pathlib import Path

                from loom_core.scheduler import Scheduler
                sched = Scheduler()
                # --pipeline-file 时，锁文件在同目录；按名解析时锁在 pipelines/
                source_dir = None
                if getattr(args_args, "pipeline_file", None):
                    source_dir = Path(args_args.pipeline_file).parent
                drift = sched.lock_drift(plan, source_dir)
                if not drift["lock_present"]:
                    print(f"[plan --diff] 无锁文件: {drift['lock_file']}")
                    print("  用 `loom lock <name>` 生成锁文件后再比对。")
                elif not drift["issues"]:
                    print(f"[plan --diff] 锁一致，无漂移（{drift['lock_file']}）")
                else:
                    print(f"[plan --diff] 检测到 {len(drift['issues'])} 项漂移：")
                    for issue in drift["issues"]:
                        print(f"  - {issue}")
                    print(f"  锁文件: {drift['lock_file']}")
            else:
                print("[plan --diff] ✗ 需要声明式流水线才能比对锁指纹："
                      "请检查 --pipeline / pipelines/ 配置")
                sys.exit(2)
            return
        if plan:
            from loom_core.scheduler import Scheduler
            sched = Scheduler()
            print(sched.visualize(plan))
        else:
            try:
                plan_preview = orch.plan(args_args.pipeline, args_args.input, config)
                print(orch.visualize_plan(plan_preview))
            except Exception as e:
                print(f"[run] ✗ 无法生成预览: {e}")
                sys.exit(1)
        return

    # 输入闸门（P1 #7）：typed 载荷经校验后渲染成临时输入文档；位置参数 input 那份
    # 自由文本在流水线声明了 inputs 时会被拒。legacy 回退时没有 plan，也就无源可校验——
    # 明确报错，不能"校验不了就照跑"（那等于闸门只在顺利的时候存在）。
    if use_legacy and (args_args.inputs_json or args_args.set_input):
        print("[run] ✗ 类型化输入需要声明式流水线（拿到了 legacy 回退）："
              "请检查 --pipeline / pipelines/ 配置，或去掉 --legacy")
        sys.exit(2)
    input_file = (args_args.input if use_legacy
                  else _resolve_cli_input(args_args, plan, task_id))

    _print_task_summary(args_args, orch, task_id, use_legacy, input_file)

    run_config = {
        "timeout": args_args.timeout,
        "output": args_args.output,
        "queries": args_args.queries or [],
        **config
    }

    if use_legacy:
        task = orch.run(
            task_id=task_id,
            pipeline_name=args_args.pipeline,
            input_file=input_file,
            config=run_config,
            wait=True,
            resume=args_args.resume,
        )
    else:
        if plan is None:
            print(f"[run] ✗ 无法加载流水线配置（pipeline={args_args.pipeline}）")
            print("[run] 请检查 pipelines/ 目录或 --pipeline-file 指定的 YAML 文件")
            print("[run] 或使用 --legacy 模式绕过 YAML 配置")
            sys.exit(1)
        if args_args.output:
            plan.raw.setdefault("pipeline", {})["output"] = args_args.output
        task = orch.run_plan(
            plan=plan,
            input_file=input_file,
            task_id=task_id,
            wait=True,
            resume=args_args.resume,
        )

    if not _poll_task_progress(orch, args_args, task, task_id):
        return None

    _render_task_result(args_args, task, task_id)
    return task


def _daemon_gc_due(orch: PipelineOrchestrator, last_gc: float,
                   now: float | None = None) -> float:
    """服务模式的一次回收检查：到点就跑一次 GC，返回新的"上次回收"时刻。

    抽成函数是为了能判据它，而不是只能靠 `sleep` 观察：两个分支（到点/没到点）
    与"显式关掉"（`maintenance.gc_seconds = 0`）都要能被单测直接驱动。
    """
    import time as _time

    from loom_core.maintenance import gc_from_orchestrator, gc_interval_seconds

    stamp = _time.time() if now is None else now
    interval = gc_interval_seconds(getattr(orch, "config", None))
    if not interval or stamp - last_gc < interval:
        return last_gc
    try:
        gc_from_orchestrator(orch, log=print)
    except Exception as e:                   # noqa: BLE001 —— 回收失败不该杀掉常驻服务
        print(f"[gc] 失败（常驻服务继续）：{e}")
    return stamp


def _run_daemon(orch: PipelineOrchestrator) -> None:
    """守护进程模式：保持 Admin API 常驻直到收到 KeyboardInterrupt。

    服务模式没有 worker 的轮询 tick，回收只能按时间计（`maintenance.gc_seconds`，
    默认 600 秒、0 = 不跑）。不接这一口的话，"只跑 `--admin` / `--dashboard` /
    `--daemon` 的部署"里保留期配置等于摆设——worker 循环根本不在这条路径上。
    """
    print("\n[run] 守护进程模式 — 按 Ctrl+C 退出")
    with contextlib.suppress(KeyboardInterrupt):
        import time
        last_gc = time.time()
        while True:
            time.sleep(10)
            last_gc = _daemon_gc_due(orch, last_gc)


# ─── CLI 动词收敛（spec §7.3 第 7 步 / §5.3 CLI 行）─────────────
#: 七个动词：run / plan / lock / pack / plugin / demo / reconcile
#（demo = A4 离线体验；reconcile = A7 只读对账报告）
VERBS = ("run", "plan", "lock", "pack", "plugin", "demo", "reconcile")

_PACK_ACTIONS = {"list": "pack_list", "check": "pack_check", "new": "pack_new"}


def _split_verb(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """拆分动词前缀，返回 (verb, action, rest)。

    action ∈ {None, "plan", "lock", "pack_list", "pack_check", "plugin_list",
    "demo", "reconcile"}。首个 token 不在动词表里 → (None, None, argv)：**裸输入 = 隐式
    run**，既有 `python run.py input.md …` 的调用形态全部照旧。

    `loom plan docgen` / `loom lock docgen` 里紧随动词的裸词 = 流水线名，
    翻译为 `--pipeline <name>`（与 flag 形态等价）。
    """
    if not argv or argv[0] not in VERBS:
        return None, None, argv
    verb, rest = argv[0], argv[1:]
    if verb == "pack":
        sub = rest[0] if rest and not rest[0].startswith("-") else ""
        action = _PACK_ACTIONS.get(sub)
        if action is None:
            print("用法: loom pack list|check|new <name>", file=sys.stderr)
            raise SystemExit(2)
        return verb, action, rest[1:]
    if verb == "plugin":
        sub = rest[0] if rest and not rest[0].startswith("-") else ""
        if sub != "list":
            print("用法: loom plugin list", file=sys.stderr)
            raise SystemExit(2)
        return verb, "plugin_list", rest[1:]
    if verb == "demo":
        if rest:
            print("用法: loom demo（demo 不接受参数；沙盒建在 cwd 下）",
                  file=sys.stderr)
            raise SystemExit(2)
        return verb, "demo", []
    if verb == "reconcile":
        if rest and not rest[0].startswith("-"):
            # 只拒位置参数；--stale-seconds / --json-output 等 flag 留给 argparse
            print("用法: loom reconcile（只读对账报告，不接受位置参数）",
                  file=sys.stderr)
            raise SystemExit(2)
        return verb, "reconcile", rest
    if verb in ("plan", "lock"):
        if rest and not rest[0].startswith("-"):
            return verb, verb, ["--pipeline", rest[0], *rest[1:]]
        return verb, verb, rest
    return verb, None, rest


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器（含输入校验规则）"""
    parser = argparse.ArgumentParser(
        description=f"Agent 工作流引擎 v{__version__} - 声明式 DAG + 自动重做",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
动词（spec §5.3，缺省动词 = run，裸输入等价 `loom run <input>`）:
  loom run [input]      执行流水线（本例最常见形态）
  loom plan [name]      只预览执行计划，不执行
  loom lock [name]      生成/刷新流水线锁文件后退出（维护动作，不执行）
  loom pack list        列出 pack 清册（物理 pack + 散装流水线）
  loom pack check       运行启动自检（等价 --check）
  loom pack new <name>  生成新 pack 骨架（目录 + 模板文件，不注册不校验）
  loom plugin list      列出全部 Agent / 插件（含来源标记）
  loom demo             离线体验：沙盒里跑一次完整交付（无网络无 Key）
  loom reconcile        只读对账报告：列队列×投影 stale-running 漂移（不杀进程不写库）

示例:
  # 运行完整流水线 (自动发现 pipeline YAML；动词可省)
  loom docs/README.md
  loom run docs/README.md

  # 指定 pipeline 配置文件
  loom run docs/README.md --pipeline-file pipelines/docgen.yaml

  # 从断点续传
  loom run docs/README.md --task-id mytask --resume

  # 改过流水线配置后重写锁（不执行）
  loom lock docgen

  # 旧模式（已冻结，仅兜底：按 Agent 注册元数据执行，不经 Scheduler/YAML）
  loom run docs/README.md --legacy
        """
    )
    # 动词层在解析后注入这三个内部开关（无对应 flag：旧 --plan/--dry-run/
    # --write-lock/--list-agents 已在动词收敛批次摘除）
    parser.set_defaults(plan=False, write_lock=False, list_agents=False, diff=False)

    parser.add_argument("input", nargs="?", default=None, help="输入文件")
    parser.add_argument("--task-id", "-t", default=None, help="任务ID（默认自动生成）")
    _pipeline_names = _available_pipeline_names()
    parser.add_argument("--pipeline", "-p", default="docgen",
                        choices=_pipeline_names or None,
                        help="流水线名称（可选值来自 pipelines/ 目录）")
    parser.add_argument("--pipeline-file", "-f", default=None, help="pipeline YAML 配置路径")
    parser.add_argument("--queries", "-q", nargs="*", help="检索词（多个）")
    parser.add_argument("--agent", "-a", action="append", help="指定运行的 Agent")
    parser.add_argument("--inputs-json", default=None, metavar="JSON",
                        help="类型化运行输入（JSON 对象）：按流水线 pipeline.inputs 声明"
                             "逐字段校验后渲染成输入文档；与位置参数 input 二选一")
    parser.add_argument("--set-input", action="append", default=[], metavar="K=V",
                        help="逐个给类型化输入（可重复，V 先按 JSON 解析、失败当字符串），"
                             "例：--set-input topic=云成本 --set-input depth=3")
    parser.add_argument("--resume", action="store_true", help="从断点续传")
    parser.add_argument("--timeout", default=600, type=int, help="超时（秒）")
    parser.add_argument("--output", "-o", default=None, help="输出文件")
    parser.add_argument("--report", action="store_true", help="生成详细报告")
    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument("--json-output", action="store_true", help="输出 JSON 结果到 stdout（供 wrapper 解析）")
    parser.add_argument("--legacy", action="store_true",
                        help="（已冻结，仅兜底）旧模式：直接按 Agent 注册元数据执行，"
                             "不经过 Scheduler/YAML。生产请使用默认 DAG 模式")
    parser.add_argument("--diff", action="store_true",
                        help="配合 loom plan：比对当前流水线与锁文件的指纹差异，"
                             "输出人话差异项后退出（不执行）")
    parser.add_argument("--admin", action="store_true", help="启动管理 API 服务")
    parser.add_argument("--dashboard", action="store_true", help="启动管理 API + 仪表盘（隐含 --admin）")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式：流水线执行完后保持 Admin API 常驻")
    parser.add_argument("--check", action="store_true", help="运行启动自检后退出")
    parser.add_argument("--three-pass", action="store_true", help="（已移除）三阶段流水线已并入 DAG 模式，此参数保留仅为兼容报错")
    parser.add_argument("--health-check", action="store_true", help="启动时运行 LLM 健康检查")
    parser.add_argument("--export", choices=["html", "word", "png"], help="导出格式（html/word/png）")
    parser.add_argument("--export-output", default=None, help="导出文件路径")
    parser.add_argument("--fix-ascii", action="store_true", help="将文档中的 ASCII 图转换为 Mermaid 图")
    parser.add_argument("--mcp", action="store_true", help="启动 MCP server（stdio JSON-RPC，供 AI agent 调度）")
    parser.add_argument("--recover", action="store_true", help="恢复中断的任务（重启后把 running 改回 pending 并重新执行）")
    parser.add_argument("--replay-run", default=None, metavar="RUN_ID",
                        help="补账：把一个 projection=incomplete 的运行按交付账本重投影后退出")
    parser.add_argument("--replay-incomplete", action="store_true",
                        help="补账：把所有 projection=incomplete 的运行重投影后退出")
    parser.add_argument("--replay-dry-run", action="store_true",
                        help="配合 --replay-run/--replay-incomplete：只报告将补什么，不写任何东西")
    parser.add_argument("--worker", action="store_true",
                        help="以常驻 worker 身份消费任务队列（claim → 执行 → 落状态）")
    parser.add_argument("--worker-id", default="", help="worker 标识，写进队列的 worker_id 列")
    parser.add_argument("--once", action="store_true", help="配合 --worker：只消费一条任务")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="worker 空闲轮询间隔秒数")
    parser.add_argument("--gc", action="store_true",
                        help="保留期 GC：按 retention 配置清理过期运行（一次，维护动作）")
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="worker 空转多少秒后退出（默认不退出）")
    parser.add_argument("--lease-stale", type=float, default=900.0,
                        help="回收 running 行的租约阈值（秒）")
    parser.add_argument("--stale-seconds", type=float, default=900.0,
                        help="reconcile 报告判 running 行过期的阈值（秒），默认与 --lease-stale 同口径")
    parser.add_argument("--triggers", action="store_true",
                        help="定时触发常驻模式：按 triggers.yaml 的 cron 提交 run（Ctrl+C 退出）")
    parser.add_argument("--triggers-file", default=None,
                        help="触发配置路径（默认仓库根 triggers.yaml）")
    parser.add_argument("--triggers-dry-run", action="store_true",
                        help="只列出各触发的最近 3 次时刻后退出（校验配置用）")
    return parser


def main():
    verb, verb_action, argv = _split_verb(sys.argv[1:])
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # verb -> 内部开关（等价旧 flag 的既有执行路径；旧 flag 已随收敛批次摘除）
    if verb_action == "plan":
        args.plan = True
    elif verb_action == "lock":
        args.write_lock = True
    elif verb_action == "plugin_list":
        args.list_agents = True
    elif verb_action == "pack_check":
        args.check = True

    # --json-output 的契约是"stdout 上只有一行 JSON 供 wrapper 解析"。实测这里
    # 之前会先印 11 行人类可读的过程输出（banner 之外的 [run] 提示、任务头等），
    # wrapper 按行解析就得到垃圾。把过程输出整体改接 stderr，结果 JSON 由
    # output_json_result 显式写回 _RESULT_OUT（重定向前抓下的那条真 stdout）。
    # 注意不给 --mcp 做同样的重定向：MCP 的协议帧就走 sys.stdout，换了会把它送去 stderr。
    if args.json_output:
        globals()["_RESULT_OUT"] = sys.stdout
        sys.stdout = sys.stderr

    # worker 模式自带参数解析，直接交给 loom_core.worker，不与流水线参数纠缠
    if args.worker:
        from loom_core.worker import main as worker_main

        raise SystemExit(worker_main([
            *([f"--worker-id={args.worker_id}"] if args.worker_id else []),
            *(["--once"] if args.once else []),
            f"--poll-interval={args.poll_interval}",
            *([f"--idle-timeout={args.idle_timeout}"] if args.idle_timeout is not None else []),
            f"--lease-stale={args.lease_stale}",
        ]))

    # ─── 保留期 GC（A6）：一次性维护动作 ────────────
    if args.gc:
        from loom_core import maintenance
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        orch.register_agents()
        try:
            summary = maintenance.gc_from_orchestrator(orch, log=print)
        finally:
            orch.shutdown()
        # 逐表打印而不是甩一个 dict：这是要人据此承担"删对了没有"的维护动作
        print(maintenance.format_summary(
            summary, maintenance.retention_from_config(orch.config)))
        sys.exit(0)

    # 无输入文件时：仅 --check/--mcp/--recover/--replay-*/--triggers*、服务模式与
    # plan/lock/pack/plugin/demo/reconcile 动词可用。类型化输入（--inputs-json/--set-input）
    # 是"没有输入文件"的另一种输入形式，同样算给了输入。
    typed_cli = bool(args.inputs_json or args.set_input)
    if args.input is None and not typed_cli and not args.check and not args.mcp \
            and not args.recover \
            and not args.replay_run and not args.replay_incomplete \
            and not args.triggers and not args.triggers_dry_run \
            and not args.admin and not args.dashboard and not args.daemon \
            and verb_action not in ("plan", "lock", "pack_list", "pack_new",
                                    "plugin_list",
                                    "demo", "reconcile"):
        parser.error('input required (or use verbs loom plan/lock/pack/plugin/demo/reconcile, or flags like --check)')

    print_banner()

    # ─── 离线体验：loom demo ────────────────
    # 入口即验收线：干净环境里一条命令看到一次完整交付（沙盒隔离、无网络
    # 无 Key）。失败不粉饰——状态非 done 退出码非 0。
    if verb_action == "demo":
        from loom_core.demo import run_demo
        try:
            result = run_demo()
        except Exception as e:
            print(f"[demo] ✗ demo 运行失败: {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0 if result.status == "done" else 1)

    # ─── 启动自检 ──────────────────────────────
    if args.check:
        from loom_core.bootstrap import run_startup_check
        report = run_startup_check(run_health_check=args.health_check)
        print(report.summary())
        sys.exit(1 if report.has_errors else 0)

    # ─── pack list（loom pack list）──────────────
    if verb_action == "pack_list":
        from loom_core import pack_manifest as pack_mf
        infos = pack_mf.discover()
        print(f"[pack] {len(infos)} packs")
        for info in infos:
            if info.is_physical:
                pipes = ", ".join(info.declared_pipelines) or "-"
                print(f"  {info.name:14s} [{info.source}]  agents={len(info.agent_names)}  pipelines: {pipes}")
            else:
                print(f"  {info.name:14s} [{info.source}]  agents={len(info.agent_names)}")
        return

    # ─── pack new（loom pack new <name>）────────────
    if verb_action == "pack_new":
        from loom_core.pack_scaffold import scaffold_pack, validate_pack_name
        pack_name = args.input  # 位置参数 input 被用作 pack 名
        err = validate_pack_name(pack_name or "")
        if err:
            print(f"[pack new] ✗ {err}", file=sys.stderr)
            sys.exit(2)
        project_root = Path(__file__).parent
        try:
            created = scaffold_pack(pack_name, project_root)
        except FileExistsError as e:
            print(f"[pack new] ✗ {e}", file=sys.stderr)
            sys.exit(2)
        print(f"[pack new] pack {pack_name!r} 已生成（{len(created)} 个文件）:")
        for path in created:
            print(f"  {path.relative_to(project_root)}")
        from loom_core.pack_scaffold import _slug
        slug = _slug(pack_name)
        print(f"\n下一步: 编辑 agents/{slug}_agent.py 实现业务逻辑，"
              f"然后 loom pack check 验证")
        return

    # ─── MCP server 模式 ──────────────────────
    if args.mcp:
        from loom_core.mcp_server import run_mcp_server
        run_mcp_server()
        return

    # ─── 定时触发模式 ──────────────────────────
    if args.triggers or args.triggers_dry_run:
        from datetime import datetime as _dt

        from loom_core.triggers import TriggerConfigError, describe_triggers, load_triggers, run_trigger_loop
        cfg_path = Path(args.triggers_file) if args.triggers_file \
            else Path(__file__).parent / "triggers.yaml"
        try:
            triggers = load_triggers(cfg_path)
        except TriggerConfigError as e:
            print(f"[triggers] ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        if args.triggers_dry_run:
            print(f"[triggers] 配置文件: {cfg_path}（{len(triggers)} 个触发）")
            for line in describe_triggers(triggers, _dt.now()):
                print(line)
            return
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        orch.register_agents()
        run_trigger_loop(orch, triggers)
        # 循环只在 Ctrl+C 后返回；收尾关总线/任务队列等资源（与 recover 分支同款）
        orch.shutdown()
        return

    # ─── 恢复中断任务 ──────────────────────
    # ─── 补账：投影写失败后的重放（loom_core/backfill.py）──────────
    if args.replay_run or args.replay_incomplete:
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        dry = bool(args.replay_dry_run)
        failed = False
        try:
            if args.replay_run:
                res = orch.replay_run(args.replay_run, dry_run=dry)
                print(f"[replay] {res['run_id']}: {res['action']} — {res['reason']}")
                print(f"         投影行建立={res['created_run']} "
                      f"节点补写={res['nodes_written']} 终态事件={res['closed']}"
                      f"{'（dry-run，未写入）' if dry else ''}")
                failed = res["action"] in ("failed", "refused")
            else:
                res = orch.replay_incomplete_runs(dry_run=dry)
                for r in res["results"]:
                    print(f"[replay] {r['run_id']}: {r['action']} — {r['reason']}")
                print(f"[replay] 检查 {res['checked']} 条，已补 {res['replayed']} 条，"
                      f"拒绝 {len(res['refused'])} 条，失败 {len(res['failed'])} 条"
                      f"{'（dry-run，未写入）' if dry else ''}")
                if not res["checked"]:
                    print("[replay] 没有 projection=incomplete 的运行")
                failed = bool(res["failed"])
        finally:
            orch.shutdown()
        sys.exit(1 if failed else 0)

    if args.recover:
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        orch.register_agents()
        recovered = orch.recover_tasks()
        if not recovered:
            print("[recover] 没有需要恢复的中断任务")
            sys.exit(0)
        print(f"[recover] 发现 {len(recovered)} 个中断任务，开始恢复...")
        from loom_core.scheduler import Scheduler
        sched = Scheduler()
        for t in recovered:
            print(f"  → 恢复任务 {t['task_id']} (pipeline={t['pipeline_name']}, input={t['input_file']})")
            try:
                plan = sched.parse(t["pipeline_name"])
                task = orch.run_plan(plan, input_file=t["input_file"],
                                     task_id=t["task_id"], wait=False)
                print(f"    已重新启动，新状态: {task.status.value}")
            except Exception as e:
                print(f"    恢复失败: {e}")
        print(f"[recover] 恢复完成，{len(recovered)} 个任务已重新提交")
        orch.shutdown()
        return

    # ─── 只读对账报告（loom reconcile，A7）─────────────────
    # 只报告、不修复：不自动杀进程、不写库（与 --recover 的回收写库语义刻意
    # 分开；人在圈内看了报告再决定动什么）。三类判据见 loom_core/reconcile.py。
    if verb_action == "reconcile":
        from loom_core.reconcile import stale_running_report
        from loom_core.run_store import RunStore
        from loom_core.task_queue import TaskQueue

        rows = stale_running_report(TaskQueue(), RunStore(),
                                    stale_seconds=args.stale_seconds)
        if args.json_output:
            # 同 output_json_result 的通道：json_output 模式下 stdout 已改接
            # stderr，结果 JSON 写 _RESULT_OUT（重定向前抓下的真 stdout）。
            out = _RESULT_OUT if _RESULT_OUT is not None else sys.stdout
            print(json.dumps(rows, ensure_ascii=False), file=out)
            out.flush()
        else:
            print(f"[reconcile] stale-running 报告 "
                  f"（stale_seconds={args.stale_seconds}；只读：未杀进程、未写库）")
            if not rows:
                print("[reconcile] 无 stale-running 漂移")
            for r in rows:
                pid = r["owner_pid"] or "—"
                run = r["run_status"] or "（无投影行）"
                print(f"  [{r['category']}] task={r['task_id']} "
                      f"queue={r['queue_status']} run={run} pid={pid} "
                      f"last_event_ts={r['last_event_ts']} reason={r['reason']}")
            print(f"[reconcile] 共 {len(rows)} 行漂移")
        return

    # ─── 三阶段流水线模式（已移除，提供迁移指引）──────────
    if args.three_pass:
        print("[run] ERROR: --three-pass 已在 v3.6.0 移除（ThreePassPipeline 已废弃）。"
              "其能力已由 DAG 模式覆盖且更完善：python run.py <input> --pipeline docgen",
              file=sys.stderr)
        sys.exit(2)

    # 快速自检（非阻塞，仅警告）
    try:
        from loom_core.bootstrap import quick_check
        if not quick_check():
            print("[run] 启动自检发现错误，使用 --check 查看详情")
    except Exception:
        pass  # 自检失败不阻断启动

    # ─── 锁文件维护（loom lock [name]）：写完即退出，不执行流水线 ───
    # 旧 --write-lock 是"重写锁并继续执行"；收敛后 lock 是纯维护动词，
    # 重写与执行分开（示例：loom lock docgen && loom run input.md）。
    if verb_action == "lock":
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        config = _load_config(args, project_root)
        orch.register_agents(agent_names=args.agent or None, config=config)
        _plan, lock_loaded = _resolve_pipeline_plan(args, orch, config)
        orch.shutdown()
        sys.exit(0 if lock_loaded else 1)

    # ─── 正常流水线执行 ──────────────────────
    project_root = Path(__file__).parent
    orch = _get_orchestrator(project_root)
    config = _load_config(args, project_root)
    loaded = orch.register_agents(agent_names=args.agent or None, config=config)

    if args.list_agents:
        print(f"\n已注册 {len(loaded)} 个 Agent:")
        print("-" * 60)
        for name in loaded:
            meta = orch.registry.get(name)
            if meta:
                status = orch.registry.get_status(name).value
                source = meta.get("source", "builtin")
                tag = "" if source == "builtin" else f"  [{source}]"
                print(f"  {name:20s} v{meta['version']:6s} [{status:12s}] "
                      f"{meta.get('description', '')[:40]}{tag}")
        print()
        return

    if not loaded:
        print("[run] 没有加载任何 Agent，请检查 agents 目录")
        return

    # 纯服务模式：无输入文件，仅常驻 Admin API（任务经 POST /api/tasks 提交）。
    # plan 动词无输入但只是预览，不得落进服务模式（否则 loom plan 会启 API 常驻）。
    service_only = args.input is None and not args.plan and not typed_cli
    finished_task = None
    if not service_only:
        finished_task = _run_single_task(args, orch, config)

    # ─── 管理 API / 仪表盘 / 守护进程 ──────────────
    want_server = args.admin or args.dashboard or args.daemon or service_only
    if want_server:
        # admin_api.host/port 可由配置文件覆盖（config.production.json 等）；
        # 未配置时默认仅本机回环绑定
        admin_cfg = config.get("admin_api") or {}
        host = str(admin_cfg.get("host", "127.0.0.1"))
        try:
            port = int(admin_cfg.get("port", 8910))
        except (TypeError, ValueError):
            print(f"[run] ERROR: admin_api.port 配置非法: {admin_cfg.get('port')!r}",
                  file=sys.stderr)
            sys.exit(1)
        # 控制台随包发布（loom_core/dashboard/），管理 API 一律带上——
        # 发行物里不再出现"装完打开 / 只有一行纯文本"的形态。
        from loom_core.paths import dashboard_dir as _dashboard_dir

        ok = orch.start_admin_api(
            host=host,
            port=port,
            serve_static=True,
            dashboard_dir=str(_dashboard_dir()),
        )
        if not ok:
            print("[run] ERROR: Admin API 启动失败（详见上方日志；"
                  "非本机绑定需设置 ADMIN_API_KEY）", file=sys.stderr)
            sys.exit(1)
        print(f"[run] 管理 API: http://{host}:{port}")
        print(f"[run] 控制台:  http://{host}:{port}/")
        if args.daemon or service_only:
            _run_daemon(orch)

    orch.shutdown()

    # 任务终态映射退出码（failed=1 / cancelled=2），服务模式与既有语义不受影响
    exit_code = _task_exit_code(finished_task) if finished_task is not None else 0
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    _install_sigterm_handler()
    main()
