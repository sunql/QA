"""G1 问卷摄入包一致性检查器 —— 断言层 A（槽位模型与 §9.1 表格，第 1–8 项）。

每项检查返回**问题清单**（空列表 = 通过）；不抛异常、不打印、不退出 ——
入口 `check_procurement_answer_package.py` 负责汇总与退出码。

设计纪律（防止「检查器永远绿」）：

1. **按集合断言，不只按计数断言** —— 计数相同但条目重复/越界时必须报错。
2. **按等值断言，不按包含断言** —— ``RECEIPT_DATE`` 是 ``PROMISED_RECEIPT_DATE`` 的子串，
   包含式断言会让「日期列选错」这种致命错误溜过去。
3. **崩溃不等于通过** —— 缺文件、坏 JSON、表格少列都转成**可读的失败项**。
4. **期望值从被检查的产物里解析**（而不是只跟脚本常量比）⇒ 能抓到「文档自己跟自己矛盾」。
5. **不变式优先于计数** —— 「RATIO 必有分子分母」「confirmed 必有值」这类业务规则，
   比「第 N 个槽位等于某值」更能抓住真实错误。

本层只做「槽位 JSON ↔ §9.1 表格 ↔ DB 快照」三方对账；
登记表与对外文档的检查在断言层 B（`..._checks_b.py`）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path


def _loadLib():
    """从同目录加载解析层（绕开 `scripts/__init__.py` 的副作用依赖）。"""
    name = "check_procurement_answer_package_lib"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lib = _loadLib()



#: 槽位取值的形态（缺省 = 字符串；`None` 合法）。
SLOT_VALUE_KIND: dict[str, str] = {
    "numerator_measure": "dict",
    "denominator_measure": "dict",
    "grain_keys": "list",
}

#: README §2.2 状态字面量 ⇒ JSON 三分桶。
STATUS_BUCKET_PREFIX = ("✅", "⚠", "❌")


def _kindOf(value: object) -> str:
    if value is None:
        return "None"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _checkValueKind(code: str, slot: str, value: object) -> list[str]:
    expected = SLOT_VALUE_KIND.get(slot, "str")
    actual = _kindOf(value)
    if actual == "None":
        return []
    if actual != expected:
        return [f"{code}.{slot}：取值形态是 {actual}，应为 {expected}"]
    return []


# --- 检查 1：槽位计数 ---


def checkSlotCounts(repoRoot: Path, pkg: Path) -> list[str]:
    """`summary.slotCounts` 与逐槽位重算一致；三分桶把 12 条 KPI 划分干净；补充注记槽形态正确。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    statuses = lib.slotStatuses(model)
    declared = model.summary.get("slotCounts", {})
    known = set(model.statusLegend)
    problems: list[str] = []

    unknown = set(statuses) - known
    if unknown:
        problems.append(f"槽位出现未知状态词 {sorted(unknown)}（合法值 {sorted(known)}）")
    total = sum(statuses.values())
    if total != lib.EXPECTED_SLOT_TOTAL:
        problems.append(f"槽位总数 {total} ≠ 期望 {lib.EXPECTED_SLOT_TOTAL}")
    if declared.get("total") != total:
        problems.append(f"slotCounts.total={declared.get('total')} ≠ 重算 {total}")
    for status, actual in statuses.items():
        if declared.get(status) != actual:
            problems.append(f"slotCounts.{status}={declared.get(status)} ≠ 重算 {actual}")
    for status in sorted(known):
        if status not in statuses:
            problems.append(f"slotCounts 缺少状态项 {status}")
    extra = set(declared) - set(statuses) - {"total", "scope"}
    if extra:
        problems.append(f"slotCounts 出现未定义的状态项 {sorted(extra)}")

    problems.extend(_checkPublishSplit(model))
    problems.extend(_checkSupplementarySlots(model))
    return problems


