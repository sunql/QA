"""采购源绑定 API（M1-1.1）。

采购域「类 → 物理表」「属性 → 物理列」绑定的 CRUD 与确认流转：

- `GET    /procurement-bindings`                     全量列表（含候选）——**admin**
- `GET    /procurement-bindings/effective`           生效视图（已确认且未失效）——登录即可
- `GET    /procurement-bindings/effective/properties` 生效的**属性**映射——登录即可
- `GET    /procurement-bindings/{id}`                绑定详情（含属性绑定）——**admin**
- `POST   /procurement-bindings`                     新增**候选**源绑定——admin
- `POST   /procurement-bindings/{id}/properties`     新增**候选**属性绑定——admin
- `POST   /procurement-bindings/{id}/confirm`        源绑定确认——admin
- `POST   /procurement-bindings/{id}/expire`         源绑定软失效——admin
- `POST   /procurement-bindings/{id}/properties/{propertyBindingId}/confirm`
                                                     属性绑定确认——admin
- `POST   /procurement-bindings/{id}/properties/{propertyBindingId}/expire`
                                                     属性绑定软失效——admin

**为什么读面分两档**：候选（`is_candidate = TRUE`）是**未经确认的治理草案**，
普通用户看到会当成结论 ⇒ 全量视图限 admin；只有「已确认且当前生效」的行对
所有登录用户可见 —— 这也是 D-3「未确认的候选不得被读路径消费」在 HTTP 面的落法。

**确认人取当前登录用户**，不接受客户端传入（防伪造审计）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb, requireAdmin
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.schemas.procurement_binding import (
    PropertyBindingCreate,
    PropertyBindingRead,
    SourceBindingCreate,
    SourceBindingDetail,
    SourceBindingRead,
)
from app.services.procurement_binding_service import ProcurementBindingService

router = APIRouter(
    prefix="/procurement-bindings",
    tags=["procurement-bindings"],
    dependencies=[Depends(getCurrentUser)],
)
_bindingService = ProcurementBindingService()


def _toSourceRead(
    binding: ProcurementSourceBinding,
    classNames: dict[int, str],
    propertyCounts: dict[int, int],
) -> SourceBindingRead:
    """把 ORM 行转成读视图（类名与属性条数由调用方批量查好，避免 N+1）。"""
    return SourceBindingRead(
        id=binding.id,
        class_id=binding.class_id,
        class_name=classNames.get(binding.class_id),
        datasource_id=binding.datasource_id,
        schema_name=binding.schema_name,
        source_table=binding.source_table,
        binding_role=binding.binding_role,
        filter_expression=binding.filter_expression,
        filter_allowlist=list(binding.filter_allowlist or []),
        is_candidate=binding.is_candidate,
        confirmed_by=binding.confirmed_by,
        confirmed_time=binding.confirmed_time,
        valid_from=binding.valid_from,
        valid_to=binding.valid_to,
        created_by=binding.created_by,
        property_count=propertyCounts.get(binding.id, 0),
    )


def _toPropertyRead(
    binding: ProcurementPropertyBinding, propertyNames: dict[int, str]
) -> PropertyBindingRead:
    """把属性绑定 ORM 行转成读视图。"""
    return PropertyBindingRead(
        id=binding.id,
        source_binding_id=binding.source_binding_id,
        property_id=binding.property_id,
        property_name=propertyNames.get(binding.property_id),
        source_column=binding.source_column,
        key_ordinal=binding.key_ordinal,
        transform_expression=binding.transform_expression,
        is_candidate=binding.is_candidate,
        confirmed_by=binding.confirmed_by,
        confirmed_time=binding.confirmed_time,
        valid_from=binding.valid_from,
        valid_to=binding.valid_to,
    )


async def _readMany(
    session: AsyncSession,
    bindings: list[ProcurementSourceBinding],
    *,
    onlyEffectiveCounts: bool = False,
) -> list[SourceBindingRead]:
    """批量把源绑定列表转成读视图（两次批量查询 + N 次纯内存组装）。

    `onlyEffectiveCounts=True` 时属性条数只数**已确认且未失效**的行 ——
    登录即可访问的生效视图必须用它，否则候选/已失效行会通过「条数」侧面泄漏。
    """
    if not bindings:
        return []
    classNames = await _bindingService.classNameMap(
        session, [item.class_id for item in bindings]
    )
    propertyCounts = await _bindingService.propertyCountMap(
        session, [item.id for item in bindings], onlyEffective=onlyEffectiveCounts
    )
    return [
        _toSourceRead(item, classNames, propertyCounts) for item in bindings
    ]


@router.get("", response_model=list[SourceBindingRead], status_code=status.HTTP_200_OK)
async def listBindings(
    classId: int | None = None,
    datasourceId: int | None = None,
    effectiveOnly: bool = False,
    _user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> list[SourceBindingRead]:
    """列出源绑定（含候选与已失效行）——管理台视图，限 admin。

    `effectiveOnly=true` 时的过滤口径与 `GET /effective` 一致：
    「已确认且当前生效」（候选不得进入读路径，D-3）。
    """
    bindings = await _bindingService.listSourceBindings(
        db, classId=classId, datasourceId=datasourceId, effectiveOnly=effectiveOnly
    )
    return await _readMany(db, bindings)


@router.get("/effective", response_model=list[SourceBindingRead], status_code=status.HTTP_200_OK)
async def listEffectiveBindings(
    db: AsyncSession = Depends(getDb),
) -> list[SourceBindingRead]:
    """列出**生效**的源绑定（已确认 + `valid_to IS NULL`）——所有登录用户可见。

    这是唯一对普通用户开放的绑定视图：**候选与已失效行一律不出现**。
    """
    bindings = await _bindingService.listSourceBindings(db, effectiveOnly=True)
    return await _readMany(db, bindings, onlyEffectiveCounts=True)


@router.get(
    "/effective/properties",
    response_model=list[PropertyBindingRead],
    status_code=status.HTTP_200_OK,
)
async def listEffectivePropertyBindings(
    db: AsyncSession = Depends(getDb),
) -> list[PropertyBindingRead]:
    """列出**生效的属性映射**（读路径的唯一入口）——所有登录用户可见。

    口径：属性绑定自身已确认且未失效，**且父源绑定**已确认且未失效
    ⇒ 不会出现「父已过期、子还在生效」的孤儿映射。
    """
    bindings = await _bindingService.listEffectivePropertyBindings(db)
    propertyNames = await _bindingService.propertyNameMap(
        db, [item.property_id for item in bindings]
    )
    return [_toPropertyRead(item, propertyNames) for item in bindings]


@router.get("/{id}", response_model=SourceBindingDetail, status_code=status.HTTP_200_OK)
async def getBinding(
    id: int,
    _user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> SourceBindingDetail:
    """源绑定详情 = 绑定本体 + 其属性绑定列表（限 admin：可能含候选）。"""
    binding = await _bindingService.getSourceBinding(db, id)
    properties = await _bindingService.listPropertyBindings(db, id)
    classNames = await _bindingService.classNameMap(db, [binding.class_id])
    propertyCounts = await _bindingService.propertyCountMap(db, [binding.id])
    propertyNames = await _bindingService.propertyNameMap(
        db, [item.property_id for item in properties]
    )
    return SourceBindingDetail(
        binding=_toSourceRead(binding, classNames, propertyCounts),
        properties=[_toPropertyRead(item, propertyNames) for item in properties],
    )


@router.post("", response_model=SourceBindingRead, status_code=status.HTTP_201_CREATED)
async def createBinding(
    dto: SourceBindingCreate,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> SourceBindingRead:
    """新增一条**候选**源绑定（限 admin）。

    新建**一律落候选**：确认必须由人显式触发（决策 D-3）。
    """
    binding = await _bindingService.createSourceBinding(db, dto, actor=user.userId)
    classNames = await _bindingService.classNameMap(db, [binding.class_id])
    return _toSourceRead(binding, classNames, {})


@router.post(
    "/{id}/properties",
    response_model=PropertyBindingRead,
    status_code=status.HTTP_201_CREATED,
)
async def createPropertyBinding(
    id: int,
    dto: PropertyBindingCreate,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> PropertyBindingRead:
    """在源绑定下新增一条**候选**属性绑定（限 admin）。"""
    binding = await _bindingService.createPropertyBinding(
        db, id, dto, actor=user.userId
    )
    propertyNames = await _bindingService.propertyNameMap(db, [binding.property_id])
    return _toPropertyRead(binding, propertyNames)


@router.post("/{id}/confirm", response_model=SourceBindingRead, status_code=status.HTTP_200_OK)
async def confirmBinding(
    id: int,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> SourceBindingRead:
    """确认源绑定：候选 → 已确认（限 admin）。

    确认人取当前登录用户；前置条件由服务层判定（至少一条属性绑定、
    复合键序号连续）。
    """
    binding = await _bindingService.confirmSourceBinding(db, id, actor=user.userId)
    classNames = await _bindingService.classNameMap(db, [binding.class_id])
    propertyCounts = await _bindingService.propertyCountMap(db, [binding.id])
    return _toSourceRead(binding, classNames, propertyCounts)


@router.post("/{id}/expire", response_model=SourceBindingRead, status_code=status.HTTP_200_OK)
async def expireBinding(
    id: int,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> SourceBindingRead:
    """软失效源绑定（限 admin）：写 `valid_to`，不删行。

    其属性绑定一并失效 ⇒ 不会出现「父已过期、子还生效」的孤儿映射。
    """
    binding = await _bindingService.expireSourceBinding(db, id, actor=user.userId)
    classNames = await _bindingService.classNameMap(db, [binding.class_id])
    propertyCounts = await _bindingService.propertyCountMap(db, [binding.id])
    return _toSourceRead(binding, classNames, propertyCounts)


@router.post(
    "/{id}/properties/{propertyBindingId}/confirm",
    response_model=PropertyBindingRead,
    status_code=status.HTTP_200_OK,
)
async def confirmPropertyBinding(
    id: int,
    propertyBindingId: int,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> PropertyBindingRead:
    """确认属性绑定：候选 → 已确认（限 admin）。

    路径里的 `id`（父源绑定）**必须与属性绑定的实际归属一致** ⇒ 杜绝
    「拿 A 的路径去确认 B 的子绑定」。
    """
    binding = await _bindingService.confirmPropertyBinding(
        db, id, propertyBindingId, actor=user.userId
    )
    propertyNames = await _bindingService.propertyNameMap(db, [binding.property_id])
    return _toPropertyRead(binding, propertyNames)


@router.post(
    "/{id}/properties/{propertyBindingId}/expire",
    response_model=PropertyBindingRead,
    status_code=status.HTTP_200_OK,
)
async def expirePropertyBinding(
    id: int,
    propertyBindingId: int,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> PropertyBindingRead:
    """软失效单条属性绑定（限 admin）：绑错了列时只作废这一条。

    前提：父源绑定仍是候选态且未失效（父已确认 ⇒ 子集合冻结）。
    """
    binding = await _bindingService.expirePropertyBinding(
        db, id, propertyBindingId, actor=user.userId
    )
    propertyNames = await _bindingService.propertyNameMap(db, [binding.property_id])
    return _toPropertyRead(binding, propertyNames)
