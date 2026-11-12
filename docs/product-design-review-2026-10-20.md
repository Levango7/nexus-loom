# Loom 产品与架构设计复核

> 复核日期：2026-10-20 ｜ 基线：`main` @ `2eba599`（v3.12.0 已发布，PyPI latest=3.12.0）
> 方法：全仓静态检索 + 本机实测（Python 3.14.3，无 LLM Key，单进程）+ 两份独立子审计交叉复核。
> 本文是**判断与提案**，不是既成事实的复述；每条结论尽量给可复验的落点。
> 下文“现场基线”描述 v3.12.0 发布时的状态，不应当作当前主干缺陷清单。

### 实施状态（2026-10-20）

- #49 已合入 `a13526a`，主干 CI 已通过：定位首屏收口、控制台资源进包、待审批收件箱、部分聚合端点的作用域控制已实现；不代表已发布到 PyPI。
- 浏览器复核发现成本/质量/日志响应没有写入页面状态，以及取消驳回继续确认的问题；本轮修复并新增 `tests/check_dashboard_browser.py`，真实 Chrome 七组检查通过，旧版反向验证失败。该脚本使用固定 HTTP 响应，不冒充后端集成或全面 UI 验收。
- 审批边界加固已交付：决定绑定闸门节点集合、暂停边界保存完整检查点、落库失败不放行、活 owner 不被重排；并补上**真双进程**端到端（执行方仍活着停在闸门，另一进程批准→台账为唯一通路→暂停循环自行观察并跑完，前序节点用真实执行漏斗计数证明未重跑）。仍未闭合：跨主机接管同一队列（pid 存活判定只在单机成立），故不宣称多机高可用。
- 下一阶段先做统一运行查询与交付物读取，再评估存储迁移；不因提出 RunStore 就立即替换所有数据库。

### 实施状态（2026-10-26 追加：本文 §3.1 的 A4/A6 与 §3.2 的 B4/B6 已在工程批次分支交付；A3 由 PR #65 交付）

- **A3 类型化输入**：由并行 PR #65（feat/typed-inputs）交付——声明式子集校验器、
  四条提交口（CLI/HTTP/MCP/webhook）同一道闸门、workflows 提交端点与声明读回
  端点、自由文本拦截；本分支不重复携带该实现。
- **A4 loom demo**：沙盒里 5 秒跑完一次完整交付（docx+html+质量门 82/100+
  账本），干净 venv 验收通过。P0 欠账清零。
- **A6 保留期 GC**：RunStore/队列/cost/quality_feedback 的双判据清理 +
  worker 周期接线（重启后继续）+ `loom --gc`。
- **B4 触发租约**：cron 触发的多进程互斥（schedules 表，条件 UPDATE 原子裁决，
  真跨进程判据钉住）。
- **B6 配置 CAS**：覆写写口的写后读回验证 + 重试（多进程写不同键不再互相覆盖）。
- 仍开放（不在本轮范围）：§3.1 的 A2/A5/A7、§3.2 的 B5/B8、§6 的 P2 生态线。

---

## 0. 一句话结论

**引擎是对的，产品身份是分裂的，控制台还不是产品。**

- 技术上，这已经是一个有真实差异化的执行内核：产出契约、锁文件拓扑指纹、交付账本、审批闸门、成本预算、六种触发面，且都有真判据。
- 但产品对外同时讲三个身份：**文档生成流水线**（README 主打）、**领域无关 Agent 运行时**（spec 定位）、**文档交付自动化**（实际默认配置与包内容）。三者互不背书，导致：新用户不知道这是什么、老用户不知道为何要升级、竞品分析不知道跟谁比。
- 控制台只有两块真屏（S2/S3），而且**根本不在 PyPI 包里**——`pip install nexusloom` 的用户看不到任何 UI（实测 3.12.0 wheel：152 个文件，0 个 dashboard 资源）。
- 因此本轮的优先级不是继续加能力轴，而是 **收敛身份 → 修产品级缺陷 → 把"可验收交付"这一个卖点做成可感知的产品时刻**。

---

## 1. 现场基线（实测，条件随数字）

