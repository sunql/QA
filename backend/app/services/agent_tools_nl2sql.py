"""NL2SQL 工具 handler（供给 L4 Agent Loop 调用）。

5 个工具：
- list_tables       → 返回 ontology_class 表名列表（schema.table 格式，去重）
- describe_table    → 返回指定表的列定义（列式紧凑编码 + 自描述表头）
- sample_rows       → 返回表前 N 行
- execute_sql       → 执行只读 SELECT（SQL Guard 必走；LLM 可见行数截到 100）
- list_joins        → 返回 TABLE.COL -> TABLE.COL 文本边（去重、可按表过滤）

所有 handler 返回 frozen ToolResult，content 为 JSON 字符串。
异常路径返回 ToolResult(is_error=True)。

工具结果是 L4 prompt 的最大头（2026-10-31 取证：占 prompt 92%），因此输出格式
按「列式 + 表名化 + 行数上限」紧凑编码；row_count 等语义字段仍报真实值。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import SqlSafetyError
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.infrastructure.business_db_pool import _assert_read_only
from app.infrastructure.llm.base_client import ToolCall

log = logging.getLogger(__name__)

# LLM 可见的 execute_sql 行数上限。执行器仍取全量（row_count 报真实总数），
# 只截「喂给 LLM 的序列化」——一次 286 行的明细曾产生 12 万字符工具结果。
L4_TOOL_ROW_CAP = 100

# describe_table 列式输出的自描述表头（LLM 靠它把数组位映射回字段）。
DESCRIBE_TABLE_COLUMN_FORMAT = (
    "[name,alias,data_type,source_column,flags,ref]; "
    "alias='' means same as name/source_column; "
    "flags='PK'|'FK'|'PK,FK'|''; ref=referenced table ('' if none)"
)


# =============================================================================
# 输出编码器
# =============================================================================


def encodeExecuteSqlResult(rows: list[dict]) -> dict:
    """把 execute_sql 的行字典列表编码成「列名一次 + 值数组」的紧凑结构。

    - 列取所有行键的并集（按首次出现顺序），不规则行缺失值补 None；
    - 最多输出 ``L4_TOOL_ROW_CAP`` 行；``row_count`` 永远报真实总行数；
    - 截断时追加 ``truncated`` + ``note``（提示聚合应在 SQL 内完成）。
    """
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    encodedRows = [[row.get(c) for c in columns] for row in rows[:L4_TOOL_ROW_CAP]]
    payload: dict[str, object] = {
        "columns": columns,
        "rows": encodedRows,
        "row_count": len(rows),
    }
    if len(rows) > L4_TOOL_ROW_CAP:
        payload["truncated"] = True
        payload["note"] = (
            f"showing first {L4_TOOL_ROW_CAP} of {len(rows)} rows; "
            "aggregate in SQL if the full set matters"
        )
    return payload


# =============================================================================
# ToolResult（frozen dataclass）
# =============================================================================


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果（不可变）。content 为 JSON 字符串（OpenAI tool message 要求）。"""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


# =============================================================================
# 5 个 handler
# =============================================================================


async def handle_list_tables(*, session: AsyncSession) -> ToolResult:
    """列出 ontology 全部表（schema.table 格式，仅未软删除）。"""
    try:
        stmt = (
            select(OntologyClass.source_table)
            .where(OntologyClass.valid_to.is_(None))
            .where(OntologyClass.source_table.isnot(None))
            .order_by(OntologyClass.source_table)
        )
        result = await session.execute(stmt)
        # 同一 source_table 可能对应多个 ontology_class：去重保序
        tables = list(dict.fromkeys(row[0] for row in result.fetchall()))
        return ToolResult(
            tool_call_id="",
            name="list_tables",
            content=json.dumps({"tables": tables}, ensure_ascii=False),
        )
    except Exception as exc:
        return _errorResult("", "list_tables", exc)


