"""双路径 parity 护栏（架构债冻结决策，2026-08）

背景：
  - ``orch.run()``（--legacy）：DAG 由 agent 注册元数据（registry.deps_order）构建，
    按设计绕过 pipelines/ YAML 与 Scheduler（无 lockfile 校验、无 per-node config）。
  - ``orch.run_plan()``：由 Scheduler 解析 YAML 驱动，是生产主路径。

决策：不重写 legacy 路径（冻结），但用本测试防止单边修复导致两条路径漂移。
护栏内容：
  1. 同一组 agent、同一输入，两条路径的终态与结果键集合必须一致；
  2. test_pipeline.yaml 的每个节点名必须能解析到已注册 agent
     （回归：曾把 safe_writer 误写成 safewriter，节点静默失败多时未被发现）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loom_core import PipelineOrchestrator  # noqa: E402
from loom_core.scheduler import Scheduler  # noqa: E402

HERE = Path(__file__).parent
PROJECT = HERE.parent
AGENTS_DIR = str(PROJECT / "agents")
INPUT_FILE = str(PROJECT / "test_input.md")
TEST_PIPELINE_YAML = str(PROJECT / "pipelines" / "test_pipeline.yaml")

# test_pipeline.yaml 中的 7 个 agent（注册名 → loader 文件 stem）
AGENTS_UNDER_TEST = {
    "researcher": "researcher",
    "fetcher": "fetcher",
    "writer": "writer",
    "quality_gate": "quality_gate",
    "checker": "checker",
    "layout": "layout",
    "safe_writer": "safe_writer_agent",
}

# legacy 路径无 per-node config，mock 引擎经全局 config 注入。
# 后两条与 pipelines/test_pipeline.yaml 的 quality_gate 节点同款：那是编排接线
# 夹具，底线两条判据显式关闭（真实底线行为钉在 tests/test_fidelity_gate.py）。
# 2026-11-10 起 legacy 也注入同款——keyless 拒绝稿在默认判据下判红，会让两条
# 路径终态分叉（YAML 路径夹具关底线判绿、legacy 判红），parity 被夹具语义差异
# 打穿，而非编排漂移。
MOCK_CONFIG = {"search_engines": ["mock"], "fail_fast": False,
               "min_output_chars": 0, "placeholder_markers": []}


def _make_orch(tmp_path: Path) -> PipelineOrchestrator:
    o = PipelineOrchestrator(
        agents_dir=AGENTS_DIR,
        checkpoint_dir=str(tmp_path / "ckpt"),
    )
    loaded = o.register_agents(
        agent_names=list(AGENTS_UNDER_TEST.values()),
        config=MOCK_CONFIG,
    )
    assert sorted(loaded) == sorted(AGENTS_UNDER_TEST), \
        f"注册结果与预期不符: {loaded}"
    return o


class TestYamlNodeResolution:
    """YAML 节点名必须能解析到已注册 agent（防 safewriter 式静默失败回归）"""

    def test_every_node_resolves_to_registered_agent(self, tmp_path):
        orch = _make_orch(tmp_path)
        try:
            plan = Scheduler().parse_file(TEST_PIPELINE_YAML)
            node_names = [n.agent_name for level in plan.levels for n in level]
            assert len(node_names) == len(AGENTS_UNDER_TEST)
            for name in node_names:
                assert orch.registry.get_instance(name) is not None, \
                    f"节点 '{name}' 无法解析到已注册 agent（注册名不匹配？）"
        finally:
            orch.shutdown()


class TestDualPathParity:
    """legacy run() 与 run_plan() 对同一组 agent 的结果必须一致"""

    def test_both_paths_same_status_and_result_keys(self, tmp_path):
        plan = Scheduler().parse_file(TEST_PIPELINE_YAML)

        orch_yaml = _make_orch(tmp_path / "yaml")
        try:
            task_yaml = orch_yaml.run_plan(
                plan, input_file=INPUT_FILE, task_id="parity_yaml")
        finally:
            orch_yaml.shutdown()

        orch_legacy = _make_orch(tmp_path / "legacy")
        try:
            task_legacy = orch_legacy.run(
                task_id="parity_legacy",
                pipeline_name="test_pipeline",
                input_file=INPUT_FILE,
                config=dict(MOCK_CONFIG),
                wait=True,
            )
        finally:
            orch_legacy.shutdown()

        # 终态一致
        assert task_yaml.status.name == "DONE", \
            f"YAML 路径失败: {task_yaml.status} error={task_yaml.error}"
        assert task_legacy.status.name == "DONE", \
            f"Legacy 路径失败: {task_legacy.status} error={task_legacy.error}"

        # 结果键集合一致（漂移的核心信号）
        keys_yaml = set(task_yaml.result.keys())
        keys_legacy = set(task_legacy.result.keys())
        assert keys_yaml == keys_legacy, \
            f"结果键漂移: yaml_only={keys_yaml - keys_legacy}, " \
            f"legacy_only={keys_legacy - keys_yaml}"
        assert keys_yaml == set(AGENTS_UNDER_TEST)

        # 两条路径所有节点/步骤均成功
        failed_yaml = [k for k, v in task_yaml.dag_nodes.items()
                       if v.status != "success"]
        assert not failed_yaml, f"YAML 路径存在失败节点: {failed_yaml}"
        failed_legacy = [s.step_name for s in task_legacy.steps
                         if s.status != "success"]
        assert not failed_legacy, f"Legacy 路径存在失败步骤: {failed_legacy}"
