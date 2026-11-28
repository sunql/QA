#!/usr/bin/env python3
"""把多轮 Oracle 备份 SQL **按表合并**成一份（表级覆盖，优先级可配）。

为什么需要：备份是分多轮跑的（断点续传 + 规则修正重导），
同一个 schema 的不同轮次落在不同文件里，且**同一张表可能有多份**（旧规则版 / 新规则版）。
⇒ 直接 `cat` 会产生重复 CREATE TABLE；按「表」取一份才是可回放的脚本。

块完整性判定（与看门狗同一套口径）：
- 该表块**有备注行**（`-- 时间过滤：` / `-- ⚠️ …`）⇒ 数据写完了；
- 或所在文件以 `COMMIT;` 结尾 ⇒ 整文件都完整。

优先级：同一张表出现在多个文件里时，取 **priority 大**的那个文件（数字大 = 更新）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time

HEADER_RE = re.compile(rb"^-- ===== THBI\.(\w+) =====$")
#: 备注行（中文 + emoji）用 **str** 匹配 ⇒ 逐行 decode 后再判
NOTE_PREFIXES = ("-- 时间过滤：", "-- ⚠️ ")
INSERT_RE = re.compile(rb'^INSERT INTO "(\w+)"')
COMMIT_MARK = b"COMMIT;"


def indexFile(path: pathlib.Path) -> tuple[dict[str, dict], bool]:
    """扫描一个备份 SQL，返回 {表名: {start, end, rows, complete}} 与「文件是否收尾」。"""
    blocks: dict[str, dict] = {}
    current: str | None = None
    rows = 0
    hasNote = False
    carry = b""          # 跨块残行
    carryStart = 0       # carry[0] 在文件中的绝对偏移
    fileEnd = 0
    tail = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 22)
            if not chunk:
                break
            tail = chunk[-64:]
            fileEnd = carryStart + len(carry) + len(chunk)
            carry += chunk
            lines = carry.split(b"\n")
            carry = lines.pop()
            pos = carryStart
            for line in lines:
                lineLen = len(line) + 1
                matched = HEADER_RE.match(line)
                if matched:
                    if current is not None:
                        blocks[current].update(
                            {"end": pos, "rows": rows, "complete": hasNote}
                        )
                    current = matched.group(1).decode()
                    rows = 0
                    hasNote = False
                    blocks[current] = {
                        "start": pos,
                        "end": pos,
                        "rows": 0,
                        "complete": False,
                    }
                elif current is not None:
                    if INSERT_RE.match(line):
                        rows += 1
                    elif line.decode("utf-8", "replace").startswith(NOTE_PREFIXES):
                        hasNote = True
                pos += lineLen
            carryStart = pos
    if current is not None:
        blocks[current].update({"end": fileEnd, "rows": rows, "complete": hasNote})
    finished = COMMIT_MARK in tail
    if finished:
        for block in blocks.values():
            block["complete"] = True
    return blocks, finished


def main() -> int:
    parser = argparse.ArgumentParser(description="合并多轮 Oracle 备份 SQL")
    parser.add_argument("--out", required=True, help="合并后的 .sql")
    parser.add_argument("--manifest", required=True, help="合并后的 manifest.json")
    parser.add_argument(
        "--order",
        default="",
        help="表顺序 JSON（list）；不给则按**全部输入文件里出现过的表名排序**"
        "（= 导出脚本内部的 Python 排序，顺序一致）",
    )
    parser.add_argument(
        "--file",
        action="append",
        required=True,
        help="备份文件，格式 <priority>:<path>（priority 大的优先）",
    )
    args = parser.parse_args()

    sources: list[tuple[int, pathlib.Path]] = []
    for item in args.file:
        priority, _, raw = item.partition(":")
        sources.append((int(priority), pathlib.Path(raw)))

    chosen: dict[str, tuple[int, pathlib.Path, dict]] = {}
    seenTables: set[str] = set()
    for priority, path in sources:
        if not path.exists():
            continue
        blocks, finished = indexFile(path)
        seenTables |= set(blocks)
        for table, block in blocks.items():
            if not block["complete"]:
                continue
            current = chosen.get(table)
            if current is None or priority > current[0]:
                chosen[table] = (priority, path, block)
        print(f"[merge] 索引 {path.name}: 表={len(blocks)} 完整={sum(1 for b in blocks.values() if b['complete'])} 收尾={finished}")

    if args.order:
        order: list[str] = json.loads(pathlib.Path(args.order).read_text())
    else:
        order = sorted(seenTables)

    missing = [t for t in order if t not in chosen]
    if missing:
        print(f"[merge] ⚠️ 仍缺 {len(missing)} 张：{', '.join(missing[:20])}")

    outPath = pathlib.Path(args.out)
    manifestTables: list[dict] = []
    totalRows = 0
    with outPath.open("wb") as out:
        out.write(b"-- Oracle schema backup (merged)\n")
        out.write(f"-- generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n".encode())
        out.write(b"-- merged by merge_backup_sql.py (table-level, one block per table)\n")
        out.write(b"SET DEFINE OFF;\nWHENEVER SQLERROR EXIT SQL.SQLCODE;\n\n")
        for table in order:
            entry = chosen.get(table)
            if entry is None:
                continue
            _, path, block = entry
            with path.open("rb") as handle:
                handle.seek(block["start"])
                payload = handle.read(block["end"] - block["start"])
            out.write(payload)
            if not payload.endswith(b"\n"):
                out.write(b"\n")
            manifestTables.append(
                {
                    "table": table,
                    "rows": block["rows"],
                    "sourceFile": path.name,
                }
            )
            totalRows += block["rows"]
        out.write(b"\nCOMMIT;\nEXIT;\n")

    pathlib.Path(args.manifest).write_text(
        json.dumps(
            {
                "schema": "THBI",
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": "多轮备份按表合并；每表取 priority 最高且数据完整的那份",
                "tableCount": len(manifestTables),
                "rowCount": totalRows,
                "tables": manifestTables,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[merge] 输出 {outPath} 表={len(manifestTables)} 行={totalRows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
