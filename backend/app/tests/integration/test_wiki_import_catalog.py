"""Wiki 导入路径留存源文件 + 登记 document_catalog（P0 溯源地基）。

真实 PostgreSQL + 完整 API 链路。**MinIO 客户端被替换为假实现** —— 不联外网，
但保留完整调用链（内容哈希 → 内容寻址对象名 → catalog 写入），与既有集成测试
替换假 LLM 客户端（``_INVOKER_CLIENT``）是同一思路：外部服务走假实现，
数据库与 API 链路保持真实。真实 MinIO 的端到端验证由 Task 7 的真实数据脚本承担。
"""

from __future__ import annotations

import hashlib
from typing import Iterator
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentType
from app.domain.models import DocumentCatalog

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/import"
_CLIENT = "app.infrastructure.object_storage._getClient"
_FILE_URL = f"{_BASE}/preview-file"


class _FakeMinio:
    """记录 put_object 的假客户端（形状对齐 minio.Minio 的被调用面）。"""

    def __init__(self) -> None:
        self.putCalls: list[dict] = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        raise AssertionError(f"桶 {bucket} 应已存在，不该被创建")

    def put_object(self, bucket, objectName, data, length=None, content_type=None):
        self.putCalls.append(
            {
                "bucket": bucket,
                "objectName": objectName,
                "content": data.read(),
                "contentType": content_type,
            }
        )


@pytest.fixture(autouse=True)
def fakeMinio() -> Iterator[_FakeMinio]:
    """模块级 autouse：本模块每个用例都不联真实 MinIO。"""
    fake = _FakeMinio()
    with patch(_CLIENT, return_value=fake):
        yield fake


async def _upload(
    client: AsyncClient, name: str, body: bytes, mime: str = "text/plain"
):
    return await client.post(_FILE_URL, files={"file": (name, body, mime)})


async def test_preview_file_stores_source_and_registers_catalog(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """上传后对象已写入，catalog 有一行指向它，且 hash 是**文件字节**的摘要。"""
    # Arrange
    body = "## 准入规则\n\n注册资本 >= 1000 万。".encode("utf-8")
    expectedHash = hashlib.sha256(body).hexdigest()
    expectedObject = f"sources/{expectedHash[:2]}/{expectedHash}/规则.txt"

    # Act
    resp = await _upload(client, "规则.txt", body)

    # Assert —— 对象侧
    assert resp.status_code == 200
    assert len(fakeMinio.putCalls) == 1
    assert fakeMinio.putCalls[0]["objectName"] == expectedObject
    assert fakeMinio.putCalls[0]["content"] == body
    assert fakeMinio.putCalls[0]["bucket"] == "qa-knowledge-sources"

    # Assert —— 目录侧
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.document_name == "规则.txt"
    # 关键：是**文件字节**的摘要，不是草稿文本的摘要（后者是旧方案的妥协）
    assert row.content_hash == expectedHash
    assert row.storage_url == f"s3://qa-knowledge-sources/{expectedObject}"
    # 不能是 source_type（"MARKDOWN"）—— 那既不是合法枚举值也语义不符
    assert row.document_type == DocumentType.OTHER


async def test_response_contract_unchanged(client: AsyncClient) -> None:
    """响应形状不变：仍是 text + sourceType，**没有新增字段**。

    落库是纯服务端副作用，客户端不需要回传任何东西 —— 这正是选它的理由。
    若将来给响应加了字段，本用例会失败，逼人回来看这里。
    """
    # Act
    resp = await _upload(client, "规则.txt", "## 准入规则\n\n注册资本 >= 1000 万。".encode())

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"text", "sourceType"}
    assert "注册资本 >= 1000 万。" in body["text"]
    assert body["sourceType"] == "MARKDOWN"


async def test_same_file_twice_does_not_duplicate_catalog(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """同一份文件重复上传，catalog 不应新增第二行。"""
    # Arrange
    body = "# 同一份\n\n同样的内容".encode()

    # Act
    first = await _upload(client, "same.md", body)
    second = await _upload(client, "same.md", body)

    # Assert
    assert first.status_code == 200
    assert second.status_code == 200
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 1, f"同 hash 重复上传产生了 {len(rows)} 行 catalog"


async def test_different_files_produce_separate_rows(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """不同内容应是两行 —— 否则上面的去重可能只是「永远只写一行」的假象。"""
    # Act
    await _upload(client, "a.md", "# a\n\nA 的内容".encode())
    await _upload(client, "b.md", "# b\n\nB 的内容".encode())

    # Assert
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert len(rows) == 2
    assert {r.document_name for r in rows} == {"a.md", "b.md"}


async def test_rejected_upload_stores_nothing(
    client: AsyncClient, dbSession: AsyncSession, fakeMinio: _FakeMinio
) -> None:
    """被拒绝的文件不留痕：格式不支持 → 422，且对象存储与 catalog 都是空的。

    这条钉死的是**顺序**：留存必须发生在解析成功之后。反过来的实现
    （先存再解析）会让每个格式不符/损坏的文件都在对象存储里留下垃圾，
    而其余用例全都照样通过 —— 所以必须有这一条。
    """
    # Act
    resp = await _upload(client, "note.xyz", b"anything", "application/octet-stream")

    # Assert
    assert resp.status_code == 422
    assert fakeMinio.putCalls == []
    rows = (await dbSession.execute(select(DocumentCatalog))).scalars().all()
    assert rows == []
