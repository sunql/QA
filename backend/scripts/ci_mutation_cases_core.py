"""CI 判据守卫的**变异清单 · 核心批**（M1–M36，纯数据）。

从 `ci_mutation_cases.py` 拆出（M37 起的 N6 批太大 ⇒ 两个文件都超 800 行上限）。
规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。
本文件只放**数据**；跑者在 `backend/scripts/ci_mutation_evidence.py`，
完整清单的拼装在 `ci_mutation_cases.py`（`MUTATIONS = MUTATIONS_CORE + N6 批`）。

⚠️ **本文件与跑者同属「CI 相关面」**（改了它就能让守卫恒绿）
⇒ 必须同时进 `.github/CODEOWNERS` 与 `ci_surface.py` 的 `_CI_SURFACE_PATTERNS`。
"""

from __future__ import annotations

# (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)
MUTATIONS_CORE: list[tuple[str, str, str, str, str, str, int]] = [
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
        # ⚠️ 锚点必须唯一：`SHARD_COUNT: "4"` 在 ci.yml 出现**两次**（unit + integration）
        # ⇒ 用 unit 块特有的上一行注释（「空分片 ⇒ 收全树 ⇒ 假绿」）消歧，
        #    只钉 **unit** job 这一处（守卫断言的就是 unit 步骤的 env）。
        '（空分片 ⇒ 收全树 ⇒ 假绿）。\n          SHARD_COUNT: "4"',
        '（空分片 ⇒ 收全树 ⇒ 假绿）。\n          SHARD_COUNT: "5"',
        "app/tests/unit/test_ci_integration_step.py::test_unit_step_declares_shard_count_matching_matrix",
        1,
    ),
    (
        "M27",
        "backend",
        "app/utils/ci_surface.py",
        '    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log|judge|report|flakes|conftest_chain)\\.py$",',
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
    # ---------- M29–M36：2026-11-07「净变更 + 基线可信性 + 树解析 fail-closed」批 ----------
    (
        "M29",
        "backend",
        "app/utils/ci_failure_set.py",
        "    ciChanges = detectCiSurfaceChanges(surface.netChangedFiles)",
        "    ciChanges = detectCiSurfaceChanges(surface.changedFiles)",
        (
            "app/tests/unit/test_ci_failure_set_surface.py::"
            "test_main_returns_six_when_ci_surface_differs_but_is_not_attributable_to_the_pr"
        ),
        1,
    ),
    (
        "M30",
        "backend",
        "app/utils/ci_failure_set.py",
        "    changedFiles = None if surface.error is not None else surface.netChangedFiles",
        "    changedFiles = None if surface.error is not None else surface.changedFiles",
        (
            "app/tests/unit/test_ci_failure_set_surface.py::"
            "test_main_does_not_exempt_a_test_whose_file_changed_on_either_side"
        ),
        1,
    ),
    (
        "M31",
        "backend",
        "app/utils/ci_surface.py",
        "    trustError = _baseTrustError(repo, baseInfo)",
        "    trustError = None",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_rejects_a_base_run_that_is_not_a_push_run"
        ),
        1,
    ),
    (
        "M32",
        "backend",
        "app/utils/ci_gh.py",
        (
            '    if "truncated" not in parsed:\n'
            "        raise ValueError(\n"
            '            "Trees API 响应缺少 truncated 字段 ⇒ 无法证明清单完整 ⇒ 须人工确认"\n'
            "        )"
        ),
        '    if "truncated" not in parsed:\n        parsed["truncated"] = False',
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_missing_truncated_flag"
        ),
        1,
    ),
    (
        "M33",
        "backend",
        "app/utils/ci_gh.py",
        "    _assertTreeIsRecursive(directoryPaths, allPaths)",
        "    pass  # 变异：去掉「递归确实生效」的不变式检查",
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_non_recursive_tree"
        ),
        1,
    ),
    (
        "M34",
        "backend",
        "app/utils/ci_gh.py",
        "        if objType not in _TREE_ENTRY_TYPES:",
        "        if False:",
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_an_unknown_entry_type"
        ),
        1,
    ),
    (
        "M35",
        "backend",
        "app/utils/ci_gh.py",
        'f"repos/{repo}/compare/{baseSha}...{headSha}"',
        'f"repos/{repo}/compare/{baseSha}..{headSha}"',
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_fetchMergeBaseSha_calls_the_three_dot_compare_endpoint"
        ),
        1,
    ),
    (
        "M36",
        "backend",
        "app/utils/ci_gh.py",
        (
            '    raise ValueError(\n'
            '        "compare 响应里没有 merge_base_commit ⇒ 无法确定分叉点 ⇒ 须人工确认"\n'
            "    )"
        ),
        '    return "0" * 40',
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseMergeBaseSha_rejects_a_response_without_merge_base"
        ),
        1,
    ),
]
