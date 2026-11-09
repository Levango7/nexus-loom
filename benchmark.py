"""Loom 性能基准测试 —— 量化各项优化收益。

运行方式:
    python benchmark.py              # 全部基准
    python benchmark.py --quick      # 快速模式（减少迭代次数）
    python benchmark.py --ci         # CI 模式：对比 baseline，回归超阈值则 exit(1)
    python benchmark.py --update-baseline  # 更新 baseline（当前结果写入 benchmark_results.json）

CI 模式:
    对比 benchmark_results.json 中的 baseline 值，若性能回归超过阈值则失败。
    回归阈值默认 20%（可通过 --threshold 0.3 调整）。
    仅对数值型指标检测回归，新增指标自动忽略。

基准项:
    1. HTML 正文提取: selectolax vs 正则
    2. 缓存层: CacheManager get/set 吞吐
    3. 并行执行: ThreadPool vs ProcessPool vs 串行
    4. SSE 流式: chunk 传播延迟
    5. TF-IDF 语义匹配: 大规模段落评分
"""
from __future__ import annotations

import json
import os
import platform
import re
import statistics
import sys
import time
from pathlib import Path

# 确保 import 路径
sys.path.insert(0, str(Path(__file__).parent))

QUICK = "--quick" in sys.argv or "--ci" in sys.argv
CI_MODE = "--ci" in sys.argv
UPDATE_BASELINE = "--update-baseline" in sys.argv
ITERATIONS = 3 if QUICK else 10
LARGE_HTML_SIZE = 500_000  # 模拟大页面

# 回归阈值：性能下降超过此比例则 CI 失败
# P1 修复：边界保护 — 当 --threshold 是最后一个 argv 元素时避免 IndexError
_threshold_idx = sys.argv.index("--threshold") + 1 if "--threshold" in sys.argv else -1
try:
    REGRESSION_THRESHOLD = float(sys.argv[_threshold_idx]) if 0 < _threshold_idx < len(sys.argv) else 0.20
except (ValueError, IndexError):
    REGRESSION_THRESHOLD = 0.20

# 采样轮数：CI 默认 3 轮取中位数并量出波动，本地默认 1 轮（省时）。
# 上限 9 是防止有人把它当压测开关——波动要靠环境多样本来暴露，不是靠轮数堆。
_samples_idx = sys.argv.index("--samples") + 1 if "--samples" in sys.argv else -1
_samples_arg: int | None = None
if 0 < _samples_idx < len(sys.argv):
    try:
        _samples_arg = int(sys.argv[_samples_idx])
    except ValueError:
        _samples_arg = None        # 坏值：回默认，而不是拿 0/None 去跑零轮采样
_default_samples = 3 if CI_MODE else 1
SAMPLES = max(1, min(_samples_arg if _samples_arg is not None else _default_samples, 9))

# 指标方向映射：True=越高越好（吞吐、加速比），False=越低越好（耗时、延迟）
# 未列出的指标默认按值变化方向自动推断
METRIC_HIGHER_BETTER = {
    "speedup", "thread_speedup", "process_speedup",
    "set_ops_per_sec", "get_hit_ops_per_sec", "get_miss_ops_per_sec",
    "emit_ops_per_sec",
}
METRIC_LOWER_BETTER = {
    "selectolax", "regex", "serial", "thread_pool", "process_pool",
    "set_ms_per_op", "get_hit_ms_per_op",
    "emit_ms_per_op", "consume_ms",
    "elapsed_ms",
}

