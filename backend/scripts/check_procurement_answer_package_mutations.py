#!/usr/bin/env python3
"""检查器的变异反证（mutation proof）。

用途：证明 `check_procurement_answer_package.py` **不是「永远绿」**。
做法：把产物复制到临时目录，先断言**原始副本必须全绿**，再逐个注入已知的错误变异，
要求检查器对**每一个**变异都非零退出，**报出的是产物层面的问题**（不是检查器自己崩了），
**且报出的问题命中该变异的预期关键词**（⇒ 不能靠一个无关的检查项「顺便变红」蒙混过关）。

为什么要有「原始副本必须全绿」这一步：如果原始产物本身就不一致，那么「每个变异都让检查器变红」
就毫无意义 —— 红的原因是本来就红。所以先把「绿」钉死，再谈「会不会变红」。

为什么要求「不是检查器异常」：检查器崩了（traceback / `检查器异常`）也算非零退出，
若把崩溃当成「抓到缺陷」，那么一个写法错误（比如 JSON 解析炸了）会被误判成「检查器很严」，
于是真正漏网的变异反被掩盖。⇒ 缺陷型变异只接受**产物层面**的失败信息。

为什么要求「命中预期关键词」：只判「非零退出」的话，一个变异被**别的**检查项顺手抓出来也算过
（假绿的一种）。⇒ 每条变异都必须指名它期望被抓的那条信息。

变异清单覆盖（v4 · 16 项断言逐项至少一条）：
- HIGH-1  源绑定清单里塞一条重复 classId（计数不变、类并集变小）
- HIGH-2a §9.1 表里把一个 KPI 码写错（行数不变、覆盖变窄）
- HIGH-2b §9.1 表里复制另一行（行数变多、覆盖变窄）
- HIGH-3  冲突表里复制一行 C（编号集合不变、行数变多）
- HIGH-4  §9.1 表里把 time_column 的取值换成别的列名（字形仍对、取值必须等值）
- HIGH-5  JSON 的 `summary.slotCounts` 分项被改（总数对、分项错）
- MED-1  登记表里把一条空白的处置从 ∅ 改成 ✅
- MED-2  §9.1 统计行里的一个数字改成别的
- MED-3  分卷标题的题数写错（文档自相矛盾）
- MED-4  §9.1 某一行被截断（少几列）
- MED-5  分节标题的题数写错
- MED-6  G 系列编号越界
- MED-7  R 系列编号越界
- MED-8  Q 系列编号越界
- MED-9  README 摘要数字被改
- MED-10 统计块「触发回问」数被改（P0/P1 拆分与冲突文档对不上）
- V4-1   JSON 的 metricId 与 DB 快照对不上
- V4-2   DB 快照的 metric_id 被改
- V4-3   §9.1 表头列序被换（时间粒 ↔ 时间列）
- V4-4   §9.1 行序被换（内容对、顺序错）
- V4-5   §9.1 图例少一个字形
- V4-6   §2 值域被收窄（AVG 出域）
- V4-7   取值越出 §2 值域（metric_type=RATIOX）
- V4-8   RATIO 的 aggregation_order 被填成 NONE
- V4-9   MEASURE 的 denominator 非空（违反「必须为空」）
- V4-10  confirmed 槽位取值为空
- V4-11  槽位 answers 回指不存在的题号
- V4-12  README §2.2 的 KPI 状态与 JSON 三分桶不符
- V4-13  meta.evidence 指向不存在的文件
- V4-14  落点引用了不存在的登记编号
- V4-15  ∅ 行的落点没有 G 系列引用
- V4-16  补充注记槽形态被改（list → dict）
- V4-17  publishable/blocked 三分桶有重复 KPI
- CRASH-1 产物文件缺失（检查器要报「产物缺失」，不能崩也不能绿）
- CRASH-2 产物 JSON 损坏（检查器要报「产物损坏」，不能崩也不能绿）
- CRASH-3 DB 快照缺失（检查器要报「产物缺失」）

用法::

    python3 backend/scripts/check_procurement_answer_package_mutations.py

退出码：0 = 全部变异都被捕获；1 = 有变异漏网（说明检查器有假绿）；2 = 反证脚本自身出错。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "backend/scripts/check_procurement_answer_package.py"

#: 沙箱里要一并复制的「被检查产物」—— 清单由解析层给出（单一事实来源）。
LIB_PATH = REPO_ROOT / "backend/scripts/check_procurement_answer_package_lib.py"


def _loadLib():
    spec = importlib.util.spec_from_file_location(
        "check_procurement_answer_package_lib", LIB_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _loadLib()

#: 产物相对仓库根的路径（检查器只读这几个）+ 检查器自身脚本（README §1 会链接到它们）。
ARTIFACTS = tuple(lib.REQUIRED_ARTIFACTS) + (
    "backend/scripts/check_procurement_answer_package.py",
    "backend/scripts/check_procurement_answer_package_lib.py",
    "backend/scripts/check_procurement_answer_package_checks_a.py",
    "backend/scripts/check_procurement_answer_package_checks_b.py",
    "backend/scripts/check_procurement_answer_package_mutations.py",
)


def _sandbox(stack: Path) -> Path:
    for relative in ARTIFACTS:
        target = stack / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)
    return stack


def _digest(stack: Path) -> str:
    """把沙箱内全部产物的内容摘要拼起来（用于判断变异是否真的落地）。"""
    sha = hashlib.sha256()
    for relative in ARTIFACTS:
        target = stack / relative
        if target.exists():
            sha.update(target.read_bytes())
        else:
            sha.update(b"<deleted>")
    return sha.hexdigest()


def _replaceOnce(path: Path, old: str, new: str) -> bool:
    """替换一次；没找到就返回 False（⇒ 变异没落地，不能算「抓到缺陷」）。"""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def _editJson(path: Path, mutate) -> bool:
    """读 JSON → 改 → 写回（格式化成 indent=2 无妨：检查器只读语义）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if mutate(data) is False:
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _section91Lines(lines: list[str]) -> tuple[int, int] | None:
    """返回 §9.1 小节在行列表里的 [start, end) 区间。

    ⚠️ 模板里 §3 也有同形的 KPI 行（回收前基线快照），变异必须只落在 §9.1，
    否则改的是「已被 §9 修订的历史快照」，检查器当然不会报。
    """
    start = next((i for i, line in enumerate(lines) if line.startswith("### 9.1")), None)
    if start is None:
        return None
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start=start + 1) if line.startswith("### 9.")),
        len(lines),
    )
    return start, end


