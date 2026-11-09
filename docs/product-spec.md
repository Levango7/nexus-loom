# 产品定义 · Agent 工作流运行时

> 状态：**已确认方案（2026-10-06）**，作为升级工作的基线文档。
> 本文只定义"做什么/长什么样/怎么算合格"，不含实施排期。

## 0. 证据口径（先说清哪些是量过的）

| 标记 | 含义 |
|---|---|
| 〔实测〕 | 2026-10-06 在本机量得：**Python 3.14.3**（全局 site-packages，无 venv）、**无 `.env`/无 LLM Key**、单进程、`pool_size: 2`、`defaults.parallelism.mode: single` |
| 〔静态〕 | 读代码/`git grep` 得到的结构与计数，未在运行时验证 |
| 〔参照〕 | 竞品公开定位，**本仓未逐家实测**，仅作坐标参考 |

规模基线〔静态〕：`pipeline_core` 16,593 LOC / 42 文件；`agents` 6,470 LOC / 14 个 Agent；`docpipeline` 1,246 LOC / 3 文件；`tests` 27,153 LOC / 99 文件；`run.py` 707 行。
测试基线〔实测〕：**2211 passed / 2 skipped / 6 deselected，242s**（README 写 1854，属过期低估）。

---

## 1. 产品定位

**一句话**：领域无关的 **Agent 工作流运行时** —— 用声明式 YAML 编排可插拔 Agent，内核自带节点级重试/熔断/限流/幂等/检查点续跑、**产出质量契约**与 LLM 成本预算熔断；可 `pip install`，可被 CLI / HTTP / MCP / 队列调用。**docgen 只是装在它上面的第一个 pack。**

### 1.1 目标用户与待办任务

| 用户 | 待办任务 | 今天怎么做 | 我们赢在哪 |
|---|---|---|---|
| 企业内部自动化工程师 | 把"取数 → 加工 → 核查 → 交付"做成可维护、可回滚的作业 | n8n / Dify 拖拽，逻辑粘在节点脚本里 | YAML-as-code 进 Git + 锁文件拓扑指纹防漂移 |
| Agent 应用开发者 | 要 durable execution，但不想养 Temporal 集群 | LangGraph 自研胶水 + 手写重试 | 现成的重试/熔断/限流/续跑 + 成本预算熔断 |
| 生态内项目（MAOP 等） | 需要一个真跑 Python 节点的执行内核 | 已经通过 `doc-pipeline` 适配器调用本仓 | 同一进程内的节点语义，不是外壳派发 |

### 1.2 差异化（本仓可指认的证据）

1. **工作流可复现**：`pipelines/*.lock` 记 `config_hash` + `topology_hash`，配置或拓扑漂移即拒绝执行；`call` 内联后的展开图与 `inputs` 实参都进指纹〔静态〕。
2. **产出契约是运行时概念**：节点按 `PRODUCES` 声明产物，声明了落盘节点却没交付物就不算 `done`（`pipeline.py` 交付契约）〔实测：修复前该情形会 done + exit 0〕。`when` 不成立的节点被跳过而**下游照跑**〔实测：探针里 `fact_checker__quality_tail` 返回 `skipped` 且后续节点正常执行〕。
3. **成本可治理**：`cost_tracker.py` 定价表 + 预算熔断 + `GET /api/cost`〔静态〕。
4. **通用件已接线而非"实现了没人调"**：`http_request`（SSRF 守护、超限中止不截断、3xx 不跟随）+ `transform`（受限声明语言、不 eval 表达式）〔实测：一条零文档语义 DAG `http_request → transform → transform__b → safe_writer` 全链跑通并落盘，GitHub API 200、`guard: public`、同一 Agent 以别名在一图内出现两次〕。

### 1.3 与 MAOP 的边界（不得越界）

| | MAOP（+ MAOS） | 本产品 |
|---|---|---|
| 执行对象 | **外部 CLI Agent**（`config/agents.yaml` 31 条适配器） | **进程内 Python Agent**（DAG 节点） |
| 职责 | 派发、治理、SLA、RBAC/SSO/多租户（在 MAOS） | 执行内核 + 节点语义 + 产出契约 |
| 关系 | 调用方 | 被调用的运行时 |

**不做**：可视化拖拽编辑器（拼不过 n8n 的集成数量）；通用作业调度器（Airflow 地盘）；身份/RBAC/SSO/多租户治理面（MAOP+MAOS 地盘）。

### 1.4 诚实短板（定位成立前必须清）

第三方插件已 6 个（`entry_points` 机制 2026-10-08 落地，2026-10-10 补足生态面至 6 个真 pip 包，见 §2）· 无身份/租户（一把共享 `ADMIN_API_KEY`）· 状态全是单机文件且租约按本机 PID 判活 · **质量门会假绿**（见 §6）。

---

## 2. "20 倍"的可测量口径

能力 = 四轴组合数。当前值全部量得，不许用口号代替分母。

| 轴 | 现状 | 目标 | 依据 |
|---|---|---|---|
| 任务类型（真实跑通的 pack） | **11**（docgen 系 / api-report / intel-brief / kb-brief / data-qc / alert-runbook / deck-brief / minutes-weekly / invoice-extract / k8s-patrol / spec-cases〔最后 9 条 2026-10-09 落地〕） | **≥10** | 〔实测〕每条 pack 一条真 E2E：9 条在 `tests/test_pack_suite.py`（真 Scheduler→DAG→Agent→落盘，只换外部 IO），存量两条在 `test_kb_pipeline_wiring` / `test_generic_agents`；`TestPackInventory` 清册护栏逐条钉住 YAML + lock + 锁校验 + 判据类存在 |
| 触发方式 | **6**（CLI / HTTP `POST /api/tasks` / MCP stdio / **定时 `--triggers`** / **入站 webhook** / **队列多机 worker**〔均 2026-10-08 落地〕） | **≥6** | 〔实测〕`tests/test_triggers.py` 钉定时提交路径、`tests/test_webhooks.py` 钉 HMAC/Token 鉴权与审计留痕、`tests/test_multi_process_workers.py` 钉真跨进程互斥与租约回收（边界：跨主机接管需共享 pid 命名空间） |
| 交付形态 | **7**（md / docx / pdf / xlsx / pptx / **html**〔2026-10-10：renderer 纯标准库后端〕/ **结构化 JSON 落库 + HTML 站点**〔2026-10-10：`pipeline.deliver.json/site` 声明式交付 + `deliveries.db` 账本〕） | **≥5**（第 4 条原文五种：md / docx / pdf / 结构化 JSON 落库 / HTML 站点——**已齐**） | 〔实测〕`tests/test_renderer.py` 读回判真；`tests/test_delivery_forms.py` 9 条（账本读回/站点导航/真 E2E 三交付物一致/失败运行也落账）；xlsx/pptx 为结构化子集而非全量转换 |
| 扩展来源 | **2**（本仓 `agents/` glob + **entry_points** 机制〔2026-10-08 落地；2026-10-12 起 group = `loom.agents`（旧名 `doc_pipeline.agents` 过渡期回退可读）〕）；真实第三方插件实测数 **1**（2026-10-12 生态轴 0→1：`Levango7/loom-plugin-sqlite`——外部仓 + 外部包，纯净 venv 装 PyPI 正式宿主后被零改动发现/注册/执行；其 CI 以双宿主版本矩阵（3.10.0/3.11.0）自跑全链；≥5 的验收线仍为长期目标）（2026-10-11 用户拍板单发行物整合：原五支示例插件并入产品为内置件，examples/plugin-hello 留作外部作者模板 + 机制 E2E 证明） | **≥2 且第三方插件 ≥5** | 〔实测〕`tests/test_agent_loader.py::TestEntryPointPlugins` 八条 + `tests/test_plugin_example.py` 真 venv 装机即发现 + `tests/test_plugins_ecosystem.py` 内置化/模板/诚实计数 |

