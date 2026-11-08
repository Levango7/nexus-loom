"""实时流行（live stream）三处缺陷：F3（running 行）、F12（断线提示）、F13（重放帧）。

证据来源＝2026-11-07 生产力审计 L2 前端线报告 §4 的 F3 / F12 / F13（板面 §二十三 台账归 zcode C-24；
配色两条由 #112 承担）。三条都在**同一段代码路径**上（`toggleLiveStream` → `handleLiveEvent` →
`closeLiveStream`），故成一组。

- **F3**（P2）：三个症状同源 ——
  ① 流行只在 `renderTasks` 里插 ⇒ 点完要等下一轮 5s 轮询才可见（实测 1.5s 内界面零变化）；
  ② `toggleLiveStream` 是**开关**且状态不可见 ⇒ "以为没点上"再点一次 = 静默关掉
     （实测双击一次，实时进度永远不出现）；
  ③ ID 单元格写着 title「查看运行详情」+ 链接样式，却**没有自己的处理器** ⇒ 点击冒泡到整行、
     走的是实时流；连带 **running 的任务在 UI 里没有进入运行详情的路径**。
- **F12**（P3）：断线提示写进一个**同一 tick 就被摘掉**的节点（`closeLiveStream(id, 0)`），
  而断线常发生在首次渲染之前 ⇒ 界面上零反馈（实测 6 秒后仍 `disconnectTextVisible:false`）。
- **F13**（P3）：跨进程/重放推的是 `type:"run_event"` 帧（后端 `_send_run_event`），前端只认
  `section/progress/complete/error` ⇒ 落 `default` 被忽略，行里长时间停在「等待章节事件… / 0%」。

判据用**绑定敏感**正则（钉住"谁作用于谁、调用顺序"），不用全文子串包含 ——
同批第 1 组（PR #113）的变异电池实测过：全文匹配会被"改个名字 / 加一处恒假短路"蒙混。
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "loom_core" / "dashboard" / "app.js"


def _strip_js_comments(src: str) -> str:
    """去掉整行 `//` 注释与 `/* ... */` 块：判据只看代码（修复处刻意留了注释讲旧写法）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("//"))


def _js() -> str:
    return _strip_js_comments(APP_JS.read_text(encoding="utf-8"))