def _mutateInSection91(path: Path, mutate) -> bool:
    """只对 §9.1 小节应用一次行级变异。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    span = _section91Lines(lines)
    if span is None:
        return False
    start, end = span
    if not mutate(lines, start, end):
        return False
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _templatePath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/M0.5-KPI口径槽位模板.md"


def _slotsPath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/answers/kpi-slots-filled.json"


def _registryPath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/answers/answers-registry.md"


def _readmePath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/answers/README.md"


def _conflictsPath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/answers/conflicts-and-degradations.md"


def _snapshotPath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/evidence/kpi-slot-snapshot.txt"


def _candidatesPath(stack: Path) -> Path:
    return stack / "docs/update/1118/M0/procurement-source-binding-candidates.json"


# --- 既有变异 ---


def _mutateDuplicateBinding(stack: Path) -> bool:
    def mutate(data: dict) -> bool:
        data["bindings"][1] = json.loads(json.dumps(data["bindings"][0]))
        return True

    return _editJson(_candidatesPath(stack), mutate)


def _mutateTypoInKpiCode(stack: Path) -> bool:
    def mutate(lines: list[str], start: int, end: int) -> bool:
        for index in range(start, end):
            if lines[index].startswith("| `KPI_SUPPLIER_NCR_RATE`"):
                lines[index] = lines[index].replace(
                    "`KPI_SUPPLIER_NCR_RATE`", "`KPI_SUPPLIER_NCR_RATIO`", 1
                )
                return True
        return False

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateDuplicateSlotRow(stack: Path) -> bool:
    def mutate(lines: list[str], start: int, end: int) -> bool:
        for index in range(start, end):
            if lines[index].startswith("| `KPI_SUPPLIER_OTD`"):
                lines.insert(index + 1, lines[index])
                return True
        return False

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateDuplicateConflictRow(stack: Path) -> bool:
    lines = _conflictsPath(stack).read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if line.startswith("| C-05 "):
            lines.insert(index + 1, line)
            _conflictsPath(stack).write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def _mutateTimeColumnValue(stack: Path) -> bool:
    def mutate(lines: list[str], start: int, end: int) -> bool:
        for index in range(start, end):
            if lines[index].startswith("| `KPI_SUPPLIER_OTD`"):
                lines[index] = lines[index].replace(
                    "`PROMISED_RECEIPT_DATE`", "`TOTALLY_WRONG_COLUMN`", 1
                )
                return True
        return False

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateSlotCountsJson(stack: Path) -> bool:
    """JSON 自身的 `slotCounts` 分项被改（总数对、分项错 ⇒ 只比总数会漏）。"""
    def mutate(data: dict) -> bool:
        counts = data["summary"]["slotCounts"]
        counts["confirmed"] = counts.get("confirmed", 0) + 3
        return True

    return _editJson(_slotsPath(stack), mutate)


def _mutateDisposition(stack: Path) -> bool:
    return _replaceOnce(
        _registryPath(stack), "| C2.2 | （空白） | ∅ |", "| C2.2 | （空白） | ✅ |"
    )


def _mutateSummaryNumber(stack: Path) -> bool:
    return _replaceOnce(_templatePath(stack), "`confirmed` 77", "`confirmed` 96")


def _mutateVolumeHeading(stack: Path) -> bool:
    return _replaceOnce(_registryPath(stack), "（40 题）", "（39 题）")


def _mutateTruncatedRow(stack: Path) -> bool:
    def mutate(lines: list[str], start: int, end: int) -> bool:
        for index in range(start, end):
            if lines[index].startswith("| `KPI_SUPPLIER_OTD`"):
                lines[index] = "| `KPI_SUPPLIER_OTD` | ● | ◑ | ● | ● | ● | ● | ⊘ | ● |"
                return True
        return False

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateSectionHeading(stack: Path) -> bool:
    return _replaceOnce(
        _registryPath(stack), "### A1 `KPI_SUPPLIER_OTD`（6 题）", "### A1 `KPI_SUPPLIER_OTD`（7 题）"
    )


def _mutateIdSeries(stack: Path, prefix: str, bad: str) -> bool:
    """把某系列的编号改成一个越界号（如 `G-02` → `G-99`）。

    ⚠️ 缺口登记（G 系列）的编号是**加粗**的（`| **G-01** …`），C/R 系列不是 —— 两种形态都要能改。
    """
    path = _conflictsPath(stack)
    lines = path.read_text(encoding="utf-8").split("\n")
    pattern = re.compile(rf"^\|\s*\**{re.escape(prefix)}-?\d{{2,3}}\**\s")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = re.sub(
                rf"^\|\s*\**{re.escape(prefix)}-?\d{{2,3}}\**",
                f"| **{prefix}-{bad}**" if "**" in line.split("|")[1] else f"| {prefix}-{bad}",
                line,
            )
            path.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def _mutateQSeries(stack: Path) -> bool:
    path = _conflictsPath(stack)
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if re.match(r"^\|\s*Q\d{1,3}\s", line):
            lines[index] = re.sub(r"^\|\s*Q\d{1,3}", "| Q99", line)
            path.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def _mutateReadmeNumber(stack: Path) -> bool:
    return _replaceOnce(_readmePath(stack), "机器可读 | 156 槽位", "机器可读 | 158 槽位")


def _mutateReaskSplit(stack: Path) -> bool:
    return _replaceOnce(
        _registryPath(stack), "**18 项**（P0 8 项 + P1 10 项）", "**19 项**（P0 8 项 + P1 11 项）"
    )


# --- v4 新增变异（逐项断言） ---


def _mutateMetricIdJson(stack: Path) -> bool:
    """JSON 的 metricId 与 DB 快照对不上。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_OTD":
                kpi["metricId"] = 99
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateSnapshotMetricId(stack: Path) -> bool:
    """DB 快照里的 metric_id 被改（JSON 不动 ⇒ 对不上）。"""
    return _replaceOnce(_snapshotPath(stack), "KPI_SUPPLIER_OTD | 2 |", "KPI_SUPPLIER_OTD | 22 |")


