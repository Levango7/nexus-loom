# Loom（发行名 nexusloom）

> **主打：可验收的 Agent 交付运行时** — 把「取数/资料 → 加工 → 核查 → 人审 → 交付」做成可复现、可审计的作业

Loom 是一个自托管的 Agent 工作流运行时：声明式 YAML 编排可插拔 Agent，内核自带
**产出契约**（声明了交付物却没交付就不算 `done`）、**锁文件拓扑指纹**（配置或拓扑漂移即拒绝执行）、
**审批闸门**、**交付账本**与 **LLM 成本预算熔断**；可 `pip install`，可被 CLI / HTTP / MCP / 队列调用。

**文档生成（docgen）是它自带的旗舰 pack**；检索、摄入、变换等通用 Agent 也可被其他工作流复用。
下面同时列出文档场景与 `api-report` 等通用工作流，引擎本身不限定交付领域。

## 出厂场景（文档 pack 与通用工作流）

| 级别 | 场景 | 入口 | 说明 |
|---|---|---|---|
| **主打** | 文档生成 | `python run.py <input> --pipeline docgen` | 完整 7 Agent 流水线，生产可用 |
| **主打** | 文档生成 + 事实核查 | `--pipeline docgen-verified` | 增加 fact_checker 节点，数字类声明交叉验证 |
| **省钱** | 文档生成 + **条件升级核查** | `--pipeline docgen-lean` | fact_checker 挂 `when`：质量分达标才付核查成本，不达标时该节点被跳过而下游照常跑 |
| **主打** | **文档生成 + 多格式渲染** | `--pipeline docgen-render` | 追加 renderer 节点，产出 **docx / pdf**（可编辑 Word / 可打印归档） |
| **主打** | **本地资料 → 知识库接地文档** | `--pipeline kb-docgen` | 摄入自己的 PDF/图片/文本 → 切块向量入库 → 检索命中驱动写作；离线可跑（无 LLM 时如实产出抽取式草稿） |
| **实验性** | 需求分析 | `--pipeline docreq` | requirements_analyzer 输出结构化 DocumentSpec |
| **通用** | 任意 JSON 接口 → 报告 | `--pipeline api-report` | 无领域节点：`http_request` 取接口 → `transform` 挑字段渲染 → 落盘 |
| **通用** | 接口列表 → **逐项摘要** | `--pipeline api-digest` | 同一个 Agent 按 `foreach` 逐项跑 N 次，聚合产物再落盘（列表长度超 `max_items` 直接报错） |
| **实验性** | 文档增强（库） | `loom_doc.document_enhancer` | 逐章节 LLM 深化 + 搜索补充；CLI 入口已随动词收敛摘除（库仍可用，见 `docs/agents.md`） |
| **实验性** | MCP Server | `--mcp` | JSON-RPC 2.0 over stdio，供外部 Agent 调度 |

### 输出格式

| 格式 | 技术路线 | 适用 | 说明 |
|---|---|---|---|
| `.md` | — | 中间态 / 二次编辑 | 管线唯一中间产物，**始终保留** |
| `.docx` | python-docx（OOXML） | 交付 / 二次编辑 | 落**真正的 Word 标题样式**（Title / Heading / List Bullet）与**真表格**（Table Grid 样式、表头加粗），Word 能自动生成目录、导航窗格可跳转 |
| `.pdf` | ReportLab | 打印 / 归档 / 送审 | A4，中文字符 100% 保留，代码缩进保真，表格落带网格线的真表格 |
| `.xlsx` | openpyxl | 数据交付 / 二次计算 | 结构化子集：每张表一个工作表（表头加粗、冻结首行、列宽自适应）；无表格时整篇落「正文」单列（`mode` 字段如实回报） |
| `.pptx` | python-pptx | 演示 / 汇报 | 结构化子集：h1/h2/h3 各起一页、正文成要点、表格落**真 PowerPoint 表格**；超限表格/代码块就地截断并在页内注明 |

> docx 与 pdf **互补而非替代**：docx 是"活文档"（可编辑、可协作），
> pdf 是"定版归档"（可打印、可送审）；xlsx / pptx 是**结构化子集**而非全量转换器。
> 各后端均为可选依赖，缺失时 renderer 如实回报跳过原因，**不影响 Markdown 主产物**。

### 输入来源

除了输入主题去公网检索，系统还能直接吃下**你手上的已有资料**：

| 来源 | 支持 | 说明 |
|---|---|---|
| `.pdf` | ✅ | 数字版 PDF 提取，**按字号推断标题层级**（22pt→h1 / 16pt→h2），中文无损 |
| `.png` `.jpg` 等图片 | 需 OCR 后端 | 扫描件/图片需 `paddleocr` 或 `mineru`，未安装时明确告知而非静默失败 |
| `.md` `.txt` | ✅ | 编码嗅探（UTF-8 / GB18030 / GBK），中文不乱码 |

资料进入**知识库**后持久化，可反复按语义检索：

```bash
# 一条命令跑完：摄入 → 建库检索 → 接地写作 → 质检 → 排版 → 落盘
# 输入 Markdown 里第一行写主题，其余行写资料路径（相对/绝对皆可）
python run.py input.md --pipeline kb-docgen -o output/kb_doc.md
```

也可以只用库和检索这两层的能力：

```python
from artesian.knowledge_base import KnowledgeBase

kb = KnowledgeBase("knowledge_base.db")        # 默认 hash 嵌入（离线可用）
kb.add_file("财报.pdf")                        # 自动经摄入层转 Markdown
kb.search("本季度营收增长多少", top_k=3)        # 向量检索
```

**诚实边界**：内置 `hash` 嵌入是**特征哈希（词法相似）**，能找到"用词相近"
的内容，**不能**理解同义词与语义改写（"营收" vs "收入" 匹配不上）。
需要真语义请装 `sentence-transformers`（`local` 后端）或配 `EMBEDDING_API_KEY`
（`api` 后端）；两者不可用时 `auto` 会自动回落到 hash 并记录原因，不会失败。

---

## 特性

| 类别 | 能力 |
|------|------|
| **编排** | DAG 并行执行、断点续传、可视化执行计划、SQLite 任务队列恢复 |
| **组合** | `when` 条件节点（不成立就跳过、下游照跑）、`call` 子流水线内联（同一 Agent 可在一张图里出现多次）、`foreach` 逐项展开（按上游列表把同一个 Agent 跑 N 次：幂等键按项分裂、产物按声明聚合、**任一项失败即节点失败**） |
| **通用工作流** | 领域无关的 `http_request`（出网走 SSRF 校验、超限不截断、重定向不跟随）+ `transform`（声明式取数/过滤/模板，不执行表达式），出厂流水线 `api-report` 就是它们的消费者：换一种任务类型仍然成立 |
| **需求** | **requirements_analyzer 需求分析器**（输入 → 结构化 DocumentSpec：类型/范围/读者/深度，置信度评分 + 追问建议，`--pipeline docreq`） |
| **检索** | Bocha + Tavily + Serper + Metaso + Bing + Sogou + 360 等 10 引擎、LRU+TTL 跨任务缓存 |
| **抓取** | Async I/O（aiohttp 并发）/ 同步线程池降级、内容质量识别 |
| **写作** | TF-IDF 向量语义匹配、骨架生成、LLM 润色、质量反馈闭环 |
| **质量** | QualityGate v2（Profile 模板）、**产出保真底线**（空/占位文档直接判失败，不再 done+exit 0）、Style Enforcer、Citation Verifier、评分历史学习、**fact_checker 事实核查**（数字类声明 vs 检索源一致性，`--pipeline docgen-verified`） |
| **弹性** | 熔断器、限流器、Agent Pool、背压、自动重生成、告警机制 |
| **可观测** | 结构化日志（轮转）、Prometheus Metrics、Admin REST API、**内置控制台**（随包发布，打开 `/` 即用）、日志查询 |
| **成本** | LLM 调用成本追踪（16 供应商定价表）、预算熔断、`GET /api/cost` |
| **安全** | Agent 沙箱（AST 安全检查 + 白名单）、.env 明文密钥检测 |
| **运维** | 版本锁定（`loom lock` 生成/刷新锁文件；运行时自动比对 version + config_hash，配置漂移即拒绝执行）、Schema 校验、基础鉴权、Docker 化、配置热更新、MCP Server |

---

## 快速开始

### 前置条件

- Python 3.11+
- 网络连接（用于检索引擎抓取内容）
- 搜索引擎 API Key（可选）— 复制 `.env.example` 为 `.env` 并填入 Bocha/Tavily/Serper 等 Key；无 Key 时默认使用 Bing/Sogou/360 免费引擎

### 3 步快速体验

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成文档
python run.py test_input.md -o output/my_doc.md

# 3. 查看结果
type output\my_doc.md
```

输入文件是一行主题描述，输出是结构化 Markdown 文档。

### 安装

```bash
pip install -r requirements.txt
```

### 基本用法

```bash
# 生成文档（默认 docgen 流水线）
python run.py test_input.md --pipeline docgen --output out.md

# 指定检索词
python run.py topic.md --queries "RAG 架构" "检索增强生成" --pipeline docgen

# 仅预览执行计划（不实际运行；动词形态）
loom plan docgen

# 从断点恢复
python run.py test_input.md --resume --task-id <id>

# 启动 Dashboard（默认 http://127.0.0.1:8910，入口 /index.html）
python run.py test_input.md --dashboard
```

### Python SDK

`pip install nexusloom` 之后即可 `from loom_core import LoomClient`——**零第三方依赖**（stdlib
`urllib`），Admin API 的薄封装，方法与 HTTP 端点、OpenAPI spec 三处同源（文档表由测试双向守着）：

```python
from loom_core import LoomClient, LoomError

client = LoomClient("http://127.0.0.1:8910", token="alice-2026")   # 或 api_key=...
task = client.wait(client.submit(query="写一篇 Git rebase 指南", pipeline="docgen")["task_id"])
for art in client.artifacts(task["id"]):
    print(art.get("kind"), art.get("path") or art.get("keys"))