def _checkPublishSplit(model: lib.SlotModel) -> list[str]:
    """12 条 KPI 必须被 `publishableNow` / `publishableAfterReask` / `blocked` 无重叠地划分完。"""
    codes = set(model.order)
    buckets = {
        "publishableNow": model.summary.get("publishableNow", []),
        "publishableAfterReask": model.summary.get("publishableAfterReask", []),
        "blocked": model.summary.get("blocked", []),
    }
    problems: list[str] = []
    for name, items in buckets.items():
        unknown = sorted(set(items) - codes)
        if unknown:
            problems.append(f"summary.{name} 含未知 KPI：{unknown}")
    flat = [code for items in buckets.values() for code in items]
    duplicates = sorted({code for code, count in Counter(flat).items() if count > 1})
    if duplicates:
        problems.append(f"publishable/blocked 三分里有重复 KPI：{duplicates}")
    missing = sorted(codes - set(flat))
    if missing:
        problems.append(f"publishable/blocked 三分漏掉的 KPI：{missing}")
    return problems


def _checkSupplementarySlots(model: lib.SlotModel) -> list[str]:
    """补充注记槽：形态与 `slotCounts.scope` 声明的「另有 6 项」都要对得上。"""
    problems: list[str] = []
    entries = lib.supplementaryEntries(model)
    if len(entries) != lib.EXPECTED_SUPPLEMENTARY_ENTRIES:
        problems.append(
            f"补充注记槽条目 {len(entries)} ≠ 声明的 {lib.EXPECTED_SUPPLEMENTARY_ENTRIES}"
        )
    for code, name, value in entries:
        expected = lib.SUPPLEMENTARY_SLOT_SHAPES.get(name)
        if expected is None:
            problems.append(f"{code}.{name}：未登记的补充注记槽（不在已知的 4 种形态里）")
            continue
        if _kindOf(value) != expected:
            problems.append(f"{code}.{name}：形态是 {_kindOf(value)}，应为 {expected}")
    return problems


# --- 检查 2：取值形态 + §2 值域 + 业务不变式 ---