def _mutateHeaderColumnOrder(stack: Path) -> bool:
    """§9.1 表头两列对调（数据列不动 ⇒ 只有「列序」检查能抓到）。"""
    def mutate(lines: list[str], start: int, end: int) -> bool:
        for index in range(start, end):
            if line_cells(lines[index]) and line_cells(lines[index])[0] == "KPI":
                lines[index] = lines[index].replace(
                    "| 时间粒 | 时间列 |", "| 时间列 | 时间粒 |", 1
                )
                return True
        return False

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateRowOrder(stack: Path) -> bool:
    """§9.1 两行对调（每行内容自洽 ⇒ 只有「行序」检查能抓到）。"""
    def mutate(lines: list[str], start: int, end: int) -> bool:
        first = next(
            (i for i in range(start, end) if lines[i].startswith("| `KPI_SUPPLIER_OTD`")), None
        )
        second = next(
            (
                i
                for i in range(start, end)
                if lines[i].startswith("| `KPI_SUPPLIER_OVERDUE_RATIO`")
            ),
            None,
        )
        if first is None or second is None:
            return False
        lines[first], lines[second] = lines[second], lines[first]
        return True

    return _mutateInSection91(_templatePath(stack), mutate)


def _mutateLegendGlyph(stack: Path) -> bool:
    """§9.1 图例少一个 `◑`（状态词还在 ⇒ 只有「图例字形完备」能抓到）。"""
    path = _templatePath(stack)
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if line.startswith("字形 ↔ JSON 状态词映射"):
            lines[index] = line.replace("`◑`", "", 1)
            path.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def _mutateNarrowDomain(stack: Path) -> bool:
    """§2 的 agg_function 值域被收窄（去掉 AVG）⇒ 现有取值越域。"""
    path = _templatePath(stack)
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if line.startswith("| 4 | `agg_function`"):
            lines[index] = line.replace(
                "`SUM` / `COUNT` / `AVG` / `COUNT_DISTINCT`",
                "`SUM` / `COUNT` / `COUNT_DISTINCT`",
                1,
            )
            path.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def _mutateValueOutOfDomain(stack: Path) -> bool:
    """metric_type 取一个 §2 值域外的值。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_OTD":
                kpi["slots"]["metric_type"]["value"] = "RATIOX"
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateRatioAggregationOrder(stack: Path) -> bool:
    """RATIO 的 aggregation_order 被填成 NONE（违反 §2 的「必须是前两者之一」）。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_OTD":
                kpi["slots"]["aggregation_order"]["value"] = "NONE"
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateMeasureDenominator(stack: Path) -> bool:
    """MEASURE 的 denominator_measure 非空（违反 §2 的「必须为空」）。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_DELAY_DAYS":
                kpi["slots"]["denominator_measure"] = {
                    "value": {"agg": "COUNT", "expr": "X"},
                    "status": "confirmed",
                    "answers": [],
                }
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateConfirmedNull(stack: Path) -> bool:
    """confirmed 槽位却取值为空（字形与语义自相矛盾）。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_OTD":
                kpi["slots"]["agg_function"]["value"] = None
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateAnswerRef(stack: Path) -> bool:
    """槽位 answers 回指一个不存在的题号。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_SUPPLIER_OTD":
                kpi["slots"]["time_column"]["answers"] = ["A99.9"]
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateReadmeKpiStatus(stack: Path) -> bool:
    """README §2.2 的 KPI 状态与 JSON 三分桶不符。"""
    return _replaceOnce(
        _readmePath(stack),
        "| `KPI_SUPPLIER_OTD` | ✅ 可算 |",
        "| `KPI_SUPPLIER_OTD` | ⚠ 待回问 |",
    )


def _mutateMetaEvidence(stack: Path) -> bool:
    """meta.evidence 指向一个不存在的文件。"""
    def mutate(data: dict) -> bool:
        data["meta"]["evidence"].append("docs/update/1118/M0/evidence/does-not-exist.txt")
        return True

    return _editJson(_slotsPath(stack), mutate)


def _mutateLandingUnknownRef(stack: Path) -> bool:
    """落点引用了不存在的登记编号。"""
    return _replaceOnce(
        _registryPath(stack), "（两种读法，见 C-02）", "（两种读法，见 C-99）"
    )


def _mutateLandingDropGapRef(stack: Path) -> bool:
    """∅ 行的落点没有 G 系列引用（处置与落点不相容）。"""
    return _replaceOnce(
        _registryPath(stack), "| B1.10 | （空白） | ∅ | G-01 |", "| B1.10 | （空白） | ∅ | 无 |"
    )


def _mutateSupplementaryShape(stack: Path) -> bool:
    """补充注记槽形态被改（derivedMetrics: list → dict）。"""
    def mutate(data: dict) -> bool:
        for kpi in data["kpis"]:
            if kpi["kpiCode"] == "KPI_PURCHASE_CYCLE_TIME":
                kpi["slots"]["derivedMetrics"] = {"note": "不再是数组"}
                return True
        return False

    return _editJson(_slotsPath(stack), mutate)


def _mutateBucketOverlap(stack: Path) -> bool:
    """publishable/blocked 三分桶里同一个 KPI 出现两次。"""
    def mutate(data: dict) -> bool:
        data["summary"]["publishableNow"].append("KPI_SUPPLIER_OTD")
        return True

    return _editJson(_slotsPath(stack), mutate)


def _mutateMissingFile(stack: Path) -> bool:
    _slotsPath(stack).unlink()
    return True


def _mutateBrokenJson(stack: Path) -> bool:
    _slotsPath(stack).write_text("{ not json", encoding="utf-8")
    return True


def _mutateMissingSnapshot(stack: Path) -> bool:
    _snapshotPath(stack).unlink()
    return True


def line_cells(line: str) -> list[str]:
    """表格行 → 单元格（与解析层同一套切分规则）。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]


