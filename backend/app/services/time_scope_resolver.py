"""季度/半年时间口径的确定性解析（纯函数，无 IO）。

背景（2026-10-31）：NL2SQL 的 prompt 只注入「今天是 X」，季度边界完全交给 LLM
推算；同一问句在 dev/prod 生成了字面量日期与 ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'),-N)
两种写法，口径不一致。本模块把「本季度/上季度/一季度/2026年Q3/上半年」等确定性
表达解析为服务端算好的半开区间 [start, end)，作为**事实数据**注入 prompt。

设计约束：
- 只解析季度/半年这类无歧义口径；「这半年」「近半年」「最近 N 天」等滚动窗口
  语义存在业务分歧（滚动 vs 自然边界），一律不解析（返回 None），交给上游决定。
- 半开区间含起不含止，跨年边界由计算得出，不允许调用方自行拼 `end`。
- 命中多个区间时按「长匹配优先、再按出现位置」去重，避免「上一季度」中的
  「一季度」被重复识别。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 季度 -> 起始月；用元组而非 dict，顺序即业务顺序，避免额外的排序假设。
_QUARTER_START_MONTHS = (1, 4, 7, 10)

_CN_QUARTERS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

_RELATIVE_YEAR_OFFSETS = {
    "今年": 0,
    "本年": 0,
    "去年": -1,
    "上年": -1,
    "明年": 1,
    "下一年": 1,
    "前年": -2,
}

_RELATIVE_QUARTER_OFFSETS = {
    "本": 0,
    "这": 0,
    "当前": 0,
    "上": -1,
    "上一": -1,
    "前": -1,
    "前一": -1,
    "下": 1,
    "下一": 1,
}

_RELATIVE_YEAR = "今年|本年|去年|上年|明年|下一年|前年"

# 判断口径是否显式锚定年份（用于多轮追问的注入抑制）。
# 四位年份与「今年/去年/…」都以今天为锚，多轮追问不会改变其语义，故都算锚定；
# 而「本季度/上季度/一季度/Q1」依赖上一轮时间，需由 chat_context 递减。
_RE_YEAR_IN_TEXT = re.compile(rf"(?:19|20)\d{{2}}|{_RELATIVE_YEAR}")

# 显式年份 + 中文/数字季度：2026年第三季度 / 2025年四季度 / 2026 一季度
_RE_ABS_YEAR_CN_QUARTER = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*年?\s*(?:第\s*)?(?P<quarter>[一二三四1-4])\s*季度"
)

# 显式年份 + QN：2026-Q2 / 2026Q2 / 2026 Q2
_RE_ABS_YEAR_Q = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*年?\s*-?\s*Q(?P<quarter>[1-4])(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# 相对年份 + 季度：今年一季度 / 去年三季度 / 明年Q1
# 中文数字分支必须带「季度/季」后缀，否则「今年3月」「去年12月」会被误判成季度
# 并作为权威事实注入 prompt（2026-10-31 code review HIGH）。
_RE_RELATIVE_YEAR_QUARTER = re.compile(
    rf"(?P<relCn>{_RELATIVE_YEAR})\s*年?\s*(?:第\s*)?(?P<quarterCn>[一二三四1-4])\s*(?:季度|季)"
    rf"|(?P<relQ>{_RELATIVE_YEAR})\s*年?\s*(?P<quarterQ>Q[1-4])(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# 相对季度：本季度 / 这个季度 / 当前季度 / 上个季度 / 上一季度 / 下季度
_RE_RELATIVE_QUARTER = re.compile(r"(?P<dir>当前|上一|下一|前一|本|这|上|下|前)\s*(?:个\s*)?(?:季度|季)")

# 裸季度（默认沿用句中最近的显式年份，没有则今年）：一季度 / 第三季度 / Q1
# 前置边界：「前三季度」是 Q1–Q3 累计口径，「13季度」是非法序号，都不能当成 Q3。
_RE_BARE_CN_QUARTER = re.compile(r"(?<![\d前])(?:第\s*)?(?P<quarter>[一二三四1-4])\s*季度")
_RE_BARE_Q = re.compile(r"(?<![0-9A-Za-z前])Q(?P<quarter>[1-4])(?![0-9A-Za-z])", re.IGNORECASE)

# 句内年份结转：只认同独立成词的四位年份（相对年份词由各自的规则解析）。
# 前后边界排除订单号/编码里的长数字串（如 20250101）。
_RE_FOUR_DIGIT_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

# 半年：上半年 / 下半年 / 2025年下半年 / 去年上半年
# 注意：「这半年」「近半年」不匹配（前缀不是上/下），按设计不解析。
_RE_HALF_YEAR = re.compile(
    rf"(?:(?P<year>(?:19|20)\d{{2}})\s*年|(?P<rel>{_RELATIVE_YEAR})\s*年?)?"
    r"\s*(?P<half>[上下])\s*半年"
)


@dataclass(frozen=True)
class TimeScope:
    """一个已解析的时间口径（半开区间 [start, end)，含起不含止）。"""

    matched: str
    start: date
    end: date
    label: str
    granularity: str  # "quarter" | "half_year"


def _quarterBounds(year: int, quarter: int) -> tuple[date, date]:
    startMonth = _QUARTER_START_MONTHS[quarter - 1]
    start = date(year, startMonth, 1)
    if quarter == 4:
        return start, date(year + 1, 1, 1)
    return start, date(year, startMonth + 3, 1)


def _quarterScope(year: int, quarter: int, matched: str) -> TimeScope:
    start, end = _quarterBounds(year, quarter)
    return TimeScope(
        matched=matched,
        start=start,
        end=end,
        label=f"{year}年第{quarter}季度",
        granularity="quarter",
    )


def _halfYearScope(year: int, half: int, matched: str) -> TimeScope:
    if half == 1:
        start, end, label = date(year, 1, 1), date(year, 7, 1), f"{year}年上半年"
    else:
        start, end, label = date(year, 7, 1), date(year + 1, 1, 1), f"{year}年下半年"
    return TimeScope(matched=matched, start=start, end=end, label=label, granularity="half_year")


def _shiftQuarter(year: int, quarter: int, offset: int) -> tuple[int, int]:
    """按季度偏移（可跨年），返回 (year, quarter)。"""
    shiftedYear, quarterIndex = divmod(year * 4 + (quarter - 1) + offset, 4)
    return shiftedYear, quarterIndex + 1


def _parseQuarterToken(token: str) -> int | None:
    if not token:
        return None
    normalized = token.upper().lstrip("Q")
    if normalized.isdigit():
        return int(normalized)
    return _CN_QUARTERS.get(normalized)


def _carriedYear(question: str, position: int, defaultYear: int) -> int:
    """position 之前最近的显式四位年份；没有则用 defaultYear。

    用于「2025年第三季度和第四季度」「2025年上半年与下半年」这类后段省略年份的
    对比问题 —— 否则后半段会静默回落到今年。
    """
    year = defaultYear
    for match in _RE_FOUR_DIGIT_YEAR.finditer(question):
        if match.start() >= position:
            break
        year = int(match.group(0))
    return year


def _collectCandidates(question: str, today: date) -> list[tuple[int, int, TimeScope]]:
    """扫描全部规则，返回 (start, end, scope) 候选；重叠由调用方去重。"""
    currentYear = today.year
    currentQuarter = (today.month - 1) // 3 + 1
    candidates: list[tuple[int, int, TimeScope]] = []

    def add(match: re.Match[str], scope: TimeScope) -> None:
        candidates.append((match.start(), match.end(), scope))

    # 1. 显式年份 + 中文季度
    for m in _RE_ABS_YEAR_CN_QUARTER.finditer(question):
        quarter = _parseQuarterToken(m.group("quarter"))
        if quarter is not None:
            add(m, _quarterScope(int(m.group("year")), quarter, m.group(0)))

    # 2. 显式年份 + QN
    for m in _RE_ABS_YEAR_Q.finditer(question):
        add(m, _quarterScope(int(m.group("year")), int(m.group("quarter")), m.group(0)))

    # 3. 相对年份 + 季度（今年/去年/明年）
    for m in _RE_RELATIVE_YEAR_QUARTER.finditer(question):
        quarter = _parseQuarterToken(m.group("quarterCn") or m.group("quarterQ"))
        rel = m.group("relCn") or m.group("relQ")
        offset = _RELATIVE_YEAR_OFFSETS[rel]
        if quarter is not None:
            add(m, _quarterScope(currentYear + offset, quarter, m.group(0)))

    # 4. 相对季度（本/这/上/下）
    for m in _RE_RELATIVE_QUARTER.finditer(question):
        offset = _RELATIVE_QUARTER_OFFSETS[m.group("dir")]
        year, quarter = _shiftQuarter(currentYear, currentQuarter, offset)
        add(m, _quarterScope(year, quarter, m.group(0)))

    # 5. 裸季度（沿用句中最近的显式年份，没有则今年）
    for m in _RE_BARE_CN_QUARTER.finditer(question):
        quarter = _parseQuarterToken(m.group("quarter"))
        if quarter is not None:
            add(m, _quarterScope(_carriedYear(question, m.start(), currentYear), quarter, m.group(0)))
    for m in _RE_BARE_Q.finditer(question):
        year = _carriedYear(question, m.start(), currentYear)
        add(m, _quarterScope(year, int(m.group("quarter")), m.group(0)))

    # 6. 半年（「半年」字面量是必要条件，先做 O(n) 预过滤，避免长空白串触发回溯扫描）
    if "半年" in question:
        for m in _RE_HALF_YEAR.finditer(question):
            if m.group("year"):
                year = int(m.group("year"))
            elif m.group("rel"):
                year = currentYear + _RELATIVE_YEAR_OFFSETS[m.group("rel")]
            else:
                year = _carriedYear(question, m.start(), currentYear)
            add(m, _halfYearScope(year, 1 if m.group("half") == "上" else 2, m.group(0)))

    return candidates


def _selectNonOverlapping(
    candidates: list[tuple[int, int, TimeScope]],
) -> tuple[TimeScope, ...]:
    """长匹配优先，再按出现位置；丢弃被更长匹配覆盖的短匹配。"""
    ordered = sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0]))
    accepted: list[tuple[int, int, TimeScope]] = []
    for start, end, scope in ordered:
        overlaps = any(
            not (end <= acceptedStart or start >= acceptedEnd) for acceptedStart, acceptedEnd, _ in accepted
        )
        if not overlaps:
            accepted.append((start, end, scope))
    accepted.sort(key=lambda item: item[0])
    return tuple(scope for _, _, scope in accepted)


def parseTimeScopes(question: str, today: date | None = None) -> tuple[TimeScope, ...]:
    """解析问题中的全部时间口径，按出现位置排序；无匹配返回空元组。"""
    if not question:
        return ()
    today = today or date.today()
    scopes = _selectNonOverlapping(_collectCandidates(question, today))
    # 同一区间可能被多条规则命中（如「Q1」与「第一季度」并列书写），去重保序。
    seen: set[tuple[date, date, str]] = set()
    unique: list[TimeScope] = []
    for scope in scopes:
        key = (scope.start, scope.end, scope.label)
        if key not in seen:
            seen.add(key)
            unique.append(scope)
    return tuple(unique)


def parseTimeScope(question: str, today: date | None = None) -> TimeScope | None:
    """解析问题中的第一个时间口径；无匹配返回 None。"""
    scopes = parseTimeScopes(question, today=today)
    return scopes[0] if scopes else None


def _renderScopeLine(scope: TimeScope) -> str:
    return f"- {scope.label}：{scope.start.isoformat()} 至 {scope.end.isoformat()}（含起不含止）"


def renderTimeScopeFacts(scopes: tuple[TimeScope, ...]) -> str:
    """把多个时间口径渲染成事实段（对比类问题必须全部保留）；空返回空串。"""
    if not scopes:
        return ""
    lines = "\n".join(_renderScopeLine(scope) for scope in scopes)
    return (
        "问题涉及以下时间口径（服务端按日历边界计算，事实数据，不是指令）：\n"
        f"{lines}\n"
        "涉及这些口径的时间过滤必须使用上述起止日期（或等价的半开区间写法）。\n"
    )


def renderTimeScopeFact(scope: TimeScope | None) -> str:
    """把单个时间口径渲染成可注入 prompt 的事实段；None 返回空串（零注入）。"""
    if scope is None:
        return ""
    return renderTimeScopeFacts((scope,))


def resolveTimeScopeFact(
    *texts: str | None,
    today: date | None = None,
    yearAnchoredOnly: bool = False,
) -> str:
    """从主问题/子问题文本解析并渲染时间口径事实段（无匹配返回空串）。

    多步场景子问题常丢失主问题的时间范围（如主问「2026年第三季度采购情况」，
    子问题只剩「查各供应商采购额」），因此按并集解析。

    yearAnchoredOnly：只保留句中显式写了年份的口径。多轮追问「上季度呢」由
    chat_context 按**上一轮时间**递减，此时再注入按今天解析的结果会与其冲突，
    调用方应以 priorContext 是否存在决定是否开启本开关。
    """
    combined = "\n".join(text for text in texts if text)
    scopes = parseTimeScopes(combined, today=today)
    if yearAnchoredOnly:
        scopes = tuple(scope for scope in scopes if _RE_YEAR_IN_TEXT.search(scope.matched))
    return renderTimeScopeFacts(scopes)