def checkSlotValues(repoRoot: Path, pkg: Path) -> list[str]:
    """槽位取值必须在 §2 声明的值域内，且满足业务不变式（而不是只比计数）。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    template = lib.readText(repoRoot / lib.TEMPLATE_FILE)
    definitions = lib.parseSlotDefinitions(template)
    problems: list[str] = []

    missing = [name for name in lib.MODEL_SLOTS if name not in definitions]
    if missing:
        problems.append(f"模板 §2 缺少槽位定义：{missing}")
        return problems

    ratioOrders = _ratioOrders(definitions)
    for code in model.order:
        entries = model.entries(code)
        metricType = entries["metric_type"].value
        for name, entry in entries.items():
            problems.extend(_checkValueKind(code, name, entry.value))
            problems.extend(_checkDomain(code, name, entry, definitions[name]))
            problems.extend(_checkStatusValue(code, name, entry))
            problems.extend(_checkRequired(code, name, entry, definitions[name], metricType))
        problems.extend(_checkMetricInvariants(code, entries, metricType, ratioOrders))
    return problems


def _ratioOrders(definitions: dict[str, lib.SlotDefinition]) -> tuple[str, ...]:
    """§2 的 `aggregation_order` 值域里，「比率型」允许取的那些（= 值域去掉 `NONE`）。"""
    domain = definitions["aggregation_order"].domain
    return tuple(value for value in domain if value != "NONE")


def _checkDomain(code: str, name: str, entry: lib.SlotEntry, definition: lib.SlotDefinition) -> list[str]:
    """取值必须落在 §2 声明的值域内（没有声明值域的槽位不查）。"""
    if entry.value is None or not definition.domain:
        return []
    if entry.value not in definition.domain:
        return [
            f"{code}.{name}：取值 {entry.value!r} 不在 §2 值域 {list(definition.domain)} 内"
        ]
    return []


def _checkStatusValue(code: str, name: str, entry: lib.SlotEntry) -> list[str]:
    """`confirmed` ⇒ 必有值；`not_applicable` ⇒ 必无值（否则字形与语义会自相矛盾）。"""
    if entry.status == "confirmed" and entry.value is None:
        return [f"{code}.{name}：状态 confirmed 但取值为空"]
    if entry.status == "not_applicable" and entry.value is not None:
        return [f"{code}.{name}：状态 not_applicable 但取值非空 {entry.value!r}"]
    return []


def _checkRequired(
    code: str,
    name: str,
    entry: lib.SlotEntry,
    definition: lib.SlotDefinition,
    metricType: object,
) -> list[str]:
    """§2「必填条件」里写死的两条规则：**恒必填**（槽位必适用）与 **必须为空**。"""
    required = definition.required
    if "恒必填" in required and entry.status == "not_applicable":
        return [f"{code}.{name}：§2 声明「恒必填」，但状态是 not_applicable"]
    if "必须为空" in required and metricType == "MEASURE":
        if entry.value is not None:
            return [f"{code}.{name}：§2 声明 MEASURE 时「必须为空」，但取值非空"]
        if entry.status != "not_applicable":
            return [f"{code}.{name}：§2 声明 MEASURE 时「必须为空」，状态应为 not_applicable"]
    if "≥1 个" in required:
        if not isinstance(entry.value, list) or not entry.value:
            return [f"{code}.{name}：§2 声明「≥1 个」，但取值是 {_kindOf(entry.value)}"]
    return []


def _checkMetricInvariants(
    code: str,
    entries: dict[str, lib.SlotEntry],
    metricType: object,
    ratioOrders: tuple[str, ...],
) -> list[str]:
    """指标级不变式：类型 ⇔ 分子分母、类型 ⇔ 聚合阶、粒度恒非空。"""
    problems: list[str] = []
    numerator = entries["numerator_measure"]
    denominator = entries["denominator_measure"]
    aggregation = entries["aggregation_order"]

    if metricType == "MEASURE":
        if denominator.status != "not_applicable":
            problems.append(f"{code}：MEASURE 的 denominator_measure 应为 not_applicable")
    elif metricType == "RATIO":
        for label, entry in (("numerator_measure", numerator), ("denominator_measure", denominator)):
            if entry.status == "not_applicable":
                problems.append(f"{code}：RATIO 的 {label} 不应为 not_applicable")
        execution = entries["execution_source"]
        if execution.value != "NONE":
            for label, entry in (("numerator_measure", numerator), ("denominator_measure", denominator)):
                if entry.value is None:
                    problems.append(
                        f"{code}：RATIO 且 execution_source ≠ NONE，{label} 必非空"
                    )
        if aggregation.value not in ratioOrders:
            problems.append(
                f"{code}：RATIO 的 aggregation_order={aggregation.value!r} 不在 {list(ratioOrders)} 内"
            )
    else:
        if aggregation.value != "NONE":
            problems.append(f"{code}：非 RATIO 的 aggregation_order 应为 NONE，实际 {aggregation.value!r}")

    grain = entries["grain_keys"]
    if not isinstance(grain.value, list) or not grain.value:
        problems.append(f"{code}：grain_keys 恒非空，实际 {_kindOf(grain.value)}")
    return problems


# --- 检查 3：§9.1 表 vs JSON ---


def checkSlotTable(repoRoot: Path, pkg: Path) -> list[str]:
    """§9.1 表逐格字形与 JSON 一致，取值按等值比对，且行集合/行序/列数都对得上。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    template = lib.readText(repoRoot / lib.TEMPLATE_FILE)
    table = lib.parseSection91(template)
    problems: list[str] = []

    seen: list[str] = []
    for code, cells in table.rows:
        if code not in model.byCode:
            problems.append(f"§9.1 表出现 JSON 中不存在的 KPI：{code}")
            continue
        seen.append(code)
        if len(cells) != len(lib.MODEL_SLOTS):
            problems.append(
                f"{code}：表行有 {len(cells)} 个槽位格，应为 {len(lib.MODEL_SLOTS)}"
            )
            continue
        problems.extend(_compareRow(code, model.byCode[code], cells, set(model.statusLegend)))

    duplicates = sorted({code for code, count in Counter(seen).items() if count > 1})
    if duplicates:
        problems.append(f"§9.1 表出现重复行：{duplicates}")
    missing = sorted(set(model.order) - set(seen))
    if missing:
        problems.append(f"§9.1 表缺少 KPI：{missing}")
    if seen != list(model.order) and not missing and not duplicates:
        problems.append(f"§9.1 表行序与 JSON 的 KPI 顺序不一致：{seen}")
    return problems


