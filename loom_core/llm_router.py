"""
LLM Router — 多供应商 LLM 路由器
=================================
核心特性：
  - 供应商定义表 16 家，按 `.env` 是否配齐 URL/MODEL/KEY 决定实际启用哪几家
  - 健康检查 + 自动故障转移
  - 加权选择 + 优先级排序
  - 从 .env 加载配置
  - 线程安全

已定义的 16 家（`_build_providers` 里的 `provider_defs`，顺序即优先级）：
  cloudflare、openai、deepseek、moonshot、qwen、xiaomi_mimo、longcat、sensenova、
  glm、agnes、nvidia、bailian、qianfan、dahl、siliconflow、ollama

> **口径边界（务必如实理解"支持 16 家"）**：这 16 条是**端点与定价定义**，不是 16 套
> 协议实现。请求路径只有一条 `_call_llm` / `_call_llm_async`，响应解析按 OpenAI 兼容形态
> 取 `choices[0].message.content`，唯一的形态分支是 Cloudflare 的 `/ai/run` URL。
> 因此配进表里的 URL 必须是该家的 **OpenAI 兼容端点**（`/api/generate` 这类原生形态
> 在这条解析路径上取不到内容），否则配了 Key 也会在解析响应时失败。
> 启用状态请用 `python run.py --check` 实测，别照本表推断。
> 本段与表的一致性由 `tests/test_doc_consistency.py` 把关（条目数取自代码，不写死数字）。
"""
import asyncio
import atexit
import contextlib
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from artesian.env import load_env
from artesian.fast_json import dumps as _fast_dumps
from artesian.fast_json import loads as _fast_loads

logger = logging.getLogger(__name__)

# ─── 共享 aiohttp Session（连接池复用）──────────────
_shared_aiohttp_session: object | None = None
_shared_session_loop: object | None = None  # C15: 记录 session 绑定的创建 loop
_session_lock = threading.Lock()


class _EmptyContentError(Exception):
    """C15: HTTP 成功但内容为空——请求已计费，不得回退同步重试同一供应商"""


async def _get_shared_session():
    """获取共享 aiohttp.ClientSession（连接池复用，避免每次创建新连接）

    首次调用时创建 Session，后续复用。线程安全。

    C15: aiohttp.ClientSession 绑定创建它的事件循环，跨 loop 使用会抛
    "attached to a different loop"。writer 等调用方按文档新建 loop，此时
    返回 None 让调用方直接走 run_in_executor 同步路径，而不是等运行时
    报错后被动回退。
    """
    global _shared_aiohttp_session, _shared_session_loop
    loop = asyncio.get_running_loop()
    if _shared_aiohttp_session is not None and not _shared_aiohttp_session.closed:
        if _shared_session_loop is loop:
            return _shared_aiohttp_session
        return None
    try:
        import aiohttp
    except ImportError:
        return None
    with _session_lock:
        if _shared_aiohttp_session is not None and not _shared_aiohttp_session.closed:
            if _shared_session_loop is loop:
                return _shared_aiohttp_session
            return None
        _shared_aiohttp_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=20, limit_per_host=5),
        )
        _shared_session_loop = loop
    return _shared_aiohttp_session


async def _close_shared_session():
    """关闭共享 Session（应用退出时调用）"""
    global _shared_aiohttp_session, _shared_session_loop
    if _shared_aiohttp_session is not None:
        await _shared_aiohttp_session.close()
        _shared_aiohttp_session = None
        _shared_session_loop = None


def _close_shared_session_at_exit() -> None:
    """进程退出时关闭共享 aiohttp Session（best-effort，避免连接泄漏告警）"""
    if _shared_aiohttp_session is None or getattr(_shared_aiohttp_session, "closed", True):
        return
    with contextlib.suppress(Exception):
        asyncio.run(_close_shared_session())


atexit.register(_close_shared_session_at_exit)


