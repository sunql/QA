"""Wiki 问答 prompt 模板 + 纯函数 helper（无 IO，feat-wiki-chat）。

WIKI_QA_SYSTEM_PROMPT：企业 Wiki 知识助手的 system prompt（含引用规则与
「给建议」要求——对话工具不是检索列表，适用时要给出可执行建议）；
WIKI_QA_USER_TEMPLATE：history + question 拼接模板；
format_chunks_json：给 wiki 语义检索 hits 加 id 字段并序列化（LLM 用 id 标注 [1][2]）；
format_history_block：上 N 轮对话拼成纯文本（不含 citations JSON）。
"""
from __future__ import annotations

import json
from typing import Any

WIKI_QA_SYSTEM_PROMPT = """你是企业 Wiki 知识助手。基于以下引用的企业 Wiki 知识条目回答用户问题。

规则：
1. 仅基于提供的知识内容回答，不要编造。
2. 引用处用 [1]、[2] 等编号标注，对应下方引用列表。
3. 知识无相关信息时，明确说明「未在企业 Wiki 中找到相关知识」，不要猜测。
4. 问题适用时，在回答末尾给出可执行的建议（格式：「建议：」开头的条目）。
5. 用中文回答，简洁准确。

引用知识：
{chunks_json}"""

WIKI_QA_USER_TEMPLATE = "{history_block}\n\n当前问题：{question}"


def format_chunks_json(chunks: list[dict[str, Any]]) -> str:
    """为每个 wiki chunk 加 1-indexed id，返回 JSON 字符串给 LLM 当引用表。

    输入：WikiVectorService.searchSemantic 返回的 hit 字典列表（含 pageId /
        title / chunkText / score 等）。
    """
    with_id = [{"id": idx + 1, **c} for idx, c in enumerate(chunks)]
    return json.dumps(with_id, ensure_ascii=False, default=str)


def format_history_block(messages: list[dict[str, str]]) -> str:
    """把上 N 轮对话拼成纯文本 role: content 列表（不含 citations，避免膨胀）。"""
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


__all__ = [
    "WIKI_QA_SYSTEM_PROMPT",
    "WIKI_QA_USER_TEMPLATE",
    "format_chunks_json",
    "format_history_block",
]