**组合数：120（2×6×5×2，2026-10-09 按实测轴值重算） → 924（11×6×7×2，2026-10-11 单发行物整合后：扩展来源轴诚实回归 2）**，相对最初立项基线 **18 为 51x**，越过 ≥20x（=360）承诺线；§2.1 四条验收线：1/3/4 达标，第 2 条机制达标（entry_points + 真 venv E2E）、真实第三方生态为 0——由真实外部包出现时点亮，不再用自家内置件充数。

### 2.1 验收线（四条同时成立才算达成）

1. pack ≥ 10，且每个 pack 有至少一条端到端测试真跑通（不是 mock 断言）。**〔2026-10-09 达标：11 条 pack，11 条真 E2E，清册护栏 `TestPackInventory` 逐条钉住〕**
2. 第三方插件 ≥ 5：由**外部 pip 包**提供 Agent，**本仓零改动**即被发现、注册、执行。**〔机制已达标（2026-10-08）：entry_points group + 真 venv E2E（`tests/test_plugin_example.py`）；2026-10-11 单发行物整合后，真实第三方插件实测数诚实回归 0——原六支为自家内置件，不算第三方，由真实外部包出现时点亮此线〕**
3. 触发方式 ≥ 6：CLI / HTTP / MCP / inbound webhook / 定时 / 队列多机。**〔2026-10-08 达标〕**
4. 交付形态 ≥ 5：md / docx / pdf / 结构化 JSON 落库 / HTML 站点。**〔2026-10-10 达标：`pipeline.deliver.json` + `deliveries.db` 账本 + `pipeline.deliver.site` + renderer html 后端，`tests/test_delivery_forms.py` 9 条〕**

> 第 2 条是 20x 的物理来源。自研 Agent 数量撑不到 20 倍，**扩展面**才撑得到。

---

## 3. 产品原型（三级递进，每级都是可运行的东西）

### P0 · 引擎内核原型（先做，2–3 周）

**形态**：`pip install <新发行名>` 得到的库 + `loom` CLI + `entry_points` 插件机制 + docgen 降级为 `pack-docgen`。

**验收判据（5 条，可当场验）**：
1. 干净 venv 装完后 `--check` 不红（当前 `bootstrap.py:171` 要求 `tests/` 存在 → 装完必红〔实测：本机 26 OK / 1 WARN / 1 ERROR〕）。
2. ~~外部仓发一个 Agent 包，本仓零改动被 CLI 列出并跑通一条用它的工作流~~ **已达标（2026-10-12）**：`Levango7/loom-plugin-sqlite`（外部仓，dist `loom-plugin-sqlite`）——本仓零改动；`loom plugin list` 列出 `sqlite_query [entry_point:sqlite_query]`；报表管线（sqlite_query → safe_writer）真跑落盘；审批版管线（插入 approval 闸门）停住→放行→驳回双向实测；外部仓 CI（GitHub runner，双宿主版本矩阵）全绿。
3. 一条不含文档语义的 YAML 端到端跑通并落盘（已有探针证明引擎可行）。
4. §6 的 FP-1 / FP-2 / FP-3 全部关闭。
5. 测试数 ≥ 2211 且保持全绿。

**退出条件**：5 条全过才进 P1。**P0 阶段不做任何新 UI。**

### P1 · 自托管运行时原型（4–6 周）

**形态**：常驻服务（替换 stdlib HTTP/1.0 + 线程-per-连接，无并发上限）+ token 身份 + 状态后端可切（SQLite → Postgres）+ §4 的 6 屏控制台。

**验收判据**：两实例共享一库并发跑不串台（当前租约按本机 PID 判活，跨机必重复执行或搁死〔静态〕）；每 token 只能看到自己的任务与成本；`/api/config` 热更新不再只对本进程生效。

**进度（2026-10-13）**：三项验收判据**全部闭合**——①两实例共享一库〔P1-B，PR #41〕；②token 隔离任务与成本〔P1-A，PR #40〕；③配置热更新跨进程〔P1-C，续44：覆写文件 + revision 轮询〕。形态四项中 **token 身份 / 状态后端 / 配置热更新 / 常驻服务已交付**；常驻服务走**决策 A**（2026-10-13 用户拍板）：**保留 stdlib 不引框架**，把 spec 点名的两个缺陷事实性关闭——HTTP/1.1 keep-alive（配套：所有响应补 `Content-Length`，SSE 显式不复用连接）+ 有界线程池（池满 503+Retry-After，`LOOM_ADMIN_MAX_CONCURRENCY` 默认 32）+ socket 读/写超时（`LOOM_ADMIN_SOCKET_TIMEOUT` 默认 30s）。判据 `tests/test_admin_concurrency.py` 14 条全真 HTTP。**形态四项只剩 §4 六屏控制台**（决策：先 C 后 B——先交付 S2 运行详情 DAG 屏 + 审批操作接入，以真实使用反馈定技术栈，再上框架补齐其余五屏）。

### P2 · 生态原型

**形态**：插件目录 + 版本兼容矩阵 + `pack new` 模板仓。

**验收判据**：第三方包可安装与升级；版本/拓扑冲突被显式拒绝，而不是静默覆盖既有 pack。

---

## 4. 产品图样

### 4.1 现有基线

`dashboard/`（index.html 11 KB + app.js 31 KB + style.css 15 KB，最后改动 2026-09-05〔静态〕）是**监控台**：状态 / 任务 / Agent / 质量 / 成本 / 日志 / 指标 7 面板 + 提交任务 + Token 连接。
可复用为 P1 的"运行观测"半边；**缺 authoring、插件视图、单次运行视图**。且 `app.js` 只调 5 个端点，`/api/logs`、`/api/alerts`、`/dlq`、`/api/versions*`、`/stream` 均未接〔静态〕。

### 4.2 目标屏幕（统一骨架：左导航 + 主区 + 右侧检查器抽屉）

| # | 屏 | 主区内容 | 与现状 |
|---|---|---|---|
| S1 | 工作流目录 | YAML 列表、**lock 状态徽标**（漂移=红）、拓扑层数、来源（内置/pack/插件） | 新建 |
| S2 | 运行详情 | **DAG 有向图**：节点四态 success/skipped/failed/retried；`when` 跳过画虚线分支；内联节点显示别名（如 `checker__quality_tail`）；检查点续跑标记；每节点耗时/重试次数 | **已交付（2026-10-13 续46 + 10-14 续47补齐断点标记）**——DAG 分层图 + 四态 + when 虚线 + 重试旗标 + 耗时/尝试 + 审批操作条（放行/驳回 + 决策流水）+ 断点续跑角标（断点已落盘/续跑）。**仍缺**：内联节点别名展示（`checker__quality_tail` 形态） |
| S3 | 节点检查器 | artifact 结构预览、幂等键、熔断/限流态、质量门明细 | **已交付（2026-10-14，续47）**——点 S2 节点出右侧抽屉：artifact 键/类型/截断预览、幂等键 `{task}:{node}:{attempts}` 与落库态、熔断/限流态、质量门总分/维度分/阈值/风格问题、断点视图；**"取不到就不编"**（未建立 = None 而不是假读数）；FP-1 已修故无假分 |
| S4 | 插件与 Agent | entry_points 来源分组、`PRODUCES`/`CONFIG_SCHEMA` 声明、沙箱判定、健康态 | 半复用 agents 面板 |
| S5 | 触发与集成 | HTTP / MCP / webhook / 定时 / 队列 的接入配置与调试 | 新建 |
| S6 | 成本与质量 | 预算熔断线、按 pack/token 归因、质量失败原因分布 | 复用 cost/quality 面板 |

