# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.9.2]

Loom 首个 PyPI 发行版（发行名 `nexusloom`；原名 doc-pipeline，2026-10-11 更名）。
单发行物：引擎 + artesian 底座 + 全部 Agent（含五支实用插件）随包自带，
`pip install nexusloom` 一次装齐。

### Added（2026-10-10/11 累计）

- **单发行物整合**：artesian 并回主仓顶层包（307 条底座判据随库走）；五支实用
  插件（keyword_extract / readability / link_check / fact_coverage / tldr）
  内置化进 agents/；examples/plugin-hello 留作第三方作者模板。
- **交付形态补缺**：结构化 JSON 落库（deliveries.db 账本）+ HTML 站点 +
  renderer html 后端——交付形态 5 → 7。
- **发布门禁** `scripts/verify_dist.py`：wheel/sdist 数据文件、依赖闭合、
  版本一致、干净 venv 安装态真跑。

### Fixed

- SQLite 并发首建竞态（WAL 切换重试 + 幂等加列，task_queue/cost/quality/
  message 四处 store）——main 侧 CI 偶发 `database is locked` 的根因。
- quality_gate/writer 资源目录相对包路径（安装态指向空壳）——改走统一解析。
- data-files 子目录 glob 不下钻（quality/prompts 静默丢失）——子目录单列。

### Changed

- 命名清理第一步：banner/CLI 文案去领域味；发行更名 nexusloom。
- spec 诚实口径：扩展来源轴回归 2（自家内置件不再充数第三方）。

## [3.15.0] - 2026-10-27

### Added（2026-10-27，A5：交付指纹）

- **交付记录新增 `topology_hash` + `config_hashes`**：`_build_delivery_record` 复用
  `Scheduler._topology_hash(plan)` 与 `Scheduler._node_config_hash(ac)` 静态方法计算
  拓扑指纹与逐 agent 配置指纹——与 lockfile 生成/校验同一实现，避免两处各算一遍漂移。
- **`loom plan --diff` CLI flag**：比对当前流水线与锁文件，报告漂移项（无锁/一致/漂移三态）。
  `--diff` 绕过锁校验但不写锁文件（`skip_lock_verify` 区分 `write_lock` 与 `diff`）。

### Added（2026-10-27，A2：交付产物哈希与清理）

- **交付记录新增 `output_sha256`**：交付产物文件内容哈希（64 位 hex），无产物时空串——
  区分「无输出」与「输出但文件丢失」。
- **`DeliveryLedger.cleanup()`**：双判据（`retention_days` + `max_runs`），与 `run_store.cleanup`
  同一口径，两个判据可同时生效。接入 `gc_runs()` 的 `summary["deliveries"]`。

### Tests（+21 条，3106→3118）

- `test_delivery_forms.py`: 指纹格式验证 / 跨账本对齐 / 失败运行也带指纹 / 直接比对 Scheduler
  静态方法 / 跨运行稳定性（+3）
- `test_cli_verbs.py`: `--diff` flag 注册 / 无锁文件 / 锁一致 / 漂移检测（+4）
- `test_maintenance.py`: cleanup 年龄判删 / 最大保留 / 双判据 / 禁用 / gc_runs 接线 / 失败隔离（+6）

## [Unreleased]

### Tests（2026-11-02，节点检查器投影态：前端换文案这件事终于有判据）

#87 把"跨进程运行从 RunStore 投影答"修好了，后端契约有判据
（`tests/test_node_inspector.py` 断言 `source == "runstore"` 且四类明细显式 `None`），
前端 `app.js` 也按 `d.source === 'runstore'` 换了文案——但**"前端据此换文案"这句话此前
没有任何证据**：`tests/test_dashboard_s2.py` 只按字符串 token 扫 app.js 源码（代理指标，
两句文案同时印出来也能绿），`tests/check_dashboard_browser.py` 里 `inspector|runstore|投影`
零命中。而那个真 Chrome 门是手动的、不进 pytest 收集，用例被删 CI 也不会红。

- **手动门补真渲染用例**（`tests/check_dashboard_browser.py`，真 Chrome 实跑 10 条 PASS，
  其中 2 条新增）：投影态节点必须出现"跨进程运行…不落投影"三处换过的文案、不得出现
  "该节点没有产出 / 熔断器未建立 / 限流器未建立 / 查不到（store 未就绪）"、质量门与断点块
  整块不渲染、`max_retries=None` 显示 `1 / ?`；同一门的**反向对照**是内存态节点，那几行
  必须还是原文案——只判一边的话，把两套字都印出来也能过。
- **门覆盖度进 CI**（新增 `tests/test_browser_gate.py`，7 条）：把门里每条 `print("PASS: …")`
  当成一条已交付用例，断言 ①主题一条不缺 ②每条用例的 PASS 之前最后一句可执行语句必须是
  断言（判"两个 PASS 之间有 expect"太松——前置 setup 行会替被删的断言兜底）③投影态正反两面
  都在。**它挡的是"用例被删/断言被删"，不冒充"渲染已被 CI 验证"**：浏览器门仍然只在本机跑。
- **mock 与后端同形**：门里 mock 的投影响应必须与 `_node_inspector_from_projection` 的真产出
  逐字段一致——mock 一漂移，门就在验一个不存在的契约，而那种漂移在手动门里永远看不出来。
- 窄变异 5 条全 RED（前端判定写死 false / 写死 true / 删掉用例最后一条断言 / 整块删用例 /
  后端把 `max_retries` 改成 0），每条注入后字节级还原并核 sha256。

### Fixed（2026-10-31，控制台审计与节点检查器；PR #87，代记）

> 这条由 qoder 代 zcode 记：#87 改的是 `admin_api.py` / `dashboard` / `openapi_spec`，
> 没动 CHANGELOG，而发版说明是 `release.yml` 从本文件 awk 抽的——不补就等于这一批
> 用户能撞到的修复在 Release notes 里不存在。**功能与判据归 #87 的作者**，这里只补索引。

- **跨进程运行的节点检查器不再必 404**：内存里没有那次运行（被淘汰 / 别的进程跑的）时，
  `/api/tasks/<id>/nodes/<node>` 改从 RunStore 投影给可读事实，并显式标 `source: "runstore"`；
  投影里从不落的四类明细（artifact 正文预览 / 幂等键 / 熔断限流 / 质量门）按"取不到就不编"给
  `None`，前端据此换文案，而不是把 `None` 读成"没有内容"。越权与不存在仍同形（404）。
- **交付账本明细被轮询覆盖**：账本详情页会被 5 秒轮询刷回列表 ⇒ 返回按钮 pin 到同一渲染口径。
- 控制台端点契约与键盘可达性判据 +11 条（`tests/test_dashboard_contract.py`、
  `tests/test_node_inspector.py`、`tests/test_delivery_traceability.py`、`tests/test_dashboard_s2.py`）。

### Fixed（2026-10-31，`maintenance.gc_seconds` 写成空值不再等于关掉回收）

YAML 里 `gc_seconds:` 只写键不给值时读回来是 `None`，旧写法 `int(cfg.get(key, 600) or 0)`
把它折成 **0 = 服务模式不跑 GC**。README 与 `docs/deployment.md` 的口径都是"默认 600、
`0` 是显式选择"——空值不是显式选择，这个折叠恰好把 #79 刚接上的那口气又静默关掉了。
现在空值落回默认 600，`0` 仍然是显式关闭。

### Tests（2026-10-31，维护面接线层 + SDK 契约面，+51 条）

两处"门在、但那一层从来没被执行过"：

- `tests/test_maintenance_wiring.py`（28 条）：`gc_from_orchestrator` 在三处用例里**一律被
  monkeypatch 成替身**，函数体本身零执行；`_dlq_store` / `_run_cleanup` 零引用。补的是文档里
  写明却没有判据的三条降级契约——配置写坏回退到"不启用"、RunStore 取不到只跳过并告警（其余面
  照常回收）、store 缺清理方法/清理抛异常都记 `failed` 而不上抛。
- `tests/test_client_contract.py`（23 条）：`docs/sdk.md` 列了 16 个方法，其中 8 个的
  method/path/query/body 形状从没被断言过；`{"raw": ...}` 非 JSON 退路、空响应体读成 `{}`、
  连不上抛 `URLError` 不包装、`token` 优先于 `api_key` 同样没有判据。用 stdlib 桩服务器逐条
  对照 `client.routes()` 登记的那一帧长什么样。

变异电池：C-8 九条、C-9 十四条，**全 RED（有射程）**，每条注入后字节级还原并核 sha；
`client.py` 覆盖 83% → 98%、`content_digest.py` 89% → 100%。

### Fixed（2026-10-31，类型门漏洞：run.py 从没进过 mypy）

`mypy run.py` 在 main 上实测两条 `no-any-return`（`_result_for` 返回 dict、`_resolve_cli_input`
返回 `args_args.input`），而 CI 的 mypy 步骤只查包目录——CLI 入口一直是类型门的法外之地，
尽管覆盖率口径 `[tool.coverage.run]` 早就算着它。两处收窄后类型干净；`_resolve_cli_input`
把 argparse 的 `None` 归一成空串（引擎约定的"无输入"形状）并如实标注。mypy 命令补上 `run.py`，
新增一条判据按 **token** 核这条命令行——YAML 块标量里的注释也是字符串，第一版拿整段 `run:`
做子串匹配，被自己写的解释性注释骗成了绿。

### Tests（2026-10-31，账本面板补渲染面判据）

`tests/check_dashboard_browser.py`（真 Chrome，手动门）补交付账本用例：mock `/api/deliveries`
与详情端点，断言列表行、明细路径、产物摘要进 DOM，再点「返回账本列表」。此前 M10b 变异
（`state.deliveries` 被覆写成空态）0 failed；现在同形态变异实测红在 `badge-deliveries` 0≠1。

### Docs（2026-10-31，设计锚点重钉）

`scripts/check_design_anchors.py` 两条 `deliveries.py` 锚点随 #81 的"清理只碰终态"漂移
（`def record` 49→50、`def list` 89→90），重钉后 52/52。该脚本不进 CI，并 main 后要手跑。

并 #87 进本分支后同一轮再重钉 8 条 `admin_api.py` 锚点（`_task_checkpoint_view` 1389→1436、
两处 `list_runs` 1785→1835 / 2278→2325、`run_plan_async` 2642→2689、`existing_callback`
2711→2758、`_handle_deliveries` 1894→1941、`_handle_delivery_detail` 1912→1959、
`_handle_locks` 1972→2019）——这 8 条漂移是 #87 自己往上加代码推出来的，不是 main 的账；
重钉后实跑 `checked=52 mismatched=0`。

### Docs（2026-10-29，#10：SDK 从"存在"变成"可发现"）

`LoomClient`（产品评审 A7）随 v3.15.0 起就在包里、也与 OpenAPI 同源，但**用户文档里零命中**：
`docs/` 没有任何 SDK 页，README 只在计数段落里提过它一次 —— 集成方只能读源码才知道有这个
东西。这跟此前 `retention.*` 在用户文档零命中是同一类缺陷（功能在、但等于没交付）。

- 新增 `docs/sdk.md`：安装（`pip install nexusloom`，零第三方依赖）、快速开始、认证三态与
  **信任模型原文**（为什么刻意不做 base_url 白名单）、16 个方法的完整表（方法 / HTTP 动词 /
  路由 / 用途）、错误模型（`LoomError` / `LoomTimeoutError` 的 `status=0` / 本地 `ValueError`
  / `URLError` 不包装）、以及五条**语义边界**（`paused` 不是终态、`approve` 只在等待中受理、
  `deliveries` 没有翻页、`locks()` 回答的是"现在"、产物下载没有）。末尾单列「还没做」，
  避免把"没有"读成"应该有"。
- README 增加「Python SDK」小节与文档表入口；英文版说明里注明该页**暂无英译**，不让读者以为漏了。
- 修一处随版本前进而变假的注释：`client.locks()` 原写"运行时的 config_hash/topology_hash
  不落库，后者今天无法回答"——v3.15.0 起它们随交付记录落库、#79 又从明细读面暴露，改为指向
  `delivery(run_id)`。
- 判据 +2 条（`tests/test_doc_consistency.py`）：① 文档方法表 ↔ `LoomClient._ROUTES` **双向**
  相等且 method/path 逐条一致（先断言表里至少解析出 10 行，挡住"解析器什么都不认"这种假绿）；
  ② 可发现性——README 必须链到 `docs/sdk.md`，且该文档里要有可复制的导入示例与安装命令。
  加上原有的 `_ROUTES` ↔ OpenAPI 门禁，**文档 / 实现 / spec 三处现在只改一边就会红**。

## [3.16.0] - 2026-10-29

把「功能写好了但跑不到 / 跑了看不见」这一类收口：A6 保留期回收在有活的 worker、
常驻服务和死信表上都真跑起来了；交付账本从整篇 `output_sha256` 补到**逐节点逐产物**
的摘要，并在控制台第一屏可见。计数护栏的容差也从写死的常量换成可解释的派生判据。

### Added（2026-10-28·续69，A6 的活性 + 交付追溯面 + 控制台账本）

删的是"功能写好了但根本不会被跑到 / 跑了也看不见"这一类。三块：

**1. GC 的活性（A6 此前只在一种运行形态下成立）**

- **worker 有活也回收**：`_maybe_gc()` 原先挂在 `if item is None:` 的空转分支里，
  于是**持续有活的 worker 永远不回收**——而那正是 A6 要治的场景（长跑且饱和的
  worker 无限膨胀）。现在它在分支外，按 `maintenance.gc_ticks` 计。
- **常驻服务接回收**：`--admin` / `--dashboard` / `--daemon` 这条路径上根本没有
  worker 循环，保留期配置此前等于摆设。新增 `maintenance.gc_seconds`（默认 600、
  `0` = 服务模式显式不跑），`_run_daemon` 按秒计节拍；节拍判据抽成
  `_daemon_gc_due()` 以便直接驱动（不用靠 sleep 观察）。
- **死信表进回收**：`PersistentStore.prune_dlq(max_age_days, max_rows)` —— `dlq`
  此前**只进不出**（`move_to_dlq` 插入、`list_dlq` 读，没有任何删除面），是 A6
  漏掉的最后一张无界表。两个参数都 <= 0 时返回 0：这张表从来没有过清理面，
  不沿用 `task_queue` 那种"age=0 就是删光"的历史语义。

**2. 逐节点产物摘要（A2 追溯面的内容级补充）**

- 子集里只有整篇交付物的 `output_sha256`（v3.15.0）与产物**键名**，"各节点分别
  产出了什么内容、是不是当时那份"仍无从核对。新增 `loom_core/content_digest.py`
  （`digest_of` / `artifact_digests`）：文本按 UTF-8 原文、结构按 `sort_keys` 规范
  JSON（不排序则同一 dict 换键序换一个 sha，追溯当场失效）、编不动的对象退 `repr`
  且**绝不外抛**。摘要只在写入时算一次——产物值活到进程结束，事后没有第二处能算出
  同一个值。账本记录里落成 `nodes[].artifacts.<键> = {sha256, bytes, kind}`。

**3. 读面与控制台**

- `GET /api/deliveries` 逐行带 `output_sha256`；`GET /api/deliveries/<run_id>` 追加
  `topology_hash` / `config_hashes` / 逐节点 `artifacts`。
- 控制台新增「交付账本」卡（`#panel-deliveries`）：列表给运行/流水线/状态/完成/
  摘要短形/节点·产物数；点行进明细——交付物路径 + 完整 sha256 + 锁指纹（逐节点
  config_hash）+ 逐节点产物摘要行（复用运行详情检查器的三列版式，不另发明一套）。
- 文档：README 新增「保留期与回收」（四个配置键、`--gc` 输出样例、三条边界），
  `docs/deployment.md` §6.1 给运维口径，`docs/api.md` 补追溯字段的三种形状。
  此前 `retention.*` 与 `maintenance.gc_ticks` 在用户文档里**零命中**，功能等于不可发现。

### Fixed（2026-10-28·续69，GC 汇总日志的错账）

`gc_runs` 的删除行数原先是 `sum(int(v) for v in summary.values() if isinstance(v, int))`，
两头都错：`enabled: True` 是 int 的子类，被算成"删了 1 行"；而 `run_store` 返回的逐表
dict（真删了几十行）因为不是 int 被整个跳过。本机实测复现：**删 18 行、日志写"删除 1 行"**。
新增 `count_dropped()`（跳过 `enabled` 与 bool、dict 逐表加总）与 `format_summary()`
（逐表 + 合计），`loom --gc` 不再甩原始 dict。

### Changed（2026-10-28·续69，计数护栏的容差改为派生）

`test_readme_test_count_matches_collection` 原先写死"`collected − declared ≤ 10`"。
这个 10 是按当年的环境门 skip 数手调的；`requires_pg` 那批涨到 21 条之后，护栏
开始把一个**正确**的 README 数字判成过期（本批现场：`3179 passed` 被要求和 3200
相差 ≤10，实际差 21）。改成 `budget = README 自报的 skipped + 平台差(8)`：两个数
同源，环境门增减都会被 README 那一行带着走，而"忘了改计数"仍然会红——窄变异（把
声明数改小 1 → Windows 侧差值 25 > 24）实测转红。这不是为了凑绿放宽门，是把一个写错的
常量换成可解释的判据。

**rebase 到含 #78 的 main 之后重测（2026-10-29）**：绝对数全部前移——收集 3200 → **3209**、
skipped 21 → **24**（#78 给 `test_state_backend` 加了 3 条真 PG 门）、passed → **3185**，
Linux 侧按同差额预期 3217（预算 24 + 8 = 32 恰好等于 3217 − 3185，"恰好"是有意的）。
同一轮里唯一的红就是这条护栏自身，改完 README 后 `tests/test_doc_consistency.py` 9 passed。
另：`scripts/check_design_anchors.py` 里 17 处锚点被 #78 的行号推移打偏（pipeline/run_store/
dag_executor/db_backend/deliveries 五个文件，偏移 6~34 行非常数），按整行文本就近重钉后
`checked=52 mismatched=0` 复验——这条脚本不进 CI，偏了不会有人红，只能靠每次并 main 手跑。

### 判据与变异（36 条新判据：`tests/test_gc_liveness.py` 18 + `tests/test_delivery_traceability.py` 18）

窄变异电池（每条打在判定维度上，逐条落盘日志、还原走字节级写回并核 `restored=True`）：

| 变异 | 红在哪 |
|------|--------|
| M1 GC 退回空转分支 | 1 failed（有活时不回收） |
| M2 `count_dropped` 的 dict 分支不计行 | 2 failed |
| M3 GC 不扫死信表 | 1 failed |
| M4 死信清理不删任何行 | 1 failed |
| M5 常驻服务忽略节拍 | 1 failed |
| M6 逐节点产物摘要不写 | 1 failed |
| M7 列表不暴露 `output_sha256` | 1 failed |
| M8 明细不带锁指纹 | 1 failed |
| M9 控制台读响应里不存在的字段 | 1 failed（跨端契约） |
| M10 `render()` 里不再渲染账本面板 | 1 failed |

**招供**：M10 第一次跑是**绿的**——原判据是子串匹配 `"renderDeliveries(state.deliveries)"`，
而明细页返回按钮那行 `back.onclick = () => renderDeliveries(...)` 也含这段子串，把"渲染
被注释掉"骗成了通过。改成逐行比对 `render()` 函数体之后才红；这类"同一符号的两处调用
互相顶罪"是子串型判据的通病。另：控制台判据是**结构与跨端字段契约**级，仓库仍无浏览器级
证据（设计复核 §4 那条缺口没变）。

**合并 #78 后重跑（2026-10-29，最终头 `61f567a` 的树）**：#78 把 `loom_core/deliveries.py`
改了 150 行、`pipeline.py` 改了 26 行，摘要与指纹的落点都在那一片，所以合并前的电池结论
不能直接搬过来用。基线（未注入）`tests/test_delivery_traceability.py` **18 passed / 90.23s**
（本机同负载态），逐条注入：

| 变异 | 合并后实测 |
|------|-----------|
| M6 `"artifacts": artifact_digests(...)` → `{}` | 15 passed / **3 failed** |
| M7 列表 `output_sha256` 恒空串 | 17 passed / **1 failed** |
| M8 明细不带 `topology_hash` + `config_hashes` | 17 passed / **1 failed** |
| M9 前端改读响应里不存在的键名 | 16 passed / **2 failed** |
| M10a `render()` 里删掉渲染调用 | 17 passed / **1 failed** |
| M10b `renderDeliveries()` 开头把数据覆写成空态 | 18 passed / **0 failed ← 零射程** |

**第二条招供（新的、还没补）**：M10b 是绿的。也就是说现有判据只证明"`render()` 会调用
账本渲染函数"与"前端读的键在真响应里存在"，**不证明真数据被画出来**——把传入数据整包换成
空态，Python 侧的静态/契约判据一条都不会红。要抓到它只有两条路：jsdom 级执行渲染函数、
或浏览器级点验；本仓两者都没有（本机也没装 playwright 的浏览器内核）。这条缺口按"待补"
记账，不写成"已覆盖"。


## [3.14.0] - 2026-10-27

### Added（2026-10-27，A7：LoomClient Python SDK）

- **`LoomClient` 正式成为包导出符号**：`pip install nexusloom` 后 `from nexusloom import LoomClient`
  即可用——Admin API 的 Python 客户端，覆盖 submit / cancel / wait / approve / reject /
  run_workflow 全链路。此前 SDK 代码在仓里但没导出，用户得 `from loom_core.client import LoomClient`
  绕一圈。现在 `loom_core/__init__.py` 的 `__all__` 里有三个新符号：`LoomClient` / `LoomError` /
  `LoomTimeoutError`。
- **7 条真 HTTP 集成测试**（`tests/test_client_sdk.py`）：submit-cancel-wait 链 / approve-reject
  收件箱链 / 生命周期 404 作用域 / OpenAPI 路由同源门禁 / submit 参数校验 / run_workflow 400 /
  paused 任务 wait 超时。AdminAPI 绑 127.0.0.1:0 + tokens.yaml admin token，不走 mock。

### Added（2026-10-27，B5：RunStore 双后端真 PG 测试腿）

- **11 条真 Postgres RunStore 测试**（`tests/test_state_backend.py::TestPostgresRunStoreSql`）：
  此前只有 2 条最小 SQL 形状测试，PG 路径从未被真 PG 验证过（设计评审 §288 预警的正是这个）。
  新测试覆盖六表 schema 幂等 / open_run 生命周期 / events after_seq+limit / record_nodes 批量
  upsert / upsert 幂等 / 审批全生命周期 / 排程租约互斥 / seq 取号并发 8 线程×5 / cleanup 级联删 /
  retention_days 判删 / list_runs principal 作用域。
- **`cleanup()` 从 `executemany` 改为逐行 `execute` 循环**：psycopg3 的 `Connection` 没有
  `executemany`（只有 `Cursor` 有），而 `_Conn` 包装的 `execute` 在两边都能用。这是 PG 路径
  第一次被真 PG 测出来的缺陷——SQLite 侧 `executemany` 一直可用，所以本地永远绿。

### Changed（2026-10-27，B8 引擎收敛：run vs run_plan 两套引擎合并为一条）

- **`orch.run()` 委托给 `run_plan()`**：legacy 路径不再自己跑 DAG 循环，而是构建
  `ExecutionPlan` 后委托给声明式引擎。legacy 因此获得 RunStore 投影、交付契约检查、
  审批闸门支持等全部声明式能力。`_run_dag_parallel` / `_emit_task_events` /
  `_finalize_task_queue` 三个方法已移除（无调用者）。
- **配置入口收敛**：`run()` 现在是统一入口——有 `config` 走 legacy 注册表依赖序，
  无 `config` 有 `pipeline_name` 则先尝试 `Scheduler.parse()` 加载 YAML，加载失败
  回落 legacy。两条路径最终都走 `run_plan()`。
- **legacy 路径现在写真实投影**：B8 收敛前，legacy `run()` 从不调 `_runstore_open`/
  `_runstore_nodes`/`_runstore_close`，`projection` 标记为 `legacy`。收敛后，legacy
  经由 `run_plan()` 的统一引擎执行，投影标记从 `legacy` 升为 `ok`。`legacy` 值仍保留
  给 Stage 1 之前落账的历史运行。
- **`pipeline.started` 事件补回 redacted config**：`run_plan()` 的 `pipeline.started`
  载荷新增 `config` 键（经 `_redact_config` 脱敏），保持与旧 `run()` 一致的安全可观测性。

判据：`tests/test_projection_health.py` 的 legacy 测试已更新（期望 `ok` 而非 `legacy`）；
`tests/test_dual_path_parity.py` 2/2 通过；`tests/test_pipeline_redaction.py` 4/4 通过。

## [3.13.0] - 2026-10-26

本版把 2026-10-20 设计复核列出的 P0/P1 工程欠账一次清完，另含两条独立交付。
六个主题：**类型化输入**（#7，四条提交口共用一道闸门）／**交付账本只读 HTTP 面**
（#8）／**cron 触发租约**（B4，多进程只允许一个触发者）／**保留期与 GC**（A6，
死代码清理面变成可调度的统一口径）／**配置覆写 CAS**（B6，多进程写不再丢键）／
**loom demo 离线体验**（A4，P0 唯一欠账清零）。测试 2950 → 3087（+137 条判据，
全部带变异对照或窄变异证明能红）。


### Added（2026-10-26·续65，#7 类型化输入：声明式子集 + 四条提交口共用一道闸门）

- **inputs 第一次有形状**。流水线可以在 YAML 里写 `pipeline.inputs`，四个调用面在提交
  那一刻按同一份声明校验，非法载荷被拒且**错误定位到字段**（`spec.region`、`tags[1]`
  这样的点号/下标路径）。落点只有一处：`loom_core/input_schema.py`。
- **声明式子集，不是完整 JSON Schema**：支持的 keyword 是
  `type`/`required`/`enum`/`pattern`/`minimum`/`maximum`/`minLength`/`maxLength`/`default`/
  `items`/`properties`/`additionalProperties`。不引 `jsonschema`（新增依赖是独立决策，
  不在这一条线里替你决定），`type` 沿用引擎既有词表（`str`/`int`/`float`/`bool`/`list`/
  `dict`）而不是 JSON Schema 的 `string`/`integer`——同一个值在本仓只该有一种叫法。
- **未知 keyword 在解析期红**，不留到提交期：把 `maxLength` 写成 `max_length` 时，忽略
  等于那条约束**从没存在过**，而运行照常提交。同理 `required` 指向没声明的名字、
  `pattern` 是坏正则、边界不是数值，全部在 `Scheduler._build_plan` 里抛
  `SchemaDeclarationError`（错误带 `<pipeline>.pipeline.inputs.properties.<字段>` 全路径）。
- **先 parse 再过闸门**这个顺序是刻意的：`render_inputs_doc` 把 dict 拍成 `## 键` 分节的
  文本，而 `dag_executor._extract_queries` 读文档时把所有 `#` 行丢掉——结构在渲染那一瞬
  就没了，渲染之后再也问不出"哪个字段错了"。所以 MCP 的共享提交尾与 webhook 都改成
  解析→校验→渲染→落盘。
- **自由文本入口不留旁路**（`gate_freeform`）：`POST /api/tasks` 的 `query`、MCP
  `generate_document`、CLI 的位置参数 `input`、webhook 的非 JSON 文本载荷传的是整篇文本，
  没有可校验的键→值结构。流水线一旦声明了 `inputs`，这些入口明确拒绝并指出该用哪个口。
  理由：留一条不经过门的路，声明就是装饰——而**门禁假阳性比没门禁更难查**。
- **新增面**：HTTP `POST /api/workflows/{name}/runs`（类型化提交口，400 带 `fields`）、
  `GET /api/workflows/{name}/inputs`（读回声明，`declared: false` 而不是报错，客户端据此
  生成表单）、`/api/tasks` 可选 `inputs`、CLI `--inputs-json` / `--set-input k=v`
  （值先按 JSON 解析，`depth=3` 落真整数）、MCP `get_pipeline_info` 返回 `inputs` 声明。
  Admin 侧两个提交口抽出共享尾巴 `_launch_from_doc`：wait 语义、SSE callback 摘挂、
  异步输入文件的 reaper、`owner=principal.id` 只在一处。
- **三条边界照实写**：① 声明**不进锁指纹**（`config_hash`/`topology_hash` 都不含它）——
  代价是改 schema 不会被锁拦下，收益是随包 20 条流水线不必因此全部重锁，这条选择由
  `test_declaration_does_not_change_lock_hashes` 钉住；② 没声明的流水线一律不受影响
  （渲染出的文档逐字节相同，四处都有反向对照判据）；③ 恢复（`--recover`）与 worker 拿的是
  **已渲染的 input_file**，不过这道门——它们重放的是当初已过校验的载荷，再校验一次只会
  因为 schema 演进把老任务卡死。

判据：**+78 条**（`tests/test_input_schema.py` 38 条 + `tests/test_typed_input_surfaces.py`
40 条）。后者的分组方式是"每条口都要有'确实到达闸门'的证据"——单测 `input_schema` 全绿
只证明校验实现是对的，不证明 CLI/HTTP/MCP/webhook 真去调它。每条口三条：非法载荷必须拒
**且引擎没被调用**（拒绝要在执行之前）、合法载荷要看到闸门产出的文档（含 `default` 填上）、
没声明的照旧。另有一条真 HTTP 端到端、一条真 CLI 子进程、以及"闸门只有一处实现"的结构
判据（含"不许出现第二套 `validate_inputs`"）。

窄变异电池 26 条，**26 条全部有射程**（G0 反向对照 0 红 / 78 passed，G-end 还原后复跑 0 红）：

| 变异 | 红 | 红的是 |
|---|---|---|
| V1 required 检查摘掉 | 7 | 四条口的 400 判据 + 错误表 + 真 CLI |
| V2 未声明键放行 | 2 | `extra` 字段判据 / HTTP 错误体形状 |
| V3 maxLength 摘掉 | 1 | 参数化表里那一条 |
| V4 类型不符不短路 | 1 | **第二轮才有红**（见下面的招供①） |
| V5 bool 被当 int 收 | 1 | `test_bool_is_not_an_int` |
| V6 enum 摘掉 / V7 嵌套 required 摘掉 / V8 下标路径丢 / V9 载荷非对象放行 | 2/1/1/1 | 各自那条约束的判据 |
| D1 未知 keyword 不报 | 2 | 声明期两条（含"错误要带字段全路径"） |
| D2 required 悬空不报 / D3 default 覆盖显式值 | 1/1 | 声明期判据 / `test_explicit_value_is_never_overwritten` |
| G1 `prepare_inputs` 不校验 | 12 | 四条口的所有"先拒后跑"判据 |
| G2 `gate_freeform` 空转 | 3 | 三条自由文本旁路判据 |
| G3 `declaration()` 的 isinstance 守卫摘掉 | 1 | 替身 plan 那条（见招供②） |
| G4 解析期不载入声明 | 49 | 整个提交面（没有源就无话可校验） |
| M1/W1/H1/H2/C1/C2 各口跳过闸门 | 4/4/5/1/4/1 | 对应那条口的判据 + 结构同源判据 |
| H3 路由不分发 `runs` | 2 | dispatch 判据 + 真 HTTP |
| H4 错误体丢 `code` | 5 | 四条口读同一形状的那几条 |
| H5 新端点不走共享尾巴 | 5 | owner/echo/异步收尾 |
| S1 schema 进锁指纹 | 1 | "不进锁"这条边界判据 |

两处自我伤害要说清，否则这轮结论会被读大：

① **首轮 V4 是零射程**：我把"类型不符就 `return errors`"换成 `pass`，78 条判据一条都没红。
不是判据写错，是**被测形状不存在**——fixture 里没有任何字段同时声明 `type` 和 `enum`，
而短路只在"后面还有会冒出来的约束"时才看得见。补了 `stage: {type: str, enum: [...]}` 与
一条要求同一字段只报一条的判据，再整条电池重跑一轮才有上面这张表（第一轮的数字不作数）。
② **闸门上线时把三条既有用例打红了**：`gate_freeform` 用 `getattr(plan, "inputs_schema")`
判"有没有声明"，而测试替身（MagicMock）返回的是**真值但不是 dict**，于是"没声明"被读成
"声明了但内容为空"，自由文本入口凭空被拒。修成 `declaration()` 只认 dict，并补
`test_duck_typed_plan_is_treated_as_undeclared`——假阳性比没门禁更难查，这条不只是测试卫生。
③ **电池驱动器自己的还原自检骗了我一次**：H4 那条变异的 `new` 是 `old` 的前缀，
"还原后确认文件里已无变异痕迹"的写法（`new not in back`）在**还原成功**的情况下仍然判失败，
整轮电池在第一轮崩溃中断。核对方式是拿 `git show HEAD` 的字节与归一化后的工作树逐文件比
（六个文件 `normalized_equal=True` ⇒ 只剩行尾抖动，没有残留变异），随后改成"逐字节写回原文件"
并断言字节相等。第二轮 G-end 复跑 0 红是这条修正的证据。

三条既有判据因为 #7 合法地改了契约而被同步改掉（不是为了让门禁闭嘴）：
`test_openapi_spec.py::test_task_submit_schema`（spec 原来写死 `required: ["query"]`，
现在 `query`/`inputs` 二选一用 `anyOf`，判据改成"spec 不得反过来禁止真实允许的载荷形状"）、
`test_security_hardening.py::test_all_three_call_sites_wired`（`new_task_id()` 从入口移进
共享尾巴；判据改严而不是改松：入口必须走尾巴、且自己不得再铸第二个 id）、
`test_mcp_server.py::test_render_inputs_doc_shapes`（原来 import 的是 mcp_server 里的转发别名，
别名随闸门上移删掉，改成从真实出处 `input_docs` import）。

**没做的那一半**：A3 的第三小句"UI 依 schema 生成表单"只交付了前提（声明已能从 HTTP 与
MCP 读到），控制台的表单本体没做，和 #8 剩下的"产物 sha256/保留期""逐运行锁指纹""控制台
呈现"排在同一批待拍板。另记一条既有事实：`/api/tasks` 的 `output` 字段**只校验白名单、
不参与落盘**（改动前就是这样）——把它接成真按客户端路径写文件是另一件事，本批没顺手做。

### Added（2026-10-26·续64，#8 交付账本与锁巡检的只读 HTTP 面）

- **看得见才算接口**：续63 给了 `incomplete` 一个出口（`--replay-run`），但那个出口的
  输入源——交付账本——只有 CLI 与进程内读得到。本批补三个只读口：`GET /api/deliveries`
  （`?limit=`，默认 50、夹到 200，逐行带 `projection` 健康位）、
  `GET /api/deliveries/<run_id>`（整条结构化载荷：逐节点 `status`/`duration_sec`/`artifact_keys`）、
  `GET /api/locks`（`?pipeline=<name>` 只看一条）。
- **两条口径写进响应，不给读的人留误读空间**：
  `/api/locks` 的 `scope` 恒为 `"current files"`——它回答"当前 YAML 与当前锁对不对"，
  **不回答**"某次运行时用的锁对不对"。实测：`config_hash`/`topology_hash` 从未落库
  （只在 `verify_lockfile` 那一瞬存在于内存，交付载荷只存 `plan_id` 那个随机 uuid8），
  所以后者今天无法证据化，也不拿今天的值冒充当时。`matches` 是三值：`true` 比过且一致、
  `false` 漂移（`issues` 带两侧哈希）、`null` 没有锁可比或 YAML 解析失败——
  "没发现问题"与"没法判断"不混成一个布尔；缺锁的行不进 `drifted`。
- **账本没有归属列 ⇒ 可见性逐行问投影**：`_delivery_item` 对非 admin 走 `_scoped_run`
  那条口径，**越权与不存在同一个 404**（存在性本身就是信息）；CLI 直跑、投影里查无此运行
  的行 `projection` 报 `unknown` 且只有 admin 看得见。账本读不动给 **503**，不降级成
  200 空列表——把"读不到"报成"没有交付"是最难被发现的那类假绿。
- **锁路径解析只留一处**：原来解析规则埋在 `_verify_lock_after_parse` 里、判据是"抛异常"。
  只读巡检要同一套规则但不抛，于是抽出 `resolve_lock_path`（按流水线名解析，不需要先有
  plan）+ `lock_drift`（返回问题表）。留在原处再复制一份的话，巡检会报出执行时根本不会
  发生的漂移——这条由 U9a/U9b 两条变异分别打红。
- **`GET /api/locks` 全量巡检加了按文件时间戳的复用**（详见下面"实测"）：绑
  `(yaml, lock)` 的 `(mtime_ns, size)`，不是 TTL；`?pipeline=` 显式单条**恒不读缓存**，
  因为那一问的语义是"我刚改了，现在到底对不对"。

判据：新增 `tests/test_deliveries_api.py` 22 条，全部走**真 HTTP**（handler 写了但路由没接、
路由接了但读内存 task、行取对了但越权漏出别人的运行——这三种失败只有真请求分得开）。
其中两条是口径判据而不是行为判据：`scope == "current files"` 与"锁解析同源"。

窄变异两轮共 26 条，22 条判据**全部被至少一条打死**（零射程判据 0 条）。第一轮 15 条：

| 变异 | 红 | 红的是 |
|---|---|---|
| A1 可见性过滤摘掉 | 3 | 越权 404 同形 / 非 admin 只见自己 / 孤儿行对具名 token 不可见 |
| A2 列表 503 吞成空列表 | 1 | `test_list_unreadable_ledger_is_503_not_an_empty_list` |
| A3 明细 503 换成 404 | 1 | `test_detail_unreadable_ledger_is_503` |
| A4 健康位写死成 ok | 4 | 列表 / 明细 / incomplete / 孤儿行 |
| A5 明细不给逐节点载荷 | 1 | `test_detail_carries_the_node_rows` |
| A6 `limit` 不夹上限 | 1 | `test_limit_defaults_and_is_clamped` |
| A7 缺锁也算 `matches: true` | 1 | `test_absent_lock_is_never_reported_as_matching` |
| A8 `scope` 字段被删 | 1 | `test_scope_is_current_files_and_covers_every_core_pipeline` |
| A9 缓存不看文件时间戳 | 1 | `test_repeat_poll_is_served_from_the_file_stamps` |
| A10 显式单条也读缓存 | 1 | `test_explicit_single_ask_never_reads_the_cache`（首轮红 0，见下） |
| A11 `cached` 谎报成全命中 | 2 | 上面两条缓存判据 |
| A12 结论算完不写缓存 | 1 | `test_repeat_poll_is_served_from_the_file_stamps` |
| S1 锁解析不再看 YAML 同目录 | 1 | `test_resolve_prefers_the_directory_beside_the_yaml` |
| W1 摘掉 `/api/deliveries` 分发 | 6 | 列表系全部 + `test_scope…`（粗变异，只作接线对照） |
| G0 反向对照：只改日志文案 | **0** | ——证明电池本身不制造红 |

第二轮 11 条专打"第一轮里没有任何变异走到的 9 条判据"：

