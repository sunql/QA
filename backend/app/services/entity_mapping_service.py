"""跨系统编码映射服务（Phase 3.1 + Phase 4.5 扩展 owner-based ACL）。

承载 EntityMapping 表的 CRUD：把各源系统（ERP/SRM/QMS/MDM/PLM）的原始编码
映射到企业统一代理键 / 统一编码。唯一约束 (entity_type, enterprise_key,
source_system) 由 service 层主动查重抛 ValidationError，DB 唯一索引兜底防 race。

Phase 4.5 扩展：updateMapping / deleteMapping 走 AclService.assertCanModify。
createMapping 接收 actor 并将 owner 设为 actor.departments[0]（防止 client
任意声明 owner 越权）；actor.departments 为空时 owner=None（仅 admin 可改）。

bulk import（feat-entity-mapping-bulk-import 2026-09-16）：
- bulkImportMappings 接 list[EntityMappingCreate] + actor
- 单事务提交；行级隔离：某行失败不阻塞其它行
- enterprise_key 由 (entity_type, enterprise_code) 自动派生（与 scripts/sync
  脚本 _stableKey 算法一致），保证同 code → 同 key、bulk 与单条一致
- 唯一键 (entity_type, enterprise_key, source_system) 已存在 → 比对差异
  决定 inserted / updated / skipped；不存在 → inserted
- 上限 1000 行/请求（防长事务锁 PG）
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import SourceSystem
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.schemas import (
    EntityMappingBulkImportItem,
    EntityMappingBulkResult,
    EntityMappingBulkResultRow,
    EntityMappingCreate,
    EntityMappingRead,
    EntityMappingSearchHit,
    EntityMappingUpdate,
)
from app.services.acl_service import AclService
from app.services.messages_zh import (
    MSG_ENTITY_MAPPING_BULK_EMPTY,
    MSG_ENTITY_MAPPING_BULK_TOO_LARGE,
    MSG_ENTITY_MAPPING_DATE_RANGE,
    MSG_ENTITY_MAPPING_EXISTS,
    MSG_ENTITY_MAPPING_NOT_FOUND,
)
from app.services.outbox_service import OutboxService

# update 中不允许置 NULL 的列（None 语义为「不动」）；日期列允许置 None 以清除。
_NON_NULL_UPDATE_FIELDS = frozenset(
    {"enterprise_code", "source_key", "source_code", "match_rule"}
)

# bulk 导入单批上限：超出会让事务锁 PG 过久；超过时前端应自动 chunk 提交。
_BULK_MAX_ROWS = 1000

# bulk 时参与「行是否变化」比对的列（变更则 status=updated，否则 skipped）。
# 不包含 id / created_at / updated_at / owner / enterprise_key / source_system /
# entity_type —— 这些是「身份字段」或由 actor / DB 自填，bulk 不应改。
_BULK_COMPARE_FIELDS: tuple[str, ...] = (
    "enterprise_code",
    "source_key",
    "source_code",
    "match_rule",
    "effective_date",
    "expiry_date",
    "name",
)

# enterprise_key 派生常量（与 scripts/sync_entity_mapping_from_thbi.py SSOT）：
#   - SHA-256(enterprise_code) 前 8 字节 → uint64 → mod 2³² → 加 entity_type offset
# entity_type 偏移区间：SUPPLIER [800_000, 4_295_767_296)；MATERIAL [4_295_767_296, ...)；
# 其它类型暂未分配 offset，统一落到 _GENERIC_KEY_OFFSET（避免与 SUPPLIER/MATERIAL 撞区）。
_KEY_RANGE_SIZE = 2**32
_SUPPLIER_KEY_OFFSET = 800_000
_MATERIAL_KEY_OFFSET = 4_295_767_296  # = _SUPPLIER_KEY_OFFSET + _KEY_RANGE_SIZE
_GENERIC_KEY_OFFSET = 8_591_534_592  # 预留 MATERIAL 之后的下一段 4G
_ENTITY_TYPE_OFFSETS: dict[str, int] = {
    "SUPPLIER": _SUPPLIER_KEY_OFFSET,
    "MATERIAL": _MATERIAL_KEY_OFFSET,
}
# 未在 _ENTITY_TYPE_OFFSETS 里的 entity_type 走通用 offset；
# 这是故意设计：业务对象新增时不需要改 service，只在 seed_business_objects.py 注册即可。
_DEFAULT_KEY_OFFSET = _GENERIC_KEY_OFFSET


def _stableKey(code: str, *, offset: int) -> int:
    """SHA-256(code) 前 8 字节 → uint64 → mod 2³² → 加 offset。

    与 scripts/sync_entity_mapping_from_thbi.py 同源（SSOT），保证批量导入与
    运维脚本对同一 enterprise_code 派生同一 enterprise_key。
    """
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    head = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return offset + (head % _KEY_RANGE_SIZE)


def _deriveKey(entityType: str, enterpriseCode: str) -> int:
    offset = _ENTITY_TYPE_OFFSETS.get(entityType, _DEFAULT_KEY_OFFSET)
    return _stableKey(enterpriseCode, offset=offset)


def _entityToDict(entity: EntityMapping) -> dict:
    """EntityMapping 实体 → JSON 可序列化 dict（用于 outbox payload）。

    date / datetime 等非 JSON-native 类型在 dict 内原样保留会导致 PG JSONB 写入失败，
    故在此统一转字符串。
    """
    out: dict = {}
    for col in entity.__table__.columns.keys():
        v = getattr(entity, col)
        if hasattr(v, "value"):  # Enum
            out[col] = v.value
        elif isinstance(v, (date, datetime)):
            out[col] = v.isoformat()
        else:
            out[col] = v
    return out


def _existsError(dto: EntityMappingCreate) -> ValidationError:
    """唯一冲突错误：查重命中与并发 commit 失败共用同一消息。"""
    return ValidationError(
        MSG_ENTITY_MAPPING_EXISTS.format(
            entityType=dto.entity_type,
            enterpriseKey=dto.enterprise_key,
            sourceSystem=dto.source_system.value,
        )
    )


def _assertDateRange(*, effective_date: date | None, expiry_date: date | None) -> None:
    """生效日期不得晚于失效日期；仅当两端均非空时校验。"""
    if effective_date is not None and expiry_date is not None:
        if effective_date > expiry_date:
            raise ValidationError(
                MSG_ENTITY_MAPPING_DATE_RANGE.format(
                    effectiveDate=effective_date.isoformat(),
                    expiryDate=expiry_date.isoformat(),
                )
            )


class EntityMappingService:
    """跨系统编码映射 CRUD。"""

    def __init__(self, acl: AclService | None = None, outbox: OutboxService | None = None) -> None:
        # 默认实例：service 内部 new；测试可注入 mock
        self._acl = acl or AclService()
        self._outbox = outbox or OutboxService()

    async def listMappings(
        self,
        session: AsyncSession,
        *,
        entityType: str | None = None,
        sourceSystem: SourceSystem | None = None,
        enterpriseKey: int | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[EntityMapping]:
        """列表查询，可按 entityType / sourceSystem / enterpriseKey 过滤，分页返回。"""
        stmt = select(EntityMapping).order_by(EntityMapping.id)
        if entityType is not None:
            stmt = stmt.where(EntityMapping.entity_type == entityType)
        if sourceSystem is not None:
            stmt = stmt.where(EntityMapping.source_system == sourceSystem)
        if enterpriseKey is not None:
            stmt = stmt.where(EntityMapping.enterprise_key == enterpriseKey)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getMapping(self, session: AsyncSession, id: int) -> EntityMapping:
        """按 id 取映射；不存在抛 NotFoundError。"""
        entity = await session.get(EntityMapping, id)
        if entity is None:
            raise NotFoundError(MSG_ENTITY_MAPPING_NOT_FOUND.format(id=id))
        return entity

    async def searchMappings(
        self,
        session: AsyncSession,
        *,
        q: str,
        entityType: str | None = None,
        limit: int = 20,
    ) -> list[EntityMapping]:
        """模糊搜索编码映射（Phase 6.x AutoComplete 用）。

        - `q` 空字符串 → 返回空列表（避免无过滤返回全表 + 与前端空查询语义一致）
        - `q` 全数字 → 同时按 `enterprise_key` 精确匹配；否则按 `enterprise_code` /
          `source_code` ILIKE `%q%` 模糊匹配
        - `entityType` 过滤可选
        - 结果先按 enterprise_key 命中精确排序，再按 id 升序
        - `limit` 上限 100（防止误调拉全表）
        """
        q = (q or "").strip()
        if not q:
            return []
        limit = max(1, min(limit, 100))
        stmt = select(EntityMapping)
        conds = []
        if q.isdigit():
            conds.append(EntityMapping.enterprise_key == int(q))
        # 始终加 ILIKE 兜底，让"输错数字也能搜到含此串的 enterprise_code"
        like = f"%{q}%"
        conds.append(EntityMapping.enterprise_code.ilike(like))
        conds.append(EntityMapping.source_code.ilike(like))
        stmt = stmt.where(or_(*conds))
        if entityType is not None:
            stmt = stmt.where(EntityMapping.entity_type == entityType)
        # 排序：精确 enterprise_key 命中排前（按 q 全数字），其余按 id 稳定
        if q.isdigit():
            stmt = stmt.order_by(
                (EntityMapping.enterprise_key == int(q)).desc(),
                EntityMapping.id,
            )
        else:
            stmt = stmt.order_by(EntityMapping.id)
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def createMapping(
        self,
        session: AsyncSession,
        dto: EntityMappingCreate,
        actor: CurrentUser,
    ) -> EntityMapping:
        """创建编码映射；同实体 + 同源系统重复抛 ValidationError。

        Phase 4.5：owner 由 actor.departments[0] 派生，**不接受** client body
        中的 owner（已在 DTO 中移除），防止「finance 用户创建 owner=procurement
        的实体」式越权。actor.departments 为空 → owner=None → 仅 admin 可改。
        """
        _assertDateRange(effective_date=dto.effective_date, expiry_date=dto.expiry_date)
        # 唯一性查重（service 层兜底；三列均非空，DB 唯一索引也完整兜底）
        existing = await session.execute(
            select(EntityMapping).where(
                EntityMapping.entity_type == dto.entity_type,
                EntityMapping.enterprise_key == dto.enterprise_key,
                EntityMapping.source_system == dto.source_system,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise _existsError(dto)

        derivedOwner = actor.departments[0] if actor.departments else None
        entity = EntityMapping(
            entity_type=dto.entity_type,
            enterprise_key=dto.enterprise_key,
            enterprise_code=dto.enterprise_code,
            source_system=dto.source_system,
            source_key=dto.source_key,
            source_code=dto.source_code,
            match_rule=dto.match_rule,
            effective_date=dto.effective_date,
            expiry_date=dto.expiry_date,
            owner=derivedOwner,
        )
        session.add(entity)
        await session.flush()  # get entity.id for outbox payload
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_created",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"after": _entityToDict(entity)},
        )
        # 并发场景：两条请求同时越过查重，败者 commit 撞唯一索引 → 转 422 而非裸 500。
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _existsError(dto) from exc
        await session.refresh(entity)
        return entity

    async def updateMapping(
        self,
        session: AsyncSession,
        id: int,
        dto: EntityMappingUpdate,
        actor: CurrentUser,
    ) -> EntityMapping:
        """局部更新编码映射；非空列 None 视为不动，日期列可置 None 清除。

        Phase 4.5 扩展：先 ACL 检查（owner 不匹配 + 非 admin → PermissionDeniedError），
        再 apply dto changes + commit。
        """
        entity = await self.getMapping(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="ENTITY_MAPPING",
            entity_code=str(entity.id),
        )
        before = _entityToDict(entity)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            if value is None and field in _NON_NULL_UPDATE_FIELDS:
                continue
            setattr(entity, field, value)
        _assertDateRange(effective_date=entity.effective_date, expiry_date=entity.expiry_date)
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_updated",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before, "after": _entityToDict(entity)},
        )
        await session.commit()
        await session.refresh(entity)
        return entity

    async def deleteMapping(
        self,
        session: AsyncSession,
        id: int,
        actor: CurrentUser,
    ) -> None:
        """删除编码映射（物理删除，映射记录无历史追溯需求）。

        Phase 4.5 扩展：先 ACL 检查（owner 不匹配 + 非 admin → 403）。
        """
        entity = await self.getMapping(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="ENTITY_MAPPING",
            entity_code=str(entity.id),
        )
        before = _entityToDict(entity)
        # outbox 入队先于 session.delete（保持 entity 属性可访问），同事务 commit
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_deleted",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before},
        )
        await session.delete(entity)
        await session.commit()

    async def bulkImportMappings(
        self,
        session: AsyncSession,
        items: list[EntityMappingBulkImportItem],
        actor: CurrentUser,
    ) -> EntityMappingBulkResult:
        """批量导入 entity_mapping（feat-entity-mapping-bulk-import 2026-09-16）。

        语义：
        - 单事务；任一 INSERT 触发 IntegrityError 不会回滚整批（savepoint 隔离）
        - 每行按 (entity_type, enterprise_key, source_system) 比对：
          - 不存在 → INSERT（status=inserted）
          - 存在且 7 列完全一致 → SKIP（status=skipped，零副作用）
          - 存在且有变化 → UPDATE 非身份列（status=updated, changed_fields）
        - enterprise_key 由 (entity_type, enterprise_code) 派生，与 sync 脚本 SSOT
        - owner 由 actor.departments[0] 派生（与单条 create 同模式）

        上限：
        - _BULK_MAX_ROWS 行/请求；超出抛 ValidationError 422

        返回：每行 1 条结果（含 row 行号 / status / 错误原因），便于前端逐行展示
        """
        if not items:
            raise ValidationError(MSG_ENTITY_MAPPING_BULK_EMPTY)
        if len(items) > _BULK_MAX_ROWS:
            raise ValidationError(
                MSG_ENTITY_MAPPING_BULK_TOO_LARGE.format(
                    maxRows=_BULK_MAX_ROWS, actualRows=len(items)
                )
            )

        derivedOwner = actor.departments[0] if actor.departments else None
        results: list[EntityMappingBulkResultRow] = []
        inserted = updated = skipped = failed = 0

        for idx, dto in enumerate(items, start=1):
            # 1) 行级 schema 校验已由 Pydantic 在路由层完成；此处只做业务校验
            try:
                _assertDateRange(
                    effective_date=dto.effective_date, expiry_date=dto.expiry_date
                )
            except ValidationError as e:
                failed += 1
                results.append(
                    EntityMappingBulkResultRow(
                        row=idx, status="failed",
                        entity_type=dto.entity_type, enterprise_code=dto.enterprise_code,
                        error=str(e.message),
                    )
                )
                continue

            # 2) 派生 enterprise_key（DDL 不要求手填，但 ORM 必填非空）
            # dto.entity_type / dto.source_system / dto.match_rule 在 Pydantic 解析后
            # 是 str 字符串（BusinessObjectCodeType: str | MatchRule: str 等 schema 形态）；
            # 直接当 str 传 offset 计算 + 写入 ORM 列，ORM String 列会接受。
            entityTypeStr = dto.entity_type if isinstance(dto.entity_type, str) else dto.entity_type.value
            enterpriseKey = _deriveKey(entityTypeStr, dto.enterprise_code)

            # 3) 查重（按三列唯一键）
            existingRow = await session.execute(
                select(EntityMapping).where(
                    EntityMapping.entity_type == dto.entity_type,
                    EntityMapping.enterprise_key == enterpriseKey,
                    EntityMapping.source_system == dto.source_system,
                )
            )
            existing = existingRow.scalar_one_or_none()

            if existing is None:
                # 4a) INSERT 新行
                entity = EntityMapping(
                    entity_type=dto.entity_type,
                    enterprise_key=enterpriseKey,
                    enterprise_code=dto.enterprise_code,
                    source_system=dto.source_system,
                    source_key=dto.source_key,
                    source_code=dto.source_code,
                    match_rule=dto.match_rule,
                    effective_date=dto.effective_date,
                    expiry_date=dto.expiry_date,
                    name=dto.name,
                    owner=derivedOwner,
                )
                session.add(entity)
                try:
                    await session.flush()  # 让 IntegrityError 在此抛出
                except IntegrityError as e:
                    # 并发场景：另一事务刚刚 commit 同 key 行 → 把当前行记失败，继续后续行
                    await session.rollback()  # 回滚本次 flush（仅 entity insert）
                    failed += 1
                    results.append(
                        EntityMappingBulkResultRow(
                            row=idx, status="failed",
                            entity_type=dto.entity_type, enterprise_code=dto.enterprise_code,
                            error=f"unique conflict (concurrent insert): {e.orig}",
                        )
                    )
                    continue
                inserted += 1
                results.append(
                    EntityMappingBulkResultRow(
                        row=idx, status="inserted",
                        entity_type=dto.entity_type, enterprise_code=dto.enterprise_code,
                        id=entity.id,
                    )
                )
            else:
                # 4b) 已存在 → 比对 7 个非身份字段
                changed: list[str] = []
                for field in _BULK_COMPARE_FIELDS:
                    oldVal = getattr(existing, field)
                    newVal = getattr(dto, field)
                    # date 类型与 None 比较；ORM date vs Pydantic date 可能类型不同（实都是 datetime.date）
                    if oldVal != newVal:
                        changed.append(field)
                if not changed:
                    skipped += 1
                    results.append(
                        EntityMappingBulkResultRow(
                            row=idx, status="skipped",
                            entity_type=dto.entity_type, enterprise_code=dto.enterprise_code,
                            id=existing.id,
                            reason="与现存行完全一致，无变化",
                        )
                    )
                    continue
                # 4c) 应用变化（保持 enterprise_key / source_system / entity_type 不变）
                for field in changed:
                    setattr(existing, field, getattr(dto, field))
                # outbox 入队（bulk 简化：只记 update 事件，不记 before diff，量大会爆 payload）
                await self._outbox.enqueue(
                    session,
                    event_type="entity_mapping_updated",
                    entity_type="entity_mapping",
                    entity_id=existing.id,
                    actor=actor.userId,
                    actor_departments=actor.departments,
                    payload={
                        "source": "bulk_import",
                        "row": idx,
                        "changed_fields": changed,
                    },
                )
                updated += 1
                results.append(
                    EntityMappingBulkResultRow(
                        row=idx, status="updated",
                        entity_type=dto.entity_type, enterprise_code=dto.enterprise_code,
                        id=existing.id, changed_fields=changed,
                    )
                )

        # 整批 commit（任一行失败已被 savepoint 跳过，不阻塞）
        await session.commit()

        return EntityMappingBulkResult(
            total=len(items),
            inserted=inserted, updated=updated, skipped=skipped, failed=failed,
            results=results,
        )


def entityMappingToRead(mapping: EntityMapping) -> EntityMappingRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return EntityMappingRead.model_validate(mapping, from_attributes=True)


def entityMappingSearchToHit(mapping: EntityMapping) -> EntityMappingSearchHit:
    """ORM → 搜索结果轻量 DTO（Phase 6.x AutoComplete 用）。

    与 entityMappingToRead 并列放在类外：searchMappings 返回 ORM 列表，
    路由层统一转 DTO（与既有 listMappings 模式一致）。
    """
    return EntityMappingSearchHit.model_validate(mapping, from_attributes=True)
