"""dwd_merge.py — 把 DWD 建仓 INSERT SELECT 转成 MERGE 增量语句（纯函数）。

由 dw/run_build.py 的 --incremental 模式消费：把 02_dwd_master.sql /
03_dwd_facts.sql 里的 `INSERT INTO THBI.DWD_x (cols) SELECT ... FROM THBI.ODS_y`
转成按 UPDDATTIM_0 水位过滤的 MERGE 语句，使 DWD 表可增量同步
（不再 drop + 全量重灌；全量快照模式保持不变）。

设计要点：
- USING 子查询对每个 SELECT 表达式显式 `AS <DWD 列名>`，使清洗 / CASE / JOIN 列
  在 ON / UPDATE / INSERT 里都能稳定引用，且语义与全量 INSERT 逐位一致。
- MERGE 键 = DWD 表的 PRIMARY KEY（ON 列禁止出现在 UPDATE SET 中）。
  无 PK 的表（DWD_BOM / DWD_BOM_DETAIL / DWD_ROUTING_OPERATION）交
  buildReload 退化为全量重灌。
- 水位注入：单源表 `WHERE UPDDATTIM_0 >= :wm`；多源 JOIN 表
  `WHERE GREATEST(d.UPDDATTIM_0, h.UPDDATTIM_0) >= :wm`（头行变更也触发行重灌）。
- 使用 `>=` 而非 `>`：MERGE 幂等，重复处理同一水位行无害，且不漏边界。

纯函数模块，不连库，可独立单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:  # pytest 走 scripts 包导入；standalone / run_build 走脚本目录导入
    from dwd_spec import _balancedParen, _splitTopLevel, _stripComments
except ImportError:  # pragma: no cover - 双导入仅取决于导入上下文
    from .dwd_spec import _balancedParen, _splitTopLevel, _stripComments

WM_COL = "UPDDATTIM_0"
WM_BIND = ":wm"
# GREATEST 双源水位中 NULL 一侧退化为该值（Oracle GREATEST 遇 NULL 即返回 NULL，
# 会让该行被水位过滤掉，与全量 INSERT 分叉）。与 run_build.EPOCH_WM 同口径。
_EPOCH_LITERAL = "TIMESTAMP '1900-01-01 00:00:00'"

_INSERT_RE = re.compile(r"INSERT\s+INTO\s+THBI\.(DWD_\w+)", re.IGNORECASE)
_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+THBI\.(DWD_\w+)", re.IGNORECASE)
_SELECT_RE = re.compile(
    r"\bSELECT\s+(.*?)\bFROM\s+THBI\.ODS_\w+", re.IGNORECASE | re.S
)
# from_clause 已剥掉前导 FROM 关键字，首表即源表；JOIN 表仍带 JOIN 关键字
_FROM_RE = re.compile(r"^THBI\.(\w+)(?:\s+([a-zA-Z_]\w*))?", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\s+THBI\.(\w+)(?:\s+([a-zA-Z_]\w*))?", re.IGNORECASE)
_PK_RE = re.compile(r"PRIMARY\s+KEY\s*\((.*?)\)", re.IGNORECASE | re.S)


@dataclass(frozen=True)
class SourceRef:
    """源表引用：表名 + 表别名（无别名则为 None）。"""

    table: str
    alias: str | None


@dataclass(frozen=True)
class InsertInfo:
    """一条 INSERT SELECT 的结构化信息（列清单 / 表达式 / 源表 / WHERE）。"""

    target: str
    cols: tuple[str, ...]
    exprs: tuple[str, ...]
    sources: tuple[SourceRef, ...]
    from_clause: str
    where: str | None


def _findStatementEnd(text: str, start: int) -> int:
    """从 start 起找语句结束的 ';'（跳过字符串字面量内的分号）。"""
    in_str = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == ";":
            return i
        i += 1
    return n


def parseDwdInserts(sql: str) -> dict[str, InsertInfo]:
    """解析 DWD SQL 文本 -> {表名: InsertInfo}（按语句出现顺序）。"""
    clean = _stripComments(sql)
    out: dict[str, InsertInfo] = {}
    for m in _INSERT_RE.finditer(clean):
        target = m.group(1).upper()
        open_idx = clean.index("(", m.end())
        close_idx = _balancedParen(clean, open_idx)
        col_list = _splitTopLevel(clean[open_idx + 1 : close_idx])

        after = clean[close_idx + 1 :]
        sel_m = _SELECT_RE.search(after)
        if not sel_m:
            raise ValueError(f"{target}: 未找到 SELECT ... FROM THBI.ODS_xx")
        exprs = _splitTopLevel(sel_m.group(1))
        if len(col_list) != len(exprs):
            raise ValueError(
                f"{target}: INSERT 列数({len(col_list)}) != SELECT 表达式数({len(exprs)})"
            )

        stmt_end = _findStatementEnd(after, sel_m.start())
        tail = after[sel_m.start() : stmt_end].strip()
        # 从 FROM 关键字起才是 FROM/JOIN/WHERE 部分（sel_m.group(1) 是表达式体）
        from_keyword = len(sel_m.group(1)) + re.search(
            r"\bFROM\b", sel_m.group(0)[len(sel_m.group(1)):], re.IGNORECASE
        ).start()
        from_tail = tail[from_keyword:].strip()
        where_m = re.search(r"\bWHERE\b", from_tail, re.IGNORECASE)
        if where_m:
            from_clause = from_tail[: where_m.start()].strip()
            where_clause = from_tail[where_m.end() :].strip() or None
        else:
            from_clause = from_tail
            where_clause = None
        # from_clause 以 FROM 开头，剥掉前导关键字，各 builder 统一加 "FROM " 前缀
        from_clause = re.sub(
            r"^FROM\s+", "", from_clause, count=1, flags=re.IGNORECASE
        ).strip()

        sources: list[SourceRef] = []
        fm = _FROM_RE.search(from_clause)
        if fm:
            sources.append(SourceRef(fm.group(1).upper(), fm.group(2)))
        for jm in _JOIN_RE.finditer(from_clause):
            sources.append(SourceRef(jm.group(1).upper(), jm.group(2)))
        if not sources:
            raise ValueError(f"{target}: 未解析出 FROM 源表")

        out[target] = InsertInfo(
            target=target,
            cols=tuple(c.strip().lower() for c in col_list),
            exprs=tuple(e.strip() for e in exprs),
            sources=tuple(sources),
            from_clause=from_clause,
            where=where_clause,
        )
    return out


def parseCreatePks(sql: str) -> dict[str, list[str]]:
    """解析 DWD CREATE TABLE 的 PRIMARY KEY 列 -> {表名: [PK 列]}（无 PK 为空列表）。"""
    clean = _stripComments(sql)
    out: dict[str, list[str]] = {}
    for m in _CREATE_RE.finditer(clean):
        table = m.group(1).upper()
        open_idx = clean.index("(", m.end())
        close_idx = _balancedParen(clean, open_idx)
        body = clean[open_idx + 1 : close_idx]
        pk: list[str] = []
        for part in _splitTopLevel(body):
            pm = _PK_RE.search(part)
            if pm:
                pk = [c.strip().lower() for c in _splitTopLevel(pm.group(1))]
                break
        out[table] = pk
    return out


def sourceWatermarkExpr(sources: tuple[SourceRef, ...]) -> str:
    """源水位列表达式：单表无别名直接用列名，多表 GREATEST（头行变更也触发）。

    多表时对每侧 COALESCE 到 epoch：一侧 UPDDATTIM_0 为 NULL 时 GREATEST 会返回
    NULL 使该行被水位过滤掉（增量丢行，与全量 INSERT 分叉），COALESCE 让其退化为
    另一侧时间，仅当两侧都 NULL 才用 epoch 兜底。
    """
    parts = []
    for s in sources:
        if len(sources) == 1 and not s.alias:
            parts.append(WM_COL)
        elif s.alias:
            parts.append(f"{s.alias}.{WM_COL}")
        else:
            parts.append(f"{s.table}.{WM_COL}")
    if len(parts) == 1:
        return parts[0]
    inner = ", ".join(f"COALESCE({p}, {_EPOCH_LITERAL})" for p in parts)
    return f"GREATEST({inner})"


def buildDwdMerge(info: InsertInfo, pk_cols: list[str]) -> str:
    """把 INSERT SELECT 转成按水位过滤的 MERGE 语句。

    :param pk_cols: DWD 表 PK 列（小写 snake_case），作为 MERGE ON 键。
    """
    cols, exprs = list(info.cols), list(info.exprs)
    if len(cols) != len(exprs):
        raise ValueError(
            f"{info.target}: INSERT 列数({len(cols)}) != SELECT 表达式数({len(exprs)})"
        )
    missing_pk = [c for c in pk_cols if c not in cols]
    if missing_pk:
        raise ValueError(f"{info.target}: PK 列 {missing_pk} 不在 INSERT 列清单")

    aliased = ",\n    ".join(f"{e} AS {c}" for c, e in zip(cols, exprs))
    on_clause = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)
    set_lines = [
        f"  t.{c} = s.{c}" for c in cols if c not in pk_cols and c != "etl_load_ts"
    ]
    set_lines.append("  t.etl_load_ts = SYSTIMESTAMP")
    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(f"s.{c}" for c in cols)

    wm_pred = f"{sourceWatermarkExpr(info.sources)} >= {WM_BIND}"
    where_clause = f"{wm_pred} AND {info.where}" if info.where else wm_pred

    return (
        f"MERGE INTO THBI.{info.target} t\n"
        "USING (\n"
        "  SELECT\n"
        f"    {aliased}\n"
        f"  FROM {info.from_clause}\n"
        f"  WHERE {where_clause}\n"
        ") s\n"
        f"ON ({on_clause})\n"
        "WHEN MATCHED THEN UPDATE SET\n"
        f"{',\n'.join(set_lines)}\n"
        "WHEN NOT MATCHED THEN INSERT\n"
        f"  ({insert_cols})\n"
        f"  VALUES ({insert_vals})\n"
    )


def buildReload(info: InsertInfo) -> str:
    """无 PK 表的全量重灌语句（DELETE + INSERT 由调用方编排）。

    语义与原 INSERT SELECT 完全一致（保留 JOIN / WHERE / 清洗表达式）。
    """
    exprs = ",\n  ".join(info.exprs)
    tail = f"FROM {info.from_clause}"
    if info.where:
        tail = f"{tail}\nWHERE {info.where}"
    return (
        f"INSERT INTO THBI.{info.target}\n"
        f"  ({', '.join(info.cols)})\n"
        "SELECT\n"
        f"  {exprs}\n"
        f"{tail}\n"
    )


def buildOdsMerge(source: str, target: str, cols: list[str], pk_cols: list[str]) -> str:
    """ODS 层 MERGE：从 ZJTH 源表按 UPDDATTIM_0 水位增量同步。

    :param source: ZJTH 源表名（如 PORDER）
    :param target: THBI ODS 表名（如 ODS_PORDER）
    :param cols: ODS 表全部列（含 ETL_LOAD_TS，最后一个），COLUMN_ID 顺序
    :param pk_cols: ZJTH 源表唯一索引列（大写），ITMMASTER 手动 ITMREF_0
    """
    pk_cols = [p.upper() for p in pk_cols]
    etl_col = "ETL_LOAD_TS"
    pk_set = {p for p in pk_cols}
    set_lines = [
        f"  t.{c} = s.{c}" for c in cols if c.upper() not in pk_set and c.upper() != etl_col
    ]
    set_lines.append(f"  t.{etl_col} = SYSTIMESTAMP")
    on_clause = " AND ".join(f"t.{p} = s.{p}" for p in pk_cols)
    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(
        "SYSTIMESTAMP" if c.upper() == etl_col else f"s.{c}" for c in cols
    )
    return (
        f"MERGE INTO THBI.{target} t\n"
        "USING (\n"
        f"  SELECT s.*, SYSTIMESTAMP AS {etl_col}\n"
        f"  FROM ZJTH.{source} s\n"
        f"  WHERE s.{WM_COL} >= {WM_BIND}\n"
        ") s\n"
        f"ON ({on_clause})\n"
        "WHEN MATCHED THEN UPDATE SET\n"
        f"{',\n'.join(set_lines)}\n"
        "WHEN NOT MATCHED THEN INSERT\n"
        f"  ({insert_cols})\n"
        f"  VALUES ({insert_vals})\n"
    )


def buildWatermarkQuery(info: InsertInfo) -> str:
    """计算该 DWD 表下次增量起点的水位 SQL（源 MAX(UPDDATTIM_0)）。

    单源：SELECT MAX(UPDDATTIM_0) FROM THBI.ODS_x [WHERE ...]
    双源：SELECT MAX(GREATEST(COALESCE(d.UPDDATTIM_0, epoch),
          COALESCE(h.UPDDATTIM_0, epoch)))
          FROM THBI.ODS_a d JOIN THBI.ODS_b h ON ... [WHERE ...]

    与 MERGE 的 USING 子查询同谓词：保留 info.where（业务过滤），避免不合格行
    （如 INVTYP_0 非 1/2、PLI_0 空白）把水位抬得过高，导致合格行被静默漏数。
    """
    tail = f"FROM {info.from_clause}"
    if info.where:
        tail = f"{tail}\nWHERE {info.where}"
    return f"SELECT MAX({sourceWatermarkExpr(info.sources)}) {tail}"


def buildOdsWatermarkQuery(source: str) -> str:
    """计算 ODS 表下次增量起点的水位 SQL（ZJTH 源 MAX(UPDDATTIM_0)）。"""
    return f"SELECT MAX({WM_COL}) FROM ZJTH.{source}"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    DW_DIR = Path(__file__).resolve().parents[2] / "dw"
    files = [DW_DIR / f for f in ("02_dwd_master.sql", "03_dwd_facts.sql")]
    sql_text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    inserts = parseDwdInserts(sql_text)
    pks = parseCreatePks(sql_text)
    for table in inserts:
        kind = "MERGE" if pks.get(table) else "reload"
        print(f"{table:<38} {kind}")
    sys.exit(0)
