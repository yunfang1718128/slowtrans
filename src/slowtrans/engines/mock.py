"""离线测试引擎：不联网、不花钱，把原文加上标记返回。

单独成文件是因为它和真实适配器的职责完全不同——
把测试替身和 HTTP 客户端放一起，容易让人误以为 mock 也走网络。
"""
from __future__ import annotations

from .base import Context, Item, Result, Usage


class MockEngine:
    """离线测试用：把原文加上标记，验证结构与断点，不花钱。"""

    def __init__(self, fail_at: set[int] | None = None):
        self.id = "mock:echo"
        self.batch_size = 4
        self.fail_at = fail_at or set()
        self.calls = 0

    def translate(self, items: list[Item], ctx: Context) -> tuple[list[Result], Usage]:
        results: list[Result] = []
        usage = Usage()
        for it in items:
            self.calls += 1
            if it.key[1] in self.fail_at:
                results.append(Result(it.key, "", ok=False, error="mock failure"))
                continue
            text = f"[译]{it.text}"
            results.append(Result(it.key, text))
            usage.prompt_tokens += 10
            usage.completion_tokens += 10
        return results, usage

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        return "- mock 风格规则"
