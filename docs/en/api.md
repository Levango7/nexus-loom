# Admin REST API Reference

Start: `python run.py <input> --admin` (or `--dashboard`, which implies `--admin`).
Default listen: `http://127.0.0.1:8910`. Live spec: `GET /api/openapi.json`
(defined in `loom_core/openapi_spec.py`, synchronized with this document).

## Authentication

- Enable by setting environment variable `ADMIN_API_KEY`; requests carry `Authorization: Bearer <key>` or `?token=<key>`
  (query token only recommended for SSE — `EventSource` cannot set custom headers; use the `Authorization` header for regular requests,
  to avoid credentials appearing in access logs)
- **Without `ADMIN_API_KEY`, localhost trust mode**: bound to loopback (`127.0.0.1` etc.) means no auth;
  bound to non-loopback (e.g. `0.0.0.0`) means **startup refused** (safety gate preventing credential-less public exposure)
- `/health` and static resources (Dashboard) are always auth-free
- Dashboard frontend on receiving 401 shows a Token input box, stores in browser sessionStorage (session-level, cleared on tab close), then auto-retries

## Dangerous Operation Double-Confirmation (X-Confirm)

The following four dangerous operations require header `X-Confirm: yes` in addition to auth;
missing returns **428 Precondition Required** (body: `{"error": "missing X-Confirm header"}`);
confirmed requests output structured audit log (timestamp / key identity / operation / parameter summary):

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/config` | Update runtime config |
| POST | `/api/cache/clear` | Clear all cache |
| POST | `/api/versions/rollback` | Rollback file to specified version |
| POST | `/dlq/{dlq_id}/replay` | Replay dead-letter message (GET form also requires) |

Example:

```bash
curl -X POST http://127.0.0.1:8910/api/cache/clear \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "X-Confirm: yes"
```

## Endpoint Overview

### System

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/health` | Bus + Registry health | None |
| GET | `/api/health/deep` | Deep health check (LLM/search/cache) | Yes |
| GET | `/metrics` | Prometheus format metrics | Yes |
| GET | `/api/dashboard` | Dashboard aggregated data | Yes |

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tasks` | Task list |
| POST | `/api/tasks` | Submit new task (external scheduling entry, sync/async) |
| GET | `/tasks/{task_id}` | Single task detail (result / output_content / output_path / artifacts — artifact list from `RunHandle`, rebuilt from `deliveries.db` ledger after task memory-history eviction) |
| POST | `/tasks/{task_id}/cancel` | Cancel task |
| POST | `/tasks/{task_id}/rerun` | Rerun pipeline (reuse last plan) |
| POST | `/tasks/{task_id}/pause` | Pause task (checkpoint retained) |
| POST | `/tasks/{task_id}/resume` | Resume task (tasks awaiting approval are rejected — only approve/reject) |
| POST | `/tasks/{task_id}/approve` | Approve (approval-gate waiting task; optional body `{"note": "..."}`) |
| POST | `/tasks/{task_id}/reject` | Reject = terminate (optional body `{"reason": "..."}`, goes to task error and `audit/approvals.jsonl`) |

Submit request body (schema `TaskSubmit`):

```json
{
  "query": "document topic/query (required)",
  "title": "optional title",
  "pipeline": "docgen",
  "wait": false
}
```

Task detail response core fields (schema `TaskInfo`): `id`, `status`
(`pending/running/done/failed/cancelled`), `pipeline`, `result`,
`output_content`, `error`.

### Agents & Config

| Method | Path | Description |
|--------|------|-------------|
| GET | `/agents` | Registered Agent list |
| GET | `/api/agents/{name}` | Agent detail |
| GET | `/api/pipeline` | Pipeline info (registered Agents, available YAMLs under pipelines/, version) |
| GET | `/api/config` | Config snapshot |
| POST | `/api/config` | Update single config item (`{"key": ..., "value": ...}`), needs `X-Confirm: yes` |
| POST | `/api/config/reload` | Hot-reload and notify all Agents' `on_config_update` |
| GET | `/api/cache` | Cache statistics |
| POST | `/api/cache/clear` | Clear cache, needs `X-Confirm: yes` |

### Cost / Alerts / Quality / Logs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cost` | LLM cost summary (by provider/Agent/time) |
| POST | `/api/cost/budget` | Set budget cap (over-limit circuit-breaks LLM calls), body: `{"max_cost": number}` |
| GET | `/api/alerts` | Alert history, query params: `level`, `category`, `limit` |
| GET | `/api/quality/feedback` | Quality score history + weak-pattern improvement suggestions |
| GET | `/api/logs` | Structured log query, query params: `level`, `agent`, `since` (last N seconds), `limit` |

