"""G1 问卷摄入包一致性检查器 —— 解析层（只读、无第三方依赖）。

本模块只做两件事：**定义口径常量** 与 **把 Markdown / JSON 产物解析成结构化对象**。
断言在 `check_procurement_answer_package_checks.py`，入口在 `check_procurement_answer_package.py`。

为什么拆三个文件：v4 的 16 项断言 + 解析层超过 800 行（项目硬上限）⇒ 按仓库既有惯例
（`X.py` + `X_lib.py`，如 `kpi_metric_realdata.py` / `kpi_metric_realdata_lib.py`）拆开，
每个文件都保持「高内聚、低耦合」。

解析层的纪律：

1. **期望值从文档里解析**，不写死常量 —— 这样才能抓到「文档自己跟自己矛盾」。
2. **解析失败要能变成可读的失败项**，不许抛 traceback（缺文件 / 坏 JSON 由入口兜底）。
3. **一切集合都保留重复** —— `set()` 会把重复行折叠掉，看不见「同名两行」这类缺陷。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# --- 产物路径（相对仓库根） ---

PACKAGE_DIR = "docs/update/1118/M0/answers"
SLOTS_FILE = f"{PACKAGE_DIR}/kpi-slots-filled.json"
REGISTRY_FILE = f"{PACKAGE_DIR}/answers-registry.md"
CONFLICTS_FILE = f"{PACKAGE_DIR}/conflicts-and-degradations.md"
README_FILE = f"{PACKAGE_DIR}/README.md"
CANDIDATES_FILE = "docs/update/1118/M0/procurement-source-binding-candidates.json"
TEMPLATE_FILE = "docs/update/1118/M0/M0.5-KPI口径槽位模板.md"
SNAPSHOT_FILE = "docs/update/1118/M0/evidence/kpi-slot-snapshot.txt"

#: 检查器读取的全部产物（相对仓库根）—— **前检清单**。
#:
#: 为什么要有前检：缺文件 / 坏 JSON 属于**产物层问题**（要报出来，退出码 1），
#: 不能让检查器自己抛 traceback（退出码 2 = 「检查器崩了」）⇒ 否则反证脚本无法区分
#: 「产物坏了」与「检查器坏了」。
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    SLOTS_FILE,
    REGISTRY_FILE,
    CONFLICTS_FILE,
    README_FILE,
    "docs/update/1118/M0/answers/source-capability.md",
    CANDIDATES_FILE,
    TEMPLATE_FILE,
    SNAPSHOT_FILE,
    "docs/update/1118/M0/evidence/source-capability-scan.txt",
)

# --- 槽位模型（A7 §2） ---

#: 13 个模型槽位（顺序 = §9.1 表的列序，也是 `kpi-slots-filled.json` 的 `slots` 键序）。
MODEL_SLOTS: tuple[str, ...] = (
    "metric_type",
    "numerator_measure",
    "denominator_measure",
    "agg_function",
    "aggregation_order",
    "grain_keys",
    "filter_expression",
    "time_grain",
    "time_column",
    "execution_source",
    "formula",
    "status",
    "metric_alias",
)

#: §9.1 表把取值文本渲染进单元格的槽位（其余槽位只渲染字形）。
VALUE_RENDERED_SLOTS: tuple[str, ...] = ("time_column",)

#: §9.1 表头（KPI 列之后）的 13 个中文列名 —— 列序必须与 `MODEL_SLOTS` 一一对应。
SECTION91_HEADERS: tuple[str, ...] = (
    "类型",
    "分子",
    "分母",
    "聚合",
    "聚合阶",
    "粒度",
    "过滤",
    "时间粒",
    "时间列",
    "执行源",
    "公式",
    "状态",
    "别名",
)

#: 补充注记槽（非模型槽位）—— 形态各异，单独断言，不计入 156 的口径。
SUPPLEMENTARY_SLOT_SHAPES: dict[str, str] = {
    "derivedMetrics": "list",
    "coverageWarning": "dict",
    "dueDateNote": "dict",
    "restoreCondition": "str",
}

#: 期望的补充注记槽条目总数（JSON `slotCounts.scope` 里写的「另有 6 项」）。
EXPECTED_SUPPLEMENTARY_ENTRIES = 6

# --- 期望计数 ---

EXPECTED_QUESTION_TOTAL = 172
EXPECTED_QUESTION_VOLUMES = {"A": 76, "B": 27, "C": 40, "D": 29}
EXPECTED_SLOT_TOTAL = 156
EXPECTED_BINDINGS = 34
EXPECTED_REVIVED = 7
EXPECTED_BINDING_SCOPE = 41

#: 四张登记表编号的对外承诺条数（README 摘要「22 冲突 + 5 缺口 + 17 降级 + 18 回问」）。
EXPECTED_ID_COUNTS = {"C": 22, "G": 5, "R": 17, "Q": 18}

#: 处置标记的期望分布（`answers-registry.md` `## 统计` 块声明的同一组数字）。
EXPECTED_DISPOSITIONS = {
    "✅": 89,
    "⚠": 27,
    "∅": 20,
    "⛔": 16,
    "◐": 11,
    "▸": 8,
    "∅/⛔": 1,  # A·B6.4：兼属缺口与阻塞
}

# --- 字形 / 状态词 ---

#: 字形 ↔ JSON 状态词（§9.1 图例；`open` 再按有无占位值细分）。
STATUS_TO_GLYPH = {
    "confirmed": "●",
    "conditional": "◑",
    "blocked": "⛔",
    "not_applicable": "⊘",
}
GLYPH_PATTERN = re.compile(r"[●◑⛔⊘◐○]")
GLYPH_SET = frozenset("●◑⛔⊘◐○")
CODE_TOKEN_PATTERN = re.compile(r"`([^`]+)`")

#: `open` 之外的状态词与字形一一对应；`open` 按有无占位值分成 `◐` / `○`。
OPEN_PLACEHOLDER_GLYPH = "◐"
OPEN_EMPTY_GLYPH = "○"

#: 图例里的「字形 → 状态词」映射对：`` `●`=`confirmed` `` / `` `◐`/`○`=`open` ``。
#:
#: 必须**两侧都带反引号**才能算映射对 —— 图例正文里有「新增 `◑`= 有值但带条件」
#: 这种散文式说明，它提到字形但不是映射（否则删掉真正的映射行反而仍然「有该字形」）。
LEGEND_PAIR_PATTERN = re.compile(
    r"`([●◑⛔⊘◐○])`(?:\s*/\s*`([●◑⛔⊘◐○])`)?\s*=\s*`(\w+)`"
)

# --- 题号 / 编号 / 标题 ---

#: 题号：形如 `A1.6` / `C3.2b` / `A·B3.1`（A 卷内制度类题带 `卷·` 前缀以避开跨卷碰撞）。
#: 裸号必须带点（`A1` 之类的节号不是题号），否则会把别的表的编码误当成题。
QUESTION_KEY_PATTERN = re.compile(r"[A-D](?:·[A-Z]\d+(?:\.\d+)*[a-z]?|\d+(?:\.\d+)+[a-z]?)")

VOLUME_HEADING_PATTERN = re.compile(r"^## ([A-D]) · .*?（(\d+) 题）")
SECTION_CODE_PATTERN = re.compile(r"^### ([A-Z]\d+)\b")
SECTION_SPECIAL_PATTERN = re.compile(r"^### .*制度类题")
SECTION_COUNT_PATTERN = re.compile(r"（.*?(\d+) 题）")

#: 四张登记表的行首模式：编号后必须跟空白或行尾（否则 `C-05x` 会被当成 C-05）。
ID_ROW_PATTERNS = {
    "C": ("1", re.compile(r"^\|\s*\*{0,2}C-(\d{2,3})\*{0,2}(?:\s|$)")),
    "G": ("2", re.compile(r"^\|\s*\*{0,2}G-(\d{2,3})\*{0,2}(?:\s|$)")),
    "R": ("3", re.compile(r"^\|\s*\*{0,2}R-(\d{2,3})\*{0,2}(?:\s|$)")),
    "Q": ("4", re.compile(r"^\|\s*\*{0,2}Q(\d{1,3})\*{0,2}(?:\s|$)")),
}

#: 登记表「落点」列里引用的登记编号（C-/G-/R-/Q-）。
#:
#: 不能用 `\b`：Python 的 `\w` 含中文，「见C-02」这种「中文紧贴编号」的写法
#: 会被判成「无边界」而漏掉引用（⇒ 漏检）。改用**显式的 ASCII 字符类**看门。
LANDING_REF_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])([CGR])-(\d{2,3})(?![0-9-])"
    r"|(?<![A-Za-z0-9_-])Q(\d{1,2})(?![0-9])"
)

#: 处置 ⇒ 必须引用的编号系列（`∅` = 缺口 ⇒ 必须指向 G 系列缺口登记）。
DISPOSITION_REQUIRED_SERIES: dict[str, tuple[str, ...]] = {
    "∅": ("G",),
    "∅/⛔": ("G", "R"),
}

#: 只允许被哪些处置引用的编号系列（缺口系列只属「缺口」类处置）。
SERIES_ALLOWED_DISPOSITIONS: dict[str, tuple[str, ...]] = {
    "G": ("∅", "∅/⛔"),
}

#: README §2.2「逐 KPI 状态」列 ⇒ JSON 三分桶的哪一桶。
README_STATUS_BUCKET = {
    "✅": "publishableNow",
    "⚠": "publishableAfterReask",
    "❌": "blocked",
}


# --- 通用小工具 ---


def loadJson(path: Path) -> dict:
    """读 JSON（坏文件抛 `JSONDecodeError`，由入口转成失败项）。"""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def readText(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def preflight(repoRoot: Path) -> list[str]:
    """前检：产物是否齐、JSON 是否能解析。

    返回**产物层**问题清单（空 = 可继续）。把「缺文件 / 坏 JSON」在这里拦下来，
    免得它们变成检查器自己的 traceback（退出码 2 = 「检查器崩了」⇒ 反证脚本无法判罚）。
    """
    problems: list[str] = []
    for relative in REQUIRED_ARTIFACTS:
        target = repoRoot / relative
        if not target.exists():
            problems.append(f"产物缺失：{relative}")
            continue
        if relative.endswith(".json"):
            try:
                with target.open(encoding="utf-8") as handle:
                    json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                problems.append(f"产物损坏：{relative}（{type(error).__name__}: {error}）")
    return problems


def splitCells(line: str) -> list[str]:
    """按**未转义**的 `|` 切表格行（`| expr || x |` 里的 `||` 不会被误切）。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def firstInt(text: str) -> int | None:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def glyphFor(status: str, value: object, known: set[str]) -> str | None:
    """把 JSON 槽位状态映射成 §9.1 表的字形（`open` 再按有无占位值细分）。

    状态词不在 ``known``（JSON 的 ``meta.statusLegend``）内、或不在字形表内时返回 ``None``
    —— 由调用方报问题，避免 KeyError 把整个检查器带崩。
    """
    if status not in known:
        return None
    if status == "open":
        return OPEN_PLACEHOLDER_GLYPH if value is not None else OPEN_EMPTY_GLYPH
    return STATUS_TO_GLYPH.get(status)