#: (变异名, 变异函数, 预期关键词) —— 关键词必须出现在**问题行**里。
MUTATIONS = (
    ("HIGH-1 源绑定重复 classId", _mutateDuplicateBinding, ("重复 classId",)),
    ("HIGH-2a §9.1 KPI 码写错", _mutateTypoInKpiCode, ("JSON 中不存在的 KPI",)),
    ("HIGH-2b §9.1 重复行", _mutateDuplicateSlotRow, ("重复行",)),
    ("HIGH-3 冲突表重复一行 C", _mutateDuplicateConflictRow, ("登记行",)),
    ("HIGH-4 §9.1 time_column 取值被换", _mutateTimeColumnValue, ("≠ JSON 值",)),
    ("HIGH-5 JSON slotCounts 分项被改", _mutateSlotCountsJson, ("slotCounts.confirmed",)),
    ("MED-1 登记处置 ∅ 改 ✅", _mutateDisposition, ("处置",)),
    ("MED-2 §9.1 统计行数字被改", _mutateSummaryNumber, ("统计行",)),
    ("MED-3 分卷标题题数写错", _mutateVolumeHeading, ("卷标题声明",)),
    ("MED-4 §9.1 表行被截断", _mutateTruncatedRow, ("槽位格",)),
    ("MED-5 分节标题题数写错", _mutateSectionHeading, ("标题声明",)),
    ("MED-6 G 系列编号越界", lambda stack: _mutateIdSeries(stack, "G", "99"), ("G 系列",)),
    ("MED-7 R 系列编号越界", lambda stack: _mutateIdSeries(stack, "R", "99"), ("R 系列",)),
    ("MED-8 Q 系列编号越界", _mutateQSeries, ("Q 系列",)),
    ("MED-9 README 摘要数字被改", _mutateReadmeNumber, ("README 摘要",)),
    ("MED-10 统计块回问数被改", _mutateReaskSplit, ("触发回问",)),
    ("V4-1 JSON metricId 与快照不符", _mutateMetricIdJson, ("metricId",)),
    ("V4-2 快照 metric_id 被改", _mutateSnapshotMetricId, ("metricId",)),
    ("V4-3 §9.1 表头列序被换", _mutateHeaderColumnOrder, ("表头列序",)),
    ("V4-4 §9.1 行序被换", _mutateRowOrder, ("行序",)),
    ("V4-5 §9.1 图例少一个字形", _mutateLegendGlyph, ("图例缺少字形",)),
    ("V4-6 §2 值域被收窄", _mutateNarrowDomain, ("不在 §2 值域",)),
    ("V4-7 取值越出 §2 值域", _mutateValueOutOfDomain, ("不在 §2 值域",)),
    ("V4-8 RATIO 的聚合阶填成 NONE", _mutateRatioAggregationOrder, ("aggregation_order",)),
    ("V4-9 MEASURE 的分母非空", _mutateMeasureDenominator, ("必须为空",)),
    ("V4-10 confirmed 槽位取值为空", _mutateConfirmedNull, ("confirmed 但取值为空",)),
    ("V4-11 answers 回指不存在的题号", _mutateAnswerRef, ("回指题号",)),
    ("V4-12 README 逐 KPI 状态不符", _mutateReadmeKpiStatus, ("README §2.2 状态",)),
    ("V4-13 meta 引用不存在的文件", _mutateMetaEvidence, ("meta 引用的文件不存在",)),
    ("V4-14 落点引用不存在的编号", _mutateLandingUnknownRef, ("不存在的编号",)),
    ("V4-15 ∅ 行落点丢了 G 引用", _mutateLandingDropGapRef, ("应引用",)),
    ("V4-16 补充注记槽形态被改", _mutateSupplementaryShape, ("形态",)),
    ("V4-17 三分桶有重复 KPI", _mutateBucketOverlap, ("重复 KPI",)),
    ("CRASH-1 产物文件缺失", _mutateMissingFile, ("产物缺失",)),
    ("CRASH-2 产物 JSON 损坏", _mutateBrokenJson, ("产物损坏",)),
    ("CRASH-3 DB 快照缺失", _mutateMissingSnapshot, ("产物缺失",)),
)


