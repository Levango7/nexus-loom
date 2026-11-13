# Admin REST API 参考

启动方式：`python run.py <input> --admin`（或 `--dashboard`，隐含 `--admin`）。
默认监听 `http://127.0.0.1:8910`。在线规范：`GET /api/openapi.json`
（定义见 `loom_core/openapi_spec.py`，与本文同步）。

## 鉴权

- 设置环境变量 `ADMIN_API_KEY` 后启用；请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`
  （仅 SSE 场景才建议 query token——`EventSource` 无法自定义请求头；常规请求请使用 `Authorization` 头，
  避免凭证落入访问日志）
- **未配置 `ADMIN_API_KEY` 时进入本机信任模式**：绑定回环地址（`127.0.0.1` 等）时免鉴权访问；
  绑定非回环地址（如 `0.0.0.0`）则**拒绝启动**（安全门，防止无凭证暴露公网）
- `/health` 与静态资源（Dashboard）始终免鉴权
- Dashboard 前端在收到 401 时会弹出 Token 输入框，保存于浏览器 sessionStorage（会话级，关闭标签页即失效）后自动重试

## 危险操作二次确认（X-Confirm）

以下四类危险操作除鉴权外还必须携带请求头 `X-Confirm: yes`，缺失返回
**428 Precondition Required**（body：`{"error": "missing X-Confirm header"}`）；
确认放行时会输出结构化审计日志（时间/key 身份/操作/参数摘要）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config` | 更新运行时配置 |
| POST | `/api/cache/clear` | 清空所有缓存 |
| POST | `/api/versions/rollback` | 回滚文件到指定版本 |
| POST | `/dlq/{dlq_id}/replay` | 重放死信消息（GET 形式同样要求） |

示例：

```bash
curl -X POST http://127.0.0.1:8910/api/cache/clear \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "X-Confirm: yes"
```

## 端点总览

### 系统

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/health` | 总线 + Registry 健康状态 | 免 |
| GET | `/api/health/deep` | 深度健康检查（LLM/搜索/缓存） | 是 |
| GET | `/metrics` | Prometheus 格式指标 | 是 |
| GET | `/api/dashboard` | Dashboard 聚合数据 | 是 |

### 任务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks` | 任务列表 |
| POST | `/api/tasks` | 提交新任务（外部调度入口，支持同步/异步） |
| GET | `/tasks/{task_id}` | 单任务详情（含 result / output_content / output_path / artifacts——产物清单由 `RunHandle` 产出，任务被内存历史淘汰后改从 `deliveries.db` 账本重建） |
| POST | `/tasks/{task_id}/cancel` | 取消任务 |
| POST | `/tasks/{task_id}/rerun` | 重跑流水线（复用 last plan） |
| POST | `/tasks/{task_id}/pause` | 暂停任务（断点保留） |
| POST | `/tasks/{task_id}/resume` | 恢复任务（审批等待中的任务会被拒——只认 approve/reject） |
| POST | `/tasks/{task_id}/approve` | 审批放行（审批闸门等待中的任务；body 可选 `{"note": "..."}`） |
| POST | `/tasks/{task_id}/reject` | 审批驳回=终止（body 可选 `{"reason": "..."}`，进任务 error 与 `audit/approvals.jsonl`） |

提交任务请求体（schema `TaskSubmit`）：

```json
{
  "query": "文档主题/查询（必填）",
  "title": "可选标题",
  "pipeline": "docgen",
  "wait": false
}
```

任务详情响应核心字段（schema `TaskInfo`）：`id`、`status`
（`pending/running/done/failed/cancelled`）、`pipeline`、`result`、
`output_content`、`error`。

