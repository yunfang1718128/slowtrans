"""从硅基流动定价页抓免费模型名单（一次性排查工具，不是运行时依赖）。

为什么不用 /v1/models：那个接口只有 id，没有价格。
定价页是 Next.js，RSC payload 里带 modelName/DisplayName/pricing，
把转义还原后就能配对了。
"""
from __future__ import annotations

import json
import re
import urllib.request

URL = "https://siliconflow.cn/pricing"


def fetch() -> str:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
    return raw.replace('\\"', '"')


def main() -> None:
    h = fetch()
    free: dict[str, str] = {}
    priced: dict[str, str] = {}
    for m in re.finditer(r'"DisplayName":"([^"]*)"', h):
        name = m.group(1)
        seg = h[max(0, m.start() - 3500) : m.start() + 800]
        ids = re.findall(r'"modelName":"([^"]+)"', seg)
        if not ids:
            continue
        mid = ids[-1]
        if "Free" in name or "免费" in name:
            free[mid] = name
        prices = re.findall(r'"(?:inputPrice|outputPrice|Price)":"([\d.]+)"', seg)
        if prices:
            priced[mid] = "/".join(prices[:3])

    print(f"=== 明确免费（{len(free)}）===")
    for k in sorted(free):
        print(f"  {k:46s} {free[k]}")
    print("\n=== 有价格（前 40）===")
    for k in sorted(priced)[:40]:
        print(f"  {k:46s} {priced[k]}")

    # 关注款单独打印
    for want in ("Qwen/Qwen2.5-7B-Instruct", "THUDM/GLM-4-9B-0414",
                 "tencent/Hunyuan-MT-7B", "Qwen/Qwen3-8B"):
        idx = h.find(f'"modelName":"{want}"')
        seg = h[idx : idx + 4000] if idx >= 0 else ""
        pr = re.findall(r'"(?:inputPrice|outputPrice|Price|price)":"?([\d.]+)"?', seg)
        print(f"\n{want}: 免费={want in free} 价格字段={pr[:6]}")


if __name__ == "__main__":
    main()
