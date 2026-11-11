"""交付账本与锁巡检的只读 HTTP 面（P1 #8）。

要治的形状：账本从 v3.12 起就在写，但只有 CLI 和进程内读得到；续63 让
`incomplete` 变成**可处理**之后，处理之前得先**看见**。锁漂移同理——今天只有
真去跑一条流水线撞异常，才知道 YAML 和锁已经对不上。

判据一律走**真 HTTP**（`AdminAPI` + `urllib`），因为三种失败只有真请求分得开：
handler 写对了但路由没接、路由接了但读的是内存 task、行取对了但越权时把别人的
运行漏出来。

`scope` 字段是本文件里最容易被人"顺手删掉"的一句断言，它是口径本身：
`/api/locks` 回答的是"现在这两份文件对不对得上"，**不回答**"某次运行时用的锁
对不对"（运行时的指纹没有落库，见 `_handle_locks` 的注释）。拿当前的值冒充当时的
证据，比不给这个口更糟。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from loom_core.admin_api import AdminAPI, AdminHandler
from loom_core.deliveries import DeliveryLedger
from loom_core.pipeline import PipelineOrchestrator
from loom_core.run_store import RunStore
from loom_core.scheduler import LockfileMismatchError, Scheduler

PROJECT = Path(__file__).resolve().parent.parent

PLAIN_YAML = """
name: da_plain
agents:
  - name: tldr
    version: "0.1"
    dependencies: []
    config: {max_sentences: 2}
topology:
  levels: [["tldr"]]
  edges: []
"""

INPUT = "# 标题\n\n这是交付账本读面的测试输入，用于触发摘要链路。\n"

TOK_ALICE = "tok-da-alice-secret"
TOK_BOB = "tok-da-bob-secret"
TOK_OPS = "tok-da-ops-secret"


def _state_dir(monkeypatch, tmp_path: Path) -> Path:
    d = tmp_path / "state"
    monkeypatch.setenv("LOOM_STATE_DIR", str(d))
    return d


def _plan(tmp_path: Path, name: str = "plain"):
    d = tmp_path / f"pipe_{name}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.yaml"
    f.write_text(PLAIN_YAML, encoding="utf-8")
    return Scheduler(agents_dir=str(PROJECT / "agents"),
                     pipeline_dir=str(d)).parse_file(str(f), verify_lock=False)


def _orch(tmp_path: Path) -> PipelineOrchestrator:
    orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                checkpoint_dir=str(tmp_path / "ckpt"))
    orch.register_agents()
    return orch


def _run(orch, tmp_path, task_id: str, owner: str = ""):
    inp = tmp_path / f"{task_id}.md"
    inp.write_text(INPUT, encoding="utf-8")
    return orch.run_plan(_plan(tmp_path), input_file=str(inp),
                         task_id=task_id, wait=True, owner=owner)


def _wait_projection(orch, task_id: str, want: str, timeout: float = 10.0) -> None:
    """队列健康位是异步升格的，不能读完就走（等不到就红，不靠重试次数凑）。"""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = orch.task_queue.get(task_id)
        if isinstance(row, dict) and row.get("projection") == want:
            return
        time.sleep(0.05)
    raise AssertionError(f"投影健康位没到 {want!r}：{row}")


def _tokens_yaml(tmp_path: Path) -> Path:
    f = tmp_path / "tokens.yaml"
    f.write_text(f"""
tokens:
  - id: alice
    token_sha256: {hashlib.sha256(TOK_ALICE.encode()).hexdigest()}
  - id: bob
    token_sha256: {hashlib.sha256(TOK_BOB.encode()).hexdigest()}
  - id: ops
    admin: true
    token_sha256: {hashlib.sha256(TOK_OPS.encode()).hexdigest()}
