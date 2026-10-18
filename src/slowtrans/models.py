"""免费模型清单（2026-10 实测 + 官方定价页核对）。

这不是"配置文件"，而是给面板用的建议列表：用户可以填任何模型名，
这里只负责把"确认免费、适合翻译"的排在前面，并标注坑。

核对方式：
- 硅基流动定价页 RSC payload 里的 pricing（0/0 = 免费）+ DisplayName 的 (Free) 标记
- 智谱官方模型概览页的"免费文本模型"标注 + 实测可用性
- scripts/scan_free.py 可以重新跑一遍硅基流动那份

⚠️ 免费额度随时会变。真正兜底的是配置里的 price_in/price_out：
免费模型填 0，一旦开始收费，把价格填进去，预算控制自然接管。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelOption:
    id: str
    note: str = ""
    free: bool = True
    default: bool = False


CATALOG: dict[str, list[ModelOption]] = {
    "siliconflow": [
        ModelOption("Qwen/Qwen2.5-7B-Instruct", "免费 · 指令跟随最好 · 默认", free=True, default=True),
        ModelOption("THUDM/GLM-4-9B-0414", "免费 · 中文稳，语感略硬", free=True),
        ModelOption("Qwen/Qwen3-8B", "免费 · 会先思考，需额外 token", free=True),
        ModelOption("tencent/Hunyuan-MT-7B", "免费 · 翻译专用，很快，但不守批量格式", free=True),
        ModelOption("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "免费 · 推理模型，不适合翻译", free=True),
        ModelOption("Qwen/Qwen2.5-14B-Instruct", "输入免费，输出 ¥0.35/M", free=False),
        ModelOption("Qwen/Qwen3-14B", "¥0.5 / ¥0.5 每百万", free=False),
        ModelOption("zai-org/GLM-5.3-Flash", "¥0.8 / ¥2.8 每百万", free=False),
    ],
    "zhipu": [
        ModelOption("glm-4-flash", "免费 · 稳定，默认", free=True, default=True),
        ModelOption("glm-4-flash-250414", "免费 · 同上，快照版", free=True),
        ModelOption("glm-4.5-flash", "免费 · 128K 上下文", free=True),
        ModelOption("glm-4.7-flash", "免费 · 200K 上下文 · 会给思考留 token", free=True),
    ],
}

# 面板里「添加自定义服务商」的预填模板：任何 OpenAI 兼容 endpoint 都能用
CUSTOM_PRESETS = [
    {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"name": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"name": "moonshot", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"name": "ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    {"name": "vllm", "base_url": "http://127.0.0.1:8000/v1", "model": "local-model"},
]


def default_model(provider: str) -> str:
    for opt in CATALOG.get(provider, []):
        if opt.default:
            return opt.id
    return ""
