"""业务数据源 Schema 自动发现与缓存服务（5.7）。

从业务数据库的数据字典读取表/列/主键/外键结构，组装为结构化清单。
按数据源类型分派查询：
- Oracle：ALL_TAB_COLUMNS / ALL_CONSTRAINTS + ALL_CONS_COLUMNS
- PostgreSQL / MySQL：information_schema（CURRENT_SCHEMA / DATABASE）

所有查询均为只读 SELECT，经 BusinessDbAdapter.execute_read_only 的 SQL Guard 校验。
发现结果写入 schema_cache 表，按 (datasource_id, schema_name) 唯一 + MD5 版本，
版本未变化时复用缓存行，避免无效写入。schema_name 为 Oracle owner 命名空间
（如 ZJTH/THBI）；PG/MySQL 恒为 ''（连接默认）。连接用户在某个 owner 下看得到
多少表，就取决于该 owner 对其可见对象（ALL_TABLES 等数据字典语义）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.enums import DataSourceType
from app.domain.exceptions import DataSourceError, ValidationError
from app.domain.models import DataSource, SchemaCache
from app.domain.schemas import (
    ColumnSchemaRead,
    ForeignKeySchemaRead,
    MissingColumnRead,
    OntologyDriftReport,
    SchemaIntrospectResponse,
    TableSchemaRead,
)
from app.infrastructure.business_db_pool import BusinessDbAdapter, get_adapter
from app.services.messages_zh import (
    MSG_DATASOURCE_SCHEMA_READ_FAILED,
    MSG_DATASOURCE_SCHEMA_READ_FAILED_DETAIL,
    MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_DETAIL,
    MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_EXCEEDED,
    MSG_DATASOURCE_TYPE_NOT_SUPPORTED,
    MSG_DATASOURCE_USERNAME_INVALID_ORACLE_OWNER,
)

logger = logging.getLogger(__name__)

# Oracle schema owner 字符白名单：用户名规范化后内联为 SQL 字面量，杜绝注入
_ORACLE_OWNER_PATTERN = re.compile(r"[A-Z0-9_$#]+")

# 单数据源允许发现的表数量上限在 Settings.schema_max_tables（env SCHEMA_MAX_TABLES，
# 默认 3000）：避免超大 schema 撑爆 introspection 响应体与缓存，同时可按大型库调高。

# 漂移告警（2-4）注入 NL2SQL system prompt 的文案：明确告知模型不得引用已漂移的表/字段，
# 避免生成 ORA-00942 类 SQL。告警位于受信的 system prompt（非用户数据），按指令处理。
_DRIFT_WARNING_HEADER = (
    "警告：以下本体表/字段已漂移（在当前数据源中已不存在，与数据库 schema 不一致），"
    "请勿在 SQL 中引用："
)
_DRIFT_WARNING_TRAILER = (
    "若查询确实需要这些对象，请改用数据库中实际存在的表/字段，或判定该问题无法回答。"
)

AdapterProvider = Callable[[int, DataSource], BusinessDbAdapter]


def _oracleOwner(username: str) -> str:
    """把用户名规范化为 Oracle schema owner（大写 + 字符白名单校验）。"""
    owner = username.strip().upper()
    if not owner or not _ORACLE_OWNER_PATTERN.fullmatch(owner):
        raise ValidationError(MSG_DATASOURCE_USERNAME_INVALID_ORACLE_OWNER, detail=f"收到: {username!r}")
    return owner


def _normalizeSchemaName(ds: DataSource, owner: str | None) -> str:
    """把调用方传入的 owner 归一为「缓存键 + 内省 SQL 共用」的 schema 名。

    Oracle：显式非空 owner → strip().upper() + 字符白名单（非法抛 ValidationError）；
    空/缺省 → 连接用户默认 owner（UPPER(username)，向后兼容先前后端行为）。
    PG/MySQL：忽略 owner 恒返回 ''（连接默认，单份缓存语义）——避免把非 Oracle
    数据源误缓存到用户指定 owner 名下（_queryByType 亦忽略非 Oracle 的 owner）。
    返回新字符串，不改动入参。
    """
    try:
        dsType = DataSourceType(ds.type)
    except ValueError:
        return ""
    if dsType is DataSourceType.ORACLE:
        if owner is not None and owner.strip():
            return _oracleOwner(owner)
        return _oracleOwner(ds.username)
    return ""


def _emptyTable() -> dict[str, Any]:
    return {"owner": "", "columns": [], "primary_keys": [], "foreign_keys": []}


def _mergeRows(columnRows: list[dict], pkRows: list[dict], fkRows: list[dict]) -> dict[str, dict]:
    """按表名合并三份数据字典查询结果。"""
    tables: dict[str, dict] = {}
    for row in columnRows:
        entry = tables.setdefault(row["table_name"], _emptyTable())
        entry["owner"] = row.get("owner", "")
        entry["columns"].append(
            {
                "column_name": row["column_name"],
                "data_type": row["data_type"],
                "nullable": bool(row["nullable"]),
            }
        )
    for row in pkRows:
        tables.setdefault(row["table_name"], _emptyTable())["primary_keys"].append(row["column_name"])
    for row in fkRows:
        tables.setdefault(row["table_name"], _emptyTable())["foreign_keys"].append(
            {
                "column_name": row["column_name"],
                "ref_table": row["ref_table"],
                "ref_column": row["ref_column"],
            }
        )
    return tables


class SchemaIntrospectionService:
    """业务数据源 schema 发现与缓存。"""

    def __init__(self, *, adapterProvider: AdapterProvider = get_adapter) -> None:
        self._adapterProvider = adapterProvider

    async def introspect(self, ds: DataSource, *, owner: str | None = None) -> list[TableSchemaRead]:
        """读取业务库数据字典，返回结构化表清单（不落库）。

        owner 仅对 Oracle 有意义（owner 命名空间，如 THBI）；缺省回退连接用户
        默认 owner（_oracleOwner(ds.username)）。PG/MySQL 忽略 owner，恒查连接
        默认 schema。
        """
        adapter = self._adapterProvider(ds.id, ds)
        try:
            merged = await self._queryByType(adapter, ds, owner)
        except ValidationError:
            raise  # 校验类错误（如 owner 非法）保持原样，便于 API 返回精确语义
        except Exception as exc:  # noqa: BLE001 - 连接/解析异常统一包装为领域异常
            # 完整异常写服务端日志；对外仅暴露友好信息，避免泄露 DB 用户名/DSN/SQL 细节
            logger.error("数据源 %s schema 读取失败: %s", ds.id, exc, exc_info=True)
            raise DataSourceError(
                MSG_DATASOURCE_SCHEMA_READ_FAILED.format(datasourceId=ds.id),
                detail=MSG_DATASOURCE_SCHEMA_READ_FAILED_DETAIL,
            ) from exc
        tables = self._assembleTables(merged)
        maxTables = getSettings().schemaMaxTables
        if len(tables) > maxTables:
            raise DataSourceError(
                MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_EXCEEDED.format(
                    datasourceId=ds.id, tableCount=len(tables), maxTables=maxTables
                ),
                detail=MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_DETAIL,
            )
        return tables

    async def listSchemas(self, ds: DataSource) -> list[str]:
        """列出该连接可见的 Oracle owner（schema）命名空间，用于向导「选择 Schema」。

        Oracle：ALL_TABLES 的 DISTINCT owner（只读，连接用户可见对象所在的所有者，
        含自身 owner 与被授权的其他 owner，如 ZJTH 连接可同时见 ZJTH 与 THBI）。
        PG/MySQL：返回 []——无显式多 schema 概念，向导不显示 schema 选择步。
        owner 经字符白名单过滤后原样返回（大写），杜绝脏 owner 回传前端。
        """
        adapter = self._adapterProvider(ds.id, ds)
        try:
            dsType = DataSourceType(ds.type)
        except ValueError:
            dsType = None
        if dsType is not DataSourceType.ORACLE:
            return []
        try:
            rows = await adapter.execute_read_only(_ORACLE_SCHEMAS_SQL)
        except Exception as exc:  # noqa: BLE001 - 连接/解析异常统一包装为领域异常
            logger.error("数据源 %s schema 列表读取失败: %s", ds.id, exc, exc_info=True)
            raise DataSourceError(
                MSG_DATASOURCE_SCHEMA_READ_FAILED.format(datasourceId=ds.id),
                detail=MSG_DATASOURCE_SCHEMA_READ_FAILED_DETAIL,
            ) from exc
        schemas: set[str] = set()
        for row in rows:
            owner = (row.get("owner") or "").strip().upper()
            if owner and _ORACLE_OWNER_PATTERN.fullmatch(owner):
                schemas.add(owner)
        return sorted(schemas)

    async def _queryByType(
        self,
        adapter: BusinessDbAdapter,
        ds: DataSource,
        owner: str | None = None,
    ) -> dict[str, dict]:
        """按数据源类型执行数据字典查询并合并结果；未知类型抛 ValidationError。

        owner 仅 Oracle 生效：显式传 owner 时按其规范化（大写 + 白名单，杜绝注入），
        缺省回退连接用户默认 owner，向后兼容既有行为。PG/MySQL 恒查连接默认 schema。
        """
        try:
            dsType = DataSourceType(ds.type)
        except ValueError:
            dsType = None
        if dsType is DataSourceType.ORACLE:
            sqlOwner = _oracleOwner(owner) if (owner and owner.strip()) else _oracleOwner(ds.username)
            columnRows = await adapter.execute_read_only(_ORACLE_COLUMNS_SQL.format(owner=sqlOwner))
            pkRows = await adapter.execute_read_only(_ORACLE_PK_SQL.format(owner=sqlOwner))
            fkRows = await adapter.execute_read_only(_ORACLE_FK_SQL.format(owner=sqlOwner))
            return _mergeRows(columnRows, pkRows, fkRows)
        if dsType is DataSourceType.POSTGRESQL:
            return await self._fetchInfoSchema(adapter, _PG_SQL)
        if dsType is DataSourceType.MYSQL:
            return await self._fetchInfoSchema(adapter, _MYSQL_SQL)
        raise ValidationError(MSG_DATASOURCE_TYPE_NOT_SUPPORTED.format(dsType=ds.type))

    @staticmethod
    async def _fetchInfoSchema(adapter: BusinessDbAdapter, queries: dict[str, str]) -> dict[str, dict]:
        columnRows = await adapter.execute_read_only(queries["columns"])
        pkRows = await adapter.execute_read_only(queries["primary_keys"])
        fkRows = await adapter.execute_read_only(queries["foreign_keys"])
        return _mergeRows(columnRows, pkRows, fkRows)

    @staticmethod
    def _assembleTables(merged: dict[str, dict]) -> list[TableSchemaRead]:
        return [
            TableSchemaRead(
                table_name=name,
                owner=info["owner"],
                columns=[ColumnSchemaRead(**c) for c in info["columns"]],
                primary_keys=info["primary_keys"],
                foreign_keys=[ForeignKeySchemaRead(**fk) for fk in info["foreign_keys"]],
            )
            for name, info in sorted(merged.items())
        ]

    # ===== 缓存 =====

    async def getCached(
        self,
        session: AsyncSession,
        datasourceId: int,
        *,
        owner: str | None = None,
        ds: DataSource | None = None,
    ) -> SchemaCache | None:
        """按 (datasource_id, schema_name) 读 schema 缓存行，不存在返回 None。

        owner 经 _normalizeSchemaName 归一（Oracle 大写 + 白名单校验；PG/MySQL
        恒 ''）。owner 缺省时解析为数据源「默认 schema」：Oracle → UPPER(连接
        用户名)，PG/MySQL → ''。有 ds 对象时传入可避免额外一次 DataSource 查询。
        """
        if ds is None:
            ds = (
                await session.execute(
                    select(DataSource).where(DataSource.id == datasourceId)
                )
            ).scalars().first()
        schemaName = _normalizeSchemaName(ds, owner) if ds is not None else (owner or "")
        stmt = select(SchemaCache).where(
            SchemaCache.datasource_id == datasourceId,
            SchemaCache.schema_name == schemaName,
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def introspectAndCache(
        self, session: AsyncSession, ds: DataSource, *, owner: str | None = None,
    ) -> SchemaCache:
        """发现 schema 并写入缓存；数据未变化时复用已有缓存行。

        缓存键 (datasource_id, schema_name)。owner 经 _normalizeSchemaName 归一
        （Oracle：大写 + 白名单校验，小写/空白串也归一，杜绝同 owner 大小写不同
        产生两行缓存；PG/MySQL：忽略 owner 恒 ''，内省走连接默认 schema）。

        写入并发安全（跨 PG/SQLite 可移植，不依赖方言特有的 ON CONFLICT）：
        - 首次写入：session.add + commit，若与并发请求撞复合唯一约束
          （uq_schema_cache_datasource_schema），回滚后复用胜出者，避免 500；
        - 数据变化：DB 级 UPDATE（不原地修改既有 ORM 对象，符合不可变约束），再刷新取最新值。
        """
        schemaName = _normalizeSchemaName(ds, owner)
        tables = await self.introspect(ds, owner=schemaName)
        schemaData = [t.model_dump() for t in tables]
        version = _schemaVersion(schemaData)

        existing = await self.getCached(session, ds.id, owner=schemaName, ds=ds)
        if existing is not None and existing.schema_version == version:
            logger.info("schema 缓存未变化，复用 datasource_id=%s schema=%s", ds.id, schemaName)
            return existing
        if existing is None:
            cache = SchemaCache(
                datasource_id=ds.id,
                schema_name=schemaName,
                schema_data=schemaData,
                schema_version=version,
            )
            session.add(cache)
            try:
                await session.commit()
            except IntegrityError:
                # 并发双请求同时首次写入：后者撞复合唯一约束，回滚后复用胜出者
                await session.rollback()
                winner = await self.getCached(session, ds.id, owner=schemaName, ds=ds)
                if winner is not None:
                    logger.info("并发写入冲突，复用已存在缓存 datasource_id=%s schema=%s", ds.id, schemaName)
                    return winner
                raise
            await session.refresh(cache)
            logger.info("写入 schema 缓存 datasource_id=%s schema=%s tables=%s", ds.id, schemaName, len(tables))
            return cache
        # 数据变化：DB 级 UPDATE 刷新版本号与内容（同一行，缓存幂等，仅一条）
        await session.execute(
            update(SchemaCache)
            .where(
                SchemaCache.datasource_id == ds.id,
                SchemaCache.schema_name == schemaName,
            )
            .values(schema_data=schemaData, schema_version=version, updated_time=datetime.now(UTC))
        )
        await session.commit()
        await session.refresh(existing)
        logger.info("刷新 schema 缓存 datasource_id=%s schema=%s tables=%s", ds.id, schemaName, len(tables))
        return existing

    @staticmethod
    def buildResponse(cache: SchemaCache) -> SchemaIntrospectResponse:
        """把缓存行转为 API 响应（schema_data 为 snake_case，经 DTO 转 camelCase）。"""
        tables = [TableSchemaRead.model_validate(row) for row in cache.schema_data]
        return SchemaIntrospectResponse(tables=tables, cached_at=cache.updated_time)

    async def validateOntologyDrift(
        self, session: AsyncSession, ds: DataSource, classes: list[Any],
    ) -> OntologyDriftReport:
        """交叉校验本体引用的表/列与 schema 缓存是否仍存在（2-4）。

        本体是 LLM 渲染 schema 文本的唯一来源，schema_cache 是实际业务库的表/列；
        本体引用了已从业务库消失的表/列时 LLM 会生成查不存在的对象（ORA-00942）。
        本方法比对两者，产出漂移报告，供运维告警与 NL2SQL 提示注入。

        缓存不存在（尚未 introspect）时返回 schema_cached=false 的空报告——没有
        实际 schema 可对照时不臆测缺失。只读一次缓存行，不落库。
        """
        cache = await self.getCached(session, ds.id, ds=ds)
        if cache is None:
            return OntologyDriftReport(
                datasource_id=ds.id,
                has_drift=False,
                schema_cached=False,
                checked_tables=0,
                missing_tables=[],
                missing_columns=[],
            )
        return _validateOntologyAgainstSchema(cache.schema_data, classes, ds.id)


def _schemaVersion(schemaData: list[dict]) -> str:
    """对 schema 数据做规范化 JSON 序列化后取 sha256，用于判断是否变化（64 位十六进制，恰容于 VARCHAR(64)）。"""
    canonical = json.dumps(schemaData, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =============================================================================
# 本体表/列漂移交叉校验（2-4）
# =============================================================================


def _validateOntologyAgainstSchema(
    schemaData: list[dict], classes: list[Any], datasourceId: int,
) -> OntologyDriftReport:
    """交叉校验本体引用（source_table/source_column）与 schema 缓存实际表/列（2-4）。

    schemaData 为 SchemaCache.schema_data 原始 JSON（[{table_name, owner, columns:[...]}]）。
    表名同时支持裸表名（PRECEIPT）与带 owner 的限定名（ZJTH.PRECEIPT），兼容
    Oracle 限定 source_table 与 PG/MySQL 裸表名两种写法；列仅按裸列名比对。
    比较全部大小写不敏感（两侧 .upper()，对齐 _resolveRefineColumn 的折叠约定），
    避免共享大写 Oracle 本体指向小写 PG/MySQL 缓存时误报漂移。

    表本身缺失时只计入 missing_tables，不重复计入 missing_columns（列报告只针对
    "表存在但列缺失"）；多类共享同一缺失表/缺失列时去重。无 source_table 的类 /
    无 source_column 的属性自动忽略。报告中的表/列名保留本体作者原文（不因归一化改写）。

    返回新报告，不改变任何入参；schemaData/classes 为空时报告无漂移。
    """
    bareNames: set[str] = set()
    qualifiedNames: set[str] = set()
    columnsByTable: dict[str, set[str]] = {}
    for table in schemaData:
        name = table.get("table_name")
        if not name:
            continue
        bareNames.add(name.upper())
        owner = table.get("owner")
        if owner:
            qualifiedNames.add(f"{owner}.{name}".upper())
        columnsByTable[name.upper()] = {
            c["column_name"].upper() for c in table.get("columns", []) if c.get("column_name")
        }

    schemaTables = bareNames | qualifiedNames
    missingTables: list[str] = []
    missingColumns: list[MissingColumnRead] = []
    checkedTables = 0
    for cls in classes:
        table = cls.source_table
        if not table:
            continue
        checkedTables += 1
        if table.upper() not in schemaTables:
            if table not in missingTables:
                missingTables.append(table)
            continue
        knownColumns = columnsByTable.get(table.rsplit(".", 1)[-1].upper(), set())
        for prop in cls.properties:
            column = prop.source_column
            if column and column.upper() not in knownColumns:
                if not any(m.table == table and m.column == column for m in missingColumns):
                    missingColumns.append(MissingColumnRead(table=table, column=column))

    return OntologyDriftReport(
        datasource_id=datasourceId,
        has_drift=bool(missingTables or missingColumns),
        schema_cached=True,
        checked_tables=checkedTables,
        missing_tables=missingTables,
        missing_columns=missingColumns,
    )


def _sanitizeDriftIdentifier(value: str) -> str:
    """漂移标识符（source_table/source_column）渲染进 system prompt 前的净化。

    换行不折叠会向告警文本注入裸行，形成"忽略规则"式指令行；本体配置虽是
    管理员写入，但属外部输入，按 _sanitizeSchemaField 同一防御标准处理（转义
    尖括号 + 折叠换行）。不改变调用方入参。
    """
    return value.replace("<", "&lt;").replace(">", "&gt;").replace("\r", " ").replace("\n", " ")


def buildDriftWarning(report: OntologyDriftReport) -> str:
    """把漂移报告渲染为 NL2SQL schema 提示告警（2-4，表漂移能告警）。

    返回的文本由调用方注入 NL2SQL system prompt 的 schema 小节，明确告知模型
    不得引用已漂移的表/字段，避免生成引用不存在对象的 SQL。无漂移返回空串，
    调用方据此不注入，存量 schema 文本行为不变。返回新字符串，不改动入参。
    """
    if not report.has_drift:
        return ""
    lines = [_DRIFT_WARNING_HEADER]
    lines.extend(f"- 表 {_sanitizeDriftIdentifier(table)}" for table in report.missing_tables)
    lines.extend(
        f"- 字段 {_sanitizeDriftIdentifier(m.table)}.{_sanitizeDriftIdentifier(m.column)}"
        for m in report.missing_columns
    )
    lines.append(_DRIFT_WARNING_TRAILER)
    return "\n".join(lines)


# =============================================================================
# 数据字典查询（均为只读 SELECT，经 SQL Guard 校验）
# =============================================================================

# Oracle：列用双引号别名确保小写键名；owner 由 _oracleOwner 白名单后内联为字面量
_ORACLE_COLUMNS_SQL = """
SELECT table_name AS "table_name", column_name AS "column_name",
       data_type AS "data_type",
       CASE WHEN nullable = 'Y' THEN 1 ELSE 0 END AS "nullable",
       '{owner}' AS "owner"
