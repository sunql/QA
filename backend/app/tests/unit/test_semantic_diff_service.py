"""语义差异引擎（WP-ONT-AI A1 / D-3、D-5、D-6、D-7）单测。

引擎是**纯函数**：`diffManifests(base, candidate) -> SemanticDiff`，零 IO、零 DB、
不改入参。本文件钉住：

1. 差异的**结构**（section / target / field / before / after）；
2. **破坏性分类**（D-5 表：删类/删属性/改类型/收紧非空/改引用/改 join 键 = BREAKING）；
3. **确定性**（输入顺序打乱 ⇒ 输出逐条相同）；
4. **不可变性**（入参 dict 不被就地修改）。
"""

from __future__ import annotations

import copy

import pytest

from app.domain.semantic_manifest_schema import SCHEMA_VERSION
from app.services.semantic_diff_service import (
    SemanticChange,
    diffManifests,
    selectChangesForResponse,
)


def _prop(name: str, **overrides) -> dict:
    prop = {
        "propertyName": name,
        "propertyAlias": None,
        "businessAliases": [],
        "description": None,
        "dataType": "STRING",
        "isPrimaryKey": False,
        "isForeignKey": False,
        "refClassName": None,
        "sourceColumn": name.lower(),
        "unit": None,
        "allowedValues": [],
        "isNotNull": None,
        "minValue": None,
        "maxValue": None,
        "regexPattern": None,
    }
    prop.update(overrides)
    return prop


def _cls(name: str, props: list[dict] | None = None, **overrides) -> dict:
    cls = {
        "className": name,
        "classAlias": None,
        "description": None,
        "sourceTable": name.lower(),
        "objectType": "Master",
        "objectOwner": None,
        "parentClassName": None,
        "properties": props or [],
    }
    cls.update(overrides)
    return cls


def _join(key: str, **overrides) -> dict:
    join = {
        "joinKey": key,
        "sourceClassName": "A",
        "sourceColumns": ["a_id"],
        "targetClassName": "B",
        "targetColumns": ["b_id"],
        "joinType": "INNER",
        "relationType": "business",
        "description": None,
    }
    join.update(overrides)
    return join


def _manifest(classes: list[dict] | None = None, joins: list[dict] | None = None) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "domains": [],
        "classes": classes or [],
        "joins": joins or [],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


def _kinds(diff) -> list[str]:
    return [change.kind for change in diff.changes]


def _find(diff, kind: str) -> SemanticChange:
    matched = [change for change in diff.changes if change.kind == kind]
    assert matched, f"期望存在 {kind} 变更，实际 {_kinds(diff)}"
    return matched[0]


def _find_list(diff, kind: str) -> list[SemanticChange]:
    return [change for change in diff.changes if change.kind == kind]


# ---------------------------------------------------------------- 基础形状


def test_相同清单无差异():
    manifest = _manifest([_cls("A", [_prop("id", isPrimaryKey=True)])], [_join("j1")])

    diff = diffManifests(manifest, copy.deepcopy(manifest))

    assert diff.changes == ()
    assert diff.hasBreakingChanges is False
    assert diff.changeCount == 0


def test_空清单与空清单无差异():
    diff = diffManifests(_manifest(), _manifest())

    assert diff.changes == ()


def test_入参不被就地修改():
    base = _manifest([_cls("A", [_prop("id")])])
    candidate = _manifest([_cls("A", [_prop("id", dataType="INT")])])
    baseBefore = copy.deepcopy(base)
    candidateBefore = copy.deepcopy(candidate)

    diffManifests(base, candidate)

    assert base == baseBefore
    assert candidate == candidateBefore


# ---------------------------------------------------------------- 类层差异


def test_新增类判为增量():
    diff = diffManifests(_manifest(), _manifest([_cls("A")]))

    change = _find(diff, "CLASS_ADDED")
    assert change.severity == "ADDITIVE"
    assert change.section == "classes"
    assert change.target == "A"