| 变异 | 红 | 红的是 |
|---|---|---|
| U1 非法 run_id 不挡在解析前 | 1 | `test_bad_run_id_is_400_not_a_query_oddity` |
| U2 巡检把 `issues` 丢掉 | 2 | 漂移给差在哪 / 显式单条给差在哪 |
| U3 一条坏 YAML 把全口拖成 500 | 1 | `test_one_broken_yaml_does_not_take_down_the_endpoint` |
| U4 `?pipeline=` 过滤失效 | 1 | `test_pipeline_filter_narrows_and_unknown_name_404s` |
| U5 问不存在的流水线给 200 空表 | 1 | 同上（另一格） |
| U6 规范里 `run_id` 参数改名 | 1 | `test_new_verbs_are_listed_in_the_openapi_get_surface` |
| U7 摘掉 `/api/locks` 分发 | 8 | 巡检全类 + **规范↔handler parity 判据** |
| U8 摘掉明细分发 | 4 | 明细三条 + parity 判据 |
| U9a `lock_drift` 恒报一条问题 | 4 | 本批 parity 判据 + `test_lockfile.py` 既有三条 |
| U9b 执行侧不再拦漂移 | 7 | 本批两条 + `test_lockfile.py` 的 parse/CLI 五条既有判据 |
| G0 反向对照：只改注释 | **0** | —— |

三条过程上的实情，记下来比修掉更要紧：

- **那条"判据不稳定"的红是真信号，产品有问题**：`/api/locks` 不带过滤时要解析全部 19 条
  流水线，本机实测（Python 3.14.3、单进程、无并发负载）**冷 7.27s / 再跑 11.23s**。
  我那个 10s 客户端超时先撞上，症状看起来像 flaky。监控是轮询的，每次轮询付这个钱既不
  合理也必然超时——所以改的是端点不是超时。加按时间戳的复用后同条件重测：
  **冷 7.75s、热 0.08s / 0.09s、`?pipeline=` 0.47s**。两条新判据断言的是 `cached` **计数**
  而不是墙钟耗时（拿耗时当判据在负载轮里一定假红），且跑在临时目录的真文件上——不拿随包
  流水线的 `three_pass.lock` 做试验品：测试被打断时 `finally` 不执行，会把锁留在脏状态。
- **电池驱动器自己在骗我**：U2 首轮报 `red=0`（读起来像"那条判据没射程"），现场却是
  `11 passed, 1 warning, 11 errors in 177.78s`——我的红数只 grep `FAILED`，fixture 崩掉的
  条目既不计红也不计败。驱动器也是门禁，判成败得看全集：现在 **FAILED 与 ERROR 都计红**
  并单独标注"这 N 条是夹具崩，不是判据松"。复测 U2 = 2 红。那 11 errors 归因到当次瞬时负载
  （同一变异单跑 1 failed、G0 复跑 22 passed 无 error），不归因到变异。
- **两条判据是我自己写成零射程的**：①A10（显式单条读缓存）红 0——判据先改了锁文件，
  时间戳一变，"读不读缓存"两种实现给同一个答案；补一句"文件没动就问那一条，`cached`
  必须为 0"之后才有射程。②`TestSpecParity` 的 needle 用了 handler 名和 `"/api/deliveries/"`
  子串，W1 把路由改成 `/api/deliveries_disabled` 之后两者都还在 ⇒ 摘路由它不红。
  改成钉**分发表达式**本身（`== "/api/locks"` 等），U7/U8 就是专门验这两根新 needle 的射程。

实测口径（都要带条件读）：

- 本机全量（`Python 3.14.3`、`LOOM_TEST_PG_URL` **未设置**、同机无其他 pytest；
  两轮电池 00:31 前全部结束、全量 00:32 起手，不重叠 ⇒ 读的树没被打过桩）：
  `2949 passed, 1 failed, 9 skipped, 6 deselected in 918.10s ≈ 15.3 分钟`。那唯一一条红是
  README 计数护栏自身；换数后单跑 `tests/test_doc_consistency.py` ⇒ **9 passed**，
  所以 README 写 **2950 个测试本机全绿**（实际收集 2959 = 2950 + 9 skip）。
- 本批 22 条单跑：**22 passed in 52.13s**（同机空闲；第一轮那 15 条变异各跑一遍是 55–178s）。
- `ruff check .` 干净；CI 范围内 `mypy loom_core/` Success；`run.py --check` RC=0；
  `scripts/check_design_anchors.py` ⇒ `checked=50 mismatched=0`（本批新增 6 条锚点、
  remap 7 条 `admin_api.py` 位移；其中两条曾被"最近行"挑到同样含该关键字的 docstring/import 行，
  已改回按整行文本钉死）。


### Added（2026-10-26·续65，B4 触发租约：共享一库的多进程只允许一个触发者）

- **治的是什么**（设计复核 2026-10-20 §3.2 B4）：cron 触发此前只有进程内的
  tick 循环——两个 `loom --triggers` 指向同一状态目录，会把同一窗口**各提交
  一遍**（重复 run、重复成本、观测面双份）。租约挂 RunStore 的 `schedules`
  表（结构版本 3），与队列租约同一套语义：条件 UPDATE + rowcount 原子裁决
  （SQLite 侧 BEGIN IMMEDIATE 排队 / PG 天然串行），到期、无人持有或本人
  持有才成功；owner = `主机名:pid`，TTL 默认三倍轮询间隔（下限 60s），每轮
  续租，优雅退出放掉本人租约（另一进程立即接管）。
- **接管正确性有一条容易写错的判据**：tick 被租约拒绝时 `last_check` 仍要
  前移——窗口记账与提交归属是两回事；不前移的话，持约进程一死，接管进程
  会把它已经提交过的旧窗口再算成"到期"而重复提交（`TestTickLease` 的
  接管用例钉住这条）。租约回调自身故障按 trigger 隔离（与提交失败同款
  纪律），`on_fire` 记账失败不影响本次提交——观测面不是闸门。
- **配置列与租约列分开更新**：`register_schedule` 幂等轮换 crontab 时
  不许碰租约列——清了别人的约就有两个触发者（变异 M3 钉住）。
- **降级必须说出口**：RunStore 建不起时回到单进程行为（B4 之前的形状），
  但启动日志警告一行"租约停用——同库多进程可能重复触发"，不静默。
- **表结构**：`schedules(name PK, pipeline, cron, enabled, next_fire,
  last_fire, lease_owner, lease_until)`；`last_fire`/`next_fire` 是观测面
  不参与裁决；未登记的触发 acquire 返回 False 且**不被顺手建出来**（登记
  是配置侧的事，变异 M1 顺手证明了条件 UPDATE 是互斥的唯一裁决点）。
- **判据**：`tests/test_schedule_lease.py` 14 条（存储层互斥/续租/到期接管/
  只放自己的约/登记不碰租约列/未登记不建行/记账可读 + **真跨进程**两个
  独立解释器进程抢同一触发恰好一个拿到 + 真 PG 门控）；`tests/test_triggers.py`
  补 6 条（tick 闸门、被拒不前移时钟、故障隔离、on_fire、真 RunStore 循环
  接线含退出放约、降级警告）。窄变异 3 条各只红预期用例：M1 拆掉条件
  UPDATE → 互斥红；M2 被拒不前移 last_check → 接管正确性红；M3 登记清
  租约列 → 轮换正确性红。
- **文档**：设计文档 §3.1 加 `schedules` 行、落地进度与"只落 4 张表"的
  边界句同步更正；`scripts/check_design_anchors.py` 补 `register_schedule`/
  `acquire_schedule_lease` 两条新锚并重钉 run_store 四条旧行号。

### Added（2026-10-26·续66，A6 保留期与 GC：死代码清理面变成可调度的统一口径）

- 治的是什么：`task_queue.cleanup` / `cost_tracker.cleanup` 是**死代码**（实现过、
  有判据、从未被调度），quality_feedback 与 RunStore 连清理面都没有，长跑
  worker 无限膨胀。
- **`RunStore.cleanup`**：年龄（finished_at < now-days）+ 数量（只留最近 N 条）
  双判据并集，**联动删** runs/run_nodes/events/approvals（run_id 集合）——留下
  "运行没了、事件还在"的半条事实正是本设计要消灭的形状；进行中的行一行不碰；
  0 = 不启用（幂等）。
- **TaskQueue/CostTracker.cleanup 加 max_rows**（SQL 子查询 NOT IN LIMIT）；
  **0 的语义按历史保持**（age=0 = 删光，旧判据钉着）；"年龄不启用"由
  maintenance 显式传 10^9 天。QualityFeedback.cleanup 新增：0 = 不启用
  （此前无清理面，0=删光是静默行为变化，不做）。
- **`loom_core/maintenance.py`**：`gc_runs` 统一口径（假时钟 now 只传给
  RunStore；单 store 失败只记 failed 不拖垮维护）；`gc_from_orchestrator` 从
  orchestrator 组装 store。
- **worker 接线**：`run_forever` 每 `maintenance.gc_ticks` 轮（默认 60，配置热读）
  跑一次 GC——不依赖内存态，**进程重启后下一轮照常执行**（"重启后仍执行"这条
  判据落在这条性质上）；空转 worker 不构建 orchestrator；`run.py --gc` 一次性
  维护动作。
- **判据**：`tests/test_maintenance.py` 17 条（RunStore 联动删/保留集内年龄生效/
  运行行不删/未启用幂等；队列与成本的数量判据与历史 0 语义；反馈默认不启用；
  GC 汇总/失败隔离/配置解析边界；worker 周期调用、空转不构建、失败不拖垮；--gc
  CLI 端到端）。变异对照：拆掉 RunStore 联动删 ⇒ 联动用例变红。
  相关既有套件（task_queue/cost_tracker/quality_feedback/worker）86 条全绿。

### Added（2026-10-26·续67，B6 配置覆写写的 revision CAS）

- 此前 `_write_overlay_with` 是"读 revision → 写 revision+1"：两个进程并发写
  不同键时都读到 N、都写 N+1，**后写者整份覆盖先写者**——先写者的键静默丢失，
  而两边都报成功。这正是 B6 点名的形状。
- 改为读 → 改 → 原子写 → **写后读回验证**：revision 与 values 都与写入时一致
  才算成功；不一致说明有进程抢跑，**重读重写**（下一轮读到抢跑者的新值，把己方
  键并进去——所有键最终都落在文件里）。重试 10 次仍冲突则如实报错
  （`publish_reload` 同一套 CAS）。
- **判据**：`tests/test_config_overlay.py` 补 `TestOverlayCAS` 三条——真双进程
  并发写不同键都成功且两键都在文件里；持续抢跑如实报冲突；单次抢跑后重试收敛
  且两键都在。变异对照：去掉读回验证 ⇒ 冲突用例变红。

### Added（2026-10-26·续68，A4 loom demo：60 秒离线体验（P0 唯一欠账））

- 入口 `loom demo`：在沙盒目录（loom-demo-<ts>/）跑 docgen pack 的 demo
  流水线——transform 模板成稿 → quality_gate 评分 → checker 核查 → layout
  排版 → safe_writer 落盘 → renderer 出 html/docx，外加 deliver.json 结构化
  交付记录。全程无网络、无 LLM Key、不碰真实状态目录（LOOM_STATE_DIR/
  LOOM_VERSIONS_DIR/checkpoint 全部指进沙盒）。
- 实测 5 秒跑完，质量门 **82.0/100**（阈值 70，确定性离线——同输入同分数，
  不是随机量）；渲染缺后端（python-docx 未装）照实报跳过原因；运行失败
  walkthrough 不粉饰。退出码 = 状态。
- 边界：demo 是 docgen pack 的第 7 条流水线（pack.yaml 同步更新）——复用
  质量尾，核心不放领域流水线；锁文件与 YAML 同目录，demo.py 用 parse_file
  走 pack 锁校验。
- **判据**：`tests/test_demo.py` 12 条（真流水线离线端到端、交付物真内容、
  质量分过阈值、账本记录、沙盒隔离、跳过如实报、失败不粉饰、walkthrough
  全要素、动词注册、子进程端到端）；**干净 venv 验收**：`tests/test_
  installable_product.py::TestInstalledSelfCheck::test_clean_venv_demo_runs_
  offline`（真打 wheel → 真 venv → `loom demo` 跑通，实测 42.93s）。

### Added（2026-10-25·续63，RunStore 补账工具：incomplete 运行按交付账本重投影）

- **看得见就得能处理**：续62 只做到"缺口可发现 + 可计数 + 可按 id 查"，本批给那个计数
  一个出口——`loom --replay-run <id>` / `--replay-incomplete`，配 `--replay-dry-run`
  先看不动笔。编排侧入口是 `replay_run()` / `replay_incomplete_runs()`。
- **事实来源只有一个**：交付账本。不读 checkpoint（那是恢复快照，设计 §3 已划出去），
  不读内存（补账常发生在原进程已死之后）。账本没有的东西就承认没有：账本只存
  `duration_sec`、从不存节点的绝对时间 ⇒ 补写的 `run_nodes` 行时间列留 0，
  `run.replayed` 事件里标 `"times": "unavailable"`，而不是拿运行结束时刻倒推一排假时间。
- **补得诚实的四条边界**（都写成行为，不是文档承诺）：只碰 `marker='incomplete'` 且
  队列已终态的行；**只补缺**——已有 `run_nodes` 行不被事后重建覆盖；队列 / 账本 / 投影
  三方口径不一致一律**拒绝**并报两侧值；健康位最后才升格，中途炸掉停在 incomplete，
  重跑幂等。补账工具一旦开始"顺手纠正"冲突，它自己就成了第二个事实源。
- **终态事件的"至多一条"从调用方下沉到存储层**：`close_run` 改成事务内
  `UPDATE … WHERE status NOT IN (done, failed, cancelled)`，影响 0 行就不再追加事件、
  返回 None。此前这条不变量只在 `_claim_terminal_event` 里，而那是**单个 PipelineTask
  对象上**的去重——跨进程补账、重启后的重放、两个 worker 同时收尾都绕得过它。
  投影行不存在时同样返回 None：没有行却有终态事件，正是补账要消灭的半条事实。
- **顺带修掉收件箱里的一条死待办**（与本批无因果，是验证时撞出来的 main 现存缺口）：
  取消一个停在审批闸门上的运行，台账里那行会永远留在 `pending`——运行已死、
  `approve()` 只会回答"未在审批等待中"，但人看到的仍是"等你确认"。现在 `cancel()`
  把它就地置 `rejected` / `by=system` / `reason=运行已取消`；控制动作不因台账不可用而
  失败，只告警（与 approve 的 fail-closed 相反是有意的：批准发放执行权，取消只撤工作）。

判据：新增 `tests/test_run_backfill.py` 17 条；`tests/test_state_backend.py` 补一类
2 条（`close_run` 的条件 UPDATE 与队列按健康位取行都在**真 PG** 上跑——CI 四条腿都带
PG service，但 RunStore 此前从没被 PG 腿覆盖过）；异步/同步取消那两条各补一句
"取消后待批必须已结清"。

窄变异 8 条（每条只红它射程内那几条；MB1 带 PG 腿）：

| 变异 | 红 | 红的是 |
|---|---|---|
| MB1 `close_run` 回到无条件覆盖 | 3 | `test_already_terminal_only_upgrades_marker` + `TestStoreLevelTerminalClaim::test_second_close_run_writes_no_event` + PG `test_close_run_is_terminal_once` |
| MB2 不检查健康位（ok/legacy 也补） | 4 | `test_non_incomplete_markers_are_refused` 三个参数化 + `test_second_replay_is_refused_and_emits_nothing` |
| MB3 不跳过已有节点 | 2 | `test_existing_node_rows_are_not_overwritten` + `test_after_failed_close_restores_terminal_projection` |
| MB4 队列与账本状态冲突照补 | 1 | `test_status_conflict_is_refused` |
| MB5 补账不留 `run.replayed` 事件 | 1 | `test_after_failed_close_restores_terminal_projection` |
| MB6 取消不结清待批 | 2 | 同步与异步那两条取消判据 |
| MB7 dry-run 照样写 | 1 | `test_dry_run_writes_nothing` |
| MB8 运行中也能补账 | 1 | `test_running_row_is_refused` |

三条流程上的实情，记下来比修掉更要紧：

- **变异电池的还原不能用 `git checkout -- <文件>`**。本仓的工作方式是"边改边跑电池"，
  未提交的改动会被 checkout 抹回上一次提交——第一轮就是这么把 `run_store.py` 的本批
  改动整段清掉的（症状是一句 `module has no attribute TERMINAL_STATUSES`，看着像判据错）。
  现在还原走"把变异文本换回原文 + 自校验"。
- **同一份树不能并发跑两条电池**。我把第二轮起早了一步，两份同时在改 `backfill.py`，
  其中一条报出的"15 条全红"是变异叠加的产物，什么都不能说明；停掉、还原、重跑才是数。
- **MB1 第一轮只红 PG 那一条**：因为补账里还留着一处"已终态就不必再盖章"的判断，
  与存储层的新守卫等价。摘掉那处重复判断（只留存储层裁决，`will_close` 降级为
  dry-run 的预告）之后，MB1 才有 SQLite + PG 两个落点。等价守卫留两条，红的就只有
  其中一条有射程的那一条。

实测口径（都要带条件读）：

- 本机全量（`Python 3.14.3`、`LOOM_TEST_PG_URL` **未设置**、同机无其他 pytest 在跑；
  变异电池 20:53 结束、全量 20:58 起手，两者不重叠 ⇒ 这轮读的树没被打过桩）：
  `2927 passed, 1 failed, 9 skipped, 6 deselected in 906.42s ≈ 15.1 分钟`。那唯一一条红是
  README 计数护栏自身；换成实测数后单跑 `tests/test_doc_consistency.py` ⇒ **9 passed**，
  所以 README 写 **2928 个测试本机全绿**（实际收集 2937 = 2928 passed + 9 skip）。
- 带真 PG 复跑**要推的那份树**（本机起一个与 CI 同镜像同库名的临时 `postgres:16-alpine`、
  端口 55461、跑完即删）：`tests/test_state_backend.py` + `tests/test_run_backfill.py` +
  `tests/test_projection_health.py` ⇒ **43 passed in 94.31s**。这一步不能省——续62 那条
  "CI 四条腿同红、本机全量看不见"的红正来自队列 SQL 的漏改判据，而 CI 的 PG service 与
  env 挂在 job 级，四条腿都会执行那 7 条 `requires_pg` 判据。
- 电池还原后本批三套（`test_run_backfill` / `test_async_run_parity` / `test_approvals`，
  不带 PG）⇒ 35 passed；`python scripts/check_design_anchors.py` ⇒ `checked=44 mismatched=0`。

### Changed（2026-10-24·续62，RunStore Stage 3b：投影写失败标 incomplete 后继续 + 队列摘掉结果正文）

- **口径落成**（2026-10-24 拍板："标 incomplete 后继续"）：RunStore 终态盖章失败时
  队列终态照写、worker 继续跑，但这次运行**可发现**——队列行新增 `projection` 列，
  `ok` / `incomplete`（待补账）/ `legacy`（按设计没有投影）。原先的行为是"只留一行
  warning"，而回落读会把缺口洗成正常：队列有终态、投影查无此运行，接口上与一条成功
  运行完全同形。
- **先悲观后升格**：终态先按 `incomplete` 写，`_runstore_close()` 确认盖章落了才升
  `ok`。反过来（先 ok 再写投影）会开一个"队列说一切正常、投影里没有"的窗口。
  升格本身失败则停在 `incomplete`——偏保守，宁可多看一条待补账。
- **`legacy` 与 `incomplete` 是两个值**：`_runstore_open` 只在 `run_plan` /
  `run_plan_async` 里调用（实测），legacy 的 `run()` 连一行投影都不写。混成一个计数
  就是把 deprecated 兜底路径报成数据丢失，而误报会让人开始忽略这个指标。
- **`RunHandle.projection()`**（`complete` / `incomplete` / `legacy` / `unknown`）把
  标记排在"投影里查得到行"**之前**：盖章失败的运行在 `runs` 里永远停在 `running`，
  只看有无行会把它读成正常完成（这条顺序不是推演出来的，是第一轮实测撞出来的）。
  `to_dict()` 带 `projection` 键；`run_store_health()` 与 `/api/health/deep` 报
  `incomplete_runs` / `legacy_runs`。
- **队列停写 `result_json`**：实测全仓**没有读者**（`RunHandle.status/exists` 与
  `_queue_row` 只用 `status`；admin/MCP 的 `result` 来自内存 task），而结果正文的
  事实源早就是 `run_nodes.result_json` 与本批之前的 `deliveries.payload_json`——
  队列那一列是第三份拷贝，还是 §5 警告的写放大点。列与老行留在原处（不回填、不 DROP）。
- **`queue.finish` 保留**，并且**上一批写下的前置是错的**：设计文档 §4 末原先要求
  "Stage 3b 前先把 `recover()` 改读投影"——实测那条不该做：`recover()` 读的
  `status='running'` + `owner_pid` 存活 + 新鲜度阈值就是**租约**本身，投影里没有
  lease 这个概念，改读投影等于把抢占语义搬进观测面。已在设计文档里公开更正。
- **本批没做的**（写在测试文件头，不留"看起来全做了"的错觉）：补账工具——把一个
  `incomplete` 运行重新投影进 `runs` 表需要一个幂等的终态重放入口。

判据：新增 `tests/test_projection_health.py` 6 条（盖章炸掉 / 写口建不起来 / 正常升格 /
legacy 不算故障 / 只有账本的老运行报 unknown / 幽灵 id 报 unknown）；三条旧断言**换成
钉新契约而不是删**——`test_complete_success` 改钉"队列行不再带 `result` 键"，
worker 端到端那条从"结果没写回队列就查不到产出"改成"兜底写的终态必须留 incomplete
痕迹"，`test_async_run_parity` 的逐节点结果比对从队列挪到投影（比较对象没变，权威位置变了）。

窄变异（最终头，只跑这 4 个文件共 42 条用例；每条只该红它射程内那几条）：

| 变异 | 红 | 红的是 |
|---|---|---|
| M1 摘掉"先悲观"：终态落地直接按 `ok` 写队列 | 2 | `test_terminal_projection_failure_is_marked_not_swallowed` + `test_store_unavailable_also_marks_incomplete` |
| M2 盖了章也不升格：所有运行永远读成 incomplete | 2 | `test_successful_run_upgrades_to_ok` + `test_async_and_sync_leave_the_same_run_facts` |
| M3 盖章失败却 `return True`（健康位被谎报成 ok） | 1 | `test_terminal_projection_failure_is_marked_not_swallowed` |
| M4 legacy 路径不单独标（用默认值） | 1 | `test_legacy_path_is_marked_legacy_not_incomplete` |
| M5 `projection()` 判据顺序反过来（先看投影有无行） | 1 | `test_terminal_projection_failure_is_marked_not_swallowed` |
| M6 `count_projection` 恒为 0（缺口数量看不见） | 3 | `..._marked_not_swallowed` + `..._store_unavailable_also_marks_incomplete` + `..._marked_legacy_not_incomplete` |
| M7 队列行又开始带 `result` 键（第三份拷贝读回来） | 1 | `test_task_queue.py::test_complete_success` |

两条流程上的实情，记下来免得后来人以为这轮一次就跑对了：

- **M6 首轮漏跑**，且它守的正是本批的核心承诺（"缺口可计数"）——补跑才拿到上面那行数字。
- **M4 首轮报的是 red=2**，复核后是解析器把一条空名算进去了（`FAILED ` 前缀的行取第 2 段），
  pytest 自己的汇总一直是 `1 failed`。计数以最终头这次为准，解析器也改成只认带 `::` 的整段 id。

M5 是本批唯一"由实测反推出来"的变异：第一版 `projection()` 把 `record()` 放在标记之前，
注入 `close_run` 失败后仍返回 `complete`——顺序错了等于没有这个读维，所以把它钉成判据。

**还有一条红是 CI 抓回来的，本机看不见**：`tests/test_state_backend.py::
TestPostgresTaskQueue::test_submit_acquire_complete_principal` 原先断言 `row["result"]["ok"]
is True`，我改了 `complete()` 的调用却漏改这条断言 ⇒ `KeyError: 'result'`。本机全量抓不到它，
因为 `LOOM_TEST_PG_URL` 未设时整个类按 `requires_pg` 跳过；而 ci.yml 的 PG service 与该 env 写在
**job 级**（四条 Python 腿 `3.11/3.12/3.13/3.14` 都带 `postgres:16-alpine`），所以四条腿同时红
在同一条用例上——不是某条腿的环境差异。改成钉新契约（PG 方言下队列同样不再 SELECT 结果正文、
`projection` 读得出），复核方式是本机临时起一个与 CI 同镜像同库名的 `postgres:16-alpine`、带
`LOOM_TEST_PG_URL` 跑这一个文件（18 条全绿）后立刻删容器。**"摘写"的射程取决于有没有跑 PG 腿，
不在于测试文件有没有被改过**——全仓 PG 门控只有这一个文件，改队列就必须把它跑一遍。

### Added（2026-10-23·续61，RunStore Stage 3a：审批的事实源换成 runs.db 的投影 + 事件）

- **审批摘写**：待批登记、决策、消费不再写 `approvals.db`，改为写 `runs.db` 的
  `approvals` 投影 **+ 同事务的 `approval.requested/decided/consumed` 事件**（结构版本
  1 → 2）。`ApprovalStore` 变成 RunStore 的门面，五个调用点（闸门 arm、approve/reject 各
  两条路径、续跑取决定、Admin 收件箱）形状不变——换后端不该改变台账对外承诺的语义，
  所以 `tests/test_approvals.py` 那 7 条台账契约用例只换了数据来源、断言原样保留。
- **一个决定只有一个时刻**：`nodes / by / at / decision` 由 `_decision_from_record` 从台账行
  单点映射，投影行、`approval.decided` 事件、内存 `task.approvals`、审计
  `audit/approvals.jsonl` 四方同值。此前调用方 `time.time()` 一次、台账再取一次，同一个
  决定有两个时刻，对账只能靠容差糊。
- **老台账按数据边界读，不按开关读**：本批之前落进去的行仍读得到、仍批得掉、消费标记
  仍落在它自己的行上；但**不再有新增行**，文件不存在就当没有（读连接带 `query_only`，
  绝不因为"查一下"建库）。不 backfill、不抄行、不造伪事件。
  为什么不能一刀切掉：升级那一刻停在闸门上的运行如果老行读不到，`decide` 在新投影里
  查无此行 → fail-closed → 那个运行此后永远批不掉。
- **代价要说清（刻意的 fail-closed）**：`runs.db` 打不开时**审批整体不可用**——连内存路径
  的 `approve()` 也会被拒，任务留在 `PAUSED`。写不进台账却回答"已批准"等于执行一个没有
  持久授权的决定（重启后台账仍显示待批）。原因由 `/api/health/deep` 的 `runstore` 组件与
  引擎首条 error 日志承担；这也是上一批删 `LOOM_RUNSTORE=0` 退路之后一致的选择。
- **测试会话的状态目录开局先清**（`.test_state/pid<pid>/`）：Windows 会回收 pid，上个会话
  残留的老台账行会被回落读当成本会话的事实（收件箱凭空多一条待办）。

判据：`tests/test_runstore_approvals.py` 16 条，四组——投影与事件同事务（含"事件写失败
则投影不动"）、台账是决定形状的唯一来源（真闸门 + 真审计）、老台账的四条边界、写侧
不可用时 fail-closed。`tests/test_approvals.py` 14 条（7 条台账契约 + 7 条端到端：
真子进程硬退出在闸门上、双进程观察决定、真 HTTP 收件箱）只换数据来源、断言不动即过。

窄变异（同一份最终头，只跑这两个文件共 30 条用例；每条只该红它射程内的那一两条）：

| 变异 | 红 | 红的是 |
|---|---|---|
| 摘掉幂等守卫 `AND status='pending'` | 2 | `test_only_pending_can_be_decided` + `test_second_decision_writes_no_event_and_keeps_the_first` |
| 待批只写投影、不写事件 | 2 | `test_request_lands_projection_and_event_together` + `test_gate_arms_into_the_store_projection` |
| 事务异常也提交 | 1 | `test_append_failure_leaves_the_projection_pending` |
| 老台账 pending 不再回落 | 1 | `test_legacy_pending_row_is_still_readable_and_approvable` |
| 待批双写老台账（双写复活） | 7 | 老副本成了第二个事实源：二次决策被老行放行、决定错绑到前一道闸门、历史顺序被带偏 |
| 决定形状自己取时刻 | 1 | `test_audit_event_and_memory_all_read_the_ledger_row` |
| 无待批行也造一个决定（fail-open） | 1 | `test_approve_is_refused_when_the_store_is_unavailable` |
| 消费不回落老台账 | 1 | `test_legacy_decided_row_can_still_be_consumed` |

首轮还试过"摘掉 Python 侧的 status 预检"——**红 0**：它和条件 UPDATE 的 `WHERE` 是同一件事，
挡住写入的一直是后者。于是把死的那条删掉（幂等只留一条守卫），不给后来人指错门。

### Changed（2026-10-21·续60，RunStore 不再留退路：开关移除 + 失败可观测）

- **删掉 `LOOM_RUNSTORE=0`**：写侧恒开。删之前先确认过它**不承载读语义**——`RunHandle`
  的回落是数据边界（"投影里没有这行 → 队列 → 账本"），与开关无关；所以删开关不改变任何
  读结果，只失去"运行期把新写面关掉"的能力（回滚版本可替代）。
- **失败不再静默**：新增 `PipelineOrchestrator.run_store_health()`（可用性 / 方言 /
  `schema_version` / 最近一次失败原因）并接进 `/api/health/deep` 的 `runstore` 组件；
  建口失败的日志从 warning 升为 **error**。没有退路之后，"写不出投影"必须一眼看得见，
  而不是表现得像"一条运行都没跑过"。
- **判据从"设环境变量"改成"注入故障"**：两处依赖开关的用例改为用替身让 `RunStore`
  建口即抛，验证旧三面照旧、且健康检查报 `degraded` + 原因。
- **`task_queue` 的终态写入保留**（不是这一批，也不该跟着删）：`recover()` 只看
  `status='running'` 的行并把 owner 已死的翻回 pending 重跑，`queue.finish` 是这条恢复
  路径的**判据**而不是投影的镜像——删了它，跑完的运行会在下一次 worker 启动时被整批重跑。
- **顺带修两个被单测掩盖的真缺陷**：
  ① `/api/health/deep` 在真编排器上必 500——`CircuitBreakerRegistry` 没有 `list()`
  （读法是 `all() -> {name: to_dict()}`），而既有单测用 MagicMock 把 `hasattr` 与返回
  值都糊过去了；
  ② `CostTracker` 的库文件在 `__init__` 之后被删/换掉时，每个操作都撞
  `no such table: cost_log`（state 目录迁移/清理的同类场景）——改为连接时发现文件是新的
  就补建表。

判据：`tests/test_runstore_parity.py` 的降级用例改为注入不可用（旧三面照旧 + 健康报
degraded）+ `schema_version` 一条；`tests/test_runstore_readside.py` 加 `/api/health/deep`
的 `runstore` 组件断言；`tests/test_cost_tracker.py` 加"库文件被删后自愈"一条。

### Added（2026-10-21·续59，RunStore Stage 2b：`/stream` 重放改读 `events`，跨进程可重放）

- **重放改由投影回答**：进程内没有 `StreamCallback`（发起流的那条进程退出了 / 换了
  进程）时，从 `events` 按 `after_seq` 逐页重放运行级事实（`run.opened` /
  `node.finished` / `run.completed` …），运行还没到终态就**尾随**（0.5s 轮询 +
  心跳），到终态发 `complete` / `error` 收流。逐 chunk 的正文流从不落盘，因此不在
  重放范围内——边界照实写。
- **游标分两套命名空间**：`r<seq>` = 投影事件游标（跨进程有意义）；纯数字 =
  进程内 `StreamEvent` 计数器（跨进程没有意义）。不能都 `int()` 掉——把计数器当 seq
  用会从错误的断点续传。
- **重连不再起第二条流水线**：旧行为是"查不到进程内回调就再跑一遍"，把一次重连变成
  第二次运行。现在投影里有这条运行就只重放/尾随；有运行但没有可重放事件
  （`LOOM_RUNSTORE=0`）时如实回一条 error 帧，同样不回退到"再跑一遍"。
- **重放前过作用域**：`_scoped_run` 判租户，越权与"不存在"同形 404——SSE 不能成为
  存在性旁路。
- **修 `/stream` 的连接收尾**（顺带发现）：原代码在开头置 `close_connection = True`，
  随后又发 `Connection: keep-alive` 头——CPython 的 `send_header` 会把这个标志**翻回
  False** ⇒ 流推完后连接一直挂着；`EventSource` 因为自己会重连所以看不出来，而
  `http.client`/`urllib` 这类等 EOF 的客户端只能超时。改发 `Connection: close` 并在
  `end_headers()` 后重新置位。

判据 `tests/test_sse_replay.py` 11 条（真 HTTP + 真 RunStore，不跑流水线）：整段重放、
从 `r<seq>` 续传、进度帧由节点投影合成、失败运行以 error 帧收尾、**带 query 的重连
不产生第二条运行**、运行中尾随到终态、**另一个真进程写的运行照样重放**、跨租户 404 与不存在同形、admin 可重放。
三组变异：忽略游标 ⇒ 只红续传那条；摘掉作用域检查 ⇒ 只红跨租户那条；
把连接收尾还原成旧写法 ⇒ 等 EOF 的客户端超时（整段重放那条红）。

### Changed（2026-10-21·续58，RunStore Stage 2：读侧切到投影、内存降为缓存 + 身份贯穿）

- **`/tasks` / `/api/dashboard` / MCP `list_tasks` 共用 `list_runs`**：内存缓存 ∪ `runs` 投影，
  按开始时间倒序；同一 run_id 两边都有时以内存为准（它更新），但**投影里有而内存淘汰了的
  照样列出来**——`_trim_task_history` 的 100 条边界不再决定"查不查得到"。
- **`/tasks/<id>` 未命中内存时从投影回答**：状态 / 流水线 / 起止时间 / 错误 / 产物清单 /
  逐节点摘要（status、attempts、duration、artifact_keys）。**给不出的不编**：节点 `result`
  正文本就从不落盘（Stage 1 只存摘要），于是那些键不出现；`cancel/resume/审批` 仍只读内存
  ——动作只有内存态才有意义，淘汰后照旧 404（不假装能操作）。
- **`/api/cost` 的作用域集合**由投影补上（"成本归因随淘汰丢"是设计 §1 点名的三条之一）。
- **MCP `get_task` 的存在性改由 `RunHandle.exists()` 回答**（内存 → 投影 → 持久队列），
  不再先看内存；`list_tasks` 与 Admin 同一条读口径。
- **身份贯穿**：`task_queue.submit` 三处调用点传 `principal`（列早有、写侧恒空）；
  SSE 的 worker 把 `Principal.id` 交给 `run_plan_async`（此前 SSE 发起的运行 owner 恒空）。
- **`RunHandle` 的读方法能说出"这一行是谁答的"**：载荷新增 `source`
  （memory / runstore / deliveries / none），淘汰后的数据不会假装自己来自内存。

判据：`tests/test_runstore_readside.py` 17 条（真流水线 + 真 HTTP + 两个 token）；
五组窄变异各只红预期用例（列表不并投影 ⇒ 7 红、详情不读投影 ⇒ 2 红、
`exists` 只看内存 ⇒ 1 红、同步 submit 丢 principal ⇒ 1 红、SSE 丢 owner ⇒ 1 红）。
§5.4 的两 token 旧判据改为把真的 `PipelineOrchestrator.list_runs` 挂到替身上——
原先替身直接返回"已过滤好的行"，过滤逻辑本身一次都没被执行过。

边界（写清，别读成"全切完了"）：`/stream` 重放改读 `events` 拆到 Stage 2b
（事件 → SSE 帧的映射要单独定契约）；Stage 1 之前的运行不 backfill，
仍按原路径从 `deliveries` 读终态。

### Fixed（2026-10-21·续57，池满拒答的 503 送达 + 「池满」判据不再靠猜窗口）

- **503 送不到客户端**：`_reject_saturated` 原来"写 503 就不读请求直接关"。close 时
  接收队列非空 ⇒ 内核发 RST ⇒ 刚写进去的响应被客户端丢弃，`recv` 报 WinError 10053。
  而字节往往是在我们 close **之后**才落地的（本机实测到达延迟 8µs~24ms，长尾是客户端
  线程被调度挤掉），所以只做"非阻塞回收"也拦不住：回收 0 字节的拒答里 10 次有 2 次
  丢响应。后果不是测试红，而是**过载被客户端看成连接故障**，`Retry-After` 退避失效。
  现在写 503 前最多等 50ms 让字节落地 → 非阻塞取空 → 写响应 → 补收一次 → 优雅关闭。
- **等待带配额，不把修复变成新的 DoS 面**：accept 线程每秒总共只允许牺牲一个窗口
  （50ms）。真客户端一眨眼就发完，配额几乎不动；静默刷连接者把窗口耗满一次后，后续
  拒答立刻降级为即时硬关。
- **写 503 前恢复阻塞模式**：回收请求字节要把套接字调成非阻塞，而 `sendall` 跟在后面
  ——非阻塞写一满就抛 `BlockingIOError`，正好被外面那句 `suppress(OSError)` 咽掉，
  症状和这个缺陷本身一模一样（响应悄悄丢了）。现在写前显式 `setblocking(True)`。
- **「池满」的等待条件换成服务端占用计数**：三条用例原先靠 `time.sleep(0.4~0.6)` 期待
  前一个请求已经把池占住，同机有并行负载时第 3 个请求会在前两个还没派发时到达 ⇒ 池没满
  ⇒ 全 200，报"200 的数量不对"，看起来像并发上限本身失效。改成轮询 `_server.inflight`
  （accept 线程派发时递增，是被测方真实状态）并对"没等到"判红；占位请求的 gate 等待从
  5s 提到 30s（前提是它必须大于等待上限 10s，不是靠加大数值凑绿）。
- **新增 3 条判据**（`tests/test_admin_concurrency.py`，各做过变异，红集精确确定）：
  ① 缺陷版从回收到关闭约 0.1ms、修复版窗口 50ms，测试让客户端在 5ms 才发 ⇒ 夹在两个
  已知时长之间，两侧余量 10~50 倍，不是"睡一下看看"；② 写 503 那一刻套接字的阻塞模式
  必须为真（`socket` 实例带 `__slots__` 挂不了表笔，故用一层只记录模式的代理转发到真
  socket）；③ 去掉配额闸门后第二条静默连接也会等满 50ms ⇒ 判红。**注意**：原有两条真
  HTTP 的 503 用例对本缺陷是无射程的——变异 M1（去掉等待）时它们 3 轮全绿，只有新写的
  确定性用例红。
- **本机实测**（Python 3.14.3、本机相对空闲）：`TestBoundedConcurrency` 全类 5 条最终头
  10 轮 0 红（去掉 blocking 恢复前另测 20 轮 0 红；缺陷原样时同一批 20 轮红 9 轮，且
  那 9 轮全部落在两条真 HTTP 用例上）；变异 M1 ⇒ 2 红、M2 ⇒ 恰 1 红、M3 ⇒ 恰 1 红，
  三者均 3/3 轮一致，还原后 17 passed。全量在 `90e4892` 上 **2857 passed, 7 skipped,
  6 deselected in 753.83s**（收集 2864，`LOOM_TEST_PG_URL` 未设 ⇒ 5 条真 PG 判据跳过）；
  该头之后只有本行与 README 的口径数字在动，测试集合未变。

