# Agent 开发指南

本文说明如何为 Loom 编写自定义 Agent。所有内容以当前代码为准：
契约定义见 `loom_core/base_agent.py`，加载与沙箱见 `loom_core/agent_loader.py`。

**新代码放哪一层**（依赖方向单向，由 `tests/test_layering.py` 当门禁）：

| 位置 | 放什么 | 约束 |
|------|--------|------|
| `agents/` | 一个可被流水线订阅的能力（Agent） | 由加载器按目录发现，不得被 `loom_core` 硬编码 import |
| `loom_doc/` | 文档领域的纯函数/类：渲染、摄入、增强 | 只能依赖 `loom_core`；Agent 薄壳调用它 |
| `loom_core/` | 换一种任务类型仍然成立的引擎原语 | 不得 import `loom_doc` / `agents`，不得出现领域名词 |

判据：如果去掉"文档"这个场景，这段代码还有用，它属于 `loom_core`；
否则它是领域层或 Agent。

---

## 1. Agent 模块契约

每个 Agent 是 `agents/` 目录下的一个 `.py` 文件（文件名 = Agent 名）。
加载器会读取模块级常量作为注册元信息：

| 常量 | 类型 | 说明 |
|------|------|------|
| `AGENT_NAME` | str | Agent 名称（须与文件名一致） |
| `AGENT_VERSION` | str | 版本号 |
| `AGENT_DESC` | str | 一句话描述 |
| `AGENT_AUTHOR` | str | 作者 |
| `AGENT_PRIORITY` | int | 订阅优先级（越小越先） |
| `INPUT_TOPICS` | list[str] | 订阅的消息主题（自动挂到 MessageBus） |
| `OUTPUT_TOPICS` | list[str] | 输出主题（文档用途） |
| `DEPENDENCIES` | list[str] | 依赖的其他 Agent |
| `CACHE_TTL` | int | 缓存 TTL 秒数 |
| `RESPAWN` | bool | 异常退出后是否自动重生 |
| `SUPPORTS_REGENERATION` / `REGENERATION_TARGET` / `REGENERATION_RECHECK` | — | 质量重做循环：声明支持就必须给出 `REGENERATION_TARGET`，引擎不再猜默认值 |
| `PRODUCES` | dict[str, str] | 对外产物与合并策略：`{"content": "last"}`（机制见 `loom_core/artifacts.py`） |
| `CONSUMES` | list[str] | 期望的上游产物名（声明意图，供校验与阅读） |
| `CONFIG_SCHEMA` | dict[str, tuple] | 配置项契约：`{"threshold": (["int", "float"], 70)}`，类型名是**字符串** |
| `LEGACY_AUTO` | bool | 默认 `True`：是否参与 `--legacy` 的自动图。legacy 按设计不读 YAML、没有 per-node config，凡是"没配置就跑不出东西"的件（出网要 `url`、转换要 `template`）都必须置 `False`，否则它被拉进每一趟 legacy 跑并必然业务失败 |
| `SANDBOX_TRUSTED` | bool | 随产品发布的内置 Agent 显式声明为 `True`，加载器据此跳过 AST 沙箱检查 |

### 1.1 产物契约（引擎怎么把上游结果给你）

引擎组装下游载荷时**只认声明**：你 `PRODUCES` 里写过的键，才会被合并进
`payload` 顶层（同时保留在 `payload["dependencies_results"][节点名]` 里原样可取）。

| 策略 | 语义 | 典型 |
|------|------|------|
| `"list"` | 跨所有上游产出者拼接（含同层池化实例） | `researcher.results`、`fetcher.articles` |
| `"last"` | 拓扑上**离你最近**的产出者胜出 | 正文 `content`（writer → layout 逐级覆盖） |
| `"first"` | 最早的产出者定格 | `requirements_analyzer.spec` |

顺序来自 DAG 层级，不来自任何写死的 Agent 名单；没声明的键不会外泄成顶层键，
`task_id` / `config` / `queries` 等引擎自有键也永不被上游覆盖。