def sections(text: str) -> dict[str, str]:
    """按 `## N.` 标题切分正文，返回 {小节序号: 该节内容}（`###` 子标题不算切分点）。"""
    result: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current is not None:
                result[current] = "\n".join(buffer)
            current = line[3:].split(".")[0].strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        result[current] = "\n".join(buffer)
    return result


def section91(template: str) -> str:
    """只取 §9.1 小节（§3 是「回收前基线」表，不参与对账）。"""
    lines = template.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("### 9.1")), None)
    if start is None:
        return ""
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start=start + 1) if line.startswith("### 9.")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def section2(template: str) -> str:
    """只取 §2「13 个槽位定义」小节（槽位值域/必填条件的唯一出处）。

    必须**限定小节**再解析：模板里 §3/§4/§9 也有含槽位名的表，
    全文档扫描会让后面的表**覆盖**掉 §2 的定义（等于「后写的赢」）。
    """
    lines = template.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("## 2.")), None)
    if start is None:
        return ""
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start=start + 1) if line.startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


# --- 槽位 JSON ---


@dataclass(frozen=True)
class SlotEntry:
    """JSON 里一个槽位的取值 + 状态 + 依据。"""

    value: object
    status: str
    answers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotModel:
    """`kpi-slots-filled.json` 的解析结果。"""

    kpis: tuple[dict, ...] = ()
    byCode: dict[str, dict] = field(default_factory=dict)
    order: tuple[str, ...] = ()
    statusLegend: frozenset[str] = frozenset()
    summary: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def entries(self, code: str) -> dict[str, SlotEntry]:
        """某条 KPI 的 13 个模型槽位（补充注记槽不算在内）。"""
        slots = self.byCode[code]["slots"]
        return {
            name: SlotEntry(
                value=slots[name].get("value"),
                status=slots[name].get("status", ""),
                answers=tuple(slots[name].get("answers") or ()),
            )
            for name in MODEL_SLOTS
        }


