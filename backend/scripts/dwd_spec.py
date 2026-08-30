"""dwd_spec.py — 解析 THBI DWD 建仓 SQL，生成 DWD 表规格。

从 dw/02_dwd_master.sql + dw/03_dwd_facts.sql 提取，供本体重建
（rebind_ontology_thbi.py）消费：

    spec[table] = { column: {"type": <Oracle type>, "pk": bool, "x3": <X3 源列>} }

- column：DWD 英文 snake_case 列名（标准命名）
- type：Oracle 类型（VARCHAR2(20) / NUMBER / DATE / TIMESTAMP(3)）
- pk：该列是否在 DWD 表的 PRIMARY KEY 约束内
- x3：INSERT SELECT 中对应表达式解析出的 X3 源列名（如 BPSNUM_0）
  空表达式（如常量/计算）x3 为 None

纯函数模块，无外部依赖（不连库），可独立单测。
"""
from __future__ import annotations

import re
from pathlib import Path

# X3 列名：大写字母/数字/下划线，末尾 `_数字`（如 BPSNUM_0 / EXTRCPDAT_0）
_X3_COL_RE = re.compile(r"\b[A-Z][A-Z0-9_]*_\d\b")

_ORACLE_TYPE_RE = re.compile(
    r"([A-Z][A-Z0-9]*(?:\s+WITH TIME ZONE)?)(?:\((\d+)(?:,\s*(\d+))?\))?",
    re.IGNORECASE,
)


def _stripComments(sql: str) -> str:
    """去掉 `--` 行注释（保留字符串字面量内的 `--` 不受影响；本文件无此场景）。"""
    out: list[str] = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # 跳到行尾
            while i < n and sql[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _balancedParen(sql: str, start: int) -> int:
    """从 sql[start]=='(' 起扫描，返回配对的右括号下标。

    跳过字符串字面量内的括号（如 DATE '1900-01-01' 无括号，但防御性保留）。
    """
    depth = 0
    in_str = False
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("不平衡的括号")


def _splitTopLevel(sql: str) -> list[str]:
    """按顶层逗号切分（忽略字符串与括号内的逗号），返回去首尾空白片段。"""
    parts: list[str] = []
    depth = 0
    in_str = False
    i = 0
    n = len(sql)
    cur: list[str] = []
    while i < n:
        ch = sql[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    cur.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            cur.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            cur.append(ch)
            i += 1
            continue
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _oracleType(raw: str) -> str:
    """归一化 Oracle 列类型：去空白、大写（保留精度，如 VARCHAR2(20)）。"""
    m = _ORACLE_TYPE_RE.match(raw.strip())
    if not m:
        return raw.strip().upper()
    base = m.group(1).upper().replace(" ", "_")
    if m.group(2):
        return f"{base}({m.group(2)})"
    return base


def parseDwdFiles(paths: list[Path]) -> dict[str, dict[str, dict]]:
    """解析 DWD SQL 文件 -> {table: {column: {type, pk, x3}}}。

    对每个 DWD 表：CREATE TABLE 提供列名/类型/主键约束，
    同表 INSERT SELECT 提供 列 -> X3 源列 的逐位映射。
    """
    spec: dict[str, dict[str, dict]] = {}
    for path in paths:
        sql = _stripComments(path.read_text(encoding="utf-8"))
        _parseCreateTables(sql, spec)
        _parseInserts(sql, spec)
    return spec


def _parseCreateTables(sql: str, spec: dict[str, dict[str, dict]]) -> None:
    """解析 CREATE TABLE THBI.DWD_xxx (...) 的列与主键。"""
    for m in re.finditer(r"CREATE\s+TABLE\s+THBI\.(DWD_\w+)", sql, re.IGNORECASE):
        table = m.group(1).upper()
        # 定位表名后的左括号
        open_idx = sql.index("(", m.end())
        close_idx = _balancedParen(sql, open_idx)
        body = sql[open_idx + 1 : close_idx]

        cols: dict[str, dict] = {}
        pk_cols: set[str] = set()
        for part in _splitTopLevel(body):
            upper = part.upper()
            if upper.startswith("CONSTRAINT"):
                pm = re.search(r"PRIMARY\s+KEY\s*\((.*?)\)", part, re.IGNORECASE | re.S)
                if pm:
                    pk_cols = {
                        c.strip().lower() for c in _splitTopLevel(pm.group(1))
                    }
                continue
            cm = re.match(r"(\w+)\s+(.+)$", part.strip(), re.S)
            if cm:
                col = cm.group(1).lower()
                cols[col] = {
                    "type": _oracleType(cm.group(2).strip().rstrip(",")),
                    "pk": False,
                    "x3": None,
                }
        for col in pk_cols:
            if col in cols:
                cols[col]["pk"] = True
        spec[table] = cols


def _parseInserts(sql: str, spec: dict[str, dict[str, dict]]) -> None:
    """解析 INSERT INTO THBI.DWD_xxx (cols) SELECT <exprs> FROM THBI.ODS_yyy。

    expr 与 INSERT 列逐位对齐，每个 expr 取第一个 X3 列作为 x3 映射。
    """
    for m in re.finditer(r"INSERT\s+INTO\s+THBI\.(DWD_\w+)", sql, re.IGNORECASE):
        table = m.group(1).upper()
        open_idx = sql.index("(", m.end())
        close_idx = _balancedParen(sql, open_idx)
        col_list = _splitTopLevel(sql[open_idx + 1 : close_idx])

        # 右括号后找 SELECT，取到 FROM THBI.ODS_ 为止的表达式体
        after = sql[close_idx + 1 :]
        sel_m = re.search(r"\bSELECT\s+(.*?)\bFROM\s+THBI\.ODS_\w+", after, re.IGNORECASE | re.S)
        if not sel_m:
            continue
        exprs = _splitTopLevel(sel_m.group(1))

        cols = spec.get(table)
        if cols is None:
            continue
        for col_raw, expr in zip(col_list, exprs):
            col = col_raw.strip().lower()
            if col not in cols:
                continue
            # 取最后一个 X3 列：清洗表达式 `CASE WHEN QTYUOM_0 > 1e9 THEN NULL ELSE LINAMT_0 END`
            # 的守卫列是 QTYUOM_0，取值列是 LINAMT_0 —— 首个匹配会把金额列误映射到守卫列。
            matches = list(_X3_COL_RE.finditer(expr))
            if matches:
                cols[col]["x3"] = matches[-1].group(0)


def dumpSpec(spec: dict[str, dict[str, dict]]) -> str:
    """把规格渲染成可读文本（供 --dump-spec 输出 / 日志）。"""
    lines: list[str] = []
    for table in sorted(spec):
        cols = spec[table]
        lines.append(f"\n== {table} ({len(cols)} 列) ==")
        for col in sorted(cols):
            c = cols[col]
            pk = " PK" if c["pk"] else ""
            x3 = c["x3"] or "-"
            lines.append(f"  {col:<38} {c['type']:<16}{pk} <- {x3}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    DW_DIR = Path(__file__).resolve().parents[2] / "dw"
    files = [DW_DIR / f for f in ("02_dwd_master.sql", "03_dwd_facts.sql")]
    spec = parseDwdFiles(files)
    print(dumpSpec(spec))
    print(f"\n共 {len(spec)} 张 DWD 表")
    sys.exit(0)
