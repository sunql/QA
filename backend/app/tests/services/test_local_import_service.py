"""LocalImportService 单元测试。

使用内存 SQLite + Fake 依赖服务验证端到端流程与错误处理。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.exceptions import NotFoundError
from app.domain.models import DataSource
from app.domain.schemas import (
    ColumnSchemaRead,
    ConflictResolution,
    ForeignKeySchemaRead,
    ImportExecuteRequest,
    ImportPreviewRequest,
    ImportRuleConfig,
    SchemaIntrospectResponse,
    TableSchemaRead,
)
from app.services.local_import_service import LocalImportService


class FakeSchemaService:
    async def introspectAndCache(self, session, ds):
        return None

    def buildResponse(self, cache):
        return SchemaIntrospectResponse(
            tables=[
                TableSchemaRead(
                    table_name="wms_inventory",
                    columns=[
                        ColumnSchemaRead(
                            column_name="quantity",
                            data_type="DECIMAL",
                            nullable=True,
                        )
                    ],
                    primary_keys=[],
                    foreign_keys=[],
                )
            ],
            cached_at=datetime(2026, 8, 29, 0, 0, 0),
        )


class FakeOntologyService:
    def __init__(self):
        self._next_id = 0
        self._classes = []
        self._properties = []
        self._joins = []

    async def listClasses(self, session, includeExpired=False):
        return list(self._classes)

    async def listPropertiesByClass(self, session, classId):
        return [p for p in self._properties if p.class_id == classId]

    async def createClass(self, session, dto):
        from app.domain.models import OntologyClass

        self._next_id += 1
        cls = OntologyClass(
            id=self._next_id,
            class_name=dto.class_name,
            source_table=dto.source_table,
        )
        self._classes.append(cls)
        return cls

    async def createProperty(self, session, dto):
        from app.domain.models import OntologyProperty

        self._next_id += 1
        prop = OntologyProperty(
            id=self._next_id,
            class_id=dto.class_id,
            property_name=dto.property_name,
            source_column=dto.source_column,
        )
        self._properties.append(prop)
        return prop

    async def createJoin(self, session, dto):
        from app.domain.models import OntologyJoin

        self._next_id += 1
        join = OntologyJoin(id=self._next_id)
        self._joins.append(join)
        return join


async def _seed_datasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name="test-ds",
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="test",
        username="test",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.mark.asyncio
async def test_build_preview_returns_proposed_class(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    request = ImportPreviewRequest(rules=ImportRuleConfig())

    result = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=request.rules
    )

    assert len(result.proposed_classes) == 1
    assert result.proposed_classes[0].source_table == "wms_inventory"
    assert result.proposed_classes[0].class_name == "wms_inventory"
    assert len(result.proposed_classes[0].properties) == 1
    assert result.proposed_classes[0].properties[0].source_column == "quantity"
    assert result.proposed_classes[0].properties[0].data_type == "DECIMAL"


@pytest.mark.asyncio
async def test_execute_import_creates_class_and_property(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    execute_request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=execute_request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 1
    assert result.created_properties == 1
    assert result.success is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_build_preview_raises_not_found_for_missing_datasource(
    dbSession: AsyncSession,
):
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    with pytest.raises(NotFoundError):
        await svc.build_preview(
            dbSession, datasource_id=999, rules=ImportRuleConfig()
        )


@pytest.mark.asyncio
async def test_build_preview_includes_proposed_joins(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    ),
                    ColumnSchemaRead(
                        column_name="customer_id",
                        data_type="INT",
                        nullable=False,
                    ),
                ],
                primary_keys=["id"],
                foreign_keys=[
                    ForeignKeySchemaRead(
                        column_name="customer_id",
                        ref_table="customers",
                        ref_column="id",
                    )
                ],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    )
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )
    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=FakeOntologyService(),
    )

    result = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )

    assert len(result.proposed_classes) == 2
    joins = [j for j in result.proposed_joins if j.source_table == "orders"]
    assert len(joins) == 1
    assert joins[0].target_table == "customers"
    assert joins[0].source_columns == ["customer_id"]
    assert joins[0].target_columns == ["id"]


@pytest.mark.asyncio
async def test_build_preview_filters_by_selected_tables_whitelist(dbSession: AsyncSession):
    """selected_tables 白名单收窄预览：仅保留命中的类；join 只保留两端都在白名单内的。"""
    ds = await _seed_datasource(dbSession)
    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="order_items",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False),
                    ColumnSchemaRead(column_name="order_id", data_type="INT", nullable=False),
                    ColumnSchemaRead(column_name="supplier_id", data_type="INT", nullable=False),
                ],
                primary_keys=["id"],
                foreign_keys=[
                    ForeignKeySchemaRead(
                        column_name="order_id", ref_table="orders", ref_column="id"
                    ),
                    ForeignKeySchemaRead(
                        column_name="supplier_id", ref_table="suppliers", ref_column="id"
                    ),
                ],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )
    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=FakeOntologyService(),
    )

    result = await svc.build_preview(
        dbSession,
        datasource_id=ds.id,
        rules=ImportRuleConfig(),
        selected_tables=["orders", "order_items"],
    )

    assert [c.source_table for c in result.proposed_classes] == [
        "orders",
        "order_items",
    ]
    # order_items->suppliers 的 target 不在白名单内，join 一并丢弃
    assert [j.target_table for j in result.proposed_joins] == ["orders"]


@pytest.mark.asyncio
async def test_build_preview_whitelist_none_or_empty_keeps_all(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )
    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=FakeOntologyService(),
    )

    result = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig(), selected_tables=None
    )
    assert len(result.proposed_classes) == 2

    result_empty = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig(), selected_tables=[]
    )
    assert len(result_empty.proposed_classes) == 2


@pytest.mark.asyncio
async def test_execute_import_records_class_creation_error(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    ontology = FakeOntologyService()

    async def fail_create(*args, **kwargs):
        raise RuntimeError("class boom")

    ontology.createClass = fail_create
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=ontology,
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 0
    assert result.success is False
    assert any(e.type == "class" for e in result.errors)


@pytest.mark.asyncio
async def test_execute_import_records_property_creation_error(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    ontology = FakeOntologyService()

    async def fail_property(*args, **kwargs):
        raise RuntimeError("property boom")

    ontology.createProperty = fail_property
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=ontology,
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 1
    assert result.created_properties == 0
    assert any(e.type == "property" for e in result.errors)


@pytest.mark.asyncio
async def test_execute_import_counts_conflict_resolutions(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[
            ConflictResolution(type="class", existing_id=1, action="skip"),
            ConflictResolution(type="class", existing_id=2, action="overwrite"),
        ],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.skipped_conflicts == 1
    assert result.overwritten_conflicts == 1


@pytest.mark.asyncio
async def test_execute_import_skips_unselected_items(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    preview.proposed_classes[0].is_selected = False
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 0
    assert result.created_properties == 0


@pytest.mark.asyncio
async def test_execute_import_raises_not_found_for_missing_datasource(
    dbSession: AsyncSession,
):
    svc = LocalImportService(
        schema_service=FakeSchemaService(),
        ontology_service=FakeOntologyService(),
    )
    request = ImportExecuteRequest(
        confirmed_classes=[],
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )
    with pytest.raises(NotFoundError):
        await svc.execute_import(
            dbSession,
            datasource_id=999,
            request=request,
            created_by="admin",
        )


@pytest.mark.asyncio
async def test_execute_import_creates_join(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    ),
                    ColumnSchemaRead(
                        column_name="customer_id",
                        data_type="INT",
                        nullable=False,
                    ),
                ],
                primary_keys=["id"],
                foreign_keys=[
                    ForeignKeySchemaRead(
                        column_name="customer_id",
                        ref_table="customers",
                        ref_column="id",
                    )
                ],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    )
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )
    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=FakeOntologyService(),
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=preview.proposed_joins,
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 2
    assert result.created_properties == 3
    assert result.created_joins == 1
    assert result.success is True


@pytest.mark.asyncio
async def test_execute_import_records_join_creation_error(dbSession: AsyncSession):
    ds = await _seed_datasource(dbSession)
    ontology = FakeOntologyService()

    async def fail_join(*args, **kwargs):
        raise RuntimeError("join boom")

    ontology.createJoin = fail_join
    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    ),
                    ColumnSchemaRead(
                        column_name="customer_id",
                        data_type="INT",
                        nullable=False,
                    ),
                ],
                primary_keys=["id"],
                foreign_keys=[
                    ForeignKeySchemaRead(
                        column_name="customer_id",
                        ref_table="customers",
                        ref_column="id",
                    )
                ],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(
                        column_name="id", data_type="INT", nullable=False
                    )
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )
    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=ontology,
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=preview.proposed_joins,
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    assert result.created_classes == 2
    assert result.created_joins == 0
    assert any(e.type == "join" for e in result.errors)


@pytest.mark.asyncio
async def test_execute_import_recovers_session_after_property_db_failure(
    dbSession: AsyncSession,
):
    """属性 DB 失败污染会话后，后续项仍能继续写入（防 InvalidRequestError 级联）。

    createProperty 第 2 次调用模拟真实 DB 层失败（NOT NULL 违规提交失败），
    会话进入 pending rollback；修复后 execute_import 在 except 中回滚恢复会话，
    后续类/属性仍可正常创建，不会级联失败。
    """
    from types import SimpleNamespace

    from app.domain.models import OntologyClass, OntologyProperty

    ds = await _seed_datasource(dbSession)

    schema_svc = FakeSchemaService()
    schema_svc.buildResponse = lambda cache: SchemaIntrospectResponse(
        tables=[
            TableSchemaRead(
                table_name="orders",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False),
                    ColumnSchemaRead(
                        column_name="customer_id", data_type="INT", nullable=False
                    ),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
            TableSchemaRead(
                table_name="customers",
                columns=[
                    ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                ],
                primary_keys=["id"],
                foreign_keys=[],
            ),
        ],
        cached_at=datetime(2026, 8, 29, 0, 0, 0),
    )

    class PoisoningOntologyService:
        """createProperty 第 2 次调用时模拟 DB 失败并污染会话。"""

        def __init__(self):
            self._property_calls = 0
            # listClasses 返回 detached 摘要，避免回滚后过期 ORM 对象触发同步惰性加载
            self._classes: list[SimpleNamespace] = []

        async def listClasses(self, session, includeExpired=False):
            return list(self._classes)

        async def listPropertiesByClass(self, session, classId):
            return []

        async def createClass(self, session, dto):
            cls = OntologyClass(class_name=dto.class_name, source_table=dto.source_table)
            session.add(cls)
            await session.commit()
            await session.refresh(cls)
            self._classes.append(
                SimpleNamespace(id=cls.id, source_table=dto.source_table)
            )
            return cls

        async def createProperty(self, session, dto):
            self._property_calls += 1
            if self._property_calls == 2:
                # property_name=None 违反 NOT NULL：commit 失败后会话进入 pending rollback
                session.add(
                    OntologyProperty(
                        class_id=dto.class_id,
                        property_name=None,
                        data_type=dto.data_type,
                    )
                )
                await session.commit()  # raises IntegrityError
            prop = OntologyProperty(
                class_id=dto.class_id,
                property_name=dto.property_name,
                data_type=dto.data_type,
            )
            session.add(prop)
            await session.commit()
            return prop

        async def createJoin(self, session, dto):
            return None

    svc = LocalImportService(
        schema_service=schema_svc,
        ontology_service=PoisoningOntologyService(),
    )
    preview = await svc.build_preview(
        dbSession, datasource_id=ds.id, rules=ImportRuleConfig()
    )
    request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )

    result = await svc.execute_import(
        dbSession,
        datasource_id=ds.id,
        request=request,
        created_by="admin",
        actor=_ADMIN,
    )

    property_errors = [e for e in result.errors if e.type == "property"]
    class_errors = [e for e in result.errors if e.type == "class"]
    assert len(property_errors) == 1  # 仅 orders.customer_id 失败
    assert class_errors == []  # customers 类未被级联失败
    assert result.created_classes == 2
    assert result.created_properties == 2  # orders.id + customers.id 成功
    assert result.success is False
