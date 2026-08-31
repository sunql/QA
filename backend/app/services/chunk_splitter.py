"""Phase 5.2 文档分块器（TDD RED 占位）。

按段落/标题/固定长度切分文本，返回 chunk 列表供向量化使用。
"""

from __future__ import annotations


class Chunk:
    """单个文本块。"""

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
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """按段落切分文本，段落过长则再按固定长度切。

    Args:
        text: 原始纯文本
        chunk_size: 每个 chunk 的最大字符数
        overlap:  相邻 chunk 之间的重叠字符数

    Returns:
        Chunk 列表（sequence 从 0 开始）
    """
    # 按换行分段
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[Chunk] = []
    seq = 0
    accumulated: list[str] = []
    accumulated_len = 0

    for para in paragraphs:
        para_len = len(para)
        # 单段落超过 chunk_size，切成小段
        if para_len > chunk_size:
            # 先 flush 当前累积的段落
            if accumulated:
                chunks.append(_make_chunk(accumulated, seq))
                seq += 1
                accumulated = []
                accumulated_len = 0
            # 把超长段落按固定长度切（带 overlap）
            for start in range(0, para_len, chunk_size - overlap):
                end = start + chunk_size
                sub = para[start:end]
                chunks.append(Chunk(chunk_id=f"chunk-{seq}", text=sub, sequence=seq))
                seq += 1
        elif accumulated_len + para_len + (1 if accumulated else 0) <= chunk_size:
            # 可以接在当前 chunk 后面
            if accumulated:
                accumulated_len += 1  # newline
            accumulated.append(para)
            accumulated_len += para_len
        else:
            # flush 当前，剩余空间不够放本段落；直接开始新 chunk
            chunks.append(_make_chunk(accumulated, seq))
            seq += 1
            accumulated = [para]
            accumulated_len = para_len

    if accumulated:
        chunks.append(_make_chunk(accumulated, seq))

    return chunks


def _make_chunk(paragraphs: list[str], seq: int) -> Chunk:
    text = "\n".join(paragraphs)
    return Chunk(chunk_id=f"chunk-{seq}", text=text, sequence=seq)
