"""研究步结果的**结构化证据摘要**（Task 4）：把一步 SQL 的结果行压成 JSON 安全的统计摘要。

设计口径（controller D9–D15，优先于计划原文）：

**D9 · 列类型从取值推断 —— 本链路没有任何列类型元数据。**
适配器契约是 ``execute_read_only(sql) -> list[dict[str, Any]]``（``business_db_pool``），
``stepResult`` 自己产出的也只是裸列名（``columns = list(rows[0].keys())``）。因此
``columns`` 参数可选：不给就从 ``rows`` 推断 kind；给了也只按**名字**用，不假设有
``type`` 字段。**刻意不做** schema 探测查询 —— 方言相关的元数据抓取远超本任务范围。

**D10 · 无 ``question`` 参数。** 统计摘要是与问题无关的客观摘要，且构造点拿不到用户问题。

**D11 · 唯一构造点是 ``research_agent_ports.stepResult``。** 本模块只产对象 + 一个出图
路径用的包装，接线在 ``chartStep``（``rows`` 在手边）。

**D12 · 加法。** ``stepResult.summary`` 仍是人类可读**字符串**；结构化摘要另起 ``evidence`` key。

**D14 · JSONB 边界。** 摘要落 ``state`` / 检查点 options（JSONB 列），而 ``sampleRows`` 带回
的是**驱动原始值**（``Decimal``/``date``/``datetime``）⇒ 直接落库会
``TypeError: Object of type Decimal is not JSON serializable``。**复用唯一一套映射**
``research_json``（Task 3），不另写第二份类型映射，落库与 SSE 口径因此逐字节一致：
``baseline`` 在两边都是字符串 ``"100"``（读回需要数值时按 ``Decimal(str(x))`` 处理）。

**D15 · 不照抄参考实现的三个崩溃点。** 逐值 ``Decimal(str(v))`` 归一（float 不炸）、
排序键显式处理 ``None`` 且归一类型（date 与 str 不互比）、时间列用**整词 token** 匹配
而非子串（``candidate_name`` 不含 time 词边界，不当时间列）。

**D16 · 轴 / 度量的判据是「纯时间名」与「标识符后缀」，不是「含不含时间词」（修复轮 2）。**
只看「名字里有没有时间词」两头都错：``period_cost`` / ``month_volume`` 这类合法 ERP 度量名
含时间词却被整块丢掉（``total`` 直接变 ``None``）；而中文轴（``年份``）过 ``_tokenize`` 后
token 为空、永不命中 ⇒ ``total`` 变成 ``2024 + 2025 = 4049``、基线/当前变成**年份** ——
同比报表拿年份比年份，是「看着合理的错数」（最坏的一种）。故改用
:func:`_isPureTimeName`（全为时间单位 ⇒ 轴形名字）与 :func:`_isIdentifierName`（以
``id`` / ``no`` / ``序号`` 等**结尾** ⇒ id 类列）。英文仍走**整词 / 后缀**匹配
（退回子串会复现 D15 bug 3）；CJK 无词边界，故按**字符**判。

**残余限制（D9 无 schema 元数据 ⇒ 判据只能是启发式的，别当成完备）。** 判据只看名字，故
名字**纯时间**的合法度量（如 ``{d, year}`` 里 ``year`` 恰好是要统计的量）会被跳过；名字以
标识符词**结尾**的度量（如 ``order_no`` 作为「订单号计数」）同理。两者都走「宁可 ``None``
也不给错数」的保守方向，且只在**有**真时间戳时才降级成 ``None``（无真时间戳时退回第一个
数值候选，见 :func:`_metricColumn`）。

**轴侧的另一条残余路径（修复轮 3）。** 名字含时间**字**的**整数度量**（``满意度`` / ``时长`` /
``日志`` / ``活跃度``）在没有真时间戳时，曾被名字兜底的 CJK **按字**匹配升格成时间轴，于是
既被从度量候选里排除（``total=None``）又把 ``time_range`` **编造**成它的取值跨度（把满意度
评分渲染成「数据的时间跨度」）。现已把**整数**列的兜底收窄为「时间整词或纯时间名」；文本/未知
列的「含时间词」判据**仍是启发式**，对 ``stat_date`` 这类真日期轴是必要的，但对名字恰好含时间
词的文本度量仍可能误判 —— 只是那类误判不会**编造数值**（文本列不进度量候选）。

**整数档再补一条中文期间后缀（修复轮 4）。** 上一轮的收窄只救回了 ASCII 那一半：整数的名字兜底
等价于「时间**整词** ∨ **纯**时间名」，而 ``_hasTimeWord`` 对中文列名恒 ``False``（CJK 无词
边界），``_isPureTimeName`` 又要求**全字皆**时间字 ⇒ ``会计年度`` / ``统计年月`` / ``会计期间``
这类带非时间前缀的中文期间名两头都不命中，掉进度量候选被求和：``total`` 变成「年份之和 /
年月编号之和 / 期间号之和」（D16 那记「看着合理的错数」，只是从 ``time_range`` 搬到了
``total``）。故整数档再加 :func:`_hasPeriodSuffix`（名字**末两字都是**时间字 ⇒ 期间形名字）。
这是**单调加宽**（旧判据 ∨ 新判据），只增加能当轴的列，不会让任何现在正确的列失去轴资格。
**仍存的启发式缺口**：末两字非全时间字的中文期间名（如 ``报表期``）仍会掉进度量候选被求和，
而名字恰好以两个时间字结尾的度量（罕见）会被误当轴。

**轴与度量的判据方向**刻意**不同**（修复轮 3，别再「统一」）：:func:`_timeColumn` 的兜底
看「名字**含不含**时间**词**」（文本/未知列按 :func:`_hasTimeToken` 的「整词或 CJK 按字」，
整数列收窄为「整词 :func:`_hasTimeWord` **或**纯时间名 :func:`_isPureTimeName` **或**两字期间
后缀 :func:`_hasPeriodSuffix`」）；而
:func:`_metricColumn` 一律看「**是不是**纯时间名 / 标识符名」。方向不一致是**有意**的 ——
文本日期轴（``stat_date``）与整数日期轴（``createDate``）没有真时间戳时只能靠名字里的
``date`` 认出来；而整数列若沿用 CJK **按字**的宽判据，就会把含时间**字**的度量
（``满意度`` / ``时长`` / ``日志``）升格成轴，既丢掉合法度量（``total=None``）又**编造**
``time_range``（把满意度评分渲染成「数据的时间跨度」），且该列还会掉进度量候选被求和。
「统一」成任一口径都会重新引入一类缺陷：轴侧放宽回「含」（= 本轮这条 bug，整数度量被
升格成轴）；度量侧放宽成「含」（= 修复轮 1 的 ``period_cost`` 回归）。

**SSE 侧是响亮失败，不是静默。** ``trySummarizeEvidence`` 吞异常**不**代表原始行路径静默：
``emit`` 是 ``ResearchEventBus.publish`` 的 partial，它**只入队、从不序列化**；序列化在 SSE
帧构造处（``_sseFrame`` → ``research_json``），那一步**不在** ``emitEvent`` 的 try/except 里
⇒ 白名单外的驱动标量会**炸掉整条 SSE 流**。详见 :func:`trySummarizeEvidence` 的 docstring。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from app.domain.schemas import CamelModel
from app.services.research_json_encoder import research_json

logger = logging.getLogger(__name__)

# 推断出的列 kind（D9）。``integer``/``numeric`` 同属可聚合的数值列。
KIND_INTEGER = "integer"
KIND_NUMERIC = "numeric"
KIND_TIMESTAMP = "timestamp"
KIND_TEXT = "text"
KIND_BOOLEAN = "boolean"
KIND_UNKNOWN = "unknown"

NUMERIC_KINDS = frozenset({KIND_INTEGER, KIND_NUMERIC})

# 有口径可算、名字里带这些**整词**的列才可能是时间轴（D15 bug 3）。
TIME_NAME_TOKENS = frozenset(
    {"year", "month", "date", "time", "period", "quarter", "week", "day", "timestamp"}
)

# CJK 列名无词边界（``_tokenize`` 的 ASCII 切分对中文列名返回空 token）⇒ 按**字符**判时间语义：
# ``日期`` / ``月份`` / ``年度`` / ``时间`` / ``季度`` 任一字命中即算「含时间词」。
CJK_TIME_CHARS = frozenset("年月日季度周时期间")

# ``_isPureTimeName`` 额外允许的「陪跑」字：单独不表时间，但出现在纯时间名里（``年份`` /
# ``月份``）不改变判定。``份额`` 的 ``额`` 不在此列 ⇒ 它仍是度量（不是轴形名字）。
CJK_TIME_COMPANION_CHARS = frozenset("份")
CJK_PURE_TIME_CHARS = CJK_TIME_CHARS | CJK_TIME_COMPANION_CHARS

# 标识符列的**结尾**词（D16）：名字以此结尾 ⇒ id / 序号类列，永不作为度量。必须按**后缀**，
# 不能按「包含」—— ``no_of_orders`` 含 token ``no`` 却是度量（订单数），按包含判会误伤它。
IDENT_SUFFIX_TOKENS = frozenset({"id", "no", "code", "num", "number", "rank", "row", "seq"})
# 中文后缀单独列（CJK 无 token 边界，只能整体 ``endswith``）；``ID`` / ``id`` 由上面的 ASCII 后缀覆盖。
CJK_IDENT_SUFFIXES = ("编号", "代码", "序号", "排名")

DEFAULT_MAX_SAMPLE_ROWS = 5
DEFAULT_TOP_N_LIMIT = 5

# 归一排序键里「缺失」组与「有值」组的判别位（0 有值 < 1 缺失 ⇒ NULL 排到一端）。
_MISSING_RANK = 1
_PRESENT_RANK = 0


class EvidenceSummary(CamelModel):
    """一步结果的统计摘要（frozen DTO；snake 字段 + camel 线上别名，见 ``CamelModel``）。

    数值标量以**字符串**承载（与 ``research_json`` 的 ``Decimal → str`` 口径一致，无损）。
    跨任务契约（Task 5）：``empty`` / ``baseline`` / ``current`` / ``total`` 可**按 Python
    字段名**构造与读取，故保留 ``populate_by_name=True``；改名或删除会静默打断下游。

    **线上契约：数值字段是字符串**（``decimal`` 无损、且直接是 JSON 安全标量，D14）。
    故需要算术 / 比较时必须走 :meth:`numeric`（见其 docstring 的踩坑说明）。
    """

    model_config = ConfigDict(frozen=True)

    total: str | None = None
    baseline: str | None = None
    current: str | None = None
    delta: str | None = None
    delta_ratio: str | None = None
    top_n: list[dict[str, Any]] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, str] | None = None
    empty: bool = False

    @field_validator("total", "baseline", "current", "delta", "delta_ratio", mode="before")
    @classmethod
    def _normalizeNumericScalar(cls, value: Any) -> Any:
        """按名直接构造时容忍 ``Decimal``/``int``/``float``：统一归一为线上字符串。

        ``summarizeEvidence`` 自己喂的是已归一字符串（no-op）；这条只为 Task 5 的
        ``EvidenceSummary(baseline=Decimal("100"), ...)`` 契约不因类型而炸。``Decimal → str``
        无损；``float → str`` 与 ``research_json`` 同口径；``bool`` 交给 pydantic 拒绝
        （数值字段不该收布尔）。
        """
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        return value

    def numeric(self, name: str) -> Decimal | None:
        """按 **Python 字段名**读一个数值字段为 ``Decimal``；缺席 / 非数值 / 非有限 → ``None``。

        **为什么必须用这个访问器，而不是直接拿字段比较**（字段是字符串，见类 docstring）：
        - ``Decimal('200') > summary.baseline``（``baseline`` 是 ``'100'``）⇒ ``TypeError``；
        - 而 ``'1000' > '900'`` 在 Python 里是 **``False``**（字典序）⇒ 不报错，却把真实大小
          **静默反转**。后者更危险：它会让「涨了」判成「跌了」，且没有任何异常提示。
        ``numeric("baseline")`` 是消费（比较 / 算术 / 阈值判定）的唯一正确读法。
        """
        raw = getattr(self, name, None)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            parsed = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None


# ---------------------------------------------------------------------------
# 列类型推断（D9）
# ---------------------------------------------------------------------------


def _columnOrder(rows: list[dict[str, Any]], columns: Any) -> list[str]:
    """列顺序：显式传入优先（只认名字），否则取首行键序。"""
    if columns:
        names = [_columnName(column) for column in columns]
        return [name for name in names if name]
    return list(rows[0].keys())


def _columnName(column: Any) -> str | None:
    """`columns` 元素可以是裸列名（str）或含 ``name`` 的 dict；其余忽略。"""
    if isinstance(column, str):
        return column
    if isinstance(column, dict):
        name = column.get("name")
        return str(name) if name else None
    return None


def inferColumnKinds(rows: list[dict[str, Any]]) -> dict[str, str]:
    """逐列从**取值**推断 kind（本链路无列类型元数据，D9）。

    混类型列（如 int + str）保守判 ``unknown``：绝不拿一个可能错的 kind 去做算术。
    全 ``None`` 列 → ``unknown``。``bool`` 先于 ``int`` 判定（它是 ``int`` 的子类）。
    """
    return {
        name: _inferKind([row.get(name) for row in rows]) for name in _columnOrder(rows, None)
    }


def _inferKind(values: list[Any]) -> str:
    seen = {_valueKind(value) for value in values if value is not None}
    if not seen:
        return KIND_UNKNOWN
    if len(seen) == 1:
        return next(iter(seen))
    if seen <= NUMERIC_KINDS:  # int 与 Decimal/float 混排 ⇒ 统一当数值列
        return KIND_NUMERIC
    return KIND_UNKNOWN


def _valueKind(value: Any) -> str:
    if isinstance(value, bool):
        return KIND_BOOLEAN
    if isinstance(value, int):
        return KIND_INTEGER
    if isinstance(value, (Decimal, float)):
        return KIND_NUMERIC
    if isinstance(value, (datetime, date)):
        return KIND_TIMESTAMP
    if isinstance(value, str):
        return KIND_TEXT
    return KIND_UNKNOWN


def _tokenize(name: str) -> list[str]:
    """把列名拆成小写整词 token（``candidate_name`` → [candidate, name]；``createDate`` → [create, date]）。"""
    tokens: list[str] = []
    for chunk in re.split(r"[^0-9A-Za-z]+", name):
        if not chunk:
            continue
        tokens += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[0-9]+|[A-Z]+", chunk)
    return [token.lower() for token in tokens]


def _hasTimeToken(name: str) -> bool:
    """名字**含**时间词 —— 英文按**整词**（D15 bug 3），CJK 按**字**（无词边界）。

    ``candidate_name`` 里含 ``date`` 子串却**不是**时间列，故英文必须整词匹配；退回子串
    匹配就会复现 D15 bug 3。注意本判据是「含」而非「是」：``period_cost`` 也命中，
    但它不是**纯**时间名（见 :func:`_isPureTimeName`），仍应作为度量。
    """
    if TIME_NAME_TOKENS & set(_tokenize(name)):
        return True
    return any(char in CJK_TIME_CHARS for char in name)


def _hasTimeWord(name: str) -> bool:
    """名字含时间**整词**（ASCII 口径，如 ``createDate`` → ``date``；``stat_date`` → ``date``）。

    这是 :func:`_hasTimeToken` 的**英文**那一半，单独抽出给 :func:`_timeColumn` 的**整数**
    兜底用：整数列（年份数字、``20240101``）既常见又只能靠名字判时间语义，而 CJK 的**按字**
    匹配在整数列上太宽（``满意度``/``时长``/``日志`` 含时间字却是度量）。CJK 无词边界 ⇒ 本
    判据对中文列名恒 ``False``，由 :func:`_isPureTimeName` 补（``年份``/``月份``）。
    """
    return bool(TIME_NAME_TOKENS & set(_tokenize(name)))


def _isPureTimeName(name: str) -> bool:
    """名字是否**全是时间单位**（⇒ 轴形名字，永不作为度量）。

    ``year`` / ``quarter`` / ``年份`` / ``月份`` ⇒ True；
    ``period_cost``（有 ``cost``）/ ``月度量``（有 ``量``）/ ``日志``（有 ``志``）⇒ False。
    判据是「**是不是**纯时间名」而不是「含不含时间词」—— 后者会把 ``period_cost`` 这类
    合法 ERP 度量名整块丢掉（修复轮 1 的反向回归）。
    """
    tokens = _tokenize(name)
    if tokens and all(token in TIME_NAME_TOKENS for token in tokens):
        return True
    if not name:
        return False
    return all(char in CJK_PURE_TIME_CHARS for char in name)


def _hasPeriodSuffix(name: str) -> bool:
    """名字**末两字都是**时间字（``年度`` / ``年月`` / ``期间`` / ``月份`` / ``日期``）⇒ 期间形名字。

    中文期间名常带非时间前缀（``会计年度`` / ``统计年月`` / ``会计期间``）⇒ 过不了
    :func:`_isPureTimeName` 的「全字皆时间字」，但**末两字**是闭合的期间词。用已有的
    ``CJK_TIME_CHARS``，**不引入新词表**。
    ⚠️ 用 ``CJK_TIME_CHARS``（**不含** ``份``）：``月份额`` 末两字是 ``份额`` ⇒ 不算期间名
    （否则 ``份`` 会让它变轴）。故**不要**用 ``CJK_PURE_TIME_CHARS``。
    """
    return len(name) >= 2 and all(char in CJK_TIME_CHARS for char in name[-2:])


def _isIdentifierName(name: str) -> bool:
    """名字是否以**标识符词结尾**（⇒ id / 序号类列，永不作为度量）。

    必须按**后缀**：``no_of_orders`` 含 token ``no`` 但它是度量（订单数），按「包含」判
    会把它误伤成标识符列。``order_no`` / ``product_id`` / ``rank`` / ``序号`` ⇒ True；
    ``no_of_orders`` / ``amount`` ⇒ False。
    """
    tokens = _tokenize(name)
    if tokens and tokens[-1] in IDENT_SUFFIX_TOKENS:
        return True
    return name.lower().endswith(CJK_IDENT_SUFFIXES)


def _timeColumn(order: list[str], kinds: dict[str, str]) -> str | None:
    """时间轴列：**优先真时间戳**（取值即 timestamp），退而按名字判时间语义（D15 bug 3 / D16）。

    名字兜底**按 kind 分两档，口径刻意不同**（与 :func:`_metricColumn` 的「是纯时间名」
    不同方向，见模块头「残余限制」段 —— 别顺手统一）：

    - ``KIND_TEXT`` / ``KIND_UNKNOWN`` 用「**含**时间词」（:func:`_hasTimeToken`）：
      ``stat_date`` 这类**文本**日期轴没有真时间戳，只能靠名字里的 ``date`` 认出来；
    - ``KIND_INTEGER`` 收窄成「时间**整词**（:func:`_hasTimeWord`）或**纯**时间名
      （:func:`_isPureTimeName`）或**两字期间后缀**（:func:`_hasPeriodSuffix`）」：整数列不能用
      CJK **按字**的宽判据 —— ``满意度`` / ``时长`` / ``日志`` 含时间**字**却是度量，按「含」判
      会把它们升格成轴（合法度量被丢 ⇒ ``total=None``，且 ``time_range`` 被**编造**成评分/时长
      的取值跨度）。而 ``createDate`` 这类**整词**命中的整数日期列、以及 ``会计年度`` /
      ``统计年月`` / ``会计期间`` 这类**带非时间前缀**的中文期间名仍须是轴 —— 否则它们会掉进
      度量候选，``total`` 变成 ``2024 + 2025 = 4049`` / 年月编号之和 / 期间号之和（D16 那记
      「看着合理的错数」重现）。

    ``KIND_NUMERIC``（Decimal/float）两档都不收 —— 否则 ``period_amount`` 这类数值列会被
    误当时间轴。
    """
    for name in order:
        if kinds.get(name) == KIND_TIMESTAMP:
            return name
    for name in order:
        kind = kinds.get(name)
        if kind in (KIND_TEXT, KIND_UNKNOWN) and _hasTimeToken(name):
            return name
        if kind == KIND_INTEGER and (
            _hasTimeWord(name) or _isPureTimeName(name) or _hasPeriodSuffix(name)
        ):
            return name
    return None


def _metricColumn(
    order: list[str], kinds: dict[str, str], timeColumn: str | None, timeKind: str | None
) -> str | None:
    """被聚合的度量列：排除时间轴列、**纯时间名**与**标识符名**后的第一个数值列（D16）。

    判据是「名字**是不是**纯时间名」，不是「含不含时间词」（修复轮 1 的错口径）：后者对
    ``period_cost`` / ``month_volume`` 这类合法 ERP 度量名会整块丢掉（``total`` 变 ``None``），
    对 ``年份`` 这类中文轴又完全失效（``_tokenize`` 对 CJK 返回空 ⇒ 永不命中）⇒ ``total``
    变成 ``2024 + 2025 = 4049``、基线/当前变成**年份**（同比报表拿年份比年份）。同理，
    ``product_id`` / ``rank`` 这类标识符列是数值却不是量，按**后缀**排除。

    过滤后为空时：
    - 有真时间戳 ⇒ ``None``：候选真的只剩轴形名字（如 ``{year, d}``）⇒ 宁可没有可辨认的度量，
      也不给「看着合理」的错值；
    - 无真时间戳 ⇒ 退回过滤前的第一个数值候选（宽松兜底；``{month, month_sales}`` 里
      ``month_sales`` 本就是第一个通过者，不受影响）。
    """
    candidates = [
        name for name in order if name != timeColumn and kinds.get(name) in NUMERIC_KINDS
    ]
    measures = [
        name for name in candidates if not _isPureTimeName(name) and not _isIdentifierName(name)
    ]
    if measures:
        return measures[0]
    if timeKind == KIND_TIMESTAMP:
        return None
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# 数值归一与统计（D15 bug 1）
# ---------------------------------------------------------------------------


def _asDecimal(value: Any) -> Decimal | None:
    """把驱动标量归一为**有限** ``Decimal``；不可归一 / 非有限 → ``None``（调用方跳过，不抛）。

    ``float`` 走 ``Decimal(str(v))``（不是 ``Decimal(v)``）：后者会把二进制误差带进来，
    且 ``Decimal(0) + float`` 在参考实现里就是那记 ``TypeError``（D15 bug 1）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _sumFinite(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present, Decimal(0))