| 面 | 数字 | 条件 / 落点 |
|---|---|---|
| 发行物 | PyPI `nexusloom` latest **3.12.0**；v3.12.0 release run 三段全绿 | 2026-10-20 实测 |
| 引擎 | `loom_core` 18,206 LOC / 50 文件 | 工作树静态计数 |
| 域层 | `loom_doc` 1,671 / 5；`agents` 4,027 / 15；`packs/docgen` 2,817 / 7 | 同上 |
| 测试 | 131 个 `test_*.py`；本机全量 `2822 passed / 7 skipped / 6 deselected`；CI 四腿各 `2817 / 11 / 6` | 本机 741.61s；CI 腿多 skip 在 PG 与可选依赖 |
| 覆盖率 | 门禁 83%；README 记 87.99%（本机 10-08）/ 87.28%（CI 10-08） | 当前本地无同口径全量覆盖率产物，**不可复验**，引用须带日期 |
| 控制台 | `dashboard/` 3 文件 2,334 行（HTML 212 / JS 1,260 / CSS 862）≈82 KB；无框架、无构建 | 手写 vanilla JS |
| 打包 | wheel 内 **0** 个 dashboard 资源；`pyproject.toml` 对 `dashboard` 零命中 | 3.12.0 wheel 实测 |
| 持久层 | 队列可切 PG；**消息/cost/quality/deliveries 仍是 SQLite-only** | `deliveries.py:48`、`cost_tracker.py:102`、`quality_feedback.py:50`、`message_store.py:171` |
| 物理 pack | **1 个**（`packs/docgen`）；另有 13 条 YAML 直接躺在核心 `pipelines/` 里却被计作 builtin pack | `pack_manifest.py:198-205` |
| 触发 | 6 种（CLI/HTTP/MCP/cron/webhook/queue），cron 无分布式租约 | `triggers.py:226-255` |
| 审批 | 闸门状态只在内存；`approve()` 只查 `_running_tasks` ⇒ **重启后无法放行** | `pipeline.py:628-632` 实测 |
| 多租户 | `/tasks`、`/tasks/{id}`、`/api/cost` 已按 principal 隔离；**`/api/dashboard` 不隔离**（非 admin 也能看到全部任务/Agent） | `admin_api.py:1476-1507` 实测 |
| 验收证据 | 仓内**无**浏览器级测试；README/CHANGELOG 的"无头 Chrome 走查留证"在仓内无任何产物 | 131 个测试文件检索无 Playwright/Selenium |
| 残留旧名 | compose 镜像/容器 `doc-pipeline`；Dockerfile 示例命令；`/` 返回 "Doc-Pipeline Admin API v1"；OTel tracer `doc-pipeline` 3.9.1；dashboard 标题 "Doc-Pipeline Dashboard" | 各自实测 |

---

## 2. 定位复核：三个冲突的身份

### 2.1 冲突证据

| 场合 | 它说自己是 | 落点 |
|---|---|---|
| README 第一屏 | **"主打：多 Agent 协作的文档生成流水线"**，场景分级 9 条里 6 条是文档 | `README.md:3-20` |
| 产品 spec | **"领域无关的 Agent 工作流运行时"，docgen 只是第一个 pack** | `product-spec.md:21` |
| 实际发行物 | 默认 pipeline = `docgen`；13 条领域流水线在核心里；`loom_doc` 随包；admin 提交把 `title/query` 物化成临时 `.md` | `run.py:593`、`admin_api.py:1284-1331` |

这不是"文档没跟上"，而是**产品承诺与代码承诺不一致**：一个真正的通用运行时不会把 `generate_document` 作为 MCP 一等工具、不会把文档默认值写进引擎配置、不会让 13 条领域流水线与引擎共享同一个数据目录。

### 2.2 市场判断（诚实版）

| 如果坚持"通用 Agent 运行时" | 判断 |
|---|---|
| 对手 | LangGraph（生态/文档/SDK）、Temporal（durable execution 血统）、Prefect/Dagster（编排）、Dify/Coze（终端用户 Agent）、n8n（集成数量） |
| 我们的优势 | 单机模式不要求外部数据库服务，具备产出契约、锁文件与交付账本；仍需要 Python 依赖 |
| 缺口 | 当前仓库可见的不足是客户端接入、控制台完整度与恢复一致性；社区规模和用户优先级未作调研 |
| 结论 | **广泛定位的获客与维护成本可能更高，这是待验证假设**。代码审计不能证明用户是否欢迎，也不能推断 SDK、UI、HA 必然是所有用户的前三需求 |

