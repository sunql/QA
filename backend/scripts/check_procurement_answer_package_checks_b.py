"""G1 问卷摄入包一致性检查器 —— 断言层 B（登记表与对外文档，第 9–16 项）。

设计纪律见断言层 A 的文件头（拆分只为满足单文件 < 800 行的硬上限）。
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

# --- 检查 9：源绑定候选（按类集合断言） ---


def checkCandidates(repoRoot: Path, pkg: Path) -> list[str]:
    """源绑定候选清单必须按**类集合**自洽：34 + 7 = 41 且互不重复、不重叠。"""
    data = lib.loadJson(pkg.parent / "procurement-source-binding-candidates.json")
    bindings = data["bindings"]
    revived = data.get("revivedTombstones", [])
    counts = data["counts"]

    bindingIds = [item["classId"] for item in bindings]
    revivedIds = [item["classId"] for item in revived]
    problems: list[str] = []

    if len(bindings) != lib.EXPECTED_BINDINGS:
        problems.append(f"bindings 条数 {len(bindings)} ≠ {lib.EXPECTED_BINDINGS}")
    if len(revived) != lib.EXPECTED_REVIVED:
        problems.append(f"revivedTombstones 条数 {len(revived)} ≠ {lib.EXPECTED_REVIVED}")
    for label, ids in (("bindings", bindingIds), ("revivedTombstones", revivedIds)):
        duplicates = sorted({i for i, count in Counter(ids).items() if count > 1})
        if duplicates:
            problems.append(f"{label} 存在重复 classId：{duplicates}")
    overlap = sorted(set(bindingIds) & set(revivedIds))
    if overlap:
        problems.append(f"bindings 与 revivedTombstones 类重叠：{overlap}")
    scope = len(set(bindingIds) | set(revivedIds))
    if scope != lib.EXPECTED_BINDING_SCOPE:
        problems.append(f"绑定类并集 {scope} ≠ 期望 {lib.EXPECTED_BINDING_SCOPE}")

    for key, expected in (
        ("bindings", lib.EXPECTED_BINDINGS),
        ("revivedTombstones", lib.EXPECTED_REVIVED),
        ("totalBindingScope", lib.EXPECTED_BINDING_SCOPE),
    ):
        if counts.get(key) != expected:
            problems.append(f"counts.{key}={counts.get(key)} ≠ 期望 {expected}")

    resolution = Counter(item.get("resolution") for item in bindings)
    for countsKey, resolutionKey in (("unique", "unique"), ("schemaAmbiguous", "schema-ambiguous")):
        if counts.get(countsKey) != resolution.get(resolutionKey):
            problems.append(
                f"counts.{countsKey}={counts.get(countsKey)} ≠ 按 resolution 实算 {resolution.get(resolutionKey)}"
            )
    if counts.get("unique", 0) + counts.get("schemaAmbiguous", 0) != lib.EXPECTED_BINDINGS:
        problems.append("counts.unique + counts.schemaAmbiguous ≠ 34")
    return problems


# --- 检查 10：问卷登记 ---


def checkRegistry(repoRoot: Path, pkg: Path) -> list[str]:
    """题号唯一；分卷/分节标题题数 = 实际行数（双向）；处置分布与统计块声明都对账。"""
    registry = lib.parseRegistry(lib.readText(pkg / "answers-registry.md"))
    problems: list[str] = []

    duplicates = sorted({key for key, count in Counter(registry.keys).items() if count > 1})
    if duplicates:
        problems.append(f"题号重复：{duplicates}")
    if len(set(registry.keys)) != lib.EXPECTED_QUESTION_TOTAL:
        problems.append(f"唯一题数 {len(set(registry.keys))} ≠ {lib.EXPECTED_QUESTION_TOTAL}")

    volumes = Counter(key.split("·")[0][0] for key in registry.keys)
    for volume, expected in lib.EXPECTED_QUESTION_VOLUMES.items():
        if volumes.get(volume) != expected:
            problems.append(f"{volume} 卷题数 {volumes.get(volume)} ≠ {expected}")
        declared = registry.volumeDeclared.get(volume)
        if declared != volumes.get(volume):
            problems.append(f"{volume} 卷标题声明 {declared} 题 ≠ 实际 {volumes.get(volume)} 题")

    problems.extend(_compareSections(registry))
    problems.extend(_compareDispositions(registry))

    blanks = sum(1 for answer in registry.answers if answer.startswith("（空白"))
    problems.extend(_compareStats(registry, len(set(registry.keys)), blanks))
    return problems


def _compareSections(registry: lib.Registry) -> list[str]:
    """分节题数**双向**对账：有行的节必须有声明，有声明的节必须对得上。

    只对「该卷确实用了 `###` 分节标题」的卷做双向检查 —— B/C/D 卷是单表列题，
    没有分节粒度，题号前缀（B1、C2）不是小节标题，不能拿它当「缺声明」报。
    """
    actual = lib.sectionCounts(registry.keys)
    declaredVolumes = {section[0] for section in registry.sectionDeclared}
    problems: list[str] = []
    for section, declared in registry.sectionDeclared.items():
        count = actual.get(section)
        if count is None:
            problems.append(f"§{section} 标题声明 {declared} 题，但没有对应题目行")
            continue
        if declared != count:
            problems.append(f"§{section} 标题声明 {declared} 题 ≠ 实际 {count} 题")
    for section in sorted(set(actual) - set(registry.sectionDeclared)):
        if section[0] in declaredVolumes:
            problems.append(f"§{section} 有 {actual[section]} 道题，但没有标题声明题数")
    return problems


def _compareDispositions(registry: lib.Registry) -> list[str]:
    distribution = Counter(registry.dispositions)
    problems: list[str] = []
    for disposition, expected in lib.EXPECTED_DISPOSITIONS.items():
        if distribution.get(disposition) != expected:
            problems.append(
                f"处置 {disposition!r} 实际 {distribution.get(disposition)} 条 ≠ 期望 {expected} 条"
            )
    unknown = sorted(set(distribution) - set(lib.EXPECTED_DISPOSITIONS))
    if unknown:
        problems.append(f"登记表出现未登记的处置标记：{unknown}")
    return problems


def _compareStats(registry: lib.Registry, total: int, blanks: int) -> list[str]:
    """统计块里声明的数字必须等于逐行重算的结果（含回问/降级数与 P0/P1 拆分）。"""
    expected = {
        "唯一题（按「卷 + 题号」）": total,
        "已答": total - blanks,
        "空白": blanks,
        "处置为 ✅ 采纳": lib.EXPECTED_DISPOSITIONS["✅"],
        "处置为 ⚠ 需回问": lib.EXPECTED_DISPOSITIONS["⚠"],
        "处置为 ⛔ 不可算 / 阻塞": lib.EXPECTED_DISPOSITIONS["⛔"],
        "处置为 ◐ 带条件": lib.EXPECTED_DISPOSITIONS["◐"],
        "处置为 ∅ 缺口": lib.EXPECTED_DISPOSITIONS["∅"],
        "落在 13 槽位之外（▸）": lib.EXPECTED_DISPOSITIONS["▸"],
        "触发回问": lib.EXPECTED_ID_COUNTS["Q"],
        "触发降级": lib.EXPECTED_ID_COUNTS["R"],
    }
    problems = []
    for label, want in expected.items():
        got = registry.stats.get(label)
        if got != want:
            problems.append(f"统计块「{label}」声明 {got} ≠ 重算 {want}")
    return problems


# --- 检查 11：回问分级拆分 ---


def checkReaskSplit(repoRoot: Path, pkg: Path) -> list[str]:
    """登记文档统计块里的「P0 N 项 + P1 M 项」必须等于冲突文档 §4 的实际行数。"""
    registry = lib.parseRegistry(lib.readText(pkg / "answers-registry.md"))
    raw = registry.statsRaw.get("触发回问", "")
    declared = {key: int(value) for key, value in re.findall(r"(P[01])\s*(\d+)\s*项", raw)}
    if not declared:
        return [f"统计块「触发回问」里读不出 P0/P1 拆分：{raw!r}"]

    conflicts = lib.sections(lib.readText(pkg / "conflicts-and-degradations.md"))
    actual = lib.priorityCounts(conflicts.get("4", ""))
    problems: list[str] = []
    for key, want in declared.items():
        got = actual.get(key)
        if got != want:
            problems.append(f"统计块「触发回问」声明 {key} {want} 项 ≠ 冲突文档 §4 实际 {got} 项")
    if sum(declared.values()) != lib.EXPECTED_ID_COUNTS["Q"]:
        problems.append(f"统计块「触发回问」P0+P1 = {sum(declared.values())} ≠ {lib.EXPECTED_ID_COUNTS['Q']}")
    return problems


# --- 检查 12：登记表编号连续性 ---


def checkIdContinuity(repoRoot: Path, pkg: Path) -> list[str]:
    """C / G / R / Q 四张登记表的编号必须连续、条数与对外承诺一致（字符串级）。"""
    text = lib.readText(pkg / "conflicts-and-degradations.md")
    sections = lib.sections(text)
    problems: list[str] = []
    for prefix, (sectionNo, _pattern) in lib.ID_ROW_PATTERNS.items():
        expected = lib.EXPECTED_ID_COUNTS[prefix]
        body = sections.get(sectionNo)
        if body is None:
            problems.append(f"{prefix} 系列：找不到 §{sectionNo} 小节")
            continue
        rows = lib.idRows(body, prefix)
        wanted = lib.expectedIds(prefix, expected)
        if len(rows) != expected:
            problems.append(f"{prefix} 系列登记行 {len(rows)} 条 ≠ 期望 {expected} 条")
        unexpected = sorted(set(rows) - set(wanted))
        if unexpected:
            problems.append(f"{prefix} 系列出现意外编号：{unexpected}")
        missing = sorted(set(wanted) - set(rows))
        if missing:
            problems.append(f"{prefix} 系列编号断号：缺 {missing}")
    return problems


# --- 检查 13：处置 ↔ 落点交叉引用 ---


def checkLandingRefs(repoRoot: Path, pkg: Path) -> list[str]:
    """登记表「落点」列引用的 C-/G-/R-/Q- 编号必须存在，且与处置类型相容。"""
    registry = lib.parseRegistry(lib.readText(pkg / "answers-registry.md"))
    conflicts = lib.sections(lib.readText(pkg / "conflicts-and-degradations.md"))
    existing: set[str] = set()
    for prefix, (sectionNo, _pattern) in lib.ID_ROW_PATTERNS.items():
        body = conflicts.get(sectionNo, "")
        for number in lib.idRows(body, prefix):
            existing.add(f"{prefix}-{number}" if prefix != "Q" else f"Q{number}")

    problems: list[str] = []
    for row in registry.rows:
        refs = lib.landingRefs(row.landing)
        unknown = sorted(refs - existing)
        if unknown:
            problems.append(f"{row.key}：落点引用了不存在的编号 {unknown}")
        required = lib.DISPOSITION_REQUIRED_SERIES.get(row.disposition, ())
        present = {lib.refSeries(ref) for ref in refs}
        missingSeries = sorted(set(required) - present)
        if missingSeries:
            problems.append(
                f"{row.key}：处置 {row.disposition!r} 应引用 {missingSeries} 系列编号，实际引用 {sorted(refs)}"
            )
        for series, allowed in lib.SERIES_ALLOWED_DISPOSITIONS.items():
            if series in present and row.disposition not in allowed:
                problems.append(
                    f"{row.key}：{series} 系列只该被 {allowed} 处置引用，实际处置 {row.disposition!r}"
                )
    return problems


# --- 检查 14：README 摘要数字 ---


def checkReadmeSummary(repoRoot: Path, pkg: Path) -> list[str]:
    """包首页复述的摘要数字必须与被检查的产物一致（期望值从产物解析，不写死）。"""
    readme = lib.readText(pkg / "README.md")
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    registry = lib.parseRegistry(lib.readText(pkg / "answers-registry.md"))
    conflicts = lib.sections(lib.readText(pkg / "conflicts-and-degradations.md"))
    candidates = lib.loadJson(pkg.parent / "procurement-source-binding-candidates.json")
    evidence = repoRoot / "docs/update/1118/M0/evidence/source-capability-scan.txt"
    evidenceLines = len(lib.readText(evidence).split("\n")) - 1

    idCounts = {
        prefix: len(lib.idRows(conflicts.get(sectionNo, ""), prefix))
        for prefix, (sectionNo, _p) in lib.ID_ROW_PATTERNS.items()
    }
    openBySlot = lib.openSlotsByName(model)
    noneCount = sum(
        1
        for code in model.order
        if model.entries(code)["execution_source"].value == "NONE"
    )
    expectations = (
        (r"本包把 (\d+) 条回答", (len(set(registry.keys)),)),
        (r"\| (\d+) 题逐题登记", (len(set(registry.keys)),)),
        (r"机器可读 \| (\d+) 槽位", (lib.EXPECTED_SLOT_TOTAL,)),
        (r"C-01…C-(\d+) 冲突", (idCounts["C"],)),
        (r"G-01…G-(\d+) 缺口", (idCounts["G"],)),
        (r"R-01…R-(\d+) 降级", (idCounts["R"],)),
        (r"Q1…Q(\d+) 回问", (idCounts["Q"],)),
        (r"绑定范围 \*\*(\d+) 类\*\*", (candidates["counts"]["totalBindingScope"],)),
        (r"原始输出（(\d+) 行）", (evidenceLines,)),
        (
            r"(\d+) 条现在就能落槽发布，(\d+) 条需要回问后落，(\d+) 条降级或摘除",
            (
                len(model.summary.get("publishableNow", [])),
                len(model.summary.get("publishableAfterReask", [])),
                len(model.summary.get("blocked", [])),
            ),
        ),
        (
            r"「(\d+) 冲突 \+ (\d+) 缺口 \+ (\d+) 降级 \+ (\d+) 回问」",
            (idCounts["C"], idCounts["G"], idCounts["R"], idCounts["Q"]),
        ),
        (r"`status: open` 的 \*\*(\d+)\*\* 个槽位", (sum(openBySlot.values()),)),
        (
            r"（`formula` (\d+) \+ `status` (\d+) \+ `metric_alias` (\d+)）",
            (openBySlot["formula"], openBySlot["status"], openBySlot["metric_alias"]),
        ),
        (r"`execution_source = NONE` 的 (\d+) 条指标", (noneCount,)),
    )

    problems: list[str] = []
    for pattern, expected in expectations:
        match = re.search(pattern, readme)
        if match is None:
            problems.append(f"README 里找不到摘要句式：{pattern}")
            continue
        got = tuple(int(value) for value in match.groups())
        if got != expected:
            problems.append(f"README 摘要 {got} ≠ 期望 {expected}（句式 {pattern}）")
    return problems


# --- 检查 15：README 逐 KPI 状态 ↔ JSON 三分桶 ---


def checkReadmeKpiStatus(repoRoot: Path, pkg: Path) -> list[str]:
    """README §2.2 的逐 KPI 状态必须与 JSON 的 `publishable*` / `blocked` 三分桶一致。"""
    readme = lib.readText(pkg / "README.md")
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    statuses = lib.readmeKpiStatuses(readme)
    problems: list[str] = []

    missing = sorted(set(model.order) - set(statuses))
    if missing:
        problems.append(f"README §2.2 缺少 KPI：{missing}")
    extra = sorted(set(statuses) - set(model.order))
    if extra:
        problems.append(f"README §2.2 有 JSON 没有的 KPI：{extra}")

    buckets = {
        "publishableNow": set(model.summary.get("publishableNow", [])),
        "publishableAfterReask": set(model.summary.get("publishableAfterReask", [])),
        "blocked": set(model.summary.get("blocked", [])),
    }
    for code, status in sorted(statuses.items()):
        expected = [
            bucket for glyph, bucket in lib.README_STATUS_BUCKET.items() if glyph == status
        ]
        if not expected:
            problems.append(f"{code}：README §2.2 状态 {status!r} 不在 ✅/⚠/❌ 词表内")
            continue
        actual = [name for name, items in buckets.items() if code in items]
        if set(actual) != set(expected):
            problems.append(
                f"{code}：README §2.2 状态 {status!r} ⇒ {expected}，但 JSON 在 {actual}"
            )
    return problems


# --- 检查 16：证据与文件清单存在性 ---


def checkMetaEvidence(repoRoot: Path, pkg: Path) -> list[str]:
    """JSON `meta.evidence` / `meta.slotModel` 与 README §1 文件清单里的路径必须真实存在。"""
    model = lib.parseSlotModel(lib.loadJson(pkg / "kpi-slots-filled.json"))
    readme = lib.readText(pkg / "README.md")
    problems: list[str] = []

    references = list(model.meta.get("evidence", []))
    slotModel = model.meta.get("slotModel")
    if slotModel:
        # 形如 `…/M0.5-KPI口径槽位模板.md §2（13 槽位）` ⇒ 只取路径部分。
        references.append(slotModel.split("§")[0].split("（")[0].strip())
    for reference in references:
        if not reference:
            continue
        target = repoRoot / reference
        if not target.exists():
            problems.append(f"meta 引用的文件不存在：{reference}")

    for target in lib.readmeFileList(readme):
        resolved = (pkg / target).resolve()
        if not resolved.exists():
            problems.append(f"README §1 文件清单里的路径不存在：{target}")
    return problems


#: 本模块负责的检查（顺序固定，入口会与另一段拼接）。
CHECKS: tuple[tuple[str, object], ...] = (
    ("源绑定候选计数", checkCandidates),
    ("问卷登记", checkRegistry),
    ("回问分级拆分", checkReaskSplit),
    ("登记表编号连续性", checkIdContinuity),
    ("处置↔落点交叉引用", checkLandingRefs),
    ("README 摘要数字", checkReadmeSummary),
    ("README 逐 KPI 状态", checkReadmeKpiStatus),
    ("证据与文件清单存在性", checkMetaEvidence),
)
