"""P2 D15：schema 文本渲染路径的**接线闸门**（开关 + 完整性守卫 + 降级）。

本文件钉死四件事：

1. **开关默认关**：关 ⇒ 输出与「今天的直读渲染」**逐字节一致**，且连库都不碰；
2. **开关开且快照新鲜**：输出与直读**内容等价**（行多重集相等），
   但**类内列序按快照**（按 `propertyName` 排序）⇒ D14 的结论在运行期的体现；
3. **反向守卫**（任务卡原文：「开关开但快照缺字段 ⇒ 报错而非静默降级」）：
   快照缺事实（少列 / 多列 / 少 JOIN 边）⇒ 必须**记 error 级日志**，
   且降级后的输出与「开关关」**逐字节一致** —— 降级不许看起来跟「快照生效」一样；
4. **不许炸问答**：任何异常都降级，不穿透成 500（P0 裁决一）。

数据构造用**真实 ORM 实体**（不落库）+ **真实编译器**（`compileFromRows`）产快照，
这样「快照缺列」不是手搓的假 manifest，而是「发布之后本体又加了一列」的真实形态。
"""

from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace

import pytest

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services import semantic_schema_adapter as adapterModule
from app.services import semantic_schema_gate as gate
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_compiler import compileFromRows

SUPPLIER_ID = 1
ORDER_ID = 2


# ---------------------------------------------------------------------------
# 数据构造（真实 ORM 实体，不落库）
# ---------------------------------------------------------------------------


def _prop(propId: int, classId: int, name: str, **kwargs) -> OntologyProperty:
    return OntologyProperty(
        id=propId,
        class_id=classId,
        property_name=name,
        property_alias=kwargs.get("alias"),
        business_aliases=kwargs.get("business_aliases"),
        description=kwargs.get("description"),
        data_type=kwargs.get("dataType", "STRING"),
        is_primary_key=kwargs.get("isPk", False),
        is_foreign_key=kwargs.get("isFk", False),
        ref_class_id=kwargs.get("refClassId"),
        source_column=kwargs.get("sourceColumn"),
        unit=kwargs.get("unit"),
        allowed_values=kwargs.get("allowedValues"),
        is_not_null=kwargs.get("isNotNull"),
        min_value=kwargs.get("minValue"),
        max_value=kwargs.get("maxValue"),
        regex_pattern=kwargs.get("regexPattern"),
    )


def _cls(classId: int, name: str, *, sourceTable: str, properties: tuple = ()) -> OntologyClass:
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


def _dataset(*, supplierProps: tuple | None = None, joins: tuple | None = None):
    """一套含「继承无关 / 外键 / JOIN」的最小数据。

    `supplierProps` / `joins` 可替换 ⇒ 用来造「发布之后本体又改了」的陈旧快照。
    """
    if supplierProps is None:
        supplierProps = (
            # 物理行序刻意不按名字排（与生产堆物理序同形）：
            # 快照侧按 propertyName 排序 ⇒ 两侧列序不同、内容相同。
            _prop(101, SUPPLIER_ID, "SUPPLIER_NAME", sourceColumn="SUPPLIER_NAME"),
            _prop(102, SUPPLIER_ID, "SUPPLIER_CODE", isPk=True, sourceColumn="SUPPLIER_CODE"),
        )
    orderProps = (
        _prop(201, ORDER_ID, "SUPPLIER_CODE", isFk=True, refClassId=SUPPLIER_ID,
              sourceColumn="SUPPLIER_CODE"),
        _prop(202, ORDER_ID, "ORDER_CODE", isPk=True, sourceColumn="ORDER_CODE"),
    )
    classes = [
        _cls(SUPPLIER_ID, "Supplier", sourceTable="SUPPLIER", properties=supplierProps),
        _cls(ORDER_ID, "PurchaseOrder", sourceTable="PURCHASE_ORDER", properties=orderProps),
    ]
    properties = [prop for cls in classes for prop in cls.properties]
    if joins is None:
        joins = (
            OntologyJoin(
                id=1,
                source_class_id=ORDER_ID,
                source_columns=["SUPPLIER_CODE"],
                target_class_id=SUPPLIER_ID,
                target_columns=["SUPPLIER_CODE"],
                join_type="LEFT",
                relation_type="foreign_key",
                description="订单挂供方",
                join_key="PURCHASE_ORDER_SUPPLIER",
            ),
        )
    return classes, properties, list(joins)


