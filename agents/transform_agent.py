"""通用数据转换 Agent —— 工作流里的"胶水"，不靠 LLM 也不靠 eval。

为什么需要它：引擎此前每接一种新任务都要写一个领域 Agent（researcher / writer /
layout …），但大量真实工作流只需要"把上一步的产物挑几个字段、换个形状、渲染一段
文本"。没有通用件时，这件事只能靠新增 Agent 代码完成，引擎的"通用"就是空话。

设计约束（三条，都是被咬过之后定的）：

1. **没有表达式语言**。路径解析复用 `loom_core.conditions` 里 `when` 用的同一套
   点号取值器，过滤条件就是同一条 `when` 语言。引擎里绝不对数据执行 `eval`/`format`
   拼接的代码字符串——那等于把工作流的输入数据升级成代码。
2. **取不到路径就报错，不返回空**。空字符串会渲染出"看起来正常"的产物，缺字段这种
   作者笔误就会静默出厂（本项目反复在关的那类洞）。
3. **产物形状可预期**：`data` 是一个 list 或 dict，`text` 是渲染后的字符串，
   都按 `PRODUCES` 声明导出；没有声明的键不会被提升到下游载荷。

配置示例：

    - name: shape
      agent: transform
      config:
        items: artifacts.response          # 上游 http_request 的响应
        fields: [id, title]                # 每项只留这两个键
        where: {path: item.title, op: truthy}
        set:
          - {name: total, from: "len(items)"}   # 见 _resolve：仅支持 len(...)
        template: "共 {{len(items)}} 条，首条 {{items.0.title}}"
"""

import re

from loom_core.base_agent import BaseAgent, Message
from loom_core.conditions import UNRESOLVED, ConditionError, resolve
from loom_core.conditions import evaluate as evaluate_condition
from loom_core.conditions import validate as validate_condition

AGENT_NAME = "transform"
AGENT_VERSION = "1.0"
AGENT_DESC = "通用数据转换 Agent（挑字段 / 过滤 / 渲染模板，不执行表达式）"
AGENT_AUTHOR = "loom"
AGENT_PRIORITY = 50

CONFIG_SCHEMA = {
    "items": ("str", ""),
    "fields": ("list", []),
    "where": ("dict", {}),
    "set": ("list", []),
    "template": ("str", ""),
}

INPUT_TOPICS = ["transform.input"]
OUTPUT_TOPICS = ["transform.done", "transform.failed"]
PRODUCES: dict = {"data": "last", "text": "last", "content": "last"}
CONSUMES: list = []
DEPENDENCIES: list = []
# 与 http_request 同理：没有节点级 items/template 就无事可做，不该被 legacy 自动图拉进来。
LEGACY_AUTO = False
CACHE_TTL = 0
RESPAWN = False
AGENT_TAGS = ["generic", "transform"]

_LEN_RE = re.compile(r"^len\(\s*([A-Za-z0-9_.\[\]]+)\s*\)$")
# 允许 len(...) 与点号路径两种；引号、方括号、逗号一律不进正则，
# 于是 `{{__import__('os')}}` 这类只会原样留在文本里，不会被求值。
_VAR_RE = re.compile(r"\{\{\s*(len\([A-Za-z0-9_.]+\)|[A-Za-z0-9_.]+)\s*\}\}")
def _lookup(path: str, ctx: dict):
    """点号取值；取不到返回 UNRESOLVED，由调用方决定报错文案。"""
    return resolve(path, ctx)


def _resolve(path: str, ctx: dict):
    """支持 `len(a.b)` 与点号路径两种写法，其余一律拒绝。

    只给 `len()` 这一个函数是有意的：作者想要别的运算就该用条件或换 Agent，
    在配置里塞一个表达式求值器等于把数据通道变成代码通道。
    """
    stripped = (path or "").strip()
    m = _LEN_RE.match(stripped)
    if m:
        inner = _lookup(m.group(1), ctx)
        if inner is UNRESOLVED:
            raise KeyError(m.group(1))
        return len(inner if isinstance(inner, (list, tuple, dict, str)) else [])
    got = _lookup(stripped, ctx)
    if got is UNRESOLVED:
        raise KeyError(stripped)
    return got


def _context(payload: dict, cfg: dict) -> dict:
    """与 `when` 的上下文同形，作者不必学第二套取数规则。

    `item`/`index`/`count` 只在节点被 `foreach` 展开时存在（引擎逐项注入的三元组，
    见 dag_executor._execute_foreach）。**没展开就不放这三个键**，而不是放三个 None：
    键存在但值为 None 时，`{{index}}` 会渲染成字面量 "None"、`{{item.x}}` 渲染成空——
    那正是"看起来正常却少了一列"的静默形状；让它取不到值、直接报错才对。
    """
    ctx = {
        "artifacts": payload.get("upstream") or {},
        "upstream": payload.get("dependencies_results") or {},
        "config": dict(cfg or {}),
        "inputs": dict(payload.get("inputs") or {}),
        "pipeline": payload.get("pipeline"),
        "task": payload.get("task_id"),
    }
    for key in ("item", "index", "count"):
        # 连显式 None 都不放：放进来就等于让 `{{index}}` 渲染出字面量 "None"
        if payload.get(key) is not None:
            ctx[key] = payload[key]
    return ctx


