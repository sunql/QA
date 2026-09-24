"""RBAC 身份与权限表（feat-rbac-identity, 2026-09-07）。

6 张表构成多对多 RBAC 底座：
- users / roles / organizations：三类主体（扁平组织，parent_id 仅预留）
- user_roles / user_organizations：用户 ↔ 角色 / 组织的多对多关联
- permission_grant：菜单权限授予（多态主体：USER/ROLE/ORGANIZATION），
  三主体对同一资源取合集 = 用户有效权限（admin 角色旁路全量）。

数据权限预留：permission_grant.subject_type/subject_id 的多态主体模型与
合集解析语义完全复用于后续 data_permission_grant（见
Harness/changes/feat-rbac-identity/）——本次仅落菜单权限。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin

GRANT_SUBJECT_TYPES = ("USER", "ROLE", "ORGANIZATION")
ADMIN_ROLE_CODE = "admin"


class User(Base, TimestampMixin):
    """系统用户（无密码——当前无登录态，身份来自 X-User-Id 头桩映射）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # 0060 / 0082：登录 + 强制改密相关字段（DB 已有列，ORM 同步声明以避免
    # 列差异在 alembic autogenerate 中触发 phantom diff）。
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        Index("ix_users_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r}>"


class UserSession(Base):
    """用户会话表（feat-user-auth / 0082）。

    每条 Bearer token 写入一行 ``jti`` + ``revoked_at``；``getCurrentUser``
    每次同步查该表验证 session 未被吊销。``revoked_reason`` 三种：
    ``logout`` / ``password_changed`` / ``admin_reset``。
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    jti: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        Index("ix_user_sessions_jti", "jti", unique=True),
        Index("ix_user_sessions_user_id", "user_id"),
        Index("ix_user_sessions_active", "user_id", "revoked_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<UserSession id={self.id} jti={self.jti!r} "
            f"user_id={self.user_id} revoked={self.revoked_at!r}>"
        )


class Role(Base, TimestampMixin):
    """角色。code='admin' 为内置超管角色（权限旁路全量菜单）。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("code", name="uq_roles_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role id={self.id} code={self.code!r}>"


class Organization(Base, TimestampMixin):
    """组织（部门）。树形层级：parent_id 自引用 + sort_order 同级排序。

    多根节点（parent_id IS NULL）合法；权限按直接归属解析，不向上继承。
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 同级排序；sort_order=0 为默认；树形 API 按 (parent_id, sort_order) 输出
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("code", name="uq_organizations_code"),
        Index("ix_organizations_parent", "parent_id"),
        Index("ix_organizations_parent_sort", "parent_id", "sort_order"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Organization id={self.id} code={self.code!r}>"


class UserRole(Base):
    """用户 ↔ 角色多对多关联（复合 PK 防重复授予）。"""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserRole user={self.user_id} role={self.role_id}>"


class UserOrganization(Base):
    """用户 ↔ 组织多对多关联（复合 PK 防重复分配）。"""

    __tablename__ = "user_organizations"

    user_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserOrganization user={self.user_id} org={self.organization_id}>"


class PermissionGrant(Base, TimestampMixin):
    """菜单权限授予（多态主体）。

    subject_type ∈ USER/ROLE/ORGANIZATION；subject_id 为对应表 PK（无物理 FK，
    多态引用，service 层校验引用合法性）。menu_code FK→menu_config.code，
    删菜单级联清授权。
    """

    __tablename__ = "permission_grant"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigIntFk, nullable=False)
    menu_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("menu_config.code", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "menu_code", "subject_type", "subject_id",
            name="uq_permission_grant_scope",
        ),
        CheckConstraint(
            "subject_type IN ('USER', 'ROLE', 'ORGANIZATION')",
            name="ck_permission_grant_subject_type",
        ),
        Index("ix_permission_grant_subject", "subject_type", "subject_id"),
        Index("ix_permission_grant_menu", "menu_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PermissionGrant id={self.id} "
            f"{self.subject_type}:{self.subject_id} menu={self.menu_code!r}>"
        )