def _compareRow(code: str, kpi: dict, cells: tuple[str, ...], known: set[str]) -> list[str]:
    """逐格比对一行：字形必须唯一且对得上；渲染取值的槽位还要**等值**比对取值文本。"""
    problems: list[str] = []
    slots = kpi["slots"]
    for index, slot in enumerate(lib.MODEL_SLOTS):
        cell = cells[index]
        entry = slots[slot]
        expected = lib.glyphFor(entry["status"], entry.get("value"), known)
        if expected is None:
            problems.append(f"{code}.{slot}：未知状态词 {entry['status']!r}")
            continue
        found = lib.GLYPH_PATTERN.findall(cell)
        if not found:
            problems.append(f"{code}.{slot}：表中找不到字形")
            continue
        if len(found) > 1:
            problems.append(f"{code}.{slot}：单元格里有多个字形 {found}")
            continue
        if found[0] != expected:
            problems.append(
                f"{code}.{slot}：表={found[0]} ≠ JSON={expected}（状态 {entry['status']}）"
            )
            continue
        if slot not in lib.VALUE_RENDERED_SLOTS:
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not value:
            continue
        problems.extend(_compareCellValue(code, slot, cell, value))
    return problems


def _compareCellValue(code: str, slot: str, cell: str, value: str) -> list[str]:
    """取值必须**等值**命中（包含式断言会让 `RECEIPT_DATE` 混过 `PROMISED_RECEIPT_DATE`）。"""
    tokens = lib.CODE_TOKEN_PATTERN.findall(cell)
    if len(tokens) > 1:
        return [f"{code}.{slot}：单元格里有多个取值 {tokens}，应为 {value!r}"]
    if tokens:
        if tokens[0] != value:
            return [f"{code}.{slot}：表里的取值 {tokens[0]!r} ≠ JSON 值 {value!r}"]
        return []
    bare = cell.replace("**", "").replace("`", "").strip()
    if bare != value:
        return [f"{code}.{slot}：表里的取值 {bare!r} ≠ JSON 值 {value!r}"]
    return []


# --- 检查 4：§9.1 表头 ---


def checkSection91Header(repoRoot: Path, pkg: Path) -> list[str]:
    """§9.1 表头必须是 14 列，且列序与 §2 的槽位顺序一致（列序错了 ⇒ 取值全错位）。"""
    template = lib.readText(repoRoot / lib.TEMPLATE_FILE)
    table = lib.parseSection91(template)
    problems: list[str] = []
    if not table.headers:
        return ["§9.1 找不到表头行"]
    if len(table.headers) != len(lib.SECTION91_HEADERS):
        problems.append(
            f"§9.1 表头 {len(table.headers)} 列，应为 {len(lib.SECTION91_HEADERS)} 列"
        )
        return problems
    if table.headers != lib.SECTION91_HEADERS:
        problems.append(f"§9.1 表头列序 {list(table.headers)} ≠ 期望 {list(lib.SECTION91_HEADERS)}")
    return problems


# --- 检查 5：§9.1 统计行 ---