def _manifest(classes, properties, joins) -> dict:
    return compileFromRows(classes=classes, properties=properties, joins=joins)


class _FakeSnapshot:
    def __init__(self, manifest: dict, version: str = "v-test") -> None:
        self.manifest = manifest
        self.version = version
        self.schema_version = manifest.get("schemaVersion")


class _FakeSession:
    """占位会话：闸门只把它转交给 registry（替身不碰库）⇒ 不需要真实连接。

    顺带承担「现读 system_config 阈值」的调用：`_readIntConfig` 等对读失败
    一律返默认值（不阻断主链路）⇒ 假会话读不到配置正好落默认值。
    """


class _FakeRegistry:
    def __init__(self, snapshot: _FakeSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[str | None] = []

    async def getActive(self, session, *, domainCode: str | None = None):
        self.calls.append(domainCode)
        return self._snapshot


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


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


def _installSnapshot(monkeypatch: pytest.MonkeyPatch, snapshot: _FakeSnapshot | None) -> _FakeRegistry:
    registry = _FakeRegistry(snapshot)
    monkeypatch.setattr(adapterModule, "semanticRegistry", registry)
    return registry


def _columnOrder(text: str, className: str = "Supplier") -> list[str]:
    """取某个类块里的列名顺序（只看该块，不受别的块里的同名列干扰）。"""
    order: list[str] = []
    inBlock = False
    for line in text.splitlines():
        if line.startswith("### "):
            inBlock = line.startswith(f"### {className}")
            continue
        if inBlock and line.strip():
            stripped = line.strip()
            if stripped == "Columns:":
                continue
            order.append(stripped.split(":")[0].strip())
    return order


# ---------------------------------------------------------------------------
# 1. 开关语义
# ---------------------------------------------------------------------------


class Test开关:
    async def test_开关关闭时输出与直读渲染逐字节一致(self, switchOff, monkeypatch) -> None:
        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))

        text = await gate.renderSchemaText(classes, joins=joins)

        assert text == buildSchemaText(classes, joins=joins)

    async def test_开关关闭时连库都不碰(self, switchOff, monkeypatch) -> None:
        classes, properties, joins = _dataset()
        registry = _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(*_dataset())))

        await gate.renderSchemaText(classes, joins=joins)

        assert registry.calls == []

    async def test_开关打开但没有发布时输出与直读渲染逐字节一致(
        self, switchOn, monkeypatch
    ) -> None:
        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, None)

        text = await gate.renderSchemaText(classes, joins=joins, session=_FakeSession())

        assert text == buildSchemaText(classes, joins=joins)

    async def test_开关打开且快照新鲜时内容等价但列序按快照(self, switchOn, monkeypatch) -> None:
        """D14 的结论在运行期的体现：内容不变、**类内列序**按快照（按名排序）。"""
        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))

        text = await gate.renderSchemaText(classes, joins=joins, session=_FakeSession())
        direct = buildSchemaText(classes, joins=joins)

        # 内容等价（行多重集相等）
        assert Counter(text.splitlines()) == Counter(direct.splitlines())
        # 但列序不同 ⇒ 文本不相同 ⇒ 证明确实走了快照路径
        assert text != direct
        # 快照侧按 propertyName 排序；直读侧是物理行序（这里刻意不按名排）
        assert _columnOrder(text) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]
        assert _columnOrder(direct) == ["SUPPLIER_NAME", "SUPPLIER_CODE"]


# ---------------------------------------------------------------------------
# 2. 反向守卫：快照缺事实 ⇒ 不许静默降级
# ---------------------------------------------------------------------------


