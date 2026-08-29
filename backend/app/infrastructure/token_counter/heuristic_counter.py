"""启发式 Token 计数器（近似）。

用于 Ollama 本地模型与无法确定 tokenizer 的模型。
策略：CJK 字符（中日韩）按 1 token/字符近似；其余按 4 字符/token。
"""

from __future__ import annotations

from app.infrastructure.token_counter.base_counter import TokenCounter

# CJK 统一表意文字、CJK 符号和标点、全角字符
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
    (0x3400, 0x4DBF),    # CJK Extension A
)

_NON_CJK_CHARS_PER_TOKEN = 4


def _isCjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


class HeuristicCounter(TokenCounter):
    """启发式近似 Token 计数器。"""

    def countTokens(self, modelName: str, text: str) -> int:
        if not text or not text.strip():
            return 0
        cjkCount = 0
        nonCjkCount = 0
        for ch in text:
            if _isCjk(ch):
                cjkCount += 1
            else:
                nonCjkCount += 1
        # CJK 字符 ~1 token/字符；非 CJK ~4 字符/token
        nonCjkTokens = (nonCjkCount + _NON_CJK_CHARS_PER_TOKEN - 1) // _NON_CJK_CHARS_PER_TOKEN
        return cjkCount + nonCjkTokens

    def isApproximate(self) -> bool:
        return True