### Agent 与配置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agents` | 已注册 Agent 列表 |
| GET | `/api/agents/{name}` | Agent 详情 |
| GET | `/api/pipeline` | 流水线信息（已注册 Agent、pipelines/ 下可用的 YAML 清单、版本号） |
| GET | `/api/config` | 配置快照 |
| POST | `/api/config` | 更新单项配置（`{"key": ..., "value": ...}`），需 `X-Confirm: yes` |
| POST | `/api/config/reload` | 热重载并通知所有 Agent 的 `on_config_update` |
| GET | `/api/cache` | 缓存统计 |
| POST | `/api/cache/clear` | 清空缓存，需 `X-Confirm: yes` |

### 成本 / 告警 / 质量 / 日志

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/cost` | LLM 成本汇总（按供应商/Agent/时间维度） |
| POST | `/api/cost/budget` | 设置预算上限（超限熔断 LLM 调用），体：`{"max_cost": 数值}` |
| GET | `/api/alerts` | 告警历史，query 参数：`level`、`category`、`limit` |
| GET | `/api/quality/feedback` | 质量评分历史 + 弱项模式改进建议 |
| GET | `/api/logs` | 结构化日志查询，query 参数：`level`、`agent`、`since`（最近 N 秒）、`limit` |

### 事件钩子

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/events/hooks` | 已注册钩子列表 |
| POST | `/api/events/hooks` | 注册 webhook，体：`{"event": "...", "url": "..."}` |
| DELETE | `/api/events/hooks/{hook_id}` | 注销钩子 |

可用事件与行为见 README「事件钩子」章节。

### 死信队列 / 流式 / 版本管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dlq` | 死信列表 |
| POST | `/dlq/{dlq_id}/replay` | 重放死信消息，需 `X-Confirm: yes`（GET 形式同样要求） |
| GET | `/stream/metrics` | 流式推送指标快照（连接数/事件数等） |
| GET | `/stream` | SSE 流式生成，query 参数：`query`（必填）、`title`、`task_id`；
支持 `Last-Event-ID` 断线重连。返回 `text/event-stream`（需鉴权） |
| GET | `/api/versions?file=<path>` | 文件版本历史 |
| GET | `/api/versions/diff?file=<path>&v1=N&v2=M` | 对比两个版本 |
| GET | `/api/versions/stats` | 版本管理统计 |
| POST | `/api/versions/rollback` | 回滚到指定版本，JSON 体：`{"file": <path>, "version": <int>}`，需 `X-Confirm: yes` |

注：`cancel/pause/resume/rerun` 类操作对不存在的任务返回 **404**；任务存在但状态不符时
仍为 200 + `<action>: false`。`approve/reject` 例外：任务存在但**不在审批等待中**时返回
**409** + `{"error": "..."}`——审批是决策动作，"没等在闸门上的批准"必须被拒而不是记 false
（否则审计里会出现无中生有的决策）。

### 交付账本 / 锁巡检（只读）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/deliveries` | 最近交付，`?limit=`（默认 50，夹到 200）；响应 `deliveries[]` + `count` / `truncated` / `scope`；逐行带 `output_sha256` / `node_count` / `artifact_count` |
| GET | `/api/deliveries/{run_id}` | 单条交付的完整载荷：`nodes[]`（`node` / `agent` / `status` / `attempts` / `duration_sec` / `error` / `artifact_keys` / `artifacts`）+ `plan_id` / `input_file` / `output_sha256` / `output_bytes` / `topology_hash` / `config_hashes` |
| GET | `/api/deliveries/{run_id}/download` | 交付物文件本体（`Content-Disposition: attachment`，文件名 RFC 5987 双形式）；可见性与 detail 同口——越权 / 不存在 / 无落盘产物 / 文件已不在都是 404，账本读不动 503 |
| GET | `/api/locks` | 逐流水线锁巡检，`?pipeline=<name>` 只看一条；响应 `pipelines[]` + `drifted[]` + `scope` |

每行交付都带 `projection`（`complete` / `incomplete` / `legacy` / `unknown`）——它是
`GET /api/deliveries` 与补账命令之间唯一的接头：看见 `incomplete` 才去
`POST` 不了、只能走 CLI 的 `python run.py --replay-run <run_id>`。**能不能补由补账工具判**，
HTTP 侧不复制那条规则（两处判据一定会分叉）。

