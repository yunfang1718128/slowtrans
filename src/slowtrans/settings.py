"""运行期设置：面板里填的 API key、默认引擎、书库路径。

优先级刻意设计成 **文件 > 环境变量**：
- 面板里显式填过的东西，就应该是生效的东西；
- 服务器用 EnvironmentFile 注入的 key 作为兜底，方便脚本化部署。

key 明文只落在 data/settings.json（600）。API 返回一律走掩码，
日志里也绝不打印。这是硬规矩。
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SETTINGS_NAME = "settings.json"


@dataclass
class Settings:
    path: Path
    data: dict[str, Any]

    @property
    def providers(self) -> dict[str, dict]:
        return self.data.setdefault("providers", {})

    @property
    def defaults(self) -> dict[str, Any]:
        return self.data.setdefault("defaults", {})

    @property
    def library(self) -> dict[str, Any]:
        return self.data.setdefault("library", {})

    def provider_key(self, name: str) -> str:
        return str(self.providers.get(name, {}).get("api_key") or "")

    def library_root(self, fallback: Path) -> Path:
        raw = self.library.get("root") or ""
        return Path(raw).expanduser() if raw else fallback

    def masked(self, provider_names: list[str] | None = None) -> dict[str, Any]:
        """给面板看的安全视图：只透露前缀和长度，不返回明文。

        provider_names 用来把「环境变量里已经配好、但文件里没有」的情况也如实报出来，
        否则面板会显示"未配置"，而实际任务跑得通，非常误导。
        """
        names = list(dict.fromkeys(list(self.providers) + list(provider_names or [])))
        out: dict[str, Any] = {"providers": {}, "defaults": self.defaults, "library": self.library}
        for name in names:
            key = self.provider_key(name)
            out["providers"][name] = {
                "configured": bool(key),
                "hint": mask(key),
                "source": "file" if key else ("env" if os.environ.get(_env_name(name)) else "none"),
            }
        return out


def _env_name(provider: str) -> str:
    return {
        "siliconflow": "SILICONFLOW_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
    }.get(provider, provider.upper() + "_API_KEY")


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}（{len(key)} 字符）"


def load_settings(data_dir: str | Path) -> Settings:
    path = Path(data_dir) / SETTINGS_NAME
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    return Settings(path=path, data=data)


def save_settings(settings: Settings, patch: dict[str, Any]) -> Settings:
    """只合并 patch 里出现的键；api_key 传空字符串表示“不要改”。"""
    data = settings.data
    for section in ("providers", "defaults", "library"):
        incoming = patch.get(section)
        if not isinstance(incoming, dict):
            continue
        target = data.setdefault(section, {})
        for k, v in incoming.items():
            if section == "providers" and isinstance(v, dict):
                cur = target.setdefault(k, {})
                if "api_key" in v:
                    key = str(v["api_key"] or "")
                    if key:  # 空 = 保持不变，避免面板误清空
                        cur["api_key"] = key
                continue
            target[k] = v
    settings.path.parent.mkdir(parents=True, exist_ok=True)
    settings.path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:  # 只有 POSIX 有意义；Windows 上退化为默认权限
        os.chmod(settings.path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return settings
