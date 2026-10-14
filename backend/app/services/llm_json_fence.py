"""LLM 回复 JSON fence 剥离（SSOT）。

背景：deepseek-chat 等模型普遍把 JSON 包在 ```json ... ``` 围栏里，裸
``json.loads`` 必失败。此前 ``data_quality_rule_llm_service`` 与
``nl2sql_service`` 各持一份同语义实现（跨服务耦合的取舍），B6（M7
Hypothesis Hook）需要第三份时按 DRY 收敛为本模块：

- 新消费方一律 import 本模块的 ``stripJsonFence``，不要再复制；
- 既有 ``data_quality_rule_llm_service._stripJsonFence`` 已改为转发别名；
- ``nl2sql_service`` 的解析语义不同（fence 之外还有裸 JSON 提取与计划校验），
  保持独立，不在本次收口范围。

2026-10-03 追加：``stripJsonFence`` 现在**先剥离推理模型的 ``<think>`` 块**再抽
围栏。推理模型把思维链内联在回复开头，思维链里的示例围栏会被本函数的
``_JSON_FENCE_RE.search`` 命中并**静默当成模型输出返回**（无任何错误信号）。
``nl2sql_plan._parsePlanOutcome`` 与 ``nl2sql_service.parseSqlFromResponse`` 是
同一问题的另外两个出口，各自独立剥离（它们的解析语义不在本模块收口范围）。
"""

from __future__ import annotations

import re

from app.services.think_block import stripThinkBlocks

# 匹配 ```json ... ``` 或 ``` ... ``` 代码块（含可选 json 语言标记）；
# re.DOTALL 跨行匹配；re.IGNORECASE 兼容 ```JSON``` 大小写。
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def stripJsonFence(content: str) -> str:
    """从 LLM 回复中抽出 JSON 文本：剥离 <think> 块后优先 ```json/``` 代码块。

    返回的字符串不保证可被 json.loads 解析（仍可能不是 JSON）；仅负责剥掉
    think 与 fence。think 剥离**无条件执行，与 Think_Hide 无关**——该参数管
    「给用户看什么」，本函数服务的是「机器能读什么」，两者正交。
    """
    if not content:
        return content
    content = stripThinkBlocks(content)
    match = _JSON_FENCE_RE.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()
