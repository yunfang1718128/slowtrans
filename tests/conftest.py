from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title><dc:language>en</dc:language>
    <dc:identifier id="id">urn:uuid:test</dc:identifier>
  </metadata>
  <manifest>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>
"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter One</title>
<link rel="stylesheet" href="style.css"/></head>
<body>
  <h1>Chapter One</h1>
  <p>Fantasy is a conversation among many traditions, and it keeps changing.</p>
  <p>He said <em>nothing</em> at all, then left. <span id="pg1"/>The end comes later.</p>
  <div class="cover"><img src="cover.jpg" alt=""/></div>
</body></html>
"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter Two</title></head>
<body>
  <h2>Chapter Two</h2>
  <blockquote><p>Nested blocks must not be double-counted.</p></blockquote>
  <ul><li>First item about dragons.</li><li>Second item about maps.</li></ul>
</body></html>
"""


def build_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/ch1.xhtml", CH1)
        z.writestr("OEBPS/ch2.xhtml", CH2)
        z.writestr("OEBPS/style.css", "body { writing-mode: vertical-rl; }")
    return path


@pytest.fixture()
def sample_epub(tmp_path: Path) -> Path:
    return build_epub(tmp_path / "sample.epub")
