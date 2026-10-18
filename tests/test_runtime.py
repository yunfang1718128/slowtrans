from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from slowtrans.config import load_config, resolve_data_dir, validate_config
from slowtrans.runtime import (
    engine_from_config,
    in_window,
    options_from_config,
    parse_window,
    seconds_until_window,
)


def test_parse_window():
    assert parse_window("00:00-08:00") == (0, 480)
    assert parse_window("22:30-06:15") == (1350, 375)
    assert parse_window("") is None
    assert parse_window("bad") is None
    assert parse_window("25:00-26:00") is None


def test_in_window_simple_and_cross_midnight():
    assert in_window("00:00-08:00", datetime(2026, 1, 1, 3, 0)) is True
    assert in_window("00:00-08:00", datetime(2026, 1, 1, 9, 0)) is False
    # 跨午夜
    assert in_window("22:00-06:00", datetime(2026, 1, 1, 23, 0)) is True
    assert in_window("22:00-06:00", datetime(2026, 1, 1, 2, 0)) is True
    assert in_window("22:00-06:00", datetime(2026, 1, 1, 12, 0)) is False
    # 空窗口 = 全天
    assert in_window("", datetime(2026, 1, 1, 12, 0)) is True


def test_seconds_until_window():
    assert seconds_until_window("00:00-08:00", datetime(2026, 1, 1, 3, 0)) == 0
    # 09:00 等到次日 00:00 = 15 小时
    assert seconds_until_window("00:00-08:00", datetime(2026, 1, 1, 9, 0)) == 15 * 3600
    # 12:00 等 22:00 = 10 小时
    assert seconds_until_window("22:00-06:00", datetime(2026, 1, 1, 12, 0)) == 10 * 3600


def test_options_follow_config_and_allow_override(tmp_path: Path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        """
[scheduler]
max_retries = 7
backoff_base_s = 2
backoff_max_s = 60
budget_cny = 1.5
window = "01:00-06:00"
breaker_failures = 3

[epub]
bilingual = false
strip_ruby_rt = false
force_horizontal = false
max_segment_chars = 900

[engine]
batch_size = 6
price_in = 2.0
price_out = 8.0
""",
        encoding="utf-8",
    )
    cfg = load_config(cfgfile, tmp_path)
    opts = options_from_config(cfg, src=tmp_path / "a.epub", out=tmp_path / "b.epub")
    assert opts.max_retries == 7
    assert opts.backoff_base == 2
    assert opts.budget_cny == 1.5
    assert opts.window == "01:00-06:00"
    assert opts.breaker_failures == 3
    assert opts.bilingual is False
    assert opts.strip_ruby is False
    assert opts.force_horizontal is False
    assert opts.max_segment_chars == 900
    assert opts.batch_size == 6
    assert opts.price_in == 2.0 and opts.price_out == 8.0

    # 显式覆盖必须压过 config
    over = options_from_config(
        cfg, src=tmp_path / "a.epub", out=tmp_path / "b.epub",
        bilingual=True, max_retries=1, max_segment_chars=1200,
    )
    assert over.bilingual is True
    assert over.max_retries == 1
    assert over.max_segment_chars == 1200


def test_engine_reads_config(tmp_path: Path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        """
[engine]
batch_size = 9
temperature = 0.1
request_timeout_s = 33

[scheduler]
rps = 0.25
""",
        encoding="utf-8",
    )
    cfg = load_config(cfgfile, tmp_path)
    import os

    os.environ["SILICONFLOW_API_KEY"] = "TEST"
    engine = engine_from_config(cfg, "siliconflow", "Qwen/Qwen2.5-7B-Instruct")
    assert engine.batch_size == 9
    assert engine.temperature == 0.1
    assert engine.timeout_s == 33
    assert engine.min_interval == pytest.approx(4.0)


def test_validate_rejects_concurrency(tmp_path: Path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text("[scheduler]\nconcurrency = 2\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        validate_config(load_config(cfgfile, tmp_path))


def test_validate_rejects_bad_window(tmp_path: Path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text('[scheduler]\nwindow = "25:99-99:99"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        validate_config(load_config(cfgfile, tmp_path))


def test_resolve_data_dir_prefers_explicit_then_config(tmp_path: Path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text('[paths]\ndata_dir = "from-config"\n', encoding="utf-8")
    assert resolve_data_dir(cfgfile) == Path("from-config")
    assert resolve_data_dir(cfgfile, "explicit") == Path("explicit")
    assert resolve_data_dir(None) == Path("data")
