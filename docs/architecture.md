# 架构说明

本文描述 Loom 的分层结构、线程模型与一次任务的完整数据流。
模块职责速查表见 README「核心模块」章节。

---

## 1. 分层总览

```
┌─────────────────────────── 入口层 ───────────────────────────┐
│  run.py (CLI)   --mcp→ mcp_server.py (JSON-RPC/stdio)        │
│  --admin/--dashboard→ admin_api.py (ThreadingHTTPServer)      │
│  外部系统 → POST /api/tasks / SSE /stream                     │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── 编排层 ───────────────────────────┐
│  scheduler.py     pipeline YAML → ExecutionPlan（Schema+锁） │
│  pipeline.py      PipelineOrchestrator：run_plan 层级循环、   │
│                   checkpoint、recover_tasks、池化结果合并      │
│  dag_executor.py  DAGExecutor：节点调度（同步线程池/async）、  │
│                   重试退避、熔断、限流、步骤审计                │
│  executor_factory.py  thread/process 执行器工厂               │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── 传输层 ───────────────────────────┐
│  message_bus_v3.py  MessageBus：发布/订阅 + 请求响应 + 背压    │
│  message_store.py   PersistentStore：SQLite(WAL) 持久化 + DLQ │
│  task_queue.py      SQLite 任务队列（中断恢复）                │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── Agent 层 ─────────────────────────┐
│  registry.py       注册表：元信息/健康检查/重生/热插拔          │
│  agent_loader.py   发现 + AST 安全沙箱加载                    │
│  base_agent.py     BaseAgent 契约（handle + 生命周期钩子）     │
│  agents/*.py       researcher → fetcher → writer →            │
│                    quality_gate → checker → layout → safe_writer │
└───────────────────────────────────────────────────────────────┘

横切组件：circuit_breaker.py / rate_limiter.py / cache_manager.py /
         observability.py(日志+Metrics) / event_hook.py(webhook) /
         llm_router.py / cost_tracker.py /
         alert_manager.py / quality_feedback.py / version_manager.py
```

### 1.1 包级分层与依赖方向

上面是运行时层次；源码按**包**分成了两层，方向必须单向：

```
    agents/            插件层：具体能力，由 agent_loader 按目录发现
        │  可以 import ↓，两边都不许反过来硬编码 import
    loom_doc/       文档领域层：renderer / ingest / document_enhancer
        │  可以 import ↓
    loom_core/     引擎层：DAG、总线、重试/熔断/限流、检查点、Schema
```

约束与理由：

| 规则 | 理由 |
|------|------|
| `loom_core` 不得 import `loom_doc` | 引擎不认识具体领域。一旦反向引用，非文档类工作流就得改引擎 |
| `loom_core` 不得 import `agents` | Agent 靠目录发现 + AST 沙箱加载，硬编码会让新增 Agent 必须改引擎 |
| `loom_doc` 不得 import `agents` | 插件调用领域层是正方向，反向成环 |
| `loom_doc/__init__.py` 不做顶层 re-export | 保留唯一模块路径，避免 `loom_doc.render` 与 `loom_doc.renderer.render` 两个打桩入口造成的假绿 |
| `loom_doc` 的外部依赖需逐条登记 | 新增非标准库 import 必须先在 `tests/test_layering.py` 的登记表认领；`loom_doc → scripts` 是钉住的历史耦合，只允许 `document_enhancer` 一处 |

以上由 `tests/test_layering.py` 用 AST 静态扫描当门禁——覆盖函数体内的惰性
import 和 `importlib.import_module("...")` 字面量，而不只是文件头部那几行。
四条判据都做过注入式正例验证（往 core 塞一句 `from loom_doc import renderer`
即转红），否则"0 违规"只是扫描器空转，不算结论。

> `loom_doc` 只依赖 `loom_core` 的 `llm_router`（`document_enhancer` 用到），
> 它仍是引擎级横切组件，没有跟着搬。原先与它并列的 `search_engines` 已迁至独立库
> `artesian`：现在是第三方依赖，不再出现在这条"core 不认识领域"的清单里。

### 1.2 两种 skipped，别混

节点状态 `skipped` 有两个完全不同的来源，靠 `TaskNode.skip_reason` 区分：