# 每个指标的"至少差多少才算回归"，单位与该指标本身一致。
#
# 为什么要这条：CI run 37350496361 把 并行执行 serial 0.02656→0.03671 秒
# （绝对差 10 毫秒，而整个测量才 30 毫秒、大头是池 spawn）判成 38.2% 回归，
# 内置"复验"再判一次仍是 38%——两次采样取自同一台被负载污染的机器，复验并
# 不能否证它。共享 runner 上一直红的门禁等于没有门禁。
#
# 数值是量出来的，两组证据写在括号里：本机连续 4 次独立跑的 median 与跨次抖动，
# 以及 CI run 37357935560 当轮 3 次采样的波动。下限一律取"比真正关心的最小变化
# 更小、比测量噪声稍大"。
METRIC_FLOORS: dict[str, float] = {
    # 秒。这两项本机跨次只抖 5.3% / 2.6%（CI 当轮也没进波动前八名），所以它们
    # **仍然参与判定**，只是 30 毫秒量级的测量上，15 毫秒以内的差不算信号——
    # CI 那次误报正是 10 毫秒
    "serial": 0.015, "thread_pool": 0.015,
    # 秒/页（selectolax≈0.0034，本机跨次 11.8% / CI 当轮 27.1%；regex≈0.0137，5.1%）
    "selectolax": 0.0005, "regex": 0.001,
    # 毫秒（TF-IDF≈43.8：本机跨次 7.5%，CI 当轮 38.1%；SSE consume≈0.068：14.7%）
    "elapsed_ms": 10.0, "consume_ms": 0.01,
    # 毫秒/操作（5000 次平均；本机跨次 set 57.6% / get_hit 42.9%）：
    # 30% 的相对阈对它们毫无意义，下限取到统计精度这一侧，其余交给采样波动判
    "set_ms_per_op": 5e-5, "get_hit_ms_per_op": 5e-5, "emit_ms_per_op": 5e-5,
    # 无量纲加速比：分子分母同源于秒级测量，噪声相干，0.15x 以内不判
    "speedup": 0.15, "thread_speedup": 0.15,
}

# 只观测、不参与门禁的指标。理由要说准：不是"并行测量都不可信"——serial 与
# thread_pool 很稳（本机跨次 5.3% / 2.6%），它们照旧判定，只受上面的绝对下限约束。
# 不可信的是**进程池那两项**：CI run 37357935560 当轮 3 次连续采样里
# process_pool 自己就抖 91.9%、process_speedup 抖 51.2%（本机跨次也有 14.4%），
# 它测的是 spawn 成本，随机器状态漂。拿它当发布门禁就是抽签。
# "并行执行还能不能用"由 tests/test_executor_factory.py::TestProcessPoolExecution
# 负责——那是正确性断言，不是性能断言。
REPORT_ONLY_METRICS = {"process_pool", "process_speedup"}

# 吞吐类不写绝对下限：它们的噪声与量值成比例（700k ops/s 与 700 ops/s 的抖动
# 不是一个量级），这类指标靠"采样波动"那条规则判。
PROPORTIONAL_NOISE_METRICS = {
    "set_ops_per_sec", "get_hit_ops_per_sec", "get_miss_ops_per_sec", "emit_ops_per_sec",
}


# 采样波动带用的历史窗口与文件上限：跨 run 抖动取最近 20 轮，文件最多留 200 行。
HISTORY_WINDOW = 20
HISTORY_KEEP = 200


def _floor_for(metric: str, base_val: float) -> float:
    """该指标"至少差多少才算回归"；返回 0 表示不设绝对下限（交给采样波动判）。"""
    return METRIC_FLOORS.get(metric, 0.0)


def _env_fingerprint() -> dict:
    """跑一次基准所在环境的指纹。

    `load1` 不参与"环境变了没"的比较（它是瞬时量），只用来判断"当前这台
    runner 是否忙到测不出微基准"。
    """
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):        # Windows 上没有 getloadavg
        load1 = None
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load1": load1,
    }



