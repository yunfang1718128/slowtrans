from __future__ import annotations

import zipfile

from slowtrans.epub.ocf import read_package, spine_items
from slowtrans.epub.package import verify_epub, write_epub, ChapterPlan
from slowtrans.epub.segment import (
    collect_blocks,
    detach_affixes,
    flatten_block,
    placeholders_of,
    render_block,
    segment_chapter,
    split_template,
)


def _chapter_data(path, href="OEBPS/ch1.xhtml"):
    with zipfile.ZipFile(path) as z:
        return z.read(href)


def test_flatten_keeps_inline_as_placeholder(sample_epub):
    root, cs = segment_chapter(_chapter_data(sample_epub), "OEBPS/ch1.xhtml")
    # 第三块含 <em> 与 <span id>：文字会被展开，只有空锚点保留为占位符
    target = [s for s in cs.segments if "He said" in s.template]
    assert target, "应该能抽出含内联标签的段落"
    assert "{{0}}" in target[0].template or "{{1}}" in target[0].template
    assert "{{0}}" in target[0].template


def test_image_only_block_is_skipped(sample_epub):
    _, cs = segment_chapter(_chapter_data(sample_epub), "OEBPS/ch1.xhtml")
    # 纯图片块摊平后只剩 {{0}}，不应入库，否则每个图片都会变成一次“空译文”复核
    assert all(s.template.replace("{{", "").strip("}0123456789 ") for s in cs.segments)


def test_nested_block_not_double_counted(sample_epub):
    _, cs = segment_chapter(_chapter_data(sample_epub, "OEBPS/ch2.xhtml"), "OEBPS/ch2.xhtml")
    texts = [s.template for s in cs.segments]
    assert sum("Nested blocks" in t for t in texts) == 1
    assert sum("First item" in t for t in texts) == 1


def test_split_never_cuts_placeholder():
    tpl = "A" * 50 + "{{0}}" + "B" * 50 + "。"
    chunks = split_template(tpl, 60)
    assert "".join(chunks) == tpl
    for chunk in chunks:
        # 占位符必须完整落在某一段里
        assert chunk.count("{{0}}") <= 1
        assert not chunk.endswith("{{")


def test_detach_affixes_roundtrip():
    text = " {{0}}{{1}} Thanks to everyone. {{2}} "
    core, pre, suf = detach_affixes(text)
    assert "{{0}}" in pre and "{{1}}" in pre
    assert "{{2}}" in suf
    assert "Thanks" in core


def test_inline_container_text_is_translated(sample_epub):
    """<em> 里的文字必须进入模板，否则标题这类被 span 包住的文本会整段漏译。"""
    _, cs = segment_chapter(_chapter_data(sample_epub), "OEBPS/ch1.xhtml")
    seg = next(s for s in cs.segments if "He said" in s.template)
    assert "nothing" in seg.template
    assert "nothing" not in "".join("{{%d}}" % i for i in range(9))


def test_render_block_restores_atomic_elements(sample_epub):
    from lxml import etree

    data = _chapter_data(sample_epub)
    root, cs = segment_chapter(data, "OEBPS/ch1.xhtml")
    blocks = collect_blocks(root)
    idx = next(s.block_idx for s in cs.segments if "He said" in s.template)
    seg = next(s for s in cs.segments if "He said" in s.template)
    # 空 span（分页锚点）应当保留为占位符
    assert "{{0}}" in seg.template
    out = render_block(blocks[idx], ["他说什么也没说，然后离开了。{{0}}结尾稍后到来。"], strip_ruby=True)
    assert len(out) == 1
    html = etree.tostring(out[0], encoding="unicode")
    assert 'id="pg1"' in html, "锚点必须还原"
    assert "他说什么也没说" in html


def test_full_pipeline_mock(sample_epub, tmp_path):
    from slowtrans.db import DB
    from slowtrans.engines.mock import MockEngine
    from slowtrans.pipeline import Pipeline, RunOptions

    out = tmp_path / "out.epub"
    db = DB(tmp_path / "db.sqlite")
    opts = RunOptions(src=sample_epub, out=out, lang_from="en", lang_to="zh-CN", bilingual=True,
                      analyze=False, batch_size=2)
    pipe = Pipeline(opts, db, MockEngine(), "mock", "echo", log=lambda *_: None)
    pipe.run()
    assert out.exists()
    assert verify_epub(out) == []
    with zipfile.ZipFile(out) as z:
        html = z.read("OEBPS/ch1.xhtml").decode()
        assert "st-src" in html, "双语模式应保留原文段"
        assert "{{" not in html, "占位符必须全部还原"


def test_resume_is_idempotent(sample_epub, tmp_path):
    from slowtrans.db import DB
    from slowtrans.engines.mock import MockEngine
    from slowtrans.pipeline import Pipeline, RunOptions

    out = tmp_path / "out.epub"
    db = DB(tmp_path / "db.sqlite")
    engine = MockEngine()
    opts = RunOptions(src=sample_epub, out=out, lang_from="en", lang_to="zh-CN",
                      analyze=False, batch_size=2, limit_segments=2)
    Pipeline(opts, db, engine, "mock", "echo", log=lambda *_: None).run()
    first = engine.calls

    # 第二次跑：limit 放开，已完成的段必须走缓存、不再调用引擎
    opts2 = RunOptions(src=sample_epub, out=out, lang_from="en", lang_to="zh-CN",
                       analyze=False, batch_size=2)
    stats = Pipeline(opts2, db, engine, "mock", "echo", log=lambda *_: None).run()
    assert engine.calls > first, "剩余段落应该被翻译"
    assert stats.segments_cached >= 2, "已完成段落必须命中缓存"


def test_verify_rejects_broken_epub(tmp_path):
    import zipfile as zf
    from pathlib import Path

    p = Path(tmp_path / "bad.epub")
    with zf.ZipFile(p, "w") as z:
        z.writestr("OEBPS/content.opf", "<package/>")
    problems = verify_epub(p)
    assert any("mimetype" in x for x in problems)
