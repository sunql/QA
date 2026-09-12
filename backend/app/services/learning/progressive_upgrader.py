"""机制 5：渐进式结构化（feat-wiki-knowledge，Phase 8 M6）。

两件事，共用一套「什么算已结构化」的判断：

1. **阶段推导** —— 由**事实**（库里到底有没有 claim / 已确认关系 / 已接受建议 /
   产物）算出 ``wiki_page.structure_stage``，而不是在每次动作里 ``+1``。
2. **产物物化** —— 接受一条 RULE / PROCESS 建议时，把抽取结构写成
   ``wiki_rule_executable`` / ``process_workflow`` 里的一行。

## 为什么阶段是「派生」的

递增计数器只增不减：产物删了、关系撤回了，阶段还停在原地，看板会长期高报
结构化进度且没人能发现。派生让它**自愈** —— 产物行没了，下次重算就掉回去。
代价是每次都要查几张表，但单页面的这几个判定都是走索引的 ``LIMIT 1`` 存在性
查询，相比「进度长期失真」，这笔账很划算。

## 为什么不 commit

``applyStructureStage`` / ``materializeArtifact`` 只 flush，事务边界一律留给
调用方。M4 与 M5 都栽在同一个坑里：辅助方法内部 commit 会让同事务里更早写入的
计量/反馈行被一并提交或一并回滚，而调用方以为自己控制着边界。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.wiki_learning_models import (
    ProcessWorkflow,
    StructureSuggestion,
    WikiRuleExecutable,
)
from app.domain.wiki_models import KnowledgeClaim, KnowledgeRelation
from app.services.learning.wiki_rule_engine import requireOperator
from app.services.messages_zh import (
    MSG_WIKI_RULE_CONDITIONS_REQUIRED,
    MSG_WIKI_RULE_VALUE_SHAPE_INVALID,
)

logger = logging.getLogger(__name__)

# 结构阶段（与 app.domain.wiki_models.STRUCTURE_STAGES 同值；此处重复声明是为了
# 让「阶段由谁决定」这件事只有一个出口 —— 消费方都读这里的常量，改阶段定义时
# 不会漏掉某个下游。）
STAGE_MARKDOWN = "MARKDOWN"
STAGE_SEMI_STRUCTURED = "SEMI_STRUCTURED"
STAGE_FULLY_STRUCTURED = "FULLY_STRUCTURED"

# 建议的终态里只有 ACCEPTED 代表「人确认过」
_ACCEPTED = "ACCEPTED"

# 物化产物的类型标识（也用作返回值，让调用方知道落出来的是什么）
ARTIFACT_RULE = "RULE"
ARTIFACT_WORKFLOW = "WORKFLOW"

# 只有这两类建议会物化：公式（METRIC）与定义（CONCEPT）不是可执行判决，
# 硬写成规则会让 Agent 拿到一条没有条件的「规则」，也污染规则的准确率统计。
_MATERIALIZING_DIMENSIONS: dict[str, str] = {
    "RULE": ARTIFACT_RULE,
    "PROCESS": ARTIFACT_WORKFLOW,
}

_MEMBERSHIP_OPERATORS = frozenset({"IN", "NOT IN"})
_ORDER_OPERATORS = frozenset({">=", "<=", ">", "<"})

# 与 WikiRuleExecutable.target_entity 的列宽一致（VARCHAR(100)）。常量而非字面量：
# 列宽改了这里要一起改，而散在 UPSERT 里的 100 不会有人想起来。
_TARGET_ENTITY_MAX_LENGTH = 100


@dataclass(frozen=True)
class StructureStageChange:
    """一次阶段同步的结果。``changed`` 让调用方能区分「跑了」与「改了」。"""

    previous: str
    current: str
    changed: bool


# ---------------------------------------------------------------------------
# 阶段推导
# ---------------------------------------------------------------------------


async def computeStructureStage(session: AsyncSession, pageId: str) -> str:
    """按库里的**事实**算出这条知识当前的结构阶段（只读，不改库）。"""
    if await _hasArtifact(session, pageId):
        return STAGE_FULLY_STRUCTURED
    if await _hasSemiStructuredSignal(session, pageId):
        return STAGE_SEMI_STRUCTURED
    return STAGE_MARKDOWN


async def applyStructureStage(
    session: AsyncSession, pageId: str
) -> StructureStageChange:
    """把 ``wiki_page.structure_stage`` 同步成推导值（**不 commit**）。

    条目不存在时抛 404（经由 ``WikiPageService.getPage``）——阶段是条目的属性，
    没有条目就没有阶段可谈。
    """
    page = await _loadPage(session, pageId)
    previous = page.structure_stage
    current = await computeStructureStage(session, pageId)
    if previous == current:
        return StructureStageChange(previous=previous, current=current, changed=False)

    page.structure_stage = current
    await session.flush()
    logger.info(
        "wiki 结构阶段变更：page=%s %s → %s", pageId, previous, current
    )
    return StructureStageChange(previous=previous, current=current, changed=True)


async def _loadPage(session: AsyncSession, pageId: str) -> Any:
    """取页面 ORM 实例（借 ``WikiPageService`` 的 404 语义）。

    局部 import：``wiki_page_service`` 是服务层，本模块属于 learning 管线，
    模块级互相引用会让依赖方向变得难以判断。
    """
    from app.services.wiki_page_service import WikiPageService

    return await WikiPageService().getPage(session, pageId)


async def _hasArtifact(session: AsyncSession, pageId: str) -> bool:
    """是否有已物化的结构化产物（规则或流程）。"""
    for model in (WikiRuleExecutable, ProcessWorkflow):
        found = await session.execute(
            select(model.id).where(model.page_id == pageId).limit(1)
        )
        if found.scalar_one_or_none() is not None:
            return True
    return False


async def _hasSemiStructuredSignal(session: AsyncSession, pageId: str) -> bool:
    """是否有「半结构化」的证据：事实原子 / 已确认关系 / 已接受建议。

    三者的共同点是**都经过了一次机器抽取或人工确认**，故这条知识不再是一坨
    未加工的正文。刻意不含待处置与已打回的记录：那些还没被任何人认下来。
    """
    claim = await session.execute(
        select(KnowledgeClaim.id).where(KnowledgeClaim.page_id == pageId).limit(1)
    )
    if claim.scalar_one_or_none() is not None:
        return True

    relation = await session.execute(
        select(KnowledgeRelation.id)
        .where(
            KnowledgeRelation.upstream_page_id == pageId,
            KnowledgeRelation.confirmed.is_(True),
            KnowledgeRelation.rejected_at.is_(None),
        )
        .limit(1)
    )
    if relation.scalar_one_or_none() is not None:
        return True

    suggestion = await session.execute(
        select(StructureSuggestion.id)
        .where(
            StructureSuggestion.page_id == pageId,
            StructureSuggestion.status == _ACCEPTED,
        )
        .limit(1)
    )
    return suggestion.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# 产物物化
# ---------------------------------------------------------------------------


def deriveRuleKind(ruleExpression: Mapping[str, Any]) -> str:
    """由条件里的算子推出规则形态（只影响展示分组与算子归一化，不影响求值）。

    判定顺序反映「这条规则在干什么」：涉及集合归属就是集合型；涉及大小比较就是
    阈值型；只剩键值相等的就是查询型（按字段定位记录）。COMPUTED 不在产出之列
    ——它对应 METRIC 建议，而 METRIC 不物化。
    """
    conditions = ruleExpression.get("conditions")
    if not isinstance(conditions, list):
        return "LOOKUP"
    operators: set[str] = set()
    for raw in conditions:
        if isinstance(raw, Mapping):
            normalized = requireOperator(raw.get("operator"))
            operators.add(normalized)

    if operators & _MEMBERSHIP_OPERATORS:
        return "SET_MEMBERSHIP"
    if operators & _ORDER_OPERATORS:
        return "THRESHOLD"
    return "LOOKUP"


async def materializeArtifact(
    session: AsyncSession, suggestion: StructureSuggestion
) -> str | None:
    """把一条**已接受**的建议物化成可执行产物（**不 commit**）。

    返回产物类型（``RULE`` / ``WORKFLOW``），不物化的维度返回 ``None``。

    写入走 UPSERT：``page_id`` 上有唯一约束（一条知识一份产物），而「改完正文
    重新抽一条建议再接受」是合法路径 —— 直接 INSERT 会撞唯一键把一次正常的
    审核变成 500。
    """
    artifact = _MATERIALIZING_DIMENSIONS.get(suggestion.suggested_dimension or "")
    if artifact is None:
        return None

    structure = suggestion.extracted_structure
    if not isinstance(structure, Mapping):
        # 能走到这里说明 M5 的抽取校验被绕过了（正常路径不会）。宁可炸，
        # 也不要物化出一条没有内容的空规则去误导 Agent。
        raise ValidationError(MSG_WIKI_RULE_CONDITIONS_REQUIRED)

    if artifact == ARTIFACT_RULE:
        await _upsertRule(session, suggestion.page_id, structure)
    else:
        await _upsertWorkflow(session, suggestion.page_id, structure)
    return artifact


async def _upsertRule(
    session: AsyncSession, pageId: str, structure: Mapping[str, Any]
) -> None:
    """写入/覆盖可执行规则。"""
    conditions = _normalizedConditions(structure)
    action = structure.get("action")
    actionDict = dict(action) if isinstance(action, Mapping) else {}
    page = await _loadPage(session, pageId)

    # 先算一次再被 values() 与 set_ 共用：两处各算一遍是「必须同步改」的双份真相，
    # 今天一致只是因为这几个函数恰好是纯函数、输入也恰好没被改写。
    kind = deriveRuleKind(structure)
    expression = {"conditions": conditions, "action": actionDict}
    targetEntity = _boundedText(
        actionDict.get("target_entity"), _TARGET_ENTITY_MAX_LENGTH
    )
    # 权威链取自条目本身：产物不该比它的来源更权威。条目未定级时不猜，
    # 留 NULL —— 猜一个 L5 会让一条普通文档产出的规则看起来像最高效力。
    authorityChain = [page.authority_level] if page.authority_level else None

    statement = (
        pgInsert(WikiRuleExecutable)
        .values(
            page_id=pageId,
            rule_kind=kind,
            rule_expression=expression,
            target_entity=targetEntity,
            authority_chain=authorityChain,
            version="v1.0",
        )
        .on_conflict_do_update(
            index_elements=["page_id"],
            set_={
                "rule_kind": kind,
                "rule_expression": expression,
                "target_entity": targetEntity,
                "authority_chain": authorityChain,
                "updated_time": func.now(),
            },
        )
    )
    await session.execute(statement)


async def _upsertWorkflow(
    session: AsyncSession, pageId: str, structure: Mapping[str, Any]
) -> None:
    """写入/覆盖结构化流程。"""
    steps = structure.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValidationError(MSG_WIKI_RULE_CONDITIONS_REQUIRED)
    trigger = _optionalText(structure.get("trigger"))

    statement = (
        pgInsert(ProcessWorkflow)
        .values(
            page_id=pageId,
            workflow_version="v1.0",
            steps=steps,
            trigger_condition=trigger,
        )
        .on_conflict_do_update(
            index_elements=["page_id"],
            set_={
                "steps": steps,
                "trigger_condition": trigger,
                "updated_time": func.now(),
            },
        )
    )
    await session.execute(statement)


def _normalizedConditions(structure: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取条件并**归一化算子后**落库。

    归一化写进库是刻意的：求值器里的中文别名表是给「早于本版本写入的行」兜底的
    兼容层，新写入的行应当是规范形态，这样 Agent 侧的消费方读到的算子永远是可
    枚举的，不必各自再实现一份别名表。

    算子认不出即 422：宁可在审核这一步拒掉，也不要让一条永不触发的规则进库。
    """
    conditions = structure.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError(MSG_WIKI_RULE_CONDITIONS_REQUIRED)
    normalized: list[dict[str, Any]] = []
    for raw in conditions:
        if not isinstance(raw, Mapping):
            raise ValidationError(MSG_WIKI_RULE_CONDITIONS_REQUIRED)
        operator = requireOperator(raw.get("operator"))
        normalized.append(
            {
                "field": _optionalText(raw.get("field")),
                "operator": operator,
                "value": _normalizedValue(operator, raw.get("value")),
                "unit": _optionalText(raw.get("unit")),
            }
        )
    return normalized


