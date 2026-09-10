"""NL2SQL 工具 handler（供给 L4 Agent Loop 调用）。

5 个工具：
- list_tables       → 返回 ontology_class 表名列表（schema.table 格式）
- describe_table    → 返回指定表的列定义（property 行）
- sample_rows       → 返回表前 N 行
- execute_sql       → 执行只读 SELECT（SQL Guard 必走）
- list_joins        → 返回 join 关系列表

所有 handler 返回 frozen ToolResult，content 为 JSON 字符串。
异常路径返回 ToolResult(is_error=True)。
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
        tables = [row[0] for row in result.fetchall()]
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
        columns = []
        for prop in propResult.scalars().all():
            col: dict[str, object] = {
                "name": prop.property_name,
                "alias": prop.property_alias,
                "data_type": prop.data_type,
                "source_column": prop.source_column,
                "is_primary_key": prop.is_primary_key,
                "is_foreign_key": prop.is_foreign_key,
            }
            if prop.ref_class_id:
                col["ref_class_id"] = prop.ref_class_id
            columns.append(col)

        return ToolResult(
            tool_call_id="",
            name="describe_table",
            content=json.dumps(
                {"table_name": table_name, "columns": columns}, ensure_ascii=False, default=str
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
                {"rows": rows, "row_count": len(rows)}, ensure_ascii=False, default=str
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


async def handle_list_joins(*, session: AsyncSession) -> ToolResult:
    """列出 ontology 全部 join 关系。"""
    try:
        stmt = select(OntologyJoin).order_by(OntologyJoin.id)
        result = await session.execute(stmt)
        joins = []
        for join in result.scalars().all():
            joins.append({
                "id": join.id,
                "source_class_id": join.source_class_id,
                "source_columns": join.source_columns,
                "target_class_id": join.target_class_id,
                "target_columns": join.target_columns,
                "join_type": join.join_type,
                "relation_type": join.relation_type,
                "description": join.description,
            })
        return ToolResult(
            tool_call_id="",
            name="list_joins",
            content=json.dumps({"joins": joins}, ensure_ascii=False, default=str),
        )
    except Exception as exc:
        return _errorResult("", "list_joins", exc)


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
            content_obj = await handle_list_joins(session=session)
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
            "description": "执行只读 SELECT 查询（禁止 DML/DDL）",
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
            "description": "列出已建模的 join 关系",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
