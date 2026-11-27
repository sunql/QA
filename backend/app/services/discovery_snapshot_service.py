"""本体发现：不可变元数据快照服务（WP-ONT-AI A1-2）。

对应 `Harness/changes/2026-11-16-ontology-discovery-ai/plan.md` A1-2 与 D-2 / D-3 / D-9。

职责边界：

- **只读**：采集只经 `SchemaIntrospectionService.introspect`（走
  `BusinessDbAdapter.execute_read_only` + SQL Guard + 库侧只读事务）；
  发现域**不写业务库**（红线之一），策略非只读时直接拒绝。
- **只收结构不收样本**（D-3）：快照只含表 / 列 / 类型 / 可空 / 主键 / 外键 / 注释。
  行级样本属 A2（且依赖 Q3 采样授权白名单）。
- **内容寻址**（D-2）：内容哈希 = 规范化 JSON 的 SHA-256，**与 Manifest 哈希同一把
  尺子**（复用 `semantic_manifest_schema.manifestHash`）。同源同内容 ⇒ 复用既有行；
  内容变化 ⇒ **新增**一行。
- **只增不改**：**快照**行只被 INSERT（服务层无 UPDATE/DELETE 路径，库层另有触发器
  兜底，见迁移 0147）。**发现源目录**（`discovery_source`）是授权目录，可停用 /
  可改策略（见 `updateSource`）——它是「授权的现状」，不是「证据」。

与 `schema_cache` 的分工：`schema_cache` 是「当前态缓存」（内容变化即覆盖，追求快）；
`metadata_snapshot` 是「证据」（只增不改、内容寻址、可被候选/剖析/提案引用）。
两者不合并。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.enums import DataSourceType
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.external_text import sanitizeExternalText, sanitizeIdentifier
from app.domain.models import DataSource
from app.domain.schemas import TableSchemaRead
from app.domain.semantic_manifest_schema import manifestHash
from app.domain.sql_identifier import isValidIdentifier
from app.infrastructure import business_db_pool
from app.services.datasource_service import DataSourceService
from app.services.schema_introspection_service import SchemaIntrospectionService

logger = logging.getLogger(__name__)

# 快照结构版本（与 Manifest 的 `schemaVersion` 同风格，便于将来演进）。
# v2（WP-ONT-AI A2a）：列项新增 `numericPrecision` / `numericScale` / `characterMaxLength`
# ⇒ 快照 JSON 形状变了 ⇒ **旧快照行（v1）与新快照不可比**：同一份 schema 采集两次
# （v1 时代与 v2 时代）会得到不同哈希 ⇒ 发现域会新增一行快照。这是**预期**行为：
# 形状变了就是变了，版本号就是用来显式记录这件事的。
SNAPSHOT_SCHEMA_VERSION = "v2"

# 允许的访问策略模式。发现域**只读**——策略声明可写直接拒绝登记与采集。
ALLOWED_POLICY_MODES = frozenset({"READ_ONLY"})

DEFAULT_ACCESS_POLICY: dict[str, Any] = {"mode": "READ_ONLY"}

# 访问策略**允许的键**。刻意只承认「已实现」的键：未知键直接报错（不静默忽略），
# 否则「我配了 schemas 白名单但其实没生效」这种承诺与实现不一致最危险。
# - mode：访问模式，当前只允许 READ_ONLY
# - schema_owner：**库模式属主**（Oracle schema / MySQL 库），内省时的目标命名空间。
#   ⚠️ 与 `discovery_source.owner`（业务责任人）是两件事，刻意不同名。
ALLOWED_POLICY_KEYS = frozenset({"mode", "schema_owner", "tables"})

# 策略 JSON 体积上限（字节）：策略是配置不是数据载荷，超大即拒绝。
MAX_POLICY_BYTES = 16384

#: 策略里 tables 白名单的上限（防止把策略当数据载荷）。
MAX_POLICY_TABLES = 200

# 快照体积上限（字节）：快照内容会**整份**回给每个读它的人（详情端点）
# ⇒ 超大快照既是响应放大器，也是把外部文本搬进内存的入口。超限即拒绝采集。
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

# 发现源可被修改的字段。`datasource_id`（授权根）与 `name`（主键语义）**不可改**：
# 改 `datasource_id` 等于换一条被授权的连接。
UPDATABLE_SOURCE_FIELDS = ("status", "owner", "access_policy", "note")

# 发现源状态取值。
STATUS_ACTIVE = "ACTIVE"
STATUS_DISABLED = "DISABLED"

MSG_SOURCE_NAME_REQUIRED = "发现源名称不能为空"
MSG_SOURCE_NAME_DUPLICATED = "发现源名称已存在：{name}"
MSG_SOURCE_NOT_FOUND = "发现源 {id} 不存在"
MSG_SOURCE_DISABLED = "发现源已停用，不允许采集快照：{id}"
MSG_SOURCE_DUPLICATED = "发现源已存在：{name}"
MSG_SOURCE_OWNER_REQUIRED = "发现源责任人不能为空"
MSG_SOURCE_FIELD_NOT_UPDATABLE = "发现源不支持修改该字段：{field}"
MSG_SOURCE_STATUS_INVALID = "发现源状态非法：{status}（仅允许 ACTIVE / DISABLED）"
MSG_SOURCE_POLICY_NOT_OBJECT = "发现源访问策略必须是 JSON 对象"
MSG_SOURCE_POLICY_UNKNOWN_KEY = (
    "发现源访问策略含未知键：{keys}（仅支持 mode / schema_owner / tables）"
)
MSG_SOURCE_POLICY_TABLES_INVALID = "访问策略 tables 必须是非空的表名数组"
MSG_SOURCE_POLICY_TABLES_TOO_MANY = "访问策略 tables 过多：{count} > {limit}"
MSG_SNAPSHOT_POLICY_NO_TABLES_MATCHED = (
    "访问策略 tables 里的表在该源上一个都不存在：{tables}（检查 owner/大小写）"
)
MSG_SOURCE_POLICY_MODE_INVALID = "发现源访问策略 mode 非法：{mode}（仅支持 READ_ONLY）"
MSG_SOURCE_POLICY_SCHEMA_OWNER_INVALID = "发现源访问策略 schema_owner 必须是非空字符串"
MSG_SOURCE_POLICY_TOO_LARGE = "发现源访问策略过大：{size} 字节 > {limit} 字节"
MSG_SOURCE_POLICY_NULL = "发现源访问策略不能为 null（要回到只读请显式传 mode=READ_ONLY）"
MSG_SOURCE_POLICY_SCHEMA_OWNER_ORACLE_ONLY = (
    "发现源访问策略 schema_owner 仅适用于 Oracle 数据源（当前类型：{type}）"
)
MSG_DATASOURCE_INACTIVE = "数据源 {id} 已停用，不允许采集快照"
MSG_SNAPSHOT_NOT_FOUND = "元数据快照 {id} 不存在"
MSG_SNAPSHOT_CONFLICT = "元数据快照写入冲突，请重试"
MSG_SNAPSHOT_TOO_LARGE = (
    "元数据快照过大：{size} 字节 > {limit} 字节"
    "（请缩小采集范围：在发现源访问策略里用 tables 限定表，或按 owner 拆分源）"
)


@dataclass(frozen=True)
class SnapshotCapture:
    """一次采集的结果（不可变）。"""

    snapshotId: int
    sourceId: int
    schemaHash: str
    tableCount: int
    columnCount: int
    capturedAt: datetime
    reused: bool


@dataclass(frozen=True)
class ConnectOverride:
    """只读内省的**连接地址覆盖**（不可变）。

    存在的唯一理由：宿主机上跑验证脚本时，库里的容器内网名（`host.docker.internal`）
    不可解析 ⇒ 需要换成宿主机可达地址。

    边界（刻意收窄）：

    - 只改**连接地址**，不改数据源配置本身（库里的 `data_source` 行原样不动）；
    - 只用于**内省**这一条只读路径，不写业务库；
    - 不经 HTTP 暴露（只在服务层出现，API 层没有对应参数）⇒ 不构成
      「任意主机内省」的能力。
    """

    host: str | None = None
    port: int | None = None


def withConnectOverride(
    datasource: DataSource, override: ConnectOverride | None
) -> DataSource:
    """按需替换连接地址，返回**新的** DataSource（不改原对象）。"""

    if override is None:
        return datasource
    if override.host is None and override.port is None:
        return datasource
    return DataSource(
        id=datasource.id,
        name=datasource.name,
        type=datasource.type,
        host=override.host or datasource.host,
        port=override.port or datasource.port,
        database_name=datasource.database_name,
        username=datasource.username,
        password_encrypted=datasource.password_encrypted,
        description=datasource.description,
        is_active=datasource.is_active,
        is_default=datasource.is_default,
        created_by=datasource.created_by,
        oracle_version=datasource.oracle_version,
    )


def sanitizeComment(comment: str | None) -> str | None:
    """清洗**不可信外部文本**（业务库注释）：注入防御 + PII 掩码，清空归一为 None。

    快照内容会回给所有登录用户、并在 A2/A3 进入 LLM 上下文 ⇒ 尖括号（标签逃逸）、
    行分隔符（裸行注入）、不可见字符（零宽 / 双向覆写）与高置信 PII 必须在
    **入库前**剔除，不能指望出口再净化。

    实现收敛到 `app/domain/external_text.py`（单一事实源）。
    """

    return sanitizeExternalText(comment)


def sanitizeIdentifierValue(value: str | None) -> str | None:
    """清洗**标识符**（表名 / 列名 / 属主 / 类型名 / 外键表名列名）。

    与 `sanitizeComment` 的差别：标识符是**结构身份**，不做 PII 掩码
    （掩码会改掉表名语义 ⇒ 同一张表被当成两张 ⇒ 哈希抖动）。
    """

    return sanitizeIdentifier(value)


def unprofilableTableNames(snapshotJson: Mapping[str, Any]) -> list[str]:
    """快照里**不可剖析**的表名（表名 / 属主 / 列名任一不能作 SQL 标识符）。

    为什么需要（2026-11-27，feat/discovery-skip-unprofilable-tables）：
    Oracle 的**回收站对象**（`BIN$W81vJmasuczgY0bNqMBvog==$0`）含 `=` ⇒
    拼 SQL 前必被护栏拒 ⇒ 一次发现运行会因为「快照里混进一张回收站表」而整轮失败。

    这份清单给前端用：选择器**不显示**这些表 + 告诉用户「已隐藏 N 张」，
    免得用户以为自己漏选了表。口径与 `discovery_profile_sql.assertIdentifier`
    同源（`domain.sql_identifier.isValidIdentifier`）⇒ 两边不会漂移。

    返回表名（去重、保序）；快照为空或结构异常 ⇒ 返回空列表（不抛错）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for table in snapshotJson.get("tables") or []:
        if not isinstance(table, Mapping):
            continue
        name = str(table.get("tableName") or "")
        if not name or name in seen:
            continue
        owner = str(table.get("owner") or "")
        columns = table.get("columns") or []
        columnNames = [
            str(column.get("columnName") or "")
            for column in columns
            if isinstance(column, Mapping)
        ]
        if (
            isValidIdentifier(name)
            and (not owner or isValidIdentifier(owner))
            and all(isValidIdentifier(columnName) for columnName in columnNames)
        ):
            continue
        seen.add(name)
        names.append(name)
    return names


