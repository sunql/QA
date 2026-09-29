"""新增 ``mcp_call_log`` 表（feat-chat-mcp-integration）。

**触发**：chat 接入 MCP（context7 / microsoft-learn / fetch / 自研 server 等）
作为 LLM tool call 的一种形态。MCP 调用与 LLM 调用有本质差别——LLM 调用走
``session_token_usage`` 计量 token，MCP 调用是外部进程访问，绕开 token 计量
且常常高频（一秒数十次）。

**为何不混入 ``audit_log``**：
- audit_log 是「业务变更审计」，高频 MCP 调用会把表撑爆，违反「每条业务
  动作一行」的语义不变性；
- audit_log 走 outbox 模型（``outbox_id`` FK 兜底幂等），而 MCP 调用是
  「事件流」（一次调用 = 一条记录），无幂等需求；
- audit_log 默认保留期远比 30 天长，purge 周期不一致。

**为何不混入 ``session_token_usage``**：MCP server 自身的 token 不计入
LLM 计量；二者 schema 也不同（无 ``model_config_id`` FK，而 ``latency_ms``
``result_size`` 是 MCP 独有维度）。

**字段**：
- ``id``：BIGSERIAL，高频写入优先，append-only，与 audit_log 同模式；
- ``actor_id``：VARCHAR(64)；用户 ID 或 ``system``（system 作业 / agent 委派）；
- ``server_name``：VARCHAR(64)；MCP server 标识，可与 system_config
  的 server 白名单交叉验证；
- ``tool_name``：VARCHAR(128)；某个 server 下的工具名；
- ``args_json``：JSONB，未来可建 GIN 索引做「哪次调用带了特定 query」
  检索；NOT NULL（缺省空 dict，防 nullable 列 bug）；
- ``result_size``：响应字节数；只记大小**不存结果本身**，呼应 plan §6
  R5 「不持久化外部响应内容」的信息泄露防御——结果已被 LLM 在 chat 流
  里消费，无需二次落地；
- ``status``：四态白名单（success / failure / timeout / cancelled）；
- ``error_code``：弱约束（VARCHAR(64)，允许扩展例如 MCP_AUTH_ERROR、
  MCP_UNAVAILABLE、VALIDATION_ERROR 等）；
- ``latency_ms``：耗时；
- ``created_at``：写入时刻，``server_default=now()`` 由 DB 填；
  不复用 ``TimestampMixin``（无 ``updated_time``，且语义「事件时间」
  比「更新时间」更直白，与 ``audit_log`` 一致）。

**索引**：
- ``ix_mcp_call_log_created_at``：按时间清理 30 天前数据（BRIN 未来
  可优化，但 BTREE 兼容更多查询模式先落地）；
- ``ix_mcp_call_log_actor_id_created_at``：按用户回溯最近调用；
- ``ix_mcp_call_log_server_status_created_at``：熔断统计（失败率 / 超时
  率按 server×status 切片）；
- ``ix_mcp_call_log_tool_name_created_at``：工具热度（top N 工具）。

**索引创建方式**：alembic 默认事务内 ``CREATE INDEX``。**不要**
``CONCURRENTLY``——alembic 会包在事务中，PG 拒绝事务内 CONCURRENTLY。
``created_at`` 索引如未来数据量暴增再考虑 alter 改 CONCURRENTLY。

**两库同步**：prod + test 都升级到 0084；0084 是新 chain head。

Revision ID: 0084
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0084_mcp_call_log"
down_revision: str | None = "0083_audit_log_auth_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_call_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("server_name", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column(
            "args_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('success','failure','timeout','cancelled')",
            name="ck_mcp_call_log_status",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0",
            name="ck_mcp_call_log_latency",
        ),
        sa.CheckConstraint(
            "result_size IS NULL OR result_size >= 0",
            name="ck_mcp_call_log_result_size",
        ),
    )
    op.create_index(
        "ix_mcp_call_log_created_at",
        "mcp_call_log",
        ["created_at"],
    )
    op.create_index(
        "ix_mcp_call_log_actor_id_created_at",
        "mcp_call_log",
        ["actor_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_mcp_call_log_server_status_created_at",
        "mcp_call_log",
        ["server_name", "status", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_mcp_call_log_tool_name_created_at",
        "mcp_call_log",
        ["tool_name", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_call_log_tool_name_created_at", table_name="mcp_call_log"
    )
    op.drop_index(
        "ix_mcp_call_log_server_status_created_at", table_name="mcp_call_log"
    )
    op.drop_index(
        "ix_mcp_call_log_actor_id_created_at", table_name="mcp_call_log"
    )
    op.drop_index("ix_mcp_call_log_created_at", table_name="mcp_call_log")
    op.drop_table("mcp_call_log")
