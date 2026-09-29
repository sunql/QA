"""LLM 回复 JSON fence 剥离（SSOT）。

背景：deepseek-chat 等模型普遍把 JSON 包在 ```json ... ``` 围栏里，裸
``json.loads`` 必失败。此前 ``data_quality_rule_llm_service`` 与
``nl2sql_service`` 各持一份同语义实现（跨服务耦合的取舍），B6（M7
Hypothesis Hook）需要第三份时按 DRY 收敛为本模块：

- 新消费方一律 import 本模块的 ``stripJsonFence``，不要再复制；
- 既有 ``data_quality_rule_llm_service._stripJsonFence`` 已改为转发别名；
- ``nl2sql_service`` 的解析语义不同（fence 之外还有裸 JSON 提取与计划校验），
  保持独立，不在本次收口范围。
"""

from __future__ import annotations

import re

# 匹配 ```json ... ``` 或 ``` ... ``` 代码块（含可选 json 语言标记）；
# re.DOTALL 跨行匹配；re.IGNORECASE 兼容 ```JSON``` 大小写。
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def stripJsonFence(content: str) -> str:
    """从 LLM 回复中抽出 JSON 文本：优先 ```json/``` 代码块，否则原样返回。

    返回的字符串不保证可被 json.loads 解析（仍可能不是 JSON）；仅负责剥掉 fence。
    """
    if not content:
        return content
    match = _JSON_FENCE_RE.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()
