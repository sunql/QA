"""P2 D16：运行记录**钉住**语义发布（`semantic_release_id`）。

D15 把「schema 文本改读已发布快照」接进了主路径，但**每一轮都重新解析当前
ACTIVE 快照**。后果：发布一上线，正在进行中的会话**下一轮的口径就变了**
（同一句话，前后两轮看到的 schema 文本可能不是同一版）⇒
「同一请求重放」不再给出同一结果。

D16 的做法：会话状态里存下**本轮实际用的发布 id**（可空列
`session_query_state.semantic_release_id`），下一轮把它**钉住** ⇒
重放读到的还是那一版。

本文件钉死四件事：

1. **钉住优先于当前 ACTIVE**：给了发布 id ⇒ 按主键取那一版，**不去问 ACTIVE**；
2. **重放一致**：钉住 A 时渲染出的文本，与「A 还是 ACTIVE 时」的文本**逐字节相同**；
3. **空值兼容旧数据**：没钉住（NULL）⇒ 照旧取当前 ACTIVE，行为与今天一致；
4. **不许炸问答**：钉住的那一版已被删除 / 取不到 ⇒ 降级直读治理表，不抛异常，
   且**不回报发布 id**（没真用上就不能记账）。

放在 `unit/`：本文件**不写业务数据、不依赖库内数据** ⇒ 断言与库内容无关。
（但仍需 `TEST_DATABASE_URL`：`app/tests/unit/conftest.py` 有 autouse 的
`warmBusinessObjectRegistry(dbSession)`，每个 unit 用例都要真实 PG + 会清库。）

评审补测（D16 第二轮）：钉住路径的守卫口径、钉住失效的显式信号、回灌重试的口径、
落点守卫收紧、迁移的离线 SQL 往返。
"""

from __future__ import annotations

import ast
import importlib.util
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import SessionQueryState
from app.services import semantic_schema_adapter as adapterModule
from app.services import semantic_schema_gate as gate
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_compiler import compileFromRows

SUPPLIER_ID = 1
ORDER_ID = 2

_RELEASE_A = 41
_RELEASE_B = 42


# ---------------------------------------------------------------------------
# 数据构造（真实 ORM 实体，不落库）
# ---------------------------------------------------------------------------


def _prop(propId: int, classId: int, name: str, **kwargs):
    from app.domain.models import OntologyProperty

    return OntologyProperty(
        id=propId,
        class_id=classId,
        property_name=name,
        property_alias=None,
        business_aliases=None,
        description=None,
        data_type=kwargs.get("dataType", "STRING"),
        is_primary_key=kwargs.get("isPk", False),
        is_foreign_key=kwargs.get("isFk", False),
        ref_class_id=kwargs.get("refClassId"),
        source_column=kwargs.get("sourceColumn", name),
        unit=None,
        allowed_values=None,
        is_not_null=None,
        min_value=None,
        max_value=None,
        regex_pattern=None,
    )


def _cls(classId: int, name: str, *, sourceTable: str, properties: tuple = ()):
    from app.domain.models import OntologyClass

    cls = OntologyClass(
        id=classId,
        class_name=name,
        class_alias=None,
        description=None,
        source_table=sourceTable,
        object_type=None,
        object_owner=None,
        parent_class_id=None,
        valid_to=None,
    )
    cls.properties = list(properties)
    return cls


def _dataset(*, extraSupplierProp: bool = False):
    """一套最小本体。

    `extraSupplierProp=True` 造出「发布之后本体又加了一列」的形态 ——
    那一版快照与直读治理表**事实不一致** ⇒ 守卫必须拒绝它（降级直读）。
    """
    supplierProps = [
        # 物理行序刻意不按名字排：快照侧按 propertyName 排序 ⇒ 两侧列序不同、
        # 内容相同（与生产同形）。
        _prop(101, SUPPLIER_ID, "SUPPLIER_NAME"),
        _prop(102, SUPPLIER_ID, "SUPPLIER_CODE", isPk=True),
    ]
    if extraSupplierProp:
        supplierProps.append(_prop(103, SUPPLIER_ID, "EXTRA_COLUMN"))
    orderProps = [
        _prop(201, ORDER_ID, "SUPPLIER_CODE", isFk=True, refClassId=SUPPLIER_ID),
        _prop(202, ORDER_ID, "ORDER_CODE", isPk=True),
    ]
    classes = [
        _cls(SUPPLIER_ID, "Supplier", sourceTable="SUPPLIER", properties=tuple(supplierProps)),
        _cls(ORDER_ID, "PurchaseOrder", sourceTable="PURCHASE_ORDER", properties=tuple(orderProps)),
    ]
    properties = [prop for cls in classes for prop in cls.properties]
    joins = [
        type(
            "Join",
            (),
            {
                "id": 1,
                "source_class_id": ORDER_ID,
                "source_columns": ["SUPPLIER_CODE"],
                "target_class_id": SUPPLIER_ID,
                "target_columns": ["SUPPLIER_CODE"],
                "join_type": "LEFT",
                "relation_type": "foreign_key",
                "description": "订单挂供方",
                "join_key": "PURCHASE_ORDER_SUPPLIER",
            },
        )()
    ]
    return classes, properties, joins


def _manifest(classes, properties, joins) -> dict:
    return compileFromRows(classes=classes, properties=properties, joins=joins)


class _FakeSnapshot:
    """已发布快照的运行期视图（字段名对齐 `SemanticReleaseSnapshot`）。"""

    def __init__(self, manifest: dict, version: str, releaseId: int) -> None:
        self.id = releaseId
        self.version = version
        self.schema_version = manifest.get("schemaVersion")
        self.manifest = manifest


class _FakeRegistry:
    """只读 Registry 替身：同时支持 `getActive` 与 `getById`（钉住路径）。"""

    def __init__(self, *, active=None, byId=None) -> None:
        self._active = active
        self._byId = dict(byId or {})
        self.activeCalls: list[str | None] = []
        self.byIdCalls: list[int] = []

    async def getActive(self, session, *, domainCode: str | None = None):
        self.activeCalls.append(domainCode)
        return self._active

    async def getById(self, session, releaseId: int):
        self.byIdCalls.append(releaseId)
        # 与真实 Registry 同款：id 越界抛 ValidationError（不是 404）
        if releaseId < 1:
            raise ValidationError(f"语义发布 id 非法：{releaseId}")
        snapshot = self._byId.get(releaseId)
        if snapshot is None:
            raise NotFoundError("语义发布不存在")
        return snapshot


@pytest.fixture
def switchOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate, "getSettings",
        lambda: SimpleNamespace(enableSemanticRegistryNl2sql=True),
    )


@pytest.fixture
def switchOff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate, "getSettings",
        lambda: SimpleNamespace(enableSemanticRegistryNl2sql=False),
    )


def _installRegistry(monkeypatch: pytest.MonkeyPatch, registry: _FakeRegistry) -> _FakeRegistry:
    monkeypatch.setattr(adapterModule, "semanticRegistry", registry)
    return registry


def _columnOrder(text: str, className: str = "Supplier") -> list[str]:
    order: list[str] = []
    inBlock = False
    for line in text.splitlines():
        if line.startswith("### "):
            inBlock = line.startswith(f"### {className}")
            continue
        if inBlock and line.strip() and line.strip() != "Columns:":
            order.append(line.strip().split(":")[0].strip())
    return order


