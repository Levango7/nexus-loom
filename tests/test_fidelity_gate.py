"""静默绿修复：空响应 / 占位产出不得被记成 success。

实测过的三类"绿着失败"：
  1. `bus.request` 命中历史幂等键返回 None → dag_executor `result or {}`
     → 节点空转却记 success（复现方式：复用同一个 task_id 再跑一次，
     幂等库在 message_store.py:28 按 checkout 绝对路径共享）。
  2. quality_gate 返回 `{"status":"error","message":"内容为空"}`，
     被 `_handle_regeneration` 末尾的 `final_status = ... else "pass"`
     无条件改写成通过。
  3. writer 缺素材时产出 99 字节占位文档（"未采集到可整合的搜索结果"），
     评分维度只量"写得好不好"，量不出"到底有没有内容"。
"""
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loom_core.dag_executor import DAGExecutor  # noqa: E402


def _make_executor():
    return DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                       MagicMock())


REAL_DOC = (
    "# Kafka 配额与限流\n\n"
    "## 生产者配额\n\n"
    "producer_byte_rate 限制单个客户端每秒可写入的字节数。超过阈值后请求不会"
    "被直接拒绝，而是进入节流等待，从而避免突发流量打爆 broker 的网络线程。"
    "配额服务每 15 秒重新计算一次剩余份额，并在租户之间按权重公平分配。\n\n"
    "## 参考资料\n\n- [Kafka 官方文档](https://kafka.apache.org/documentation)\n"
)

PLACEHOLDER = "# 自动生成文档\n\n> 生成时间: 2026-10-04\n\n未采集到可整合的搜索结果。\n"


@pytest.fixture
def gate():
    """不跑 LLM 的 QualityGateAgent：门禁判据本身是纯规则。"""
    from loom_core.base_agent import AgentMeta, Message
    from packs.docgen.agents.quality_gate import QualityGateAgent
    agent = QualityGateAgent("quality_gate",
                             AgentMeta(name="quality_gate", version="2.0"),
                             {"quiet": True}, None, None)
    return agent, Message


# ─── 1. 幂等命中不得伪装成执行结果 ────────────────────────

class TestDuplicateKeyIsNotSuccess:
    def test_bus_returns_structured_error(self, tmp_path):
        from loom_core.message_bus_v3 import MessageBus
        bus = MessageBus(db_path=str(tmp_path / "bus.db"), enable_persistence=True)
        try:
            calls = []
            bus.subscribe("idem.err", lambda m: calls.append(m) or {"ok": True})
            first = bus.request("idem.err", "t", "a", {}, timeout=5,
                                idempotency_key="dup-key-1")
            assert first == {"ok": True}
            dup = bus.request("idem.err", "t", "a", {}, timeout=5,
                              idempotency_key="dup-key-1")
            assert isinstance(dup, dict), f"去重命中不得返回 None，实际 {dup!r}"
            assert dup.get("error") == "duplicate_idempotency_key"
            assert dup.get("idempotency_key") == "dup-key-1"
            assert len(calls) == 1, "去重命中不应再次触达订阅者"
        finally:
            bus.shutdown()

    def test_dag_node_raises_on_duplicate_key(self):
        """节点收到 duplicate_idempotency_key 必须抛错，而不是 {} + success。"""
        ex = _make_executor()
        dup = {"status": "error", "error": "duplicate_idempotency_key",
               "idempotency_key": "t1:writer:1"}
        ex.bus.request.return_value = dup
        task = MagicMock()
        task.id = "t1"
        node = MagicMock()
        node.agent_name = "writer"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        task.dag_nodes = {"writer": node}
        ex._build_node_payload = lambda *a, **k: {}
        with pytest.raises(RuntimeError, match="未执行：幂等键"):
            ex.execute_node_from_scheduler(task, node, "in.md", MagicMock())


# ─── 2. 质量门控状态不得被无条件洗成 pass ─────────────────

