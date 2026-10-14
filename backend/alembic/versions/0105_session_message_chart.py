"""session_message 增加 chart_type / chart_option 列（图表进最终报告，2026-09-30）。

**触发**：图表决策引擎落地后，图只存在于**实时响应**里 —— `chartType`/`chartOption`
既没随 `SessionMessage` 落库，历史回放与 PDF 导出都拿不到它。导出 PDF 里的图表于是
只是一个灰框占位（`pdf_export_service._chart_placeholder_flowable`：「图表对象未持久化，
原始 ECharts option 不可在 PDF 重渲染」），刷新页面后历史会话的图也整体消失。

用户要求「最终报告要有图表」⇒ 图必须能被**离线**取到：导出时早先轮次早已不在页面上。

**变更**：``session_message`` 增加两列（均 nullable，存量行天然为 NULL）：

- ``chart_type`` VARCHAR(20)：决策引擎选出的 kind（bar/bar 横向/pie/donut/line/scatter/
  heatmap/kpi/combo/waterfall/table）。存字符串而非 PG enum —— 类型集合还在长（决策 2
  一期 11 类、地图二期），枚举列每加一个值都要一次迁移。
- ``chart_option`` JSONB：渲染负载。**不含颜色**（决策 6，颜色由前端主题层补），
  TABLE 是 `{columns, rows}`、KPI 是 `{kpi: {...}}`，与线上契约同一份结构。

**为什么不与 `citations` 复用一列**：语义完全不同（问答引用 chunks vs 图表负载），
共用一列会让两边的读写都变成「先判类型再取字段」，是滥用。

**幂等性**：降级反向 DROP COLUMN（丢失图负载，可接受 —— 它是可再生的呈现数据，
丢失的后果是历史回放/导出退回占位框，与本次改动前一致）。

**两库同步**：prod + test。

Revision ID: 0105
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0105"
down_revision: str | None = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column("chart_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "session_message",
        sa.Column(
            "chart_option",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_message", "chart_option")
    op.drop_column("session_message", "chart_type")