### Added（2026-10-21·续56，RunStore Stage 1：事件 + 投影同事务的双写口）

- **新增 `loom_core/run_store.py`**：一次运行的事实从此有一个写入口。四张表
  （`schema_version` / `events` / `runs` / `run_nodes`），每个写口都是**同一事务里
  追加事件 + 更新投影**——不会出现"事件说完成、投影说运行中"这种分属两个时间坐标系
  的中间态。`seq` 在事务内取 `MAX(seq)+1`（不用自增 id：多进程共享库时跨进程唯一
  ≠ 每运行单调，而 SSE 游标要的是后者），SQLite 侧靠 `BEGIN IMMEDIATE` 排队取号，
  其余方言走驱动事务 + `SELECT … FOR UPDATE`。**`INSERT OR REPLACE` 全程禁用**——
  `db_backend` 的翻译表里没有它，写了就是"SQLite 能跑、PG 直接报错"的假可用。
- **引擎接线三处 + 一处补登记**：`run_plan` / `run_plan_async` 开头 `open_run`
  （trigger 分别是 direct/resume/stream/stream-resume），每层跑完按**当时**的
  `dag_node` 落 `run_nodes`，收尾与 Stage 0 的共用终态口一起 `close_run`。
  闸门节点在 `_arm_approval_gate` 就登记一次——只靠层尾登记会漏掉"停在闸门上被取消"
  的运行（本批的三方对账门禁实测抓到，见下）。**旧写面一个都没删**（Stage 3 才摘）。
- **`LOOM_RUNSTORE=0` 是退路，不是特性开关**：关掉后旧三面照旧、新表零行，
  读写两侧都不依赖新表（§5 要求的回退，有判据钉住"退路本身没把观测面一起删掉"）。
- **判据 `tests/test_runstore_parity.py` 9 条**：同/异步 × done/cancelled 的
  **四方对账**（`runs`+`run_nodes` vs `task_queue` 行 vs `deliveries` payload vs 内存
  `dag_nodes`，节点集合、逐节点状态与产物键、`last_seq == 事件尾部`、seq 连续），
  开关退路 1 条，「有事件必有投影行」不变式 2 条（含 `record()` 给陌生 run 也建行），
  源码级禁 `INSERT OR REPLACE` 1 条，逐条 SQL 过 Postgres 翻译层 1 条
  （`?` 全部消失、`OR IGNORE` 全部翻掉——探针先断言"至少抓到 8 条 SQL"，防自己瞎）。
- **门禁与写口各自独立才有射程**：`run_nodes` 刻意**不**借用 `_build_delivery_record`
  的形状，而是读层跑完那一刻的 `dag_node`。若改成抄那份记录，"投影 vs 账本"就成了
  自己比自己。
- **层内合批（§5 写放大缓解落成判据）**：一层多个节点**共用一次事务**落投影与事件。
  这条不是性能洁癖——`BEGIN IMMEDIATE` 拿的是整库写锁，逐节点各开一次就是一层 N 个节点
  排队 N 回。`tests/test_runstore_parity.py::TestBatching` 数 `_Txn.__enter__` 的调用次数
  钉住它（宽层 2 节点：建口/开局/一层/收尾＝4 次；退化成逐节点＝5 次当场红）。
  **数据形状两种写法一模一样，只有事务边界不同**，所以对账判据对它完全无感——
  这类"只有计数能抓"的性质必须自带判据，不能靠 review。
- **两处由判据自己抓出来的缺陷**（都已在实现里修掉，不是事后补的说明）：
  ① 实例属性 `self._runstore` 遮蔽了同名方法 `_runstore()`（实例属性优先），
  于是每次运行都在 `TypeError: 'NoneType' object is not callable` 上炸——
  8 条 ERROR + 6 条 FAILED，是把 fail-soft 承诺写在 try 之外的代价； accessor
  现在自己兜异常。② 闸门节点缺行：`run_nodes=['tldr']` vs 账本
  `['release_gate','tldr']`。
- **本批如实留下的空白**：`runs.cost_usd` / `quality_score` **不填**（来源还没定，
  不拿猜测当事实）；`principal` 沿用调用方给的归属——今天 SSE 与直跑都传不出
  owner，所以列是有的、值多为空串（Stage 2 读侧切换前先把身份接上）。
  三方对账门禁是**显式用例**覆盖两引擎 × 成败，不是"每条用例跑完自动后置检查"。

### Fixed（2026-10-21·续55，RunStore Stage 0：异步引擎补齐同步侧的运行事实）

- **异步收尾不再"只跑不记"**：`_finalize_plan_task_async` 此前整段不写持久队列、
  不发终态事件（它自己的 docstring 就写着"与 `_finalize_plan_task` 的差异是不更新
  task_queue、不发事件钩子"）。而 `run_plan_async` 的唯一调用方是管理 API 的 SSE
  worker——于是**从控制台发起的运行在 `/api/tasks` 里查不到、事件面一条不发**，
  "UI 看得见、接口查不到"。现在两条收尾共用 `_record_plan_terminal_state`
  （队列 `finish` + 终态事件），载荷键集与同步侧逐字段一致。
- **异步发起补登记**：`run_plan_async` 现在与同步版同写 `task_queue.submit` +
  `claim_direct`（直跑也占住行，防被 worker 重排成第二个执行者）并发
  `task.created`/`task.started`——此前异步运行在队列里没有行、事件序列缺头两笔。
- **异步闸门接上"他进程决定"**：暂停循环原先只 `await asyncio.sleep` 等一个
  永远不会来的信号（同步版有观察逻辑、异步版没有）。抽出同步/异步共用的
  `_pause_boundary_snapshot`（完整状态快照，浅检查点会让已完成节点全部重跑）与
  `_observe_external_decision`（waiting/running/cancelled），两条执行循环的差异
  只剩"怎么睡"。
- **终态事件至多一条**：新增 `PipelineTask.terminal_event` + `_claim_terminal_event`
  先到先得。原先 `cancel()` 在置 cancelled 的当下发一条、收尾链按状态再发一条 ⇒
  同一次运行两条 `task.cancelled`，钩子/审计看到两次收尾。现在这是**属性**而不是
  调用顺序的运气；四处写入点各自的载荷形状保持原样（对外契约不扩键、不改名）。
- **判据**：新增 `tests/test_async_run_parity.py` 4 条——同步/异步两条路径**Leave
  the same run facts**（事件序列逐条相等 + 队列行终态与节点结果）、同步取消与异步
  取消各"至多一条终态事件"、异步暂停保完整状态并被另一实例的决定唤醒。
  变异验证（在最终头 `5143380` 上重跑，不是早期树上的结果）：① 把 `pipeline.py` 换回 main 版 ⇒
  **4 条全红**（缺登记 / 缺终态 / 缺观察，且连"同步取消至多一条"也红——还原 main 同时把去重也摘掉了）；
  ② 只关掉 `_claim_terminal_event` 的"已发过"闸门 ⇒ **恰好 2 红 + 2 绿**（两条取消判据红并直接打印
  重复的 `task.cancelled`，另两条不受影响）——这条才是"变异只打掉它该打掉的东西"的对照。
  本机 `Python 3.14.3` 全量实测 `2844 passed, 7 skipped, 6 deselected in 873.44s`
  （收集 2852；唯一一条红就是读 README 计数的那条护栏，改数后 `tests/test_doc_consistency.py`
  9 passed / 16.44s；`LOOM_TEST_PG_URL` 未设 ⇒ 5 条真 Postgres 判据按 `requires_pg` 跳过）。
  影响面另跑 `218 passed / 765.32s`，rebase 到 main 后复跑 `57 passed / 217.11s`。
- **连带两处口径同步**：README 的测试计数 2839 → **2845**（不同步就会推一个已知红 SHA——
  护栏判据是 `collected − declared ≤ 10`，本批 +4 条后差值到 13）；
  `scripts/check_design_anchors.py` 的 `pipeline.py` 锚点随本次重构重钉 9 条 + 退役 1 条。
  退役的是那句「不更新 task_queue」的 docstring 引用——它已被并轨改掉，改成钉在新增的共用写口
  `pipeline.py:499 _record_plan_terminal_state` 上，**"裂缝已补"这件事本身也要可核对**。
  重钉靠整行文本对齐而不是推算偏移：同一次改动里 `+4 / +65 / +47` 三种 delta 并存
  （开头插字段、中段插 helper、又把 `_finalize_plan_task` 的内联收尾换成一次调用）。

### Added（2026-10-21·续54，RunStore + EventLog 设计入库，P1-① 起手）

- **新增设计复核** `docs/runstore-eventlog-design-2026-10-21.md`：运行态散在**五处写面**
  （内存 `_running_tasks` 硬顶 100 条 / `task_queue` / checkpoint JSON /
  `deliveries.db` / `approvals.db`），事件面散在**五条总线**（`emit_event` 只在内存、
  `bus.publish` 进 message_store、`write_audit` 进 JSONL、deliveries、approvals），
  20 处直接读点里最坏的三条（`_scoped_task` 以内存存在性判可见、`/api/cost` 的作用域
  集合、`/stream` 的进程内 `_history`）都建立在"进程没重启过"这个假设上。文档给出
  `RunRecorder` 同事务双写 + `events`/`runs`/`run_nodes` 投影的模型、Stage 0→3 分期与
  退出判据，以及三条已拍板边界：**不 backfill 历史运行**、**本批只立接口 + SQLite**
  （PG 后端延后）、**checkpoint 保留为续跑快照而不并入 RunStore**。
- **配套 `scripts/check_design_anchors.py`**：文档里 40 处 `(文件, 行号, 该行的锚文本)`
  逐条核对，行号漂了或代码被改掉即退出码 1——防止设计文档的引用变成"看起来很确定的
  历史传闻"。**刻意不接进 CI**：它是文档的自我校验工具，Stage 每推进一格就要按新现实
  更新或退役一批锚点，挂在流水线里只会让人为了变绿而改锚点。当前 40/40 通过。

### Fixed（2026-10-21·续53，判据修一处采样竞态）

- `tests/test_approvals.py::TestLiveWorkerObservesExternalApproval` 的子进程原先把
  "此刻还挂在暂停上"当成功信号。外部批准可能落在两次 50ms 采样之间——闸门确实挂起过
  （父进程读到 pending 台账行后才继续），等子进程回头读 `awaiting_approval` 已被清空，
  于是把"没观察到暂停"误报成"没暂停"（**CI `test (3.13)` 腿实测红在
  `{'phase': 'no_gate', 'status': 'done', 'counts': {'tldr': 1}}`，同一 SHA 另三条腿绿**）。
  改为子进程只报**终态**（`phase: terminal` / `timeout`），"挂起发生过"由父进程侧的台账判据负责。
  反向对照实跑：同一脚本不批准 ⇒ 落盘 `{"phase": "timeout", "status": "paused",
  "counts": {"tldr": 1}}` 且退出码 9 ⇒ 新断言不是空转。正向 3 次重跑全绿
  （19.68 / 19.70 / 26.35s，本机 Python 3.14.3，未设 `LOOM_TEST_PG_URL`）。
- `tests/test_executor_factory.py::test_parallel_speedup` 换成 `test_parallel_execution_actually_overlaps`。
  旧断言 `parallel_time < serial_time * 3.0`（n=8、sleep=0.3 ⇒ 预算 7.21s）**两头都不成立**：
  把池人为串行化（`max_workers=1`）实测 `parallel_time=2.73s`，仍远小于预算 ⇒ 它声称"防止退化"
  却对退化**零射程**；而 Windows 的 4 次进程 spawn 全算进被除数 ⇒ 本机并行负载下假阳性
  （2026-10-21 全量 2066.16s 那次红在这条，单跑 3 次 4.5–5.6s 全绿）。
  新判据看**窗口重叠峰值**（同一时刻几个任务在跑）：`workers=4` 得 peak=4，
  变异 `workers=1` 当场红 `peak=1`（33 passed / 14.18s 复原）。负载只会拖慢 spawn，
  不会伪造重叠、也不会因慢而误红。顺手删掉失去使用者的 `_sleep_identity`。
### Removed（2026-10-21·续52，死发布面清理）

- **删掉 `.github/workflows/release-plugins.yml`**：它是 2026-10-11 单发行物决策的遗留物——
  仍列 6 个 `examples/plugin-*` 目录（实际只剩 `plugin-hello` 模板）、仍断言
  `Requires-Dist: doc-pipeline`（宿主发行名早已是 `nexusloom`），一 dispatch 就必红。
  现场实测：workflow API `total_count: 0`（一次都没跑过），`loom-plugin-hello` /
  `doc-pipeline-plugin-hello` / `doc-pipeline-plugin-tldr` / `nexusloom-plugin-tldr`
  在 PyPI 全 404（没有任何已发行物被这次删除影响）。
  **处置选删除而不是修**：README 第 3 条写明"examples/plugin-hello 留作外部作者模板，
  不再单独发行"，把它修成可用等于凭空造一个本仓不该存在的第二发布物。
- **新增唯一发布面判据** `tests/test_release_readiness.py::TestSinglePublishSurface`（2 条，双向）：
  解析各 workflow 的 `steps[].uses` 而不是全文子串（注释里提到动作名不算发布面；
  子串版实测会被 `…-MUTATED` 骗过去）。多一个发布器红、把唯一的那个弄没也红。
  变异验证 5 组全部符合预期：加 ghost 发布器 / 改名 / 整步删除 → 红；
  注释含动作名 / 还原后基线 → 绿。
- **同批改掉 README 里两处同型过度声称**（复核 B9 的新实例）：「可运行的参考实现共六个独立
  pip 包 …`pip install ./examples/plugin-tldr`」与「`test_plugins_ecosystem` 一次真装六个包、
  数量不足 5 即红」都随单发行物决策失效；已按该测试的真实判据（内置件 glob / 模板形状 /
  鬼魂目录 / 第三方诚实计数 0）改写。`test_release_readiness.py` 的"六个插件包""宿主
  doc-pipeline"两处陈旧口径同步更正。

### Added（2026-10-20·续51，P0 落地：控制台随包 + 审批跨重启/收件箱 + 租户作用域）

- **控制台随包发布**：`dashboard/` 迁入 `loom_core/dashboard/` 并进 `package-data`
  （3.12.0 的 wheel 里 0 个 dashboard 资源，装完打开 `/` 只有一行纯文本）；`GET /`
  免鉴权直出入口页（只发静态壳，数据端点各自鉴权）；管理 API/守护模式一律带静态资源；
  `verify_dist.py` 把三个控制台资源列进 `REQUIRED_DATA`，发布门禁从此会拦。
- **审批跨重启**：新增 `loom_core/approvals.py`（SQLite 台账，跟随 `LOOM_STATE_DIR`）。
  闸门挂起时落 pending 行；`approve/reject` 对"已不在内存"的任务改为写台账 +
  把队列行放回 `pending` 并置 `resume=1`，worker 以 `run_plan(resume=True)` 从
  checkpoint 续跑越过闸门；决定在结算时标记消费（不会二次越闸）。驳回同理：
  台账记 rejected、队列行置 cancelled、不再执行。
- **审批收件箱**：`GET /api/approvals`（非 admin 只看 principal 属于自己的待办，
  admin 见全量并带归属）；控制台新增"待审批"卡片，放行/驳回直接落到既有端点。
- **租户作用域补齐**：`/api/dashboard` 的任务列表按 principal 过滤（此前普通 token
  能看到全仓任务）；`/api/logs`、`/api/quality/feedback`、`/api/alerts` 这些跨租户
  聚合面对非 admin 返回 403；控制台对 403 显式显示"需要 admin 权限"，不再折叠成"暂无数据"。
- **CI 类型门禁修一条上游漂移**：runner 上 pymupdf 2.0 起自带 `.pyi` 并引入 numpy，
  numpy 2.5 的 stub 用了 PEP 695 `type` 语句；mypy 1.20.2 + `python_version=3.11`
  判 `[syntax]` 直接失败（只在 3.12+ 腿命中，3.11 腿装旧 numpy 所以是绿的）。
  `follow_imports=skip` 实测**不阻止** stub 解析，故改为钉 `mypy==2.4.0`
  （对照 venv 实测：1.20.2 + numpy 2.5.4 = syntax error；2.4.0 = 通过，
  本仓全量类型面零新错）。
- **旧名清理（运行面）**：compose 镜像/容器、Dockerfile 示例、OTel tracer、控制台标题
  与根文案里的 `doc-pipeline` 全部改为 `loom`/`nexusloom`。
- **判据**：新增 `tests/test_approvals.py` 14 条——台账 7 条（请求复用 / 只有 pending
  可决策 / 决定消费 / principal 过滤 / 未知结论拒绝 / 历史倒序 / **决定绑定闸门节点集合**）
  + **真重启 2 条**（子进程硬退出在闸门上，父进程批准后 worker 续跑完成；驳回后队列为
  cancelled 且不再执行）+ 暂停边界完整检查点 1 条 + 活 owner 队列守卫 1 条 +
  **真双进程 1 条**（执行方仍活着停在闸门：另一进程批准→台账成为唯一通路→暂停循环自己
  观察到决定并跑完；同一段里钉"活 owner 不被重排"与"前序节点计数不涨"，计数挂在真实
  执行漏斗 `dag_executor.execute_node_from_scheduler` 上而非状态推断）+ 真 HTTP 2 条
  （双 token 收件箱隔离 / 越权审批 404 / 日志质量告警 403 / dashboard 任务过滤）。
  控制台新增 3 条（包内资源 / 打包声明 / `GET /` 真 HTTP 返回 HTML）。

### Changed（2026-10-20·续50，产品定位收口：宽内核、窄门面）

- **对外定位收敛为「可验收的 Agent 交付运行时」**：README 首屏与 PyPI 摘要不再以
  "文档生成流水线"自称，改为"取数/资料 → 加工 → 核查 → 人审 → 交付"的可复现、可审计作业；
  **docgen 明示为旗舰 pack**（场景分级/输出格式/检索摄入均标注属该 pack），引擎与领域无关。
- **新增设计复核** `docs/product-design-review-2026-10-20.md`：全仓实测复核（含控制台未进发行物、
  租户作用域缺口、审批不跨重启、四源运行态等缺陷），给出补/纠/删清单、控制台 IA 重设计、
  RunStore + EventLog 架构提案与 P0–P2 路线。
- **修掉两处过度声称与一处自相矛盾**：README 测试节删去"无头 Chrome 走查留证"（仓内无浏览器级
  证据）；审批边界不再写"无控制台界面"（S2 已有操作落点）。

## [3.12.0] - 2026-10-16

v3.11.0（10-12）之后的累计发行版，按下面的续41–续49 逐批归档：首个真第三方插件例证（生态轴
0→1）、P1-A token 身份与多租户作用域隔离、P1-B 状态后端可换（SQLite/Postgres 双后端）、
P1-C 配置热更新跨进程生效、P1-D 常驻服务并发模型两处缺陷关闭、spec §4.2 控制台 S2 运行详情屏
与 S3 节点检查器、spec §6 API 原语 `RunHandle` 落地并接进 MCP `get_task` 与 Admin
`GET /tasks/<id>`，以及产物清单持久化到交付账本（`deliveries.db`——任务被内存历史淘汰后
`artifacts` 不再凭空变空）。

响应面只增不改：`artifacts` 是新增字段，MCP `get_task` 的既有键
（`id/status/pipeline/result/error`）一个没动（判据 `tests/test_run_handle.py`）。
升级时请顺带核对这几处新读面：`tokens.yaml`（P1-A 身份）、状态后端 DSN（P1-B）、
配置覆写文件与 `LOOM_*` 前缀（P1-C）。

### Added（2026-10-15·续48，spec §6 的 API 原语 `RunHandle` 落地）

- **`loom_core/run_handle.py`**：一次运行的句柄，五个原语 `status / wait / cancel /
  resume / artifacts`（spec 原写 `await`，Python 关键字，落地名 `wait()` 语义不变）。
  入口是 `PipelineOrchestrator.run_handle(task_id)`；句柄不持有 task 对象，每次读
  最新态——任务不在内存时 `status()` 回落到持久队列（多机 worker / 重启后仍能问状态），
  两边都没有才报 `unknown`。
- **`artifacts()` 只报引擎确实记下的东西**：逐节点结构化产物（`TaskNode.result` 的键）
  + 声明 `WRITES_OUTPUT` 的节点挂上的交付物（路径 + 磁盘真值字节数）。这替掉的是
  MAOP 侧按节点名造伪 ID 的写法（`pipeline:<名>:step<n>:<agent>`）。
- **接线（不是"造了个没人调的类"）**：MCP `get_task` 与 Admin `GET /tasks/<id>` 的
  `artifacts` 字段都由这个原语产出；两处旧字段一个没动（MAOP 适配器读的
  `task_id/status/result/error` 仍在，实测 `RunHandle.to_dict()` 覆盖）。
- **`PAUSED` 算 `wait()` 的终态**：停在审批闸门前等人决定的任务不是"还在跑"，
  否则 `wait()` 会替一个人类闸门挂到超时。
- **判据**：`tests/test_run_handle.py` 20 条——真跑离线流水线（transform→safe_writer，
  无网络无 LLM，落盘与备份都在 tmp 目录）测 status/产物/字节真值；真 HTTP 测 admin
  接线；真 `MCPServer._handle_request` 测 MCP 接线；`wait()` 的超时预算与不挂死用
  计时断言。六组变异对照全被抓住：`artifacts()` 恒空（4 条红）、去掉持久队列回落、
  PAUSED 移出终态集（该条实测耗时 31s 才红，正是"没被卡住就会挂死"的形状）、
  句柄缓存工作对象、句柄缓存状态字符串、MCP 载荷改回下标访问。
  `loom_core/run_handle.py` 覆盖 **82/82 = 100%**（`--cov=loom_core.run_handle`）。
- **一处判据自纠**：MCP 同源断言最初写成 `payload["artifacts"] == handle.artifacts()`，
  变异后**两边同时为空、断言恒真** ⇒ 先加"确实有东西"再比同源，才抓得住。
- **review 自查（同一轮，四个提交）**，抓到六条：
  ① `status()` 一行里读两遍 `task.status`（先 `hasattr` 再取值），并行腿随时推进它 ⇒ 改读一次；
  ② 我的 freshness 用例是**侥幸变绿**的——替身用 pop-based property，恰好依赖那两遍读，
  把实现改成"缓存状态字符串"照样绿 ⇒ 替身换成可写字段并补一条专门卡它的用例；
  ③ `wait()` 默认 poll 0.05，而任务不在内存时每轮是一次 SQLite 取行 ⇒ 默认 0.2 并写明代价；
  ④ 新字段 `artifacts` 进了 HTTP 响应却没进 OpenAPI 的 `TaskInfo` ⇒ 补 schema
  （顺带记录：`dag`/`awaiting_approval`/`approvals` 同样缺，属既有漂移，本轮未扩范围）；
  ⑤ **真缺陷**：`_trim_task_history` 只留最近 100 个任务（`pipeline.py:1621`），MCP 先判存在
  再 `to_dict()` 二次读，中间被淘汰会抛 KeyError 把整条 JSON-RPC 帧打飞 ⇒ 改 `.get()` 降级，
  判据双向验过（`.get` 版 20 passed；改回下标该条红 `KeyError: 'result'`）；
  ⑥ 三条写了没测的承诺（`task_id` 属性零使用、跳过/非 dict 节点会不会混进清单、
  交付物文件读不到时条目消失）各补一条判据。
- **本机一轮的两条红与它们的性质**：README 计数护栏红是我加了 4 条用例后忘了同步数字
  （护栏正常工作，改数即绿）；`test_executor_factory::test_parallel_speedup` 红是
  **CPU 饥饿型假红**——那一轮我并发跑了变异批次/覆盖率探针/全量（1137s vs 平时 735s），
  单独复跑 `tests/test_executor_factory.py` **33 passed / 10.9s**。没有改那条判据也没有跳过它。

### Added（2026-10-15·续49，`RunHandle.artifacts()` 持久化到交付账本）

- **要替掉的行为**：任务内存历史只留最近 100 个（`pipeline._trim_task_history`），
  被淘汰之后 `orch.get_task(id)` 返回 None，`artifacts()` 于是给出空表——**运行确实交付过东西，
  查询却说没有**。这是"空结果被读成阴性结论"的形状，不是容量问题。
- **写入侧**：`_build_delivery_record` 的每个节点多带 `artifact_keys`
  （`sorted(str(k) for k in dag_node.result or {})`）。没产出的节点给**空表**——
  "这节点跑过但没交付东西"与"不知道"必须可区分。账本本就 DONE/FAILED 都记，
  所以失败运行的产物面同样可查。节点明细本就整包序列化进 `payload_json`
  （`deliveries.py` 的表只有 run_id/pipeline/status/finished_at/duration/output_path/payload），
  所以这次**不需要列迁移**——老库直接可读，只是老行里没有这个键。
- **读取侧**：`RunHandle._ledger_record()` + `_artifacts_from(rec)`，任务不在内存时
  从 `deliveries.db` 按**同一条目形状**重建（`{"kind":"artifact","node","keys","status"}` /
  `{"kind":"deliverable","node":"","path","bytes"}`）。`to_dict()` 的淘汰分支同时补
  `pipeline` / `error` / `artifacts`，仍**不给 `result`**（账本存产物键与路径，不存整份结构化结果）。
  MCP `get_task` 与 Admin `GET /tasks/<id>` 走同一原语，因此自动继承持久性。
- **三条边界（都不猜、不造）**：① 本改动之前落账的运行没有 `artifact_keys` 列 ⇒
  它们的 `artifacts` 仍是空表，**不回填历史**；② **读路径不许建库**——先查文件在不在，
  不在就返回 None，不能因为"有人查一次产物"就凭空生成 `deliveries.db`；
  ③ 账本打不开/不是 sqlite 文件 ⇒ 捕获 `(OSError, sqlite3.Error)` 降级为空表，
  不把一条查询升级成 500 或打飞 JSON-RPC 帧。
- **判据**：`tests/test_run_handle.py` 20 → **26**（新增 `TestDurableArtifacts` 6 条：
  账本行携带 artifact_keys / 被淘汰运行重建出**同一形状**（按内容比而非顺序）/
  淘汰态 `to_dict` 走账本 / 读路径不建库 / 坏账本降级不抛 / 无产物键的节点不编造条目）。
  `loom_core/run_handle.py` 覆盖 **112/112 = 100%**。两条变异对照：**短路账本读取 ⇒ 2 条红**；
  写入侧丢掉 `artifact_keys` ⇒ **3 条红**（含写入判据本身）。
- **顺手改掉一条已过期的断言名**：`test_empty_when_task_not_in_memory` 的 docstring 写的是
  "内存里没有就不报产物"——那正是本改动移除的行为，留着会把旧限制当成契约。改名
  `test_empty_when_neither_memory_nor_ledger_has_it` 并把"空"的理由改写为"账本里没有这一行"；
  断言体不变（`_StubOrch` 无账本行，仍空）。测试会话的 `LOOM_STATE_DIR` 由 `conftest.py`
  按 PID 隔离，所以这条不会碰到真实账本。


### Added（2026-10-14·续47，§4.2 S3 节点检查器 + S2 断点续跑标记——控制台第二屏）

- **`GET /tasks/<id>/nodes/<name>` 节点检查器载荷**：一次给出 spec §4.2 S3 的四类
  明细——**artifact 结构预览**（键 + 类型 + 标量值，长文本截断，不整包回传）、
  **幂等键**（`{task_id}:{node}:{attempts}` + 是否已落 `processed_keys`；取的是
  `orch.bus._store`，另开连接会读到另一份库）、**熔断/限流态**（`orch._cb_registry`
  / `orch._rate_limiters`，从未建过 = None）、**质量门明细**（只认质量门形状的产物：
  `overall_score` + 维度 `scores` + 阈值 + 风格问题），外加**断点视图**（落点/是否
  落盘/是否续跑）。
- **"取不到就不编"**：熔断器没建过、限流器没配、质量门不是本节点产物 → 字段给
  None，抽屉如实显示"未建立"，而不是 `closed`/空对象/把普通结果当质量分
  （FP-1"显示假分"同类错误）。
- **越权与不存在同形（404）**：复用 `_scoped_task`，跨租户点他人节点与节点不存在
  不可区分。
- **S2 补齐断点续跑标记**（spec §4.2 S2 表原缺项）：dag 视图多带 `checkpoint`，
  DAG 层流顶部出角标（`断点已落盘（N 节点完成）` / 续跑时高亮）。断点视图由
  `_task_checkpoint_view` 单源产出，S2 绘图与 S3 检查器共用，不会两处漂移。
- **前端**：DAG 节点可点 → 右侧检查器抽屉（spec §4.2 统一骨架的"右侧检查器"），
  五区块 + 重试旗标；运行中任务每 3 秒跟随刷新；关闭按钮/返回列表都会停表。
- **判据**：`tests/test_node_inspector.py` 11 条真 HTTP（全字段形状含 artifact 截断 /
  幂等键格式与真 PersistentStore 的 True·False·None 三态 / 熔断限流的 None 与在场两态 /
  质量门只认质量形状 / 404 三型 / 断点三种态）+ `tests/test_dashboard_s2.py` 增补 4 条
  结构护栏；无头 Chrome 真机走查（点 writer 出五区块、点 quality_gate 出质量门明细、
  断点角标、关闭恢复）留证。