def _runChecker(stack: Path) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--repo-root", str(stack)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def _toolCrashed(output: str) -> bool:
    return any(line.strip().startswith("- 检查器异常") for line in output.split("\n"))


def _problemText(output: str) -> str:
    """只看**问题行**（`- ` 开头），不看检查项标题 —— 否则关键词会误命中标题。"""
    return "\n".join(
        line.strip()[2:] for line in output.split("\n") if line.strip().startswith("- ")
    )


def _firstProblem(output: str) -> str:
    return next((line.strip() for line in output.split("\n") if line.strip().startswith("- ")), "")


def _baseline() -> int:
    """先钉死「原始副本是绿的」，否则后面的「变红」没有意义。"""
    with tempfile.TemporaryDirectory() as tmp:
        stack = _sandbox(Path(tmp))
        code, output = _runChecker(stack)
    if code == 0:
        print("✅ 基线：原始产物全绿（变异才有可比性）")
        return 0
    print("❌ 基线：原始产物就不一致 ⇒ 后续变异判定无意义")
    print(output)
    return 1


def _runMutation(name: str, mutate, keywords: tuple[str, ...]) -> tuple[bool, str]:
    """跑一个变异；返回（是否算抓到, 首个失败信息）。"""
    with tempfile.TemporaryDirectory() as tmp:
        stack = _sandbox(Path(tmp))
        before = _digest(stack)
        applied = mutate(stack)
        if applied is False:
            return False, "变异没有落地（脚本自身出错，不是检查器漏网）"
        if _digest(stack) == before:
            return False, "变异没有改变任何产物（脚本自身出错）"
        code, output = _runChecker(stack)
    if code == 0:
        return False, "检查器仍然全绿 ⇒ 存在假绿"
    if _toolCrashed(output):
        return False, "检查器自身异常（崩溃不算抓到缺陷）"
    problems = _problemText(output)
    missing = [keyword for keyword in keywords if keyword not in problems]
    if missing:
        return False, f"变红了但报的不是这条（缺关键词 {missing}）⇒ 靠别的检查项蒙混过关"
    return True, _firstProblem(output)


def main() -> int:
    if _baseline() != 0:
        return 2

    escaped: list[str] = []
    for name, mutate, keywords in MUTATIONS:
        caught, detail = _runMutation(name, mutate, keywords)
        print(f"{'✅' if caught else '❌'} {name} → {detail[:150]}")
        if not caught:
            escaped.append(f"{name}（{detail}）")

    if escaped:
        print(f"\n{len(escaped)} 个变异漏网 ⇒ 检查器存在假绿：")
        for item in escaped:
            print(f"   - {item}")
        return 1
    print(f"\n全部 {len(MUTATIONS)} 个变异都被捕获，检查器不是「永远绿」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