# ---------------------------------------------------------------------------
# 1. 钉住优先于当前 ACTIVE
# ---------------------------------------------------------------------------


class Test钉住语义发布:
    async def test_钉住时按主键取那一版且不问ACTIVE(
        self, switchOn, monkeypatch
    ) -> None:
        """钉住 A ⇒ 取 A；**不去问当前 ACTIVE**（问了就可能被偷偷换掉）。"""
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        snapshotB = _FakeSnapshot(
            _manifest(*_dataset(extraSupplierProp=True)), "v-b", _RELEASE_B
        )
        registry = _installRegistry(
            monkeypatch, _FakeRegistry(active=snapshotB, byId={_RELEASE_A: snapshotA})
        )

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object(), pinnedReleaseId=_RELEASE_A
        )

        assert registry.byIdCalls == [_RELEASE_A]
        assert registry.activeCalls == []
        assert result.semanticReleaseId == _RELEASE_A

    async def test_未钉住时取当前ACTIVE并回报它的发布id(
        self, switchOn, monkeypatch
    ) -> None:
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(monkeypatch, _FakeRegistry(active=snapshotA))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object()
        )

        assert registry.activeCalls == [None]
        assert registry.byIdCalls == []
        assert result.semanticReleaseId == _RELEASE_A

    async def test_重放读同一发布则文本逐字节相同(
        self, switchOn, monkeypatch
    ) -> None:
        """**核心判据**：A 还是 ACTIVE 时渲染的文本，与「B 已上位、但本轮钉住 A」
        时渲染的文本，必须**逐字节相同** ⇒ 同一请求重放 ⇒ 同一发布 ⇒ 同一口径。"""
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        snapshotB = _FakeSnapshot(
            _manifest(*_dataset(extraSupplierProp=True)), "v-b", _RELEASE_B
        )
        registry = _installRegistry(
            monkeypatch,
            _FakeRegistry(active=snapshotA, byId={_RELEASE_A: snapshotA}),
        )

        first = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object()
        )
        # B 上位（且 B 与当前本体事实不一致 ⇒ 用 B 会被守卫拒绝）
        registry._active = snapshotB
        replay = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object(), pinnedReleaseId=_RELEASE_A
        )

        assert replay.text == first.text
        assert replay.semanticReleaseId == _RELEASE_A
        # 对照组：真去用 B ⇒ 守卫拒绝 ⇒ 文本回到直读口径（与钉住 A 不同）
        withB = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object()
        )
        assert withB.text != first.text
        assert withB.semanticReleaseId is None

    async def test_钉住的发布已被删除时降级直读不炸问答(
        self, switchOn, monkeypatch
    ) -> None:
        """钉住的那一版被删了 ⇒ 取不到 ⇒ 降级直读，**不许抛异常**（P0 裁决一）。"""
        classes, properties, joins = _dataset()
        registry = _installRegistry(monkeypatch, _FakeRegistry(active=None))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object(), pinnedReleaseId=999
        )

        assert result.text == buildSchemaText(classes, joins=joins)
        assert result.semanticReleaseId is None
        assert registry.byIdCalls == [999]

    async def test_快照事实不一致降级时不回报发布id(
        self, switchOn, monkeypatch
    ) -> None:
        """没真用上那一版 ⇒ 不能记账（记账等于声称「本轮用的是它」）。"""
        classes, properties, joins = _dataset()
        snapshotB = _FakeSnapshot(
            _manifest(*_dataset(extraSupplierProp=True)), "v-b", _RELEASE_B
        )
        _installRegistry(monkeypatch, _FakeRegistry(active=snapshotB))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object()
        )

        assert result.text == buildSchemaText(classes, joins=joins)
        assert result.semanticReleaseId is None

    async def test_会话缺席时确定地直读且不回报发布id(
        self, switchOn, monkeypatch
    ) -> None:
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(monkeypatch, _FakeRegistry(active=snapshotA))

        result = await gate.renderSchemaWithRelease(classes, joins=joins)

        assert result.text == buildSchemaText(classes, joins=joins)
        assert result.semanticReleaseId is None
        assert registry.activeCalls == []
        assert registry.byIdCalls == []

    async def test_开关关闭时不查发布也不回报发布id(
        self, switchOff, monkeypatch
    ) -> None:
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(monkeypatch, _FakeRegistry(active=snapshotA))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object(), pinnedReleaseId=_RELEASE_A
        )

        assert result.text == buildSchemaText(classes, joins=joins)
        assert result.semanticReleaseId is None
        assert registry.activeCalls == []
        assert registry.byIdCalls == []


# ---------------------------------------------------------------------------
# 2. 向后兼容：既有出口仍返回字符串
# ---------------------------------------------------------------------------


class Test渲染出口契约:
    async def test_renderSchemaText仍返回字符串(self, switchOn, monkeypatch) -> None:
        """既有 3 个接线点用的就是这个出口 ⇒ 不能改成返回元组。"""
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        _installRegistry(monkeypatch, _FakeRegistry(active=snapshotA))

        text = await gate.renderSchemaText(classes, joins=joins, session=object())

        assert isinstance(text, str)
        assert _columnOrder(text) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]

    async def test_renderSchemaText支持钉住(self, switchOn, monkeypatch) -> None:
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(
            monkeypatch, _FakeRegistry(active=None, byId={_RELEASE_A: snapshotA})
        )

        text = await gate.renderSchemaText(
            classes, joins=joins, session=object(), pinnedReleaseId=_RELEASE_A
        )

        assert registry.byIdCalls == [_RELEASE_A]
        assert _columnOrder(text) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]


# ---------------------------------------------------------------------------
# 3. 运行记录（ORM + 迁移）形态
# ---------------------------------------------------------------------------


class Test运行记录形态:
    def test_会话状态有可空的发布id列(self) -> None:
        column = SessionQueryState.__table__.columns.get("semantic_release_id")
        assert column is not None, "session_query_state 必须有 semantic_release_id 列"
        assert column.nullable is True, "发布 id 必须可空（兼容存量旧数据）"
        assert isinstance(column.type, Integer)

    def test_迁移只加可空列且可回退(self) -> None:
        """迁移形态守卫：手写、只加法 DDL、downgrade 对称。"""
        module = _loadMigration("0144_session_query_state_semantic_release_id")
        assert module.revision == "0144"
        assert module.down_revision == "0143"

        upgradeCalls = _recordedOps(module, "upgrade")
        assert upgradeCalls == [
            ("add_column", "session_query_state", "semantic_release_id"),
        ], f"upgrade 只允许加这一列，实际：{upgradeCalls}"

        downgradeCalls = _recordedOps(module, "downgrade")
        assert downgradeCalls == [
            ("drop_column", "session_query_state", "semantic_release_id"),
        ], f"downgrade 只允许删这一列，实际：{downgradeCalls}"


# ---------------------------------------------------------------------------
# 4. 运行期接线：结果对象 / 两阶段 / 会话状态
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str, promptTokens: int = 10, completionTokens: int = 5) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.isApproximateUsage = False


class _ScriptedLlm:
    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append({"messages": messages, **kwargs})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _llmConfig():
    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _planJson() -> str:
    import json

    return json.dumps(
        {
            "target": "各供应商",
            "selectedClasses": ["Supplier"],
            "selectedProperties": ["SUPPLIER_CODE"],
            "aggregations": [],
            "groupBy": [],
        },
        ensure_ascii=False,
    )


