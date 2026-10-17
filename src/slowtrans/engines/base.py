"""引擎抽象。

所有客户端（CLI / HTTP / AstrBot 插件 / 面板）都只面对这一层，
换模型不牵动流水线，换流水线不影响模型适配。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class EngineError(Exception):
    def __init__(self, message: str, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class Item:
    """一个待译分段。key 是 (chapter_idx, block_idx, seg_idx)。"""
    key: tuple[int, int, int]
    text: str


@dataclass
class Result:
    key: tuple[int, int, int]
    text: str
    ok: bool = True
    error: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens


@dataclass
class Context:
    src_lang: str = "en"
    tgt_lang: str = "zh-CN"
    glossary: dict[str, str] = field(default_factory=dict)
    style_card: str = ""
    chapter_no: int = 0
    chapter_total: int = 0
    summary: str = ""


class Engine(Protocol):
    id: str

    def translate(self, items: list[Item], ctx: Context) -> tuple[list[Result], Usage]:
        ...

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        """术语抽取、风格卡、摘要这类一次性调用。"""
        ...


def estimate_tokens(text: str, lang: str = "en") -> int:
    """粗估，只用于预算和日志；真实用量以 API 返回的 usage 为准。"""
    if lang.startswith("zh") or lang.startswith("ja"):
        return max(1, int(len(text) / 1.5))
    return max(1, int(len(text) / 3.5))
