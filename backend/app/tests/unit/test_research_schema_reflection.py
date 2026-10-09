"""研究侧 schema 反射（`buildHypothesisSchemaHint`）单元测试。

2026-10-09 R1（schema 反射移植）：把 chat 侧 `OntologyService.listPropertiesByClass` +
`ValueSampler` 的真列 + 值域采样**搬运**到研究侧假设 prompt，**不**改 chat 任何文件。

本测试覆盖契约：
- 空 ESL / 空 BOs / 缺 classId / 缺 source_table → 占位符，不抛
- 真 BO + 真 schema → 输出含真表 + 真列 + PK/FK 标记
- ValueSampler 抛错 → 降级只返列名
- 完整 happy path → 列名 + 值域采样块

覆盖 conftest 的 autouse DB fixtures（unit 文件无需 DB）。
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.services.enterprise_semantic_layer import (
    BusinessObjectRef, ESLExtraction, MetricRef,
)


# ---------------------------------------------------------------------------
# 覆盖 conftest autouse DB fixtures —— 本文件纯 mock，无 DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def dbSession() -> Any:
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _makeEsl(bos: list[BusinessObjectRef] | None = None) -> ESLExtraction:
    return ESLExtraction(
        businessObjects=bos or [],
        metrics=[],
        knowledge=[],
        confidenceByArm={},
        conflicts=[],
    )


def _makeBo(classId: int = 1, className: str = "供应商",
            sourceTable: str = "DIM_SUPPLIER",
            matchedAlias: str = "供应商") -> BusinessObjectRef:
    return BusinessObjectRef(
        classId=classId, className=className,
        sourceTable=sourceTable, matchedAlias=matchedAlias, confidence=0.9,
    )


def _makeProp(name: str, *, source: str | None = None,
              pk: bool = False, fk: bool = False) -> Any:
    """构造一个最小 OntologyProperty 替身。"""
    p = MagicMock()
    p.property_name = name
    p.source_column = source if source is not None else name
    p.is_primary_key = pk
    p.is_foreign_key = fk
    return p


# ---------------------------------------------------------------------------
# Case 1: esl=None → 占位符
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_esl_None_返_占位符() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    result = await buildHypothesisSchemaHint(MagicMock(), uuid.uuid4(), None)
    assert "无可用 schema 反射" in result


# ---------------------------------------------------------------------------
# Case 2: esl.businessObjects=[] → 占位符
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_空_BOs_返_占位符() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    esl = _makeEsl(bos=[])
    result = await buildHypothesisSchemaHint(MagicMock(), uuid.uuid4(), esl)
    assert "无可用 schema 反射" in result


# ---------------------------------------------------------------------------
# Case 3: BO 缺 source_table → 降级为「- NAME（无 source_table）」
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_BO_无_source_table_降级() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    session = MagicMock()
    session.scalar = AsyncMock(return_value=1)

    ds = MagicMock()
    ds.id = 1
    ds.type = "oracle"
    ds.oracle_version = "19c"
    ds.username = "THBI"

    bo = _makeBo(classId=99, className="未知BO", sourceTable="")

    # OntologyService.getClass returns class with no source_table
    clsNoTable = MagicMock()
    clsNoTable.source_table = None

    with patch("app.services.research_schema_reflection.DataSourceService") as DS, \
         patch("app.services.research_schema_reflection.OntologyService") as OT, \
         patch("app.services.research_schema_reflection.ValueSampler") as VS, \
         patch("app.services.research_schema_reflection.get_adapter",
               return_value=MagicMock()), \
         patch("app.services.research_schema_reflection.Nl2SqlService") as NL:
        DS.return_value.get = AsyncMock(return_value=ds)
        OT.return_value.getClass = AsyncMock(return_value=clsNoTable)
        OT.return_value.listPropertiesByClass = AsyncMock(return_value=[])
        VS.return_value.sample = AsyncMock(return_value={})
        NL.resolveDialect.return_value.useSchemaPrefix = True

        esl = _makeEsl(bos=[bo])
        result = await buildHypothesisSchemaHint(session, uuid.uuid4(), esl)

    assert "未知BO" in result
    assert "无 source_table" in result
    # 不抛
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Case 4: 真 BO + 真 schema → 渲染真表 + 真列 + PK/FK 标记
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_真_BO_真_schema_渲染_PK_FK() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    session = MagicMock()
    session.scalar = AsyncMock(return_value=1)

    ds = MagicMock()
    ds.id = 1
    ds.type = "oracle"
    ds.oracle_version = "19c"
    ds.username = "THBI"

    bo = _makeBo(classId=2, className="收货单", sourceTable="DWD_GOODS_RECEIPT_DTL")

    cls = MagicMock()
    cls.id = 2
    cls.source_table = "DWD_GOODS_RECEIPT_DTL"

    props = [
        _makeProp("rcv_qty", pk=False, fk=False),
        _makeProp("supplier_id", pk=False, fk=True),
        _makeProp("receipt_id", pk=True, fk=False),
    ]

    with patch("app.services.research_schema_reflection.DataSourceService") as DS, \
         patch("app.services.research_schema_reflection.OntologyService") as OT, \
         patch("app.services.research_schema_reflection.ValueSampler") as VS, \
         patch("app.services.research_schema_reflection.get_adapter",
               return_value=MagicMock()), \
         patch("app.services.research_schema_reflection.Nl2SqlService") as NL:
        DS.return_value.get = AsyncMock(return_value=ds)
        OT.return_value.getClass = AsyncMock(return_value=cls)
        OT.return_value.listPropertiesByClass = AsyncMock(return_value=props)
        VS.return_value.sample = AsyncMock(return_value={})
        NL.resolveDialect.return_value.useSchemaPrefix = True

        esl = _makeEsl(bos=[bo])
        result = await buildHypothesisSchemaHint(session, uuid.uuid4(), esl)

    assert "DWD_GOODS_RECEIPT_DTL" in result
    assert "rcv_qty" in result
    assert "supplier_id" in result
    assert "receipt_id" in result
    # PK / FK 标记
    assert "[PK]" in result
    assert "[FK]" in result
    # 不要 chat 那种父类层级 —— 假设生成只关心列名
    assert "继承" not in result


# ---------------------------------------------------------------------------
# Case 5: ValueSampler.sample 抛错 → 整段不抛，返只含列名
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_采样_失败_降级_不抛() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    session = MagicMock()
    session.scalar = AsyncMock(return_value=1)

    ds = MagicMock()
    ds.id = 1
    ds.type = "oracle"
    ds.oracle_version = "19c"
    ds.username = "THBI"

    bo = _makeBo(classId=3, className="订单", sourceTable="DWD_PO")

    cls = MagicMock()
    cls.id = 3
    cls.source_table = "DWD_PO"
    props = [_makeProp("po_id", pk=True)]

    with patch("app.services.research_schema_reflection.DataSourceService") as DS, \
         patch("app.services.research_schema_reflection.OntologyService") as OT, \
         patch("app.services.research_schema_reflection.ValueSampler") as VS, \
         patch("app.services.research_schema_reflection.get_adapter",
               return_value=MagicMock()), \
         patch("app.services.research_schema_reflection.Nl2SqlService") as NL:
        DS.return_value.get = AsyncMock(return_value=ds)
        OT.return_value.getClass = AsyncMock(return_value=cls)
        OT.return_value.listPropertiesByClass = AsyncMock(return_value=props)
        VS.return_value.sample = AsyncMock(
            side_effect=RuntimeError("业务库炸了"),
        )
        NL.resolveDialect.return_value.useSchemaPrefix = True

        esl = _makeEsl(bos=[bo])
        # 不应抛
        result = await buildHypothesisSchemaHint(session, uuid.uuid4(), esl)

    # 列名还在，值域块没有
    assert "DWD_PO" in result
    assert "po_id" in result
    assert "值域采样" not in result


# ---------------------------------------------------------------------------
# Case 6: 完整 happy path → 列名 + 值域采样块
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_真_值域_采样_追加_块() -> None:
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    session = MagicMock()
    session.scalar = AsyncMock(return_value=1)

    ds = MagicMock()
    ds.id = 1
    ds.type = "oracle"
    ds.oracle_version = "19c"
    ds.username = "THBI"

    bo = _makeBo(classId=4, className="供应商", sourceTable="DIM_SUPPLIER")

    cls = MagicMock()
    cls.id = 4
    cls.source_table = "DIM_SUPPLIER"
    props = [_makeProp("supplier_code"), _makeProp("supplier_name")]

    samples = {
        ("DIM_SUPPLIER", "supplier_code"): ["S001", "S002", "S003"],
        ("DIM_SUPPLIER", "supplier_name"): ["ACME", "Globex"],
    }

    with patch("app.services.research_schema_reflection.DataSourceService") as DS, \
         patch("app.services.research_schema_reflection.OntologyService") as OT, \
         patch("app.services.research_schema_reflection.ValueSampler") as VS, \
         patch("app.services.research_schema_reflection.get_adapter",
               return_value=MagicMock()), \
         patch("app.services.research_schema_reflection.Nl2SqlService") as NL:
        DS.return_value.get = AsyncMock(return_value=ds)
        OT.return_value.getClass = AsyncMock(return_value=cls)
        OT.return_value.listPropertiesByClass = AsyncMock(return_value=props)
        VS.return_value.sample = AsyncMock(return_value=samples)
        NL.resolveDialect.return_value.useSchemaPrefix = True

        esl = _makeEsl(bos=[bo])
        result = await buildHypothesisSchemaHint(session, uuid.uuid4(), esl)

    # 列名块
    assert "DIM_SUPPLIER" in result
    assert "supplier_code" in result
    # 值域采样块
    assert "值域采样" in result
    assert "S001" in result
    assert "ACME" in result
    # 不超 valueSampleMax（默认 5）
    assert result.count(" ∈ {") <= 5


# ---------------------------------------------------------------------------
# Case 7: 生产路径 esl 是 dict（_stageEsl 里 asdict 出来的）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buildHypothesisSchemaHint_esl_为_dict_形态_亦_支持() -> None:
    """生产路径：`state["esl"]` 是 `_stageEsl` 里 `asdict(extraction)` 的 dict，
    不是 ESLExtraction 对象。本用例覆盖 duck-type normalize 路径。
    """
    from app.services.research_schema_reflection import buildHypothesisSchemaHint

    session = MagicMock()
    session.scalar = AsyncMock(return_value=1)

    ds = MagicMock()
    ds.id = 1
    ds.type = "oracle"
    ds.oracle_version = "19c"
    ds.username = "THBI"

    # dict 形态（生产路径）
    eslDict = {
        "businessObjects": [
            {"classId": 5, "className": "到货", "sourceTable": "DWD_ARRIVAL",
             "matchedAlias": "到货", "confidence": 0.9},
        ],
        "metrics": [],
        "knowledge": [],
        "confidenceByArm": {},
        "conflicts": [],
    }

    cls = MagicMock()
    cls.id = 5
    cls.source_table = "DWD_ARRIVAL"
    props = [_makeProp("arrival_dt"), _makeProp("arrival_qty", pk=True)]

    with patch("app.services.research_schema_reflection.DataSourceService") as DS, \
         patch("app.services.research_schema_reflection.OntologyService") as OT, \
         patch("app.services.research_schema_reflection.ValueSampler") as VS, \
         patch("app.services.research_schema_reflection.get_adapter",
               return_value=MagicMock()), \
         patch("app.services.research_schema_reflection.Nl2SqlService") as NL:
        DS.return_value.get = AsyncMock(return_value=ds)
        OT.return_value.getClass = AsyncMock(return_value=cls)
        OT.return_value.listPropertiesByClass = AsyncMock(return_value=props)
        VS.return_value.sample = AsyncMock(return_value={})
        NL.resolveDialect.return_value.useSchemaPrefix = True

        result = await buildHypothesisSchemaHint(session, uuid.uuid4(), eslDict)

    assert "DWD_ARRIVAL" in result
    assert "arrival_dt" in result
    assert "arrival_qty" in result
    assert "[PK]" in result


# ---------------------------------------------------------------------------
# Case 8: `buildHypothesisMessages` 的 schemaHint 透传契约
# ---------------------------------------------------------------------------


def test_buildHypothesisMessages_空_schemaHint_不_追加_块() -> None:
    """空 schemaHint → 旧行为，user prompt 不含 schema 块（chat 侧兼容）。"""
    from app.services.hypothesis_service import buildHypothesisMessages

    msgs = buildHypothesisMessages("分步分析收货下降", "step1: x", ["供应商", "K1"])
    userContent = msgs[1].content
    assert "分步分析收货下降" in userContent
    assert "供应商" in userContent
    assert "参考 schema" not in userContent


def test_buildHypothesisMessages_非空_schemaHint_追加_到_user_prompt() -> None:
    """非空 schemaHint → 追加 schema 块到 user prompt，**不**污染 system 段。"""
    from app.services.hypothesis_service import buildHypothesisMessages

    schemaHint = "可用 schema：\n- DWD_PO: po_id [PK], po_qty"
    msgs = buildHypothesisMessages(
        "分步分析", "step1: x", ["供应商"], schemaHint=schemaHint,
    )
    systemContent = msgs[0].content
    userContent = msgs[1].content
    # system 段不动（R1 约束：只在 user prompt 追加）
    assert "DWD_PO" not in systemContent
    # user 段含 schema
    assert "DWD_PO" in userContent
    assert "参考 schema（直接用真表真列，别瞎猜）" in userContent
    assert schemaHint in userContent


# ---------------------------------------------------------------------------
# Case 9: `generateHypotheses` 透传 schemaHint 到 LLM 消息
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generateHypotheses_透传_schemaHint() -> None:
    """`generateHypotheses` 必须把 schemaHint 传给 `buildHypothesisMessages`，
    否则 R1 整条数据通路断。"""
    from app.services import research_hypothesis_adapter as adapter

    captured: dict[str, Any] = {}

    def fakeBuild(question, dataSummary, drivers, *, schemaHint=""):
        captured["schemaHint"] = schemaHint
        # 返合法 JSON 让 parseHypotheses 不抛
        from app.infrastructure.llm.base_client import LlmMessage
        return [
            LlmMessage(role="system", content="sys"),
            LlmMessage(role="user", content="usr"),
        ]

    class _StubClient:
        async def complete(self, messages):
            from app.infrastructure.llm.base_client import LlmResponse
            return LlmResponse(content='[{"statement": "x", "driver": null, '
                                       '"verification_sql": "SELECT 1 FROM dual"}]',
                               promptTokens=10, completionTokens=15,
                               modelName="m", totalTokens=25)

    with patch.object(adapter, "buildHypothesisMessages", side_effect=fakeBuild):
        result = await adapter.generateHypotheses(
            _StubClient(), "q", "summary", ["d1"],
            schemaHint="可用 schema：\n- T: c1",
        )

    assert captured.get("schemaHint") == "可用 schema：\n- T: c1"
    # 真返回了一条 hypothesis
    assert len(result) == 1
    assert result[0].statement == "x"