### 1.2 配置契约怎么生效

`Scheduler` 用 **AST** 读你的 `CONFIG_SCHEMA` 字面量（不执行 Agent 代码），
在解析 pipeline 时：缺项补默认值、类型不符直接 `TypeError`。
这样配错值不会在运行时被 `config.get(key, 硬编码默认)` 悄悄吞掉。
类型名只接受 `str/int/float/bool/list/dict`，写别的在解析期即报错。

## 2. BaseAgent 必须实现的方法

继承 `loom_core.base_agent.BaseAgent` 并实现唯一的抽象方法：

```python
def handle(self, msg: Message) -> dict | None:
    """处理一条消息，返回结果 dict（写入任务输出）或 None"""
```

`BaseAgent.__init__(name, meta, config, message_bus=None, registry=None)` 由加载器调用，
子类一般不需要覆盖；如覆盖请先 `super().__init__(...)`（基类负责缓存目录、日志、
自动订阅 `INPUT_TOPICS`）。

## 3. 生命周期钩子（均可选覆盖）

| 钩子 | 触发时机 |
|------|---------|
| `on_start()` / `on_stop()` | Agent 启动 / 停止（`on_stop` 中应释放资源） |
| `on_pause()` / `on_resume()` | 流水线暂停 / 恢复（断点续传） |
| `on_config_update(changed_keys)` | `POST /api/config/reload` 配置热更新 |
| `on_snapshot()` / `on_restore(state)` | checkpoint 创建 / 恢复（断点续传状态） |
| `cleanup_task_temp(task_id)` / `cleanup_stale_temp(max_age_hours)` | 任务结束 / 启动时的临时文件清理（返回清理数量） |
| `is_healthy()` | Registry 健康检查 |

## 4. 消息与工具方法

`handle()` 内可用的基类辅助方法（详见 `loom_core/base_agent.py`）：

- `self.publish(topic, payload)` — 发布消息到总线
- `self.send_to(to_agent, topic, payload)` — 定向发送
- `self.reply(original_msg, payload)` — 回复请求
- `self.cache_get(key)` / `self.cache_set(key, data)` — 文件后端跨进程缓存
- `self.log_debug/info/warning/error(msg)` — 结构化日志
- `self.report(status, info)` — 上报状态到 Registry

## 5. 安全沙箱

第三方 Agent 加载时执行 AST 静态检查（`loom_core/agent_loader.py:_check_safety`），
内置白名单 Agent 跳过检查。默认 `strict_safety=True`，命中即阻断加载。

**危险调用黑名单**（`_DANGEROUS_CALLS`，节选）：
`os.system`、`os.popen`、`os.remove/unlink/rmdir/rename/chmod/kill/fork`、
`subprocess.Popen/run/call/check_call/check_output`、`eval`、`exec`、`compile`、
`__import__`、`shutil.rmtree/move/copy2`、`open`、`socket.socket/connect/bind`、
`ctypes.CDLL/PyDLL/WinDLL/pythonapi`、`pickle.loads/load`、`marshal.loads`。

**导入拦截**：
- `from subprocess import Popen` 等 `from <危险模块> import <危险名>` 直接拦截；
- `from os import system/remove/...` 拦截危险函数名；
- `import ctypes / pickle / marshal` 直接拦截。

即：自定义 Agent 内**不要用 `open()`**——需要读写文件时通过基类缓存或 config 提供的路径封装。

## 6. 模块身份：一个文件在进程里只能有一份对象

`AgentLoader.register()` 默认**复用** `sys.modules["agents.<name>"]` 里已经由同一个
文件加载出来的模块对象；要强制换新代码得显式传 `reload=True`。

这不是优化，是断掉一类假绿：原来每次注册都 `module_from_spec + exec_module` 并覆写
`sys.modules`，于是同一个 Agent 存在两份类对象——测试里
`patch("agents.writer.WriterAgent.handle")` 打的是先导入的那一份，注册器造的实例用的
是另一份，补丁全程空转，用例照样通过（本仓库因此收回过两次"绿了"的结论）。
重复注册还会不断丢弃旧模块，模块级状态跟着翻倍。