class TransformAgent(BaseAgent):
    """按声明挑字段 / 过滤 / 计数 / 渲染模板。"""

    def handle(self, msg: Message) -> dict:
        payload = msg.payload or {}
        # 节点级配置从载荷进来（Scheduler 把 YAML 的 config 与构造期配置合并后
        # 放进 payload["config"]）；实例配置只作兜底，否则 YAML 里的 url 会被忽略。
        cfg = {**(self.config or {}), **(msg.payload or {}).get("config", {})}
        ctx = _context(payload, cfg)

        # 先收集"列表工作"的产物
        items = None
        if cfg.get("items"):
            try:
                items = _resolve(str(cfg["items"]), ctx)
            except KeyError as e:
                return {"status": "error",
                        "error": f"items 路径取不到: {e.args[0]}"}
            if not isinstance(items, list):
                return {"status": "error",
                        "error": f"items 必须是列表，实际 {type(items).__name__}"}

        kept: list = []
        fields = [str(f) for f in (cfg.get("fields") or [])]
        where = cfg.get("where") or {}
        if where:
            try:
                validate_condition(where)
            except ConditionError as e:
                return {"status": "error", "error": f"where 非法: {e}"}
            if not cfg.get("items"):
                # 与引擎对 call.inputs 的规矩同源：写了没人读的配置必须报错，
                # 静默忽略比报错难查得多（作者以为过滤生效了，产物却全量出厂）。
                return {"status": "error",
                        "error": "where 需要 items：它按项过滤，没有列表时被过滤的其实是零项"}
        if fields and not cfg.get("items"):
            return {"status": "error",
                    "error": "fields 需要 items：挑字段是逐项目标，没给列表时会被静默忽略"}
        if items is not None:
            for idx, one in enumerate(items):
                item_ctx = dict(ctx)
                item_ctx["item"] = one
                item_ctx["items"] = items          # 供 len(items) 这类整体统计使用
                item_ctx["index"] = idx
                if where:
                    try:
                        if not evaluate_condition(where, item_ctx):
                            continue
                    except ConditionError as e:
                        return {"status": "error", "error": f"where 求值失败: {e}"}
                if fields:
                    missing = [f for f in fields if isinstance(one, dict) and f not in one]
                    if missing and isinstance(one, dict):
                        return {"status": "error",
                                "error": f"第 {idx} 项缺少字段 {missing}（不静默丢字段）"}
                    if isinstance(one, dict):
                        kept.append({f: one.get(f) for f in fields})
                    else:
                        kept.append(one)
                else:
                    kept.append(one)

        out: dict = {"status": "ok"}
        data: object = kept if items is not None else {}
        if isinstance(data, list):
            out["count"] = len(data)
        out["data"] = data

        # set：把派生值挂进产物（{name, from}）
        extras = {}
        for spec in (cfg.get("set") or []):
            if not isinstance(spec, dict) or "name" not in spec or "from" not in spec:
                return {"status": "error",
                        "error": f"set 项必须是 {{name, from}}，实际: {spec!r}"}
            src = str(spec["from"])
            probe = dict(ctx)
            probe["items"] = kept if items is not None else []
            try:
                extras[str(spec["name"])] = _resolve(src, probe)
            except KeyError:
                return {"status": "error",
                        "error": f"set.from={src!r} 取不到值（上下文里没有这个路径）"}
        if isinstance(data, dict):
            data.update(extras)
            out["data"] = data
        elif extras:
            out.update(extras)

        # template：{{path}} 文本渲染，取不到就报错而不是留空
        template = str(cfg.get("template") or "")
        if template:
            probe = dict(ctx)
            probe["items"] = kept if items is not None else []
            probe["data"] = out["data"]

            def _sub(m):
                try:
                    return str(_resolve(m.group(1), probe))
                except KeyError:
                    raise KeyError(m.group(1)) from None

            try:
                rendered = _VAR_RE.sub(_sub, template)
            except KeyError as e:
                return {"status": "error",
                        "error": f"template 里的 {e.args[0]!r} 取不到值"
                                 "（缺字段不该渲染成空串出厂）"}
            out["text"] = rendered
            # 同时挂一份 content：落盘与质检那批节点按 CONSUMES=["content"] 取正文，
            # 通用流水线要能直接接上它们，不必每个模板都再包一层映射。
            out["content"] = rendered
        return out
