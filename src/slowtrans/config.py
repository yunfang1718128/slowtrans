"""配置加载：TOML 文件 + 环境变量。

设计原则：所有会变的都放配置，代码里不写死模型名和价格。
免费模型价格填 0，预算控制自然失效——这正是我们想要的。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = """
[engine]
provider = "siliconflow"
model = "Qwen/Qwen2.5-7B-Instruct"
batch_size = 4
temperature = 0.3
max_output_tokens = 3072
request_timeout_s = 60
price_in = 0.0        # 元/百万 token，预算控制用；免费模型填 0
price_out = 0.0

[scheduler]
concurrency = 1
rps = 0.0            # 0 = 不限速；慢速部署可设 0.5
max_retries = 3
backoff_base_s = 5
backoff_max_s = 300
breaker_failures = 8
breaker_cooldown_s = 600
budget_cny = 0.0     # 0 = 不限制
window = ""          # 例 "00:00-08:00"

[epub]
bilingual = true
max_segment_chars = 1200
strip_ruby_rt = true
force_horizontal = true

[providers.siliconflow]
base_url = "https://api.siliconflow.cn/v1"
api_key_env = "SILICONFLOW_API_KEY"

[providers.zhipu]
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZHIPU_API_KEY"
"""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    api_key: str = ""
    file_key: str = ""       # 面板里填的 key，优先于环境变量

    def key(self) -> str:
        return self.file_key or self.api_key or os.environ.get(self.api_key_env, "")


@dataclass
class Config:
    raw: dict[str, Any]
    providers: dict[str, Provider] = field(default_factory=dict)
    data_dir: Path = Path("data")

    @property
    def engine_cfg(self) -> dict[str, Any]:
        return self.raw.get("engine", {})

    @property
    def sched_cfg(self) -> dict[str, Any]:
        return self.raw.get("scheduler", {})

    @property
    def epub_cfg(self) -> dict[str, Any]:
        return self.raw.get("epub", {})

    def provider(self, name: str) -> Provider:
        if name not in self.providers:
            raise SystemExit(f"未知 provider: {name}（可用：{', '.join(self.providers)}）")
        return self.providers[name]

    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "slowtrans.db"


_CACHE: dict[tuple[str, str], tuple[tuple[float, float], "Config"]] = {}


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def load_config(
    path: str | os.PathLike[str] | None = None, data_dir: str | os.PathLike[str] | None = None
) -> Config:
    """带 mtime 缓存：HTTP 每个请求要读 2-3 次配置，别每次都碰磁盘。"""
    p = Path(path) if path else None
    base = Path(data_dir) if data_dir else Path("data")
    key = (str(p or ""), str(base))
    stamp = (_mtime(p) if p else 0.0, _mtime(base / "settings.json"))
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    cfg = _build_config(p, data_dir)
    _CACHE[key] = (stamp, cfg)
    return cfg


def _build_config(
    path: str | os.PathLike[str] | None = None, data_dir: str | os.PathLike[str] | None = None
) -> Config:
    raw: dict[str, Any] = tomllib.loads(DEFAULT_CONFIG)
    if path:
        p = Path(path)
        if not p.exists():
            raise SystemExit(f"配置文件不存在：{p}")
        raw = _merge(raw, tomllib.loads(p.read_text(encoding="utf-8")))
    providers = {
        name: Provider(name=name, base_url=v["base_url"], api_key_env=v["api_key_env"])
        for name, v in raw.get("providers", {}).items()
    }
    base_dir = Path(data_dir) if data_dir else Path(raw.get("paths", {}).get("data_dir", "data"))
    # 面板里填过的设置优先于 TOML 与环境变量：显式操作应该压过默认值
    from .settings import load_settings

    s = load_settings(base_dir)
    # 面板里可以新增任意 OpenAI 兼容服务商：settings 里带 base_url 就建一个 Provider
    for name, scfg in s.providers.items():
        if name not in providers:
            if not scfg.get("base_url"):
                continue
            providers[name] = Provider(
                name=name,
                base_url=str(scfg["base_url"]),
                api_key_env=str(scfg.get("api_key_env") or (name.upper() + "_API_KEY")),
            )
        elif scfg.get("base_url"):
            providers[name].base_url = str(scfg["base_url"])
        providers[name].file_key = str(scfg.get("api_key") or "")
    defaults = s.defaults or {}
    if defaults.get("provider"):
        raw.setdefault("engine", {})["provider"] = defaults["provider"]
    if defaults.get("model"):
        raw.setdefault("engine", {})["model"] = defaults["model"]
    return Config(raw=raw, providers=providers, data_dir=base_dir)


def _merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_engine_spec(spec: str) -> tuple[str, str]:
    """'siliconflow:Qwen/Qwen2.5-7B-Instruct' -> (provider, model)。"""
    if ":" in spec:
        provider, model = spec.split(":", 1)
        return provider.strip(), model.strip()
    return "siliconflow", spec.strip()