- **踩坑两条**：①`PersistentStore` 线程本地连接在 Windows 上不关会锁住临时目录
  → 判据用 pytest `tmp_path` + `close_all()`，不用 `TemporaryDirectory`；
  ②`MagicMock` 会自动造属性——不显式把 `bus._store=/_cb_registry.get=/
  `_rate_limiters.get=` 置空，"取不到就 None"的分支根本测不到。

### Changed（2026-10-13·续45，P1-D：常驻服务并发模型——stdlib HTTP/1.0 + 无线程上限，两个缺陷一起关闭）

- **HTTP/1.1 keep-alive**（`AdminHandler.protocol_version`）：旧形态是 HTTP/1.0，
  无连接复用——dashboard 5 秒轮询 5 个端点 = 每 5 秒新建 5 条 TCP + 5 条线程。
  切换的**硬前提**是每个响应都带 `Content-Length`（旧形态靠"连接关闭"界定响应体，
  漏一处客户端就永久挂起）：`_json` / `_text` / `_prometheus` / `do_OPTIONS` 403
  分支 / 静态文件五处全部补齐；**SSE 是长响应流，没有 Content-Length 可依**
  ——`_handle_stream` 入口显式 `close_connection = True`，不参与连接复用。
- **有界线程池**（新 `BoundedThreadingHTTPServer`）：accept 线程只做派发，处理
  提交给 `ThreadPoolExecutor`；**池满即 503 + `Retry-After: 1`**——不排队、不
  无限起线程。旧 `ThreadingHTTPServer` 名保留为别名（线程-per-连接的语义由有界
  版接管），`inflight` 属性可观测在途工位数。
- **socket 读/写超时**：复用基类 `StreamRequestHandler.timeout`
  （`LOOM_ADMIN_SOCKET_TIMEOUT`，默认 30s）——半开连接与空闲 keep-alive 连接
  超时后由服务端关闭并收回工位，不再永久占着一条（有界）线程。
- **两个可调旋钮**：`LOOM_ADMIN_MAX_CONCURRENCY`（默认 32）、
  `LOOM_ADMIN_SOCKET_TIMEOUT`（默认 30）；非法值回退默认不炸启动。
- **判据** `tests/test_admin_concurrency.py` 14 条（全部真 HTTP、随机端口真
  server）：同连接两请求连续成功 + `will_close` 为假 / 五处响应路径
  Content-Length 逐一体操 / 池满 503+Retry-After 且 200 数正好等于池大小 /
  503 响应体形状 / keep-alive 下"连接开着工位占着、关连接后归还"与"空闲连接
  被超时回收"两条真时序 / 半开连接被服务端关闭 / SSE 入口置位 close_connection。
- spec §3 P1 形态第一项（常驻服务）到此关闭；**保留 stdlib 不引框架**是刻意的
  （决策 A）：pip 包不背三个重依赖，LLM 请求以 IO 等待为主，线程池已足够。

### Added（2026-10-13·续46，§4.2 S2 运行详情屏——控制台第一屏，决策"先 C 后 B"的 C 落地）

- **后端透出 DAG 视图**：`GET /tasks/<id>` 新增 `dag{nodes[],layers[]}`——
  逐节点 `status` / `attempts` / `duration_ms` / `skip_reason` / `error` /
  `dependencies`，加 `execution_order` 拓扑层级。**retried 是派生态**
  （`attempts>1` 且最终 success，引擎不另存"重试态"字段）；`attempts` 缺失时用
  `task.steps` 条数兜底（池化/别名节点按 name 匹配后再回退 agent_name，避免
  节点状态全落 pending）。`skipped` 两种语义可区分：`skip_reason="condition"`
  （`when` 不成立，下游不受影响）vs 空串（依赖失败级联跳过）。
  没有 DAG 信息的任务给**空结构而不是省略键**（legacy 路径不炸前端分支）。
- **审批闸门随详情一起出**：`awaiting_approval` + `approvals` 决策流水——
  P1-C 之后"人审"在控制台有落点（此前只有 API，没有界面）。
- **前端 S2 屏**（Vanilla JS，零构建链，沿用既有 card 骨架）：任务行点击 →
  详情态（其他卡隐藏）；DAG 分层渲染（层间 ▼、并行节点同行）；四态配色 +
  `when` 跳过**虚线边框** + 重试标 + 耗时/尝试次数 + 失败原因 inline；
  审批操作条（放行/驳回，reject 可填原因，按钮 1.5s 守卫 + confirm 二次确认）；
  决策流水列表。返回列表恢复原状。
- **判据**：`tests/test_task_dag_view.py` 7 条（真 HTTP：全字段形状 / 两种跳过
  可区分 / 无 DAG 给空结构 / 层级兜底 / steps 兜底 attempts / 审批两字段），
  `tests/test_dashboard_s2.py` 13 条结构护栏（CI 无浏览器，真机渲染由无头
  Chrome 走查留证）。
- **踩坑入档**：`.card{display:flex}` 是作者样式，**压过 UA 的
  `[hidden]{display:none}`**——不写 `.card[hidden]{display:none!important}`，
  "详情态隐藏其他卡片"只是设置了属性、画面毫无变化（无头 Chrome 实测）。

### Changed（2026-10-13·续44，P1-C：配置热更新跨进程生效——P1 三条验收判据全部闭合）

- **覆写文件 `<状态目录>/config_overrides.json`**（`LOOM_STATE_DIR` 同源；revision 单调
  递增 + 点号平铺键，原子写=临时文件 + `os.replace`，无残留 .tmp）。级联位置在
  环境变量**之后**——与既往"运行时 `set` 胜过一切"的内存语义一致；reload 不再丢
  运行时配置（覆写层随每次 `_load` 重新生效）。
- **两个生效路径**：新进程**构造时**直接读到；常驻进程**轮询时**——
  `poll_overlay()` 检出 mtime/revision 变化即热加载返回 True。覆写文件损坏 =
  保留旧配置 + `last_overlay_error` + 记住坏 mtime 不反复重解析（与主配置文件
  既有"损坏不重解析"语义对齐）。
- **接线**：`POST /api/config` 改走 `set_persisted`（响应带 `revision`/`propagated`，
  写盘失败带 `propagation_error` 且**仅本进程生效如实上报，不静默**）；
  `POST /api/config/reload` 通知 Agent 后 `publish_reload()` 广播 revision；
  worker `run_forever` 每轮 `refresh_config()`（orchestrator 未构建时不提前拉
  Agent）；Admin API 三个请求入口鉴权后 `_sync_shared_config()`。
  `PipelineOrchestrator.notify_config_update()` 从 admin_api 抽出，两条路径共用。
- **判据** `tests/test_config_overlay.py` 23 条：覆写文件契约（平铺键/原子写/增量
  更新）、跨实例（构造见值/poll 感知/env 不敌覆写/reload 保留/publish 广播/
  内存 set 不传播）、损坏语义、写盘失败不静默（覆写路径被占成目录即报错）、
  **真跨进程**（父子两进程共享状态目录，子进程轮询到父进程写入，同目录铁证）、
  Admin API / worker 消费端接线。

### Changed（2026-10-12·续43，P1-B：状态后端可换——TaskQueue 接 SQLite/Postgres 双后端）

- **`loom_core/db_backend.py` 新模块**：方言差只收敛在**两处翻译**——占位符
  `?`→`%s`（psycopg）、`INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`+`REAL`→
  `DOUBLE PRECISION`；业务侧（TaskQueue）SQL 字符串零分支。优先级：
  显式 DSN > `LOOM_DATABASE_URL`（旧名回退）> SQLite 默认路径。
- **并发契约同义**：`rowcount` 归属 / 幂等 insert / lease 回收在两个后端上
  语义一致；WAL 忙等仍是 SQLite 特供，PG 靠 MVCC，不抄 pragma。
- **真 PG 判据 `tests/test_state_backend.py` 18 条**：DSN 单元、SQLite pool
  语义（thread-local 自愈/跨线程 close 安全）、**spec 验收原文两实例共享一库**——
  两进程领取互斥不重不漏 + 两个真 TaskWorker 进程共享 PG 把 test_pipeline 跑完。
  CI test matrix 挂 postgres:16-alpine service + `LOOM_TEST_PG_URL`；
  本地跳过条件：未设 DSN 或未装 psycopg。
- **诚实边界**：只换**队列**；消息总线持久层 / cost / quality feedback 仍是
  SQLite（下一批）。`pip install nexusloom[postgres]` 才装 psycopg。

### Changed（2026-10-12·续42，P1-A：token 身份与多租户作用域隔离）

- **`tokens.yaml` 多凭证 → Principal**（新模块 `loom_core/tokens.py`）：密钥只经
  `token_env`（环境变量）或 `token_sha256` 注入，文件不落明文；`admin: true` 为
  跨租户运维位。**`ADMIN_API_KEY` 继续有效并解析为 admin**（旧部署零改动）；
  未放置 tokens.yaml 时行为与既往完全一致（本机信任/单 key，139 条既有鉴权
  回归全绿）。**启用身份后回环访问同样要求凭证**；结构性配置错误拒绝启动；
  缺环境变量的条目不可用且启动告警（空密钥静默放行是致命洞，不做）。
- **任务作用域**：`PipelineTask.owner` 贯穿执行链（CLI 空串 / API=Principal.id /
  cron=`trigger:<名>` / webhook=`webhook:<名>` / MCP=`mcp`）；task_queue 加
  `principal` 列（幂等迁移），worker 回执带回；断点往返不丢归属。非 admin 的
  list/get/cancel/pause/resume/approve/reject/rerun 全部受作用域约束，
  **越权与"不存在"同形（404）**——不泄露存在性。
- **成本作用域**：`GET /api/cost` 非 admin 只统计自己名下任务（`summary(task_ids=...)`，
  `scope: principal` 标记）；admin 全量语义不变。
- **判据** `tests/test_identity_tokens.py` 28 条：TokenStore 单元（双源/坏 hex/重复
  id/缺密钥不可用）、鉴权矩阵（真 HTTP：回环要凭证/老 key 兼容/非法配置拒启/旧世界
  回归）、**spec §5.4 验收原文——两个 token 互不可见对方任务（真 HTTP，非 mock）**
  + 六类越权操作 404 且不触达后端、owner 透传链（run_plan/断点/队列/worker）、
  成本作用域。附带修一处配置静默：`token_sha256: 0…0` 被 YAML 解析为 int 0，
  `or ""` 会吞成"未提供"——取值改为 None 判定，配置错误必须报出来。

### Changed（2026-10-12·续41，生态轴 0→1：首个真第三方插件例证）

- **`Levango7/loom-plugin-sqlite`**（外部仓、外部发行）：只读 SQLite 取数 Agent
  （`sqlite_query`）——补“本地数据库 → 报告”取数面；只读双保险（`mode=ro` +
  authorizer）、单语句 SELECT/WITH 白名单、截断/空集/错误全部如实语义。
- **本仓零改动闭环**（spec §3 P0 判据 2 达标、§2.1 第三方实测 0→1）：
  纯净 venv 装 **PyPI 正式版**宿主（3.10.0 与 3.11.0 各一遍）→
  `loom plugin list` 列出 `sqlite_query [entry_point:sqlite_query]` →
  报表管线真跑落盘 → 审批版管线（approval 闸门）停住/放行/驳回双向实测；
  外部仓 CI（GitHub runner，双宿主版本矩阵）全绿（run 38271327226 / 38271446597）。
- **过渡期三坑成范式**（外部仓 README 留档，第三方作者直接照抄）：
  ①宿主 import 根名 3.11.0 起 `pipeline_core → loom_core` → 插件 try/except 双名；
  ②entry 组名 3.10.x 只认 `doc_pipeline.agents`（新组 3.11.0 才生效）→ 双组声明，
  宿主按注册名去重不重复；③纯取数管线的输入文件仍需首行可提取主题（引擎契约），
  给一行说明文字即可。
- 附带发现（宿主侧潜在改进项，未改）：嵌入方裸构造 `PipelineOrchestrator()` 时
  `agents_dir` 默认相对 cwd——在宿主之外的目录嵌入时应用
  `agents_dir=str(loom_core.paths.agents_dir())`（CLI 入口内部已如此；已写进外部仓脚本注释）。

## [3.11.0] - 2026-10-12

命名收官版：import 根包 `pipeline_core` → `loom_core`、领域包 `docpipeline` → `loom_doc`，
插件 entry_points group 迁 `loom.agents` / `loom.packs`——spec §7.1 命名系统全量落地。
旧名全部保留为兼容 shim（meta-path 别名、identity 保真、MAOP 零改动），属向后兼容变更；
随包附 v3.10.0 发布门禁修复（verify_dist 嵌套包路径判定）。

### Changed（2026-10-12·续40，命名收官·第二段：import 根包 pipeline_core → loom_core）

- **`pipeline_core` → `loom_core`**（spec §7.1 命名系统最后一行落地）：git mv
  保历史全迁（引擎 46 模块）；仓内 191 文件机械迁移（src/tests/scripts/spike/
  pyproject/CI/docs 一次到位）。旧名保留为**兼容 shim**——meta-path 别名把
  `pipeline_core.X` 映射到 `loom_core.X` 的**同一模块对象**（identity 保真：
  枚举比较 / isinstance / patch / 事件钩子全部照常），首导旧根名记一次
  DeprecationWarning；**`pipeline_core/__init__.py` 文件保留是刻意的**——MAOP
  的 `_resolve_doc_pipeline_root()` 用它判项目根、其两条运行时 import 经 shim
  命中同一模块对象，**它侧零改动**（"需与 MAOP lockstep"的条件以零改动方案
  满足）。
- **全链更新**：pyproject（packages 双 shim 化 + coverage 口径）、CI 三检
  （py_compile/mypy/bandit 覆盖 loom_core + 两个 shim）、verify_dist（PACKAGES
  加 shim；**安装态新增断言：`import pipeline_core is loom_core`**——发布门禁
  亲自验证兼容承诺；它刻意 import 旧名，由反向守卫豁免并注明）。
- **判据**：`tests/test_naming_compat.py` 新增 `TestPipelineCoreShim` 2 条
  （MAOP 探径契约 / identity+version+枚举同一 + warn-once，子进程实证）；
  反向守卫扩展到双旧名（docpipeline + pipeline_core）。全量 **2677 passed / 0 failed**
  （一次全绿）。
- 命名系统（§7.1）至此**全部落地**：产品名/仓名/发行名/CLI/env/MCP/观测/
  import 根包/领域包/插件 group；仅剩"API 原语 RunHandle"一行是功能设计项、
  不属改名。

### Changed（2026-10-12·续39，命名收官·第一段：领域包 loom_doc + 插件 group loom.*）

- **`docpipeline` → `loom_doc`**（spec §7.1 命名系统收尾第一段）：git mv 保历史
  全迁（renderer / ingest / document_enhancer / degradation 等 5 文件）；旧名保留
  为**兼容 shim**——meta-path 别名把 `docpipeline.X` 映射到 `loom_doc.X` 的**同一
  模块对象**（identity 保真：isinstance/patch/注册表照常工作；转发文件方案会造出
  两份模块，本仓在打桩入口上栽过），首导旧根名记一次 DeprecationWarning。
  **`docpipeline/__init__.py` 文件保留**：MAOP 的 `_resolve_doc_pipeline_root()`
  正是用它判项目根，属对外契约。shim 进 wheel（packages）、发布门禁
  （verify_dist PACKAGES）与 CI 三检范围。
- **entry_points group → `loom.agents` / `loom.packs`**：loader 与 pack_manifest
  先读新 group、旧 group（`doc_pipeline.*`）过渡期回退可读（同名新组优先），
  旧 group 命中记一条迁移提示；`examples/plugin-hello` 模板与全部文档切到新名。
- **原 §7.1 目标名 `loom_pack_docgen` 修正**：该名系 pack 拆分（2026-10-12）前的
  写法；拆分后该层被核心 agents（renderer/ingest/knowledge_base）与 pack
  （writer/quality_gate）双方共用，非 pack 私有——按实际命名为 `loom_doc`
  （spec §7.1 已记）。
- **MAOP 兼容评估（"lockstep" 的零改动方案）**：其运行时只做两件事——探
  `docpipeline/__init__.py` 判根 + 两条 import（`PipelineOrchestrator`、
  `event_hook`）——shim 全覆盖，它侧零改动；其 4 条源码级静态检查读的是旧路径
  （10-11 改名后早已红），不因本批恶化（§9.1 记账延续）。
- **判据** `tests/test_naming_compat.py` 8 条：shim identity（子进程实证）/
  warn-once / MAOP 探径文件存在 / 反向守卫（仓内源码不得 import 旧名）/
  group 双读（新组优先 + 旧组迁移提示）。
- 另修 `test_release_readiness` 的宿主依赖口径（判据还在等 `doc-pipeline`
  前缀，宿主发行名 2026-10-11 起已是 `nexusloom`）。

## [3.10.0] - 2026-10-12

两个特性批合流：CLI 动词收敛（续37）与审批闸门（续38，spec §9 决策 C）。
流水线自此有「取数 → 核查 → 待批 → 交付」的人工干预点；CLI 收敛为五动词
`run/plan/lock/pack/plugin`；env 前缀迁移 `LOOM_*`（旧名 `DOC_PIPELINE_*`
过渡期回退可读）。spec §9 至此零未决项。附带修复勘察时挖出的 checkpoint
save/load 落点不同源真 bug。

### Changed（2026-10-12·续38，§9 两项拍板落地：A=perf-regression 进 required + C=审批闸门机制批）

- **决策 A 执行**（perf-regression 进 required）：分支保护 required 由 5 项扩为
  **6 项**（`test 3.11–3.14` + `docker` + `perf-regression`，`strict=true`；
  2026-10-12 API 写入并读回验证）。依据=近 42 次连续绿（PR 18 + main 24）
  + 波动带机制 + 22 秒级耗时；抖动处置=重跑该 job 或 `workflow_dispatch` 刷
  基线。spec §5.5/§9.2 落档。
- **决策 C 执行**（审批闸门机制先行最小闭环）：声明式 `approval` 节点——
  跑到闸门层前置 PAUSED（复用暂停边界的等待/checkpoint/取消同一路径），
  `POST /tasks/<id>/approve|reject`（reject=终止含原因，不做分支），
  审批等待期 `resume` 被拒（直通等于绕过审批），决策落 `audit/approvals.jsonl`
  （任务/节点/决定/note/时间/审批人标识），`task.approved/rejected` 事件；
  闸门声明进节点配置指纹（改 message 必须 `loom lock` 重写）；
  `when` 不成立的闸门按条件跳过。真跑判据见 `tests/test_approval_gate.py`
  （解析/清册/执行/条件闸门/HTTP+审计五层）。边界（P1 余量）：无分支驳回、
  无通知、无控制台、无多级审批/超时；审批人=客户端地址+凭证指纹。
- **附带修复（勘察时挖出的真 bug）**：`checkpoint_manager.save` 落点
  与 `load/remove` 不同源——save 写 `task.checkpoint_file`（orchestrator 侧
  字段，受 `config.checkpoint.dir` 覆盖、可为相对路径），load 用 manager
  自己的目录；表现为"断点保存成功但 resume 读不到"且随 cwd 漂移。
  修复：save 统一用 manager 目录并在成功后回写 `task.checkpoint_file` 真实
  路径；判据 `TestCheckpointSaveLoadSameSource`（落点/回写/往返/删除同源）。
- 另修 ruff UP037（本批新增静态方法的冗余引号，CI lint 早挂根因）。

### Changed（2026-10-12·续37，CLI 动词收敛 + env 前缀 LOOM_* + MCP/观测标识：§7.3 尾款三件落地）

- **CLI 动词收敛**（spec §5.3 v1 目标）：动词层 `run / plan / lock / pack /
  plugin` 落地（前缀翻译；缺省动词 = run，裸输入 `loom input.md` 与既有
  `python run.py input.md …` 形态全部照旧）。旧散装 flag 摘除：
  `--plan` / `--dry-run`（→ `loom plan`）、`--write-lock`（→ `loom lock`，
  语义改为**纯维护**：写完锁即退出，不再顺手执行）、`--list-agents`
  （→ `loom plugin list`）；新增 `loom pack list`（清册）与 `loom pack check`
  （自检，等价 `--check`）。`--enhance` / `--enhance-output` / `--no-search`
  摘除（库层 `docpipeline.document_enhancer` 保留并被单测覆盖）；dry-run 的
  fire-and-forget 提交路径删除（提交即等待）。判据 `tests/test_cli_verbs.py`
  26 条：翻译层（纯函数）/ 摘除（七个旧 flag 必被拒，in-process + 一条真 CLI）/
  门面（`--help` 列出五动词）/ 真 CLI 冒烟（pack list / plugin list / plan /
  lock 沙盒实证）。
- **env 前缀 `LOOM_*`**：`LOOM_ROOT`、`LOOM_STATE_DIR`、`LOOM_VERSIONS_DIR`、
  `LOOM_WEBHOOKS_FILE` 为新名；**旧名 `DOC_PIPELINE_*` 过渡期回退可读**——
  MAOP 侧按 §7.3 第 6 步约定设置的 `DOC_PIPELINE_ROOT` 继续生效（它侧零改动
  的承诺不因改名而破）；旧名首次命中记一次迁移 warning（高频调用不复读）。
  新模块 `pipeline_core/envs.py` 收口双读；paths / state_paths / webhooks /
  admin_api 全部接入。判据 `tests/test_env_prefix.py` 15 条（优先序 / 回退 /
  warning-once / 常量名 / MCP 与观测标识）。
- **MCP/观测标识**：`serverInfo.name` `doc-pipeline` → `loom`
  （`mcp_server.py` 的 `SERVER_NAME`，docstring 示例同步）；指标 namespace
  `docpipeline` → `loom`（`loom_requests` 这类）；结构化日志文件前缀
  `loom_YYYYMMDD.jsonl`。
- **判据面**：存量测试同步迁移（test_run / test_run_ext / test_observability /
  test_admin_api_ext / test_mcp_server + 12 个测试文件的 env 名切换为
  `LOOM_*`，旧名回退由 test_env_prefix 专门覆盖）。
- **刻意保留（未改名，留档）**：import 根包 `pipeline_core` / `docpipeline`
  （§7.1 第一阶段保持不动，且被 MAOP 硬绑）；entry_points group
  `doc_pipeline.agents` / `doc_pipeline.packs`（第三方插件出现前不动——
  动手前是零成本窗口，动手后是破坏性变更）。

### Changed（2026-10-12·续36，§9 未决项收口：五闭二备选 + 遗留提交复位）

- **§9 收口**：闭 #5（FP-2 的 CI 真实验证——ubuntu runner 上多轮全绿重取：
  test 3.11–3.14 + docker + perf-regression；`--check` 退出码与 pip-audit
  进程健康均为硬依赖）；闭 #6（README 文档索引表已加 `product-spec.md` 一行）。
  #2 收窄：`enforce_admins` 子项已闭——2026-10-08 已翻 true，2026-10-12 实测
  确认（§5.5 旧文写 `false` 属过期口径）；剩余待拍板项 =
  `perf-regression` 是否纳入 required，候选 A/B/C 与推荐见 spec §9「决策备选·2」。
- **§9 #3 勘察 + 备选**：人审/approval 全仓 0 命中，但半个机制已在
  （`TaskStatus.PAUSED` + 边界 checkpoint + 执行循环阻塞等待 +
  `POST /tasks/<id>/pause|resume`）；候选 A（留 P1）/ B（现在完整做）/
  C（机制先行、最小闭环）与推荐见 spec §9「决策备选·3」。
- **遗留提交复位（10-08 f47a1b9）**：共享检出上停了 3 天的未推 docs 提交
  （续17 台账改编）按此处置，它要留痕的两件事以最新事实落回本文：
  **① 共享树整文件 `git add` 会搭车**——那段台账文字落盘后 1 分钟内被并行线
  的 `git add CHANGELOG.md` 整体带走、随 PR #1 进 main；共享检出上
  `git add <整份文件>` 会捎走另一条线的在途文字，反向亦然（分工区 worktree
  即该教训的正解，此后八连发全部照此执行）。
  **② "直推被拦"的实测已拿到**——该提交当时只能记"配置已生效、拦阻形态待
  实测（GH006）"；后续批次直推确实被拒（`5 of 5 required status checks are
  expected`），悬着的这句话收口。主检出已复位至 origin/main，原提交 patch
  已在仓外留档（需要时可从 reflog 或存档恢复）。
- **本批为文档口径批**：不动引擎代码，CI 以仓库门禁为准。

### Changed（2026-10-12·续35，pack-docgen 物理拆分：spec §5.2/§7.3 最后一步）

- **文件移动**（git mv 保历史）：5 个文档件（writer/quality_gate/checker/
  layout/fact_checker）→ `packs/docgen/agents/`；docgen 系 6 条 YAML + 锁 +
  `_quality-tail` → `packs/docgen/pipelines/`；2 个质量档案 →
  `packs/docgen/quality/`；2 个 prompt 模板 → `packs/docgen/prompts/`；
  新增 `packs/docgen/pack.yaml` manifest（agents / pipelines /
  quality_profiles 清册）。拆出判据达成：删掉 `packs/` 目录引擎与其余
  13 条流水线照常工作（文档件 import 面已清零）。
- **引擎改为多目录发现**（引擎不 import pack，只经清册）：
  `pack_manifest` 新增物理 pack 层（`physical_packs`/`agent_sources`/
  `pipeline_dirs`/`quality_dirs`/`prompts_dirs`）；`agent_loader` 按
  `(目录, 模块前缀)` 发现，模块键 = 直接 import 路径
  (`packs.docgen.agents.writer`)，保证测试 import/patch 与 loader 命中同一
  模块对象；`scheduler._pipeline_path`/`installed_pipelines` 走多目录 union
  （锁与 YAML 同目录优先，`--write-lock` 把 pack 锁写回 pack 目录）；
  admin_api/mcp_server/worker/triggers 同步 union；`quality_gate`/
  `writer` 资源目录改走多目录解析；`bootstrap --check` 报物理 pack 清册
  （「pack docgen（5 agents / 6 流水线），13 条内置流水线；19 个 agent 可用」）。
- **本批最大坑**：`_schema_for_agent` 只扫核心 `agents/`——pack 里
  writer/quality_gate/checker 的 `CONFIG_SCHEMA` 找不到，schema 默认值不参与
  config → config_hash 全漂移 → 52 条锁判据齐红。修复 = 同一条多目录链。
- **打包面**：pyproject `packages` += packs 树；数据文件随 `packs.docgen`
  package-data 进 wheel（data-files 的 `pipelines/quality`/`prompts` 死键
  删除——glob 零命中是静默的）；`verify_dist` REQUIRED_DATA/PACKAGES 更新；
  CI `py_compile`/`mypy`/`bandit`/coverage 范围含 packs。
- **分层守卫**（`test_layering` 新增 2 条）：引擎不 import packs；pack
  agents 不 import 核心 agents。`--check` 实测 0 ERROR。
- **判据**：全量回归清零（含 test_lockfile/test_pack_manifest/
  test_pack_suite/test_installable_product/test_release_readiness 等新口径
  更新）；另修正一处真 bug——`installed_pipelines` 误引
  `paths.pipeline_dirs`（全量跑红才暴露）。

### Changed（2026-10-11·续34，单发行物整合：artesian 并入 + 插件内置化）

- **决策**：用户拍板"整合成一个项目，要有新的命名（以前决策过：发行名
  `nexus-loom`，spec §7.1）"。此前 PyPI 发布准备提出了 8 个发行物
  （artesian + doc-pipeline + 六插件），被否——那是把一个产品拆成了八个包。
- **artesian 并回主仓**：它只有 doc-pipeline 一个消费方，独立发行只带来
  "每次库改动手改 sha"的成本（PR #17 为此建过 EXTERNAL 登记表 + 发布火车，
  随并入一并撤销）。`src/artesian` → 顶层包 `artesian/`（import 名不变，
  零代码改动），其 8 个测试文件并入 `tests/artesian/`（307 条底座判据随库走，
  并入即绿）；它自己的 test_doc_consistency / test_verify_dist 管的是 artesian
  仓的 README 计数与发布门禁，不随迁。requirements.txt 删 git sha pin 行。
  分层守卫新增：**artesian 底座不得 import 主仓包**（并入 != 允许成环）。
- **五支插件内置化**：keyword_extract / readability / link_check /
  fact_coverage / tldr 从 examples/plugin-* 移入 `agents/`，与其它内置件同
  路径发现/注册/执行（glob），补 SANDBOX_TRUSTED 声明对齐内置件口径。
  examples/ 只留 plugin-hello 作**外部作者模板**（第三方插件机制的参照实现
  与真 venv E2E 证明）。
- **诚实口径回改**：spec §2 扩展来源轴、§2.1 验收线第 2 条、§5.2 插件发现行
  ——原"第三方插件实测数 6"是自家内置件充数，回归 **0**（机制达标、
  真实生态为 0，由真实外部包出现时点亮）；组合数 5544 → **924**
  （11×6×7×2，相对基线 18 为 51x，仍越过 20x 线）。
- **判据** `tests/test_plugins_ecosystem.py` 重做（9 条）：五支内置件的
  发现/注册/执行、SANDBOX_TRUSTED 口径、AGENT_NAME 无冲突、plugin-hello
  模板形状、examples/ 无回魂目录、诚实计数=0。
- test_layering：docpipeline 的 FIRST_PARTY 加 artesian；
  `test_local_library_has_reproducible_install_source` 删除（无外部库可管）。


### Added（2026-10-11·续33，PyPI 发布准备：门禁脚本 + 发布链 + 三个真缺陷）

- **背景**：PyPI 方向获批（本机直连 PyPI 实测通畅，相关名称均空闲）。
  发布链是一列火车：artesian（独立仓，release.yml 已就绪）→ doc-pipeline
  → 六个插件。本批把 doc-pipeline 侧的发布准备做完并建门禁，三个真缺陷
  是门禁逼出来的：
  1. **wheel 元数据缺 `artesian` 依赖**（Requires-Dist 里没有它——它只在
     requirements.txt 里按 git sha pin）。未上 PyPI 前不能写版本约束
     （写了 pip install 直接失败），做成 EXTERNAL 登记表显式认领，
     上架后一行切换；
  2. **data-files 子目录静默丢失**：`pipelines/quality/*.yaml` 写在
     `pipelines` 键的 glob 里时 setuptools 只匹配顶层——装出来的引擎没有
     quality profile 与 prompt 模板（`--check` 抓不到，quality_gate 现形）。
     修复：子目录单列成独立的 data-files 键；
  3. **quality_gate/writer 的资源目录是相对包路径**（`Path(__file__).parent.parent
     / "pipelines"`），安装态指向 site-packages 的空壳。修复：改走
     `pipeline_core.paths.pipelines_dir()`（源码树/安装态两形态都能命中）。
- **发布门禁 `scripts/verify_dist.py`**（自 artesian 同名脚本适配）：卡四件事
  ——wheel/sdist 数据文件齐全、依赖闭合（源码 import 的第三方包要么在
  Requires-Dist 要么在 EXTERNAL 登记表）、版本三处一致、干净 venv 安装态
  真跑（流水线清册非空 + quality profile 可加载）。本机实测：修复前抓红
  三处，修复后全绿（含 --installed 安装态）。
- **发布工作流**：`.github/workflows/release.yml` 追加 `pypi-verify` +
  `pypi-publish` 两个 job（与 artesian 同标准：构建→verify_dist→twine
  check --strict→干净 venv 真跑→Trusted Publishing/OIDC 上传；只在
  GitHub Release published 时真上传，dispatch 只干跑）。
- **插件元数据**：六个 `examples/plugin-*` 补齐 license/readme/classifiers
  与对宿主 `doc-pipeline>=3.9.1` 的依赖声明；12 个发行物（wheel+sdist ×6）
  `twine check --strict` 全过。
- **判据** `tests/test_release_readiness.py` 16 条：依赖闭合（与门禁同实现）、
  data-files 子目录覆盖与 glob 零命中防护、插件元数据完整性与合法 trove
  classifier、release.yml 的 PyPI 链存在且 publish 走 OIDC。
- **README**：新增「发布（PyPI）」节，写明三级发布顺序与各自的一次性配置。


### Changed（2026-10-10·续32，命名清理第一步：banner/CLI 文案去领域味）

- **背景**：product-spec §7.3 迁移动作序列第 7 步的第一小步「banner/CLI
  文案」——引擎是领域无关的工作流运行时（docgen 只是第一个 pack，§1），
  CLI 门面不该点名具体领域。验收判据原文：「`--help` 里不再出现
  『文档生成流水线』字样」。
- **改动**：`run.py` 三处（模块 docstring / banner / argparse description）
  改为「Agent 工作流引擎」。动词收敛（`run / plan / lock / pack / plugin`）
  与 `--enhance` 摘除是 breaking 变更，仍待后续小步。
- **判据** `tests/test_cli_wording.py` 2 条：run.py 源码静态零命中（钉住
  banner 与 description 的所有写入点）+ 真跑 `--help` 用户可见面零命中。
  反向验证：banner 改回旧文案，静态判据即红（`--help` 不经过 main，所以
  banner 字样只有静态判据能抓——两层判据各管一面）。
- **spec**：§5.3 CLI 行、§7.3 第 7 步标注第一步完成。


### Added（2026-10-10·续31，交付形态补缺：结构化 JSON 落库 + HTML 站点 + renderer html）

- **背景**：product-spec §2.1 验收线第 4 条原文要求五种交付形态
  「md / docx / pdf / 结构化 JSON 落库 / HTML 站点」，此前后两种不存在：
  结构化结果只有 checkpoint 里的诊断报告（非交付物、查不了），HTML 只在
  `run.py --export` 有一把单页转换（进不了流水线声明）。
- **交付账本 `pipeline_core/deliveries.py`**：每轮 run_plan 收尾（**含失败**）
  向 `deliveries.db`（SQLite WAL，跟随 DOC_PIPELINE_STATE_DIR 隔离）记一行
  完整结构化记录——run/pipeline/plan_id/起止与耗时/output_path/error +
  **节点级状态/尝试次数/耗时**。`get(run_id)` / `list(limit)` 读回。
  收尾钩子永不外抛（交付失败只记 warning，不把运行判红）；账本的意义就是
  失败也可查，所以失败运行也落账、但不写交付文件。
- **声明式交付 `pipeline.deliver`**（YAML pipeline 段，不进 topology/lock）：
  - `json: <path>` —— DONE 时把与账本同形状的结构化记录写成 JSON 文件；
  - `site: <dir>` —— DONE 且有产物时构建静态站点（index 导航 + 产物页 +
    返回链接，slug 强制 ASCII 文件名）。
- **引擎交付原语 `pipeline_core/html_export.py`**：Markdown → HTML 字符串
  与站点构建的唯一实现（纯标准库、离线）；`scripts/format_converter` 的
  html 路径改为委托它（消重 ~150 行，文件入出 API 不变，23 条既有判据全绿）。
- **renderer `html` 后端**：`docpipeline/renderer.py` 新增 html（不依赖可选包、
  永远可用）；`formats: ["html"]` 从此进得了流水线声明。交付形态 5 → 7。
- **测试** `tests/test_delivery_forms.py`（9 条）：账本三判据（读回一致 /
  幂等覆盖 / 缺 run_id 拒绝）+ 引擎原语两条 + renderer 两条 +
  **真 E2E 两条**——一条声明三交付物真跑（html / deliver.json / 账本行 /
  站点四者互相一致），一条失败路径（账本必记 failed、不写交付文件）。
  反向验证：摘掉收尾钩子，两条 E2E 即红。
- **spec**：§2 交付形态轴 5→7、§2.1 第 4 条标注达标、组合数 3960 → **5544**
  （11×6×7×12，相对基线 18 为 308x）。


### Added（2026-10-10·续30，第三方插件生态面补足至 6 个真 pip 包）

- **背景**：product-spec §2.1 验收线第 2 条要求「第三方插件 ≥5，由**外部 pip
  包**提供 Agent，本仓零改动即被发现/注册/执行」。此前只有 `plugin-hello`
  一个示例包（实测数 1，离 5 差 4），且没有判据覆盖「整生态面」。
- **新增五个插件包**（`examples/plugin-*`，每个都是独立 pip 发行物：自己的
  `pyproject.toml` + `entry_points`）：

  | 包 | Agent | 用途 |
  |---|---|---|
  | plugin-keyword-extract | `keyword_extract` | 中英混合关键词抽取（词频 + 停用词） |
  | plugin-readability | `readability` | 可读性维度：平均句长 / 长句占比 |
  | plugin-link-check | `link_check` | 链接清单抽取（刻意**不**抓取 URL，保持离线可复现） |
  | plugin-fact-coverage | `fact_coverage` | 证据密度：数字 / 代码块 / 表格 / 链接占比 |
  | plugin-tldr | `tldr` | 离线确定性摘要（标题 + 每节首句，无 LLM） |

  全部纯标准库、离线、逐字可复现；与内置 `agents/*.py` 同构（模块级
  `AGENT_NAME` + `BaseAgent` 子类），且**不**声明 `SANDBOX_TRUSTED`。
- **判据**（`tests/test_plugins_ecosystem.py`）：
  - 快判据 18 条：六个包的 entry_points group / 模块路径 / `AGENT_NAME` /
    AST 安全检查逐包钉住；
  - 真判据 1 条：建真 venv（`--system-site-packages`）、六个包全部
    `pip install`、跑发现/注册/执行探针，逐个断言被 `discover()` 列出、
    `register()` 注册、`handle()` 真跑成功，来源标记 `entry_point:<name>`；
    数量不足 5 即红。本机实测 6/6 全过。
- **文档**：`examples/README.md` 生态总览（六包表 + 契约 + 试法 + 判据分工），
  每个包目录各有一份 README；主 README 插件段从「一个示例」改为「六个包」。
- **spec 口径**：§2 扩展来源轴与 §5.2 插件发现行的第三方插件实测数 0 → **6**；
  §1.4 诚实短板同步；§2 组合数结论改为四条验收线全部成立（插件轴补上后
  组合数再乘一档至 3960）。


### Changed（2026-10-09·续29，pack 拆包：`--check` 解绑 + `pip install .` 可用）

- **背景**：product-spec §3 P0 判据 1「干净 venv 装完后 `--check` 不红」此前
  被两件事同时破坏，本批一次修掉：
  1. `bootstrap.py` 把 docgen 这一个 pack 的件写死为必需（三个 key_files）
     外加开发目录 `tests/`——装上别的 pack 或只装引擎时，这些行与实际能否
     运行无关地红（实测旧态：26 OK / 1 WARN / 1 ERROR，红的就是 docgen.yaml）；
  2. `pipelines/*.yaml`、`quality/`、`prompts/`、`scripts/checker_rules.yaml`
     不在任何 wheel 数据里——`pip install .` 装出一个**没有流水线的引擎**。
- **新增 `pipeline_core/pack_manifest.py`**：引擎回答"装了什么"的唯一入口。
  pack 数 = 非 `_` 前缀的流水线；agent 名按模块级 `AGENT_NAME` 派生（不是
  文件 stem，14 个里 7 个不同名，按文件名比会产生假缺失）；`call` 节点不算
  agent，但会追溯它指向的子流水线（否则 `quality_tail` 被误报缺失）。
- **新增 `pipeline_core/paths.py`**：资源路径收口。`pipelines/` 的位置此前由
  scheduler / mcp_server / admin_api / worker / run.py 五处各自推导，现在统一
  走这里；解析顺序为 env `DOC_PIPELINE_ROOT` → 源码树 → 安装态。安装态落点
  是实测出来的：`[tool.setuptools.data-files]` 装到 **`sys.prefix/pipelines`**
  （不是 site-packages），且 wheel 会先建一个**空** `site-packages/pipelines`
  目录——只判 `is_dir()` 会被它骗过（实测踩过），判据改成"有 yaml 才算命中"。
- **`--check` 现在按清册报数**：`pack 清册`（几条流水线可跑、几个 agent 可用）
  + `agent 齐备性`（引用不到的 agent 逐名列）。装什么报什么，没装不红。
- **打包修复**：`pyproject.toml` 的 `data-files` 增加 `pipelines`（yaml / lock /
  quality / prompts 四类），`package-data` 增加 `scripts/checker_rules.yaml`。
- **真安装验证**（新判据 `tests/test_installable_product.py`）：真打 wheel →
  真 venv（`--system-site-packages`）→ 真装 → 从中立 cwd 跑
  `run_startup_check()`，断言零 ERROR、pack 清册 > 0、无 agent 缺失。
  本机实测：23 条 pack / 14 agents / 0 红。
- **测试**：新增 `tests/test_pack_manifest.py` 8 条（清册与 scheduler 同源、
  片段不算 pack、AGENT_NAME 派生、call 不算缺失且其子流水线 agent 进清册、
  AST 不 import、拼错 agent 名必被抓）+ `tests/test_installable_product.py`
  4 条。反向验证：把派生改回文件 stem，相关判据转红。
- README 计数 2259 → 2266。

### Fixed（2026-10-09·续28，SQLite 并发首建竞态：WAL 切换重试 + 幂等加列）

- **背景**：main 侧 CI 的 `test (3.14)` 腿在全量负载下变红，失败点是
  `test_two_instances_one_queue` 的 `e2e-0 终态是 failed: database is locked`。
  本机探针（两进程 × 80 轮同建四库）复现两个**首建期**竞态：
  - `PRAGMA journal_mode=WAL` 切模式需要短暂独占锁，SQLite 明示此时**不走
    busy handler**，而是自带一个约 0.45s 的内部重试窗口；竞争（两 worker
    进程几乎同时首建同一批库）下窗口耗尽即抛 "database is locked"；
  - `ALTER TABLE ... ADD COLUMN owner_pid` 的 check-then-act：两进程都判
    "列不存在"、都执行 ALTER，后到者拿 "duplicate column name"。
  两处都只在"两个进程同时第一次初始化同一批库"时现形，单进程测试与
  隔离跑都看不见（探针实测 80 轮命中 4 次）。
- **修复**：
  - 新增 `pipeline_core/sqlite_util.py`：`enable_wal`（锁类错误退避重试，
    非锁类错误立即抛，耗尽后仍抛——"没切成 WAL"必须是可见故障）+
    `add_column_if_missing`（幂等加列，`duplicate column` 视为目标已达成）。
  - 四处 SQLite 首建全部接入：`task_queue` / `cost_tracker` /
    `quality_feedback` / `message_store`。
  - 顺带修一条自身测试污染：新判据最初用 `os.environ.pop` 还原状态目录，
    会把 conftest 的会话级 `DOC_PIPELINE_STATE_DIR` 一并删掉，把
    `test_state_isolation` 抓红（全量实测暴露）——改为 `monkeypatch.setenv`
    自动还原。
- **测试**：新增 `tests/test_sqlite_concurrent_init.py` 9 条——
  - 两进程同 state 目录并发首建四库 ×12 轮（真 subprocess、真初始化路径），
    断言零异常；另跑"建完之后真能读写"（不只是没抛异常）；
  - 确定性持锁判据：`BEGIN EXCLUSIVE` 持锁 1.2s（> SQLite 0.45s 内部窗口）
    时旧写法必然抛、`enable_wal` 必然等到锁释放后切成功；护栏正例先证明
    该场景能把旧写法打红（否则绿是空转）；
  - 助手行为面：锁类重试次数 / 耗尽后抛 / 非锁类立即抛 / 幂等加列容忍
    duplicate / 其他 OperationalError 照抛。
  - 反向验证：把 `enable_wal` 改回直连 PRAGMA，确定性判据与重试判据双双转红。
- 本机全量：**2259 passed, 1 skipped, 6 deselected**。

### Added（2026-10-09·续27，pack 2→11：九条新任务型流水线 + 每包一条真 E2E + 清册护栏）

- **背景**：docs/product-spec.md §2 的验收线第 1 条「pack ≥10，且每个 pack 有至少
  一条端到端测试真跑通（不是 mock 断言）」此前只有 2 条成立（docgen 系 /
  api-report），其余 YAML 都是 docgen 变体——20x 口径里最重的"任务类型"轴一直是
  2/10。本批把 §8 默认选型清单落地：**新增 9 条 pack，合计 11 条任务类型**。
- **新增 9 条 pack**（每条 = 新任务语义，不是旧链换模板）：
  - `intel-brief.yaml`：检索 → 逐条要点简报（researcher + transform foreach）；
  - `kb-brief.yaml`：本地资料 → 建库检索 → 摘录卡（ingest + kb(hash) +
    transform foreach；**全离线**，同一语料两次跑出同序卡片）；
  - `data-qc.yaml`：数据集质检报告（http_request + transform 的 `where/any`
    判定 + **quality_gate**——质量门首次服务机器生成的非文档产出）；
  - `alert-runbook.yaml`：告警 JSON → 逐条处置手册（foreach 展开）；
  - `deck-brief.yaml`：汇报要点 → Markdown 主产物 + **pptx 交付**（renderer）；
  - `minutes-weekly.yaml`：会议纪要 → 逐议题行动项周报；
  - `invoice-extract.yaml`：发票数据集 → 逐票要素台账（md + pptx）；
  - `k8s-patrol.yaml`：K8s Nodes API → 逐节点巡检报告；目标是**内网地址**，
    因此是 `allow_hosts` 显式放行的出厂消费者（判据同时断言
    `guard == "allowlist"`，不静默放行）；
  - `spec-cases.yaml`：需求文本 → 规则解析 → 逐关键词用例清单（`llm_enabled:
    false`，无 LLM 逐字可复现）。
  每条都配了独立 `*.lock`（config_hash + 拓扑指纹）。
- **两处真缺陷（新 pack 逼出来的，实测后修）**：
  - `renderer_agent`：节点级 config 在 DAG 模式下被**静默忽略**——构造期只读
    注册时的空 config，YAML 里写的 `formats` / `output_dir` / `title` 不生效
    （实测：声明 `formats: ["pptx"]` 的流水线仍渲染全部四种格式、`output_dir`
    也落在默认目录）。修复：按请求从 `payload["config"]` 解析成局部量
    （不改实例状态，并发复用不串台）。
  - `requirements_analyzer`：同类问题——`llm_enabled` / `confidence_threshold` /
    `max_questions` 只在构造期读，YAML 里写 `llm_enabled: false` 仍会走 LLM 路径。
    修复同上一并：按请求解析，规则/LLM 路径共用合并后的领域配置。
  - 两处各配回归判据（renderer 2 条 / analyzer 2 条），并做反向验证：把修复改回
    旧写法，对应判据转红。
- **测试**：新增 `tests/test_pack_suite.py`（32 条）——
  - 9 条 E2E：真 Scheduler → 真 DAGExecutor → 真 Agent → 真落盘，只在外部 IO
    边界换罐头（HTTP 响应 / 搜索引擎 / url_guard DNS 确定性映射），判据落在
    交付物内容上（含 pptx 读回幻灯片标题、质量门通过分、allowlist 留痕）；
  - `TestPackInventory` 清册护栏：`PACK_E2E` 登记表把"≥10 条 pack、每条 YAML +
    lock 且锁校验通过、每条有具名 E2E 判据类"钉成会变红的断言——新增 pack 忘
    登记、锁漂移未重锁、判据被删都会暴露。
  全量 **2249 passed, 1 skipped, 6 deselected**（本机 2026-10-09 实测）；
  README 计数、批线、流水线树同批更新；product-spec §2 任务类型轴按实测改为
  **11**（组合数 120 → 660 重算）。

### Added（2026-10-09·续26，`/api/config` 变更审计：共用审计模块 + 值脱敏 + 查询端点）

- **编号说明**：与并行批「acquire 竞争重试」（续25）同窗口落地，按其先合并的顺序
  本批顺延为续26。
- **背景**：docs/product-spec.md §5.4 的判据"每次 `/api/config` 变更有审计记录"此前是空的——
  变更只写进程内结构化日志，没有可落盘、可查回的持久留痕。
- **新增 `pipeline_core/audit.py`**：把 webhook 那套 JSONL 审计（续22）抽成共用模块——
  `audit_path` / `write_audit` / `read_audit` 三个函数，webhooks 与 config 两条通道
  共用同一份实现（不做第二份"差不多的写盘代码"，格式不会各自漂移）；
  读回端对半行坏行跳过，不因一行炸整份读取。`write_audit` 接住**所有**异常并返回
  错误字符串——审计失效可以，审计失效把业务请求打成 500 不行（全量跑抓出的实测缺陷）。
- **`admin_api._handle_config_set`**：每次变更追加 `audit/config.jsonl`——
  key / 新旧值 / 客户端地址 / 凭证指纹（sha256 前 12 位）；**敏感键名**
  （含 token / secret / password / api_key）的值只记脱敏形状（类型 + 长度），不落明文；
  写盘失败不静默：响应带 `audit_error`，且**不回滚已应用的配置变更**（如实回报而非假装无事）。
  handler 的 `client_address` 用 `getattr` 兜底（`__new__` 构造的夹具/嵌入方没有它）。
- **新增 `GET /api/config/audit`**（管理鉴权）：读回最近记录（新→旧，`limit` ≤ 500，
  非法值兜底 50）。OpenAPI 规范同步补该端点与 `POST /api/config` 的审计语义说明。
- **一处 patch 点修正**：`webhooks.py` 的本地 `write_audit` 改为转发共用模块后，
  `test_webhooks.py` 里"审计写盘失败"用例的 patch 目标（`wh._audit_path`）失焦——
  跟到 `pipeline_core.audit.audit_path`，否则就是"打了补丁但没命中"的假绿。
- **测试**：新增 `tests/test_config_audit.py` 9 条——变更落审计（含旧值/客户端/指纹）、
  敏感值脱敏（断言明文不在文件里）、非敏感键不脱、写盘失败如实回报且不回滚、
  被拒请求不落审计、读回新→旧、`limit` 非法兜底、坏行跳过、两通道共用实现。
  全量 **2214 passed, 1 skipped, 6 deselected**（本机 2026-10-09 实测），
  coverage 见 README；README 计数与批线同批更新。

### Fixed（2026-10-09·续25，`acquire` 抢输竞争被误报成"队列空"——重试下一条）

- **现象**：P7 的跨进程判据（8 进程抢 6 条、各 `acquire` 一次）在**全量负载**下偶发
  转红，单跑必绿——典型的竞争缺陷现场。
- **根因（实测复现，不是推断）**：`acquire` 的 `UPDATE ... AND status='pending'` 返回
  `rowcount=0` 时直接 `return None`。但 `rowcount=0` 只说明"这一条被别的 worker 先拿了"，
  队列里可能还有别的 pending——调用方却把 None 读成"没活了"。
  - 压力复现（12 进程同步起跑抢 6 条任务）：**11/15 轮有任务没人认领**（claimed=1~5）；
  - 修复后同一脚本：**0/15 轮丢失**。
- **修复**：`rowcount=0` → `conn.rollback()`（本事务快照已过期）→ `continue` 重试下一条；
  队列真空时 SELECT 无行才返回 None。推进性有界（每轮把一条被抢走的行从候选排除）。
- **回归判据**（`tests/test_task_queue.py::TestAcquireLostRace`，2 条）：
  - 用连接包装器**确定性**注入竞争窗口（SELECT 与 UPDATE 之间让第二个连接抢走），
    断言重试领到下一条而非 None；
  - 抢输且无下一条时必须返回 None（重试不许把"真没活"变成死循环）。
  - 反向验证：把修复改回 `return None`，第一条用例转红、第二条仍绿——判据精确打在缺陷上。
- 全量 **2205 passed, 1 skipped, 6 deselected**（本机 2026-10-09 实测）；README 计数
  与批线同批更新。

### Added（2026-10-08·续24，队列多机：真跨进程判据 + 两实例共享一库跑通）

- **背景**：队列与 worker 的代码早已在位，但"队列多机"的既有判据全跑在**同进程多线程**
  上（`tests/test_worker.py` 的注释自己写着"等价于两个进程共用同一份 tasks.db"）——那是类比，
  不是实证。product-spec §2.1 验收线的原文是"**两实例共享一库并发跑通**"。
- **新增 `tests/test_multi_process_workers.py`**（4 条，真 subprocess 进程）：
  - 互斥：8 个独立解释器进程同时抢 6 条任务，每条恰好被抢到一次；
  - 租约回收：持任务的子进程被 `kill()` 后，另一进程 `recover(stale_seconds=…)` 把它翻回
    pending 并能被再次领走；
  - 活 owner 不被抢：owner 进程存活期间，另一进程回收必须返回 0（否则双跑）；
  - CLI 巡检：`run.py --worker --once` 空库退出码 0、不卡死。
- **新增 `tests/test_two_instances_one_queue.py`**（1 条，**跑真流水线**）：两个真 `TaskWorker`
  进程共享一份 `tasks.db`，用 mock 搜索的 `test_pipeline` 把 4 条任务真跑完——断言无丢失
  （全部 done + 产物在盘）、无重复领取（两进程领取集合互不相交）、归属留痕（`worker_id`
  与领取者一致）。
- **反向验证（判据必须能被命中）**：摘掉 `acquire` 的 `AND status='pending'` 守卫后，
  跨进程互斥用例当场转红（实测）；验完原样还原。
- **实现期踩到并修掉一个测试写法陷阱**：子进程 worker 会打大量日志，用 `subprocess.PIPE`
  而不读取会把管道缓冲区写满、子进程阻塞在写日志上——首次跑真端到端等了 900s 超时。
  最小复现证明 worker 本身正常（任务 done、产物在盘、`shutdown()` 返回、进程自行退出），
  改成重定向到日志文件后 11s 通过。这条记在测试注释里，避免后人重踩。
- **文档**：README 增「多机 worker（共享一份任务队列）」小节——三条并发契约（绝不双跑 /
  崩溃可接管 / 活 owner 不被抢）与实验证；如实标注边界：`_pid_alive` 是同机 pid 表判据，
  "跨机接管"只在共享文件系统 + 共享 pid 命名空间下成立，真跨主机请用 `/tasks/{id}/rerun`。
  product-spec 触发方式轴按实测更新为 **6/6 达标**（剩余边界同上）。

### Added（2026-10-08·续23，外部插件示例包 + 真 venv 安装即发现的端到端判据）

- **背景**：entry_points 发现（续21）落地时只有"打桩 entry_points()"的单元判据；
  product-spec §2.1 验收线第 2 条要的是**真外部 pip 包**——本批把这条补成实证。
- **`examples/plugin-hello/`**（随仓参考实现，可复制成模板）：
  - `pyproject.toml` 声明 group `doc_pipeline.agents`、值 = 模块路径；
  - `char_stats` Agent（字符 / 行 / 中文字符统计）刻意保持"干净"——不碰黑名单
    调用、**不**声明 `SANDBOX_TRUSTED`（那是内置件的做法），示范合法插件如何
    直接通过 AST 安全检查；
  - README 写清契约（模块级 `AGENT_NAME` + BaseAgent 子类、同名本仓优先、
    安装 / 验证 / 卸载三步）。
- **端到端判据 `tests/test_plugin_example.py`**（4 条）：
  - 快判据 3 条：示例包 group 必须等于加载器常量 `ENTRY_POINT_GROUP`（写错就
    没有插件）、模块契约、AST 扫描零告警；
  - **慢判据 1 条（真 venv + 真 pip install）**：`venv --system-site-packages` +
    `pip install --no-deps --no-build-isolation ./examples/plugin-hello`，然后在
    **空 agents 目录**下跑发现脚本——`char_stats` 必须被 `discover()` 列出、
    `register()` 注册（`source == "entry_point:char_stats"`）且 `handle()` 真跑出
    统计结果。整条判据**离线可跑**（pip/setuptools 借宿主机、插件本体不装依赖），
    CI 与无网环境行为一致；本机实测该条 ~66s，是"验收证据"，不用 slow 标记
    绕开它。
- **口径**：product-spec §5.2"插件发现"行标记达标（**第三方插件实测数仍为 0**——
  等真实生态，自带的示例包不算第三方）；README「外部插件」小节指向参考实现与
  判据。全量 **2198 passed, 1 skipped, 6 deselected**（本机 2026-10-08 实测），
  coverage 87.99%。

### Added（2026-10-08·续22，入站 webhook：HMAC/Token 鉴权 + 审计留痕，`POST /api/webhooks/<name>`）

- **痛点**：触发方式此前全部是"从内部发起"（CLI / HTTP 提交 / MCP / 定时），
  外部服务（CI、监控、IM 机器人）想让流水线动起来没有正规入口——product-spec
  §2 四轴表"触发方式"（5 → 目标 ≥6）与 §5.3 的"webhook 入站有鉴权与审计"记的就是这条。
- **`pipeline_core/webhooks.py`**（配置 + 纯函数逻辑，HTTP 粘合薄）：
  - **鉴权独立于 `ADMIN_API_KEY`**——外部服务不该持有管理端凭据。每个 webhook
    一个密钥，`secret_env` 指向环境变量（密钥不落配置文件），支持
    `X-Webhook-Signature: sha256=<HMAC-SHA256(原始请求体)>`（GitHub 风格，能防体篡改）
    或 `X-Webhook-Token`（直传、常量时间比较）；**密钥未配置 = fail-closed（503）**。
  - **审计留痕**：每次调用无论成败落一行 JSON 到 `state_root()/audit/webhooks.jsonl`
    （时间 / webhook / 来源地址 / 方案 / 结果与 HTTP 状态 / task_id 或拒绝原因）；
    写盘失败在响应里带 `audit_error` 如实回报，不静默。
  - **请求体即运行输入**：JSON 对象经 `render_inputs_doc`（本批从 mcp_server
    抽出的共享模块 `pipeline_core/input_docs.py`）按「## 键」渲染，其余原样写入；
    `run_plan(wait=False)` 提交——与手动 run / 定时触发同一条路径、同一观测面，
    响应 `202 + task_id`。
- **`admin_api.py`**：`POST /api/webhooks/<name>` 置于管理鉴权门**之前**（自携密钥，
  见上）；`GET /api/webhooks`（管理鉴权）列出配置清单（不含密钥值）；
  `AdminAPI.start()` 装载 `webhooks.yaml`（路径可用 `DOC_PIPELINE_WEBHOOKS_FILE`
  或构造参数覆盖；未配置文件 = 能力休眠；配置写坏 = 整体禁用并大声记日志）。
  OpenAPI 规范同批补上两个新端点。
- **一处判据按意图修正**：`test_openapi_spec.test_responses_exist` 原要求每个操作
  必须有 `200`——webhook 的成功码是语义正确的 `202 Accepted`，判据收宽为"任意
  2xx"，而不是往规范里补一个端点根本不会返回的 200。
- **测试**：新增 `tests/test_webhooks.py` 28 条——配置校验（缺 secret_env / 坏
  enabled / 重名 / 未知流水线）、两种鉴权（含大小写折叠取头、体篡改必失败、签名
  优先于令牌）、fail-closed 503、四类拒绝全落审计（unknown / disabled /
  secret_missing / bad_credentials）、成功路径断言 `run_plan(wait=False)` 与输入
  文档内容、提交失败 500、审计写盘失败如实回报、HTTP 粘合层（含超限 413 与
  缺名 400）。全量 **2194 passed, 1 skipped, 6 deselected**（本机 2026-10-08
  实测），coverage 87.99%；README 增「入站 Webhook」小节、`webhooks.example.yaml`
  随仓（个人配置进 .gitignore）；product-spec §2 / §5.3 / §5.4 按实测更新
  （组合数按实值重算 80 → 100）。

### Added（2026-10-08·续21，entry_points 插件发现：外部 pip 包零改动接入 Agent）

- **痛点**：Agent 发现只有"本仓 `agents/*.py` glob"一条路——外部包想提供 Agent
  必须往本仓扔文件。product-spec §2 四轴表"扩展来源"（1 → 目标 ≥2 且第三方 ≥5）
  与 §2.1 验收线第 2 条（"外部 pip 包提供 Agent，本仓零改动即被发现、注册、
  执行"）记的就是这条。
- **发现**：`AgentLoader.discover()` 在 glob 之外读 `entry_points(group=
  "doc_pipeline.agents")`；entry point 的**值须是模块路径**（与内置
  `agents/*.py` 同构：模块级 `AGENT_NAME` + 一个 BaseAgent 子类）——加载出类
  之类的形态直接报错并给出修正提示，而不是猜。
- **加载**：`_register_entry_point` → `ep.load()` 得到模块 → 用 `__file__` 做
  **与内置件同一套安全检查**（未声明 `SANDBOX_TRUSTED` 就走 AST 扫描）→
  复用新抽出的 `_register_module`（两条路径从此共用"找类 → 提 meta → 实例化 →
  注册"这一段，不再有第二份分叉）。
- **来源标记**：`AgentMeta` 新增 `source` 字段（`builtin` / `entry_point:<name>`），
  `run.py --list-agents` 对非内置来源打 `[entry_point:xx]` 标记（S4 面板接它
  是后续的事）。同名时**本仓文件优先**——插件不能悄悄顶掉内置 Agent。
- **鲁棒性**：entry_points 元数据读取失败不让内置发现陪葬（记 error 后
  discover 照常返回本仓清单）；坏插件（load 抛错 / 值不是模块 / 无文件）被记
  日志跳过，不拖垮同批其他 Agent。
- **测试**：`tests/test_agent_loader.py` 新增 `TestEntryPointPlugins` 8 条——
  发现含插件且 group 名被钉住（写错就没有插件）、同名本仓优先、注册后
  `handle()` 真跑出结果（可作节点执行）、未声明信任的恶意插件被 AST 拦下、
  声明 `SANDBOX_TRUSTED` 放行、类值 entry point 被拒、元数据损坏不拖垮发现、
  agents 目录缺失仍能列出插件。全量 **2166 passed, 1 skipped, 6 deselected**
  （本机 2026-10-08 实测），coverage 87.98%；README 增「外部插件（entry_points）」
  小节并修正沙箱段落的"白名单"旧口径；product-spec §2 四轴表"扩展来源"按实测
  更新（组合数 40 → 80 重算）。

### Added（2026-10-08·续20，定时触发：`--triggers` 常驻调度 + 5 字段 cron）

- **痛点**：引擎此前只有"人来触发"的入口（CLI / HTTP / MCP / recover），
  "周期性报告"这类固定节律的用法得靠外部计划任务壳一层——product-spec §2
  四轴表里"触发方式"记的就是这条（3 → 目标 ≥6）。
- **`pipeline_core/cron.py`**：5 字段 cron 解析 + 下次触发计算，纯函数、零新依赖
  （不装 APScheduler/croniter——引擎层只依赖 artesian 与标准库）。
  - 支持 `*`、`a`、`a-b`、`*/n`、`a-b/n`、`a/n`（Vixie 语义：a 到字段上限）、
    逗号列表与 `@hourly/@daily/@weekly/@monthly/@yearly` 宏；周 7 归一为周日。
  - **日/周的 OR 语义**（两字段都受限时命中任一即触发）按 Vixie cron 实现；
    只有单边受限时按该边判定。
  - `next_fire` 按天推进扫描（不匹配日期 O(1) 跳天），5 年上限防 `0 0 30 2 *`
    这类永不匹配把调用方挂死——超限抛 CronError，不是死循环。
- **`pipeline_core/triggers.py`**：triggers.yaml 加载（名字唯一 / 流水线存在 /
  cron 合法全部前置校验）+ 调度推进 `tick()`（时钟与提交函数可注入，循环体
  无副作用）+ `run_trigger_loop()` 常驻循环。
  - **与手动 run 同一条提交路径**：`run_plan(..., wait=False)`——定时任务因此
    天然出现在同一任务队列与观测面（`list_tasks` / `GET /tasks` / 仪表盘）。
  - **错过不补跑**：积压窗口只跑最近一个，跳过数量如实打印（补跑策略是产品
    决策，不偷偷替用户选）。
  - inputs 字符串渲染为输入文档落到 `state_root()/trigger_inputs/<name>.md`。
  - **YAML 布尔陷阱显式拦下**：`name: off` 会被 YAML 解析成布尔 `False`——
    加载器直接报"name 必须是字符串（off/on/yes/no 请加引号）"，而不是把
    False 静默 stringify 成 "False"（实现期在测试里先踩到，已钉判据）。
- **CLI**：`--triggers`（常驻）/ `--triggers-file`（配置路径，默认仓库根
  triggers.yaml）/ `--triggers-dry-run`（列最近 3 次时刻后退出）；模板
  `triggers.example.yaml` 随仓，个人配置 triggers.yaml 进 .gitignore。
- **测试**：新增 `tests/test_cron.py`（37 条）与 `tests/test_triggers.py`（29 条），
  共 +66——cron 侧把 OR 语义 / 月年进位 / 闰年 2-29 / 永不匹配 / 边界独占全用
  具体时刻钉死；触发器侧注入假时钟走完"到点一次 / 不重复 / 错过计数 / 停用跳过 /
  提交失败不拖垮循环"，另有 `--triggers-dry-run` 的真子进程往返。全量
  **2158 passed, 1 skipped, 6 deselected**（本机 2026-10-08 实测），
  coverage 87.96%；README 增「定时触发（Cron）」小节与测试计数；product-spec
  §2 四轴表与 §5.3 触发方式行按实测更新（顺带补记 P1 已落地的交付形态 5 种，
  组合数按实值重算 18 → 40）。

### Added（2026-10-08·续19，MCP 通用入口 run_workflow(name, inputs)）

- **痛点**：MCP 原有 5 个 tools 全是文档动词——`generate_document` 把输入写死成
  `## 查询` 段。拿引擎跑非文档流水线（api-report / api-digest / kb-docgen）没有
  通用入口，等于把"通用引擎"在外面焊成了"文档工具"（product-spec §5.3 记的就是这条）。
