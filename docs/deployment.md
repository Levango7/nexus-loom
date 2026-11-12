# 生产环境部署指南

本文档面向生产部署，覆盖 API Key 配置、运行模式、监控对接与备份策略。
如只需本地体验，请参考 [README.md](../README.md) 的快速开始。

---

## 1. 环境要求

| 项 | 要求 |
|----|------|
| Python | 3.11+ |
| 内存 | ≥ 512MB（并发抓取建议 1GB） |
| 磁盘 | ≥ 1GB（checkpoints / logs / backups 持续增长） |
| 网络 | 可访问搜索引擎 API / 抓取目标站点 |

---

## 2. API Key 配置

复制 `.env.example` 为 `.env` 并填入所需 Key（`.env` 已被 gitignore，**切勿提交**）。

### 2.1 搜索引擎 Key（按推荐优先级）

| 环境变量 | 引擎 | 说明 | 获取地址 |
|----------|------|------|----------|
| `BOCHA_API_KEY` | Bocha | 国内首选，结构化返回 | bochaai.com |
| `TAVILY_API_KEY` | Tavily | AI Agent 专用搜索 | tavily.com |
| `SERPER_API_KEY` | Serper | Google 搜索代理 | serper.dev |
| `METASO_API_KEY` | Metaso | 秘塔结构化搜索 | metaso.cn |

**无需 Key 的兜底引擎**：Bing / Baidu / Sogou / 360 / DuckDuckGo（HTML 抓取，稳定性较低但零成本）。

> ⚠️ **降级行为**：未配置任何 API Key 时，系统自动使用 HTML 兜底引擎（Bing/Baidu/Sogou/360，
> 稳定性较低但零成本）。生产环境建议使用 `config.production.json` 启用 Bocha/Tavily/Serper 等
> 结构化 API 引擎。启动日志会输出实际生效的引擎列表，请留意确认。

### 2.2 LLM Key（用于 Writer 润色 / 质量增强）

在 `.env` 中配置 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等；`llm_router.py` 的供应商表里定义了
**16 家**，某家是否启用取决于它的 `*_API_URL` / `*_MODEL` / `*_API_KEY` **三项是否配齐**
（只配 Key 不配 URL/MODEL 等于没配），未配置时自动降级为规则模式
（骨架 + TF-IDF 匹配，不做 LLM 润色），不影响流水线完成。

> **诚实边界**：这 16 家是**定价与端点定义表**，不是 16 套协议实现。路由只有一条请求路径——
> OpenAI 兼容的 `{choices[0].message.content}` 解析（外加 Cloudflare 一个 URL 形态分支）。
> 因此表里的 URL 必须填该家的 OpenAI 兼容端点，`/api/generate` 这类原生形态喂进这条
> 解析路径取不到内容，表现为填了 Key 仍然失败。
> 配好之后请跑 `python run.py --check`，它会如实列出实际启用的供应商。

### 2.3 Admin API 鉴权

```bash
export ADMIN_API_KEY="your-strong-secret"
```

设置后所有 API 端点需携带 `Authorization: Bearer <key>`（`/health` 与静态资源免鉴权）。
**生产环境暴露到公网时必须设置。**

---

## 3. 运行模式

### 3.1 单次任务（CLI）

```bash
python run.py input.md -c config.production.json -o output/doc.md
```

### 3.2 常驻服务（Admin API + Dashboard）

```bash
python run.py --daemon --admin --dashboard -c config.production.json
# API 与仪表盘: http://<host>:8910
```

外部系统通过 `POST /api/tasks` 提交任务（支持同步/异步）。注意 `/tasks` 只接受 GET，
不是提交入口。详见 README 的管理 API 表格与 `GET /api/openapi.json` 在线规范。

### 3.3 MCP Server（供 Claude Desktop 等外部 Agent 调度）

```bash
python run.py --mcp
```

### 3.4 Docker

> **依赖来源（2026-10-07 起）**：取数底座 `artesian` 走 requirements.txt 里的
> `artesian @ git+https://github.com/Levango7/artesian.git@<sha>` 直接引用安装，
> 因此镜像构建（第 20 行 `pip install -r requirements.txt`）会自动带上它。
> 该依赖是**导入期**的（`loom_core/__init__.py` → `llm_router` → `artesian.fast_json`），
> 装不齐就在容器启动时立刻炸，不会拖到某个功能才被发觉。

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

