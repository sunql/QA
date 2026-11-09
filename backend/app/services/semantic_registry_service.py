"""语义发布 Registry：运行期只读已发布快照（P1b / D8）。

P0 裁决一（发布快照自包含）的落点：**消费方只读 `semantic_release`**，不回查
治理表、不重新编译。治理表是原地 UPDATE，一旦回查，历史发布内容就会随治理动作
漂移，回滚失效。本模块因此刻意保持极窄的依赖面，并有静态守卫钉住（见单测）。

缓存策略（日级排程 D8 指定）：**进程内缓存 + 写时失效**。
- 激活快照按域作用域缓存；「没有激活发布」也缓存，否则未发布状态下每次问答都打库。
- 写路径（发布 / 激活 / 归档 / 回滚）在**事务提交后**调用 `invalidate()`。
- 缓存带 TTL 兜底：本进程感知不到其他进程 / 手工 SQL 的变更，没有 TTL 就会
  永久服务旧快照。TTL 只是兜底上限，正常路径仍是写时失效。
- 失效与并发读之间的竞态由**代数（generation）**兜住：读开始前记下代数，
  回填缓存前比对；期间发生过失效就放弃回填，绝不把「失效前读到的旧行」塞回缓存。
- 多实例部署时各进程各自失效，TTL 保证最坏情况下的收敛时间；跨进程广播是后续演进。

快照里的 `manifest` 每次访问都从缓存的**规范化 JSON 字符串**解析出新字典：
调用方拿到的永远是私有副本，原地修改不会污染进程级缓存，也不会污染 ORM 实体
（D7b 同类 HIGH：`compileMetric` 的 `dimensionDefaults` 别名问题）。
首次加载时用 `manifest_hash` 校验快照内容，拦下绕过发布路径写入的坏数据。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SemanticReleaseStatus
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.semantic_manifest_schema import canonicalManifestJson
from app.domain.semantic_release_models import SemanticRelease
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_DOMAIN_INVALID,
    MSG_SEMANTIC_RELEASE_ID_INVALID,
    MSG_SEMANTIC_RELEASE_LIMIT_RANGE,
    MSG_SEMANTIC_RELEASE_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_OFFSET_RANGE,
    MSG_SEMANTIC_RELEASE_STATUS_INVALID,
)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
_MAX_OFFSET = 1_000_000
# bigint 上界：超出这个范围的值交给 asyncpg 会抛整型越界（变成 500），先按参数错拒绝。
MAX_RELEASE_ID = 2**63 - 1
# 读路径的快照字节上限（纵深防御的 API 层那一层，见 semantic_manifest_schema.py:56）。
# 正常快照约 1MB；超限说明库里有异常数据，拒绝下发而不是把内存/带宽交给它。
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# 缓存兜底生存期：跨进程 / 手工改库最晚 60 秒收敛。正常路径靠写时失效，不等 TTL。
_CACHE_TTL_SECONDS = 60.0
# 缓存键只有域编码；真实域数量是个位数。长度校验 + 条目上限是防「拿用户输入拼域编码」
# 撑爆缓存的安全阀。这里刻意不查 data_domain 词表：那会把只读路径重新绑回治理表。
_MAX_CACHE_ENTRIES = 16
_MAX_DOMAIN_CODE_LENGTH = 20  # 与 semantic_release.domain_code 列宽一致

_STATUS_VALUES = frozenset(status.value for status in SemanticReleaseStatus)

# 列表查询不取 `manifest_json`：快照约 1MB，列表页取 50 条就是 50MB 的无效传输。
_SUMMARY_COLUMNS = (
    SemanticRelease.id,
    SemanticRelease.version,
    SemanticRelease.domain_code,
    SemanticRelease.status,
    SemanticRelease.schema_version,
    SemanticRelease.manifest_hash,
    SemanticRelease.created_by,
    SemanticRelease.created_time,
    SemanticRelease.activated_time,
    SemanticRelease.note,
)
_SUMMARY_COLUMN_NAMES = frozenset(column.key for column in _SUMMARY_COLUMNS)

# `domainCode=None` 是合法筛选值（全局快照），不能与「不筛选」混用。
_UNSET: Any = object()


class SemanticReleaseIntegrityError(RuntimeError):
    """库里的已发布快照与 `manifest_hash` 不一致（绕过发布路径写入的坏数据）。"""


@dataclass(frozen=True, slots=True)
class SemanticReleaseSnapshot:
    """一份已发布快照的运行期视图（不可变，跨请求共享）。

    `manifest` 是属性而不是字段：每次访问都从规范化 JSON 解析出**新字典**，
    调用方无法通过原地修改影响缓存或其他请求。需要多处使用时先存到局部变量，
    避免重复解析。
    """

    id: int
    version: str
    domain_code: str | None
    status: str
    schema_version: str
    manifest_hash: str
    activated_time: datetime | None
    _manifestJson: str = field(repr=False)

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifestJson)


@dataclass(frozen=True, slots=True)
class SemanticReleaseSummary:
    """列表视图：只有元数据，不带快照正文。"""

    id: int
    version: str
    domain_code: str | None
    status: str
    schema_version: str
    manifest_hash: str
    created_by: str | None
    created_time: datetime
    activated_time: datetime | None
    note: str | None


class _CacheEntry(NamedTuple):
    snapshot: SemanticReleaseSnapshot | None
    loadedAt: float


class SemanticRegistry:
    """只读 Registry：`getActive` / `getById` / `listReleases`。"""

    def __init__(self) -> None:
        # key = domain_code（None = 全局快照）；value 里的 snapshot 为 None 表示
        # 「已确认没有激活发布」（缓存空结果，避免未发布状态每次请求都打库）。
        self._activeCache: dict[str | None, _CacheEntry] = {}
        self._generation = 0

    async def getActive(
        self, session: AsyncSession, *, domainCode: str | None = None
    ) -> SemanticReleaseSnapshot | None:
        """取当前激活快照；没有则返回 `None`（未发布是正常状态，不是错误）。"""
        validateDomainCode(domainCode)

        now = _monotonic()
        cached = self._activeCache.get(domainCode)
        if cached is not None and now - cached.loadedAt < _CACHE_TTL_SECONDS:
            return cached.snapshot

        # 读库前记下代数：期间若发生写时失效，读到的可能是失效前的旧状态，
        # 此时不能回填，否则旧快照会被永久缓存（直到下一次写）。
        generation = self._generation
        statement = select(SemanticRelease).where(
            SemanticRelease.status == SemanticReleaseStatus.ACTIVE.value,
            SemanticRelease.domain_code.is_(None)
            if domainCode is None
            else SemanticRelease.domain_code == domainCode,
        )
        # 部分唯一索引保证同域最多一条 ACTIVE；order/limit 只是让异常数据下行为确定。
        statement = statement.order_by(SemanticRelease.id.desc()).limit(1)
        row = (await session.execute(statement)).scalars().first()

        snapshot = _toSnapshot(row) if row is not None else None
        if generation == self._generation:
            self._store(domainCode, snapshot, now)
        return snapshot

    async def getById(self, session: AsyncSession, releaseId: int) -> SemanticReleaseSnapshot:
        """按主键取快照（详情页 / 回滚目标预览）。不存在 ⇒ 404 语义的领域异常。"""
        if releaseId < 1 or releaseId > MAX_RELEASE_ID:
            raise ValidationError(
                MSG_SEMANTIC_RELEASE_ID_INVALID.format(releaseId=releaseId, max=MAX_RELEASE_ID)
            )
        row = await session.get(SemanticRelease, releaseId)
        if row is None:
            raise NotFoundError(MSG_SEMANTIC_RELEASE_NOT_FOUND.format(releaseId=releaseId))
        return _toSnapshot(row)

    async def listReleases(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        domainCode: Any = _UNSET,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[SemanticReleaseSummary]:
        """发布列表（新→旧）。

        `domainCode` 三态：不传 = 不筛域；`None` = 只筛全局快照；字符串 = 该域。
        """
        _validateListQuery(status=status, limit=limit, offset=offset)
        _validateListDomainCode(domainCode)

        statement = _applyListFilters(select(*_SUMMARY_COLUMNS), status=status, domainCode=domainCode)
        statement = statement.order_by(SemanticRelease.id.desc()).limit(limit).offset(offset)

        rows = (await session.execute(statement)).all()
        return [_toSummary(row) for row in rows]

    async def countReleases(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        domainCode: Any = _UNSET,
    ) -> int:
        """与 `listReleases` 同筛选条件的总数（分页头）。

        筛选条件走同一个构造函数：两处各写一份，迟早出现「rows 2 条、total 5」。
        """
        _validateStatus(status)
        _validateListDomainCode(domainCode)

        statement = _applyListFilters(
            select(func.count()).select_from(SemanticRelease),
            status=status,
            domainCode=domainCode,
        )
        return (await session.execute(statement)).scalar_one()

    def invalidate(self) -> None:
        """写时失效：发布 / 激活 / 归档 / 回滚提交后必须调用。"""
        self._generation += 1
        self._activeCache.clear()

    def _store(self, domainCode: str | None, snapshot: SemanticReleaseSnapshot | None, now: float) -> None:
        if domainCode not in self._activeCache and len(self._activeCache) >= _MAX_CACHE_ENTRIES:
            # 安全阀：异常调用方用任意域编码撑缓存时，整体丢弃而不是无限增长。
            # 真实域只有个位数，正常操作不会触发。
            self._activeCache.clear()
        self._activeCache[domainCode] = _CacheEntry(snapshot=snapshot, loadedAt=now)


def _monotonic() -> float:
    """测试用单点：需要模拟 TTL 过期时 monkeypatch 本函数。"""
    return time.monotonic()


def validateDomainCode(domainCode: str | None) -> None:
    """写路径（D10）与读路径共用的域编码契约。"""
    if domainCode is None:
        return
    if not isinstance(domainCode, str) or not (1 <= len(domainCode) <= _MAX_DOMAIN_CODE_LENGTH):
        raise ValidationError(
            MSG_SEMANTIC_RELEASE_DOMAIN_INVALID.format(code=domainCode, max=_MAX_DOMAIN_CODE_LENGTH)
        )


def _validateListDomainCode(domainCode: Any) -> None:
    """列表的域筛选与 `/active` 同一套校验：`_UNSET` = 不筛域，`None` = 只筛全局。

    列表曾经漏了这条，导致 `?domainCode=`（空串）静默返回空集、超长值进 SQL 绑定，
    而同一个值打到 `/active` 却 422 —— 同一个参数在两条路径上契约必须一致。
    """
    if domainCode is not _UNSET:
        validateDomainCode(domainCode)


def _validateStatus(status: str | None) -> None:
    if status is not None and status not in _STATUS_VALUES:
        raise ValidationError(
            MSG_SEMANTIC_RELEASE_STATUS_INVALID.format(
                status=status, allowed=", ".join(sorted(_STATUS_VALUES))
            )
        )


def _applyListFilters(statement: Any, *, status: str | None, domainCode: Any) -> Any:
    """`listReleases` / `countReleases` 共用的筛选构造（三态域语义）。"""
    if status is not None:
        statement = statement.where(SemanticRelease.status == status)
    if domainCode is not _UNSET:
        statement = statement.where(
            SemanticRelease.domain_code.is_(None)
            if domainCode is None
            else SemanticRelease.domain_code == domainCode
        )
    return statement


def _validateListQuery(*, status: str | None, limit: int, offset: int) -> None:
    _validateStatus(status)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ValidationError(MSG_SEMANTIC_RELEASE_LIMIT_RANGE.format(min=1, max=MAX_LIST_LIMIT, value=limit))
    if offset < 0 or offset > _MAX_OFFSET:
        raise ValidationError(MSG_SEMANTIC_RELEASE_OFFSET_RANGE.format(min=0, max=_MAX_OFFSET, value=offset))


def _toSnapshot(row: SemanticRelease) -> SemanticReleaseSnapshot:
    """规范化 + 完整性校验一次完成。

    JSONB 不保留键序，所以从库读出的快照必须先按发布时的同一规则规范化，
    才能与 `manifest_hash` 比对；这个字符串同时作为缓存的正文，供每次访问解析副本。
    """
    try:
        canonical = canonicalManifestJson(row.manifest_json)
        canonicalBytes = canonical.encode("utf-8")
    except (TypeError, ValueError) as exc:
        # 孤立代理项 / NaN 这类「json.loads 读得进、规范化写不出」的坏行：
        # 与哈希不一致同属完整性失败，统一抛领域异常而不是漏成未分类 500。
        raise SemanticReleaseIntegrityError(
            f"语义发布 {row.id}（version={row.version}）的快照无法规范化序列化：{exc}"
        ) from exc
    if len(canonicalBytes) > MAX_MANIFEST_BYTES:
        raise SemanticReleaseIntegrityError(
            f"语义发布 {row.id}（version={row.version}）的快照体量 {len(canonicalBytes)} 字节"
            f"超过上限 {MAX_MANIFEST_BYTES}"
        )
    actualHash = hashlib.sha256(canonicalBytes).hexdigest()
    if actualHash != row.manifest_hash:
        raise SemanticReleaseIntegrityError(
            f"语义发布 {row.id}（version={row.version}）的快照内容与 manifest_hash 不一致"
        )
    return SemanticReleaseSnapshot(
        id=row.id,
        version=row.version,
        domain_code=row.domain_code,
        status=row.status,
        schema_version=row.schema_version,
        manifest_hash=row.manifest_hash,
        activated_time=row.activated_time,
        _manifestJson=canonical,
    )


def verifySnapshotIntegrity(row: SemanticRelease) -> SemanticReleaseSnapshot:
    """写路径（D10）共用：状态迁移前重新校验哈希与体量。

    与读路径同一个实现 —— 两套校验迟早出现「读拒绝、写放行」。
    """
    return _toSnapshot(row)


def _toSummary(row: Any) -> SemanticReleaseSummary:
    return SemanticReleaseSummary(
        id=row.id,
        version=row.version,
        domain_code=row.domain_code,
        status=row.status,
        schema_version=row.schema_version,
        manifest_hash=row.manifest_hash,
        created_by=row.created_by,
        created_time=row.created_time,
        activated_time=row.activated_time,
        note=row.note,
    )


# 模块级单例：API / 消费方共享缓存；写路径调用 `semanticRegistry.invalidate()`。
semanticRegistry = SemanticRegistry()