### Event Hooks

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events/hooks` | Registered hook list |
| POST | `/api/events/hooks` | Register webhook, body: `{"event": "...", "url": "..."}` |
| DELETE | `/api/events/hooks/{hook_id}` | Unregister hook |

Available events and behaviors: see README "Event Hooks" section.

### Dead-Letter Queue / Streaming / Version Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dlq` | Dead-letter list |
| POST | `/dlq/{dlq_id}/replay` | Replay dead-letter message, needs `X-Confirm: yes` (GET form also requires) |
| GET | `/stream/metrics` | Streaming metrics snapshot (connections/events etc.) |
| GET | `/stream` | SSE streaming generation, query params: `query` (required), `title`, `task_id`;
supports `Last-Event-ID` reconnect. Returns `text/event-stream` (auth required) |
| GET | `/api/versions?file=<path>` | File version history |
| GET | `/api/versions/diff?file=<path>&v1=N&v2=M` | Compare two versions |
| GET | `/api/versions/stats` | Version management statistics |
| POST | `/api/versions/rollback` | Rollback to version, JSON body: `{"file": <path>, "version": <int>}`, needs `X-Confirm: yes` |

Note: `cancel/pause/resume/rerun` on non-existent tasks return **404**; task exists but wrong state returns
200 + `<action>: false`. `approve/reject` exception: task exists but **not awaiting approval** returns
**409** + `{"error": "..."}` — approval is a decision action, "approving something not waiting at the gate"
must be rejected, not logged as false (otherwise audit would contain fabricated decisions).

### Delivery Ledger / Lock Inspection (Read-Only)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/deliveries` | Recent deliveries, `?limit=` (default 50, capped at 200); response `deliveries[]` + `count` / `truncated` / `scope` |
| GET | `/api/deliveries/{run_id}` | Single delivery full payload: `nodes[]` (`node` / `agent` / `status` / `attempts` / `duration_sec` / `error` / `artifact_keys` / `artifacts`) + `plan_id` / `input_file` / `output_sha256` / `output_bytes` / `topology_hash` / `config_hashes` |
| GET | `/api/deliveries/{run_id}/download` | The delivery output file itself (`Content-Disposition: attachment`, RFC 5987 filename); same visibility as detail — cross-tenant / missing / no output file / file gone all return 404, unreadable ledger 503 |
| GET | `/api/locks` | Per-pipeline lock inspection, `?pipeline=<name>` for single; response `pipelines[]` + `drifted[]` + `scope` |

Each delivery row carries `projection` (`complete` / `incomplete` / `legacy` / `unknown`) — it is the
only interface between `GET /api/deliveries` and the backfill command: seeing `incomplete` means
`POST` won't work, must use CLI `python run.py --replay-run <run_id>`. **Whether backfill is possible
is determined by the backfill tool**, HTTP side does not duplicate that rule (two criteria will diverge).

Two calibrations must be written with the endpoint, otherwise responses will be misread:

- `GET /api/locks` `scope` is always `"current files"`: comparing **current YAML vs current lock**,
  not "the lock used at some run". Runtime `config_hash` / `topology_hash` are never persisted, so
  that question cannot be evidence-based today; current values are not used to impersonate then.
  `matches` is tri-state — `true` compared and consistent, `false` drifted
  (`issues` itemizes differences, including `lock=` / `current=` hashes), `null` no lock to compare or YAML parse failed.
  `lock_present: false` rows do **not** enter `drifted`: missing lock is not drift.
- Delivery ledger table has no ownership column, so non-admin visibility is per-row reverse-lookup of
  projection `principal`: CLI direct run, rows with no matching run in projection report `projection` as
  `unknown` and are admin-only. Unauthorized and non-existent both give **the same 404** (existence itself
  is information); ledger unreadable gives **503** `{"error": "deliveries unavailable"}`,
  not downgraded to 200 empty list — reporting "cannot read" as "no deliveries" is the hardest-to-detect
  kind of false green.

### Typed Input: Submit and Declare (2026-10-26, #7)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/workflows/{name}/runs` | Generic submit: body `{"inputs": {...}, "wait": bool, "output": str}`, validates against pipeline's `pipeline.inputs` declaration then starts a run |
| GET | `/api/workflows/{name}/inputs` | Read back declaration (`{"pipeline", "declared", "inputs"}`), client generates form; when not declared, `declared: false` not an error |
| POST | `/api/tasks` | In addition to `query`, optional `inputs`: when `inputs` given, goes through same gate |

`{name}` first passes the same character whitelist as `task_id`, then goes to `resolve_pipeline_name`'s
installed list — whitelist is single-source, URL segments cannot construct directory traversal, and
two lists cannot drift independently.

Validation failure is **400** + a locatable error table (same shape as MCP/webhook):

```json
{"error": "Input validation failed (2 items):", "code": "invalid_inputs",
 "fields": [{"field": "topic", "reason": "Missing required input"},
            {"field": "tags[1]", "reason": "Expected str, got int=7"}]}
```

`field` uses dot-and-index path (`spec.region`, `tags[1]`); top-level empty string means payload is
not an object. **Four entry points share one implementation** (`loom_core/input_schema.py`), so the
existing promise "same payload gets same input document in CLI and HTTP" still holds after validation
addition — it is pinned by tests, not convention.

Two boundaries:

- Free-text entry (`/api/tasks` `query`) when pipeline **declares** `inputs` returns same 400,
  error indicates which entry to use; when not declared, behavior unchanged.
- `output` field follows existing behavior: **whitelist validation only, does not participate in
  persistence** (wiring it to actual file write is a separate matter).

---

Implementation: all endpoints (including `/api/*` extensions) are in `loom_core/admin_api.py`
(`AdminHandler` route dispatch).
