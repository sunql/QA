"""候选（未确认）源绑定的**活库汇总**（M1-1.3 批次 B）。

为什么单独一个文件
------------------
「候选绑定有多少条」这件事**只能实时查库** ⇒ 它不属于纯函数层
（`semantic_compiler_bindings` 刻意保持无 IO），但它又是发布校验的必需输入
（`SOURCE_BINDING_CANDIDATE` 校验项）⇒ 放在「校验项纯逻辑」与「编译纯函数」
之间的任何一侧都会让那一侧失去单一职责 ⇒ 独立成模块。

口径（D-3）
----------
- **候选 ≠ 结论**：候选绑定不进快照、不被读路径消费；本模块只**统计**它们，
  用来在发布报告里显式暴露治理债。
- 只算**仍然开放**的候选（`valid_to IS NULL`）：已失效的候选是历史，
  不是待办债。
- 源绑定候选与属性绑定候选**都算**：属性绑定没确认 ⇒ 该列的映射同样是债。
  ⇒ 因此「涉及类名」必须**同时**覆盖两类（否则会出现「总数含属性绑定、
  类名只有源绑定」的口径错位）。

作用域（按发布域收窄）
----------------------
一次**采购域**发布不该把别的域的候选债算进自己的报告 ⇒ 支持按**类名集合**
（发布快照切片里的类）收窄。收窄口径与快照一致 ⇒ 报告与快照**同一宇宙**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)

# 作用域过滤时一次最多带多少个类名进 `IN`（PG 参数上限远高于此，
# 但 IN 列表无上限会让 SQL 文本随发布体量线性膨胀 ⇒ 取一个明确上限）。
_MAX_SCOPE_CLASS_NAMES = 5000


@dataclass(frozen=True, slots=True)
class CandidateBindingSummary:
    """候选绑定汇总（活库事实，落进发布校验报告）。"""

    count: int
    classNames: tuple[str, ...] = ()


async def summarizeCandidateBindings(
    session: AsyncSession,
    *,
    classNameLimit: int = 5,
    scopeClassNames: Sequence[str] | None = None,
) -> CandidateBindingSummary:
    """统计仍然开放的候选绑定条数与涉及的类名。

    Args:
        session: 只读 session。
        classNameLimit: 最多返回几个类名（detail 展示用；总数另计）。
        scopeClassNames: 只统计这些类上的候选（`None` = 全库口径）。
            按**发布快照里的类**收窄 ⇒ 报告与快照同一宇宙。
    """
    limit = max(0, int(classNameLimit))
    scope = _scopeFilter(scopeClassNames)
    countsByClass = await _sourceCountsByClass(session, scope)
    for name, total in (await _propertyCountsByClass(session, scope)).items():
        countsByClass[name] = countsByClass.get(name, 0) + total
    count = sum(countsByClass.values())
    names = tuple(sorted(name for name, total in countsByClass.items() if total > 0)[:limit])
    return CandidateBindingSummary(count=count, classNames=names)


def _scopeFilter(scopeClassNames: Sequence[str] | None) -> tuple[str, ...] | None:
    """把作用域类名归一成 `IN` 列表（`None` = 不收窄）。"""
    if scopeClassNames is None:
        return None
    names = sorted({str(name) for name in scopeClassNames if str(name)})
    return tuple(names[:_MAX_SCOPE_CLASS_NAMES])


async def _sourceCountsByClass(
    session: AsyncSession, scope: tuple[str, ...] | None
) -> dict[str, int]:
    """候选**源绑定**按类名计数。"""
    statement = (
        select(OntologyClass.class_name, func.count(ProcurementSourceBinding.id))
        .select_from(ProcurementSourceBinding)
        .join(OntologyClass, ProcurementSourceBinding.class_id == OntologyClass.id)
        .where(
            ProcurementSourceBinding.is_candidate.is_(True),
            ProcurementSourceBinding.valid_to.is_(None),
        )
        .group_by(OntologyClass.class_name)
    )
    if scope is not None:
        statement = statement.where(OntologyClass.class_name.in_(scope))
    rows = (await session.execute(statement)).all()
    return {str(name): int(total) for name, total in rows}


async def _propertyCountsByClass(
    session: AsyncSession, scope: tuple[str, ...] | None
) -> dict[str, int]:
    """候选**属性绑定**按类名计数（经源绑定拿到类）。

    ⚠️ 属性绑定自己没有 `class_id`（它的身份是「哪条源绑定的哪个属性」）
    ⇒ 必须 join 回源绑定才能定位类 ⇒ 与源绑定同口径（同一类可能两类都有）。
    """
    statement = (
        select(OntologyClass.class_name, func.count(ProcurementPropertyBinding.id))
        .select_from(ProcurementPropertyBinding)
        .join(
            ProcurementSourceBinding,
            ProcurementPropertyBinding.source_binding_id == ProcurementSourceBinding.id,
        )
        .join(OntologyClass, ProcurementSourceBinding.class_id == OntologyClass.id)
        .where(
            ProcurementPropertyBinding.is_candidate.is_(True),
            ProcurementPropertyBinding.valid_to.is_(None),
        )
        .group_by(OntologyClass.class_name)
    )
    if scope is not None:
        statement = statement.where(OntologyClass.class_name.in_(scope))
    rows = (await session.execute(statement)).all()
    return {str(name): int(total) for name, total in rows}
