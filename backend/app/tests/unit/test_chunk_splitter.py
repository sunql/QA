"""chunk_splitter 单测：切分行为 + 定位符透传（P0 溯源地基）。"""

from __future__ import annotations

import pytest

from app.services.chunk_splitter import Chunk, split_by_paragraphs
from app.services.document_parser import TextBlock


def _block(text: str, *, page: int | None = None, section: str | None = None, para: int = 1) -> TextBlock:
    return TextBlock(text=text, page_number=page, section_name=section, paragraph_no=para)


class TestSplitBehavior:
    """保持既有切分语义不回归。"""

    def test_empty_blocks_returns_empty_list(self) -> None:
        assert split_by_paragraphs([]) == []

    def test_single_short_block(self) -> None:
        chunks = split_by_paragraphs([_block("这是一个短段落。")])
        assert len(chunks) == 1
        assert chunks[0].text == "这是一个短段落。"
        assert chunks[0].sequence == 0

    def test_multiple_blocks(self) -> None:
        blocks = [_block("第一段。", para=1), _block("第二段。", para=2), _block("第三段。", para=3)]
        chunks = split_by_paragraphs(blocks, chunk_size=5)
        assert len(chunks) == 3
        assert [c.text for c in chunks] == ["第一段。", "第二段。", "第三段。"]

    def test_chunk_size_respected(self) -> None:
        chunks = split_by_paragraphs([_block("A" * 1000)], chunk_size=500)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.text) <= 500

    def test_overlap_between_chunks(self) -> None:
        chunks = split_by_paragraphs([_block("第一段内容。" * 100)], chunk_size=200, overlap=50)
        assert len(chunks) >= 2
        assert chunks[0].text[-50:] == chunks[1].text[:50]

    def test_chunk_has_id_and_sequence(self) -> None:
        chunks = split_by_paragraphs([_block("第一段。"), _block("第二段。")], chunk_size=5)
        assert chunks[0].chunk_id == "chunk-0"
        assert chunks[0].sequence == 0
        assert chunks[1].chunk_id == "chunk-1"
        assert chunks[1].sequence == 1


class TestLocators:
    """定位符必须一路带到 Chunk，含超长段落分支。"""

    def test_locator_carried_to_chunk(self) -> None:
        chunks = split_by_paragraphs([_block("正文", page=18, section="质量管理", para=3)])
        assert chunks[0].metadata["page_number"] == 18
        assert chunks[0].metadata["section_name"] == "质量管理"
        assert chunks[0].metadata["paragraph_no"] == 3

    def test_overlong_block_keeps_locator(self) -> None:
        """超长段落走的是另一条构造路径，定位符不能漏。"""
        chunks = split_by_paragraphs([_block("X" * 600, page=7, para=2)], chunk_size=200)
        assert len(chunks) >= 3
        for c in chunks:
            assert c.metadata["page_number"] == 7
            assert c.metadata["paragraph_no"] == 2

    def test_chunk_spanning_blocks_takes_first_locator(self) -> None:
        blocks = [_block("甲", page=1, para=1), _block("乙", page=1, para=2)]
        chunks = split_by_paragraphs(blocks, chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0].metadata["paragraph_no"] == 1

    def test_to_dict_flattens_locators(self) -> None:
        chunks = split_by_paragraphs([_block("内容", page=5, para=9)])
        d = chunks[0].to_dict()
        assert d["chunk_id"] == "chunk-0"
        assert d["page_number"] == 5
        assert d["paragraph_no"] == 9

    def test_locator_none_values_preserved(self) -> None:
        chunks = split_by_paragraphs([_block("纯文本")])
        assert chunks[0].metadata["page_number"] is None
        assert chunks[0].metadata["section_name"] is None


class TestChunkToDict:
    def test_chunk_to_dict_basic(self) -> None:
        chunk = Chunk(chunk_id="chunk-0", text="测试文本", sequence=0)
        d = chunk.to_dict()
        assert d["chunk_id"] == "chunk-0"
        assert d["text"] == "测试文本"
        assert d["sequence"] == 0
