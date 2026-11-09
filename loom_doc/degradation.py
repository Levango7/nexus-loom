"""文档降级产物：writer 承认"某一节没内容"时用的那套标记。

这些字符串是 writer 与 quality_gate 之间的**契约**而不是文案：门禁用它们判断
"这份产出到底有没有内容"。此前两处各写一份，于是漂移过一次 —— writer 改发
「降级声明」与「（暂无可用的相关内容）」，而门禁的名单里只有
「未采集到可整合的搜索结果」与「无待整合内容」，结果一份 4/5 章节是占位符的
文档拿到了 98.8 pass 并正常落盘（2026-10-06 实测）。故字符串收敛到本模块单处定义。
"""

from __future__ import annotations

import re

# 整份文档没有可整合素材时的自述
EMPTY_RESULT_DOC = "未采集到可整合的搜索结果"
NO_SOURCE = "无待整合内容"

# 单个章节提取不到足够内容时的占位行（成对出现：一节一行）
SECTION_PLACEHOLDER = "*（暂无可用的相关内容）*"

# 无 LLM 且存在占位章节时，writer 在文档头部插入的自述块
DEGRADE_BANNER = "> ⚠️ **降级声明**"

# 无 LLM Key 但拿到了 web 素材（文章 / 检索摘要）时，writer 拒绝把素材拼贴当交付物、
# 改交的自述。web 素材的"整合"本身就是 LLM 的活，keyless 拼贴=把检索原文当成品
# 出厂（FP-1b 的源头）；KB 抽取式草稿是另一条约（内容本就叫摘录），不在此列。
KEYLESS_REFUSAL = "无 LLM API Key，拒绝输出原始素材拼贴稿"

# 门禁据此判失败：占位符标记，命中即视为"没有产出"
PLACEHOLDER_MARKERS = (EMPTY_RESULT_DOC, NO_SOURCE, KEYLESS_REFUSAL)

# ── 抓取层中间格式泄漏 ────────────────────────────────────
# `fetcher._save_article` 落盘的每篇文章是这个形状：
#     标题: … / 来源: … / 下载时间: YYYY-MM-DD HH:MM:SS / 一条 60 连等号 / 正文…
# 那是**素材的磁盘格式**，不是成品正文。writer 在素材不足时会把整块原样粘进文档，
# 于是交付物里出现"下载时间:"和 60 连等号线 —— 2026-10-07 实测两份这种文档
# （本机 28.4 KB、CI 21.9 KB）都拿到 95.5 pass，且被 CI 记成 OUTPUT FIDELITY OK。
FETCH_TIMESTAMP_RE = re.compile(r"^下载时间: \d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", re.MULTILINE)
FETCH_RULE_RE = re.compile(r"^={60}$", re.MULTILINE)

_SECTION_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)


def raw_fetch_block_count(content: str) -> int:
    """正文里泄漏的抓取块签名数（取时间戳行与 60 连等号线中的较大者）。"""
    return max(len(FETCH_TIMESTAMP_RE.findall(content)), len(FETCH_RULE_RE.findall(content)))



def placeholder_section_count(content: str) -> int:
    """正文里以占位符交出去的章节数。"""
    return content.count(SECTION_PLACEHOLDER)


def section_count(content: str) -> int:
    """二级标题数，即文档声称要交付的章节数。"""
    return len(_SECTION_HEADING_RE.findall(content))


def placeholder_ratio(content: str) -> float:
    """占位章节占比；没有章节时按"整份都是占位"计（返回 1.0 而非除零崩掉）。"""
    sections = section_count(content)
    placeholders = placeholder_section_count(content)
    if placeholders == 0:
        return 0.0
    if sections == 0:
        return 1.0
    return placeholders / sections
