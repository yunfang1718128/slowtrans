"""术语候选抽取（零成本、纯本地）。

只做"值得让模型定名"的候选筛选，不追求 NER 精度：
- 英文：大写开头的多词串 / 全大写缩写，排除句首词与高频虚词
- 日文：连续汉字串，按频次筛

真正定名交给一次批量 LLM 调用（几十个词一次，成本可忽略）。
"""
from __future__ import annotations

import re
from collections import Counter

EN_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "when", "while", "because",
    "this", "that", "these", "those", "there", "then", "than", "with", "without",
    "from", "into", "onto", "over", "under", "about", "after", "before",
    "however", "therefore", "thus", "also", "only", "even", "most", "more",
    "some", "such", "each", "every", "other", "another", "many", "much",
    "one", "two", "three", "first", "second", "third", "chapter", "part",
    "figure", "table", "note", "notes", "index", "bibliography", "contents",
    "yes", "no", "not", "can", "could", "would", "should", "may", "might",
    "must", "will", "shall", "have", "has", "had", "was", "were", "are",
    "is", "be", "been", "being", "do", "does", "did", "he", "she", "they",
    "we", "you", "it", "his", "her", "their", "our", "your", "its",
}

EN_PROPER = re.compile(r"\b([A-Z][a-z]{2,}(?:[ ’'\-][A-Z][a-z]{2,}){0,3})\b")
EN_ACRONYM = re.compile(r"\b([A-Z]{2,6})\b")
SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
JA_KANJI = re.compile(r"[\u4e00-\u9fff]{2,8}")


EN_GENERIC = {
    "university", "press", "street", "road", "chapter", "book", "books",
    "part", "volume", "editor", "author", "review", "journal", "society",
    "company", "group", "school", "college", "institute", "world", "story",
    "stories", "novel", "literature", "century", "american", "british",
    "english", "european", "history", "theory", "criticism", "edition",
}


def extract_candidates(texts: list[str], lang: str, top_n: int = 48, min_count: int = 3) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    phrases: Counter[str] = Counter()
    if lang.lower().startswith("en"):
        for text in texts:
            for sentence in SENT_SPLIT.split(text):
                words = sentence.split()
                if not words:
                    continue
                # 句首大写是语法现象：从第二个词开始匹配，并把整句接回去，
                # 这样多词专名（Oxford University Press）才抽得出来
                head = len(words[0])
                body = sentence[head:]
                for m in EN_PROPER.finditer(body):
                    cand = m.group(1).strip()
                    if len(cand) < 3 or cand.lower() in EN_STOP:
                        continue
                    if " " in cand:
                        phrases[cand] += 1
                    else:
                        counter[cand] += 1
                for m in EN_ACRONYM.finditer(body):
                    cand = m.group(1)
                    if cand.lower() not in EN_STOP:
                        counter[cand] += 1
    else:
        for text in texts:
            for m in JA_KANJI.finditer(text):
                counter[m.group(0)] += 1

    out: list[tuple[str, int]] = []
    covered: set[str] = set()
    for phrase, n in phrases.most_common():
        if n < 2:
            continue
        out.append((phrase, n))
        covered.update(w.lower() for w in phrase.split())
    for word, n in counter.most_common():
        if n < max(min_count, 3):
            continue
        if word.lower() in EN_GENERIC or word.lower() in covered:
            continue
        out.append((word, n))
    return out[:top_n]