| 来源 | `skip_reason` | 下游 | 含义 |
|------|---------------|------|------|
| `when` 条件不成立 | `"condition"` | **照常执行** | 作者声明这轮不需要这条分支 |
| 依赖未成功被级联跳过 | `""` | 继续跳过 | 上游挂了，拿不到输入 |

实现上 `_submit_level_futures`（线程版与异步版各一处，改一处必须改两处）在提交前求值
`node.when`：不成立就打标记并记一条 `status="skipped"` 的步骤（报表看得见这格是空的），
成立则正常提交；**求值抛 `ConditionError` 时让它冒出去整条 run 失败**——把它降级成
"条件不成立"就会静默跳过分支却照报 done。

求值实现是 `loom_core/conditions.py`（纯函数，不 exec/eval）。上下文里 `upstream.*`
覆盖**整个上游闭包**，不只是直接依赖：作者写 `upstream.quality_gate.overall_score` 时
quality_gate 常常是祖先节点（`docgen-lean` 的 fact_checker 就是这种写法），只暴露直接
依赖会让按直觉写的条件失效。上下文表与算子清单见 README「条件节点」。

与交付契约（§1.3）的关系：落盘节点**全部**因条件被跳过 → 视为按声明本轮无交付，
run 仍 done 并留 warning；因依赖失败没跑 → 仍然 failed。

### 1.3 交付契约

`_execute_plan` 收尾不再无条件盖 DONE：计划里存在声明 `WRITES_OUTPUT` 的节点时，
必须 `task.output_path` 指向的文件真的存在（或有非空内联内容）才算 done。
起因是实测 kb-docgen 报 `done` + exit 0 却没有任何产物文件。

### 1.4 节点身份：一个名字同时是"图里的谁"和"谁来执行"

节点名格式 `agent[_pool_i][__alias…]`，两种解析**必须分开用**（`loom_core/naming.py`）：

| 函数 | 用途 | `writer_pool_0__review` 的结果 |
|------|------|-------------------------------|
| `agent_of()` | 查注册表元信息、RPC 目标、熔断器键 | `writer` |
| `family_of()` | 归并池兄弟、上游闭包分组、结果读回退 | `writer__review` |

混用的后果是实测到的，不是推演：内联展开后我一度把闭包分组也写成 `agent_of`，
于是 `layout__review` 从 `layout__audit` 的上游闭包里掉出去，第三个节点拿到的是
两跳之前的正文——没有任何报错，产物只是悄悄变了。同理 `_merge_pooled_results`
按 Agent 归并会把两段不同子流程的池结果混成一团。
判据：分组类一律 `family_of`，查 Agent 类一律 `agent_of`，两侧各有测试
（`TestRuntimeIdentity` / `TestPoolMergeIsAliasAware`），且都做过变异回退验证会转红。

同一条规则还管**Agent 声明的能力**。`registry.get_meta(name)` 是精确查表，拿别名名去查
得到的是 `None`——`None` 不抛错，只是那项能力没了。docgen 改用质量尾片段后真机撞上两处：

| 位置 | 别名名查 meta 的后果 |
|------|--------------------|
| `_record_task_output` | `WRITES_OUTPUT` 失效 → `task.output_path` 不记 → 交付契约反过来说"没有交付物"，文件明明写着 |
| 质量重做分支 | `SUPPORTS_REGENERATION` 失效 → 内联的 quality_gate 不再触发重做，质量门降级成一次性打分 |

判据：`TestInlinedNodesKeepAgentCapabilities` 两条都跑真 executor，且把 `get_meta(base_agent)`
改回 `get_meta(node.agent_name)` 会双双转红。复检目标（`REGENERATION_RECHECK` 缺省时取
本节点名）同样要还原成 base 名——topic 是按 Agent 寻址的，`quality_gate__tail.input`
没有任何订阅者。

### 1.5 子流水线片段与参数作用域

`pipelines/_quality-tail.yaml` 是六条流水线（docgen / docgen-render / docgen-verified /
docgen-lean / docreq / kb-docgen）的共享质量尾。抽取前逐字节比对过这几份尾巴：
`quality_gate/checker/layout/safe_writer` 的 version/timeout/config 完全一致，只差
fact_checker 在不在、什么条件下跑。所以差异收敛成两个参数（`fact_check` / `min_score`），
由调用方在 `call` 节点的 `inputs:` 里传。

