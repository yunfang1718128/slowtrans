"""组装阶段：按章从库里取译文，流式写两份 epub。

单独成模块的两个原因：
1. 这是唯一需要"全书视角"的步骤，放在 Pipeline 里永远会被误认为内存 O(全书)；
2. 双语版和纯译文版只差一个开关，逻辑必须完全共享，否则会各自漂移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .db import DB
from .epub.package import ChapterPlan, verify_epub, write_epub


@dataclass
class AssembleResult:
    bilingual_path: Path | None = None
    mono_path: Path | None = None
    chapters: int = 0
    blocks: int = 0
    bytes: int = 0
    skipped_blocks: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def assemble(
    db: DB,
    jid: str,
    src: Path,
    out: Path,
    *,
    bilingual: bool = True,
    emit_mono: bool = True,
    allow_partial: bool = True,
    strip_ruby: bool = True,
    force_horizontal: bool = True,
    log=print,
) -> AssembleResult:
    idx_by_href = db.chapter_idx_by_href(jid)
    if not idx_by_href:
        log("没有任何可组装的章节")
        return AssembleResult()
    skipped = {"n": 0}

    def provider(href: str):
        cidx = idx_by_href.get(href)
        if cidx is None:
            return None
        expect, done = db.chapter_done_blocks(jid, cidx)
        plan = ChapterPlan()
        for bidx, total in expect.items():
            segs = done.get(bidx, [])
            if len(segs) < total:
                skipped["n"] += 1
                continue
            plan.blocks[bidx] = [t for _, t in sorted(segs)]
        return plan if plan.blocks else None

    result = AssembleResult()
    report = write_epub(
        src, out, provider,
        bilingual=bilingual, strip_ruby=strip_ruby, force_horizontal=force_horizontal,
    )
    result.bilingual_path = out
    result.chapters = report.chapters_written
    result.blocks = report.blocks_replaced
    result.bytes = report.bytes_out
    result.problems = list(report.problems)

    if skipped["n"]:
        log(f"  ! {skipped['n']} 个块未译完，已跳过（不会写入半成品）")
    result.skipped_blocks = skipped["n"]

    if not bilingual and result.skipped_blocks and not allow_partial:
        out.unlink(missing_ok=True)
        result.bilingual_path = None
        result.problems.append("纯译文模式存在未译完的块")
        return result

    if bilingual and emit_mono:
        mono = out.with_name(f"{out.stem}.mono{out.suffix}")
        mono_report = write_epub(
            src, mono, provider,
            bilingual=False, strip_ruby=strip_ruby, force_horizontal=force_horizontal,
        )
        result.mono_path = mono
        result.problems.extend(mono_report.problems)

    if result.problems:
        log("  ! 自检问题：" + "；".join(result.problems))
    return result


def verify(path: Path) -> list[str]:
    return verify_epub(path)
