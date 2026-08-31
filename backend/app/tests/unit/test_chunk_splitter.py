"""Phase 5.2 chunk_splitter 单测（TDD RED）。

RED: 先写测试（FAIL）→ 实现（GREEN）→ 重构（IMPROVE）。
"""

from __future__ import annotations

import pytest

from app.services.chunk_splitter import Chunk, split_by_paragraphs


class TestSplitByParagraphs:
    """基本切分行为。"""

    def test_empty_text_returns_empty_list(self) -> None:
        result = split_by_paragraphs("")
        assert result == []

    def test_single_short_paragraph(self) -> None:
        text = "这是一个短段落。"
        chunks = split_by_paragraphs(text)
        assert len(chunks) == 1
        assert chunks[0].text == "这是一个短段落。"
        assert chunks[0].sequence == 0

    def test_multiple_paragraphs(self) -> None:
        text = "第一段。\n第二段。\n第三段。"
        # chunk_size=5 强制每个段落独立成块（每段落 4 字符 < 5）
        chunks = split_by_paragraphs(text, chunk_size=5)
        assert len(chunks) == 3
        assert chunks[0].text == "第一段。"
        assert chunks[1].text == "第二段。"
        assert chunks[2].text == "第三段。"

    def test_chunk_size_respected(self) -> None:
        # 制造一个超长段落（> 500 字符）
        long_text = "A" * 1000
        chunks = split_by_paragraphs(long_text, chunk_size=500)
        # 至少切成 2 块
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.text) <= 500

    def test_overlap_between_chunks(self) -> None:
        # 重叠只在多 chunk 场景有意义
        text = "第一段内容。" * 100  # 长文本
        chunks = split_by_paragraphs(text, chunk_size=200, overlap=50)
        if len(chunks) >= 2:
            # 重叠部分的内容应该相同（取末尾 overlap 字符）
            assert chunks[0].text[-50:] == chunks[1].text[:50]

    def test_whitespace_only_lines_ignored(self) -> None:
        text = "第一段。\n   \n\t\n第二段。"
        chunks = split_by_paragraphs(text, chunk_size=5)
        assert len(chunks) == 2
        assert chunks[0].text == "第一段。"
        assert chunks[1].text == "第二段。"

    def test_chunk_has_id_and_sequence(self) -> None:
        text = "第一段。\n第二段。"
        chunks = split_by_paragraphs(text, chunk_size=5)
        assert chunks[0].chunk_id == "chunk-0"
        assert chunks[0].sequence == 0
        assert chunks[1].chunk_id == "chunk-1"
        assert chunks[1].sequence == 1

    def test_chunk_to_dict(self) -> None:
        chunk = Chunk(chunk_id="chunk-0", text="测试文本", sequence=0)
        d = chunk.to_dict()
        assert d["chunk_id"] == "chunk-0"
        assert d["text"] == "测试文本"
        assert d["sequence"] == 0

    def test_paragraph_longer_than_chunk_size_splits(self) -> None:
        # 单一超长段落
        text = "X" * 600
        chunks = split_by_paragraphs(text, chunk_size=200)
        assert len(chunks) >= 3  # 600/200 = 3 块（按 chunk_size 切）
        # 所有 chunk 长度不超过 200
        for c in chunks:
            assert len(c.text) <= 200