def _fn_body(src: str, name: str) -> str:
    """取一个函数的函数体（到下一个顶层 `function ` / `/* ----` 段注释为止）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"app.js 里找不到函数 {name} —— 本文件的解析需要改写法"
    rest = src[m.start():]
    nxt = re.search(r"\n  (?:/\* ----|function )", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


# ─── 射程自证 ────────────────────────────────────────────────

def test_extraction_is_not_vacuous():
    """四个被审函数都要真取到，且取到的是函数体。"""
    js = _js()
    for name in ("toggleLiveStream", "insertStreamRow", "handleLiveEvent",
                 "closeLiveStream", "renderTasks"):
        body = _fn_body(js, name)
        assert len(body) > 120, f"{name} 的函数体只取到 {len(body)} 字节，解析器失效"


# ─── F3a：流行要立刻可见 ─────────────────────────────────────

def test_stream_row_is_inserted_immediately():
    """`liveStreams.set(...)` 之后必须**立刻**插行，别等下一轮 5s 轮询。"""
    body = _fn_body(_js(), "toggleLiveStream")
    m_set = re.search(r"liveStreams\.set\(", body)
    assert m_set, "toggleLiveStream 里找不到 liveStreams.set( —— 判据需要改写法"
    # 窗口必须收窄到「set 之后、事件接线（es.onmessage / es.onerror）之前」：
    # 断线处理器里**也**会调 insertStreamRow（F12 的修法），窗口开太大时那个调用会替它满足本条 ——
    # 本判据的第一版就是这样被 MUTANT-N2（撤掉立即插入）逃逸的。
    m_wire = re.search(r"es\.(?:onmessage|onerror)\s*=", body[m_set.end():])
    end = m_set.end() + m_wire.start() if m_wire else len(body)
    assert re.search(r"insertStreamRow\(id,\s*row\)", body[m_set.end():end]), (
        "`liveStreams.set` 与事件接线之间没有 `insertStreamRow(id, row)` —— "
        "流行要等下一轮 renderTasks 才出现，点击后长达 5s 界面无变化（F3a 回归）")


def test_insert_helper_targets_the_tasks_table_row():
    """插行助手必须按任务行定位（插到它紧后面），而不是盲插到表尾。"""
    body = _fn_body(_js(), "insertStreamRow")
    assert "tr[data-task-id]" in body, (
        "insertStreamRow 没有按任务行定位 —— 盲插表尾在长列表里会让流行出现在错误位置")
    assert "insertBefore(" in body, "insertStreamRow 没有插到目标行紧后面"
    assert "tasksPanel" in body, "insertStreamRow 没有限定在 Tasks 面板内查找"


# ─── F3b：再点不再静默关掉 ───────────────────────────────────

def test_repeat_click_does_not_silently_close():
    """`toggleLiveStream` 里**不得**再出现 `closeLiveStream`（幂等"展开"，不是开关）。

    这条是绑定敏感的最小判据：原实现把关闭放在函数首部（`liveStreams.has(id) ⇒ closeLiveStream(id,0)`），
    只要有人把它改回开关形态，这里必红。
    """
    body = _fn_body(_js(), "toggleLiveStream")
    # 只审"重复点击"这一分支：函数体内还有 `es.onerror` 里那条**合法**的断线关闭，不能一起算。
    m = re.search(r"if\s*\(\s*liveStreams\.has\(id\)\s*\)\s*\{(.*?)\n    \}", body, re.S)
    assert m, "找不到 `if (liveStreams.has(id))` 分支 —— 判据需要改写法"
    branch = m.group(1)
    assert "closeLiveStream" not in branch, (
        "重复点击的分支里又出现了 closeLiveStream —— 会再次**静默关掉**实时进度"
        "（F3b 回归：用户以为没点上，再点一次就永远看不到了）")
    assert re.search(r"return\s*;", branch), (
        "重复点击的分支没有 return —— 会继续往下走并**新建**一个流行（重复订阅同一任务）")


# ─── F3c：ID 单元格的承诺与动作必须一致 ──────────────────────

def test_id_cell_has_its_own_handler_to_run_detail():
    """ID 单元格必须自持处理器：停冒泡 + 打开运行详情（它的 title 正是这么写的）。"""
    body = _fn_body(_js(), "renderTasks")
    m = re.search(r"tdId\.addEventListener\(\s*'click'", body)
    assert m, (
        "ID 单元格没有自己的 click 处理器 —— 点击会冒泡到整行（running 行 ⇒ 实时流），"
        "而它的 title/链接样式写的是「查看运行详情」= 假承诺（F3c 回归）")
    seg = body[m.end(): m.end() + 400]
    assert "stopPropagation()" in seg, (
        "ID 单元格的处理器没有 stopPropagation —— 会同时触发行级动作（实时流/详情）")
    assert re.search(r"openRun\(", seg), "ID 单元格的处理器没有打开运行详情"


def test_id_cell_promise_and_action_are_paired():
    """前提：title 与动作成对（title 说"查看运行详情" ⇒ 处理器必须 openRun）。

    若哪天把 title 改成别的语义，本组判据（"承诺与动作一致"）的前提就变了 ⇒ 应复审，而不是恒真。
    """
    body = _fn_body(_js(), "renderTasks")
    assert "tdId.title = '查看运行详情'" in body, (
        "renderTasks 里 ID 单元格的 title 不再是「查看运行详情」—— 若语义变了，请复审本组判据")


# ─── F12：断线提示要留得住 ───────────────────────────────────

def _onerror_block() -> str:
    body = _fn_body(_js(), "toggleLiveStream")
    m = re.search(r"es\.onerror\s*=\s*\(\)\s*=>\s*\{(.*?)\n    \};", body, re.S)
    assert m, "toggleLiveStream 里找不到 es.onerror 处理器 —— 判据需要改写法"
    return m.group(1)


def test_disconnect_notice_survives_long_enough_to_be_read():
    """`closeLiveStream(id, N)` 的 N 必须 > 0（原为 0 ⇒ 同一 tick 就被摘掉，零反馈）。"""
    blk = _onerror_block()
    m = re.search(r"closeLiveStream\(id,\s*(\d+)\)", blk)
    assert m, "es.onerror 里没有 closeLiveStream(id, N) —— 判据需要改写法"
    keep_ms = int(m.group(1))
    assert keep_ms > 0, (
        f"断线后只留 {keep_ms}ms —— 提示写进去就被摘掉，用户看不到（F12 回归）")


def test_disconnect_inserts_the_row_before_scheduling_removal():
    """断线常发生在首次渲染之前：必须先确保行在 DOM 里，再安排延后摘除。"""
    blk = _onerror_block()
    m_ins = re.search(r"insertStreamRow\(", blk)
    m_close = re.search(r"closeLiveStream\(", blk)
    assert m_ins, "es.onerror 没有先 insertStreamRow —— 行不在 DOM 时提示等于没写"
    assert m_close, "es.onerror 没有 closeLiveStream —— 判据需要改写法"
    assert m_ins.start() < m_close.start(), "顺序反了：先安排摘除、后插行，提示仍会被吃掉"


# ─── F13：重放帧要处理，且不许编数 ───────────────────────────

def _run_event_case() -> str:
    body = _fn_body(_js(), "handleLiveEvent")
    m = re.search(r"case\s+'run_event'\s*:\s*\{(.*?)\n      \}", body, re.S)
    assert m, (
        "handleLiveEvent 里没有 `case 'run_event'` —— 重放的运行会落到 default 被忽略（F13 回归）")
    return m.group(1)


def test_run_event_frames_are_handled():
    """`run_event` 必须有自己的分支（后端 `_send_run_event` 就推这一种帧）。"""
    case = _run_event_case()
    assert "sectionName" in case, "run_event 分支没有把事件画出来（行里会一直停在「等待章节事件…」）"


def test_run_event_does_not_fabricate_a_percentage():
    """run_event 帧 `section=-1 / total=0` ⇒ **算不出百分比**，不许硬画进度条。"""
    case = _run_event_case()
    assert ".style.width" not in case, (
        "run_event 分支在写进度条宽度 —— 这类帧没有 total/section，硬算就是编数；"
        "只报游标（seq）才是如实的")
    assert re.search(r"seq", case), "run_event 分支没有用运行级游标 seq 交代推进程度"


# ─── 前提：轮询路径仍要能补插（长列表/被过滤的行） ─────────────

def test_poll_path_still_attaches_existing_stream_rows():
    """`renderTasks` 里"已有流则附上其行"的逻辑必须保留 —— 它是被过滤/延迟场景的兜底。"""
    body = _fn_body(_js(), "renderTasks")
    assert re.search(r"if\s*\(\s*liveStreams\.has\(String\(t\.id\)\)\s*\)", body), (
        "renderTasks 里没有「附上已有实时流行」的分支了 —— 说明该职责被挪走，请复审 F3a 的处置")