def _normalizedValue(operator: str, value: Any) -> Any:
    """按**归一化后的算子**校验取值类型（本阶段是取值类型的唯一裁决点）。

    集合算子的取值必须是**非空数组**：求值器 ``_compareMembership`` 就是这么判的，
    塞一个标量进去，这条规则永远判不了 —— 而它已经通过了物化的其它全部校验
    （算子合法、条件非空），会安安静静地进库。一条「永不触发」的规则比一条报错的
    规则危险得多：dry-run 会把它报成「期望不命中」而全绿，事后无从发现。

    空数组同样拒绝：``IN []`` 恒假、``NOT IN []`` 恒真，两者都是「没有条件」的
    伪装，不该被当成一条有判据的规则放行。
    """
    if operator in _MEMBERSHIP_OPERATORS and (
        not isinstance(value, list) or not value
    ):
        raise ValidationError(
            MSG_WIKI_RULE_VALUE_SHAPE_INVALID.format(operator=operator)
        )
    return value


def _optionalText(value: Any) -> str | None:
    """非空字符串原样返回，其余（含空串、非字符串）一律 ``None``。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _boundedText(value: Any, maxLength: int) -> str | None:
    """``_optionalText`` + 列宽上限。

    SQLAlchemy **不校验**字符串长度（``String(100)`` 只作用于 DDL），超长值会一路
    到 Postgres 抛 ``DataError``。该异常没有领域异常映射，最终表现为用户可见的 500，
    而接受建议是终态不可逆的动作 —— 用户只会看到「点了没反应」，且没有入口重试。

    截断而非 422：完整取值仍原样保留在 ``rule_expression.action`` 里（读模型一并
    返回），截的只是这个索引列，没有信息真正丢失；而 422 会让用户卡在一条他无从
    修改的建议上（``action`` 不由前端编辑）。
    """
    text = _optionalText(value)
    if text is None or len(text) <= maxLength:
        return text
    logger.warning(
        "target_entity 超长已截断：%d → %d 字符（完整值仍在 rule_expression 中）",
        len(text),
        maxLength,
    )
    return text[:maxLength]


__all__ = [
    "STAGE_MARKDOWN",
    "STAGE_SEMI_STRUCTURED",
    "STAGE_FULLY_STRUCTURED",
    "ARTIFACT_RULE",
    "ARTIFACT_WORKFLOW",
    "StructureStageChange",
    "computeStructureStage",
    "applyStructureStage",
    "deriveRuleKind",
    "materializeArtifact",
]
