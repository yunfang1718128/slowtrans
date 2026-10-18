"""块级切分与还原。

为什么按"块"而不是按字符数切：
- 块（p/h1-6/li/td…）是排版单位，切在块内会破坏内联标签的落点；
- 一册书的块数量与阅读顺序稳定，天然适合做断点续传的键。

内联标签用 {{n}} 占位符保护：模型只看到纯文本 + 编号，
我们按编号把原始元素插回去。这样模型永远不会破坏 HTML 结构。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from lxml import etree

from .ocf import XHTML_NS, local, parse_xml

BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd",
    "blockquote",
    "figcaption", "caption", "td", "th",
    "div", "section", "article", "aside", "header", "footer", "main", "nav",
    "pre", "address", "center",
}

# 原子元素：没有可译文字，整块原样插回译文
INLINE_ATOMIC = {"img", "br", "hr", "wbr", "svg", "math", "sup", "sub"}

# 容器型内联元素：里面的文字必须翻译（否则标题会整段漏译），元素本身不保留。
# 早期版本把这些整体当占位符，结果 <h1><span>Title</span></h1> 的标题
# 完全没进翻译队列——纯中文版开头还是英文。
INLINE_RECURSE = {
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn",
    "em", "i", "ins", "kbd", "mark", "q", "s", "samp", "small", "span",
    "strong", "time", "u", "var",
}

# 只有纯数字/符号的链接（脚注标记）保留元素，保住跳转
_FOOTNOTE_MARK = re.compile(r"^[\[\]()0-9*†‡§¶\s.\-]{1,8}$")

PH_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
# 只要还有真正的字母/汉字/假名就值得翻；只剩标点、数字、页码的索引条目跳过。
WORD_RE = re.compile(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff]")
BOUNDARY_RE = re.compile(r"(?<=[。．！？!?…])|(?<=[.])\s+(?=[A-Z\"“「『(])")


@dataclass
class BlockSegment:
    block_idx: int
    seg_idx: int
    template: str
    ph_count: int
    block_tag: str
    block_attr_id: str | None = None


@dataclass
class ChapterSegments:
    href: str
    blocks: list[etree._Element] = field(default_factory=list)
    segments: list[BlockSegment] = field(default_factory=list)


def has_block_child(el: etree._Element) -> bool:
    for child in el:
        if isinstance(child.tag, str) and local(child.tag) in BLOCK_TAGS:
            return True
    return False


def collect_blocks(root: etree._Element) -> list[etree._Element]:
    body = None
    for el in root.iter():
        if local(el.tag) == "body":
            body = el
            break
    if body is None:
        body = root
    blocks: list[etree._Element] = []

    def walk(parent):
        for child in parent:
            if not isinstance(child.tag, str):
                continue
            if local(child.tag) in BLOCK_TAGS and not has_block_child(child):
                blocks.append(child)
            else:
                walk(child)

    walk(body)
    return blocks


def _ruby_base(el: etree._Element) -> str:
    """ruby 只取基文，丢掉 <rt> 假名注音（中文读者不需要）。"""
    parts = [el.text or ""]
    for child in el:
        if local(child.tag) == "rt":
            parts.append(child.tail or "")
            continue
        parts.append("".join(child.itertext()))
        parts.append(child.tail or "")
    return "".join(parts)


def flatten_block(el: etree._Element, strip_ruby: bool = True) -> tuple[str, list[etree._Element]]:
    """把块的可见文本摊平成模板串，内联元素换成 {{n}}。"""
    phs: list[etree._Element] = []
    out: list[str] = []

    def emit_text(text: str | None):
        if text:
            out.append(text)

    def keep(node: etree._Element) -> None:
        out.append("{{%d}}" % len(phs))
        phs.append(node)

    def emit_node(node: etree._Element):
        name = local(node.tag)
        if name == "ruby" and strip_ruby:
            emit_text(_ruby_base(node))
            return
        if name in INLINE_ATOMIC:
            keep(node)
            return
        if name in INLINE_RECURSE:
            inner = "".join(node.itertext())
            if not inner.strip():
                keep(node)          # 空锚点 <span id="page_1"/>
                return
            if name == "a" and _FOOTNOTE_MARK.match(inner.strip()):
                keep(node)          # [1] / * / † 这类脚注标记，保住链接
                return
            emit_text(node.text)
            for child in node:
                emit_node(child)
                emit_text(child.tail)
            return
        # 未知标签：继续往里走，别丢文字
        emit_text(node.text)
        for child in node:
            emit_node(child)
            emit_text(child.tail)

    emit_text(el.text)
    for child in el:
        emit_node(child)
        emit_text(child.tail)
    return "".join(out), phs


def _safe_cut(tpl: str, pos: int) -> int:
    """切点不能落在 {{n}} 中间。"""
    for m in PH_RE.finditer(tpl):
        if m.start() < pos < m.end():
            return m.start()
    return pos


def normalize_template(tpl: str) -> str:
    """把排版换行/缩进折叠成单空格。

    否则 XHTML 里的缩进会原样进 prompt，模型有时会把它当格式指令。
    """
    return re.sub(r"[ \t\r\n]+", " ", tpl).strip()


def _sentences(tpl: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in BOUNDARY_RE.finditer(tpl):
        end = m.end()
        if end > start:
            out.append(tpl[start:end])
            start = end
    if start < len(tpl):
        out.append(tpl[start:])
    return out or [tpl]


def split_template(tpl: str, max_chars: int) -> list[str]:
    """先按句子切，再贪心合并到 max_chars；单句超长才硬切。

    硬切也必须避开 {{n}}，否则占位符会被劈成两半。
    """
    if len(tpl) <= max_chars:
        return [tpl]
    chunks: list[str] = []
    buf = ""
    for sent in _sentences(tpl):
        while len(sent) > max_chars:
            cut = _safe_cut(sent, max_chars)
            if cut <= 0:
                cut = max_chars
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(sent[:cut])
            sent = sent[cut:]
        if buf and len(buf) + len(sent) > max_chars:
            chunks.append(buf)
            buf = ""
        buf += sent
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def segment_chapter(
    data: bytes, href: str, max_chars: int = 1200, strip_ruby: bool = True
) -> tuple[etree._Element, ChapterSegments]:
    root = parse_xml(data)
    blocks = collect_blocks(root)
    cs = ChapterSegments(href=href, blocks=blocks)
    for bi, block in enumerate(blocks):
        tpl, phs = flatten_block(block, strip_ruby=strip_ruby)
        if not tpl.strip() and not phs:
            continue
        # 只有占位符、没有可译文字的块（图片、纯锚点）不入库：
        # 既不浪费 API 调用，也不会制造一堆"空译文"复核。
        tpl = normalize_template(tpl)
        if not WORD_RE.search(PH_RE.sub("", tpl)):
            continue
        for si, chunk in enumerate(split_template(tpl, max_chars)):
            chunk = normalize_template(chunk)
            if not WORD_RE.search(PH_RE.sub("", chunk)):
                continue
            ids = {int(m.group(1)) for m in PH_RE.finditer(chunk)}
            cs.segments.append(
                BlockSegment(
                    block_idx=bi,
                    seg_idx=si,
                    template=chunk,
                    ph_count=len(ids),
                    block_tag=local(block.tag),
                    block_attr_id=block.get("id"),
                )
            )
    return root, cs


def _append_text(elem: etree._Element, last: etree._Element, text: str) -> etree._Element:
    if not text:
        return last
    if last is elem and len(elem) == 0:
        elem.text = (elem.text or "") + text
    else:
        last.tail = (last.tail or "") + text
    return last


def render_block(
    src_block: etree._Element,
    texts: list[str],
    strip_ruby: bool = True,
) -> list[etree._Element]:
    """把一段或多段译文渲染成新的块元素（超出长度上限时一段一个）。"""
    _, phs = flatten_block(src_block, strip_ruby=strip_ruby)
    tag = src_block.tag
    out: list[etree._Element] = []
    for si, text in enumerate(texts):
        # deepcopy 而不是重新建：namespace、class、id、data-* 全部原样保留
        new = copy.deepcopy(src_block)
        new.text = None
        new.tail = None
        for child in list(new):
            new.remove(child)
        if si > 0 and "id" in new.attrib:
            new.set("id", f"{new.attrib['id']}-s{si}")
        last = new
        pos = 0
        for m in PH_RE.finditer(text):
            last = _append_text(new, last, text[pos:m.start()])
            idx = int(m.group(1))
            if 0 <= idx < len(phs):
                el = copy.deepcopy(phs[idx])
                el.tail = None
                new.append(el)
                last = el
            pos = m.end()
        _append_text(new, last, text[pos:])
        out.append(new)
    return out


def placeholders_of(text: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for m in PH_RE.finditer(text):
        i = int(m.group(1))
        counts[i] = counts.get(i, 0) + 1
    return counts


_AFFIX_PRE = re.compile(r"\A((?:\s*\{\{\d+\}\})+\s*)")
_AFFIX_SUF = re.compile(r"(\s*(?:\{\{\d+\}\})+)\s*\Z")


def detach_affixes(text: str) -> tuple[str, str, str]:
    """把行首/行尾的占位符摘下来，不送给模型。

    实测小模型最常丢的就是行首的 {{0}}（分页锚点、图片）。它们本来也不该
    由模型决定去留，于是干脆物理隔离：只发中间可译文本，回来后原样拼回。
    """
    prefix = ""
    suffix = ""
    m = _AFFIX_PRE.match(text)
    if m:
        prefix = m.group(1)
        text = text[m.end() :]
    m = _AFFIX_SUF.search(text)
    if m:
        suffix = m.group(1)
        text = text[: m.start()]
    return text, prefix, suffix