def parseSlotModel(data: dict) -> SlotModel:
    """解析 `kpi-slots-filled.json`。"""
    kpis = tuple(data["kpis"])
    return SlotModel(
        kpis=kpis,
        byCode={kpi["kpiCode"]: kpi for kpi in kpis},
        order=tuple(kpi["kpiCode"] for kpi in kpis),
        statusLegend=frozenset(data["meta"]["statusLegend"]),
        summary=data.get("summary", {}),
        meta=data.get("meta", {}),
    )


def slotStatuses(model: SlotModel) -> Counter[str]:
    """156 个模型槽位的状态分布。"""
    counts: Counter[str] = Counter()
    for code in model.order:
        for entry in model.entries(code).values():
            counts[entry.status] += 1
    return counts


def slotGlyphs(model: SlotModel) -> Counter[str]:
    """156 个模型槽位的字形分布（`open` 按有无占位值细分）。"""
    counts: Counter[str] = Counter()
    for code in model.order:
        for entry in model.entries(code).values():
            glyph = glyphFor(entry.status, entry.value, set(model.statusLegend))
            if glyph is not None:
                counts[glyph] += 1
    return counts


def supplementaryEntries(model: SlotModel) -> list[tuple[str, str, object]]:
    """补充注记槽的 (KPI, 槽名, 值) 列表。"""
    found: list[tuple[str, str, object]] = []
    for kpi in model.kpis:
        for name, value in kpi["slots"].items():
            if name not in MODEL_SLOTS:
                found.append((kpi["kpiCode"], name, value))
    return found


