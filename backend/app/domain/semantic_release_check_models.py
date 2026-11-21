"""发布校验结论表（`semantic_release_check`，M1-1.2 / 迁移 0155）。

计划 §1.2：「发布校验落表：`semantic_release_check`（校验项、severity、result、detail）」；
准则 H + D1 裁决：V0.3 草案里的 `procurement_ontology_release_check` **建**，
但**改名**为 `semantic_release_check`（不新建 `procurement/*` 语义重复表）。

## 这张表回答什么问题

「这次发布到底查了什么、每项结论如何」。原先闸门结论只以 `blockers` / `warnings`
两个字符串数组出现在写接口的响应体里 —— **响应一关就没了**，无法回答
「上周那次被拦下的发布，拦在哪一条、后来怎么处理的」。

⇒ 每个校验项一行（`PASS` 与 `FAIL` 都落），可按发布版本回查。

## 为什么 severity 与 result 是两列（而不是一列枚举）

- `severity`：**该校验项失败时的严重级别**（`ERROR` = 阻断发布 / `WARNING` = 治理债）。
- `result`：**该校验项本次的结论**（`PASS` / `FAIL`）。

两者正交：一个 `PASS` 行仍带 `severity`，它回答「若这条失败会有多严重」，
于是「全 PASS 的报告」也能直接告诉读者每项的风险面。

## 约束

- FK → `semantic_release.id` **CASCADE**：发布被删 ⇒ 校验证据随之消失
  （留着孤儿证据比删掉更糟：它会让人以为那次发布还存在）。
- `uq_semantic_release_check_unique`：(release_id, check_code, COALESCE(target_key, ''))
  唯一 ⇒ 同一发布同一校验项同一对象只留一行。`COALESCE` 是必需的：
  PostgreSQL 的 NULL 互不相等，直接建唯一索引会让 target_key 为 NULL 的
  发布级校验项（快照完整性 / Manifest 契约）无限重复落库。
- CHECK 钉住 `severity` / `result` 字面量：绕过 ORM 的写入只能落进
  「看起来已校验、其实没人知道结论」的坏行。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntPk, TimestampMixin

# 列注释与迁移 0155 的 COLUMN_COMMENTS **逐字一致**
# （`alembic check` 比对注释：ORM 不声明 ⇒ 它认为该删注释 ⇒ 报「New upgrade operations」；
#  单测 `test_semantic_release_check_models.py` 双向钉住）。
_RELEASE_ID_COMMENT = "0155，M1-1.2：所属发布（semantic_release.id；发布删除 ⇒ 证据 CASCADE）"
_CHECK_CODE_COMMENT = "0155，M1-1.2：校验项编码（机器可读，如 METRIC_SOURCE_PRESENT）"
_CHECK_ITEM_COMMENT = "0155，M1-1.2：校验项名称（中文，展示用）"
_SEVERITY_COMMENT = "0155，M1-1.2：失败时的严重级别（ERROR = 阻断发布；WARNING = 治理债）"
_RESULT_COMMENT = "0155，M1-1.2：本次结论（PASS / FAIL）"
_TARGET_TYPE_COMMENT = "0155，M1-1.2：被校验对象类型（RELEASE / METRIC / …）"
_TARGET_KEY_COMMENT = "0155，M1-1.2：被校验对象标识（指标名等；NULL = 发布级校验项）"
_DETAIL_COMMENT = "0155，M1-1.2：结论详情（人类可读）"
_CHECKED_TIME_COMMENT = "0155，M1-1.2：校验发生时刻（覆盖写 ⇒ 每次评估刷新）"
_CHECKED_BY_COMMENT = "0155，M1-1.2：触发本次校验的操作人（治理可追溯）"


class SemanticReleaseCheck(Base, TimestampMixin):
    """一条发布校验结论。"""

    __tablename__ = "semantic_release_check"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # 所属发布。发布被删 ⇒ 校验证据一并消失。
    release_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey(
            "semantic_release.id",
            ondelete="CASCADE",
            name="fk_semantic_release_check_release",
        ),
        nullable=False,
        comment=_RELEASE_ID_COMMENT,
    )
    # 校验项编码（机器可读，如 `METRIC_SOURCE_PRESENT`）。
    check_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=_CHECK_CODE_COMMENT
    )
    # 校验项名称（中文，展示用）。
    check_item: Mapped[str] = mapped_column(
        String(200), nullable=False, comment=_CHECK_ITEM_COMMENT
    )
    # 失败时的严重级别：ERROR（阻断发布）/ WARNING（治理债）。
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, comment=_SEVERITY_COMMENT
    )
    # 本次结论：PASS / FAIL。
    result: Mapped[str] = mapped_column(String(10), nullable=False, comment=_RESULT_COMMENT)
    # 被校验对象的类型（RELEASE / METRIC / …）与标识（指标名等）。
    target_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment=_TARGET_TYPE_COMMENT
    )
    target_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment=_TARGET_KEY_COMMENT
    )
    # 结论详情（人类可读）。
    detail: Mapped[str] = mapped_column(Text, nullable=False, comment=_DETAIL_COMMENT)
    # 校验发生时刻（覆盖写 ⇒ 每次评估都刷新）。
    checked_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        comment=_CHECKED_TIME_COMMENT,
    )
    # 触发本次校验的操作人（治理可追溯；与 audit_log 的 actor 同源）。
    checked_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_CHECKED_BY_COMMENT
    )

    __table_args__ = (
        Index("ix_semantic_release_check_release", "release_id"),
        # 同一发布 + 同一校验项 + 同一对象 ⇒ 只允许一行。
        # COALESCE 不可省：target_key 为 NULL 的发布级校验项（快照完整性 /
        # Manifest 契约）在 PostgreSQL 里 NULL 互不相等，直接建唯一索引等于没建。
        Index(
            "uq_semantic_release_check_unique",
            "release_id",
            "check_code",
            sa_text("COALESCE(target_key, '')"),
            unique=True,
        ),
        CheckConstraint(
            "severity IN ('ERROR','WARNING')",
            name="ck_semantic_release_check_severity",
        ),
        CheckConstraint(
            "result IN ('PASS','FAIL')",
            name="ck_semantic_release_check_result",
        ),
        CheckConstraint(
            "length(check_code) > 0 AND length(check_item) > 0",
            name="ck_semantic_release_check_item_not_empty",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SemanticReleaseCheck id={self.id} release={self.release_id} "
            f"code={self.check_code} severity={self.severity} result={self.result}>"
        )