class TestRegenerationStatusDiscipline:
    def test_error_status_is_not_washed_into_pass(self):
        ex = _make_executor()
        result = {"status": "error", "message": "内容为空", "score": 0}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {},
                              regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "error", (
            f"业务失败被软化放行了（原缺陷）：{out['status']}")

    def test_hard_floor_stays_fail(self):
        ex = _make_executor()
        result = {"status": "fail", "hard_floor": True, "needs_regenerate": False,
                  "violations": ["内容过短（40 < 200 字符）"]}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {},
                              regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "fail"
        assert out.get("hard_floor") is True

    def test_soft_fail_after_max_generations_still_warns(self):
        """已达重做上限的低分文档维持既有语义：accepted_with_warnings。"""
        ex = _make_executor()
        result = {"status": "fail", "needs_regenerate": True, "can_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {},
                              regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "accepted_with_warnings"

    def test_pass_stays_pass(self):
        ex = _make_executor()
        out = ex._handle_regeneration(MagicMock(), MagicMock(),
                                      {"status": "pass", "needs_regenerate": False}, {},
                                      regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "pass"


# ─── 3. 硬失败必须穿透 fail_fast=false ─────────────────────

class TestHardFloorIsFatal:
    """`pipeline.fail_fast: false` 的语义是"单个 Agent 的软失败不中断"，
    不能把"产出根本没有内容"也一起兑成 done + exit 0。"""

    def _node(self):
        node = MagicMock()
        node.agent_name = "quality_gate"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        return node

    def test_executor_raises_with_hard_floor_marker(self):
        from loom_core.dag_executor import _HARD_FLOOR_PREFIX
        ex = _make_executor()
        ex.bus.request.return_value = {
            "status": "fail", "hard_floor": True, "needs_regenerate": False,
            "violations": ["内容过短（52 < 120 字符）"],
        }
        ex._build_node_payload = lambda *a, **k: {}
        task = MagicMock()
        task.id = "t1"
        task.dag_nodes = {"quality_gate": MagicMock(result=None, status="pending")}
        with pytest.raises(RuntimeError) as exc_info:
            ex.execute_node_from_scheduler(task, self._node(), "in.md", MagicMock())
        assert str(exc_info.value).startswith(_HARD_FLOOR_PREFIX), exc_info.value
        assert "保真底线" in str(exc_info.value)

    def test_both_level_runners_honor_the_marker(self):
        """同步与 async 两条执行路径都得把它当硬失败（防只修一半）。"""
        src = Path(inspect.getfile(DAGExecutor))
        text = src.read_text(encoding="utf-8")
        assert text.count("_HARD_FLOOR_PREFIX") >= 3, (
            "execute_level 与 execute_level_async 都需检查硬失败前缀，"
            f"实际出现 {text.count('_HARD_FLOOR_PREFIX')} 次")

    def test_soft_fail_still_respects_fail_fast_setting(self):
        """checker 的 P1 问题等软失败语义不变：仍由 fail_fast 决定。"""
        ex = _make_executor()
        result = {"status": "fail", "needs_regenerate": True, "can_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {},
                              regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "accepted_with_warnings"
        assert not out.get("hard_floor")

# ─── 5. 重做目标必须由 Agent 声明 ─────────────────────────

class TestRegenerationTargetIsDeclared:
    """旧实现把目标写死成 writer/quality_gate，等于引擎替领域做决定。"""

    def test_missing_target_fails_loudly(self):
        from loom_core.base_agent import AgentMeta

        ex = _make_executor()
        meta = AgentMeta(name="gatey", version="1.0",
                         input_topics=["gatey.input"],
                         supports_regeneration=True,
                         regeneration_target="", regeneration_recheck="")
        ex.registry.get_meta.return_value = meta
        ex.registry.get.return_value = object()          # 实例存在即可
        ex.bus.request.return_value = {"needs_regenerate": True,
                                       "can_regenerate": True, "overall_score": 40}
        ex._build_node_payload = lambda *a, **k: {}

        node = MagicMock()
        node.agent_name = "gatey"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        node.agent_config.circuit_breaker = {}
        task = MagicMock()
        task.id = "t-rt"
        task.dag_nodes = {"gatey": MagicMock(result=None, dependencies=[],
                                             attempts=0, status="pending")}
        with pytest.raises(RuntimeError, match="REGENERATION_TARGET"):
            ex.execute_node_from_scheduler(task, node, "in.md", MagicMock())


# ─── 6. QualityGate 产出保真底线 ──────────────────────────

class TestFidelityFloor:
    def test_placeholder_document_is_rejected(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": PLACEHOLDER,
                                            "task_id": "t1", "queries": ["Kafka 配额"]}))
        assert res["status"] == "fail"
        assert res["hard_floor"] is True
        assert res["needs_regenerate"] is False, "占位文档重做也救不回来"
        assert any("占位内容" in v for v in res["violations"]), res["violations"]

    def test_too_short_document_is_rejected(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": "# 标题\n\n很短。\n",
                                            "task_id": "t2"}))
        assert res["hard_floor"] is True
        assert any("内容过短" in v for v in res["violations"]), res["violations"]

    def test_real_document_passes_the_floor(self, gate):
        """底线只量"有没有内容"，不能把正常文档也一起拦掉。"""
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC, "task_id": "t3",
                                            "queries": ["Kafka 配额"]}))
        assert not res.get("hard_floor"), res.get("violations")
        assert res["overall_score"] > 0

    def test_thin_but_real_content_is_scored_not_hard_failed(self, gate):
        """底线只拦"没有产出"；薄内容仍走评分→重做（既有语义，不许被劫持）。"""
        agent, Message = gate
        watery = (
            "# Python 简介\n\n"
            "Python 是一门非常通用的编程语言，它很简单，很好用，也很流行，很多人都非常"
            "喜欢它，觉得它是最好的语言，学起来也很容易，用起来也很方便，社区也很庞大，"
            "生态也很丰富，做什么都行，写脚本很快，写服务也行，大家都说好。\n"
        )
        assert len(watery.strip()) >= 120, f"样本需过底线才能测评分路径：{len(watery)}"
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": watery, "task_id": "t4",
                                            "queries": ["Python"]}))
        assert not res.get("hard_floor"), res.get("violations")
        assert res.get("needs_regenerate") is True, \
            f"水话应由评分判出，而不是底线：{res.get('overall_score')}"

    def test_floor_is_configurable(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC[:80],
                                            "task_id": "t4b",
                                            "config": {"min_output_chars": 50}}))
        assert not res.get("hard_floor"), res.get("violations")

    def test_invalid_min_chars_falls_back_to_default(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC[:80], "task_id": "t5",
                                            "config": {"min_output_chars": "很多"}}))
        assert res.get("hard_floor") is True