class Test反向守卫:
    async def test_快照少了列时降级直读并记error(self, switchOn, monkeypatch, caplog) -> None:
        """发布之后本体又加了一列 ⇒ 快照缺事实 ⇒ 降级 + **响**（error 级日志）。"""
        classes, properties, joins = _dataset()
        # 陈旧快照：Supplier 少了 SUPPLIER_NAME 这一列
        staleProps = (
            _prop(102, SUPPLIER_ID, "SUPPLIER_CODE", isPk=True, sourceColumn="SUPPLIER_CODE"),
        )
        staleClasses, staleProperties, staleJoins = _dataset(supplierProps=staleProps)
        _installSnapshot(
            monkeypatch, _FakeSnapshot(_manifest(staleClasses, staleProperties, staleJoins))
        )

        with caplog.at_level("ERROR"):
            text = await gate.renderSchemaText(
                classes, joins=joins, session=_FakeSession()
            )

        # 降级后的输出与「开关关」逐字节一致
        assert text == buildSchemaText(classes, joins=joins)
        # 且必须**响**：error 级日志
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "快照缺事实时必须记 error 级日志，不许静默降级"

    async def test_快照多了列时同样降级(self, switchOn, monkeypatch, caplog) -> None:
        """快照比活库多一列 ⇒ 事实不一致 ⇒ 同样降级（宁可少信息也不错信息）。"""
        classes, properties, joins = _dataset()
        # 快照侧多一列（活库里没有）
        extraProps = (
            _prop(101, SUPPLIER_ID, "SUPPLIER_NAME", sourceColumn="SUPPLIER_NAME"),
            _prop(102, SUPPLIER_ID, "SUPPLIER_CODE", isPk=True, sourceColumn="SUPPLIER_CODE"),
            _prop(103, SUPPLIER_ID, "SUPPLIER_ADDR", sourceColumn="SUPPLIER_ADDR"),
        )
        extraClasses, extraProperties, extraJoins = _dataset(supplierProps=extraProps)
        _installSnapshot(
            monkeypatch, _FakeSnapshot(_manifest(extraClasses, extraProperties, extraJoins))
        )

        with caplog.at_level("ERROR"):
            text = await gate.renderSchemaText(
                classes, joins=joins, session=_FakeSession()
            )

        assert text == buildSchemaText(classes, joins=joins)
        assert [r for r in caplog.records if r.levelname == "ERROR"]

    async def test_快照少了JOIN边时降级直读并记error(self, switchOn, monkeypatch, caplog) -> None:
        """发布之后本体又加了一条 JOIN 边 ⇒ 快照侧渲染不出这条边 ⇒ 事实丢失。"""
        classes, properties, joins = _dataset()
        staleClasses, staleProperties, staleJoins = _dataset(joins=())
        _installSnapshot(
            monkeypatch, _FakeSnapshot(_manifest(staleClasses, staleProperties, staleJoins))
        )

        with caplog.at_level("ERROR"):
            text = await gate.renderSchemaText(
                classes, joins=joins, session=_FakeSession()
            )

        assert text == buildSchemaText(classes, joins=joins)
        assert "### JOIN 关系" in text
        assert [r for r in caplog.records if r.levelname == "ERROR"]

    async def test_快照缺类时由适配器陈旧闸门降级直读(self, switchOn, monkeypatch, caplog) -> None:
        """快照里根本没有这个类 ⇒ 降级（适配器的**陈旧**闸门先拦一道）。

        注意：这一档是 **warning** 级日志（快照陈旧 = 发布节奏的常态），
        与「快照缺列 / 缺边」那一档的 **error** 级不同 ⇒ 测试名不写「记error」。
        """
        classes, properties, joins = _dataset()
        onlySupplierClasses, onlySupplierProperties, onlySupplierJoins = _dataset()
        manifest = _manifest(onlySupplierClasses, onlySupplierProperties, onlySupplierJoins)
        manifest["classes"] = [
            cls for cls in manifest["classes"] if cls["className"] == "Supplier"
        ]
        _installSnapshot(monkeypatch, _FakeSnapshot(manifest))

        with caplog.at_level("WARNING"):
            text = await gate.renderSchemaText(
                classes, joins=joins, session=_FakeSession()
            )

        assert text == buildSchemaText(classes, joins=joins)
        assert any("落后于召回子集" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. 不许炸问答
# ---------------------------------------------------------------------------


class Test不许炸问答:
    async def test_快照畸形时降级直读不抛异常(self, switchOn, monkeypatch) -> None:
        """快照内容畸形（classes 里塞了非对象元素）⇒ 适配器判畸形 ⇒ 降级直读。

        注意口径：`classes` 整个**不是列表**时，复原层按「空类清单」处理 ⇒
        报的是**陈旧**而不是畸形（另一条路径，已由上一条测试覆盖）；
        要打到「畸形」这一档，得让 `classes` 是列表但**元素不是对象**。
        """
        classes, properties, joins = _dataset()
        _installSnapshot(
            monkeypatch, _FakeSnapshot({"schemaVersion": "v1", "classes": [123]})
        )

        text = await gate.renderSchemaText(classes, joins=joins, session=_FakeSession())

        assert text == buildSchemaText(classes, joins=joins)

    async def test_会话缺席时确定地直读且一次都不查快照(self, switchOn, monkeypatch) -> None:
        """session 缺席 ⇒ 取不到快照 ⇒ **确定地**直读（不许「试一把再降级」）。

        为什么重要：`getActive(None)` 在**缓存冷**时抛异常、**缓存热**时命中
        ⇒ 同一轮的两次渲染可能一次走快照一次直读 ⇒ **列序不同 ⇒ prompt 不同**
        ⇒ 行为不可复现、问题难定位（安全评审 M-2 / 代码评审 LOW-1）。
        """
        classes, properties, joins = _dataset()
        registry = _installSnapshot(
            monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins))
        )

        text = await gate.renderSchemaText(classes, joins=joins)

        assert text == buildSchemaText(classes, joins=joins)
        assert registry.calls == [], "session 缺席时不该去查快照（会炸或行为不确定）"

    async def test_取快照抛异常时降级直读不抛异常(self, switchOn, monkeypatch) -> None:
        classes, properties, joins = _dataset()

        class _BoomRegistry:
            async def getActive(self, session, *, domainCode: str | None = None):
                raise RuntimeError("库里炸了")

        monkeypatch.setattr(adapterModule, "semanticRegistry", _BoomRegistry())

        text = await gate.renderSchemaText(classes, joins=joins, session=_FakeSession())

        assert text == buildSchemaText(classes, joins=joins)

    async def test_快照不可用时记warning而不是error(self, switchOn, monkeypatch, caplog) -> None:
        """「快照不可用」不是事实缺口 ⇒ 不许占用 **error** 级通道。

        error 级在这套代码里的语义是「**快照与当前本体事实不一致**」（守卫生效，
        降级直读）——那是要人去看的信号。若把「表还没建 / 取快照抛异常」也记成
        error，两类信号混在一起 ⇒ 真正的事实缺口会被淹没。

        （生产未部署 0142 时 `semantic_release` 表不存在 ⇒ 每次渲染都会走这条降级
        ⇒ 误报成 error 会直接淹没告警。）
        """
        classes, properties, joins = _dataset()

        class _BoomRegistry:
            async def getActive(self, session, *, domainCode: str | None = None):
                raise RuntimeError("relation \"semantic_release\" does not exist")

        monkeypatch.setattr(adapterModule, "semanticRegistry", _BoomRegistry())

        with caplog.at_level("WARNING"):
            text = await gate.renderSchemaText(classes, joins=joins, session=_FakeSession())

        assert text == buildSchemaText(classes, joins=joins)
        assert [r for r in caplog.records if r.levelname == "WARNING"], "降级必须留痕"
        assert not [r for r in caplog.records if r.levelname == "ERROR"], (
            "快照不可用不是事实缺口，不许占用 error 级通道"
        )