| 如果收敛到"**可验收交付**的工作流运行时" | 判断 |
|---|---|
| 一句话 | 把"取数/资料 → 加工 → 核查 → **人审** → 交付"做成可复现、可审计、可回滚的作业；交付物有质量底线与账本，不是聊天记录 |
| 买家 | 企业内部自动化/数据/合规团队：周报月报、监管报送、质检报告、告警处置手册、投标/方案文档 |
| 对手 | 内部模板工具链、DIY 脚本、文档中台；**不是** LangGraph/Temporal |
| 我们的差异化（已被判据钉住） | ① `PRODUCES` 未交付不算 done；② 空章节/素材直粘被硬底线拦；③ 锁文件防配置漂移；④ 交付账本 + 审批流水；⑤ 预算熔断；⑥ 单机可跑 |
| 验证目标 | 先邀请 3–5 个团队试用一种高频交付流程，确认可用性、复用频次与付费意愿；不把“数十团队/数百次交付”当现有需求或商业预测 |

试用按四周观察：首次有效交付耗时（明确是否已有模型凭据）、需人工返工比例、每周重复使用团队数、
每次被验收交付的实际成本。分母分别为首次上手尝试、所有提交审核的交付、所有参与试用团队、所有被验收交付；
失败/放弃也计入，不能只统计成功运行。将现有脚本作为对照，确认节省的人工成本后再决定扩展哪些 pack。

**建议**：架构继续"内核通用 + 领域 pack"（这是对的，不动）；**对外叙事收敛为窄承诺**——"可验收交付"。即：**宽内核、窄门面**。README 第一屏、PyPI 摘要、MCP 工具面、默认配置全部服从这句话。

### 2.3 定位纠偏清单

1. README 首屏从"文档生成流水线"改为"**可验收的 Agent 交付运行时**（文档/报告/数据集），docgen 是旗舰 pack"；场景分级重写为按**交付物类型**分，而不是按 pipeline 列。
2. 引擎不再硬编码默认 pipeline。`run.py` 的默认值改为"发行物默认 pack 提供的默认工作流"，核心不写 `docgen`。
3. MCP 工具面按产品名词收敛：`list_workflows / run_workflow / get_run / get_artifact / approve_run`；`generate_document` 降为 pack 自带工具（或保留为便捷别名并标注 pack 来源）。
4. 13 条领域流水线按 pack 归位（见 §5.4），核心里只留 2–3 条通用示例。
5. 全仓清除 `doc-pipeline` 运行面旧名（compose/Dockerfile/OTel/控制台标题/`/` 文案），保留 import shim 不动。

---

## 3. 产品设计：补什么 / 纠什么 / 删什么

### 3.1 必须补（按价值排序，均带验收判据）

