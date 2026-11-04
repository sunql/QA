#!/usr/bin/env python3
"""CI 判据守卫的**变异取证**：每条守卫都必须证明「把它改坏 ⇒ 对应用例转红」。

规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。

扩展说明（本轮）：
- 支持 **repo 根路径**（`.github/workflows/ci.yml`）—— 作用域 `repo`；
- 支持 `occurrence`：**锚点第 N 次出现**（同一行在 unit / integration 两段里各出现一次时用）。

用法（在仓库根或任意子目录执行均可，脚本按自身位置自动定位仓库根）：

    python3 backend/scripts/ci_mutation_evidence.py            # 全部 28 组
    python3 backend/scripts/ci_mutation_evidence.py M5 M6      # 只跑指定组

环境要求：`TEST_DATABASE_URL` / `TEST_NEO4J_URI` / `TEST_NEO4J_PASSWORD`
（unit 守卫的环境契约，缺一即拒跑）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(os.environ.get("QA_REPO_ROOT", pathlib.Path(__file__).resolve().parents[2]))
BACKEND = REPO / "backend"

# (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)
MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # ---------- M1–M12：继承自已合入的 12 组（文件拆分后锚点跟着搬家） ----------
    (
        "M1",
        "backend",
        "app/utils/ci_judge.py",
        "_COMPLETED_STATUSES = frozenset({0, 1})",
        "_COMPLETED_STATUSES = frozenset({0, 1, 2, 3, 4, 5, 137})",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M2",
        "backend",
        "app/utils/ci_judge.py",
        """    coverageReason = _coverageMismatchReason(baseTuple, prTuple)
    if coverageReason is not None:
        return Verdict(
            canMerge=False,
            blockingReason=coverageReason,
            reasonCode=_REASON_COVERAGE,
        )
""",
        "",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M3",
        "backend",
        "app/utils/ci_judge.py",
        """    if diff.removed:
        # 净丢失**不等于**回归（可能是「顺手修好了」），但规则要求人工确认
        # 不是「把失败藏起来了」⇒ 单列成「需人工确认」，不与净新增混为一谈。
        return Verdict(
            canMerge=False,
            blockingReason=(
                f"净丢失失败用例 {len(diff.removed)} 条 ⇒ 须人工确认不是「把失败藏起来了」"
            ),
            reasonCode=_REASON_NEEDS_REVIEW,
        )