async def handle_describe_table(*, session: AsyncSession, table_name: str) -> ToolResult:
    """返回指定表的列定义（property 行 + 类型 + 是否 PK/FK）。"""
    try:
        # 1. 找到对应的 ontology_class
        stmt = select(OntologyClass).where(
            OntologyClass.source_table == table_name,
            OntologyClass.valid_to.is_(None),
        )
        result = await session.execute(stmt)
        cls = result.scalar_one_or_none()
        if cls is None:
            return ToolResult(
                tool_call_id="",
                name="describe_table",
                content=json.dumps(
                    {"error": "TABLE_NOT_FOUND", "detail": f"表 {table_name} 不存在"},
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        # 2. 读取属性列表
        propStmt = (
            select(OntologyProperty)
            .where(OntologyProperty.class_id == cls.id)
            .order_by(OntologyProperty.property_name)
        )
        propResult = await session.execute(propStmt)
        props = list(propResult.scalars().all())

        # 3. 外键目标 class_id → 表名（class_id 对 LLM 不可用：没有工具能映射）
        refIds = sorted({p.ref_class_id for p in props if p.ref_class_id})
        refTableById = await _loadClassTables(session, refIds)

        columns: list[list[str]] = []
        for prop in props:
            alias = prop.property_alias or ""
            if alias in (prop.property_name, prop.source_column):
                alias = ""
            flags = ",".join(
                flag
                for flag, enabled in (
                    ("PK", prop.is_primary_key),
                    ("FK", prop.is_foreign_key),
                )
                if enabled
            )
            ref = refTableById.get(prop.ref_class_id, "") if prop.ref_class_id else ""
            columns.append(
                [
                    prop.property_name,
                    alias,
                    prop.data_type,
                    prop.source_column,
                    flags,
                    ref,
                ]
            )

        return ToolResult(
            tool_call_id="",
            name="describe_table",
            content=json.dumps(
                {
                    "table": table_name,
                    "column_format": DESCRIBE_TABLE_COLUMN_FORMAT,
                    "columns": columns,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    except Exception as exc:
        return _errorResult("", "describe_table", exc)


async def handle_sample_rows(
    *, session: AsyncSession, table_name: str, limit: int = 5
) -> ToolResult:
    """返回表前 N 行数据（直接 SQL 查询）。"""
    try:
        # 先确认表存在（通过 ontology_class）
        clsStmt = select(OntologyClass).where(
            OntologyClass.source_table == table_name,
            OntologyClass.valid_to.is_(None),
        )
        clsResult = await session.execute(clsStmt)
        if clsResult.scalar_one_or_none() is None:
            return ToolResult(
                tool_call_id="",
                name="sample_rows",
                content=json.dumps(
                    {"error": "TABLE_NOT_FOUND", "detail": f"表 {table_name} 不存在"},
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        # SQL Guard 校验
        sql = f'SELECT * FROM {table_name} FETCH FIRST {limit} ROWS ONLY'
        _assert_read_only(sql)

        result = await session.execute(text(sql))
        rows = [dict(row._mapping) for row in result.fetchall()]
        return ToolResult(
            tool_call_id="",
            name="sample_rows",
            content=json.dumps({"rows": rows, "limit": limit}, ensure_ascii=False, default=str),
        )
    except SqlSafetyError as exc:
        return ToolResult(
            tool_call_id="",
            name="sample_rows",
            content=json.dumps({"error": "SQL_GUARD_REJECTED", "reason": str(exc)}, ensure_ascii=False),
            is_error=True,
        )
    except Exception as exc:
        return _errorResult("", "sample_rows", exc)


async def handle_execute_sql(
    *,
    session: AsyncSession,
    sql: str,
) -> ToolResult:
    """执行只读 SELECT（SQL Guard 必走）。"""
    try:
        _assert_read_only(sql)
        result = await session.execute(text(sql))
        rows = [dict(row._mapping) for row in result.fetchall()]
        return ToolResult(
            tool_call_id="",
            name="execute_sql",
            content=json.dumps(
                encodeExecuteSqlResult(rows), ensure_ascii=False, default=str
            ),
        )
    except SqlSafetyError as exc:
        return ToolResult(
            tool_call_id="",
            name="execute_sql",
            content=json.dumps({"error": "SQL_GUARD_REJECTED", "reason": str(exc)}, ensure_ascii=False),
            is_error=True,
        )
    except Exception as exc:
        return _errorResult("", "execute_sql", exc)


async def handle_list_joins(
    *, session: AsyncSession, tables: list[str] | str | None = None
) -> ToolResult:
    """列出 join 关系，渲染成 ``TABLE.COL -> TABLE.COL`` 文本边。

    - class_id 批量解析成表名；端点不可解析（软删类等）的边跳过；
    - 反向重复边（A→B 与 B→A 同列）去重，同表对不同列的多条边保留；
    - ``tables`` 非空时只保留任一端命中的边（大小写不敏感，支持裸表名）。
    """
    try:
        stmt = select(OntologyJoin).order_by(OntologyJoin.id)
        result = await session.execute(stmt)
        joins = list(result.scalars().all())

        classIds = sorted(
            {j.source_class_id for j in joins} | {j.target_class_id for j in joins}
        )
        tableById = await _loadClassTables(session, classIds)

        # LLM 可能把数组传成单个字符串：统一成列表再过滤（否则会逐字符迭代）
        if isinstance(tables, str):
            tables = [tables]
        wanted = {t.strip().upper() for t in (tables or []) if t.strip()}
        seen: set[tuple] = set()
        edges: list[str] = []
        skipped = 0
        for join in joins:
            sourceTable = tableById.get(join.source_class_id)
            targetTable = tableById.get(join.target_class_id)
            if not sourceTable or not targetTable:
                skipped += 1
                continue
            sourceCols = _joinColumns(join.source_columns)
            targetCols = _joinColumns(join.target_columns)
            key = tuple(sorted([
                (sourceTable.upper(), sourceCols),
                (targetTable.upper(), targetCols),
            ]))
            if key in seen:
                continue
            seen.add(key)
            if wanted and not (
                _matchesTable(sourceTable, wanted) or _matchesTable(targetTable, wanted)
            ):
                continue
            edges.append(
                _renderEdge(join, sourceTable, sourceCols, targetTable, targetCols)
            )

        if skipped:
            log.info("list_joins: 跳过 %d 条端点不可解析的边", skipped)
        return ToolResult(
            tool_call_id="",
            name="list_joins",
            content=json.dumps(
                {"count": len(edges), "edges": edges}, ensure_ascii=False
            ),
        )
    except Exception as exc:
        return _errorResult("", "list_joins", exc)


def _joinColumns(value: object) -> tuple[str, ...]:
    """join 列字段（JSONB list）转成稳定 tuple。"""
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),) if value else ()


def _matchesTable(tableName: str, wanted: set[str]) -> bool:
    """表名匹配：全名或裸表名（最后一个 . 之后）命中即可。"""
    upper = tableName.upper()
    return upper in wanted or upper.rsplit(".", 1)[-1] in wanted


def _renderEdge(
    join: OntologyJoin,
    sourceTable: str,
    sourceCols: tuple[str, ...],
    targetTable: str,
    targetCols: tuple[str, ...],
) -> str:
    """单条边渲染：``TABLE.COL -> TABLE.COL (LEFT) # 描述``。"""
    left = f"{sourceTable}.{','.join(sourceCols) or '*'}"
    right = f"{targetTable}.{','.join(targetCols) or '*'}"
    edge = f"{left} -> {right}"
    if (join.join_type or "INNER").upper() != "INNER":
        edge += f" ({join.join_type})"
    if join.description:
        edge += f" # {join.description}"
    return edge


async def _loadClassTables(
    session: AsyncSession, classIds: list[int]
) -> dict[int, str]:
    """class_id → source_table 批量解析（只取未软删、有表名的类）。"""
    if not classIds:
        return {}
    stmt = select(OntologyClass.id, OntologyClass.source_table).where(
        OntologyClass.id.in_(classIds),
        OntologyClass.valid_to.is_(None),
    )
    result = await session.execute(stmt)
    tableById: dict[int, str] = {}
    for row in result.fetchall():
        mapping = row._mapping
        if mapping.get("source_table"):
            tableById[mapping["id"]] = mapping["source_table"]
    return tableById


# =============================================================================
# 统一错误返回
# =============================================================================


def _errorResult(tool_call_id: str, name: str, exc: Exception) -> ToolResult:
    """统一错误返回（is_error=True）。"""
    log.warning("tool %s failed: %s", name, exc)
    return ToolResult(
        tool_call_id=tool_call_id,
        name=name,
        content=json.dumps(
            {"error": type(exc).__name__, "detail": str(exc)},
            ensure_ascii=False,
        ),
        is_error=True,
    )


# =============================================================================
# dispatcher
# =============================================================================


async def dispatch_tool_call(
    tool_call: ToolCall,
    *,
    session: AsyncSession,
) -> ToolResult:
    """根据 tool_call.name 路由到对应 handler。

    所有 handler 共享同一个 AsyncSession（来自调用方注入）。
    SQL Guard 错误（SqlSafetyError）在 handle_execute_sql / handle_sample_rows 内部捕获。
    """
    name = tool_call.name
    args = tool_call.args

    try:
        if name == "list_tables":
            content_obj = await handle_list_tables(session=session)
        elif name == "describe_table":
            content_obj = await handle_describe_table(session=session, table_name=args["table_name"])
        elif name == "sample_rows":
            content_obj = await handle_sample_rows(
                session=session,
                table_name=args["table_name"],
                limit=args.get("limit", 5),
            )
        elif name == "execute_sql":
            content_obj = await handle_execute_sql(session=session, sql=args["sql"])
        elif name == "list_joins":
            content_obj = await handle_list_joins(
                session=session, tables=args.get("tables")
            )
        else:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=json.dumps(
                    {
                        "error": "UNKNOWN_TOOL",
                        "known_tools": ["list_tables", "describe_table", "sample_rows", "execute_sql", "list_joins"],
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        # 注入 tool_call_id（handler 不知道 caller 传来的 tc）
        return ToolResult(
            tool_call_id=tool_call.id,
            name=content_obj.name,
            content=content_obj.content,
            is_error=content_obj.is_error,
        )
    except Exception as exc:
        return _errorResult(tool_call.id, name, exc)


# =============================================================================
# OpenAI function schemas（供 LLM 看到）
# =============================================================================

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出数据库中所有可用表（含 schema 前缀）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "获取指定表的列定义",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string"}},
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sample_rows",
            "description": "返回指定表前 N 行数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "执行只读 SELECT 查询（禁止 DML/DDL）；最多返回 100 行，"
                "聚合/计数请直接在 SQL 内完成"
            ),
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_joins",
            "description": "列出已建模的 join 关系（TABLE.COL -> TABLE.COL 形式）",
            "parameters": {
                "type": "object",
                "properties": {
                    "tables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "只返回涉及这些表的边（可传裸表名或 schema.table）；"
                            "不传返回全部"
                        ),
                    }
                },
            },
        },
    },
]
