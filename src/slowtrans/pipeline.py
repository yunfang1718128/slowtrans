"""流水线：分段入库 -> 术语/风格 -> 逐批翻译 -> 校验 -> 组装。

核心不变量：
- 每个分段只依赖 (src_text, glossary_version, style_version, model, prompt_version)
- 因此任何一次重跑都是幂等的，改术语只重译命中段
- 单并发串行：术语表和前情摘要是顺序生长的，并发反而会破坏一致性
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .db import DB, Segment, job_id_for, text_hash
from .engines.base import Context, Engine, EngineError, Item, Result, Usage, estimate_tokens
from .epub.ocf import local, read_package, spine_items
from .epub.package import ChapterPlan, WriteReport, write_epub
from .epub.segment import PH_RE, detach_affixes, placeholders_of, segment_chapter
from .glossary import extract_candidates


@dataclass
class RunOptions:
    src: Path
    out: Path
    lang_from: str = "en"
    lang_to: str = "zh-CN"
    bilingual: bool = True
    max_segment_chars: int = 1200
    batch_size: int = 4
    limit_segments: int = 0
    analyze: bool = True
    max_retries: int = 4
    backoff_base: float = 8.0
    backoff_max: float = 300.0
    budget_cny: float = 0.0
    price_in: float = 0.0     # 元 / 百万 token；免费模型填 0
    price_out: float = 0.0
    allow_partial: bool = True
    dry_run: bool = False
    log_every: int = 1


@dataclass
class Stats:
    chapters: int = 0
    segments_total: int = 0
    segments_done: int = 0
    segments_cached: int = 0
    segments_review: int = 0
    segments_failed: int = 0
    api_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    started: float = field(default_factory=time.time)
    glossary_terms: int = 0

    @property
    def cost(self) -> float:
        return 0.0


class Pipeline:
    def __init__(
        self, opts: RunOptions, db: DB, engine: Engine, provider: str, model: str,
        log=print, should_stop=None,
    ):
        self.opts = opts
        self.db = db
        self.engine = engine
        self.provider = provider
        self.model = model
        self.log = log
        self.should_stop = should_stop
        self.stats = Stats()
        self.jid = job_id_for(opts.src, opts.lang_from, opts.lang_to, provider, model)
        self.href_by_idx: dict[int, str] = {}
        self.title_by_idx: dict[int, str] = {}

    # ---------------- 1. 分段入库 ----------------
    def prepare(self) -> None:
        o = self.opts
        self.db.ensure_job(
            self.jid,
            src_path=str(o.src),
            out_path=str(o.out),
            lang_from=o.lang_from,
            lang_to=o.lang_to,
            provider=self.provider,
            model=self.model,
        )
        self.db.set_state(self.jid, "segmenting")
        inserted = 0
        with zipfile.ZipFile(o.src) as z:
            pkg = read_package(z)
            for idx, item in spine_items(pkg):
                href = item.href
                try:
                    _, cs = segment_chapter(
                        z.read(href), href, max_chars=o.max_segment_chars
                    )
                except Exception as exc:
                    self.log(f"  ! 跳过无法解析的章节 {href}: {exc}")
                    continue
                if not cs.segments:
                    continue
                self.href_by_idx[idx] = href
                title = _chapter_title(cs.blocks) or Path(href).stem
                self.title_by_idx[idx] = title
                self.db.add_chapter(self.jid, idx, href, title, len(cs.segments))
                self.stats.chapters += 1
                # 不用 chapter_has_segments 跳过：--limit 截断过的章会缺尾段，
                # INSERT OR IGNORE 本身幂等，重扫一遍才能补齐。
                segs = [
                    Segment(
                        chapter_idx=idx,
                        block_idx=s.block_idx,
                        seg_idx=s.seg_idx,
                        src_text=s.template,
                        ph_count=s.ph_count,
                    )
                    for s in cs.segments
                ]
                if o.limit_segments:
                    room = o.limit_segments - inserted
                    if room <= 0:
                        break
                    segs = segs[:room]
                inserted += self.db.insert_segments(self.jid, segs)
        self.db.refresh_stats(self.jid)
        self.stats.segments_total = self.db.job(self.jid)["total_segments"]
        self.log(f"分段完成：{self.stats.chapters} 章，{self.stats.segments_total} 段（新入库 {inserted}）")

    # ---------------- 2. 分析：术语 + 风格卡 ----------------
    def analyze(self) -> None:
        if not self.opts.analyze:
            return
        self.db.set_state(self.jid, "analyzing")
        # 分析阶段必须幂等：术语已经定过名就绝不能再让模型重定一遍，
        # 否则同一本书重跑会出现两套译名，比不跑还糟。
        existing_terms = self.db.glossary(self.jid)
        if existing_terms:
            self.stats.glossary_terms = len(existing_terms)
            self.log(f"术语表已存在（{len(existing_terms)} 条），跳过定名")
        else:
            rows = self.db.all_segments(self.jid)
            texts = [r["src_text"] for r in rows]
            candidates = extract_candidates(texts, self.opts.lang_from)
            self.log(f"术语候选 {len(candidates)} 个，正在让模型定名……")
            if candidates:
                try:
                    raw = self.engine.complete(
                        prompts.SYSTEM_EXTRACT_TERMS,
                        prompts.extract_terms_prompt(candidates),
                        max_tokens=1500,
                    )
                    for src, dst in _parse_terms(raw):
                        self.db.put_glossary(self.jid, src, dst, kind="term", locked=True)
                except Exception as exc:
                    self.log(f"  ! 术语定名失败，降级为不加术语表：{exc}")
            self.stats.glossary_terms = len(self.db.glossary(self.jid))
            self.log(f"术语表 {self.stats.glossary_terms} 条")

        if self.db.style_card(self.jid)[1]:
            self.log(f"风格卡已存在（v{self.db.style_card(self.jid)[1]}），跳过生成")
            return
        rows = self.db.all_segments(self.jid)
        sample = "\n".join(r["src_text"] for r in rows[:400])
        try:
            card = self.engine.complete(
                prompts.SYSTEM_STYLE_CARD, prompts.style_card_prompt(sample), max_tokens=500
            )
            card = "\n".join(
                ln.strip() for ln in card.splitlines() if ln.strip().startswith(("-", "•", "*"))
            ) or card.strip()
            if card:
                self.db.put_style_card(self.jid, card)
        except Exception as exc:
            self.log(f"  ! 风格卡生成失败，跳过：{exc}")

        self.stats.glossary_terms = len(self.db.glossary(self.jid))
        self.log(f"术语表 {self.stats.glossary_terms} 条，风格卡 v{self.db.style_card(self.jid)[1]}")

    # ---------------- 3. 翻译 ----------------
    def translate(self) -> None:
        self.db.set_state(self.jid, "translating")
        o = self.opts
        glossary = {r["src"]: r["dst"] for r in self.db.glossary(self.jid) if r["locked"]}
        style_card, style_ver = self.db.style_card(self.jid)
        gver = self.db.glossary_version(self.jid)
        chapters = self.db.conn.execute(
            "SELECT idx FROM chapters WHERE job_id=? ORDER BY idx", (self.jid,)
        ).fetchall()

        breaker = 0
        # 上一次运行已经完成的段：不花钱也不重扫，直接计入“未花费”统计
        self.stats.segments_cached += self.db.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE job_id=? AND state IN ('done','review')",
            (self.jid,),
        ).fetchone()[0]
        for pos, ch in enumerate(chapters):
            cidx = ch["idx"]
            pending = self.db.pending_segments(self.jid, cidx)
            if not pending:
                continue
            ctx = Context(
                src_lang=o.lang_from,
                tgt_lang=o.lang_to,
                glossary=glossary,
                style_card=style_card,
                chapter_no=pos + 1,
                chapter_total=len(chapters),
            )
            self.log(
                f"[章 {pos + 1}/{len(chapters)}] {self.title_by_idx.get(cidx, '')[:36]} 待译 {len(pending)} 段"
            )
            todo: list[tuple[object, str]] = []
            for row in pending:
                ck = _cache_key(row["src_hash"], gver, style_ver, self.engine.id, o.lang_to)
                hit = self.db.cache_get(ck)
                if hit:
                    self.db.mark_done(self.jid, (cidx, row["block_idx"], row["seg_idx"]), hit, self.engine.id)
                    self.stats.segments_cached += 1
                    continue
                todo.append((row, ck))

            done_count = 0
            for batch in _batches(todo, o.batch_size):
                if self.should_stop and self.should_stop():
                    self.db.set_state(self.jid, "paused")
                    self.log("已暂停（当前批次已完成，进度已保存）")
                    return
                items: list[Item] = []
                affixes: dict[tuple[int, int, int], tuple[str, str]] = {}
                for row, ck in batch:
                    key = (cidx, row["block_idx"], row["seg_idx"])
                    core, pre, suf = detach_affixes(row["src_text"])
                    if not core.strip():
                        # 只有锚点/图片：原文即成品，不用花钱
                        self.db.mark_done(self.jid, key, row["src_text"], self.engine.id)
                        self.stats.segments_cached += 1
                        continue
                    affixes[key] = (pre, suf)
                    items.append(Item(key, core))
                if not items:
                    continue
                try:
                    results = self._translate_with_retry(items, ctx)
                    breaker = 0
                except EngineError as exc:
                    breaker += 1
                    self.log(f"  ! 引擎错误：{exc}")
                    for row, _ in batch:
                        self.db.mark_failed(
                            self.jid, (cidx, row["block_idx"], row["seg_idx"]), str(exc),
                            row["attempts"] + 1,
                        )
                    self.stats.segments_failed += len(batch)
                    if breaker >= 8:
                        raise SystemExit("连续失败过多，已停止（检查额度/网络/模型名）") from exc
                    time.sleep(min(o.backoff_max, o.backoff_base * (2 ** breaker)))
                    continue

                result_by_key = {r.key: r for r in results}
                for row, ck in batch:
                    key = (cidx, row["block_idx"], row["seg_idx"])
                    if key not in result_by_key:
                        continue
                    res = result_by_key[key]
                    pre, suf = affixes.get(key, ("", ""))
                    text = (
                        f"{pre.rstrip()}{res.text}{suf.lstrip()}"
                        if res.ok
                        else ""
                    )
                    problem = None
                    if not res.ok:
                        problem = res.error or "未返回"
                    else:
                        problem = _validate(row["src_text"], text, o.lang_to)
                    if problem:
                        # 结构校验不过：先尝试“去掉占位符重译”的救命路径。
                        # 丢掉一点内联样式 << 整段失败，读者要的是能读的中文。
                        salvaged = self._salvage(row, cidx, ctx)
                        if salvaged:
                            self.db.mark_done(self.jid, key, salvaged, self.engine.id)
                            self.db.cache_put(ck, salvaged, self.engine.id)
                            self.stats.segments_done += 1
                            continue
                        # 宁可留原文，也不往成品里塞坏结构
                        self.stats.segments_review += 1
                        self.db.mark_review(self.jid, key, row["src_text"], problem)
                        continue
                    self.db.mark_done(self.jid, key, text, self.engine.id)
                    self.db.cache_put(ck, text, self.engine.id)
                    self.stats.segments_done += 1
                self.db.touch_chapter(self.jid, cidx)
                self.db.refresh_stats(self.jid)
                self.stats.api_calls += 1
                if self._over_budget():
                    self.db.set_state(self.jid, "paused")
                    self.log("预算到点，已暂停")
                    return
                done_count += len(batch)
                if o.log_every:
                    self._progress(cidx, len(chapters), len(todo), done_count)
        self.db.refresh_stats(self.jid)

    def _salvage(self, row, cidx: int, ctx: Context) -> str | None:
        """降级翻译：删掉全部占位符，只要一段通顺译文。"""
        key = (cidx, row["block_idx"], row["seg_idx"])
        core, _pre, _suf = detach_affixes(row["src_text"])
        plain = PH_RE.sub("", core).strip()
        if not plain:
            return None
        for _ in range(2):
            try:
                res, usage = self.engine.translate([Item(key, plain)], ctx)
                self.stats.usage.add(usage)
            except EngineError as exc:
                if not exc.retryable:
                    return None
                continue
            if not res or not res[0].ok:
                continue
            text = res[0].text.strip()
            if not text:
                continue
            if _validate_plain(row["src_text"], text, self.opts.lang_to):
                continue
            return text
        return None

    def _translate_with_retry(self, items: list[Item], ctx: Context) -> list:
        """返回顺序必须与 items 严格一致——错位会把译文写到别的段落上。"""
        o = self.opts
        done: dict[tuple, str] = {}
        pending = list(items)
        attempt = 0
        last_error = "重试耗尽"
        while pending:
            if attempt >= o.max_retries:
                break
            if attempt > 0:
                wait = min(o.backoff_max, o.backoff_base * (2 ** (attempt - 1)))
                self.log(f"  重试 {attempt}/{o.max_retries} 后 {wait:.0f}s（{last_error[:80]}）")
                time.sleep(wait)
            try:
                res, usage = self.engine.translate(pending, ctx)
                self.stats.usage.add(usage)
            except EngineError as exc:
                if not exc.retryable:
                    raise
                last_error = str(exc)
                attempt += 1
                continue
            for r in res:
                if r.ok:
                    done[r.key] = r.text
                else:
                    last_error = r.error or "未返回"
            pending = [i for i in pending if i.key not in done]
            attempt += 1

        out: list[Result] = []
        for it in items:
            if it.key in done:
                out.append(Result(it.key, done[it.key]))
            else:
                out.append(Result(it.key, "", ok=False, error=last_error))
        return out

    def _over_budget(self) -> bool:
        if self.opts.budget_cny <= 0:
            return False
        cost = (
            self.stats.usage.prompt_tokens / 1e6 * self.opts.price_in
            + self.stats.usage.completion_tokens / 1e6 * self.opts.price_out
        )
        return cost >= self.opts.budget_cny

    def _progress(self, cidx: int, total_ch: int, total: int, done: int) -> None:
        elapsed = time.time() - self.stats.started
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        self.log(
            f"    {done}/{total} 段 | {rate:.2f} 段/s | 已用 {elapsed/60:.1f} 分 | "
            f"剩约 {eta/60:.1f} 分 | tokens in/out "
            f"{self.stats.usage.prompt_tokens}/{self.stats.usage.completion_tokens}"
        )

    # ---------------- 4. 组装 ----------------
    def assemble(self) -> WriteReport | None:
        if self.opts.dry_run:
            return None
        rows = self.db.all_segments(self.jid)
        by_chapter: dict[int, dict[int, list[tuple[int, str]]]] = {}
        expect: dict[tuple[int, int], int] = {}
        for r in rows:
            k = (r["chapter_idx"], r["block_idx"])
            expect[k] = expect.get(k, 0) + 1
            if r["state"] in ("done", "review") and r["dst_text"]:
                by_chapter.setdefault(r["chapter_idx"], {}).setdefault(r["block_idx"], []).append(
                    (r["seg_idx"], r["dst_text"])
                )

        plans: dict[str, ChapterPlan] = {}
        skipped = 0
        for cidx, blocks in by_chapter.items():
            href = self.href_by_idx.get(cidx)
            if not href:
                row = self.db.conn.execute(
                    "SELECT href FROM chapters WHERE job_id=? AND idx=?", (self.jid, cidx)
                ).fetchone()
                href = row["href"] if row else None
            if not href:
                continue
            plan = ChapterPlan()
            for bidx, segs in blocks.items():
                if len(segs) < expect.get((cidx, bidx), len(segs)):
                    skipped += 1
                    continue
                plan.blocks[bidx] = [t for _, t in sorted(segs)]
            if plan.blocks:
                plans[href] = plan

        if skipped:
            self.log(f"  ! {skipped} 个块未译完，已跳过（不会写入半成品）")
        if not plans:
            self.log("没有任何可组装的章节")
            return None
        if not self.opts.bilingual and skipped and not self.opts.allow_partial:
            self.log("纯译文模式且不完整，按配置中止组装")
            return None
        report = write_epub(
            self.opts.src,
            self.opts.out,
            plans,
            bilingual=self.opts.bilingual,
        )
        self.db.set_state(self.jid, "done")
        self.db.event(
            self.jid, "job.completed",
            {"chapters": report.chapters_written, "blocks": report.blocks_replaced,
             "bytes": report.bytes_out, "out": str(self.opts.out)},
        )
        self.log(
            f"成品：{self.opts.out}（{report.chapters_written} 章，"
            f"{report.blocks_replaced} 块，{report.bytes_out/1024/1024:.2f} MB，自检通过）"
        )
        return report

    # ---------------- 总控 ----------------
    def run(self) -> Stats:
        self.prepare()
        if self.opts.dry_run:
            self.log("dry-run：只做分段，不调用 API")
            return self.stats
        self.analyze()
        self.translate()
        self.assemble()
        j = self.db.job(self.jid)
        self.log(
            f"完成。段：{j['done_segments']}/{j['total_segments']} 已译，"
            f"待复核 {j['review_segments']}，失败 {j['failed_segments']}；"
            f"缓存命中 {self.stats.segments_cached}"
        )
        return self.stats


def _batches(todo: list, batch_size: int, max_chars: int = 2600):
    """按字符数动态分批。

    固定“一次 4 段”在长短段混排时会炸：四个长段叠起来能把 prompt 推到上万字符，
    免费小模型直接超时。按总字符数封顶比按段数封顶稳得多。
    """
    batch: list = []
    size = 0
    for item in todo:
        n = len(item[0]["src_text"])
        if batch and (len(batch) >= batch_size or size + n > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += n
    if batch:
        yield batch


def _chapter_title(blocks) -> str:
    for el in blocks:
        if local(el.tag) in ("h1", "h2", "h3"):
            t = " ".join("".join(el.itertext()).split())
            if t:
                return t[:80]
    return ""


def _cache_key(src_hash: str, gver: int, sver: int, model: str, lang_to: str) -> str:
    payload = f"{src_hash}|g{gver}|s{sver}|{model}|{prompts.PROMPT_VERSION}|{lang_to}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_plain(src_text: str, dst_text: str, lang_to: str) -> str | None:
    """降级路径的校验：没有占位符，只看长度是否离谱。"""
    if not dst_text or not dst_text.strip():
        return "空译文"
    ratio = len(dst_text) / max(1, len(PH_RE.sub("", src_text)))
    if lang_to.lower().startswith("zh"):
        if ratio < 0.12:
            return f"译文过短（比例 {ratio:.2f}）"
        if ratio > 4.5:
            return f"译文过长（比例 {ratio:.2f}）"
    return None


def _validate(src_text: str, dst_text: str, lang_to: str) -> str | None:
    if not dst_text or not dst_text.strip():
        return "空译文"
    src_ph = placeholders_of(src_text)
    dst_ph = placeholders_of(dst_text)
    if src_ph != dst_ph:
        return f"占位符不一致 src={sorted(src_ph)} dst={sorted(dst_ph)}"
    ratio = len(dst_text) / max(1, len(src_text))
    if lang_to.lower().startswith("zh"):
        if ratio < 0.15:
            return f"译文过短（比例 {ratio:.2f}）"
        if ratio > 4.0:
            return f"译文过长（比例 {ratio:.2f}）"
    return None


def _parse_terms(raw: str) -> list[tuple[str, str]]:
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src", "")).strip()
        dst = str(item.get("dst", "")).strip()
        if src and dst and src.lower() != dst.lower():
            out.append((src, dst))
    return out
