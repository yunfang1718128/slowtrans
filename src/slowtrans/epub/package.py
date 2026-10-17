"""EPUB 组装与结构自检。

铁律（和 scripts/webdav/epub-surgery.py 里踩过的坑一致）：
1. mimetype 必须是第一个条目，且 STORE 不压缩；
2. 其余条目保持原有顺序与压缩方式；
3. 写完立刻回读自检，绝不产出"看着正常、打开报错"的书。
"""
from __future__ import annotations

import copy
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .ocf import XHTML_NS, iter_entries, local, parse_xml, read_package, spine_items
from .segment import collect_blocks, render_block

HORIZONTAL_CSS = (
    "\n/* slowtrans: 强制横排，避免译文继承日文竖排样式 */\n"
    "html, body { writing-mode: horizontal-tb !important; }\n"
)
BILINGUAL_CSS = (
    "/* slowtrans: 双语对照样式 */\n"
    ".st-src { opacity: .72; font-size: .95em; }\n"
)


@dataclass
class ChapterPlan:
    """一章的译文：{block_idx: [各段译文, ...]}"""
    blocks: dict[int, list[str]] = field(default_factory=dict)


@dataclass
class WriteReport:
    chapters_written: int = 0
    blocks_replaced: int = 0
    bytes_out: int = 0
    problems: list[str] = field(default_factory=list)


def _doctype_of(data: bytes) -> str | None:
    m = re.search(rb"<!DOCTYPE[^>]*>", data, re.I)
    return m.group(0).decode("utf-8", "replace") if m else None


def _serialize(tree, doctype: str | None) -> bytes:
    return etree.tostring(
        tree, encoding="utf-8", xml_declaration=True, doctype=doctype
    )


def write_epub(
    src_path: str | Path,
    out_path: str | Path,
    plans: dict[str, ChapterPlan],
    bilingual: bool = True,
    strip_ruby: bool = True,
    force_horizontal: bool = True,
) -> WriteReport:
    src_path, out_path = Path(src_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = WriteReport()
    tmp = out_path.with_suffix(".part")

    with zipfile.ZipFile(src_path) as zin:
        pkg = read_package(zin)
        chapter_hrefs = {item.href for _, item in spine_items(pkg)}

        with zipfile.ZipFile(tmp, "w") as zout:
            # 1) mimetype 第一且 STORE
            mt = None
            for info in zin.infolist():
                if info.filename == "mimetype":
                    mt = info
                    break
            zi = zipfile.ZipInfo("mimetype", date_time=mt.date_time if mt else (1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zout.writestr(zi, zin.read("mimetype") if mt else b"application/epub+zip")

            # 2) 其余条目按原顺序
            for info in iter_entries(zin):
                if info.filename == "mimetype":
                    continue
                data = zin.read(info)

                if info.filename in chapter_hrefs and info.filename in plans:
                    data = _rewrite_chapter(
                        data, plans[info.filename], bilingual, strip_ruby, report
                    )
                    report.chapters_written += 1
                elif info.filename.endswith(".css"):
                    data = _patch_css(data, force_horizontal, bilingual)

                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                zout.writestr(zi, data)

    report.bytes_out = tmp.stat().st_size
    report.problems = verify_epub(tmp)
    if report.problems:
        raise RuntimeError("输出 epub 自检失败：" + "；".join(report.problems))
    tmp.replace(out_path)
    return report


def _patch_css(data: bytes, force_horizontal: bool, bilingual: bool) -> bytes:
    add = ""
    if force_horizontal:
        add += HORIZONTAL_CSS
    if bilingual:
        add += BILINGUAL_CSS
    if not add:
        return data
    return data + add.encode("utf-8")


def _rewrite_chapter(
    data: bytes, plan: ChapterPlan, bilingual: bool, strip_ruby: bool, report: WriteReport
) -> bytes:
    doctype = _doctype_of(data)
    tree = parse_xml(data)
    blocks = collect_blocks(tree)

    for block_idx, texts in plan.blocks.items():
        if block_idx >= len(blocks) or not texts:
            continue
        old = blocks[block_idx]
        parent = old.getparent()
        if parent is None:
            continue
        pos = parent.index(old)
        tail = old.tail

        new_elems = render_block(old, texts, strip_ruby=strip_ruby)
        if bilingual:
            src_clone = copy.deepcopy(old)
            src_clone.tail = None
            cls = src_clone.get("class", "")
            src_clone.set("class", (cls + " st-src").strip())
            sid = src_clone.get("id")
            if sid:
                src_clone.set("id", sid + "-src")
            parent.insert(pos, src_clone)
            pos += 1

        for offset, el in enumerate(new_elems):
            parent.insert(pos + offset, el)
        parent.remove(old)
        if tail and new_elems:
            new_elems[-1].tail = tail
        report.blocks_replaced += 1

    return _serialize(tree, doctype)


def verify_epub(path: str | Path) -> list[str]:
    """不做完整 epubcheck（要 Java + 几百 MB），只查会让阅读器直接拒开的硬伤。"""
    problems: list[str] = []
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if not infos or infos[0].filename != "mimetype":
                problems.append("mimetype 不是第一个条目")
            else:
                mt = infos[0]
                if mt.compress_type != zipfile.ZIP_STORED:
                    problems.append("mimetype 被压缩了（必须 STORE）")
                if z.read("mimetype").strip() != b"application/epub+zip":
                    problems.append("mimetype 内容不正确")

            names = set(z.namelist())
            if "META-INF/container.xml" not in names:
                problems.append("缺少 META-INF/container.xml")
                return problems

            pkg = read_package(z)
            if pkg.opf_path not in names:
                problems.append(f"OPF 不存在：{pkg.opf_path}")
            for _, item in spine_items(pkg):
                if item.href not in names:
                    problems.append(f"spine 引用了不存在的文件：{item.href}")

            for name in names:
                if name.endswith((".xhtml", ".html", ".opf", ".ncx")):
                    try:
                        parse_xml(z.read(name))
                    except Exception as exc:  # lxml recover 后仍失败说明真的坏了
                        problems.append(f"{name} 无法解析：{exc}")
            if z.testzip() is not None:
                problems.append("zip 校验失败")
    except zipfile.BadZipFile:
        problems.append("不是有效的 zip/epub")
    return problems


def read_chapter_text(path: str | Path) -> dict[str, list[tuple[int, int, str, int]]]:
    """开发期自检用：把一本书按 (章, 块, 段) 摊平，不写库。"""
    out: dict[str, list[tuple[int, int, str, int]]] = {}
    from .segment import segment_chapter

    with zipfile.ZipFile(path) as z:
        pkg = read_package(z)
        for _, item in spine_items(pkg):
            try:
                _, cs = segment_chapter(z.read(item.href), item.href)
            except Exception:
                continue
            if not cs.segments:
                continue
            out[item.href] = [
                (s.block_idx, s.seg_idx, s.template, s.ph_count) for s in cs.segments
            ]
    return out
