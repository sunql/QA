"""Task 6 单元守卫：关键词锚定接线（`_selectRelevantClasses`）。

锚定（2026-11-19「问法敏感漏选」根治）走**正常命中路径**：向量命中之后、
MAX_CLASSES 截断之前，把问句片段字面匹配的类强制纳入候选并排在命中之前。
本文件覆盖接线语义（顺序/去重/计数/上限闸门/跨源/ODS 闸门），集成层用例见
`app/tests/integration/test_classify_and_recall.py::test_anchor_forces_missed_class_into_candidates`。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.domain.models import OntologyClass
from app.domain.multi_step_plan import StepPlan
from app.services.chat_helpers import _PipelineContext
from app.services.chat_recall import (
    _ANCHOR_MAX_CLASSES_DEFAULT,
    _CLASS_FILTER_MAX_CLASSES_DEFAULT,
    _CLASS_SCOPE_MAX_IDS,
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


class TestAnchorFallbackRescue:
    """Gap B：降级路径也执行锚定（向量路径失效时的锚定救援）。

    生产事故（2026-11-19）：跨源过滤后 94 个本地类，向量 top20 全落在跨源/墓碑类上
    ⇒ 0 命中解析 ⇒ `no_match` 早退 ⇒ 锚定块（位于早退之后）从未执行
    ⇒ 兜底把 43 个本地类全塞进 schema，召回裁剪被架空。

    语义：向量路径失效时**锚定命中 ⇒ 窄窗口（锚定类）**；锚定也没命中才保留原全量回退
    （宁可宽不可答不出）。检索基础设施故障（search_error / no_hits）不接管。

    两道闸门（任一不过 ⇒ 回落全量回退）：
    - 数据源口径（``isDatasourceScoped``）：候选集没按数据源收窄时不接管；
    - 证据强度：唯一 2 字弱匹配（误锚定会独占窗口）时不接管。
    """

    @pytest.mark.asyncio
    async def test_no_match_rescued_by_anchor_narrows_window(self) -> None:
        """向量命中解析不出本地类 + 问句字面命中本地类 ⇒ 窗口收敛到锚定类。

        999 不在类集里 ⇒ hits 解析不出类 ⇒ 走 no_match 分支（既有早退点）。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")
        service = _service([supplier, receipt], hits=[999])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, receipt],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]  # 只剩锚定类（不是 43 类宽窗）
        assert recall.mode == "rescue"  # 独立 mode ⇒ 前端能出降级告警
        assert recall.anchoredCount == 1
        assert recall.hitCount == 1  # 纯向量命中数 = hitCount - anchoredCount = 0

    @pytest.mark.asyncio
    async def test_no_match_without_anchor_keeps_wide_fallback(self) -> None:
        """锚定也没命中 ⇒ 保留原全量回退（行为逐字不变）。"""
        classes = [_cls(1, "C1"), _cls(2, "C2")]
        service = _service(classes, hits=[999])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "收货数量", classes, isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1, 2]
        assert recall.mode == "fallback"
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_ods_only_hits_rescued_by_anchor(self) -> None:
        """命中全是 ODS 业务表 ⇒ 锚定救援（ODS 业务表本身不进锚定池）。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        ods = _cls(2, "ODS_BPARTNER", "供应商伙伴表", src="ODS_BPARTNER")
        service = _service([supplier, ods], hits=[2])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, ods],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]  # ODS 类既不命中也不被锚定
        assert recall.mode == "rescue"
        assert recall.anchoredCount == 1

    @pytest.mark.asyncio
    async def test_ods_only_hits_branch_logged_with_its_own_reason(self, caplog) -> None:
        """ods_only_hits 与 no_match 是两条分支 ⇒ 日志里的 reason 必须能区分。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        ods = _cls(2, "ODS_BPARTNER", "供应商伙伴表", src="ODS_BPARTNER")
        service = _service([supplier, ods], hits=[2])

        with caplog.at_level(logging.WARNING, logger="app.services.chat_recall"):
            await service._selectRelevantClasses(
                _FakeSession(), "公司目前有多少供应商", [supplier, ods],
                isDatasourceScoped=True,
            )

        assert "reason=ods_only_hits" in caplog.text
        assert "reason=no_match" not in caplog.text

    @pytest.mark.asyncio
    async def test_search_error_keeps_wide_fallback(self) -> None:
        """检索基础设施故障 ⇒ 不接管（宽窗更稳），行为不变。"""

        class _BoomSearchOntology(_FakeOntologyService):
            async def searchByKeyword(
                self, query, *, topK=5, typeFilter=None, classOntologyIds=None
            ) -> list:
                raise RuntimeError("Milvus 不可用")

        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        service = _buildService(ontology=_BoomSearchOntology([supplier]))[0]

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]
        assert recall.mode == "fallback"
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_no_hits_keeps_wide_fallback(self) -> None:
        """检索 0 命中 ⇒ 不接管（更像索引/嵌入异常），行为不变。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        service = _service([supplier], hits=[])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]
        assert recall.mode == "fallback"
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_rescue_respects_anchor_cap(self) -> None:
        """救援窗口同样受 ANCHOR_MAX_CLASSES 上限约束。"""
        classes = [_cls(i, f"TBL_{i}", "供应商维度") for i in range(1, 4)]
        service = _service(classes, hits=[999])

        result, recall = await service._selectRelevantClasses(
            _RoutingSession(anchorCap="1"), "公司目前有多少供应商", classes,
            isDatasourceScoped=True,
        )

        assert recall.anchoredCount == 1
        assert [c.id for c in result] == [1]

    @pytest.mark.asyncio
    async def test_rescue_logs_exactly_one_reason_line(self, caplog) -> None:
        """回退率按「含 reason= 的行」聚合 ⇒ 救援路径也恰好一行 reason=。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")
        service = _service([supplier, receipt], hits=[999])

        with caplog.at_level(logging.WARNING, logger="app.services.chat_recall"):
            await service._selectRelevantClasses(
                _FakeSession(), "公司目前有多少供应商", [supplier, receipt],
                isDatasourceScoped=True,
            )

        assert caplog.text.count("reason=no_match") == 1
        assert "anchored=1" in caplog.text  # 救援与普通回退可区分

    @pytest.mark.asyncio
    async def test_rescue_never_returns_empty_window(self) -> None:
        """救援窗口恒非空（空 schema 会让整问失败）。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        service = _service([supplier], hits=[999])

        result, _ = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_unscoped_candidates_keep_wide_fallback(self) -> None:
        """闸门 1：候选集未按数据源收窄 ⇒ 不接管（窄窗可能全是别源表）。

        未选数据源（no_datasource）或数据源目录缺失（no_cache）时，候选集仍含
        别源类；此时锚定命中可能命中别源表，窄窗一旦只含别源表就是必然的
        ORA-00942 ⇒ 保留宽窗。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")
        service = _service([supplier, receipt], hits=[999])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, receipt],
        )

        assert [c.id for c in result] == [1, 2]  # 宽窗，未收窄
        assert recall.mode == "fallback"
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_weak_single_anchor_keeps_wide_fallback(self) -> None:
        """闸门 2：唯一 2 字弱匹配 ⇒ 证据不足 ⇒ 保留宽窗。

        「公司」这类常用词极易误命中；弱命中独占窗口（救援路径没有向量命中
        兜底）比宽窗更糟 ⇒ 宁可宽不可答不出。
        """
        company = _cls(1, "DIM_COMPANY", "公司主数据")
        supplier = _cls(2, "DIM_SUPPLIER", "供应商主数据")
        service = _service([company, supplier], hits=[999])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司信息有哪些", [company, supplier],
            isDatasourceScoped=True,
        )

        # 问句里只有「公司」(2 字) 能命中 DIM_COMPANY（「供应商」不在问句里）
        assert [c.id for c in result] == [1, 2]  # 宽窗
        assert recall.mode == "fallback"
        assert recall.anchoredCount == 0

    @pytest.mark.asyncio
    async def test_exact_short_anchor_still_narrows(self) -> None:
        """精确等值命中即使只有 2 字也是强证据 ⇒ 允许收窄。"""
        stock = _cls(1, "STOCK", "库存")
        other = _cls(2, "RECEIPT", "收货单")
        service = _service([stock, other], hits=[999])

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "库存", [stock, other], isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]
        assert recall.mode == "rescue"

    @pytest.mark.asyncio
    async def test_dimension_hint_pulls_dim_classes_into_rescue(self) -> None:
        """维度词（分类/编号…）触发的 DIM 补拉在救援路径同样生效。

        救援路径没有向量命中兜底，若整条丢掉维度词 DIM 补拉，含维度词的问句
        会漏掉维度表 ⇒ 窗口与正常路径不一致。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        material = _cls(3, "DIM_MATERIAL", "物料维度")
        service = _service([supplier, material], hits=[999])

        async def _fakeDimClasses(session):
            return [material]

        service._fetchAllDimClasses = _fakeDimClasses  # type: ignore[method-assign]

        # 「分类」是维度词 ⇒ 触发 DIM 补拉；「物料」不在问句里 ⇒ material 不被锚定
        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "供应商有多少种分类", [supplier, material],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1, 3]  # 锚定类 + 维度词 DIM 补拉
        assert recall.mode == "rescue"
        assert recall.anchoredCount == 1  # material 靠 DIM 补拉进来，不算锚定

    @pytest.mark.asyncio
    async def test_rescue_window_survives_expand_failure(self) -> None:
        """扩边异常 ⇒ 退化为未扩边的锚定窗口（不让整问失败）。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        service = _service([supplier], hits=[999])

        async def _boom(*args, **kwargs):
            raise RuntimeError("JOIN 目录炸了")

        service._expandByJoinNeighbors = _boom  # type: ignore[method-assign]

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert [c.id for c in result] == [1]
        assert recall.mode == "rescue"


