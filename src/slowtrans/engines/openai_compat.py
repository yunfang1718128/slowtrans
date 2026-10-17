"""OpenAI 兼容协议的引擎适配器。

一个适配器覆盖硅基流动 / 智谱 / DeepSeek / 百炼 / Ollama / vLLM —— 因为它们
都用同一套 /chat/completions。不要为每家公司写 SDK，那是维护地狱。

廉价 API 的真实故障都在这儿兜：
- 429 与 5xx：可重试，交给 pipeline 退避
- 空 content（思考型模型把答案放进了 reasoning_content）：判失败，降级
- 批量输出缺段/乱序：解析不出来就自动退回逐段
- finish_reason=length：静默截断，必须判失败而不是当成译文
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from ..prompts import SYSTEM_TRANSLATE, build_user_prompt
from .base import Context, EngineError, Item, Result, Usage

NUM_RE = re.compile(r"<<<\s*(\d+)\s*>>>")


class OpenAICompatEngine:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        *,
        temperature: float = 0.3,
        timeout_s: int = 180,
        max_output_tokens: int = 2048,
        extra_body: dict | None = None,
        batch_size: int = 4,
        rps: float = 0.0,
    ):
        if not api_key:
            raise SystemExit(f"缺少 {provider} 的 API key（检查环境变量）")
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.extra_body = extra_body or {}
        self.batch_size = max(1, batch_size)
        self.min_interval = (1.0 / rps) if rps and rps > 0 else 0.0
        self._last_call = 0.0
        self.id = f"{provider}:{model}"

    # ---------------- 底层 ----------------
    def _throttle(self) -> None:
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.monotonic()

    def _chat(self, messages: list[dict], max_tokens: int, temperature: float | None = None) -> tuple[str, Usage]:
        self._throttle()
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        body.update(self.extra_body)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            retryable = exc.code == 429 or exc.code >= 500 or exc.code in (408, 409)
            raise EngineError(f"HTTP {exc.code}: {detail}", retryable=retryable, status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EngineError(f"网络错误: {exc}", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise EngineError(f"返回不是 JSON: {exc}", retryable=True) from exc

        choices = payload.get("choices") or []
        if not choices:
            raise EngineError(f"没有 choices: {str(payload)[:300]}", retryable=True)

        choice = choices[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        finish = choice.get("finish_reason")
        usage_raw = payload.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )

        if not content:
            if finish == "length":
                raise EngineError("输出被 max_tokens 截断且为空", retryable=True)
            raise EngineError(
                f"模型返回空内容（reasoning={len(reasoning)} 字符，finish={finish}）",
                retryable=True,
            )
        if finish == "length":
            raise EngineError("输出被 max_tokens 截断", retryable=True)
        return content, usage

    # ---------------- 对上层 ----------------
    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        content, _ = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return content

    def translate(self, items: list[Item], ctx: Context) -> tuple[list[Result], Usage]:
        total = Usage()
        results: list[Result] = []
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            if len(batch) == 1:
                res, use = self._translate_one(batch[0], ctx)
                results.append(res)
                total.add(use)
                continue
            try:
                out, use = self._translate_batch(batch, ctx)
                total.add(use)
                results.extend(out)
            except EngineError as exc:
                if not exc.retryable:
                    raise
                # 批量格式不听话是常态：整批降级逐段，别让一批毁一章
                for it in batch:
                    res, use = self._translate_one(it, ctx)
                    results.append(res)
                    total.add(use)
        return results, total

    def _translate_batch(self, batch: list[Item], ctx: Context) -> tuple[list[Result], Usage]:
        numbered = [(i + 1, it.text) for i, it in enumerate(batch)]
        hits = self._glossary_hits(batch, ctx.glossary)
        system = SYSTEM_TRANSLATE.format(target=_lang_name(ctx.tgt_lang))
        user = build_user_prompt(
            numbered,
            src_lang=ctx.src_lang,
            glossary_hits=hits,
            style_card=ctx.style_card,
            chapter_no=ctx.chapter_no,
            chapter_total=ctx.chapter_total,
        )
        content, usage = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=self.max_output_tokens,
        )
        parsed = _parse_numbered(content)
        if parsed is None or len(parsed) != len(batch):
            raise EngineError(f"批量输出无法解析为 {len(batch)} 段", retryable=True)
        out: list[Result] = []
        for i, it in enumerate(batch):
            text = parsed.get(i + 1, "").strip()
            if not text:
                out.append(Result(it.key, "", ok=False, error="批量中该段为空"))
            else:
                out.append(Result(it.key, text))
        return out, usage

    def _translate_one(self, item: Item, ctx: Context) -> tuple[Result, Usage]:
        hits = self._glossary_hits([item], ctx.glossary)
        system = SYSTEM_TRANSLATE.format(target=_lang_name(ctx.tgt_lang))
        user = build_user_prompt(
            [(1, item.text)],
            src_lang=ctx.src_lang,
            glossary_hits=hits,
            style_card=ctx.style_card,
            chapter_no=ctx.chapter_no,
            chapter_total=ctx.chapter_total,
        )
        try:
            content, usage = self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=self.max_output_tokens,
            )
        except EngineError as exc:
            if exc.status == 429 or not exc.retryable:
                raise
            return Result(item.key, "", ok=False, error=str(exc)), Usage()
        parsed = _parse_numbered(content)
        if parsed and 1 in parsed:
            text = parsed[1]
        else:
            text = content.strip()
            # 单段请求时模型常直接给译文，去掉可能残留的编号标记
            text = NUM_RE.sub("", text).strip()
        return Result(item.key, text, ok=bool(text)), usage

    @staticmethod
    def _glossary_hits(items: list[Item], glossary: dict[str, str]) -> dict[str, str]:
        """只注入命中的术语：全表塞进去会白白吃掉上下文。"""
        if not glossary:
            return {}
        hits: dict[str, str] = {}
        joined = "\n".join(it.text for it in items).lower()
        for src, dst in glossary.items():
            if src.lower() in joined:
                hits[src] = dst
                if len(hits) >= 12:
                    break
        return hits


def _lang_name(code: str) -> str:
    return {
        "zh": "简体中文", "zh-cn": "简体中文", "zh-tw": "繁体中文",
        "ja": "日语", "en": "英语", "ko": "韩语",
    }.get(code.lower(), code)


def _parse_numbered(text: str) -> dict[int, str] | None:
    """解析 <<<n>>> 分段输出。容忍模型加粗、多说一句、编号后带冒号。"""
    matches = list(NUM_RE.finditer(text))
    if not matches:
        return None
    out: dict[int, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        body = body.lstrip("：: ").strip()
        out[int(m.group(1))] = body
    return out


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