def _gen_mock_html(size: int = 100_000) -> str:
    """生成模拟 HTML 页面"""
    para = "<p>This is a paragraph about Apache Kafka, a distributed event streaming platform. " \
           "It is used for high-throughput, low-latency data pipelines. " \
           "Kafka publishes records to topics, which are partitioned for scalability.</p>"
    nav = '<nav><a href="/">Home</a><a href="/about">About</a></nav>'
    footer = '<footer><p>Copyright 2024. All rights reserved.</p></footer>'
    body = para * (size // len(para) + 1)
    return f"<html><head><title>Test</title></head><body>{nav}{body[:size]}{footer}</body></html>"


def bench_html_extraction():
    """基准 1: HTML 正文提取 —— selectolax vs 正则"""
    html = _gen_mock_html(LARGE_HTML_SIZE)
    results = {}

    # selectolax —— 按内核可用性取解析器（历史上这里写死
    # `from selectolax.parser import HTMLParser`，在 selectolax 1.0 上必然
    # ImportError，于是本项长期记为 null，等于没测）
    from artesian import selectolax_compat as compat

    if compat.resolve_backend() is None:
        results["selectolax"] = None
    else:
        def _selectolax_extract(html_str):
            parsed = compat.get_parser(html_str)
            if parsed is None:
                return ""
            tree, _backend = parsed
            for tag in ("nav", "footer", "aside", "script", "style"):
                for node in tree.css(tag):
                    tree.decompose(node)
            return tree.text(separator=" ", strip=True)

        t0 = time.perf_counter()
        for _ in range(ITERATIONS):
            _selectolax_extract(html)
        results["selectolax"] = (time.perf_counter() - t0) / ITERATIONS

    # 正则
    def _regex_extract(html_str):
        text = re.sub(r"<script[^>]*>.*?</script>", "", html_str, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        _regex_extract(html)
    results["regex"] = (time.perf_counter() - t0) / ITERATIONS

    if results["selectolax"] and results["regex"]:
        # P1 修复：除零保护
        results["speedup"] = results["regex"] / results["selectolax"] if results["selectolax"] > 0 else 0.0

    return results


def bench_cache_throughput():
    """基准 2: CacheManager get/set 吞吐"""
    from loom_core.cache_manager import CacheManager

    cache = CacheManager(name="bench", max_size=10000, ttl=3600)
    n = 5000

    # SET
    t0 = time.perf_counter()
    for i in range(n):
        cache.set(f"key_{i}", f"value_{i}" * 100)
    set_time = time.perf_counter() - t0

    # GET (hit)
    t0 = time.perf_counter()
    for i in range(n):
        cache.get(f"key_{i}")
    get_hit_time = time.perf_counter() - t0

    # GET (miss)
    t0 = time.perf_counter()
    for i in range(n):
        cache.get(f"miss_{i}")
    get_miss_time = time.perf_counter() - t0

    # P1 修复：除零保护（极小时间可能为 0）
    return {
        "set_ops_per_sec": n / set_time if set_time > 0 else float("inf"),
        "get_hit_ops_per_sec": n / get_hit_time if get_hit_time > 0 else float("inf"),
        "get_miss_ops_per_sec": n / get_miss_time if get_miss_time > 0 else float("inf"),
        "set_ms_per_op": set_time / n * 1000,
        "get_hit_ms_per_op": get_hit_time / n * 1000,
    }


def _cpu_work(x):
    """CPU 密集型工作（模块级，可 pickle）"""
    total = 0
    for i in range(100_000):
        total += i * x
    return total


def bench_parallel_execution():
    """基准 3: 并行执行 —— ThreadPool vs ProcessPool vs 串行"""
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor


    n = 8
    results = {}

    # 串行
    t0 = time.perf_counter()
    [_cpu_work(i) for i in range(n)]
    results["serial"] = time.perf_counter() - t0

    # ThreadPool
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_cpu_work, range(n)))
    results["thread_pool"] = time.perf_counter() - t0

    # ProcessPool
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_cpu_work, range(n)))
    results["process_pool"] = time.perf_counter() - t0

    # P1 修复：除零保护（thread_pool/process_pool 时间极小时可能为 0）
    results["thread_speedup"] = results["serial"] / results["thread_pool"] if results["thread_pool"] > 0 else 0.0
    results["process_speedup"] = results["serial"] / results["process_pool"] if results["process_pool"] > 0 else 0.0

    return results


def bench_streaming_overhead():
    """基准 4: SSE 流式 chunk 传播开销"""
    from loom_core.streaming import StreamCallback

    callback = StreamCallback()
    n = 1000
    chunk = "Hello World, this is a test chunk. "

    t0 = time.perf_counter()
    for _i in range(n):
        callback.on_chunk(chunk, section_index=0)
    emit_time = time.perf_counter() - t0

    # 消费
    t0 = time.perf_counter()
    events = callback.get_events()
    consume_time = time.perf_counter() - t0

    # P1 修复：除零保护
    return {
        "emit_ops_per_sec": n / emit_time if emit_time > 0 else float("inf"),
        "emit_ms_per_op": emit_time / n * 1000,
        "consume_ms": consume_time * 1000,
        "events_buffered": len(events),
    }


