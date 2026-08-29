"""值域采样（2-1）：为 schema 关键列注入去重值域，让 WHERE 值不再写错。

对 STRING/DATETIME 类维度列取前 N 个 DISTINCT 值，注入 buildSchemaText 生成的
schema 文本，LLM 据此写出真实存在的 WHERE 字面量（如状态码 '1'/'A'、区域码等）。

采样是增强而非硬依赖：
- 任何单列采样失败、DB 不可用、或高基数列都单独跳过，不阻断 NL2SQL 流水线；
- 表名/列名过白名单校验，本体数据无法注入采样 SQL（SQL 注入防御）；
- 结果按 (数据源, 表, 列) 做有界 FIFO 缓存，降低对业务库的重复访问。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 单请求最多采样的列数（每条 DISTINCT 都是业务库访问，需封顶）
_VALUE_SAMPLE_MAX_COLUMNS = 6
# 并行采样上限（过高的并发会压业务库连接池）
_VALUE_SAMPLE_CONCURRENCY = 3
# 单列去重值上限：取 N+1 条探测高基数，超出则视为枚举/维度值过多，不注入
_VALUE_SAMPLE_DISTINCT_LIMIT = 10
# 有界缓存条目上限（FIFO 淘汰）
_VALUE_SAMPLE_CACHE_MAX = 128
# 仅对枚举/维度类列采样（数值列通常是连续量，采样无意义）
_CANDIDATE_TYPES = {"STRING", "DATETIME"}

# 标识符白名单（防御性）：表名可带 schema 限定（如 ZJTH.PRECEIPT），列名为单标识符。
# 拒绝任何含引号/分号/空白等的值，防止本体数据注入采样 SQL。
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def clearValueSampleCache() -> None:
    """清空模块级采样缓存（测试隔离用）。"""
    _VALUE_SAMPLE_CACHE.clear()


# 模块级有界缓存：key=(datasourceId, source_table, source_column) → 去重值列表
_VALUE_SAMPLE_CACHE: dict[tuple[int, str, str], list[str]] = {}


def _cachePut(
    cache: dict[tuple[int, str, str], list[str]],
    key: tuple[int, str, str],
    values: list[str],
) -> None:
    if len(cache) >= _VALUE_SAMPLE_CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = values


def _extractValue(row: dict[str, Any]) -> Any | None:
    """从 DISTINCT 查询单行中取出列值；空行返回 None。"""
    if not row:
        return None
    for value in row.values():
        return value
    return None


class ValueSampler:
    """对一批本体类中候选列做去重值域采样。

    execute 为业务库只读执行器（async (sql) -> list[dict]）；dialect 提供
    取前 N 行去重查询的方言语法；safePrefix 为 schema 前缀（Oracle 用，烘焙表名）。
    采样失败逐列降级，绝不抛出到流水线。
    """

    def __init__(
        self,
        execute: Any,
        *,
        datasourceId: int = 0,
        dialect: Any,
        safePrefix: str | None = None,
        cache: dict[tuple[int, str, str], list[str]] | None = None,
    ) -> None:
        self._execute = execute
        self._datasourceId = datasourceId
        self._dialect = dialect
        self._safePrefix = safePrefix
        self._cache = cache if cache is not None else _VALUE_SAMPLE_CACHE

    async def sample(self, classes: list[Any]) -> dict[tuple[str, str], list[str]]:
        """返回 {(source_table, source_column): [去重值]}；无候选或全部失败返回 {}。"""
        candidates = self._candidates(classes)
        if not candidates:
            return {}
        sem = asyncio.Semaphore(_VALUE_SAMPLE_CONCURRENCY)
        results = await asyncio.gather(
            *(self._sampleOne(sem, cls, prop) for cls, prop in candidates),
            return_exceptions=True,
        )
        out: dict[tuple[str, str], list[str]] = {}
        for (cls, prop), res in zip(candidates, results):
            if isinstance(res, Exception):
                logger.warning(
                    "值域采样失败 table=%s col=%s: %s",
                    cls.source_table, prop.source_column, res,
                )
                continue
            if res:
                out[(cls.source_table, prop.source_column)] = res
        return out

    def _candidates(self, classes: list[Any]) -> list[tuple[Any, Any]]:
        """筛选候选列：表/列过白名单、列类型为 STRING/DATETIME，并封顶数量。"""
        cands: list[tuple[Any, Any]] = []
        for cls in classes:
            if not cls.source_table or not _TABLE_RE.match(cls.source_table):
                continue
            for prop in cls.properties:
                if not prop.source_column or not _COLUMN_RE.match(prop.source_column):
                    continue
                if prop.data_type not in _CANDIDATE_TYPES:
                    continue
                cands.append((cls, prop))
        return cands[:_VALUE_SAMPLE_MAX_COLUMNS]

    async def _sampleOne(
        self, sem: asyncio.Semaphore, cls: Any, prop: Any
    ) -> list[str] | None:
        """采样单列；缓存命中直接返回；高基数或失败返回 None（不注入）。"""
        key = (self._datasourceId, cls.source_table, prop.source_column)
        if key in self._cache:
            return self._cache[key]
        bakedTable = self._bakeTable(cls.source_table)
        async with sem:
            sql = self._dialect.boundedDistinct(
                bakedTable, prop.source_column, _VALUE_SAMPLE_DISTINCT_LIMIT + 1
            )
            rows = await self._execute(sql)
        values: list[str] = []
        for row in rows:
            value = _extractValue(row)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                values.append(text)
        # 高基数：取满 N+1 说明枚举/维度值超过上限，注入反而误导（截断的枚举不完整）
        if len(values) > _VALUE_SAMPLE_DISTINCT_LIMIT:
            return None
        result = values[:_VALUE_SAMPLE_DISTINCT_LIMIT]
        _cachePut(self._cache, key, result)
        return result

    def _bakeTable(self, table: str) -> str:
        """烘焙 schema 前缀（Oracle 的 username 即 schema owner）。

        与 nl2sql_service._bakeTableName 同一去重规则：仅当表名尚未带该前缀时烘焙，
        避免 schema 文本展示与采样查询指向不同表。
        """
        if self._safePrefix and not table.startswith(f"{self._safePrefix}."):
            return f"{self._safePrefix}.{table}"
        return table
