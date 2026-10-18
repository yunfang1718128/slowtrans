"""书库：扫描 epub、读元数据、抽封面。

服务器上没人会手敲路径。面板需要一个"书架"：封面 + 书名点一下就能开工。
这里只做最低成本的事——读 zip 里的 OPF，取 title/author/cover，不解析整本书。
"""
from __future__ import annotations

import hashlib
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .epub.ocf import Package, read_package

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


@dataclass
class BookInfo:
    rel: str
    name: str
    size: int
    mtime: float
    title: str = ""
    authors: list[str] = field(default_factory=list)
    language: str = ""
    cover_href: str = ""
    error: str = ""

    @property
    def display(self) -> str:
        return self.title or Path(self.name).stem


def scan_library(root: Path, limit: int = 300) -> list[BookInfo]:
    root = root.expanduser()
    if not root.exists():
        return []
    out: list[BookInfo] = []
    for p in sorted(root.rglob("*.epub"), key=lambda x: x.stat().st_mtime, reverse=True):
        if len(out) >= limit:
            break
        try:
            st = p.stat()
            info = BookInfo(
                rel=p.relative_to(root).as_posix(),
                name=p.name,
                size=st.st_size,
                mtime=st.st_mtime,
            )
            try:
                with zipfile.ZipFile(p) as zf:
                    pkg = read_package(zf)
                    info.title = pkg.title
                    info.authors = pkg.authors
                    info.language = pkg.language
                    info.cover_href = pkg.cover_href
            except Exception as exc:
                info.error = f"元数据读取失败：{exc}"
            out.append(info)
        except (OSError, ValueError):
            continue
    return out


def resolve_inside(root: Path, rel: str) -> Path:
    """把面板传来的相对路径钉死在书库根目录内，防止 ../ 穿越。"""
    root = root.expanduser().resolve()
    p = (root / rel).resolve()
    if root != p and root not in p.parents:
        raise PermissionError("路径越界")
    if p.suffix.lower() != ".epub":
        raise PermissionError("只允许 epub")
    if not p.exists():
        raise FileNotFoundError(rel)
    return p


def cover_bytes(
    root: Path, rel: str, cache_dir: Path | None = None, max_bytes: int = 2_500_000
) -> tuple[bytes, str]:
    """返回 (图片字节, content-type)。抽不到就抛 LookupError，由调用方给占位图。"""
    path = resolve_inside(root, rel)
    key = hashlib.sha1(f"{path}|{path.stat().st_mtime}".encode()).hexdigest()[:16]
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for ext, ctype in ((e, t) for t, e in IMAGE_TYPES.items()):
            cached = cache_dir / f"{key}{ext}"
            if cached.exists():
                return cached.read_bytes(), ctype

    with zipfile.ZipFile(path) as zf:
        pkg = read_package(zf)
        href = pkg.cover_href or _fallback_cover(pkg)
        if not href or href not in zf.namelist():
            raise LookupError("没有封面")
        data = zf.read(href)
        if len(data) > max_bytes:
            raise LookupError("封面太大")
        ctype = _guess_type(href)

    if cache_dir:
        ext = IMAGE_TYPES.get(ctype, ".jpg")
        (cache_dir / f"{key}{ext}").write_bytes(data)
    return data, ctype


def _fallback_cover(pkg: Package) -> str:
    for item in pkg.manifest.values():
        if item.media_type in IMAGE_TYPES and "cover" in item.href.lower():
            return item.href
    for item in pkg.manifest.values():
        if item.media_type in IMAGE_TYPES:
            return item.href
    return ""


def _guess_type(href: str) -> str:
    ext = posixpath.splitext(href)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }.get(ext, "image/jpeg")