# ---------------------------------------------------------------------------
# 4. 事实差异的比较口径（守卫的判据本身）
# ---------------------------------------------------------------------------


class Test事实差异口径:
    def test_块内列序不同不算事实差异(self) -> None:
        direct = "### A: table=A\n  x: STRING\n  y: INT"
        snapshot = "### A: table=A\n  y: INT\n  x: STRING"

        assert gate._factDiff(direct, snapshot) == []

    def test_块内少一行算事实差异(self) -> None:
        direct = "### A: table=A\n  x: STRING\n  y: INT"
        snapshot = "### A: table=A\n  x: STRING"

        gaps = gate._factDiff(direct, snapshot)

        assert gaps and "### A: table=A" in gaps[0]

    def test_行在块之间搬家算事实差异(self) -> None:
        """整段文本的行多重集相等，但行换了归属 ⇒ 事实归属变了 ⇒ 算差异。"""
        direct = "### A: table=A\n  x: STRING\n### B: table=B\n  y: INT"
        snapshot = "### A: table=A\n### B: table=B\n  x: STRING\n  y: INT"

        assert gate._factDiff(direct, snapshot)

    def test_多出一个块算事实差异(self) -> None:
        direct = "### A: table=A\n  x: STRING"
        snapshot = "### A: table=A\n  x: STRING\n### B: table=B\n  y: INT"

        assert gate._factDiff(direct, snapshot)

    def test_差异样本被截断不把整段业务描述写进日志(self) -> None:
        """差异摘要里的样本行必须**截断**。

        渲染出来的列行里带 `description`（管理员写入的外部文本，可能很长）。
        不截断 ⇒ 一条差异日志可能带上几百上千字的业务描述 ⇒ 日志被业务文本
        淹没、也可能把不该进日志的内容带出去。
        """
        longText = "业务描述" * 200
        direct = f"### A: table=A\n  x: STRING  # {longText}"
        snapshot = "### A: table=A\n  x: STRING"

        gaps = gate._factDiff(direct, snapshot)

        assert gaps, "这确实是事实差异（少了一行）"
        assert longText not in gaps[0], "差异摘要不许带整段业务描述"
        assert len(gaps[0]) < 200, f"差异摘要过长：{len(gaps[0])}"