def checkSlotTableSummary(repoRoot: Path, pkg: Path) -> list[str]:
    """§9.1 表下方的汇总数字必须能被逐槽位重算对上。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    template = lib.readText(repoRoot / lib.TEMPLATE_FILE)
    table = lib.parseSection91(template)
    statuses = lib.slotStatuses(model)
    glyphs = lib.slotGlyphs(model)

    problems: list[str] = []
    if not table.statsLine:
        return ["§9.1 找不到「统计：…」汇总行"]
    total = lib.firstInt(table.statsLine)
    if total != lib.EXPECTED_SLOT_TOTAL:
        problems.append(f"§9.1 统计行总数 {total} ≠ 期望 {lib.EXPECTED_SLOT_TOTAL}")

    blob = " ".join(
        line for line in table.statsLine.split("\n") + _legendStatsLines(template)
    )
    declaredStatus = {label: int(num) for label, num in re.findall(r"`(\w+)`\s*(\d+)", blob)}
    for status, actual in statuses.items():
        if declaredStatus.get(status) != actual:
            problems.append(f"§9.1 统计行 {status}={declaredStatus.get(status)} ≠ 重算 {actual}")
    declaredGlyph = {glyph: int(num) for glyph, num in re.findall(r"`([●◑⛔⊘◐○])`\s*(\d+)", blob)}
    for glyph, actual in glyphs.items():
        if declaredGlyph.get(glyph) != actual:
            problems.append(f"§9.1 统计行 {glyph}={declaredGlyph.get(glyph)} ≠ 重算 {actual}")
    return problems


def _legendStatsLines(template: str) -> list[str]:
    """§9.1 里「按字形 = …」那几行（统计信息可能跨行）。"""
    body = lib.section91(template)
    return [line for line in body.split("\n") if line.startswith("按字形")]


# --- 检查 6：图例完备 ---


def checkLegend(repoRoot: Path, pkg: Path) -> list[str]:
    """§9.1 图例必须把**每个状态词 ↔ 每个字形**映射齐全，且与 §9.1 表用到的字形一致。

    只判「字形出现过」不够 —— 图例正文里的一句散文也能提到字形
    （⇒ 删掉真正的映射行反而仍然「有该字形」）。⇒ 解析「字形 = 状态词」**映射对**。
    """
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    template = lib.readText(repoRoot / lib.TEMPLATE_FILE)
    table = lib.parseSection91(template)
    problems: list[str] = []

    mapping: dict[str, set[str]] = {}
    for first, second, status in lib.LEGEND_PAIR_PATTERN.findall(table.legend):
        mapping.setdefault(status, set()).add(first)
        if second:
            mapping.setdefault(status, set()).add(second)

    expected = {
        "confirmed": {"●"},
        "conditional": {"◑"},
        "blocked": {"⛔"},
        "not_applicable": {"⊘"},
        "open": {"◐", "○"},
    }
    for status in sorted(model.statusLegend):
        want = expected.get(status)
        got = mapping.get(status)
        if got is None:
            problems.append(f"§9.1 图例缺少状态词 {status!r} 的字形映射")
            continue
        if want is not None and got != want:
            problems.append(f"§9.1 图例 {status} 的字形是 {sorted(got)}，应为 {sorted(want)}")

    producible = set(lib.slotGlyphs(model))
    mapped = {glyph for glyphs in mapping.values() for glyph in glyphs}
    missing = sorted(producible - mapped)
    if missing:
        problems.append(f"§9.1 图例缺少字形 {missing}（JSON 里能产出这些字形）")
    return problems


# --- 检查 7：metricId ↔ kpiCode 对账 ---


def checkMetricIds(repoRoot: Path, pkg: Path) -> list[str]:
    """JSON 的 `metricId` 必须与 DB 快照 `evidence/kpi-slot-snapshot.txt` 的 `metric_id` 一一对应。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    snapshot = lib.parseSnapshot(lib.readText(repoRoot / lib.SNAPSHOT_FILE))
    problems: list[str] = []

    missing = sorted(set(model.order) - set(snapshot))
    if missing:
        problems.append(f"DB 快照里没有这些 KPI：{missing}")
    extra = sorted(set(snapshot) - set(model.order))
    if extra:
        problems.append(f"DB 快照里有 JSON 没有的 KPI：{extra}")

    for code in model.order:
        if code not in snapshot:
            continue
        declared = model.byCode[code].get("metricId")
        if declared != snapshot[code]:
            problems.append(f"{code}：JSON metricId={declared!r} ≠ DB 快照 metric_id={snapshot[code]!r}")

    ids = [model.byCode[code].get("metricId") for code in model.order]
    duplicates = sorted({value for value, count in Counter(ids).items() if count > 1})
    if duplicates:
        problems.append(f"多条 KPI 指向同一个 metricId：{duplicates}")
    return problems


# --- 检查 8：槽位 answers 回指 ---


def checkAnswerRefs(repoRoot: Path, pkg: Path) -> list[str]:
    """槽位 `answers` 里回指的题号必须真实存在于登记表（否则依据悬空）。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    registry = lib.parseRegistry(lib.readText(pkg / "answers-registry.md"))
    known = set(registry.keys)
    problems: list[str] = []
    for code in model.order:
        for name, entry in model.entries(code).items():
            for reference in entry.answers:
                if reference not in known:
                    problems.append(f"{code}.{name}：回指题号 {reference!r} 不在登记表里")
    return problems


#: 本模块负责的检查（顺序固定，入口会与另一段拼接）。
CHECKS: tuple[tuple[str, object], ...] = (
    ("槽位计数", checkSlotCounts),
    ("槽位取值与不变式", checkSlotValues),
    ("槽位表 vs JSON", checkSlotTable),
    ("§9.1 表头列序", checkSection91Header),
    ("§9.1 统计行", checkSlotTableSummary),
    ("§9.1 图例完备", checkLegend),
    ("metricId ↔ kpiCode 对账", checkMetricIds),
    ("槽位 answers 回指", checkAnswerRefs),
)