# ─── 7. 逐节占位：长度过底线不等于"有内容" ─────────────────

def _doc_with_placeholders(total: int, empty: int) -> str:
    """拼一份"看起来有字数、其实大半是占位符"的文档。

    有内容的章节里塞的是抓取回来的网页样板文字 —— 真实事故的形状就是如此：
    长度足以过 `min_output_chars`，内容却几乎全是噪声。
    """
    from loom_doc import degradation
    parts = ["# 自动生成文档", "", "> 主题: Python 异步编程的基本概念和用法", ""]
    for i in range(total - empty):
        body = _SCRAPE_JUNK if i == 0 else (
            "协程不是操作系统提供的能力，而是用户态内的上下文切换技术；"
            "事件循环负责在就绪回调之间调度，因此单线程也能撑住大量并发 IO。")
        parts += [f"## 有内容的一节 {i + 1}", body, ""]
    for _ in range(empty):
        parts += ["## 空的一节", degradation.SECTION_PLACEHOLDER, ""]
    parts += ["## 参考资料", "- [来源](https://example.com/a)", ""]
    return "\n".join(parts)


#: 360 识图页面的导航样板文字节选（实测样本里被当成正文出厂的东西）
_SCRAPE_JUNK = (
    "网页 资讯 AI问答 视频 图片 良医 地图 百科 文库 软件 翻译 360搜索首页 反馈 登录 搜索 "
    "360识图 粘贴图片网址 如何粘贴图片网址： 1. 右键点击网页上的图片，选择「复制图片网址」。 "
    "2. 在搜索框中粘贴该网址(Ctrl+v)，按 enter 键或点击「搜索」按钮。上传图片 提示：您也可以将"
    "图片拖至此处(Chrome 浏览器还支持截图上传)，图片不要超过 2MB 哦~ 不支持文字输入，请输入"
    "图片网址或截屏粘贴的图片 相关搜索：python 基本结构有哪三种 python 用于哪些领域 Python 的"
    "应用领域 java 基础知识点 python 三大结构 python 输入代码 点击下方图片，秒变简笔画。"
    "全部尺寸 大尺寸 中尺寸 小尺寸 壁纸尺寸 自定义 宽 高 确定 全部颜色 全部类型 动态图片 静态图片"
    "全部图片 精选素材 版权图片 版权图搜索上线啦 更多品质、低价、免费版权图片供您选择 我知道了 "
    "360搜索客户端官网 意见反馈 产品论坛 网站收录 使用帮助 推广合作 官方微信 站长平台 隐私管理")