镜像特性：两阶段构建、非 root 用户（uid 1001）、内置 `/health` 健康检查、checkpoints 与 logs 声明为持久化卷。

---

## 4. 监控与可观测

| 能力 | 入口 | 说明 |
|------|------|------|
| 健康检查 | `GET /health` | 总线 + Registry 状态，供 LB / k8s liveness 探针（Docker 已内置） |
| 指标 | `GET /metrics` | Prometheus 格式，直接接入 Prometheus → Grafana |
| 日志 | `logs/loom_*.jsonl` | 结构化 JSONL，自动轮转；`GET /api/logs` 支持在线过滤查询 |
| 告警 | `GET /api/alerts` | 熔断 / 死信 / 限流 / 预算超限事件 |
| 成本 | `GET /api/cost` | LLM 调用成本按供应商 / Agent / 时间维度统计 |
| 预算熔断 | `POST /api/cost/budget` | 设置上限，超限自动熔断 LLM 调用 |

推荐 Grafana 面板维度：任务成功率、各 Agent 耗时 P95、LLM 成本/日、熔断器状态。

---

## 5. 弹性与恢复

- **断点续传**：任务中断后 `python run.py input.md --resume --task-id <id>` 从最近 checkpoint 恢复；
  服务启动时加 `--recover` 自动恢复 SQLite 任务队列中的中断任务。
- **熔断 / 限流**：内置 CLOSED/OPEN/HALF_OPEN 熔断器与令牌桶限流器，无需外部组件。
- **死信队列**：`GET /dlq` 查看失败消息，`POST /dlq/<id>/replay` 重放。

---

## 6. 备份策略

| 数据 | 位置 | 建议 |
|------|------|------|
| 断点与报告 | `checkpoints/` | 每日增量备份，保留 30 天 |
| 文档版本 | `versions/` | 含 diff 和回滚能力，建议与代码库同级备份 |
| 总线 / 任务数据库 | `bus_data/*.db` | SQLite（WAL 模式）。备份前**必须先停实例**，或先执行 `PRAGMA wal_checkpoint(TRUNCATE)`，并把 `*.db-wal` / `*.db-shm` 一并拷走——实例还在写的时候拷文件会得到半状态，而 WAL 侧文件缺失会让副本打开时报损坏。运行态位置可用 `LOOM_STATE_DIR` 重定向（默认锚在检出目录） |
| 产出文档 | `output/` + `backups/` | SafeWriter 原子写入 + 自动备份（默认 7 天 / 20 份，可在 config 调整） |

`config.production.json` 中可调项：`safe_writer.backup_ttl_days`、`safe_writer.max_backups`。

### 6.1 运行态回收（`retention.*`）

观测面（运行投影 / 事件 / 审批 / 队列 / 成本 / 质量反馈 / checkpoint / 交付账本 / 死信）
默认**一个都不删**：`retention.runs_days` 与 `retention.max_runs` 都是 `0`。要开回收，
配这两个键（或环境覆写 `DOCPIPE_RETENTION__RUNS_DAYS`）：

- **worker 形态**：每 `maintenance.gc_ticks`（默认 60）个轮询跑一次，**有活没活都算**
  ——持续饱和的 worker 同样会回收（此前它只在空转时回收，那恰好漏掉了最需要回收的场景）。
- **常驻服务形态**（`--admin` / `--dashboard` / `--daemon`）：这条路径上没有 worker 循环，
  按 `maintenance.gc_seconds`（默认 600 秒）计；设 `0` 表示服务模式不跑回收（显式选择），
  写成空值（`gc_seconds:` 不给值）或写坏一律回默认 600——只有显式的 `0` 才算关掉。
- **一次性维护**：`python run.py --gc`，逐表报删了多少行。

备份时请注意：**回收只动数据库里的行，不动 `output/` 里的交付物文件**；历史行不
backfill 摘要（读侧如实显示"未记录"）。

---

## 7. 上线前检查清单

- [ ] `.env` 已配置所需搜索 / LLM Key，且未提交到 git
- [ ] `ADMIN_API_KEY` 已设置（对外暴露时）
- [ ] 使用 `config.production.json` 启动（`fail_fast: false`，多引擎）
- [ ] 启动日志确认生效的搜索引擎列表
- [ ] `GET /health` 返回健康、`GET /metrics` 可被抓取
- [ ] 备份策略已覆盖 checkpoints / versions / bus_data