# --- 模板 §2 值域 ---


@dataclass(frozen=True)
class SlotDefinition:
    """§2 里一个槽位的定义（字段名 / 值域 / 必填条件）。"""

    field: str
    domain: tuple[str, ...]
    required: str


#: 匹配「`A` / `B` / `C`」形式的枚举链（≥ 2 个反引号 token，中间只隔 ` / `）。
_ENUM_CHAIN_PATTERN = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)`(?:\s*/\s*`[A-Za-z_][A-Za-z0-9_]*`)+"
)


def _enumChain(cell: str) -> tuple[str, ...]:
    """抽出一格里「反引号枚举链」的全部取值（没有链 ⇒ 返回空元组）。"""
    match = _ENUM_CHAIN_PATTERN.search(cell)
    if match is None:
        return ()
    return tuple(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", match.group(0)))


def parseSlotDefinitions(template: str) -> dict[str, SlotDefinition]:
    """解析模板 §2 的槽位定义表（值域 + 必填条件）。"""
    definitions: dict[str, SlotDefinition] = {}
    for line in section2(template).split("\n"):
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = splitCells(line)
        if len(cells) < 4:
            continue
        name = cells[1].strip().strip("`")
        if name not in MODEL_SLOTS:
            continue
        definitions[name] = SlotDefinition(
            field=name,
            domain=_enumChain(cells[2]),
            required=cells[3],
        )
    return definitions


# --- 模板 §9.1 表 ---


@dataclass(frozen=True)
class Section91Table:
    """§9.1 表的解析结果。"""

    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, tuple[str, ...]], ...] = ()
    legend: str = ""
    statsLine: str = ""