```

认证、完整方法表（16 个方法）、错误模型与五条语义边界见 [Python SDK](docs/sdk.md)。

### CLI 动词与参数

**动词**（spec §5.3；缺省动词 = run，`loom input.md` 等价 `loom run input.md`）：

| 动词 | 说明 |
|------|------|
| `loom run [input]` | 执行流水线（最常见形态，动词可省） |
| `loom plan [name]` | 只预览执行计划，不执行 |
| `loom lock [name]` | 生成/刷新锁文件后退出（与 YAML 同目录：核心落 `pipelines/*.lock`，pack 落 `packs/<pack>/pipelines/*.lock`；纯维护动作，不执行流水线） |
| `loom pack list` | 列出 pack 清册（物理 pack + 散装流水线） |
| `loom pack check` | 运行启动自检（等价 `--check`） |
| `loom plugin list` | 列出全部 Agent / 插件（含来源标记） |

**其余参数**（服务/运维开关保持 flag）：

| 参数 | 说明 |
|------|------|
| `input` | 输入文件（Markdown/文本） |
| `--pipeline, -p` | 流水线名称（默认 `docgen`） |
| `--queries, -q` | 检索词（可多个） |
| `--output, -o` | 输出文件路径 |
| `--resume` | 从断点续传（声明式与 legacy 两条路径都生效），配 `--task-id` 指定要续的任务；断点属另一条流水线时会被拒收并告警，而不是合错状态 |
| `--admin / --dashboard` | 启动管理 API / 仪表盘 |
| `--daemon` | 执行完后保持 API 常驻 |
| `--mcp` | 启动 MCP Server（JSON-RPC over stdio，供外部 Agent 调度） |
| `--recover` | 启动时恢复中断的任务队列 |
| `--worker` | 常驻 worker：消费 `task_queue` 里的 pending 任务（claim → 执行 → 落状态）；配 `--once` 只消费一条，`--poll-interval` / `--idle-timeout` / `--lease-stale` 控制节奏与崩溃租约回收 |
| `--config, -c` | 自定义配置文件 |
| `--json-output` | 输出 JSON 结果（供 wrapper 解析） |
| `--legacy` | （兜底）按 Agent 注册元数据执行，不读 pipelines/*.yaml；B8 引擎收敛后，此路径经 `run()` → `run_plan()` 统一引擎执行，仍获得 RunStore 投影、交付契约检查等全部声明式能力 |
| `--check` | 启动自检后退出：如实报告 HTML 解析内核、LLM 供应商、依赖与目录结构。退出码只反映**结构性故障**——缺 LLM 凭据记 WARN 不记 ERROR，因此 `rc` 可被 CI 直接采信（历史上 CI 从不读这个 rc，只 grep 一行文案） |

> **执行路径说明**：B8 引擎收敛后，`orch.run()` 是唯一入口——有 `--config` 走 legacy 注册表依赖序，无 `--config` 有 `--pipeline` 则先尝试 Scheduler 加载 YAML，加载失败回落 legacy。两条路径最终都委托给 `run_plan()` 统一引擎执行，一致性由 `tests/test_dual_path_parity.py` 护栏。

---

## 架构

```
                    ┌─────────────┐
   input.md  ──────▶ │  Researcher │  多引擎搜索 + 过滤
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │   Fetcher   │  并发下载 + 正文提取 + 质量识别
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │   Writer    │  骨架 + TF-IDF 匹配 + LLM 润色
                    └──────┬──────┘
                           ▼
                 ┌───────────────────┐
                 │   QualityGate     │  Profile 评分 + Style/Citation 扣分
                 │  (不达标自动重生成) │
                 └──────┬────────────┘
                        ▼
                 ┌─────────────┐
                 │   Checker   │  结构完整性分级质检（P0-P3）
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │   Layout    │  标题层级/目录/格式优化
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ SafeWriter  │  原子写入 + 备份
                 └─────────────┘
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `loom_core/scheduler.py` | 读取 pipeline YAML → ExecutionPlan（含 Schema 校验、Lockfile） |
| `loom_core/pipeline.py` | Orchestrator：DAG 执行、断点续传、rerun、统一节点模型 |
| `loom_core/dag_executor.py` | DAG 构建、节点调度、熔断/限流/重做循环 |
| `loom_core/message_bus_v3.py` | SQLite 持久化消息总线（WAL + 批量投递 + 背压） |
| `loom_core/circuit_breaker.py` | 熔断器（CLOSED/OPEN/HALF_OPEN） |
| `loom_core/rate_limiter.py` | 令牌桶限流器 |
| `loom_core/registry.py` | Agent 注册表（健康检查 + 热插拔） |
| `loom_core/observability.py` | 结构化日志（异步批量写入）+ Prometheus Metrics |
| `loom_core/admin_api.py` | REST API（多线程，健康/指标/任务/成本/告警/日志/配置管理） |
| `loom_core/version_manager.py` | 文档版本管理（自动版本号/diff/回滚） |
| `loom_core/cache_manager.py` | 统一缓存（memory/file/multi 三级） |
| `loom_core/llm_router.py` | 多供应商 LLM 路由器（16 供应商定义，按 .env 启用） |
| `loom_core/cost_tracker.py` | LLM 成本追踪（16 供应商定价表 + 预算熔断） |
| `loom_core/alert_manager.py` | 告警机制（熔断/DLQ/限流/预算超限通知） |
| `loom_core/quality_feedback.py` | 质量评分历史 + 弱项模式分析 + 写作建议 |
| `loom_core/task_queue.py` | SQLite 持久化任务队列（原子 claim、租约回收、终态只写一次） |
| `loom_core/worker.py` | 常驻 worker：消费队列里的 pending 任务（`--worker`），多 worker 互斥、崩溃租约回收 |
| `loom_core/mcp_server.py` | MCP Server（JSON-RPC 2.0 over stdio，5 tools） |
| `loom_core/openapi_spec.py` | OpenAPI 3.0 规范生成 |
| `loom_core/agent_loader.py` | Agent 安全加载（AST 检查 + 白名单沙箱） |
| `loom_doc/renderer.py` | 渲染层：Markdown → docx（OOXML）/ pdf（ReportLab），双后端可选依赖 |
| `artesian`（独立库，自本仓 `loom_core/` 迁出） | `fast_json`（orjson 优先、stdlib 回退）+ HTML 解析内核兼容层（modest/lexbor 按可用性选，降级显式记录）+ 嵌入层（hash 内置 / local 模型 / API，auto 自动回落）+ 知识库（切块 → 向量化 → SQLite 持久化 → 向量检索）+ 统一搜索引擎接口（10 引擎 + mock 测试桩 + 自带 LRU+TTL 缓存）+ URL 安全校验（SSRF：仅 http/https、拒私网/保留/链路本地、逐跳重定向复验、DNS 结果 TTL 缓存） |
| `loom_core/state_paths.py` | 运行态路径解析（`LOOM_STATE_DIR` / `LOOM_VERSIONS_DIR`），测试与真实运行不再共用一份幂等键历史 |
| `loom_doc/ingest.py` | 摄入层：PDF/图片/文本 → 结构化 Markdown（PDF 按字号推断标题层级） |
| `loom_doc/document_enhancer.py` | 文档增强：对已有 Markdown 逐章节 LLM 深化 + ASCII 图修复 + 导出 |
| `agents/` | 14 个核心 Agent 实现（researcher/fetcher/safe_writer/requirements_analyzer/renderer/ingest/knowledge_base/http_request/transform + keyword_extract/readability/link_check/fact_coverage/tldr） |
| `packs/docgen/` | 物理 pack（spec §5.2）：5 个文档件（writer/quality_gate/checker/layout/fact_checker）+ docgen 系 6 条流水线 + 2 个质量档案 + 2 个 prompt 模板，`pack.yaml` 为清册；引擎经 `loom_core.pack_manifest` 多目录发现，删掉本目录引擎照常工作 |

---

## 配置

### Pipeline 定义（`packs/docgen/pipelines/docgen.yaml`）

```yaml
defaults:
  timeout: 300
  retry:
    max_attempts: 3
    backoff: exponential
    initial_delay: 1.0

agents:
  - name: researcher
    version: "2.0"
    config:
      search_engines: [bocha, tavily, serper, bing, sogou, 360]
      max_results: 20

  - name: writer
    version: "2.0"
    config:
      template: default
      polish_cache_ttl: 3600

  # ... 其余 Agent

topology:
  type: dag
  levels:
    - [researcher]
    - [fetcher]
    - [writer]
    - [quality_gate]
    - [checker]
    - [layout]
    - [safe_writer]
  edges:
    - [researcher, fetcher]
    - [fetcher, writer]
    # ...
```

### 条件节点（`when`）

节点可以声明执行条件；不成立时该节点被跳过，**下游照常执行**（这正是"可选分支"的含义）：

```yaml
agents:
  - name: renderer
    when:
      path: config.format            # 见下方可用路径
      op: "in"
      value: [docx, pdf]

  - name: fact_checker
    when:
      all:                           # 只允许一层组合
        - {path: upstream.quality_gate.overall_score, op: ">=", value: 60}
        - {path: artifacts.results, op: truthy}
```

求值上下文（`loom_core/conditions.py`，纯声明、不 eval）：

| 前缀 | 内容 | 例子 |
|------|------|------|
| `artifacts.*` | 上游按 `PRODUCES` 合并后的产物 | `artifacts.content` |
| `upstream.*` | 某个具体上游节点的原始结果 | `upstream.quality_gate.overall_score` |
| `config.*` | 本节点的节点级配置 | `config.format` |
| `inputs.*` | 调用方传给本子流水线的参数 | `inputs.min_score` |
| `pipeline` / `task.*` | 流水线名 / 任务 id 与输入文件 | `task.input_file` |

算子：`==` `!=` `<` `<=` `>` `>=` `in` `not in` `exists` `truthy` `falsy`。三条硬规矩：

- 写法非法（未知算子、缺 `value`、嵌套 `all/any`、单条件还套组合）在**解析期**报错；
- 数值比较要求两侧同型，`bool` 不与数字比较（Python 里 `True == 1` 会静默成立）；
- **路径取不到就抛错**，整条 run 失败——把它当"条件不成立"就会静默跳过分支却照报 done。
  只有 `exists` / `truthy` / `falsy` 允许路径缺失。

`when` 参与 `topology_hash`：给节点加条件会让既有 lockfile 报拓扑漂移，必须 `loom lock <name>` 重写锁。
不写 `when` 的节点行为与之前完全一致（各内置流水线的 lockfile 指纹实测未变，`tests/test_condition_nodes.py` 逐条校验）。

右值也能从上下文取，这是子流水线参数化的前提：

```yaml
when:
  path: upstream.quality_gate.overall_score
  op: ">="
  value_from: inputs.min_score        # 阈值由调用方传，不抄死在片段里
```

`value` 与 `value_from` 必须恰好给一个。`inputs.*` 若没人提供，解析期就报错而不是等运行期：
`all:` 会短路，缺的参数可能还没被求值到就已经绕过了。

### 子流水线（`call`）

一个节点可以引用另一条流水线，解析期把它**内联展开**成普通节点，因此幂等键、检查点、
重试、熔断、`when`、产物契约一律照旧生效：

```yaml
agents:
  - name: writer
    dependencies: []
  - name: review                 # 节点身份，不是 Agent 名
    call: docgen-verified        # 被引用的流水线（packs/docgen/pipelines/docgen-verified.yaml）
    dependencies: [writer]
    inputs:                      # 传给子流水线的参数，片段用 when 的 inputs.* 读
      fact_check: true
      min_score: 70
  - name: publish
    dependencies: [review]       # 自动接到子流水线的出口节点
```

- 内联进来的节点带别名：`checker` → `checker__review`，所以**同一个 Agent 可以在一张
  图里出现多次**而互不覆盖；`agent_of()` 仍还原成 `checker` 去查注册表。
- 环与深度在解析期拒绝：`a → b → a` 报"循环引用"，超过 3 层（含叶子）报"嵌套超过上限"。
- `call` 节点不接受 `config` / `when` / `pool_size` / `rate_limit` / `foreach` —— 配置属于
  子流水线，父子各写一份会变成两处真相；`foreach` 更是没有对象（展开后"本节点"不存在）。
- **被引子流水线改了，调用方的 lockfile 会报拓扑漂移**（展开后的图算指纹，
  `inputs` 的实参也算），必须 `loom lock <name>` 重写锁。

参数（`inputs`）的作用域：

| 写在哪 | 含义 |
| --- | --- |
| `call` 节点 | 这次调用传给子流水线的实参 |
| 普通节点 | 自己的默认值，仅当它的 `when` 读到 `inputs.*` 才允许写 |

内联时按"节点默认值 ← 调用方实参"合并，且只把该节点确实会读的那几个键落到节点上；
更深一层的 `call` 有自己的作用域，外层参数不会灌进去。参数双向都在解析期查：
引用了没人传 → 报错（`all:` 会短路，缺参到运行期可能被静默绕过）；传了没人读 → 报错
（`min_scor: 70` 这类拼写错误原本会静默走默认值）。

出厂片段 `packs/docgen/pipelines/_quality-tail.yaml`（质量尾：quality_gate → checker → 可选
fact_checker → layout → safe_writer）被 docgen / docgen-render / docgen-verified /
docgen-lean / docreq / kb-docgen 六条流水线引用；抽取前逐字节比对过这六份尾巴，
`quality_gate/checker/layout/safe_writer` 的 version/timeout/config 完全一致，
差异只有 fact_checker 的有无与门槛，于是收敛成两个参数：`fact_check: false`（docgen /
docgen-render / docreq / kb-docgen）、`{true, 0}`（docgen-verified，无条件核查）、
`{true, 70}`（docgen-lean，达标才核查）。

`_` 前缀 = 内部片段：不进 `--pipeline` 候选清单（`installed_pipelines` /
`list_pipelines` / run.py 三处口径一致），但仍能被 `call` 加载；片段必须与引用它的
YAML 同目录。它自带默认值，所以也能单独解析与加锁。版本锁定靠调用方的 lockfile：
片段的节点、连线、配置哈希与实参都进了父图的 `topology_hash` / `config_hash`。

### 逐项展开（`foreach`）

`when` 决定"这个节点跑不跑"，`foreach` 决定"这个节点跑几次"——按上游列表**逐项**调用
同一个 Agent，再把 N 份结果聚合成一份产物：

```yaml
agents:
  - name: http_request
    config:
      url: https://api.github.com/repos/apache/kafka/issues?state=open
      expect: json
  - name: digest
    dependencies: [http_request]
    foreach:
      over: artifacts.response    # 与 when 同一套点号路径；取不到值就报错
      max_items: 5                # 默认 64；超限报错而不是截断
    config:
      template: "- [#{{item.number}}] {{item.title}}（第 {{index}}/{{count}} 条）"
```

- 展开发生在**一次节点执行内部**：DAG 里仍是一个节点（不新增节点身份、不改层级），
  lockfile、检查点、重试、熔断都按节点粒度照旧。
- 每一项拿到三个引擎自有键：`item`（当前项）、`index`（序号）、`count`（总项数）。
  它们是引擎自有键，谁声明成同名产物都顶不掉——被顶掉之后每一项看到的是同一份数据，
  渲染出 N 份相同内容却一声不响。
- 每一项一个独立幂等键（后缀 `#{i}`）。共用前缀的话第 2 项起会命中第 1 项的缓存 ——
  表现是"展开了 N 次、只执行了 1 次"，而产物看起来是完整的。
- 限流按项领令牌：一次节点执行现在等于 N 次对外调用，只领一次等于把作者设的速率整倍放大。
- 聚合结果 `{status, count, items: [逐项原始结果], ...按 PRODUCES 聚合的产物}`。
  str 产物按行拼接、list 产物首尾相接、其余类型收成列表 —— 不给"取最后一项"这种
  覆盖语义，覆盖在逐项展开里等于"只剩第 N 项"。
- **任一项失败 ⇒ 整个节点失败**（含订阅者返回 None 的超时项）；`hard_floor` 逐项累积。
  零项展开、非列表、`over` 取不到、超过 `max_items` 都是显式错误：静默少跑比失败难查。
- 不与 `pool_size>1` / `call` / 质量重做（`REGENERATION_*`）/ 落盘（`WRITES_OUTPUT`）同用。
  前两者的展开分配语义没有唯一答案，后两者的操作对象是"整份产物"，逐项展开里没这个东西；
  要落盘就把落盘节点排在 foreach 之后，让它拿聚合产物。
- 展开契约（`over` 与 `max_items`）进 `topology_hash`：改了必须 `loom lock <name>` 重写锁，否则运行时
  报拓扑漂移。锁的是**声明**而不是运行期解析出的项数——项数来自数据，每次跑都可能不同。

出厂消费者 `pipelines/api-digest.yaml`（HTTP 列表接口 → `transform` 逐项渲染 → `safe_writer`
落盘），判据与回归见 `tests/test_foreach.py`（含离线跑通整条 Scheduler → DAGExecutor 链的
真跑用例）。

### 审批闸门（`approval`）

流水线里声明"跑到这里停下来等人批准"的节点（spec §9 决策 C 最小闭环）：

```yaml
agents:
  - name: release_gate
    approval:
      message: "确认对外发布"        # 审批人该看到的话（声明进锁指纹，改了要 loom lock）
    dependencies: ["writer"]
  - name: publish
    dependencies: ["release_gate"]   # 放行后才跑；驳回则整任务终止
```

语义：

- 执行器推进到闸门所在层级前把任务置为 `paused`（复用暂停边界的等待与 checkpoint；
  `GET /tasks/<id>` 的 `awaiting_approval` 字段里有节点名与 message）；
- `POST /tasks/<id>/approve`（body 可选 `note`）放行：闸门节点记 success，结果形如
  `{"status": "approved", "note": ...}`——下游可用 `when` 读 `upstream.<gate>.status`；
- `POST /tasks/<id>/reject`（body 可选 `reason`）驳回=**终止**：任务转 `cancelled`，
  原因进任务 error；
- **审批等待中的任务拒绝 `resume`**（直通等于绕过审批，只认 approve/reject）；
- 决策落审计 `audit/approvals.jsonl`（任务/节点/决定/note/时间/审批人标识）；
- `when` 不成立的闸门不挂起，按条件跳过（与普通条件节点同语义）。

- **重启后仍可批**：待批与决策落 `runs.db` 的 `approvals` 投影，并在同一条事务里追加
  `approval.requested/decided/consumed` 事件（跟随 `LOOM_STATE_DIR`）。进程重启后
  `POST /tasks/<id>/approve` 把队列行放回 `pending` 并带续跑标记，worker 从 checkpoint
  续跑越过闸门；驳回则把行置 `cancelled`、不再执行。判据 `tests/test_approvals.py`
  含真子进程硬退出在闸门上、父进程批准后完成的端到端，`tests/test_runstore_approvals.py`
  钉"投影与事件同事务""决定只有一个时刻""老台账按数据边界回落"；
- **老台账 `approvals.db` 只读不再写**（2026-10-23 起）：本批之前落进去的行仍读得到、
  仍批得掉（升级前停在闸门上的运行不能升级后批不掉），但不 backfill、也不再有新增行；
  老行耗尽后该文件只剩历史；
- **台账不可用 = 审批不可用**（刻意的 fail-closed）：`runs.db` 打不开时登记待办报错、
  `approve()` 拒绝放行，任务留在 `PAUSED`——写不进台账却回答"已批准"等于执行一个没有
  持久授权的决定。原因看 `/api/health/deep` 的 `runstore` 组件；
- **批准方与执行方可以是两个进程**：执行方还活着停在闸门时，别的进程批准只写台账，
  暂停循环自己观察到决定并继续跑；同一时刻队列不会把这行重排给第二个执行者
  （活 owner 守卫），前序节点不重跑（判据挂在真实执行漏斗上计数，不是状态推断）；
- **待办收件箱**：`GET /api/approvals`（非 admin 只看自己的，admin 见全量），控制台
  "待审批"卡片可直接放行/驳回。

边界（P1 余量，刻意不做）：不提供"驳回到另一分支"、无通知渠道、无多级审批与超时策略；
"审批人"当前记客户端地址 + 凭证指纹（真名实姓等 P1 身份系统）。
存活探测按**单机**解释（pid 是否活着只在同一台机器上有意义）：跨主机接管同一队列需要
共享 pid 命名空间，未做之前多机审批不宣称可用。
判据见 `tests/test_approval_gate.py`（解析/清册/执行/条件闸门/HTTP 与审计五层）。

### 通用 Agent（`http_request` / `transform`）

引擎不止能写文档：这两个 Agent 不带任何领域语义，配起来就是一条"取数据 → 变形 → 落盘"
的通用工作流，出厂流水线 `pipelines/api-report.yaml` 就是它们的真实消费者（也是接线判据，
`tests/test_generic_agents.py` 离线跑通整条 Scheduler → DAGExecutor → safe_writer 链并断言落盘产物）。

`http_request`：出网取一个接口。

| 配置 | 默认 | 说明 |
|------|------|------|
| `url` | 必填 | 也可留空、由上游产物给（`payload["url"]`）；只接受 `http/https`，指向内网/回环/链路本地地址默认**拒绝** |
| `method` | `GET` | `GET/POST/PUT/PATCH/DELETE/HEAD`，其余该节点直接失败 |
| `headers` | `{}` | 原样发出；进产物的是**响应**头，且 `authorization`/`token`/`secret`/`cookie`/`api-key` 一类值恒为 `***` |
| `body_artifact` | `""` | 用哪个上游产物当 JSON 请求体；点名了却不在上游产物里 → 节点失败 |
| `expect` | `json` | `json` 解析失败即节点失败（错误里带响应前 500 字符）；`text` 原样传下游 |
| `timeout_s` | `15` | 单次请求超时 |
| `max_bytes` | `200000` | 超限**中止并报错**，不截断——残缺 JSON 会让下游拿到看似合法的坏数据 |
| `allow_hosts` | `[]` | 内网目标的显式白名单（比对 hostname **全等**，不做前缀匹配）；命中时产物带 `guard: "allowlist"`，不静默放行 |

重定向恒不跟随（`allow_redirects=False`，不是配置项）：3xx 只回 `redirect_to` 就返回，
且不进解析——重定向响应体常为空，让"不是合法 JSON"抢先判死，作者就看不到 Location 了。

`transform`：声明式的胶水，替代"为每个接口写一个 Agent"。

```yaml
- name: transform
  dependencies: [http_request]
  config:
    items: artifacts.response.items       # 给了就按列表逐项处理；不给只渲染一次
    fields: [name, id]                    # 每项挑哪些字段（缺字段 → 节点失败，不静默丢）
    where: {path: item.enabled, op: truthy}  # 逐项过滤；与 when 同一套受限语言
    set:                                  # 派生值：{name, from}
      - {name: total, from: "len(items)"}
    template: |                           # 渲染一次，不是逐 item 渲染
      # 共 {{len(items)}} 条
      - 首条：{{items.0.name}}（{{items.0.id}}）
```

路径与 `when` 同源（`conditions.resolve`），因此支持列表下标：`artifacts.items.0.id`。
`where` 与 `fields` 都要求给 `items`——没有列表时它们无物可作用，而这属于"写了没人读"，
一律报错而不是静默出厂全量。
模板只认 `{{路径}}` 与 `{{len(路径)}}`（`len` 是唯一被放行的函数：想要别的运算就该用条件
或换 Agent，在配置里塞表达式求值器等于把数据通道变成代码通道），取不到值就判失败而不是
渲染成空字符串——空产物比报错难查得多。产物键：`data`（结构化，逐项处理时是列表）、
`text` / `content`（渲染结果，同一份，方便直接接 `safe_writer`/质检那批按 `content` 取正文的节点）。

### Quality Profile（`packs/docgen/quality/`）

```yaml
# technical-doc.yaml
name: technical-doc
threshold: 70
# 权重维度与 quality_gate._score_all 的产出一一对应（总和 = 1.0）
weights:
  completeness: 0.18
  structure: 0.12
  readability: 0.10
  citation: 0.12
  depth: 0.10
  substance: 0.18
  topic_relevance: 0.20
style_rules:
  - id: broken_links
    pattern: '\[([^\]]*)\]\(\s*\)'
    penalty: 15
    message: 空链接/危险协议
citation:
  enabled: true
  penalty_per_issue: 5
  check_url_format: true
```

权重只在 Quality Profile 一处定义;style 不作为评分维度——风格问题按
`style_rules` 以扣分形式计入（与 citation 扣分共享 `max_penalty` 上限）。

运行时通过 pipeline YAML 的 `quality_profile` 切换。

### 生产环境配置

项目提供 `config.production.json` 模板，与默认 `config.json` 的区别：

| 配置项 | 默认 (config.json) | 生产 (config.production.json) |
|--------|-------------------|------------------------------|
| `researcher.search_engines` | `["bing", "sogou", "360"]`（免费 HTML 兜底引擎） | `["bocha", "tavily", "serper", "bing", "sogou", "360"]`（含付费 API 引擎） |
| `fail_fast` | `true` | `false`（单 Agent 失败不中断流水线） |
| `researcher.max_workers` | 3 | 5 |
| `researcher.cache_size` | 1000 | 2000 |
| `admin_api.host` / `admin_api.port` | `127.0.0.1` / `8910`（由 CLI 启停） | `0.0.0.0` / `8910`（**非回环绑定必须设置 `ADMIN_API_KEY`，否则拒绝启动**） |

```bash
# 使用生产配置
python run.py input.md -c config.production.json -o output/doc.md
```

---

### 保留期与回收（`retention.*` / `loom --gc`）

长跑的 worker 会攒下所有观测面：运行投影、事件、审批、队列、成本、质量反馈、
checkpoint、交付账本、死信。默认**一个都不删**——保守默认比"替你清掉数据"更容易恢复。

| 配置键 | 默认 | 语义 |
|--------|------|------|
| `retention.runs_days` | `0`（不启用） | 年龄判据：终态运行超过 N 天就回收 |
| `retention.max_runs` | `0`（不启用） | 数量判据：只保留最近 N 条终态运行 |
| `maintenance.gc_ticks` | `60` | worker 每 N 个轮询跑一次 GC（**有活没活都算**） |
| `maintenance.gc_seconds` | `600` | 常驻服务（`--admin`/`--dashboard`/`--daemon`）两次回收之间的秒数；`0` = 服务模式不跑 |

两条保留期判据任一 > 0 即启用；配成负数或读不懂的值一律按 `0` 处理——配置写错
不该悄悄变成"删光一切"。覆盖面：`runs`/`run_nodes`/`events`/`approvals`、任务队列、
成本、质量反馈、checkpoint（仅在 `runs_days > 0` 时）、交付账本、以及消息总线的死信表。

手动跑一次并看清每张表删了多少：

```bash
python run.py --gc
# [gc] 保留 30 天 / 最近 500 条
#   run_store: approvals=1 events=9 run_nodes=5 runs=3
#   deliveries: 2 行
#   dlq: 0 行
#   合计删除 20 行
```

三条边界要说在前面：

- **进行中的运行一行不碰**——把活运行从观测面抹掉比不删更糟，它还在写投影。
- **只删库里的行，不删磁盘产物**：`output_path` 指向你自己声明的输出目录，删它
  等于删你的文件。账本行走了文件还在，核对靠 `output_sha256` 与逐节点摘要。
- **历史行不 backfill**：A6 之前写入的账本行没有逐节点摘要，读侧如实显示"未记录"，
  不会补算一个当时并不存在的值。

## 管理 API

启动：`python run.py <input> --admin` （默认 `http://127.0.0.1:8910`）

**并发模型（2026-10-13，spec §3 P1 决策 A）**：HTTP/1.1 keep-alive + **有界线程池**
+ socket 读/写超时。取代旧的 stdlib HTTP/1.0「线程-per-连接、无上限」形态：
dashboard 轮询不再每 5 秒新建 TCP+线程；慢请求/慢客户端/半开连接不会无限占线程。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LOOM_ADMIN_MAX_CONCURRENCY` | 32 | 并发处理上限；池满即 `503` + `Retry-After: 1`（不排队、不无限起线程） |
| `LOOM_ADMIN_SOCKET_TIMEOUT` | 30 | socket 读/写超时（秒）；半开连接与空闲 keep-alive 连接超时后由服务端关闭并收回工位 |

| 端点 | 方法 | 说明 |
|------|------|------|
| **任务管理** | | |
| `/tasks` | GET | 任务列表 |
| `/tasks/<id>` | GET | 单任务详情（含 result/output_content/output_path） |
| `/tasks/<id>/cancel` | POST | 取消任务 |
| `/tasks/<id>/rerun` | POST | 重跑流水线（复用 last plan） |
| `/api/tasks` | POST | 提交新任务（同步/异步，外部 Agent 调度入口） |
| **Agent 管理** | | |
| `/agents` | GET | 已注册 Agent 列表 |
| `/api/agents/` | GET | Agent 详情（含 stats/meta/熔断器状态） |
| **配置与成本** | | |
| `/api/config` | GET | 运行时配置快照 |
| `/api/config` | POST | 运行时配置变更（同时写覆写文件，其他进程随之生效） |
| `/api/config/reload` | POST | 配置热更新（通知所有 Agent + 广播 revision） |
| `/api/cost` | GET | LLM 成本统计（按供应商/Agent/时间维度） |
| `/api/cost/budget` | POST | 设置预算上限（超限自动熔断） |
| **质量与日志** | | |
| `/api/quality/feedback` | GET | 质量评分历史 + 弱项模式分析 |
| `/api/logs` | GET | 结构化日志查询（level/agent/since/limit 过滤） |
| `/api/alerts` | GET | 告警历史查询（level/category/limit 过滤） |
| **版本管理** | | |
| `/api/versions` | GET | 文件版本历史 |
| `/api/versions/diff` | GET | 对比两版本差异 |
| `/api/versions/rollback` | POST | 回滚到指定版本 |
| `/api/versions/stats` | GET | 版本管理统计 |
| **死信队列** | | |
| `/dlq` | GET | 死信队列列表 |
| `/dlq/<id>/replay` | POST | 重放死信 |
| **事件钩子** | | |
| `/api/events/hooks` | GET | 列出已注册事件钩子 |
| `/api/events/hooks` | POST | 注册事件钩子（SSRF 防护，拒绝私有 IP） |
| `/api/events/hooks/<id>` | DELETE | 注销事件钩子 |
| **缓存** | | |
| `/api/cache` | GET | 缓存统计 |
| `/api/cache/clear` | POST | 清空所有缓存 |
| **快捷操作** | | |
| `/cancel` | POST | 取消任务（body 传 task_id） |
| `/pause` | POST | 暂停任务 |
| `/resume` | POST | 恢复暂停的任务（审批等待中的任务会被拒——那种 PAUSED 只认 approve/reject） |
| `/approve` | POST | 审批放行（审批闸门等待中的任务；body 可选 `note`） |
| `/reject` | POST | 审批驳回=终止（body 可选 `reason`，进任务 error 与审计） |
| `/replay` | POST | 重放死信（body 传 dlq_id） |
| `/rerun` | POST | 重跑流水线（body 传 task_id） |
| **健康与指标** | | |
| `/health` | GET | 总线 + Registry 健康状态（免鉴权） |
| `/api/health/deep` | GET | 全组件深度健康检查 |
| `/metrics` | GET | Prometheus 格式指标 |
| `/api/pipeline` | GET | 流水线配置信息 |
| **流式与规范** | | |
| `/stream` | GET | SSE 流式推送文档生成进度（支持 Last-Event-ID 重连） |
| `/stream/metrics` | GET | 流式指标快照（JSON） |
| `/api/openapi.json` | GET | OpenAPI 3.0 规范 |
| `/api/dashboard` | GET | Dashboard 数据 |
| **静态资源** | | |
| `/` | GET | API 纯文本索引（**非仪表盘**） |
| `/index.html` | GET | 静态仪表盘入口（若 `--dashboard` 启动） |

### 鉴权

通过环境变量 `ADMIN_API_KEY` 或 `AdminAPI(api_key=...)` 启用：

```bash
export ADMIN_API_KEY="your-secret-key"
python run.py input.md --admin
```

客户端请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`（仅 SSE 场景才建议 query token，
常规请用 Authorization 头，避免凭证落入访问日志）。
未配置时禁用鉴权（仅本机访问）。`/health` 与静态资源始终免鉴权。

#### Token 身份与多租户（`tokens.yaml`，spec §5.4）

在单密钥（等价 admin）之上，用 `tokens.yaml`（模板 `tokens.example.yaml`；路径可被
`LOOM_TOKENS_FILE` 覆盖）把凭证升级为**身份**——每个 token 解析出一个 Principal，
**任务与成本按 Principal 隔离**：

```yaml
tokens:
  - id: alice
    tenant: acme
    token_env: LOOM_TOKEN_ALICE     # 密钥经环境变量注入，文件只放 id/租户
  - id: ops
    admin: true                     # 跨租户全可见的运维位
    token_env: LOOM_TOKEN_OPS
```

行为要点：

- **启用后回环访问同样要求凭证**（启用身份却对回环免检等于没启用）；未放置
  `tokens.yaml` 时行为与既往完全一致（`ADMIN_API_KEY` 单 key 或本机信任模式）；
- `ADMIN_API_KEY` 继续有效（解析为 admin）——旧部署零改动；
- 非 admin 只见自己名下的任务；**越权访问与"不存在"同形（404）**，不泄露存在性；
  cancel / pause / resume / approve / reject / rerun 一律受作用域约束；
- `GET /api/cost` 非 admin 只统计自己任务的成本（`scope: principal`）；
- 缺环境变量的 token 条目**不可用且启动告警**（该凭证一律 401，不会静默放行）；
- 触发类任务（cron / webhook / MCP）分别记 `trigger:<名>` / `webhook:<名>` / `mcp` 归属。

### MCP Server

通过 `--mcp` 启动 JSON-RPC 2.0 over stdio，供外部 Agent（如 Claude Desktop）调度：

```bash
python run.py --mcp
```

提供 6 个 tools：`generate_document`、`run_workflow`（通用入口：任意已安装流水线 +
任意 `inputs`，对象按「## 键」序列化为输入文档、字符串原样写入）、`get_task`、
`list_tasks`、`list_pipelines`、`get_pipeline_info`。`get_task` 与 `GET /tasks/<id>`
返回的 `artifacts` 字段来自同一个原语（见下）。

### 编程句柄 `RunHandle`

嵌入式调用方（MAOP 适配器、自研服务）拿的不该是一个裸 `task_id` 加一堆约定：

```python
handle = orch.run_handle(task_id)     # 任务不在内存时也能问状态（回落持久队列）
handle.status()                       # "pending" / "running" / "paused" / "done" / …
handle.wait(timeout=60)               # 阻塞到终态；超时返回最后状态而不是抛异常
handle.artifacts()                    # [{"kind": "artifact", "node": …, "keys": […]},
                                      #  {"kind": "deliverable", "path": …, "bytes": …}]
handle.cancel(); handle.resume()      # 引擎拒绝（已完成 / 停在审批闸门）就返回 False
```

`PAUSED`（停在审批闸门前）算 `wait()` 的终态——等的是人，不该把调用方一起挂住。
产物只报引擎确实记下的东西，不按节点名造伪 ID。判据 `tests/test_run_handle.py`。

任务内存历史只留最近 100 个（`pipeline._trim_task_history`），更早的运行会被淘汰。
**淘汰不等于查不到**（2026-10-21 Stage 2 起）：内存只是缓存，`RunHandle` 与
`PipelineOrchestrator.list_runs` 先读 RunStore 的 `runs` / `run_nodes` 投影——
`/tasks`、`/tasks/<id>`、`/api/dashboard`、`/api/cost` 的作用域集合、MCP
`list_tasks` / `get_task` 在内存淘汰后仍答得出来，载荷里的 `source` 字段标明这一行
来自 `memory` / `runstore` / `deliveries`。投影里没有这条运行（投影写失败，
或 Stage 1 之前没有投影的老运行）时才回落到交付账本——这是**数据边界**，不是开关：
`LOOM_RUNSTORE=0` 那条退路已在 2026-10-21 移除，写侧恒开、失败由
`/api/health/deep` 的 `runstore` 组件报出来。四条边界：
①本改动（2026-10-15）之前落账的运行没有 `artifact_keys` 这批字段，其 `artifacts` 是空表——
**不回填、不猜**；②读路径只读，账本/投影文件不存在就返回空，绝不因为"查一下"而创建数据库；
③淘汰后的详情给 `pipeline` / `error` / `artifacts` / 逐节点摘要（status、attempts、耗时、
artifact_keys），但**不给 `result` 正文本**——它从不落盘（投影只存摘要），不编空值冒充；
④`cancel` / `resume` / 审批仍只读内存：**动作**只有内存态才有意义，淘汰后照旧 404。

**回落会掩盖缺失，所以补了一个健康位**（2026-10-24 Stage 3b）：队列行带 `projection`
列，`ok` = 投影盖章落了，`incomplete` = 想写没写成（待补账），`legacy` = 走的是从不写
投影的历史兜底路径（按设计缺，不是故障）。顺序是**先悲观后升格**——终态先按
`incomplete` 写队列行，投影确认落地才升 `ok`；反过来会开一个"队列说一切正常、投影里
查无此运行"的窗口。口径是"标 incomplete 后继续"：观测面故障不放大成可用性故障，但缺口
必须可发现。`RunHandle.projection()`（`complete` / `incomplete` / `legacy` / `unknown`）
与 `run_store_health()` 的 `incomplete_runs` / `legacy_runs` 把它报出来，`to_dict()` 里
也有 `projection` 键。**标记优先于"投影里查得到行"**：终态盖章失败时 `runs` 那一行会
永远停在 `running`，只看有无行会把它读成正常完成。补账工具（重放终态）本批未做。

> **B8 引擎收敛（2026-10-27）**：`orch.run()` 现在委托给 `run_plan()` 统一引擎执行，
> legacy 路径不再"按设计缺投影"——它经由 `run_plan()` 写真实 RunStore 投影，`projection`
> 标记从 `legacy` 升为 `ok`。`legacy` 值仍保留给 Stage 1 之前落账的历史运行。

同批摘掉的一处写面：**队列不再承载结果正文**（`result_json` 列停写、也不再 SELECT）。
实测全仓没有读者——`RunHandle` 与 `_queue_row` 只用 `status`；结果正文的事实源是
`run_nodes.result_json`（以及本批之前落账的 `deliveries.payload_json`）。老库里的列与
行留在原处（不回填、不 DROP）。`recover()` 读的 `status='running'` + `owner_pid` 存活
**保留**：那是租约判据，不是运行事实的第二份拷贝。

`/stream` 的重连也走同一份投影（2026-10-21 Stage 2b）：没有进程内回调时按 `events`
重放运行级事实，帧 id 是运行级游标 `r<seq>`（`Last-Event-ID: r<seq>` 可续传），
运行没结束就尾随到终态；**重连不会重新起一条流水线**。逐 chunk 的正文流从不落盘，
不在重放范围内——重放给的是"这次运行发生过什么"，不是正文。

**看得见就得能处理**（2026-10-25 补账工具）：`incomplete` 的运行用命令重投影，事实
来源只有交付账本：

```bash
python run.py --replay-incomplete --replay-dry-run   # 先看会补什么（一个字节都不写）
python run.py --replay-incomplete                    # 全部补账
python run.py --replay-run <run_id>                  # 按 id 补一条
```

边界照实写：只碰队列里 `projection='incomplete'` **且已终态**的行（运行中的行不补，
那是与执行方抢写）；**只补缺**——已有的 `run_nodes` 行不被事后重建覆盖；账本没有节点
的绝对时间，所以补写的节点行时间列留 0（读侧因此不显示时长，事件里记
`"times": "unavailable"`）；队列 / 账本 / 投影三方口径不一致一律**拒绝**，不猜哪个对。
每次成功补账追加一条 `run.replayed` 事件——事后重建必须与原始执行分得开。
退出码：`--replay-run` 被拒绝或失败 ⇒ 1，别让脚本把"没补"当成"补过了"。

同批把**终态事件"至多一条"从调用方约定下沉成存储层的原子裁决**：`close_run` 现在带
`WHERE status NOT IN (done, failed, cancelled)`，所以补账、重启后的重放、两个进程同时
收尾都不会再往审计里塞第二条终态事件（此前只靠 `_claim_terminal_event`，那是**进程内**
的去重，跨进程与事后重放都绕过它）。

**账本与锁要能在 HTTP 侧读**（2026-10-25，#8）：上面那两条命令的输入源——交付账本——
此前只有 CLI 与进程内读得到，运维判断"该不该补"必须先看见它。新增三个只读口：

| 口 | 给什么 |
|----|--------|
| `GET /api/deliveries` | 最近交付（`?limit=`，默认 50、夹到 200），逐行带 `projection` 健康位与交付物 `output_sha256` |
| `GET /api/deliveries/<run_id>` | 整条结构化载荷：逐节点状态/耗时/`artifact_keys` |
| `GET /api/locks` | 逐流水线锁巡检（`?pipeline=<name>` 只看一条） |

两条口径要说在前面，别让响应被读成别的含义：

- **`/api/locks` 回答的是"当前 YAML 与当前锁对不对"**，响应里的 `scope: "current files"`
  就是钉这一点的。它**不回答**"某次运行时用的锁对不对"——运行时的 `config_hash` /
  `topology_hash` 只在校验那一瞬存在于内存，从未落库，所以那件事今天无法证据化，
  也不拿今天的值冒充当时。三值 `matches`：`true` 比过且一致、`false` 漂移（`issues`
  给差在哪）、`null` 没有锁可比或 YAML 解析失败——"没发现问题"与"没法判断"不混。
  巡检与执行共用同一处锁路径解析（`Scheduler.resolve_lock_path`），否则会报出执行时
  根本不会发生的漂移。
- **交付账本自己没有归属列**，所以非 admin 的可见性是逐行反查投影的 `principal`：
  CLI 直跑、投影里没有的运行，`projection` 报 `unknown` 且**只有 admin 看得见**。
  越权与不存在给同一个 404（存在性本身就是信息）；账本读不动给 503，**不降级成空列表**。

看到 `projection: "incomplete"` 就走到上面那条 `python run.py --replay-run <run_id>`——
**能不能补由补账工具判**（它拒绝时会说清是运行中、三方冲突还是已升格），这里不复制那条规则。

### 类型化运行输入（2026-10-26，#7）

流水线可以在 YAML 里声明运行输入的**结构与约束**，四个调用面（CLI / HTTP / MCP / webhook）
在提交那一刻按同一份声明校验，非法载荷被拒且错误**定位到字段**：

```yaml
pipeline:
  inputs:
    type: object
    required: [topic]
    additionalProperties: false      # 本实现只支持 true/false
    properties:
      topic: {type: str, minLength: 2, maxLength: 40}
      depth: {type: int, minimum: 1, maximum: 5, default: 3}
      tags:  {type: list, items: {type: str}}
      mode:  {enum: [fast, full]}
```

```bash
python run.py -p mypipe --set-input topic=云成本 --set-input depth=3
python run.py -p mypipe --inputs-json '{"topic": "云成本", "tags": ["a", "b"]}'
curl -X POST localhost:8080/api/workflows/mypipe/runs \
     -H "Authorization: Bearer $ADMIN_API_KEY" -d '{"inputs": {"topic": "云成本"}}'
curl localhost:8080/api/workflows/mypipe/inputs     # 读回声明，客户端据此生成表单
```

支持的 keyword 是**声明式子集**（`type`/`required`/`enum`/`pattern`/`minimum`/`maximum`/
`minLength`/`maxLength`/`default`/`items`/`properties`/`additionalProperties`），不是完整
JSON Schema：本仓不为此新增第三方依赖，`type` 沿用引擎既有类型名（`str`/`int`/`float`/
`bool`/`list`/`dict`），同一个值只有一种叫法。写在这套子集之外的 keyword（含拼错的
`max_length`）在**解析流水线时**就报错——被静默忽略的约束等于那条约束从没存在过。

三条边界要说在前面：

- **声明不进锁指纹**（`config_hash` / `topology_hash` 都不含它）。代价照实说：改 schema
  不会被锁拦下。收益是随包 20 条流水线不必因为加了这个字段而全部重锁。
- **声明写错 = 解析期红**，不留到提交期：一条拼错名的流水线如果照常放行，它从来没有
  过 schema，而没人会去查。
- **自由文本入口在声明存在时被拒绝**。`POST /api/tasks` 的 `query`、MCP
  `generate_document`、CLI 的位置参数 `input`、webhook 的非 JSON 文本载荷传的是整篇
  文本，没有可校验的键→值结构；流水线一旦声明了 `inputs`，这些入口会明确拒并指出该用
  哪个口。**没有声明的流水线一切照旧**（随包 20 条行为不变，含渲染出的文档逐字节相同）。

恢复（`--recover`）与 worker 取的是**已渲染的输入文件**，不过这道门——它们重放的是
当初已经通过校验的那份载荷，再校验一次只会因为 schema 演进把老任务卡死。

### 定时触发（Cron）

按 `triggers.yaml` 的 cron 常驻调度（本地时间；**错过不补跑**，跳过窗口会打印出来）：

```bash
python run.py --triggers-dry-run    # 先核对各触发最近 3 次时刻，再启用
python run.py --triggers            # 常驻调度（Ctrl+C 退出）
```

配置模板见根目录 `triggers.example.yaml`（复制为 `triggers.yaml`，该文件已在
`.gitignore` 中，属个人调度配置）：

```yaml
triggers:
  - name: weekly-report
    pipeline: docgen-render
    cron: "0 9 * * 1"              # 每周一 09:00（五字段：分 时 日 月 周，周 0=周日）
    inputs: |
      本周技术周报
    output: output/weekly_report.md
```

cron 支持 `*`、`a`、`a-b`、`*/n`、`a-b/n`、`a/n`、逗号列表与 `@daily` 等宏；
「日」与「周」都受限时命中任一即触发（Vixie cron 的 OR 语义）。

定时触发的 run 与手动 `python run.py` **走同一条提交路径**（`run_plan`），
因此和任何任务一样出现在同一任务队列与观测面（`list_tasks` / `GET /tasks` / 仪表盘）。

### 入站 Webhook（外部服务触发）

管理 API（`--admin` / `--dashboard` / 纯服务模式）提供入站端点：

```bash
POST /api/webhooks/<name>        # <name> 在 webhooks.yaml 里定义（模板见 webhooks.example.yaml）
```

- **鉴权独立于 `ADMIN_API_KEY`**（外部服务不该持有管理端凭据），每个 webhook
  一个密钥、经 `secret_env` 指向环境变量注入；两种携带方式二选一：
  `X-Webhook-Signature: sha256=<HMAC-SHA256(secret, 原始请求体)>`（推荐，GitHub 风格）
  或 `X-Webhook-Token: <secret>`。**密钥未配置 = 拒绝一切（503，fail-closed）**。
- **审计留痕**：每次调用无论成败都落一行 JSON 到 `<状态目录>/audit/webhooks.jsonl`
  （时间 / webhook / 来源地址 / 方案 / 结果与 HTTP 状态 / task_id 或拒绝原因）；
  写盘失败会在响应里如实回报 `audit_error`，不静默。
- **请求体即运行输入**：JSON 对象按「## 键」渲染成输入文档，其余原样写入；
  提交走 `run_plan(wait=False)`，任务出现在同一队列与观测面，响应 `202 + task_id`。

```bash
# 例：HMAC 签名调用（密钥在环境变量 LOOM_WEBHOOK_CI_SECRET）
body='{"ref":"main"}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$LOOM_WEBHOOK_CI_SECRET" | awk '{print $2}')
curl -X POST http://127.0.0.1:8910/api/webhooks/ci-deploy \
     -H "X-Webhook-Signature: sha256=$sig" -d "$body"