# ---------------------------------------------------------------------------
# 5. 接线点：三个 schema 文本出口都走闸门
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

    def systemPrompts(self) -> list[str]:
        return [c["messages"][0].content for c in self.calls]


def _llmConfig():
    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _planJson() -> str:
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


class _ClarifyHarness:
    """`_handleClarify` 的最小宿主：只提供该方法真正用到的几个协作者。

    刻意**不**用真 ChatService（那是 HTTP 层 + 会话 + 用量落库的整套机器）——
    本用例要证明的只有一件事：**schema 文本是不是从闸门出来的**。
    """

    def __init__(self, nl2sql, prompts: list[list]) -> None:
        from app.services.chat_domain import DomainCommandMixin

        self._mixin = DomainCommandMixin
        self._nl2sql = nl2sql
        self._prompts = prompts
        self._selected = SimpleNamespace(model_name="test-model", id=1)

    async def _callWithFallback(self, session, sessionId, configs, selected, purpose, fn):
        resp = await fn(selected)
        return resp, selected, (0, 0)

    def _llmFactory(self, cfg):
        return SimpleNamespace(complete=self._complete)

    async def _complete(self, messages, model=None):
        self._prompts.append(messages)
        return _Resp("解释")

    def _costFor(self, cfg, promptTokens, completionTokens) -> float:
        return 0.0

    async def _recordUsage(self, *args, **kwargs) -> None:
        return None

    async def _storeSessionMessages(self, *args, **kwargs) -> None:
        return None

    async def _buildAffinityStatus(self, *args, **kwargs):
        return None

    async def _handleClarify(self, session, dto, pc):
        return await self._mixin._handleClarify(self, session, dto, pc)


