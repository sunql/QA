"""供应商名称 → enterprise_code 预解析（Phase 6.5）。

ChatService / AgentRuntimeService 顶层共用：在数字正则未命中时，
把用户消息里的中文供应商名（entity_mapping.name）解析为 enterprise_code
并替换回消息文本，下游 arg_extractor / 意图分类 / NL2SQL 零感知。

解析顺序：
1. 数字正则（extractSupplierAnyKey）→ 命中即返回（零 DB 开销）
2. 中文名提取：「供应商/supplier」后跟 2-30 字符中文段；
   未命中 → 裸公司名回退（以 有限公司/有限责任公司 结尾，后缀前 ≥ 4 字符）
3. entity_mapping.name 精确匹配（==）
4. entity_mapping.name 模糊匹配（ilike %name%）

失败语义（均 raise ValidationError）：
- not_found    — 0 命中
- ambiguous    - (1, 50) 条命中 → details.candidates 列全量
- over_limit   — ≥ 50 条命中 → details.candidate_count（不列全量，防响应过大）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EntityType
from app.domain.error_messages import (
    MSG_SUPPLIER_NAME_AMBIGUOUS,
    MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT,
    MSG_SUPPLIER_NAME_NOT_FOUND,
)
from app.domain.exceptions import ValidationError
from app.domain.models import EntityMapping
from app.services.intent_service import extractSupplierAnyKey

logger = logging.getLogger(__name__)

# 候选展示上限：≥ 此值视为「过宽」，错误消息只给数量不列全量
# （真实业务 SUPPLIER 约 3500+，LIKE '%汽车%' 易命中数百条）
_CANDIDATE_DISPLAY_LIMIT = 50

# 「供应商/supplier」后跟中文段（至少 1 汉字，总长 2-30；支持 · - 数字字母。
# 刻意不含 \s：否则「供应商 X 的 360° 视图」会把「 的 360」一并吞入 name，
# 导致 apply() 替换源过长而替换失败；
# 前缀要求冒号或空格分隔，避免「供应商采购额」中的「供应商」被误认为关键词；
# 负向前瞻排除常见虚词/介词/连词开头，避免「供应商的收货量」被误解析为公司名）
_NAME_EXTRACT_RE = re.compile(
    r"(?:供应商|supplier)(?:\s*[:：]\s*|\s+)"
    r"(?!\s*[的一是在和与及到从对为向给把被让比跟同于将并而且且或若])"
    r"(?P<name>[一-龥][一-龥A-Za-z0-9·\-]{1,29})"
)

# 裸公司名回退：无「供应商」前缀时，仅当中文段以公司后缀结尾才提取
# （真实数据 2743/3500 供应商名以 有限公司/有限责任公司 结尾）。
# 后缀前至少 4 字符为数据驱动的下限（真实供应商名最短前缀 4 字符，
# 如「华建重工有限公司」），同时过滤「什么是有限公司」这类普通句子。
# 无后缀裸词（如「吉利」）仍要求带前缀——误报率不可控。
# 副作用：「供应商{名称}」无分隔符写法（_NAME_EXTRACT_RE 的已知缺口）经此路径恢复
# （提取后剥离前导 供应商/supplier 关键字）。
_BARE_NAME_RE = re.compile(
    r"(?P<name>[一-龥A-Za-z0-9·\-]{4,29}(?:有限公司|有限责任公司))"
)


def _stripSupplierKeyword(name: str) -> str:
    """剥离裸名提取结果中前导的「供应商/supplier」关键字（无分隔符前缀写法）。"""
    for keyword in ("供应商", "supplier"):
        if keyword in name:
            name = name.rsplit(keyword, 1)[1]
    return name


# apply() 用：名称起点之前是否紧邻「供应商/supplier」（含冒号/空格分隔）
_PREFIX_BEFORE_NAME_RE = re.compile(r"(?:供应商|supplier)[\s:：]*$")


class ResolvedKey(NamedTuple):
    """成功路径的解析结果；失败路径由 resolve() 抛 ValidationError。"""

    key: str  # enterprise_code
    resolved_by: Literal["code_regex", "name_exact", "name_like"]
    original_name: str | None  # apply() 用作 replace 源；code_regex 路径为 None


def _format_candidates(rows: Sequence[tuple[object, str]]) -> str:
    """(code, name) 行 → '10105 济南吉利汽车有限公司 | 10106 ...'（错误消息用）。"""
    return " | ".join(f"{code} {name}" for code, name in rows)


def _rows_to_pairs(rows: Sequence[tuple[object, str]]) -> list[list[str]]:
    """(code, name) 行 → [[code, name], ...]（ValidationError.details.candidates 用）。"""
    return [[str(code), str(name)] for code, name in rows]


class SupplierNameResolver:
    """顶层名字→编码预解析（详见模块 docstring）。"""

    async def resolve(
        self, message: str, session: AsyncSession
    ) -> ResolvedKey | None:
        """成功 → ResolvedKey；失败 → ValidationError；无关键词 → None。"""
        # Pass 0：数字正则（复用既有意图抽取器，保持「供应商 10105」判定一致）
        numeric = extractSupplierAnyKey(message)
        if numeric is not None:
            return ResolvedKey(key=numeric, resolved_by="code_regex", original_name=None)

        # Pass 1：中文名提取；无关键词 → 让下游 pipeline 自行处理
        match = _NAME_EXTRACT_RE.search(message)
        if match is not None:
            name = match.group("name").strip()
        else:
            # 裸公司名回退（详见 _BARE_NAME_RE 注释）；仍无命中 → 交还下游
            bare = _BARE_NAME_RE.search(message)
            if bare is None:
                return None
            name = _stripSupplierKeyword(bare.group("name"))
            if len(name) < len("XX有限公司"):
                return None

        # Pass 2：精确匹配（理论上 (entity_type, name) 唯一；多条属脏数据，按歧义处理）
        exact_rows = (
            (
                await session.execute(
                    select(EntityMapping.enterprise_code, EntityMapping.name)
                    .where(
                        EntityMapping.entity_type == EntityType.SUPPLIER,
                        EntityMapping.name == name,
                    )
                    .limit(2)
                )
            )
            .all()
        )
        if len(exact_rows) == 1:
            return ResolvedKey(
                key=str(exact_rows[0][0]), resolved_by="name_exact", original_name=name
            )
        if len(exact_rows) > 1:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name, n=len(exact_rows),
                    candidates=_format_candidates(exact_rows),
                ),
                details={"candidates": _rows_to_pairs(exact_rows)},
            )

        # Pass 3：LIKE 模糊匹配（无 trigram index，3500 行顺序扫 O(ms)，暂不引 pg_trgm）
        like_rows = (
            (
                await session.execute(
                    select(EntityMapping.enterprise_code, EntityMapping.name).where(
                        EntityMapping.entity_type == EntityType.SUPPLIER,
                        EntityMapping.name.ilike(f"%{name}%"),
                    )
                )
            )
            .all()
        )
        n = len(like_rows)
        if n == 1:
            return ResolvedKey(
                key=str(like_rows[0][0]), resolved_by="name_like", original_name=name
            )
        if 1 < n < _CANDIDATE_DISPLAY_LIMIT:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name, n=n, candidates=_format_candidates(like_rows),
                ),
                details={"candidates": _rows_to_pairs(like_rows)},
            )
        if n >= _CANDIDATE_DISPLAY_LIMIT:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT.format(
                    name=name, limit=_CANDIDATE_DISPLAY_LIMIT
                ),
                details={"candidate_count": n, "name": name},
            )

        # 0 命中
        logger.info("SupplierNameResolver 未命中 name=%s", name)
        raise ValidationError(MSG_SUPPLIER_NAME_NOT_FOUND.format(name=name))

    def apply(self, message: str, resolved: ResolvedKey | None) -> str:
        """把 message 中 original_name 替换为规范形态（仅 name 路径；返回新字符串）。

        名称前紧邻「供应商/supplier」（含冒号/空格分隔）→ 只替换为 key
        （「评估供应商济南吉利汽车有限公司」→「评估供应商10105」）；
        否则替换为「供应商 {key}」（「查询 济南吉利…」→「查询 供应商 10105」），
        保证下游 arg_extractor / 意图正则的数字路径可命中。
        """
        if resolved is None or resolved.original_name is None:
            return message
        idx = message.find(resolved.original_name)
        if idx < 0:
            return message
        preceded = _PREFIX_BEFORE_NAME_RE.search(message, 0, idx)
        replacement = resolved.key if preceded else f"供应商 {resolved.key}"
        return (
            message[:idx] + replacement + message[idx + len(resolved.original_name):]
        )