| # | 功能 | 为什么是产品级缺口 | 验收判据 |
|---|---|---|---|
| A1 | **审批跨重启 + 待办收件箱** | "人审"是交付型产品的核心；现在重启后闸门永远无法放行（`approve()` 只查内存）。这是把差异化做成一次性演示 | 杀进程重启后 `POST /tasks/{id}/approve` 成功；跨进程两实例可见同一待办；真 PG 同样通过 —— **2026-11-12 T7 核验追加**：持久化与重启两小句已交付（审批状态机落 `loom_core/run_store.py:63`；`tests/test_approvals.py:196` 杀进程重启后 approve 放行并续跑、`:465` 收件箱按 principal 过滤且 durable；`test_approval_gate.py`/`test_runstore_approvals.py`/`test_dashboard_approval_inbox.py` 同批钉死）。**未验**：「真 PG 同样通过」无判据（本机判据走 SQLite 形态），认领不写死 |
| A2 | **交付物浏览器（Deliveries）** | `deliveries.db` 已经在记，但用户看不到；交付物的 checksum/来源节点/保留期才是"可验收"的实体 | `GET /api/artifacts?principal=&workflow=` 真 HTTP；UI 可下载并显示 sha256 与产出节点；被淘汰运行仍可查 —— **2026-10-26 读侧已交付（续64）**：落的是 `GET /api/deliveries` 与 `GET /api/deliveries/<run_id>`（真 HTTP 判据、逐行按投影裁决可见性），没另起 `/api/artifacts`——账本行本身就是交付记录，产物键挂在节点上。**未做**：sha256/保留期（要写侧与 A6 一起）、UI 下载呈现（前端批） —— **2026-11-11 T6 追加**：UI 下载呈现已交付（`GET /api/deliveries/<run_id>/download` + 控制台明细下载按钮，sha256/字节数随明细呈现）；sha256 写侧此前已落（`output_sha256`，v3.15.0 起），本批补 `output_bytes` 三态；保留期仍归 A6 口径，不在本批 |
| A3 | **类型化输入（JSON Schema）** | 现在 inputs 是"文件路径/## 键文档"，无法校验、无法生成表单、无法计费预估；这是所有触发面的共同入口 | `pipeline.inputs` schema 在提交时校验（CLI/HTTP/MCP 同一实现）；非法输入 400 且错误字段可定位；UI 依 schema 生成表单 —— **2026-10-26 前两小句已交付（续65）**：落的是**声明式子集**而非完整 JSON Schema（本仓不为此新增依赖，`type` 沿用引擎类型名），声明期红 + 提交期逐字段红，四条口（CLI/HTTP/MCP/webhook）共用 `input_schema.prepare_inputs` 一处实现，错误带 `field` 点号/下标路径。**要说清的边界**：声明**不进锁指纹**（进锁=随包 20 条全部重锁），且自由文本入口在声明存在时明确拒绝，否则留一条绕过门的路。第三小句只交付前提（声明可读：`GET /api/workflows/{name}/inputs` + MCP `get_pipeline_info.inputs`），控制台表单未做 —— **2026-11-11 T6 追加**：第三小句已交付，控制台按声明渲染逐字段表单（enum/bool 下拉、数值带范围步进、list/dict JSON 框、`default` 预填、必填留空提交前拦截），提交走 `body.inputs`；无声明流水线保持自由文本 `query` 不变 |
| A4 | **`loom demo` 60 秒离线体验** | 现在"快速开始"要装 requirements + 网络 + Key；新用户在第一步流失 | 干净 venv 里 `pip install nexusloom && loom demo` 产出一份 docx + 质量报告 + 账本记录，全程无网络无 Key —— **2026-11-12 T7 核验追加**：机制判据齐，已交付。`loom_core/demo.py` 沙盒目录产交付物 + 质量报告 + 账本记录，全程无网络无 Key；`tests/test_demo.py` 11 条钉死（离线端到端 / 交付物真实有内容 / 质量分真算 / 账本记录 / 沙盒隔离 / 缺渲染格式如实报跳过不假装成功 / 失败运行不掩盖 / walkthrough / 离线质量尾段 / 锁在且一致）。边界：docx 随 render extra，缺时如实报跳过而非假装成功；「干净 venv pip install」包装层未机验 |
| A5 | **可复现性的人话呈现** | 锁文件是差异化，但用户只看到"拒绝执行" | Run 详情显示 `topology_hash/config_hash` 与"本次与上次的差异项"；`loom plan --diff` 输出人话 —— **2026-11-12 T7 核验追加**：已交付。交付明细显示 `topology_hash` + 逐节点 `config_hash` 前 12 位（控制台锁指纹块）；`loom plan --diff` 人话三态（无锁文件 / 锁一致 / N 项漂移逐条列，`run.py:531`）；锁巡检面把「当前文件与锁不一致」指向 `plan --diff` |
| A6 | **保留期与 GC** | 队列/cost/quality/checkpoint 全部无界；`cleanup()` 从未被调度 | 维护循环按配置删除过期运行（含 events/artifacts/cost）；重启后仍执行；判据用假时钟 —— **2026-11-12 T7 核验追加**：已交付。`loom_core/maintenance.py` 双判据（`retention.runs_days` / `retention.max_runs`，任一 >0 才启用，0=保守默认）；worker 循环按 `maintenance.gc_ticks` 接线、常驻服务按 `maintenance.gc_seconds`（`loom_core/worker.py:108`）；判据注入假时钟（`tests/test_maintenance.py` `now=`），`tests/test_gc_liveness.py` 钉活性面（有活也回收 / daemon 路径 / dlq / 账实相符） |
| A7 | **Python 客户端 SDK** | 集成方现在只能裸 HTTP/MCP | `LoomClient`（submit/status/wait/artifacts/approve）与 OpenAPI 同源；集成测试走真 HTTP —— **2026-11-12 T7 核验追加**：已交付。`loom_core/client.py` LoomClient 五方法（submit/status/wait/artifacts/approve）的方法→(method, path) 表与 `generate_spec()["paths"]` 同源对照由 `tests/test_client_sdk.py` 钉死；集成走真 HTTP（`tests/test_client_contract.py` 本地 server 逐请求对账） |