- **新增 `run_workflow`**：只要求 `name`（已安装流水线名），`inputs` 可选、对象或字符串：
  - 对象 → 按 `## 键` 分节序列化为输入文档（列表成 `- ` 行、嵌套对象走 JSON、
    标量直书）；字符串 → 原样写入；空输入 → 只写标题行。
  - 输入文档就是引擎既有的运行接口（首层 Agent 从它提取查询词），提取会跳过
    `#` 行，所以标题不会被误当主题。
  - 返回值回显 `inputs`；`wait` / `output` 语义与 `generate_document` 完全一致。
- **顺带收口**：两条工具抽出共享提交尾 `_submit_plan_run`（写临时输入文件 →
  parse → run_plan → wait/异步 watcher 收尾只在单处维护）；`_resolve_target_path`
  同理。原 `generate_document` 在 resolve 失败时"先写文件再删"的绕路随之消失
  （顺序改成先 resolve 后写文件）。
- **一处守卫跟着生成点走**：`test_security_hardening` 里那条 `inspect.getsource`
  断言原盯 `_tool_generate_document` 里有没有 `new_task_id()`，重构后失焦（它在新
  结构下转红是对的）；改盯 `_submit_plan_run`，并钉住两条工具都必须经过它。
- **测试**：`tests/test_mcp_server.py` 14 → 20 条（+6：schema / 缺 name / 未知流水线
  列可用清单 / 对象 inputs 落输入文档 / 字符串原样 / `_render_inputs_doc` 形状直测；
  提交路径测试 patch 掉 watcher 线程再读临时文件——不 patch 会被后台清理删走）。
  全量 **2092 passed, 1 skipped, 6 deselected**（本机 2026-10-08 实测），
  coverage 87.95%；README 的 MCP 小节与测试计数、product-spec §5.3 的 MCP 行
  （标记达标）同批更新。

### Added（2026-10-08·续18，xlsx / pptx 结构化子集渲染：表格落工作表、标题起幻灯片）

- **编号说明**：续16（本仓渲染层结构化表格）与并行批的 enforce_admins 台账原本
  撞号，后者已改续17；本批顺延为续18，不再制造第二个撞号。
- **痛点**：渲染层此前只有 docx / pdf 两条"整篇文档"路线——报告里的表格数据要拿去
  二次计算（xlsx）、提纲要拿去汇报（pptx）都得换工具。这两条新路线按**结构化子集**
  兑现：不做全量转换器，只把结构信息（表格 / 标题 / 列表）映射成目标格式的**真对象**，
  映射不了的一律如实回报而不是静默吞掉。
- **xlsx（openpyxl）**：每张 Markdown 表格一个工作表——表名取最近的上游标题
  （超 31 字符 / 含 `[]:*?/\` 自动清洗、重名自动加序号），表头加粗、冻结首行、
  列宽按内容（CJK 按双宽）估。**无表格时**整篇按行落进单个「正文」工作表，
  `mode` 字段如实回报走了哪条路；单元格一律按文本写入（`007` 前导零、ID 串
  不做数值推断——保真优先于"看起来像数字"）。
- **pptx（python-pptx）**：h1/h2/h3 各起一页、其后的段落/列表/引用成要点；
  表格落**真的 PowerPoint 表格**（不是图片也不是文本块）。幻灯片不是文档——
  超 12 行 / 8 列的表格、超 12 行的代码块**就地截断并把「原表 N 行 × M 列」
  写进页内**：pptx 的溢出不报错、只是看不见，宁可显式截断也不静默丢。
  字体同 docx 一条边界：`font.name` 只写 `a:latin`，中文要在 rPr 上手写
  `a:ea`（连带 `a:cs`），否则跨机打开中文字形回退。
- **降级与边界**：两条后端都走既有"可选依赖如实回报"约定（缺 openpyxl /
  python-pptx 时返回结构化 error，不中断流水线）；`render()` 分发与
  `supported_formats()` 环境探测同步扩到四种；renderer Agent 的 `formats`
  配置项因此天然支持 xlsx / pptx（docgen-render.yaml 未动，默认仍 docx+pdf）。
- **注册与依赖**：`tests/test_layering.py` 的 docpipeline 依赖登记表新增
  openpyxl / pptx 两条（IMPORT_TO_DIST 映射到发行名 python-pptx）；
  requirements.txt 与 pyproject 的 `render` / `all` extras 同批声明。
- **测试**：渲染套件 64 → 80 条（+16：xlsx 8 / pptx 8），全部"读回"判真——
  xlsx 用 openpyxl 重开断言表名 / 表头加粗 / 冻结 / 转义竖线还原 / 文本模式，
  pptx 用 python-pptx 重开断言页数 / 标题 / 要点 / 真表格与截断注记，并用
  zipfile 断言 `a:ea` 字体真的写进了 slide XML。全量 **2086 passed, 1 skipped,
  6 deselected**（本机 2026-10-08 实测），coverage 87.93%；README 格式表与
  测试计数同批更新。

### Changed（2026-10-08·续16，main 的 required 检查从此也管管理员）

- **查出来的一件事**：`main` 上确实挂着 5 条 required 检查
  （`test (3.11/3.12/3.13/3.14)` + `docker`，`strict=true`），但 `enforce_admins=false`——
  所以那两批直推各自打印了 `Bypassed rule violations for refs/heads/main: - 5 of 5 required
  status checks are expected.`。**"全绿才进 main"在此之前只是惯例，不是机器判据**：
  真把 main 推红了，门禁不会拦在门外，只会事后告诉你红了。
- **处置**：`enforce_admins` 翻 true。读回整份 protection 与改动前逐字段 diff，
  **只有这一个字段变了**——5 个 context、`strict`、`required_signatures`、
  `required_linear_history`、`allow_force_pushes`、`allow_deletions` 全部原样，
  也**没有**顺手开启"必须经 PR 才能改"（那是另一件事，没授权就不动）。
- **端点形态记一笔**：这条子资源是 `POST .../protection/enforce_admins` 生效、`PUT` 同一 URL 返
  **404**；而上一批停用 workflow 时正好反过来（`PUT .../disable` 生效、`POST` 404）。
  所以"写操作 404"先怀疑动词与端点形态，别急着归因权限——GET 一路都通、scope 也够。
- **从下一批开始的代价**：直推 `main` 会被拒（required 检查对新 sha 只能"预期"、不能"已过"），
  每批要走 分支 → PR → CI 全绿 → 合并。这是这条规则应有的样子，但节奏确实变了，
  后续推送与合并仍需逐批授权。

### Added（2026-10-08·续16，渲染层结构化表格：docx 落真 Word 表格、pdf 落 Table）

- **背景**：`docpipeline/renderer.py` 里挂着产品代码全仓唯一的 TODO——"表格、图片
  暂按段落处理"。上游确有其物：摄入层把 PDF 表格转成 Markdown 管道表
  （`_rows_to_markdown`，PyMuPDF `find_tables`），writer 产出的正文也可能带表格；
  渲染层原样把它们当普通段落写进 docx/pdf，交付文档里是一串带竖线的字面文本。
- **解析层**：`parse_markdown` 新增 `table` 块类型。判据刻意保守——表头行须以
  `|` 起收、分隔行须含竖线（`(?=.*\|)` 前置断言），两条都满足才认；否则整段
  回落为普通段落，散文里的竖线行不被误吞。
- **docx**：落**真 Word 表格**（`Table Grid` 样式、表头行加粗）——Word 里可
  继续编辑行列，而不是一串管道符段落。**pdf**：落 ReportLab `Table`
  （浅灰网格线、表头浅底、单元格样式从 BodyText 继承），单元格文本进 pdf
  文本层（pymupdf 可读回，中文无损）。
- **单元格切分**只按未转义竖线切（`(?<!\\)\|`），`\|` 还原为字面竖线——与
  ingest 产出（单元格内字面竖线写作 `\|`）对齐；列数取齐最长行（缺的补空）。
- **顺带修掉图片标记残渣**：`clean_inline` 原来只吃链接，`![截图](url)` 会被
  `_RE_LINK` 命中 `[截图](url)` 而留下 `!` 残渣（渲染为 `!截图 (url)`）；
  现图片规则先于链接执行，输出 `截图 (url)`。
- **实现期抓出并钉住两个真缺陷（各有回归用例，先在旧实现上复现再修）**：
  1. 分隔行不带首竖线（`--- | ---`，GFM 合法形态）时，旧"先把连续 `|` 行全收
     进来再 `pop(1)` 丢分隔行"的写法直接 `IndexError`（收行条件与分隔行形态
     不匹配）——复现 `REPRO OK, IndexError: pop index out of range` 后改为
     显式跳过分隔行的收集；
  2. 单独一行 `---`（水平线）跟在看起来像表头的行后会被误判成分隔行——
     `_RE_TABLE_SEP` 补 `(?=.*\|)` 要求行内至少一个竖线。
- **测试**：`tests/test_renderer.py` 49 → 64 条（+15：解析 9 / docx·pdf 端到端
  4 / 行内清洗 2）。端到端不只看文件生成：docx 用 python-docx 读回 `doc.tables`
  断言行列数与单元格文本、表头加粗；pdf 用 pymupdf 断言文本层含单元格内容与
  转义竖线还原。README 测试数与格式表同批更新（**2070 个测试本机全绿**，
  coverage 87.89%）——护栏按设计先红后绿，更新后复跑为绿。

### Changed（2026-10-08·续15，E2E Nightly 停用：常驻红灯不是回归信号）

- **现象**：E2E Nightly 连续三晚红（10-05/10-06/10-07，run 37389154677 /
  37538468919 / 37696603993，都是 `schedule` 触发、20–48 秒就结束）。
- **根因不是坏了，是判据按设计生效了**：`7d925b5`（2026-10-05）给这个工作流装了
  "0 执行不许报绿"的闸门，而仓库**从未配置过任何 `E2E_*` Secret**
  （`gh secret list` 返回 `[]`，仓库也没有 environment），于是第一道
  `::error::未配置任何 E2E_* Secret…` 每晚必然命中——真正的 6 条 e2e 用例
  从来没机会跑，第二道判据也就从没被执行过。
- **停用前的假绿也一并核过**（不是凭注释推断）：10-02/10-03/10-04 三个 schedule run
  的日志尾部分别是 `6 skipped, 1519 deselected`、`6 skipped, 1674 deselected`、
  `6 skipped, 1784 deselected`，工作流却都是 success——正是那条判据要治的形态。
- **处置（两条都做，避免只改一处）**：GitHub 侧工作流状态置为 `disabled_manually`
  （workflow id 344188685，`gh workflow list` 回读确认）；YAML 里 `schedule` 块摘掉，
  只留 `workflow_dispatch`——UI 上的停用状态不进 git，光靠它下次改文件就可能被翻回来。
  `workflow_dispatch` 保留是为了"配好 key 就能手动验一次"，不用先恢复定时。
- **README「测试」一节的口径补齐**：原话"CI 未配 Secret 时这些用例会全部 skip
  （历史上 E2E Nightly 因此'绿而未跑'）"不算写错，但只交代了旧假绿，没交代
  **现在每晚红**这件事——读者按 README 看仓库会以为夜间回归还在跑。现补齐 6 条用例
  各自的 `skipif` 前置（搜索要 `BOCHA_API_KEY`/`TAVILY_API_KEY` 其一、LLM 要
  `LLM_API_KEY`、全链路两条都要）、停用的原因与日期，以及重启条件；
  CONTRIBUTING 的工作流表同样从"schedule / dispatch"改成只写 dispatch 并注明停用。
- **顺手加了两条判据把这份口径钉住**（`tests/test_doc_consistency.py`）：一条先证明
  读取函数用的是 YAML 解析器而不是文本搜索（当前 schedule 块就留在文件里当注释，
  grep 会把它读成"定时还在"），一条要求"有没有定时"与"README/CONTRIBUTING 怎么说"
  同向——反向也成立，将来真恢复定时却没改文档一样红。
  五条漂移变异（恢复定时不改文档 / 删停用声明 / 删重启条件 / 表格回退 / 连手动触发一起摘）
  逐一转红且失败原因各对其意，原样为绿，跑完文件逐字节还原。
- **离线兜底没有变薄**：`tests/test_e2e_mock.py` 与 `tests/test_kb_pipeline_wiring.py`
  都不带 `e2e` 标记，默认 CI 真跑（本次实测 `-m e2e` 收集到 6/2060，其余 2054 在默认口径里）。
- **README 的测试数随这批 +2 条判据抬到 2055**（`2055 passed, 1 skipped, 6 deselected`，
  2026-10-08 本机全量实测 12 分 18 秒）。coverage 那句原来只写"本轮实测 87.84%"，没说是哪台：
  现拆成两条——本机 10-07 全量 87.84%，CI 侧 10-08 在 `7b140bf` 实测 87.28%（3.11–3.13 三档一致，
  3.14 是 87.25%）。
- **CI 与本机的通过数差 10 条不是这批造成的**：CI 四档都是 `2045 passed, 10 skipped`，
  本机是 `2055 passed, 1 skipped`——总数两边都差 1 条（收集 2055 vs 2056），这是可选依赖
  带来的收集/执行差异，README 早就声明过；拿上一笔 `e944bbc` 的 CI 日志对了一下，
  当时是 `2043 passed, 10 skipped`，**差值同样是 10 条**，而这批正好加 2 条判据
  （2043→2045），所以对不上账的不是这批。
- **推送后的终判**：CI run 37714279545（head sha `7b140bf1…` 与 `git ls-remote` 逐字节相同）
  conclusion success，四档测试矩阵 + docker + perf-regression 全绿，`refresh-baseline` skipped；
  两条新判据的 PASSED 行在 3.11/3.12/3.13/3.14 四档日志里各有两行（不是"步骤绿而用例没跑"）。
  推 YAML 之后回读 `gh workflow list --all`，E2E Nightly 仍是 `disabled_manually`——
  改文件没有把它自动翻回 active。
- **顺带排除一个假嫌疑人**：Perf Trend 只有一次 10-01 的 run，看着像"另一个从不跑的定时"，
  实际它的 cron 是 `0 0 1 * *`（每月 1 日），下次该在 11-01——不是缺陷。

### Added（2026-10-08·续14，artesian 发布流水线就位（等一次性 PyPI 配置））

- artesian 加了 `.github/workflows/release.yml` + `tools/verify_dist.py`：
  **只在 GitHub Release 被 published 时才上传**，`workflow_dispatch` 默认 `dry_run=true`
  只构建与校验；用 Trusted Publishing（OIDC），仓库里不存长期 token。
  `publish` 搬运 `verify` 产出的那份 artifact 字节，不重新构建——否则"校验过的"
  与"发出去的"不是同一批东西。
- `verify` 的四道（都在 runner 上实测过，见下）：build → 产物校验（版本在
  文件名/`pyproject`/`__version__`/包元数据四处必须一致；wheel 内容逐个文件对齐
  `src/artesian/`；`py.typed` 必须在 wheel 与 sdist 两处都在）→ `twine check --strict`
  → **把 wheel 装进干净 venv，再从仓库外面跑一遍测试**（源码全绿不代表 wheel 里那份能用）。
- **干跑抓到一条真 bug**：dispatch 没有 tag 时 `${WANT}` 展开成**空串参数**，脚本把空串
  当成"给了版本号"，报"tag 要发 ，但 pyproject 的 version 是 0.1.0"直接把 verify 打红
  （run 37694108621，`verify` failure／`publish` skipped——跳过是对的，说明条件表达式也没写错）。
  本机手测时传的是显式版本，**正好绕过这条**：又是一次"本地路径覆盖不到 CI 的参数形态"。
  修完把判据常驻成 `tests/test_verify_dist.py`（8 条：1 正例 + 7 反向对照，含空串这条），
  并做了一次变异——把那行修复还原，空串用例立刻转红。
- 复验（全部在 runner 上，不是本机）：Release run 37694975220（#2）`verify` 12 步 success，
  正文 `Checking …whl/…tar.gz: PASSED`、`Successfully installed artesian-0.1.0 orjson-3.13.0`、
  从仓库外面跑 `314 passed, 3 skipped`；CI run（同 sha）也 success。
  总数两边一致（317），runner 多出的 2 条 skip 是那步只装了 wheel + pytest，
  `selectolax`/嵌入类可选依赖不在场 ⇒ skipif 生效。**注意这条口径**：
  装包那一步验的是**裸安装**形态，带可选依赖的路径仍由 `ci.yml`（`.[html,dev]`）覆盖。
- **还差的那一步只能你来做**（我这边没有 PyPI 凭据，也不该有）：PyPI 上把 `artesian`
  登记为 Trusted Publisher（provider=GitHub、仓库 `Levango7/artesian`、workflow
  `release.yml`、environment `pypi`），GitHub 侧建同名 environment（要人工闸门就勾
  Required reviewers）。做完发版就是"改 version → 打 tag → 发 Release"三步
  （步骤已写在 artesian README「发布」一节，顺序反了会被版本一致性判红）。
  发布成功后本仓把 `artesian @ git+…@<sha>` 换成 `artesian>=x.y.z`，
  **手工跟 sha 这条成本随之消失**，`pip-audit` 也才第一次真能扫到它
  （现在它是 `version=None/vulns=[]` 的空账——续13 记过）。

### Fixed（2026-10-08·续13，收回一条说过头的话：迁出的代码当时其实没被任何 SAST 扫）

- **发现**：续8 里写过一句"被删模块自然脱离这些 glob，属预期（实现与判据已在
  artesian 侧，**那边有自己的门禁**）"。对 ruff/mypy/coverage 成立，**对 SAST 不成立**：
  本仓 bandit 的口径是 `-r pipeline_core docpipeline agents`，而 artesian 的 CI 当时
  只有 ruff / mypy / pytest 三步——于是三波迁出去的那 **2561 行**（含 `urlopen`、
  `subprocess`、以及我们刚写的 SSRF 逻辑）**两边都不扫**。这是"搬包会静默脱离
  按目录 glob 的门禁"的教科书复发，而且这次是我自己在清点时漏掉的一条。
- **处置**：artesian CI 加 `Security scan (bandit)`，与消费方同口径（`-ll`：MEDIUM+ 即红），
  `dev` extras 补 `bandit>=1.7.0`（不装就会变成"这一步在 CI 里根本没跑"）。
  另钉一条 **LOW 级棘轮**：把种类集合锁成 `{B404, B603, B607}`（全在 `ProSearchEngine`
  的子进程调用——argv 列表无 shell、`node` 由 PATH 解析、脚本路径只能显式注入），
  以后新增 LOW 必须回这里显式改账，不允许被 `-ll` 的阈值静默吸收。
- **一条必须写明白的旁注**：artesian 的 MEDIUM+ 之所以是 0，**不是"扫出了 0 个问题"**——
  搜索层出网从 `urllib.request.urlopen` 换成 `build_opener().open()` 之后，
  bandit 的 B310 是按**入口名**匹配的，告警随名字一起消失。所以真正有信息量的是
  那条 LOW 棘轮，而不是 `-ll` 的绿。
- **本仓口径同步更正**（`pyproject.toml` 的 `[tool.bandit]` 注释）：原写"全部 11 处均为
  调用固定的 https 端点"。实测两处不准：① 迁出前（`e4c02b4`）scope 内只有 **10** 处
  （`search_engines` 7 + `writer` 2 + `llm_router` 1），其中 7 处已随库走，现在剩 **3** 处；
  ② 这 3 处的 URL 是**运维配置**决定的（env 的 `*_API_URL` / config 的 `llm_api_url`），
  不是"固定 https 端点"——指向内网的本地推理端点（如 `http://127.0.0.1:11434`）
  是有意支持的场景，所以这里豁免 B310 而**不做**阻断；真吃外部 URL 的两条路各自有闸
  （webhook → `event_hook` 调 `artesian.url_guard`，抓页面 → fetcher）。
  顺带说明这条豁免不是死账：不加 skip 时 `-ll` 下 B310 会以 MEDIUM 响 3 条。
- **验证**（本机 + runner 双侧）：`bandit -ll -r src` rc=0；全量 LOW 恰为 `{B404,B603,B607}`；
  把期望改窄或多塞一条探针（临时 `_probe_low.py`，跑完即删并核零残留）都会判红；
  `set +e` 那段也按 runner 的 `bash -e` 跑过正反两形状——不写 `set +e` 时整块被 errexit
  掐死（rc=1、零输出），正是历史上那种"红得没有正文"的成因。
  artesian run 37691564832（#4）`completed/success`，三档矩阵各 **11/11 步**，
  runner 正文真打了 `LOW 级告警集合: ['B404', 'B603', 'B607']`。
- **pin 跟随**：按 续12 定的"追 main"政策，requirements 的直接引用抬到
  `1a83b77`（本笔只是 CI/dev-extra 改动，库代码未变）。这是新政策下的第一次跟随，
  也是"未发布 ⇒ 每次库改动都要消费方动手"的活样本。

### Changed（2026-10-08·续12，直接引用改为追 artesian main）

- requirements.txt 的 `artesian @ git+…@<sha>` 从 `203987a`（v0.1.0 那一笔）
  移到 `0f24388`（artesian main 当前一笔，只改了 README 的模块口径）。
- **口径变化要说明白**：续11 里"tag `v0.1.0` == requirements 的 pin"这条对应关系
  到此不再成立——pin 追 main，tag 仍停在发布物上。代价是 pin 指向的提交可能不属于
  任何 tag，日后回滚/审计要按**提交号**而不是版本名去找。
  但"追 main"追的仍是一个**固定提交**，不是分支头：分层判据
  `test_local_library_has_reproducible_install_source` 只认完整 40 位 sha，
  写成 `@main` 会当场判红（分支名会让同一份清单在不同时间装出不同的库；
  这条是拿六例合成变异直接调真判据验的，不是复刻逻辑：现状 PASS／`@main` FAIL／
  7 位短 sha FAIL／只有注释 FAIL／什么都没有 FAIL／PyPI 版本约束 PASS）。
- **复验**：远端存在性双向核过（`git ls-remote origin main` 与
  `gh api repos/Levango7/artesian/commits/0f24388…` 都给同一个全 sha）。
  新建 venv 里按这一行 `pip install --no-deps` 装出的产物含 10 个文件
  （9 个模块 + `py.typed`），`_guarded_urlopen` / `_SsrfRedirectHandler` /
  `validate_public_http_url` 都在位；`FirecrawlExtractor(api_key="k")` 对
  `169.254.169.254` 返回 `success=False`，`api_key=""` 时仍先短路成 `no API key`
  （护栏没有把旧的优先级顺序挤掉）。用该 venv 解释器跑本仓 8 个相关套件
  （kb 接线 / HTML 后端 / researcher / 分层 / llm_router / fetcher 安全 /
  fetcher 扩展 / 文档一致性）**213 passed**，且 `artesian.__file__` 落在
  该 venv 的 site-packages——验的是装出来的那份，不是本机 editable 的 `../artesian`。

