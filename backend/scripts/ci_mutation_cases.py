"""CI 判据守卫的**变异清单**（纯数据，67 组 = 核心批 + N6 批）。

规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。

本文件只放**数据**：每组变异 = (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)。
跑者（改坏 → 跑用例 → 判三态）在 `backend/scripts/ci_mutation_evidence.py`。
M1–M36（继承批 + 模块拆分批 + workflow 批 + 净变更批）在 `ci_mutation_cases_core.py`，
本文件从 M37（N6 批）起 —— 两个文件都超 800 行上限才拆。

⚠️ **本文件与跑者同属「CI 相关面」**（改了它就能让守卫恒绿）
⇒ 必须同时进 `.github/CODEOWNERS` 与 `ci_surface.py` 的 `_CI_SURFACE_PATTERNS`。

用法（在仓库根或任意子目录执行均可，脚本按自身位置自动定位仓库根）：

    python3 backend/scripts/ci_mutation_evidence.py            # 全部 67 组
    python3 backend/scripts/ci_mutation_evidence.py M5 M6      # 只跑指定组
"""

from __future__ import annotations

from scripts.ci_mutation_cases_core import MUTATIONS_CORE

# (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)
MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = MUTATIONS_CORE + [
    # ---------- M37–M55：2026-11-07 N6「判定集合必须落在两次 run 实际跑的树上」批 ----------
    #
    # ⚠️ 背景（实测）：`ci.yml` 对 `pull_request` 触发，三个 `actions/checkout`
    # 都不带 `with: ref:` ⇒ 检出 `refs/pull/N/merge` ⇒ **PR 的 CI 跑的是
    # merge(main@run 时点, PR 头)**，不是 PR 头。
    #
    # ⇒ 第三版判定集合 = `A ∪ P`：
    #   `A` = main 在 `[M_base, M_T]` 的内容推进（`M_T` = PR run 事件派发那一刻 main 的 tip）
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
        "    return dispatched",
        "    return datetime.fromtimestamp(dispatched.timestamp() + 120, tz=UTC)",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_applies_no_grace_window"
        ),
        1,
    ),
    (
        "M49",
        "backend",
        "app/utils/ci_gh.py",
        (
            "    dispatched = _parseIsoTimestamp(createdAt)\n"
            "    if dispatched is None:\n"
            "        raise ValueError(\n"
            '            "PR run 的 created_at 缺失或形态异常"\n'
            '            " ⇒ 算不出 main 侧的取数上界 ⇒ 判定不出来"\n'
            "        )"
        ),
        (
            "    dispatched = _parseIsoTimestamp(createdAt)\n"
            "    if dispatched is None:\n"
            "        dispatched = datetime.now(tz=UTC)"
        ),
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_does_not_fall_back_to_run_started_at"
        ),
        1,
    ),
    (
        "M50",
        "backend",
        "app/utils/ci_surface.py",
        (
            "    try:\n"
            "        cutoff = mainTipCutoff(createdAt=prInfo.createdAt)\n"
            "    except ValueError as error:\n"
            "        return None, sanitizeForReport(str(error))"
        ),
        (
            "    try:\n"
            "        cutoff = mainTipCutoff(createdAt=prInfo.createdAt)\n"
            "    except ValueError:\n"
            "        cutoff = mainTipCutoff(createdAt=prInfo.runStartedAt)"
        ),
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_fails_closed_when_the_pr_run_has_no_timestamp"
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
        "        cutoff = mainTipCutoff(createdAt=prInfo.createdAt)",
        "        cutoff = mainTipCutoff(createdAt=prInfo.runStartedAt)",
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
        "def mainTipCutoff(*, createdAt: str) -> datetime:",
        "def mainTipCutoff(createdAt: str) -> datetime:",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_mainTipCutoff_requires_explicitly_named_timestamps"
        ),
        1,
    ),
    # ---------- M59–M65：2026-11-07 N6 第三版「两条腿取并集 + 祖先核对 + 主线回溯」批 ----------
    #
    # ⚠️ 背景：`M_T`（PR run 的 main 侧底座）有**两条**取法，各自都可能偏旧
    # ⇒ 单用一条会漏掉 main 的推进 ⇒ 两次 run 的 CI 定义可能其实不同
    # ⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ **fail-open**。
    # ⇒ `A` 腿取**两条腿的并集**；且基线必须是底座的**祖先**（否则 fail-closed）。
    (
        "M59",
        "backend",
        "app/utils/ci_surface.py",
        (
            "        byFirstParent = (\n"
            "            byRunBased\n"
            "            if firstParentTipSha == mainTipSha\n"
            "            else fetchNetChangedFiles(repo, baseSha, firstParentTipSha)\n"
            "        )"
        ),
        "        byFirstParent = byRunBased",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_union_of_the_legs_is_actually_exercised"
        ),
        1,
    ),
    (
        "M60",
        "backend",
        "app/utils/ci_surface.py",
        "    if mergeBase != baseSha:",
        "    if False:",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_fails_closed_when_the_base_is_not_an_ancestor"
        ),
        1,
    ),
    (
        "M61",
        "backend",
        "app/utils/ci_surface.py",
        (
            "    except _FETCH_ERRORS as error:\n"
            '        return f"无法核对基线与 main 侧底座的祖先关系：'
            '{sanitizeForReport(str(error))}"'
        ),
        (
            "    except _FETCH_ERRORS:\n"
            "        return None"
        ),
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_resolveCiSurface_fails_closed_when_the_ancestor_check_cannot_be_run"
        ),
        1,
    ),
    (
        "M62",
        "backend",
        "app/utils/ci_gh.py",
        "            first = parents[0]",
        "            first = parents[-1]",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_fetchMainFirstParentTipAt_never_returns_a_side_commit"
        ),
        1,
    ),
    (
        "M63",
        "backend",
        "app/utils/ci_gh.py",
        "    for _ in range(_MAX_FIRST_PARENT_HOPS):",
        "    for _ in range(10 ** 9):",
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_fetchMainFirstParentTipAt_raises_when_the_chain_is_too_long"
        ),
        1,
    ),
    (
        "M64",
        "backend",
        "app/utils/ci_gh.py",
        (
            "        committed = _parseIsoTimestamp(dateRaw)\n"
            "        if committed is None:\n"
            "            raise ValueError("
            'f"commit 的 committer.date 形态异常：{sanitizeForReport(dateRaw)}")'
        ),
        (
            "        committed = _parseIsoTimestamp(dateRaw)\n"
            "        if committed is None:\n"
            '            committed = _parseIsoTimestamp("1970-01-01T00:00:00Z")'
        ),
        (
            "app/tests/unit/test_ci_main_tip.py::"
            "test_fetchMainFirstParentTipAt_raises_on_a_malformed_history"
        ),
        1,
    ),
    (
        "M65",
        "repo",
        ".github/workflows/merge-gate.yml",
        '.status == "completed" and ',
        "",
        (
            "app/tests/unit/test_ci_merge_gate_workflow.py::"
            "test_merge_gate_base_run_query_rechecks_the_run_fields_in_jq"
        ),
        1,
    ),
    # ---------- M66–M67：2026-11-08 第三轮安全评审 C-1「vitest workspace 自动发现绕过链」批 ----------
    (
        "M66",
        "backend",
        "app/utils/ci_surface.py",
        'r"^frontend/(?:.*/)?vitest\\.(?:workspace|projects)\\.(?:ts|mts|cts|js|mjs|cjs|json)$",',
        "",
        (
            "app/tests/unit/test_ci_surface_frontend_reverse.py::"
            "test_vitest_workspace_and_projects_files_are_ci_surfaces"
        ),
        1,
    ),
    (
        "M67",
        "backend",
        "app/tests/unit/test_ci_surface_frontend_reverse.py",
        "or _WORKSPACE_NAME_PATTERN.match(name)",
        "or False",
        (
            "app/tests/unit/test_ci_surface_frontend_reverse.py::"
            "test_the_workspace_scanner_is_not_vacuous"
        ),
        1,
    ),
]
