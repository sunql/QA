"""Token 计数器抽象基类。

实现统一接口，使上层（模型路由）无需关心具体模型的 tokenizer 差异。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TokenCounter(ABC):
    """Token 计数器接口。"""

    @abstractmethod
    def countTokens(self, modelName: str, text: str) -> int:
        """估算 modelName 模型编码 text 所需的 token 数。

        Args:
            modelName: 模型名，如 gpt-4o / deepseek-chat / llama3.1
            text: 待计数的文本

        Returns:
            非负整数 token 数。
        """

    @abstractmethod
    def isApproximate(self) -> bool:
        """是否为近似计数（用于日志标注与成本估算可信度）。"""