### Fixed（2026-10-07）

- **质量门不再放过"有字的垃圾"（FP-1，产品定义 docs/product-spec.md §6 列的前置必修）**。
  实测现场：本机 keyless 跑 `--pipeline docgen`，落盘 3065 字节、`writer.stats.empty_sections`
  为 4 个（共 5 节正文），正文实为 360 识图页面的导航样板文字——当时 `quality_gate` 给
  **98.8 pass**、`checker` 只报 P3、`done` + `exit 0` 并正常出厂。
  - 根因一：保真底线只有两条判据（非空 + `min_output_chars=120`）与两个占位语，而 writer
    降级时写的是「降级声明」「（暂无可用的相关内容）」，都不在那份名单里 ⇒ 3000 字的垃圾
    照样过长度线。
  - 根因二：`run.py:349` 已经算出 `empty_sections`，却只 print 一条 WARNING，不参与判定。
  - 根因三：评分维度被抓取噪声反向骗过（`substance=100` 因样板文字信息密度高、重复率低；
    `topic_relevance=100` 因页面标题恰含主题词）。
  - 处置：新增 `docpipeline/degradation.py` 作为 writer ↔ 门禁的**单处**契约（占位行、降级
    声明、空结果自述与占比计算），两处不再各写一份字面量；底线补一条结构判据——
    **占位章节占比 > `max_placeholder_section_ratio`（默认 0.34）即 `hard_floor` 失败**。
    阈值取 1/3 而非"命中一次即判死"，是为了守住既有分工：偶尔一节没料属于评分该管的
    "写得好不好"，大半交白卷才是"到底有没有内容"。
  - 现场复验：同一命令现在 `exit 1`，报
    `HARD_FLOOR: 产出未过保真底线: 4/7 个章节是占位符（57% > 34%）`，**且不落盘任何交付物**。
  - 变异验证：把占比判据整块摘掉（`placeholder_ratio → 0.0`）后，同一份文档重新
    `status=pass / score=92.9` 且无 `hard_floor` ⇒ 拦下它的确实是新判据，不是别处顺带生效。
  - 判据：`TestPlaceholderSectionFloor` 五条（实测样本被拒 / 6 节只空 1 节不得判死 /
    阈值可配 / 非法阈值回落默认 / writer 与门禁共用单处定义的源码护栏）。
  - **锁文件影响**：`quality_gate` 的 `CONFIG_SCHEMA` 新增一项 ⇒ 节点有效配置变了 ⇒
    按既有规矩对 9 条含 quality_gate 的流水线重签 `--write-lock`
    （`_quality-tail/docgen/docgen-lean/docgen-render/docgen-verified/docreq/kb-docgen/test_pipeline/three_pass`，
    quality_gate 的 `config_hash` 由 `e3ebdd7e47f8` 变 `db28e2c9363f`）。
    `api-report.lock` 只差 `created_at`/`plan_id`（不参与校验），已还原以免无意义churn。

- **MCP 的 stdio 通道被启动 banner 污染，且 `serverInfo.version` 恒为 `unknown`（FP-3，
  产品定义 docs/product-spec.md §6）**。真子进程跑 `python run.py --mcp` 喂
  `initialize`/`tools/list` 复现：stdout 头四行是框线 banner，之后才是 JSON-RPC 帧——
  严格说这是往协议通道里塞非 JSON 行，`--json-output` 的 wrapper 同样受影响。
  - 处置：`print_banner()` 改走 stderr（stdout 只留机器读的产物通道）。
  - 版本回显的根因是**包初始化次序**：`pipeline_core/__init__.py` 在第 31 行就导入
    `mcp_server`，而 `__version__` 定义在文件末尾（原第 83 行），于是子模块里
    `from . import __version__` 发生在包半初始化时抛 ImportError，被就近的
    `except ImportError: SERVER_VERSION = "unknown"` 吞掉 ⇒ 表面永远报 unknown。
    现在 `__version__` 上移到子模块导入之前，并把那个兜底 except **删掉**——
    取不到版本应当当场炸，而不是伪装成一个能出厂的字符串。
  - 判据：新增 `TestMCPOverRealStdio` 两条（每个请求恰好一帧且 stdout 无 banner 文本；
    `serverInfo.version` 必须等于 `pipeline_core.__version__`）。此前 12 条 MCP 用例
    全在进程内调 `_handle_request`，所以这两个缺陷没有任何测试能发现。
  - 变异验证：banner 改回 stdout ⇒ 两条全红（断言里直接看到框线字符）；
    把 `SERVER_VERSION` 钉成 `"unknown"` ⇒ 版本那条红并打出真实回显字典。均已还原。
  - 真 stdio 用例把 `DOC_PIPELINE_STATE_DIR` 指到 tmp_path：幂等库按检出绝对路径共享，
    跑在仓库 `bus_data/` 上会与真实运行串台。

### Fixed（2026-10-07·续2）

- **CI 三处"绿而未证"收口（FP-2，产品定义 docs/product-spec.md §6）**。这三处的共同形状
  是"判据存在，但判定维度上没人读它的结果"，于是绿灯被当成能力证明。
  - `Pipeline smoke test` 步骤里 `python run.py --check` 跑在 `set +e` 下，**退出码从未
    被读取**，CI 只用 `grep -q "HTML 解析后端: selectolax:"` 卡一行文案 ⇒ 自检里除那一行
    以外的任何结构性故障（缺模块、目录不对、初始化抛异常）都能一路绿。现在把 `rc` 读回
    来并据此红；selectolax 那条 grep 作为**独立**断言保留（它挡的是另一类静默降级）。
    本机 12 条 bootstrap 用例新增 `test_llm_without_credentials_is_warn_not_error` /
    `test_llm_router_crash_stays_error` 锁住这条区分。
  - **缺凭据与装坏了不再混为一谈**：`bootstrap` 的 `LLM 路由器 / 无可用供应商` 由 `error`
    降为 `warn`（路由器真的抛异常仍是 `error`）。否则 keyless 环境永远 `rc=1`，CI 就
    不敢采信退出码——这正是它当初改用 grep 的原因。实测：本机 keyless `--check` 从
    `26 OK / 1 WARN / 1 ERROR, rc=1` 变为 `26 OK / 2 WARN / 0 ERROR, rc=0`。
  - `pip-audit` 进程失败原先被强制 `rc=0`（"跑不起来"当成"跑了且干净"）。现在重试一次，
    仍失败即 `::error::` + `exit 1`。本地对脚本块做三态验证：桩设为失败 ⇒ `rc=1` 并打出
    「查不了」告警；解析出真实漏洞 ⇒ `rc=1`；**干净分支那行 python 解析逻辑与 HEAD 逐字
    一致（去缩进比对为真），本轮未改动**，其在本地桩环境返回 127 只是本机 bash 子进程
    PATH 里没有 `python`（CI 上由 setup-python 提供），不是门禁语义问题。
  - keyless docgen 分支不再报 `SMOKE OK`：改为 `::warning::` + 写入 `$GITHUB_STEP_SUMMARY`
    的 `docgen smoke: SKIPPED (keyless)`。它证明的是"保真底线会拦截"，从来没证明过
    "能成文"，绿灯不该被读成后者。成文能力的证明归属带凭据的 e2e 腿（该腿在同一 SHA
    `14c98b3d` 上目前为 failure，属未收口项）。
  - 语法自证：改动后的两个 `run:` 块 YAML 解析通过（`test` 作业 10 步）且 `bash -n` 无错。
  - **残留风险（须看第一次真实 CI 运行）**：本轮把 `--check` 的 rc 与 pip-audit 的进程
    健康变成硬依赖，若 ubuntu runner 上存在本机没暴露的结构性问题或 PyPI 数据源抖动，
    CI 会**如实变红**——这是修复的目的而非回归，但意味着"CI 全绿"这一条需要重取。

### Fixed（2026-10-07·续3）

- **成品不得夹带抓取层原始素材（FP-1 的余波，由真实 CI 暴露）**。CI run 37504991608 在
  `690c6d7` 上 keyless 产出 **21,926 字节**文档并记为 `OUTPUT FIDELITY OK`；本机同形状产物
  28,412 字节、`quality_gate` 给 **95.5 pass**。占比判据拦不住它——16 个章节里只有 4 个是
  占位符（25% < 34%），其余章节被喂进了 `fetcher._save_article` 的**磁盘格式**：
  `标题: / 来源: / 下载时间: YYYY-MM-DD HH:MM:SS / 60 连等号分隔线`（`agents/fetcher.py:700-703`）。
  - 先试过两个候选指标：段落近重复率、导航样例行占比。实测在真假样本上都不可分离
    （junk 样本 `dup_para_rate=0.167`、`menu_like_rate=0.024`，与真文档同量级）——
    **拿这种指标做门禁等于再造一个假绿**，故弃用。
  - 处置：判据取结构性签名（fetcher 自己的落盘格式），`raw_fetch_block_count() > 0` 即
    `hard_floor`；需要附原文的场景用 `allow_raw_fetch_blocks` 显式放行，且字符串 `"false"`
    不被 `bool()` 判真（`_flag_enabled` 只认显式真值）。
  - 现场复验：本机 keyless docgen 由 `exit 0 + 28.4 KB 落盘` 变为 `exit 1` 并报
    `正文泄漏 3 块抓取层原始素材…`，交付物不再写出。CI 的 keyless 分支因此回到
    `SKIPPED`（诚实），而不是 `OUTPUT FIDELITY OK`（假）。
  - 判据 `TestRawFetchBlockFloor` 五条：素材块头被拒 / 真文档不误伤 / 显式放行生效 /
    `"false"` 字符串不放行 / **签名与 fetcher 落盘格式同源**（用 `inspect.getsource` 断言
    fetcher 格式变了就必须同步改签名，防止判据单方面失效）。
  - CI 侧同判据再落一遍：`.github/workflows/ci.yml` 的 docgen 步骤在 `OUTPUT FIDELITY OK`
    之前自己 grep 一次素材签名，不单独依赖 gate——让 CI 只信 gate 等于把判定权交给可能被改松
    的一侧。本地验证：把该 grep 摘掉 ⇒ `test_ci_step_carries_the_output_fidelity_checks` 红；
    合成样本（含 `下载时间:` 行、长度已过 800 B 下限）判红，同形但去掉签名行的文档判 OK
    ——两份样本都先过了尺寸与占位语两关，所以红只能来自新加的那道签名检查。
- **researcher 池化从"复制"改为"分片"**。`_build_node_payload` 的分片分支原先要求
  `len(all_queries) >= pool_size`，查询词比池实例少时整体退回全量复制。实测后果：
  `pool_size: 2` 的两个 `researcher_pool_*` 返回逐字节相同的结果，同一条 query 出网两遍。
  （`EXTRACTS_QUERIES` 自 3674de6 起就由 researcher 声明、`AgentLoader` 也会把它搬进
  `AgentMeta` —— 坏的只是这个长度门槛，不是声明缺失。）
  - 处置：条件去掉长度门槛，`all_queries[pool_idx::pool_size]` 天然让多余实例领空活
    （如实回报零结果）。
  - 复验：keyless docgen 实测 `researcher_pool_0 queries=1 results=10` /
    `researcher_pool_1 queries=0 results=0`。
  - 判据 `tests/test_pool_sharding.py` 五条，含"未认领标记的 Agent 仍看全量"（writer 这类
    必须看到完整上下文）与一条**接线证明**（`AgentLoader.register` 后
    `Registry.get_meta("researcher").extracts_queries` 必须为真——模块里写了但没进 meta 等于没写）。
- **`--json-output` 的 stdout 契约**。README 承诺它"输出 JSON 结果供 wrapper 解析"，实测
  stdout 上先有 11 行人类可读过程输出（`[run] 加载流水线配置…`、任务头），wrapper 逐行解析
  必炸。处置：该模式下 `sys.stdout` 整体改接 stderr，结果 JSON 由 `output_json_result`
  显式写回 `_RESULT_OUT`（进入该模式前抓下的真 stdout；不在导入期绑死，否则 capsys 失效）；
  `--mcp` **不做**同样重定向（MCP 的协议帧就走 `sys.stdout`）。
  判据 `tests/test_cli_output_channels.py` 三条（单行 JSON / 过程输出没被弄丢 /
  默认模式的人类报告仍在 stdout，防反向回归）。
- **依赖与文档口径**：`pyproject.toml` 补 `render / ingest / ocr / embed` 四个能力 extras
  （此前 python-docx、reportlab、pymupdf 只在 `requirements.txt` 与 Dockerfile 里，
  `pip install .` 与 `pip install -r requirements.txt` 会装出能力不同的两份东西；
  `paddleocr` 被 `docpipeline/ingest.py` import 却两处都没声明）。下限取自本机实测可用版本：
  `ocr` 必须是 `paddleocr>=3.0` + `paddlex[ocr]>=3.0`（本机 paddleocr 3.7.0 / paddlex 3.7.2；
  2.x 没有 `PPStructureV3` 这个符号，只装 paddleocr 时实例化即抛错——`paddlex` 的
  `Provides-Extra` 里确实有 `ocr`）。`all` 有意不含 `ocr / embed`（它们拉起 PaddleX 与 torch）。
  `docs/deployment.md` 三处硬错更正：`POST /tasks` 提交任务（该路由只有 GET，提交入口是
  `/api/tasks`）、"llm_router 支持 12 家"（定义表 16 家，且只有一条 OpenAI 兼容请求路径）、
  WAL 库"停机后直接拷贝即可"（须先 checkpoint 并带走 `-wal/-shm`）。
  `pipeline_core/llm_router.py` 模块注释从"10 个供应商"改为与定义表同源并写清"16 家是端点与
  定价定义，不是 16 套协议实现"。README 的测试数改为实测值（本机全量
  `2239 passed, 2 skipped, 6 deselected`，截至 2026-10-07）。
- **新增文档一致性护栏** `tests/test_doc_consistency.py`：此前 `tests/` 里没有任何一条测试
  引用 README 或 docs，所以"1854 passed"这类数字落后于代码无人发现。三处口径（6 条用例）
  都从代码取数（`--co` 的收集数、生成的 OpenAPI 路径、`provider_defs` 的 AST 长度），不写死常量。
  变异验证：README 改回 1854 ⇒ 红；文档注入 `POST /tasks` ⇒ 红（两处均已还原）。

### Added（2026-10-07·续4）

- **`foreach` 逐项展开接线完成（FP-4）**。此前主干里只有一个解析期助手
  `scheduler._as_foreach`——**零调用点**，而 `naming.py:6` 把 foreach 写成目标能力，
  正是"已声明未接线"那一类。这一轮把它接到底：
  - 解析期（`pipeline_core/scheduler.py`）：新增 `AgentConfig.foreach` /
    `ExecutionNode.foreach`，`_build_plan` 真调用 `_as_foreach` 做规范化
    （`over` 必填非空字符串、只认 `over`/`max_items`、`max_items` 必须 ≥1 整数、缺省 64），
    并拒绝两种组合：`foreach` 与 `call` 同用（并入原有 call 节点配置黑名单）、
    `foreach` 与 `pool_size>1` 同用（"把一个 Agent 拆成多次执行"的两种机制叠加，
    分配语义没有唯一答案）。展开契约进 `topology_hash`（条目形如 `{node}%{json}`），
    改 `over` 或 `max_items` 不重锁即报拓扑漂移。
  - 运行期（`pipeline_core/dag_executor.py`）：投递段抽出 `_request_node_once`
    （普通节点与每一项**共用同一套**"幂等键撞历史 / 空响应"判据，避免长出
    "普通节点报错、逐项放行"的分裂标准），新增 `_execute_foreach`：`over` 在节点自己的
    `when` 上下文里解析（路径语法同源；取不到值 / 非列表 / 空列表 / 超 `max_items`
    全部显式报错，不截断也不静默少跑），逐项深拷贝载荷并注入引擎自有键
    `item`/`index`/`count`（已加入 `artifacts.ENGINE_OWNED_KEYS`），逐项幂等键后缀 `#{i}`、
    逐项领限流令牌，`stop_event` 置位即中止；对声明 `REGENERATION_*` 或 `WRITES_OUTPUT`
    的件直接拒绝，且**拒绝发生在任何投递之前**。
  - **展开声明只认映射**（`isinstance(spec, dict)`，不是判真值）。首轮全量实测暴露了这个
    缺陷：`tests/test_fidelity_gate.py` 的两条用例用 `MagicMock()` 当节点，而 mock 的任意
    属性恒为真值，于是它们被卷进 `_execute_foreach`，报出"writer 同时声明 foreach 与质量
    重做"而不是原本的"未执行：幂等键"。改成只认真映射后两条复绿（解析期产出的规格本来
    只会是 dict 或 None，按类型判不损失任何声明），并在 `tests/test_foreach.py` 里留下
    自己的守卫 `TestDuckTypedNodesStayOnTheOldPath`（含"mock 节点只投递一次"的断言），
    不去改别人的判据。
  - 聚合（`_aggregate_foreach`）：`{status, count, items: [逐项原始结果], ...按 PRODUCES
    聚合的产物}`，str 按行拼接、list 首尾相接、其余收成列表（不给"取最后一项"这种
    覆盖语义——覆盖在逐项展开里等于只剩第 N 项）；`hard_floor` 逐项累积；
    **任一项业务失败或返回 None ⇒ 整个节点 error**。`count`/`items` 在产物之后写入，
    谁声明同名产物也顶不掉。
  - 出厂消费者 `pipelines/api-digest.yaml` + `api-digest.lock`
    （`topology_hash=001d667537bd`）：GitHub issues 列表接口 → `transform` 声明
    `foreach: {over: artifacts.response, max_items: 5}` 逐项渲染 → `safe_writer` 落盘。
    `agents/transform_agent.py` 的 `_context` 补 `index`/`count`，逐项模板才能写
    "第 {{index}}/{{count}} 条"；但三元组**只在真的存在且非 None 时**才放进上下文——
    无条件 `payload.get(key)` 会让没展开的节点把 `{{index}}` 渲染成字面量 `None`、
    把 `{{item.x}}` 渲染成空，等于"少了一列却照样出件"（本仓反复在关的那类静默）。
  - 判据 `tests/test_foreach.py` 46 条：解析期形状与组合拒绝、`topology_hash` 随
    `max_items` 变化且锁拒漂移、逐项载荷/引擎键/幂等键分裂、聚合三型规则、单项失败与
    None 超时均判失败、`hard_floor` 传播、四条边界各自报错且**不发出任何投递**、
    限流超时、`stop_event` 中止、同名产物顶不掉引擎键、`item/index/count ∈
    ENGINE_OWNED_KEYS` 且 `merge_artifact` 拒收、一条盯 `_as_foreach` 调用点的 AST 守卫
    （防它再退回死代码），外加三条离线真跑（3 项渲染 3 行并落盘 / 6 项超上限 ⇒
    `failed` 且不落盘 / 空列表 ⇒ `failed`）；另有 mock 节点仍走单跳投递的守卫一条，
    以及真件 `TransformAgent` 消费三元组的四条（渲染出"第 2/5 条"、没展开时取不到值报错、
    显式 None 不许渲染成字面量 `None`、`0`/`False`/`""` 这类合法项值不误伤）。
  - 变异对照六处，全部被抓住、无 ESCAPED：去掉 `#{i}` 后缀 ⇒ 幂等键测试红
    （三项拿到同一个键 `T:transform:0`）；删掉 `per_payload["item"] = one` ⇒ 载荷测试红；
    `max_items` 判据短路 ⇒ 护栏测试与 E2E 红（`DID NOT RAISE`）；把单项失败洗成成功 ⇒
    `test_any_failed_item_fails_the_node` 红；foreach 不进 hash ⇒ 拓扑测试红
    （`assert '4fb8cf7766e9' != '4fb8cf7766e9'`）；`_as_foreach` 调用点改回 `foreach=None` ⇒
    死代码守卫红（"_as_foreach 又变成死代码了"）。六处逐一还原后 40/40 复绿。
- 文档：README 新增「逐项展开（`foreach`）」小节、场景分级补 `api-report` / `api-digest`
  两行、特性表新增「组合」一行、`call` 节点黑名单补 `foreach`；
  `docs/product-spec.md` §5.1 与 §6 的 FP-4 状态由"死代码 / 未开始"改为已接线（附本轮实测）。
  README 的测试口径改为本轮实测：`2286 passed, 2 skipped, 6 deselected`、coverage 88.19%
  （`--co` 收集 2288，与"passed+skipped"逐项对得上）。
  **（留痕说明：此条为迁出前口径，工具层迁出后见下方 Changed 条目，勿再引用这里的数字。）**

### Changed（2026-10-07·续5，工具层迁出）

- **`fast_json` 与 `selectolax_compat` 迁至独立库 `artesian`，本仓不再留副本**。
  两个模块本就是领域无关的取数/工具层——留在 `pipeline_core` 里既让"引擎不认识领域"
  的分层叙事多两块无主地带，也让新项目（取数/知识底座）没有落地处。单一事实来源
  迁至 `F:\Nexus\artesian`（本地仓，未推送、未发布 PyPI）。
  - **引用重定向 19 处、12 文件**：`from .fast_json import ...`（admin_api/llm_router/
    mcp_server/message_store/search_engines/task_queue 六文件各 2 行、streaming 1 行）、
    `from pipeline_core.fast_json import ...`（writer 2 行）、
    `from pipeline_core import selectolax_compat`（fetcher / benchmark / test_html_backend）、
    `bootstrap.py` 启动自检里的懒导入。全部改为 `artesian.*`；替换脚本逐处断言
    "恰好命中 1 次"后才写盘，重排由 ruff `I` 规则完成并人工复核 diff。
  - **行为变更（唯一一处）**：HTML 后端固定用的环境变量由 `DOC_PIPELINE_HTML_BACKEND`
    改名为 `ARTESIAN_HTML_BACKEND`，**旧名不再被识别**。旧名原字面量只出现在
    `selectolax_compat.py` 定义处（测试经 `compat.ENV_OVERRIDE` 常量引用，不受影响）；
    全仓（含 CI/文档）已确认无其它使用点。依赖旧环境变量固定内核的部署需同步改名。
  - **判据去向**：`tests/test_fast_json.py`（19 条）随库走，并在新库加强到 35 条
    （补了"无 orjson 环境"强制重载真跑、selectolax 适配面逐接口、导入失败留痕等）；
    `tests/test_html_backend.py` 留在本仓（fixture 依赖 FetcherAgent），仅改导入指向，
    断言未弱化。新库自带 coverage 门禁（fail_under=90，首轮实测 100%）。
  - **安装过渡**：artesian 未发布 PyPI，开发期需 `pip install -e ../artesian`
    （requirements.txt 顶部已注明）；发布后改为版本约束依赖。依赖方向：本仓 -> artesian。
    **CI 尚未接线（推送前阻塞项）**：`.github/workflows/ci.yml` 的 Install 步骤只装
    requirements.txt，装不到 artesian；本批刻意不推（两仓都在本地），推之前必须先让
    artesian 可安装（推仓后 CI 加 `pip install git+https://...@<sha>`，或先发 PyPI）。
    `run.py --check` 与全量测试在本机（已装 artesian）均为绿，CI 差异仅此一条。
  - **测试口径**：`2286 passed → 2268 passed`（-18 条随库移出、-1 条 skipif 随库移出），
    覆盖 `tests/test_doc_consistency.py` 的 README 计数护栏已同批更新。

### Changed（2026-10-07·续6，嵌入层与知识库迁出）

- **`embeddings` 与 `knowledge_base` 迁至 `artesian`，本仓不再留副本**（第二波，承续5）。
  两者与 doc-pipeline 领域零耦合（迁移前按 import 与字面量双重探明），归位后
  `pipeline_core` 只剩引擎与状态设施；下一波 `search_engines`（1223 行）的路由此铺平。
  - **引用重定向 9 处、5 文件**：`agents/knowledge_base_agent.py` 2 行、
    `tests/test_kb_pipeline_wiring.py` 3 行、`spike/check_embeddings.py` /
    `spike/check_kb.py` / `spike/check_kb_rebuild.py` 共 4 行；替换脚本逐处断言
    "恰好命中 1 次"后才写盘，ruff `I` 规则重排后人工复核 diff。
  - **行为变更：无**。环境变量（EMBEDDING_API_KEY / OPENAI_API_KEY / *_BASE_URL /
    HF_HUB_OFFLINE）与续5 的 HTML 后端不同，均未改名——本就与 doc-pipeline 无关。
  - **迁移引入的唯一回归及修复**：artesian 无 PEP 561 标记时消费方 mypy 视其为 Any，
    `KnowledgeBase.search` 的返回形状失守，CI 口径 mypy 报
    `agents/knowledge_base_agent.py:298: [no-any-return]`。双侧处置：artesian 随包发
    `py.typed`（已核 wheel 产物确含该文件），调用点显式标注 `res: dict` 钉住契约形状；
    修复后 CI 镜像 mypy（pipeline_core/ docpipeline/ agents/，58 文件）零 issue。
  - **判据去向**：`tests/test_knowledge_base.py`（74 条）随库走，并在新库加强到
    91 条（local/api 后端用假模块注入逐分支真跑、model_is_cached 五分支、
    离线探测环境变量还原、auto 回落原因），全程不联网、不下载模型权重；
    新库 coverage 实测 98.29%。
  - **测试口径**：`2268 passed → 2194 passed`（-74 条随库移出；README 句式为
    "2194 passed, 1 skipped, 6 deselected"），`tests/test_doc_consistency.py`
    计数护栏已同批更新并复跑为绿；coverage 本仓实测 88.26% → 87.93%（门禁 83% 不变）。
  - **CI 阻塞项不变（推送前阻塞）**：artesian 未发布 PyPI 且未推远端，CI 装不到；
    本批两仓仍均只作本地提交（artesian `ee9a689` + `05c6375`）。`run.py --check`
    与全量测试在本机（已装 artesian）均为绿，CI 差异仅此一条。

### Changed（2026-10-07·续7，搜索引擎迁出·迁出边界收口）

- **`search_engines` 迁至 `artesian`（第三波，规划的 5 个模块至此全部落地）**，
  本仓不再留副本。为把两处仓内耦合断掉，新库另落两件底座：`artesian.cache`
  （线程安全 LRU+TTL）与 `artesian.env`（`.env` 读取与系统环境合并）。
  - **引用重定向 15 处 / 9 文件**：`agents/fetcher.py`、`agents/researcher.py`、
    `docpipeline/document_enhancer.py`、`pipeline_core/admin_api.py`、
    `pipeline_core/bootstrap.py` 各 1 处；`tests/test_e2e_real.py` 3、
    `tests/test_researcher.py` 3、`tests/test_e2e_mock.py` 2、
    `tests/test_admin_api_ext.py` 2（后四处含 `patch("…SearchEngineManager.from_env")`
    的**字符串目标**——这类目标改错不会 ImportError，只会让 mock 打空、用例照绿，
    故逐处按"恰好命中 N 次"断言后替换）。
    另有 `pipeline_core/__init__.py` 删两行再导出（`SearchEngineManager` / `SearchItem`
    实测无任何消费方走包级路径，全部走 submodule）。
  - **耦合一（`_load_env`）——归一而非复制**：实现搬进 `artesian.env.load_env`，
    `llm_router._load_env` 改为它的**别名**（`_load_env = load_env`）。这样本仓调用点、
    `patch("pipeline_core.llm_router._load_env")`、`from pipeline_core.llm_router import
    _load_env` 三方口径都不变（实测 `lr._load_env.__module__ == "artesian.env"`）。
    语义细节随实现一起搬走：`.env` 空值不算配置、系统**非空**值优先、
    系统空值不得抹掉文件里的有效值。
  - **耦合二（CacheManager）——换成库内 LRUCache，并去掉一条不可能分支**：
    原 `SearchEngineManager.__init__` 用 `try: from .cache_manager import CacheManager`
    做"可选依赖装不上就降级无缓存"。缓存实现进了同一个包之后这条分支不再可能发生，
    留着就是给覆盖率报一个永不执行的分支：改为无条件构造 `LRUCache(max_size, ttl)`，
    并把"关掉缓存"做成显式能力 `SearchEngineManager(cache_ttl=-1)`（LRUCache 的
    负 TTL＝读写皆空转）。判据换成带**正对照**的一条：关缓存时两次搜索都真打引擎
    且 `size()==0`，默认开缓存时 `size()==1`——只断言"缓存为空"在两种实现下都会成立。
    - **可观测性核对**：`admin_api` 的缓存统计走 `cache_manager.all_stats()`，而
      `_registry` 只由 `get_cache()` 写入、`CacheManager.__init__` 不登记
      （`cache_manager.py:400-409`）⇒ 搜索缓存此前就不在这份账里，本次换实现**没有**
      丢统计。
  - **行为变更（唯一一处对外可见）**：`ProSearchEngine` 不再内置两条本机绝对路径
    （`F:\Program Files\QClaw\…` 与 `F:\Program Files (x86)\qclaw\…`）。改为
    `PROSEARCH_PATH`（单条）/ `ARTESIAN_PROSEARCH_PATHS`（os.pathsep 分隔候选）/
    `ProSearchEngine(script_paths=[...])` 三种显式注入，`PROSEARCH_SCRIPT_PATH`
    的旧覆盖语义保留。**本机实测这两条路径都不存在** ⇒ 本机可用性不变（该引擎此前
    就自动跳过）。`.env.example` 已补逃生门说明；新库另加一条源码护栏判据
    "模块里不得出现盘符路径字面量"，并用合成变异验过它不误报 `https://`。
  - **分层护栏被自己触发了一次**：`tests/test_layering.py` 的"docpipeline 外部依赖
    必须逐条登记"如期判红（`artesian` 未在册）。处置是新增 `LOCAL_LIBRARY={"artesian"}`
    类别，并补一条同源判据 `test_local_library_install_is_documented`——登记为本地库的
    依赖必须在 `requirements.txt` 写明 `pip install -e ../artesian`，否则新环境按
    requirements 装完只会得到 ImportError（artesian 不在 PyPI，进不了包名行，
    这条判据就是那一类的替代品）。
  - **判据去向**：`tests/test_search_engines.py`（30 条）与 `tests/test_search_engines_ext.py`
    （61 条）随库走；新库里分别为 31 / 61 条（补了 `FirecrawlExtractor.is_available`
    ——此前由本仓 agents 用例顺带覆盖，模块迁出后那层不在库的覆盖范围里，判据得自带），
    另新写 `tests/test_cache_and_env.py` 30 条（LRU 逐出顺序、TTL 三档、`.env` 合并
    优先级、ProSearch 注入面）。`_fake_aiohttp_module` 原先从 `tests.test_writer`
    import，随库走时改为文件内自带（去掉搜索面用不到的流式分支）。
  - **测试口径**：`2194 passed → 2104 passed`（-91 条随库移出、+1 条新分层判据），
    README 计数护栏同批更新并复跑为绿；本仓 coverage 88.26%→87.93%→**87.90%**
    （门禁 83% 不变），mypy 口径 58 文件 → 57 文件；CI 镜像 mypy/ruff 均绿，
    `run.py --check` 的引擎行与迁移前一致（`5 个引擎可用: bing, baidu, sogou, 360,
    duckduckgo`）。新库 artesian：**248 条**（本机 `247 passed, 1 skipped`），
    coverage **97.06%**（cache/env/fast_json/embeddings/selectolax_compat 100%，
    search_engines/knowledge_base 96%）。
  - **迁出后的遗留议题**（不在本批处置）：2026-07-22 评审旧账 #5（HTML 引擎与
    Firecrawl 对可控 URL 无私网防护）随模块一起归 artesian——本仓 `url_guard.py`
    并未随迁，该判据要在库侧另立，属下一议题。**CI 阻塞项不变**：artesian 仍未发布，
    本批两仓均只本地提交（artesian `ee9a689`/`05c6375` 之后的第三笔）。

### Docs（2026-10-07·续8，三波迁出后的口径清点与镜像阻塞项）

- **清点结论：静态门禁没有因删模块而失明**。CI 的四处口径全是按目录走的 glob——
  `py_compile pipeline_core/*.py docpipeline/*.py agents/*.py run.py`（`ci.yml:52`）、
  `mypy pipeline_core/ docpipeline/ agents/`（`:57`）、`bandit -r pipeline_core
  docpipeline agents`（`:67`）、coverage `include`（pyproject，四个目录）；
  被删模块自然脱离这些 glob，属预期（实现与判据已在 artesian 侧，那边有自己的门禁）。
  本机复跑 `run.py --check`：rc=0，且 CI 用来防"静默降级"的那条
  `grep "HTML 解析后端: selectolax:"` 仍命中（`:112`）⇒ 门禁语义未被迁移削弱。
- **发现一条新的推送前阻塞项（此前只记了 CI 那半）**：`Dockerfile:19-20` 只
  `pip install -r requirements.txt`，第 45 行再 `COPY . .`——镜像里不会有 `artesian`，
  而依赖发生在**导入期**：本机把 `sys.modules["artesian"]=None` 后
  `import pipeline_core` 立刻 `ModuleNotFoundError: No module named 'artesian.fast_json'`
  ⇒ 现构建出的镜像在 ENTRYPOINT 就死。`README.md` 的 Docker 一节与
  `docs/deployment.md` §3.4 教的 `docker build` 因此在补齐安装源之前不成立，
  两处已就地写明前置条件（没有删部署章节，也没有把它写成"能用"）。
- **修法已在真机验证过形态，只缺一次授权**：把 pin 写进 requirements.txt 一行即可
  同时救活 CI 与镜像（两边的安装步骤都是 `pip install -r requirements.txt`）。
  机制本机验通：`pip install --no-deps git+file:///F:/Nexus/artesian@2413023…`
  安装成功后，用该 venv 解释器跑消费方套件 `162 passed`，再跑分层/KB 接线
  `42 passed`；`artesian.__file__` 落在 site-packages（非 editable），`py.typed`
  随包到位。产物核对：wheel 与 sdist 均含 8 个文件
  （`__init__ / fast_json / selectolax_compat / embeddings / knowledge_base /
  search_engines / cache / env` + `py.typed`）。
- **待拍板的两条路**（择一，另一条留作后续）：① artesian 建公开仓 →
  `artesian @ git+https://github.com/Levango7/artesian.git@<sha>`（pin 要随库改动更新；
  私有仓则 CI 需要额外凭据，`pip` 匿名读不了）；② 发 PyPI 0.1.0 →
  `artesian>=0.1.0`（无 git 依赖，但发布近似不可撤；`artesian` 这个包名 2026-10-07
  实测 PyPI 上未被占用，查询返回 404）。

### Changed（2026-10-07·续9，接线落地：公开仓 + git 直接引用 pin）

- **选了①并已落地**：`Levango7/artesian` 建为**公开仓**（`gh api` 核对 `public=true`、
  默认分支 `main`），5 笔提交推上去后远端 `refs/heads/main` == 本地 HEAD
  （`24130232e20b9106fb556e10877f62bebd4a407f`，匿名 `git ls-remote` 与 codeload 都是 200）。
  requirements.txt 因此加上一行直接引用：

  ```
  artesian @ git+https://github.com/Levango7/artesian.git@24130232e20b9106fb556e10877f62bebd4a407f
  ```

  **为什么写在这里而不是 CI 里加一步**：CI 与 Dockerfile 的安装动作是同一句
  `pip install -r requirements.txt`（`ci.yml:37/193/272`、`Dockerfile:20`），
  一行同时救两侧；而且照 README/CONTRIBUTING 装环境的用户也天然装得到——
  依赖是导入期的，缺它就在 `import pipeline_core` 当场炸。
- **判据跟着换**（`tests/test_layering.py`）：`test_local_library_install_is_documented`
  → `test_local_library_has_reproducible_install_source`。旧的那条认的是
  "注释里留一句 `pip install -e ../artesian`"，而注释对 CI/镜像无效——正是这次要治的病。
  新判据只接受两种形态：sha pin 的 git 直接引用（且必须**完整 40 位**，分支名会让同一份
  清单在不同时间装出不同的库），或发布 PyPI 之后的版本约束行。
  六例合成变异验过它不空转（现状 PASS／分支名 FAIL／7 位短 sha FAIL／只有注释 FAIL／
  什么都没有 FAIL／`artesian>=0.1.0` PASS）——调的是判据本身，不是复刻的逻辑。
- **真机把 CI 那条路走通了一遍**：新 venv 里 `pip install --no-deps
  "artesian @ git+https://github.com/Levango7/artesian.git@<sha>"` 成功，
  `artesian.__file__` 落在 site-packages（非 editable）、9 个文件含 `py.typed` 齐全；
  用该解释器跑本仓消费方套件（kb 接线 / HTML 后端 / researcher / 分层 / llm_router /
  文档一致性）**162 passed**。产物侧另核过 wheel 与 sdist 内容一致。
- **一条如实的副作用**：`pip-audit -r requirements.txt` 现在会为满足这个 VCS 需求去
  clone 仓库——本机实测 rc=0，并顺带把 artesian 的传递依赖 `orjson` 纳入了审计；
  但对 `artesian` 自身，审计结果是 `version=None / vulns=[]`，即**它不在漏洞数据源里**。
  ⇒ "第三方漏洞扫描覆盖了取数底座"这句话不成立，别这么读；要覆盖它得先发布 PyPI。
- **本机网络事实（影响复现 CI 的手法）**：`github.com:443` 直连被拦
  （`Failed to connect to github.com port 443 after 21098 ms`），全局
  `url.https://gh-proxy.com/...insteadOf https://github.com/` 的镜像改写才是本机通途。
  第一次我为了"忠实模拟 CI"把 `GIT_CONFIG_GLOBAL` 指到空文件，结果拿到的是**假红**
  （clone 连不上）；GitHub runner 上没有这层改写，不受此影响。差异记下来免得下次再踩。
- 部署两处口径（`README.md` Docker 一节、`docs/deployment.md` §3.4）由"目前做不到"
  改回"已就绪"，并保留 sha 更新义务与导入期依赖这两点提醒。

### Fixed（2026-10-07·续10，续9 的 pin 把镜像构建打红：slim 基础镜像没有 git）

- **现象**：推上去的 `37240e0` 触发 run 37635875679，`docker` job 红在
  `Docker build validation` 一步，正文是
  `ERROR: Cannot find command 'git' - do you have 'git' installed and in your PATH?`
  （紧接着 `Collecting artesian@ git+https://github.com/Levango7/artesian.git@2413…` →
  `Error [Errno 2] No such file or directory: 'git' while executing command git version`）。
  上一笔 main `e4c02b4`（2026-10-06）同一 job 是 success ⇒ **是本批 pin 引入的**，不是环境抖动。
  同一次 run 里其余 job 全绿（`test (3.11/3.12/3.13/3.14)` 各 0 个失败步、
  `perf-regression` success、`refresh-baseline` skipped）——runner 自带 git，
  只有容器里没有；这四条矩阵同时也是"三波迁出 + git pin"在干净环境下的通过证据。
