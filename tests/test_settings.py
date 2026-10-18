from __future__ import annotations

from pathlib import Path

import pytest

from slowtrans.library import cover_bytes, resolve_inside, scan_library
from slowtrans.settings import load_settings, mask, save_settings


def test_settings_mask_never_leaks_full_key(tmp_path: Path):
    s = load_settings(tmp_path)
    save_settings(s, {"providers": {"siliconflow": {"api_key": "TEST-KEY-abcdefghijklmnop"}}})
    view = load_settings(tmp_path).masked(["siliconflow", "zhipu"])
    hint = view["providers"]["siliconflow"]["hint"]
    assert "abcdefghij" not in hint
    assert hint.startswith("TEST-K")
    assert view["providers"]["siliconflow"]["configured"] is True
    # 文件里没有的 provider 也要报出来（否则面板会误显示"未配置"）
    assert "zhipu" in view["providers"]


def test_settings_blank_key_does_not_clear(tmp_path: Path):
    s = load_settings(tmp_path)
    save_settings(s, {"providers": {"zhipu": {"api_key": "abc.def"}}})
    save_settings(load_settings(tmp_path), {"providers": {"zhipu": {"api_key": ""}}})
    assert load_settings(tmp_path).provider_key("zhipu") == "abc.def"


def test_settings_defaults_merge(tmp_path: Path):
    s = load_settings(tmp_path)
    save_settings(s, {"defaults": {"provider": "zhipu", "model": "glm-4-flash"}})
    save_settings(load_settings(tmp_path), {"defaults": {"model": "glm-4.5-flash"}})
    d = load_settings(tmp_path).defaults
    assert d["provider"] == "zhipu" and d["model"] == "glm-4.5-flash"


def test_mask_short_key():
    assert mask("abc") == "***"
    assert mask("") == ""


def test_library_scan(sample_epub, tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "book.epub").write_bytes(sample_epub.read_bytes())
    books = scan_library(root)
    assert len(books) == 1
    assert books[0].title == "Test Book"
    assert books[0].rel == "book.epub"
    # 这本测试书没有封面图，应当明确报"没有封面"而不是返回垃圾
    with pytest.raises(LookupError):
        cover_bytes(root, "book.epub")


def test_library_rejects_path_traversal(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    for bad in ["../x.epub", "/etc/passwd", "a/../../b.epub"]:
        try:
            resolve_inside(root, bad)
        except Exception:
            pass
        else:
            raise AssertionError(f"应当拒绝：{bad}")
