"""按域发布的版本号唯一索引：`uq_semantic_release_domain_version`（0157）。

设计依据 `Harness/changes/2026-11-19-m1-procurement-domain-release/summary.md` §6.6
（批次 B：按域发布上线 ⇒ ORM 里那句「按域发布上线时须补同域版唯一索引
（届时再随迁移一起做，不提前建）」的时点到了）。

**为什么是 `(version, domain_code)` 而不是 `(version)`**：

- 全局作用域已有 `uq_semantic_release_global_version`（`version` WHERE
  `domain_code IS NULL`）；
- 域作用域要的是「**同一域内**版本号唯一」⇒ 键必须含 `domain_code`
  ⇒ 不同域**可以**用同一个版本号（版本号标识的是「哪一次发布」，
  不是「哪一套治理内容」）；
- 若建成 `(version)` WHERE `domain_code IS NOT NULL`，就会把「不同域用同一个
  版本号」也禁掉 —— 那是更严的语义，且与「版本号在作用域内唯一」的口径不符。

**为什么必须建（服务层查重不够）**：服务层查重是「先查后写」，两个并发请求
可以同时查到「没有重复」然后同时写入 ⇒ 库侧唯一索引才是兜底。全局作用域早就是
「服务层查重 + 库侧索引」双保险，域作用域补齐成同款。

**存量数据**：本迁移是纯加法 DDL（只建一个部分唯一索引）。若库里已存在
「同域同版本号」的历史行，建索引会**直接失败** —— 这是**想要的行为**：
重复版本号的历史行必须先由人工裁决（哪一条是笔误），不能静默挑一条留下。
建索引前会先跑一次重复探测，把冲突清单写进日志，失败时错误信息自带上下文。

迁移号说明：head 0156（manifest v2 默认值）→ 本批 0157。
A4-0（审批面放开 `instance_id`）按「串行占号、不许插队」规则取当时的下一个空号；
本批 0158（采购本体对象核对与补齐）已占 ⇒ A4-0 落地时取 **0159**。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None

_TABLE = "semantic_release"
_INDEX = "uq_semantic_release_domain_version"


def upgrade() -> None:
    _assertNoDuplicateDomainVersions()
    op.create_index(
        _INDEX,
        _TABLE,
        ["version", "domain_code"],
        unique=True,
        postgresql_where=sa.text("domain_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)


def _assertNoDuplicateDomainVersions() -> None:
    """建索引前先探测「同域同版本号」的存量重复（重复 ⇒ 建索引会失败）。

    ⚠️ 这里**不做任何自动修复**（不删行、不改版本号）：重复版本号意味着
    「哪一次发布」无法标识 ⇒ 必须人工裁决。静默挑一条留下 = 伪造发布历史。
    """
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT domain_code, version, count(*) AS n "
            "FROM semantic_release "
            "WHERE domain_code IS NOT NULL "
            "GROUP BY domain_code, version "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    if not duplicates:
        return
    detail = "; ".join(
        f"domain={row[0]} version={row[1]} x{row[2]}" for row in duplicates
    )
    raise RuntimeError(
        "存在同域重复版本号，无法建立唯一索引（需人工裁决后重跑）：" + detail
    )