def bench_tfidf():
    """基准 5: TF-IDF 语义匹配"""
    import numpy as np

    # 模拟 500 段落，每段 200 词
    n_paragraphs = 500
    n_keywords = 10
    paragraphs = []
    for i in range(n_paragraphs):
        words = [f"word_{(i + j) % 1000}" for j in range(200)]
        paragraphs.append({"text": " ".join(words), "title": f"Doc {i}", "url": f"http://ex{i}.com"})
    keywords = [f"word_{i}" for i in range(n_keywords)]

    t0 = time.perf_counter()

    # 构建词汇表
    all_docs = []
    for p in paragraphs:
        words = re.findall(r'[\w]{2,}', p["text"].lower())
        all_docs.append(words)

    vocab_set = set()
    for doc in all_docs:
        vocab_set.update(doc)
    vocab = sorted(vocab_set)
    vocab_idx = {w: i for i, w in enumerate(vocab)}
    n_docs = len(all_docs)
    n_terms = len(vocab)

    tfidf = np.zeros((n_docs, n_terms), dtype=np.float32)
    for i, doc in enumerate(all_docs):
        for w in doc:
            if w in vocab_idx:
                tfidf[i, vocab_idx[w]] += 1

    df = np.zeros(n_terms, dtype=np.float32)
    for i in range(n_docs):
        df += (tfidf[i] > 0).astype(np.float32)
    idf = np.log((n_docs + 1) / (df + 1)) + 1
    tfidf *= idf.reshape(1, -1)

    query_vec = np.zeros(n_terms, dtype=np.float32)
    for w in keywords:
        if w in vocab_idx:
            query_vec[vocab_idx[w]] = 1.0
    norm = np.linalg.norm(query_vec)
    if norm > 0:
        query_vec = query_vec / norm

    doc_norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
    doc_norms[doc_norms == 0] = 1
    similarities = (tfidf / doc_norms) @ query_vec
    sorted_indices = np.argsort(-similarities)

    elapsed = time.perf_counter() - t0

    return {
        "n_paragraphs": n_paragraphs,
        "n_terms": n_terms,
        "elapsed_ms": elapsed * 1000,
        "top_score": float(similarities[sorted_indices[0]]),
    }


# ═══════════════════════════════════════════════════════════

def _environment_factor(current: dict, baseline: dict, min_metrics: int = 3,
                        min_shift: float = 0.10) -> tuple[float, int, int]:
    """整体环境因子：本轮相对基线"普遍慢/普遍差"多少倍。

    共享 runner 上最常见的情形不是某个函数变慢，而是整台机器一起慢——CI run
    37360857091 就是这个形状：selectolax +32.2%、serial +30.9%、regex +36.4%。
    三项互不相干的代码不可能同时回归三成；把它当三次回归判红，门禁就又变成抽签。

    取"变差倍数"的中位数（不是均值：一个真回归的 outlier 会把均值带走），
    并要求至少 min_metrics 项参与。返回 (因子, 参与项数, 与因子同向的项数)；
    不够条件时因子为 1.0（即不校正）。
    """
    factors: list[float] = []
    for bench_name, cur_metrics in current.items():
        if str(bench_name).startswith("_") or not isinstance(cur_metrics, dict):
            continue
        base_metrics = baseline.get(bench_name)
        if not isinstance(base_metrics, dict):
            continue
        for metric, cur_val in cur_metrics.items():
            if metric in REPORT_ONLY_METRICS:
                continue        # 只观测的项不参与"机器整体快慢"的估计
            if not isinstance(cur_val, (int, float)) or isinstance(cur_val, bool) or not cur_val:
                continue
            base_val = base_metrics.get(metric)
            if not isinstance(base_val, (int, float)) or isinstance(base_val, bool) or not base_val:
                continue
            if metric in METRIC_HIGHER_BETTER:
                factors.append(float(base_val) / float(cur_val))
            elif metric in METRIC_LOWER_BETTER:
                factors.append(float(cur_val) / float(base_val))
    if len(factors) < min_metrics:
        return 1.0, len(factors), 0
    med = statistics.median(factors)
    if med < 1.0 + min_shift:
        return 1.0, len(factors), 0
    agreeing = sum(1 for f in factors if 0.6 * med <= f <= 1.6 * med)
    return med, len(factors), agreeing