""", encoding="utf-8")
    return f


def _start(orch, tokens_file: str):
    api = AdminAPI(host="127.0.0.1", port=0, api_key="", tokens_file=tokens_file)
    assert api.start(orch) is True
    port = api._server.server_address[1]
    return f"http://127.0.0.1:{port}", api


def _req(method: str, url: str, token: str | None = None, timeout: float = 20.0):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


@pytest.fixture
def served(tmp_path, monkeypatch):
    """两条真运行（分属 alice/bob），内存已清空，服务在答持久面。"""
    _state_dir(monkeypatch, tmp_path)
    orch = _orch(tmp_path)
    _run(orch, tmp_path, "da-alice", owner="alice")
    _run(orch, tmp_path, "da-bob", owner="bob")
    _wait_projection(orch, "da-alice", "ok")
    _wait_projection(orch, "da-bob", "ok")
    orch._running_tasks.clear()
    base, api = _start(orch, str(_tokens_yaml(tmp_path)))
    yield base, orch
    api.stop()
    orch.shutdown()
    AdminHandler.tokens = None
    AdminHandler.orch = None
    AdminHandler.principal = None


# ─── 1. 账本列表 / 明细 ───────────────────────────────

class TestDeliveriesHTTP:
    def test_admin_lists_rows_from_disk_after_eviction(self, served):
        """内存清空后仍列得出来，且带的是账本里的真实字段。"""
        base, _orch = served
        code, body = _req("GET", f"{base}/api/deliveries", TOK_OPS)
        assert code == 200, body
        ids = [d["run_id"] for d in body["deliveries"]]
        assert {"da-alice", "da-bob"} <= set(ids), ids
        row = next(d for d in body["deliveries"] if d["run_id"] == "da-alice")
        assert row["status"] == "done"
        assert row["pipeline"] == "plain"
        assert row["node_count"] == 1, row
        assert isinstance(row["artifact_count"], int)
        assert row["projection"] == "complete", row
        assert body["scope"] == "all" and body["count"] == len(body["deliveries"])

    def test_non_admin_only_sees_rows_whose_projection_is_theirs(self, served):
        """账本自己没有归属列 ⇒ 可见性只能问投影，且必须问到。"""
        base, _orch = served
        code, body = _req("GET", f"{base}/api/deliveries", TOK_ALICE)
        assert code == 200, body
        assert [d["run_id"] for d in body["deliveries"]] == ["da-alice"], body
        assert body["scope"] == "principal"
        code, body = _req("GET", f"{base}/api/deliveries", TOK_BOB)
        assert [d["run_id"] for d in body["deliveries"]] == ["da-bob"], body

    def test_detail_carries_the_node_rows(self, served):
        """明细必须是逐节点载荷——列表给的是计数，补账前要看的是节点。"""
        base, _orch = served
        code, body = _req("GET", f"{base}/api/deliveries/da-alice", TOK_ALICE)
        assert code == 200, body
        d = body["delivery"]
        assert d["run_id"] == "da-alice"
        assert d["projection"] == "complete"
        assert [n["node"] for n in d["nodes"]] == ["tldr"], d["nodes"]
        assert d["nodes"][0]["status"] == "success", d["nodes"]
        assert isinstance(d["nodes"][0]["artifact_keys"], list)
        assert d["plan_id"] and d["input_file"]

    def test_cross_tenant_detail_is_indistinguishable_from_missing(self, served):
        """越权与不存在同形：403 会说"这条在、只是不给你"。"""
        base, _orch = served
        code_cross, body_cross = _req("GET", f"{base}/api/deliveries/da-alice", TOK_BOB)
        code_missing, body_missing = _req("GET", f"{base}/api/deliveries/da-nope", TOK_BOB)
        assert code_cross == code_missing == 404, (code_cross, code_missing)
        assert body_cross == body_missing

    def test_bad_run_id_is_400_not_a_query_oddity(self, served):
        base, _orch = served
        code, _body = _req("GET", f"{base}/api/deliveries/..%2F..%2Fetc", TOK_OPS)
        assert code == 400, "路径里的非法 id 要挡在解析之前"

    def test_list_unreadable_ledger_is_503_not_an_empty_list(self, served, monkeypatch):
        """把"读不到"报成"没有交付"是这批最不能犯的错。"""
        base, _orch = served

        def _boom(self, limit: int = 50):
            raise RuntimeError("deliveries.db 被锁住")

        monkeypatch.setattr(DeliveryLedger, "list", _boom)
        code, body = _req("GET", f"{base}/api/deliveries", TOK_OPS)
        assert code == 503, body
        assert body["error"] == "deliveries unavailable"

    def test_detail_unreadable_ledger_is_503(self, served, monkeypatch):
        base, _orch = served

        def _boom(self, run_id: str):
            raise RuntimeError("deliveries.db 被锁住")

        monkeypatch.setattr(DeliveryLedger, "get", _boom)
        code, body = _req("GET", f"{base}/api/deliveries/da-alice", TOK_OPS)
        assert code == 503, body

    def test_limit_defaults_and_is_clamped(self, served):
        """坏值回落默认、大值夹住：一次请求不能把整本账本拖进内存。"""
        base, _orch = served
        code, body = _req("GET", f"{base}/api/deliveries?limit=abc", TOK_OPS)
        assert code == 200 and body["limit"] == 50, body
        code, body = _req("GET", f"{base}/api/deliveries?limit=999999", TOK_OPS)
        assert body["limit"] == 200, body
        code, body = _req("GET", f"{base}/api/deliveries?limit=1", TOK_OPS)
        assert len(body["deliveries"]) == 1 and body["truncated"] is True, body

    def test_incomplete_projection_shows_up_in_the_list(self, tmp_path, monkeypatch):
        """续63 的出口要有入口：健康位必须出现在账本响应里。

        这里真的把投影盖章炸掉（不是把字段写死成 ok），否则这条判据对
        "handler 里硬编码 projection"这种变异毫无射程。
        """
        _state_dir(monkeypatch, tmp_path)
        orch = _orch(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("runs.db 写不进去")

        monkeypatch.setattr(RunStore, "close_run", _boom)
        _run(orch, tmp_path, "da-broken", owner="alice")
        _wait_projection(orch, "da-broken", "incomplete")
        base, api = _start(orch, str(_tokens_yaml(tmp_path)))
        try:
            code, body = _req("GET", f"{base}/api/deliveries", TOK_OPS)
            assert code == 200, body
            row = next(d for d in body["deliveries"] if d["run_id"] == "da-broken")
            assert row["projection"] == "incomplete", row
            assert row["status"] == "done", "投影缺口不等于执行失败（3b 拍板的口径）"
        finally:
            api.stop()
            orch.shutdown()
            AdminHandler.tokens = None
            AdminHandler.orch = None
            AdminHandler.principal = None

    def test_orphan_ledger_row_is_unknown_for_admin_and_invisible_for_named_token(
            self, served):
        """CLI 直跑/无投影的行：admin 看得见并报 unknown，具名 token 看不见。

        这是"账本没有归属列"的直接后果，写死在这里，将来加列时这条判据必须一起改。
        """
        base, _orch = served
        DeliveryLedger().record({"run_id": "da-orphan", "pipeline": "plain",
                                 "status": "done", "finished_at": time.time(),
                                 "nodes": []})
        code, body = _req("GET", f"{base}/api/deliveries", TOK_OPS)
        row = next(d for d in body["deliveries"] if d["run_id"] == "da-orphan")
        assert row["projection"] == "unknown", row
        code, body = _req("GET", f"{base}/api/deliveries", TOK_ALICE)
        assert "da-orphan" not in [d["run_id"] for d in body["deliveries"]], body
        code, body = _req("GET", f"{base}/api/deliveries/da-orphan", TOK_ALICE)
        assert code == 404, body


# ─── 1b. 交付物下载 ─────────────────────────────────────

def _raw_req(method: str, url: str, token: str | None = None, timeout: float = 20.0):
    """下载判据要的是原始字节与响应头，不是 JSON——单独一条裸读法。"""
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _attach_output(run_id: str, path: Path) -> None:
    """给已存在的账本行补上 output_path（保留其余字段与投影归属）。"""
    rec = DeliveryLedger().get(run_id)
    assert rec is not None, f"账本里没有 {run_id}，夹具先坏了"
    rec["output_path"] = str(path)
    DeliveryLedger().record(rec)


class TestDeliveryDownloadHTTP:
    """`GET /api/deliveries/<run_id>/download`：文件本体，可见性与明细同口。"""

    def test_download_streams_the_recorded_file_bytes(self, served, tmp_path):
        base, _orch = served
        out = tmp_path / "out.md"
        payload = "# 交付物\n\n正文按字节对账。\n".encode("utf-8")
        out.write_bytes(payload)
        _attach_output("da-alice", out)
        code, headers, body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_ALICE)
        assert code == 200, body
        assert body == payload, "下载内容必须与磁盘文件逐字节一致"
        assert headers.get("Content-Length") == str(len(payload))
        disp = headers.get("Content-Disposition", "")
        assert disp.startswith("attachment;"), disp
        assert "out.md" in disp, disp

    def test_download_without_token_is_401(self, served, tmp_path):
        base, _orch = served
        out = tmp_path / "out.md"
        out.write_bytes(b"x")
        _attach_output("da-alice", out)
        code, _headers, _body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download")
        assert code == 401

    def test_cross_tenant_download_is_indistinguishable_from_missing(self, served,
                                                                     tmp_path):
        """越权与不存在同形——下载口不能比明细口多漏一点存在性。"""
        base, _orch = served
        out = tmp_path / "out.md"
        out.write_bytes(b"alice-only")
        _attach_output("da-alice", out)
        code_cross, _h1, body_cross = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_BOB)
        code_missing, _h2, body_missing = _raw_req(
            "GET", f"{base}/api/deliveries/da-nope/download", TOK_BOB)
        assert code_cross == code_missing == 404, (code_cross, code_missing)
        assert body_cross == body_missing

    def test_missing_output_file_is_404_not_500(self, served, tmp_path):
        """账本行还在、文件已被清理：这是"没有可下载的东西"，不是服务出错。"""
        base, _orch = served
        _attach_output("da-alice", tmp_path / "gone.md")
        code, _headers, body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_ALICE)
        assert code == 404, body
        assert json.loads(body)["error"] == "delivery output not on disk"

    def test_row_without_output_path_is_404(self, served):
        base, _orch = served
        code, _headers, body = _raw_req(
            "GET", f"{base}/api/deliveries/da-bob/download", TOK_BOB)
        assert code == 404, body
        assert json.loads(body)["error"] == "delivery has no output file"

    def test_empty_file_downloads_as_zero_bytes(self, served, tmp_path):
        """空文件是 0 字节的 200，不是 404——"空"与"没有"必须可区分。"""
        base, _orch = served
        out = tmp_path / "empty.md"
        out.write_bytes(b"")
        _attach_output("da-alice", out)
        code, headers, body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_ALICE)
        assert code == 200, body
        assert body == b""
        assert headers.get("Content-Length") == "0"

    def test_non_ascii_filename_is_rfc5987_encoded(self, served, tmp_path):
        base, _orch = served
        out = tmp_path / "交付 报告.md"
        out.write_bytes("中文文件名".encode("utf-8"))
        _attach_output("da-alice", out)
        code, headers, _body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_ALICE)
        assert code == 200
        disp = headers.get("Content-Disposition", "")
        assert "filename*=UTF-8''" in disp, disp
        assert quote("交付 报告.md") in disp, disp
        ascii_part = disp.split("filename*=")[0]
        assert ascii_part.isascii(), f"ASCII 回退名里混进了非 ASCII：{ascii_part!r}"
        assert "\r" not in disp and "\n" not in disp

    def test_unreadable_ledger_is_503(self, served, monkeypatch):
        base, _orch = served

        def _boom(self, run_id: str):
            raise RuntimeError("deliveries.db 被锁住")

        monkeypatch.setattr(DeliveryLedger, "get", _boom)
        code, _headers, body = _raw_req(
            "GET", f"{base}/api/deliveries/da-alice/download", TOK_OPS)
        assert code == 503, body
        assert json.loads(body)["error"] == "deliveries unavailable"

    def test_bad_run_id_is_400_before_any_lookup(self, served):
        base, _orch = served
        code, _headers, _body = _raw_req(
            "GET", f"{base}/api/deliveries/..%2F..%2Fetc/download", TOK_OPS)
        assert code == 400, "路径里的非法 id 要挡在解析之前"


# ─── 2. 锁巡检 ────────────────────────────────────────

class TestLocksHTTP:
    """`/api/locks` 的判据。

    巡检缓存挂在类上（跨请求复用），所以每条判据前后都要清——否则上一条的
    结论会串到下一条，尤其是"同一批文件名、不同内容"的两份 lock。
    """

    @pytest.fixture(autouse=True)
    def _reset_survey_cache(self):
        AdminHandler._LOCKS_SURVEY_CACHE.clear()
        yield
        AdminHandler._LOCKS_SURVEY_CACHE.clear()

    def _locks(self, base, query: str = ""):
        """全量巡检要解析 19 条流水线（本机实测冷 7.27s / 再跑 11.23s），给足预算。"""
        return _req("GET", f"{base}/api/locks{query}", TOK_OPS, timeout=120.0)

    def test_scope_is_current_files_and_covers_every_core_pipeline(self, served):
        base, _orch = served
        code, body = self._locks(base)
        assert code == 200, body
        assert body["scope"] == "current files", "口径字段不能被顺手删掉"
        names = {e["pipeline"] for e in body["pipelines"]}
        core = set(Scheduler(pipeline_dir=str(PROJECT / "pipelines")).list_pipelines())
        assert core and core <= names, f"核心流水线没被枚举全：缺 {core - names}"
        for entry in body["pipelines"]:
            assert {"pipeline", "lock_present", "matches", "issues"} <= set(entry), entry
        assert set(body["drifted"]) <= names

    def test_drift_is_reported_as_issues_instead_of_raising(self, served, monkeypatch,
                                                            tmp_path):
        """漂移从异常变成可读的问题表。"""
        base, _orch = served
        d = tmp_path / "lockdir"
        d.mkdir(parents=True, exist_ok=True)
        (d / "drift.yaml").write_text(PLAIN_YAML, encoding="utf-8")
        lock = d / "drift.lock"
        lock.write_text(json.dumps({"pipeline": "drift", "topology_hash": "deadbeef0000",
                                    "agents": {"tldr": {"version": "0.1",
                                                        "config_hash": "0" * 12}}}),
                        encoding="utf-8")
        import loom_core.pack_manifest as pm
        monkeypatch.setattr(pm, "pipeline_dirs", lambda: [d])
        code, body = self._locks(base)
        assert code == 200, body
        entry = next(e for e in body["pipelines"] if e["pipeline"] == "drift")
        assert entry["lock_present"] is True and entry["matches"] is False, entry
        assert entry["issues"], "报了不匹配却没给出差在哪，等于没报"
        assert body["drifted"] == ["drift"], body
        assert lock.exists()

    def test_absent_lock_is_never_reported_as_matching(self, served, monkeypatch, tmp_path):
        """没有锁可比 ⇒ matches 给 null，不给 true（"没发现问题"和"没法判断"互斥）。"""
        base, _orch = served
        d = tmp_path / "lockdir2"
        d.mkdir(parents=True, exist_ok=True)
        (d / "nolock.yaml").write_text(PLAIN_YAML, encoding="utf-8")
        import loom_core.pack_manifest as pm
        monkeypatch.setattr(pm, "pipeline_dirs", lambda: [d])
        code, body = self._locks(base)
        entry = next(e for e in body["pipelines"] if e["pipeline"] == "nolock")
        assert entry["lock_present"] is False, entry
        assert entry["matches"] is None, entry
        assert body["drifted"] == [], "缺锁不是漂移"

    def test_one_broken_yaml_does_not_take_down_the_endpoint(self, served, monkeypatch,
                                                             tmp_path):
        base, _orch = served
        d = tmp_path / "lockdir3"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.yaml").write_text("agents: [ {", encoding="utf-8")
        import loom_core.pack_manifest as pm
        monkeypatch.setattr(pm, "pipeline_dirs", lambda: [d])
        code, body = self._locks(base)
        assert code == 200, body
        entry = next(e for e in body["pipelines"] if e["pipeline"] == "bad")
        assert entry["matches"] is None and entry["issues"], entry
        assert "解析失败" in entry["issues"][0], entry

    def test_pipeline_filter_narrows_and_unknown_name_404s(self, served):
        base, _orch = served
        code, body = self._locks(base, "?pipeline=three_pass")
        assert code == 200 and [e["pipeline"] for e in body["pipelines"]] == ["three_pass"], body
        code, body = self._locks(base, "?pipeline=no-such-pipeline")
        assert code == 404, body

    def _tmp_pipeline_dir(self, tmp_path, monkeypatch, name="cachecheck"):
        """巡检的两条缓存判据都跑在临时目录的真文件上。

        不拿 `pipelines/*.lock` 做试验品：测试被打断（Ctrl+C / 超时 kill）时
        finally 不会执行，那会把一条随包流水线的锁留在脏状态。
        """
        d = tmp_path / f"cachedir_{name}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.yaml").write_text(PLAIN_YAML, encoding="utf-8")
        import loom_core.pack_manifest as pm
        monkeypatch.setattr(pm, "pipeline_dirs", lambda: [d])
        return d, name

    def test_repeat_poll_is_served_from_the_file_stamps(self, served, tmp_path,
                                                        monkeypatch):
        """轮询不重复付解析钱——但复用绑在文件时间戳上，不是绑在 TTL 上。

        断言的是语义计数（`cached`），不是墙钟：拿耗时当判据在负载轮里一定会假红。
        """
        base, _orch = served
        d, name = self._tmp_pipeline_dir(tmp_path, monkeypatch)
        _code, first = self._locks(base)
        assert first["cached"] == 0, "冷启动第一遍就该全量解析，报 cached>0 是假复用"
        _code, second = self._locks(base)
        assert second["count"] == 1 and second["cached"] == 1, second
        # 文件动一个字节 ⇒ 那一条必须重算（缓存不能把"我改了锁"吞掉）
        (d / f"{name}.lock").write_text(json.dumps({"pipeline": name,
                                                   "topology_hash": "0" * 12,
                                                   "agents": {}}), encoding="utf-8")
        _code, third = self._locks(base)
        assert third["cached"] == 0, "新建了锁文件还命中旧缓存 = 巡检在读旧事实"
        entry = third["pipelines"][0]
        assert entry["lock_present"] is True and entry["matches"] is False, entry

    def test_explicit_single_ask_never_reads_the_cache(self, served, tmp_path,
                                                       monkeypatch):
        """`?pipeline=` 的语义是"我刚改了，现在对不对"——读缓存就是另一个答案了。"""
        base, _orch = served
        d, name = self._tmp_pipeline_dir(tmp_path, monkeypatch, "singlecheck")
        _code, primed = self._locks(base)          # 先灌满缓存（此刻还没有锁）
        assert primed["cached"] == 0 and primed["pipelines"][0]["lock_present"] is False
        # 文件没动就直接问那一条：显式单条**不读缓存**，所以 `cached` 必须是 0。
        # 上一轮 A10（把这条守卫改成恒读）红 0 条，就是因为判据先改了文件——
        # 时间戳一变，读不读缓存都是同一答案，那条判据对"读缓存"没有射程。
        _code, untouched = self._locks(base, f"?pipeline={name}")
        assert untouched["count"] == 1 and untouched["cached"] == 0, untouched
        (d / f"{name}.lock").write_text(json.dumps({"pipeline": name,
                                                   "topology_hash": "a" * 12,
                                                   "agents": {}}), encoding="utf-8")
        _code, single = self._locks(base, f"?pipeline={name}")
        entry = single["pipelines"][0]
        assert single["cached"] == 0, "显式单条问了还报缓存命中"
        assert entry["lock_present"] is True and entry["matches"] is False, entry
        assert entry["issues"], "改了锁要给出差在哪，不只是给个 false"

# ─── 3. 解析规则只有一处（执行侧与巡检侧共用）─────────────

class TestLockResolutionParity:
    """巡检报的漂移必须就是执行会拦的漂移——两套解析迟早分叉。"""

    def _dir(self, tmp_path):
        d = tmp_path / "parity"
        d.mkdir(parents=True, exist_ok=True)
        (d / "p.yaml").write_text(PLAIN_YAML, encoding="utf-8")
        return d

    def _sched(self, d):
        return Scheduler(agents_dir=str(PROJECT / "agents"), pipeline_dir=str(d))

    def test_matching_lock_is_clean_for_both_paths(self, tmp_path):
        d = self._dir(tmp_path)
        sched = self._sched(d)
        plan = sched.parse_file(str(d / "p.yaml"), verify_lock=False)
        sched.generate_lockfile(plan, output_dir=str(d))
        assert sched.lock_drift(plan, source_dir=d)["issues"] == []
        assert sched.parse_file(str(d / "p.yaml"), verify_lock=True).pipeline_name == "p"

    def test_drift_raises_on_execution_and_lists_on_survey(self, tmp_path):
        d = self._dir(tmp_path)
        sched = self._sched(d)
        plan = sched.parse_file(str(d / "p.yaml"), verify_lock=False)
        (d / "p.lock").write_text(json.dumps(
            {"pipeline": "p", "topology_hash": "f" * 12,
             "agents": {"tldr": {"version": "0.1", "config_hash": "1" * 12}}}),
            encoding="utf-8")
        drift = sched.lock_drift(plan, source_dir=d)
        assert drift["lock_present"] is True and drift["issues"], drift
        with pytest.raises(LockfileMismatchError):
            sched.parse_file(str(d / "p.yaml"), verify_lock=True)

    def test_resolve_prefers_the_directory_beside_the_yaml(self, tmp_path):
        """pack 流水线的锁在 pack 自己目录里；集中式 pipelines/ 只是兜底。"""
        pack = self._dir(tmp_path)
        central = tmp_path / "central"
        central.mkdir()
        (pack / "p.lock").write_text(json.dumps({"pipeline": "p"}), encoding="utf-8")
        sched = self._sched(central)
        assert sched.resolve_lock_path("p", source_dir=pack) == pack / "p.lock"
        assert sched.resolve_lock_path("p") == central / "p.lock"


# ─── 4. 规范 ↔ handler：文档里的口要真能走到 ────────────────

class TestSpecParity:
    """`test_doc_consistency` 只查"文档里的路径在 openapi 规范里"，规范和 handler
    之间没人守——本仓栽过一次的形状（规范写了、路由没接，读的人以为有口）。"""

    #: needle 是**分发表达式**而不是 handler 名——上一轮 W1（把路由摘成
    #: `/api/deliveries_disabled`）时 handler 名和 `"/api/deliveries/"` 子串都还在，
    #: 这条判据没红，等于它对"路由被摘"零射程。规范里写了、路由不接，正是本仓
    #: 栽过的那个形状。
    ROUTES = {"/api/deliveries": '== "/api/deliveries"',
              "/api/deliveries/{run_id}": 'startswith("/api/deliveries/")',
              "/api/deliveries/{run_id}/download": 'endswith("/download")',
              "/api/locks": '== "/api/locks"'}

    def test_each_spec_path_is_both_routed_and_documented(self):
        from loom_core.openapi_spec import generate_spec
        spec_paths = generate_spec()["paths"]
        source = (PROJECT / "loom_core" / "admin_api.py").read_text(encoding="utf-8")
        from loom_core.admin_api import AdminHandler
        for path, needle in self.ROUTES.items():
            assert path in spec_paths, f"规范里没有 {path}"
            assert needle in source, f"{path} 只在规范里，admin_api 没有对应路由/handler"
        assert callable(AdminHandler._handle_deliveries)
        assert callable(AdminHandler._handle_delivery_detail)
        assert callable(AdminHandler._handle_delivery_download)
        assert callable(AdminHandler._handle_locks)

    def test_new_verbs_are_listed_in_the_openapi_get_surface(self):
        """计数型断言比"存在性"更容易被后来的删改抓到。"""
        from loom_core.openapi_spec import generate_spec
        paths = generate_spec()["paths"]
        assert paths["/api/deliveries"]["get"]["summary"]
        assert paths["/api/deliveries/{run_id}"]["get"]["parameters"][0]["name"] == "run_id"
        dl = paths["/api/deliveries/{run_id}/download"]["get"]
        assert dl["parameters"][0]["name"] == "run_id"
        assert set(dl["responses"]) == {"200", "404", "503"}
        assert paths["/api/locks"]["get"]["parameters"][0]["name"] == "pipeline"
