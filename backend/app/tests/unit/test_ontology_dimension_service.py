"""P3a D19：治理维度解析（PG 治理表 → Milvus 过滤维度）。

维度映射规则（P3a D18 定稿，见 `docs/update/1109/p0/milvus-方案.md` §4.3）
------------------------------------------------------------------------
- class：``business_object.header_class_id = class.id`` ⇒ 主域 + 附域 + BO 码；
  组织 = 域的 ``primary_org_id`` → ``organizations.code`` ∪ ``class.object_owner``
  （后者按组织码精确匹配，匹配不到就**不并入**——宁可少一个维度，
  不可猜一个错组织）；
- property：继承其 class 的全部维度；
- metric：经 ``target_class_id`` → class 的维度；
- kpi：经 ``metric_id`` → metric → class 的维度。

三条不可让步的口径
------------------
1. **链路断了就留空**（空数组 / 0），不可瞎猜一个域 —— 空维度在域过滤下
   是 fail-closed（被排除），是**可解释**的收窄；
2. **顺序稳定**（域码/组织码排序后去重）⇒ 回填幂等、影子比对逐字可比；
3. **有效期用 epoch 毫秒**（Milvus 的 INT64 范围比较实测最稳），
   ``valid_to`` 为空 ⇒ 0（= 长期有效的哨兵）。

本文件分两部分：纯映射（不需要 DB）+ 真实 PG 解析（用 unit 夹具的 dbSession）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ontology_dimension_service import (
    DimensionSet,
    buildClassDimension,
    resolveDimensions,
)


# ---------------------------------------------------------------------------
# 1. 纯映射
# ---------------------------------------------------------------------------


class Test纯映射:
    def test_未归域的类得到全空维度(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=(),
        )
        assert dim == DimensionSet()

    def test_有BO时实体类型取BO码(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode="SUPPLIER",
            primaryDomainCode="PROCUREMENT",
            attachedDomainCodes=(),
            orgCodes=(),
        )
        assert dim.entityType == "SUPPLIER"

    def test_主域与附域合并去重且排序(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode="SUPPLIER",
            primaryDomainCode="PROCUREMENT",
            attachedDomainCodes=("LOGISTICS", "PROCUREMENT", "QUALITY"),
            orgCodes=(),
        )
        assert dim.domainCodes == ("LOGISTICS", "PROCUREMENT", "QUALITY")

    def test_组织码进入组织维度(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode="SUPPLIER",
            primaryDomainCode="PROCUREMENT",
            attachedDomainCodes=(),
            orgCodes=("procurement",),
        )
        assert dim.orgCodes == ("procurement",)

    def test_组织码去重且排序(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=("zzz", "aaa", "zzz"),
        )
        assert dim.orgCodes == ("aaa", "zzz")

    def test_有效期转epoch毫秒(self) -> None:
        dim = buildClassDimension(
            validFrom=datetime(2026, 1, 1, tzinfo=timezone.utc),
            validTo=datetime(2026, 2, 1, tzinfo=timezone.utc),
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=(),
        )
        assert dim.validFrom == 1_767_225_600_000
        assert dim.validTo == 1_769_904_000_000

    def test_无结束时间视为长期有效哨兵零(self) -> None:
        dim = buildClassDimension(
            validFrom=datetime(2026, 1, 1, tzinfo=timezone.utc),
            validTo=None,
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=(),
        )
        assert dim.validTo == 0

    def test_无起始时间视为自始哨兵零(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=(),
        )
        assert dim.validFrom == 0

    def test_发布版本可透传(self) -> None:
        dim = buildClassDimension(
            validFrom=None,
            validTo=None,
            boCode=None,
            primaryDomainCode=None,
            attachedDomainCodes=(),
            orgCodes=(),
            releaseId=7,
        )
        assert dim.releaseId == 7


# ---------------------------------------------------------------------------
# 2. 真实 PG 解析
# ---------------------------------------------------------------------------


async def _seedOrg(session: AsyncSession, code: str) -> int:
    """建组织（测试库 truncate 后 organizations 为空，域的主管理组织要自己种）。"""
    from app.models.rbac import Organization

    org = Organization(code=code, name=f"组织 {code}", sort_order=0)
    session.add(org)
    await session.flush()
    return org.id


async def _seedDomain(session: AsyncSession, code: str, primaryOrgId: int | None) -> None:
    from app.domain.models import DataDomain

    session.add(
        DataDomain(
            code=code,
            name=f"域 {code}",
            primary_org_id=primaryOrgId,
            enabled=True,
        )
    )
    await session.flush()


async def _seedClass(
    session: AsyncSession,
    *,
    className: str,
    objectOwner: str | None = None,
) -> int:
    from app.domain.models import OntologyClass

    row = OntologyClass(
        class_name=className,
        class_alias=None,
        description="维度解析用例",
        version=1,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=None,
        object_owner=objectOwner,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _seedBusinessObject(
    session: AsyncSession,
    *,
    code: str,
    headerClassId: int,
    primaryDomainId: str,
) -> None:
    from app.domain.models import BusinessObject

    session.add(
        BusinessObject(
            code=code,
            name=f"BO {code}",
            header_class_id=headerClassId,
            primary_domain_id=primaryDomainId,
        )
    )
    await session.flush()


class Test真实库解析:
    async def test_类解析出主域与实体类型(self, dbSession: AsyncSession) -> None:
        orgId = await _seedOrg(dbSession, "dimorg")
        await _seedDomain(dbSession, "DIMDOM", orgId)
        classId = await _seedClass(dbSession, className="DIM_CLASS_A")
        await _seedBusinessObject(
            dbSession, code="DIMBOA", headerClassId=classId, primaryDomainId="DIMDOM"
        )
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "class", [classId])

        assert classId in dims
        dim = dims[classId]
        assert dim.entityType == "DIMBOA"
        assert dim.domainCodes == ("DIMDOM",)
        # 域的主管理组织 → 组织码
        assert dim.orgCodes == ("dimorg",)

    async def test_附域并入域维度(self, dbSession: AsyncSession) -> None:
        from app.domain.models import BusinessObjectDomain

        orgId = await _seedOrg(dbSession, "dimorg2")
        await _seedDomain(dbSession, "DIMDOM2", orgId)
        classId = await _seedClass(dbSession, className="DIM_CLASS_B")
        await _seedBusinessObject(
            dbSession, code="DIMBOB", headerClassId=classId, primaryDomainId="DIMDOM2"
        )
        dbSession.add(BusinessObjectDomain(bo_code="DIMBOB", domain_id="LOGISTICS"))
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "class", [classId])

        assert dims[classId].domainCodes == ("DIMDOM2", "LOGISTICS")

    async def test_未归域类维度内容为空(self, dbSession: AsyncSession) -> None:
        """未归 BO 头 ⇒ 域/组织/实体三维度全空。

        ⚠️ 有效期**不**随「未归域」清空：``ontology_class.valid_from`` 非空，
        它是类自身的生效时间窗（映射规则见 `p0/milvus-方案.md` §4.3）⇒
        这里断言「内容维度为空 + 有效期照常」，而不是整块 ``DimensionSet()`` 相等。
        """
        classId = await _seedClass(dbSession, className="DIM_CLASS_C")
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "class", [classId])

        dim = dims[classId]
        assert dim.domainCodes == ()
        assert dim.orgCodes == ()
        assert dim.entityType == ""
        assert dim.validFrom == 1_767_225_600_000
        assert dim.validTo == 0

    async def test_对象负责人按组织码精确匹配才并入(self, dbSession: AsyncSession) -> None:
        """匹配不到组织 ⇒ 不并入（宁可少一个维度，不可猜错组织）。"""
        await _seedOrg(dbSession, "dimorg3")
        classId = await _seedClass(
            dbSession, className="DIM_CLASS_D", objectOwner="不存在的组织"
        )
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "class", [classId])

        assert dims[classId].orgCodes == ()

    async def test_对象负责人命中组织码时并入(self, dbSession: AsyncSession) -> None:
        await _seedOrg(dbSession, "dimorg4")
        classId = await _seedClass(
            dbSession, className="DIM_CLASS_D2", objectOwner="dimorg4"
        )
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "class", [classId])

        assert dims[classId].orgCodes == ("dimorg4",)

    async def test_属性继承其类的维度(self, dbSession: AsyncSession) -> None:
        from app.domain.models import OntologyProperty

        orgId = await _seedOrg(dbSession, "dimorg5")
        await _seedDomain(dbSession, "DIMDOM5", orgId)
        classId = await _seedClass(dbSession, className="DIM_CLASS_E")
        await _seedBusinessObject(
            dbSession, code="DIMBOE", headerClassId=classId, primaryDomainId="DIMDOM5"
        )
        prop = OntologyProperty(
            class_id=classId,
            property_name="dim_prop",
            description="维度解析用例",
            data_type="STRING",
        )
        dbSession.add(prop)
        await dbSession.flush()
        propId = prop.id
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "property", [propId])

        assert dims[propId].entityType == "DIMBOE"
        assert dims[propId].domainCodes == ("DIMDOM5",)

    async def test_指标经目标类继承维度(self, dbSession: AsyncSession) -> None:
        from app.domain.models import OntologyMetric

        orgId = await _seedOrg(dbSession, "dimorg6")
        await _seedDomain(dbSession, "DIMDOM6", orgId)
        classId = await _seedClass(dbSession, className="DIM_CLASS_F")
        await _seedBusinessObject(
            dbSession, code="DIMBOF", headerClassId=classId, primaryDomainId="DIMDOM6"
        )
        metric = OntologyMetric(
            metric_name="dim_metric",
            formula="sum(a)",
            agg_function="SUM",
            target_class_id=classId,
        )
        dbSession.add(metric)
        await dbSession.flush()
        metricId = metric.id
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "metric", [metricId])

        assert dims[metricId].entityType == "DIMBOF"

    async def test_指标无目标类时维度全空(self, dbSession: AsyncSession) -> None:
        from app.domain.models import OntologyMetric

        metric = OntologyMetric(
            metric_name="dim_metric_orphan",
            formula="sum(b)",
            agg_function="SUM",
            target_class_id=None,
        )
        dbSession.add(metric)
        await dbSession.flush()
        metricId = metric.id
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "metric", [metricId])

        assert dims[metricId] == DimensionSet()

    async def test_KPI经指标继承维度(self, dbSession: AsyncSession) -> None:
        from app.domain.models import KpiCatalog, OntologyMetric

        orgId = await _seedOrg(dbSession, "dimorg7")
        await _seedDomain(dbSession, "DIMDOM7", orgId)
        classId = await _seedClass(dbSession, className="DIM_CLASS_G")
        await _seedBusinessObject(
            dbSession, code="DIMBOG", headerClassId=classId, primaryDomainId="DIMDOM7"
        )
        metric = OntologyMetric(
            metric_name="dim_metric_kpi",
            formula="sum(c)",
            agg_function="SUM",
            target_class_id=classId,
        )
        dbSession.add(metric)
        await dbSession.flush()
        kpi = KpiCatalog(
            kpi_code="DIMKPI",
            kpi_name="维度解析 KPI",
            business_definition="用例",
            metric_id=metric.id,
        )
        dbSession.add(kpi)
        await dbSession.flush()
        kpiId = kpi.id
        await dbSession.commit()

        dims = await resolveDimensions(dbSession, "kpi", [kpiId])

        assert dims[kpiId].entityType == "DIMBOG"
        assert dims[kpiId].domainCodes == ("DIMDOM7",)

    async def test_未知类型报错(self, dbSession: AsyncSession) -> None:
        with pytest.raises(ValueError):
            await resolveDimensions(dbSession, "unknown", [1])

    async def test_空id列表返回空映射(self, dbSession: AsyncSession) -> None:
        assert await resolveDimensions(dbSession, "class", []) == {}
