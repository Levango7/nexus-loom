"""GET /api/schedules —— cron 触发租约的只读观测面（Workflows & Schedules 屏）。

被测契约（真 HTTP、随机端口真 server）：
- 返回 schedules 全列表：name / pipeline / cron / enabled / last_fire / next_fire /
  lease_owner / lease_until（来自 RunStore.list_schedules，B4 租约落地的表）；
- `lease_state` 三态由**服务端**算好：held（有主且未到期）/ stale（有主已到期）/
  free（无主）——"是否在期内"是时点判断，口径不放前端；
- run_store 缺席（orchestrator 没挂 store）→ 空表，不编数据；
- 只读：本端点不出现在任何写路径。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import AdminAPI, AdminHandler


@pytest.fixture
def orch_mock():
    o = MagicMock()
    o.list_tasks.return_value = []
    o.registry.list.return_value = []
    o.bus.health.return_value = {"status": "ok", "subscribers": 0}
    return o


@pytest.fixture(autouse=True)
def reset_handler_state():
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None
    AdminHandler.tokens = MagicMock(enabled=False)


def _start(orch):
    api = AdminAPI(host="127.0.0.1", port=0, api_key="")
    assert api.start(orch) is True
    return api._server.server_address[1], api


def _get(port, path):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def list_schedules(self):
        return self._rows


ROW = {"name": "nightly", "pipeline": "demo", "cron": "0 2 * * *",
       "enabled": True, "next_fire": 1793000000.0, "last_fire": 1792900000.0,
       "lease_owner": "", "lease_until": 0}


class TestSchedulesEndpoint:
    def test_lists_schedules_with_computed_lease_state(self, orch_mock):
        store = _FakeStore([
            ROW,
            dict(ROW, name="hourly", lease_owner="worker-1", lease_until=9999999999.0),
        ])
        orch_mock.run_store.return_value = store
        port, api = _start(orch_mock)
        try:
            status, data = _get(port, "/api/schedules")
            assert status == 200
            assert data["count"] == 2
            by_name = {s["name"]: s for s in data["schedules"]}
            assert by_name["nightly"]["lease_state"] == "free"      # 无主 = 空闲
            assert by_name["hourly"]["lease_state"] == "held"       # 有主且未到期
            assert by_name["nightly"]["enabled"] is True
            assert by_name["nightly"]["cron"] == "0 2 * * *"
            assert by_name["nightly"]["pipeline"] == "demo"
        finally:
            api.stop()

    def test_expired_lease_is_stale(self, orch_mock):
        """有主但租约已过点：如实报 stale——这是排障时最想看到的一态。"""
        store = _FakeStore([dict(ROW, lease_owner="dead-worker", lease_until=100.0)])
        orch_mock.run_store.return_value = store
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/api/schedules")
            assert data["schedules"][0]["lease_state"] == "stale"
            assert data["schedules"][0]["lease_owner"] == "dead-worker"
        finally:
            api.stop()

    def test_no_store_yields_empty_list(self, orch_mock):
        """orchestrator 没挂 store：空表，不编数据。"""
        orch_mock.run_store.return_value = None
        port, api = _start(orch_mock)
        try:
            status, data = _get(port, "/api/schedules")
            assert status == 200
            assert data == {"schedules": [], "count": 0}
        finally:
            api.stop()

    def test_disabled_schedule_keeps_flag(self, orch_mock):
        store = _FakeStore([dict(ROW, enabled=False)])
        orch_mock.run_store.return_value = store
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/api/schedules")
            assert data["schedules"][0]["enabled"] is False
        finally:
            api.stop()
