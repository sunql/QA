"""Task 3.2: Streaming upload size limit — RED test.

Audit Finding 8 (MEDIUM): file upload endpoints accept unbounded multipart bodies.
Fix: per-endpoint streaming size limit (documents: 50 MB, wiki: 10 MB) with HTTP 413
returned when exceeded.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


# admin headers — same stub pattern as test_wiki_import_api.py
_ADMIN_HEADERS = {"X-User-Id": "wiki-test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
async def test_document_upload_above_limit_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /api/v1/documents/upload with a 60 MB file → 413 Payload Too Large.

    Uses 60 MB (above 50 MB limit) — small enough to complete quickly while
    still proving the size guard is implemented.
    """
    # No headers needed — same as test_rag_ingest_provenance.py which passes
    big = b"x" * (60 * 1024 * 1024)
    files = {"file": ("big.pdf", big, "application/pdf")}
    resp = await client.post("/api/v1/documents/upload", files=files)
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_wiki_upload_above_10mb_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /api/v1/wiki/import/preview-file with a 15 MB file → 413 Payload Too Large.

    Uses 15 MB (above 10 MB limit). The wiki file upload endpoint is /preview-file.
    """
    big = b"x" * (15 * 1024 * 1024)
    files = {"file": ("big.md", big, "text/markdown")}
    resp = await client.post("/api/v1/wiki/import/preview-file", files=files, headers=_ADMIN_HEADERS)
    assert resp.status_code == 413
