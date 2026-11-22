"""文档关联的「目标对象」检索（供前端新建关联时做可搜索下拉）。

背景（2026-11-22 用户反馈）：原表单让用户手填
「实体键（enterprise_key）」——但**一份制度/合同往往关联的是一个本体类、
一个指标或一个属性**，不是某个具体实体键；而且用户根本不知道该填什么。

于是把「关联目标」收敛为四类，**每类都由系统提供可搜索候选**：

===================  ==========================================  ==============================
实体类型              候选来自                                    entity_key 存什么
===================  ==========================================  ==============================
业务对象码             ``entity_mapping``（编码/名称模糊搜）        企业编码（如 ``D135``）
``ONTOLOGY_CLASS``    ``ontology_class``（类名/别名）             类名（如 ``DIM_SUPPLIER``）
``ONTOLOGY_METRIC``   ``ontology_metric``（指标名/别名）          指标名（如 ``到货及时率``）
``ONTOLOGY_PROPERTY`` ``ontology_property`` + 所属类              ``类名.属性名``
===================  ==========================================  ==============================

**为什么 key 存可读名而不是主键 id**：关联会出现在知识检索的证据里
（``entityKeys`` 过滤、文档检索溯源），可读名才对人有意义；主键 id 换个库就失效。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

TOPIC_ONTOLOGY_CLASS = "ONTOLOGY_CLASS"
TOPIC_ONTOLOGY_METRIC = "ONTOLOGY_METRIC"
TOPIC_ONTOLOGY_PROPERTY = "ONTOLOGY_PROPERTY"

#: 非业务对象的「知识对象」关联类型（不在 business_object 注册表里，需单独放行）。
KNOWLEDGE_ENTITY_TYPES: frozenset[str] = frozenset(
    {TOPIC_ONTOLOGY_CLASS, TOPIC_ONTOLOGY_METRIC, TOPIC_ONTOLOGY_PROPERTY}
)

#: 前端下拉的分组标题（中英双语由前端 i18n 处理，这里给稳定机器码）。
ENTITY_TYPE_LABELS: dict[str, str] = {
    TOPIC_ONTOLOGY_CLASS: "本体类",
    TOPIC_ONTOLOGY_METRIC: "指标",
    TOPIC_ONTOLOGY_PROPERTY: "属性",
}

#: 单次候选返回上限（下拉不需要全量）。
MAX_TARGETS = 50


def isKnowledgeEntityType(entityType: str) -> bool:
    return entityType in KNOWLEDGE_ENTITY_TYPES


async def _searchBusinessObject(
    session: AsyncSession, entityType: str, keyword: str, limit: int
) -> list[dict[str, Any]]:
    """业务对象：按企业编码 / 名称模糊搜（复用 entity_mapping 既有语义）。"""
    from app.services.entity_mapping_service import EntityMappingService

    rows = await EntityMappingService().searchMappings(
        session, q=keyword, entityType=entityType, limit=limit
    )
    return [
        {
            "key": str(getattr(row, "enterprise_code", "") or getattr(row, "enterprise_key", "")),
            "label": str(getattr(row, "name", "") or getattr(row, "enterprise_code", "")),
            "hint": f"{entityType} · {getattr(row, 'source_system', '')}",
        }
        for row in rows
    ]


async def _searchOntologyClass(
    session: AsyncSession, keyword: str, limit: int
) -> list[dict[str, Any]]:
    from app.domain.models import OntologyClass

    like = f"%{keyword}%"
    stmt = (
        select(OntologyClass)
        .where(or_(OntologyClass.class_name.ilike(like), OntologyClass.class_alias.ilike(like)))
        .order_by(OntologyClass.class_name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "key": row.class_name,
            "label": row.class_alias or row.class_name,
            "hint": row.class_name,
        }
        for row in rows
    ]


async def _searchOntologyMetric(
    session: AsyncSession, keyword: str, limit: int
) -> list[dict[str, Any]]:
    from app.domain.models import OntologyMetric

    like = f"%{keyword}%"
    stmt = (
        select(OntologyMetric)
        .where(or_(OntologyMetric.metric_name.ilike(like), OntologyMetric.metric_alias.ilike(like)))
        .order_by(OntologyMetric.metric_name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "key": row.metric_name,
            "label": row.metric_alias or row.metric_name,
            "hint": row.metric_name,
        }
        for row in rows
    ]


async def _searchOntologyProperty(
    session: AsyncSession, keyword: str, limit: int
) -> list[dict[str, Any]]:
    """属性：key = ``类名.属性名``（脱离类名的属性名无法唯一定位）。"""
    from app.domain.models import OntologyClass, OntologyProperty

    like = f"%{keyword}%"
    stmt = (
        select(OntologyProperty, OntologyClass.class_name)
        .join(OntologyClass, OntologyClass.id == OntologyProperty.class_id)
        .where(
            or_(
                OntologyProperty.property_name.ilike(like),
                OntologyProperty.property_alias.ilike(like),
                OntologyClass.class_name.ilike(like),
            )
        )
        .order_by(OntologyClass.class_name, OntologyProperty.property_name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "key": f"{className}.{prop.property_name}",
            "label": prop.property_alias or prop.property_name,
            "hint": f"{className}.{prop.property_name}",
        }
        for prop, className in rows
    ]


async def searchRelationTargets(
    session: AsyncSession, entityType: str, keyword: str, limit: int = 20
) -> list[dict[str, Any]]:
    """按类型检索关联目标候选（前端下拉用）。

    ``keyword`` 为空 ⇒ 返回该类型的前 N 个（让用户能直接浏览，而不是必须先想关键词）。
    """
    keyword = (keyword or "").strip()
    limit = max(1, min(limit, MAX_TARGETS))
    if entityType == TOPIC_ONTOLOGY_CLASS:
        return await _searchOntologyClass(session, keyword, limit)
    if entityType == TOPIC_ONTOLOGY_METRIC:
        return await _searchOntologyMetric(session, keyword, limit)
    if entityType == TOPIC_ONTOLOGY_PROPERTY:
        return await _searchOntologyProperty(session, keyword, limit)
    # 其余都按业务对象码处理（合法性由调用方先用注册表校验）
    return await _searchBusinessObject(session, entityType, keyword, limit)
