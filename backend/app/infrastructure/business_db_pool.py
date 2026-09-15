"""业务数据源连接池。

按 datasource_id 缓存异步适配器，与元数据库（PostgreSQL）引擎隔离。
支持 PostgreSQL / MySQL（SQLAlchemy async）与 Oracle（oracledb 原生 async）。
统一在 execute_read_only 层做只读校验、行数限制与超时，作为后续 SQL Guard 的基础。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol
from urllib.parse import quote

import oracledb
import sqlparse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import getSettings
from app.domain.enums import DataSourceType
from app.domain.error_messages import (
    MSG_DATASOURCE_CONNECT_OK,
    MSG_ORACLE_NOT_SQLALCHEMY_URL,
    MSG_SQL_EMPTY,
    MSG_SQL_FORBIDDEN_OPERATION,
    MSG_SQL_MULTI_STATEMENT,
    MSG_SQL_NOT_READONLY,
    MSG_SQL_PARSE_FAILED,
)
from app.domain.exceptions import SqlSafetyError
from app.domain.models import DataSource
from app.infrastructure.security.crypto import decryptApiKey

logger = logging.getLogger(__name__)

# 只允许这些语句动词；首个非空白 token 必须在白名单内
_READ_ONLY_VERBS = {"SELECT", "WITH"}

# 写入/结构/控制类动词黑名单（防御性，正常流程已被白名单拦截）
_FORBIDDEN_VERBS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "MERGE",
    "CALL",
    "EXEC",
    "EXECUTE",
}

# 危险函数名（即便包在只读 SELECT 里也属写/侧信道）：序列推进、跨库执行、
# 大对象文件 I/O、服务器文件读写/列目录。命中即拒，仅当 token 为 Name（函数名）。
_FORBIDDEN_FUNCTIONS = {
    "NEXTVAL",
    "SETVAL",
    "DBLINK_EXEC",
    "DBLINK_SEND_QUERY",
    "LO_EXPORT",
    "LO_IMPORT",
    "PG_READ_FILE",
    "PG_WRITE_FILE",
    "PG_READ_BINARY_FILE",
    "PG_WRITE_BINARY_FILE",
    "PG_LS_DIR",
}

_adapters: dict[int, "BusinessDbAdapter"] = {}

# Oracle 取消行数上限时的循环 fetchmany 批量大小（避免一次性巨大数组）
_UNLIMITED_BATCH_SIZE = 1000


# =============================================================================
# 只读校验
# =============================================================================


def _assert_read_only(sql: str) -> None:
    """校验 SQL 仅包含 SELECT/WITH 只读语句，拒绝多语句与写操作。

    使用 sqlparse 拆分语句；每个语句的首个非空白 token 必须是白名单动词。
    含多语句（出现 ; 分隔的多条非空语句）且其中有非只读语句时拒绝。

    仅看首 token 不足以防注入：`WITH x AS (DELETE ...) SELECT ...`、
    `SELECT ... INTO ...`、`SELECT ... FOR UPDATE` 等首 token 都合法，
    故在首 token 白名单通过后再做深度扫描（见 _assertNoHiddenWrites）。
    """
    if not sql or not sql.strip():
        raise SqlSafetyError(MSG_SQL_EMPTY, sql=sql)

    statements = sqlparse.parse(sql)
    nonEmptyStmts: list[sqlparse.sql.Statement] = []
    for stmt in statements:
        tokens = [t for t in stmt.tokens if not t.is_whitespace]
        if not tokens:
            continue
        nonEmptyStmts.append(stmt)

    if not nonEmptyStmts:
        raise SqlSafetyError(MSG_SQL_EMPTY, sql=sql)

    # 拒绝显式多语句脚本，避免语句注入
    if len(nonEmptyStmts) > 1:
        raise SqlSafetyError(MSG_SQL_MULTI_STATEMENT, sql=sql)

    stmt = nonEmptyStmts[0]
    firstToken = stmt.token_first(skip_ws=True, skip_cm=True)
    if firstToken is None:
        raise SqlSafetyError(MSG_SQL_PARSE_FAILED, sql=sql)

    verb = firstToken.value.upper()
    if verb in _FORBIDDEN_VERBS:
        raise SqlSafetyError(MSG_SQL_FORBIDDEN_OPERATION.format(verb=verb), sql=sql)
    if verb not in _READ_ONLY_VERBS:
        raise SqlSafetyError(MSG_SQL_NOT_READONLY.format(verb=verb), sql=sql)

    # 深度扫描：白名单首 token 通过后，仍要拦下藏于 CTE/子查询/尾随子句的写操作
    _assertNoHiddenWrites(stmt, sql)


def _assertNoHiddenWrites(stmt: sqlparse.sql.Statement, sql: str) -> None:
    """深度扫描已通过白名单首 token 的语句，拦截隐藏写操作。

    覆盖（均为首 token 合法但语义为写/侧信道的形态）：
    - `WITH x AS (DELETE ...) SELECT * FROM x`：数据修改 CTE —— DELETE 等
      动词出现在 Keyword.DML/DDL token 中；
    - `SELECT ... INTO t`（PG/标准 SQL 建表）、`SELECT ... INTO OUTFILE/DUMPFILE`
      （MySQL 写文件）—— 关键 token 是 Keyword `INTO`（OUTFILE 本身被 sqlparse
      归为 Name，故不单独判）；
    - `SELECT ... FOR UPDATE` / `FOR SHARE`（PG 行锁）与 `LOCK IN SHARE MODE`
      （MySQL）—— UPDATE 已入 _FORBIDDEN_VERBS，SHARE 是 Keyword 需单独判；
    - `SELECT nextval('seq')` / `pg_read_file(...)` 等危险函数 —— Name token
      命中 _FORBIDDEN_FUNCTIONS。

    字符串/数字字面量（Token.Literal）整段跳过，避免把 `'DELETE FROM x'` 这类
    文本误判为写操作；注释亦跳过。
    """
    from sqlparse import tokens as T

    for tok in stmt.flatten():
        if tok.is_whitespace:
            continue
        ttype = tok.ttype
        if ttype in T.Comment or ttype in T.Literal:
            continue
        upper = tok.value.upper()
        if ttype in (T.Keyword.DML, T.Keyword.DDL) and upper in _FORBIDDEN_VERBS:
            raise SqlSafetyError(MSG_SQL_FORBIDDEN_OPERATION.format(verb=upper), sql=sql)
        if ttype is T.Keyword and upper == "INTO":
            raise SqlSafetyError(MSG_SQL_FORBIDDEN_OPERATION.format(verb="INTO"), sql=sql)
        if ttype is T.Keyword and upper == "SHARE":
            raise SqlSafetyError(MSG_SQL_FORBIDDEN_OPERATION.format(verb="SHARE"), sql=sql)
        if ttype is T.Name and upper in _FORBIDDEN_FUNCTIONS:
            raise SqlSafetyError(MSG_SQL_FORBIDDEN_OPERATION.format(verb=upper), sql=sql)


# Oracle 标识符字符集：数字/字母/下划线 + CJK 统一表意文字（含扩展 A）
_QUOTE_IDENTIFIER_CHAR = r"[0-9A-Za-z_㐀-鿿]"
# 数字开头 + 至少一个非数字字符（字母/中文/下划线）的未加引号标识符
_QUOTE_DIGIT_LEADING_IDENT = rf"[0-9]+[A-Za-z_㐀-鿿]{_QUOTE_IDENTIFIER_CHAR}*"
_AS_DIGIT_LEADING_RE = re.compile(rf"\bAS\s+({_QUOTE_DIGIT_LEADING_IDENT})")
_DOT_DIGIT_LEADING_RE = re.compile(rf"\.\s*({_QUOTE_DIGIT_LEADING_IDENT})")
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_DOUBLE_QUOTED_RE = re.compile(r'"(?:[^"]|"")*"')
_DESC_KEYWORD_RE = re.compile(r"\bDESC\b", re.IGNORECASE)
_NULLS_KEYWORD_RE = re.compile(r"\bNULLS\b", re.IGNORECASE)


def _quote_digit_leading_identifiers(sql: str) -> str:
    """给数字开头且含非数字字符的未加引号标识符（列别名）加双引号，消除 Oracle ORA-00923。

    LLM 生成跨年差异查询时常用 `AS 2025采购量` / `t.2025采购量` 这类数字开头中文别名，
    Oracle 标识符不能以数字开头，未加引号直接报 ORA-00923。此函数在执行前确定性兜底：

    - 只处理「数字开头 + 至少一个非数字字符（字母/中文/下划线）+ 未加引号」的 token；
    - 纯数字字面量（`= 2025`、`ROWNUM <= 10`）与字母开头标识符（`AVG_PRICE_2025`）不受影响；
    - 字符串字面量（单引号）内部先占位保护、规整后恢复，不被误改。

    返回新字符串，不改动入参。
    """
    if not sql:
        return sql
    # 1. 占位保护字符串字面量，避免其内部文本被误改
    placeholders: dict[str, str] = {}

    def _shelter(match: re.Match[str]) -> str:
        token = f"__STR{len(placeholders)}__"
        placeholders[token] = match.group(0)
        return token

    protected = _STRING_LITERAL_RE.sub(_shelter, sql)
    # 2. AS 定义处 + 点引用处规整数字开头标识符
    rewritten = _AS_DIGIT_LEADING_RE.sub(lambda m: f'AS "{m.group(1)}"', protected)
    rewritten = _DOT_DIGIT_LEADING_RE.sub(lambda m: f'."{m.group(1)}"', rewritten)
    # 3. 恢复字符串字面量
    for token, original in placeholders.items():
        rewritten = rewritten.replace(token, original)
    return rewritten


def _inject_nulls_last(sql: str) -> str:
    """给裸 `ORDER BY ... DESC` 自动补 `NULLS LAST`，消除跨年 top-N 抓 NULL 行。

    Oracle/PostgreSQL 中 `ORDER BY col DESC` 默认 NULLS FIRST —— 跨年/分组对比查询里
    仅部分分组有数据的行（聚合值为 NULL）会排到最前，被外层 ROWNUM/LIMIT 取到，
    造成「top-N 返回的却全是空行」。此函数在执行前确定性兜底：

    - 对每个未显式指定 NULLS 排序的 DESC 排序键追加 NULLS LAST（业务查询几乎总是
      希望 NULL 排最后；`DESC NULLS FIRST` / `DESC NULLS LAST` 显式写法被尊重、跳过）；
    - 字符串字面量与双引号标识符先占位保护，`'desc'` / `"DESC"` 不被误改；
    - 无 DESC 的 SQL 是无副作用的 no-op。

    返回新字符串，不改动入参。
    """
    if not sql:
        return sql
    # 1. 占位保护字符串字面量与双引号标识符
    placeholders: dict[str, str] = {}

    def _shelter(match: re.Match[str]) -> str:
        token = f"__QT{len(placeholders)}__"
        placeholders[token] = match.group(0)
        return token

    protected = _STRING_LITERAL_RE.sub(_shelter, sql)
    protected = _DOUBLE_QUOTED_RE.sub(_shelter, protected)

    def _inject(match: re.Match[str]) -> str:
        after = protected[match.end():].lstrip()
        if _NULLS_KEYWORD_RE.match(after):
            return match.group(0)  # 已显式指定 NULLS 排序，尊重原意
        return match.group(0) + " NULLS LAST"

    # 2. 给每个裸 DESC 排序键补 NULLS LAST
    rewritten = _DESC_KEYWORD_RE.sub(_inject, protected)
    # 3. 恢复占位内容
    for token, original in placeholders.items():
        rewritten = rewritten.replace(token, original)
    return rewritten


# =============================================================================
# 适配器协议
# =============================================================================


class BusinessDbAdapter(Protocol):
    """业务数据库适配器统一接口。"""

    async def test(self) -> tuple[bool, str]:
        """测试连接，返回 (是否成功, 消息)。"""
        ...

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        """执行只读 SQL，返回字典行列表。已做只读校验、行数限制与超时。"""
        ...

    async def dispose(self) -> None:
        """释放底层连接资源。"""
        ...


# =============================================================================
# URL 构造
# =============================================================================


def _build_sqlalchemy_url(
    dsType: DataSourceType,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> str:
    """为 PG/MySQL 构造 SQLAlchemy 异步连接 URL，密码做 URL 编码。"""
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    if dsType == DataSourceType.POSTGRESQL:
        return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{database}"
    if dsType == DataSourceType.MYSQL:
        return f"mysql+aiomysql://{user}:{pwd}@{host}:{port}/{database}"
    raise ValueError(MSG_ORACLE_NOT_SQLALCHEMY_URL.format(dsType=dsType))


def _build_oracle_dsn(host: str, port: int, service_name: str) -> str:
    """构造 oracledb DSN：host:port/service_name。"""
    return f"{host}:{port}/{service_name}"


# =============================================================================
# SQLAlchemy 适配器（PG / MySQL）
# =============================================================================


class _SqlaAdapter:
    """基于 SQLAlchemy 异步引擎的适配器（PostgreSQL / MySQL 共用）。"""

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine: AsyncEngine | None = None
        # 给 evaluator / 报表工具按 dialect 选 SQL 引号（[[feat-dialect-quoting]]）：
        # MySQL 默认反引号 `` `tbl` ``，PG 默认双引号 `"tbl"`。识别靠 URL 前缀
        # （postgresql+asyncpg / mysql+aiomysql），新驱动前缀请同步更新。
        self.dialect: str = "postgresql" if url.startswith("postgresql") else "mysql"

    def _ensureEngine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=2,
            )
        return self._engine

    async def test(self) -> tuple[bool, str]:
        try:
            engine = self._ensureEngine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True, MSG_DATASOURCE_CONNECT_OK
        except Exception as exc:  # noqa: BLE001 - 测试连接需捕获所有异常
            return False, str(exc)

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        _assert_read_only(sql)
        # queryRowLimit <= 0 视为取消行数上限。注意：不能依赖 fetchmany(None)=全部行——
        # 该假设仅对 asyncpg 成立，aiomysql 的 fetchmany(None) 按 cursor.arraysize（默认 1）
        # 只返回 1 行，导致 MySQL 数据源 introspection 每个查询只取到首行。无限行必须显式 all()。
        limit = getSettings().queryRowLimit
        timeout = getSettings().queryTimeoutSeconds

        async def _run() -> list[dict[str, Any]]:
            engine = self._ensureEngine()
            async with engine.connect() as conn:
                result = await conn.execute(text(sql))
                mappings = result.mappings()
                rows = mappings.all() if limit <= 0 else mappings.fetchmany(limit)
                return [dict(r) for r in rows]

        return await asyncio.wait_for(_run(), timeout)

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None


# =============================================================================
# Oracle 适配器（oracledb 原生 async）
# =============================================================================


class _OracleAdapter:
    """基于 oracledb 异步 API 的 Oracle 适配器。"""

    def __init__(self, host: str, port: int, service_name: str, username: str, password: str) -> None:
        self._dsn = _build_oracle_dsn(host, port, service_name)
        self._username = username
        self._password = password

    async def test(self) -> tuple[bool, str]:
        try:
            conn = await oracledb.connect_async(
                user=self._username,
                password=self._password,
                dsn=self._dsn,
            )
            try:
                await conn.ping()
            finally:
                await conn.close()
            return True, MSG_DATASOURCE_CONNECT_OK
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        _assert_read_only(sql)
        # 执行前兜底：给 LLM 生成的数字开头中文别名加双引号，消除 ORA-00923。
        # 只对 Oracle 生效（MySQL/PG 用反引号/不同规则）；对已正确 SQL 是无副作用的 no-op。
        sql = _quote_digit_leading_identifiers(sql)
        # 执行前兜底：裸 ORDER BY ... DESC 补 NULLS LAST（Oracle DESC 默认 NULLS FIRST，
        # 跨年对比时仅部分年份有数据的行会排到最前）；对已显式写 NULLS 的 SQL 是 no-op。
        sql = _inject_nulls_last(sql)
        # queryRowLimit <= 0 视为取消行数上限：
        # 注意 oracledb 的 cursor.fetchmany(None) 实际按 cursor.arraysize（默认 100）截行，
        # 不会返回全部剩余行；要真"无限制"必须显式循环 fetchmany 直到耗尽。
        limit = getSettings().queryRowLimit
        timeout = getSettings().queryTimeoutSeconds

        async def _run() -> list[dict[str, Any]]:
            conn = await oracledb.connect_async(
                user=self._username,
                password=self._password,
                dsn=self._dsn,
            )
            try:
                cursor = conn.cursor()
                await cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                # Oracle cursor.description 返回的是 Oracle 标识符字面大小写（默认大写），
                # 与 SQLAlchemy 适配器走 result.mappings() 返回小写键不一致。evaluator
                # 内部全部以小写键（"total"/"passed"）取值，统一规范成小写让 5 个
                # evaluator（completeness/uniqueness/consistency/validity/referential）
                # 不用逐个适配 Oracle。否则 row.get("total") 在大写键下命中 None
                # 回退 0，导致 total=0/passed=0/passRate=0/FAIL 的假阴性。
                columns = [c.lower() if isinstance(c, str) else c for c in columns]
                if limit > 0:
                    fetched = await cursor.fetchmany(limit)
                else:
                    # 取消上限：按 1000 行/批循环取，避免一次 fetchmany 巨大数组
                    fetched: list = []
                    while True:
                        batch = await cursor.fetchmany(_UNLIMITED_BATCH_SIZE)
                        if not batch:
                            break
                        fetched.extend(batch)
                # oracledb 4.x 中 AsyncCursor.close() 是同步方法（返回 None），无需 await
                cursor.close()
                return [dict(zip(columns, row, strict=False)) for row in fetched]
            finally:
                await conn.close()

        return await asyncio.wait_for(_run(), timeout)

    async def dispose(self) -> None:
        # oracledb 原生连接无连接池，无需释放
        return None


# =============================================================================
# 工厂
# =============================================================================


def build_adapter(
    dsType: DataSourceType | str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> BusinessDbAdapter:
    """按类型构造适配器（不缓存）。"""
    dsType = DataSourceType(dsType)
    if dsType == DataSourceType.ORACLE:
        return _OracleAdapter(host, port, database, username, password)
    url = _build_sqlalchemy_url(dsType, host, port, database, username, password)
    return _SqlaAdapter(url)


# =============================================================================
# 缓存池
# =============================================================================


def get_adapter(datasourceId: int, ds: DataSource) -> BusinessDbAdapter:
    """获取（必要时创建并缓存）数据源适配器。密码从密文解密。"""
    cached = _adapters.get(datasourceId)
    if cached is not None:
        return cached
    password = decryptApiKey(ds.password_encrypted)
    adapter = build_adapter(
        ds.type,
        ds.host,
        ds.port,
        ds.database_name,
        ds.username,
        password,
    )
    _adapters[datasourceId] = adapter
    return adapter


async def dispose_adapter(datasourceId: int) -> None:
    """释放并移除缓存的适配器。"""
    adapter = _adapters.pop(datasourceId, None)
    if adapter is not None:
        try:
            await adapter.dispose()
        except Exception:  # noqa: BLE001
            logger.warning("释放数据源 %s 适配器失败", datasourceId, exc_info=True)


async def reset_pool() -> None:
    """释放全部缓存适配器（测试用）。"""
    ids = list(_adapters.keys())
    for datasourceId in ids:
        await dispose_adapter(datasourceId)