判据不是"看起来一样"：重锁后按节点比对抽取前后 lock 里的 `config_hash` / `version` /
`pool_size`，六条共 26 组，0 处不一致。

`three_pass` 故意不引用片段——它的尾巴是另一份真相（threshold 65、max_regenerations 2、
checker/layout 超时 120、safe_writer 超时 60）。把它并进来要么改它行为，要么给片段
加一堆"每节点可覆盖"的参数，那是为了消灭重复而制造更复杂的重复。

参数作用域三条规则：

1. **片段节点可写 `inputs` 当默认值**，前提是它自己的 `when` 读到 `inputs.*`——否则是
   "配了没人读"的死配置，解析期拒绝。有了默认值，片段自身也能被解析/lint（不必假装只有
   被调用时才存在）。
2. **只把该节点确实会读的键落到节点上**。整段 tail 的每个节点都背一份用不到的实参，
   lockfile 就会记满没意义的条目，外层参数还会灌进内层作用域。
3. **嵌套 `call` 各用各的实参**：内层 `call` 节点的参数在它自己的子计划里生效，外层同名
   键不会覆盖它（`test_outer_args_do_not_leak_into_inner_scope`，对旧的"合并外层"写法会红）。

片段不进用户可见清单：`installed_pipelines` / `Scheduler.list_pipelines` / run.py 的候选名
一律过滤 `_` 前缀，但 `load()` 仍按名字取得到（`call` 需要）。片段与引用它的 YAML 必须
同目录——`parse_file` 把引用方目录传给内联逻辑，于是 `--pipeline-file /tmp/x.yaml` 也能
在 `/tmp` 找到它的片段，而不是回头看进程 cwd。

### 1.6 逐项展开（`foreach`）：展开在一次节点执行内部

`naming.py` 当年把"支持 foreach（同一节点按项展开）"写成集中节点身份解析的理由，这一节
是它还债的形态。

**为什么不给每一项造一个新节点身份**（`transform__0` / `transform__1` …）：那会让层级、
上游闭包、lockfile 的节点集合全部随**运行期数据**变化，而锁文件的意义恰恰是"声明没变就不
该变"。所以展开发生在一次 `execute_node_from_scheduler` 内部：DAG 看见的仍是 1 个节点，
`node_count`、检查点粒度、重试粒度、报表步骤都照旧。代价是断点续传只能按节点续——一个
foreach 节点中断后恢复会重跑它的每一项，这是明确的取舍，不是遗漏。

三处必须记住的机制：

1. **幂等键按项分裂**（后缀 `#{i}`）。总线按 `task_id:node:attempts` 记历史，共用前缀时
   第 2 项起直接命中第 1 项的缓存——表现是"展开 N 次、真实执行 1 次"，产物形状却完全正常。
   判据在 `test_idempotency_keys_split_per_item`，去掉后缀即红。
2. **`item`/`index`/`count` 是引擎自有键**（进了 `ENGINE_OWNED_KEYS`）。上游若声明同名产物
   就顶掉了它们，后果不是报错而是 N 项渲染出同一份内容；`merge_artifact` 那一层就拒收。
3. **任一项失败 = 节点失败**，包括订阅者返回 `None` 的超时项；`hard_floor` 逐项累积后仍走
   硬失败前缀。理由与本项目一路在关的洞同源：聚合时"跳过坏项"会让交付物悄悄短一截，
   而短一截的文档看起来仍是完整的。

拒绝同用的组合：`foreach` + `call`（展开后"本节点"不存在）、`foreach` + `pool_size>1`
（两种"拆成多次执行"叠加后的分配语义没有唯一答案）、`foreach` + `REGENERATION_*` 与
`foreach` + `WRITES_OUTPUT`（后两者的操作对象是"整份产物"；落盘要排在 foreach 之后，
让 sink 吃聚合产物）。前两类在解析期报错，后两类在**任何投递发生之前**报错。

`topology_hash` 收的是**展开契约**（`over` 与 `max_items`），不是运行期解析出的项数：
项数来自数据，把它写进锁等于让锁依赖数据文件，`loom lock` 就变成每天重锁。


版本锁定的覆盖面：片段的节点、连线、配置哈希、以及**调用方传的实参**都进了父流水线的
`topology_hash`/`config_hash`，所以改片段或改 `inputs` 都会让父锁报漂移；片段自己那份锁
记录的是它单独展开的形状。

## 2. 核心概念

