"""Wiki 覆盖度领域模型（feat-wiki-knowledge，Phase 8 M7）。

机制 6「覆盖度自感知」的两张表：

- ``class_domain_mapping``：本体类 → 业务域的映射。``ontology_class`` **没有
  domain 列**（它的治理字段是 object_type / object_owner），而覆盖度要按业务域
  出矩阵，故域轴独立成表。
- ``coverage_cell``：覆盖度矩阵的一个单元格 = (维度 × 本体类 × 业务域)。

## 为什么 domain 是 NOT NULL 且有 ``UNASSIGNED`` 哨兵值

唯一键含 domain。PostgreSQL 的唯一索引默认 **NULL 互不相等**，若允许 domain 为
NULL，「没标域的类」每次刷新都会插入一行新的 NULL 行，唯一约束形同虚设，矩阵会
无声地越长越胖。哨兵值把「未分配」变成一个**可见的桶**而不是不可去重的空洞 ——
看板上能直接看到「这些类还没标业务域」，这本身就是一条待办。

## 为什么域词表**不**复用 AGENT_DATA_DOMAINS

``agent_vocabulary.AGENT_DATA_DOMAINS`` 是**授权词表**（PROCUREMENT/QUALITY/
LOGISTICS）：它决定 Agent 能访问什么，必须闭合，多一个值就是多一份权限。
覆盖度的 domain 是**分类轴**：知识积累到新领域时就该冒出新域，锁死三个值等于
把「不锁业务域」这条核心诉求做废。故这里只做 strip+upper 归一化（与
``normalizeAgentDomain`` 同手法），不设白名单。

## 为什么覆盖度可以整体重算

``coverage_cell`` 是**派生快照**而非事实源。与机制 5 的阶段推导同一个理由
（见 progressive_upgrader 模块说明）：自增计数器删不掉、会长期高报且无人发现，
重算则自愈。单元总数 = 维度数 × 类数 × 域数，上限在千级，全量重算比维护增量
便宜得多，也就不会出现「增量漏算导致看板长期失真」这类没人能发现的问题。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, _utcnow

# 覆盖度状态。四态各有**不同的修复动作**，合并任何一个都会让看板说不出话：
#   MISSING  这条知识根本不存在        → 去写一条
#   OUTDATED 有知识但已失效/过期        → 去更新那几条
#   PARTIAL  有知识但没审到生效          → 去审核
#   COMPLETE 已生效知识覆盖该格          → 无需动作
COVERAGE_STATUSES: tuple[str, ...] = (
    "COMPLETE",
    "PARTIAL",
    "MISSING",
    "OUTDATED",
)

# 类尚未标注业务域时的占位域。见模块说明「为什么 domain 是 NOT NULL」。
DOMAIN_UNASSIGNED = "UNASSIGNED"

# 「有知识但没挂到任何业务对象上」的缺口类型。它不在矩阵里（矩阵的每一格都
# 有类），却是**初始阶段最大的真实缺口**：这些条目 Agent 按对象检索时找不到。
#
# 放在领域层而非 service：它是对外契约的一部分（DTO 的 gapType 字段值），
# 前端要按它分支渲染，让 schemas 反过来 import service 是把依赖方向掰反了。
GAP_UNLINKED = "UNLINKED"

# 被视为「已生效」的条目状态（覆盖度的分母口径）。
# 只看 APPROVED/EFFECTIVE：DRAFT/REVIEW 的知识还没被任何人认下来，
# 把它们算作覆盖会让看板在审核完成前就显示绿色。
COVERAGE_ACTIVE_STATUSES: tuple[str, ...] = ("APPROVED", "EFFECTIVE")

# 域名的最大长度（与两表的 VARCHAR(100) 一致，校验层共用同一个常量）
DOMAIN_MAX_LENGTH = 100


class ClassDomainMapping(Base):
    """本体类 → 业务域的映射（覆盖度的域轴）。

    一个类可以映射到**多个**域（「供应商」既是采购也是质量的关注对象），
    所以唯一键是 (class_id, domain) 而不是 class_id 单列。

    用 ``ontology_class_id``（数值主键）而非 ``class_name`` 作为引用键：
    ``ontology_class.class_name`` **不是唯一的**（同名可有多版本行，见其模型
    注释），拿它当外键会在本体演进后指向错误的行。
    """

    __tablename__ = "class_domain_mapping"
    __table_args__ = (
        UniqueConstraint(
            "ontology_class_id", "domain", name="uq_class_domain_mapping"
        ),
        Index("ix_class_domain_mapping_domain", "domain"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    ontology_class_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("ontology_class.id", ondelete="CASCADE"),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(String(DOMAIN_MAX_LENGTH), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<ClassDomainMapping class={self.ontology_class_id} "
            f"domain={self.domain}>"
        )


class CoverageCell(Base):
    """覆盖度矩阵的一个单元：(维度 × 本体类 × 业务域) 的知识覆盖情况。

    ``page_count`` / ``approved_count`` 都是**重算得到的快照**，不是累加器；
    ``last_refreshed_at`` 让看板能回答「这个数字是什么时候的」。

    刻意**不存** ``page_ids``：单元格的意义是「这一格够不够」，把命中的条目 id
    列表物化进来会让表随知识量线性膨胀，而「点开某一格看是哪些条目」完全可以在
    需要时按同样的判据现查（判据与重算完全一致，不存在两份真相）。
    """

    __tablename__ = "coverage_cell"
    __table_args__ = (
        UniqueConstraint(
            "dimension",
            "ontology_class_id",
            "domain",
            name="uq_coverage_cell",
        ),
        # 看板主查询：按域/状态筛出缺口格。
        Index("ix_coverage_cell_domain_status", "domain", "coverage_status"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    dimension: Mapped[str] = mapped_column(String(30), nullable=False)
    ontology_class_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("ontology_class.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 见模块说明：NOT NULL + UNASSIGNED 哨兵，避免 NULL 在唯一索引里互不相等。
    domain: Mapped[str] = mapped_column(
        String(DOMAIN_MAX_LENGTH),
        nullable=False,
        default=DOMAIN_UNASSIGNED,
        server_default=DOMAIN_UNASSIGNED,
    )
    page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    approved_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    coverage_status: Mapped[str] = mapped_column(String(30), nullable=False)
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<CoverageCell dim={self.dimension} class={self.ontology_class_id} "
            f"domain={self.domain} {self.approved_count}/{self.page_count} "
            f"{self.coverage_status}>"
        )


__all__ = [
    "COVERAGE_STATUSES",
    "COVERAGE_ACTIVE_STATUSES",
    "DOMAIN_UNASSIGNED",
    "DOMAIN_MAX_LENGTH",
    "GAP_UNLINKED",
    "ClassDomainMapping",
    "CoverageCell",
]