> **2026-10-20 复核后的状态**：控制台已**随包发布**（`loom_core/dashboard/`，`GET /` 直出，
> 发布门禁 `verify_dist.py` 钉住三个资源）；新增**审批收件箱** `GET /api/approvals` + 控制台
> "待审批"卡片，审批**跨重启可用**（台账 `approvals.db` + 队列续跑标记）。
> 〔**2026-10-23 更新**：审批台账的事实源已换成 `runs.db` 的 `approvals` 投影 +
> `approval.*` 事件（RunStore Stage 3a）；`approvals.db` 停止新增行，只按数据边界
> 读/了结本批之前的老行。跨重启可批这件事不变。〕
> 六屏 IA（S1–S6）在 `docs/product-design-review-2026-10-20.md` 里被建议替换为按任务切的
> 六个面（Runs / Run 详情 / Approvals / Deliveries / Workflows&Schedules / Settings）——
> 本表保留为历史目标，不再作为下一批 UI 的排期依据。

### 4.3 视觉基调（已确认）

**沿用现有深色等宽"运维控制台"基调**，不另起浅色 SaaS 风。理由：买家是自动化工程师，运维台审美更对味，且能直接复用 `style.css` 基线。

---

## 5. 功能设计（分层 · 现状 · 目标 · 验收判据）

### 5.1 内核层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 声明式 DAG + `when` 条件节点 + `call` 子流水线内联 | **已有**〔实测〕 | 保持 | 零文档语义探针继续通过；内置流水线 lockfile 指纹不变 |
| `foreach` 逐项展开 | **已接线**（2026-10-07·续4）：解析期 `_as_foreach` 有调用点并进 `topology_hash`，运行期 `_execute_foreach` 逐项投递与聚合〔实测：`tests/test_foreach.py` 46 条，六处变异对照全被抓住〕。原记录"死代码：`scheduler.py:55 _as_foreach` 零调用点〔静态〕"是 10-07 上午的状态，保留备查 | 保持 | 100 项展开受 `max_items` 护栏（实测：6 项 > `max_items=5` ⇒ `failed` 且不落盘）；幂等键按 item 分裂（实测后缀 `#0/#1/#2`，去掉后缀则测试红）；展开契约进 `topology_hash`；真跑测试 = `pipelines/api-digest.yaml` 离线跑通 Scheduler → DAGExecutor → safe_writer |
| 产出质量契约 | **有但会假绿**〔实测：4/5 章节空的产物得 98.8 pass〕 | 按 FP-1 修 | `empty_sections` 占比 / 降级声明 / 抓取元信息占比 三判据进底线，并参与 exit code |
| 检查点与续跑 | 声明式路径的 `--resume` 原本是空转（工作区有未提交修复在飞）〔实测：diff 含 `TestDeclarativeResumeEndToEnd` 4 例〕 | 先合入 | 中断后 `--resume` 真跳过已完成节点，而非重跑 |
| 熔断 / 限流 / Agent 池 | **已改为分片**：池实例按 `all_queries[pool_idx::pool_size]` 领活；未认领 `EXTRACTS_QUERIES` 的 Agent 仍看全量（writer 这类必须看完整上下文）。查询词少于实例数时多余实例领**空活**并如实回报零结果，不再复制整条 query | 保持 | 新增的池实例不重复领取同一 query；根因只是 `len>=pool_size` 这道长度门槛，`EXTRACTS_QUERIES` 自 3674de6 已由 `agents/researcher.py` 声明并经 `AgentLoader` 进到 `AgentMeta`（`tests/test_pool_sharding.py` 五条，含接线证明） |
| 交付物不得夹带中间格式 | **已加判据**：抓取层落盘签名（`下载时间: …` / 60 连等号分隔线）出现在正文即 `hard_floor`；可用 `allow_raw_fetch_blocks` 显式放行（字符串 `"false"` 不会被当成真） | 保持 | 本机与 CI 实测的那两类"素材直粘"文档（28.4 KB / 21.9 KB，曾拿 95.5 pass）现在 `exit 1` 且不落盘 |

### 5.2 扩展层（20x 的引擎盖）

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 插件发现 | **机制已达标**（2026-10-08，单发行物整合后口径）：entry_points group `loom.agents`（旧名 `doc_pipeline.agents` 回退可读）与本仓 glob 并存（`TestEntryPointPlugins` 八条）；`examples/plugin-hello/` 留作外部作者模板，真 venv + 真 pip install 的端到端判据在位；原五支示例插件已并入 agents/ 为内置件（`tests/test_plugins_ecosystem.py` 钉住发现/注册/执行/诚实计数） | `entry_points` group 与本仓 glob **并存** | 外部 venv 包装好即被 `list agents` 列出，并能作为节点执行 |
| Agent 沙箱 | 已有（AST + 白名单，内置件跳过检查） | 增加来源/签名标记，并在 S4 显示 | 第三方危险 Agent 被拒且报出命中规则 |
| pack 机制 | **已达标**（2026-10-12 物理拆分落地）：`packs/docgen/` 独立目录 + `pack.yaml` manifest（5 agents / 6 流水线 / 2 质量档案），引擎经 `pipeline_core.pack_manifest` 多目录发现（AgentLoader/scheduler/quality/prompts 全部 pack 感知）；`--check` 按清册报「pack docgen（5 agents / 6 流水线）+ 13 条内置流水线、19 个 agent 可用」；引擎层与 pack 层双向 import 由 `tests/test_layering.py` 两条守卫钉住。数据文件随 `packs.docgen` package-data 进 wheel，`tests/test_installable_product.py` 真 venv 判据维持。「拆出判据」达成：删掉 `packs/` 目录引擎与其余 13 条流水线照常工作（文档件 import 面已清零） | 拆出 `pack-docgen`：5 个文档件（writer/quality_gate/checker/layout/fact_checker）+ 6 条 docgen YAML + quality profile | 已兑现：`--check` 不要求 `agents/writer.py` 与 `pipelines/docgen.yaml`（原 `bootstrap.py:183-185,235` 硬绑已随 manifest 化消除〔旧态实测：`--check` 输出逐条含这两项，装完即红〕） |
| 领域件与引擎件的依赖方向 | 引擎层无 `import agents/docpipeline`〔静态+护栏 `tests/test_layering.py`〕 | 保持 | 分层测试继续通过 |