class Test接线点:
    async def test_generateSql走闸门_快照新鲜时prompt列序按快照(
        self, switchOn, monkeypatch
    ) -> None:
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))
        fake = _ScriptedLlm([_Resp("```sql\nSELECT SUPPLIER_CODE FROM SUPPLIER\n```")])

        await Nl2SqlService().generateSql(
            "问题", classes, fake, _llmConfig(), joins=joins, session=_FakeSession()
        )

        systemPrompt = fake.systemPrompts()[0]
        assert _columnOrder(systemPrompt) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]

    async def test_generateSql走闸门_开关关闭时与今天逐字节一致(
        self, switchOff, monkeypatch
    ) -> None:
        """开关关时，`generateSql` 送进 prompt 的 schema 文本必须等于
        **今天的直读渲染**（`buildSchemaText` 显式参照），而不只是「两次跑一样」。

        上一版把「两次开关关的运行」互相比 ⇒ 只能测出**不确定性**
        （代码评审 MEDIUM-3：假绿）。⇒ 这里必须与**显式参照**比。
        """
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))
        fake = _ScriptedLlm([_Resp("```sql\nSELECT SUPPLIER_CODE FROM SUPPLIER\n```")])

        await Nl2SqlService().generateSql(
            "问题", classes, fake, _llmConfig(), joins=joins, session=_FakeSession()
        )

        systemPrompt = fake.systemPrompts()[0]
        reference = buildSchemaText(classes, joins=joins)
        assert reference in systemPrompt
        assert systemPrompt.count(reference) == 1

    async def test_generateQueryPlan走闸门_快照新鲜时prompt列序按快照(
        self, switchOn, monkeypatch
    ) -> None:
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))
        fake = _ScriptedLlm([_Resp(_planJson())])

        await Nl2SqlService().generateQueryPlan(
            "问题", classes, fake, _llmConfig(), joins=joins, session=_FakeSession()
        )

        systemPrompt = fake.systemPrompts()[0]
        assert _columnOrder(systemPrompt) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]

    async def test_clarify走闸门_快照新鲜时prompt列序按快照(
        self, switchOn, monkeypatch
    ) -> None:
        """CLARIFY（概念解释）是第三个 schema 文本出口，同样必须走闸门。"""
        from app.services.nl2sql_service import Nl2SqlService

        classes, properties, joins = _dataset()
        _installSnapshot(monkeypatch, _FakeSnapshot(_manifest(classes, properties, joins)))
        prompts: list[list] = []
        harness = _ClarifyHarness(Nl2SqlService(), prompts)
        dto = SimpleNamespace(sessionId="s-1", question="供应商是什么")
        pc = SimpleNamespace(
            classes=classes,
            joins=joins,
            configs=[],
            selected=SimpleNamespace(model_name="test-model", id=1),
        )

        await harness._handleClarify(_FakeSession(), dto, pc)

        userPrompt = prompts[0][1].content
        assert _columnOrder(userPrompt) == ["SUPPLIER_CODE", "SUPPLIER_NAME"]


# ---------------------------------------------------------------------------
# 6. 生产侧不许绕过闸门
# ---------------------------------------------------------------------------


class Test生产侧不许绕过闸门:
    """闸门只有在**所有** schema 文本出口都走它时才有意义。

    只要还留着一个「直接调 `buildSchemaText`」的后门，影子接入就是**半生效**的
    （开关打开时有的问答用快照、有的用直读 ⇒ 口径不一致 ⇒ 对比结论不可信）。
    ⇒ 用一条**结构性守卫**把后门焊死。
    """

    # 允许直接持有渲染器的模块：
    # - semantic_schema_gate.py：闸门本体（唯一出口）
    # - semantic_schema_shadow_diff.py：D14 的**离线影子比对工具**（不在请求路径上，
    #   它的职责就是同时渲染两侧）
    _ALLOWED_DIRECT_USERS = {
        "semantic_schema_gate.py",
        "semantic_schema_shadow_diff.py",
    }

    def test_服务层只有闸门直接调用渲染器(self) -> None:
        import pathlib

        import app.services as servicesPkg

        servicesDir = pathlib.Path(servicesPkg.__file__).parent
        offenders: list[str] = []
        for path in sorted(servicesDir.rglob("*.py")):
            if path.name in self._ALLOWED_DIRECT_USERS:
                continue
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 只看真正的「导入」与「调用」；文档字符串里的提及不算
                if "buildSchemaText" not in line:
                    continue
                if "buildSchemaText(" in line and "def buildSchemaText" not in line:
                    offenders.append(f"{path.name}:{no}: {stripped}")
                    continue
                if stripped.startswith(("from ", "import ")) and "buildSchemaText" in line:
                    offenders.append(f"{path.name}:{no}: {stripped}")

        assert offenders == [], (
            "服务层出现了绕过语义快照闸门的 schema 文本渲染点（后门会让影子接入半生效）：\n"
            + "\n".join(offenders)
        )

    def test_Nl2SqlService不再暴露buildSchemaText后门(self) -> None:
        from app.services.nl2sql_service import Nl2SqlService

        assert not hasattr(Nl2SqlService, "buildSchemaText"), (
            "Nl2SqlService.buildSchemaText 是绕过闸门的公开后门 ⇒ 必须删掉，"
            "调用方一律走 renderSchemaText"
        )