- **根因**：pip 满足 VCS 直接引用要**调用 git 可执行文件**，而 `python:3.12-slim`
  不带 git；Dockerfile 里那句 `# Install build tools (none needed — pure Python deps)`
  正是这次被证伪的前提——三波迁出之后依赖已经不"纯 Python"了。
- **处置**：builder 阶段先 `apt-get install --no-install-recommends -y git`（同一条 RUN 里
  `apt-get update` + 清 `lists`），注释改成实情。放在 builder 是有意的：运行阶段只
  `COPY --from=builder /opt/venv`，apt 包不进最终镜像。**没有**改用
  `artesian @ https://…/archive/<sha>.tar.gz` 绕开 git——那种形态在本机反而更糟：
  pip 走 urllib 直连 github.com:443（本机被拦），而 git 那条路经全局 `url.insteadOf`
  镜像改写是通的（本机实测 pin 安装成功）。
- **判据（防同源漂移再犯）**：新增 `tests/test_layering.py::
  test_vcs_requirements_are_installable_in_the_image`——requirements 里只要出现 `git+`
  直接引用，Dockerfile 就必须在"装 requirements 的那一步"**之前**装 git。
  判据绑的是顺序而不是"文件里有没有 git 这个词"（装在后面等于没装）。
  五例合成变异验过它不空转：修法 PASS／git 装晚 FAIL／压根没装 FAIL／
  无 VCS 依赖时不适用 PASS／Dockerfile 里找不到 pip 行 FAIL（这条还顺带钉住
  "镜像确实从同一份 requirements 装"这个前提，口径换了判据先失明）。
- **取证过程中的两条手法账**（都实测过）：① run 未结束时 `gh run view --log/--log-failed`
  是**空的**，必须等 run 终态；② `actions/workflows/<id>/jobs` 这类列表接口在本机
  经镜像会 404，`gh run list --json` + `actions/runs/<run_id>/jobs` 才是稳的路子。
  另外 `run_number` 与 API 要的 `run_id` 不是一回事（拿 `runs/1` 查会 404）。
- **复验（2026-10-07 同日，`371140d`）**：run 37637367530（#103）`completed/success`，268s。
  `docker` job 5/5 步绿（真实 `docker build` 过了）；`test (3.11/3.12/3.13/3.14)` 四条矩阵
  全 success——3.12 正文 `2095 passed, 10 skipped, 6 deselected`、
  `Total coverage: 87.34%`（门禁 83%）；`perf-regression` success，`refresh-baseline` skipped。
  CI 收集数 2105 与本机一致，只是 skip 分配不同（本机 1 条、CI 10 条：渲染/OCR/嵌入类
  `skipif` 取决于该 job 装了哪些可选依赖），这正是 README 测试一节预留的那句出入。
  artesian 侧 run 37632571610（#1）三档矩阵各 10/10 步绿，正文 `247 passed, 1 skipped`、
  coverage 96.99%。

### Changed（2026-10-07·续11，url_guard 归位 + 搜索层 SSRF 防线；库侧首发 v0.1.0）

- **第 6 个模块 `url_guard` 归位 artesian**（迁出边界从 5 个模块扩到 6 个）。动机是
  2026-07-22 评审旧账 #5 随 `search_engines` 落到了库侧，而它要用的校验函数还在本仓——
  两边都缺一个共同的家。搬完后单一来源在 `artesian.url_guard`，本仓三处产品码
  （`agents/fetcher.py`、`agents/http_request_agent.py`、`pipeline_core/event_hook.py`）
  与两处测试改为引用库路径，共 **5 文件 6 处**（含 `http_request_agent` 顶部那条
  写着旧路径的规范注释）；`pipeline_core/url_guard.py` 与 `tests/test_url_guard.py`
  删除，实现零改动（只把 docstring 里点名的消费方改成中性表述）。
- **库侧新增防线（行为变更都在 artesian，本仓无感）**：
  ① 搜索层 7 处出网统一走 `_guarded_urlopen`，**重定向每一跳**重新过 SSRF 校验；
  校验只作用于重定向、首发 URL 仍由调用方/运维配置决定 ⇒ 自托管 Firecrawl、
  内网代理这类合法端点不被误伤。② `FirecrawlExtractor.scrape(url)` 的目标 URL
  按**不可信输入**处理（它来自搜索结果），先校验再发，拒绝时沿用既有错误字典形态
  返回且一个包都不发。本仓 fetcher 拿到 `success=False` 后自然回落普通下载路径，
  而那条路本来就有 SSRF 校验 ⇒ 没有新的崩溃面，也没有"防护一换就漏一段"的空档。
- **判据**：52 条随库走；库侧新写 9 条（handler 单测 + **真 socket 本地跳转服务**
  端到端 + Firecrawl 拒绝路径）。端到端那条带正对照——同一跳板用原生 `urlopen`
  必须真把"内网机密"取回来，否则"被拦下"在"服务压根没重定向"的夹具下也成立。
  放行侧也各有对照（公网字面量 IP 照常请求 / 照常发包），防的是"恒拒绝"实现照样绿。
- **一条手法账（本仓 editable 安装造成的 split-brain）**：搬完之后 artesian 里
  4 条 url_guard 用例红得莫名其妙（`calls == []` 对上 `[IPv4Address('8.8.8.8')]`、
  `KeyError: 'ttl.com'`）。根因是那份测试里有 **12 处函数体内的
  `import pipeline_core.url_guard as url_guard`**——本仓以 editable 方式装着，
  旧模块仍在 `sys.path` 上，于是这些用例一边调新模块的函数、一边 patch/检查
  旧模块的状态，验的是另一份副本。按行首 `^from|^import` 扫的引用清单会整类漏掉
  这种写法。**做法改成**：搬完先 `grep -c <旧包名>` 全量数一遍（不限行首形态），
  并**删掉旧文件后重跑**来逼出隐式引用；本次据此把 12 处收敛成模块级一次导入。
- **测试口径**：`2104 → 2053 passed`（-52 随库走、+1 上一波新增的分层判据），
  coverage 87.90%→**87.84%**（门禁 83%），mypy 口径仍 57 文件。
- **artesian 首发 `v0.1.0`**：annotated tag 打在 `203987a`（== 本批 requirements 的
  pin），并建 GitHub Release（含模块清单、安装形态、质量口径与"已知边界"三条）。
  requirements.txt 的直接引用同步换 sha；本机按新 sha 重新安装核实过：装出来的包里
  有 `url_guard.py`、`_guarded_urlopen`/`_SsrfRedirectHandler` 都在，且 Firecrawl
  对 `169.254.169.254` 的拒绝日志确实来自那份安装产物。
- **CI 复验**：artesian run 37641435745（#2）`completed/success`，三档矩阵各 10/10 步，
  3.12 正文 `308 passed, 1 skipped`、coverage 97.01%——本地跳转服务那条判据在
  Linux runner 上也是真跑的（监听套接字没被沙箱挡）。
  本仓 run 37643026175（#105）`completed/success`：`test (3.11/3.12/3.13/3.14)`、
  `docker`、`perf-regression` 全绿，3.12 正文 `2043 passed, 10 skipped, 6 deselected`、
  coverage 87.28%；这一笔的 requirements 装的就是 `203987a`，所以 CI 侧的
  `artesian.url_guard` 与逐跳护栏是**从 GitHub 取来的那份**在跑，不是本机 editable 的副本。
- **顺手把"CI 比本机少 1 条"归因清楚**（不拿"环境差异"糊过去）：按 nodeid 做集合差
  时先要堵两个坑——本仓 `addopts` 带 `-v` 会把 `--co -q` 变成树形（要用 `-o addopts=` 关掉），
  日志里的用例行带 ANSI 颜色码（不剥就漏 9 条）。真差异只有一条：
  `tests/test_html_backend.py::TestKernelExtraction::test_kernel_meets_extraction_invariants[modest]`。
  它的参数来自 `parametrize("backend", _available_kernels())`——**条数由可选依赖决定**：
  本机 `modest`+`lexbor` 两个内核都能 import（2 条），runner 上只有 `lexbor`（1 条），
  而那条在 CI 是 PASSED。本机 2054 selected / CI 2053 selected，差 1 ⇒ 计数护栏的
  ±10 容忍覆盖得到，两侧都绿是正当结果而不是掩盖。

### Added（2026-10-06，未发版）

- **子流水线参数化（`call.inputs` + `when.value_from`）**：`call` 节点可以向片段传实参，
  片段节点的 `when` 用 `inputs.*` 读（左值 `path` 与右值 `value_from` 都支持）。
  - `value` / `value_from` 必须恰好给一个；`inputs` 的键不得含点号（点号是路径分隔符）。
  - 作用域三条：普通节点上的 `inputs` 是**默认值**且必须被自己的 `when` 读到；内联时
    只把该节点会读的键落到节点上；嵌套 `call` 各用各的实参，外层不覆盖内层。
  - 解析期双向查参数：引用了没人传 → 报错（`all:` 的短路会让缺参静默绕过）；传了没人读
    → 报错（`min_scor: 70` 这类拼写错误原本会静默走默认值）。
  - `inputs` 参与 `topology_hash`：改实参会让调用方 lockfile 报漂移。
- **出厂片段 `pipelines/_quality-tail.yaml`**：质量尾（quality_gate → checker →
  可选 fact_checker → layout → safe_writer）。四条 docgen 流水线的这一段逐字节比对后
  只差 fact_checker 的有无与门槛，于是差异收敛成 `fact_check` / `min_score` 两个参数：
  docgen 与 docgen-render 传 `fact_check: false`，docgen-verified 传
  `fact_check: true, min_score: 0`（无条件核查），docgen-lean 传 `min_score: 70`
  （达标才核查，取代原先写死在条件里的 70）。
- **片段的目录解析**：`parse_file` 把引用方所在目录传给内联逻辑，于是
  `--pipeline-file /tmp/x.yaml` 能在 `/tmp` 找到它的片段，而不是回头看进程 cwd。
  `_` 前缀的片段不进 `--pipeline` 候选清单（`installed_pipelines` /
  `list_pipelines` / run.py 三处口径一致），但仍可被 `call` 加载。

### Fixed（2026-10-06）

- **内联展开把父图前置接到子流水线的每个节点**：入口判据写成 `d in renamed`，而那时
  `dependencies` 已经是改名**之后**的身份（`renamed` 的键是改名前的名字），于是恒不命中、
  每个内联节点都被当成入口。实测后果：docgen 的 `writer` 成了 fact_checker / layout
  的依赖，层级与并发窗口跟着错，`when` 求值也会因"依赖未成功"被误跳过。
  判据改用改名后的身份集合（`test_parent_edges_move_to_sub_boundary` 现在同时断言
  "非入口不得带父图前置"）。
- **内联节点的 Agent 能力整段失效**：`registry.get_meta(node.agent_name)` 用别名名查表
  得到 `None`，`None` 不抛错，只是能力没了。两处真机撞出来：
  `WRITES_OUTPUT` 失效 → `task.output_path` 不记 → 交付契约反过来说"声明了落盘节点却没有
  交付物"（文件 24 KB 明明在）；`SUPPORTS_REGENERATION` 失效 → 内联的 quality_gate
  不再触发重做，质量门降级成一次性打分。改为按 `agent_of` 后的 base 名查，
  复检目标（`REGENERATION_RECHECK` 缺省取本节点名）也还原成 base 名，否则 RPC topic
  `quality_gate__tail.input` 没有订阅者。判据：`TestInlinedNodesKeepAgentCapabilities`
  两条，把修复回退会双双转红。
- **报表里质量门控警告静默消失**：`run.py` 还用 `(task.result or {}).get("quality_gate")`，
  内联后键名是 `quality_gate__quality_tail` → 拿空 → 低分文档只剩"执行完成"。
  与 writer 一样改走 `_result_for`（同名多处出现时不猜）。
- **`Scheduler.validate()` 查不到内联/池化节点的 Agent 文件**：它用原始节点名拼
  `agents/<name>.py`，`writer_pool_0` 早就拼错（该方法目前无调用方，见待办）。
  改为先 `agent_of()` 还原。（附带记录：`validate()` 全仓零调用方，
  `config_schema.py` 注释里"语法坏掉的 Agent 文件由 validate() 另行报告"因此落空。）
- **YAML 布尔名字陷阱**：`- name: on` 被 YAML 1.1 解析成 `True`，报错落在几千行之外的
  `'bool' object has no attribute 'split'`。解析期加判据并点名这个陷阱。

### Docs（2026-10-06）

- README：`value_from`、`call.inputs` 与默认值/作用域表、`_quality-tail` 片段说明；
  求值上下文表加 `inputs.*` 一行。
- docs/architecture.md：§1.4 补"能力也按 Agent 名解析"两处教训；新增 §1.5
  片段与参数作用域（含锁定覆盖面、`_` 前缀约定、同目录要求）。

### 拓扑与锁文件差异（2026-10-06，四条流水线 + 新片段）

| 流水线 | node_count | topology_hash | 锁里节点名变化 |
|---|---|---|---|
| docgen | 8 → 9 | `6f706b933bce` → `1c3199450b61` | 去 `checker/layout/quality_gate/safe_writer`；加同名 `__quality_tail` 五个（多出 `fact_checker__quality_tail`，`fact_check: false` 时被条件跳过） |
| docgen-render | 9 → 10 | `23d5d9b92290` → `3a28a72ff2e2` | 同上；`renderer` 的依赖由 `safe_writer` 改接到 `safe_writer__quality_tail` |
| docgen-verified | 9 → 9 | `34a01b8c2d1b` → `697f95b94353` | 五个尾巴节点全部换成 `__quality_tail` 身份；`fact_checker` 从"无条件"变成"`fact_check: true, min_score: 0`" |
| docgen-lean | 9 → 9 | `94e9b0597cc8` → `a6b8de38ebd4` | 同上；阈值 70 从写死在 `when.value` 变成调用方 `inputs.min_score` |
| _quality-tail（新） | — → 5 | 新 `f88f21d8a323` | 片段的默认展开：`fact_checker` 带默认值 `{fact_check: true, min_score: 0}`，因此片段自身也能解析/加锁 |

四条都重新 `--write-lock`；存活名字之间 `config_hash` 无一漂移（配置逐字节照抄，只换了归属文件）。
三条 smoke 实跑：docgen `done`/exit 0（fact_checker ⏭️ 条件跳过）、docgen-verified `done`
（fact_checker 实跑）、docgen-render `done`（renderer 因 python-docx 对占位稿报 XML 控制字符
错误而 ❌，与本次抽取无关，另立待办）。

### Added（2026-10-06·续，未发版）

- **docreq / kb-docgen 也接上质量尾片段**（承接上面"四条"的范围，现共六条流水线引用
  `_quality-tail`）。两条的尾巴与片段逐字段相等（threshold 70、max_regenerations 3、
  timeout 300、`fail_fast: false`、`style: markdown`、`backup_dir/atomic`），
  都只要 `inputs: {fact_check: false}`。上一笔写"未引用片段的四条…逐字段未变"，
  那是当时的范围决定而非结论——`three_pass` 才是不该并入的那条
  （threshold 65 / max_regenerations 2 / 超时 120·60 与片段不同）。
- 平价判据（脚本比对，不是断言）：抽取前的 lock（`/tmp/locks_before`）与重锁后的 lock
  逐节点比 `config_hash` / `version` / `pool_size`，六条流水线共 **26 组，0 处不一致**。
- 拓扑与锁文件差异（本笔两条）：

  | 流水线 | node_count | topology_hash | 节点名变化 |
  |---|---|---|---|
  | docreq | 9 → 10 | `06008455dc1f` → `13ab15c3b1b5` | 去 `checker/layout/quality_gate/safe_writer`；加同名 `__quality_tail` 五个（多出 `fact_checker__quality_tail`，被条件跳过） |
  | kb-docgen | 7 → 8 | `e196e27b5b09` → `9297257cbd1d` | 同上 |

### Fixed（2026-10-06·续）

- **perf-regression 门禁改成噪声感知**（`benchmark.py`，#19）。原来的判据是
  "单样本比基线慢 30% 就红"，在共享 runner 上就是抽签：CI run 37350496361 把
  并行执行 `serial 0.02656→0.03671 秒`（绝对差 10 毫秒）判成 38.2% 回归，
  内置的"复验"再判一次还是 38%（两次采样取自同一台被负载污染的机器，复验并不能否证），
  同一个 commit 反复重跑也只是在同一台机器上再抽一次签。现在：
  - CI 默认 `--samples 3`，逐项取**中位数**（一次被抢占的采样拖不偏中位数），
    并算出本轮相对波动 `(max-min)/median`；
  - 判红要同时过三关：相对阈、该指标的绝对下限（`METRIC_FLOORS`，按本机 4 次独立跑
    实测的噪声与量级标定）、变化量超过 2×噪声带（带取本轮波动 / 基线记录波动 /
    最近 20 轮历史跨 run 抖动三者的最大值）；
  - 过不了后两关的项打 **`UNVERIFIED`**：显式打印、退出码 0，绝不冒充"通过"，
    也不冒充"回归"；基线里新增 `_env`（system/machine/python/cpu_count）与 `_spreads`，
    环境指纹变了就整体降级为不可判并提示重立基线；
  - `并行执行` 的 serial/thread_pool/process_pool 划入 `REPORT_ONLY_METRICS`：
    它们测的是池 spawn 成本（本机跨次抖 2.6–14.4%），照旧测量与进趋势但不许判红；
    "并行还能不能用"由 `tests/test_executor_factory.py` 的正确性断言负责。
  - 判据本身有测试：三个方向的变异（取消绝对下限 / 取消只观测豁免 / 噪声退回只看本轮）
    各自让对应用例转红；端到端合成三例——全指标慢一倍 ⇒ exit 1（7 项确认）、
    复刻 CI 那组并行三项 +38% ⇒ exit 0、只让 TF-IDF 真的慢 64% ⇒ exit 1。
  - 附带：`.gitignore` 补 `benchmark_history.jsonl`（此前 untracked 且未忽略，
    `git add .` 会把它带进提交）；CI 的 perf 缓存改为基线与历史**成对** restore/save，
    否则波动带在 CI 里永远攒不到样本；`ci.yml` 里两处 save 步骤同步。
- **上一笔的 Docs 条目领先于文件**：那条说 README 已有"`call.inputs` 与默认值/作用域表、
  `_quality-tail` 片段说明"，实际只有求值上下文表的 `inputs.*` 一行和 `value_from`
  片段两处落地——那次批量编辑里最大的一块（作用域表 + 片段说明）因锚点文本不一致没有
  应用。旧条目保留不删，此处补记。现在 README 的子流水线一节有了 `inputs` 示例、
  作用域表、六条引用关系与 `_` 前缀约定；`pipelines/` 目录树补上 `_quality-tail.yaml`
  与 `docreq.yaml`，并标注 `three_pass` 为何不引用片段。

### Added（2026-10-06·续2，未发版）

- **AgentLoader 复用已导入的模块对象**（#17 收口）：`register()` 原来每次都
  `module_from_spec + exec_module` 并覆写 `sys.modules["agents.<name>"]`，同一个 Agent
  因此存在两份类对象——测试里 `patch("agents.writer.WriterAgent.handle")` 打的是先导入
  的那一份，注册器造的实例用的是另一份，补丁全程空转、用例照样绿（本仓库已因此收回过
  两次"绿了"的结论）。现在默认复用，并区分三种边界：同一文件 → 复用；同名但来自
  另一个目录（夹具、插件目录并存时常见）→ 按本目录文件重新加载；`reload=True` →
  强制换新的热插拔口子。无论复用与否都照旧跑 AST 安全扫描——缓存里那一份可能是普通
  `import` 带进来的，从没走过这道检查。判据：
  `tests/test_agent_loader.py::TestModuleIdentity` 七条（含"注册前打的补丁必须生效"、
  "同名不同目录不得复用"、"reload 真的换对象"、"复用时仍扫描"），
  把复用条件写死成 `False` 会有三条转红。`docs/agents.md` 新增"模块身份"一节。

### Fixed（2026-10-06·续2）

- **perf 门禁的豁免名单按 CI 实测收窄**：e90ebd6 那一版把 serial / thread_pool /
  process_pool 三项一起划成"只观测"，理由写得对不对要拿数据说话——CI run 37357935560
  当轮 3 次连续采样的波动是 process_pool 91.9%、process_speedup 51.2%、elapsed_ms
  38.1%、selectolax 27.1%，而 serial / thread_pool 连前八都没进（本机跨次也只有
  5.3% / 2.6%）。所以豁免只留进程池那两项，serial / thread_pool 回到判定，
  改由 15 毫秒的绝对下限挡住当初那次误报（差 10 毫秒被判 38.2%）。
  新增 `test_thread_and_serial_are_still_gated`：慢一倍必须还判得红，
  豁免不是"并行全家不测"。

### Added（2026-10-06·续3，未发版）

- **通用 Agent 套件（#22）**：`agents/http_request_agent.py` + `agents/transform_agent.py`，
  两个都不带文档语义——引擎"换一种任务类型仍然成立"从此有可运行的例子，而不是一句宣言。
  - `http_request`：出网走 `url_guard.validate_public_http_url`，**没有关闭校验的配置项**；
    要指内网只能显式列 `allow_hosts`，比对 hostname 全等（`db.internal` 不顺带放行
    `evil-db.internal.attacker.com`），命中时产物带 `guard: "allowlist"`。`max_bytes` 超限
    **中止并报错，不截断**（残缺 JSON 会让下游拿到看似合法的坏数据）；3xx 恒不跟随且抢在解析前
    返回 `redirect_to`（重定向体常为空，让"不是合法 JSON"抢先判死就看不见 Location）；
    4xx/5xx 判节点失败而不是把错误页当产物往下传；`expect: json` 解析失败如实报错；
    响应头进产物前脱敏（`authorization`/`token`/`secret`/`cookie`/`api-key` 恒为 `***`）。
  - `transform`：声明式 `items / fields / where / set / template`，求值语言与上下文和 `when`
    同源（`conditions.resolve`），作者不必学第二套取数规则；`len()` 是唯一放行的函数——
    在配置里塞表达式求值器等于把数据通道变成代码通道。缺字段、路径取不到、模板变量取不到
    一律判失败，不渲染成空串出厂。产物 `data` / `text` / `content`（后两个同值，方便直接接
    按 `content` 取正文的 `safe_writer` 与质检节点）。
  - `conditions.resolve` 支持列表下标（`items.0.id`），`when` 与模板同时受益。
- **出厂消费者 `pipelines/api-report.yaml`**：`http_request → transform → safe_writer`，
  三个节点零领域词。它存在的意义是守住接线——能力没进任何流水线就等于零，
  这条已经为 `ingest`/`knowledge_base` 与 `when` 各补过一次出厂消费者。
  判据 `tests/test_generic_agents.py` 41 条：离线跑通真件（requests 换成罐头响应，Scheduler →
  DAGExecutor → 真 transform → 真 safe_writer 落盘），断言的是**交付物本身**——文件存在、
  正文含渲染结果、模板里没有残留的 `{{`；另一条把接口打挂，要求流水线如实 `failed` 并带出
  错误，而不是拿空产物报 done。本机另用真网络实跑过一次（`run.py test_input.md --pipeline
  api-report`，1.4s，三节点全 ✅，落盘产物首行 `# apache/kafka`、正文含真实
  `Stars: 33912` 与 `接口返回码: 200`），离线罐头响应与真实出网两条路都走通。
- 拓扑与锁文件差异（本笔，全新流水线一条）：

  | 流水线 | node_count | topology_hash | 说明 |
  |---|---|---|---|
  | api-report（新） | 3 | 新 `fee618522b1f` | `http_request` / `transform` / `safe_writer` 三层各一节点，无 `call` 内联 |

### Fixed（2026-10-06·续3）

- **perf 门禁加整体环境因子，并修掉入口处的类型混用**（#19 收口）。
  push 出去的 `f235ae3` 在 CI run 37360857091 上被 perf-regression 判红：
  `regex 0.01204 → 0.01642（+36.4%）`，同时 `selectolax +32.2%`、`serial +30.9%` 各自
  因为绝对下限记为 UNVERIFIED。那笔提交只动 `agent_loader` 的模块复用，与这三段互不相干
  的耗时没有因果；三个数同向就是"那台 runner 那天慢"。现在先估**整体环境因子**
  （非豁免指标"变差倍数"的中位数，要求参与≥3 项且多数与中位数同向，否则不校正），
  用 `(1+原始)/env − 1` 校正每项再判三关；被扣除的幅度在消息里明写"原始 X%，已按 env 校正"。
  回放那次 run 的原始数字现在 0 项确认、0 项待证（`test_the_real_ci_red_of_run_37360857091_is_no_longer_red`）。
  - 代价写清楚：**所有指标一起变慢的真实回归会被当成机器慢放过**。换它是因为另一半更糟——
    共享 runner 上整体漂移是常态，而把它判成三次回归等于让大家习惯忽略红叉。
    窄域回归照判（`test_outlier_still_fails_when_the_rest_is_stable`）。
  - **入口 bug**：`main()` 里 `env` 一名两义——先是 `_env_fingerprint()` 的 dict，
    再被赋成 `_effective_env()` 的 float，随后指纹比较那两行对它 `.get()` →
    `AttributeError: 'float' object has no attribute 'get'`，整条 perf job 会当场崩；
    同一处还把 `_env` 写成那个 float，下一轮比较彻底失效。拆成 `fingerprint` / `env`，
    并补 `TestCiEntryPoint` 三条真正走 `main()` 的用例（此前**没有任何测试跑过入口**，
    所以判据测得再细也没挡住）。把 `_env` 改回写 float 会有两条转红。
  - CI 缓存拆成基线与历史**成对** restore/save（`Save perf baseline` 只在成功时写，
    `Save perf history` 恒写），否则历史永远攒不到样本、跨 run 波动带恒缺席。
- **`StructuredLogger` 的后台写线程一次失败即永久静默**（由通用件 E2E 撞出，表现为
  `PytestUnhandledThreadExceptionWarning`）。`log_dir` 是相对路径，每次 flush 按当前 cwd
  解析；进程 chdir 或临时目录被清理后 `open(..., "a")` 抛 FileNotFoundError，而它跑在
  daemon 线程里——线程一死，队列只进不出，之后每一条日志都"入队成功"却永不落盘，
  排查时看到的是"没有异常"。现在 flush 全程包住，目录不在就重建，失败批次计数打到
  stderr 后继续跑。判据两条（`TestWriterThreadSurvival`）分别对应两半：去掉重建 → 第一条红；
  让异常逃出 → 第二条红（修复过程中就实测红过一次，那时 `_get_file()` 还留在 try 外）。
- **通用件第一版只读 `self.config`**：YAML 里的节点 `config` 实际由执行器放进
  `payload["config"]`，于是单元测试全绿、出厂 `api-report` 一跑就报"未给出 url"。
  改为 `{**self.config, **payload["config"]}` 合流，并把这条约定写进 `docs/agents.md` §8。

- **legacy 自动图新增准入位 `LEGACY_AUTO`**：`orch.run()` 那条被冻结的兜底路径按设计
  不读 YAML，它的图 = `registry.deps_order()` = **全体注册件**（`registry.py:219`），
  于是"没有节点级配置就跑不出东西"的件被拉进去必然业务失败——`http_request` 落地后
  `tests/test_resume_recovery.py::TestResumeEndToEnd` 就报了 `未给出 url`（实测双向：
  移走两个新件文件该用例过，移回来 3/3 稳定红；清掉 `checkpoints/` 残留也红，排除脏状态）。
  修法不是把这类件的失败洗成"跳过即成功"，而是让件自己声明不参与 legacy 自动图：
  `LEGACY_AUTO = False`（默认 True，旧件语义不变），`_legacy_agent_order()` 同时供
  `plan()` 预览与 `_run_dag_parallel()` 实跑使用（两处必须同一套准入，否则 `--plan` 里
  看得见、实跑没有）。件照旧注册、照旧被声明式流水线调用。判据三条 + 把过滤器退回
  裸 `deps_order()` 会同时让这条新判据与那条 resume E2E 转红。
  - **顺带记下一处真缺口**（本笔不修，另立待办）：`--resume` 只接在 legacy 分支上
    （`run.py:424`），声明式分支的 `run_plan()` 根本没有 resume 参数；
    `DAGExecutor._merge_resumed_nodes` 依赖 `task._resumed_node_snapshots`
    （`checkpoint_manager.load` 才会设），而只有 `orch.run(resume=True)` 调 `_load_checkpoint`。
    即默认路径下 `--resume` 是空转的，README 的 CLI 表把它写成"从断点续传"因此领先于实现。

### Docs（2026-10-06·续3）

- README：新增"通用 Agent（`http_request` / `transform`）"一节（两张配置表、重定向与脱敏
  语义、`transform` 示例）；目录结构补 `api-report.yaml`，Agent 计数 12 → 14。
- `docs/agents.md`：§8 节点级配置从 `payload["config"]` 来（含 `PRODUCES` / `WRITES_OUTPUT`
  的交付契约提醒）；§9 通用能力必须有出厂消费者。
- CONTRIBUTING：性能门禁一节补"环境校正"判据行与它的代价说明；`REPORT_ONLY_METRICS`
  那段此前仍写着 serial / thread_pool / process_pool 三项——`bead99d` 已按实测收窄到
  进程池两项，此处按代码改正（`benchmark.REPORT_ONLY_METRICS` 实测为
  `['process_pool', 'process_speedup']`）。

### Fixed（2026-10-06·续4）

- **`transform` 里 `where` / `fields` 不给 `items` 时被静默忽略**：两者只在逐项循环里生效，
  没给列表就等于"写了没人读"——作者以为过滤跑了，产物却全量出厂。现在直接判失败
  （`需要 items`），与引擎对 `call.inputs` 的规矩同源。判据一条两个方向：分别注掉
  `where` 或 `fields` 的守卫，用例都会转红（不是只验了第一半）。

- **渲染层拒绝不了抓取正文里的控制字符**（#21，docgen-render 出厂流水线因此 ❌）。
  `renderer` 把正文交给 python-docx，lxml 在写 `<w:t>` 时抛
  `ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or
  control characters`，整条流水线到此为止。触发样本是抓取来的一页正文里混着的**一个
  `\x08`**（`运行实例 » \x08相关文章`）。pdf 那条走 reportlab 同理。
  现在 `xml_safe()` 在进后端前剔掉 XML 非法控制字符（保留 `\t\n\r`），并把
  **剔了几个**如实放进结果（`control_chars_stripped`），`renderer_agent` 收到非零就
  `log_warning`——这些字符不可见，但不等于"正文被动过"可以静默。
  判据五条（`TestControlCharsAreStripped`）：计数与保留 `\t\n\r`、docx 渲染成功且
  round-trip 后正文完好、pdf 同样成功、干净输入回报 0（不能虚报动过）、脏标题
  （`\x0b`）被清洗后 `core_properties.title` 仍是"季度报告"。
  标题是另一条入口：docx 那边 `core_properties` 写入被 `contextlib.suppress` 包着，
  脏标题**不报错而是静默丢掉文档属性**，所以 `xml_safe` 也作用于 `title` 并计入同一个数。
  把 `xml_safe` 调用换成 `stripped = 0` 会让 docx 与 pdf 两条直接复现上面那句 ValueError；
  单独注掉标题那两行会让脏标题用例转红。
  实跑确认：`python run.py test_input.md --pipeline docgen-render` 现在 10 个节点全 ✅
  （renderer 142.5ms，此前是 ❌），落盘 `output/render_probe2.docx` 47 KB，
  重新读回 docx 后正文里非法控制字符 0 个、Title/Heading 样式照旧在（目录可跳转没退化）。

### Fixed（2026-10-06·续5）

- **未知检索引擎不再回落 mock，docgen smoke 从"间歇抽签"变成确定性**（#25，
  CI run 37373808297 上 docgen smoke 判红的根因）。`docgen.yaml` 配的引擎是
  bocha/tavily/serper，而 `researcher` 里没有这三者的实现分支，原来的兜底
  `else: _mock_search(...)` 把它们静默换成假摘要——实测出过一次 351 字节的
  bocha 占位稿被当成功出厂（同一 commit 重跑又全绿，近 6 次里 1 红 5 绿），
  smoke 门禁（≥800 字节且非占位）因此一直在抽签。
  现在 **mock 必须是显式选择**：未知引擎只告警跳过（`results = []`）⇒ 无可用
  引擎的环境里流水线必然被产出保真底线拦下（`HARD_FLOOR`，rc=1，不落盘），
  ci.yml 的 `rc≠0 + HARD_FLOOR` 分支成为 keyless 路径的固定走向（已加注释）。
  dead-proxy 环境实跑复现：`错误: HARD_FLOOR: 产出未过保真底线: 内容过短（52 < 120
  字符）；产出为占位内容（命中"未采集到可整合的搜索结果"）`，rc=1 且无产物文件。
  判据 `TestUnknownEngineDoesNotFallBackToMock` 三条；另改写两条把 bug 写进断言的
  旧用例（"未知引擎→mock"的期望值、以及从未命中的异常注入）。
- **`--resume` 在声明式路径是空转**（#24，README 的 CLI 表因此领先于实现）。
  `run.py` 默认走 `run_plan()`，而它根本没有 resume 参数——断点状态要
  `checkpoint_manager.load()` 设到 `task._resumed_node_snapshots` 才生效，
  只有 legacy 的 `orch.run(resume=True)` 会调 `_load_checkpoint`。现在：
  - `run_plan(..., resume=False)` / `run_plan_async(..., resume=False)` 接断点，
    run.py 声明式分支透传 `--resume`；`_resume_snapshots_for` 先做**归属校验**：
    断点流水线名与当前流水线不符就拒收并告警——否则节点名对不上时静默全量重跑、
    对得上时用错结果，两种都不是"续传"。
  - `DAGExecutor._merge_resumed_nodes` 改成**按层增量**合并：`_execute_plan` 每层
    现场新建 TaskNode，原先"每任务一次"的合并只能覆盖第一层，后面几层的已完成
    状态永远合不进来，resume 退化成"除第一层全部重跑"；已合并过的节点记录在案，
    节点重跑出新结果后不会被旧快照倒回去。
  - 顺带对齐一处 async 差异：`run_plan_async` 此前无条件盖 DONE，声明了落盘节点
    却没有交付物也报 done（SSE 路径），改为与同步版同一交付契约。
  - 判据四条（`TestDeclarativeResumeEndToEnd`）：声明式 E2E（4 个已完成节点复用、
    下游 3 节点真跑、交付物落盘且含恢复内容）、async E2E、归属拒收、async 交付
    契约。两处变异实测转红：摘掉快照挂载 ⇒ 4 个已完成节点全部重发 bus.request；
    注掉交付契约 ⇒ 无产物仍报 DONE。
- **Docs**：README 的 `--resume` 行按实现改写（两条路径都生效，流水线归属不符会被拒收）。

### Added（2026-10-05，未发版）

- **Phase 1 解耦——产物契约**（`pipeline_core/artifacts.py` + `config_schema.py`）：
  节点之间传什么由 Agent 自己声明，引擎不再持有领域词表。
  - Agent 声明 `PRODUCES`（`{"content": "last"}`）/ `CONSUMES`，Scheduler 与
    DAGExecutor 按声明组装载荷；"谁的内容更新"由 DAG 层级决定，
    取代写死的 `content_priority = ["layout","quality_gate","writer","fact_checker"]`
    与 `articles`/`results`/`spec` 字面量拆解。
  - 配置契约从 `scheduler.AGENT_SCHEMAS`（9 个内置 Agent 的集中表）下沉为各
    Agent 模块的 `CONFIG_SCHEMA`，Scheduler 用 **AST** 读取（不执行 Agent 代码）；
    默认值注入改为深拷贝，避免同池实例共享同一个 list 对象。
  - 沙箱信任由 Agent 自证（模块顶层 `SANDBOX_TRUSTED = True`，加载器 AST 核实），
    删除 core 里 13 个名字的 `_TRUSTED_AGENTS` 名单（含 `fast_pool_0` 测试遗留项）。
  - 重做循环不再猜目标：声明 `SUPPORTS_REGENERATION` 必须给 `REGENERATION_TARGET`；
    反馈载荷不再拆 `quality_scores`/`citation_report` 等 gate 专属键（实测无 Agent 读），
    整体转发为 `gate_feedback`。
  - **P1-4 接口层去领域化**：Admin API / MCP / OpenAPI 此前各自把 `"docgen"` 写成
    默认流水线名，并各写一份"从 result 里翻 safe_writer/layout/checker 找产出"的
    猜测。现在：落盘节点声明 `WRITES_OUTPUT`，引擎把交付物挂到
    `task.output_path/output_content`，接口层共用 `artifacts.task_output`；
    默认值改由 `config.default_pipeline` 提供（纯函数
    `scheduler.resolve_pipeline_name` 解析，歧义时报错列出可用清单而不猜）。
    OpenAPI 的 `pipeline` 字段改为 enum 列举真实流水线。
- 护栏：`tests/test_artifact_contract.py` 的 `TestCoreStaysDomainNeutral` 用 AST
    扫 11 个 core 模块的代码级字符串常量，领域 Agent 名一旦回流 core 即失败。

- **kb-docgen 流水线**：把此前未被任何流水线引用的摄入层 / 向量知识库接进编排，
  实现"本地资料 → 建库检索 → 知识库接地写作"。离线（无 LLM Key）也能交付
  抽取式草稿，`tests/test_kb_pipeline_wiring.py` 覆盖 21 例（含整条 DAG 离线跑通）。
- **产出保真底线**（`agents/quality_gate.py`）：内容长度与已知占位语两条判据，
  命中即 `hard_floor` 失败，不重做、不因 `fail_fast=false` 放行，流水线 exit 1。
- **`pipeline_core/selectolax_compat.py`**：HTML 解析内核兼容层（modest/lexbor），
  按真实可导入性选内核，降级计入 `_parser_fallbacks` 并首次告警。
- **`pipeline_core/state_paths.py`**：运行态路径可重定向
  （`DOC_PIPELINE_STATE_DIR` / `DOC_PIPELINE_VERSIONS_DIR`）。
- **edges 一致性校验**（`scheduler.py`）：`topology.edges` 与 agent.dependencies
  双向比对；七条流水线全部补齐 lockfile（此前只有 2 条受漂移护栏保护）。
- **分层拆包：文档领域层独立成 `docpipeline/`**（Phase 1 收口）。
  `renderer.py` / `ingest.py` / `document_enhancer.py` 从 `pipeline_core/` 移出，
  `pipeline_core` 不再导出 `DocumentEnhancer`。这三个模块此前不依赖任何领域无关
  引擎能力（`ingest`/`renderer` 根本不 import `pipeline_core`，只在函数内惰性加载
  可选后端 docx/reportlab/pymupdf/paddleocr/mineru；`document_enhancer` 只用到
  `llm_router` + `search_engines`），是 engine 里仅剩的文档域代码。
  依赖方向钉成单向 `agents → docpipeline → pipeline_core`，由
  `tests/test_layering.py` 用 AST 扫描当门禁（含函数体内的惰性 import 与
  `importlib.import_module("…")` 字面量）。四条判据都做过注入式正例验证：
  往 core 塞一句 `from docpipeline import renderer`、往 ingest 塞 `import numpy`、
  把 `python-docx` 从 requirements 注释掉、往 renderer 加 `import scripts…`——
  各自转红，确认"0 违规"是结论而不是空转。
  docpipeline 的非标准库依赖改为显式登记制（`pipeline_core` / `scripts` /
  已声明的 docx·reportlab·pymupdf / 有意不进 requirements 的 paddleocr·mineru），
  新增外部依赖必须先在登记表认领；其中 `docpipeline → scripts`
  是钉住的历史耦合，只允许 `document_enhancer` 一处。
  `docpipeline/__init__.py` 不做顶层 re-export，避免同一函数出现两个打桩入口。
  CI 的 py_compile/mypy/bandit 口径与 pyproject `packages`/coverage `include`
  同步纳入 `docpipeline/`，否则搬出去的代码会静默脱离门禁。