```

`GET /api/webhooks`（管理鉴权）可查看已配置的 webhook 清单（不含密钥值）。

### 多机 worker（共享一份任务队列）

`--worker` 可以起多份、跑在多台机器上。

**状态后端可换**（2026-10-12，spec §5.4 P1-B）：`LOOM_DATABASE_URL`
指向 Postgres 时队列走 PG（`pip install nexusloom[postgres]`），多机无需共享
文件系统、由数据库提供并发事务；不设则维持 SQLite WAL——`LOOM_STATE_DIR`
指向**同一份持久卷**（跨机共享需要是真共享文件系统，不是本地盘各自一份）。

```bash
# SQLite（默认）：同一份 state 目录
LOOM_STATE_DIR=/mnt/shared/dp-state python run.py --worker --worker-id=node-a

# Postgres：一台库，多机直连（无共享盘要求）
LOOM_DATABASE_URL=postgresql://user:pass@db.host:5432/loom \
  python run.py --worker --worker-id=node-a
```

并发契约由 SQL 守卫保证，不靠时钟与运气：

- **同一任务绝不双跑**：`acquire` 是「SELECT pending 一条 → UPDATE ... AND status='pending'」，
  靠 `cursor.rowcount` 判定归属，两个 worker 同时抢只有一方 rowcount=1（跨进程实证见
  `tests/test_multi_process_workers.py`：8 个真进程抢 6 条任务，每条恰好被抢到一次；
  摘掉那条 `AND status='pending'` 守卫，该用例当场转红）；
- **崩溃可接管**：owner 进程被强杀后行会停在 running，别的 worker 的 `reclaim_stale`
  在「`owner_pid` 已死 **且** 超过 `--lease-stale`」时把它翻回 pending 重跑（实证：真子进程
  被 kill 后被另一进程回收）；
- **活着的 owner 不会被抢**：只有 `_pid_alive(owner_pid)` 为假才回收——否则会把正在跑的
  任务翻回 pending 造成双跑（实证：持任务的子进程存活期间，另一进程 `recover` 返回 0）。

> 边界：`_pid_alive` 的判据是**同机 pid 表**，所以「跨机接管」只在共享文件系统 + 共享 pid
> 命名空间（同一容器/同一主机）下成立；真跨主机场景请用同一编排器上的 `/tasks/{id}/rerun`
> 或把 `--lease-stale` 调小配合外部健康检查——跨主机 pid，本仓不做猜测。

### 事件钩子（Event Hooks）

通过 `POST /api/events/hooks` 注册 HTTP 回调，流水线事件触发时异步 POST JSON 到指定 URL：

| 事件 | 说明 |
|------|------|
| `task.created` | 任务创建 |
| `task.started` | 任务开始执行 |
| `task.completed` | 任务完成 |
| `task.failed` | 任务失败 |
| `task.cancelled` | 任务取消 |
| `agent.started` | Agent 启动 |
| `agent.error` | Agent 异常 |
| `quality_gate.evaluated` | 质量门控评分完成 |
| `circuit_breaker.open` | 熔断器打开 |
| `circuit_breaker.close` | 熔断器恢复 |

```bash
# 注册 webhook
curl -X POST http://127.0.0.1:8910/api/events/hooks \
  -H "Content-Type: application/json" \
  -d '{"event": "task.completed", "url": "https://your-webhook-handler.com/notify"}'

