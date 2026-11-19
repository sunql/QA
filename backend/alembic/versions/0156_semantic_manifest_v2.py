"""Manifest Schema 版本 v1 → v2：`semantic_release.schema_version` 默认值（0156）。

设计依据 `Harness/changes/2026-11-19-m1-procurement-domain-release/summary.md` §3.1
（决策 D-9：本批把采购源绑定纳入 manifest ⇒ `SCHEMA_VERSION` v1 → v2）。

**为什么只是一条 ALTER DEFAULT**：

- `sourceBindings` 是 Manifest **JSON 里的一个分区**，不是新表 ⇒ 库里没有新对象；
- Manifest 的版本号落在 `semantic_release.schema_version`（快照自带版本），
  既有行（v1 快照）的值**保持不变** —— 回放历史快照时按它自身的版本选 Schema 校验
  （`semantic_manifest_schema.MANIFEST_SCHEMAS`），绝不偷偷放宽 v1；
- 只有**新建**发布走默认值 ⇒ 从 v2 起，新快照带 `sourceBindings` 分区。

**为什么默认值必须与 ORM 一致**：`app/domain/semantic_release_models.py` 的
`server_default` 与本迁移的 `SET DEFAULT` 必须逐字相同 —— 否则 drift 守卫
（`check_schema_drift.py`）会报「DB 多余/缺失默认值」，而 `alembic check`
**只比名不比式**，默认值这一项只能靠单测
`test_release_orm_default_schema_version_matches` 钉住。

迁移号说明：head 0155（发布校验结论表）→ 本批 0156。
A4-0（审批面放开 `instance_id`）按「串行占号、不许插队」规则取当时的下一个空号；
本批 0157（域发布版本号唯一索引）先占 ⇒ A4-0 落地时取 **0158**。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None

_TABLE = "semantic_release"
_COLUMN = "schema_version"
_NEW_DEFAULT = "v2"
_OLD_DEFAULT = "v1"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=20),
        server_default=sa.text(f"'{_NEW_DEFAULT}'"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=20),
        server_default=sa.text(f"'{_OLD_DEFAULT}'"),
        existing_nullable=False,
    )
