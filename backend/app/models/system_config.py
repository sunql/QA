"""system_config 运行时 KV 配置表（chat_service 等代码读 ENABLE_* 类开关）。

Schema 由 0052 migration 创建（CREATE TABLE IF NOT EXISTS + 默认 INSERT）：
    CREATE TABLE system_config (
        key VARCHAR(64) PRIMARY KEY,
        value TEXT,
        description TEXT,
        updated_time TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );

ORM 模型用于：
  1. 让 Schema drift 校验（main.py lifespan）识别该表存在
  2. 提供类型安全的 ORM 读写路径（service 层 / admin API）

注意：本表只声明 updated_time（DB 默认在 INSERT 时填 NOW；UPDATE 时由 service
层 setattr + commit 刷新），不复用 TimestampMixin（后者额外引入 created_time，
与 DB schema 不一致会触发 UndefinedColumnError）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base


class SystemConfig(Base):
    """运行时 KV 配置表（key=value 单行 KV）。

    主键 key = 配置项名（如 'ENABLE_L4_AGENT_LOOP'）。
    value 通常是 'true' / 'false' 字符串或 JSON-like 文本。
    """

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemConfig key={self.key!r} value={self.value!r}>"