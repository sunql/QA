#!/usr/bin/env python3
"""L4 token profile 日志分析 CLI（薄壳，逻辑在 app/services/l4_token_profile_report.py）。

用法：
    docker logs qa-backend --since 2026-10-31T00:00:00 2>&1 \\
        | python analyze_token_profile.py --input-price 0.0014 --output-price 0.0028
    python analyze_token_profile.py evidence/l4-profile.log

选项：
    --input-price / --output-price   每千 token 单价（USD，deepseek-chat: 0.0014 / 0.0028）
    --cache-multiplier               缓存命中计费折扣（生产 LLM_CACHE_HIT_MULTIPLIER=0.25）
"""

from __future__ import annotations

import argparse
import pathlib
import sys

DEFAULT_BACKEND = pathlib.Path(__file__).resolve().parents[3] / "backend"


def main() -> int:
    parser = argparse.ArgumentParser(description="L4 token profile 分析")
    parser.add_argument("logfile", nargs="?", default="-", help="日志文件路径；缺省读 stdin")
    parser.add_argument("--input-price", type=float, default=0.0014)
    parser.add_argument("--output-price", type=float, default=0.0028)
    parser.add_argument("--cache-multiplier", type=float, default=0.25)
    args = parser.parse_args()

    sys.path.insert(0, str(DEFAULT_BACKEND))
    from app.services.l4_token_profile_report import aggregateRun, parseProfileLines, renderMarkdown

    if args.logfile == "-":
        lines = sys.stdin.read().splitlines()
    else:
        lines = pathlib.Path(args.logfile).read_text(encoding="utf-8", errors="replace").splitlines()

    parsed = parseProfileLines(lines)
    print(
        f">> 解析：{parsed.linesSeen} 行，匹配 {parsed.linesMatched}，"
        f"跳过 {parsed.linesSkipped}；runs={len(parsed.runs)}"
    )
    if not parsed.runs:
        print(">> 没有匹配到 L4_TOKEN_PROFILE / L4_TOOL_RESULT 行")
        return 1

    loopEndByRun = {str(r.get("runId")): r for r in parsed.loopEnds}
    for runId, profiles in parsed.runs.items():
        run = aggregateRun(
            runId,
            profiles,
            toolResultRecords=parsed.toolResultRecords,
            loopEnd=loopEndByRun.get(runId),
            cacheMultiplier=args.cache_multiplier,
            inputPricePer1k=args.input_price,
            outputPricePer1k=args.output_price,
        )
        print(renderMarkdown(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
