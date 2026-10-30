"""数据源管理 CRUD 路由。

GET    /api/v1/datasources                 列出
POST   /api/v1/datasources                 创建
GET    /api/v1/datasources/{datasourceId}   获取
PUT    /api/v1/datasources/{datasourceId}   更新
DELETE /api/v1/datasources/{datasourceId}   删除
POST   /api/v1/datasources/test            测试连接
POST   /api/v1/datasources/{datasourceId}/introspect  触发 schema 发现并缓存
GET    /api/v1/datasources/{datasourceId}/schema     读取 schema 缓存
GET    /api/v1/datasources/{datasourceId}/schemas    列出可见 schema（Oracle owner）
GET    /api/v1/datasources/{datasourceId}/layers     数据层分布（前缀规则 × schema 缓存）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.error_messages import (
    MSG_DATASOURCE_SCHEMA_NOT_CACHED,
    MSG_PARAM_ACTIVE_ONLY_DATASOURCES,
)
from app.domain.exceptions import NotFoundError
from app.domain.schemas import (
    DataLayerCountRead,
    DataSourceCreate,
    DataSourceLayersRead,
    DataSourceRead,
    DataSourceTestRequest,
    DataSourceTestResponse,
    DataSourceUpdate,
    OntologyDriftReport,
    SchemaIntrospectResponse,
)
from app.infrastructure.rate_limit import limiter
from app.services.datasource_service import DataSourceService
from app.services.ontology_service import OntologyService
from app.services.schema_introspection_service import SchemaIntrospectionService

router = APIRouter(dependencies=[Depends(getCurrentUser)])
_service = DataSourceService()
_schemaService = SchemaIntrospectionService()
_ontologyService = OntologyService()
# introspect 是代价高昂的运维操作（连接业务库读数据字典），收紧独立限额
_INTROSPECT_LIMIT = "10/minute"


@router.get("", response_model=list[DataSourceRead])
async def listDataSources(
    activeOnly: bool = Query(default=False, description=MSG_PARAM_ACTIVE_ONLY_DATASOURCES),
    session: AsyncSession = Depends(getDb),
) -> list[DataSourceRead]:
    dataSources = await _service.list(session, activeOnly=activeOnly)
    return [DataSourceRead.model_validate(ds) for ds in dataSources]


@router.post("", response_model=DataSourceRead, status_code=201)
async def createDataSource(
    dto: DataSourceCreate,
    session: AsyncSession = Depends(getDb),
    currentUser: CurrentUser = Depends(getCurrentUser),
) -> DataSourceRead:
    ds = await _service.create(session, dto, createdBy=currentUser.userId, actor=currentUser)
    return DataSourceRead.model_validate(ds)


@router.post("/test", response_model=DataSourceTestResponse)
async def testDataSource(
    dto: DataSourceTestRequest,
) -> DataSourceTestResponse:
    return await _service.test_connection(dto)


@router.get("/{datasourceId}", response_model=DataSourceRead)
async def getDataSource(
    datasourceId: int, session: AsyncSession = Depends(getDb)
) -> DataSourceRead:
    ds = await _service.get(session, datasourceId)
    return DataSourceRead.model_validate(ds)


@router.put("/{datasourceId}", response_model=DataSourceRead)
async def updateDataSource(
    datasourceId: int,
    dto: DataSourceUpdate,
    session: AsyncSession = Depends(getDb),
    currentUser: CurrentUser = Depends(getCurrentUser),
) -> DataSourceRead:
    ds = await _service.update(session, datasourceId, dto, actor=currentUser)
    return DataSourceRead.model_validate(ds)


@router.delete("/{datasourceId}", status_code=204)
async def deleteDataSource(
    datasourceId: int,
    session: AsyncSession = Depends(getDb),
    currentUser: CurrentUser = Depends(getCurrentUser),
) -> Response:
    await _service.delete(session, datasourceId, actor=currentUser)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{datasourceId}/introspect", response_model=SchemaIntrospectResponse)
@limiter.limit(_INTROSPECT_LIMIT)
async def introspectSchema(
    request: Request,
    datasourceId: int,
    schema: str | None = Query(
        default=None, description="Oracle owner 命名空间（如 THBI）；缺省取连接用户默认 owner"
    ),
    session: AsyncSession = Depends(getDb),
) -> SchemaIntrospectResponse:
    """触发业务库 schema 自动发现并写入缓存（数据未变化时复用缓存）。

    可选 schema 指定要内省的 Oracle owner；缺省回退连接用户默认 owner（向后兼容）。
    """
    ds = await _service.get(session, datasourceId)
    cache = await _schemaService.introspectAndCache(session, ds, owner=schema)
    return _schemaService.buildResponse(cache)


@router.get("/{datasourceId}/schema", response_model=SchemaIntrospectResponse)
async def getCachedSchema(
    datasourceId: int,
    schema: str | None = Query(
        default=None, description="Oracle owner 命名空间（如 THBI）；缺省取连接用户默认 owner"
    ),
    session: AsyncSession = Depends(getDb),
) -> SchemaIntrospectResponse:
    """读取数据源已缓存的 schema；未缓存时 404（提示先调用 introspect）。"""
    cache = await _schemaService.getCached(session, datasourceId, owner=schema)
    if cache is None:
        raise NotFoundError(MSG_DATASOURCE_SCHEMA_NOT_CACHED.format(datasourceId=datasourceId))
    return _schemaService.buildResponse(cache)


@router.get("/{datasourceId}/schemas", response_model=list[str])
async def listDatasourceSchemas(
    datasourceId: int, session: AsyncSession = Depends(getDb)
) -> list[str]:
    """列出该连接可见的 schema（Oracle owner）命名空间，供导入向导「选择 Schema」。

    PG/MySQL 返回空列表（无显式多 schema 概念，前端据此隐藏 schema 选择步）。
    """
    ds = await _service.get(session, datasourceId)
    return await _schemaService.listSchemas(ds)


@router.get("/{datasourceId}/layers", response_model=DataSourceLayersRead)
async def getDatasourceLayers(
    datasourceId: int,
    session: AsyncSession = Depends(getDb),
    owner: str | None = Query(default=None),
) -> DataSourceLayersRead:
    """数据层分布（feat-data-domain-layer 方案 2）。

    **cache-only**：只读 schema 缓存，miss 不实时探库——Oracle 全量内省可能超
    前端 30s 全局超时；fromCache=false 时前端提示先执行探测。表名按
    data_layer_rule 前缀规则（priority 升序）归类；未命中前缀的表计入
    unknownTables 显式透出（ERP 原生命名断层不吞）。
    """
    from sqlalchemy import select as sa_select

    from app.domain.models import DataSource
    from app.services.data_layer_defaults import DEFAULT_LAYER_RULES
    from app.services.layer_rule_registry import layerRuleRegistry
    from app.services.data_layer_defaults import matchTableNameLayer

    ds = (
        await session.execute(
            sa_select(DataSource).where(DataSource.id == datasourceId)
        )
    ).scalars().first()
    if ds is None:
        raise NotFoundError(f"数据源 id={datasourceId} 不存在")

    cache = await _schemaService.getCached(session, datasourceId, owner=owner, ds=ds)
    if cache is None:
        return DataSourceLayersRead(
            datasourceId=datasourceId,
            schemaName=owner or "",
            fromCache=False,
        )

    rules = (
        layerRuleRegistry.rules()
        if layerRuleRegistry.isWarmedUp()
        else DEFAULT_LAYER_RULES
    )
    layerCounts: dict[str, int] = {}
    unknown = 0
    total = 0
    for table in cache.schema_data:
        name = table.get("table_name") or table.get("tableName") or ""
        if not name:
            continue
        total += 1
        layer = matchTableNameLayer(str(name).upper(), rules)
        if layer is None:
            unknown += 1
        else:
            layerCounts[layer] = layerCounts.get(layer, 0) + 1
    return DataSourceLayersRead(
        datasourceId=datasourceId,
        schemaName=cache.schema_name,
        fromCache=True,
        totalTables=total,
        unknownTables=unknown,
        layers=sorted(
            (
                DataLayerCountRead(layer=layer, tableCount=count)
                for layer, count in layerCounts.items()
            ),
            key=lambda c: c.layer,
        ),
    )


@router.get("/{datasourceId}/ontology-drift", response_model=OntologyDriftReport)
async def getOntologyDrift(
    datasourceId: int, session: AsyncSession = Depends(getDb)
) -> OntologyDriftReport:
    """交叉校验本体引用的表/列与 schema 缓存，报告已漂移对象（2-4，表漂移能告警）。

    本体为全局共享（R4 已知限制），报告覆盖全部本体类相对该数据源缓存的漂移。
    未缓存时 schemaCached=false（不臆测缺失，提示先调用 introspect）。
    """
    ds = await _service.get(session, datasourceId)
    classes = await _ontologyService.listClasses(session)
    return await _schemaService.validateOntologyDrift(session, ds, classes)
