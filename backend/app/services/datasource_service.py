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
from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataSource
from app.domain.schemas import (
    DataSourceCreate,
    DataSourceTestRequest,
    DataSourceTestResponse,
    DataSourceUpdate,
)
from app.infrastructure.business_db_pool import build_adapter, dispose_adapter
from app.infrastructure.security.crypto import decryptApiKey, encryptApiKey
from app.services.audit_service import AuditService
from app.services.messages_zh import (
    MSG_DATASOURCE_CONNECT_FAILED,
    MSG_DATASOURCE_HOST_ALLOWLIST_DETAIL,
    MSG_DATASOURCE_HOST_NOT_ALLOWED,
    MSG_DATASOURCE_NAME_EXISTS,
    MSG_DATASOURCE_NOT_FOUND,
)

logger = logging.getLogger(__name__)

_audit = AuditService()


class DataSourceService:
    """数据源管理服务。"""

    async def create(
        self,
        session: AsyncSession,
        dto: DataSourceCreate,
        createdBy: str | None,
        actor: CurrentUser | None = None,
    ) -> DataSource:
        """创建数据源。name 唯一，主机须在白名单内。"""
        _validateHost(dto.host)
        await _assertNameUnique(session, dto.name)

        if dto.is_default:
            await _clearOtherDefaults(session, keepId=None)

        # 版本来源优先级：用户显式值 > best-effort 探测（方言分发的关键输入，
        # 尤其 Oracle 11g 与 12c+ 的分页语法分歧）。探测失败不阻断创建。
        oracleVersion = dto.oracle_version
        if oracleVersion is None:
            oracleVersion = await _probeServerVersion(
                dto.type, dto.host, dto.port,
                dto.database_name, dto.username, dto.password,
            )

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
            oracle_version=oracleVersion,
        )
        session.add(ds)
        await session.flush()
        await _audit.record(
            session,
            entity_type="data_source",
            entity_id=ds.id,
            action="CREATE",
            actor=actor.userId if actor else createdBy or "anonymous",
            actor_departments=actor.departments if actor else None,
            after=_datasourceToDict(ds),
        )
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
        self,
        session: AsyncSession,
        datasourceId: int,
        dto: DataSourceUpdate,
        actor: CurrentUser | None = None,
    ) -> DataSource:
        """部分更新。password 非空时重新加密；连接参数变更时释放缓存适配器。"""
        ds = await self.get(session, datasourceId)
        before_state = _datasourceToDict(ds)
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

        # 版本补探测（best-effort，不覆盖任何已有值）：用户没显式给版本、且
        # （连接参数变了 或 版本仍为空）时，用**更新后**的最终参数重新探测。
        # 探测失败只记日志，保存照常。
        userSetVersion = updates.get("oracle_version") is not None
        if not userSetVersion and (connChanged or ds.oracle_version is None):
            probed = await _probeServerVersion(
                ds.type, ds.host, ds.port,
                ds.database_name, ds.username,
                plainPassword if passwordChanged else decryptApiKey(ds.password_encrypted),
            )
            if probed is not None and ds.oracle_version is None:
                ds.oracle_version = probed

        await session.flush()
        await _audit.record(
            session,
            entity_type="data_source",
            entity_id=ds.id,
            action="UPDATE",
            actor=actor.userId if actor else "anonymous",
            actor_departments=actor.departments if actor else None,
            before=before_state,
            after=_datasourceToDict(ds),
        )
        await session.commit()
        await session.refresh(ds)

        if connChanged or passwordChanged:
            await dispose_adapter(datasourceId)

        logger.info("更新数据源 id=%s fields=%s", datasourceId, list(updates.keys()))
        return ds

    async def delete(
        self,
        session: AsyncSession,
        datasourceId: int,
        actor: CurrentUser | None = None,
    ) -> None:
        """硬删除数据源并释放适配器；若删除的是默认源则提升首个启用源为默认。"""
        ds = await self.get(session, datasourceId)
        wasDefault = ds.is_default
        before_state = _datasourceToDict(ds)
        await session.flush()
        await _audit.record(
            session,
            entity_type="data_source",
            entity_id=ds.id,
            action="DELETE",
            actor=actor.userId if actor else "anonymous",
            actor_departments=actor.departments if actor else None,
            before=before_state,
        )
        await session.delete(ds)
        await session.commit()
        await dispose_adapter(datasourceId)
        logger.info("删除数据源 id=%s", datasourceId)

        if wasDefault:
            await _promoteNextDefault(session)

    async def test_connection(self, dto: DataSourceTestRequest) -> DataSourceTestResponse:
        """测试连接（不持久化）。构建临时适配器并调用 test()，顺带探测服务端版本。"""
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
            success, message, rawVersion = await adapter.test()
        finally:
            await adapter.dispose()
        if not success:
            logger.warning("数据源连接测试失败 host=%s: %s", dto.host, message)
        return DataSourceTestResponse(
            success=success,
            message=message if success else MSG_DATASOURCE_CONNECT_FAILED.format(message=message),
            server_version=_normalizeServerVersion(rawVersion),
        )


# =============================================================================
# 辅助函数
# =============================================================================


def _normalizeServerVersion(raw: object | None) -> str | None:
    """把驱动的版本原文归一为短字符串（None 安全，超长截断防 banner 进字段）。

    SQLAlchemy 的 server_version_info 是 tuple（如 ("8","0","46")），oracledb 的
    conn.version 是 str（如 "19.0.0.0.0"）；两者都归一为点分/原文短串。
    """
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)):
        normalized = ".".join(str(part) for part in raw if str(part))
    else:
        normalized = str(raw)
    normalized = normalized.strip()
    return normalized[:40] or None


async def _probeServerVersion(
    dsType: DataSourceType,
    host: str,
    port: int,
    databaseName: str,
    username: str,
    password: str,
) -> str | None:
    """best-effort 探测服务端版本：建临时适配器 → test() → 释放，失败只记日志。

    与 test_connection 的区别：这是 create/update 的内部探测，任何异常都不向上
    传播 —— 探测是增强，绝不能阻断数据源的创建/保存。
    """
    try:
        adapter = build_adapter(dsType, host, port, databaseName, username, password)
    except Exception:  # noqa: BLE001 - 探测失败不阻断
        logger.warning("版本探测构建适配器失败 host=%s", host, exc_info=True)
        return None
    try:
        success, _message, rawVersion = await adapter.test()
        if not success:
            return None
        return _normalizeServerVersion(rawVersion)
    except Exception:  # noqa: BLE001 - 探测失败不阻断
        logger.warning("版本探测失败 host=%s", host, exc_info=True)
        return None
    finally:
        await adapter.dispose()


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


def _datasourceToDict(ds: DataSource) -> dict:
    """将 DataSource 模型实例转换为字典（审计用，不含密码）。"""
    return {
        "id": ds.id,
        "name": ds.name,
        "type": ds.type.value if hasattr(ds.type, "value") else ds.type,
        "host": ds.host,
        "port": ds.port,
        "databaseName": ds.database_name,
        "username": ds.username,
        "description": ds.description,
        "isActive": ds.is_active,
        "isDefault": ds.is_default,
        "createdBy": ds.created_by,
        "oracleVersion": ds.oracle_version,
    }