FROM ALL_TAB_COLUMNS
WHERE owner = '{owner}'
ORDER BY table_name, column_id
"""

_ORACLE_PK_SQL = """
SELECT c.table_name AS "table_name", cc.column_name AS "column_name"
FROM ALL_CONSTRAINTS c
JOIN ALL_CONS_COLUMNS cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
WHERE c.owner = '{owner}' AND c.constraint_type = 'P'
ORDER BY c.table_name, cc.position
"""

# 仅发现同 schema 内（rc.owner = '{owner}'）的外键：跨 schema 引用与业务库 schema 无关，
# 注入 NL2SQL 提示反而引入噪音。owner 由 _oracleOwner 白名单后内联为字面量。
_ORACLE_FK_SQL = """
SELECT cc.table_name AS "table_name", cc.column_name AS "column_name",
       rc.table_name AS "ref_table", rcc.column_name AS "ref_column"
FROM ALL_CONSTRAINTS c
JOIN ALL_CONS_COLUMNS cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
JOIN ALL_CONSTRAINTS rc ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name
JOIN ALL_CONS_COLUMNS rcc ON rcc.owner = rc.owner AND rcc.constraint_name = rc.constraint_name
                            AND rcc.position = cc.position
WHERE c.owner = '{owner}' AND c.constraint_type = 'R' AND rc.owner = '{owner}'
ORDER BY cc.table_name, cc.position
"""


# Oracle：列出连接用户可见对象所在的所有者（DISTINCT），供向导「选择 Schema」。
# 纯静态只读 SQL，无用户输入参与拼接，owner 值在 Python 侧再经白名单过滤。
_ORACLE_SCHEMAS_SQL = """
SELECT DISTINCT owner AS "owner"
FROM ALL_TABLES
ORDER BY owner
"""


def _buildInfoSchemaQueries(schemaExpr: str) -> dict[str, str]:
    """构造 information_schema 三连查；schemaExpr 为 CURRENT_SCHEMA() 或 DATABASE()。

    schemaExpr 恒为模块内受信常量，绝非用户输入，无注入面（f-string 仅用于拼接固定表达式）。
    """
    return {
        "columns": f"""
SELECT table_name AS table_name, column_name AS column_name, data_type AS data_type,
       CASE WHEN is_nullable = 'YES' THEN 1 ELSE 0 END AS nullable,
       table_schema AS owner
FROM information_schema.columns
WHERE table_schema = {schemaExpr}
ORDER BY table_name, ordinal_position
""",
        "primary_keys": f"""
SELECT tc.table_name AS table_name, kcu.column_name AS column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = {schemaExpr}
ORDER BY tc.table_name, kcu.ordinal_position
""",
        "foreign_keys": f"""
SELECT tc.table_name AS table_name, kcu.column_name AS column_name,
       kcu.referenced_table_name AS ref_table, kcu.referenced_column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = {schemaExpr}
ORDER BY tc.table_name, kcu.ordinal_position
""",
    }


_PG_SQL = _buildInfoSchemaQueries("CURRENT_SCHEMA()")
_MYSQL_SQL = _buildInfoSchemaQueries("DATABASE()")
