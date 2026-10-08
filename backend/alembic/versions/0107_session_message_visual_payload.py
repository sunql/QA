"""session_message 增加 table_option / visual_rationale 列（可视化输出策略，2026-10-01）。

**触发**：可视化输出策略（决策 5）给每个回答新增两个负载 —— 图之外的**明细表**
（``tableOption``，与 ``chartOption`` 是同一份 data 的两个投影）与**判断依据**
（``visualRationale``，为什么画这张图 / 为什么不画）。它们此前只活在实时响应里
（chart 事件 / step_result / ChatResponse），没随 ``session_message`` 落库。于是历史
回放与 PDF 导出只能重建出图（0105 落的两列），却拿不到图旁的表格与「为什么这么画」
—— 决策 5 要求这两个负载也能**离线**重建，否则刷新后它们整体消失。

**变更**：``session_message`` 增加两列（均 nullable，存量行天然为 NULL）：

- ``table_option`` JSONB：图之外的明细表负载（``{columns, rows, truncated}``）。
  TABLE / KPI 的图型不附第二份表（表已在 chartOption 里 / 单值卡没有表），故这两
  种 kind 下本列为 NULL。落库前按 `_PERSIST_MAX_TABLE_ROWS`（200 行）截一道
  （见 ``chat_chart_persist.boundedTableOption``）。
- ``visual_rationale`` JSONB：判断依据（``{"code": str, "params": dict}``）。
  code 取 `chart_decision` 的 ruleId（或 `R_FORCED_CLIENT` / `DEGRADE_SPEC_INVALID` /
  `SUMMARY_TEXT_ONLY`），params 只放插值变量不放文案。存 dict 而非结构化列 —— code
  全集还在长（21 个），结构化列每加一个 code 都要一次迁移。

**为什么单独两列而非塞进 `chart_option` envelope（决策 5）**：chartOption 是「渲染
负载」，其 TABLE 形态是 ECharts 的表；tableOption 是「数据表」的另一份投影，两者
截断阈值、生命周期、前端消费点都不同（导出 PDF 里原生表格走 chartOption、rationale
不进 PDF）。塞进一个 envelope 会让读写都变成「先判类型再取字段」，且 0105 已落地
chartOption 契约，回填 envelope 会破坏既有回放/导出。

**幂等性**：降级反向 DROP COLUMN（丢失表/依据负载，可接受 —— 它们是可再生的呈现
数据，丢失的后果是历史回放/导出退回「有图无表/无依据」，与本次改动前一致）。

**两库同步**：prod + test（部署时 prod 先备份，见部署决策 7）。

Revision ID: 0107
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0107"
down_revision: str | None = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column(
            "table_option",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "session_message",
        sa.Column(
            "visual_rationale",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_message", "visual_rationale")
    op.drop_column("session_message", "table_option")
