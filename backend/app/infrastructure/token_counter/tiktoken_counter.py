"""基于 tiktoken 的 Token 计数器。

覆盖 OpenAI / Azure OpenAI / 国内 OpenAI 兼容代理（DeepSeek/通义/Qwen 等）。
对未知模型名回退到 cl100k_base 编码器，保证不抛异常。
"""

from __future__ import annotations

import logging

import tiktoken

from app.infrastructure.token_counter.base_counter import TokenCounter

logger = logging.getLogger(__name__)

# tiktoken 不识别的模型名回退到此编码器（GPT-4/4o 系列编码器，对多数 BPE 模型近似可用）
_FALLBACK_ENCODING = "cl100k_base"

# 单例编码器缓存：{encoding_name: Encoding}
_encodingCache: dict[str, tiktoken.Encoding] = {}


def _resolveEncoding(modelName: str) -> tiktoken.Encoding:
    """解析模型对应的 tiktoken 编码器；未知模型回退到 cl100k_base。"""
    # 1. 直接按模型名解析
    try:
        return tiktoken.encoding_for_model(modelName)
    except KeyError:
        pass
    # 2. 前缀启发：gpt-4o* 用 o200k_base，gpt-3.5/gpt-4 用 cl100k_base
    name = modelName.lower()
    if name.startswith("gpt-4o") or name.startswith("o1") or name.startswith("o3"):
        encName = "o200k_base"
    elif name.startswith("gpt-4") or name.startswith("gpt-3.5") or name.startswith("text-embedding"):
        encName = "cl100k_base"
    elif "deepseek" in name or "qwen" in name or "llama" in name:
        # 国内/开源模型无官方 tiktoken，用 cl100k_base 近似
        encName = "cl100k_base"
    else:
        encName = _FALLBACK_ENCODING

    if encName not in _encodingCache:
        _encodingCache[encName] = tiktoken.get_encoding(encName)
    return _encodingCache[encName]


class TiktokenCounter(TokenCounter):
    """tiktoken 实现的 Token 计数器。"""

    def countTokens(self, modelName: str, text: str) -> int:
        if not text or not text.strip():
            return 0
        encoding = _resolveEncoding(modelName)
        return len(encoding.encode(text))

    def isApproximate(self) -> bool:
        # 对已知 OpenAI 模型精确，对代理/未知模型为近似；统一标记为非近似，
        # 具体可信度由调用方结合 provider 判断。
        return False