# 列出已注册钩子
curl http://127.0.0.1:8910/api/events/hooks

# 注销钩子
curl -X DELETE http://127.0.0.1:8910/api/events/hooks/<id>
```

Webhook 使用独立事件循环异步发送（aiohttp 连接池），不阻塞流水线主线程。

### 外部插件（entry_points）

除了本仓 `agents/*.py`，外部 pip 包可以通过 entry_points **零改动**接入 Agent
（group `loom.agents`；entry point 的**值须是模块路径**，与内置
`agents/*.py` 同构：模块级 `AGENT_NAME` + 一个 BaseAgent 子类）：

```toml
# 插件包的 pyproject.toml
[project.entry-points."loom.agents"]
my_agent = "my_pkg.my_agent"
```

安装后即被发现与注册——`loom plugin list` 会带来源标记
`[entry_point:my_agent]`，且能作为任何 pipeline 的节点执行；同名时本仓
`agents/*.py` 优先，插件不能悄悄顶掉内置 Agent。

**首个真第三方插件（生态轴 0→1，2026-10-12）**：[`loom-plugin-sqlite`](https://github.com/Levango7/loom-plugin-sqlite)——外部仓独立维护，补“只读 SQLite 取数 → 报表”取数面，含报表/审批闸门两条示例管线与双宿主版本 CI。它同时是外部作者的最佳参照（含过渡期双名/双组兼容范式）。

可运行的外部作者模板是 **`examples/plugin-hello`** 一支（`char_stats` 字符统计）：
声明 `loom.agents` group + 模块级 `AGENT_NAME` 与 `BaseAgent` 子类，用来证明"宿主零改动
即可接入外部包"。原五支示例插件（keyword_extract / readability / link_check /
fact_coverage / tldr）已随 2026-10-11 的单发行物决策**并入 `agents/` 成为内置件**，
`pip install nexusloom` 后直接可用，不再单独发行（契约说明见
[`examples/README.md`](examples/README.md)）。

判据分两层：`tests/test_plugin_example.py` 盯**示例包契约**（真 venv + 真
pip install，空 agents 目录下仍被发现/注册/执行——"本仓零改动"的可复现证明）；
`tests/test_plugins_ecosystem.py` 盯**生态面**（五支内置件从 `agents/` glob 被发现、
被注册、被真实执行；plugin-hello 模板形状合法；`examples/` 不许长出已并入目录的鬼魂；
本机环境的第三方发行物计数诚实记 0——外部例证在仓外，见上）。

### 发布（PyPI）

发布链已收敛为**单发行物**（2026-10-11 整合后 artesian 与全部 Agent 都在包内，
无外部依赖、无第二发布物）：

1. **一次性登记（PyPI 控制台）**：https://pypi.org/manage/account/publishing/
   添加 pending publisher —— project `nexusloom` / owner `Levango7` /
   repository `nexusloom` / workflow `release.yml` / environment `pypi`
   （2026-10-11 仓库名已由 doc-pipeline 改为 nexusloom，登记以新仓名为准；
   若之前按旧仓名登记过，删掉重加）。
2. **发版动作**：打 tag `v<版本>`（tag 与 `pyproject` 的 version 一致，
   门禁会核对）+ 建 GitHub Release，`release.yml` 的 `pypi-verify` +
   `pypi-publish` 自动执行。
   发布门禁 `scripts/verify_dist.py` 卡四件事：wheel/sdist 数据文件齐全
   （核心流水线在 data-files 的 `pipelines` 键；docgen pack 的
   pipelines/quality/prompts/pack.yaml 在 `packs.docgen` package-data——
   任一声明 glob 零命中即红，2026-10-12 拆分后口径）、
   依赖闭合、版本三处一致、干净 venv 安装态真跑。
3. ~~六插件单独发行~~ **已随单发行物整合取消**（2026-10-11）：五支实用
   插件并入产品为内置件，随 `pip install nexusloom` 直接可用；
   examples/plugin-hello 留作外部作者模板，不再单独发行。
   随之存在的 `release-plugins.yml` 已在 2026-10-21 删除——它仍列 6 个插件目录
   （实际只剩模板 1 个）、断言 `Requires-Dist: doc-pipeline`（宿主早已改名），
   workflow API 实测 `total_count: 0` 一次都没跑过。现在**全仓只有一个 PyPI 发布面**
   （`release.yml` 的 `pypi-publish`），判据
   `tests/test_release_readiness.py::TestSinglePublishSurface` 双向钉住：多一个发布器红，
   把这唯一的一个弄没也红。

`pypi-verify` / `pypi-publish` 只在 GitHub Release published 时触发；
`workflow_dispatch`（dry_run=true 默认）只构建与校验、不上传。

## Agent 安全沙箱

第三方 Agent 加载时自动执行 AST 安全检查（禁止 `os.system`/`subprocess`/`eval`/`exec`/`open` 等），
内置 Agent 通过在模块顶层声明 `SANDBOX_TRUSTED = True` 跳过检查（信任来自声明本身，
不再依赖引擎里写死的名单）；entry_points 插件与内置件同一条策略。
默认 `strict_safety=True`，检测到危险调用直接阻断加载。

---

## Docker

> **构建前置（2026-10-07 起已就绪）**：镜像里要 import 的取数底座 `artesian` 由
> `requirements.txt` 的**直接引用 pin** 提供（`artesian @ git+https://github.com/Levango7/artesian.git@<sha>`），
> CI 与 Dockerfile 都只跑 `pip install -r requirements.txt`，靠的就是这一行。
> 库侧每次改动都要把 sha 换成新的；`tests/test_layering.py` 钉住"必须是完整 40 位提交号"
> （分支名会让同一份清单在不同时间装出不同的库）。

```bash
# 构建并启动常驻 Admin API 服务（生产配置，绑定 0.0.0.0:8910；
# 非回环绑定强制要求 ADMIN_API_KEY，缺失时容器会拒绝启动并提示）
docker build -t doc-pipeline .
docker run -d -p 8910:8910 -e ADMIN_API_KEY=change-me \
  -v $(pwd)/checkpoints:/app/checkpoints doc-pipeline

# 提交任务：POST /api/tasks（鉴权头 Authorization: Bearer <ADMIN_API_KEY>）
# 一次性生成文档（覆盖默认 CMD）：
docker run --rm -v $(pwd)/output:/app/output doc-pipeline \
  test_input.md -o output/doc.md
```

非 root 用户运行，暴露 8910 端口，`/app/checkpoints` 与 `/app/logs` 为持久化卷；
HEALTHCHECK 直接探测容器内 `/health`（免鉴权）。

---

## 测试

```bash
python -m pytest tests/ -v
```

**3297 个测试本机全绿**（`3297 passed, 24 skipped, 6 deselected`，2026-11-01 Windows 本机实测，
耗时 25 分 49 秒 / 1549.45s（并入 Settings 面之前在 main 树上是 31 分 19 秒 / 1879.43s），
同一套判据在机器别处还跑着东西时是 52 分 12 秒 / 3132.00s，
这个量级随负载浮动，别拿去当基准；更早一轮轻负载下是 19 分 37 秒 / 1177.27s；
本行在 SDK 文档批追加 2 条判据后按
「同 skipped 集 +2」改为 3190 / 收集 3214；#83 +1 条（清理不删磁盘交付物）、+5 条
（sqlite_util 标识符白名单）、+16 条（telemetry 双路径）后按同 skipped 集推为 3212 / 收集 3236，
1 条（run.py 纳入 mypy 类型门的防退化判据）后再推为 3213 / 收集 3237；+11 条（控制台修复批：
节点检查器投影回退 4 + 端点契约 4 + 账本明细跨轮询 3）后再推为 3224 / 收集 3248；+3 条
（Runs 深链与状态过滤接线）后再推为 3227 / 收集 3251；+51 条（维护面接线层 28 + SDK 契约面 23，
其中 `gc_seconds` 空值缺陷由这批判据查出）后为 3278 / 收集 3302；+4 条（Settings 面 v1 接线：
四源/掩码/只读边界）+1 条（webhook 字段契约：前端只许读后端真返回的字段）后为 3283 / 收集 3307；
+5 条（视觉改版 v1：浅色默认/深色覆写/切换持久化/无残留深色底/主操作前置）后为 3288 / 收集 3312；
+1 条（卡片分层对比：卡身/底布/边框三重 WCAG 比率）后为 3289 / 收集 3313；+8 条（Workflows & Schedules 面：后端 GET /api/schedules 真 HTTP 4 + 前端接线 4）后为 3297 / 收集 3321，
SKIPPED 集合未变）；
实际收集 **3321** 条（Windows 本机）/ **3329** 条（Linux CI，多 8 条为 POSIX-only 用例），
差值 **24** = 22 条真 PG 门（`test_state_backend` 21 + `test_schedule_lease` 1，`LOOM_TEST_PG_URL`
未设时跳过，CI 四条腿都挂真 PG 会执行）+ 1 条仅 POSIX 语义的路径用例 + 1 条可选 JSON 后端缺包；
护栏的上界 = 本行自报的 `skipped`（Windows 侧精确）+ 在 Linux 上再加实测的 8 条平台差；
口径：`Python 3.14.3`、串行无 `-n`、本次跑测期间没有别的 pytest 在本仓跑
（同机另有别的 python 进程，命令行读不到、没归因）、`LOOM_TEST_PG_URL` 未设；
**本批 +36 条**（2026-10-28，A6 活性 + 交付追溯面 + 控制台账本）：`tests/test_gc_liveness.py` 18 条
（有活时也回收 / 间隔门反向对照 / 空转超时回归 / 常驻服务三态节拍 / 死信回收四判据 / GC 错账与
`--gc` 逐表打印的 CLI 端到端）+ `tests/test_delivery_traceability.py` 18 条（摘要函数的规范形态、
真跑一遍逐节点摘要与内存结果逐字一致、`output_sha256` 对上磁盘、锁指纹形状、两条真 HTTP 往返、
控制台接线与**跨端字段契约**）；
**本批 +46 条**（2026-10-27 `tests/test_pack_scaffold.py` P2 #11 pack new 骨架）：`loom pack new <name>` 命令行脚手架——包名校验（正则 + 保留字）、目录/文件/AGENT_NAME 的连字符 slug 化（`my-pack` → `my_pack`）、PascalCase 类名、六模板文件生成（pack.yaml / agents / pipelines / prompts / quality）、discoverable 端到端验证（pack_manifest 发现 + agent_inventory + pipeline_dirs + agent_sources + pack_check 判据）；
- A5 指纹：`topology_hash` + 逐 agent `config_hashes` 进交付记录（复用 Scheduler 静态方法，两处各算一遍必然漂）
- A2 `output_sha256`：交付产物内容哈希（64 位 hex，无产物时空串）
- A5 `loom plan --diff`：CLI flag，比对当前流水线与锁文件，报告漂移项
- A2 `DeliveryLedger.cleanup()`：双判据（retention_days + max_runs），接入 `gc_runs()`；
  **+3 条**（2026-10-29 `tests/test_maintenance.py` 终态对齐）：cleanup 只碰终态
  （`TERMINAL_STATUSES` 自 `run_store` 引入不得漂）——进行中的账本行连年龄判据都不碰、
  也不占 `max_runs` 名额（与 `run_store.cleanup` 字面同口径）；同步把 `test_state_backend.py`
  PG 腿种子状态改成终态（旧种子 `completed`/`re-ran` 非终态，新口径会把它留下）；
- A2 `DeliveryLedger.cleanup()` 不删磁盘交付物：**+1 条**（2026-10-30 `tests/test_maintenance.py`）
  ——清理只删账本行，`output_path` 指向的用户产物原样保留；防的是将来把「清理过期记录」
  扩写成「清理过期产物」。变异验证：给 cleanup 临时加上按 `output_path` 删文件的实现，这条即红；
- 控制台缺陷修复批：**+11 条**（2026-10-31）——节点检查器跨进程投影回退（`test_node_inspector` +4，
  修"列表能开详情、点节点必 404"）、账本明细跨轮询存活接线（`test_dashboard_s2` +3，
  修"明细 ≤5 秒被轮询覆盖"）、控制台端点 ↔ OpenAPI 同源契约（`test_dashboard_contract` +4，
  评审 §4.3 判据 1 落地）；
- 安全加固 `sqlite_util` 标识符白名单：**+5 条**（2026-10-30 `tests/test_sqlite_util.py`）——
  PRAGMA/ALTER 的插值点无法参数化，把「调用方只传字面常量」从约定升级为强制校验
  （表名/列名严格标识符；DDL 类型片段拒 `;` `--` `/*` 等结构字符），真 DDL 幂等行为不变；
  来自 Mimosa 审计 35 条 findings 的逐条分诊（其余判误报或已有书面接受的取舍，见板 §十）。
- `loom_core/telemetry.py` 双路径判据：**+16 条**（2026-10-30 `tests/test_telemetry.py`）——
  可选依赖包装层的两种环境都要正确：no-op 降级面 + OTel 启用面（假模块注入法驱动，
  CI 无 OTel 照跑；模块级第三方接缝显式 patch）。覆盖 67% → **100%**（54 语句 0 未覆盖），
  整文件跑完兜底断言模块回到 no-op 态，不给其他判据留假 tracer。
**上一批 +11 条**（2026-10-27 `tests/test_state_backend.py` B5）：RunStore 双后端真 PG 腿——
六表 schema 幂等 / open_run 生命周期 / events after_seq+limit / record_nodes 批量 upsert /
**上一批 +7 条**（2026-10-27 `tests/test_client_sdk.py`）：LoomClient Python SDK（产品评审 A7）
的真 HTTP 集成测试——submit-cancel-wait 链 / approve-reject 收件箱链 / 生命周期 404 作用域 /
OpenAPI 路由同源门禁 / submit 参数校验 / run_workflow 400 / paused 任务 wait 超时；AdminAPI
绑 127.0.0.1:0 + tokens.yaml admin token，不走 mock；
**上一批 +2 条**（2026-10-26 `tests/test_repo_hygiene.py`）：冲突标记不许进库——起因是 main 上
真出现过一次带 `<<<<<<<` 的文档被合进去而六条 required 全绿（没有任何门禁扫 `.md`）；判据
扫 `git ls-files` 全量、并且**自带射程自证**（对构造样本必须命中，否则扫描器写成永远返回空
也会一直绿）。上一轮这段把"实际收集"写成 3037（与 3077 自相矛盾），一并按实测改正为 3096；
本轮口径：`Python 3.14.3`、`LOOM_TEST_PG_URL` 未设置、同机无并发 pytest，全量
**1522.40s ≈ 25.4 分钟**（上一批 1473.05s ≈ 24.6 分钟、再上一批 918.10s ≈ 15.3 分钟；
读这几个数请带各自的负载条件，别互相换算）；
上一行写的 2950 之后的本批 #7 类型化输入 **+78 条**（`tests/test_input_schema.py` 38 +
`tests/test_typed_input_surfaces.py` 40：声明期四类写错都要在解析时红且带字段全路径 /
载荷期每类约束各红一条且错误**可定位**（`tags[1]`、`spec.region`）/ `default` 只填没给的 /
四条口逐条证明"确实到达闸门"（非法必拒且**引擎没被调用**、合法要看到闸门产出的文档、
没声明的照旧=反向对照）/ 一条真 HTTP 端到端 + 一条真 CLI 子进程 + 闸门只有一处实现的结构判据；
26 条窄变异全部有射程，含首轮那条零射程变异逼出来的"类型不符即短路"补判据，见 CHANGELOG 续65）；
上一行写的 2928 之后的本批 #8 只读 HTTP 面 **+22 条**（`tests/test_deliveries_api.py`：
账本列表/明细走**真 HTTP**（handler 写了但路由没接、路由接了但读内存 task、行取对了但
越权漏出别人这三种失败只有真请求分得开）/ 逐行按投影裁决可见性 / 越权与不存在同一个 404 /
账本读不动给 503 不降级成空列表 / `limit` 默认与夹逼 / 健康位真的从队列读出来（写死成
`ok` 会被红抓）/ 锁巡检三值 `matches` 与 `scope: current files` / 缓存绑文件时间戳且显式
单条不读缓存 / 解析规则执行侧与巡检侧同源；两轮共 26 条窄变异，22 条判据全部被至少一条
打死，含电池驱动器自己那处"只数 FAILED、把 11 条 fixture ERROR 读成没射程"的修正，
见 CHANGELOG 续64）；
再往前，2928 = 上一行写的 2911 + Stage 3c 补账工具 **+17 条**（`tests/test_run_backfill.py`：
真把投影炸掉再补回 / 只补缺不覆盖 / 三方口径冲突一律拒绝 / 时间不编（事件里写
`times: unavailable`）/ `run.replayed` 与原始执行事件分得开 / 健康位最后一步升格所以中途
炸掉还能重跑 / 存储层"至多一条终态事件"两条 / CLI 退出码；8 条窄变异 MB1–MB8 逐条红在
预期用例上、无零射程，见 CHANGELOG 续63）+ **2 条真 PG 判据**（`TestPostgresRunStoreSql`：
`close_run` 在 PG 方言下同样只盖一次、队列按健康位取行——本机没设 `LOOM_TEST_PG_URL` 时跳过，
所以不进上面的通过数，CI 四条腿都挂真 PG 会执行）；
再往前，2911 = Stage 3a 审批摘写 **+16 条**（`tests/test_runstore_approvals.py`：投影与事件同事务、
台账是决定形状的唯一来源、老台账的四条数据边界、写侧不可用时 fail-closed；8 条窄变异逐条红在
预期用例上，见 CHANGELOG 续61）之后，Stage 3b 队列收口 **+6 条**（`tests/test_projection_health.py`：
盖章炸掉 / 写口建不起来 / 正常升格 / legacy 不算故障 / 只有账本的老运行报 unknown /
幽灵 id 报 unknown；7 条窄变异各自只红在射程内，含首轮漏跑、补跑才拿到数字的 M6，见
CHANGELOG 续62；另有 3 条旧断言换成钉新契约而不是删）；
更早一批（Stage 2）里全量曾抓到一条真回归——`RunHandle.exists()` 把替身自动生成的
`task_queue.get()` 当成"队列里有这行"，一个不存在的 id 会一路走到拼载荷、序列化时炸成
JSON-RPC internal error，已收紧成 `isinstance(row, dict)`；
续60 那批的口径条件：`Python 3.14.3`、**同机有并行会话**（全量 1226.74s ≈ 20.5 分钟；静默基线
753.83s ≈ 12.5 分钟）。那轮负载里先后暴露过两条时序判据：`test_runstore_parity` 的
"账本行还没落地就断言"（已改成有界等待，见续58）、`test_p2_infra_fixes` 的队列排空竞态
（与本批无因果，单跑 3 次各绿，另立待办）；两处处理完后那轮 2875 条全绿；
`tests/test_admin_concurrency.py::TestBoundedConcurrency` 原先在满载轮里错位红，**这里旧写的
"属时序窗口争用不是产品缺陷"是错的**——根因是池满拒答带着未读请求字节 close ⇒ 内核发 RST
把 503 吞了（WinError 10053，缺陷原样时 20 轮红 9 轮），续57 已修并把等待条件换成
`_server.inflight` 探针，最终头该全类 10 轮 0 红；
本轮 `LOOM_TEST_PG_URL` 未设置 ⇒ 9 条跳过里 7 条是真 Postgres 判据（`requires_pg`，
现测：`pytest "tests/test_state_backend.py::TestPostgresTaskQueue"
"tests/test_state_backend.py::TestPostgresRunStoreSql" --co -q` ⇒ 7 collected），
另 2 条是 `test_e2e_real` 的环境门；设了该变量的口径应比本行数多出那 7 条执行
（CI 四条 test 腿都带 PG service 与 job 级 env，所以那里跑的是 7 条）；
工具层迁出 artesian 后 19 条 fast_json 用例随库走，等价判据在新库加强至 35 条；
嵌入层与知识库迁出后 74 条用例随库走，等价判据在新库加强至 91 条；
搜索引擎迁出后 91 条用例随库走，等价判据在新库加强至 92 条，另配 30 条缓存/env 底座判据；
url_guard 归位后 52 条用例随库走，库侧另加 9 条 SSRF 判据（含真 socket 重定向端到端）；
E2E Nightly 停用那一批加了 2 条判据，锁"工作流有没有定时"与文档说法同向；
渲染层结构化表格那批加了 15 条判据（表格解析 9 / 渲染端到端 4 / 行内清洗 2）；
xlsx/pptx 结构化子集那批加了 16 条判据（xlsx 8 / pptx 8）；
MCP 通用入口那批加了 6 条判据（run_workflow 5 / 序列化形状 1）；
定时触发那批加了 66 条判据（cron 解析与时刻计算 37 / 触发器与调度 29）；
entry_points 插件那批加了 8 条判据（发现/加载/沙箱/同名优先）；
入站 webhook 那批加了 28 条判据（配置/两种鉴权/审计留痕/HTTP 粘合层）；
插件示例包那批加了 4 条判据（包形状 3 / 真 venv 安装即发现 1）；
队列多机那批加了 5 条判据（真跨进程互斥 / 跨进程租约回收 / 活 owner 不被抢 / CLI 巡检 / 两实例共享一库跑真流水线）；
acquire 竞争重试那批加了 2 条判据（抢输后重试下一条 / 真空才返回 None）；
配置审计那批加了 9 条判据（变更落 JSONL / 敏感值脱敏 / 写盘失败不静默 / 读回新→旧 / 坏行跳过 / 两通道共用实现）；
pack 套件那批加了 32 条判据（9 条新任务型 pack 各一条真 E2E：情报简报 / 本地资料摘录 / 数据集质检 /
告警处置手册 / 汇报演示稿 / 会议纪要周报 / 发票要素抽取 / K8s 巡检 / 需求转用例；
外加 pack 清册护栏 3 类 ×11 条 + renderer/analyzer 节点级配置生效回归 4 条）；
SQLite 并发首建那批加了 9 条判据（两进程同建四库真并发 12 轮 / 建完可读写 /
WAL 切换重试与失败面 / 幂等加列；含确定性持锁场景把旧写法钉红）；
pack 拆包那批加了 7 条判据（真 wheel + 真 venv 安装态自检零 ERROR / 打包声明在位 /
bootstrap 不再点名 pack 文件 / 清册与 scheduler 清单同源、片段不算 pack、
agent 名按 AGENT_NAME 派生、call 节点不算缺失）；
交付形态补缺那批加了 9 条判据（账本读回一致 / 幂等覆盖 / 缺 run_id 拒绝 /
html 单页与站点导航 / renderer html 后端 / 真 E2E 三交付物互相一致 + 失败也落账）；
命名清理第一步那批加了 2 条判据（run.py 源码静态零文档味 / 真 --help 可见面零文档味）；
PyPI 发布准备那批加了 16 条判据（依赖闭合 / data-files 子目录覆盖 /
插件元数据与合法 trove classifier / release.yml 的 OIDC 发布链）；
单发行物整合那批加了 9 条判据（artesian 并入后 307 条底座判据随库走 /
五支内置件发现注册执行 / plugin-hello 模板形状 / 第三方诚实计数=0）；
pack-docgen 物理拆分那批净加 4 条判据（引擎不 import packs / pack agents 不 import
核心 agents 双向守卫等，其余为清册口径改写）；
§9 收口那批为口径批（README 索引 + spec 决策备选附录），存量判据改口径不加条数；
CLI 动词收敛与命名尾款那批净加 41 条判据（动词翻译/摘除/门面/真 CLI 冒烟 26 +
env 双读与 MCP/观测标识 15，另改写 7 条对应旧 flag 的存量用例）；
§9 两决策落地那批净加 22 条判据（审批闸门五层：解析/清册/执行/条件闸门/HTTP+审计 19 条 +
checkpoint save/load 同源 3 条）；
命名收官那批净加 8 条判据（loom_doc shim identity/warn-once/反向守卫 +
entry_points group 双读）；
命名收官·第二段净加 2 条判据（loom_core shim：MAOP 探径契约 + identity/version/枚举同一）；
P1-A token 身份批净加 28 条判据（真 HTTP 双 token 互不可见 + 六类越权 404 + 鉴权矩阵 + owner 透传链 + 成本作用域）；
P1-B 状态后端批净加 18 条判据（DSN 解析 / 方言翻译 / SQLite pool 语义 / **spec 验收原文两实例共享一库**：两进程领取互斥 + 两个真 TaskWorker 共享 PG 跑完流水线）；
P1-C 配置热更新批净加 26 条判据（覆写文件契约 / 跨实例构造见值与 poll 感知 / env 不敌覆写 /
损坏保留旧配置 / 写盘失败不静默 / **真跨进程两进程共享状态目录** / Admin API 与 worker 消费端接线 /
orchestrator 通知循环真遍历）；
P1-D 并发模型批净加 14 条判据（同连接两请求 keep-alive / 五处响应路径 Content-Length /
池满 503+Retry-After 与 200 数等于池大小 / 工位借还与空闲回收两条真时序 /
半开连接被服务端关闭 / SSE 入口不复用连接）；
S2 运行详情批净加 20 条判据（DAG 视图全字段真 HTTP：四态/两种跳过可区分/无 DAG 给空结构/
层级与 attempts 兜底/审批两字段 + dashboard 结构护栏 13 条；浏览器验证范围见下方独立命令）；
S3 节点检查器批净加 15 条判据（节点检查器载荷真 HTTP：artifact 截断预览 / 幂等键三态
（真库 True·False·store 未就绪 None）/ 熔断限流 None 与在场两态 / 质量门只认质量形状 /
404 三型 / 断点三种态 + dashboard 结构护栏增补 4 条；抽屉交互无浏览器级证据）；
RunHandle 那批加了 20 条判据（status 新鲜度/读穿/队列回落/未知态/to_dict 6 / wait 终态·超时·审批闸门 3 /
cancel·resume 透传引擎布尔 3 / 产物清单形状与不虚增 5 / MCP+Admin 同源与淘汰降级 3）；
产物持久化那批加了 6 条判据（账本行携带逐节点 artifact_keys / 被淘汰运行从账本重建同一形状 /
淘汰态 to_dict 补齐 pipeline·error·artifacts / 读路径不建库 / 坏账本降级不抛 / 无产物键节点不编造）；
控制台随包 + 审批跨重启那批净加 17 条判据（控制台 3：包内资源 / 打包声明 / 真 HTTP `GET /` 返回 HTML；
审批 14：台账 7（含决定绑定闸门节点）+ **真子进程硬退出后批准并续跑完成** 1 + 驳回后不再执行 1 +
暂停边界保完整状态 1 + **真双进程：活 owner 被批准不重排、前序节点计数不涨** 1 +
活 owner 队列守卫 1 + 真 HTTP 收件箱作用域 2）；
现测命令 `python -m pytest tests/ -q`；coverage 门禁 83%，
本机 2026-10-08 全量实测 87.99%，CI 侧 2026-10-08 实测 87.28%（3.11–3.13，3.14 是 87.25%））。
CI 的通过数可能与本机略有
出入——渲染层/OCR/嵌入类用例带 `skipif`，取决于该 job 装了哪些可选依赖。
数字由 `tests/test_doc_consistency.py` 与实际收集数比对把关，落后于代码即红（此前这里
长期写 1854 而无人能证明它对不对，因为 `tests/` 里没有一条测试引用 README）。覆盖：Scheduler 解析、
Schema 校验、Lockfile 与 edges 一致性、消息总线（含幂等去重的显式回报）、
熔断器、限流器（含集成）、QualityGate（含产出保真底线）、Agent 集成、
容错注入、断点续传、管理 API、并发压力、SSE 流式、执行器工厂、任务队列、
成本追踪、告警机制、质量闭环、MCP Server、OpenAPI Spec、Agent 沙箱、
配置热更新、kb-docgen 离线端到端、运行态路径隔离。

控制台浏览器回归（独立于 pytest 计数，需安装 Playwright 与 Chrome）：

```bash
python -m pip install playwright
python tests/check_dashboard_browser.py --channel chrome
```

该脚本启动随机回环端口，在真实浏览器执行控制台源码，以固定 HTTP 响应验证成本/质量/日志渲染、
403 与 503 的区别、真实空结果、驳回取消/确认、登录后重载工作流目录。它不替代后端鉴权与数据库测试，
也未自动接入 CI；已有 Chromium 可省略 `--channel`，使用 Edge 可改为 `--channel msedge`。

> `python -m pytest tests/ -m e2e` 是 6 个打真实网络的用例，`skipif` 读仓库根的 `.env`：
> 搜索类要 `BOCHA_API_KEY` 或 `TAVILY_API_KEY`，LLM 类要 `LLM_API_KEY`，全链路两条都要，
> 缺哪个跳哪个——所以本机没有 `.env` 时这里是"0 执行"而非失败。
> 离线端到端能力由 `tests/test_kb_pipeline_wiring.py` 与 `tests/test_e2e_mock.py` 承担
> （两者不带 `e2e` 标记，默认 CI 里真跑）。
>
> **E2E Nightly 工作流已于 2026-10-08 停用**：GitHub 侧状态置为 `disabled_manually`，
> `.github/workflows/e2e-nightly.yml` 里的 `schedule` 块同步摘掉，`workflow_dispatch` 保留。
> 原因不是判据坏了，而是它按设计生效了——仓库从未配置过任何 `E2E_*` Secret
> （`gh secret list` 返回 `[]`），而夜间回归自 2026-10-05 起拒绝"0 执行却报绿"
> （此前 10-02/10-03/10-04 三个 schedule run 就是这样假绿的），于是每晚稳定判红
> （10-05/10-06/10-07 连续三个 schedule run failure）。一条只播报"这里没有 key"的
> 常驻红灯不是回归信号，还会淹掉真出问题时同一条红。
> **重启条件**：配好 `E2E_LLM_API_KEY` + `E2E_BOCHA_API_KEY`（或 `E2E_TAVILY_API_KEY`），
> 手动 `workflow_dispatch` 触发拿到非零 passed，再把 `schedule` 加回 YAML。

```bash
# 运行真实端到端测试（需要网络 + LLM API Key）
python -m pytest tests/ -m e2e -v
```

---

## 质量门控机制

`QualityGate v2` 按 Profile 权重评分：

```
产出保真底线（先于评分）：内容非空 且 ≥ min_output_chars（默认 120）
                     且 不含已知占位语（"未采集到可整合的搜索结果"等）
                     且 占位章节占比 ≤ max_placeholder_section_ratio（默认 0.34）
                     且 正文不含抓取层原始素材签名（"下载时间:" 行 / 60 连等号分隔线；
                         确需附原文用 allow_raw_fetch_blocks: true 显式放行）
                     → 违反即 status=fail + hard_floor=true，
                       不重做、不因 pipeline.fail_fast=false 而放行，整条流水线 exit 1

总分 = Σ(维度得分 × 权重) − 风格扣分 − 引用扣分
阈值 = profile.threshold (默认 70)

不达标 → 自动重生成 (最多 max_regenerations=3 次)
重做后仍不达标 → accepted_with_warnings（放行但如实标注）
```

底线与评分的分工：评分量的是"写得好不好"，可以被扣分拉低后仍放行；
底线量的是"到底有没有内容"。**历史上没有底线**——无检索结果时 writer
会写一份 99 字节的占位文档，一路 9 步全绿、`done` + `exit 0` 并落盘
docx/pdf，把失败伪装成成功（`tests/test_fidelity_gate.py` 锁住该回归）。

### 维度

评分共 7 个维度（`technical-doc` 权重）：主题相关度 topic_relevance 0.20、
完整性 completeness 0.18、内容实质度 substance 0.18（信息密度/重复率/实质信号）、
结构 structure 0.12、引用 citation 0.12、可读性 readability 0.10、深度 depth 0.10。
风格问题不计分，按 profile 的 `style_rules` 扣分（与引用扣分共享 `max_penalty=40` 上限）。

> 诚实边界：以上维度均为规则/启发式度量（结构、字数、关键词命中、URL 格式等），
> 不构成事实正确性验证——事实核查请使用 `--pipeline docgen-verified` 的 fact_checker
> （其启发式边界见 `agents/fact_checker.py` 模块注释）。

---

## 目录结构

```
nexusloom/
├── agents/              # 14 个核心 Agent 实现（含内置化的五支实用插件）
├── packs/               # 物理 pack：领域件集合（引擎经 pack_manifest 多目录发现）
│   └── docgen/          # pack-docgen（spec §5.2 最后一步）
│       ├── pack.yaml    #   manifest：agents / pipelines / quality_profiles 清册
│       ├── agents/      #   5 个文档件：writer/quality_gate/checker/layout/fact_checker
│       ├── pipelines/   #   docgen 系 6 条 + _quality-tail（均带 .lock 同目录）
│       ├── quality/     #   质量档案：technical-doc / tutorial
│       └── prompts/     #   prompt 模板：default / generic-tech
├── loom_core/       # 引擎层：领域无关的编排框架（不 import 文档层）
│   ├── pipeline.py      # Orchestrator（统一节点模型）
│   ├── dag_executor.py  # DAG 构建 + 节点调度
│   ├── scheduler.py     # YAML → ExecutionPlan + Schema + Lockfile
│   ├── message_bus_v3.py # 消息总线（WAL + 批量投递）
│   ├── message_store.py  # SQLite 持久化层
│   ├── circuit_breaker.py # 熔断器
│   ├── rate_limiter.py  # 令牌桶限流
│   ├── registry.py      # Agent 注册表
│   ├── observability.py # 结构化日志（异步）+ Metrics
│   ├── admin_api.py     # REST API（多线程）
│   ├── version_manager.py # 文档版本管理
│   ├── cache_manager.py # 统一缓存
│   ├── llm_router.py    # LLM 多供应商路由
│   ├── cost_tracker.py  # LLM 成本追踪 + 预算熔断
│   ├── alert_manager.py # 告警机制
│   ├── quality_feedback.py # 质量闭环学习
│   ├── task_queue.py    # SQLite 任务队列
│   ├── worker.py        # 常驻 worker（--worker）：消费队列、租约回收
│   ├── mcp_server.py    # MCP Server (JSON-RPC)
│   ├── openapi_spec.py  # OpenAPI 3.0 规范
│   ├── agent_loader.py  # Agent 安全加载（沙箱）
│   ├── streaming.py     # SSE 流式输出
│   └── ...              # 更多模块
├── loom_doc/         # 文档领域层（只依赖 loom_core，方向单向）
│   ├── renderer.py      # Markdown → docx / pdf
│   ├── ingest.py        # PDF / 图片 / 文本 → 结构化 Markdown
│   └── document_enhancer.py # 已有文档逐章节 LLM 增强
├── pipelines/           # 核心散装流水线（单文件任务型，13 条；docgen 系已随 pack 迁出）
│   ├── api-report.yaml  # 通用件示例：JSON API → transform 挑字段 → 落盘（无文档领域节点）
│   ├── api-digest.yaml  # foreach 消费者：列表接口 → 同一个 Agent 逐项渲染 → 聚合落盘
│   ├── intel-brief.yaml # 情报简报：检索 → 逐条要点（foreach）→ 落盘
│   ├── kb-brief.yaml    # 本地资料摘录卡：ingest → kb(hash) → 逐条摘录 → 落盘（全离线）
│   ├── data-qc.yaml     # 数据集质检：JSON → where 判定统计 → 质量门 → 落盘
│   ├── alert-runbook.yaml # 告警处置手册：告警 JSON → 逐条处置步骤 → 落盘
│   ├── deck-brief.yaml  # 汇报演示稿：要点 JSON → 分节渲染 → md + pptx
│   ├── minutes-weekly.yaml # 会议纪要周报：议题 JSON → 逐议题行动项 → 落盘
│   ├── invoice-extract.yaml # 发票要素抽取：逐票要素台账 → md + pptx
│   ├── k8s-patrol.yaml  # K8s 巡检：内网 API（allow_hosts 放行）→ 逐节点报告
│   ├── spec-cases.yaml  # 需求转测试用例：规则解析 → 逐关键词用例清单（全离线）
│   ├── three_pass.yaml  # 三阶段流水线（尾巴阈值/重试与片段不同，故不引用片段）
│   ├── test_pipeline.yaml
│   └── *.lock           # 版本锁定：config_hash + 拓扑指纹，漂移即拒绝执行
├── dashboard/           # 前端仪表盘
├── spike/               # 渲染层可行性验证脚本与结论（见 spike/README.md）
├── tests/               # 测试套件（数量见上文「测试」一节；另有 e2e 标记用例）
├── checkpoints/         # 断点 + 日志（自动轮转）
├── versions/            # 文档版本存储
├── run.py               # CLI 入口
├── Dockerfile
├── requirements.txt
└── .github/workflows/ci.yml
```

---

## 性能

> **注意**：以下数据来自 `benchmark.py` 的 mock 基准（模拟引擎，无网络 I/O，质量门控跳过 LLM），
> 反映框架本身的开销。默认 `config.json` 已启用 Bing/Sogou/360 免费 HTML 兜底引擎（无需 API Key，
> 结果质量低于付费 API 引擎）；完整多引擎检索 + LLM 润色请使用：
> `python run.py input.md -c config.production.json -o out.md`

| 指标 | 数值 | 模式 |
|------|------|------|
| Fetcher 并发 | 20 页 / 3s (aiohttp) | 真实网络 |
| LLM 额度消耗 | 0（质量门控跳过，规则兜底） | mock |
| 消息总线吞吐 | 批量 drain 50 条/轮 | — |
| 缓存命中 | 74 万 ops/s (get_hit) | 基线（ubuntu/3.12） |
| 测试覆盖 | 见「测试」一节的现测数字（另有 e2e 标记用例） | — |

### 生产模式预期耗时（config.production.json）

| 阶段 | 预期耗时 | 说明 |
|------|----------|------|
| Researcher（真实搜索） | 3-8s | 取决于引擎响应速度 |
| Fetcher（20 页下载） | 2-4s | aiohttp 并发 |
| Writer（TF-IDF + LLM 润色） | 5-30s | LLM 调用为主要耗时 |
| QualityGate + Checker + Layout | <1s | 纯规则计算 |
| **总计** | **10-45s** | 视 LLM 可用性和网络状况 |

---

## 文档

| 文档 | 内容 |
|------|------|
| [产品规格](docs/product-spec.md) | 定位、四轴验收线、pack 机制、分层目标（P0/P1）与未决项台账 |
| [部署指南](docs/deployment.md) | 生产环境配置、API Key、监控、备份策略 |
| [API 参考](docs/api.md) | Admin REST API 全端点说明与鉴权 |
| [Python SDK](docs/sdk.md) | `LoomClient` 客户端：安装、认证与信任模型、16 个方法的完整表、错误模型与语义边界 |
| [架构说明](docs/architecture.md) | 分层结构、线程模型、数据流 |
| [Agent 开发指南](docs/agents.md) | 自定义 Agent 契约、沙箱规则、最小示例 |

英文版：`docs/en/` 下有同样四篇的英译（[架构](docs/en/architecture.md) /
[API](docs/en/api.md) / [Agent](docs/en/agents.md) / [部署](docs/en/deployment.md)）。
两份内容以中文版为准；改了中文版请同步英译，别让它们各说各话。（[Python SDK](docs/sdk.md)
暂无英译。）

---

## License

MIT