def _boundaryValues(
    sortedRows: list[dict[str, Any]], timeColumn: str, metricColumn: str
) -> tuple[Decimal | None, Decimal | None]:
    """基线 / 当前：按时间排序后，**有时间值**的行里第一个 / 最后一个带度量的值。

    时间缺失的行不参与边界（否则「当前」可能是行序最末的无时间行）。
    """
    values = [
        (row.get(timeColumn), _asDecimal(row.get(metricColumn)))
        for row in sortedRows
        if row.get(timeColumn) is not None
    ]
    present = [metric for _, metric in values if metric is not None]
    if not present:
        return None, None
    baseline = next(metric for _, metric in values if metric is not None)
    current = next(metric for _, metric in reversed(values) if metric is not None)
    return baseline, current


def _delta(baseline: Decimal | None, current: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if baseline is None or current is None:
        return None, None
    difference = current - baseline
    if baseline == 0:
        return difference, None
    try:
        return difference, difference / baseline
    except (InvalidOperation, ZeroDivisionError):
        return difference, None


def _metricValues(rows: list[dict[str, Any]], metricColumn: str | None) -> list[Decimal | None]:
    if metricColumn is None:
        return []
    return [_asDecimal(row.get(metricColumn)) for row in rows]


def _topNRows(rows: list[dict[str, Any]], metricColumn: str | None, limit: int) -> list[dict[str, Any]]:
    """Top-N：按度量值降序（稳定排序 ⇒ 并列保持行序，无 set 迭代不确定性）。"""
    if metricColumn is None:
        return []
    ranked = [(row, _asDecimal(row.get(metricColumn))) for row in rows]
    ranked = [(row, metric) for row, metric in ranked if metric is not None]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [{"rank": rank, "row": dict(row)} for rank, (row, _) in enumerate(ranked[:limit], 1)]


# ---------------------------------------------------------------------------
# 时间排序 / 时间范围（D15 bug 2）
# ---------------------------------------------------------------------------


def _sortByTime(
    rows: list[dict[str, Any]], timeColumn: str | None, timeKind: str | None
) -> list[dict[str, Any]]:
    """按时间列排序：键按推断 kind 归一（数值列用 Decimal，其余用 ISO/字符串），缺失排一端。"""
    if timeColumn is None:
        return list(rows)
    if timeKind in NUMERIC_KINDS:
        return sorted(rows, key=lambda row: _numericSortKey(row.get(timeColumn)))
    return sorted(rows, key=lambda row: _textSortKey(row.get(timeColumn)))


def _numericSortKey(value: Any) -> tuple[int, Decimal]:
    parsed = _asDecimal(value)
    if parsed is None:
        return (_MISSING_RANK, Decimal(0))
    return (_PRESENT_RANK, parsed)


def _textSortKey(value: Any) -> tuple[int, str]:
    if value is None:
        return (_MISSING_RANK, "")
    return (_PRESENT_RANK, _timeLabel(value))


def _timeLabel(value: Any) -> str:
    """时间轴标签：``date``/``datetime`` → ISO 8601；其余 → ``str``（整数 year → ``"2024"``）。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _timeRange(
    sortedRows: list[dict[str, Any]], timeColumn: str | None
) -> dict[str, str] | None:
    if timeColumn is None:
        return None
    labels = [_timeLabel(row.get(timeColumn)) for row in sortedRows if row.get(timeColumn) is not None]
    if not labels:
        return None
    return {"start": labels[0], "end": labels[-1]}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def summarizeEvidence(
    rows: list[dict[str, Any]],
    columns: Any = None,
    *,
    maxSampleRows: int = DEFAULT_MAX_SAMPLE_ROWS,
    topNLimit: int = DEFAULT_TOP_N_LIMIT,
) -> EvidenceSummary:
    """把一步的结果行压成 JSON 安全的 :class:`EvidenceSummary`。

    ``columns`` 可选（D9）：省略则从 ``rows`` 推断 kind；给出则只按名字用作列顺序。
    返回的模型字段已是 JSON 安全值（``research_json`` 口径），可直接 ``model_dump`` 落库。

    最后一步把整个 payload 过一遍 **``research_json``**（D14）：``sampleRows`` / ``topN``
    里的驱动原始值（``Decimal``/``date``/``datetime``）与数值标量一并归一为 JSON 安全
    标量。**刻意复用唯一一套映射**，不另写第二份类型映射 —— 落库与 SSE 因此逐字节一致。
    """
    if not rows:
        return EvidenceSummary(empty=True)

    order = _columnOrder(rows, columns)
    kinds = {name: _inferKind([row.get(name) for row in rows]) for name in order}
    timeColumn = _timeColumn(order, kinds)
    timeKind = kinds.get(timeColumn) if timeColumn else None
    metricColumn = _metricColumn(order, kinds, timeColumn, timeKind)

    sortedRows = _sortByTime(rows, timeColumn, timeKind)
    baseline, current = (
        _boundaryValues(sortedRows, timeColumn, metricColumn) if timeColumn and metricColumn else (None, None)
    )
    delta, deltaRatio = _delta(baseline, current)

    payload: dict[str, Any] = {
        "total": _sumFinite(_metricValues(rows, metricColumn)),
        "baseline": baseline,
        "current": current,
        "delta": delta,
        "delta_ratio": deltaRatio,
        "top_n": _topNRows(rows, metricColumn, topNLimit),
        "sample_rows": [dict(row) for row in rows[:maxSampleRows]],
        "time_range": _timeRange(sortedRows, timeColumn),
        "empty": False,
    }
    return EvidenceSummary.model_validate(json.loads(research_json(payload)))


def trySummarizeEvidence(
    rows: list[dict[str, Any]],
    columns: Any = None,
    *,
    maxSampleRows: int = DEFAULT_MAX_SAMPLE_ROWS,
    topNLimit: int = DEFAULT_TOP_N_LIMIT,
) -> EvidenceSummary | None:
    """出图路径专用包装：**任何失败只留痕，不让单步失败**（D12 加法原则）。

    本模块读的是驱动原始行，可能含 ``research_json`` 白名单外的类型（``bytes`` /
    ``memoryview`` / oracledb LOB ...）。

    **机制（实测核实，勿再写成「被 ``emitEvent`` 吞掉」）**：``emit`` 是
    ``ResearchEventBus.publish`` 的 partial，而 ``publish`` 只把 ``(event, payload)``
    **入队、从不序列化**。因此「同一批行在 emit 处失败」这条路径上**根本没有失败** ——
    ``emitEvent`` 守卫的是 ``emit`` 那一步，而入队不碰 payload 类型。真正的序列化在 **SSE
    帧构造处**（``_sseFrame`` → ``research_json``），那一步**不在** ``emitEvent`` 的
    try/except 里 ⇒ 同一批行在那里失败会**炸掉整条 SSE 流**，是**响亮失败**而非静默。

    正因为原始行路径在 SSE 侧已经足够响亮，结构化摘要这一路才不该反过来把**单步**升级成
    **整 turn 失败**：此处上抛会经服务的 ``_guardedRun`` 落进终态失败通道，让一个**本可
    降级**成「本步没有结构化证据」的加法功能成为整轮终止的理由。它是加法：算不出来就不给，
    返回 ``None`` 即「本步无结构化证据」。
    """
    try:
        return summarizeEvidence(
            rows, columns, maxSampleRows=maxSampleRows, topNLimit=topNLimit
        )
    except Exception:  # noqa: BLE001 —— 见 docstring：失败不得升级成步失败
        logger.warning("结构化证据摘要生成失败，本步不带 evidence", exc_info=True)
        return None
