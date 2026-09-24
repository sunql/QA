"""文档分块器：把 TextBlock 列表切成 Chunk，并携带定位符。

P0 溯源地基：``Chunk.metadata`` 原本已存在但从未被填充，是现成的扩展点。
本实现把 TextBlock 的页码/章节/段号写进 metadata，供下游写入 Milvus 并
最终支撑证据链的出处展示。
"""

from __future__ import annotations

from app.services.document_parser import TextBlock


class Chunk:
    """单个文本块。

    ``metadata`` 承载定位符（``page_number`` / ``section_name`` /
    ``paragraph_no``），``to_dict()`` 会把它摊平到顶层。
    """

    def __init__(
        self,
        chunk_id: str,
        text: str,
        sequence: int,
        metadata: dict | None = None,
    ) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.sequence = sequence
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "sequence": self.sequence,
            **self.metadata,
        }


def split_by_paragraphs(
    blocks: list[TextBlock],
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """按段落切分文本块，块过长则再按固定长度切。

    **chunk 不跨页**：累积块时遇到页码不同的块，先 flush 再另起，保证任一
    chunk 的 ``page_number`` 对其全部正文都成立（P3 的 evidence 直接吃这个值，
    跨页 chunk 被标成起点页等于证据链指错页）。同一页内跨块的 chunk 其定位符
    仍取**第一个块**（段号取起点段，属可接受近似）。``page_number`` 为 ``None``
    的格式（DOCX/MD）不做页边界切分，维持原累积逻辑。

    Args:
        blocks: 带定位信息的文本块（来自 parse_document）
        chunk_size: 每个 chunk 的最大字符数
        overlap: 相邻 chunk 之间的重叠字符数

    Returns:
        Chunk 列表（sequence 从 0 开始）
    """
    chunks: list[Chunk] = []
    seq = 0
    accumulated: list[TextBlock] = []
    accumulated_len = 0

    def flush() -> None:
        nonlocal seq, accumulated, accumulated_len
        if accumulated:
            chunks.append(_make_chunk(accumulated, seq))
            seq += 1
            accumulated = []
            accumulated_len = 0

    for block in blocks:
        blockLen = len(block.text)
        # 页边界：累积中遇到页码不同的块，先 flush 再另起，保证 chunk 不跨页。
        # page_number 为 None 的格式（DOCX/MD）不做页边界切分。
        if (
            accumulated
            and block.page_number is not None
            and accumulated[0].page_number is not None
            and block.page_number != accumulated[0].page_number
        ):
            flush()
        # 单块超过 chunk_size：先 flush 累积，再按固定长度切（带 overlap）
        if blockLen > chunk_size:
            flush()
            for start in range(0, blockLen, chunk_size - overlap):
                sub = block.text[start : start + chunk_size]
                chunks.append(_make_chunk([TextBlock(
                    text=sub,
                    page_number=block.page_number,
                    section_name=block.section_name,
                    paragraph_no=block.paragraph_no,
                )], seq))
                seq += 1
        elif accumulated_len + blockLen + (1 if accumulated else 0) <= chunk_size:
            if accumulated:
                accumulated_len += 1  # newline
            accumulated.append(block)
            accumulated_len += blockLen
        else:
            # 剩余空间不够放本块，flush 后另起
            flush()
            accumulated = [block]
            accumulated_len = blockLen

    flush()
    return chunks


def _make_chunk(blocks: list[TextBlock], seq: int) -> Chunk:
    """唯一的 Chunk 构造点：所有路径都必须经过它，定位符才不会漏。"""
    text = "\n".join(b.text for b in blocks)
    first = blocks[0]
    return Chunk(
        chunk_id=f"chunk-{seq}",
        text=text,
        sequence=seq,
        metadata={
            "page_number": first.page_number,
            "section_name": first.section_name,
            "paragraph_no": first.paragraph_no,
        },
    )
