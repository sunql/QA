"""发布写路径的**支撑层**纯函数（M1-1.3 批次 B）。

钉住三件事：

1. **`blockedVersion()`**：被闸门拦下的回滚副本要**释放用户请求的版本号**
   （否则用户拿同一个版本号重试只会收到「版本已存在」的误导性 409），
   同时合成版本号必须**落在列宽内**（`semantic_release.version` 是 String(32)）。
2. **`_buildRollbackCopy()`**：副本正文必须**深拷贝** —— JSONB 不做可变追踪 ⇒
   共享 dict 就是静默脏数据。
3. **`_validateNote()`**：备注长度闸在服务层（`note` 是 `Text`，库里没有列宽兜底）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.exceptions import ValidationError
from app.domain.semantic_release_models import SemanticRelease
from app.services.semantic_release_support import (
    _MAX_VERSION_LENGTH,
    _buildRollbackCopy,
    _validateNote,
    blockedVersion,
)


def _release(manifest: dict[str, Any]) -> SemanticRelease:
    return SemanticRelease(
        id=7,
        version="2026.11.19-src",
        domain_code="PROCUREMENT",
        status="ARCHIVED",
        schema_version="v2",
        manifest_json=manifest,
        manifest_hash="a" * 64,
        created_by="tester",
    )


# --- blockedVersion ---------------------------------------------------------


def test_blockedVersion_appends_the_release_id() -> None:
    assert blockedVersion("2026.11.19-rb", 42) == "2026.11.19-rb#b42"


def test_blockedVersion_fits_the_column_width() -> None:
    """`version` 列宽 32 ⇒ 合成版本号不得超宽（超宽 ⇒ 落库 500）。"""
    result = blockedVersion("v" * 60, 123)

    assert len(result) <= _MAX_VERSION_LENGTH


def test_blockedVersion_keeps_the_release_id_intact() -> None:
    """截断只能截**前缀** —— 副本 id 保证了唯一性，截掉它就撞了。"""
    result = blockedVersion("v" * 60, 123)

    assert result.endswith("#b123")


def test_blockedVersion_distinguishes_different_releases() -> None:
    assert blockedVersion("v", 1) != blockedVersion("v", 2)


# --- 回滚副本 ---------------------------------------------------------------


def test_buildRollbackCopy_copies_the_body_verbatim() -> None:
    manifest = {"schemaVersion": "v2", "classes": [{"className": "Supplier"}]}
    source = _release(manifest)

    copied = _buildRollbackCopy(source, "2026.11.19-copy", "tester", None)

    assert copied.manifest_json == manifest
    assert copied.manifest_hash == source.manifest_hash
    assert copied.status == "DRAFT"
    assert copied.domain_code == source.domain_code
    assert copied.schema_version == source.schema_version


def test_buildRollbackCopy_deep_copies_the_body() -> None:
    """副本与源**不共享对象**（JSONB 不做可变追踪 ⇒ 就地改写 = 静默脏数据）。"""
    source = _release({"schemaVersion": "v2", "classes": [{"className": "Supplier"}]})

    copied = _buildRollbackCopy(source, "2026.11.19-copy", "tester", None)
    copied.manifest_json["classes"][0]["className"] = "MUTATED"

    assert source.manifest_json["classes"][0]["className"] == "Supplier"


def test_buildRollbackCopy_uses_the_new_version() -> None:
    source = _release({"schemaVersion": "v2"})

    copied = _buildRollbackCopy(source, "2026.11.19-copy", "tester", "回滚说明")

    assert copied.version == "2026.11.19-copy"
    assert copied.note == "回滚说明"
    assert copied.created_by == "tester"


# --- 备注长度闸 -------------------------------------------------------------


def test_validateNote_rejects_an_oversized_note() -> None:
    with pytest.raises(ValidationError):
        _validateNote("x" * 2001)


def test_validateNote_accepts_a_note_at_the_limit() -> None:
    _validateNote("x" * 2000)


def test_validateNote_accepts_none() -> None:
    _validateNote(None)