### 3.2 必须纠（缺陷，不是偏好）

| # | 问题 | 证据 | 处置 |
|---|---|---|---|
| B1 | **控制台没进发行物** | 3.12.0 wheel 152 文件、0 dashboard；`pyproject.toml` 零命中 | dashboard 进 `package-data`/data-files；`GET /` 302/渲染 dashboard；装完即可用 |
| B2 | **租户泄漏** | `/api/dashboard` 不按 principal；`/api/agents`、`/api/logs`、`/api/quality/feedback`、`/api/alerts` 同样未隔离 | 统一 principal 作用域中间层；补"双 token 互不可见"的真 HTTP 判据（沿用 `test_identity_tokens.py` 口径） |
| B3 | **审批不持久** | `pipeline.py:628-632` | 见 A1；与 RunStore 一起做 |
| B4 | **cron 无租约** | `triggers.py:226-255` | 复用队列租约语义（lease_owner/lease_until），两进程只允许一个触发者 |
| B5 | **"状态后端可切"言过其实** | 仅队列可 PG | 二选一：把 approvals/artifacts/events 至少迁到 RunStore 双后端，或把文档口径收窄为"队列可切" |
| B6 | **配置 RMW 无 CAS** | `config.py:246-263` | revision 乐观锁：写回冲突即重读重试并如实报版本冲突 |
| B7 | **四源运行态** | 内存任务 / task_queue / checkpoint / deliveries 各自为政 | 见 §5.1 单一 RunStore + EventLog |
| B8 | **两套执行引擎、两套结果形状、两条配置路径** | `run` vs `run_plan`；`AgentResult` 零使用；`--config` 只进 legacy 路径 | ✅ **2026-10-27 完成**：`run()` 委托给 `run_plan()` 统一引擎；配置入口收敛（YAML 优先、legacy config 兜底）；死代码已移除（`_run_dag_parallel`/`_emit_task_events`/`_finalize_task_queue`）；legacy 路径现在写真实 RunStore 投影 |
| B9 | **文档过度声称** | README:976/979 "无头 Chrome 走查留证"（仓内无产物）；README:458 "无控制台界面"与 :84/:976 自相矛盾；spec §4.1 的体量数字过期 | 删/改声称；或补真浏览器冒烟（见 A/测试计划）后保留。**2026-10-21 又抓到同型两处**：README「可运行的参考实现共六个独立 pip 包 …`pip install ./examples/plugin-tldr`」与「`test_plugins_ecosystem` 一次真装六个包、数量不足 5 即红」——两者都随 2026-10-11 单发行物决策失效（`examples/` 只剩模板一支，实测该测试的三条判据是内置件 glob/模板形状/鬼魂目录/诚实计数，从不装六个包），已按测试真实内容改写 |
| B10 | **陈旧发布工作流** | `release-plugins.yml` 列 6 个插件目录（仅 `plugin-hello` 存在）且断言 `Requires-Dist: doc-pipeline` | **2026-10-21 已删除**（原建议"改成按存在目录发现 + 断言 nexusloom"撤回）：README/spec 记的是单发行物决策——`examples/plugin-hello` 明示"不再单独发行"，给它留一条可用的发布链等于制造一个本仓不该存在的第二发布物。实测该 workflow `total_count: 0`（一次没跑过），四个候选插件名在 PyPI 全 404。改判据把"唯一发布面 = `release.yml`"钉住（`TestSinglePublishSurface` 双向） |

### 3.3 应该删（范围纪律）