- **阶段 2-2：常驻 worker 补上队列的消费端**（`pipeline_core/worker.py` + `--worker`）。
  此前 `TaskQueue.acquire()` 没有任何调用方（全仓 grep 只命中定义与 docstring 示例），
  `POST /api/tasks` 入队之后必须有人再手动跑一次 `run.py --recover` 才会真的执行——
  队列只进不出，"提交即排队"在跨进程部署里并不成立。
  - `TaskWorker`：`recover(stale_seconds)` 回收崩溃租约（要求 owner_pid 已死且 started_at
    过期，活进程正在跑的任务不动）→ `acquire(worker_id)` 原子 claim → 按名解析 YAML
    （锁文件漂移直接拒绝执行并写 failed，不退回 legacy）→ `run_plan` → 落终态。
    `run_forever` 支持 stop_event 与 `--idle-timeout`，SIGINT 处理完当前任务才退出。
  - `TaskQueue.finish()`：**终态只写一次**。`update_status` 是无条件覆写，而 worker 化之后
    API 取消（行已 cancelled）与流水线收尾会并发，取消会被洗成 done；两条收尾路径
    （`_finalize_plan_task` / `_finalize_task_queue`）都改用它。
  - CLI：`--worker` / `--once` / `--poll-interval` / `--idle-timeout` / `--lease-stale`。
  - 测试 `tests/test_worker.py` 15 例，两个关键判据都做过反向验证：
    去掉 acquire 的 `AND status='pending'` → 互斥用例转红；
    把 `finish` 改回无条件覆写 → 6 例转红。第一版互斥用例用同一个 `TaskQueue`
    实例开两个线程，被实例内的 `threading.Lock` 先串行化了，SQL 守卫根本没被行使，
    反向验证时**没有转红**——那是假绿，已改为两个独立句柄（等价于两个进程）。
  - 本机实测跑通真实队列：worker claim 到两条历史 kb-docgen 任务，因 input 文件已不存在
    而如实写 failed（错误串同时带"文件不存在"和交付契约原因），不是静默跳过。
  - bandit 拦下我自己写的 B608：`finish` 里按 `allow_states` 长度拼 `IN (?,?,…)` 被判
    "string-based query construction"（MEDIUM，`-ll` 门禁会红）。改成先读当前状态、
    再用常量 SQL 把观测值放进 WHERE——观测值参与条件同样挡住"读到 cancelled 之后
    状态又变"的竞态（状态一变 rowcount 归 0）。改后 `-ll` 零命中。
    另注：本机 `bandit` 控制台脚本是坏壳（`--version` 无输出、退出 1），要用
    `python -m bandit`；CI 上脚本正常。

- **阶段 2-3 第一步（地基）：节点身份的解析收敛成一处**（`pipeline_core/naming.py`）。
  "从节点名还原执行它的 Agent"此前以 `split("_pool_")[0]` 的形式重复在 12 处
  （dag_executor 8 / pipeline 2 / scheduler 2），语义还略有差别。要做子流水线内联
  （同一 Agent 在一棵图里出现多次，`writer__review`）与 foreach，必须先有唯一解析处，
  否则加一种命名形态就要改 12 个文件。约定 `agent[_pool_i][__alias]` +
  `agent_of / pool_index_of / alias_of / node_id`。
  行为零变化（现名里没有 `__`，`agent_of` 对它们恒等）：全量 2013 passed 不变；
  新增 `tests/test_naming.py` 23 例，其中一例遍历 `agents/` 的 `AGENT_NAME`，
  保证没有真实 Agent 名会被这层解析削掉一段。

- **阶段 2-3：子流水线 `call`（解析期内联展开）**。一条流水线此前只能是一张扁平
  DAG，没有"把另一段流程当节点"的能力。
  - 做法是**计划层展开**而非新增执行语义：`_expand_calls` 把被引流水线的节点带别名
    内联进调用方图（`checker` → `checker__review`），call 节点的前置接到子图入口、
    依赖它的节点改接子图出口，然后按依赖重算层级。于是幂等键、检查点、重试、熔断、
    `when`、产物契约全部照旧，不需要在引擎里再维护一套子流程语义。
  - 同一 Agent 因此可以在一张图里出现多次而互不覆盖（父子都写 checker →
    `checker` 与 `checker__review`）。
  - 解析期拒绝：环引用（按**被引流水线名**判，两条节点复用同一子流程是合法复用）、
    嵌套超过 3 层、被引流水线不存在（报可用清单）、call 节点自带
    config/when/pool_size/rate_limit（配置属于子流水线，两处真相不可读）。
  - 拓扑指纹按**展开后**的图计算：只改子流水线一个字节，调用方 lockfile 就报漂移
    （含"节点数不变、只换 Agent"这种靠 node_count 抓不住的情形）。
  - **揪出我自己刚引入的回归**：命名收敛那一步把闭包分组、池归并、结果读回退
    一并改成了 `agent_of`，别名被剥掉 → `layout__review` 掉出上游闭包、
    两段内联池结果互相串台。补 `family_of()`（保留别名、只去池下标）并把三类用法
    分开；`TestRuntimeIdentity`（链式 call 拿到的是上一跳出口内容）与
    `TestPoolMergeIsAliasAware` 各做过变异回退验证会转红。
  - 测试：`tests/test_subpipeline.py` 19 例（展开/边界接线/层级重算/复用不撞名/
    池化下标保留/环·深度·配置拒绝/锁漂移/运行期身份隔离）+
    `tests/test_naming.py` 增至 31 例。README「子流水线」与 architecture §1.4 记录约定。

- **阶段 2-1：条件节点 `when`——拓扑第一次能表达"看结果决定"**。
  此前 `topology.levels` 是手写静态层级，全仓 `condition/branch/loop/foreach/sub_pipeline`
  零命中，DAG 一旦确定就照跑。新增 `pipeline_core/conditions.py`（受限声明式，
  纯函数、不 exec/eval）+ Scheduler/`ExecutionNode`/`AgentConfig` 接线 +
  `TaskNode.skip_reason`。
  - 语义：条件不成立 → 节点标 `skipped` + `skip_reason="condition"`、不投递消息，
    且**下游照常执行**（可选分支不该让整个下游停摆）；因依赖失败被级联跳过的依赖
    仍然让下游跳过，两条路径靠 `skip_reason` 区分，各有用例钉住。
  - 求值上下文：`artifacts.*`（上游按 PRODUCES 合并的产物）/ `upstream.<node>.*`
    （原始结果）/ `config.*` / `pipeline` / `task.*`。
  - 三条硬规矩：算子白名单与写法在**解析期**校验（`ValueError: [agent] when 条件非法`）；
    `bool` 不与数字比较（`True == 1` 在 Python 里会静默成立）；
    **路径取不到直接抛 `ConditionError`，整条 run 失败**——当成"条件不成立"就是
    静默跳过分支却照报 done，正是本项目一路在关的那类洞。
  - `when` 进 `topology_hash`：加条件会让既有 lockfile 报拓扑漂移；不写 `when`
    的节点不贡献条目，内置流水线指纹实测未变、锁文件全部校验通过
    （`test_shipped_pipelines_keep_their_hashes` 用 glob 逐条 `verify_lock=True`，
    新增流水线不会被这条退出门漏掉）。
  - **出厂消费者 `pipelines/docgen-lean.yaml`**：语言实现完就用它接了一条真实流水线
    ——fact_checker 挂 `when: upstream.quality_gate.overall_score >= 70`，
    质量分不达标的草稿不再付核查成本（迟早要重做），达标才升级核查。
    需要无条件核查仍用 `docgen-verified`，这条的核查是可选的，YAML 头部与 README
    场景表都写明了这个区别。
    接这条流水线当场抓出我自己的设计缺口：`_condition_context` 原先只暴露**直接依赖**，
    而 `fact_checker` 的直接依赖是 `checker`、`quality_gate` 是祖先节点，于是 shipped
    配置一跑就抛"路径取不到"。已改为纳入整个上游闭包（`_upstream_closure`）——
    作者按直觉写的条件不该因为依赖图的跳数而失效。这条测试是
    `TestShippedConsumer` 三例：低分跳过且 layout 照跑、高分执行、指纹未变。
  - 交付契约与之衔接：落盘节点全部被条件跳过 → 视为"按声明本轮无交付"，run 仍 done
    并留 warning；因依赖失败而没跑 → 仍然 failed（#13 关掉的洞不借此复活）。
  - 测试：`tests/test_conditions.py` 56 例（语言本身，含"每个算子都可用"的对照表）+
    `tests/test_condition_nodes.py` 16 例（接线行为）。三条判据各做过反向变异并确认转红：
    移除"条件跳过不阻塞下游"的例外 → 1 例红；把求值错误降级成"条件不成立" → 1 例红；
    `when` 不进指纹 → 2 例红。还原后全绿。

### Fixed（2026-10-05）

- **SSE 流式回调从来没挂上过**：`admin_api._find_streaming_agent` 遍历
  `registry._agents.values()`——那存的是 `AgentMeta.to_dict()` 的 dict，
  `hasattr(dict, "handle_streaming")` 恒为 False，于是方法恒返回 None，
  API 提交的任务静默失去增量内容。改为按能力在实例上查找。
- **`embedder: auto` 冷导入 sentence_transformers 78 秒**（拉起 torch）：
  auto 只需知道"有没有这个候选"，改用 `find_spec`；真要用（模型已缓存）
  才付加载成本。kb-docgen CLI 实测 78s → 3.9s。
- **默认流水线跑错定义文件**：`--pipeline docgen` 因 `glob("docgen*.yaml")` 前缀
  匹配 + 首个成功即返回，实际一直在跑 `docgen-render.yaml`；改为精确同名优先、
  多义显式报错（`run.py`）。
- **正文提取静默降级**：selectolax 1.0 起 `selectolax.parser` 在导入期主动报错，
  生产安装（CI 实测 selectolax-1.0.0）恒久退化为正则启发式，而 `--check` 只探测
  顶层包仍报 OK；现在走兼容层并让 `--check`/CI 冒烟如实报告所用内核。
- **四类"静默绿"**：幂等键命中历史返回 None 被记成 success；重试结果的业务失败
  未置 `retry_ok=False`；重做循环末尾无条件把 `status=error` 洗成 `pass`；
  checker 把"检出 P1"表达成 `status=fail` 从而跳过下游。
- **KnowledgeBaseAgent 未订阅 `knowledge_base.input`**：DAG 用
  `f"{agent}.input"` 做 RPC topic，缺订阅时请求静默空转；新增总线寻址护栏。
- **节点 config 不生效**：知识库的 `embedder/db_path/top_k` 与 checker 的
  `block_on_p1` 现在在 handle 期应用（Agent 经 registry 加载时构造期 config 为空）。
- **`embedder: auto` 触发模型下载**：huggingface.co 不可达时按 1/2/4/8/16s 退避
  重试 5 次，CLI 跑 kb-docgen 表现为挂死（实测 >240s 未结束）。
  第一版只设 `HF_HUB_OFFLINE` 不够——sentence-transformers 仍会去查 Hub 的
  revision；现在 auto 先用 `model_is_cached()` 判本地缓存，没缓存**根本不构造**，
  有缓存才套离线环境。修完 kb-docgen CLI 实测 28.8s 跑完并产出接地文档。
- **ingest 初始化实例化 OCR 后端**：为一条日志付 84 秒（本机实测），改为按需探测。
- **运行态污染**：测试与真实运行共用 `<checkout>/bus_data`（幂等记录积 1116 条）
  与 `versions/`（260+ 死条目，使 `/api/versions/stats` 慢到 4.9s）；测试状态
  隔离到 `.test_state/`。
- **E2E Nightly 假绿**：未配 Secret 时 6 skipped / 1674 deselected 仍报 success；
  现在无 Secret 或实际执行 0 用例一律失败。
- **docgen 默认流水线拿不到任何检索结果**（推送后 CI 才暴露，本机同样复现）：
  接线 ingest 进流水线时给它多挂了一个 `researcher.input` 订阅，于是发给
  researcher 的定向 RPC 被 ingest 接走，回了 `{"status":"error","message":"未指定待摄入文件"}`。
  两层判据同时失守把它洗成了"成功"：`_business_failure` 只认 `blocked/fail` 与
  `"error"` 键（这份回执两者都不满足），契约层则原样把空产物铺给下游——
  researcher 记为 success、fetcher 收到空 results、writer 产出 52 字符占位文。
  修复：ingest 只订阅自己的主题；`status: "error"` 归入业务失败状态
  （`_BUSINESS_FAILURE_STATUSES`，与 `_record_task_output` 用同一套语义）；
  业务失败异常带上节点名（原来只有裸 message，排查时认不出是谁报的）。
  新增两条护栏——`TestBusAddressing` 禁止任何 Agent 订阅别人的 `<agent>.input`，
  池化上游声明的产物必须出现在下游 payload 顶层（此前只测了合并这一步）。
  修复后同一条命令实测：`状态: done`，产出 26473 字节（修复前 52 字节 exit 0）。
- **error 判为失败之后揪出的两处真缺陷**：
  - `knowledge_base` 的 `handle` 推断出 `action=search` 后一律走单条 `query` 的
    `_do_search`，而 DAG 节点载荷给的是 `queries` 列表——带查询词的检索节点
    其实一直在回"未指定查询词"。改为有 `queries` 就走 `_do_search_multi`，
    并补 `test_inferred_search_uses_queries_not_only_single_query`。
  - 引入 `status: "skipped"` 表达"无事可做"：ingest 没有语料、kb 没有查询词，
    既不该判失败（legacy `run()` 会给每个已注册 Agent 都发一次 RPC），
    更不该记成交付——`_apply_node_success` 现在把这类节点步骤写成 `skipped`。
    显式 `kb.search` 请求缺 `query` 仍然算 error。

- **`fail_fast: false` 把"没交付"洗成 done**：`_execute_plan` 收尾只要状态不是
  FAILED/CANCELLED 就无条件盖 DONE，于是末端节点保持 RUNNING（软失败不中断）也能
  "完成"。实测 kb-docgen：ingest 如实 skipped、knowledge_base 失败、writer 之后从未执行，
  结果报 `状态: done` + exit 0，而 `--output` 指定的文件根本不存在。
  现在加了交付契约：计划里声明过 `WRITES_OUTPUT` 的节点存在时，必须真拿出交付物
  （`output_path` 指向的文件存在，或有非空内联内容）才算 done，否则 FAILED 并把原因
  并进 `task.error`。实测：kb-docgen 空语料改为 `状态: failed` + exit 1（错误里同时
  给出"节点失败原因；声明了落盘节点却没有交付物"），docgen 正常交付不受影响
  （done / exit 0 / 20471 字节）。
- **mock E2E 的补丁是空转的**（`tests/test_e2e_mock.py`）：`agent_loader` 以
  `spec_from_file_location` 重新加载 `agents/*.py` 并覆写 `sys.modules["agents.<name>"]`，
  因此在 `register_agents()` 之前进入的 `patch("agents.writer.WriterAgent.handle", ...)`
  绑到旧模块对象，对真正实例化的类无效。探针实测：`实例的 handle is mock: False`。
  结果这个"Mock 端到端测试"跑的是真 writer/真 quality_gate，只靠
  `assert "writer" in task.result` 这种弱断言通过。现改为注册之后再对
  `sys.modules["agents.writer"].WriterAgent` 打补丁，并**断言 mock 的哨兵字符串
  真的出现在产出里**——补丁失效时测试自己会红。
  loader 覆写 `sys.modules` 造成同名模块两份对象（模块级锁/缓存各一套）这个设计问题
  仍未处理，已另立待办。

### Docs（2026-10-05）

- README 数据校准：测试数（本机 1854 passed / 2 skipped，CI 1772 passed）、
  Agent 与模块计数、场景表补 kb-docgen、CLI 参数补 `--write-lock` / `--check`、
  示例 YAML 的 `safewriter` 拼错修正，并新增保真底线一节说明其与评分维度的分工。

## [3.9.1] - 2026-08-30

### Fixed

- **发版物 pyproject 依赖不完整**：`[project] dependencies` 此前只列 3 个包
  (PyYAML/requests/selectolax)，但源码 import 还硬依赖 `aiohttp`、
  `orjson`、`duckduckgo-search`——新用户 `pip install doc-pipeline`
  会立即 ImportError。补齐 6 个硬依赖（与 requirements.txt 对齐），
  wheel 重新构建后全栈 import 走通。

## [3.9.0] - 2026-08-30

### Test（覆盖率大补 + 测试可靠性）

- 覆盖率 65.59% → 85.97%（门禁 fail_under 63 → 83）：新增 9 个测试文件 +448 用例，
  补齐 writer/researcher/search_engines/admin_api/fetcher/layout/checker/
  event_hook/cache_manager/run 十个薄弱模块（writer 32→96%、checker 38→100% 等）
- 修复 fetcher 测试依赖真实外网 DNS 的 CI 脆弱点（11 用例改确定性 DNS 映射）；
  修复 duckduckgo"未安装分支"依赖环境假设；修复 2 个 Windows 路径样本在
  Linux 的假通过/假失败；test_run_ext 消除 sys.modules 模块身份陷阱
- 全量 1426 passed / 2 skipped，新增版本一致性护栏（pyproject ==
  pipeline_core.__version__，防发版漂移）

### Performance（实测闭环验证）

- 冒烟实测端到端 researcher **122.6s → 35.6s（-71%）**：
  - duckduckgo 不可达网络（DNS 污染）降序至 HTML 引擎后 + 60s 快速失败窗口
    （原每次查询固定烧 30s 双栈超时）
  - search_with_sites 双护栏：常规满额跳过站点批次；站点搜索只用首引擎
    （9 站点 × 多引擎串行超时 → 单引擎）
- url_guard DNS 解析 TTL 缓存 + 负缓存（20 页下载最坏 300 次同步 DNS → ≤20 次）
  + 字面 IP/DNS 判定 lru_cache
- SSE 推送事件驱动唤醒替代 5Hz 轮询（推送延迟 200ms → 亚毫秒，空闲唤醒 -80%）
- /api/logs 流式读取 + mtime 早退（内存 O(文件) → O(1)）
- fetcher/researcher 热路径正则模块级预编译（正文提取 1.54x，等价性逐字节验证）
- cache_manager file 后端 size() 版本+TTL 缓存；run.py 流水线名进程内缓存

### Fixed

- **SQLite 跨线程 close 段错误（CI Linux 稳定复现，Windows 不可见）**：
  message_store/task_queue 的 close_all 跨线程关闭连接与投递线程执行中
  语句在 C 层竞争触发 SIGSEGV；改为失效标记 + 拥有线程自愈重建
- researcher._normalize_results 运算符优先级 bug（dict 输入恒返回空列表）
- 版本漂移：pipeline_core.__version__ 3.7.0 与 pyproject 3.8.0 不一致
  （banner/API/MCP 对外全报错版本）
- run.py 无 Agent 加载时不再漏 shutdown；事件钩子 webhook 逐跳 SSRF 复检

### CI / 基建

- 测试工具链钉版（pytest==8.3.4/pytest-cov==7.1.0/coverage==7.15.2/
  pytest-asyncio==0.25.2）——浮动安装漂到 pytest 9 后 Linux 全矩阵红
- 修 perf 基线三重污染：save 失败 run 不再污染基线缓存（always()→success()）；
  清理 10 条污染缓存；新增 workflow_dispatch 基线刷新 job
- e2e-nightly pytest 同步钉版

### Removed

- 清理 agent 工具残留目录（.zcode/、.inscode/ 项目快照副本）

## [3.8.0] - 2026-08-26

### Security（深度审计修复 — 3路并行审计，~45项发现）

- **P0 SSRF 全裸修复**：fetcher sync/async 出网请求接入新增 `pipeline_core/url_guard.py`
  （私网/环回/云元数据/DNS解析逐记录校验），redirect 改手动循环逐跳校验（上限5跳）；
  公网302跳内网/169.254元数据端点路径封死（58项新测试）
- **P0 断点续传静默数据损坏**：checkpoint load 恢复 DAG 节点状态，execute_level 跳过已完成节点
  不再重发 bus.request；resume 后 attempts==0 节点绕过持久化幂等键；save 改原子写且失败上浮
- **P0 成本控制双失灵**：`check_budget()` 接线进 llm_router chat/chat_async 调用前（超预算抛
  BudgetExceededError）；chat_async 补记成本（writer 主力路径不再漏记）；PRICING 补
  openai/deepseek/moonshot/qwen/default；响应 usage 字段优先计费
- **P1 error字典被判成功**：`{"error":...}` 结果进入业务失败通道（此前下游拿空数据绿色DONE）
- **P1 message_bus 持锁投递地雷拆除**：REQUEST/RESPONSE 移到锁外 deliver；订阅者异常立即回覆
  错误响应（消灭单节点空烧 node.timeout×max_retries≈20分钟）
- **P1 scheduler 同层依赖校验漏洞**：同层依赖现在正确报错（此前并行执行确定性读到空上游结果）
- **P1 熔断器/限流器切 time.monotonic()**：NTP 回拨不再把令牌桶扣成负值持续拒流
- **P1 进程池 BrokenProcessPool 自愈**：销毁中毒单例重建并重试一次，子进程崩溃不再永久毒化 auto 模式
- **P1 CORS 敞口收口**：Origin 白名单（同源/回环/ADMIN_CORS_ORIGINS env），移除通配 ACAO:*
- **P1 pipeline.started 事件配置脱敏**：api_key/token/secret/password 叶子值 ***redacted***
  （此前明文落盘消息库并广播 webhook）
- **P1 CLI 契约**：--pipeline 拼错报错列出可用名退出码2（此前静默改跑第一个yaml）；
  任务 FAILED 进程退出码 1（此前恒 exit 0）
- **P1 config 热更新半写容错** / **version_manager 索引原子写+损坏安全模式（不清零历史）** /
  rollback 写回前自动保存当前内容 / quality_gate 英文功能词不再误判专名（消灭无谓三轮重烧） /
  scripts/safe_writer 对齐 os.replace 原子替换

### Fixed

- `/stream/metrics` 恒为零的功能性失效（改为跨任务聚合快照）
- SSE 统一 StreamEvent 序列化（ts/section/total 上线）+ 15s 心跳帧 + 客户端断连取消流水线止损
- MCP 业务失败改 isError:true 语义；get_task 校验 task_id；generate_document 支持 output；
  流水线目录锚点统一项目根；mcp_ 临时输入文件清理
- Admin API 错误格式统一（503/400 语义、兜底不泄内部异常）；task_{id}.md 输入临时文件清理
- openapi_spec：TaskInfo 枚举补 paused、/stream/metrics 内容类型、补 / 与 Last-Event-ID 声明
- streaming pause 自旋空转（Event→Condition）；Registry get_or_create 支持配置热更新
- dashboard：API_BASE 同源化、progress 百分比换算修正

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added

- **dashboard 新建任务卡片**：query/pipeline(下拉含 docreq)/output 表单提交 POST /api/tasks，
  running 任务展开 EventSource 实时章节进度——docreq/docgen-verified 获得 UI 触达
- `pipeline_core/url_guard.py` 共享 URL 安全校验模块
- 新增测试 ~200 项（642→840 passed），ruff/mypy 零告警
## [3.7.0] - 2026-08-25

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（需求分析器）
- **requirements_analyzer 需求分析 Agent**：流水线最前端的意图解析节点，把用户输入
  解析为结构化 `DocumentSpec`（doc_type / scope / audience / depth / constraints /
  sources / template / language），供下游 researcher 与 writer 消费：
  - 双路径：有 LLM 走一次小调用生成 JSON（含枚举校验与置信度钳制，非法值回落默认），
    无 LLM / 失败时回退规则引擎（类型·深度·读者提示词匹配 + 关键词提取 + URL/文件引用收集）
  - 歧义检测：输入过短、类型不明、受众未指定时降低 confidence 并生成追问建议
    （field/question/suggestion），confidence 低于阈值（默认 0.7，可配）标记
    needs_clarification，追问条数上限可配（max_questions）
  - 下游接线：dag_executor 将 spec 注入所有后续节点 payload；researcher 用 spec.scope
    补充检索词；writer 用 doc_type 前缀标题、scope 兜底主题、audience 记录读者水平
- 新增 `pipelines/docreq.yaml`（9 层 DAG，首层 requirements_analyzer）。
  命名避开 `docgen*` 前缀以保持既有默认流水线解析顺序不变；
  运行方式：`python run.py input.md --pipeline docreq`

### 测试
- 新增 tests/test_requirements_analyzer.py（24 项）：规则分析各维度、DocumentSpec
  往返序列化、关键词提取去重/截断/停用词、handle 的 DAG 输入文件读取、LLM 成功/
  失败回退/非法枚举回落、analyze() 便捷函数

## [3.6.0] - 2026-08-24

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（内容生产能力提升）
- **fact_checker 事实核查 Agent（MVP）**：
  - 从最终文档提取数字类可验证声明（百分比/带单位数值/年份/版本号，上限可配），
    对照检索源做一致性核查：无 LLM 用归一化字符串匹配（零成本基线），有 LLM 用
    批量语义判定（supported/refuted/unverifiable），LLM 失败自动回退字符串匹配
  - 未核实声明在文档尾部附加「事实核查附注」（明确标注：启发式核查，
    unverifiable ≠ 错误），核查报告同时写入节点结果供 API/MCP 消费
  - 新增 `pipelines/docgen-verified.yaml`（8 层 DAG：checker → fact_checker → layout）；
    **默认 docgen 流水线零改动**
- **主流 LLM 供应商预置**：llm_router 新增 openai / deepseek / moonshot / qwen 四个
  OpenAI 兼容供应商定义（此前仅国内二线云厂商），`.env.example` 补三行组配置示例；
  Claude 原生接口非 OpenAI 格式，已在模板中说明经兼容网关接入

### Fixed（降级透明化）
- **空章节不再静默交付**：writer 无 LLM 路径下未能填充内容的章节，现在会在文档头部
  插入「⚠️ 降级声明」块列出章节名与修复建议，CLI 渲染时同步输出 stderr 警告，
  result.stats 带 empty_sections 字段供程序化消费
- 移除 run.py 中一段历史遗留的不可达死代码（three_pass 分支内 except 块后的 ascii-fix）

### Removed（Breaking）
- **移除已废弃的 ThreePassPipeline**（该模块自带的 DeprecationWarning 声明
  "不再维护，将在未来版本移除"）：删除模块、`--three-pass` CLI 参数、包导出。
  迁移：`--pipeline docgen` 已覆盖同等能力且更完善；`--three-pass` 现在打印迁移指引并 exit 2
- 删除 config.json / config.production.json 中零消费的死配置键 `writer.template_dir`
  （writer 实际使用内置骨架模板，templates 目录从未存在）

### 测试
- 新增 tests/test_fact_checker.py（13 项）：声明提取/来源匹配/嵌套来源收集/
  LLM 失败回退/附注渲染；docgen-verified.yaml 解析与 fact_checker 注册端到端验证

## [3.5.0] - 2026-08-24

### Fixed（第九轮审查修复：API 契约 / 鉴权可用性 / MCP / CICD）

**鉴权与 Dashboard 可用性**
- **鉴权本机信任模式**：未配置 `ADMIN_API_KEY` 时，绑定回环地址免鉴权访问
  （此前所有受保护端点无条件 401，Dashboard 永远空白且无任何提示）；
  绑定非回环地址 + 无 key → **拒绝启动**（安全门），与 docs/api.md 原有描述对齐
- Dashboard 前端：请求携带 Bearer Token（localStorage）；401 时弹出 Token 输入框自动重试
- run.py 消费配置文件的 `admin_api.host/port`（此前该配置块整体无效，
  config.production.json 的 `0.0.0.0` 绑定从未生效）

**API 契约修复**
- **OpenAPI 安全声明反转修正**：新增全局 `security: [BearerAuth]`；
  `/stream` 移除错误的 `security: []`（实际需鉴权）；`/health` 显式豁免——
  此前整份 Spec 将全部端点描述为公开，与实现完全相反
- **版本管理端点接线**：`_handle_versions_list/diff/rollback/stats` 四个 handler
  已实现但从未注册路由（全部 404 死代码）→ 现已接入 do_GET/do_POST 并补 OpenAPI 定义
- 错误响应信封统一为 `{"error": ...}`（消除三种顶层结构并存）
- `cancel/pause/resume/rerun` 任务不存在时返回 **404**（此前 200 + false 无法区分）
- `/api/dashboard`、`/api/pipeline`、`/stream/metrics`、versions 四端点补入 OpenAPI；
  TaskSubmit schema 补 `output` 参数

**前端字段契约修复（Dashboard 三处恒错数据显示）**
- Queue Depth 改读 `/health` 顶层 `queue_depth`（原读 metrics 子对象不存在的字段，恒 0）
- DB Store 显示 `store.messages` 条目数 + `db_size` MB（原读不存在字段恒显示 "6 entries"）
- 任务列表改用 `/api/dashboard` 聚合端点，进度条真实生效
  （原读 `/tasks` 列表不存在的 progress/steps 字段，恒 0%）
- 部分请求失败时状态栏显示"⚠ 部分数据不可用"角标（原先静默渲染空数据）

**其他修复**
- MCP Server `get_pipeline_info` 必失败修复：改读 `plan.levels`
  （原访问不存在的 `plan.execution_order/dag_nodes` 属性，每次调用 -32603）；
  SERVER_VERSION 动态取包版本（原硬编码 3.2.0）
- SSE 流水线线程 `worker.join()` 补 120s 超时（原无限阻塞可耗尽 HTTP 线程）
- pause() 不再在节点并发执行中途保存撕裂 checkpoint；改为执行循环在暂停边界
  （上一 level 完结后）保存一致性快照；语义已在 docstring 文档化
- POST 路由先鉴权后读请求体；task_id 路径参数接入 `_validate_task_id` 校验
- 删除死模块 `batch_queue.py`（167 行语句零调用方）

### Changed（CICD）
- **perf-regression job 从形同虚设变为真实回归门**：
  baseline 经 actions/cache 在运行间传递 + benchmark.py 对比通过后滚动更新基线
  （此前 baseline 被 gitignore 导致 CI 永远走"无 baseline 跳过检测"分支）

### 测试
- 新增 `tests/test_auth_contract.py`（15 项）：真实 HTTP 层鉴权矩阵、
  versions 路由接线、404 语义、OpenAPI 安全契约、MCP get_pipeline_info

## [3.4.0] - 2026-08-22

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（进程执行模式正式支持）
- **`executor_type: process` 从实验性限制变为可用特性**——子进程上下文自动重建：
  - `DAGExecutor` 新增 `child_context` 配置（agents_dir / agent_names / config，
    由 `PipelineOrchestrator.register_agents()` 写入），纯数据、可 pickle
  - `_execute_node_worker` 在 worker 进程内依据 child_context 一次性重建
    Registry（关闭健康检查线程）与非持久化 MessageBus，经 AgentLoader 加载 Agent；
    每个worker 进程仅重建一次（模块级缓存）
  - `__getstate__` 补齐剥离全部含线程锁组件（熔断器/限流器/Metrics/查询缓存），
    修复此前 DAGExecutor 整体 pickle 必然失败导致节点回落父进程重试的问题；
    `__setstate__` 对剥离组件按需重建可用实例
- 新增 `tests/test_process_mode.py`：pickle 往返、无 context 守卫报错、
  **真实 ProcessPoolExecutor 跨进程执行**（探针 Agent 返回子进程 pid ≠ 父进程 pid）
- 文档：docs/architecture.md §5 更新为进程模式工作原理 + 已知限制

### Changed
- executor_factory 进程模式告警更新：不再称"实验性/必然失败"，改为说明序列化开销与隔离限制

## [3.3.3] - 2026-08-22

### Changed（剩余技术债清零）
- **边缘超长方法拆分**（上轮 150L 边缘项全部处理，全仓 >150L 函数归零）：
  - `run.py:main` 199L → ~110L（提取 `build_arg_parser`）
  - `run.py:_run_single_task` 157L → ~60L（提取任务摘要/进度轮询/步骤收集/
    输出路径解析/结果渲染 5 个函数）
  - `pipeline.py:run_plan_async` 153L → ~85L（池化合并复用 `_merge_pooled_results`；
    收尾提取为 `_finalize_plan_task_async`，与同步版差异保留：不更新 task_queue、不发事件钩子）
  - `scripts/safe_writer.py:safe_write` 161L → ~90L（提取备份+manifest/换行符处理/
    临时文件写入/体积行数校验 4 个函数）
  - `admin_api.py:_handle_stream` 167L → ~80L（提取 SSE 帧发送/重连 replay/
    writer 查找/后台流水线线程 4 个方法）
- **.dockerignore 补全**：新增 checkpoints/versions/backups/cache/bus_data/
  .pytest_tmp/.test_checkpoints/.test_outputs/.zcode（运行时产物不进镜像构建上下文）

### Fixed
- mypy：run.py 提取函数的 Any 返回值显式 str() 收敛（3 处 no-any-return）

## [3.3.2] - 2026-08-22

### Changed（技术债清理）
- **超长方法拆分**（行为不变，582 测试全过）：
  - `writer.py:_restructure_document` 237L → 57L（提取 prompt 增强/素材构建/异步生成/
    Mermaid 修复/拼接装配等 7 个方法；顺带删除无调用的死代码嵌套函数 `_llm_generate`）
  - `pipeline.py:run_plan` 192L → 116L（提取 `_merge_pooled_results` / `_finalize_plan_task`）
  - `dag_executor.py:execute_level` 190L → 62L、`execute_level_async` 178L → 瘦身
    （共享提取：业务失败判定/重试结果评估/节点成功落账/futures 提交/同步与异步重试循环/步骤记录，
     两版本 finally 记录逻辑去重为单一 `_record_step_result`）
  - `openapi_spec.py:generate_spec` 267L → ~30L（按 section 拆为模块级函数；
     生成结果经 JSON diff 验证字节级一致）
- **Dashboard XSS 加固**：app.js 全部 6 处 innerHTML 字符串拼接改为
  createElement/textContent DOM 构建，动态数据不再经过 HTML 解析；
  移除不再需要的 escapeHtml 辅助函数
- **文档补全**：新增 docs/api.md（REST API 参考）、docs/architecture.md（分层架构 +
  线程模型 + 数据流）、docs/agents.md（Agent 开发指南 + 沙箱规则）；README 增加文档索引

### Fixed
- mypy：run_plan 提取后 `combined` 字典失去上下文推断导致的 2 个新错误（显式标注）；
  dag_executor 辅助函数返回类型收紧（原代码靠 type: ignore 压制）

## [3.3.1] - 2026-08-22

### Fixed
- **writer.py 生产 bug**：`_restructure_document` 的 `asyncio.gather` 在事件循环外调用，
  配置 LLM Key 且走同步调用路径时必然抛 `RuntimeError`（无 current event loop，协程未 await）。
  修复为在运行中的 loop 内执行 gather；同时移除集成测试中掩盖该缺陷的 `contextlib.suppress`
- scripts/format_converter.py: 清零 18 个 mypy 类型错误（str/Path 混用、Optional 未收窄），行为不变
- llm_router.py: 注册 atexit 钩子关闭共享 aiohttp Session，消除连接泄漏告警
- Dockerfile: 显式创建并 chown checkpoints/logs/versions/backups 目录，补全 VOLUME 声明
  （修复匿名卷 root 属主隐患；versions/backups 数据不再随容器销毁丢失）
- 文档：README/deployment.md 与 config.json 实际搜索引擎列表对齐（原 mock 描述过时）；
  Python 版本要求统一为 3.11+（与 pyproject 一致）；测试计数更新为 582；
  生产配置对比表与 config.production.json 实际内容对齐

### Changed
- CI: perf-regression 在 push main 时也触发（原来仅 PR）；dev 依赖安装加版本下界（对齐 pyproject dev extras）
- run.py: `_resolve_pipeline_plan` 返回类型精确为 `tuple[Any, bool]`

## [3.3.0] - 2026-08-11

### Fixed
- 修复全部140项审计问题（24 P0 + 58 P1 + 58 P2）
- P0: pipeline.py run() NameError、run_steps清理、PipelineTask pickle化
- P0: dag_executor.py fail_fast软中断
- P0: message_bus_v3.py 超时竞态、幂等原子性、DLQ处理
- P0: task_queue.py fd泄漏(threading.local)、total_changes回归
- P0: circuit_breaker.py HALF_OPEN CAS原子递增、回调移出锁外
- P0: rate_limiter.py Condition关联锁、notify_all
- P0: cache_manager.py 回填用文件原始ts、双重record_set修复
- P0: llm_router.py aiohttp timeout、异常吞没
- P0: search_engines.py 异常吞没、CacheManager.put→set、线程安全
- P0: admin_api.py /stream鉴权、webhook SSRF防护、output路径白名单
- P0: checkpoint_manager.py task_id路径遍历校验
- P0: version_manager.py rollback路径白名单
- P0: agent_loader.py AST沙箱增强(ImportFrom+裸名黑名单)
- P0: quality_gate.py 专有名词覆盖率阈值策略
- P0: safe_writer_agent.py _current_payload初始化、handle_writer_done写入
- P0: checker.py 移除重复订阅
- P0: researcher.py _search_manager属性名修复
- P0: run.py YAML加载失败友好退出、SIGTERM handler、局部导入修复
- P1: observability.py log put阻塞→put_nowait
- P1: event_hook.py 冗余_ensure_webhook_engine
- P1: registry.py _check_respawn持锁外部调用
- P1: quality_feedback.py busy_timeout PRAGMA
- P1: document_enhancer.py 空内容回退
- P1: three_pass_pipeline.py ThreadPoolExecutor(0)边界
- P1: benchmark.py 除零保护
- P1: scripts/markdown_checker.py _check_structure修复
- P1: scripts/safe_writer.py checksum自引用修复、file_checksum异常处理
- P1: scripts/convert_ascii.py ASCII_TREE_PATTERN乱码修复
- P1: scripts/format_converter.py 列表项<ul>包裹、mermaid重名

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 新增10个测试模块（test_llm_router, test_search_engines, test_quality_gate_scoring, test_run, test_benchmark, test_markdown_checker, test_safe_writer, test_layout_optimizer, test_convert_ascii, test_format_converter）
- 525个测试全部通过

## [3.2.0] - 2026-08-08

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 成本追踪/告警/质量闭环/MCP Server/Agent沙箱/集成测试

## [3.1.0] - 2026-08-06

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- async I/O + orjson + SSE reconnect + fast_json module
- PEV-ready API extensions + EventHook system
- /stream endpoint with end-to-end async pipeline
- run_plan_async + on_stop lifecycle hook
