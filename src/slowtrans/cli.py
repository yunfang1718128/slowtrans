"""命令行入口。

刻意保持三个子命令：
  run     —— 一条命令跑完整流程（默认全自动）
  inspect —— 只看结构，不产生任何调用
  serve   —— 起 HTTP 桥接（给 AstrBot 插件和本地面板用）
  convert —— 预留位，明确返回"尚未实现"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import load_config, parse_engine_spec
from .db import DB, job_id_for
from .engines.openai_compat import MockEngine, OpenAICompatEngine
from .pipeline import Pipeline, RunOptions


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc and enc not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _load_dotenv() -> None:
    for name in (".env", "../.env"):
        p = Path(name)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return


def _build_engine(cfg, spec: str, args):
    provider, model = parse_engine_spec(spec)
    if provider == "mock":
        return MockEngine(), provider, model
    p = cfg.provider(provider)
    engine = OpenAICompatEngine(
        provider=provider,
        model=model,
        api_key=p.key(),
        base_url=p.base_url,
        temperature=args.temperature,
        timeout_s=args.timeout,
        batch_size=args.batch,
        rps=args.rps,
        max_output_tokens=int(cfg.engine_cfg.get("max_output_tokens", 3072)),
        extra_body=json.loads(args.extra_body) if args.extra_body else None,
    )
    return engine, provider, model


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"找不到输入文件：{src}")
    out = Path(args.out) if args.out else src.with_name(src.stem + f".{args.to}.epub")

    spec = args.engine or f"{cfg.engine_cfg.get('provider')}:{cfg.engine_cfg.get('model')}"
    engine, provider, model = _build_engine(cfg, spec, args)

    opts = RunOptions(
        src=src,
        out=out,
        lang_from=args.src_lang,
        lang_to=args.to,
        bilingual=not args.mono,
        max_segment_chars=args.max_chars,
        batch_size=args.batch,
        limit_segments=args.limit,
        analyze=not args.no_analyze,
        max_retries=args.retries,
        backoff_base=args.backoff,
        budget_cny=args.budget,
        price_in=args.price_in,
        price_out=args.price_out,
        dry_run=args.dry_run,
    )
    db = DB(Path(args.data) / "slowtrans.db")
    pipe = Pipeline(opts, db, engine, provider, model)
    print(f"job {pipe.jid} | {provider}:{model} | {opts.lang_from} -> {opts.lang_to}")
    stats = pipe.run()
    if not args.dry_run:
        print(
            f"统计：新译 {stats.segments_done}，缓存命中 {stats.segments_cached}，"
            f"待复核 {stats.segments_review}，失败 {stats.segments_failed}，"
            f"API 调用 {stats.api_calls}，tokens {stats.usage.prompt_tokens}/"
            f"{stats.usage.completion_tokens}"
        )
    return 0


def cmd_inspect(args) -> int:
    import zipfile

    from .epub.ocf import read_package, spine_items
    from .epub.segment import segment_chapter

    with zipfile.ZipFile(args.src) as z:
        pkg = read_package(z)
        print(f"标题: {pkg.title}\n语言: {pkg.language}\nOPF : {pkg.opf_path}")
        total = 0
        for idx, item in spine_items(pkg):
            data = z.read(item.href)
            try:
                _, cs = segment_chapter(data, item.href, max_chars=args.max_chars)
            except Exception as exc:
                print(f"  [{idx}] {item.href} 解析失败: {exc}")
                continue
            chars = sum(len(s.template) for s in cs.segments)
            total += len(cs.segments)
            print(f"  [{idx:02d}] {item.href:<42} 块 {len(cs.blocks):>4} 段 {len(cs.segments):>4} 字符 {chars:>7}")
        print(f"合计 {total} 段")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api.app import create_app

    app = create_app(config_path=args.config, data_dir=args.data)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_convert(args) -> int:
    print(
        "格式转换尚未实现（feature=convert, status=not_implemented）。\n"
        "当前版本只支持 epub -> epub 翻译。转换接口已预留：\n"
        "  POST /api/v1/jobs {\"type\":\"convert\", ...} -> 501\n"
        "实现计划见 README 的 Roadmap。",
        file=sys.stderr,
    )
    return 3


def cmd_status(args) -> int:
    db = DB(Path(args.data) / "slowtrans.db")
    rows = db.conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 20").fetchall()
    if not rows:
        print("还没有任务")
        return 0
    for r in rows:
        print(
            f"{r['id']}  {r['state']:<12} {r['done_segments']}/{r['total_segments']:<5} "
            f"{r['provider']}:{r['model']}  {Path(r['src_path']).name}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slowtrans", description="低内存、可中断、慢速省钱的 EPUB 翻译器")
    p.add_argument("--version", action="version", version=f"slowtrans {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", default=None, help="TOML 配置文件")
        sp.add_argument("--data", default="data", help="状态库与产物目录")

    r = sub.add_parser("run", help="跑完整流程")
    r.add_argument("--src", required=True)
    r.add_argument("--out", default=None)
    r.add_argument("--from", dest="src_lang", default="en")
    r.add_argument("--to", default="zh-CN")
    r.add_argument("--engine", default=None, help="provider:model，如 siliconflow:Qwen/Qwen2.5-7B-Instruct")
    r.add_argument("--batch", type=int, default=4)
    r.add_argument("--limit", type=int, default=0, help="只入库前 N 段（调试用）")
    r.add_argument("--max-chars", type=int, default=1200)
    r.add_argument("--mono", action="store_true", help="只输出译文（默认双语对照）")
    r.add_argument("--no-analyze", action="store_true", help="跳过术语/风格分析")
    r.add_argument("--dry-run", action="store_true", help="只分段不调用 API")
    r.add_argument("--temperature", type=float, default=0.3)
    r.add_argument("--timeout", type=int, default=90)
    r.add_argument("--retries", type=int, default=4)
    r.add_argument("--backoff", type=float, default=8.0)
    r.add_argument("--rps", type=float, default=0.0)
    r.add_argument("--budget", type=float, default=0.0, help="预算上限（元），0=不限")
    r.add_argument("--price-in", type=float, default=0.0, help="输入价格 元/百万token")
    r.add_argument("--price-out", type=float, default=0.0, help="输出价格 元/百万token")
    r.add_argument("--extra-body", default=None, help='额外请求体 JSON，如 {"enable_thinking":false}')
    common(r)
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("inspect", help="查看 epub 结构与分段统计")
    i.add_argument("--src", required=True)
    i.add_argument("--max-chars", type=int, default=1200)
    i.set_defaults(func=cmd_inspect)

    s = sub.add_parser("serve", help="起 HTTP 桥接服务（仅回环）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    common(s)
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("convert", help="格式转换（尚未实现）")
    c.add_argument("--src", required=True)
    c.add_argument("--out", required=True)
    common(c)
    c.set_defaults(func=cmd_convert)

    st = sub.add_parser("status", help="列出任务")
    common(st)
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    _load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
