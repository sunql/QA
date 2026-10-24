"""本体元数据导出：开发环境 → ontology_export.json（供 import_ontology.py 导入生产）。

导出范围（7 类配置，全部来自 qa_metadata PG，只读不写）：
- 本体类（别名/对象类型/描述/source_table）——仅当前版本（valid_to IS NULL）
- 本体属性（别名/业务别名/描述/类型/主外键/值引用类/来源列）
- 度量（formula/聚合/目标类）
- 业务对象（code/name/头类/图标签/描述）
- JOIN 目录（两端类 + 列配对 + join_type/relation_type）
- 类级语义关系（三元组 + 描述）
- KPI 目录（业务定义/口径/语义关键词/绑定的数据源名称）

设计要点：导出文件**不含任何数据库 id**——开发与生产的 ontology_class.id
不可通约（生产新装库已跑 seed_ontology），跨环境一律用自然键
（class_name / metric_name / kpi_code / business_object.code / join 配对）锚定，
导入侧负责重映射。

用法：cd backend && uv run python scripts/export_ontology.py [输出路径]
默认输出 ./ontology_export.json。文件含业务元数据（不含密码/密钥），可 scp。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.domain.models import (
    BusinessObject,
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyProperty,
    OntologyRelation,
)
from app.infrastructure.database import getSessionFactory


def _classNameOf(classesById: dict[int, OntologyClass], classId: int | None) -> str | None:
    """id → class_name；导出不携带 id，引用一律转名字。"""
    if classId is None:
        return None
    cls = classesById.get(classId)
    return cls.class_name if cls is not None else None


async def exportOntology(session) -> dict:
    """只读导出全部本体治理元数据，返回可直接 json.dump 的 dict。"""
    classes = (
        await session.execute(
            select(OntologyClass).where(OntologyClass.valid_to.is_(None))
        )
    ).scalars().all()
    classesById = {c.id: c for c in classes}

    properties = (
        await session.execute(
            select(OntologyProperty).where(
                OntologyProperty.class_id.in_([c.id for c in classes])
            )
        )
    ).scalars().all()
    metrics = (await session.execute(select(OntologyMetric))).scalars().all()
    businessObjects = (await session.execute(select(BusinessObject))).scalars().all()
    joins = (await session.execute(select(OntologyJoin))).scalars().all()
    relations = (await session.execute(select(OntologyRelation))).scalars().all()
    kpis = (await session.execute(select(KpiCatalog))).scalars().all()
    dataSourcesById: dict[int, DataSource] = {
        ds.id: ds
        for ds in (await session.execute(select(DataSource))).scalars().all()
    }

    return {
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "classes": [
            {
                "className": c.class_name,
                "classAlias": c.class_alias,
                "description": c.description,
                "sourceTable": c.source_table,
                "objectType": c.object_type,
                "parentClassName": _classNameOf(classesById, c.parent_class_id),
                "createdBy": c.created_by,
            }
            for c in classes
        ],
        "properties": [
            {
                "className": classesById[p.class_id].class_name,
                "propertyName": p.property_name,
                "propertyAlias": p.property_alias,
                "businessAliases": p.business_aliases,
                "description": p.description,
                "dataType": p.data_type,
                "isPrimaryKey": p.is_primary_key,
                "isForeignKey": p.is_foreign_key,
                "refClassName": _classNameOf(classesById, p.ref_class_id),
                "sourceColumn": p.source_column,
            }
            for p in properties
        ],
        "metrics": [
            {
                "metricName": m.metric_name,
                "metricAlias": m.metric_alias,
                "formula": m.formula,
                "aggFunction": m.agg_function,
                "targetClassName": _classNameOf(classesById, m.target_class_id),
                "dimensionDefaults": m.dimension_defaults,
            }
            for m in metrics
        ],
        "businessObjects": [
            {
                "code": bo.code,
                "name": bo.name,
                "headerClassName": _classNameOf(classesById, bo.header_class_id),
                "graphLabel": bo.graph_label,
                "description": bo.description,
            }
            for bo in businessObjects
        ],
        "joins": [
            {
                "sourceClassName": _classNameOf(classesById, j.source_class_id),
                "sourceColumns": list(j.source_columns or []),
                "targetClassName": _classNameOf(classesById, j.target_class_id),
                "targetColumns": list(j.target_columns or []),
                "joinType": j.join_type,
                "relationType": j.relation_type,
                "description": j.description,
            }
            for j in joins
        ],
        "relations": [
            {
                "sourceClassName": _classNameOf(classesById, r.source_class_id),
                "targetClassName": _classNameOf(classesById, r.target_class_id),
                "relationType": r.relation_type,
                "description": r.description,
            }
            for r in relations
        ],
        "kpis": [
            {
                "kpiCode": k.kpi_code,
                "kpiName": k.kpi_name,
                "businessDefinition": k.business_definition,
                "formula": k.formula,
                "numerator": k.numerator,
                "denominator": k.denominator,
                "grain": k.grain,
                "unit": k.unit,
                "dataSource": k.data_source,
                "owner": k.owner,
                "version": k.version,
                "status": k.status,
                "metricName": next(
                    (
                        m.metric_name
                        for m in metrics
                        if m.id == k.metric_id
                    ),
                    None,
                ),
                "semanticKeywords": list(k.semantic_keywords or []) or None,
                "matchThreshold": float(k.match_threshold),
                # 数据源按名称锚定（data_source.name 有 unique 约束）；
                # 生产 datasource_id 另行分配，导入侧按名称回绑。
                "datasourceName": (
                    dataSourcesById[k.datasource_id].name
                    if k.datasource_id is not None
                    and k.datasource_id in dataSourcesById
                    else None
                ),
            }
            for k in kpis
        ],
    }


async def main() -> None:
    outPath = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ontology_export.json")
    factory = getSessionFactory()
    async with factory() as session:
        data = await exportOntology(session)

    counts = {key: len(value) for key, value in data.items() if isinstance(value, list)}
    outPath.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已导出 → {outPath.resolve()}")
    for key, count in counts.items():
        print(f"  {key}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
