"""system_config 运行时 KV 配置表（chat_service 等代码读 ENABLE_* 类开关）。

Schema 由 0052 migration 创建（CREATE TABLE IF NOT EXISTS + 默认 INSERT）。
ORM 模型用于：
  1. 让 Schema drift 校验（main.py lifespan）识别该表存在
  2. 提供类型安全的 ORM 读写路径

注意：本表与 menu_config 等业务表不同——它是运行时 feature flag 的 KV 容器，
不应在业务模块里直接 ORM 查询；service 层仍走 text() 直查（chat_service.py:577）
以保证零业务耦合 + 失败安全（缺表 → return False）。本模型仅供 alembic
autogenerate + drift 校验使用。
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, TimestampMixin


class SystemConfig(Base, TimestampMixin):
    """运行时 KV 配置表（key=value 单行 KV）。

    主键 key = 配置项名（如 'ENABLE_L4_AGENT_LOOP'）。
    value 通常是 'true' / 'false' 字符串或 JSON-like 文本。
    """

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemConfig key={self.key!r} value={self.value!r}>"