"""文档问答 prompt 模板 + 纯函数 helper（无 IO）。

DOC_QA_SYSTEM_PROMPT：包含引用规则的 system prompt；
DOC_QA_USER_TEMPLATE：history + question 拼接模板；
format_chunks_json：给 chunks 列表加 id 字段并序列化为 JSON 字符串（LLM 用 id 标注 [1][2]）；
format_history_block：上 5 轮对话拼成纯文本（不含 citations JSON）。
"""
from __future__ import annotations

import json
from typing import Any

DOC_QA_SYSTEM_PROMPT = """你是文档问答助手。基于以下引用的文档内容回答用户问题。

规则：
1. 仅基于提供的文档内容回答，不要编造。
2. 引用处用 [1]、[2] 等编号标注，对应下方引用列表。
3. 文档无相关信息时，明确说明「未在已上传文档中找到相关依据」。
4. 用中文回答，简洁准确。

引用文档：
{chunks_json}"""

DOC_QA_USER_TEMPLATE = "{history_block}\n\n当前问题：{question}"


def format_chunks_json(chunks: list[dict[str, Any]]) -> str:
    """为每个 chunk 加 1-indexed id，返回 JSON 字符串给 LLM 当引用表。

    输入：rag_service.searchDocuments 返回的 chunk 字典列表（含 document_id /
        document_name / chunk_text / score 等）。
    输出：JSON 字符串，每项含 id 字段 + 原字段拷贝。
    """
    with_id = [{"id": idx + 1, **c} for idx, c in enumerate(chunks)]
    return json.dumps(with_id, ensure_ascii=False, default=str)


def format_history_block(messages: list[dict[str, str]]) -> str:
    """把上 N 轮对话拼成纯文本 role: content 列表。

    不含 citations JSON（避免 prompt 膨胀）。
    返回新字符串，不修改入参。
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


__all__ = [
    "DOC_QA_SYSTEM_PROMPT",
    "DOC_QA_USER_TEMPLATE",
    "format_chunks_json",
    "format_history_block",
]