def _effective_env(current: dict, baseline: dict) -> float:
    """需要时返回整体环境因子，否则 1.0。

    "多数指标一起动"才算环境：一半一半的时候，中位数没有资格替真回归开脱，
    所以要求同向项数占多数。
    """
    env, involved, agreeing = _environment_factor(current, baseline)
    if env == 1.0 or involved < 3 or agreeing * 2 < involved:
        return 1.0
    return env


def classify_change(bench_name: str, metric: str, base_val: float, cur_val: float,
                    threshold: float, spread: float | None = None,
                    env: float = 1.0,
                    ) -> tuple[str, str] | None:
    """判定一次指标变化：返回 (级别, 消息) 或 None（无需报告）。

    级别两种，含义必须分开：
      REGRESSION  —— 超出相对阈 **且** 超出该指标的测量噪声（绝对下限 + 采样波动）
      UNVERIFIED  —— 超出相对阈，但噪声解释得掉它：门禁没有资格据此判红
    把 UNVERIFIED 当 FAIL 就是"一直红的门禁等于没有门禁"；把它当 PASS 静默忽略
    又会让真回归溜过去，所以它要显式打印、要留在结果里。

    `env` 是整体环境因子（见 _environment_factor）：先按它校正变化幅度，再判三关。
    """
    higher_better = metric in METRIC_HIGHER_BETTER
    lower_better = metric in METRIC_LOWER_BETTER
    if not higher_better and not lower_better:
        return None
    raw_ratio = ((base_val - cur_val) if higher_better
                 else (cur_val - base_val)) / base_val
    ratio = (1.0 + raw_ratio) / (env or 1.0) - 1.0
    if ratio <= threshold:
        return None

    verb = "drop" if higher_better else "increase"
    note = f"  {bench_name}.{metric} baseline={base_val:.4g} current={cur_val:.4g} " \
           f"{verb}={ratio:.1%} > {threshold:.0%}"
    if env != 1.0:
        note += f"（原始 {raw_ratio:.1%}，已按整体环境因子 {env:.2f}× 校正）"
    reasons = []
    floor = _floor_for(metric, base_val)
    abs_delta = abs(cur_val - base_val)
    if floor and abs_delta < floor:
        reasons.append(f"绝对差 {abs_delta:.4g} 不到该指标的可测下限 {floor:.4g}")
    if spread is not None and ratio < 2 * spread:
        reasons.append(f"同一份代码重复测量的波动已达 {spread:.1%}"
                       f"（判据要求回归超过波动的 2 倍才可信）")
    if reasons:
        return "UNVERIFIED", f"{note} —— 不可判：{'；'.join(reasons)}"
    return "REGRESSION", f"REGRESSION: {note}"


def _historical_spreads(history_path: Path, min_runs: int = 5,
                        keep: int | None = None) -> dict[str, float]:
    """从趋势历史里量出每个指标的跨 run 抖动：(max-min)/median。

    为什么不能只靠本轮采样波动：连续 3 轮取自同一个时刻，躲不开"这台 runner
    这一小时整体慢 40%"。历史是唯一能区分"代码慢了"与"机器慢了"的参照。
    样本不足 min_runs 轮时返回空——没有足够历史就不假装知道噪声有多大。
    """
    if not history_path.exists():
        return {}
    series: dict[str, list[float]] = {}
    runs = 0
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        runs += 1
        for key, val in row.items():
            if str(key).startswith("_"):
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                series.setdefault(str(key), []).append(float(val))
    if runs < min_runs:
        return {}
    out: dict[str, float] = {}
    for key, vals in series.items():
        recent = vals[-(keep or HISTORY_WINDOW):]
        med = statistics.median(recent)
        if med:
            out[key] = (max(recent) - min(recent)) / med
    return out