1. **把"六屏控制台 S1–S6"当目标 IA** —— 屏幕按子系统切分，用户按任务切分。改为 §4 的六个任务面。
2. **"20x = 924 组合"当对外卖点** —— 组合数度量的是表面积，不是价值。保留为内部覆盖指标，不进门面。
3. **`--daemon`（只是 sleep）** —— 误导；用 `loom serve`（admin+worker+triggers）替代。
4. **未验证的能力声明** —— `executor_type: process`（0 条流水线使用、无端到端判据）、`AgentResult`（0 个 Agent 使用）。要么补判据，要么删。
5. **admin 提交的文档语义** —— `title/query → 临时 .md` 是旧领域契约，统一到 A3 的类型化输入。
6. **13 条"builtin packs"的说法** —— 要么给每条 YAML 配 `pack.yaml` 成为真 pack，要么在清册里如实叫 `builtin pipelines`。

---

## 4. 控制台重新设计（IA 与交互）

### 4.1 判断

- 现有 dashboard 作为"监控页"是合格的：S2（DAG+审批条）与 S3（节点检查器）是**真实数据、真实交互**，且没有 innerHTML 类 XSS。
- 但它不是产品：无路由、无深链、9 张卡片平铺、S1/S5 缺失、S4/S6 单薄、租户泄漏、且不随包发布。
- **技术选型建议：继续 vanilla JS，不引框架/构建链**。理由：单页运维台、无 SSR 需求、团队偏好零依赖；1,260 行 JS 可控。但必须做三件事：拆 ES module、加 hash 路由、加"端点用量 vs OpenAPI"静态判据。
- 若未来要做多页/富交互（如 schema 驱动表单 + 大表格），再评估引入轻量框架；不在本轮。

### 4.2 目标 IA（按用户任务，不按子系统）

| 面 | 解决的问题 | 主区 | 关键交互 | 优先级 |
|---|---|---|---|---|
| **Runs（默认首页）** | "我现在有什么在跑/跑成什么样" | 运行列表（状态/工作流/principal/耗时/成本/质量分） | 过滤、新建运行（A3 schema 表单）、深链 `/ #/runs/{id}` | P0 |
| **Run 详情** | "这一次到底发生了什么" | 现有 S2 DAG + S3 抽屉 + 审批条 + 产物区 + 成本/trace | 节点点击、放行/驳回、下载产物、看锁指纹差异 | P0（复用现有） |
| **Approvals 收件箱** | "等我批的有哪些" | 跨运行待办（提交人/等待时长/SLA/超时策略） | 批量放行/驳回、留言、跳转运行 | P0（A1 的 UI） |
| **Deliveries** | "交付了什么、能不能追溯" | 交付物列表（sha256/字节/来源节点/保留期） | 下载、按工作流/时间过滤、查看来源运行 | P1（A2） |
| **Workflows & Schedules** | "有哪些工作流、锁有没有漂、什么时候跑" | 工作流卡片（lock 徽标/输入 schema/来源 pack）+ 排程（last/next fire） | 立即运行、看漂移差异、启停排程、DLQ 重放 | P1 |
| **Settings** | "谁能用、花多少、接了谁" | tokens/预算/集成（webhook/MCP/队列）/插件 | 增删 token（只显指纹）、设预算、看插件来源与沙箱判定 | P2 |

视觉基调沿用深色等宽运维风（已确认）；补三件事：统一中文文案（去掉 "Doc-Pipeline Dashboard"、`timeAgo` 英文）、键盘可达（行/节点可聚焦）、每面板独立 loading/error（不要 `allSettled` 把失败折叠成空）。

### 4.3 控制台的正确性判据（不靠人眼）

1. **端点契约定**：解析 `dashboard/app.js` 里的请求字符串，断言每个都被 `openapi_spec.generate_spec()` 收录（Python 测试，零浏览器）。
2. **打包判据**：构建 wheel 后断言 dashboard 资源在包内且 `GET /` 可取得（沿用 `test_installable_product.py` 的真 venv 口径）。
3. **租户判据**：双 token 真 HTTP，断言 dashboard/agents/logs 互不可见。
4. 可选：Playwright 冒烟（`[ui]` extra，CI 手动 dispatch），产出截图；在此之前**不得**再写"无头 Chrome 走查留证"。

---

## 5. 架构提案：把"运行"变成一等对象

### 5.1 核心问题：四个状态源

