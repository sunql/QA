"""menu_config 单表存菜单节点（自引用父子关系）。

Phase X：菜单层级重新设计。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin


class MenuConfig(Base, TimestampMixin):
    """菜单配置表（一级类 + 叶子项共用）。

    自引用 parent_id 表达层级（path NULL 表示一级类）。code 全局唯一；sort_order
    控制同级展示顺序；visible=false 表示软隐藏；permission_code / roles 字段
    为后续权限/角色灰度预留。

    children 上 cascade='all, delete-orphan' + FK ondelete CASCADE：删除一级类
    自动级联清理叶子项。service 层仍需主动校验是否有叶子项引用（避免误删）。
    """

    __tablename__ = "menu_config"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey("menu_config.id", ondelete="CASCADE"),
        nullable=True,
    )
    label_key: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    icon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    permission_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    roles: Mapped[str | None] = mapped_column(String(512), nullable=True)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    parent: Mapped["MenuConfig | None"] = relationship(
        "MenuConfig",
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list["MenuConfig"]] = relationship(
        "MenuConfig",
        back_populates="parent",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_menu_config_parent", "parent_id"),
        Index("ix_menu_config_visible_sort", "visible", "sort_order"),
        Index("ix_menu_config_parent_sort", "parent_id", "sort_order", "id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MenuConfig id={self.id} code={self.code!r}>"