def _classify_all(current: dict, baseline: dict, threshold: float,
                  spreads: dict | None = None, history_spreads: dict | None = None,
                  env: float | None = None) -> tuple[list[str], list[str]]:
    """逐项判定，返回 (确认回归消息, 不可判消息)。

    波动取三处里最大的那个：本轮采样波动、基线自己记录的波动、历史跨 run 抖动。

    只用本轮会漏判——基线若是单次采样留下的（老版本行为），它自带的抖动会被
    误当成"当前变慢了"。实测 2026-10-06 本机 4 次独立跑：process_pool 跨次抖
    14.4%、set_ms_per_op 抖 57.6%，而阈值是 30%——这些指标单靠相对阈判，
    红与不红就是抽签。
    """
    hits: list[str] = []
    unverified: list[str] = []
    base_spreads = baseline.get("_spreads") or {}
    hist = history_spreads or {}
    if env is None:
        env = _effective_env(current, baseline)
    for bench_name, cur_metrics in current.items():
        if str(bench_name).startswith("_") or not isinstance(cur_metrics, dict):
            continue
        base_metrics = baseline.get(bench_name)
        if not base_metrics or not isinstance(base_metrics, dict):
            continue
        for metric, cur_val in cur_metrics.items():
            if metric in REPORT_ONLY_METRICS:
                continue        # 只观测、不判红，理由见该常量注释
            if not isinstance(cur_val, (int, float)) or isinstance(cur_val, bool) or cur_val == 0:
                continue
            base_val = base_metrics.get(metric)
            if not isinstance(base_val, (int, float)) or isinstance(base_val, bool) or base_val == 0:
                continue
            key = f"{bench_name}.{metric}"
            candidates = [v for v in ((spreads or {}).get(key), base_spreads.get(key),
                                      hist.get(key)) if v is not None]
            spread = max(float(v) for v in candidates) if candidates else None
            found = classify_change(bench_name, metric, float(base_val), float(cur_val),
                                    threshold, spread, env)
            if not found:
                continue
            level, msg = found
            (hits if level == "REGRESSION" else unverified).append(msg)
    return hits, unverified


def _check_regression(current: dict, baseline: dict, threshold: float,
                      spreads: dict | None = None) -> list[str]:
    """对比当前结果与 baseline，返回**确认的**回归列表（不可判的项不在这里）。"""
    hits, _ = _classify_all(current, baseline, threshold, spreads)
    return hits


def _aggregate(samples: list[dict]) -> tuple[dict, dict]:
    """多样本 → (各项中位数, 各项相对波动)。

    取中位数不是取平均：一次被抢占的采样会把平均拖偏，中位数不会被单个
    离群点带走。波动 =（max-min)/median，它是"这项在当前机器上测得准吗"的
    唯一证据——门禁要么用它，要么就一直在噪声上判红。
    """
    medians: dict = {}
    spreads: dict = {}
    for bench in samples[0]:
        per_metric: dict[str, list[float]] = {}
        extras: dict[str, object] = {}
        for s in samples:
            got = s.get(bench)
            if not isinstance(got, dict):
                continue
            for key, val in got.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    per_metric.setdefault(key, []).append(float(val))
                else:
                    extras.setdefault(key, val)
        merged: dict = dict(extras)
        for key, vals in per_metric.items():
            med = statistics.median(vals)
            merged[key] = med
            if med and len(vals) > 1:
                spreads[f"{bench}.{key}"] = (max(vals) - min(vals)) / med
            if med == 0 and extras.get("error"):
                merged[key] = vals[0]
        medians[bench] = merged or {"error": "no samples"}
    return medians, spreads


def _flatten_for_history(results: dict) -> dict:
    """把嵌套 benchmark 结果压平为 {metric_name: value}，便于 JSONL 趋势存储。"""
    flat: dict[str, float] = {}
    for bench_name, metrics in results.items():
        if str(bench_name).startswith("_"):     # _env / _spreads 不是基准项
            continue
        if not isinstance(metrics, dict) or "error" in metrics:
            continue
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                flat[f"{bench_name}.{k}"] = float(v)
    return flat


def _run_all_benchmarks(verbose: bool = True) -> dict:
    """执行全部基准并返回结果 dict"""
    benchmarks = [
        ("HTML 正文提取 (selectolax vs regex)", bench_html_extraction),
        ("CacheManager 吞吐", bench_cache_throughput),
        ("并行执行 (Thread vs Process)", bench_parallel_execution),
        ("SSE 流式 chunk 传播", bench_streaming_overhead),
        ("TF-IDF 语义匹配", bench_tfidf),
    ]

    all_results = {}
    for name, fn in benchmarks:
        if verbose:
            print(f"\n{'─' * 50}")
            print(f"  {name}")
            print(f"{'─' * 50}")
        try:
            result = fn()
            all_results[name] = result
            if verbose:
                for k, v in result.items():
                    if v is None:
                        print(f"    {k:.<30} N/A")
                    elif isinstance(v, float):
                        if v > 100:
                            print(f"    {k:.<30} {v:,.1f}")
                        elif v < 0.01:
                            print(f"    {k:.<30} {v*1000:.3f} us")
                        else:
                            print(f"    {k:.<30} {v:.3f}")
                    else:
                        print(f"    {k:.<30} {v}")
        except Exception as e:
            print(f"    ERROR: {e}")
            all_results[name] = {"error": str(e)}
    return all_results