def normalizeSnapshot(tables: Sequence[TableSchemaRead]) -> dict[str, Any]:
    """把内省结果规范化成**确定性**的 JSON 结构（内容寻址的前提）。

    规范化规则：

    - 表按 `(owner, tableName)` 排序，列按列名排序 ⇒ **输入顺序不影响哈希**；
    - 主键**保留原顺序**（复合主键的列序本身有语义）；
    - 外键按 `(columnName, refTable, refColumn)` 排序；
    - 字段名统一为 camelCase，与 Manifest 分区的字段风格一致；
    - 注释走 `sanitizeComment` 清洗（外部不可信文本）。

    不修改入参（只读遍历 + 新建 dict）。
    """

    normalized: list[dict[str, Any]] = []
    for table in tables:
        # 列 / 外键先按**净化后**的名字排序（净化可能改变名字）⇒ 哈希稳定。
        columns = [
            {
                "columnName": sanitizeIdentifierValue(column.column_name) or "",
                "dataType": sanitizeIdentifierValue(column.data_type) or "",
                "nullable": bool(column.nullable),
                "comment": sanitizeComment(column.comment),
                # 精度 / 长度（L-9）：数值不是外部文本 ⇒ 无需净化；
                # 「采集不到」= None（与 comment 同口径：键恒在、值可为 None）。
                "numericPrecision": column.numeric_precision,
                "numericScale": column.numeric_scale,
                "characterMaxLength": column.character_max_length,
            }
            for column in table.columns
        ]
        columns.sort(key=lambda item: item["columnName"])
        foreignKeys = [
            {
                "columnName": sanitizeIdentifierValue(foreignKey.column_name) or "",
                "refTable": sanitizeIdentifierValue(foreignKey.ref_table) or "",
                "refColumn": sanitizeIdentifierValue(foreignKey.ref_column) or "",
            }
            for foreignKey in table.foreign_keys
        ]
        foreignKeys.sort(
            key=lambda item: (item["columnName"], item["refTable"], item["refColumn"])
        )
        normalized.append(
            {
                "tableName": sanitizeIdentifierValue(table.table_name) or "",
                "owner": sanitizeIdentifierValue(table.owner) or "",
                "comment": sanitizeComment(table.comment),
                "primaryKeys": [
                    sanitizeIdentifierValue(primaryKey) or ""
                    for primaryKey in (table.primary_keys or [])
                ],
                "columns": columns,
                "foreignKeys": foreignKeys,
            }
        )
    normalized.sort(key=lambda item: (item["owner"], item["tableName"]))
    return {"schemaVersion": SNAPSHOT_SCHEMA_VERSION, "tables": normalized}


