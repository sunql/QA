"""流式上传大小上限的守卫（Task 3.2 / audit-fx-3.2）。

**测机制，不测数字**：上限已改为可配置（`MAX_UPLOAD_MB`，默认 1 GB）——
把具体阈值写死会让「调整上限」变成「测试红」。这里把上限**压到很小的值**
再撞，既快又与被配置的具体数值无关。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


# admin headers — same stub pattern as test_wiki_import_api.py
_ADMIN_HEADERS = {"X-User-Id": "wiki-test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
async def test_document_upload_above_limit_rejected(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """超过上限 → 413（把上限压到 1 KB，传 4 KB 必拒）。"""
    from app.api.v1 import documents as documentsApi

    monkeypatch.setattr(documentsApi, "MAX_UPLOAD_BYTES", 1024)
    big = b"x" * (4 * 1024)
    files = {"file": ("big.pdf", big, "application/pdf")}

    resp = await client.post("/api/v1/documents/upload", files=files)

    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_wiki_upload_above_limit_rejected(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """wiki 上传同样受上限约束（上限压到 1 KB，传 4 KB 必拒）。"""
    from app.api.v1 import wiki_import as wikiImportApi

    monkeypatch.setattr(wikiImportApi, "MAX_UPLOAD_BYTES", 1024)
    big = b"x" * (4 * 1024)
    files = {"file": ("big.md", big, "text/markdown")}

    resp = await client.post(
        "/api/v1/wiki/import/preview-file", files=files, headers=_ADMIN_HEADERS
    )

    assert resp.status_code == 413, resp.text
