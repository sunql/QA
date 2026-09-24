"""对象存储（MinIO）：知识源文件留存。

P0 溯源地基的一部分。此前源文件上传后不留底，``document_catalog.storage_url``
写的是 ``milvus://N_chunks`` 这种假 URL，``content_hash`` 恒为 None。本模块
提供真实的对象存储读写，使证据链能回指原始文件。

对象名内容寻址（``sources/<hash[:2]>/<hash>/<name>``）：同一文件重复上传落到
同一 key，天然去重。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "qa-knowledge-sources"

# 文件名里可能带路径分隔符或 ..，落到对象名上会越权，统一净化。
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]")


class ObjectStorageError(Exception):
    """对象存储操作失败。

    与「对象不存在」区分：前者是基础设施故障（调用方应上报并中止），
    后者是正常的查询未命中。
    """
    pass


def hashContent(content: bytes) -> str:
    """内容 SHA-256（十六进制小写）。"""
    return hashlib.sha256(content).hexdigest()


def buildSourceObjectName(contentHash: str, filename: str) -> str:
    """由内容哈希 + 文件名派生对象名（内容寻址）。"""
    safeName = _UNSAFE_NAME_RE.sub("_", filename.rsplit("/", 1)[-1]).strip("_")
    if not safeName:
        safeName = "unnamed"
    return f"sources/{contentHash[:2]}/{contentHash}/{safeName}"


def _getClient() -> Any:
    """构造 MinIO 客户端。配置缺失即失败，不降级。

    先校验配置，再 import SDK：配置缺失是本模块**自己的**错误，不该被
    SDK 未安装的 ``ModuleNotFoundError`` 抢在前面掩盖掉 —— 那会把
    「没配 key」误报成「依赖没装」，排查方向完全错。
    """
    endpoint = os.environ.get("MINIO_ENDPOINT")
    accessKey = os.environ.get("MINIO_ROOT_USER")
    secretKey = os.environ.get("MINIO_ROOT_PASSWORD")
    if not endpoint:
        raise ObjectStorageError("MINIO_ENDPOINT 未配置")
    if not accessKey:
        raise ObjectStorageError("MINIO_ROOT_USER 未配置")
    if not secretKey:
        raise ObjectStorageError("MINIO_ROOT_PASSWORD 未配置")

    from minio import Minio

    return Minio(endpoint, access_key=accessKey, secret_key=secretKey, secure=False)


def ensureBucket(bucket: str = DEFAULT_BUCKET) -> None:
    """幂等创建桶。"""
    client = _getClient()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("Created object storage bucket '%s'", bucket)
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"确保桶 {bucket} 存在失败: {e}") from e


def putSourceObject(
    objectName: str,
    content: bytes,
    contentType: str,
    bucket: str = DEFAULT_BUCKET,
) -> str:
    """存入源文件，返回 ``s3://<bucket>/<objectName>``。

    Raises:
        ObjectStorageError: 配置缺失或写入失败
    """
    import io

    client = _getClient()
    try:
        ensureBucket(bucket)
        client.put_object(
            bucket,
            objectName,
            io.BytesIO(content),
            length=len(content),
            content_type=contentType or "application/octet-stream",
        )
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"源文件写入失败 {objectName}: {e}") from e
    return f"s3://{bucket}/{objectName}"


def getSourceObject(objectName: str, bucket: str = DEFAULT_BUCKET) -> bytes:
    """读回源文件。

    Raises:
        ObjectStorageError: 配置缺失、对象不存在或读取失败
    """
    client = _getClient()
    response = None
    try:
        response = client.get_object(bucket, objectName)
        return response.read()
    except ObjectStorageError:
        raise
    except Exception as e:
        raise ObjectStorageError(f"源文件读取失败 {objectName}: {e}") from e
    finally:
        if response is not None:
            response.close()
            response.release_conn()