#: 2026-10-06 实跑 keyless docgen 的真实结构：6 个二级章节、4 个交白卷，
#: 正文 3065 字节（远超 min_output_chars=120），当时却拿到 98.8 pass 并落盘。
MEASURED_JUNK_DOC = _doc_with_placeholders(total=6, empty=4)


class TestPlaceholderSectionFloor:
    def test_measured_junk_document_is_rejected(self, gate):
        agent, Message = gate
        assert len(MEASURED_JUNK_DOC) > 600, "样本须远超 min_output_chars=120，才能证明拦它的是占比判据"
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": MEASURED_JUNK_DOC,
                                            "task_id": "p1", "queries": ["Python 异步"]}))
        assert res["hard_floor"] is True, \
            f"4/6 章节交白卷仍被判合格，正是 2026-10-06 的假绿现场：{res.get('violations')}"
        assert any("章节是占位符" in v for v in res["violations"]), res["violations"]
        assert res["needs_regenerate"] is False, "占位章节不是「写不好」，重做也救不回来"

    def test_single_thin_section_is_scored_not_hard_failed(self, gate):
        """底线不许顺手把"偶尔一节没料"也判死——那属于评分该管的事。"""
        agent, Message = gate
        doc = _doc_with_placeholders(total=6, empty=1)
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": doc, "task_id": "p2",
                                            "queries": ["Python 异步"]}))
        assert not res.get("hard_floor"), res.get("violations")

    def test_ratio_threshold_is_configurable(self, gate):
        agent, Message = gate
        doc = _doc_with_placeholders(total=6, empty=2)   # 33%，恰好不超默认阈值
        assert not agent.handle(Message(topic="quality_gate.input",
                                        payload={"content": doc, "task_id": "p3"})).get("hard_floor")
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": doc, "task_id": "p3b",
                                            "config": {"max_placeholder_section_ratio": 0.2}}))
        assert res["hard_floor"] is True, res.get("violations")

    def test_invalid_ratio_falls_back_to_default(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": MEASURED_JUNK_DOC, "task_id": "p4",
                                            "config": {"max_placeholder_section_ratio": "很多"}}))
        assert res["hard_floor"] is True, res.get("violations")

    def test_markers_have_a_single_definition_site(self):
        """writer 与门禁共用一处定义 —— 两处各写一份时漂移过一次（假绿的根因）。"""
        import packs.docgen.agents.quality_gate as qg
        from loom_doc import degradation
        assert qg.PLACEHOLDER_MARKERS is degradation.PLACEHOLDER_MARKERS
        src = (Path(__file__).parent.parent / "packs" / "docgen" / "agents"
               / "writer.py").read_text(encoding="utf-8")
        assert "degradation.SECTION_PLACEHOLDER" in src, "writer 又自带字面量占位串了"
        assert degradation.SECTION_PLACEHOLDER not in src, "writer 里不该再留第二份字面量"

    def test_keyless_refusal_is_placeholder_not_pass(self, gate):
        """治本批（2026-11-10）：keyless 拒绝稿命中占位标记 → 底线判失败。

        keyless 交拒绝自述=没有交付物，门禁必须红；否则"拒绝拼贴"会退化成
        另一种静默绿（字数可能够、占比可能低，就靠这条标记兜住）。
        """
        import packs.docgen.agents.quality_gate as qg
        from loom_doc import degradation
        assert degradation.KEYLESS_REFUSAL in qg.PLACEHOLDER_MARKERS
        agent, Message = gate
        stub = (f"# T\n\n> 生成时间: x\n\n{degradation.KEYLESS_REFUSAL}"
                "：已读取 3 篇素材，无法整合成文\n")
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": stub, "task_id": "kr1",
                                            "queries": ["Kafka"]}))
        assert res["hard_floor"] is True, res.get("violations")
        assert any("占位" in v for v in res["violations"]), res["violations"]


