# Production Deployment Guide

This document covers production deployment: API Key configuration, run modes, monitoring, and backup strategy.
For local quick start, see [README.md](../README.md).

---

## 1. Environment Requirements

| Item | Requirement |
|------|-------------|
| Python | 3.11+ |
| Memory | ≥ 512MB (1GB recommended for concurrent fetching) |
| Disk | ≥ 1GB (checkpoints / logs / backups grow continuously) |
| Network | Access to search engine APIs / target sites for fetching |

---

## 2. API Key Configuration

Copy `.env.example` to `.env` and fill in required keys (`.env` is gitignored, **do not commit**).

### 2.1 Search Engine Keys (by recommended priority)

| Environment Variable | Engine | Description | Get URL |
|----------------------|--------|-------------|---------|
| `BOCHA_API_KEY` | Bocha | Domestic-first, structured return | bochaai.com |
| `TAVILY_API_KEY` | Tavily | AI Agent-dedicated search | tavily.com |
| `SERPER_API_KEY` | Serper | Google search proxy | serper.dev |
| `METASO_API_KEY` | Metaso | Metaso structured search | metaso.cn |

**Key-free fallback engines**: Bing / Baidu / Sogou / 360 / DuckDuckGo (HTML scraping, lower stability but zero cost).

> ⚠️ **Degradation behavior**: When no API Key is configured, the system automatically uses HTML fallback
> engines (Bing/Baidu/Sogou/360, lower stability but zero cost). Production environments are recommended
> to use `config.production.json` to enable Bocha/Tavily/Serper structured API engines. Startup logs
> output the actually active engine list — please verify.

### 2.2 LLM Keys (for Writer polish / quality enhancement)

Configure `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` etc. in `.env`; `llm_router.py`'s provider table defines
**16 providers**. Whether a provider is active depends on all three of its `*_API_URL` / `*_MODEL` / `*_API_KEY`
being configured (only Key without URL/MODEL = not configured). When not configured, automatically degrades
to rule mode (skeleton + TF-IDF matching, no LLM polish), pipeline still completes.

> **Honest boundary**: These 16 are a **pricing and endpoint definition table**, not 16 protocol implementations.
> Routing has a single request path — OpenAI-compatible `{choices[0].message.content}` parsing
> (plus one Cloudflare URL-form branch). Therefore the table's URL must be that provider's OpenAI-compatible
> endpoint; native forms like `/api/generate` fed into this parsing path yield no content, manifesting as
> "filled Key but still failing".
> After configuration, run `python run.py --check` — it truthfully lists actually enabled providers.

### 2.3 Admin API Authentication

```bash
export ADMIN_API_KEY="your-strong-secret"
```

When set, all API endpoints require `Authorization: Bearer <key>` (`/health` and static resources are
auth-free). **Must be set when exposing to public networks in production.**

---

## 3. Run Modes

### 3.1 Single Task (CLI)

```bash
python run.py input.md -c config.production.json -o output/doc.md
```

### 3.2 Daemon Service (Admin API + Dashboard)

```bash
python run.py --daemon --admin --dashboard -c config.production.json
# API and dashboard: http://<host>:8910
```

External systems submit tasks via `POST /api/tasks` (sync/async supported). Note `/tasks` only accepts
GET, it is not a submit entry. See README admin API table and `GET /api/openapi.json` live spec.

### 3.3 MCP Server (for Claude Desktop etc. external Agent scheduling)

```bash
python run.py --mcp
```

### 3.4 Docker

> **Dependency source (since 2026-10-07)**: Data foundation `artesian` goes through
> `artesian @ git+https://github.com/Levango7/artesian.git@<sha>` in requirements.txt
> for direct-reference installation, so image build (line 20 `pip install -r requirements.txt`)
> automatically includes it. This dependency is **import-time** (`loom_core/__init__.py` → `llm_router` →
> `artesian.fast_json`); if not fully installed, the container crashes immediately at startup, not
> delayed until some feature discovers it.

```bash
docker build -t nexusloom .
docker run -d --name nexusloom \
  -p 8910:8910 \
  -v $(pwd)/.env:/app/.env:ro \
  -v nexusloom-checkpoints:/app/checkpoints \
  -v nexusloom-logs:/app/logs \
  --env-file .env \
  nexusloom input.md -c config.production.json -o output/doc.md
```

Image features: two-stage build, non-root user (uid 1001), built-in `/health` check, checkpoints and
logs declared as persistent volumes.

---

## 4. Monitoring & Observability

| Capability | Entry | Description |
|------------|-------|-------------|
| Health check | `GET /health` | Bus + Registry state, for LB / k8s liveness probe (Docker built-in) |
| Metrics | `GET /metrics` | Prometheus format, direct to Prometheus → Grafana |
| Logs | `logs/loom_*.jsonl` | Structured JSONL, auto-rotated; `GET /api/logs` online filtered query |
| Alerts | `GET /api/alerts` | Circuit-breaker / dead-letter / rate-limit / budget-exceeded events |
| Cost | `GET /api/cost` | LLM call cost by provider / Agent / time dimension |
| Budget circuit-breaker | `POST /api/cost/budget` | Set cap, auto circuit-breaks LLM calls when exceeded |

Recommended Grafana panel dimensions: task success rate, per-Agent P95 latency, LLM cost/day, circuit-breaker state.

---

## 5. Resilience & Recovery

- **Breakpoint resumption**: After task interruption, `python run.py input.md --resume --task-id <id>`
  restores from latest checkpoint; add `--recover` at service startup to auto-recover interrupted tasks
  in SQLite task queue.
- **Circuit-breaker / Rate-limit**: Built-in CLOSED/OPEN/HALF_OPEN circuit breaker and token-bucket rate
  limiter, no external components needed.
- **Dead-letter queue**: `GET /dlq` view failed messages, `POST /dlq/<id>/replay` replay.

---

## 6. Backup Strategy

| Data | Location | Recommendation |
|------|----------|----------------|
| Checkpoints & reports | `checkpoints/` | Daily incremental backup, retain 30 days |
| Document versions | `versions/` | With diff and rollback, recommend same-level backup as codebase |
| Bus / Task database | `bus_data/*.db` | SQLite (WAL mode). **Must stop instance before backup**, or first run `PRAGMA wal_checkpoint(TRUNCATE)`, and copy `*.db-wal` / `*.db-shm` together — copying while instance is writing yields half-state, and missing WAL-side files makes the copy report corruption on open. Runtime location can be redirected via `LOOM_STATE_DIR` (default anchored at checkout dir) |
| Output documents | `output/` + `backups/` | SafeWriter atomic write + auto backup (default 7 days / 20 copies, adjustable in config) |

Adjustable in `config.production.json`: `safe_writer.backup_ttl_days`, `safe_writer.max_backups`.

---

## 7. Pre-Launch Checklist

- [ ] `.env` configured with required search / LLM keys, and not committed to git
- [ ] `ADMIN_API_KEY` set (when externally exposed)
- [ ] Started with `config.production.json` (`fail_fast: false`, multi-engine)
- [ ] Startup logs confirm active search engine list
- [ ] `GET /health` returns healthy, `GET /metrics` is scrapeable
- [ ] Backup strategy covers checkpoints / versions / bus_data
