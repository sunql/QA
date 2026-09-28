"""MCP 工具调用日志 ORM 模型（feat-chat-mcp-integration，2026-09-22）。

与 alembic 0084 迁移逐项对齐：drift 校验已到列+索引粒度，缺一即红。
列名 / 类型 / CHECK / 默认值与 DDL 完全同步；任何 DDL 改动须同步此处。

设计要点：
- ``args_json`` 不可空，缺省空 dict（与 DDL ``server_default='{}'::jsonb``
  对应），避免 nullable JSONB 在 Python 侧取值时的 None 分支噪声；
- ``result_size`` 可空——某些 server 提前 fail 时无响应字节；
- ``created_at`` 由 DB 填（``server_default=now()``），Python 侧不设
  ``default=_utcnow``，避免双源时钟漂移；
- 4 个索引命名规则 ``ix_<table>_<col1>[_<col2>...]``，与项目既有
  ``ix_audit_log_actor`` / ``ix_user_sessions_active`` 对齐。

边界：
- 不与 ``AuditLog`` 共享基类——audit 走 outbox + 永久保留，MCP 调用走
  append-only + 30 天 purge；
- 不与 ``SessionTokenUsage`` 冲突——token 计量由 LLM 路径写，
  MCP 调用是「外部请求」语义，对话层若想统计须走 ``application_logs``
  或专门的 product analytics，不混入 token_usage。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base


class MCPCallLog(Base):
    """MCP 工具调用事件流（一次调用 = 一行）。

    append-only，禁止 UPDATE / DELETE（应用层纪律；DB 无 trigger，参考
    ``AuditLog`` 的「软不可变」约定）。
    """

    __tablename__ = "mcp_call_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    server_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    result_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # DDL server_default=now()；Python 侧不重复 default，DB 写入时自填，
    # 与 audit_log.created_at 的「无 updated_time / 事件时间」约定一致。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','failure','timeout','cancelled')",
            name="ck_mcp_call_log_status",
        ),
        CheckConstraint(
            "latency_ms >= 0",
            name="ck_mcp_call_log_latency",
        ),
        CheckConstraint(
            "result_size IS NULL OR result_size >= 0",
            name="ck_mcp_call_log_result_size",
        ),
        Index("ix_mcp_call_log_created_at", "created_at"),
        Index(
            "ix_mcp_call_log_actor_id_created_at",
            "actor_id",
            sa_text("created_at DESC"),
        ),
        Index(
            "ix_mcp_call_log_server_status_created_at",
            "server_name",
            "status",
            sa_text("created_at DESC"),
        ),
        Index(
            "ix_mcp_call_log_tool_name_created_at",
            "tool_name",
            sa_text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MCPCallLog id={self.id} "
            f"{self.server_name}/{self.tool_name} "
            f"by {self.actor_id} status={self.status} "
            f"latency_ms={self.latency_ms}>"
        )
