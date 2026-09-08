"""纯模块 rag_qa_prompt 测试。"""
import json

from app.services.rag_qa_prompt import (
    DOC_QA_SYSTEM_PROMPT,
    DOC_QA_USER_TEMPLATE,
    format_chunks_json,
    format_history_block,
)


def test_format_chunks_json_assigns_ids_one_indexed() -> None:
    chunks = [
        {"document_id": "DOC-A", "document_name": "合同", "chunk_text": "条款1"},
        {"document_id": "DOC-B", "document_name": "指南", "chunk_text": "段落"},
    ]
    result = format_chunks_json(chunks)
    parsed = json.loads(result)
    assert parsed[0]["id"] == 1
    assert parsed[1]["id"] == 2
    assert parsed[0]["document_name"] == "合同"


def test_format_history_block_plain_text_only() -> None:
    """历史块只含 role: content，不含 citations JSON。"""
    msgs = [
        {"role": "user", "content": "什么是质量协议？"},
        {"role": "assistant", "content": "质量协议是..."},
    ]
    out = format_history_block(msgs)
    assert "user: 什么是质量协议？" in out
    assert "assistant: 质量协议是..." in out
    assert "citations" not in out
    assert "{" not in out  # 不应含 JSON 结构


def test_system_prompt_contains_chunk_placeholder() -> None:
    assert "{chunks_json}" in DOC_QA_SYSTEM_PROMPT
    assert "[1]" in DOC_QA_SYSTEM_PROMPT  # 引用规则提示


def test_user_template_has_placeholders() -> None:
    assert "{history_block}" in DOC_QA_USER_TEMPLATE
    assert "{question}" in DOC_QA_USER_TEMPLATE