class Test运行期接线:
    """结果对象与两阶段编排必须把「本轮用的哪一版」一路带到底。"""

    def test_结果对象带发布id且默认空(self) -> None:
        from app.domain.query_plan import PlanResult
        from app.services.nl2sql_service import SqlResult

        assert SqlResult(sql="SELECT 1", promptTokens=1, completionTokens=2).semanticReleaseId is None
        plan = PlanResult(plan=SimpleNamespace(), promptTokens=1, completionTokens=2)
        assert plan.semanticReleaseId is None

    async def test_generateSql钉住时用那一版并回报它(self, switchOn, monkeypatch) -> None:
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(
            monkeypatch, _FakeRegistry(active=None, byId={_RELEASE_A: snapshotA})
        )
        fake = _ScriptedLlm([_Resp("```sql\nSELECT SUPPLIER_CODE FROM SUPPLIER\n```")])

        result = await Nl2SqlService().generateSql(
            "问题", classes, fake, _llmConfig(), joins=joins, session=object(),
            pinnedReleaseId=_RELEASE_A,
        )

        assert registry.byIdCalls == [_RELEASE_A]
        assert result.semanticReleaseId == _RELEASE_A

    async def test_generateSql直接读时不查发布也不回报id(self, switchOn, monkeypatch) -> None:
        """`directOnly` = 上层已判定这一轮不走快照 ⇒ 连发布都不查。"""
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(monkeypatch, _FakeRegistry(active=snapshotA))
        fake = _ScriptedLlm([_Resp("```sql\nSELECT SUPPLIER_CODE FROM SUPPLIER\n```")])

        result = await Nl2SqlService().generateSql(
            "问题", classes, fake, _llmConfig(), joins=joins, session=object(),
            directOnly=True,
        )

        assert registry.activeCalls == []
        assert registry.byIdCalls == []
        assert result.semanticReleaseId is None

    async def test_计划阶段钉住时回报该发布id(self, switchOn, monkeypatch) -> None:
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(
            monkeypatch, _FakeRegistry(active=None, byId={_RELEASE_A: snapshotA})
        )
        fake = _ScriptedLlm([_Resp(_planJson())])

        planResult = await Nl2SqlService().generateQueryPlan(
            "问题", classes, fake, _llmConfig(), joins=joins, session=object(),
            pinnedReleaseId=_RELEASE_A,
        )

        assert registry.byIdCalls == [_RELEASE_A]
        assert planResult.semanticReleaseId == _RELEASE_A

    async def test_编排层把钉住透传给计划阶段(self, switchOn, monkeypatch) -> None:
        """`generateValidatedPlan` 是计划阶段的唯一编排入口 ⇒ 钉住必须透传。"""
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        registry = _installRegistry(
            monkeypatch, _FakeRegistry(active=None, byId={_RELEASE_A: snapshotA})
        )
        fake = _ScriptedLlm([_Resp(_planJson())])

        planResult = await Nl2SqlService().generateValidatedPlan(
            "问题", classes, fake, _llmConfig(), joins=joins, session=object(),
            pinnedReleaseId=_RELEASE_A,
        )

        assert registry.byIdCalls == [_RELEASE_A]
        assert planResult.semanticReleaseId == _RELEASE_A


class _FakeNl2Sql:
    """`_twoStageGenerate` 的 NL2SQL 替身：记录两阶段各自收到的参数。

    `planPinInvalidated` / `sqlPinInvalidated` 模拟**闸门**的「钉住失效」信号：
    真实链路里这个信号由 `SchemaRenderResult.pinInvalidated` 一路带下来
    （闸门 → PlanResult/SqlResult → _SqlOutcome）。替身必须能**分别**表达
    「没尝试钉住」（False）与「尝试了但没用上」（True），否则编排层那条
    「pinned is not None and id is None ⇒ 失效」的推导式就无法被测出来。
    """

    def __init__(
        self,
        *,
        planReleaseId: int | None,
        sqlReleaseId: int | None = None,
        planPinInvalidated: bool = False,
        sqlPinInvalidated: bool = False,
        planUnanswerable: bool = False,
        sqlReleaseIds: list[int | None] | None = None,
        sqlPinInvalidatedSeq: list[bool] | None = None,
    ) -> None:
        self._planReleaseId = planReleaseId
        self._sqlReleaseId = sqlReleaseId
        self._planPinInvalidated = planPinInvalidated
        self._sqlPinInvalidated = sqlPinInvalidated
        self._planUnanswerable = planUnanswerable
        # 「每次调用换一版」的序列：模拟「首次生成用 A 版、回灌重试时 A 版已被删
        # ⇒ 重试降级直读」这类**同轮内换版**的场景。
        self._sqlReleaseIds = list(sqlReleaseIds) if sqlReleaseIds is not None else None
        self._sqlPinInvalidatedSeq = (
            list(sqlPinInvalidatedSeq) if sqlPinInvalidatedSeq is not None else None
        )
        self.planCalls: list[dict] = []
        self.sqlCalls: list[dict] = []

    async def generateValidatedPlan(self, *args, **kwargs):
        self.planCalls.append(kwargs)
        return SimpleNamespace(
            plan=SimpleNamespace(isUnanswerable=self._planUnanswerable),
            promptTokens=1,
            completionTokens=2,
            cachedTokens=None,
            semanticReleaseId=self._planReleaseId,
            pinInvalidated=self._planPinInvalidated,
        )

    async def generateSql(self, *args, **kwargs):
        self.sqlCalls.append(kwargs)
        releaseId = self._sqlReleaseId
        if self._sqlReleaseIds is not None:
            idx = len(self.sqlCalls) - 1
            releaseId = self._sqlReleaseIds[idx] if idx < len(self._sqlReleaseIds) else None
        pinInvalidated = self._sqlPinInvalidated
        if self._sqlPinInvalidatedSeq is not None:
            idx = len(self.sqlCalls) - 1
            pinInvalidated = (
                self._sqlPinInvalidatedSeq[idx]
                if idx < len(self._sqlPinInvalidatedSeq)
                else False
            )
        return SimpleNamespace(
            sql="SELECT 1", promptTokens=1, completionTokens=2, cachedTokens=None,
            semanticReleaseId=releaseId,
            pinInvalidated=pinInvalidated,
        )


class _ChatStub:
    """`_planAndGenerateSql` / `_twoStageGenerate` 的最小宿主。"""

    def __init__(self, nl2sql: _FakeNl2Sql) -> None:
        self._nl2sql = nl2sql
        self.twoStageCalls: list[dict] = []

    def _llmFactory(self, cfg):
        return SimpleNamespace(complete=None)

    async def _getStateHistoryFieldLimit(self, session) -> int:
        return 5

    async def _isWikiInjectionEnabled(self, session) -> bool:
        return False

    async def _collectKpiContractBlock(self, session, question) -> str:
        return ""

    async def _callWithFallback(self, session, sessionId, configs, selected, purpose, fn, forced=None):
        return await fn(selected), selected, (0, 0)

    async def _recordUsage(self, *args, **kwargs) -> None:
        return None

    async def _twoStageGenerate(self, *args, **kwargs):
        self.twoStageCalls.append(kwargs)
        from app.services.chat_service import ChatService

        return await ChatService._twoStageGenerate(self, *args, **kwargs)


def _pc():
    return SimpleNamespace(
        classes=[],
        joins=[],
        configs=[],
        selected=SimpleNamespace(model_name="test-model", id=1),
        ds=SimpleNamespace(type="postgresql", oracle_version=None, username=None),
        contextPrompt=None,
        fewShot=None,
        valueSamples=None,
        driftWarning=None,
        dictionaryText=None,
        featureCatalogText=None,
        forcedModel=None,
    )