### 5.3 表面层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| MCP Server | **已达标**（2026-10-08）：6 tools——新增通用 `run_workflow(name, inputs)`（inputs 按「## 键」渲染为输入文档）；banner/日志改 stderr 与 `serverInfo.version` 接 `__version__` 均已修（FP-3 关闭）；真 stdio 往返测试在位（`tests/test_mcp_server.py::TestMCPOverRealStdio`）。原现状留档：5 tools 全是文档动词 / banner 污染 stdout / version 恒 `unknown`〔实测〕 | 加通用 `run_workflow(name, inputs)`；banner/日志改 stderr；version 接 `__version__` | 一条**真实 stdio 往返**测试通过 |
| HTTP 执行 API | **已达标（2026-10-26，续65）**：`POST /api/tasks`（文档语义）之外新增 `POST /api/workflows/{name}/runs`——按该流水线 `pipeline.inputs` 声明逐字段校验的通用提交口（400 带 `fields` 定位到字段），另有 `GET /api/workflows/{name}/inputs` 读回声明供客户端生成表单；判据 `tests/test_typed_input_surfaces.py` 40 条（含真 HTTP 端到端、四条口共用同一实现）。原现状留档：只有"提交文档生成任务"〔静态：`openapi_spec.py:126`〕 | `POST /api/workflows/{name}/runs` 接受任意 payload | 非 docgen 流水线真 HTTP 跑通（判据内用临时 typed 流水线；**未**用随包 pack 走该端点——这一条仍开着） |
| CLI | **已达标**（2026-10-12 动词收敛落地）：banner/description/docstring 去领域味（2026-10-10）；动词层 `run / plan / lock / pack / plugin` 落地——`--plan`/`--dry-run`/`--write-lock`/`--list-agents` 四处旧 flag 摘除，裸输入 = 隐式 run（既有调用形态照旧）；`--enhance`（含 `--enhance-output`/`--no-search`）摘除，库层 `docpipeline.document_enhancer` 保留。判据 `tests/test_cli_verbs.py` 26 条（翻译层/摘除/门面/真 CLI 冒烟，lock 为沙盒实证）〔2026-10-26 追加〕：第六动词 `demo` 落地（A4 离线体验，见 §3.1 该条）；`--gc` 一次性保留期清理（A6）。判据 `tests/test_demo.py` 12 条 + CLI 动词判据 | 改名 + 动词收敛为 `run / plan / lock / pack / plugin` | `--help` 里不再出现"文档生成流水线"字样〔已达标〕；五个动词进 `--help` 且旧 flag 被拒〔2026-10-12 达标〕；第六动词 `demo` 进 `--help`〔2026-10-26 达标〕 |
| 触发方式 | **6 种**（CLI / HTTP API / MCP / **定时** / **入站 webhook** / **队列多机 worker**〔均 2026-10-08 落地〕）；多机并发契约由真跨进程判据实证（互斥 / 崩溃接管 / 活 owner 不被抢），跨主机接管需共享 pid 命名空间；**B4 定时触发租约已达标（2026-10-25）**：共享一库的多进程只允许一个触发者（`schedules` 表条件 UPDATE 原子裁决，`tests/test_schedule_lease.py` 含真跨进程判据），此前两个 `--triggers` 会把同一窗口各提交一遍 | 6 种（+ 定时 + inbound webhook） | 定时触发的 run 出现在同一队列与观测面（同 `run_plan` 路径，`tests/test_triggers.py` 钉住）；webhook 入站鉴权与审计均已兑现（`tests/test_webhooks.py`）；多机 worker 判据见 `tests/test_multi_process_workers.py`；B4 判据见 `tests/test_schedule_lease.py` 14 条 |

### 5.4 治理层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 身份与租户 | **已达标（2026-10-12，P1-A；2026-10-20 补控制台作用域）**：`tokens.yaml` 多凭证 → Principal（密钥只经 env/hash 注入；admin 位与普通租户可分）；`ADMIN_API_KEY` 兼容为 admin（旧部署零改动）；启用身份后回环同样要求凭证；任务与成本按 Principal 隔离，越权与不存在同形（404，不泄露存在性）；触发类提交记 `trigger:/webhook:/mcp` 归属；缺密钥条目不可用且启动告警。判据 `tests/test_identity_tokens.py` 28 条（含真 HTTP 双 token 互不可见 + 六类越权操作）；控制台 `/api/dashboard` 任务列表按 principal 过滤，日志/质量/告警对非 admin 403（`tests/test_approvals.py` 真 HTTP） | token → 任务/成本/产物三类数据的作用域隔离 | **两个 token 互不可见对方任务（真 HTTP 断言，非 mock）〔2026-10-12 达标〕** |
| 状态后端 | 队列后端可换（2026-10-12，P1-B）：`LOOM_DATABASE_URL` 指向 Postgres 时 `TaskQueue` 走 PG（psycopg，`nexusloom[postgres]` extra；不设 = SQLite WAL 默认不变）；并发契约由 SQL 层保证（rowcount 归属 / ON CONFLICT 幂等 / lease 回收跨后端同义）。**仍单机**：消息总线持久层、cost/quality feedback、pub/sub 进程内回调 | 后端抽象（SQLite / Postgres），队列与总线可换 | **两实例共享一库并发跑通（P1 判据）〔2026-10-12 达成：真 PG 上两进程领取互斥 + 两个真 TaskWorker 共享一库跑完 test_pipeline〕**；总线可换是下一批 |
| 配置热更新 | **已达标（2026-10-13，P1-C）**：运行时变更写覆写文件 `<状态目录>/config_overrides.json`（revision 单调递增 + 点号平铺键 + 原子写），级联位置在环境变量之后；新进程构造即读、常驻进程 `poll_overlay()` 轮询即热加载；`POST /api/config` 与 `/reload` 均带传播结果（失败=仅本进程生效，响应如实带 `propagation_error`，不静默）；worker 循环与 Admin API 请求入口均接线。判据 `tests/test_config_overlay.py` 23 条（含真跨进程两进程共享状态目录） | 配置变更对所有相关进程生效 | **`/api/config` 热更新不再只对本进程生效〔2026-10-13 达标：真子进程轮询到父进程写入；P1 §3 三条验收判据至此全部闭合〕** |
| 成本归因 | 定价表 + 按供应商/Agent 维度已有 | 增加按 **pack / token** 归因 | `GET /api/cost` 能按 pack 出账 |
| 审计 | **入站调用与配置变更均已留痕**（2026-10-08：webhook 全部调用成败落 JSONL；`/api/config` 每次变更落 `audit/config.jsonl`——key/新旧值/客户端/凭证指纹，敏感值只记脱敏形状，`GET /api/config/audit` 可查回） | 入站调用与配置变更留痕 | 每次 `/api/config` 变更有审计记录（`tests/test_config_audit.py` 九条钉住，含脱敏与写盘失败不静默） |

### 5.5 CI 与门禁（跨层，先于一切功能）

按 FP-2 修三处静默判绿；**性能门禁 `perf-regression` 已纳入 required**（2026-10-12，§9.2 决策 A 执行）〔实测读回：required = `test (3.11/3.12/3.13/3.14)` + `docker` + `perf-regression` 共 6 项，`strict=true`，`enforce_admins=true`（2026-10-08 已翻 true；本文旧句写 false，属过期口径）〕。依据=近 42 次连续绿（PR 18 + main 24）+ 波动带机制（区分 runner 整体慢与代码真慢）+ 22 秒级耗时；抖动处置=重跑该 job 或 `workflow_dispatch` 刷基线。

---

## 6. 前置必修：四个假阳性（不修则所有验收判据都能被"绿而未跑"骗过）

| 项 | 状态（2026-10-07） | 落点 |
|---|---|---|
| FP-1 质量门假绿 | **已关闭** | `4ede02b` |
| FP-1b 成品夹带抓取层原始素材 | **已关闭**（FP-1 的余波，见下方说明）：gate 与 CI 两侧各带同一判据，CI 不再只信 gate | 本轮提交 |
| FP-2 CI 三处静默判绿 | **已关闭并经真实 CI 确认** | `9ecfc12`；CI run 37504991608 在 `690c6d7` 上 5 条 required + perf 全绿，日志实证 `结果: 26 OK / 2 WARN / 0 ERROR` 与 `VULNS: []`（即 rc 被采信且没有误红） |
| FP-3 MCP stdout 污染 | **已关闭** | `10cc2e7` |
| FP-3b `--json-output` 的 stdout 混入过程输出 | **已关闭**：`--json-output` 时过程输出整体改接 stderr，JSON 是唯一一行 stdout（`tests/test_cli_output_channels.py`） | 本轮提交 |
| FP-4 `foreach` 未接线 / 无时间触发 / 无人审 | **三项全部落地**（2026-10-15 复测）：`foreach` 接线并出厂（续4，`tests/test_foreach.py` 46 条 + `pipelines/api-digest.yaml`，六处变异对照全被抓住）；时间触发 = `loom_core/triggers.py`（`triggers.yaml` → 到点提交成一次普通 run，`--triggers` / `--triggers-dry-run`，判据 `tests/test_triggers.py`）；人审 = `scheduler._as_approval` 声明 + `dag_executor` 闸门 + `POST /tasks/<id>/approve\|reject`，判据 `tests/test_approval_gate.py`。本行原写"仍为 0"是 2026-10-07 那一轮的实测，**已被后续两轮实现覆盖**——现测命令：`grep -rn "_as_approval" loom_core/scheduler.py`、`ls loom_core/triggers.py tests/test_triggers.py` | §5.1 |