def test_删除类判为破坏性():
    diff = diffManifests(_manifest([_cls("A")]), _manifest())

    change = _find(diff, "CLASS_REMOVED")
    assert change.severity == "BREAKING"
    assert diff.hasBreakingChanges is True


def test_删除类不再逐条报属性删除():
    """类被删 ⇒ 其属性随之消失，只报一次 CLASS_REMOVED（避免噪声）。"""

    diff = diffManifests(_manifest([_cls("A", [_prop("id"), _prop("name")])]), _manifest())

    assert _kinds(diff) == ["CLASS_REMOVED"]


def test_改描述判为外观级():
    diff = diffManifests(
        _manifest([_cls("A", description="旧")]),
        _manifest([_cls("A", description="新")]),
    )

    change = _find(diff, "CLASS_DESCRIPTION_CHANGED")
    assert change.severity == "COSMETIC"
    assert change.field == "description"
    assert change.before == "旧"
    assert change.after == "新"


def test_改物理表判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", sourceTable="t_old")]),
        _manifest([_cls("A", sourceTable="t_new")]),
    )

    change = _find(diff, "CLASS_SOURCE_TABLE_CHANGED")
    assert change.severity == "BREAKING"


def test_改父类判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", parentClassName="P1")]),
        _manifest([_cls("A", parentClassName="P2")]),
    )

    change = _find(diff, "CLASS_PARENT_CHANGED")
    assert change.severity == "BREAKING"


# ---------------------------------------------------------------- 属性层差异


def test_新增属性判为增量():
    diff = diffManifests(
        _manifest([_cls("A")]),
        _manifest([_cls("A", [_prop("x")])]),
    )

    change = _find(diff, "PROPERTY_ADDED")
    assert change.severity == "ADDITIVE"
    assert change.target == "A.x"


def test_删除属性判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x")])]),
        _manifest([_cls("A")]),
    )

    change = _find(diff, "PROPERTY_REMOVED")
    assert change.severity == "BREAKING"
    assert change.target == "A.x"


def test_改数据类型判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", dataType="STRING")])]),
        _manifest([_cls("A", [_prop("x", dataType="INT")])]),
    )

    change = _find(diff, "PROPERTY_TYPE_CHANGED")
    assert change.severity == "BREAKING"
    assert change.before == "STRING"
    assert change.after == "INT"


def test_收紧非空判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", isNotNull=False)])]),
        _manifest([_cls("A", [_prop("x", isNotNull=True)])]),
    )

    change = _find(diff, "PROPERTY_REQUIRED_CHANGED")
    assert change.severity == "BREAKING"


def test_放宽非空判为外观级():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", isNotNull=True)])]),
        _manifest([_cls("A", [_prop("x", isNotNull=False)])]),
    )

    change = _find(diff, "PROPERTY_REQUIRED_CHANGED")
    assert change.severity == "COSMETIC"


def test_改引用类判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", isForeignKey=True, refClassName="B")])]),
        _manifest([_cls("A", [_prop("x", isForeignKey=True, refClassName="C")])]),
    )

    change = _find(diff, "PROPERTY_REF_CHANGED")
    assert change.severity == "BREAKING"


def test_改物理列判为破坏性():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", sourceColumn="x_old")])]),
        _manifest([_cls("A", [_prop("x", sourceColumn="x_new")])]),
    )

    change = _find(diff, "PROPERTY_SOURCE_COLUMN_CHANGED")
    assert change.severity == "BREAKING"