def parseSection91(template: str) -> Section91Table:
    """解析 §9.1 的表头、数据行、图例行与统计行。

    图例**横跨两行**（第一行 `●`…`⊘`，第二行 `◐`/`○`=open）⇒ 必须合并，
    只取第一行会漏掉 `open` 与两个 open 字形（⇒ 假红）。
    """
    body = section91(template)
    headers: tuple[str, ...] = ()
    rows: list[tuple[str, tuple[str, ...]]] = []
    legendLines: list[str] = []
    statsLine = ""

    for line in body.split("\n"):
        if line.startswith("统计："):
            statsLine = line
            continue
        if line.startswith("按字形"):
            continue
        if not line.startswith("|"):
            # 图例行 = 非表格行 + 含字形 + 含「=」（排除「统计/按字形」两行）
            if GLYPH_PATTERN.search(line) and "=" in line:
                legendLines.append(line)
            continue
        cells = splitCells(line)
        if set(cells[0]) <= {"-", ":"} and len(cells) > 1:
            continue  # 分隔行（`|---|:-:|…`）—— 不能让它覆盖表头
        if cells[0] == "KPI":
            headers = tuple(cells[1:])
            continue
        if cells[0].startswith("`KPI_"):
            code = cells[0].strip().strip("`")
            rows.append((code, tuple(cells[1:])))
    return Section91Table(
        headers=headers,
        rows=tuple(rows),
        legend="\n".join(legendLines),
        statsLine=statsLine,
    )


# --- 登记表 ---


@dataclass(frozen=True)
class RegistryRow:
    """登记表的一行。"""

    key: str
    answer: str
    disposition: str
    landing: str


@dataclass(frozen=True)
class Registry:
    """`answers-registry.md` 的解析结果（构造后不再修改）。"""

    rows: tuple[RegistryRow, ...] = ()
    keys: tuple[str, ...] = ()
    dispositions: tuple[str, ...] = ()
    answers: tuple[str, ...] = ()
    volumeDeclared: dict[str, int] = field(default_factory=dict)
    sectionDeclared: dict[str, int] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    statsRaw: dict[str, str] = field(default_factory=dict)


def _sectionOfHeading(heading: str) -> str | None:
    """把分节标题归一成一个可比较的键（`A1` … `A12` / `A·制度类题`）。"""
    if SECTION_SPECIAL_PATTERN.match(heading):
        return "A·制度类题"
    match = SECTION_CODE_PATTERN.match(heading)
    return match.group(1) if match else None


def parseRegistryHeadings(text: str) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, str]]:
    """解析标题里声明的题数与 `## 统计` 块里的数字。"""
    volumeDeclared: dict[str, int] = {}
    sectionDeclared: dict[str, int] = {}
    stats: dict[str, int] = {}
    statsRaw: dict[str, str] = {}
    inStats = False

    for line in text.split("\n"):
        heading = VOLUME_HEADING_PATTERN.match(line)
        if heading:
            volumeDeclared[heading.group(1)] = int(heading.group(2))
            inStats = False
            continue
        if line.startswith("### "):
            section = _sectionOfHeading(line)
            count = SECTION_COUNT_PATTERN.search(line)
            if section and count:
                sectionDeclared[section] = int(count.group(1))
            inStats = False
            continue
        if line.startswith("## "):
            inStats = line.startswith("## 统计")
            continue
        if not inStats or not line.startswith("|") or line.startswith("|---"):
            continue
        cells = splitCells(line)
        if len(cells) < 2 or cells[0] == "维度":
            continue
        number = firstInt(cells[1])
        if number is not None:
            stats[cells[0]] = number
            statsRaw[cells[0]] = cells[1]
    return volumeDeclared, sectionDeclared, stats, statsRaw


def parseRegistryRows(text: str) -> tuple[RegistryRow, ...]:
    """解析逐题数据行（题号 / 答案要点 / 处置 / 落点）。"""
    rows: list[RegistryRow] = []
    for line in text.split("\n"):
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = splitCells(line)
        if len(cells) < 3 or cells[0] in ("题号", "标记", "维度"):
            continue
        if not QUESTION_KEY_PATTERN.fullmatch(cells[0]):
            continue
        rows.append(
            RegistryRow(
                key=cells[0],
                answer=cells[1],
                disposition=cells[2],
                landing=cells[3] if len(cells) > 3 else "",
            )
        )
    return tuple(rows)


def parseRegistry(text: str) -> Registry:
    """解析登记文档：逐题数据行 + 标题里声明的题数 + `## 统计` 块里的数字。"""
    volume, section, stats, statsRaw = parseRegistryHeadings(text)
    rows = parseRegistryRows(text)
    return Registry(
        rows=rows,
        keys=tuple(row.key for row in rows),
        dispositions=tuple(row.disposition for row in rows),
        answers=tuple(row.answer for row in rows),
        volumeDeclared=volume,
        sectionDeclared=section,
        stats=stats,
        statsRaw=statsRaw,
    )


