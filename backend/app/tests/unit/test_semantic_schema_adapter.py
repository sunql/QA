"""P2：快照 → schema 文本渲染视图的复原层（D12）。

本文件钉死三件必须成立的事：

1. **字段映射**：camelCase 快照 → snake_case 视图，一个字段不丢、一个字段不串；
2. **合成 id 在渲染子集内自洽**：parent / ref / join 两端能互相对上，
   且「引用的类不在子集内」时行为与 ORM 路径一致（查不到 ⇒ 不渲染 FK 目标表）；
3. **复原视图能被 `nl2sql_schema.buildSchemaText` 原样消费** ——
   该函数探 `cls.__dict__` / `prop.__dict__`（惰性关系探测），
   用 `slots=True` 的 dataclass 会抛 `AttributeError: __dict__`。

第 3 条是最容易被"写完就绿、线上炸"的地方：视图对象必须带实例 `__dict__`。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.semantic_registry_service import SemanticReleaseIntegrityError
from app.services.semantic_schema_adapter import (
    SemanticSnapshotMalformedError,
    SemanticSnapshotStaleError,
    loadClassesForSchema,
    restoreSnapshotSchema,
)


class _FakeSnapshot:
    """Registry 快照替身：只带适配器真正用到的几个字段。"""

    def __init__(
        self, manifest: dict, version: str = "v-test", schema_version: str | None = None,
        releaseId: int = 1,
    ) -> None:
        self.manifest = manifest
        self.version = version
        self.schema_version = schema_version or manifest.get("schemaVersion")
        # 真实 `SemanticReleaseSnapshot.id` 是主键，必有 ⇒ 替身必须带
        # （缺了它，适配器会按「契约违规」降级直读，测试就测不到快照路径了）。
        self.id = releaseId


class _FakeRegistry:
    def __init__(self, snapshot: _FakeSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[str | None] = []

    async def getActive(self, session, *, domainCode: str | None = None):
        self.calls.append(domainCode)
        return self._snapshot


def _prop(
    name: str,
    *,
    alias: str | None = None,
    business_aliases: list[str] | None = None,
    description: str | None = None,
    data_type: str = "STRING",
    is_pk: bool = False,
    is_fk: bool = False,
    ref_class_name: str | None = None,
    source_column: str | None = None,
) -> dict:
    return {
        "propertyName": name,
        "propertyAlias": alias,
        "businessAliases": business_aliases or [],
        "description": description,
        "dataType": data_type,
        "isPrimaryKey": is_pk,
        "isForeignKey": is_fk,
        "refClassName": ref_class_name,
        "sourceColumn": source_column,
        "unit": None,
        "allowedValues": [],
        "isNotNull": None,
        "minValue": None,
        "maxValue": None,
        "regexPattern": None,
    }


def _manifest() -> dict:
    """三个类：Supplier（被继承）、PurchaseOrder（继承 Supplier）、Material。"""
    return {
        "schemaVersion": "v1",  # 与 SCHEMA_VERSION 一致
        "domains": [],
        "classes": [
            {
                "className": "Supplier",
                "classAlias": "供应商",
                "description": "供应商主数据",
                "sourceTable": "SUPPLIER",
                "objectType": None,
                "objectOwner": None,
                "parentClassName": None,
                "properties": [
                    _prop("SUPPLIER_CODE", is_pk=True, source_column="SUPPLIER_CODE"),
                    _prop(
                        "SUPPLIER_NAME",
                        alias="供应商名称",
                        business_aliases=["供方名称"],
                        description="供方全称",
                        source_column="SUPPLIER_NAME",
                    ),
                ],
            },
            {
                "className": "PurchaseOrder",
                "classAlias": "采购订单",
                "description": None,
                "sourceTable": "PURCHASE_ORDER",
                "objectType": None,
                "objectOwner": None,
                "parentClassName": "Supplier",
                "properties": [
                    _prop("ORDER_CODE", is_pk=True, source_column="ORDER_CODE"),
                    _prop(
                        "SUPPLIER_CODE",
                        is_fk=True,
                        ref_class_name="Supplier",
                        source_column="SUPPLIER_CODE",
                    ),
                ],
            },
            {
                "className": "Material",
                "classAlias": None,
                "description": None,
                "sourceTable": "MATERIAL",
                "objectType": None,
                "objectOwner": None,
                "parentClassName": None,
                "properties": [
                    _prop("MATERIAL_CODE", is_pk=True, source_column="MATERIAL_CODE"),
                ],
            },
        ],
        "joins": [
            {
                "joinKey": "PURCHASE_ORDER_SUPPLIER",
                "sourceClassName": "PurchaseOrder",
                "sourceColumns": ["SUPPLIER_CODE"],
                "targetClassName": "Supplier",
                "targetColumns": ["SUPPLIER_CODE"],
                "joinType": "LEFT",
                "relationType": "foreign_key",
                "description": "订单挂供方",
            }
        ],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


class Test字段映射:
    def test_类字段按camelCase映射到snake_case(self) -> None:
        classes, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        cls = classes[0]
        assert cls.class_name == "Supplier"
        assert cls.class_alias == "供应商"
        assert cls.source_table == "SUPPLIER"

    def test_属性字段按camelCase映射到snake_case(self) -> None:
        classes, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        props = classes[0].properties
        by_name = {p.property_name: p for p in props}
        assert by_name["SUPPLIER_CODE"].is_primary_key is True
        assert by_name["SUPPLIER_CODE"].is_foreign_key is False
        assert by_name["SUPPLIER_CODE"].source_column == "SUPPLIER_CODE"
        assert by_name["SUPPLIER_NAME"].property_alias == "供应商名称"
        assert by_name["SUPPLIER_NAME"].business_aliases == ("供方名称",)
        assert by_name["SUPPLIER_NAME"].description == "供方全称"

    def test_空business_aliases归一成空列表而不是None(self) -> None:
        classes, _ = restoreSnapshotSchema(_manifest(), ["Material"])
        prop = classes[0].properties[0]
        assert prop.business_aliases == ()

    def test_类顺序与调用方给定的顺序一致(self) -> None:
        classes, _ = restoreSnapshotSchema(
            _manifest(), ["Material", "Supplier", "PurchaseOrder"]
        )
        assert [c.class_name for c in classes] == [
            "Material",
            "Supplier",
            "PurchaseOrder",
        ]


class Test合成id:
    def test_id在子集内唯一(self) -> None:
        classes, _ = restoreSnapshotSchema(
            _manifest(), ["Supplier", "PurchaseOrder", "Material"]
        )
        ids = [c.id for c in classes]
        assert len(ids) == len(set(ids))

    def test_同一份manifest两次复原得到同样的id(self) -> None:
        first, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        second, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        assert first[0].id == second[0].id

    def test_父类引用解析到父类的合成id(self) -> None:
        classes, _ = restoreSnapshotSchema(
            _manifest(), ["Supplier", "PurchaseOrder"]
        )
        by_name = {c.class_name: c for c in classes}
        assert by_name["PurchaseOrder"].parent_class_id == by_name["Supplier"].id

    def test_外键引用解析到目标类的合成id(self) -> None:
        classes, _ = restoreSnapshotSchema(
            _manifest(), ["Supplier", "PurchaseOrder"]
        )
        by_name = {c.class_name: c for c in classes}
        order = by_name["PurchaseOrder"]
        fk = next(p for p in order.properties if p.property_name == "SUPPLIER_CODE")
        assert fk.ref_class_id == by_name["Supplier"].id

    def test_引用的类不在渲染子集内时取None(self) -> None:
        """与 ORM 路径同口径：classesById 只在传入子集里查，查不到就是查不到。"""
        classes, _ = restoreSnapshotSchema(_manifest(), ["PurchaseOrder"])
        order = classes[0]
        assert order.parent_class_id is None
        fk = next(p for p in order.properties if p.property_name == "SUPPLIER_CODE")
        assert fk.ref_class_id is None

    def test_join两端解析到类的合成id(self) -> None:
        classes, joins = restoreSnapshotSchema(
            _manifest(), ["Supplier", "PurchaseOrder"]
        )
        by_name = {c.class_name: c for c in classes}
        join = joins[0]
        assert join.source_class_id == by_name["PurchaseOrder"].id
        assert join.target_class_id == by_name["Supplier"].id
        assert join.source_columns == ("SUPPLIER_CODE",)
        assert join.target_columns == ("SUPPLIER_CODE",)
        assert join.description == "订单挂供方"

    def test_join一端不在渲染子集内时该端为None(self) -> None:
        classes, joins = restoreSnapshotSchema(_manifest(), ["Supplier"])
        join = joins[0]
        assert join.source_class_id is None
        assert join.target_class_id == classes[0].id


class Test陈旧快照:
    def test_子集里的类不在快照中时抛领域异常(self) -> None:
        with pytest.raises(SemanticSnapshotStaleError) as exc:
            restoreSnapshotSchema(_manifest(), ["Supplier", "NotInSnapshot"])
        assert "NotInSnapshot" in str(exc.value)

    def test_快照里多出来的类不进入渲染子集(self) -> None:
        classes, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        assert [c.class_name for c in classes] == ["Supplier"]


class Test渲染器可消费:
    def test_复原视图带实例dict(self) -> None:
        """buildSchemaText 探 cls.__dict__ / prop.__dict__（惰性关系探测）。"""
        classes, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        cls = classes[0]
        assert isinstance(cls.__dict__, dict)
        assert isinstance(cls.properties[0].__dict__, dict)
        assert "parent" not in cls.__dict__
        assert "ref_class" not in cls.properties[0].__dict__


class _OrmStub:
    """治理表对象替身：适配器只读 `class_name`。"""

    def __init__(self, class_name: str) -> None:
        self.class_name = class_name


class Test真实渲染器可消费:
    def test_真实渲染器能消费复原视图(self) -> None:
        """复原视图直接喂 `buildSchemaText`：继承 / FK / JOIN 三条线都要出得来。

        这是本层最容易被「写完就绿、线上炸」的地方：渲染器探
        `cls.__dict__["parent"]` 与 `prop.__dict__["ref_class"]`，
        用 `slots=True` 的 dataclass 会抛 `AttributeError: __dict__`。
        """
        from app.services.nl2sql_schema import buildSchemaText

        classes, joins = restoreSnapshotSchema(_manifest(), ["Supplier", "PurchaseOrder"])
        text = buildSchemaText(classes, joins=joins)

        assert "### Supplier (供应商): table=SUPPLIER" in text
        assert "### PurchaseOrder (采购订单) (继承 Supplier): table=PURCHASE_ORDER" in text
        assert "SUPPLIER_CODE: STRING [FK → SUPPLIER]" in text
        assert "PURCHASE_ORDER.SUPPLIER_CODE → SUPPLIER.SUPPLIER_CODE" in text

    def test_子集外的引用不渲染目标表(self) -> None:
        """ORM 路径：目标类不在召回子集 ⇒ FK 只出列名，不出目标表。复原视图同口径。"""
        from app.services.nl2sql_schema import buildSchemaText

        classes, joins = restoreSnapshotSchema(_manifest(), ["PurchaseOrder"])
        text = buildSchemaText(classes, joins=joins)

        assert "SUPPLIER_CODE: STRING [FK" in text
        assert "FK → SUPPLIER" not in text
        assert "继承 Supplier" not in text
        assert "### JOIN 关系" not in text


class Test取类闸门:
    """`loadClassesForSchema`：开关关 / 无发布 ⇒ 逐字节回退到今天的直读路径。"""

    async def test_开关关闭时原样返回治理表对象(self) -> None:
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(_manifest()))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=False, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins
        assert registry.calls == []  # 开关关 ⇒ 连库都不碰

    async def test_无激活发布时原样返回治理表对象(self) -> None:
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(None)
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins
        assert registry.calls == [None]

    async def test_无激活发布时记info让影子未生效可观测(self, caplog) -> None:
        """没有 ACTIVE 快照 ⇒ 影子**没生效** ⇒ 这件事必须可观测。

        静默直读 ⇒ 现场只会看到「开关开了但口径没变」，无从判断是配置没生效、
        还是发布还没做、还是域作用域发布没被查到。
        """
        classes = [_OrmStub("Supplier")]
        registry = _FakeRegistry(None)

        with caplog.at_level("INFO"):
            await loadClassesForSchema(
                classes, None, session=None, enabled=True, registry=registry
            )

        assert any(
            "没有" in r.getMessage() and "快照" in r.getMessage()
            for r in caplog.records
            if r.levelname == "INFO"
        ), "无 ACTIVE 快照时必须记 info，说明影子未生效"

    async def test_有激活发布时返回复原视图(self) -> None:
        classes = [_OrmStub("Supplier"), _OrmStub("PurchaseOrder")]
        registry = _FakeRegistry(_FakeSnapshot(_manifest()))
        restored, _ = await loadClassesForSchema(
            classes, None, session=None, enabled=True, registry=registry
        )
        assert [c.class_name for c in restored] == ["Supplier", "PurchaseOrder"]
        assert restored[0] is not classes[0]

    async def test_快照陈旧时降级回直读治理表(self) -> None:
        """快照落后于治理表是发布节奏的常态，不该让问答整体失败。"""
        classes = [_OrmStub("Supplier"), _OrmStub("新增类")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(_manifest()))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins

    async def test_域编码透传给Registry(self) -> None:
        registry = _FakeRegistry(None)
        await loadClassesForSchema(
            [], None, session=None, enabled=True, domainCode="PROCUREMENT",
            registry=registry,
        )
        assert registry.calls == ["PROCUREMENT"]

    async def test_调用方没传join时也不替换成快照的join(self) -> None:
        """调用方传 `joins=None` = 不要 JOIN 段；快照侧不能擅自塞一份进去。"""
        classes = [_OrmStub("Supplier")]
        registry = _FakeRegistry(_FakeSnapshot(_manifest()))
        restored, restoredJoins = await loadClassesForSchema(
            classes, None, session=None, enabled=True, registry=registry
        )
        assert restoredJoins is None


class Test快照内容闸门:
    """快照**内容**不合法 ⇒ 不能当作事实用（宁可降级直读，也不要错的事实）。

    编译器在发布时就会拒绝「同名多版本活跃类」，发布路径还有 JSON Schema +
    `manifest_hash` 完整性两道闸 ⇒ 这些形态在正常发布下不可能出现。
    但快照也可能**绕过发布路径**被写进库（直接改表），此时内容就可能不合法。

    最危险的不是报错，而是**静默翻转事实**：`bool("false")` 在 Python 里是
    `True` ⇒ 一个非主键被当成主键渲染进 schema 文本 ⇒ LLM 照着错的元数据写 SQL。
    """

    def test_布尔字段写成字符串时不被Python语义翻转(self) -> None:
        """`"isPrimaryKey": "false"` 不是 `False`，是**非法数据**。"""
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["isPrimaryKey"] = "false"
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_字符串布尔值同样判为非法(self) -> None:
        """契约由 JSON Schema 定死（布尔字段就是布尔），复原层不发明第二套方言。"""
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["isPrimaryKey"] = "TRUE"
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_别名列表不是列表时判为非法(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"][1]["businessAliases"] = "供应商,供方"
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_清单里同名类出现两次时不静默丢弃(self) -> None:
        manifest = _manifest()
        duplicate = dict(manifest["classes"][0])
        manifest["classes"].append(duplicate)
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_属性清单不是列表时判为非法(self) -> None:
        """`"properties": ["SUPPLIER_CODE"]` ⇒ 不是对象数组 ⇒ 畸形。

        旧实现让 `rawProperties[index].get(...)` 自己炸 ⇒ `AttributeError`
        穿透到问答路径 = 500。这一层的全部意义就是拦住它。
        """
        manifest = _manifest()
        manifest["classes"][0]["properties"] = ["SUPPLIER_CODE"]
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_属性清单是对象时判为非法(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"] = {"SUPPLIER_CODE": {}}
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_属性元素不是对象时判为非法(self) -> None:
        """跳过非法元素 = 静默丢一条属性 ⇒ 模型照着残缺元数据写 SQL。"""
        manifest = _manifest()
        manifest["classes"][0]["properties"] = ["SUPPLIER_CODE", {"propertyName": "X"}]
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_join列清单不是列表时判为非法(self) -> None:
        """`list("ABC")` = ['A','B','C'] ⇒ 列名被逐字符拆开喂给模型。"""
        manifest = _manifest()
        manifest["joins"][0]["sourceColumns"] = "SUPPLIER_CODE"
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])

    def test_可空字符串字段不是字符串时判为非法(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["classAlias"] = {"zh": "供应商"}
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_陈旧优先于畸形(self) -> None:
        """既缺类又畸形 ⇒ 先报「陈旧」（发布节奏常态），别把排查方向带偏。"""
        manifest = _manifest()
        manifest["classes"][0]["properties"] = ["SUPPLIER_CODE"]
        with pytest.raises(SemanticSnapshotStaleError):
            restoreSnapshotSchema(manifest, ["Supplier", "不存在的类"])

    async def test_内容非法时降级回直读治理表(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["isPrimaryKey"] = "false"
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(manifest))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins


class Test快照版本闸门:
    """快照的 schema 版本与代码不一致 ⇒ 降级直读，不静默丢事实。

    演化规则（`app/domain/semantic_manifest_schema.py:15`）：改字段名 / 改类型
    ⇒ `SCHEMA_VERSION` +1。库里若还挂着一份 v1 的 ACTIVE 快照而代码已是 v2，
    复原层会把新字段读成空（继承没了、FK 目标没了、描述没了）——
    **每一项事实都静默消失，且不报错不告警**。必须拦住。
    """

    async def test_快照版本与代码不一致时降级回直读(self) -> None:
        manifest = _manifest()
        manifest["schemaVersion"] = "v0"
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(manifest))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins

    async def test_行上的版本与代码不一致时也降级(self) -> None:
        """行里的 `schema_version` 与 manifest 正文不一致同样要拦（绕过发布路径写入）。"""
        manifest = _manifest()
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(manifest, schema_version="v0"))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins

    async def test_版本一致时正常走快照(self) -> None:
        classes = [_OrmStub("Supplier")]
        registry = _FakeRegistry(_FakeSnapshot(_manifest()))
        restored, _ = await loadClassesForSchema(
            classes, None, session=None, enabled=True, registry=registry
        )
        assert restored is not classes
        assert restored[0].class_name == "Supplier"

    async def test_快照缺版本号时也降级回直读(self) -> None:
        """manifest 的 `schemaVersion` 是必填项 ⇒ 缺它只可能是坏数据。"""
        manifest = _manifest()
        del manifest["schemaVersion"]
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(manifest))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins


class Test快照完整性闸门:
    """库里那行 ACTIVE 快照**被绕过发布路径改过** ⇒ 降级直读，不许 500。

    `SemanticReleaseIntegrityError` 由 `semantic_registry_service._toSnapshot`
    抛出（哈希对不上 / 体量超限 / 无法规范化序列化）。
    """

    async def test_完整性校验失败时降级回直读治理表(self) -> None:
        class _BrokenRegistry:
            async def getActive(self, session, *, domainCode: str | None = None):
                raise SemanticReleaseIntegrityError("快照内容与 manifest_hash 不一致")

        classes = [_OrmStub("Supplier")]
        joins = [object()]
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=_BrokenRegistry()
        )
        assert restored is classes
        assert restoredJoins is joins


class Test复原视图不可变:
    """复原视图会被多个消费方共享 ⇒ 原地改一个会污染其他（项目硬约束）。"""

    def test_复原视图字段不可原地修改(self) -> None:
        restored, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        with pytest.raises(dataclasses.FrozenInstanceError):
            restored[0].class_alias = "改掉了"  # type: ignore[misc]

    def test_复原视图仍带实例dict(self) -> None:
        """不可变（frozen）不能牺牲渲染器的 `__dict__` 探针。"""
        restored, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        assert "parent" not in restored[0].__dict__
        assert "ref_class" not in restored[0].properties[0].__dict__

    def test_集合字段是元组而不是列表(self) -> None:
        """frozen dataclass 只冻结**字段绑定**，不冻结字段里的 list。

        `business_aliases: list[str]` 放在 frozen dataclass 里 ⇒
        `prop.business_aliases.append("x")` 照样成功 ⇒ 「不可变视图」名不副实，
        而这份视图会被多个消费方共享（渲染 / JOIN 图 / 关键列筛选）。
        """
        restored, joins = restoreSnapshotSchema(_manifest(), ["Supplier"])
        prop = restored[0].properties[1]
        assert isinstance(prop.business_aliases, tuple)
        assert isinstance(prop.allowed_values, tuple)
        assert isinstance(restored[0].properties, tuple)
        assert isinstance(joins[0].source_columns, tuple)
        assert isinstance(joins[0].target_columns, tuple)

    def test_元组字段无法被原地修改(self) -> None:
        restored, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        prop = restored[0].properties[1]
        with pytest.raises(AttributeError):
            prop.business_aliases.append("偷偷加的")  # type: ignore[attr-defined]

    def test_类属性集合无法被原地修改(self) -> None:
        restored, _ = restoreSnapshotSchema(_manifest(), ["Supplier"])
        with pytest.raises(AttributeError):
            restored[0].properties.clear()  # type: ignore[attr-defined]


class Test合成id不自洽时判畸形:
    """取不到合成属性 id ⇒ 判畸形，**不给**一个兜底的 `index + 1`。

    兜底 id 会与其它快照不一致 ⇒ 影子比对的「id 一致」判据假绿，
    差异被吞掉（看起来完全一致，其实不是同一份 id 映射）。
    """

    def test_取不到合成属性id时抛畸形异常(self) -> None:
        from app.services.semantic_schema_adapter import _propertyIdAt

        with pytest.raises(SemanticSnapshotMalformedError):
            _propertyIdAt({}, "Supplier", 0)

    def test_取不到时异常里点名了类名(self) -> None:
        from app.services.semantic_schema_adapter import _propertyIdAt

        with pytest.raises(SemanticSnapshotMalformedError) as exc:
            _propertyIdAt({}, "Supplier", 3)
        assert "Supplier" in str(exc.value)


class Test严格取值:
    """快照字段**不做隐式强转** —— 契约外的东西一律判畸形、降级直读。

    为什么：`str(123)` = "123" 看着无害，但 `str(None)` = "" 会让
    「属性名 / 数据类型」静默变成空串 ⇒ schema 文本少一列 ⇒ 模型照着残缺的
    元数据写 SQL，**全程无报错**。宁可整份快照判畸形。
    """

    def test_字符串列表里有非字符串元素时判畸形(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["businessAliases"] = ["别名", 7]
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_属性名不是字符串时判畸形(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["propertyName"] = 123
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_属性名缺失时判畸形(self) -> None:
        manifest = _manifest()
        del manifest["classes"][0]["properties"][0]["propertyName"]
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_数据类型不是字符串时判畸形(self) -> None:
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["dataType"] = 123
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_类名缺失时判畸形(self) -> None:
        """类名缺失的类被静默跳过 = 静默丢掉一整个类（还可能正是召回要的那个）。"""
        manifest = _manifest()
        del manifest["classes"][0]["className"]
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_类清单里有非对象元素时判畸形(self) -> None:
        manifest = _manifest()
        manifest["classes"].append("Material")
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_join类型不是字符串时判畸形(self) -> None:
        manifest = _manifest()
        manifest["joins"][0]["joinType"] = 1
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    def test_join键不是字符串时判畸形(self) -> None:
        manifest = _manifest()
        manifest["joins"][0]["joinKey"] = 1
        with pytest.raises(SemanticSnapshotMalformedError):
            restoreSnapshotSchema(manifest, ["Supplier"])

    async def test_畸形快照降级回直读治理表(self) -> None:
        """畸形不是 500：问答路径必须还能走（直读治理表）。"""
        manifest = _manifest()
        manifest["classes"][0]["properties"][0]["dataType"] = 123
        classes = [_OrmStub("Supplier")]
        joins = [object()]
        registry = _FakeRegistry(_FakeSnapshot(manifest))
        restored, restoredJoins = await loadClassesForSchema(
            classes, joins, session=None, enabled=True, registry=registry
        )
        assert restored is classes
        assert restoredJoins is joins


class Test顺序确定性:
    """D14 的顺序判据之一：同一份快照，复原出来的顺序**必须可复现**。

    为什么这条比「与直读侧顺序一致」更重要：直读侧的列序来自
    `OntologyClass.properties` 关系（**没有 ORDER BY** ⇒ SQL 层面不保证），
    JOIN 顺序来自自增 id（插入序 ⇒ 跨环境不同）⇒ 直读侧的顺序**本身不是契约**。
    快照侧必须自己给出**确定的**顺序，否则同一份发布在不同机器上会渲染出
    不同的 prompt ⇒ 发布快照的字节确定性就白做了。
    """

    def test_同一份manifest两次复原渲染出逐字节相同的文本(self) -> None:
        from app.services.nl2sql_schema import buildSchemaText

        names = ["Supplier", "PurchaseOrder", "Material"]
        firstClasses, firstJoins = restoreSnapshotSchema(_manifest(), names)
        secondClasses, secondJoins = restoreSnapshotSchema(_manifest(), names)

        firstText = buildSchemaText(firstClasses, joins=firstJoins)
        secondText = buildSchemaText(secondClasses, joins=secondJoins)

        assert firstText == secondText
        assert firstText.encode("utf-8") == secondText.encode("utf-8")

    def test_列顺序忠实于manifest里属性的顺序(self) -> None:
        """复原层**不额外排序**：列顺序 = manifest 里该类 properties 的顺序。

        为什么钉这条：快照侧的列序若由复原层自己排（而不是由编译器排），
        排序键就换成了「复原层看得见的东西」—— 一旦复原层引入了新字段
        （比如改按 sourceColumn 排），列序就会在**不重新发布**的情况下改变
        ⇒ 已发布的快照不再是稳定契约。

        「编译器按属性名排序」那一半由 P1a 的 golden manifest 反向守卫负责
        （`test_semantic_manifest_golden.py`）。
        """
        from app.services.nl2sql_schema import buildSchemaText

        manifest = _manifest()
        supplier = manifest["classes"][0]
        assert [p["propertyName"] for p in supplier["properties"]] == [
            "SUPPLIER_CODE",
            "SUPPLIER_NAME",
        ]
        # 手工把属性倒序 —— 复原层不该把它排回来。
        supplier["properties"] = list(reversed(supplier["properties"]))

        classes, joins = restoreSnapshotSchema(manifest, ["Supplier"])
        text = buildSchemaText(classes, joins=joins)

        assert text.index("SUPPLIER_NAME") < text.index("SUPPLIER_CODE")