def test_属性差异只在两边都存在的类上比较():
    """只有一边存在的类 ⇒ 由类层差异表达，不重复报属性。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x")])]),
        _manifest([_cls("B", [_prop("y")])]),
    )

    assert sorted(_kinds(diff)) == ["CLASS_ADDED", "CLASS_REMOVED"]


# ---------------------------------------------------------------- JOIN 差异


def test_新增join判为增量():
    diff = diffManifests(_manifest(), _manifest(joins=[_join("j1")]))

    change = _find(diff, "JOIN_ADDED")
    assert change.severity == "ADDITIVE"
    assert change.target == "j1"


def test_删除join判为破坏性():
    diff = diffManifests(_manifest(joins=[_join("j1")]), _manifest())

    change = _find(diff, "JOIN_REMOVED")
    assert change.severity == "BREAKING"


def test_改join键列判为破坏性():
    diff = diffManifests(
        _manifest(joins=[_join("j1", targetColumns=["b_id"])]),
        _manifest(joins=[_join("j1", targetColumns=["code"])]),
    )

    change = _find(diff, "JOIN_COLUMNS_CHANGED")
    assert change.severity == "BREAKING"
    assert change.field == "targetColumns"


def test_改join类型判为破坏性():
    diff = diffManifests(
        _manifest(joins=[_join("j1", joinType="INNER")]),
        _manifest(joins=[_join("j1", joinType="LEFT")]),
    )

    change = _find(diff, "JOIN_TYPE_CHANGED")
    assert change.severity == "BREAKING"


# ---------------------------------------------------------------- 确定性 / 统计


def test_输入顺序打乱输出不变():
    base = _manifest(
        [_cls("A", [_prop("x"), _prop("y")]), _cls("B")],
        [_join("j2"), _join("j1")],
    )
    candidate = _manifest(
        [_cls("B", [_prop("z")]), _cls("A", [_prop("x", dataType="INT")])],
        [_join("j1", joinType="LEFT"), _join("j3")],
    )

    first = diffManifests(base, candidate)
    second = diffManifests(
        _manifest(
            list(reversed(base["classes"])),
            list(reversed(base["joins"])),
        ),
        _manifest(
            list(reversed(candidate["classes"])),
            list(reversed(candidate["joins"])),
        ),
    )

    assert first.changes == second.changes


def test_按严重度统计():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x")])]),
        _manifest([_cls("A", [_prop("x", dataType="INT")]), _cls("B")]),
    )

    assert diff.changeCount == 2
    assert diff.breakingCount == 1
    assert diff.additiveCount == 1
    assert len(diff.changesBySeverity("BREAKING")) == 1


def test_缺失分区按空处理():
    """基线只有 classes 段、候选只有 joins 段 ⇒ 不抛错，按各自分区比较。"""

    diff = diffManifests({"classes": [_cls("A")]}, {"joins": [_join("j1")]})

    assert sorted(_kinds(diff)) == ["CLASS_REMOVED", "JOIN_ADDED"]


@pytest.mark.parametrize("bad", [None, [], "x"])
def test_非法输入抛错(bad):
    with pytest.raises(ValueError):
        diffManifests(bad, _manifest())


# ---------------------------------------------------------------- 风险分级（D-6）


def test_破坏性变更风险为高():
    diff = diffManifests(_manifest([_cls("A")]), _manifest())

    assert _find(diff, "CLASS_REMOVED").riskLevel == "HIGH"


def test_增量变更风险为低():
    diff = diffManifests(_manifest(), _manifest([_cls("A")]))

    assert _find(diff, "CLASS_ADDED").riskLevel == "LOW"


def test_外观级变更风险为信息():
    diff = diffManifests(
        _manifest([_cls("A", description="旧")]),
        _manifest([_cls("A", description="新")]),
    )

    assert _find(diff, "CLASS_DESCRIPTION_CHANGED").riskLevel == "INFO"


def test_整体风险等级取最高():
    """有破坏性变更 ⇒ 整体 HIGH；否则有增量 ⇒ LOW；再否则 INFO。"""

    breaking = diffManifests(_manifest([_cls("A")]), _manifest())
    additive = diffManifests(_manifest(), _manifest([_cls("A")]))
    cosmetic = diffManifests(
        _manifest([_cls("A", description="旧")]),
        _manifest([_cls("A", description="新")]),
    )

    assert breaking.overallRiskLevel == "HIGH"
    assert additive.overallRiskLevel == "LOW"
    assert cosmetic.overallRiskLevel == "INFO"


# ---------------------------------------------------------------- 约束方向规则
#
# D-5 方向规则（代码审查 M-4）：约束**收紧** = 破坏性，**放宽** = 不破坏。
# 与 `isNotNull` 同族，不能一律记成外观级。


def test_抬高下限是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue=0)])]),
        _manifest([_cls("A", [_prop("x", minValue=10)])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "BREAKING"


def test_放宽下限不是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue=10)])]),
        _manifest([_cls("A", [_prop("x", minValue=0)])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "COSMETIC"


def test_压低上限是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", maxValue=100)])]),
        _manifest([_cls("A", [_prop("x", maxValue=10)])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "BREAKING"


def test_新增约束是破坏性变更():
    """从「无约束」到「有约束」= 值域变小 = 破坏性。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue=None)])]),
        _manifest([_cls("A", [_prop("x", minValue=1)])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "BREAKING"


def test_移除约束不是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", maxValue=100)])]),
        _manifest([_cls("A", [_prop("x", maxValue=None)])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "COSMETIC"


def test_枚举集变小是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", allowedValues=["A", "B"])])]),
        _manifest([_cls("A", [_prop("x", allowedValues=["A"])])]),
    )

    assert _find(diff, "PROPERTY_ALLOWED_VALUES_CHANGED").severity == "BREAKING"


def test_枚举集变大不是破坏性变更():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", allowedValues=["A"])])]),
        _manifest([_cls("A", [_prop("x", allowedValues=["A", "B"])])]),
    )

    assert _find(diff, "PROPERTY_ALLOWED_VALUES_CHANGED").severity == "COSMETIC"


def test_正则变更按收紧处理():
    """判不了方向 ⇒ 保守高报（宁可误报风险也不漏报「既有写入被拒」）。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", regexPattern="^A")])]),
        _manifest([_cls("A", [_prop("x", regexPattern="^B")])]),
    )

    assert _find(diff, "PROPERTY_PATTERN_CHANGED").severity == "BREAKING"


# ---------------------------------------------------------------- 覆盖度披露


def test_差异披露覆盖与未覆盖分区():
    """M-3：未覆盖 metrics/kpis/domains/businessObjects ⇒ 必须披露，别让
    「部分比较」被误读成「没有破坏性变更」。

    v2 的 `sourceBindings` 已在 M1-1.3 批次 B **纳入覆盖**（此前是已知缺口），
    但它不在「未覆盖」清单里这件事本身也要被钉住 —— 悄悄回退成未覆盖
    ⇒ 「发布后绑定变了」又不可见了。
    """

    diff = diffManifests(_manifest(), _manifest())

    assert "classes" in diff.coveredSections
    assert "properties" in diff.coveredSections
    assert "joins" in diff.coveredSections
    assert "sourceBindings" in diff.coveredSections
    assert set(diff.uncoveredSections) == {
        "domains",
        "metrics",
        "kpis",
        "businessObjects",
    }
    assert diff.isPartial is True


# ---------------------------------------------------------------- 值拷贝


def test_变更值不与入参共享对象():
    """L-1：change.after 必须是**副本** ⇒ 调用方改它不会污染入参 manifest。"""

    base = _manifest([_cls("A", [_prop("x", allowedValues=["A"])])])
    candidate = _manifest([_cls("A", [_prop("x", allowedValues=["A", "B"])])])

    diff = diffManifests(base, candidate)
    change = _find(diff, "PROPERTY_ALLOWED_VALUES_CHANGED")
    assert change.after is not candidate["classes"][0]["properties"][0]["allowedValues"]

    change.after.append("C")

    assert candidate["classes"][0]["properties"][0]["allowedValues"] == ["A", "B"]


# ---------------------------------------------------------------- 边界按数值比较
#
# 代码审查 HIGH-1：`min_value` / `max_value` 在 ORM 里是 `String(50)`，Manifest 里
# 也是字符串 ⇒ **真实数据永远是字符串**。原实现按字典序比较 ⇒ `"9"` → `"10"`
# （数值上是抬高下限 = 收紧）被判成 COSMETIC ⇒ **漏判破坏性变更**。
# 旧单测用 int 边界（`0 → 10`）恒走数值分支 ⇒ 假绿。


def test_字符串下限按数值比较抬高即收紧():
    """`"9"` → `"10"`：字典序 `"10" < "9"` 会判成放宽（假阴性）⇒ 必须按数值比较。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue="9")])]),
        _manifest([_cls("A", [_prop("x", minValue="10")])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "BREAKING"


def test_字符串上限按数值比较放宽即非收紧():
    """`"9"` → `"10"`：上限抬高 = 放宽 ⇒ 不该报破坏性。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", maxValue="9")])]),
        _manifest([_cls("A", [_prop("x", maxValue="10")])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "COSMETIC"


def test_字符串下限按数值比较放宽即非收紧():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue="100")])]),
        _manifest([_cls("A", [_prop("x", minValue="99")])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "COSMETIC"


def test_字符串上限按数值比较压低即收紧():
    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", maxValue="100")])]),
        _manifest([_cls("A", [_prop("x", maxValue="99")])]),
    )

    assert _find(diff, "PROPERTY_RANGE_CHANGED").severity == "BREAKING"


def test_等值不同写法按数值比较不算变更():
    """`"10"` 与 `"10.0"` 数值相同 ⇒ 不该报变更（数值口径）。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", minValue="10")])]),
        _manifest([_cls("A", [_prop("x", minValue="10.0")])]),
    )

    assert _kinds(diff) == []


# ---------------------------------------------------------------- 枚举集顺序


def test_枚举集仅顺序不同不是破坏性变更():
    """代码审查 MEDIUM-6：集合完全相同、只是顺序不同 ⇒ 顺序无语义 ⇒ 外观级。"""

    diff = diffManifests(
        _manifest([_cls("A", [_prop("x", allowedValues=["A", "B"])])]),
        _manifest([_cls("A", [_prop("x", allowedValues=["B", "A"])])]),
    )

    assert _find(diff, "PROPERTY_ALLOWED_VALUES_CHANGED").severity == "COSMETIC"


# ---------------------------------------------------------------- 字段级覆盖度
#
# 代码审查 MEDIUM-5：`covered_sections` 只到分区级，而实际比较的是三张**手写**
# 字段表 ⇒ 将来 manifest 契约新增字段而引擎漏比时，披露与事实不符。


def test_引擎比较的字段与manifest契约双向一致():
    """引擎的字段表必须与 manifest 契约**双向**一致（多一个少一个都红）。

    少比 ⇒ 漏判破坏性变更；多比 ⇒ 比了契约里不存在的字段（永远不相等 ⇒ 假变更）。
    """

    from app.domain.semantic_manifest_schema import (
        _CLASS_SCHEMA,
        _JOIN_SCHEMA,
        _PROPERTY_SCHEMA,
    )
    from app.services.semantic_diff_service import (
        _CLASS_FIELDS,
        _JOIN_FIELDS,
        _PROPERTY_FIELDS,
    )

    cases = [
        ("classes", _CLASS_FIELDS, _CLASS_SCHEMA, {"className", "properties"}),
        ("properties", _PROPERTY_FIELDS, _PROPERTY_SCHEMA, {"propertyName"}),
        ("joins", _JOIN_FIELDS, _JOIN_SCHEMA, {"joinKey"}),
    ]

    for label, fields, schema, excluded in cases:
        engineFields = {field for field, _kind, _severity in fields}
        contractFields = set(schema["properties"]) - excluded

        assert engineFields == contractFields, (
            f"{label} 分区：引擎字段与 manifest 契约不一致；"
            f"仅引擎有={sorted(engineFields - contractFields)}，"
            f"仅契约有={sorted(contractFields - engineFields)}"
        )


def test_差异披露字段级覆盖():
    """覆盖度必须披露到**字段级** ⇒ 消费方能判断「哪些字段比过了」。"""

    diff = diffManifests(_manifest(), _manifest())

    assert set(diff.coveredFields) == {
        "classes",
        "properties",
        "joins",
        "sourceBindings",
    }
    assert "dataType" in diff.coveredFields["properties"]
    assert "sourceTable" in diff.coveredFields["classes"]
    assert "joinKey" not in diff.coveredFields["joins"]
    # 绑定分区（M1-1.3 批次 B）：过滤表达式与逐属性映射都要披露
    assert "filterExpression" in diff.coveredFields["sourceBindings"]
    assert "properties[].sourceColumn" in diff.coveredFields["sourceBindings"]


# ---------------------------------------------------------------- 响应选取


def _change(kind: str, severity: str, target: str) -> SemanticChange:
    return SemanticChange(
        kind=kind,
        severity=severity,
        section="classes",
        target=target,
    )


class TestSelectChangesForResponse:
    """截断时**优先保留破坏性变更**（MEDIUM-2）。

    若按原序截断，破坏性变更排在后面时会被丢掉 ⇒ 调用方看到
    「变更列表里没有破坏性变更」而 `hasBreakingChanges=true` ⇒ 误判。
    """

    def test_破坏性变更排在后面也不会被截掉(self):
        changes = [
            _change("CLASS_ADDED", "ADDITIVE", f"A{i}") for i in range(5)
        ] + [_change("CLASS_REMOVED", "BREAKING", "被删的类")]

        selected, dropped = selectChangesForResponse(changes, limit=3)

        # 破坏性变更排第一（其余按原序补满上限），且没有破坏性变更被丢弃。
        assert [change.target for change in selected] == ["被删的类", "A0", "A1"]
        assert dropped == 0

    def test_破坏性变更超出上限时如实报告丢弃条数(self):
        changes = [_change("CLASS_REMOVED", "BREAKING", f"B{i}") for i in range(5)]

        selected, dropped = selectChangesForResponse(changes, limit=2)

        assert len(selected) == 2
        assert dropped == 3

    def test_上限为零时返回空并报告全部破坏性条数(self):
        changes = [
            _change("CLASS_REMOVED", "BREAKING", "B1"),
            _change("CLASS_ADDED", "ADDITIVE", "A1"),
        ]

        selected, dropped = selectChangesForResponse(changes, limit=0)

        assert selected == ()
        assert dropped == 1

    def test_未截断时保留全部变更(self):
        changes = [
            _change("CLASS_ADDED", "ADDITIVE", "A1"),
            _change("CLASS_REMOVED", "BREAKING", "B1"),
        ]

        selected, dropped = selectChangesForResponse(changes, limit=10)

        assert len(selected) == 2
        assert dropped == 0
        assert {change.target for change in selected} == {"A1", "B1"}


# ---------------------------------------------------------------- 源绑定层（v2 / M1-1.3 批次 B）


def _bindingProp(name: str, **overrides) -> dict:
    prop = {
        "propertyName": name,
        "sourceColumn": name.lower(),
        "keyOrdinal": None,
        "transformExpression": None,
    }
    prop.update(overrides)
    return prop


def _binding(className: str = "A", **overrides) -> dict:
    binding = {
        "className": className,
        "datasourceName": "THBI",
        "schemaName": "ERP",
        "sourceTable": "T_A",
        "bindingRole": "BASE",
        "filterExpression": None,
        "filterAllowlist": [],
        "properties": [],
    }
    binding.update(overrides)
    return binding


def _manifestWithBindings(bindings: list[dict]) -> dict:
    manifest = _manifest()
    manifest["sourceBindings"] = bindings
    return manifest


def test_新增源绑定判为增量():
    diff = diffManifests(_manifest(), _manifestWithBindings([_binding()]))

    change = _find(diff, "BINDING_ADDED")
    assert change.severity == "ADDITIVE"
    assert change.section == "sourceBindings"


def test_删除源绑定判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding()]), _manifest()
    )

    change = _find(diff, "BINDING_REMOVED")
    assert change.severity == "BREAKING"


