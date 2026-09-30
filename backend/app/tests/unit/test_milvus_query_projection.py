"""本体向量全量读的字段投影：必须含 alias / description。

背景（2026-09-30）：`_queryAllRowsFromCollection` 只读
``["ontology_id", "type", "name", "external_id"]``。`_cleanup` 用它读全量后，
step 5 要按 alias/description 写回重建，于是 `r["alias"]` 直接 KeyError ——
而 **drop 已经执行**，集合被清空后脚本崩溃。这正是「cleanup 每跑一次向量就多一套、
且线上集合时不时空掉」的机制：drop → 崩 → 重新 sync → append 累积重复。

alias/description 本来就在 schema 里且不含向量（不违反「不读 1024 维」的初衷），
补齐即可。
"""

from __future__ import annotations

import pytest

import app.infrastructure.milvus_query_helpers as helpers
from app.infrastructure.milvus_query_helpers import _queryAllRowsFromCollection


class _RecordingCollection:
    def __init__(self) -> None:
        self.outputFields: list[str] | None = None

    def load(self) -> None:
        pass

    def query(self, expr: str, output_fields: list[str], limit: int) -> list[dict]:
        self.outputFields = list(output_fields)
        return []


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingCollection:
    fake = _RecordingCollection()
    monkeypatch.setattr(helpers, "_ensureCollectionByName", lambda name: fake)
    return fake


class TestFullReadProjection:
    def test_reads_alias_and_description(self, recorder) -> None:
        """重建路径需要 alias/description —— 缺一则 cleanup 在 drop 之后崩溃。"""
        _queryAllRowsFromCollection("ontology_class_embeddings")

        assert recorder.outputFields is not None
        for field in ("ontology_id", "type", "name", "external_id", "alias", "description"):
            assert field in recorder.outputFields, (
                f"全量读缺字段 {field}，_cleanup 会在 drop 之后 KeyError"
            )

    def test_does_not_read_embedding_vector(self, recorder) -> None:
        """不读 1024 维向量：调用方（如 /system/vectors 元数据视图）不想要它。"""
        _queryAllRowsFromCollection("ontology_property_embeddings")

        assert "embedding" not in (recorder.outputFields or [])
