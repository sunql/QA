"""P4 D36：试点数据集播种（**真实数据** · 隔离测试库）。

做什么
------
在**隔离测试库**里落一份「采购域 + 质量域」的试点数据集：组织 / 数据域主组织绑定 /
本体类别名 / 属性 / JOIN / 指标 / KPI / 业务对象；然后**走真实发布写路径**
（草稿 → 校验 → 激活）给采购域与质量域各发布一版 ACTIVE 语义发布。

为什么必须走真实写路径
----------------------
试点要验的是「**按发布读**的知识服务」。若快照是手写塞进去的，验的就不是生产里
那条路（编译器 / 发布闸门 / 哈希 / 审计全被绕开）⇒ 试点通过率没有意义。
⇒ 这里用 `semanticReleaseService.createDraft → markValidated → activate`：
manifest 由**真实编译器**从**真实治理表**编译，闸门真跑，审计真写。

与迁移种子的关系（**不重复造类**）
--------------------------------
迁移 0040 已经种了 5 个本体类（Supplier / ItemMaster / PurchaseOrder / Receipt /
IncomingInspection）与 3 个数据域（PROCUREMENT / QUALITY / LOGISTICS）⇒ 本脚本
**复用**这些类（只补别名与属性），不新建同义类：
- 同名类两份 ⇒ manifest 引用闸门（类名重复）会直接挡住发布；
- 类名重复还会让「这个类属于哪个域」变得无法判定。

数据域的组织绑定（**这是权限的唯一抓手**）
--------------------------------------
- `PROCUREMENT.primary_org_id = PROCUREMENT_ORG`（采购部）
- `QUALITY.primary_org_id = QUALITY_ORG`（质量部）
- `LOGISTICS.primary_org_id` **保持 NULL** ⇒ 非 admin 看不见（fail-closed，L-P4-1）
  ⇒ 试点里天然有一个「域无主组织 ⇒ 不可见」的对照样本。

数据集形状（覆盖试点要验的四件事）
----------------------------------
1. **采购域**：Supplier / PurchaseOrder / Receipt / ItemMaster + 属性 + JOIN
   （`po_supplier` / `gr_po`）+ 指标（交付天数 / 订单金额）+ KPI；
2. **质量域**：IncomingInspection + `iqc_gr`（**跨域 JOIN**）+ 来料合格率指标
   ⇒ 用来证明「域切片真的把别的域切掉了」（同一问题在两个域结果不同）；
3. **权限**：组织 `PROCUREMENT_ORG` / `QUALITY_ORG` + 域主组织绑定
   ⇒ 用来证明「域可见性按主组织推导」；
4. **物理层**：类带 `source_table`、属性带 `source_column`
   ⇒ SQL 路能渲染出真实表 / 列。

⚠️ 指标 / KPI 一律 **DRAFT**
--------------------------
治理侧的「发布」有闸门（`kpi_metric_publish_gate`：KPI 还要求数据源与对账通过），
闸门开关默认关。播种脚本直写 `PUBLISHED` 等于**伪造一个没走过的治理状态** ⇒
「已发布」这个状态就失去含义了。
而知识服务读的是**语义发布快照**（`semantic_release`），与指标的治理状态无关
⇒ 试点要验的「按发布读」照样成立。

安全闸（与仓内其它写脚本同口径）
--------------------------------
- 目标库必须命中仓内白名单（`qa_metadata` / `qa_metadata_test*`）；
  正式库（prod 同源）还必须显式 `--allow-prod-write`（`scripts/kpi_metric_db_guard.py`）；
- **默认只预演**：`--apply` 才写库；
- 口令一律打码打印（`maskDsn`）；
- 幂等：重复执行不产生第二份数据（按自然键查在先）。

用法
----
    cd backend
    PILOT_DATABASE_URL='postgresql+asyncpg://user:pwd@localhost:5434/qa_metadata_test_pilot' \
      uv run python -m scripts.knowledge_pilot_seed --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn  # noqa: E402

#: 试点域（采购）与对照域（质量）。
PILOT_DOMAIN_CODE = "PROCUREMENT"
CONTRAST_DOMAIN_CODE = "QUALITY"

#: 试点用到的组织 code。
PILOT_ORG_CODE = "PROCUREMENT_ORG"
CONTRAST_ORG_CODE = "QUALITY_ORG"

#: 发布版本号（幂等键；一个域一版）。
PILOT_RELEASE_VERSIONS: tuple[tuple[str, str], ...] = (
    (PILOT_DOMAIN_CODE, "pilot-2026.11.16-1"),
    (CONTRAST_DOMAIN_CODE, "pilot-2026.11.16-2"),
)

#: 写操作的审计主体。
PILOT_ACTOR = "pilot-seed"

# --- 数据定义（纯数据 ⇒ 便于复核「试点数据集到底长什么样」）-------------------

_ORGS: tuple[tuple[str, str], ...] = (
    (PILOT_ORG_CODE, "采购部"),
    (CONTRAST_ORG_CODE, "质量部"),
    # 不拥有任何域的组织 ⇒ 用来验证「非域主组织查不到别的域」（试点 P-09）。
    ("OUTSIDE_ORG", "其它部"),
)

#: (域 code, 域名, 主组织 code)
_DOMAINS: tuple[tuple[str, str, str | None], ...] = (
    (PILOT_DOMAIN_CODE, "采购域", PILOT_ORG_CODE),
    (CONTRAST_DOMAIN_CODE, "质量域", CONTRAST_ORG_CODE),
)

#: (类名, 别名, 物理表, 对象类型) —— 类名与迁移 0040 种的一致（复用，不新建）。
_CLASSES: tuple[tuple[str, str, str, str], ...] = (
    ("Supplier", "供应商", "DWD_SUPPLIER", "Master"),
    ("PurchaseOrder", "采购订单", "DWD_PURCHASE_ORDER", "Transaction"),
    ("Receipt", "收货单", "DWD_GOODS_RECEIPT", "Transaction"),
    ("ItemMaster", "物料", "DWD_MATERIAL", "Master"),
    ("IncomingInspection", "来料检验单", "DWD_INCOMING_INSPECTION", "Transaction"),
)

#: (类名, 属性名, 属性别名, 业务别名, 类型, 物理列)
_PROPERTIES: tuple[tuple[str, str, str, tuple[str, ...], str, str], ...] = (
    ("Supplier", "supplier_name", "供应商名称", ("供应商",), "varchar", "SUPPLIER_NAME"),
    ("Supplier", "delivery_days", "交付天数", ("交期", "货期"), "int", "DELIVERY_DAYS"),
    ("Supplier", "contact_person", "联系人", (), "varchar", "CONTACT_PERSON"),
    ("PurchaseOrder", "po_no", "采购订单号", ("订单号",), "varchar", "PO_NO"),
    ("PurchaseOrder", "order_date", "下单日期", ("订单日期",), "date", "ORDER_DATE"),
    ("PurchaseOrder", "order_amount", "订单金额", ("采购金额",), "decimal", "ORDER_AMOUNT"),
    ("Receipt", "receipt_no", "收货单号", ("收货号",), "varchar", "RECEIPT_NO"),
    ("Receipt", "receipt_qty", "收货数量", ("到货数量",), "decimal", "RECEIPT_QTY"),
    ("ItemMaster", "material_name", "物料名称", ("物料",), "varchar", "MATERIAL_NAME"),
    ("ItemMaster", "material_spec", "物料规格", ("规格",), "varchar", "MATERIAL_SPEC"),
    ("IncomingInspection", "pass_qty", "合格数量", ("合格数",), "decimal", "PASS_QTY"),
    ("IncomingInspection", "inspect_qty", "检验数量", ("送检数量",), "decimal", "INSPECT_QTY"),
    ("IncomingInspection", "pass_rate", "合格率", ("来料合格率",), "decimal", "PASS_RATE"),
)

#: (joinKey, 源类, 源列, 目标类, 目标列, 关系类型)
_JOINS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("po_supplier", "PurchaseOrder", "SUPPLIER_ID", "Supplier", "ID", "foreign_key"),
    ("gr_po", "Receipt", "PO_ID", "PurchaseOrder", "ID", "foreign_key"),
    ("iqc_gr", "IncomingInspection", "GR_ID", "Receipt", "ID", "business"),
)

#: (指标名, 别名, 类型, 目标类, 聚合函数, 比率聚合阶, 分子类, 分子表达式, 分母类, 分母表达式)
_METRICS: tuple[
    tuple[
        str, str, str, str, str, str, str, str, str | None, str | None
    ],
    ...,
] = (
    (
        "supplier_delivery_days",
        "供应商交付天数",
        "MEASURE",
        "Supplier",
        "AVG",
        "NONE",
        "Supplier",
        "DELIVERY_DAYS",
        None,
        None,
    ),
    (
        "po_amount",
        "采购订单金额",
        "MEASURE",
        "PurchaseOrder",
        "SUM",
        "NONE",
        "PurchaseOrder",
        "ORDER_AMOUNT",
        None,
        None,
    ),
    (
        "iqc_pass_rate",
        "来料合格率",
        "RATIO",
        "IncomingInspection",
        "SUM",
        "WEIGHTED_RATIO",
        "IncomingInspection",
        "PASS_QTY",
        "IncomingInspection",
        "INSPECT_QTY",
    ),
)

#: (KPI 码, KPI 名, 指标名, 语义关键词, 单位)
_KPIS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
    ("KPI_DELIVERY", "供应商交付及时率", "supplier_delivery_days", ("交付", "及时率"), "天"),
    ("KPI_PO_AMOUNT", "采购订单金额", "po_amount", ("订单金额", "采购金额"), "元"),
    ("KPI_IQC", "来料合格率", "iqc_pass_rate", ("合格率", "来料质量"), "%"),
)

#: (BO code, 名称, header 类名, 主域)
_BUSINESS_OBJECTS: tuple[tuple[str, str, str, str], ...] = (
    ("SUPPLIER", "供应商", "Supplier", PILOT_DOMAIN_CODE),
    ("PURCHASE_ORDER", "采购订单", "PurchaseOrder", PILOT_DOMAIN_CODE),
    ("GOODS_RECEIPT", "收货单", "Receipt", PILOT_DOMAIN_CODE),
    ("ITEM_MATERIAL", "物料", "ItemMaster", PILOT_DOMAIN_CODE),
    ("INCOMING_INSPECTION", "来料检验单", "IncomingInspection", CONTRAST_DOMAIN_CODE),
)


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P4 试点数据集播种（默认预演）")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("PILOT_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
        help="目标库 DSN（默认取环境变量 PILOT_DATABASE_URL / DATABASE_URL）",
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许写正式库 qa_metadata（prod 同源；必须先做当日备份）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "允许改绑域的主组织 / 替换同域已有生效发布"
            "（两者都是权限语义变更，默认一律拒绝）"
        ),
    )
    return parser.parse_args()


def _printPlan() -> None:
    """预演：把将要落的数据摊开（便于人工复核）。"""
    print("预演：将落下列数据（--apply 才写库）")
    print(f"  组织：{[code for code, _name in _ORGS]}")
    print(f"  数据域（域 code → 主组织）：{[(code, org) for code, _n, org in _DOMAINS]}")
    print(f"  本体类：{[name for name, *_rest in _CLASSES]}")
    print(f"  属性：{len(_PROPERTIES)} 条")
    print(f"  JOIN：{[key for key, *_rest in _JOINS]}")
    print(f"  指标：{[name for name, *_rest in _METRICS]}")
    print(f"  KPI：{[code for code, *_rest in _KPIS]}")
    print(f"  业务对象：{[code for code, *_rest in _BUSINESS_OBJECTS]}")
    print(f"  发布：{[(domain, version) for domain, version in PILOT_RELEASE_VERSIONS]}")


async def _seed(session: object, *, apply: bool, force: bool = False) -> None:
    """落治理数据（幂等）。"""
    if not apply:
        _printPlan()
        return

    orgIds = await _seedOrganizations(session)
    await _seedDomains(session, orgIds, force=force)
    classIds = await _seedClasses(session)
    await _seedProperties(session, classIds)
    await _seedJoins(session, classIds)
    metricIds = await _seedMetrics(session, classIds)
    await _seedKpis(session, metricIds)
    await _seedBusinessObjects(session, classIds)

    await session.commit()
    print("治理数据已落库（幂等）")


async def _seedOrganizations(session: object) -> dict[str, int]:
    """落组织（已存在则复用）⇒ 返回 组织 code → id。"""
    from sqlalchemy import select

    from app.models.rbac import Organization

    orgIds: dict[str, int] = {}
    for code, name in _ORGS:
        org = (
            await session.execute(select(Organization).where(Organization.code == code))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(code=code, name=name)
            session.add(org)
            await session.flush()
        orgIds[code] = org.id
    return orgIds


async def _seedDomains(
    session: object, orgIds: dict[str, int], *, force: bool
) -> None:
    """落数据域。

    ⚠️ **不改绑**已存在域的 `primary_org_id`：那是**权限推导的唯一抓手**
    （`permission.py`）⇒ 静默改绑 = 悄悄换掉一整个域的可见性。
    确实要改 ⇒ 显式 `--force`。

    ⚠️ 但**空值可以补**：`primary_org_id IS NULL` 时绑定试点主组织是
    「填坑」（迁移种下的域就是这样：还没有任何可见性语义）⇒ 不算改绑。
    """
    from sqlalchemy import select

    from app.domain.models import DataDomain

    for code, name, orgCode in _DOMAINS:
        domain = (
            await session.execute(select(DataDomain).where(DataDomain.code == code))
        ).scalar_one_or_none()
        primaryOrgId = orgIds.get(orgCode) if orgCode else None
        if domain is None:
            session.add(
                DataDomain(
                    code=code, name=name, primary_org_id=primaryOrgId, enabled=True
                )
            )
            await session.flush()
            continue
        if domain.primary_org_id == primaryOrgId:
            continue
        if domain.primary_org_id is None:
            # 填坑：迁移种下的域还没绑主组织（= 不可见，fail-closed）。
            domain.primary_org_id = primaryOrgId
            print(f"域 {code} 补绑主组织：{primaryOrgId}")
            continue
        if not force:
            raise RuntimeError(
                f"域 {code} 已存在且主组织与试点数据集不一致"
                f"（库内={domain.primary_org_id}，试点={primaryOrgId}）—— "
                f"主组织是权限推导的唯一抓手，不静默改绑；确认要改请加 --force"
            )
        domain.primary_org_id = primaryOrgId
        print(f"域 {code} 改绑主组织（--force）：{domain.primary_org_id}")


async def _seedClasses(session: object) -> dict[str, int]:
    """落本体类（复用迁移 0040 已种的同名类）⇒ 返回 类名 → id。"""
    from sqlalchemy import select

    from app.domain.models import OntologyClass

    classIds: dict[str, int] = {}
    for name, alias, table, objectType in _CLASSES:
        cls = (
            await session.execute(
                select(OntologyClass).where(
                    OntologyClass.class_name == name, OntologyClass.version == 1
                )
            )
        ).scalars().first()
        if cls is None:
            cls = OntologyClass(
                class_name=name,
                class_alias=alias,
                description=f"{alias}（试点数据集）",
                source_table=table,
                object_type=objectType,
                object_owner="procurement-owner",
                version=1,
                created_by=PILOT_ACTOR,
            )
            session.add(cls)
            await session.flush()
        elif not cls.class_alias:
            # 迁移种下的类没有别名 ⇒ 补上（别名是**召回词项**，缺了就只能靠英文类名匹配）。
            cls.class_alias = alias
        classIds[name] = cls.id
    return classIds


async def _seedProperties(session: object, classIds: dict[str, int]) -> None:
    """落属性（已存在则跳过）。"""
    from sqlalchemy import select

    from app.domain.models import OntologyProperty

    for className, propName, alias, businessAliases, dataType, column in _PROPERTIES:
        existing = (
            await session.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == classIds[className],
                    OntologyProperty.property_name == propName,
                )
            )
        ).scalars().first()
        if existing is None:
            session.add(
                OntologyProperty(
                    class_id=classIds[className],
                    property_name=propName,
                    property_alias=alias,
                    business_aliases=list(businessAliases),
                    description=f"{alias}（试点数据集）",
                    data_type=dataType,
                    source_column=column,
                    is_primary_key=False,
                    is_foreign_key=False,
                )
            )


async def _seedJoins(session: object, classIds: dict[str, int]) -> None:
    """落 JOIN（已存在则跳过）。"""
    from sqlalchemy import select

    from app.domain.models import OntologyJoin

    for joinKey, sourceClass, sourceColumn, targetClass, targetColumn, relationType in _JOINS:
        existing = (
            await session.execute(
                select(OntologyJoin).where(OntologyJoin.join_key == joinKey)
            )
        ).scalars().first()
        if existing is None:
            session.add(
                OntologyJoin(
                    source_class_id=classIds[sourceClass],
                    source_columns=[sourceColumn],
                    target_class_id=classIds[targetClass],
                    target_columns=[targetColumn],
                    join_type="INNER",
                    relation_type=relationType,
                    description=f"{sourceClass} → {targetClass}",
                    join_key=joinKey,
                    created_by=PILOT_ACTOR,
                )
            )


def _measureOf(expr: str, classId: int) -> dict[str, Any]:
    """一条度量（分子 / 分母同形）。

    ⚠️ 度量里的类引用是 **class_id**（JSON 内嵌引用）：编译器会把内嵌 id
    归一成 className。手写 className 会被编译器的「不应自带 className」拒绝。
    """
    return {
        "agg": "SUM",
        "expr": expr,
        "filter": None,
        "class_id": classId,
        "class_alias": None,
    }


def _metricOf(row: tuple[Any, ...], classIds: dict[str, int]) -> Any:
    """造一条指标 ORM 行（见 `_measureOf` 的 class_id 约定）。"""
    from app.domain.models import OntologyMetric

    (
        metricName,
        alias,
        metricType,
        targetClass,
        aggFunction,
        aggregationOrder,
        numeratorClass,
        numeratorExpr,
        denominatorClass,
        denominatorExpr,
    ) = row
    return OntologyMetric(
        metric_name=metricName,
        metric_alias=alias,
        formula=_formulaOf(numeratorExpr, denominatorExpr),
        agg_function=aggFunction,
        target_class_id=classIds[targetClass],
        metric_type=metricType,
        numerator_measure=_measureOf(numeratorExpr, classIds[numeratorClass]),
        denominator_measure=(
            _measureOf(denominatorExpr, classIds[denominatorClass])
            if denominatorClass is not None and denominatorExpr is not None
            else None
        ),
        aggregation_order=aggregationOrder,
        status="DRAFT",
        owner="procurement-owner",
        execution_source="COMPILED",
        created_by=PILOT_ACTOR,
    )


async def _seedMetrics(session: object, classIds: dict[str, int]) -> dict[str, int]:
    """落指标 + 指标数据源（已存在则跳过）⇒ 返回 指标名 → id。"""
    from sqlalchemy import select

    from app.domain.models import OntologyMetric, OntologyMetricSource

    metricIds: dict[str, int] = {}
    for row in _METRICS:
        metricName = str(row[0])
        numeratorClass = str(row[6])
        metric = (
            await session.execute(
                select(OntologyMetric).where(OntologyMetric.metric_name == metricName)
            )
        ).scalar_one_or_none()
        if metric is None:
            metric = _metricOf(row, classIds)
            session.add(metric)
            await session.flush()
        metricIds[metricName] = metric.id

        source = (
            await session.execute(
                select(OntologyMetricSource).where(
                    OntologyMetricSource.metric_id == metric.id,
                    OntologyMetricSource.class_id == classIds[numeratorClass],
                    OntologyMetricSource.role == "BASE",
                )
            )
        ).scalars().first()
        if source is None:
            session.add(
                OntologyMetricSource(
                    metric_id=metric.id,
                    class_id=classIds[numeratorClass],
                    role="BASE",
                )
            )
    return metricIds


async def _seedKpis(session: object, metricIds: dict[str, int]) -> None:
    """落 KPI（已存在则跳过）。"""
    from sqlalchemy import select

    from app.domain.models import KpiCatalog

    for kpiCode, kpiName, metricName, keywords, unit in _KPIS:
        kpi = (
            await session.execute(
                select(KpiCatalog).where(KpiCatalog.kpi_code == kpiCode)
            )
        ).scalars().first()
        if kpi is None:
            session.add(
                KpiCatalog(
                    kpi_code=kpiCode,
                    kpi_name=kpiName,
                    business_definition=f"{kpiName}（试点数据集口径）",
                    metric_id=metricIds.get(metricName),
                    semantic_keywords=list(keywords),
                    owner="procurement-owner",
                    unit=unit,
                    version="v1.0",
                    status="DRAFT",
                    created_by=PILOT_ACTOR,
                )
            )


async def _seedBusinessObjects(session: object, classIds: dict[str, int]) -> None:
    """落业务对象（已存在则跳过）。"""
    from sqlalchemy import select

    from app.domain.models import BusinessObject

    for boCode, boName, headerClass, primaryDomain in _BUSINESS_OBJECTS:
        bo = (
            await session.execute(
                select(BusinessObject).where(BusinessObject.code == boCode)
            )
        ).scalar_one_or_none()
        if bo is None:
            session.add(
                BusinessObject(
                    code=boCode,
                    name=boName,
                    header_class_id=classIds[headerClass],
                    primary_domain_id=primaryDomain,
                    graph_label=headerClass,
                    description=f"{boName}（试点数据集）",
                    created_by=PILOT_ACTOR,
                )
            )


def _formulaOf(numeratorExpr: str, denominatorExpr: str | None) -> str:
    """指标公式文本（只用于人读；真实口径在分子/分母度量里）。"""
    if denominatorExpr is None:
        return f"SUM({numeratorExpr})"
    return f"SUM({numeratorExpr}) / SUM({denominatorExpr})"


async def _releaseByVersion(session: object, version: str) -> Any:
    """按版本号取发布行（幂等键；同一版本全局唯一）。"""
    from sqlalchemy import select

    from app.domain.semantic_release_models import SemanticRelease

    return (
        await session.execute(
            select(SemanticRelease).where(SemanticRelease.version == version)
        )
    ).scalars().first()


async def _assertReplaceAllowed(
    session: object, domainCode: str, version: str, *, force: bool
) -> None:
    """**替换闸门**：`activate()` 会把同域**其它** ACTIVE 版本归档下线
    ⇒ 那等于替换线上发布。脚本默认**拒绝**（要 `--force` 才放行）。
    """
    from sqlalchemy import select

    from app.domain.enums import SemanticReleaseStatus
    from app.domain.semantic_release_models import SemanticRelease

    actives = (
        await session.execute(
            select(SemanticRelease).where(
                SemanticRelease.domain_code == domainCode,
                SemanticRelease.status == SemanticReleaseStatus.ACTIVE.value,
            )
        )
    ).scalars().all()
    conflicts = [row for row in actives if row.version != version]
    if conflicts and not force:
        raise RuntimeError(
            f"域 {domainCode} 已有生效发布（{'、'.join(row.version for row in conflicts)}），"
            f"发布 {version} 会把它归档下线；确认要替换请加 --force"
        )


async def _applyPublish(
    session: object, existing: Any, domainCode: str, version: str
) -> bool:
    """草稿 → 校验 → 激活（走**真实**服务层写路径）⇒ 成功与否。"""
    from app.domain.enums import SemanticReleaseStatus
    from app.services.semantic_release_service import semanticReleaseService

    if existing is None:
        operation = await semanticReleaseService.createDraft(
            session,
            version=version,
            actor=PILOT_ACTOR,
            domainCode=domainCode,
            departments=(PILOT_ORG_CODE,),
            note="P4 D36 试点数据集",
        )
        if operation.blockers:
            print(f"发布闸门阻断：{operation.blockers}")
            return False
        existing = operation.release

    releaseId = existing.id
    if existing.status == SemanticReleaseStatus.DRAFT.value:
        await semanticReleaseService.markValidated(
            session, releaseId, actor=PILOT_ACTOR, departments=(PILOT_ORG_CODE,)
        )
    await semanticReleaseService.activate(
        session, releaseId, actor=PILOT_ACTOR, departments=(PILOT_ORG_CODE,)
    )
    print(f"已发布：releaseId={releaseId} domain={domainCode} version={version}")
    return True


async def _publishOne(
    session: object,
    domainCode: str,
    version: str,
    *,
    apply: bool,
    force: bool,
) -> str | None:
    """发布「一个域的一个版本」⇒ 返回版本号（已生效 / 本次发布成功）或 `None`。"""
    from app.domain.enums import SemanticReleaseStatus

    existing = await _releaseByVersion(session, version)
    if existing is not None and existing.status == SemanticReleaseStatus.ACTIVE.value:
        print(f"发布已存在且生效：id={existing.id} version={version}")
        return version

    await _assertReplaceAllowed(session, domainCode, version, force=force)
    if not apply:
        return None
    if not await _applyPublish(session, existing, domainCode, version):
        return None
    return version


async def _publish(
    session: object, *, apply: bool, force: bool = False
) -> list[str]:
    """走**真实发布写路径**发布 ACTIVE（一个域一版）。"""
    published: list[str] = []
    for domainCode, version in PILOT_RELEASE_VERSIONS:
        result = await _publishOne(
            session, domainCode, version, apply=apply, force=force
        )
        if result is not None:
            published.append(result)
    return published


async def _run(dsn: str, *, apply: bool, force: bool = False) -> int:
    """播种 + 发布（`apply=False` 时只打印将要做什么）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(dsn, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await _seed(session, apply=apply, force=force)
            published = await _publish(session, apply=apply, force=force)
            if published:
                print(f"已发布版本：{published}")
            elif not apply:
                print("（预演模式：未发布任何发布）")
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    args = _parseArgs()
    if not args.db_url:
        print("缺少目标库：请用 --db-url 或环境变量 PILOT_DATABASE_URL", file=sys.stderr)
        return 2
    assertSafeTarget(args.db_url, allowProdWrite=args.allow_prod_write)
    print(f"目标库：{maskDsn(args.db_url)}")
    if not args.apply:
        print("预演模式（--apply 才写库）")
    return asyncio.run(_run(args.db_url, apply=args.apply, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
