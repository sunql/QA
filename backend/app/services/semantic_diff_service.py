"""语义差异引擎：两份 Manifest 的结构化差异 + 破坏性分类（WP-ONT-AI A1）。

为什么需要它（设计稿 F06「本体差异与影响分析」）：语义发布把「某一刻的本体」冻结成
Manifest 快照（`semantic_release.manifest_json`），但治理表本身是**原地 UPDATE** 的
⇒ 事后无法回答「发布之后本体被改了什么、改动会不会打破下游」。
本模块提供那个答案的**纯函数**部分。

设计要点（对齐 `Harness/changes/2026-11-16-ontology-discovery-ai/plan.md` D-5/D-6/D-7）：

1. **纯函数、零 IO**：输入两个 Manifest 字典，输出 `SemanticDiff`；不查库、不写库、
   **不修改入参**（全程新建对象）。⇒ 可在单测里逐条断言，也可被 A3 的 LLM 候选
   （同一形状的 manifest）直接复用。
2. **结构化而非自然语言**：每条变更是 `SemanticChange`，带 `kind` / `severity` /
   `section` / `target` / `field` / `before` / `after` —— 机器可读，机器码优先
   （与 `kpi_metric_publish_gate.GateItem` 的「英文机器码 + 中文正文分离」同风格）。
3. **破坏性分类是显式判据表**，不是启发式：删类 / 删属性 / 改类型 / 收紧非空 /
   改引用 / 改物理列 / 改 join 键 = `BREAKING`；新增 = `ADDITIVE`；
   描述别名等展示字段 = `COSMETIC`。
4. **不做启发式改名检测**：类改名呈现为「删除 + 新增」。启发式改名会制造**假重命名**
   （两个本该分开的东西被并到一起），比漏报一个改名更危险。
5. **删类不重复报属性**：类被删 ⇒ 其属性随之消失，只报一条 `CLASS_REMOVED`。
6. **确定性**：输出按 (分区, 目标, 变更类型, 字段) 稳定排序 ⇒ 输入顺序打乱结果不变。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# 严重度取值（与设计稿 D-6 的风险分级一一对应）。
BREAKING = "BREAKING"
ADDITIVE = "ADDITIVE"
COSMETIC = "COSMETIC"

# 风险分级（D-6）：BREAKING → HIGH；ADDITIVE → LOW；COSMETIC → INFO。
# **不引入「兼容百分比」这类虚构指标**——兼容判据 = 「是否存在 BREAKING 变更」。
RISK_HIGH = "HIGH"
RISK_LOW = "LOW"
RISK_INFO = "INFO"

_RISK_BY_SEVERITY = {BREAKING: RISK_HIGH, ADDITIVE: RISK_LOW, COSMETIC: RISK_INFO}

# 分区名（与 Manifest v1 的顶层分区同名）。
SECTION_CLASSES = "classes"
SECTION_PROPERTIES = "properties"
SECTION_JOINS = "joins"

_SECTION_ORDER = {SECTION_CLASSES: 0, SECTION_PROPERTIES: 1, SECTION_JOINS: 2}

# Manifest 的顶层分区全集（`semantic_manifest_schema.MANIFEST_SCHEMAS`）。
# ⚠️ v2 追加了 `sourceBindings`（M1-1.3 / D-9）—— 本引擎**未覆盖**它，
# ⇒ 它自动落进 `UNCOVERED_SECTIONS`，在 API 契约里被显式披露
# （「发布后绑定变了」目前不可见 —— 已知缺口，本批不修）。
MANIFEST_SECTIONS = (
    "domains",
    "classes",
    "joins",
    "metrics",
    "kpis",
    "businessObjects",
    "sourceBindings",
)

# 本引擎**已覆盖**的分区：类、类内属性、JOIN。
COVERED_SECTIONS = (SECTION_CLASSES, SECTION_PROPERTIES, SECTION_JOINS)

# 本引擎**未覆盖**的分区（plan D-10 已登记为范围裁剪）。
# ⚠️ 必须在 API 契约里**披露**：否则「KPI 被删了」这类变更会得到
# `hasBreakingChanges=false`，把「部分比较」误读成「没有破坏性变更」。
UNCOVERED_SECTIONS = tuple(
    name for name in MANIFEST_SECTIONS if name not in COVERED_SECTIONS
)


@dataclass(frozen=True)
class SemanticChange:
    """一条结构化差异。"""

    kind: str
    severity: str
    section: str
    target: str
    field: str | None = None
    before: Any = None
    after: Any = None

    @property
    def riskLevel(self) -> str:
        """该变更的风险等级（D-6）。"""

        return _RISK_BY_SEVERITY.get(self.severity, RISK_INFO)


def selectChangesForResponse(
    changes: Sequence[SemanticChange], limit: int
) -> tuple[tuple[SemanticChange, ...], int]:
    """为响应挑变更：截断时**优先保留 BREAKING**（MEDIUM-2）。

    为什么要特殊处理：Manifest 允许 5000 类 + 20000 join ⇒ 全量差异可达上万条，
    响应必须截断。但**破坏性变更不能被截掉** —— 若按原序截断，破坏性变更恰好
    排在后面时，调用方会看到「变更列表里没有破坏性变更」而 `hasBreakingChanges`
    却是 true ⇒ 误判。

    返回 `(选中的变更, 被丢弃的 BREAKING 条数)`：
    - 先放全部 BREAKING（按原序），再按原序补其余变更；
    - BREAKING 本身超过上限时，按原序取前 N 条，并如实报告丢弃了几条
      （调用方据此知道「破坏性变更没看全」，不能当成「已看完」）。
    """

    if limit <= 0:
        return (), sum(1 for change in changes if change.severity == BREAKING)
    breaking = [change for change in changes if change.severity == BREAKING]
    others = [change for change in changes if change.severity != BREAKING]
    selected = breaking[:limit]
    droppedBreaking = len(breaking) - len(selected)
    room = limit - len(selected)
    if room > 0:
        selected = selected + others[:room]
    return tuple(selected), droppedBreaking


@dataclass(frozen=True)
class SemanticDiff:
    """一次差异分析的结果（不可变）。"""

    changes: tuple[SemanticChange, ...] = ()

    @property
    def changeCount(self) -> int:
        return len(self.changes)

    @property
    def hasBreakingChanges(self) -> bool:
        return any(change.severity == BREAKING for change in self.changes)

    @property
    def breakingCount(self) -> int:
        return self._countBy(BREAKING)

    @property
    def additiveCount(self) -> int:
        return self._countBy(ADDITIVE)

    @property
    def cosmeticCount(self) -> int:
        return self._countBy(COSMETIC)

    @property
    def overallRiskLevel(self) -> str:
        """整体风险等级：取各变更的最高级（有 BREAKING ⇒ HIGH）。"""

        if self.hasBreakingChanges:
            return RISK_HIGH
        if self.additiveCount > 0:
            return RISK_LOW
        return RISK_INFO

    def changesBySeverity(self, severity: str) -> tuple[SemanticChange, ...]:
        """按严重度过滤（顺序保持全局排序）。"""

        return tuple(change for change in self.changes if change.severity == severity)

    @property
    def coveredSections(self) -> tuple[str, ...]:
        """本次比较**覆盖**的分区。"""

        return COVERED_SECTIONS

    @property
    def coveredFields(self) -> dict[str, tuple[str, ...]]:
        """本次比较**覆盖**的字段（分区 → 字段名）。

        分区级披露不够：`covered_sections` 说「比了 properties」，但实际比的是
        一张**手写**字段表 ⇒ 将来 manifest 契约新增字段而引擎漏比时，
        披露与事实不符（代码审查 MEDIUM-5）。字段级披露让消费方能自己判断。
        """

        return {
            SECTION_CLASSES: tuple(field for field, _kind, _severity in _CLASS_FIELDS),
            SECTION_PROPERTIES: tuple(
                field for field, _kind, _severity in _PROPERTY_FIELDS
            ),
            SECTION_JOINS: tuple(field for field, _kind, _severity in _JOIN_FIELDS),
        }

    @property
    def uncoveredSections(self) -> tuple[str, ...]:
        """本次比较**未覆盖**的分区（Manifest 里还有这些分区）。"""

        return UNCOVERED_SECTIONS

    @property
    def isPartial(self) -> bool:
        """是否为「部分比较」（未覆盖全部分区 ⇒ 不能据此断言「无破坏性变更」）。"""

        return bool(UNCOVERED_SECTIONS)

    def _countBy(self, severity: str) -> int:
        return sum(1 for change in self.changes if change.severity == severity)


# (字段, 变更类型, 严重度)。`isNotNull` 的严重度随方向变化，单独处理。
_CLASS_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("classAlias", "CLASS_ALIAS_CHANGED", COSMETIC),
    ("description", "CLASS_DESCRIPTION_CHANGED", COSMETIC),
    ("sourceTable", "CLASS_SOURCE_TABLE_CHANGED", BREAKING),
    ("objectType", "CLASS_OBJECT_TYPE_CHANGED", BREAKING),
    ("objectOwner", "CLASS_OWNER_CHANGED", COSMETIC),
    ("parentClassName", "CLASS_PARENT_CHANGED", BREAKING),
)

_PROPERTY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("propertyAlias", "PROPERTY_ALIAS_CHANGED", COSMETIC),
    ("businessAliases", "PROPERTY_BUSINESS_ALIASES_CHANGED", COSMETIC),
    ("description", "PROPERTY_DESCRIPTION_CHANGED", COSMETIC),
    ("dataType", "PROPERTY_TYPE_CHANGED", BREAKING),
    ("isPrimaryKey", "PROPERTY_KEY_CHANGED", BREAKING),
    ("isForeignKey", "PROPERTY_KEY_CHANGED", BREAKING),
    ("refClassName", "PROPERTY_REF_CHANGED", BREAKING),
    ("sourceColumn", "PROPERTY_SOURCE_COLUMN_CHANGED", BREAKING),
    ("unit", "PROPERTY_UNIT_CHANGED", COSMETIC),
    # 下面 4 个是**约束**字段：严重度随方向变化（收紧 = 破坏性 / 放宽 = 不破坏），
    # 由 `_severityFor` 的方向规则覆盖 ⇒ 表里的默认值只是「收紧侧」的兜底。
    ("allowedValues", "PROPERTY_ALLOWED_VALUES_CHANGED", BREAKING),
    # 三态：None = 未约束、False = 允许为空、True = 必填。
    # 严重度随方向变化（收紧破坏性 / 放宽不破坏）⇒ 由 `_severityFor` 覆盖。
    ("isNotNull", "PROPERTY_REQUIRED_CHANGED", COSMETIC),
    ("minValue", "PROPERTY_RANGE_CHANGED", BREAKING),
    ("maxValue", "PROPERTY_RANGE_CHANGED", BREAKING),
    ("regexPattern", "PROPERTY_PATTERN_CHANGED", BREAKING),
)

_JOIN_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("sourceClassName", "JOIN_TARGET_CHANGED", BREAKING),
    ("targetClassName", "JOIN_TARGET_CHANGED", BREAKING),
    ("sourceColumns", "JOIN_COLUMNS_CHANGED", BREAKING),
    ("targetColumns", "JOIN_COLUMNS_CHANGED", BREAKING),
    ("joinType", "JOIN_TYPE_CHANGED", BREAKING),
    ("relationType", "JOIN_RELATION_TYPE_CHANGED", COSMETIC),
    ("description", "JOIN_DESCRIPTION_CHANGED", COSMETIC),
)


def diffManifests(
    base: Mapping[str, Any], candidate: Mapping[str, Any]
) -> SemanticDiff:
    """比较两份 Manifest，返回结构化差异。

    参数：
    - `base`：基线（通常是已发布发布的 `manifest_json`）。
    - `candidate`：候选（当前治理表编译结果，或未来 LLM 产出的候选 manifest）。

    缺失的分区按**空**处理（不是错误）⇒ 只有一个分区也能比较。
    """

    _assertManifestMapping(base)
    _assertManifestMapping(candidate)
    changes: list[SemanticChange] = []
    changes.extend(_diffClasses(base, candidate))
    changes.extend(_diffProperties(base, candidate))
    changes.extend(_diffJoins(base, candidate))
    return SemanticDiff(changes=tuple(_sortedChanges(changes)))


# ---------------------------------------------------------------- 输入校验


def _assertManifestMapping(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(
            f"Manifest 必须是字典（Mapping），收到 {type(value).__name__}"
        )


def _section(value: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    """取一个分区；缺失或非列表一律按空列表处理。"""

    raw = value.get(name)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


# ---------------------------------------------------------------- 类层


def _diffClasses(
    base: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[SemanticChange]:
    baseByName = _indexByName(_section(base, SECTION_CLASSES), "className")
    candidateByName = _indexByName(_section(candidate, SECTION_CLASSES), "className")
    changes: list[SemanticChange] = []
    for name in sorted(candidateByName):
        if name not in baseByName:
            changes.append(
                SemanticChange(
                    kind="CLASS_ADDED",
                    severity=ADDITIVE,
                    section=SECTION_CLASSES,
                    target=name,
                )
            )
            continue
        changes.extend(
            _compareFields(
                baseByName[name],
                candidateByName[name],
                _CLASS_FIELDS,
                section=SECTION_CLASSES,
                target=name,
            )
        )
    for name in sorted(baseByName):
        if name not in candidateByName:
            changes.append(
                SemanticChange(
                    kind="CLASS_REMOVED",
                    severity=BREAKING,
                    section=SECTION_CLASSES,
                    target=name,
                )
            )
    return changes


def _compareFields(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: tuple[tuple[str, str, str], ...],
    *,
    section: str,
    target: str,
) -> list[SemanticChange]:
    changes: list[SemanticChange] = []
    for field, kind, severity in fields:
        oldValue = before.get(field)
        newValue = after.get(field)
        if oldValue == newValue:
            continue
        if _isEquivalent(field, oldValue, newValue):
            continue
        changes.append(
            SemanticChange(
                kind=kind,
                severity=_severityFor(field, severity, oldValue, newValue),
                section=section,
                target=target,
                field=field,
                before=_copyValue(oldValue),
                after=_copyValue(newValue),
            )
        )
    return changes


def _isEquivalent(field: str, oldValue: Any, newValue: Any) -> bool:
    """边界字段的「等价」判定：**数值相等**即等价（`"10"` 与 `"10.0"`）。

    边界在契约里是字符串（`min_value` / `max_value` 是 `String(50)`），但业务
    语义上是数 ⇒ `"10"` 改写成 `"10.0"` 不是一次收紧，只是写法不同。
    字符串字面量不等却被当成「变更」会制造假变更。
    """

    if field not in ("minValue", "maxValue"):
        return False
    oldNumber = _asNumber(oldValue)
    newNumber = _asNumber(newValue)
    if oldNumber is None or newNumber is None:
        return False
    return oldNumber == newNumber


def _copyValue(value: Any) -> Any:
    """列表 / 字典值复制一份。

    不复制的话，`change.after` 与入参 manifest **共享同一个对象** ⇒ 调用方
    （A3 的 LLM 候选 manifest）改一下 change 就把入参改了 ⇒ 违反「全程新建
    对象、绝不修改入参」的承诺。
    """

    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _severityFor(
    field: str, default: str, oldValue: Any, newValue: Any
) -> str:
    """严重度的**方向规则**：约束收紧 = 破坏性，放宽 = 不破坏。

    - `isNotNull`：None → True（必填化）= 收紧 = BREAKING；True → False = 放宽。
    - `minValue`：抬高下限 = 值域变小 = BREAKING；`maxValue`：压低上限同理。
    - `allowedValues`：枚举集变小 = BREAKING；变大 = 放宽。
    - `regexPattern`：**判不了方向**（新正则可能更宽也可能更窄）⇒ 按收紧处理
      （保守：宁可高报风险，也不漏报「既有写入被拒」）。

    「从无约束变成有约束」一律算收紧（BREAKING）；「有约束变成无约束」算放宽。
    """

    if field == "isNotNull":
        return BREAKING if newValue is True else COSMETIC
    if field == "minValue":
        return _boundSeverity(oldValue, newValue, tighterWhenHigher=True)
    if field == "maxValue":
        return _boundSeverity(oldValue, newValue, tighterWhenHigher=False)
    if field == "allowedValues":
        return _allowedValuesSeverity(oldValue, newValue)
    if field == "regexPattern":
        return BREAKING
    return default


def _boundSeverity(oldValue: Any, newValue: Any, *, tighterWhenHigher: bool) -> str:
    """数值 / 字符串边界的方向判定。

    **优先按数值比较**（代码审查 HIGH-1）：边界在契约里是字符串
    （`min_value` / `max_value` 是 `String(50)`），但字典序 ≠ 数值序 ⇒
    `"9"` → `"10"`（数值上是抬高下限 = 收紧）会被字典序判成**放宽** ⇒
    漏判破坏性变更。旧单测用 int 边界（`0 → 10`）恒走数值分支 ⇒ 假绿。

    两边都解析不出数字时退回**字典序**比较：ISO 日期串（`2024-01-01`）的
    字典序与时序一致；两边类型都不可比（bool / 混合类型）⇒ 保守按收紧处理
    （宁可高报风险，也不漏报「既有写入被拒」）。
    """

    if newValue is None:
        return COSMETIC  # 约束被移除 = 放宽
    if oldValue is None:
        return BREAKING  # 新增约束 = 收紧
    if isinstance(oldValue, bool) or isinstance(newValue, bool):
        return BREAKING  # 不可比 ⇒ 保守
    oldNumber = _asNumber(oldValue)
    newNumber = _asNumber(newValue)
    if oldNumber is not None and newNumber is not None:
        tightened = (
            newNumber > oldNumber if tighterWhenHigher else newNumber < oldNumber
        )
        return BREAKING if tightened else COSMETIC
    if isinstance(oldValue, str) and isinstance(newValue, str):
        tightened = newValue > oldValue if tighterWhenHigher else newValue < oldValue
        return BREAKING if tightened else COSMETIC
    return BREAKING  # 不可比 ⇒ 保守


def _asNumber(value: Any) -> Decimal | None:
    """把边界值解析成十进制数；解析不了（或非有限）返回 None。"""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def _allowedValuesSeverity(oldValue: Any, newValue: Any) -> str:
    """枚举集的方向判定（变小 = 收紧）。"""

    if newValue is None:
        return COSMETIC  # 约束被移除 = 放宽
    if oldValue is None:
        return BREAKING  # 新增约束 = 收紧
    if not isinstance(oldValue, list) or not isinstance(newValue, list):
        return BREAKING
    oldSet = _hashableSet(oldValue)
    newSet = _hashableSet(newValue)
    if oldSet is None or newSet is None:
        # 元素不可哈希 ⇒ 判不了方向 ⇒ 保守按收紧处理
        return BREAKING
    if newSet == oldSet:
        # 集合相同（只是顺序不同）⇒ 顺序无语义 ⇒ 不是约束变化
        return COSMETIC
    if newSet < oldSet:
        return BREAKING  # 枚举集变小 = 收紧
    if newSet > oldSet:
        return COSMETIC  # 枚举集变大 = 放宽
    # 同样多但内容不同 ⇒ 有旧值被丢弃 ⇒ 既有写入可能被拒
    return BREAKING


def _hashableSet(values: list[Any]) -> set[Any] | None:
    try:
        return set(values)
    except TypeError:
        return None


# ---------------------------------------------------------------- 属性层


def _diffProperties(
    base: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[SemanticChange]:
    baseClasses = _indexByName(_section(base, SECTION_CLASSES), "className")
    candidateClasses = _indexByName(_section(candidate, SECTION_CLASSES), "className")
    changes: list[SemanticChange] = []
    for className in sorted(candidateClasses):
        if className not in baseClasses:
            continue
        changes.extend(
            _diffClassProperties(
                className,
                baseClasses[className],
                candidateClasses[className],
            )
        )
    return changes


def _diffClassProperties(
    className: str,
    baseClass: Mapping[str, Any],
    candidateClass: Mapping[str, Any],
) -> list[SemanticChange]:
    baseProps = _indexByName(_propertiesOf(baseClass), "propertyName")
    candidateProps = _indexByName(_propertiesOf(candidateClass), "propertyName")
    changes: list[SemanticChange] = []
    for name in sorted(candidateProps):
        target = f"{className}.{name}"
        if name not in baseProps:
            changes.append(
                SemanticChange(
                    kind="PROPERTY_ADDED",
                    severity=ADDITIVE,
                    section=SECTION_PROPERTIES,
                    target=target,
                )
            )
            continue
        changes.extend(
            _compareFields(
                baseProps[name],
                candidateProps[name],
                _PROPERTY_FIELDS,
                section=SECTION_PROPERTIES,
                target=target,
            )
        )
    for name in sorted(baseProps):
        if name not in candidateProps:
            changes.append(
                SemanticChange(
                    kind="PROPERTY_REMOVED",
                    severity=BREAKING,
                    section=SECTION_PROPERTIES,
                    target=f"{className}.{name}",
                )
            )
    return changes


def _propertiesOf(cls: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = cls.get("properties")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


# ---------------------------------------------------------------- JOIN 层


def _diffJoins(
    base: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[SemanticChange]:
    baseByKey = _indexByName(_section(base, SECTION_JOINS), "joinKey")
    candidateByKey = _indexByName(_section(candidate, SECTION_JOINS), "joinKey")
    changes: list[SemanticChange] = []
    for key in sorted(candidateByKey):
        if key not in baseByKey:
            changes.append(
                SemanticChange(
                    kind="JOIN_ADDED",
                    severity=ADDITIVE,
                    section=SECTION_JOINS,
                    target=key,
                )
            )
            continue
        changes.extend(
            _compareFields(
                baseByKey[key],
                candidateByKey[key],
                _JOIN_FIELDS,
                section=SECTION_JOINS,
                target=key,
            )
        )
    for key in sorted(baseByKey):
        if key not in candidateByKey:
            changes.append(
                SemanticChange(
                    kind="JOIN_REMOVED",
                    severity=BREAKING,
                    section=SECTION_JOINS,
                    target=key,
                )
            )
    return changes


# ---------------------------------------------------------------- 工具


def _indexByName(
    rows: Sequence[Mapping[str, Any]], key: str
) -> dict[str, Mapping[str, Any]]:
    """按标识字段建索引；重复标识取**首条**（编译器保证唯一，此处防御性取首条）。"""

    index: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        name = row.get(key)
        if isinstance(name, str) and name not in index:
            index[name] = row
    return index


def _sortedChanges(changes: Sequence[SemanticChange]) -> list[SemanticChange]:
    return sorted(
        changes,
        key=lambda change: (
            _SECTION_ORDER.get(change.section, 99),
            change.target,
            change.kind,
            change.field or "",
        ),
    )
