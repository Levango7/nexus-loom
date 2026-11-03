"""OpenAPI 3.0 Spec — 机器可读 API 定义

让 AI agent 和前端自动发现接口。
访问: GET /api/openapi.json
"""
from __future__ import annotations


def _available_pipelines() -> list[str]:
    """把可用流水线列进 OpenAPI 的 enum，而不是在规范里写死一个产品名。"""
    try:
        from .scheduler import Scheduler
        return Scheduler().list_pipelines()
    except Exception:  # pragma: no cover - 规范生成不该因目录问题失败
        return []


def _schemas() -> dict:
    """components.schemas 定义"""
    return {
        "TaskSubmit": {
            "type": "object",
            # `query` 与 `inputs` 二选一：写死 required:["query"] 会对"只带类型化载荷"
            # 这条合法提交说不，所以用 anyOf 表达"至少给一个"。
            "anyOf": [{"required": ["query"]}, {"required": ["inputs"]}],
            "properties": {
                "query": {"type": "string", "description": "文档主题/查询（自由文本形）"},
                "inputs": {
                    "type": "object",
                    "description": "类型化载荷（与 query 二选一）：流水线声明了 "
                                   "pipeline.inputs 时逐字段校验，不合法返回 400 + fields；"
                                   "声明未给的流水线按「## 键」分节渲染成输入文档",
                },
                "title": {"type": "string", "description": "文档标题"},
                "pipeline": {
                    "type": "string",
                    "description": "流水线名；缺省用 config.default_pipeline，"
                                   "未配置且存在多条时接口返回 400 并列出可用值",
                    "enum": _available_pipelines(),
                },
                "wait": {"type": "boolean", "default": False},
                "output": {"type": "string", "description": "输出文件路径（白名单目录内）"},
            },
        },
        "TaskInfo": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "running", "paused", "done", "failed", "cancelled"]},
                "pipeline": {"type": "string"},
                "result": {"type": "object"},
                "output_content": {"type": "string", "nullable": True},
                "error": {"type": "string"},
                # 由 RunHandle.artifacts() 单源产出：kind=artifact 带 node+keys，
                # kind=deliverable 带 path+bytes（读不到磁盘时 bytes=-1，不省略这条）
                "artifacts": {"type": "array", "items": {"type": "object"}},
            },
        },
        "CostSummary": {
            "type": "object",
            "properties": {
                "total_cost": {"type": "number"},
                "budget": {"type": "number"},
                "budget_remaining": {"type": "number", "nullable": True},
                "budget_exceeded": {"type": "boolean"},
                "by_provider": {"type": "object"},
            },
        },
        "Error": {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
            },
        },
        "InvalidInputs": {
            "type": "object",
            "description": "类型化输入校验未通过（input_schema.InputValidationError.to_dict）",
            "properties": {
                "error": {"type": "string"},
                "code": {"type": "string", "enum": ["invalid_inputs"]},
                "fields": {
                    "type": "array",
                    "description": "每条都能定位到字段：点号路径（spec.region）与"
                                   "下标路径（tags[1]），顶层为空串表示载荷整体不是对象",
                    "items": {
                        "type": "object",
                        "required": ["field", "reason"],
                        "properties": {
                            "field": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
        "ApproveRequest": {
            "type": "object",
            "description": "审批放行请求体；整体可省略（空 body 即放行）",
            "properties": {
                "note": {"type": "string", "description": "放行备注（进审计）"},
            },
        },
        "RejectRequest": {
            "type": "object",
            "description": "审批驳回请求体；整体可省略（空 body 即驳回）",
            "properties": {
                "reason": {"type": "string", "description": "驳回原因（进任务 error 与审计）"},
            },
        },
        "ApproveResponse": {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "task_id": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "RejectResponse": {
            "type": "object",
            "properties": {
                "rejected": {"type": "boolean"},
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    }


def _system_paths() -> dict:
    """系统级端点：根索引 / 健康检查 / 指标"""
    return {
        "/": {
            "get": {
                "summary": "API 索引（纯文本端点列表，非仪表盘；仪表盘在 /index.html）",
                "responses": {
                    "200": {
                        "description": "纯文本索引",
                        "content": {"text/plain": {"schema": {"type": "string"}}},
                    },
                },
            },
        },
        "/health": {
            "get": {
                "summary": "健康检查",
                "security": [],
                "responses": {"200": {"description": "健康状态"}},
            },
        },
        "/api/health/deep": {
            "get": {
                "summary": "深度健康检查（LLM/搜索/缓存）",
                "responses": {"200": {"description": "组件健康状态"}},
            },
        },
        "/metrics": {
            "get": {
                "summary": "Prometheus 指标",
                "responses": {"200": {"description": "Prometheus 格式指标"}},
            },
        },
    }


def _task_paths() -> dict:
    """任务生命周期端点：列表 / 详情 / 提交 / 取消 / 重跑 / 暂停 / 恢复"""
    return {
        "/tasks": {
            "get": {
                "summary": "任务列表",
                "responses": {"200": {"description": "任务列表"}},
            },
        },
        "/tasks/{task_id}": {
            "get": {
                "summary": "查询单任务（含结果和输出内容）",
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "任务详情", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskInfo"}}}},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/nodes/{node_name}": {
            "get": {
                "summary": "节点检查器（S3）：artifact 结构预览 / 幂等键 / 熔断限流态 / 质量门明细；"
                           "跨进程运行从 RunStore 投影答（source=runstore，四类进程内明细为 null）",
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "node_name", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "节点明细"},
                    "404": {"description": "任务或节点不存在",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/tasks": {
            "post": {
                "summary": "提交新文档生成任务（query 自由文本 或 inputs 类型化载荷）",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskSubmit"}}},
                },
                "responses": {
                    "200": {"description": "任务已提交/完成"},
                    "400": {"description": "参数错误或输入不符合声明",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvalidInputs"}}}},
                },
            },
        },
        "/api/workflows/{name}/runs": {
            "post": {
                "summary": "通用提交口：按流水线声明校验 inputs 后开一次运行（P1 #7）",
                "parameters": [{"name": "name", "in": "path", "required": True,
                               "schema": {"type": "string",
                                          "enum": _available_pipelines()}}],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["inputs"],
                        "properties": {
                            "inputs": {"type": "object"},
                            "wait": {"type": "boolean", "default": False},
                            "output": {"type": "string"},
                        }}}},
                },
                "responses": {
                    "200": {"description": "运行已创建（wait=false 时返回 task_id 供轮询）"},
                    "400": {"description": "流水线名未知 / 输入不符合声明（fields 定位到字段）",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvalidInputs"}}}},
                },
            },
        },
        "/api/workflows/{name}/inputs": {
            "get": {
                "summary": "读回某条流水线的类型化输入声明（客户端据此生成表单；"
                           "没声明时 declared=false，退回自由文本）",
                "parameters": [{"name": "name", "in": "path", "required": True,
                               "schema": {"type": "string",
                                          "enum": _available_pipelines()}}],
                "responses": {"200": {"description": "声明的副本"},
                              "404": {"description": "流水线不存在"}},
            },
        },
        "/tasks/{task_id}/cancel": {
            "post": {
                "summary": "取消任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "取消结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/rerun": {
            "post": {
                "summary": "重跑任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "重跑已启动"},
                    "404": {"description": "原任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/pause": {
            "post": {
                "summary": "暂停任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "暂停结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/resume": {
            "post": {
                "summary": "恢复任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "恢复结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/approve": {
            "post": {
                "summary": "审批放行（只受理审批等待中的任务；不在等待中时 409，审计不出现无中生有的决策）",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApproveRequest"}}},
                },
                "responses": {
                    "200": {"description": "放行成功",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApproveResponse"}}}},
                    "400": {"description": "请求体不是合法 JSON",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                    "404": {"description": "任务不存在",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                    "409": {"description": "不在审批等待中（fail-closed）",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/reject": {
            "post": {
                "summary": "审批驳回（驳回=终止；原因进任务 error 与审计）",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RejectRequest"}}},
                },
                "responses": {
                    "200": {"description": "驳回成功",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RejectResponse"}}}},
                    "400": {"description": "请求体不是合法 JSON",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                    "404": {"description": "任务不存在",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                    "409": {"description": "不在审批等待中（fail-closed）",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
    }


def _resource_paths() -> dict:
    """资源查询端点：Agent / 配置 / 缓存"""
    return {
        "/agents": {
            "get": {
                "summary": "Agent 列表",
                "responses": {"200": {"description": "已注册 Agent"}},
            },
        },
        "/api/agents/{name}": {
            "get": {
                "summary": "Agent 详情",
                "parameters": [{"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Agent 详情"}},
            },
        },
        "/api/config": {
            "get": {
                "summary": "读取配置",
                "responses": {"200": {"description": "配置快照"}},
            },
            "post": {
                "summary": "更新配置（每次变更有审计记录）",
                "description": "变更落 <状态目录>/audit/config.jsonl：key / 新旧值 / 客户端 / "
                               "凭证指纹；敏感键名（token/secret/password/api_key）的值只记"
                               "脱敏形状。审计写盘失败时响应携带 audit_error。",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}}}}},
                "responses": {"200": {"description": "更新成功（含 audit/audit_error）"}},
            },
        },
        "/api/config/audit": {
            "get": {
                "summary": "配置变更审计（新→旧）",
                "parameters": [{"name": "limit", "in": "query", "required": False,
                                "schema": {"type": "integer", "maximum": 500}}],
                "responses": {"200": {"description": "最近若干条变更记录"}},
            },
        },
        "/api/cache": {
            "get": {
                "summary": "缓存统计",
                "responses": {"200": {"description": "缓存统计"}},
            },
        },
        "/api/cache/clear": {
            "post": {
                "summary": "清空缓存",
                "responses": {"200": {"description": "已清空"}},
            },
        },
    }


def _ops_paths() -> dict:
    """运维端点：成本 / 告警 / 质量 / 日志 / 事件钩子 / 死信 / SSE 流"""
    return {
        "/api/cost": {
            "get": {
                "summary": "LLM 成本统计",
                "responses": {"200": {"description": "成本汇总", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CostSummary"}}}}},
            },
        },
        "/api/cost/budget": {
            "post": {
                "summary": "设置预算上限",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"max_cost": {"type": "number"}}}}}},
                "responses": {"200": {"description": "预算已设置"}},
            },
        },
        "/api/alerts": {
            "get": {
                "summary": "告警列表",
                "parameters": [
                    {"name": "level", "in": "query", "schema": {"type": "string"}},
                    {"name": "category", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "告警列表"}},
            },
        },
        "/api/quality/feedback": {
            "get": {
                "summary": "质量闭环反馈统计",
                "responses": {"200": {"description": "质量评分历史和改进建议"}},
            },
        },
        "/api/logs": {
            "get": {
                "summary": "结构化日志查询",
                "parameters": [
                    {"name": "level", "in": "query", "schema": {"type": "string"}},
                    {"name": "agent", "in": "query", "schema": {"type": "string"}},
                    {"name": "since", "in": "query", "schema": {"type": "integer"}, "description": "最近 N 秒"},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "日志条目列表"}},
            },
        },
        "/api/config/reload": {
            "post": {
                "summary": "热重载配置并通知所有 Agent",
                "responses": {"200": {"description": "重载结果"}},
            },
        },
        "/api/events/hooks": {
            "get": {
                "summary": "事件钩子列表",
                "responses": {"200": {"description": "钩子列表"}},
            },
            "post": {
                "summary": "注册事件钩子",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"event": {"type": "string"}, "url": {"type": "string"}}}}}},
                "responses": {"200": {"description": "已注册"}},
            },
        },
        "/api/events/hooks/{hook_id}": {
            "delete": {
                "summary": "注销事件钩子",
                "parameters": [{"name": "hook_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "已注销"}},
            },
        },
        "/api/webhooks": {
            "get": {
                "summary": "入站 webhook 清单（不含密钥值）",
                "responses": {"200": {"description": "webhook 列表"}},
            },
        },
        "/api/webhooks/{name}": {
            "post": {
                "summary": "入站触发：按 webhooks.yaml 的条目提交流水线 run",
                "description": "自携密钥鉴权（X-Webhook-Signature 的 HMAC-SHA256 或 "
                               "X-Webhook-Token），不走 ADMIN_API_KEY；密钥未配置时 "
                               "fail-closed（503）。每次调用无论成败都落审计 "
                               "（<状态目录>/audit/webhooks.jsonl）。请求体即运行输入。",
                "parameters": [{"name": "name", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"202": {"description": "已提交（含 task_id）"},
                              "401": {"description": "凭据无效"},
                              "403": {"description": "webhook 已停用"},
                              "404": {"description": "未知 webhook"},
                              "503": {"description": "密钥未配置（fail-closed）"}},
            },
        },
        "/dlq": {
            "get": {
                "summary": "死信队列",
                "responses": {"200": {"description": "死信列表"}},
            },
        },
        "/dlq/{dlq_id}/replay": {
            "post": {
                "summary": "重放死信",
                "parameters": [{"name": "dlq_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "重放结果"}},
            },
        },
        "/api/approvals": {
            "get": {
                "summary": "审批收件箱（跨运行、跨重启；非 admin 只看自己的待办）",
                "responses": {"200": {"description": "待批列表"}},
            },
        },
        "/api/deliveries": {
            "get": {
                "summary": "交付账本最近若干条（带投影健康位；非 admin 只看投影归属自己的行）",
                "parameters": [{"name": "limit", "in": "query", "required": False,
                               "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "交付列表"},
                              "503": {"description": "账本读不动"}},
            },
        },
        "/api/deliveries/{run_id}": {
            "get": {
                "summary": "单条交付的完整结构化载荷（逐节点状态/耗时/产物键）",
                "parameters": [{"name": "run_id", "in": "path", "required": True,
                               "schema": {"type": "string"}}],
                "responses": {"200": {"description": "交付明细"},
                              "404": {"description": "账本里没有这条，或不属于你（同一个答案）"}},
            },
        },
        "/api/locks": {
            "get": {
                "summary": "锁巡检。scope=current files：比的是当前 YAML 与当前锁，"
                           "不是某次运行时用的锁（运行时的指纹没有落库）",
                "parameters": [{"name": "pipeline", "in": "query", "required": False,
                               "schema": {"type": "string"}}],
                "responses": {"200": {"description": "逐流水线的漂移问题表"}},
            },
        },
        "/api/schedules": {
            "get": {
                "summary": "cron 触发租约的观测面（只读）：逐排程的 cron/启停/"
                           "last_fire/next_fire/租约三态（held/stale/free，服务端算好）",
                "responses": {"200": {"description": "排程列表与数量"}},
            },
        },
        "/api/dashboard": {
            "get": {
                "summary": "Dashboard 聚合数据（任务含 progress/steps + Agent 摘要；任务按 principal 过滤）",
                "responses": {"200": {"description": "聚合状态"}},
            },
        },
        "/api/pipeline": {
            "get": {
                "summary": "流水线配置与版本信息",
                "responses": {"200": {"description": "流水线信息"}},
            },
        },
        "/api/versions": {
            "get": {
                "summary": "文件版本历史",
                "parameters": [{"name": "file", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "版本列表"},
                    "400": {"description": "参数/路径校验失败", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/versions/diff": {
            "get": {
                "summary": "对比两个版本",
                "parameters": [
                    {"name": "file", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "v1", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "v2", "in": "query", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {"description": "差异文本"},
                    "400": {"description": "参数错误", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/versions/stats": {
            "get": {
                "summary": "版本管理统计",
                "responses": {"200": {"description": "统计信息"}},
            },
        },
        "/api/versions/rollback": {
            "post": {
                "summary": "回滚到指定版本",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["file", "version"],
                    "properties": {"file": {"type": "string"}, "version": {"type": "integer"}},
                }}}},
                "responses": {
                    "200": {"description": "回滚结果"},
                    "404": {"description": "版本不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/stream/metrics": {
            "get": {
                "summary": "流式推送指标快照（连接数/事件数等）",
                "responses": {
                    "200": {
                        "description": "流式指标快照",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                },
            },
        },
        "/stream": {
            "get": {
                "summary": "SSE 流式文档生成",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "title", "in": "query", "schema": {"type": "string"}},
                    {"name": "task_id", "in": "query", "schema": {"type": "string"}},
                    {
                        "name": "Last-Event-ID",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "SSE 重连续点：携带最后接收的事件 id，服务端从断点续推",
                    },
                ],
                "responses": {"200": {"description": "text/event-stream"}},
            },
        },
    }


def generate_spec() -> dict:
    """生成 OpenAPI 3.0 spec"""
    try:
        from . import __version__
    except ImportError:  # pragma: no cover
        __version__ = "unknown"
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Doc-Pipeline Admin API",
            "description": "多 Agent 文档生成流水线管理 API",
            "version": __version__,
        },
        "servers": [
            {"url": "http://localhost:8910", "description": "本地开发"},
        ],
        # 全局安全要求：除显式标注 security: [] 的端点（/health）外均需 Bearer 凭证，
        # 与 AdminHandler._check_auth 实现一致
        "security": [{"BearerAuth": []}],
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            },
            "schemas": _schemas(),
        },
        "paths": {
            **_system_paths(),
            **_task_paths(),
            **_resource_paths(),
            **_ops_paths(),
        },
    }