class TestMultistepAnchorRescue:
    """步骤级召回同样会走救援路径（多步的第二步也可能命中解析不出类）。"""

    @pytest.mark.asyncio
    async def test_step_recall_rescues_narrows_window(self) -> None:
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")
        service = _service([supplier, receipt], hits=[999])
        session = _RoutingSession(
            schemaData=[{"table_name": "DIM_SUPPLIER", "owner": "ZJTH"},
                        {"table_name": "RECEIPT", "owner": "ZJTH"}],
        )
        pc = _PipelineContext(
            ds=SimpleNamespace(id=7), classes=[], configs=[],
            selected=None, client=None, contextPrompt="",
        )
        stepPlan = StepPlan(
            index=0, description="查供应商", sub_question="公司目前有多少供应商",
        )

        newPc = await service._recallForStep(session, pc, stepPlan)

        assert [c.id for c in newPc.classes] == [1]  # 锚定窄窗（非宽窗全量）



class TestClassIdScopeWiring:
    """Gap A：本体类 id 作用域推进向量检索窗口（接线层）。

    根因（生产实证 2026-11-19）：数据源过滤只在**应用侧候选集**生效，而向量
    检索窗口是**全局**的 ⇒ topK 名额被跨源类/墓碑类吃光 ⇒
    ``hits=20 total=94`` 里能解析到本地类的为 0。

    接线层的两条不可让步的口径：
    1. **作用域 = 候选集的 id 集**（与候选集同口径 ⇒ 窗口与候选集一致）；
    2. **无作用域时调用形态逐字不变**（不向下游多传 kwarg）——既有替身只认
       ``(query, *, topK, typeFilter)``，多传 kwarg 会让它们整体抛 TypeError
       ⇒ 被 ``except Exception`` 吞成 ``search_error`` ⇒ 测试静默失去向量
       路径的覆盖（假绿）。
    """

    @pytest.mark.asyncio
    async def test_scoped_call_carries_local_class_ids(self) -> None:
        """候选集已按数据源收窄 ⇒ 作用域 = 候选集的 id 集。"""
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")
        ontology = _FakeOntologyService(
            [supplier, receipt], searchHits=[SimpleNamespace(id=2)],
        )
        service = _buildService(ontology=ontology)[0]

        await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, receipt],
            isDatasourceScoped=True,
        )

        assert len(ontology.searchCalls) == 1
        assert ontology.searchCalls[0]["classOntologyIds"] == [1, 2]

    @pytest.mark.asyncio
    async def test_unscoped_call_shape_is_unchanged(self) -> None:
        """候选集未按数据源收窄 ⇒ **不多传 kwarg**（调用形态与今天逐字一致）。

        用一个**只认** ``(query, *, topK, typeFilter)`` 的严格替身证明：真多传了
        ``classOntologyIds`` ⇒ TypeError ⇒ 会被 ``except Exception`` 吞成
        ``search_error`` ⇒ 表现为「宽窗回退」而不是报错。

        断言用「没有降级」而不是「没报错」：降级是静默的，报错反而不是。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")
        receipt = _cls(2, "RECEIPT", "收货单")

        class _StrictOntology(_FakeOntologyService):
            """故意**不**接受 classOntologyIds：多传 kwarg 必然 TypeError。"""

            async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
                return [SimpleNamespace(id=1)]

        service = _buildService(
            ontology=_StrictOntology([supplier, receipt])
        )[0]

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier, receipt],
        )

        # 若多传了 kwarg ⇒ TypeError ⇒ search_error ⇒ mode="fallback" 且全量回退
        assert recall.mode == "recall"
        assert [c.id for c in result] == [1]

    @pytest.mark.asyncio
    async def test_empty_scope_is_deny_all_not_unlimited(self) -> None:
        """候选集里没有任何带 id 的类 ⇒ 作用域 = 空集（deny-all），**不是**不限。

        回落成「不限」= 把「本数据源没有类」误读成「全局都能看」⇒ 跨源类又回来。
        """
        noId = OntologyClass(
            id=None, class_name="X", class_alias=None, description=None,
            source_table="X", properties=[],
        )
        ontology = _FakeOntologyService([noId], searchHits=[])
        service = _buildService(ontology=ontology)[0]

        await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [noId],
            isDatasourceScoped=True,
        )

        assert ontology.searchCalls[0]["classOntologyIds"] == []

    @pytest.mark.asyncio
    async def test_scope_excludes_cross_source_classes(self) -> None:
        """作用域只含**本地**类 id（跨源类已被数据源过滤剔除，不进作用域）。

        若跨源类 id 混进作用域 ⇒ Milvus 窗口里仍有跨源类 ⇒ 根因没修掉。
        """
        local = _cls(1, "SUPPLY_ACCESS", "供应商准入")
        ghost = _cls(2, "MAIN_DATA_SUPPLIER", "供应商主数据", src="main_data_supplier")
        ontology = _FakeOntologyService(
            [local], searchHits=[SimpleNamespace(id=1)],
        )
        service = _buildService(ontology=ontology)[0]

        await service._selectRelevantClasses(
            _FakeSession(), "供应商准入有哪些", [local],
            isDatasourceScoped=True,
        )

        assert ontology.searchCalls[0]["classOntologyIds"] == [1]
        assert 2 not in ontology.searchCalls[0]["classOntologyIds"]

    @pytest.mark.asyncio
    async def test_scope_ids_are_sorted_and_deduped(self) -> None:
        """作用域 id 集 = 候选集 id 集（**去重 + 升序** ⇒ 顺序稳定可复现）。

        不去重会让 expr 变长；不升序会让同一候选集在不同查询顺序下生成不同
        expr ⇒ 里程碑/缓存键不可复现。
        """
        seven = _cls(7, "C7")
        three = _cls(3, "C3")
        sevenAgain = _cls(7, "C7_ALT")
        ontology = _FakeOntologyService(
            [seven, three, sevenAgain], searchHits=[SimpleNamespace(id=3)],
        )
        service = _buildService(ontology=ontology)[0]

        await service._selectRelevantClasses(
            _FakeSession(), "查询", [seven, three, sevenAgain],
            isDatasourceScoped=True,
        )

        assert ontology.searchCalls[0]["classOntologyIds"] == [3, 7]

    @pytest.mark.asyncio
    async def test_scope_too_large_degrades_to_unlimited(self, caplog) -> None:
        """作用域超过上限 ⇒ **显式降级为不限** + 告警（而不是让 expr 炸掉）。

        expr 过长 ⇒ Milvus 解析异常 ⇒ 被 ``except Exception`` 吞成
        ``search_error`` ⇒ 静默退回宽窗（且只有一行 warning，没人看见）。
        ⇒ 超限时主动降级 + 显式告警，行为与今天一致但**可见**。
        """
        classes = [_cls(i, f"C{i}") for i in range(1, _CLASS_SCOPE_MAX_IDS + 2)]

        class _StrictOntology(_FakeOntologyService):
            """只认三个参数：降级后若仍多传 kwarg ⇒ TypeError ⇒ 宽窗回退。"""

            async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
                return [SimpleNamespace(id=1)]

        service = _buildService(ontology=_StrictOntology(classes))[0]

        with caplog.at_level(logging.WARNING, logger="app.services.chat_recall"):
            result, recall = await service._selectRelevantClasses(
                _FakeSession(), "查询", classes, isDatasourceScoped=True,
            )

        assert recall.mode == "recall"  # 没被吞成 search_error
        assert [c.id for c in result] == [1]
        assert "作用域过大" in caplog.text
        # 降级本身不是召回回退 ⇒ 不许污染回退率（reason= 行）聚合
        assert "本体类回退降级" not in caplog.text

    @pytest.mark.asyncio
    async def test_scoped_empty_window_retries_unscoped_search(self) -> None:
        """作用域窗口为空 ⇒ 再做一次**无作用域**检索（自愈 + 与今天同口径）。

        作用域窗口空有两种成因：(a) 本地类在 Milvus 里没有向量（embedding
        缺口）；(b) expr 过滤过严。无作用域重试把这两种都兜住：

        - 重试命中能解析出本地类 ⇒ 走正常裁剪（自愈）；
        - 重试命中解析不出本地类 ⇒ ``no_match`` ⇒ 锚定救援窄窗（与今天同一
          状态下的表现一致）；
        - 重试仍 0 命中 ⇒ ``no_hits`` ⇒ 宽窗回退（行为逐字不变）。

        ⚠️ 关键：**不能**直接回落宽窗 —— 宽窗把 43 个本地类全塞进 schema
        （召回裁剪被架空，正是本批要消灭的症状）。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")

        class _EmptyScopedOntology(_FakeOntologyService):
            """作用域检索恒空；无作用域检索也恒空（索引/嵌入异常）。"""

            async def searchByKeyword(
                self, query, *, topK=5, typeFilter=None, classOntologyIds=None
            ) -> list:
                self.searchCalls.append({
                    "query": query, "topK": topK, "typeFilter": typeFilter,
                    "classOntologyIds": classOntologyIds,
                })
                return []

        ontology = _EmptyScopedOntology([supplier])
        service = _buildService(ontology=ontology)[0]

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert len(ontology.searchCalls) == 2  # 作用域一次 + 无作用域一次
        assert ontology.searchCalls[0]["classOntologyIds"] == [1]
        assert ontology.searchCalls[1]["classOntologyIds"] is None
        # 连无作用域都 0 命中 ⇒ no_hits ⇒ 宽窗回退（Gap B 语义逐字不变）
        assert recall.mode == "fallback"
        assert [c.id for c in result] == [1]

    @pytest.mark.asyncio
    async def test_scoped_empty_window_rescues_by_anchor_after_retry(self) -> None:
        """作用域窗口空 + 无作用域重试只命中跨源类 ⇒ 锚定救援窄窗（同今天）。

        今天（无作用域）在这个状态下：全局命中跨源类 ⇒ no_match ⇒ 锚定救援
        ⇒ 窄窗。若作用域路径直接回落宽窗，就会**比今天更宽** ⇒ 行为回退。
        """
        supplier = _cls(1, "DIM_SUPPLIER", "供应商主数据")

        class _CrossSourceOnlyOntology(_FakeOntologyService):
            """作用域检索恒空；无作用域检索只命中跨源类（999 不在候选集里）。"""

            async def searchByKeyword(
                self, query, *, topK=5, typeFilter=None, classOntologyIds=None
            ) -> list:
                self.searchCalls.append({
                    "query": query, "topK": topK, "typeFilter": typeFilter,
                    "classOntologyIds": classOntologyIds,
                })
                if classOntologyIds is not None:
                    return []
                return [SimpleNamespace(id=999)]

        ontology = _CrossSourceOnlyOntology([supplier])
        service = _buildService(ontology=ontology)[0]

        result, recall = await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [supplier],
            isDatasourceScoped=True,
        )

        assert len(ontology.searchCalls) == 2
        assert recall.mode == "rescue"
        assert [c.id for c in result] == [1]  # 窄窗（锚定类），不是宽窗

    @pytest.mark.asyncio
    async def test_pinned_release_view_skips_class_id_scope(self) -> None:
        """钉住语义发布路径**不施加** id 作用域（快照 id 是合成 id，不是主键）。

        快照类的 id 由 ``semantic_schema_adapter._synthesizeIds`` 按类名排序
        合成（1..N），与 Milvus 的 ``ontology_id``（``ontology_class.id`` 自增
        主键）**不是同一 id 空间**。拿合成 id 去过滤真实主键会同时踩三个坑：

        (a) 真本地类的向量被挡在窗口外 ⇒ 作用域检索恒空；
        (b) 合成 id 恰好撞上别的真实主键 ⇒ 跨源/墓碑类的向量又进窗口
            （正是本批要消灭的症状）；
        (c) 命中 id（真实主键）与快照类 id（合成）错映射 ⇒ 错类归因。

        ⇒ 钉住路径传 ``None``（作用域不限）：与今天逐字节一致。该路径的 id
        空间错配是**既有缺陷**，另批根治（见 summary 遗留挂账）。
        """
        from app.services.semantic_recall_gate import (
            RecallClass,
            RecallOntology,
        )

        snapshotClass = RecallClass(
            id=1,  # 合成 id（按类名排序分配的 1..N）
            class_name="DIM_SUPPLIER", class_alias="供应商主数据",
            description=None, source_table="DIM_SUPPLIER",
            object_type=None, object_owner=None, parent_class_id=None,
        )
        view = RecallOntology(
            releaseId=7, version="v1", classes=[snapshotClass], joins=[],
        )
        ontology = _FakeOntologyService(
            [snapshotClass], searchHits=[SimpleNamespace(id=1)],
        )
        service = _buildService(ontology=ontology)[0]

        await service._selectRelevantClasses(
            _FakeSession(), "公司目前有多少供应商", [snapshotClass],
            view=view, isDatasourceScoped=True,
        )

        assert len(ontology.searchCalls) == 1
        assert ontology.searchCalls[0]["classOntologyIds"] is None  # 不施加作用域


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