| 概念 | 定义位置 | 说明 |
|------|---------|------|
| `ExecutionPlan` | `scheduler.py` | YAML 解析产物：`levels`（拓扑层级）、`raw` 原始配置、`checkpoint` 策略 |
| `PipelineTask` | `pipeline.py` | 运行时任务状态：status/dag_nodes/steps/result/stop_event |
| `TaskNode` | `pipeline.py` | 统一节点模型：静态配置（timeout/retry/backoff）+ 运行时状态（attempts/status/result） |
| `AgentMeta` | `registry.py` | Agent 注册元信息 |
| 熔断器 | `circuit_breaker.py` | CLOSED/OPEN/HALF_OPEN 三态，per-agent 隔离，打开时快速失败 |
| 重生成循环 | `agents/quality_gate.py` | 评分不达标 → 触发 `REGENERATION_TARGET`（writer）重做 → 复检 |

## 3. 线程模型

| 线程 | 创建处 | 职责 | 关闭方式 |
|------|--------|------|---------|
| Bus worker | `MessageBus.__init__` | 消费异步队列、批量投递订阅者 | `bus.shutdown()` |
| Webhook 事件循环 | `event_hook.py` | aiohttp 异步投递 webhook | orchestrator shutdown 时通知 |
| Admin HTTP | `ThreadingHTTPServer`（daemon_threads=True） | 每连接一线程处理 REST/SSE | 进程退出 |
| 执行器池 | `executor_factory.create_executor` | 节点执行工作线程 | with 上下文自动关闭 |

关闭入口统一为 `orch.shutdown()`（`run.py` 各分支末尾调用），负责停总线、
清理 agent（含 `cleanup_stale_temp`）。SIGTERM 经 `_sigterm_handler` 转为
KeyboardInterrupt 走同一收尾路径。

## 4. 一次任务的数据流

以默认 `docgen` 流水线为例：

1. **提交**：CLI / `POST /api/tasks` → `orch.run_plan(plan, input_file, task_id)`
2. **建任务**：创建 `PipelineTask`（RUNNING）、注册到 `_running_tasks`、
   写入 SQLite `task_queue`、发 `task.created/started` 事件与 `pipeline.started` 消息
3. **层级循环**：按 `plan.levels` 逐层执行；每层前检查 cancel/pause、快照状态；
   `DAGExecutor.execute_level` 将层内节点提交线程池并发执行
4. **节点执行**：`execute_node_from_scheduler` → 组装 Message → `agent.handle(msg)`，
   结果写入任务输出；失败走指数退避重试（可被 stop_event 中断），最终失败触发
   熔断计数或 fail_fast 软中断；每步记录 StepResult + 审计 + Metrics
5. **质量闭环**：quality_gate 评分不达标时按重生成配置回退 writer 重做
6. **池化合并**：层内多实例（`*_pool_*`）结果按 `results_merge` 策略聚合
7. **收尾**：`_finalize_plan_task` 更新进度/时间戳 → task_queue 状态 → 事件钩子
   （completed/failed/cancelled）→ 报告生成 → 临时文件清理 → checkpoint 处理
8. **落盘**：safe_writer 原子写入输出文档并备份

断点续传：任务中断后 checkpoint 保留各 agent `on_snapshot()` 状态；
`--resume` 或 `--recover` 从 checkpoint/task_queue 恢复。

## 5. 设计取舍备注

- **进程模式**（`executor_type: process`）：节点在 ProcessPoolExecutor 子进程中执行。
  DAGExecutor 经 pickle 传入子进程时剥离全部含线程锁的组件；worker 入口
  （`dag_executor._execute_node_worker`）依据构造时记录的 `child_context`
  （agents_dir / agent_names / config，由 `register_agents()` 写入）在每个 worker
  进程内一次性重建 Registry 与非持久化 MessageBus，再通过 AgentLoader 加载 Agent。
  **已知限制**：① 子进程内的总线事件不回传父进程（节点结果经返回值 pickle 回传，
  不受影响）；② 熔断器与限流计数按进程隔离，不跨进程聚合；③ 每个 worker 首次执行
  有 Agent 冷启动开销。I/O 密集场景仍建议默认的 thread 模式。
- `MessageBus` 在构造时即启动 worker 线程——每个实例必须配套 `shutdown()`
  （orchestrator 与测试 fixture 已保证）。