class Test两阶段口径一致:
    """一轮之内，schema 文本要么**两阶段都走同一版快照**，要么**两阶段都直读**。"""

    async def test_两阶段都走快照时SQL阶段钉住计划阶段那一版(self) -> None:
        stub = _ChatStub(_FakeNl2Sql(planReleaseId=_RELEASE_A))

        await stub._twoStageGenerate(
            "问题", [], None, None,
            datasourceType="postgresql", oracle_version=None, schemaPrefix=None,
            context=None, statePrompt=None,
        )

        sqlCall = stub._nl2sql.sqlCalls[0]
        assert sqlCall["pinnedReleaseId"] == _RELEASE_A
        assert sqlCall.get("directOnly") is not True

    async def test_计划阶段降级时SQL阶段也直读(self) -> None:
        """计划阶段没拿到发布（降级 / 开关关）⇒ SQL 阶段**不许**自己去取 ACTIVE：
        否则两阶段一个快照一个直读 ⇒ 同一轮里 prompt 口径不一致。"""
        stub = _ChatStub(_FakeNl2Sql(planReleaseId=None))

        await stub._twoStageGenerate(
            "问题", [], None, None,
            datasourceType="postgresql", oracle_version=None, schemaPrefix=None,
            context=None, statePrompt=None,
        )

        sqlCall = stub._nl2sql.sqlCalls[0]
        assert sqlCall.get("directOnly") is True

    async def test_会话状态里的发布id被当作钉住传给两阶段(self) -> None:
        """**运行记录 → 钉住** 的那一段：上一轮记下的发布 id，
        下一轮必须原样传给两阶段（否则「钉住」只是存了个数没人用）。"""
        from app.domain.enums import IntentType
        from app.services.chat_service import ChatService

        stub = _ChatStub(_FakeNl2Sql(planReleaseId=_RELEASE_A))
        dto = SimpleNamespace(question="问题", sessionId="s-1")
        state = SimpleNamespace(semantic_release_id=_RELEASE_A, last_sql=None)

        await ChatService._planAndGenerateSql(
            stub, object(), dto, _pc(), IntentType.NEW_QUERY, state,
        )

        assert stub.twoStageCalls[0]["pinnedReleaseId"] == _RELEASE_A

    async def test_会话状态没有发布id时不钉住(self) -> None:
        from app.domain.enums import IntentType
        from app.services.chat_service import ChatService

        stub = _ChatStub(_FakeNl2Sql(planReleaseId=None))
        dto = SimpleNamespace(question="问题", sessionId="s-1")
        state = SimpleNamespace(semantic_release_id=None, last_sql=None)

        await ChatService._planAndGenerateSql(
            stub, object(), dto, _pc(), IntentType.NEW_QUERY, state,
        )

        assert stub.twoStageCalls[0]["pinnedReleaseId"] is None