三条边界：

| 情况 | 行为 |
|------|------|
| 缓存模块的 `__file__` 与 `agents_dir/<name>.py` 是同一个文件 | 复用，不重新执行 |
| 同名但来自**另一个目录**（测试夹具、插件目录并存时常见） | 不复用，按本目录的文件重新加载 |
| `reload=True` | 强制重新执行（热插拔新代码的口子） |

复用与否都照旧跑 AST 安全扫描：缓存里那一份可能是普通 `import` 带进来的，
从没走过这道检查，只在"新加载"时检查等于给已导入模块开免检通道。

判据：`tests/test_agent_loader.py::TestModuleIdentity`（含"注册前打的补丁必须生效"、
"同名不同目录不得复用"、"reload 真的换对象"、"复用时仍扫描"），
把复用条件改回"永远新建"会有三条转红。

## 7. 最小示例

```python
"""agents/echo_agent.py — 最小自定义 Agent 示例"""
from loom_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "echo"
AGENT_VERSION = "1.0"
AGENT_DESC = "回显输入的演示 Agent"
AGENT_AUTHOR = "your-name"
AGENT_PRIORITY = 90
INPUT_TOPICS = ["echo.input"]
OUTPUT_TOPICS = ["echo.done"]
DEPENDENCIES = []
CACHE_TTL = 0
RESPAWN = False


class EchoAgent(BaseAgent):
    def handle(self, msg: Message) -> dict | None:
        payload = msg.payload if hasattr(msg, "payload") else {}
        text = payload.get("text", "")
        self.log_info(f"echo 收到: {text[:50]}")
        self.publish("echo.done", {"text": text})
        return {"status": "ok", "text": text}
```

放入 `agents/` 目录后，`python run.py input.md --agent echo` 或在 pipeline YAML 的
`agents:` 列表中声明即可被加载。若 Agent 未被流水线 DAG 引用，
可用 `loom plugin list` 确认注册状态。

## 8. 节点级配置从 `payload["config"]` 来

YAML 里 `- name: xxx` 下面的 `config:` **不保证**出现在 `self.config` 里：DAG 执行器把
节点配置并进载荷的 `config` 键下发，实例配置只是兜底。所以读配置要两边合：

```python
cfg = {**(self.config or {}), **(msg.payload or {}).get("config", {})}
url = str(cfg.get("url") or "")
```

只读 `self.config` 的 Agent 会在单测里全绿、在真流水线上直接失败或静默默认值——
`http_request` 第一版就是这样（单元测试直接构造实例传 config，出厂 `api-report` 一跑
就报"未给出 url"）。同理 `CONFIG_SCHEMA` 的默认值只在缺键时生效，别拿它当"运行期一定会读到"。

产物键必须声明：`PRODUCES = {"response": "last"}`、`CONSUMES = [...]`。引擎按声明组装
下游载荷，没声明的返回值不会成为产物；写文件类 Agent 还要命中交付契约（`WRITES_OUTPUT`），
否则"跑通了却没有交付物"这类事没人替你发现。

## 9. 通用能力必须有出厂消费者

新增一个不带领域语义的 Agent（`agents/http_request_agent.py`、`agents/transform_agent.py`），
必须同时交一条**真实引用它的流水线**（`pipelines/api-report.yaml`）和一条**跑通整条链的
离线 E2E**。理由是本仓库反复验证过：实现了 ≠ 接线了，能力没进任何流水线就等于零，
而单测只证明函数能被调用，不证明 Scheduler → DAGExecutor → 落盘这条链认它。

E2E 的断言对象是**交付物本身**，不是 task.status：文件存在、模板里没有残留的 `{{`、
渲染结果在正文里。把接口打挂的另一条用例则要求流水线如实 `failed` 并把错误带出来——
"失败也如实"和"成功有产物"是同一枚硬币的两面。
