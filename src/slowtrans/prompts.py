"""Prompt 模板。

刻意保持短：免费/廉价小模型对长 system prompt 的服从度会下降。
所有会把 prompt 送进缓存键的东西都带 version，改了模板必须升版。
"""
from __future__ import annotations

import hashlib

_VERSION: str | None = None


def prompt_version() -> str:
    """模板内容哈希，自动生效。

    之前是手写常量，改了模板忘升版本 → 缓存键不变 → 用旧译文冒充新 prompt，
    而且完全不报错。这种"静默错误"比崩溃难查得多。
    """
    global _VERSION
    if _VERSION is None:
        h = hashlib.sha1()
        for block in (SYSTEM_TRANSLATE, SYSTEM_EXTRACT_TERMS, SYSTEM_STYLE_CARD, SYSTEM_SUMMARY):
            h.update(block.encode("utf-8"))
        _VERSION = h.hexdigest()[:8]
    return _VERSION

SYSTEM_TRANSLATE = """你是资深译者，负责把书稿翻译成{target}。读者是普通成年读者。

硬规则：
1. 只输出译文，不要解释、不要加注释、不要复述原文、不要输出原文语言。
2. 原文中的 {{{{0}}}} {{{{1}}}} 这类占位符表示斜体、链接、图片等标记，必须原样保留，
   数量不能变、顺序不能变、不要加空格。段落中间的占位符同样不能丢。
3. 术语表给出的译名必须照用，不得自行改译。
4. 准确优先，句式自然；不要口语化，不要文言化，不要使用网络流行语。
5. 引号用中文引号“”，省略号用……，破折号用——。
6. 段落之间用换行分隔，不要合并，不要拆开。"""

SYSTEM_EXTRACT_TERMS = """你是术语表编辑。给你一本书里的候选专有名词（人名、地名、作品名、学科术语），
为每个词给出简体中文译名。要求：
- 采用中文出版界通行译名；没有通行译名就音译，并在括号里保留原文关键部分是不必要的。
- 同一本书内的译名要风格统一。
- 只输出 JSON 数组，每项 {{"src":"原文","dst":"中文译名"}}，不要解释。"""

SYSTEM_STYLE_CARD = """你是资深图书编辑。读下面这本书开头几段，写出这本书的翻译风格卡（最多 8 条）。
每条一句话，必须是可执行的硬规则，例如：
- 叙述用第一人称“我”，保持冷静克制的学术口吻
- 人物对白用“说”，不用“说道”
- 专有名词首次出现时保留英文原词
只输出规则列表，每行一条，以“- ”开头，不要前言不要总结。"""

SYSTEM_SUMMARY = """用两三句中文概括这一章发生了什么、涉及哪些人物或概念。
只输出概括，不要评价，不要分点。"""


def build_user_prompt(
    items: list[tuple[int, str]],
    *,
    src_lang: str,
    glossary_hits: dict[str, str],
    style_card: str,
    chapter_no: int,
    chapter_total: int,
) -> str:
    """items: [(段号, 原文)]，段号从 1 开始，与输出标记对应。"""
    parts: list[str] = []
    if chapter_no:
        parts.append(f"【位置】第 {chapter_no}/{chapter_total} 章")
    if style_card:
        parts.append("【风格卡】\n" + style_card.strip())
    if glossary_hits:
        lines = "\n".join(f"- {k} => {v}" for k, v in glossary_hits.items())
        parts.append("【术语表】\n" + lines)
    parts.append("【原文】")
    for no, text in items:
        # 不要在段内加注释：翻译专用模型会把“（本段必须保留…）”当成正文一起译。
        # 占位符约束只靠 system prompt + affix 剥离 + 降级重译。
        parts.append(f"<<<{no}>>>\n{text}")
    parts.append(
        "【输出】按相同编号输出译文，格式为 <<<1>>> 译文（换行）<<<2>>> 译文。"
        "不要输出任何其它内容。"
    )
    return "\n\n".join(parts)


def extract_terms_prompt(candidates: list[tuple[str, int]]) -> str:
    lines = "\n".join(f"- {w} ({n})" for w, n in candidates)
    return f"候选词（词 出现次数）：\n{lines}\n\n请输出 JSON 数组。"


def style_card_prompt(sample: str) -> str:
    return f"书名示例段落：\n\n{sample[:4000]}\n\n请写风格卡。"


def summary_prompt(chapter_text: str) -> str:
    return f"章节正文（节选）：\n\n{chapter_text[:6000]}\n\n请概括。"