@dataclass
class LLMProvider:
    """LLM 供应商配置"""
    name: str
    api_url: str
    api_key: str
    model: str
    priority: int = 100          # 数字越小优先级越高
    enabled: bool = True
    is_cloudflare: bool = False  # CF Workers AI 需特殊格式
    max_tokens: int = 4096
    timeout: int = 120
    # 运行时状态
    _healthy: bool = True
    _last_error: str = ""
    _last_check: float = 0.0
    _fail_count: int = 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    def mark_failed(self, error: str):
        self._fail_count += 1
        self._last_error = error
        self._last_check = time.time()
        if self._fail_count >= 3:
            self._healthy = False
            logger.warning(f"LLM 供应商 {self.name} 标记为不健康 (连续失败 {self._fail_count} 次)")

    def mark_success(self):
        self._fail_count = 0
        self._healthy = True
        self._last_error = ""
        self._last_check = time.time()

    def check_health(self, cooldown: float = 60.0) -> bool:
        """主动健康检查（发送简单请求）

        Args:
            cooldown: 健康检查冷却时间（秒），跳过最近检查过的不健康供应商
        """
        # 冷却期内跳过已不健康的供应商，避免每次调用都重试
        if not self._healthy and self._last_check > 0:
            elapsed = time.time() - self._last_check
            if elapsed < cooldown:
                return False
        try:
            messages = [{"role": "user", "content": "1+1=?"}]
            _call_llm(self, messages, max_tokens=10, timeout=15)
            self.mark_success()
            return True
        except Exception as e:
            self.mark_failed(str(e))
            return False