class Test钉住失效信号的传递:
    """「钉住失效」必须**由闸门说了算**，编排层不许自己猜。

    闸门是唯一知道「这一轮到底有没有尝试钉住」的地方：
    - 开关关 / 会话缺席 / 上层要求直读 ⇒ **没尝试**（pinInvalidated=False）；
    - 钉住的那一版取不到 / 陈旧 / 版本不符 ⇒ **尝试了但没用上**（True）。

    编排层若改用「有钉住 id 且结果 id 为空 ⇒ 失效」这种**近似推导**，
    就会在「开关关」时把既有钉住误判成失效 ⇒ 运维关一次开关 ⇒
    所有会话的钉住被静默抹掉（kill switch 变成了「清库开关」）。
    """

    async def test_闸门没尝试钉住时编排层不判钉住失效(self) -> None:
        """kill switch 关掉的情形：闸门返回 direct 且 pinInvalidated=False。

        期望：outcome.pinInvalidated is False ⇒ 运行记录**保留**既有钉住。
        """
        from app.domain.enums import IntentType
        from app.services.chat_service import ChatService

        stub = _ChatStub(
            _FakeNl2Sql(
                planReleaseId=None,
                sqlReleaseId=None,
                planPinInvalidated=False,
                sqlPinInvalidated=False,
            )
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1")
        state = SimpleNamespace(semantic_release_id=_RELEASE_A, last_sql=None)

        outcome = await ChatService._planAndGenerateSql(
            stub, object(), dto, _pc(), IntentType.NEW_QUERY, state,
        )

        assert outcome.semanticReleaseId is None
        assert outcome.pinInvalidated is False, (
            "闸门说「没尝试钉住」⇒ 不许判成钉住失效"
            "（否则 kill switch 关一次就把所有会话的钉住抹掉）"
        )

    async def test_闸门说钉住失效时编排层如实上报(self) -> None:
        """钉住的那一版被删 ⇒ 闸门 pinInvalidated=True ⇒ 编排层必须如实上报。

        这条与上一条成对：**同形输入、不同信号 ⇒ 不同结论** ⇒ 断言有牙。
        """
        from app.domain.enums import IntentType
        from app.services.chat_service import ChatService

        stub = _ChatStub(
            _FakeNl2Sql(
                planReleaseId=None,
                sqlReleaseId=None,
                planPinInvalidated=True,
                sqlPinInvalidated=False,
            )
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1")
        state = SimpleNamespace(semantic_release_id=_RELEASE_A, last_sql=None)

        outcome = await ChatService._planAndGenerateSql(
            stub, object(), dto, _pc(), IntentType.NEW_QUERY, state,
        )

        assert outcome.pinInvalidated is True, (
            "闸门报了「钉住失效」⇒ 编排层必须如实上报 ⇒ 运行记录清空那一列（死钉自愈）"
        )

    async def test_计划阶段失效时sql阶段直读也算失效(self) -> None:
        """计划阶段钉住失败（降级直读）⇒ SQL 阶段随之直读 ⇒ 整轮都算「钉住失效」。

        防止「计划阶段报了失效、SQL 阶段没报 ⇒ 取与 ⇒ 失效信号被吃掉」。
        """
        from app.domain.enums import IntentType
        from app.services.chat_service import ChatService

        stub = _ChatStub(
            _FakeNl2Sql(
                planReleaseId=None,
                sqlReleaseId=None,
                planPinInvalidated=True,
                sqlPinInvalidated=False,
            )
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1")
        state = SimpleNamespace(semantic_release_id=_RELEASE_A, last_sql=None)

        outcome = await ChatService._planAndGenerateSql(
            stub, object(), dto, _pc(), IntentType.NEW_QUERY, state,
        )

        assert outcome.pinInvalidated is True


# ---------------------------------------------------------------------------
# 运行记录落库口径（多步 / 流式 / 非流式）
# ---------------------------------------------------------------------------

# 「本轮跑完了、有 schema 文本」⇒ 必须把本轮用的发布 id 记进运行记录的落点。
# 每一项是 (模块名, 函数名)。这些函数里**至少有一处** `_saveQueryState` 调用
# 必须带 `semanticReleaseId` —— 否则这一条链路的会话永远钉不住（下一轮照样漂）。
#
# 为什么不逐个调用点断言「全都得带」：有的调用点是**故意不带**的
# （不可回答 / 特征直返 / 超限拒收 / 断连兜底 —— 这些路径本轮没渲染 schema，
# 没有发布 id 可记 ⇒ None ⇒ 保留既有钉住）。逐个断言会把「故意不带」也钉死成
# 「必须带」⇒ 逼着这些路径伪造一个 id ⇒ **记了没用过的版本**（更坏）。
_MUST_RECORD_RELEASE_ID = [
    ("app/services/chat_service.py", "_handleGenericQuery"),
    ("app/services/chat_service.py", "_tryFeatureResponse"),
    ("app/services/chat_service.py", "_unanswerableResponse"),
    ("app/services/chat_stream_query.py", "_streamQuery"),
    ("app/services/chat_multistep.py", "_executeMultiStep"),
    ("app/services/chat_multistep.py", "_finalizeMultiStepDegrade"),
    ("app/services/chat_multistep.py", "_rejectOversizedPlan"),
    ("app/services/chat_stream_multistep.py", "_streamMultiStep"),
    ("app/services/chat_stream_persist.py", "persistInterruptedStream"),
]


def _saveQueryStateCalls(modulePath: str, funcName: str) -> list[ast.Call]:
    """取某个函数里所有 `_saveQueryState(...)` 调用节点。"""
    source = Path(__file__).resolve().parents[3] / modulePath
    tree = ast.parse(source.read_text())
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name != funcName:
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "_saveQueryState"
            ):
                found.append(sub)
    return found


class Test运行记录落库口径:
    """每一轮跑完都要能钉住 —— 落点覆盖 + 多步降级收尾的实际传参。"""

    @pytest.mark.parametrize(("modulePath", "funcName"), _MUST_RECORD_RELEASE_ID)
    def test_落点必须把发布id记进运行记录(self, modulePath, funcName) -> None:
        calls = _saveQueryStateCalls(modulePath, funcName)
        assert calls, f"{modulePath}::{funcName} 里没有 _saveQueryState 调用"
        withId = [
            call for call in calls
            if any(kw.arg == "semanticReleaseId" for kw in call.keywords)
        ]
        assert withId, (
            f"{modulePath}::{funcName} 的 _saveQueryState 没传 semanticReleaseId"
            " ⇒ 这条链路的会话永远钉不住发布"
        )

    @pytest.mark.parametrize(("modulePath", "funcName"), _MUST_RECORD_RELEASE_ID)
    def test_落点必须显式带钉住失效标志(self, modulePath, funcName) -> None:
        """每一处落点都必须**显式**传 `pinInvalidated`（值可以是 False）。

        为什么要求「显式」：缺省 False 与显式 False 在**今天**语义等价，但在改动
        里不等价 —— 落点若忘了传，就拿不到闸门的失效信号 ⇒ 死钉不自愈。
        显式传 ⇒ 每次改动都必须想一次「这一处该不该清掉那个钉住」。
        """
        calls = _saveQueryStateCalls(modulePath, funcName)
        assert calls, f"{modulePath}::{funcName} 里没有 _saveQueryState 调用"
        withoutFlag = [
            call for call in calls
            if not any(kw.arg == "pinInvalidated" for kw in call.keywords)
        ]
        assert not withoutFlag, (
            f"{modulePath}::{funcName} 的 _saveQueryState 有调用点没显式传 pinInvalidated"
            " ⇒ 拿不到闸门的「钉住失效」信号 ⇒ 死钉不自愈"
        )

    async def test_多步降级收尾把发布id传给运行记录(self) -> None:
        """D16：多步降级收尾（`_finalizeMultiStepDegrade`）把本轮 id 透传给落库。"""
        from app.services.chat_multistep import MultiStepMixin

        calls: list[dict] = []

        class _Stub:
            async def _storeSessionMessages(self, *args, **kwargs) -> None:
                return None

            def _columns(self, data) -> list[str]:
                return []

            async def _saveQueryState(self, *args, **kwargs) -> None:
                calls.append(kwargs)

        stub = _Stub()
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        await MultiStepMixin._finalizeMultiStepDegrade(
            stub, object(), dto, [],
            last_plan=None, last_sql=None, last_data=[],
            total_cost=Decimal("0"), _t0=0.0, last_release_id=41,
        )
        assert calls[0]["semanticReleaseId"] == 41

    async def test_多步降级收尾没发布id时不覆盖既有钉住(self) -> None:
        """D16：没有发布 id ⇒ 传 None ⇒ 写侧「保留既有钉住」语义生效。"""
        from app.services.chat_multistep import MultiStepMixin

        calls: list[dict] = []

        class _Stub:
            async def _storeSessionMessages(self, *args, **kwargs) -> None:
                return None

            def _columns(self, data) -> list[str]:
                return []

            async def _saveQueryState(self, *args, **kwargs) -> None:
                calls.append(kwargs)

        stub = _Stub()
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        await MultiStepMixin._finalizeMultiStepDegrade(
            stub, object(), dto, [],
            last_plan=None, last_sql=None, last_data=[],
            total_cost=Decimal("0"), _t0=0.0,
        )
        assert calls[0]["semanticReleaseId"] is None

    def test_步骤产物带发布id(self) -> None:
        """D16：`_StepRun` 携带本步的发布 id（调用方据此更新「最后一个成功步」的锚点）。"""
        from app.services.chat_helpers import _StepRun

        run = _StepRun(result=SimpleNamespace(), semanticReleaseId=41)
        assert run.semanticReleaseId == 41
        assert _StepRun(result=SimpleNamespace()).semanticReleaseId is None


# ---------------------------------------------------------------------------
# 迁移形态守卫的小工具
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"


def _loadMigration(fileStem: str):
    path = _MIGRATIONS_DIR / f"{fileStem}.py"
    assert path.exists(), f"迁移文件不存在：{path}"
    spec = importlib.util.spec_from_file_location(f"_mig_{fileStem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recordedOps(module, funcName: str) -> list[tuple]:
    """跑一遍迁移函数，记录它对 `alembic.op` 发了哪些 DDL 调用。"""
    calls: list[tuple] = []

    class _FakeOp:
        def add_column(self, table, column, **kwargs):
            calls.append(("add_column", table, column.name))

        def drop_column(self, table, columnName):
            calls.append(("drop_column", table, columnName))

        def __getattr__(self, name):
            def _unexpected(*args, **kwargs):
                calls.append((name, *args))

            return _unexpected

    original = module.op
    module.op = _FakeOp()
    try:
        getattr(module, funcName)()
    finally:
        module.op = original
    return calls


# ---------------------------------------------------------------------------
# 评审补测（第 2 轮）：钉住路径口径 / 钉住失效 / 回灌重试 / 落点守卫 / 迁移往返
# ---------------------------------------------------------------------------


class Test钉住路径口径:
    """钉住的那一版与当前本体不一致时，**仍用那一版**（D16 偏差 ⑭）。

    为什么单列一档：D15 的事实守卫拿「快照 vs **当前本体**」比差异 ——
    对**未钉住**的 ACTIVE 快照是对的（陈旧 ⇒ 拒绝用快照）。
    但对**钉住**的路径，它恰好否定了钉住的意义：钉住的就是「上一轮那一版」，
    它与当前本体不同是**发布之后的常态**，不是数据事故 ⇒ 照旧降级的话，
    钉住永远拿不到那一版 ⇒「同一请求重放读同一发布」落空。
    ⇒ 钉住路径只做**自完整性**校验（manifest_hash / 体量 / schema 版本），
    与当前本体的差异**记 warning 但不降级**。
    """

    async def test_钉住那一版与当前本体不一致时仍用钉住版(
        self, switchOn, monkeypatch
    ) -> None:
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        # 当前本体比 A 多一列 ⇒ A 与当前本体事实不一致
        currentClasses, currentProps, currentJoins = _dataset(extraSupplierProp=True)
        snapshotB = _FakeSnapshot(
            _manifest(currentClasses, currentProps, currentJoins), "v-b", _RELEASE_B
        )
        _installRegistry(
            monkeypatch, _FakeRegistry(active=snapshotB, byId={_RELEASE_A: snapshotA})
        )

        result = await gate.renderSchemaWithRelease(
            currentClasses, joins=currentJoins, session=object(),
            pinnedReleaseId=_RELEASE_A,
        )

        assert result.semanticReleaseId == _RELEASE_A, "钉住路径必须用钉住的那一版"
        # 用的是 A 的快照（没有 EXTRA_COLUMN），不是直读当前本体
        assert "EXTRA_COLUMN" not in result.text
        assert "EXTRA_COLUMN" in buildSchemaText(currentClasses, joins=currentJoins)
        assert result.text != buildSchemaText(currentClasses, joins=currentJoins)

    async def test_钉住那一版与当前本体不一致时记警告(
        self, switchOn, monkeypatch, caplog
    ) -> None:
        """差异**必须可观测**（不许静默）：但级别是 warning，不是 error ——
        error 级在本模块的语义是「事实缺口」（ACTIVE 快照陈旧）。"""
        classes, properties, joins = _dataset()
        snapshotA = _FakeSnapshot(_manifest(*_dataset()), "v-a", _RELEASE_A)
        currentClasses, currentProps, currentJoins = _dataset(extraSupplierProp=True)
        _installRegistry(
            monkeypatch,
            _FakeRegistry(
                active=_FakeSnapshot(
                    _manifest(currentClasses, currentProps, currentJoins), "v-b", _RELEASE_B
                ),
                byId={_RELEASE_A: snapshotA},
            ),
        )

        with caplog.at_level(logging.WARNING):
            await gate.renderSchemaWithRelease(
                currentClasses, joins=currentJoins, session=object(),
                pinnedReleaseId=_RELEASE_A,
            )

        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert any("钉住" in rec.getMessage() for rec in warnings), (
            f"钉住路径与当前本体不一致必须留 warning 级痕迹，实际日志："
            f"{[rec.getMessage() for rec in warnings]}"
        )

    async def test_未钉住时ACTIVE陈旧仍然降级(self, switchOn, monkeypatch) -> None:
        """对照组：**未钉住**的 ACTIVE 快照陈旧 ⇒ 仍然拒绝用快照（D15 语义不变）。

        形态：ACTIVE 快照比当前本体**少一列**（发布之后本体又加了列）。"""
        currentClasses, currentProps, currentJoins = _dataset(extraSupplierProp=True)
        snapshotOld = _FakeSnapshot(_manifest(*_dataset()), "v-old", _RELEASE_A)
        _installRegistry(monkeypatch, _FakeRegistry(active=snapshotOld))

        result = await gate.renderSchemaWithRelease(
            currentClasses, joins=currentJoins, session=object()
        )

        assert result.semanticReleaseId is None
        assert result.text == buildSchemaText(currentClasses, joins=currentJoins)


class Test钉住失效信号:
    """钉住的那一版**用不上**时，运行记录里的钉住必须被**清掉**。

    为什么：运行记录声称「本会话钉住 A」，而 A 已经取不到 ⇒ 下一轮继续钉 A
    ⇒ 每一轮都降级直读、每一轮都记 warning ⇒ 死钉。若不清掉，会话永远起不来。
    ⇒ 渲染层回报一个**显式的失效信号**（而不是让调用方从「没有 id」反推）。
    """

    async def test_钉住那一版取不到时标记钉住失效(self, switchOn, monkeypatch) -> None:
        classes, properties, joins = _dataset()
        _installRegistry(monkeypatch, _FakeRegistry(active=None))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object(), pinnedReleaseId=999
        )

        assert result.semanticReleaseId is None
        assert result.pinInvalidated is True, "钉住取不到 ⇒ 必须回报「钉住失效」"

    async def test_没钉住时直读不算钉住失效(self, switchOn, monkeypatch) -> None:
        """没有钉住可失效 ⇒ 失效信号必须为 False（否则会误清既有钉住）。

        场景：库里**没有 ACTIVE 发布** ⇒ 本轮确定直读 ⇒ 没有「钉住」可言。
        """
        classes, properties, joins = _dataset()
        _installRegistry(monkeypatch, _FakeRegistry(active=None))

        result = await gate.renderSchemaWithRelease(
            classes, joins=joins, session=object()
        )

        assert result.semanticReleaseId is None
        assert result.pinInvalidated is False, (
            "本轮压根没钉住 ⇒ 失效信号必须为 False，否则会误清既有钉住"
        )

    async def test_钉住的发布id非法时降级直读且留专门信号(
        self, switchOn, monkeypatch, caplog
    ) -> None:
        """id 越界 ⇒ Registry 抛 ValidationError ⇒ 必须被接住（不许穿透成 500），
        且日志要带 releaseId（这是「运行记录指向了非法发布」需要被看到的场景）。"""
        classes, properties, joins = _dataset()
        _installRegistry(monkeypatch, _FakeRegistry(active=None))

        with caplog.at_level(logging.WARNING):
            result = await gate.renderSchemaWithRelease(
                classes, joins=joins, session=object(), pinnedReleaseId=0
            )

        assert result.text == buildSchemaText(classes, joins=joins)
        assert result.semanticReleaseId is None
        assert result.pinInvalidated is True
        warnings = [rec.getMessage() for rec in caplog.records]
        assert any("releaseId=0" in msg for msg in warnings), (
            f"非法发布 id 必须留下带 releaseId 的专用信号，实际日志：{warnings}"
        )


class Test回灌重试口径:
    """回灌重试生成的 SQL **就是这一轮实际执行的 SQL** ⇒ 它必须与首次生成同一版。"""

    async def test_重试换了版本时实际执行的版本被回报(self) -> None:
        """钉住的那一版在**重试前**被删 ⇒ 重试降级直读 ⇒ 实际执行的 SQL 是直读口径。

        期望：`_runQueryWithRetry` 必须把「重试那一版」交回调用方 ——
        否则运行记录会继续声称用了旧版（那一版实际没参与执行），
        且死钉（指向已删除的发布）不会被清掉。
        """
        from app.services.chat_usage import UsageMixin

        class _Stub(UsageMixin):
            def __init__(self) -> None:
                self._nl2sql = _FakeNl2Sql(
                    planReleaseId=_RELEASE_A,
                    # 回灌重试时 A 版已被删 ⇒ 降级直读（None）+ 钉住失效
                    sqlReleaseIds=[None],
                    sqlPinInvalidatedSeq=[True],
                )
                self.failed = False

            def _llmFactory(self, cfg):
                return None

            async def _runQuery(self, pc, dto, sql, *, user_id=None):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("首次执行失败")
                return [{"x": 1}]

            def _checkShareResult(self, plan, data, sql) -> None:
                return None

        stub = _Stub()
        outcome = SimpleNamespace(
            sql="SELECT 1", plan=SimpleNamespace(), sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=_RELEASE_A,
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        pc = SimpleNamespace(
            classes=[], joins=[],
            ds=SimpleNamespace(type="postgresql", oracle_version=None, username=None),
            contextPrompt=None, fewShot=None, valueSamples=None, driftWarning=None,
            selected=SimpleNamespace(model_name="test-model", id=1),
        )

        result = await stub._runQueryWithRetry(object(), dto, pc, outcome)

        retryResult = result[3]
        assert retryResult is not None, "重试发生了 ⇒ 必须把重试那一版交回调用方"
        assert retryResult.semanticReleaseId is None, (
            "重试降级直读 ⇒ 实际执行的 SQL 是直读口径 ⇒ 必须回报 None"
        )
        assert retryResult.pinInvalidated is True, (
            "重试时钉住的那一版已不可用 ⇒ 必须回报「钉住失效」⇒ 运行记录清掉死钉"
        )

    async def test_没有重试时不交回重试结果(self) -> None:
        """没触发重试 ⇒ 第四元为 None（调用方据此同时不改 outcome）。"""
        from app.services.chat_usage import UsageMixin

        class _Stub(UsageMixin):
            def __init__(self) -> None:
                self._nl2sql = _FakeNl2Sql(planReleaseId=_RELEASE_A, sqlReleaseId=_RELEASE_A)

            def _llmFactory(self, cfg):
                return None

            async def _runQuery(self, pc, dto, sql, *, user_id=None):
                return [{"x": 1}]

            def _checkShareResult(self, plan, data, sql) -> None:
                return None

        stub = _Stub()
        outcome = SimpleNamespace(
            sql="SELECT 1", plan=SimpleNamespace(), sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=_RELEASE_A,
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        pc = SimpleNamespace(
            classes=[], joins=[],
            ds=SimpleNamespace(type="postgresql", oracle_version=None, username=None),
            contextPrompt=None, fewShot=None, valueSamples=None, driftWarning=None,
            selected=SimpleNamespace(model_name="test-model", id=1),
        )

        result = await stub._runQueryWithRetry(object(), dto, pc, outcome)

        assert result[3] is None

    async def test_回灌重试沿用本轮那一版发布(self) -> None:
        from app.services.chat_usage import UsageMixin

        class _Stub(UsageMixin):
            def __init__(self) -> None:
                self._nl2sql = _FakeNl2Sql(
                    planReleaseId=_RELEASE_A, sqlReleaseId=_RELEASE_A
                )
                self.failed = False

            def _llmFactory(self, cfg):
                return None

            async def _runQuery(self, pc, dto, sql, *, user_id=None):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("首次执行失败")
                return [{"x": 1}]

            def _checkShareResult(self, plan, data, sql) -> None:
                return None

        stub = _Stub()
        outcome = SimpleNamespace(
            sql="SELECT 1", plan=SimpleNamespace(), sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=_RELEASE_A,
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        pc = SimpleNamespace(
            classes=[], joins=[],
            ds=SimpleNamespace(type="postgresql", oracle_version=None, username=None),
            contextPrompt=None, fewShot=None, valueSamples=None, driftWarning=None,
            selected=SimpleNamespace(model_name="test-model", id=1),
        )

        await stub._runQueryWithRetry(object(), dto, pc, outcome)

        assert stub._nl2sql.sqlCalls, "回灌重试应当重新生成一次 SQL"
        retryCall = stub._nl2sql.sqlCalls[0]
        assert retryCall["pinnedReleaseId"] == _RELEASE_A, (
            "回灌重试必须沿用本轮那一版发布，否则「记账的版本」与"
            "「实际执行的 SQL 的口径」不是同一版"
        )
        assert retryCall.get("directOnly") is not True

    async def test_本轮直读时回灌重试也直读(self) -> None:
        """本轮没走快照 ⇒ 重试也直读（不许中途换口径）。"""
        from app.services.chat_usage import UsageMixin

        class _Stub(UsageMixin):
            def __init__(self) -> None:
                self._nl2sql = _FakeNl2Sql(planReleaseId=None, sqlReleaseId=None)
                self.failed = False

            def _llmFactory(self, cfg):
                return None

            async def _runQuery(self, pc, dto, sql, *, user_id=None):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("首次执行失败")
                return [{"x": 1}]

            def _checkShareResult(self, plan, data, sql) -> None:
                return None

        stub = _Stub()
        outcome = SimpleNamespace(
            sql="SELECT 1", plan=SimpleNamespace(), sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=None,
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        pc = SimpleNamespace(
            classes=[], joins=[],
            ds=SimpleNamespace(type="postgresql", oracle_version=None, username=None),
            contextPrompt=None, fewShot=None, valueSamples=None, driftWarning=None,
            selected=SimpleNamespace(model_name="test-model", id=1),
        )

        await stub._runQueryWithRetry(object(), dto, pc, outcome)

        retryCall = stub._nl2sql.sqlCalls[0]
        assert retryCall["pinnedReleaseId"] is None
        assert retryCall.get("directOnly") is True

    def test_合并后口径以实际执行的版本为准(self) -> None:
        """`_mergeExecutedRelease`：重试换版 ⇒ outcome 改成「实际执行的那一版」。

        口径载体 = **实际执行的那条 SQL 的口径**（DD：口径载体 = SQL 阶段）。
        """
        from app.services.chat_helpers import _mergeExecutedRelease, _SqlOutcome

        outcome = _SqlOutcome(
            plan=SimpleNamespace(), sql="SELECT 1", sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=_RELEASE_A,
        )
        retryResult = SimpleNamespace(semanticReleaseId=None, pinInvalidated=True)

        merged = _mergeExecutedRelease(outcome, retryResult)

        assert merged.semanticReleaseId is None, "实际执行的是直读口径 ⇒ 必须记 None"
        assert merged.pinInvalidated is True, "钉住已不可用 ⇒ 必须判失效（死钉自愈）"
        assert outcome.semanticReleaseId == _RELEASE_A, "原 outcome 不可被就地改写"

    def test_合并时同版不动(self) -> None:
        """重试与首次同版 ⇒ outcome 原样返回（不产生无谓的新对象）。"""
        from app.services.chat_helpers import _mergeExecutedRelease, _SqlOutcome

        outcome = _SqlOutcome(
            plan=SimpleNamespace(), sql="SELECT 1", sqlConfig=None,
            promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
            semanticReleaseId=_RELEASE_A,
        )
        retryResult = SimpleNamespace(semanticReleaseId=_RELEASE_A, pinInvalidated=False)

        merged = _mergeExecutedRelease(outcome, retryResult)

        assert merged is outcome

    @pytest.mark.parametrize(
        "modulePath",
        [
            "app/services/chat_service.py",
            "app/services/chat_multistep.py",
            "app/services/chat_stream_query.py",
        ],
    )
    def test_三条执行链都必须按实际执行的版本改写outcome(self, modulePath) -> None:
        """AST 守卫：拿到重试结果后必须调用 `_mergeExecutedRelease`。

        漏了 ⇒ 运行记录继续声称首次那一版，而实际执行的是重试那一版的口径。
        """
        source = Path(__file__).resolve().parents[3] / modulePath
        tree = ast.parse(source.read_text())
        calls = [
            call
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_mergeExecutedRelease"
        ]
        assert calls, f"{modulePath} 必须把「实际执行的那一版」并回 outcome"


class Test多步口径传递:
    """多步的一轮里，各数据步必须**共用同一版**发布。"""

    def test_步骤调用必须带上本轮的发布id(self) -> None:
        """AST 守卫：多步循环必须把「本轮已定的发布 id」传给下一个数据步。"""
        source = Path(__file__).resolve().parents[3] / "app/services/chat_multistep.py"
        tree = ast.parse(source.read_text())
        stepCalls = [
            call
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_executeMultiStep"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_executeDataStep"
        ]
        assert stepCalls, "多步循环里必须调用 _executeDataStep"
        for call in stepCalls:
            names = [kw.arg for kw in call.keywords]
            assert "pinned_release_id" in names, (
                "多步的数据步必须带上本轮的发布 id，否则各步口径可能漂移"
            )

    async def test_数据步把发布id传给计划生成(self) -> None:
        """行为断言：本轮已定的发布 id 必须真的传到 `_planAndGenerateSql`。"""
        from app.services.chat_multistep import MultiStepMixin

        seen: list[dict] = []

        class _Stub:
            async def _recallForStep(self, session, pc, step_plan):
                return pc

            async def _planAndGenerateSql(self, *args, **kwargs):
                seen.append(kwargs)
                return SimpleNamespace(
                    sql="SELECT 1",
                    plan=SimpleNamespace(isUnanswerable=False, selectedClasses=[]),
                    promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
                    sqlConfig=None, semanticReleaseId=_RELEASE_A,
                )

            async def _runQueryWithRetry(self, *args, **kwargs):
                # 第四元 = 重试那一次的生成结果（None = 没重试），P2 D16。
                return [{"x": 1}], "SELECT 1", (0, 0), None

            def _costForSql(self, outcome, cfg):
                return Decimal("0")

            def _spawnEmbedding(self, *args, **kwargs):
                return None

            async def _stepChart(self, *args, **kwargs):
                return (
                    SimpleNamespace(value="table"), None, None, None, 0, 0, False,
                )

            def _summarizeStepData(self, data):
                return "摘要"

            @property
            def _subquestionRewriter(self):
                return SimpleNamespace(
                    rewrite=lambda **kwargs: SimpleNamespace(rewritten=None)
                )

        stub = _Stub()
        pc = SimpleNamespace(
            classes=[], joins=[], selected=SimpleNamespace(model_name="m", id=1),
            ds=SimpleNamespace(type="postgresql", oracle_version=None),
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        ctx = SimpleNamespace(
            inject_to_prompt=lambda index: None, completed_steps=[],
            global_filters=None,
        )
        stepPlan = SimpleNamespace(
            index=1, description="d", sub_question="子问题",
        )

        await MultiStepMixin._executeDataStep(
            stub, object(), dto, pc, ctx, stepPlan, None,
            pinned_release_id=_RELEASE_A,
        )

        assert seen and seen[0].get("pinned_release_id") == _RELEASE_A

    async def test_软失败步骤也必须带上钉住失效信号(self) -> None:
        """多步的软失败分支（LLM 判不可回答）**不能丢**钉住信号。

        丢了的后果：钉住的那一版已被删 ⇒ 本步降级直读 ⇒ 信号没上报 ⇒ 运行记录
        保留那个已经用不了的钉住 ⇒ 下一轮继续钉它 ⇒ 每轮降级、每轮一条 WARNING
        ⇒ **死钉永不自愈**（与单步路径行为分叉）。
        """
        from app.services.chat_multistep import MultiStepMixin

        class _Stub:
            async def _recallForStep(self, session, pc, step_plan):
                return pc

            async def _planAndGenerateSql(self, *args, **kwargs):
                # 计划判不可回答 ⇒ sql=None；同时本轮的钉住失效了（那一版被删）。
                return SimpleNamespace(
                    sql=None,
                    plan=SimpleNamespace(isUnanswerable=True, selectedClasses=[]),
                    promptTokens=1, completionTokens=1, wasted=(0, 0), cachedTokens=None,
                    sqlConfig=None, semanticReleaseId=None, pinInvalidated=True,
                )

            async def _runQueryWithRetry(self, *args, **kwargs):
                return [{"x": 1}], "SELECT 1", (0, 0), None

            def _costForSql(self, outcome, cfg):
                return Decimal("0")

            def _spawnEmbedding(self, *args, **kwargs):
                return None

            async def _stepChart(self, *args, **kwargs):
                return (
                    SimpleNamespace(value="table"), None, None, None, 0, 0, False,
                )

            def _summarizeStepData(self, data):
                return "摘要"

            @property
            def _subquestionRewriter(self):
                return SimpleNamespace(
                    rewrite=lambda **kwargs: SimpleNamespace(rewritten=None)
                )

        stub = _Stub()
        pc = SimpleNamespace(
            classes=[], joins=[], selected=SimpleNamespace(model_name="m", id=1),
            ds=SimpleNamespace(type="postgresql", oracle_version=None),
        )
        dto = SimpleNamespace(question="问题", sessionId="s-1", datasourceId=1)
        ctx = SimpleNamespace(
            inject_to_prompt=lambda index: None, completed_steps=[],
            global_filters=None,
        )
        stepPlan = SimpleNamespace(index=1, description="d", sub_question="子问题")

        stepRun = await MultiStepMixin._executeDataStep(
            stub, object(), dto, pc, ctx, stepPlan, None,
            pinned_release_id=_RELEASE_A,
        )

        assert stepRun.pinInvalidated is True, (
            "软失败步骤丢了「钉住失效」信号 ⇒ 运行记录保留死钉 ⇒ 死钉永不自愈"
        )
        assert stepRun.semanticReleaseId is None


class Test落点守卫收紧:
    """落点守卫必须是「每一处都带」，不是「至少有一处带」。"""

    @pytest.mark.parametrize(("modulePath", "funcName"), _MUST_RECORD_RELEASE_ID)
    def test_每一处落点都必须显式带发布id(self, modulePath, funcName) -> None:
        calls = _saveQueryStateCalls(modulePath, funcName)
        assert calls, f"{modulePath}::{funcName} 里没有 _saveQueryState 调用"
        missing = [
            index
            for index, call in enumerate(calls)
            if not any(kw.arg == "semanticReleaseId" for kw in call.keywords)
        ]
        assert not missing, (
            f"{modulePath}::{funcName} 的第 {missing} 处 _saveQueryState 没显式写 "
            "semanticReleaseId=（哪怕值是 None）⇒ 落点口径不可审计"
        )


class Test迁移离线SQL往返:
    """迁移的**离线 SQL** 往返（不碰库 ⇒ 不受分片轮转影响）。

    真库的 upgrade→downgrade→upgrade 往返由集成用例承担；
    这里锁住「迁移发出来的 DDL 是什么」：只加一列、可空、downgrade 对称。
    """

    def test_升级与回退发出的SQL对称(self) -> None:
        import io

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        module = _loadMigration("0144_session_query_state_semantic_release_id")

        def _emit(funcName: str) -> str:
            buffer = io.StringIO()
            context = MigrationContext.configure(
                dialect_name="postgresql",
                opts={"as_sql": True, "output_buffer": buffer},
            )
            with Operations.context(context):
                getattr(module, funcName)()
            return buffer.getvalue()

        upgradeSql = _emit("upgrade")
        downgradeSql = _emit("downgrade")

        assert "ALTER TABLE session_query_state ADD COLUMN semantic_release_id INTEGER" in upgradeSql
        assert "NOT NULL" not in upgradeSql, "新列必须可空（存量旧行没有这个值）"
        assert "ALTER TABLE session_query_state DROP COLUMN semantic_release_id" in downgradeSql

