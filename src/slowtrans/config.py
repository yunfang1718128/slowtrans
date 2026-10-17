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

    def key(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")


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


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
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
    data_dir = Path(raw.get("paths", {}).get("data_dir", "data"))
    return Config(raw=raw, providers=providers, data_dir=data_dir)


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
