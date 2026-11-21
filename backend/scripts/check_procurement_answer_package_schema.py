"""G1 问卷摄入包一致性检查器 —— **产物结构前检**（v5）。

为什么单独一层：**结构缺陷是产物层问题（退出码 1），不是检查器故障（退出码 2）**。

v4 的教训（code-reviewer 判为 MEDIUM-1）：槽位 JSON 少一个键（如 `time_column`）
⇒ 断言层在取值时抛 `KeyError` ⇒ 入口把它记成「检查器异常」⇒ **退出码 2**
⇒ 反证脚本按「崩溃 ≠ 抓到缺陷」的纪律判罚 ⇒ **归因反了**（产物坏了被说成工具坏了），
而且这类变异当时**没有对应条目**（覆盖缺口）。

本层把「断言层依赖的结构契约」显式化，任何一条不满足都报成**产物层问题**：

- `kpi-slots-filled.json`：顶层键 / `meta` 必需键 / `summary.slotCounts` 必需键 /
  每条 KPI 的必需键 / 每条 KPI 的 **13 个模型槽位** / 每个槽位对象含 `status`；
- `procurement-source-binding-candidates.json`：`bindings` / `counts` 必需键。

结构已坏 ⇒ 其余断言的结论只会是噪声 ⇒ 入口**不再执行**后续断言。

只依赖标准库（与检查器其余部分一致 ⇒ 生产机也能秒级复跑）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

#: `kpi-slots-filled.json` 顶层必需键。
REQUIRED_TOP_KEYS: tuple[str, ...] = ("meta", "kpis", "summary")

#: `meta` 必需键（少任何一条都会让断言层「空转」—— 见 checks_b 的 `checkMetaEvidence`）。
REQUIRED_META_KEYS: tuple[str, ...] = (
    "slotModel",
    "evidence",
    "statusLegend",
    "metricCount",
)

#: `summary.slotCounts` 必需键（5 个状态词 + 总数 + 口径说明）。
REQUIRED_SLOTCOUNT_KEYS: tuple[str, ...] = (
    "total",
    "confirmed",
    "conditional",
    "open",
    "blocked",
    "not_applicable",
    "scope",
)

#: 每条 KPI 的必需键。
REQUIRED_KPI_KEYS: tuple[str, ...] = ("kpiCode", "metricId", "name", "slots")

#: `procurement-source-binding-candidates.json` 必需键。
REQUIRED_CANDIDATES_KEYS: tuple[str, ...] = ("bindings", "counts")


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


def loadJson(path: Path) -> dict:
    """读 JSON（异常直接抛给入口 → 入口判退出码 2 = 工具/环境故障）。"""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _missing(container: object, keys: tuple[str, ...]) -> list[str]:
    """列出 `container` 里缺失的键（`container` 不是 dict ⇒ 整个都算缺失）。"""
    if not isinstance(container, dict):
        return list(keys)
    return [key for key in keys if key not in container]


def slotsStructureProblems(data: object) -> list[str]:
    """`kpi-slots-filled.json` 的结构契约。"""
    problems: list[str] = []
    for key in _missing(data, REQUIRED_TOP_KEYS):
        problems.append(f"kpi-slots-filled.json 缺少顶层键：{key}")
    if not isinstance(data, dict):
        return problems

    meta = data.get("meta")
    for key in _missing(meta, REQUIRED_META_KEYS):
        problems.append(f"kpi-slots-filled.json 的 meta 缺少键：{key}")
    if isinstance(meta, dict):
        evidence = meta.get("evidence")
        if isinstance(evidence, list) and not evidence:
            problems.append("kpi-slots-filled.json 的 meta.evidence 是空列表（证据清单必须显式声明）")

    summary = data.get("summary")
    if not isinstance(summary, dict):
        problems.append("kpi-slots-filled.json 的 summary 不是对象")
    else:
        for key in _missing(summary.get("slotCounts"), REQUIRED_SLOTCOUNT_KEYS):
            problems.append(f"summary.slotCounts 缺少键：{key}")

    kpis = data.get("kpis")
    if not isinstance(kpis, list):
        problems.append("kpi-slots-filled.json 的 kpis 不是数组")
        return problems

    for index, kpi in enumerate(kpis):
        label = f"kpis[{index}]"
        if isinstance(kpi, dict) and isinstance(kpi.get("kpiCode"), str):
            label = f"kpis[{index}]（{kpi['kpiCode']}）"
        for key in _missing(kpi, REQUIRED_KPI_KEYS):
            problems.append(f"{label} 缺少键：{key}")
        if not isinstance(kpi, dict):
            continue
        slots = kpi.get("slots")
        if not isinstance(slots, dict):
            problems.append(f"{label} 的 slots 不是对象")
            continue
        for slot in lib.MODEL_SLOTS:
            if slot not in slots:
                problems.append(f"{label} 缺少模型槽位：{slot}")
                continue
            if not isinstance(slots[slot], dict):
                problems.append(f"{label}.{slot} 不是对象")
                continue
            if "status" not in slots[slot]:
                problems.append(f"{label}.{slot} 缺少 status")
    return problems


def candidatesStructureProblems(data: object) -> list[str]:
    """`procurement-source-binding-candidates.json` 的结构契约。"""
    problems: list[str] = []
    for key in _missing(data, REQUIRED_CANDIDATES_KEYS):
        problems.append(f"procurement-source-binding-candidates.json 缺少键：{key}")
    if isinstance(data, dict):
        if not isinstance(data.get("bindings"), list):
            problems.append("candidates.bindings 不是数组")
        if not isinstance(data.get("counts"), dict):
            problems.append("candidates.counts 不是对象")
    return problems


def run(repoRoot: Path) -> list[str]:
    """跑结构前检，返回**产物层**问题清单（空 = 结构完好）。"""
    problems: list[str] = []
    problems.extend(
        slotsStructureProblems(loadJson(repoRoot / lib.SLOTS_FILE))
    )
    problems.extend(
        candidatesStructureProblems(loadJson(repoRoot / lib.CANDIDATES_FILE))
    )
    return problems
