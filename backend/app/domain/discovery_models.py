"""本体发现域的 ORM 映射（WP-ONT-AI A1）。

设计依据 `docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md` §4 与
`Harness/changes/2026-11-16-ontology-discovery-ai/plan.md` D-1 / D-2 / D-9。

两张表：

- `discovery_source`：**授权目录**。它**不新建连接**，只引用既有 `data_source.id`
  （连接凭据、主机白名单、只读护栏、审计都在 `datasource_service` /
  `business_db_pool` 收口），额外承载「谁授权、可访问什么、当前是否启用」。
  ⇒ 不出现第二套数据源真相源。
- `metadata_snapshot`：**不可变**的 schema 结构快照。内容寻址（`schema_hash` =
  规范化 JSON 的 SHA-256）：同一源下内容不变 ⇒ 复用同一行；内容变 ⇒ **新增**一行。
  行只增不改（没有 `updated_time`，服务层不提供更新路径）。

与 `schema_cache` 的分工：`schema_cache` 是「当前态缓存」（内容变化即 UPDATE 覆盖，
追求快）；`metadata_snapshot` 是「证据」（只增不改，可被候选 / 剖析 / 提案引用，
追求可寻址与可追溯）。两者**不合并**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk

# 与 `app/alembic/versions/0146_discovery_snapshot.py` 的列注释逐字一致
# （alembic autogenerate 会比对列注释，两边不一致 ⇒ `alembic check` 报漂移）。
SOURCE_COMMENT = "0146，WP-ONT-AI A1：发现源名称（同一实例内唯一）"
SOURCE_ACCESS_POLICY_COMMENT = (
    "0146，WP-ONT-AI A1：访问策略（JSON 对象；键仅 mode=READ_ONLY 与 schema_owner=库模式属主，未知键拒绝）"
)
SOURCE_STATUS_COMMENT = "0146，WP-ONT-AI A1：发现源状态（ACTIVE / DISABLED）"
SNAPSHOT_HASH_COMMENT = (
    "0146，WP-ONT-AI A1：schema 结构快照的内容哈希（规范化 JSON 的 SHA-256 十六进制）"
)
SNAPSHOT_JSON_COMMENT = "0146，WP-ONT-AI A1：schema 结构快照内容（表 / 列 / 类型 / 主外键 / 注释）"
SNAPSHOT_CAPTURED_AT_COMMENT = "0146，WP-ONT-AI A1：快照采集时刻"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DiscoverySource(Base):
    """发现源目录：授权与访问策略，连接本身复用既有 `data_source`。"""

    __tablename__ = "discovery_source"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment=SOURCE_COMMENT
    )
    # 连接复用既有数据源：只读护栏、凭据加密、主机白名单都在那一侧。
    datasource_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("data_source.id", ondelete="RESTRICT", name="fk_discovery_source_datasource"),
        nullable=False,
    )
    # 责任人（业务 / 数据 Owner）——后续提案审核按它分派（Q4）。
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    access_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment=SOURCE_ACCESS_POLICY_COMMENT,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="ACTIVE",
        server_default=sa_text("'ACTIVE'"),
        comment=SOURCE_STATUS_COMMENT,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        # 显式命名唯一约束（不用 `unique=True` 的匿名约束）：迁移侧同名 ⇒
        # autogenerate 不会因为「匿名 vs 命名」而报漂移。
        UniqueConstraint("name", name="uq_discovery_source_name"),
        CheckConstraint(
            "status IN ('ACTIVE','DISABLED')",
            name="ck_discovery_source_status",
        ),
        Index("ix_discovery_source_datasource", "datasource_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoverySource id={self.id} name={self.name} "
            f"datasource={self.datasource_id} status={self.status}>"
        )


class MetadataSnapshot(Base):
    """不可变的 schema 结构快照（内容寻址，只增不改）。"""

    __tablename__ = "metadata_snapshot"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("discovery_source.id", ondelete="RESTRICT", name="fk_metadata_snapshot_source"),
        nullable=False,
    )
    schema_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=SNAPSHOT_HASH_COMMENT,
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment=SNAPSHOT_JSON_COMMENT,
    )
    table_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    column_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        comment=SNAPSHOT_CAPTURED_AT_COMMENT,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        # 内容寻址：同一源下同一内容只存一行 ⇒ 重复采集天然幂等。
        UniqueConstraint("source_id", "schema_hash", name="uq_metadata_snapshot_source_hash"),
        CheckConstraint(
            "schema_hash ~ '^[0-9a-f]{64}$'",
            name="ck_metadata_snapshot_hash_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot_json) = 'object'",
            name="ck_metadata_snapshot_is_object",
        ),
        CheckConstraint(
            "table_count >= 0 AND column_count >= 0",
            name="ck_metadata_snapshot_counts_nonneg",
        ),
        Index("ix_metadata_snapshot_source", "source_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<MetadataSnapshot id={self.id} source={self.source_id} "
            f"hash={self.schema_hash[:12]}… tables={self.table_count}>"
        )
