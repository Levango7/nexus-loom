"""核对设计文档里引用的 file:line 是否真的指向声称的东西。

用法：python scripts/check_design_anchors.py  （在仓库根跑）
每条 = (文件, 行号, 该行必须出现的子串)。任一条不符即退出 1 并打印实际行内容。

**不进 CI**：ANCHORS 钉的是 `docs/runstore-eventlog-design-2026-10-21.md` 引用当时的行号，
而那份设计正是要把这些行陆续改掉/删掉的——接进 CI 就变成"写死断言不随迁移增长"的反例。
规矩：Stage 0/1/2/3 每落地一批，手工更新对应条目或删掉该行；某 Stage 全部落地后本脚本可退役。
"""
import pathlib
import sys

ANCHORS = [
    ("loom_core/event_hook.py", 285, "def emit"),
    ("loom_core/message_bus_v3.py", 128, "save_message"),
    ("loom_core/message_store.py", 171, "sqlite3.connect"),
    ("loom_core/audit.py", 31, "def write_audit"),
    ("loom_core/deliveries.py", 50, "def record"),
    # Stage 3a：审批的三个写点从 approvals.py 搬进了 RunStore（投影 + 同事务事件）。
    # §1 那一行"审批本身就是一个写面"的结论必须指向新位置，而不是退役成无处可指。
    # Stage 3c 补账把这三口整体下推 12 行（`close_run` 长成条件裁决版）。
    ("loom_core/" + "run_store.py", 344, "def open_approval"),
    ("loom_core/" + "run_store.py", 365, "def decide_approval"),
    ("loom_core/" + "run_store.py", 406, "def consume_approval"),
    # Stage 3c（补账）的两处新落点：设计文档 §4 末"至多一条终态事件已下沉到存储层"
    # 这句话必须指向一个真实存在的函数，而不是只写在文档里。
    ("loom_core/" + "run_store.py", 271, "def close_run"),
    # B4 触发租约的新写口：设计文档 §3.1 的 schedules 行必须指向真实存在的方法——
    # 注册（register）与取租约（acquire）是两个方法，**只钉一个会让另一半结论无处可指**
    # （合并 #65 收口时 main 上就只剩了 register 这条，acquire 被合并掉了）。
    # 实测行号 570/587（早先写的 573/590 偏 3 行；本脚本不进 CI，所以偏了没人红）。
    ("loom_core/run_store.py", 613, "def register_schedule"),
    ("loom_core/run_store.py", 630, "def acquire_schedule_lease"),
    ("loom_core/backfill.py", 35, "class RunBackfill"),
    ("loom_core/backfill.py", 59, "def replay"),
    # Stage 3b：`complete()` 摘掉 result 参数，整体下移到 203。
    ("loom_core/task_queue.py", 203, "def complete"),
    # Stage 3b 给 task_queue 加了第 4 列（projection 健康位），迁移调用也跟着变成 4 处。
    ("loom_core/task_queue.py", 120, "add_column_if_missing"),
    ("loom_core/task_queue.py", 123, "add_column_if_missing"),
    ("loom_core/task_queue.py", 127, "add_column_if_missing"),
    ("loom_core/task_queue.py", 134, "add_column_if_missing"),
    ("loom_core/checkpoint_manager.py", 38, "def save"),
    ("loom_core/checkpoint_manager.py", 87, "os.replace"),
    ("loom_core/run_handle.py", 124, "def status"),
    # Stage 3b：`projection()` 新读维插在 artifacts 之前，三个方法一起下移 25 行。
    ("loom_core/run_handle.py", 203, "def artifacts"),
    ("loom_core/run_handle.py", 261, "def _ledger_record"),
    ("loom_core/run_handle.py", 306, "def to_dict"),
    ("loom_core/db_backend.py", 90, "add_column_if_missing"),
    # 2026-11-10 再钉一次：origin/main 5bf6445（L4-01 run-metrics）+ 25aed24（C-23 二次修正）
    # 两批把 dag_executor.py 推了 14 行、pipeline.py 推了 6~14 行（cancel/resume +6、
    # record +14 等，非常数偏移）；逐条按整行原文重定位，未按偏移推算。
    ("loom_core/dag_executor.py", 869, "def _merge_resumed_nodes"),
    ("loom_core/dag_executor.py", 1000, "def _reuse_completed_node"),
    ("loom_core/streaming.py", 154, "_history"),
    # pipeline.py 这一批随 Stage 0（异步并轨）+ Stage 1（双写口）整体下移：Stage 0 插了
    # 4 行字段 + 61 行 helper，Stage 1 又插 7 行 init + 108 行写口。**偏移不是常数**
    # （+4 / +65 / +47 / +95 / +106 并存），所以 remap 只能按整行文本对齐，不能推算。
    # Stage 3b 再钉一次：`_record_plan_terminal_state` 里的"先悲观后升格"与
    # `_runstore_close` 让下面的调用点又各自下移（同样不是常数偏移）。
    # Stage 3c 再钉一次：cancel() 结清待批 + 补账两个入口把 pipeline.py 又推了 18~43 行。
    # 2026-10-29 再钉一次：main 上 #78（进程执行器 + db_backend 迁移）把 loom_core 推了
    # 6~34 行（pipeline/run_store/dag_executor/db_backend/deliveries 五处、共 17 条），
    # 按整行文本就近重钉后 checked=52 mismatched=0 复验。
    # 2026-11-12 第二次钉：T6（PR #133）pipeline.py 补 output_bytes 三态（+7）、
    # admin_api.py 加交付物下载口（dispatch +7 / 投影 +2 / handler +51），
    # 偏移非常数（+7/+9/+60 并存），逐条按整行原文在合并后的树上重钉后复验。
    # 2026-11-12 再钉一次：#78 之后 beb6242（L4-02 GC）/4ae0394（async 尾部钉测）/
    # 0dbc4a2（#132 空计划守卫）三批把 run_store.py 推 +31/+31/+31/+35/+35、pipeline.py
    # 推 +5/+8/+5/+8/+11/+11/+11（偏移非常数），逐条按整行原文重钉后复验。
    ("loom_core/pipeline.py", 321, "_max_task_history"),
    ("loom_core/pipeline.py", 759, "def cancel"),
    ("loom_core/pipeline.py", 802, "def resume"),
    # 原 1290「不更新 task_queue」那句 docstring 已被 Stage 0 改掉——退役它，改钉新增的
    # 共用写口：§2 那条裂缝的"已并轨"结论必须指向一个真实存在的函数。
    ("loom_core/pipeline.py", 456, "def _record_plan_terminal_state"),
    # 注：legacy `run()` 已改走同一条发布点；run_plan_async 也发 pipeline.started（1608），
    # 「只剩 run_plan() 里这一处」已过时，本条钉同步发布点（1487）
    ("loom_core/pipeline.py", 1487, "pipeline.started"),
    ("loom_core/pipeline.py", 1064, "pipeline.finished"),    # _finalize_plan_task 内
    ("loom_core/pipeline.py", 1529, "pipeline.finished"),    # _finalize_plan_task_async 内
    ("loom_core/pipeline.py", 1808, "record"),               # _deliver 内 DeliveryLedger().record
    ("loom_core/pipeline.py", 1906, "pipeline.audit"),
    # 2026-11-06：#106（C-23）给 resolve_pipeline_name 的 docstring 加了 4 行，
    # 只有这一条在其下方，2049→2053；同批其余 pipeline 锚点都在改动点之上，未动。
    ("loom_core/pipeline.py", 2077, ".done"),
    # 2026-11-06 再钉一次：控制台三面（#90/#91/#93）在 admin_api.py 里加删代码，
    # 这组 10 条整批下移（1072→1074 … 2019→2052，偏移不是常数，按整行文本对齐）。
    # 2026-11-07 第三次：批次一（#107 F1 / #108 F2 / #109 F3 / #110 F4+F5）又在 admin_api.py
    # 增删了路由与校验，这组锚点再次整体下移（+83…+104，偏移同样不是常数）；
    # 逐条按**整行原文**在合并后的树上重新定位，未按偏移推算。
    ("loom_core/admin_api.py", 754, "get_task"),
    ("loom_core/admin_api.py", 1184, "list_tasks"),
    # Stage 2：/tasks、dashboard、cost 三处读口从 `list_tasks`（内存）换成
    # `list_runs`（内存缓存 ∪ 投影）——锚点跟着换而不是删，否则"读侧已并轨"
    # 这句话在表里就没有对应物了。
    ("loom_core/admin_api.py", 1202, "list_runs"),
    ("loom_core/admin_api.py", 1548, "def _task_checkpoint_view"),
    ("loom_core/admin_api.py", 1948, "list_runs"),
    ("loom_core/admin_api.py", 2522, "list_runs"),
    ("loom_core/admin_api.py", 2886, "task = _aio.run(orch.run_plan_async("),
    ("loom_core/admin_api.py", 2955, "existing_callback = get_callback(task_id)"),
    # #8：账本与锁的只读 HTTP 面。三条 handler 进表，"文档教了这三个口"才在代码里
    # 有对应物（`test_doc_consistency` 只把文档对到 openapi 规范，规范和 handler
    # 之间没人守——这条就是补那段距离的）。
    ("loom_core/admin_api.py", 2056, "def _handle_deliveries"),
    ("loom_core/admin_api.py", 2074, "def _handle_delivery_detail"),
    ("loom_core/admin_api.py", 2216, "def _handle_locks"),
    # 锁路径解析只留一处：巡检口（`lock_drift`）与执行口（`_verify_lock_after_parse`）
    # 各自解析一次就会报出执行时根本不会发生的漂移。
    ("loom_core/scheduler.py", 338, "def resolve_lock_path"),
    ("loom_core/scheduler.py", 357, "def lock_drift"),
    # 账本的读口：`list` 在本批之前**没有任何生产调用者**（只有测试读它）。
    ("loom_core/deliveries.py", 90, "def list(self, limit"),
]

root = pathlib.Path(__file__).resolve().parent.parent
bad = []
for rel, lineno, needle in ANCHORS:
    path = root / rel
    if not path.is_file():
        bad.append((rel, lineno, needle, "<文件不存在>"))
        continue
    lines = path.read_text(encoding="utf-8").splitlines()
    actual = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "<行号越界>"
    if needle not in actual:
        bad.append((rel, lineno, needle, actual))

print(f"checked={len(ANCHORS)} mismatched={len(bad)}")
for rel, lineno, needle, actual in bad:
    print(f"  {rel}:{lineno} 期望含 {needle!r} 实际 {actual[:110]!r}")
sys.exit(1 if bad else 0)