# ─── 8. 抓取层中间格式不得当作成品交付 ────────────────────

#: 2026-10-07 实测两类"字数够、占比过、却是素材直粘"的交付物形状（本机 28.4 KB /
#: CI 21.9 KB 都曾拿 95.5 pass，CI 还把它记成 OUTPUT FIDELITY OK）
FETCH_LEAK_DOC = (
    "# 自动生成文档\n\n## 简介\n\n"
    "标题: 欢迎来到 Python.org - Python 编程语言\n"
    "来源: https://www.python.org/\n"
    "下载时间: 2026-10-07 01:52:32\n"
    + "=" * 60 + "\n\n"
    "Python is a great language.\n\n## 应用场景\n\n"
    "数据分析、Web 开发与自动化都常用它，社区也提供了大量标准库支持。\n"
)


class TestRawFetchBlockFloor:
    def test_scraped_block_header_is_rejected(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": FETCH_LEAK_DOC, "task_id": "f1",
                                            "queries": ["Python"]}))
        assert res["hard_floor"] is True, (
            f"素材块头被当成品出厂仍判合格：{res.get('violations')}")
        assert any("抓取层原始素材" in v for v in res["violations"]), res["violations"]

    def test_real_document_is_not_flagged(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC, "task_id": "f2",
                                            "queries": ["Kafka 配额"]}))
        assert not res.get("hard_floor"), res.get("violations")

    def test_opt_out_is_explicit(self, gate):
        """确有场景要把原文附在交付物里时，必须显式放行，而不是默认放过。"""
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": FETCH_LEAK_DOC, "task_id": "f3",
                                            "config": {"allow_raw_fetch_blocks": True}}))
        assert not res.get("hard_floor"), res.get("violations")

    def test_string_false_does_not_mean_enabled(self, gate):
        """YAML 里 `allow_raw_fetch_blocks: "false"` 是字符串，bool("false") 会判真。"""
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": FETCH_LEAK_DOC, "task_id": "f4",
                                            "config": {"allow_raw_fetch_blocks": "false"}}))
        assert res["hard_floor"] is True, res.get("violations")

    def test_detector_counts_the_fetchers_own_format(self):
        """签名取自 fetcher 落盘格式本身，不是猜的关键词。"""
        import inspect

        import agents.fetcher as fetcher
        from loom_doc import degradation
        src = inspect.getsource(fetcher.FetcherAgent._save_article)
        assert '"标题: ' in src and "{'='*60}" in src, "fetcher 的块格式变了，签名要同步"
        assert degradation.raw_fetch_block_count(FETCH_LEAK_DOC) == 1
        assert degradation.raw_fetch_block_count(REAL_DOC) == 0