现状：内存 `_running_tasks`、`task_queue`、checkpoint JSON、`deliveries.db` 各存一部分运行事实，`RunHandle` 在四者之间拼装。后果：跨进程列表不可见、审批不持久、cost 作用域随内存淘汰丢失、UI/SSE/审计各读一份。

**目标：单一 RunStore + 追加式 EventLog 作为运行事实源。**

```
                     ┌────────────── EventLog (append-only, seq) ──────────────┐
                     │  run.created / node.started / node.finished /           │
                     │  approval.requested|decided / artifact.recorded /       │
                     │  run.finished                                          │
                     └──────────────┬─────────────────────────────────────────┘
                                    │ 派生（同源）
        RunStore (runs / run_nodes / approvals / artifacts)  ──► UI / SSE / 审计 / 账本 / 成本归因
                                    ▲
      Orchestrator / Worker / Triggers / Admin / MCP ──► RunHandle（唯一读原语）
```

要点：
- `RunStore` 是接口（SQLite + Postgres 两实现）；内存只是缓存，不是事实源。
- 审批 = `approvals` 行 + 队列里一条"待恢复"信号；`approve()` 不再依赖进程内存。
- 运行列表、成本作用域、artifact 查询全部走 RunStore（principal 必带）。
- `Deliveries` 成为 `artifacts` 的一个视图（保留现有表做兼容读，逐步迁移）。
- 事件流同时服务 SSE 重放与审计，不再是三套独立写入。

### 5.2 数据模型（最小集）

| 表 | 关键列 | 说明 |
|---|---|---|
| `schema_version` | version, applied_at | 取代 `add_column_if_missing` 的临时迁移 |
| `runs` | run_id PK, workflow, principal, status, inputs_json, trigger, trace_id, started_at, finished_at, error, quality_score, cost_usd | 列表页与作用域的唯一来源 |
| `run_nodes` | run_id, node, status, attempts, started_at, finished_at, error, result_json, artifact_keys_json | S2/S3 直读 |
| `artifacts` | artifact_id, run_id, node, kind, path, bytes, sha256, retention_until, created_at | 交付物浏览器 |
| `approvals` | approval_id, run_id, node, status, requested_at, expires_at, decided_at, decided_by, note | 跨重启/跨进程 |
| `events` | run_id, seq, type, payload_json, ts | SSE/审计同源 |
| `schedules` | name, cron, next_fire, last_fire, lease_owner, lease_until | 触发器租约 |
| `cost_entries` | run_id, principal, workflow, agent, provider, tokens_in, tokens_out, usd, ts | 按 principal/workflow 归因 |

### 5.3 执行与接口的收敛

- **一条引擎**：`run()` 只做"YAML → plan → run_plan"的适配；删除并行维护的第二条循环，双路径 parity 测试改为一次性迁移判据。
- **一个结果契约**：Agent 统一返回 `dict`（现状事实），`AgentResult` 删除或标废弃；`PRODUCES` 校验按 dict 键。
- **一个输入契约**：`pipeline.inputs` JSON Schema → CLI/HTTP/MCP/UI 共用校验器；`POST /api/workflows/{name}/runs` 为唯一提交语义。
- **一个句柄**：`RunHandle` 读 RunStore；`status/wait/cancel/resume/artifacts/approve` 同源。
- **一个配置入口**：`loom.yaml`（state/auth/budget/retention/limits）+ env 覆写；`--config` 与 `ConfigCenter` 合并。

### 5.4 Pack 模型（让"通用内核"名副其实）

`pack.yaml` 增加：`inputs` schema、`delivery` 声明、`requires`（宿主版本/依赖）、`quality_profiles`。按域重整：
- `packs/documents`（docgen 6 条 + renderer）
- `packs/reports`（api-report / api-digest / data-qc / intel-brief）
- `packs/ops`（alert-runbook / k8s-patrol / invoice-extract / minutes-weekly / spec-cases）
- 核心只留 `hello`/`echo` 两条示例流水线。

判据：删掉全部 `packs/` 后引擎与示例仍可运行；每个 pack 有 inputs schema、一条真 E2E、一条"缺依赖时如实报错"的判据。

### 5.5 运维形态