**FP-1b 的来源（重要，因为它说明"修好一个假阳性"会暴露下一个）**：真实 CI 在 keyless 条件下产出了一份
**21,926 字节**文档并被本步骤记为 `OUTPUT FIDELITY OK`；本机同形状产物 28.4 KB、`quality_gate` 95.5 pass。
占比判据拦不住它——16 个章节里只有 4 个是占位符（25% < 34%），其余章节被喂进了抓取层的**原始素材块**
（`标题:/来源:/下载时间:` + 60 连等号分隔线，`agents/fetcher.py:700-703` 的落盘格式）。
先试过重复率与"导航行占比"两个候选指标，实测在真假样本上都没有可分离的分布（junk 样本
`dup_para_rate=0.167`、`menu_like_rate=0.024`，与真样本几乎同量级）——**用这种指标做门禁等于再造一个假绿**。
最终改用结构性签名：签名取自 fetcher 自己的落盘格式，不是关键词猜测。
> 遗留（不是本轮范围）：真正该做的是 writer 不再粘原块 + 主题相关性/引用可追溯性判定，
> 见 §5.1 与后续 P0。
> 进展（2026-11-10，治本批）：「writer 不再粘原块」已对 keyless 两条 web 素材路径
> 落地——无 Key 时 writer 拒绝拼贴、只交 `KEYLESS_REFUSAL` 降级自述（新进门禁占位
> 标记，命中判红）；有 Key 路径本就不出厂原文（LLM 重构覆盖）。KB 抽取式草稿按
> 其设计契约保留（内容本就叫摘录，不算粘原块）。剩余：「主题相关性判定」与
> 「引用可追溯性」仍开（排第二批）；另有一处窄边界如实登记：有 Key 但 prompt
> 模板为空时 `_restructure_document` 仍返回 None，回退拼贴未堵。

### FP-1 · 质量门给空壳文档打 98.8 —— P0，代价 S

〔实测，本机 keyless / `pool_size: 2` / 单进程〕`--pipeline docgen` **exit 0**，落盘 3065 字节，`overall_score = 98.8 status = pass`，`checker` 只报 4 条 P3；而正文是 360 识图页面的样板文案，`writer.stats.empty_sections = [核心概念, 详细分析, 实践与应用, 总结]`（**4/5 章节为空**）。

根因三条：
1. `agents/quality_gate.py:60` —— `PLACEHOLDER_MARKERS` 只有 `"未采集到可整合的搜索结果"`、`"无待整合内容"` 两条；writer 降级时实际写的「降级声明」「（暂无可用的相关内容）」都不在其中，于是底线只剩"≥120 字符"，而垃圾有 3000 字符。
2. `run.py:349-352` —— 引擎**已经算出** `empty_sections`，却只 print 一条 WARNING，不参与 status/exit。信号算完就扔。
3. 评分维度被抓取噪声反向骗过：`substance = 100`（导航文案信息密度高、重复率低）、`topic_relevance = 100`（页面标题恰含主题词）。

修法：底线补三条判据（空章节占比 / 降级声明命中 / 抓取元信息行占比），`run.py` 的 WARNING 升级为影响 exit code；以本次实测输出为回归样本加测试。

### FP-2 · CI 三处静默判绿 —— P0，代价 S–M

〔静态，行号来自 `ci.yml` 当前工作区版本〕
1. `:90-98` `python run.py --check` 跑在 `set +e` 下，**退出码从未被读**，只用 grep 卡一行 selectolax。本机 `--check` 现为 `26 OK / 1 WARN / 1 ERROR`〔实测〕而 CI 照绿。
2. `:76-85` pip-audit **进程失败被强制 `rc = 0`**，只有跑出干净 JSON 且含漏洞才红 → 断网/超时 = "无漏洞"。
3. `:113-119` keyless docgen 只要日志含 `HARD_FLOOR` 就记 "SMOKE OK" → **CI 从不证明系统能产出任何东西**。
4. 连带：`:126-134` 的产物门禁是"≥800 字节 + 不含一条指定串"，**恰好挡不住 FP-1 那份 3065 字垃圾** —— 两个洞互补，单修任一个仍留绿。

修法：`--check` 退出码读回并区分 WARN/ERROR；pip-audit 失败改显式红或以 `xfail(strict)` 承载已知缺口；smoke 拆成"有 Key 才判成功，无 Key 明确标 skip 而非 OK"。
注意：修完 CI 很可能立刻变红〔实测：同一 SHA `14c98b3d` 的 E2E Nightly 已是 failure〕。**按"全绿才合、合并权在维护者"执行。**

### FP-3 · MCP stdio 被 banner 污染 —— P1，代价 S

〔实测〕起真子进程 `python run.py --mcp` 喂 `initialize` + `tools/list`：stdout 头两行是 `run.py:58-63` 的框线 banner，之后才是 JSON-RPC；`serverInfo.version` 恒为 `unknown`。现有 12 个 MCP 测试全在进程内调 `_handle_request`，**没有任何一条走过真实 stdio**〔静态〕，所以该 bug 永远测不到。A 路线的第一入口就是 MCP，stdio 通道被污染属协议级问题。

### FP-4 · `foreach` 已声明未接线 —— P1，属立项而非修复

〔静态〕`scheduler.py:52-76` 的 `_as_foreach` 零调用点（`DEFAULT_FOREACH_MAX_ITEMS` 只在自己函数内用），而 `naming.py:6` 把 foreach 写成目标能力。全仓无 cron/时间触发、`grep approval|human|manual_review` 0 命中。
处置：**接线**（按"已声明未接线要么接要么删"的规矩）。`foreach` 是相对 MAOP 的差异化表达力。代价 M（3–5 天）：幂等键按 item 分裂 + 上限护栏 + item 数进 `topology_hash` 与锁文件。
附带立项：**人审 / approval 节点**（当前完全没有），因为"取数 → 核查 → **待批** → 交付"是运行时类产品的常见诉求。

> **进展（2026-10-07·续4，实测）**：`foreach` 一项已接线完成——解析期 `_as_foreach` 有了调用点
> （`AgentConfig.foreach` / `ExecutionNode.foreach`，并拒绝 `foreach`+`call`、`foreach`+`pool_size>1`），
> 运行期 `_execute_foreach` 按项投递（引擎自有键 `item`/`index`/`count`、逐项幂等键后缀 `#{i}`、
> 逐项限流、空列表/非列表/取不到值/超 `max_items` 均显式报错、任一项失败即节点失败），
> 展开契约进了 `topology_hash`；出厂消费者 `pipelines/api-digest.yaml`（+ lock，
> `topology_hash=001d667537bd`）；判据 `tests/test_foreach.py` 46 条，含一条盯调用点的 AST 守卫，
> 六处变异对照全部被抓住。**同一节里剩下的两项仍是零**：全仓无 cron/时间触发，
> 也仍无 `approval` / 人审节点——本轮不占口径，别把"foreach 已接线"读成"FP-4 已关闭"。

