"""seed: 初始化 3 个本地 embedding 模型服务

Revision ID: 0013_seed_embedding_providers
Revises: 0012_embedding_provider
Create Date: 2026-08-13
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_seed_embedding_providers"
down_revision: Union[str, None] = "0012_embedding_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 三个本地 embedding 模型服务（均为 OpenAI 兼容 /v1/embeddings，1024 维）。
# api_key_encrypted 置 NULL：Ollama / oMLX 无需鉴权，运行期空 key 直接构造客户端
# （EmbeddingClient 空 key + 显式 base_url 不再抛"缺少 API key"，见 embedding_client.py）。
# 真实 key 场景由 CRUD API 写入（Fernet 加密，密钥来自运行期 SECRET_KEY）。
_SEED_ROWS = [
    (
        "oMLX bge-m3 FP16",
        "omlx",
        "http://localhost:8080/v1",
        "bge-m3-mlx-fp16",
        1024,
        False,
    ),
    (
        "oMLX bge-m3 8bit",
        "omlx",
        "http://localhost:8080/v1",
        "bge-m3-mlx-8bit",
        1024,
        False,
    ),
    (
        "Ollama bge-m3",
        "ollama",
        "http://localhost:11434/v1",
        "bge-m3:latest",
        1024,
        True,
    ),
]


def upgrade() -> None:
    values = ", ".join(
        "({name!r}, {ptype!r}, {url!r}, {model!r}, NULL, {dim}, {active}, NOW(), NOW())".format(
            name=name, ptype=ptype, url=url, model=model, dim=dim, active="true" if active else "false"
        )
        for name, ptype, url, model, dim, active in _SEED_ROWS
    )
    op.execute(
        "INSERT INTO embedding_provider"
        " (name, provider_type, base_url, model_name, api_key_encrypted, dimension, is_active,"
        "  created_time, updated_time) VALUES " + values
    )


def downgrade() -> None:
    names = ", ".join(repr(row[0]) for row in _SEED_ROWS)
    op.execute(f"DELETE FROM embedding_provider WHERE name IN ({names})")
