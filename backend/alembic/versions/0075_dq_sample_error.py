"""feat-sampling-error-visible (2026-09-15)

data_quality_violation_sample 新增 sampling_error TEXT NULL 列，用于在
dispatcher.collectSamples 抛错时把异常信息落库，前端可显式提示「采样失败: ORA-...」。

历史：dispatcher.collectSamples 的 `except Exception: return []` 静默吞错（含
Oracle ORA-00933 / 缺列等），调用方 sampleForRule 在 samples=[] 时直接 return None
不写 DB 行——前端看不到「违规总数 N 条但违规样本 0 条」是采样失败还是 0 命中。
memory eval-dispatcher-silent-except 复盘。

加列后：
- 旧行 sampling_error IS NULL：与新行兼容（NULL 默认）
- 采样成功：sampling_error=None，sample_size>0
- 采样失败：sampling_error=<异常文本>，sample_size=0，sample_pk_values=[]
- 配置错误（rule is None / unknown rule_type / 缺 datasource）依然不写行

文件名 23 字符 ≤ alembic_version.version_num varchar(32) 上限。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "0075_dq_sample_error"
down_revision: str | None = "0074_dq_eval_report_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 添加列；可空，老行不强制填。Text 字段存异常文本，截断在 service 层做（500 chars）。
    op.add_column(
        "data_quality_violation_sample",
        sa.Column("sampling_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_quality_violation_sample", "sampling_error")