> **进展（2026-10-15，实测）**：上面那句"仍是零"只对 10-07 那一轮成立，之后两项都补上了——
> 时间触发是 `loom_core/triggers.py`（`triggers.yaml` + `--triggers` / `--triggers-dry-run`，
> 到点提交成一次普通 run，判据 `tests/test_triggers.py`）；人审是 `scheduler._as_approval`
> 声明 + `dag_executor` 闸门 + `POST /tasks/<id>/approve|reject`（决策进节点结果、任务台账
> 与 `approvals.jsonl`，判据 `tests/test_approval_gate.py`，控制台 S2 已有操作落点）。
> ⇒ **FP-4 三项全部关闭**。复核命令见 §5.1 该行的现测列。

---

## 7. 命名与迁移（已确认：原地改名，本地路径迁至 `F:\Nexus\`）

### 7.1 命名系统（一次定齐，避免二次破坏）

| 项 | 现值 | 目标值 | 影响面〔静态〕 |
|---|---|---|---|
| 产品名 / banner | Doc-Pipeline · 文档生成流水线 | **Loom · Agent 工作流运行时** | `run.py:60`；"文档生成流水线" 11 处 / 8 文件 |
| 本地路径 | `F:\Nexus\Workflow\doc-pipeline` | `F:\Nexus\Loom` | 见 §7.2 |
| GitHub 仓 | `Levango7/doc-pipeline` | `Levango7/nexusloom` | 旧 SSH remote 需更新 |
| 发行名 | `doc-pipeline` | `nexusloom` | `pyproject.toml:6` |
| **import 根包** | `pipeline_core` | **`loom_core`**〔2026-10-12 命名收官第二段已落地：`pipeline_core` → `loom_core`（git mv 保历史 + 旧名 shim：转发别名、identity 保真、warn once；`pipeline_core/__init__.py` 文件保留，MAOP 的 `_resolve_doc_pipeline_root()` 探径契约与两条运行时 import 全部零改动）〕 | 原 647 处 / 131 文件已全量迁移 |
| 领域包 | `docpipeline` | **`loom_doc`**〔2026-10-12 已落地：git mv 全迁 + 旧名 shim（转发别名，identity 保真 + warn once）；**原目标名 `loom_pack_docgen`（pack 化）系 pack 拆分前的写法，核实修正**——该层（renderer/ingest/document_enhancer）被核心 agents 与 pack 双方共用，不是 pack 私有〕 | 83 处 / 23 文件 |
| console script / CLI | `doc-pipeline` | `loom`（动词：`loom run / plan / lock / pack / plugin`） | `pyproject.toml:41` |
| env 前缀 | `DOC_PIPELINE_*` / `DOCPIPE_*` | `LOOM_*` | 13 处 / 8 文件 + 16 处 / 26 文件 |
| **插件 group（新增）** | 无 | **`loom.agents` / `loom.packs`**〔2026-10-12 已落地；旧名 `doc_pipeline.agents`/`doc_pipeline.packs` 过渡期回退可读（命中记一次迁移提示）〕 | 20x 的开关 |
| MCP `serverInfo.name` | `doc-pipeline` | `loom` | `mcp_server.py:52` |
| 观测 namespace | `docpipeline` | `loom` | `observability.py:161` |
| **API 原语（新增）** | 裸 `task_id` 字符串 | **`RunHandle`**：`status / wait / cancel / resume / artifacts`（2026-10-24 Stage 3b 增第 6 个读维 `projection`：这次运行的投影到底落成没有——`complete` / `incomplete`（想写没写成，待补账）/ `legacy`（Stage 1 之前落账的历史运行按设计没有投影；B8 引擎收敛后新运行不再产生此标记）/ `unknown`；**标记排在"投影里查得到行"之前**，因为盖章失败时 `runs` 那行永远停在 `running`，只看有无行会读成正常完成。经 `to_dict()` 从 MCP `get_task` 暴露；Admin 侧由 `/api/health/deep` 的 `incomplete_runs` / `legacy_runs` 聚合报数，单任务详情不带该键）〔2026-10-15 落地：`loom_core/run_handle.py` + `PipelineOrchestrator.run_handle(task_id)`；spec 原写的 `await` 是 Python 关键字，落地名 `wait()`，语义不变〕 | **已接线**：MCP `get_task` 与 Admin `GET /tasks/<id>` 的 `artifacts` 字段由它产出（同源，判据 `tests/test_run_handle.py`）；POST 提交类响应仍以 `task_id` 为凭据——句柄是"读一次运行"的入口，不是收据。产物**不因内存窗口失效**：任务历史只留最近 100 个（`_trim_task_history`），被淘汰的运行由 RunStore 投影重建（`runs`/`run_nodes`，2026-10-21 Stage 2），投影没有才回落到 `deliveries.db` 账本（2026-10-15；边界=Stage 1 之前落账的老运行没有 `artifact_keys` 字段，其清单为空，不回填） |

> **命名已确认（2026-10-07）**：产品名 **Loom**；`handle` 这个词留给 API 原语 `RunHandle`
> ——运行时交给调用方的本来就是一个句柄，而现在 HTTP/MCP 传的是裸 `task_id` 字符串。
> 占用实测（HTTP 200=已占、404=空闲）：PyPI 裸名 `loom`/`chassis`/`tessera`/`weft`/`flowkit`/
> `runloom`/`agentloom`/`loomkit`/`handle` **均已被占**（其中 `handle` 是 0.0.0、summary 写
> "placeholder" 的空壳抢注），`nexusloom`/`loom-runtime`/`handle-runtime`/`nexloom` **空闲**；
> npm 的 `handle` 与 `official` 都已占。故发行名必须带前缀，不能裸 `loom`。
> `official` 已否决：形容词无专指、搜索不可达、CLI 读作无主语、`import official` 与
> Vue 生态"官方模板目录"的既有心智撞车。`track` 已否决：PyPI 被真实包占（基因组数据读写），
> 且默认联想是追踪/埋点。
> 已避开：Conductor / Cadence / Temporal / Prefect / Weave / Flux / Foundry（同层同名产品）。

### 7.2 迁移依赖（实测：唯一外部消费者是 MAOP）

跨 `F:\Nexus` 全树扫描，除本仓两份历史审计文档（记录旧路径，不改）外，只有 **MAOP** 与它的工作副本 **MAOP-pr64** 依赖本位置：

- `MAOP/py/maop/delegate/doc_pipeline_adapter.py:38-55`：按三条候选找根 —— `DOC_PIPELINE_ROOT` env → `~/Nexus/Workflow/doc-pipeline` → `repo_nexus/Workflow/doc-pipeline`，**判定文件是 `pipeline_core/__init__.py`**（这也是包名不能轻改的直接证据）。
- `MAOP/py/tests/test_doc_pipeline_adapter.py:62,68,79,86,93`：**硬编码 `F:\Nexus\Workflow\doc-pipeline`**，直接读 `pipeline_core/pipeline.py`、`circuit_breaker.py`、`event_hook.py` 源码文本；不在 `skipif import pipeline_core` 的保护范围内 → **搬目录后 MAOP 测试直接红**。
- 同步项：`config/agents.yaml:176-192` 与 `:737-748` 两处条目、`core/reliability/circuit_breaker.py:38` 的 agent 名单、MAOP docs 4 处。
- 本仓 `.test_state/`、`logs/`、`bus_data/` 中的绝对路径全是 gitignore 产物，搬完清掉重跑即可。

### 7.3 迁移动作序列（顺序不可颠倒）

