# loom-plugin-hello —— 外部插件示例

演示 `loom.agents` entry_points 接入：**装好即被发现 / 注册 / 执行，
本仓零改动**（product-spec §2.1 验收线第 2 条）。

## 契约（与内置 `agents/*.py` 同构）

- entry point 的**值必须是模块路径**（`包.模块`），group 固定 `loom.agents`；
- 模块顶层声明 `AGENT_NAME`（`AGENT_VERSION` / `AGENT_DESC` / topics 可选），
  并至少定义一个 `BaseAgent` 子类；
- 加载器按 `__file__` 做与内置件同一套 AST 安全检查——不碰黑名单里的调用就
  不需要任何声明；`SANDBOX_TRUSTED = True` 只留给随产品发布的内置件；
- 同名冲突时**本仓 `agents/*.py` 优先**，插件顶不掉内置 Agent。

## 试一下

```bash
pip install ./examples/plugin-hello
python run.py --list-agents        # 会看到 char_stats 带 [entry_point:char_stats] 标记
```

之后就能把它当任意流水线的节点用（`pipelines/*.yaml` 的 `agents:` 里写
`name: char_stats`）；不想要了就 `pip uninstall loom-plugin-hello`。

## 验证（与 CI 同款判据）

`tests/test_plugin_example.py` 会：

1. 快判据——示例包 pyproject 的 group 与加载器常量一致、模块契约成立、
   通过 AST 安全检查；
2. 慢判据（端到端）——建一个真 venv、`pip install` 本目录、在**空 agents 目录**
   下运行发现脚本：`char_stats` 必须被 `discover()` 列出、`register()` 注册
   （`source == "entry_point:char_stats"`）且 `handle()` 真跑出结果。

venv 用 `--system-site-packages`、插件 `--no-deps` 安装——整条判据离线可跑。