def _call_llm(provider: LLMProvider, messages: list[dict],
              max_tokens: int = 4096, temperature: float = 0.3,
              timeout: int = None, usage_out: dict = None) -> str:
    """底层 LLM 调用（自动适配 Cloudflare 格式）

    Args:
        usage_out: 可选 dict，响应含 usage 字段时回填 prompt_tokens/completion_tokens
    """
    timeout = timeout or provider.timeout
    is_cf = provider.is_cloudflare or "/ai/run" in provider.api_url

    if is_cf:
        payload = {
            "model": provider.model,
            "input": {"messages": messages},
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    else:
        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    data = _fast_dumps(payload).encode()
    req = urllib.request.Request(
        provider.api_url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = _fast_loads(resp.read())

    if is_cf:
        body = body.get("result", body)

    if usage_out is not None:
        u = body.get("usage") or {}
        if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
            usage_out["prompt_tokens"] = u["prompt_tokens"]
            usage_out["completion_tokens"] = u["completion_tokens"]

    content = body["choices"][0]["message"].get("content", "") or ""

    # 过滤 <think>...</think> 标签（Dahl/MiniMax 等推理模型）
    content = re.sub(r'<\s*think\s*>.*?<\s*/\s*think\s*>', '', content, flags=re.DOTALL).strip()

    if not content:
        raise ValueError(f"供应商 {provider.name} 返回空内容（可能因 reasoning 模型 max_tokens 不足）")
    return content


async def _call_llm_async(provider: LLMProvider, messages: list[dict],
                          max_tokens: int = 4096, temperature: float = 0.3,
                          timeout: int = None, usage_out: dict = None) -> str:
    """异步 LLM 调用 — 使用 aiohttp 真异步 HTTP，不阻塞事件循环。

    优先使用共享 aiohttp Session（连接池复用），无 aiohttp 时回退到 run_in_executor。

    Args:
        usage_out: 可选 dict，响应含 usage 字段时回填 prompt_tokens/completion_tokens
    """
    timeout = timeout or provider.timeout
    is_cf = provider.is_cloudflare or "/ai/run" in provider.api_url

    if is_cf:
        payload = {
            "model": provider.model,
            "input": {"messages": messages},
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    else:
        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    # 尝试 aiohttp 真异步
    session = await _get_shared_session()
    if session is not None:
        try:
            # 注意: aiohttp 在 _get_shared_session 内局部 import，
            # 这里通过 sys.modules 引用，避免 NameError 和 dir() 误判
            aiohttp_mod = sys.modules.get("aiohttp")
            cf_timeout = aiohttp_mod.ClientTimeout(total=timeout) if aiohttp_mod else None
            async with session.post(
                provider.api_url, json=payload, headers=headers,
                timeout=cf_timeout,
            ) as resp:
                body = await resp.json()
            if is_cf:
                body = body.get("result", body)
            if usage_out is not None:
                u = body.get("usage") or {}
                if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
                    usage_out["prompt_tokens"] = u["prompt_tokens"]
                    usage_out["completion_tokens"] = u["completion_tokens"]
            content = body["choices"][0]["message"].get("content", "") or ""
            content = re.sub(r'<\s*think\s*>.*?<\s*/\s*think\s*>', '', content, flags=re.DOTALL).strip()
            if not content:
                raise _EmptyContentError(f"供应商 {provider.name} 返回空内容")
            return content
        except _EmptyContentError:
            # C15：空内容是业务层结果而非传输故障，HTTP 调用已计费；
            # 回退同步等于对同一供应商重复计费请求。直接上抛，
            # 由 chat_async 的 per-provider except 走下一供应商。
            raise
        except Exception as e:
            logger.debug(f"aiohttp 调用 {provider.name} 失败，回退同步: {e}")

    # 回退到同步（run_in_executor）
    # 优先使用 get_running_loop()（3.10+ 推荐），无运行循环时回退到 get_event_loop()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _call_llm, provider, messages, max_tokens, temperature, timeout, usage_out,
    )


def _enforce_budget() -> None:
    """预算熔断：超预算抛 BudgetExceededError；未设置预算时放行

    检查器自身故障（DB 损坏等）不影响主流程，仅告警。
    """
    from .cost_tracker import BudgetExceededError, get_cost_tracker
    try:
        get_cost_tracker().ensure_budget()
    except BudgetExceededError:
        raise
    except Exception as e:
        logger.warning(f"预算检查失败（不影响主流程）: {e}")


class LLMRouter:
    """多供应商 LLM 路由器

    用法：
        router = LLMRouter.from_env()
        reply = router.chat([{"role": "user", "content": "你好"}])
        # 自动选择健康供应商，失败自动 fallback
    """

    # 健康检查冷却时间（秒），避免每次调用都重试不健康的供应商
    HEALTH_CHECK_COOLDOWN = 60  # 1 分钟（429 限流通常 1 分钟内恢复）

    def __init__(self, providers: list[LLMProvider] = None):
        self._providers: list[LLMProvider] = providers or []
        self._lock = threading.Lock()
        self._sort_providers()
        logger.info(f"LLMRouter 初始化: {len(self._providers)} 个供应商")

    def _sort_providers(self):
        """按优先级排序"""
        self._providers.sort(key=lambda p: (p.priority, not p.enabled))

    def add_provider(self, provider: LLMProvider):
        with self._lock:
            self._providers.append(provider)
            self._sort_providers()

    def get_active_providers(self) -> list[LLMProvider]:
        """获取所有启用的供应商（按优先级排序）"""
        return [p for p in self._providers if p.enabled and p.healthy]

    def get_best_provider(self) -> LLMProvider | None:
        """获取最佳供应商"""
        active = self.get_active_providers()
        return active[0] if active else None

    def chat(self, messages: list[dict], max_tokens: int = 4096,
             temperature: float = 0.3, timeout: int = None,
             preferred: str = None) -> tuple[str, str]:
        """调用 LLM（自动 fallback）

        返回: (content, provider_name)
        异常: 所有供应商都失败时抛 RuntimeError；预算耗尽抛 BudgetExceededError
        """
        _enforce_budget()
        with self._lock:
            candidates = list(self._providers)

        # 优先使用指定供应商
        if preferred:
            for p in candidates:
                if p.name == preferred and p.enabled and p.healthy:
                    candidates.remove(p)
                    candidates.insert(0, p)
                    break

        # 过滤可用供应商
        usable = [p for p in candidates if p.enabled and p.healthy]
        if not usable:
            # 尝试重新激活不健康的供应商（有冷却时间）
            logger.info("所有供应商不健康，尝试重新检查...")
            usable = [p for p in candidates if p.enabled]
            for p in usable:
                if p.check_health(cooldown=self.HEALTH_CHECK_COOLDOWN):
                    logger.info(f"供应商 {p.name} 已恢复")
                else:
                    # 只在冷却期外才打印警告，避免日志洪水
                    if not p._healthy and p._last_check > 0:
                        elapsed = time.time() - p._last_check
                        if elapsed >= self.HEALTH_CHECK_COOLDOWN:
                            logger.warning(f"供应商 {p.name} 仍不健康: {p._last_error}")
            usable = [p for p in usable if p.healthy]

        if not usable:
            raise RuntimeError("所有 LLM 供应商不可用")

        last_error = ""
        for provider in usable:
            usage: dict = {}
            try:
                content = _call_llm(provider, messages, max_tokens, temperature, timeout,
                                    usage_out=usage)
                provider.mark_success()
                logger.debug(f"LLM 调用成功: {provider.name}")

                try:
                    from .cost_tracker import current_cost_task_id, get_cost_tracker
                    pt = usage.get("prompt_tokens")
                    ct = usage.get("completion_tokens")
                    if isinstance(pt, int) and isinstance(ct, int):
                        get_cost_tracker().record(
                            provider=provider.name,
                            prompt_tokens=pt,
                            completion_tokens=ct,
                            model=provider.model,
                            task_id=current_cost_task_id(),
                        )
                    else:
                        get_cost_tracker().record_call(
                            provider=provider.name,
                            messages=messages,
                            response=content,
                            model=provider.model,
                            task_id=current_cost_task_id(),
                        )
                except Exception as e:
                    # 记录成本失败不应影响主流程，但需留痕便于排查（避免 silent pass 吞掉 DB 损坏等关键错误）
                    logger.warning(f"记录成本失败（不影响主流程）: {e}")

                return content, provider.name
            except Exception as e:
                provider.mark_failed(str(e))
                last_error = f"{provider.name}: {e}"
                logger.warning(f"LLM 供应商 {provider.name} 失败: {e}")
                continue

        raise RuntimeError(f"所有 LLM 供应商调用失败，最后错误: {last_error}")

    async def chat_async(self, messages: list[dict], max_tokens: int = 4096,
                         temperature: float = 0.3, timeout: int = None,
                         preferred: str = None) -> tuple[str, str]:
        """异步调用 LLM（自动 fallback）— 不阻塞事件循环

        返回: (content, provider_name)
        异常: 所有供应商都失败时抛 RuntimeError；预算耗尽抛 BudgetExceededError
        """
        _enforce_budget()
        with self._lock:
            candidates = list(self._providers)

        if preferred:
            for p in candidates:
                if p.name == preferred and p.enabled and p.healthy:
                    candidates.remove(p)
                    candidates.insert(0, p)
                    break

        usable = [p for p in candidates if p.enabled and p.healthy]
        if not usable:
            # 同步健康检查（快速路径，不阻塞事件循环太久）
            logger.info("所有供应商不健康，尝试重新检查...")
            usable = [p for p in candidates if p.enabled]
            for p in usable:
                if p.check_health(cooldown=self.HEALTH_CHECK_COOLDOWN):
                    logger.info(f"供应商 {p.name} 已恢复")
                else:
                    if not p._healthy and p._last_check > 0:
                        elapsed = time.time() - p._last_check
                        if elapsed >= self.HEALTH_CHECK_COOLDOWN:
                            logger.warning(f"供应商 {p.name} 仍不健康: {p._last_error}")
            usable = [p for p in usable if p.healthy]

        if not usable:
            raise RuntimeError("所有 LLM 供应商不可用")

        last_error = ""
        for provider in usable:
            usage: dict = {}
            try:
                content = await _call_llm_async(provider, messages, max_tokens, temperature,
                                                timeout, usage_out=usage)
                provider.mark_success()
                logger.debug(f"LLM 异步调用成功: {provider.name}")

                try:
                    from .cost_tracker import current_cost_task_id, get_cost_tracker
                    pt = usage.get("prompt_tokens")
                    ct = usage.get("completion_tokens")
                    if isinstance(pt, int) and isinstance(ct, int):
                        get_cost_tracker().record(
                            provider=provider.name,
                            prompt_tokens=pt,
                            completion_tokens=ct,
                            model=provider.model,
                            task_id=current_cost_task_id(),
                        )
                    else:
                        get_cost_tracker().record_call(
                            provider=provider.name,
                            messages=messages,
                            response=content,
                            model=provider.model,
                            task_id=current_cost_task_id(),
                        )
                except Exception as e:
                    logger.warning(f"记录成本失败（不影响主流程）: {e}")

                return content, provider.name
            except Exception as e:
                provider.mark_failed(str(e))
                last_error = f"{provider.name}: {e}"
                logger.warning(f"LLM 供应商 {provider.name} 异步失败: {e}")
                continue

        raise RuntimeError(f"所有 LLM 供应商异步调用失败，最后错误: {last_error}")

    def health_check_all(self) -> dict:
        """检查所有供应商健康状态"""
        results = {}
        for p in self._providers:
            if not p.enabled:
                results[p.name] = {"status": "disabled", "healthy": False}
                continue
            healthy = p.check_health(cooldown=0)  # 强制检查，不冷却
            results[p.name] = {
                "status": "healthy" if healthy else "unhealthy",
                "healthy": healthy,
                "fail_count": p._fail_count,
                "last_error": p._last_error,
                "model": p.model,
                "priority": p.priority,
            }
        return results

    def status(self) -> dict:
        """获取路由器状态摘要"""
        return {
            "total_providers": len(self._providers),
            "active": len(self.get_active_providers()),
            "providers": [
                {
                    "name": p.name,
                    "model": p.model,
                    "priority": p.priority,
                    "enabled": p.enabled,
                    "healthy": p.healthy,
                    "fail_count": p._fail_count,
                }
                for p in self._providers
            ],
        }

    # ─── 从 .env 加载 ──────────────────────────────

    @classmethod
    def from_env(cls, env_path: str = None) -> "LLMRouter":
        """从 .env 文件加载所有供应商配置

        .env 格式（每个供应商三行）：
            LLM_API_KEY=xxx
            LLM_API_URL=xxx
            LLM_MODEL=xxx
        备选供应商以注释状态存在，取消注释即激活。
        """
        env = _load_env(env_path)
        providers = []

        # 供应商定义表: (name, url_key, model_key, key_key, priority, is_cf)
        provider_defs = [
            ("cloudflare",  "LLM_API_URL",  "LLM_MODEL",  "LLM_API_KEY",  10, True),
            ("openai",      "OPENAI_API_URL", "OPENAI_MODEL", "OPENAI_API_KEY", 12, False),
            ("deepseek",    "DEEPSEEK_API_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", 13, False),
            ("moonshot",    "MOONSHOT_API_URL", "MOONSHOT_MODEL", "MOONSHOT_API_KEY", 14, False),
            ("qwen",        "QWEN_API_URL",   "QWEN_MODEL",   "QWEN_API_KEY",   15, False),
            ("xiaomi_mimo", "MIMO_API_URL", "MIMO_MODEL", "MIMO_API_KEY", 20, False),
            ("longcat",     "LONGCAT_API_URL","LONGCAT_MODEL","LONGCAT_API_KEY", 30, False),
            ("sensenova",   "SENSENOVA_API_URL","SENSENOVA_MODEL","SENSENOVA_API_KEY", 40, False),
            ("glm",         "GLM_API_URL",     "GLM_MODEL",     "GLM_API_KEY",     45, False),
            ("agnes",       "AGNES_API_URL", "AGNES_MODEL", "AGNES_API_KEY", 50, False),
            ("nvidia",      "NVIDIA_API_URL","NVIDIA_MODEL","NVIDIA_API_KEY", 60, False),
            ("bailian",     "BAILIAN_API_URL","BAILIAN_MODEL","BAILIAN_API_KEY", 65, False),
            ("qianfan",     "QIANFAN_API_URL","QIANFAN_MODEL","QIANFAN_API_KEY", 70, False),
            ("dahl",        "DAHL_API_URL",  "DAHL_MODEL",  "DAHL_API_KEY",  80, False),
            ("siliconflow", "SILICONFLOW_API_URL","SILICONFLOW_MODEL","SILICONFLOW_API_KEY", 90, False),
            ("ollama",      "OLLAMA_API_URL","OLLAMA_MODEL","OLLAMA_API_KEY", 100, False),
        ]

        for name, url_key, model_key, key_key, priority, is_cf in provider_defs:
            api_key = env.get(key_key, "")
            api_url = env.get(url_key, "")
            model = env.get(model_key, "")
            if api_key and api_url and model:
                providers.append(LLMProvider(
                    name=name, api_url=api_url, api_key=api_key,
                    model=model, priority=priority, is_cloudflare=is_cf,
                ))

        # 兼容：如果只有通用 LLM_API_KEY/URL/MODEL（无供应商前缀）
        if not providers:
            api_key = env.get("LLM_API_KEY", "")
            api_url = env.get("LLM_API_URL", "")
            model = env.get("LLM_MODEL", "")
            if api_key and api_url and model:
                is_cf = "/ai/run" in api_url
                providers.append(LLMProvider(
                    name="default", api_url=api_url, api_key=api_key,
                    model=model, priority=10, is_cloudflare=is_cf,
                ))

        return cls(providers)

    @classmethod
    def from_dict(cls, config: dict) -> "LLMRouter":
        """从字典配置加载"""
        providers = []
        for i, p_cfg in enumerate(config.get("providers", [])):
            providers.append(LLMProvider(
                name=p_cfg.get("name", f"provider_{i}"),
                api_url=p_cfg["api_url"],
                api_key=p_cfg["api_key"],
                model=p_cfg["model"],
                priority=p_cfg.get("priority", 100),
                enabled=p_cfg.get("enabled", True),
                is_cloudflare=p_cfg.get("is_cloudflare", False),
                max_tokens=p_cfg.get("max_tokens", 4096),
                timeout=p_cfg.get("timeout", 120),
            ))
        return cls(providers)


# _load_env 的实现已迁至 artesian.env.load_env（单一事实来源，搜索层与路由层共用）。
# 保留 _load_env 这个名字：本模块调用点、测试的 patch 目标与
# `from loom_core.llm_router import _load_env` 都指向它。
_load_env = load_env


# ─── 便捷函数 ──────────────────────────────────

_router_instance: LLMRouter | None = None
_router_lock = threading.Lock()


def get_router() -> LLMRouter:
    """获取全局 LLMRouter 单例"""
    global _router_instance
    with _router_lock:
        if _router_instance is None:
            _router_instance = LLMRouter.from_env()
        return _router_instance


def reset_router():
    """重置全局路由器（配置变更后调用）"""
    global _router_instance
    with _router_lock:
        _router_instance = None