| 形态 | 命令 | 说明 |
|---|---|---|
| 单进程 | `loom serve` | admin + worker + triggers；内部租约保证同库只有一个触发器/维护者 |
| 拆分 | `loom admin` / `loom worker` / `loom triggers` | 多机部署；共享 RunStore（PG） |
| 容器 | 镜像名 `nexusloom` | healthcheck `/health`；readiness `/ready`（检查 RunStore 与 pack 装载） |
| 维护 | `loom maintenance`（或内置循环） | 保留期、checkpoint GC、账本归档 |

配置分三层：`loom.yaml`（文件）→ `LOOM_*`（env）→ 覆写文件（热更新，带 revision CAS）。

---

## 6. 落地顺序（建议）

### P0（1–2 周）：先让产品自洽
1. 定位收口：README 首屏/PyPI 摘要/MCP 工具面/默认配置（§2.3）。
2. 控制台进包 + `GET /` 指向控制台 + 租户作用域（B1/B2）。
3. 审批持久化 + Approvals 收件箱（A1）——这是差异化第一次变成可感知产品。
4. 清残留旧名 + 陈旧 release-plugins + 文过其实的声称（B9/B10）。
5. 判据：真 venv 装完 `/` 可开；双 token 互不可见；杀进程重启后可审批；`loom demo` 离线可跑。

### P1（3–4 周）：把"交付"做成一等对象
6. RunStore + EventLog 迁移（先 approvals/artifacts/list_runs，再 events；双写期 parity 测试）。
7. 类型化输入（A3）+ `POST /api/workflows/{name}/runs`。—— **2026-10-26 本项按"闸门 + 四条口"
   收口**（续65）：`loom_core/input_schema.py` 是声明式子集（11 个 keyword，不引 `jsonschema`），
   `pipeline.inputs` 在**解析期**验声明、在**提交期**验载荷；CLI（`--inputs-json` /
   `--set-input`）、HTTP（`POST /api/workflows/{name}/runs`、`/api/tasks` 的 `inputs`）、MCP
   （`run_workflow`）、webhook 四条口共用这一处实现，40 条判据逐口证明"确实到达闸门"并带
   反向对照。第三小句"UI 依 schema 生成表单"只交付了**前提**：`GET /api/workflows/{name}/inputs`
   与 MCP `get_pipeline_info.inputs` 让声明读得到，控制台里的表单本身还没做（与控制台呈现
   一起排，跟 #8 剩下那两半同一批）。
8. Deliveries 浏览器（A2）+ 锁指纹差异呈现（A5）。—— **2026-10-26 本项按"读端点"收口**：
   `GET /api/deliveries`、`GET /api/deliveries/<run_id>`、`GET /api/locks` 三个只读口 + 22 条
   真 HTTP 判据入库（续64）。剩下两半都要先拍板再动：A2 的 sha256/保留期要跟 A6（GC）
   一起做，A5 的"逐运行指纹"要在交付载荷里加一个键（写侧改动，且只对今后的运行有效）。
9. 排程租约 + DLQ 重放 + 保留期 GC（A6/B4）。
10. Python SDK（A7）。

### P2（4–6 周）：生态与规模
11. pack 重整 + `pack new` 模板 + 2–3 个真实第三方插件。
12. PG 覆盖 RunStore/artifacts/events；多实例 admin。
13. 浏览器冒烟（可选 extra）与英文文档；示例库（每个 pack 一份输入/输出样例）。

---

## 7. 风险与不做清单

**风险**
- RunStore 迁移是本文最大改动：必须"先读写双轨 + parity 判据 + 可回退开关"，禁止一次性切换。
- 控制台继续 vanilla JS 的前提是**只做运维面**；一旦要做 schema 驱动表单与复杂表格，先做技术选型再动工。
- PG 双后端会放大测试负担：RunStore 一致性测试必须在 SQLite 与真 PG 各跑一遍。
- 定位收敛会放弃一部分"通用运行时"的想象空间；这是有意的取舍——宽内核、窄门面。

**不做**
- 不做拖拽编辑器（拼不过 n8n 的集成数量）。
- 不做通用作业调度器（Airflow 地盘）。
- 不做身份/RBAC/SSO 治理面（MAOP/MAOS 地盘）。
- 不把"组合数"当对外卖点。
- 不在没有浏览器证据时声称做过浏览器走查。