def test_改过滤表达式判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding()]),
        _manifestWithBindings([_binding(filterExpression="STATUS = 'A'")]),
    )

    change = _find(diff, "BINDING_FILTER_EXPRESSION_CHANGED")
    assert change.severity == "BREAKING"


def test_改过滤白名单判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding()]),
        _manifestWithBindings([_binding(filterAllowlist=["STATUS"])]),
    )

    change = _find(diff, "BINDING_FILTER_ALLOWLIST_CHANGED")
    assert change.severity == "BREAKING"


def test_改属性源列判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding(properties=[_bindingProp("po_no")])]),
        _manifestWithBindings(
            [_binding(properties=[_bindingProp("po_no", sourceColumn="PO_NUMBER")])]
        ),
    )

    change = _find(diff, "BINDING_PROPERTY_SOURCE_COLUMN_CHANGED")
    assert change.severity == "BREAKING"


def test_改属性变换表达式判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding(properties=[_bindingProp("po_no")])]),
        _manifestWithBindings(
            [
                _binding(
                    properties=[
                        _bindingProp("po_no", transformExpression="UPPER(PO_NO)")
                    ]
                )
            ]
        ),
    )

    change = _find(diff, "BINDING_PROPERTY_TRANSFORM_CHANGED")
    assert change.severity == "BREAKING"


