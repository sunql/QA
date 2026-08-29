"""数据源 CRUD 服务。

负责 DataSource 的创建/查询/更新/删除与连接测试：
- 主机白名单校验（DATASOURCE_HOST_ALLOWLIST）
- 密码 Fernet 加密存储，读 DTO 永不返回
- 默认数据源排他性（同一时刻仅一个 is_default=True）
- 连接参数变更时释放缓存的业务库适配器
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataSource
from app.domain.schemas import (
    DataSourceCreate,
    DataSourceTestRequest,
    DataSourceTestResponse,
    DataSourceUpdate,
)
from app.infrastructure.business_db_pool import build_adapter, dispose_adapter
from app.infrastructure.security.crypto import encryptApiKey
from app.services.messages_zh import (
    MSG_DATASOURCE_CONNECT_FAILED,
    MSG_DATASOURCE_HOST_ALLOWLIST_DETAIL,
    MSG_DATASOURCE_HOST_NOT_ALLOWED,
    MSG_DATASOURCE_NAME_EXISTS,
    MSG_DATASOURCE_NOT_FOUND,
)

logger = logging.getLogger(__name__)


class DataSourceService:
    """数据源管理服务。"""

    async def create(self, session: AsyncSession, dto: DataSourceCreate, createdBy: str | None) -> DataSource:
        """创建数据源。name 唯一，主机须在白名单内。"""
        _validateHost(dto.host)
        await _assertNameUnique(session, dto.name)

        if dto.is_default:
            await _clearOtherDefaults(session, keepId=None)

        ds = DataSource(
            name=dto.name,
            type=dto.type,
            host=dto.host,
            port=dto.port,
            database_name=dto.database_name,
            username=dto.username,
            password_encrypted=encryptApiKey(dto.password),
            description=dto.description,
            is_active=dto.is_active,
            is_default=dto.is_default,
            created_by=createdBy,
        )
        session.add(ds)
        await session.commit()
        await session.refresh(ds)
        logger.info("创建数据源 id=%s name=%s type=%s", ds.id, ds.name, ds.type)
        return ds

    async def list(self, session: AsyncSession, *, activeOnly: bool = False) -> list[DataSource]:
        """列出数据源，默认源置顶。"""
        stmt = select(DataSource).order_by(DataSource.is_default.desc(), DataSource.id)
        if activeOnly:
            stmt = stmt.where(DataSource.is_active.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, session: AsyncSession, datasourceId: int) -> DataSource:
        """按 id 获取，不存在抛 NotFoundError。"""
        ds = await session.get(DataSource, datasourceId)
        if ds is None:
            raise NotFoundError(MSG_DATASOURCE_NOT_FOUND.format(id=datasourceId))
        return ds

    async def update(
        self, session: AsyncSession, datasourceId: int, dto: DataSourceUpdate
    ) -> DataSource:
        """部分更新。password 非空时重新加密；连接参数变更时释放缓存适配器。"""
        ds = await self.get(session, datasourceId)
        updates = dto.model_dump(exclude_unset=True)

        plainPassword = updates.pop("password", None)
        passwordChanged = bool(plainPassword)

        if "name" in updates and updates["name"] is not None and updates["name"] != ds.name:
            await _assertNameUnique(session, updates["name"], excludeId=datasourceId)

        if "host" in updates and updates["host"] is not None:
            _validateHost(updates["host"])

        if passwordChanged:
            ds.password_encrypted = encryptApiKey(plainPassword)

        # 检测连接参数是否变更，决定是否释放缓存适配器
        connChanged = any(
            field in updates and updates[field] is not None and updates[field] != getattr(ds, field)
            for field in ("type", "host", "port", "database_name", "username")
        )

        for field, value in updates.items():
            if value is None and field != "description":
                continue
            setattr(ds, field, value)

        if dto.is_default is True:
            await _clearOtherDefaults(session, keepId=datasourceId)

        await session.commit()
        await session.refresh(ds)

        if connChanged or passwordChanged:
            await dispose_adapter(datasourceId)

        logger.info("更新数据源 id=%s fields=%s", datasourceId, list(updates.keys()))
        return ds

    async def delete(self, session: AsyncSession, datasourceId: int) -> None:
        """硬删除数据源并释放适配器；若删除的是默认源则提升首个启用源为默认。"""
        ds = await self.get(session, datasourceId)
        wasDefault = ds.is_default
        await session.delete(ds)
        await session.commit()
        await dispose_adapter(datasourceId)
        logger.info("删除数据源 id=%s", datasourceId)

        if wasDefault:
            await _promoteNextDefault(session)

    async def test_connection(self, dto: DataSourceTestRequest) -> DataSourceTestResponse:
        """测试连接（不持久化）。构建临时适配器并调用 test()。"""
        _validateHost(dto.host)
        adapter = build_adapter(
            dto.type,
            dto.host,
            dto.port,
            dto.database_name,
            dto.username,
            dto.password,
        )
        try:
            success, message = await adapter.test()
        finally:
            await adapter.dispose()
        if not success:
            logger.warning("数据源连接测试失败 host=%s: %s", dto.host, message)
        return DataSourceTestResponse(
            success=success,
            message=message if success else MSG_DATASOURCE_CONNECT_FAILED.format(message=message),
        )


# =============================================================================
# 辅助函数
# =============================================================================


def _validateHost(host: str) -> None:
    """主机白名单校验：配置了白名单且 host 不在其中则抛 ValidationError。"""
    allowlist = getSettings().datasourceHosts
    if allowlist and host not in allowlist:
        raise ValidationError(
            MSG_DATASOURCE_HOST_NOT_ALLOWED.format(host=host),
            detail=MSG_DATASOURCE_HOST_ALLOWLIST_DETAIL.format(hosts=", ".join(allowlist)),
        )


async def _assertNameUnique(
    session: AsyncSession, name: str, *, excludeId: int | None = None
) -> None:
    """校验名称唯一。"""
    stmt = select(DataSource.id).where(DataSource.name == name)
    if excludeId is not None:
        stmt = stmt.where(DataSource.id != excludeId)
    existing = await session.execute(stmt)
    if existing.scalar_one_or_none() is not None:
        raise ValidationError(MSG_DATASOURCE_NAME_EXISTS.format(name=name))


async def _clearOtherDefaults(session: AsyncSession, *, keepId: int | None) -> None:
    """取消其他数据源的默认标记。"""
    stmt = update(DataSource).where(DataSource.is_default.is_(True))
    if keepId is not None:
        stmt = stmt.where(DataSource.id != keepId)
    stmt = stmt.values(is_default=False)
    await session.execute(stmt)


async def _promoteNextDefault(session: AsyncSession) -> None:
    """删除默认源后，提升首个启用源为默认。"""
    stmt = (
        select(DataSource)
        .where(DataSource.is_active.is_(True))
        .order_by(DataSource.id)
        .limit(1)
    )
    result = await session.execute(stmt)
    nextDs = result.scalars().first()
    if nextDs is not None:
        nextDs.is_default = True
        await session.commit()
        logger.info("提升数据源 id=%s 为默认", nextDs.id)
