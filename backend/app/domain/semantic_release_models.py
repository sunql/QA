"""语义发布快照表（P1a / D4）。

设计依据 `docs/update/1109/p0/裁决记录.md`：

- **裁决一（自包含）**：`manifest_json` 是运行期唯一真相。本体治理表被原地更新，
  `ontology_class.version/valid_*` 是 Phase 6 移除版本管理后的兼容残留，不能承载
  发布语义 ⇒ 快照必须整份落库，`manifest_hash` 用规范化 JSON 的 SHA-256 固定。
- **裁决四（作用域）**：`domain_code` 可空 —— NULL = 全企业快照（首期唯一形态），
  非空 = 未来按域发布（本期只保留列与外键校验）。

「同域最多一条 ACTIVE」由部分唯一索引 `uq_semantic_release_one_active` 在库侧兜底，
服务层状态机（D10）仍须拒绝非法转换；两者是双保险，不是二选一。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text as sa_text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import SemanticReleaseStatus
from app.domain.models import Base, BigIntPk, TimestampMixin

_JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


class SemanticRelease(Base, TimestampMixin):
    """一次已编译的语义发布（快照 + 状态机 + 激活时间）。"""

    __tablename__ = "semantic_release"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # NULL = 全局快照；非空时必须存在于 data_domain.code（域被删则拒绝）。
    domain_code: Mapped[str | None] = mapped_column(
        String(20),
        ForeignKey(
            "data_domain.code",
            ondelete="RESTRICT",
            name="fk_semantic_release_domain",
        ),
        nullable=True,
    )
    # 发布版本号（如 2026.11.09-1）。首期只有全局作用域，版本号唯一性由下方
    # `uq_semantic_release_global_version`（WHERE domain_code IS NULL）保证；
    # 按域发布上线时须补同域版唯一索引（届时再随迁移一起做，不提前建）。
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SemanticReleaseStatus.DRAFT.value,
        server_default=SemanticReleaseStatus.DRAFT.value,
    )
    # Manifest 自身的 Schema 版本（与发布 version 解耦，便于 Schema 演进时回放旧快照）。
    schema_version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="v1"
    )
    # 自包含快照：类 / 属性 / JOIN / 指标 / BO / 域，全部在这一次编译里固化。
    manifest_json: Mapped[dict] = mapped_column(_JSONB, nullable=False)
    # 规范化 JSON（键序稳定、无浮点噪声）的 SHA-256 十六进制串。
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activated_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # 全局快照下版本号唯一。PostgreSQL 唯一约束对 NULL 互不相等，
        # 所以 domain_code IS NULL 的部分索引是必需的（不能指望联合唯一约束）。
        Index(
            "uq_semantic_release_global_version",
            "version",
            unique=True,
            postgresql_where=sa_text("domain_code IS NULL"),
        ),
        # 同域最多一条 ACTIVE。COALESCE 把 NULL 归一，否则全局快照漏防。
        Index(
            "uq_semantic_release_one_active",
            sa_text("COALESCE(domain_code, '')"),
            unique=True,
            postgresql_where=sa_text("status = 'ACTIVE'"),
        ),
        Index("ix_semantic_release_status", "status"),
        # 状态字面量必须受库侧约束：`uq_semantic_release_one_active` 的谓词是
        # `status = 'ACTIVE'`，若写入路径存进 'active'/'ACTIVE '，索引会当成不同值，
        # 「同域最多一条 ACTIVE」的兜底静默失效。服务层状态机（D10）与 CHECK 是双保险。
        CheckConstraint(
            "status IN ('DRAFT','VALIDATED','ACTIVE','ARCHIVED')",
            name="ck_semantic_release_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SemanticRelease id={self.id} version={self.version} "
            f"status={self.status} domain={self.domain_code}>"
        )
