"""CI 判据守卫的**变异清单**（纯数据，58 组）。

规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。

本文件只放**数据**：每组变异 = (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)。
跑者（改坏 → 跑用例 → 判三态）在 `backend/scripts/ci_mutation_evidence.py`。

⚠️ **本文件与跑者同属「CI 相关面」**（改了它就能让守卫恒绿）
⇒ 必须同时进 `.github/CODEOWNERS` 与 `ci_surface.py` 的 `_CI_SURFACE_PATTERNS`。

用法（在仓库根或任意子目录执行均可，脚本按自身位置自动定位仓库根）：

    python3 backend/scripts/ci_mutation_evidence.py            # 全部 58 组
    python3 backend/scripts/ci_mutation_evidence.py M5 M6      # 只跑指定组
"""

from __future__ import annotations

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
        '    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log|judge|report|flakes)\\.py$",',
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
    # ---------- M37–M55：2026-11-07 N6「判定集合必须落在两次 run 实际跑的树上」批 ----------
    #
    # ⚠️ 背景（实测）：`ci.yml` 对 `pull_request` 触发，三个 `actions/checkout`
    # 都不带 `with: ref:` ⇒ 检出 `refs/pull/N/merge` ⇒ **PR 的 CI 跑的是
    # merge(main@run 时点, PR 头)**，不是 PR 头。
    #
    # ⇒ 第三版判定集合 = `A ∪ P`：
    #   `A` = main 在 `[M_base, M_T]` 的内容推进（`M_T` = PR run 开跑那一刻 main 的 tip）
    #   `P` = PR 自己改的文件（vs `merge_base(M_T, PR 头)`）
    #
    # ⚠️ 为什么不能只算其中一边：
    # - 只算 `P` ⇒ main 改了 CI 文件、PR 没跟上 ⇒ **fail-open**；
    # - 只算 `A` ⇒ PR 自己改的 CI 文件**看不见** ⇒ **假放行**。
    (
        "M37",
        "backend",
        "app/utils/ci_surface.py",
        "    judgment = tuple(sorted(set(mainAdvance) | set(prChanged)))",
        "    judgment = tuple(sorted(set(prChanged)))",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_judges_on_the_union_of_main_advance_and_pr_changes"
        ),
        1,
    ),
    (
        "M38",
        "backend",
        "app/utils/ci_surface.py",
        "    judgment = tuple(sorted(set(mainAdvance) | set(prChanged)))",
        "    judgment = tuple(sorted(set(mainAdvance)))",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_flags_a_ci_file_the_pr_itself_changed"
        ),
        1,
    ),
    (
        "M39",
        "backend",
        "app/utils/ci_surface.py",
        (
            "    try:\n"
            "        runBasedSha = fetchMainTipShaAt(repo, cutoffIso)\n"
            "    except _FETCH_ERRORS as error:\n"
            "        return None, (\n"
            '            "无法确定 PR run 的 main 侧底座（PR 的 CI 检出的是 `refs/pull/N/merge`，"\n'
            '            f"不是 PR 头）：{sanitizeForReport(str(error))}"\n'
            "        )"
        ),
        (
            "    try:\n"
            "        runBasedSha = fetchMainTipShaAt(repo, cutoffIso)\n"
            "    except _FETCH_ERRORS:\n"
            "        runBasedSha = prInfo.headSha"
        ),
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_fails_closed_when_the_main_tip_is_unknown"
        ),
        1,
    ),
    (
        "M40",
        "backend",
        "app/utils/ci_gh.py",
        '    if parsed["truncated"] is not False:',
        '    if parsed["truncated"] is not False and parsed["truncated"] != "false":',
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_string_false_truncated_flag"
        ),
        1,
    ),
    (
        "M41",
        "backend",
        "app/utils/ci_gh.py",
        '    if parsed["truncated"] is not False:',
        '    if parsed["truncated"]:',
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_zero_truncated_flag"
        ),
        1,
    ),
    (
        "M42",
        "backend",
        "app/utils/ci_gh.py",
        (
            '            ancestor = "/".join(parts[:index])\n'
            "            if ancestor not in pathSet:"
        ),
        (
            '            ancestor = "/".join(parts[:index])\n'
            "            if False:"
        ),
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_response_missing_ancestor_entries"
        ),
        1,
    ),
    (
        "M43",
        "backend",
        "app/utils/ci_gh.py",
        "    if not hasNestedPath:",
        "    if False:",
        (
            "app/tests/unit/test_ci_gh.py::"
            "test_parseTreePathShas_rejects_a_fully_flat_response"
        ),
        1,
    ),
    (
        "M44",
        "backend",
        "app/utils/ci_gh.py",
        '    if str(run.get("event") or "") != "push":',
        "    if False:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_parseMainTipSha_ignores_a_pull_request_run"
        ),
        1,
    ),
    (
        "M45",
        "backend",
        "app/utils/ci_gh.py",
        '    if str(run.get("head_branch") or "") != "main":',
        "    if False:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_parseMainTipSha_ignores_a_run_on_another_branch"
        ),
        1,
    ),
    (
        "M46",
        "repo",
        ".github/workflows/merge-gate.yml",
        "and .created_at <= $cutoff",
        "",
        (
            "app/tests/unit/test_ci_merge_gate_workflow.py::"
            "test_merge_gate_base_run_is_bounded_by_the_pr_run_dispatch_time"
        ),
        1,
    ),
    (
        "M47",
        "backend",
        "app/utils/ci_failure_set.py",
        "    if surface.baseSha == surface.mainTipSha:",
        "    if surface.baseSha != surface.mainTipSha:",
        (
            "app/tests/unit/test_ci_main_tip_note.py::"
            "test_main_does_not_report_a_mismatch_when_the_base_is_the_same_commit"
        ),
        1,
    ),
    (
        "M48",
        "backend",
        "app/utils/ci_gh.py",
        (
            "    started = _parseIsoTimestamp(createdAt)\n"
            "    if started is None:\n"
            "        started = _parseIsoTimestamp(runStartedAt)"
        ),
        (
            "    started = _parseIsoTimestamp(runStartedAt)\n"
            "    if started is None:\n"
            "        started = _parseIsoTimestamp(createdAt)"
        ),
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_prefers_created_at"
        ),
        1,
    ),
    (
        "M49",
        "backend",
        "app/utils/ci_gh.py",
        "    return started + timedelta(seconds=_MAIN_TIP_GRACE_SECONDS)",
        "    return started",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_applies_the_grace_window"
        ),
        1,
    ),
    (
        "M50",
        "backend",
        "app/utils/ci_gh.py",
        (
            "    if started is None:\n"
            "        raise ValueError(\n"
            '            "PR run 的 created_at / run_started_at 都缺失或形态异常"\n'
            '            " ⇒ 算不出 main 侧的取数上界 ⇒ 判定不出来"\n'
            "        )"
        ),
        (
            "    if started is None:\n"
            "        return datetime.now(tz=UTC)"
        ),
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_raises_when_both_timestamps_are_missing"
        ),
        1,
    ),
    (
        "M51",
        "backend",
        "app/utils/ci_gh.py",
        "    if headRepo.lower() != repo.lower():",
        "    if False:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_parseMainTipSha_ignores_a_run_from_another_repository"
        ),
        1,
    ),
    (
        "M52",
        "backend",
        "app/utils/ci_gh.py",
        "    if created > cutoff:",
        "    if False:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_parseMainTipSha_ignores_a_run_after_the_cutoff"
        ),
        1,
    ),
    (
        "M53",
        "backend",
        "app/utils/ci_gh.py",
        (
            "    if best is None:\n"
            "        raise ValueError(\n"
            '            f"找不到不晚于 {sanitizeForReport(cutoffIso)} 的 main push run"\n'
            '            " ⇒ 算不出 PR run 的 main 侧底座 ⇒ 判定不出来"\n'
            "        )"
        ),
        (
            "    if best is None:\n"
            '        return "0" * 40'
        ),
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_parseMainTipSha_raises_when_no_run_qualifies"
        ),
        1,
    ),
    (
        "M54",
        "backend",
        "scripts/ci_mutation_evidence.py",
        (
            "    if completed.returncode != _PYTEST_EXIT_ASSERTION_FAILED:\n"
            "        return (\n"
            '            "inconclusive",\n'
            '            f"pytest 退出码 {completed.returncode}（不是 {_PYTEST_EXIT_ASSERTION_FAILED}）"\n'
            '            f" ⇒ 用例没跑起来 ⇒ 不能算「转红」\\n{output}",\n'
            "        )"
        ),
        (
            "    if completed.returncode != 0:\n"
            '        return "red", output'
        ),
        (
            "app/tests/unit/test_ci_mutation_evidence.py::"
            "test_run_case_treats_a_non_assertion_exit_code_as_inconclusive"
        ),
        1,
    ),
    (
        "M55",
        "backend",
        "app/utils/ci_surface.py",
        "        unattributedCiFiles=tuple(sorted(set(netCiFiles) - set(prCiFiles))),",
        "        unattributedCiFiles=tuple(sorted(set(netCiFiles))),",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_flags_a_ci_file_the_pr_itself_changed"
        ),
        1,
    ),
    # ---------- M56–M58：2026-11-07 CRITICAL-1「时间锚点 = 事件派发时刻（created_at）」批 ----------
    (
        "M56",
        "repo",
        ".github/workflows/merge-gate.yml",
        "PR_RUN_CREATED_AT: ${{ github.event.workflow_run.created_at }}",
        "PR_RUN_CREATED_AT: ${{ github.event.workflow_run.run_started_at }}",
        (
            "app/tests/unit/test_ci_merge_gate_workflow.py::"
            "test_merge_gate_uses_created_at_not_run_started_at"
        ),
        1,
    ),
    (
        "M57",
        "backend",
        "app/utils/ci_surface.py",
        "            createdAt=prInfo.createdAt, runStartedAt=prInfo.runStartedAt",
        "            createdAt=prInfo.runStartedAt, runStartedAt=prInfo.createdAt",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_derives_the_main_tip_cutoff_from_created_at"
        ),
        1,
    ),
    (
        "M58",
        "backend",
        "app/utils/ci_gh.py",
        "def mainTipCutoff(*, createdAt: str, runStartedAt: str) -> datetime:",
        "def mainTipCutoff(createdAt: str, runStartedAt: str) -> datetime:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_requires_explicitly_named_timestamps"
        ),
        1,
    ),
]
