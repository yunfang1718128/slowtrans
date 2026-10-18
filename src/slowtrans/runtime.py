"""配置 → 运行参数的唯一映射入口。

以前 CLI 和 API 各自拼 `RunOptions` / `Engine`：
- CLI 用 argparse 的默认值（retries=4, backoff=8）
- API 读 `[scheduler]` 里的 budget，但没读 retries/backoff
- `[epub] strip_ruby_rt` / `force_horizontal` 压根没传下去

结果是**同一个 config.toml，两个入口行为不同**，而且写进配置的开关不一定生效。
现在两边都走这里：显式覆盖 > config > 内置默认。
"""
from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path

from .config import Config
from .engines.openai_compat import OpenAICompatEngine


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------- 时间窗
def parse_window(spec: str) -> tuple[int, int] | None:
    """'00:00-08:00' -> (0, 480) 分钟。支持跨午夜（22:00-06:00）。"""
    if not spec or "-" not in spec:
        return None
    left, right = spec.split("-", 1)
    try:
        def to_min(x: str) -> int:
            h, m = x.strip().split(":")
            return int(h) * 60 + int(m)

        start, end = to_min(left), to_min(right)
    except ValueError:
        return None
    if not (0 <= start < 1440 and 0 <= end <= 1440):
        return None
    return start, end


def in_window(spec: str, now: datetime | None = None) -> bool:
    w = parse_window(spec)
    if not w:
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = w
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def seconds_until_window(spec: str, now: datetime | None = None) -> int:
    """距离窗口开始还有多少秒；已在窗口内返回 0。"""
    w = parse_window(spec)
    if not w:
        return 0
    now = now or datetime.now()
    if in_window(spec, now):
        return 0
    cur = now.hour * 3600 + now.minute * 60 + now.second
    target = w[0] * 60
    if target <= cur:
        target += 86400
    return target - cur


# ---------------------------------------------------------------- 引擎
def engine_from_config(
    cfg: Config,
    provider: str,
    model: str,
    *,
    batch_size: int | None = None,
    rps: float | None = None,
    timeout_s: int | None = None,
    temperature: float | None = None,
    extra_body: dict | None = None,
) -> OpenAICompatEngine:
    p = cfg.provider(provider)
    ecfg = cfg.engine_cfg
    sched = cfg.sched_cfg
    return OpenAICompatEngine(
        provider=provider,
        model=model,
        api_key=p.key(),
        base_url=p.base_url,
        temperature=float(ecfg.get("temperature", 0.3)) if temperature is None else temperature,
        timeout_s=int(ecfg.get("request_timeout_s", 60)) if timeout_s is None else timeout_s,
        max_output_tokens=int(ecfg.get("max_output_tokens", 3072)),
        batch_size=int(batch_size or ecfg.get("batch_size", 4)),
        rps=float(rps if rps is not None else (sched.get("rps", 0) or 0)),
        extra_body=extra_body,
    )


# ---------------------------------------------------------------- 运行参数
def options_from_config(
    cfg: Config,
    *,
    src: str | Path,
    out: str | Path,
    lang_from: str = "en",
    lang_to: str = "zh-CN",
    bilingual: bool | None = None,
    batch_size: int | None = None,
    max_retries: int | None = None,
    backoff_base: float | None = None,
    backoff_max: float | None = None,
    budget_cny: float | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    max_segment_chars: int | None = None,
    limit_segments: int = 0,
    analyze: bool = True,
    dry_run: bool = False,
    emit_mono: bool = True,
):
    from .pipeline import RunOptions

    sched = cfg.sched_cfg
    epub = cfg.epub_cfg
    ecfg = cfg.engine_cfg

    def pick(value, default):
        return default if value is None else value

    return RunOptions(
        src=Path(src),
        out=Path(out),
        lang_from=lang_from,
        lang_to=lang_to,
        bilingual=pick(bilingual, bool(epub.get("bilingual", True))),
        max_segment_chars=int(pick(max_segment_chars, epub.get("max_segment_chars", 1200))),
        batch_size=int(pick(batch_size, ecfg.get("batch_size", 4))),
        limit_segments=limit_segments,
        analyze=analyze,
        max_retries=int(pick(max_retries, sched.get("max_retries", 3))),
        backoff_base=float(pick(backoff_base, sched.get("backoff_base_s", 5))),
        backoff_max=float(pick(backoff_max, sched.get("backoff_max_s", 300))),
        budget_cny=float(pick(budget_cny, sched.get("budget_cny", 0) or 0)),
        price_in=float(pick(price_in, ecfg.get("price_in", 0) or 0)),
        price_out=float(pick(price_out, ecfg.get("price_out", 0) or 0)),
        strip_ruby=bool(epub.get("strip_ruby_rt", True)),
        force_horizontal=bool(epub.get("force_horizontal", True)),
        window=str(sched.get("window", "") or ""),
        breaker_failures=int(sched.get("breaker_failures", 8)),
        breaker_cooldown_s=float(sched.get("breaker_cooldown_s", 600)),
        emit_mono=emit_mono,
        dry_run=dry_run,
        owner=owner_id(),
    )
