"""Task 6 单元守卫：关键词锚定接线（`_selectRelevantClasses`）。

锚定（2026-11-19「问法敏感漏选」根治）走**正常命中路径**：向量命中之后、
MAX_CLASSES 截断之前，把问句片段字面匹配的类强制纳入候选并排在命中之前。
本文件覆盖接线语义（顺序/去重/计数/上限闸门/跨源/ODS 闸门），集成层用例见
`app/tests/integration/test_classify_and_recall.py::test_anchor_forces_missed_class_into_candidates`。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.models import OntologyClass
from app.domain.multi_step_plan import StepPlan
from app.services.chat_helpers import _PipelineContext
from app.services.chat_recall import (
    _ANCHOR_MAX_CLASSES_DEFAULT,
    _CLASS_FILTER_MAX_CLASSES_DEFAULT,
)
from app.services.chat_service import ChatService
from app.tests.unit.test_chat_service import (
    _FakeOntologyService,
    _FakeSession,
    _buildService,
)


def _cls(
    cid: int, name: str, alias: str | None = None, src: str | None = None
) -> OntologyClass:
    return OntologyClass(
        id=cid, class_name=name, class_alias=alias, description=None,
        source_table=src or name, properties=[],
    )


def _service(classes: list[OntologyClass], hits: list[int]):
    ontology = _FakeOntologyService(
        classes, searchHits=[SimpleNamespace(id=i) for i in hits],
    )
    return _buildService(ontology=ontology)[0]


class _RoutingSession:
    """按语句路由的假会话：system_config 现读值 + schema_cache 物理表清单。

    - 文本查询命中 ``ANCHOR_MAX_CLASSES`` / ``CLASS_FILTER_MAX_CLASSES`` 时返回
      对应值；其余 system_config 键返回 None（走各自默认值）。
    - ``select(schema_cache)`` 返回给定 schema_data（跨源过滤的表名集合来源）。

    与 ``_FakeSession`` 的差别：本类能按 SQL 文本分派不同值，用于验证
    「运行期配置真的作用到接线」——单测里所有配置读数都返默认时，闸门是否
    被接线读到根本测不出来。
    """

    def __init__(
        self,
        *,
        anchorCap: str | None = None,
        maxClasses: str | None = None,
        schemaData: list[dict] | None = None,
    ) -> None:
        self._anchorCap = anchorCap
        self._maxClasses = maxClasses
        self._schemaData = schemaData or []
        self.info: dict = {}

    async def execute(self, stmt):
        sql = str(stmt).upper()
        if "SCHEMA_CACHE" in sql:
            rows = [SimpleNamespace(schema_data=self._schemaData)]

            class _Scalars:
                def all(self) -> list:
                    return list(rows)

            class _Result:
                def scalars(self):
                    return _Scalars()

                def scalar_one_or_none(self):
                    return None

            return _Result()
        value: str | None = None
        if "ANCHOR_MAX_CLASSES" in sql:
            value = self._anchorCap
        elif "CLASS_FILTER_MAX_CLASSES" in sql:
            value = self._maxClasses

        class _Result:
            def scalar_one_or_none(self):
                return value

        return _Result()


class TestAnchorWiring:
    """锚定接线语义。"""

    @pytest.mark.asyncio
    async def test_anchored_class_precedes_vector_hit(self) -> None:
        """锚定类排向量命中之前（截断前接入 ⇒ 不被 MAX_CLASSES 挤掉）。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        access = _cls(2, "SUPPLY_ACCESS", "供应商准入")
        service = _service([supplier, access], hits=[2])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, access]
        )

        assert [c.id for c in result] == [1, 2]
        assert recall.anchoredCount == 1  # 只计向量没捞到、靠锚定新增的类

    @pytest.mark.asyncio
    async def test_anchored_class_already_hit_not_counted_again(self) -> None:
        """向量已命中的类不重复计入 anchoredCount（去重）。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        service = _service([supplier], hits=[1])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier]
        )

        assert [c.id for c in result] == [1]
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_anchor_survives_max_classes_truncation(self) -> None:
        """锚定类排在命中之前 ⇒ MAX_CLASSES 截断时被保下来（截断不挤掉锚定）。

        截断取前 N 个：锚定类置顶 ⇒ 即使命中数超上限，锚定类仍在窗口内。
        """
        cap = _CLASS_FILTER_MAX_CLASSES_DEFAULT
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        others = [_cls(100 + i, f"C{i}") for i in range(cap + 5)]
        classes = [supplier] + others
        service = _service(classes, hits=[c.id for c in others])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", classes
        )

        assert 1 in [c.id for c in result]  # 锚定类未被截断挤掉
        assert recall.truncated is True

    @pytest.mark.asyncio
    async def test_ods_business_table_not_anchored(self) -> None:
        """ODS 业务表不进锚定候选（2026-09-19 ODS_BPARTNER 事故同类闸门）。

        向量命中路径对 ODS 业务表有过滤，锚定是另一条入候选的路径 ⇒ 必须同口径，
        否则「问句提到供应商」会把贴源备份表捞回 schema 让 LLM 幻觉属性名。
        """
        real = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        ods = _cls(2, "ODS_BPARTNER", "供应商伙伴表", src="ODS_BPARTNER")
        service = _service([real, ods], hits=[1])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [real, ods]
        )

        assert [c.id for c in result] == [1]
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_ods_business_table_anchored_when_explicitly_requested(self) -> None:
        """显式点名 ODS 表时锚定保留（与向量命中路径同口径：显式请求不拦）。"""
        real = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        ods = _cls(2, "ODS_BPARTNER", "供应商伙伴表", src="ODS_BPARTNER")
        service = _service([real, ods], hits=[1])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "ODS_BPARTNER 里有哪些供应商", [real, ods]
        )

        assert 2 in [c.id for c in result]
        assert recall.anchoredCount == 1


class TestAnchorCapWiring:
    """`ANCHOR_MAX_CLASSES` 上限在接线层真的生效（不是只读出来就丢）。"""

    @pytest.mark.asyncio
    async def test_anchor_cap_limits_number_of_anchored_classes(self) -> None:
        """配 2 ⇒ 只锚 2 个类（其余字面匹配的类不进候选）。"""
        classes = [_cls(i, f"TBL_{i}", "供应商维度") for i in range(1, 6)]
        classes.append(_cls(99, "RECEIPT", "收货单"))  # 唯一向量命中（不匹配问句）
        service = _service(classes, hits=[99])

        result, recall = await service._selectRelevantClasses(
            _RoutingSession(anchorCap="2"), "公司目前有多少供应商", classes
        )

        assert recall.anchoredCount == 2
        assert [c.id for c in result][:2] == [1, 2]  # 锚定在前且只有 2 个

    @pytest.mark.asyncio
    async def test_anchor_cap_clamped_to_max_classes_window(self) -> None:
        """锚定上限夹到 MAX_CLASSES：披露数 = 实际留在窗口里的锚定数（不说多）。"""
        cap = _CLASS_FILTER_MAX_CLASSES_DEFAULT
        classes = [_cls(i, f"TBL_{i}", "供应商维度") for i in range(1, cap + 10)]
        classes.append(_cls(999, "RECEIPT", "收货单"))
        service = _service(classes, hits=[999])

        result, recall = await service._selectRelevantClasses(
            _RoutingSession(anchorCap="100"), "公司目前有多少供应商", classes
        )

        assert recall.anchoredCount == cap  # 夹到窗口内，不是配置的 100
        assert len(result) == cap
        assert recall.truncated is True


class TestMultistepAnchorDatasourceGuard:
    """多步步骤召回的锚定同样不得捞回跨源幽灵类。"""

    @pytest.mark.asyncio
    async def test_step_recall_never_anchors_cross_source_class(self) -> None:
        """步骤召回先跨源过滤 ⇒ 跨源类不进锚定池（生产事故 ORA-00942 同源）。"""
        ghost = _cls(1, "MAIN_DATA_SUPPLIER", "供应商主数据", src="main_data_supplier")
        real = _cls(2, "SUPPLY_ACCESS", "供应商准入", src="SUPPLY_ACCESS")
        service = _service([ghost, real], hits=[2])
        session = _RoutingSession(
            schemaData=[{"table_name": "SUPPLY_ACCESS", "owner": "ZJTH"}],
        )
        pc = _PipelineContext(
            ds=SimpleNamespace(id=7), classes=[], configs=[],
            selected=None, client=None, contextPrompt="",
        )
        stepPlan = StepPlan(
            index=0, description="查供应商", sub_question="公司目前有多少供应商",
        )

        newPc = await service._recallForStep(session, pc, stepPlan)

        assert [c.id for c in newPc.classes] == [2]  # 跨源幽灵类既不命中也不被锚定


class TestAnchorMaxClassesConfig:
    """`ANCHOR_MAX_CLASSES` 上限闸门（system_config 运行时读）。"""

    @staticmethod
    def _sessionReturning(value: str):
        class _FakeSessionRead:
            async def execute(self, stmt):
                class _R:
                    def scalar_one_or_none(self_inner):
                        return value

                return _R()

        return _FakeSessionRead()

    @pytest.mark.asyncio
    async def test_reads_configured_value(self) -> None:
        svc = object.__new__(ChatService)
        assert await svc._getAnchorMaxClasses(self._sessionReturning("3")) == 3

    @pytest.mark.asyncio
    async def test_falls_back_to_default_on_invalid_values(self) -> None:
        """缺席/空/格式错/非正 ⇒ 返默认（非正不是「不锚定」而是闸门非法）。"""
        svc = object.__new__(ChatService)
        for badRaw in (None, "", "not-an-int", "0", "-3"):
            assert (
                await svc._getAnchorMaxClasses(self._sessionReturning(badRaw))
                == _ANCHOR_MAX_CLASSES_DEFAULT
            )