**边界先说清：本产品是独立项目，不并入 MAOP。** MAOP 是外部调用方（`MAOP/config/agents.yaml:176` 已把本仓登记为一条 `driver: python` 适配器），这个关系本身就是"独立"的证据。MAOP 侧硬编码本仓绝对路径属**它的技术债**，不构成本项目并入的理由；四条独立成仓的硬理由：① 20x 的物理来源是第三方能 pip 安装并发插件，合并进治理层后插件要跟着 MAOP 发版；② 两套执行模型（派发外部 CLI 外壳 vs 跑进程内 Python 节点）混在一仓会抹掉差异化；③ 许可结构会糊——`MAOP/LICENSE` 是 MIT 而 `MAOS/LICENSE` 是 `Commercial License - Levango7`，运行时一旦被并进 MAOP 线就会被期待做成 MAOS 商业特性；④ 终态应是 MAOP 依赖发布出去的包，而不是 `sys.path.insert(外部仓路径)`。

1. ~~先把在飞的 10 个未提交文件收成一个 commit~~ **已完成**（`d538f8a`，与产品定义/修复工作分账）。
2. 关闭 §6 的前置必修项后再搬，避免"搬迁 + 修 bug"两种失败原因混在一次 CI 里。
3. GitHub 端 rename（保留重定向）。
4. 本地整目录 move（不 cp，保 `.git` 完整）。
5. 更新 remote URL + 清 gitignore 产物（`bus_data/`、`logs/`、`.test_state/` 里都是旧绝对路径）+ 重跑全量测试。
6. **MAOP 侧不改仓**（已定）：迁移时设环境变量 `DOC_PIPELINE_ROOT=<新路径>` —— `doc_pipeline_adapter.py:38-55` 的第一候选就是它，于是 MAOP 立刻能找到新位置，无需它侧任何提交。
   - 残留：`MAOP/py/tests/test_doc_pipeline_adapter.py:62,68,79,86,93` 五处**硬编码绝对路径**，环境变量救不了它们（不经 `_resolve_doc_pipeline_root()`）→ 搬完 MAOP 那 5 条用例会红。这是 MAOP 仓的账，**改动权在维护者**，本项目只记账不代改。
   - 若日后想让旧脚本零改动继续跑，可在旧路径留 Windows junction 兜底；代价是机器上出现"看着像两个仓"的入口，容易误编辑，非必要时不采用。
7. 才开始动代码内的名字（顺序：banner/CLI 文案 → env 前缀 → MCP/观测标识 → 包名（需与 MAOP lockstep）→ pack 拆分）。**〔2026-10-10：第一步「banner/CLI 文案」已做**——run.py 的 docstring/banner/argparse 三处改为「Agent 工作流引擎」，`--help` 用户可见面零"文档生成流水线"字样（判据 `tests/test_cli_wording.py` 2 条，含静态防回归）；动词收敛 / --enhance 摘除 / env 前缀 / MCP 标识仍待后续〕。**〔2026-10-11：包名已落**——发行名 `nexusloom` 已上 PyPI（3.9.2），GitHub 仓 `Levango7/nexusloom`，本地路径 `F:\Nexus\Workflow\nexusloom`〕。**〔2026-10-12：最后一步 pack 拆分已做**——`agents/{writer,quality_gate,checker,layout,fact_checker}.py` 与 6 条 docgen YAML + `_quality-tail` + quality/prompts 移入 `packs/docgen/`（git mv 保历史），`pack.yaml` manifest 落档；引擎全部改为多目录发现，`--check` 与全量判据绿〕。**〔2026-10-12 尾款：动词收敛 + env 前缀 + MCP/观测标识三件同批落地**——①CLI 动词层 `run/plan/lock/pack/plugin`（`--plan`/`--dry-run`/`--write-lock`/`--list-agents` 摘除；`--enhance` 一族摘除、库保留；裸输入 = 隐式 run；判据 `tests/test_cli_verbs.py`）；②env 前缀 `LOOM_*` 落地——`LOOM_ROOT`/`LOOM_STATE_DIR`/`LOOM_VERSIONS_DIR`/`LOOM_WEBHOOKS_FILE`，**旧名 `DOC_PIPELINE_*` 过渡期回退可读**（MAOP 兼容：它侧设的 `DOC_PIPELINE_ROOT` 继续生效；判据 `tests/test_env_prefix.py`）；③MCP `serverInfo.name` = `loom`、观测 namespace/日志前缀 = `loom`。**未改名项（刻意保留）**：import 根包 `pipeline_core`/`docpipeline`（§7.1 第一阶段保持不动，且被 MAOP 硬绑）、entry_points group（`doc_pipeline.agents`/`doc_pipeline.packs`，待第三方插件出现前再定）。**〔2026-10-12 命名末批·第一段：领域包 + 插件 group 落地**——`docpipeline` → `loom_doc`（git mv 保历史 + 旧名 shim：转发别名、identity 保真、warn once；`docpipeline/__init__.py` 文件保留，维持 MAOP `_resolve_doc_pipeline_root()` 的探径契约；原 `loom_pack_docgen` 目标名按 pack 拆分后的实际（共享域层）修正）；entry_points group → `loom.agents` / `loom.packs`（旧名双读回退 + 迁移提示）；判据 `tests/test_naming_compat.py`。第二段 `pipeline_core` → `loom_core` 已随同批后续 commit 落地（同一 PR：git mv 保历史 + 旧名 shim（identity 保真、warn once、`pipeline_core/__init__.py` 保留维持 MAOP 探径契约）；仓内 191 文件机械迁移；pyproject/CI 三检/verify_dist（含安装态 shim 同一性断言）全链更新；发布门禁刻意 import 旧名验证 shim、由反向守卫豁免）。****〔2026-10-12 生态轴 0→1：首个真第三方插件**——`Levango7/loom-plugin-sqlite`（外部仓独立、发行独立）：只读 SQLite 取数 Agent，补“本地数据库→报告”取数面；纯净 venv（PyPI 正式宿主 3.10.0/3.11.0 各一遍）零改动被发现/注册/执行，报表与审批闸门两条管线真跑；外部仓 CI 双宿主版本矩阵全绿。**三个过渡期坑已成范式**（外部作者照抄外部仓 README）：宿主 import 名双名兼容、entry 组名双组声明（3.10.x 只认旧组）、纯取数管线的 input 契约（首行需可提取主题）。**

---

## 8. 首发 pack 清单（默认选型，可替换）

凑 §2 的 ≥10 任务类型。前 8 个为默认选型，后 2 个为候补：

| # | pack | 主要复用件 | 为何选它 |
|---|---|---|---|
| 1 | 文档生成（现 docgen 降级） | writer/quality_gate/checker/layout/fact_checker/renderer | 存量能力，证明"引擎不换语义也能跑旧活" |
| 2 | API 报告 | http_request/transform/safe_writer | 已有 `api-report.yaml`，是插件契约的现成消费者 |
| 3 | 竞品/技术情报 | researcher/fetcher/transform | 检索+抓取是已实测最成熟的两个通用件 |
| 4 | 本地资料 → 知识摘要 | ingest/knowledge_base/embeddings | `kb-docgen` 已离线跑通 |
| 5 | 数据集质检报告 | http_request/transform/quality_gate 骨架 | 让质量门在非文本产出上被验证 |
| 6 | 会议纪要 → 周报/OKR | transform/safe_writer/version_manager | 高频、无需外部 API |
| 7 | 告警 → 处置手册 | ingest/transform/writer 模板 | 把 fact_checker 用在结构化证据上 |
| 8 | 合同/发票要素抽取 | ingest/http_request/transform | 考验"受限声明语言"表达力，反向推动 `foreach` |
| 9 | K8s 巡检报告 | http_request（API server）/transform | 需内网白名单，正好压测 `allow_hosts` |
| 10 | 需求 → 测试用例 | requirements_analyzer/transform | 已有 `docreq` 半接线 |

