"""EPUB 的 OCF/OPF 解析：只做必要的事，不引入整本载入内存的库。"""
from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from typing import Iterator

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

XML_PARSER = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=False)


def parse_xml(data: bytes):
    return etree.fromstring(data, parser=XML_PARSER)


def local(tag: str) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else str(tag)


@dataclass
class ManifestItem:
    id: str
    href: str          # 相对 OPF 的路径，已规范化
    media_type: str
    properties: str = ""


@dataclass
class Package:
    opf_path: str
    title: str
    language: str
    manifest: dict[str, ManifestItem]
    spine: list[str]   # manifest id 列表，按阅读顺序


def find_opf(zf: zipfile.ZipFile) -> str:
    data = zf.read("META-INF/container.xml")
    root = parse_xml(data)
    for el in root.iter():
        if local(el.tag) == "rootfile":
            path = el.get("full-path")
            if path:
                return path
    raise ValueError("container.xml 里没有 rootfile")


def read_package(zf: zipfile.ZipFile) -> Package:
    opf_path = find_opf(zf)
    root = parse_xml(zf.read(opf_path))
    base = posixpath.dirname(opf_path)

    manifest: dict[str, ManifestItem] = {}
    spine_ids: list[str] = []
    title = ""
    language = ""

    for el in root.iter():
        name = local(el.tag)
        if name == "item":
            href = el.get("href")
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(base, href)) if base else href
            item = ManifestItem(
                id=el.get("id", ""),
                href=full,
                media_type=el.get("media-type", ""),
                properties=el.get("properties", ""),
            )
            manifest[item.id] = item
        elif name == "itemref":
            idref = el.get("idref")
            if idref:
                spine_ids.append(idref)
        elif name == "title" and not title:
            title = (el.text or "").strip()
        elif name == "language" and not language:
            language = (el.text or "").strip()

    return Package(opf_path, title, language, manifest, spine_ids)


def spine_items(pkg: Package) -> Iterator[tuple[int, ManifestItem]]:
    """按阅读顺序产出 (连续序号, 条目)。序号必须连续：它既是断点续传的键，
    也是 writer 里重新解析章节时的定位依据。"""
    n = 0
    for idref in pkg.spine:
        item = pkg.manifest.get(idref)
        if item and item.media_type in ("application/xhtml+xml", "text/html"):
            yield n, item
            n += 1


def iter_entries(zf: zipfile.ZipFile) -> Iterator[zipfile.ZipInfo]:
    for info in zf.infolist():
        if info.is_dir():
            continue
        yield info