def computeSnapshotHash(snapshot: Mapping[str, Any]) -> str:
    """快照内容哈希 = 规范化 JSON 的 SHA-256（**与 Manifest 哈希同一把尺子**，D-2）。"""

    return manifestHash(dict(snapshot))


def normalizeStatus(status: Any) -> str:
    """归一发现源状态（大小写不敏感；非法值拒绝）。"""

    normalized = str(status or "").strip().upper()
    if normalized not in (STATUS_ACTIVE, STATUS_DISABLED):
        raise ValidationError(MSG_SOURCE_STATUS_INVALID.format(status=status))
    return normalized


def normalizeOwner(owner: Any) -> str:
    """归一发现源业务责任人（空白即拒绝）。"""

    normalized = str(owner or "").strip()
    if not normalized:
        raise ValidationError(MSG_SOURCE_OWNER_REQUIRED)
    return normalized


def normalizePolicy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验并补全访问策略（缺省只读；未知键 / 非法取值 / 超大一律拒绝）。

    「静默忽略未知键」是最危险的失败模式：调用方以为配了白名单，实际没生效。
    """

    if policy is None:
        return dict(DEFAULT_ACCESS_POLICY)
    if not isinstance(policy, Mapping):
        raise ValidationError(MSG_SOURCE_POLICY_NOT_OBJECT)
    unknown = sorted(set(policy) - ALLOWED_POLICY_KEYS)
    if unknown:
        raise ValidationError(
            MSG_SOURCE_POLICY_UNKNOWN_KEY.format(keys=", ".join(unknown))
        )
    normalized = dict(policy)
    mode = normalized.get("mode", DEFAULT_ACCESS_POLICY["mode"])
    if not isinstance(mode, str) or mode not in ALLOWED_POLICY_MODES:
        raise ValidationError(MSG_SOURCE_POLICY_MODE_INVALID.format(mode=mode))
    normalized["mode"] = mode
    if "schema_owner" in normalized:
        schemaOwner = normalized["schema_owner"]
        if not isinstance(schemaOwner, str) or not schemaOwner.strip():
            raise ValidationError(MSG_SOURCE_POLICY_SCHEMA_OWNER_INVALID)
        normalized["schema_owner"] = schemaOwner.strip()
    if "tables" in normalized:
        normalized["tables"] = normalizePolicyTables(normalized["tables"])
    size = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
    if size > MAX_POLICY_BYTES:
        raise ValidationError(
            MSG_SOURCE_POLICY_TOO_LARGE.format(size=size, limit=MAX_POLICY_BYTES)
        )
    return normalized


_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def normalizePolicyTables(raw: Any) -> list[str]:
    """校验并归一 `tables` 白名单（去空白、去重保序、严格标识符）。"""

    if not isinstance(raw, list) or not raw:
        raise ValidationError(MSG_SOURCE_POLICY_TABLES_INVALID)
    if len(raw) > MAX_POLICY_TABLES:
        raise ValidationError(
            MSG_SOURCE_POLICY_TABLES_TOO_MANY.format(
                count=len(raw), limit=MAX_POLICY_TABLES
            )
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValidationError(MSG_SOURCE_POLICY_TABLES_INVALID)
        name = item.strip()
        if not name or not _TABLE_IDENTIFIER.match(name):
            raise ValidationError(MSG_SOURCE_POLICY_TABLES_INVALID)
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(name)
    return normalized


def applyTablePolicy(
    tables: Sequence[TableSchemaRead], policy: Mapping[str, Any]
) -> list[TableSchemaRead]:
    """按策略 `tables` 收窄采集范围（大小写不敏感；空交集 ⇒ 拒绝而不是静默空快照）。"""

    wanted = policy.get("tables")
    if not wanted:
        return list(tables)
    wantedUpper = {str(name).upper() for name in wanted}
    filtered = [table for table in tables if table.table_name.upper() in wantedUpper]
    if not filtered:
        raise ValidationError(
            MSG_SNAPSHOT_POLICY_NO_TABLES_MATCHED.format(tables="、".join(wanted))
        )
    return filtered


def assertPolicyMatchesDatasource(
    policy: Mapping[str, Any], datasource: DataSource
) -> None:
    """`schema_owner` 只对 **Oracle** 有意义（Oracle 是 owner 命名空间）。

    PG / MySQL 的内省层会**静默忽略** owner（恒查连接默认 schema）⇒ 在非 Oracle
    数据源上配 `schema_owner` 属于「配了但没生效」——正是本域要拒绝的
    「承诺与实现不一致」。故显式拒绝。
    """

    if "schema_owner" not in policy:
        return
    if str(datasource.type).lower() != DataSourceType.ORACLE.value:
        raise ValidationError(
            MSG_SOURCE_POLICY_SCHEMA_OWNER_ORACLE_ONLY.format(type=datasource.type)
        )


class DiscoverySnapshotService:
    """发现源登记 + 不可变快照采集。"""

    def __init__(
        self,
        *,
        introspectionService: SchemaIntrospectionService | None = None,
        datasourceService: DataSourceService | None = None,
    ) -> None:
        self._introspection = (
            introspectionService if introspectionService is not None else SchemaIntrospectionService()
        )
        self._datasources = (
            datasourceService if datasourceService is not None else DataSourceService()
        )

    # ------------------------------------------------------------ 发现源

    async def registerSource(
        self,
        session: AsyncSession,
        *,
        name: str,
        datasourceId: int,
        owner: str,
        accessPolicy: Mapping[str, Any] | None = None,
        status: str = STATUS_ACTIVE,
        note: str | None = None,
        createdBy: str | None = None,
    ) -> DiscoverySource:
        """登记一个发现源（只引用既有 `data_source`，**不新建连接**，D-9）。"""

        normalizedName = (name or "").strip()
        if not normalizedName:
            raise ValidationError(MSG_SOURCE_NAME_REQUIRED)
        # 登记即校验（MEDIUM-3 / LOW-2）：非法状态 / 空责任人不再「先落库、
        # 采集时才炸」⇒ 库里不留死行。
        normalizedStatus = normalizeStatus(status)
        normalizedOwner = normalizeOwner(owner)
        policy = normalizePolicy(accessPolicy)
        datasource = await self._datasources.get(session, datasourceId)
        assertPolicyMatchesDatasource(policy, datasource)

        duplicated = await session.scalar(
            select(DiscoverySource.id).where(DiscoverySource.name == normalizedName)
        )
        if duplicated is not None:
            raise ConflictError(MSG_SOURCE_NAME_DUPLICATED.format(name=normalizedName))

        source = DiscoverySource(
            name=normalizedName,
            datasource_id=datasourceId,
            owner=normalizedOwner,
            access_policy=policy,
            status=normalizedStatus,
            note=note,
            created_by=createdBy,
        )
        try:
            # SAVEPOINT：唯一约束冲突只回滚本次插入，不牵连调用方事务
            # （M-7：不回滚就抛会让 session 处于 aborted 态，后续调用方撞
            # PendingRollbackError）。
            async with session.begin_nested():
                session.add(source)
                await session.flush()
        except IntegrityError as exc:
            logger.warning(
                "发现源登记冲突（name=%s）: %s", normalizedName, exc, exc_info=True
            )
            raise ConflictError(MSG_SOURCE_DUPLICATED.format(name=normalizedName)) from exc
        return source

    async def listSources(
        self,
        session: AsyncSession,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[DiscoverySource]:
        statement = select(DiscoverySource).order_by(DiscoverySource.id)
        statement = statement.limit(limit).offset(offset)
        result = await session.execute(statement)
        return list(result.scalars().all())

    async def getSource(self, session: AsyncSession, sourceId: int) -> DiscoverySource:
        source = await session.get(DiscoverySource, sourceId)
        if source is None:
            raise NotFoundError(MSG_SOURCE_NOT_FOUND.format(id=sourceId))
        return source

    async def updateSource(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        changes: Mapping[str, Any],
    ) -> DiscoverySource:
        """更新发现源的**非授权根**字段（停用 / 改责任人 / 改策略 / 改备注）。

        - `datasource_id` 与 `name` 不可改：前者是授权根（换它 = 换一条被授权的
          连接），后者是主键语义 ⇒ 出现在 `changes` 里直接拒绝。
        - `status` 是**撤销授权的唯一入口**：没有它，`DISABLED` 就是个够不着的
          取值，「授权可撤销」是空承诺。
        """

        source = await self.getSource(session, sourceId)
        unknown = sorted(set(changes) - set(UPDATABLE_SOURCE_FIELDS))
        if unknown:
            raise ValidationError(
                MSG_SOURCE_FIELD_NOT_UPDATABLE.format(field=", ".join(unknown))
            )
        if "status" in changes:
            source.status = normalizeStatus(changes["status"])
        if "owner" in changes:
            source.owner = normalizeOwner(changes["owner"])
        if "access_policy" in changes:
            policy = changes["access_policy"]
            if policy is None:
                # 传 null ≠ 「不改」：静默重置策略会把授权悄悄改掉 ⇒ 显式拒绝。
                raise ValidationError(MSG_SOURCE_POLICY_NULL)
            normalizedPolicy = normalizePolicy(policy)
            datasource = await self._datasources.get(session, source.datasource_id)
            assertPolicyMatchesDatasource(normalizedPolicy, datasource)
            source.access_policy = normalizedPolicy
        if "note" in changes:
            source.note = changes["note"]
        await session.flush()
        return source

    # ------------------------------------------------------------ 快照

    async def captureSnapshot(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        createdBy: str | None = None,
        connectOverride: ConnectOverride | None = None,
    ) -> SnapshotCapture:
        """采集一次快照（**内容寻址、幂等**）。

        同一源 + 同一内容 ⇒ 复用既有行（`reused=True`），**不新增**；
        内容变化 ⇒ 新增一行，旧行不动（只增不改）。

        `connectOverride` 只改**本次内省的连接地址**（宿主机跑验证脚本用），
        不改库里的数据源配置 ⇒ 见 `ConnectOverride` 的边界说明。
        """

        source = await self.getSource(session, sourceId)
        if source.status != STATUS_ACTIVE:
            raise ValidationError(MSG_SOURCE_DISABLED.format(id=sourceId))
        # 策略校验在此处再走一遍：库里可能存着旧版本写入的非法策略（登记时已拦，
        # 但历史行不迁就）⇒ 采集前必须复检（发现域只读是红线）。
        policy = normalizePolicy(source.access_policy)

        datasource = await self._datasources.get(session, source.datasource_id)
        if not datasource.is_active:
            # M-6：授权根已停用 ⇒ 不再连接（停用数据源还去连 = 撤销没生效）。
            raise ValidationError(MSG_DATASOURCE_INACTIVE.format(id=datasource.id))
        # 采集前复检策略适用范围（数据源类型可能已改 ⇒ 承诺不能失效）。
        assertPolicyMatchesDatasource(policy, datasource)
        effective = withConnectOverride(datasource, connectOverride)
        tables = await self._introspectWithOverride(
            effective, datasource, connectOverride, policy
        )
        tables = applyTablePolicy(tables, policy)
        snapshot = normalizeSnapshot(tables)
        snapshotSize = len(
            json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        )
        if snapshotSize > MAX_SNAPSHOT_BYTES:
            raise ValidationError(
                MSG_SNAPSHOT_TOO_LARGE.format(size=snapshotSize, limit=MAX_SNAPSHOT_BYTES)
            )
        schemaHash = computeSnapshotHash(snapshot)
        tableCount = len(snapshot["tables"])
        columnCount = sum(len(table["columns"]) for table in snapshot["tables"])

        existing = await self._findByHash(session, sourceId, schemaHash)
        if existing is not None:
            return self._captureResult(existing, reused=True)

        row = MetadataSnapshot(
            source_id=sourceId,
            schema_hash=schemaHash,
            snapshot_json=snapshot,
            table_count=tableCount,
            column_count=columnCount,
            captured_at=datetime.now(UTC),
            created_by=createdBy,
        )
        try:
            # SAVEPOINT：唯一约束冲突只回滚本次插入，不牵连调用方事务。
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            logger.warning(
                "快照写入冲突（source=%s hash=%s）: %s",
                sourceId,
                schemaHash[:12],
                exc,
                exc_info=True,
            )
            conflicted = await self._findByHash(session, sourceId, schemaHash)
            if conflicted is not None:
                return self._captureResult(conflicted, reused=True)
            raise ConflictError(MSG_SNAPSHOT_CONFLICT) from exc
        return self._captureResult(row, reused=False)

    async def _introspectWithOverride(
        self,
        effective: DataSource,
        datasource: DataSource,
        connectOverride: ConnectOverride | None,
        policy: Mapping[str, Any],
    ) -> list[TableSchemaRead]:
        """内省，必要时**隔离连接池**（连接地址覆盖专用）。

        代码审查 MEDIUM-7：`business_db_pool.get_adapter(datasourceId, ds)` **按 id
        缓存**，命中即返回旧适配器、忽略传入的 `ds` ⇒ 覆盖会被池化适配器静默吃掉。
        修法：覆盖生效期间**释放**该数据源的池化适配器（采集前一次、采集后一次），
        采集后那一次保证「覆盖用的地址」不留在共享池里。

        没有覆盖时**不动**共享连接池（避免打断别的调用方的连接）。
        """

        owner = policy.get("schema_owner")
        overrideInEffect = connectOverride is not None and (
            connectOverride.host is not None or connectOverride.port is not None
        )
        if not overrideInEffect:
            return await self._introspection.introspect(effective, owner=owner)
        await business_db_pool.dispose_adapter(datasource.id)
        try:
            return await self._introspection.introspect(effective, owner=owner)
        finally:
            await business_db_pool.dispose_adapter(datasource.id)

    async def listSnapshots(
        self,
        session: AsyncSession,
        *,
        sourceId: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MetadataSnapshot]:
        statement = select(MetadataSnapshot)
        if sourceId is not None:
            statement = statement.where(MetadataSnapshot.source_id == sourceId)
        statement = statement.order_by(
            MetadataSnapshot.captured_at.desc(), MetadataSnapshot.id.desc()
        )
        statement = statement.limit(limit).offset(offset)
        result = await session.execute(statement)
        return list(result.scalars().all())

    async def getSnapshot(self, session: AsyncSession, snapshotId: int) -> MetadataSnapshot:
        row = await session.get(MetadataSnapshot, snapshotId)
        if row is None:
            raise NotFoundError(MSG_SNAPSHOT_NOT_FOUND.format(id=snapshotId))
        return row

    async def countSnapshots(
        self, session: AsyncSession, *, sourceId: int | None = None
    ) -> int:
        statement = select(func.count()).select_from(MetadataSnapshot)
        if sourceId is not None:
            statement = statement.where(MetadataSnapshot.source_id == sourceId)
        return int(await session.scalar(statement) or 0)

    async def countSources(self, session: AsyncSession) -> int:
        """发现源总数（分页响应的 `total` 语义 = 全量条数，不是本页长度）。"""

        return int(
            await session.scalar(
                select(func.count()).select_from(DiscoverySource)
            )
            or 0
        )

    async def countSnapshotsBySource(
        self, session: AsyncSession, *, sourceIds: Sequence[int] | None = None
    ) -> dict[int, int]:
        """一次 GROUP BY 拿快照计数（避免列表端点 N+1，M-6）。

        `sourceIds` 限定只统计**当前页**的发现源：整表 GROUP BY 会把全库快照
        都聚合一遍，而响应只会用到本页那几行的计数（MEDIUM-10）。
        """

        statement = (
            select(MetadataSnapshot.source_id, func.count())
            .group_by(MetadataSnapshot.source_id)
        )
        if sourceIds is not None:
            ids = list(sourceIds)
            if not ids:
                return {}
            statement = statement.where(MetadataSnapshot.source_id.in_(ids))
        rows = await session.execute(statement)
        return {int(sourceId): int(count) for sourceId, count in rows.all()}

    # ------------------------------------------------------------ 内部

    async def _findByHash(
        self, session: AsyncSession, sourceId: int, schemaHash: str
    ) -> MetadataSnapshot | None:
        result = await session.execute(
            select(MetadataSnapshot).where(
                MetadataSnapshot.source_id == sourceId,
                MetadataSnapshot.schema_hash == schemaHash,
            )
        )
        return result.scalars().first()

    def _captureResult(
        self, row: MetadataSnapshot, *, reused: bool
    ) -> SnapshotCapture:
        return SnapshotCapture(
            snapshotId=row.id,
            sourceId=row.source_id,
            schemaHash=row.schema_hash,
            tableCount=row.table_count,
            columnCount=row.column_count,
            capturedAt=row.captured_at,
            reused=reused,
        )