**交付状态（2026-10-09）**：1–10 全部落地为 `pipelines/*.yaml`；表外另加 1 条 `deck-brief`（汇报要点 → md + pptx 双交付），合计 11 条 pack，每条都有独立 `.lock` 与真 E2E 判据（清册见 §2 表）。第 8 条的 `foreach` 与第 9 条的 `allow_hosts` 由此从机制变成出厂流水线里的实际载荷。

---

## 9. 未决项

1. ~~是否授权改 MAOP 仓~~ **已定：不改**（§7.3 第 6 步）。迁移用 `DOC_PIPELINE_ROOT` 环境变量指向新路径；MAOP 那 5 处硬编码测试会红，属它自己的账，本项目只记账不代改。
2. ~~`perf-regression` 是否纳入 required~~ **已拍板并执行（2026-10-12）：走 A（纳入）**——分支保护实测读回为 6 项（`test 3.11–3.14` + `docker` + `perf-regression`，`strict=true`）；依据=近 42 次连续绿（PR 18 + main 24）+ 波动带机制 + 22 秒级耗时；抖动处置路径=环境性红重跑该 job（基线在缓存）或 `workflow_dispatch` 刷基线（§5.5 已同步）。`enforce_admins` 子项早已闭合（2026-10-08 翻 true，2026-10-12 实测确认）。
3. ~~人审/approval 节点进 P0 还是 P1~~ **已拍板并实施（2026-10-12）：走 C（机制先行最小闭环）**——声明式 `approval` 节点（挂起复用 PAUSED 边界 + checkpoint）、`POST /tasks/<id>/approve|reject`（reject=终止含原因，不做分支）、审计落库（复用 audit 通道）均已落地（`feat/approval-gate`，`tests/test_approval_gate.py` 五层判据）；通知/控制台/多级审批/超时策略留 P1，见「决策备选·3」实施落地附注。
4. ~~Chassis / Tessera 的 PyPI 占用复查~~ **已查**：两者裸名均已被占（§7.1 注）。当前定名 Loom / 发行名 `nexusloom`（实测空闲）。
5. ~~FP-2 的真实验证只能在 CI 上做~~ **已关**（2026-10-12）：CI 全绿已在 ubuntu runner 上多轮重取（test 3.11–3.14 + docker + perf-regression；`--check` 退出码与 pip-audit 进程健康均为硬依赖），§3 P0 判据第 4 条以此为准。
6. ~~README 文档表与 CHANGELOG 对本 spec 的引用~~ **已关**（2026-10-12）：README 文档索引表已加 `product-spec.md` 一行；CHANGELOG 对 spec 的引用随各批同步记录。

### 决策备选·2 —— `perf-regression` 进不进 required（待拍板）

**事实**（2026-10-12 实测）：该 job 在 PR 与 main 上都会跑（`needs: test`），耗时约 22 秒–1 分钟；含跨 run 波动带（`benchmark.py` 的 `_historical_spreads`，区分"runner 整体慢"与"代码真慢"）与 success-only 基线滚动（失败 run 不污染基线）；近期 42 次连续绿（PR 18 + main 24），历史抖动（早期"同代码两次跑 12 项 vs 2 项回归"）在波动带机制落地后未再复现。

- **A. 纳入 required（+1 项，共 6 项）**：门禁真正生效——性能回归与功能回归同级阻断合并。优：与"CI 全绿才可合并"的既有纪律一致；代价记入文档，不再靠人肉判读。劣：runner 环境抖动时 PR 会被阻塞，处置=重跑该 job（基线在缓存里，22 秒级，成本低）。
- **B. 维持非 required（现状）**：保留观察期。优：零阻塞风险。劣：门禁只是"参考"——红不挡合并，需要每次人肉判读"这次红是真回归还是噪音"，而人肉判读是最不可靠的一环（非必需红被继承、被无视是业界常态）。
- **C. 折中：先加 required 但设 60 天回顾**：等同 A，另约定一次回顾（若出现环境性阻塞红则回退/调阈值 0.30）。优：兼顾门禁刚性与纠错。劣：回顾是口头承诺，不如 A 干脆。

**推荐：A**（42 次连续绿 + 波动带机制实证 + 22 秒成本），C 为次选；不推荐 B。

### 决策备选·3 —— 人审/approval 节点进 P0 还是留 P1（待拍板）

**事实**（2026-10-12 勘察）：全仓 `approval/human/manual_review` 0 命中；但**半个机制已在**——`TaskStatus.PAUSED` + 边界 checkpoint 保存 + 执行循环阻塞等待（`pipeline.py:566-596,768-776`）+ Admin API `POST /tasks/<id>/pause|resume`（人工对 running 任务暂停/恢复）。缺的是：① 声明式 YAML 的"审批节点"（跑到此处挂起等批，而不是人对跑着的任务按暂停）；② approve/reject 的决策语义（reject=终止还是走分支）；③ 审计（人/时间/决定；`pipeline_core/audit.py` 可复用，含客户端指纹可充当"审批人"标识）；④ 通知（审批发生时告知人）。

- **A. 留 P1（现状）**：不新增。优：与 P1 的"自托管运行时"（身份/控制台/状态后端）天然协同——审批的"谁批"要身份系统，界面要控制台，届时一起做不返工；现在没有真实使用场景，做了没有消费方。劣：作为"多功能工作台"，流水线缺人工干预点，"取数→核查→待批→交付"类场景用不起来（spec FP-4 原文点名的诉求）。
- **B. 现在完整做（P0）**：机制+API+审计+通知+（简易）界面。优：机制闭环一次到位。劣：在没有身份系统的前提下"审批人"只能是凭证指纹，语义勉强；通知渠道（企业微信/钉钉/飞书等国内渠道可达）均需用户注册配置凭据，属于产品化投入而非引擎机制；控制台类界面属 P1 范围，P0 明确"不做任何新 UI"。返工风险最大。
- **C. 机制先行，最小闭环现在做（推荐候选）**：只做引擎层——YAML 声明式 `approval` 节点（挂起→释放等待）、`POST /tasks/<id>/approve|reject`（决策面，reject 语义=终止并可注释原因，不做分支）、审计落库（复用 audit.py）。通知/控制台/多级审批/超时策略留 P1。规模约 1–2 天，复用已有 PAUSED/checkpoint/审计三个半成品，不碰 UI。优：以最低成本补齐"工作台"关键机制，P1 只需接界面与通知。劣：仍无"真名实姓"的审批人（记凭证指纹），需要向使用者说明这一边界。

**推荐：C**；若倾向"不见真实场景不动工"则 A；不推荐 B。

**实施落地（2026-10-12，决策 C）**：引擎层最小闭环已完成——scheduler 解析期校验（`approval: true | {message}`，形状漂移即炸；与 Agent 字段互斥；声明并入节点配置指纹，改措辞必漂锁）、pipeline 挂起与决策状态机（`awaiting_approval`/`approvals` 台账进 `to_dict`；闸门等待期 `resume` 被拒；approve→RUNNING / reject→CANCELLED 含原因）、admin API `POST /tasks/<id>/approve|reject`（未在等待中→409，决策进不了审计）、审计落库 + `task.approved/rejected` 事件、pack 清册豁免（闸门不是 agent）。`tests/test_approval_gate.py` 覆盖解析/清册/真 orchestrator 执行/条件闸门/HTTP+审计五层判据。**P1 余量**：通知渠道、控制台界面、多级审批、超时策略、真名实姓的审批人（当前记客户端指纹）。