def sectionCounts(keys: tuple[str, ...]) -> dict[str, int]:
    """按分节统计题数。"""
    counts: dict[str, int] = {}
    for key in keys:
        bucket = "A·制度类题" if "·" in key else key.split(".")[0]
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def priorityCounts(body: str) -> dict[str, int]:
    """统计 §4 里 `### P0` / `### P1` 各自有几道回问。"""
    counts: dict[str, int] = {}
    current: str | None = None
    pattern = ID_ROW_PATTERNS["Q"][1]
    for line in body.split("\n"):
        if line.startswith("### "):
            current = line[3:].strip().split(" ")[0]
            counts.setdefault(current, 0)
            continue
        if current and pattern.match(line):
            counts[current] += 1
    return counts


# --- 冲突文档编号 ---


def idRows(body: str, prefix: str) -> list[str]:
    """抽出某系列的编号字符串（保留重复 —— 重复行是缺陷，不能被 `set()` 折叠）。"""
    pattern = ID_ROW_PATTERNS[prefix][1]
    return [match.group(1) for line in body.split("\n") if (match := pattern.match(line))]


def expectedIds(prefix: str, count: int) -> list[str]:
    """期望的编号字符串（`C-01…C-22` / `Q1…Q18`；用字符串比，`C-022` 不会被当成 `C-22`）。"""
    if prefix == "Q":
        return [str(index) for index in range(1, count + 1)]
    return [f"{index:02d}" for index in range(1, count + 1)]


def landingRefs(landing: str) -> set[str]:
    """「落点」列里引用的登记编号（`C-03` / `G-01` / `R-10` / `Q7`）。"""
    refs: set[str] = set()
    for group, number, qNumber in LANDING_REF_PATTERN.findall(landing):
        if group:
            refs.add(f"{group}-{number}")
        elif qNumber:
            refs.add(f"Q{qNumber}")
    return refs


def refSeries(ref: str) -> str:
    """编号属于哪个系列（`C` / `G` / `R` / `Q`）。"""
    return ref.split("-")[0] if "-" in ref else "Q"


# --- DB 快照（metricId ↔ kpiCode 的唯一外部锚点） ---


def parseSnapshot(text: str) -> dict[str, int]:
    """解析 `evidence/kpi-slot-snapshot.txt` ⇒ {kpi_code: metric_id}。

    文件里有注释行、`(N rows)` 计数行、表头行；数据行是 ` | ` 分隔的管道表。
    """
    mapping: dict[str, int] = {}
    header: list[str] | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("("):
            continue
        cells = [cell.strip() for cell in stripped.split("|")]
        if header is None:
            header = cells
            continue
        if len(cells) < 2:
            continue
        mapping[cells[0]] = int(cells[1])
    return mapping


# --- README ---


def readmeFileList(readme: str) -> list[str]:
    """§1「文件清单」表里列出的相对路径（Markdown 链接的目标）。"""
    body = sections(readme).get("1", "")
    targets: list[str] = []
    for line in body.split("\n"):
        if not line.startswith("|"):
            continue
        for target in re.findall(r"\]\(([^)]+)\)", line):
            if target.startswith(("http://", "https://", "#")):
                continue
            targets.append(target)
    return targets


def readmeKpiStatuses(readme: str) -> dict[str, str]:
    """§2.2「逐 KPI 状态」表 ⇒ {kpi_code: 状态前缀}（✅ / ⚠ / ❌）。"""
    body = sections(readme).get("2", "")
    result: dict[str, str] = {}
    for line in body.split("\n"):
        if not line.startswith("|"):
            continue
        cells = splitCells(line)
        if len(cells) < 3 or cells[0] == "KPI":
            continue
        code = cells[0].strip().strip("`")
        status = cells[1].strip()
        if not code.startswith("KPI_") or not status:
            continue
        result[code] = status[0]
    return result


def openSlotsByName(model: SlotModel) -> Counter[str]:
    """`status = open` 的槽位按槽位名分布（README §4 复述了它的分项）。"""
    counts: Counter[str] = Counter()
    for code in model.order:
        for name, entry in model.entries(code).items():
            if entry.status == "open":
                counts[name] += 1
    return counts