def test_改键序号判为外观性():
    diff = diffManifests(
        _manifestWithBindings(
            [_binding(properties=[_bindingProp("po_no", keyOrdinal=1)])]
        ),
        _manifestWithBindings(
            [_binding(properties=[_bindingProp("po_no", keyOrdinal=2)])]
        ),
    )

    change = _find(diff, "BINDING_PROPERTY_KEY_ORDINAL_CHANGED")
    assert change.severity == "COSMETIC"


def test_新增属性映射判为增量():
    diff = diffManifests(
        _manifestWithBindings([_binding(properties=[])]),
        _manifestWithBindings([_binding(properties=[_bindingProp("po_no")])]),
    )

    change = _find(diff, "BINDING_PROPERTY_ADDED")
    assert change.severity == "ADDITIVE"


def test_删除属性映射判为破坏性():
    diff = diffManifests(
        _manifestWithBindings([_binding(properties=[_bindingProp("po_no")])]),
        _manifestWithBindings([_binding(properties=[])]),
    )

    change = _find(diff, "BINDING_PROPERTY_REMOVED")
    assert change.severity == "BREAKING"


def test_同表不同数据源是两条独立绑定():
    """身份是五元组 ⇒ 同表两个数据源不能被挤成一条（否则变更被吞）。"""
    base = _manifestWithBindings([_binding(datasourceName="THBI")])
    candidate = _manifestWithBindings(
        [
            _binding(datasourceName="THBI"),
            _binding(datasourceName="ERP2"),
        ]
    )

    diff = diffManifests(base, candidate)

    assert len(_find_list(diff, "BINDING_ADDED")) == 1


def test_同表不同角色是两条独立绑定():
    base = _manifestWithBindings([_binding(bindingRole="BASE")])
    candidate = _manifestWithBindings(
        [
            _binding(bindingRole="BASE"),
            _binding(bindingRole="NUMERATOR"),
        ]
    )

    diff = diffManifests(base, candidate)

    assert len(_find_list(diff, "BINDING_ADDED")) == 1


def test_绑定差异不修改入参():
    base = _manifestWithBindings([_binding(properties=[_bindingProp("po_no")])])
    candidate = _manifestWithBindings(
        [_binding(properties=[_bindingProp("po_no", sourceColumn="X")])]
    )
    baseBefore = copy.deepcopy(base)
    candidateBefore = copy.deepcopy(candidate)

    diffManifests(base, candidate)

    assert base == baseBefore
    assert candidate == candidateBefore