""",
        "",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M4",
        "backend",
        "app/utils/ci_judge.py",
        '        if reason is None and job.conclusion == "failure" and not extractedIds:',
        '        if False and job.conclusion == "failure" and not extractedIds:',
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M5",
        "backend",
        "app/utils/ci_judge.py",
        "    if reason is None and (marker.name != expectedMarker[0] or marker.shard != expectedMarker[1]):",
        "    if False and (marker.name != expectedMarker[0] or marker.shard != expectedMarker[1]):",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M6",
        "backend",
        "app/utils/ci_judge.py",
        "    if marker.status != 0 and not extractedIds:",
        "    if False and marker.status != 0 and not extractedIds:",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M7",
        "backend",
        "app/utils/ci_judge.py",
        "    if len(markers) > 1:",
        "    if len(markers) > 99:",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M8",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if baseRunId == prRunId:",
        "    if False:",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M9",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if not baseAssessments or not prAssessments:",
        "    if False:",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M10",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if not verdict.canMerge or allowCiChanges:",
        "    if True or allowCiChanges:",
        "app/tests/unit/test_ci_failure_set.py",
        1,
    ),
    (
        "M11",
        "backend",
        "app/utils/ci_text.py",
        '    re.compile(r"(?i)\\bsig=[A-Za-z0-9%+/=_-]{10,}"),\n',
        "",
        "app/tests/unit/test_ci_text.py",
        1,
    ),
    (
        "M12",
        "backend",
        "app/utils/ci_surface.py",
        '    r"^\\.github/workflows/",',
        '    r"^\\.github/workflows-NOMATCH/",',
        "app/tests/unit/test_ci_surface.py",
        1,
    ),
    # ---------- M13–M20：模块拆分后新增的 8 条 ----------
    (
        "M13",
        "backend",
        "app/utils/ci_judge.py",
        "    if not hasPytestSummary(logText):",
        "    if False:",
        "app/tests/unit/test_ci_failure_set.py::test_assessJob_rejects_green_run_without_pytest_summary",
        1,
    ),
    (
        "M14",
        "backend",
        "app/utils/ci_judge.py",
        "    countReason = summaryCountMismatchReason(logText)",
        "    countReason = None",
        "app/tests/unit/test_ci_failure_set.py::test_assessJob_rejects_summary_failure_count_mismatch",
        1,
    ),
    (
        "M15",
        "backend",
        "app/utils/ci_judge.py",
        "    if marker.shard == INVALID_SHARD:",
        "    if False:",
        "app/tests/unit/test_ci_failure_set.py::test_assessJob_rejects_invalid_shard_sentinel",
        1,
    ),
    (
        "M16",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if freshness.isFresh:\n        return verdict",
        "    if True:  # 变异：把「run 新鲜度」检查短路\n        return verdict",
        "app/tests/unit/test_ci_failure_set.py::test_main_returns_three_when_pr_run_is_stale",
        1,
    ),
    (
        "M17",
        "backend",
        "app/utils/ci_report.py",
        "    if len(lines) > _MAX_REPORT_LINES:",
        "    if False:",
        "app/tests/unit/test_ci_report.py::test_report_caps_total_line_count",
        1,
    ),
    (
        "M18",
        "backend",
        "app/utils/ci_report.py",
        "    digest = hashlib.sha256(failureId.encode(\"utf-8\")).hexdigest()[:8]\n    return f\"{cleaned} [sha256:{digest}]\"",
        "    return cleaned",
        "app/tests/unit/test_ci_report.py::test_report_adds_fingerprint_when_failure_id_was_sanitized",
        1,
    ),
    (
        "M19",
        "backend",
        "app/utils/ci_report.py",
        "    for note in notes:\n        lines.append(f\"提示：{sanitizeForReport(note)}\")",
        "    pass",
        "app/tests/unit/test_ci_failure_set.py::test_main_surfaces_fork_note_in_report",
        1,
    ),
    (
        "M20",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if verdict.reasonCode == _REASON_STALE_RUN:\n        return 3",
        "    if False:\n        return 3",
        "app/tests/unit/test_ci_failure_set.py::test_main_returns_three_when_pr_run_is_stale",
        1,
    ),
    # ---------- 本轮新增：workflow 侧（L-2 / L-3 / L-9 / L-11 + MEDIUM-4） ----------
    (
        "M21",
        "repo",
        ".github/workflows/ci.yml",
        '            echo "UNIT_SHARD_DONE shard=$shard_index status=4"',
        "            :  # 变异：移除 unit 失败路径的完成标记",
        "app/tests/unit/test_ci_integration_step.py::test_unit_step_marks_failure_paths",
        1,
    ),
    (
        "M22",
        "repo",
        ".github/workflows/ci.yml",
        "          shard_index=-1\n          fail_unit() {",
        "          shard_index=0\n          fail_unit() {",
        "app/tests/unit/test_ci_integration_step.py::test_both_jobs_use_invalid_shard_sentinel",
        1,
    ),
    (
        "M23",
        "repo",
        ".github/workflows/ci.yml",
        "          trap 'rm -f \"$list_file\"' EXIT",
        "          :  # 变异：移除 integration 侧 trap 清理",
        "app/tests/unit/test_ci_integration_step.py::test_both_jobs_trap_temp_file_cleanup",
        2,
    ),
    (
        "M24",
        "repo",
        ".github/workflows/ci.yml",
        '--shards "$SHARD_COUNT" --shard "$shard_index" --nul',
        '--shards 4 --shard "$shard_index" --nul',
        "app/tests/unit/test_ci_integration_step.py::test_unit_run_passes_shard_count_to_the_shard_tool",
        1,
    ),
    (
        "M25",
        "repo",
        ".github/workflows/ci.yml",
        '--shards "$SHARD_COUNT" --shard "$shard_index" --nul',
        '--shards "$SHARD_COUNT" --shard "$shard_index"',
        "app/tests/unit/test_ci_integration_step.py::test_unit_run_uses_nul_separated_file_list",
        1,
    ),
    (
        "M26",
        "repo",
        ".github/workflows/ci.yml",
        '          SHARD_COUNT: "4"',
        '          SHARD_COUNT: "5"',
        "app/tests/unit/test_ci_integration_step.py::test_unit_step_declares_shard_count_matching_matrix",
        1,
    ),
    (
        "M27",
        "backend",
        "app/utils/ci_surface.py",
        '    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log|judge|report)\\.py$",',
        '    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log)\\.py$",',
        "app/tests/unit/test_ci_surface.py::test_detectCiSurfaceChanges_covers_judgment_and_report_modules",
        1,
    ),
    (
        "M28",
        "backend",
        "app/utils/ci_shard.py",
        '        sys.stdout.write("".join(f"{path}\\0" for path in paths))',
        '        sys.stdout.write("\\n".join(paths))',
        "app/tests/unit/test_ci_shard.py::test_cli_can_emit_nul_separated_list",
        1,
    ),
]

_REQUIRED_ENV = ("TEST_DATABASE_URL", "TEST_NEO4J_URI", "TEST_NEO4J_PASSWORD")

# 单条守卫用例跑不完就当取证失败（fail-closed：宁可误报也不无限等）
CASE_TIMEOUT_SECONDS = 300


def buildEnv() -> dict[str, str]:
    """继承当前 shell 环境（凭据不入库）；缺必需变量就 fail-fast。"""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"缺少必需环境变量：{', '.join(missing)}（见文件头环境要求）")
    return dict(os.environ)


def resolve(scope: str, relPath: str) -> pathlib.Path:
    return (BACKEND if scope == "backend" else REPO) / relPath


def replaceOccurrence(source: str, original: str, mutated: str, occurrence: int) -> str | None:
    """把锚点的第 `occurrence` 次出现替换掉；找不到返回 None。"""
    index = -1
    for _ in range(occurrence):
        index = source.find(original, index + 1)
        if index < 0:
            return None
    return source[:index] + mutated + source[index + len(original) :]


def run_case(case: str, env: dict[str, str]) -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", case, "-q", "--no-header", "-p", "no:randomly"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=CASE_TIMEOUT_SECONDS,
    )
    return completed.returncode != 0, completed.stdout[-800:]


def main() -> int:
    only = set(sys.argv[1:])
    env = buildEnv()
    results: list[tuple[str, bool, str]] = []
    for name, scope, relPath, original, mutated, case, occurrence in MUTATIONS:
        if only and name not in only:
            continue
        path = resolve(scope, relPath)
        source = path.read_text()
        total = source.count(original)
        if total < occurrence:
            print(f"✗ {name}: 变异锚点在 {relPath} 里只出现 {total} 次（第 {occurrence} 次不存在）")
            results.append((name, False, "锚点不存在"))
            continue
        changed = replaceOccurrence(source, original, mutated, occurrence)
        assert changed is not None
        path.write_text(changed)
        try:
            wentRed, output = run_case(case, env)
        finally:
            path.write_text(source)
        results.append((name, wentRed, case))
        mark = "✓" if wentRed else "✗"
        print(f"{mark} {name}: {case} ⇒ {'转红（守卫有效）' if wentRed else '仍然绿（守卫无效！）'}")
        if not wentRed:
            print(output)

    bad = [name for name, ok, _ in results if not ok]
    print(f"\n合计 {len(results)} 组变异，净漏 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
