"""数据源管理 CRUD 路由。

GET    /api/v1/datasources                 列出
POST   /api/v1/datasources                 创建
GET    /api/v1/datasources/{datasourceId}   获取
PUT    /api/v1/datasources/{datasourceId}   更新
DELETE /api/v1/datasources/{datasourceId}   删除
POST   /api/v1/datasources/test            测试连接
POST   /api/v1/datasources/{datasourceId}/introspect  触发 schema 发现并缓存
GET    /api/v1/datasources/{datasourceId}/schema     读取 schema 缓存
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
    DataSourceCreate,
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
    ds = await _service.create(session, dto, createdBy=currentUser.userId)
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
) -> DataSourceRead:
    ds = await _service.update(session, datasourceId, dto)
    return DataSourceRead.model_validate(ds)


@router.delete("/{datasourceId}", status_code=204)
async def deleteDataSource(
    datasourceId: int, session: AsyncSession = Depends(getDb)
) -> Response:
    await _service.delete(session, datasourceId)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{datasourceId}/introspect", response_model=SchemaIntrospectResponse)
@limiter.limit(_INTROSPECT_LIMIT)
async def introspectSchema(
    request: Request,
    datasourceId: int,
    session: AsyncSession = Depends(getDb),
) -> SchemaIntrospectResponse:
    """触发业务库 schema 自动发现并写入缓存（数据未变化时复用缓存）。"""
    ds = await _service.get(session, datasourceId)
    cache = await _schemaService.introspectAndCache(session, ds)
    return _schemaService.buildResponse(cache)


@router.get("/{datasourceId}/schema", response_model=SchemaIntrospectResponse)
async def getCachedSchema(
    datasourceId: int, session: AsyncSession = Depends(getDb)
) -> SchemaIntrospectResponse:
    """读取数据源已缓存的 schema；未缓存时 404（提示先调用 introspect）。"""
    cache = await _schemaService.getCached(session, datasourceId)
    if cache is None:
        raise NotFoundError(MSG_DATASOURCE_SCHEMA_NOT_CACHED.format(datasourceId=datasourceId))
    return _schemaService.buildResponse(cache)


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