# 落盘路径（CI 的两把缓存键对齐的就是这两个文件）。提到模块级是为了让 main() 这条
# 出厂路径可测：测试把它们换成 tmp 下的文件，不必碰仓库里那份真基线。
BASELINE_PATH = Path(__file__).parent / "benchmark_results.json"
HISTORY_PATH = Path(__file__).parent / "benchmark_history.jsonl"


def main():
    print("=" * 70)
    print("Loom 性能基准测试")
    mode_label = "CI" if CI_MODE else ("快速" if QUICK else "完整")
    print(f"模式: {mode_label} | 迭代: {ITERATIONS}x")
    if CI_MODE:
        print(f"回归阈值: {REGRESSION_THRESHOLD:.0%}")
    print("=" * 70)

    all_results = _run_all_benchmarks(verbose=True)

    # 汇总
    print(f"\n{'=' * 70}")
    print("汇总")
    print(f"{'=' * 70}")

    # selectolax 加速比
    html_r = all_results.get("HTML 正文提取 (selectolax vs regex)", {})
    if html_r.get("speedup"):
        print(f"  selectolax 加速比: {html_r['speedup']:.1f}x")

    # 并行加速比
    par_r = all_results.get("并行执行 (Thread vs Process)", {})
    if par_r.get("thread_speedup"):
        print(f"  ThreadPool 加速比: {par_r['thread_speedup']:.2f}x")
    if par_r.get("process_speedup"):
        print(f"  ProcessPool 加速比: {par_r['process_speedup']:.2f}x")

    # 缓存吞吐
    cache_r = all_results.get("CacheManager 吞吐", {})
    if cache_r.get("set_ops_per_sec"):
        print(f"  Cache SET 吞吐: {cache_r['set_ops_per_sec']:,.0f} ops/s")
        print(f"  Cache GET(hit) 吞吐: {cache_r['get_hit_ops_per_sec']:,.0f} ops/s")

    # 流式开销
    stream_r = all_results.get("SSE 流式 chunk 传播", {})
    if stream_r.get("emit_ms_per_op"):
        print(f"  SSE chunk emit: {stream_r['emit_ms_per_op']:.4f} ms/op")

    print()

    # 导出 JSON
    output_path = BASELINE_PATH
    history_path = HISTORY_PATH

    if CI_MODE:
        # CI 模式：对比 baseline，检测回归
        baseline_path = BASELINE_PATH

        # 多样本：第一份已在上面跑过（给人看的那张表），其余静默补采。
        # 单样本比 30% 相对阈在共享 runner 上就是抛硬币——见 METRIC_FLOORS 的注释。
        samples = [all_results]
        for i in range(SAMPLES - 1):
            print(f"补采样本 {i + 2}/{SAMPLES} …")
            samples.append(_run_all_benchmarks(verbose=False))
        current, spreads = _aggregate(samples) if SAMPLES > 1 else (all_results, {})
        if spreads:
            print(f"\n{'=' * 70}")
            print(f"采样波动（{SAMPLES} 轮，(max-min)/median）")
            print(f"{'=' * 70}")
            for key in sorted(spreads, key=lambda k: -spreads[k])[:8]:
                print(f"  {key:.<58} {spreads[key]:.1%}")
            print(f"  （波动超过阈值 {REGRESSION_THRESHOLD:.0%} 的项，本轮无法判定回归）")

        fingerprint = _env_fingerprint()
        if baseline_path.exists():
            with open(baseline_path, encoding="utf-8") as f:
                baseline = json.load(f)
            history_spreads = _historical_spreads(history_path)
            print(f"\n{'=' * 70}")
            print(f"CI 回归检测 (阈值: {REGRESSION_THRESHOLD:.0%} | 样本: {SAMPLES} | "
                  f"绝对下限: {len(METRIC_FLOORS)} 项 | 只观测不判红: "
                  f"{len(REPORT_ONLY_METRICS)} 项 | 历史波动带: "
                  f"{'启用 ' + str(len(history_spreads)) + ' 项' if history_spreads else '样本不足（<5 轮）'}）")
            print(f"{'='* 70}")
            env = _effective_env(current, baseline)
            hits, unverified = _classify_all(current, baseline, REGRESSION_THRESHOLD,
                                             spreads, history_spreads, env)
            if env != 1.0:
                print(f"整体环境因子 {env:.2f}×：本轮多数指标与基线同向偏移，"
                      "已按该因子校正后再判定（否则 runner 抖动会被读成回归）")

            base_env = baseline.get("_env") or {}
            changed = {k: (base_env.get(k), fingerprint.get(k))
                       for k in ("system", "machine", "python", "cpu_count")
                       if base_env.get(k) not in (None, fingerprint.get(k))}
            if changed and (hits or unverified):
                # 基线是别的机器/别的 Python 跑出来的：相对比不再有可比性。
                # 判红会把平台迁移变成"性能回归"，直接忽略又会漏掉真回归，
                # 所以降为 UNVERIFIED 并要求重立基线（refresh-baseline）。
                note = (f"基线环境指纹与当前不同 {changed}——相对比较不可信，"
                        "请用 refresh-baseline 重立基线")
                hits = [f"{h} —— 改判不可判：{note}" for h in hits]
                hits, unverified = [], hits + unverified
            noisy = (fingerprint.get("load1") or 0) > (fingerprint.get("cpu_count") or 1)
            if noisy and (hits or unverified):
                note = (f"当前机器 1 分钟负载 {fingerprint['load1']} 超过核数 "
                        f"{fingerprint['cpu_count']}，微基准在此刻不可信")
                hits = [f"{h} —— 改判不可判：{note}" for h in hits]
                hits, unverified = [], hits + unverified

            for msg in unverified:
                print(f"  UNVERIFIED:{msg}")
            if hits:
                print(f"\nFAILED: {len(hits)} 项确认性能回归"
                      f"（超阈值、超绝对下限、且超过采样波动的 2 倍）:")
                for r in hits:
                    print(r)
                sys.exit(1)
            if unverified:
                print(f"\nPASSED(with warnings): 无确认回归；{len(unverified)} 项因噪声不可判，"
                      "已逐条列在上方——这不是『通过』，是『这台机器此刻测不出答案』，"
                      "需要时用 --samples 5 或 refresh-baseline 复核")
            else:
                print("PASSED: 无性能回归")
            # 滚动更新 baseline：CI 缓存中的基线始终对齐最近一次通过的 main 运行
            all_results = current
            all_results["_env"] = fingerprint
            all_results["_spreads"] = spreads
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(all_results, f, indent=2, default=str, ensure_ascii=False)
        else:
            print("WARNING: 无 baseline 文件，跳过回归检测")
            # 首次运行，写入 baseline
            all_results["_env"] = fingerprint
            all_results["_spreads"] = spreads
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(all_results, f, indent=2, default=str, ensure_ascii=False)
            print(f"已写入初始 baseline: {output_path}")

        # ── 趋势记录：每次 CI 跑完追加到 JSONL 历史文件 ──
        try:
            flat = _flatten_for_history(all_results)
            flat["_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(flat, default=str, ensure_ascii=False) + "\n")
            # 有界：历史只当噪声尺子用，留最近 HISTORY_KEEP 轮就够（跨 run 波动带
            # 取的是最近 20 轮）。无限追加会让它变成没人读的日志。
            lines = history_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > HISTORY_KEEP:
                history_path.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n",
                                        encoding="utf-8")
            print(f"趋势已追加: {history_path}（保留最近 {min(len(lines), HISTORY_KEEP)} 轮）")
        except Exception as e:
            print(f"趋势记录失败（非阻塞）: {e}")

    elif UPDATE_BASELINE:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, default=str, ensure_ascii=False)
        print(f"Baseline 已更新: {output_path}")
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, default=str, ensure_ascii=False)
        print(f"结果已导出: {output_path}")


if __name__ == "__main__":
    main()
