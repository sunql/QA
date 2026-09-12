"""对象存储客户端单测（P0 溯源地基）。MinIO 客户端被 mock。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.infrastructure.object_storage import (
    ObjectStorageError,
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)


class TestHashContent:
    def test_sha256_known_value(self) -> None:
        # echo -n "" | sha256sum
        assert hashContent(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_same_content_same_hash(self) -> None:
        assert hashContent(b"abc") == hashContent(b"abc")

    def test_different_content_different_hash(self) -> None:
        assert hashContent(b"abc") != hashContent(b"abd")


class TestBuildSourceObjectName:
    def test_content_addressed_layout(self) -> None:
        h = hashContent(b"abc")
        name = buildSourceObjectName(h, "制度.pdf")
        assert name == f"sources/{h[:2]}/{h}/制度.pdf"

    def test_same_content_same_name_regardless_of_filename(self) -> None:
        h = hashContent(b"abc")
        assert buildSourceObjectName(h, "a.pdf") != buildSourceObjectName(h, "b.pdf")

    def test_path_traversal_in_filename_is_neutralized(self) -> None:
        h = hashContent(b"abc")
        name = buildSourceObjectName(h, "../../etc/passwd")
        assert ".." not in name
        assert name.startswith(f"sources/{h[:2]}/{h}/")


class TestPutSourceObject:
    def test_missing_config_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ObjectStorageError, match="MINIO_ENDPOINT"):
                putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")

    def test_returns_s3_url(self) -> None:
        fake = MagicMock()
        with patch.dict(
            "os.environ",
            {
                "MINIO_ENDPOINT": "objects:9000",
                "MINIO_ROOT_USER": "u",
                "MINIO_ROOT_PASSWORD": "p",
            },
        ):
            with patch(
                "app.infrastructure.object_storage._getClient", return_value=fake
            ):
                url = putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")
        assert url == "s3://qa-knowledge-sources/sources/aa/x/a.pdf"
        fake.put_object.assert_called_once()

    def test_client_error_wrapped_as_object_storage_error(self) -> None:
        fake = MagicMock()
        fake.put_object.side_effect = RuntimeError("connection refused")
        with patch.dict(
            "os.environ",
            {
                "MINIO_ENDPOINT": "objects:9000",
                "MINIO_ROOT_USER": "u",
                "MINIO_ROOT_PASSWORD": "p",
            },
        ):
            with patch(
                "app.infrastructure.object_storage._getClient", return_value=fake
            ):
                with pytest.raises(ObjectStorageError, match="connection refused"):
                    putSourceObject("sources/aa/x/a.pdf", b"data", "application/pdf")