两条口径必须随端点一起写，否则响应会被读成别的含义：

- `GET /api/locks` 的 `scope` 恒为 `"current files"`：比的是**当前 YAML 与当前锁**，
  不是"某次运行时用的锁"。运行时的 `config_hash` / `topology_hash` 从未落库，那件事今天
  无法证据化，这里不拿当前值冒充当时。`matches` 是三值——`true` 比过且一致、`false` 漂移
  （`issues` 逐条给差在哪，含 `lock=` / `当前=` 两个哈希）、`null` 没有锁可比或 YAML 解析失败。
  `lock_present: false` 的行**不进** `drifted`：缺锁不是漂移。
- **追溯字段三种形状别合并读**：`output_sha256: ""` = 没有落盘产物，或这一跑写在
  指纹/摘要落地之前（读侧显示"未记录"，不冒充已核对）；`nodes[].artifacts.<键>`
  是**写入时**算的内容摘要（`sha256` / `bytes` / `kind`，文本按 UTF-8 原文、结构按
  `sort_keys` 规范 JSON），老记录没有这一项就是没有；`topology_hash` 与
  `config_hashes` 是 A5 的逐运行锁指纹，**事后重锁不会回头改它**——"某次运行时
  用的锁对不对"只能靠这一份回答，不能在 `/api/locks` 现场重算。
- 交付账本表里没有归属列，所以非 admin 的可见性逐行反查投影的 `principal`：CLI 直跑、
  投影里查无此运行的行 `projection` 报 `unknown` 且只有 admin 可见。越权与不存在给**同一个
  404**（存在性本身就是信息）；账本读不动给 **503** `{"error": "deliveries unavailable"}`，
  不降级成 200 空列表——把"读不到"报成"没有交付"是最难被发现的那类假绿。

### 类型化输入：提交与声明（2026-10-26，#7）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/workflows/{name}/runs` | 通用提交口：body `{"inputs": {...}, "wait": bool, "output": str}`，按该流水线 `pipeline.inputs` 声明校验后开一次运行 |
| GET | `/api/workflows/{name}/inputs` | 读回声明（`{"pipeline", "declared", "inputs"}`），客户端据此生成表单；没声明时 `declared: false` 而不是报错 |
| POST | `/api/tasks` | 除 `query` 外可选 `inputs`：给了 `inputs` 就走同一道闸门 |

`{name}` 先过与 `task_id` 同一套字符白名单，再交给 `resolve_pipeline_name` 的已安装名单——
白名单只有一处，URL 段拼不出目录遍历，也不会出现两份名单各漂各的。

校验不通过时是 **400** + 一份可定位的错误表（与 MCP/webhook 同一形状）：

```json
{"error": "输入校验未通过（2 项）:", "code": "invalid_inputs",
 "fields": [{"field": "topic", "reason": "缺少必填输入"},
            {"field": "tags[1]", "reason": "期望 str，实际 int=7"}]}
```

`field` 用点号与下标路径定位（`spec.region`、`tags[1]`）；顶层为空串表示载荷整体不是对象。
**四条口共用这一处实现**（`loom_core/input_schema.py`），所以"同一个载荷在 CLI 与 HTTP
得到同一份输入文档"这条既有承诺在校验加入后仍然成立——它是被测试钉住的，不是约定。

两条边界：

- 自由文本入口（`/api/tasks` 的 `query`）在流水线**声明了** `inputs` 时返回同样的 400，
  错误里指出该用哪个口；没声明时行为与文档形状一字未变。
- `output` 字段沿用既有行为：**只做白名单校验，不参与落盘**（把它接成真写文件是另一件事）。

---

实现位置：全部端点（含 `/api/*` 扩展）在 `loom_core/admin_api.py`（`AdminHandler` 路由分发）。
