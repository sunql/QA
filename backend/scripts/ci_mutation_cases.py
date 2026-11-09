"""CI 判据守卫的**变异清单**（纯数据，278 组 = 核心批 + N6 批 + 第四/五轮批 + 第六–九轮复审批 + 第十二轮收口批 + 第十三/十四轮收口批 + 第十五/十六轮收口批 + 第十八轮收口批 + 第二十轮收口批 + 第三十轮修复批 + 第三十二轮修复批）。

规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。

本文件只放**数据**：每组变异 = (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)。
跑者（改坏 → 跑用例 → 判三态）在 `backend/scripts/ci_mutation_evidence.py`。
M1–M36（继承批 + 模块拆分批 + workflow 批 + 净变更批）在 `ci_mutation_cases_core.py`，
本文件从 M37（N6 批）起，M100–M121（第十三/十四轮收口批）在
`ci_mutation_cases_r14.py`，M122–M136（第十五/十六轮收口批）在
`ci_mutation_cases_r16.py`，M137–M151（第十八轮收口批）在
`ci_mutation_cases_r18.py`，M152–M174（第二十轮收口批）在
`ci_mutation_cases_r20.py`，M175 起（第二十二轮收口批）在
`ci_mutation_cases_r21.py` —— 文件超 800 行上限才拆。

⚠️ **本文件与跑者同属「CI 相关面」**（改了它就能让守卫恒绿）
⇒ 必须同时进 `.github/CODEOWNERS` 与 `ci_surface.py` 的 `_CI_SURFACE_PATTERNS`。

用法（在仓库根或任意子目录执行均可，脚本按自身位置自动定位仓库根）：

    python3 backend/scripts/ci_mutation_evidence.py            # 全部 278 组
    python3 backend/scripts/ci_mutation_evidence.py M5 M6      # 只跑指定组
"""

from __future__ import annotations

from scripts.ci_mutation_cases_core import MUTATIONS_CORE
from scripts.ci_mutation_cases_r14 import R14_MUTATIONS
from scripts.ci_mutation_cases_r16 import R16_MUTATIONS
from scripts.ci_mutation_cases_r18 import R18_MUTATIONS
from scripts.ci_mutation_cases_r20 import R20_MUTATIONS
from scripts.ci_mutation_cases_r21 import R21_MUTATIONS
from scripts.ci_mutation_cases_r23 import R23_MUTATIONS
from scripts.ci_mutation_cases_r24 import R24_MUTATIONS
from scripts.ci_mutation_cases_r26 import R26_MUTATIONS
from scripts.ci_mutation_cases_r28 import R28_MUTATIONS
from scripts.ci_mutation_cases_r30 import R30_MUTATIONS
from scripts.ci_mutation_cases_r32 import R32_MUTATIONS

# (编号, 作用域, 文件, 原文, 变异后, 期望转红的用例, occurrence)
MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = (
    MUTATIONS_CORE + R14_MUTATIONS + R16_MUTATIONS + R18_MUTATIONS + R20_MUTATIONS
    + R21_MUTATIONS + R23_MUTATIONS + R24_MUTATIONS + R26_MUTATIONS
    + R28_MUTATIONS + R30_MUTATIONS + R32_MUTATIONS + [

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
    # ---------- M68：2026-11-08 第四轮代码评审 HIGH-1「npm-shrinkwrap 优先于 lockfile」批 ----------
    # npm 的 arborist：`data = sw || lock` ⇒ 有 shrinkwrap 则 package-lock.json **被忽略**
    # ⇒ 只新增一个 shrinkwrap 就能换掉前端依赖树 ⇒ vitest 版本可能整个变化
    # ⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ fail-open。
    (
        "M68",
        "backend",
        "app/utils/ci_surface.py",
        'r"^frontend/(?:package\\.json|package-lock\\.json|npm-shrinkwrap\\.json|pnpm-lock\\.yaml|pnpm-workspace\\.yaml)$",',
        'r"^frontend/(?:package\\.json|package-lock\\.json|pnpm-lock\\.yaml|pnpm-workspace\\.yaml)$",',
        (
            "app/tests/unit/test_ci_surface_frontend_reverse.py::"
            "test_the_frontend_ci_files_are_actually_flagged"
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
    # ---------- M69–M76：2026-11-08 第五轮安全评审「环境定义杠杆」批 ----------
    #
    # ⚠️ 背景：CI 在 unit / integration 两个 job 前**真正执行或加载**的文件里，
    # 有一批不在 CI 相关面 ⇒ 只新增/改写一个文件就能换掉测试环境
    # ⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ fail-open。
    # - HIGH-1：`backend/alembic/env.py` —— CI 两个 job 前都跑 `alembic upgrade head`，
    #   env.py 是真正建 engine、跑迁移的任意 Python（可在 run_migrations 后追加 DDL）；
    # - MEDIUM-2：`backend/.env` —— conftest `load_dotenv(override=True)` ⇒ 覆盖 CI 注入
    #   的全部变量（`.gitignore` 拦不住 `git add -f`）；
    # - MEDIUM-3：`backend/.python-version` / `uv.toml` —— uv 拿它选解释器，
    #   盖过 setup-python 的 3.13；
    # - LOW-5：`_neo4j_support` / `_testapp` / seed 脚本 —— 塑形测试库与 registry；
    # - LOW-6：M-3 拆分后 `cases_core` 没有正向用例钉住。
    (
        "M69",
        "backend",
        "app/utils/ci_surface.py",
        # 第十五次重锚（第三十二轮）：新增 `_r32` 批后 pattern 行随之加宽。
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16|_r18|_r20|_r21|_r23|_r24|_r26|_r28|_r30|_r32)?)\\.py$",',
        'r"^backend/scripts/ci_mutation_(?:evidence|cases)\\.py$",',
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file"
        ),
        1,
    ),
    (
        "M70",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/alembic/env\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M71",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/\\.env(?:\\.[\\w.-]+)?$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M72",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/\\.python-version$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M73",
        "backend",
        "app/utils/ci_surface.py",
        'r"^frontend/\\.env(?:\\.[\\w.-]+)?$",',
        "",
        (
            "app/tests/unit/test_ci_surface_frontend_reverse.py::"
            "test_the_frontend_ci_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M74",
        "backend",
        "app/tests/unit/test_ci_surface_frontend_reverse.py",
        "or _ENV_NAME_PATTERN.match(name)",
        "or False",
        (
            "app/tests/unit/test_ci_surface_frontend_reverse.py::"
            "test_the_env_scanner_is_not_vacuous"
        ),
        1,
    ),
    (
        "M75",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/tests/(?:_pg_support|_neo4j_support|_testapp|milvus_isolation)\\.py$",',
        'r"^backend/app/tests/(?:_pg_support|milvus_isolation)\\.py$",',
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M76",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/(?:scripts/)?seed_[\\w.-]+\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    # ---------- M77–M80：2026-11-08 第六轮复审「环境杠杆补漏」批 ----------
    # - HIGH-1：迁移脚本本体 `backend/alembic/versions/*.py` —— `alembic upgrade head`
    #   执行每个 revision 的 `upgrade()`（pytest 收集前的任意 Python，139 个文件，
    #   工程规则要求每个 ORM 变更都带迁移 ⇒ 常规路径）；
    # - MEDIUM-1：仓库根 `uv.toml` —— uv 沿工作目录父链发现配置（CI 在 backend/ 跑 uv）；
    # - MEDIUM-2：`backend/seed_ontology.py` —— conftest **模块级** import ⇒ 收集前执行；
    # - LOW-1：`backend/scripts/kpi_metric_db_guard.py` —— `_pg_support.assertTestDatabase`
    #   的依赖（改它 = 改「测试必须连哪个库」的守卫口径）。
    (
        "M77",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/alembic/versions/[^/]+\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M78",
        "backend",
        "app/utils/ci_surface.py",
        'r"^uv\\.toml$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M79",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/(?:scripts/)?seed_[\\w.-]+\\.py$",',
        'r"^backend/scripts/seed_[\\w.-]+\\.py$",',
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M80",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/scripts/kpi_metric_db_guard\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    # ---------- M81–M83：2026-11-08 第七轮复审「收集前执行的包 init + revision 文件名形态」批 ----------
    # - HIGH-1：`backend/scripts/__init__.py` —— 任何 `from scripts.… import …`
    #   （unit/integration conftest、_pg_support、单测）都先执行包 init ⇒ 收集前任意
    #   Python（注册 pytest 钩子 / 改 stdout 都行）；此前清单与 CODEOWNERS 双 MISS；
    # - HIGH-2：versions pattern 从 `[\w.-]+` **放宽**到 `[^/]+`（alembic 1.19.1 的
    #   `_only_source_rev_file` 只排除 `__init__`/`.#` 前缀 ⇒ `0141_foo+bar.py` 会被
    #   import 却逃出窄清单）。M77 只钉「删除」，钉不住「形态收窄」⇒ M82 把它改回
    #   窄形态必须转红；M77 锚点已同步为宽行原文（第七轮，同 M76 重锚的手法）；
    # - MEDIUM：`backend/app/tests/**/__init__.py` —— pytest prepend 导入链上的包
    #   init（现在 7 个空文件，收集前执行 ⇒ 塞 import 时代码 = conftest 同级杠杆）。
    (
        "M81",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/scripts/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M82",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/alembic/versions/[^/]+\\.py$",',
        'r"^backend/alembic/versions/[\\w.-]+\\.py$",',
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M83",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/tests/(?:[^/]+/)*__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    # ---------- M84：2026-11-08 第八轮复审「backend 包 init」批 ----------
    # - HIGH-1：`backend/__init__.py` 现在**不存在**，但 PR 新增这一个文件就把
    #   pytest prepend 的 `resolve_package_path` 顶到仓库根 ⇒ conftest 改以
    #   `backend.app.tests.unit.conftest` 导入 ⇒ 本文件在任何收集之前执行
    #   （现有 `app.*` 导入在该文件注入 sys.path 后照常工作）—— 与
    #   `tests/**/__init__` 同一机制、链上更高一层，且是**全新路径**、此前
    #   清单与 CODEOWNERS 双 MISS。
    (
        "M84",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    # ---------- M85–M91：2026-11-08 第九轮复审「app 包 init + 派生式 import 链守卫」批 ----------
    # - HIGH-1：`backend/app/__init__.py` —— conftest 以 `app.tests.unit.conftest` 导入
    #   ⇒ 这是 pytest prepend 链上**现存且每次都执行**的最高一层包 init（第八轮钉了
    #   尚不存在的 `backend/__init__.py`，却漏了这层真在链上的）。
    # - MEDIUM-1：conftest 模块级 import 的 harness 配置文件（settings / engine /
    #   crypto）import 即执行；M85–M90 逐条删除新 pattern 行 ⇒ lever 测试须红。
    # - M91（结构性收口）：`ci_conftest_chain.preCollectionImportPaths` 是派生式守卫的
    #   数据源 ⇒ 掏空它（返回空 dict）⇒ `test_ci_conftest_chain` 的派生守卫必须红。
    (
        "M85",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M86",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/config\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M87",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/infrastructure/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M88",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/infrastructure/database\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M89",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/infrastructure/security/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M90",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/infrastructure/security/crypto\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M91",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    return result",
        "    return {}",
        (
            "app/tests/unit/test_ci_conftest_chain.py::"
            "test_every_pre_collection_conftest_import_is_ci_surface_or_reviewed"
        ),
        1,
    ),
    # ---------- M92–M99：2026-11-08 第十二轮复审收口「祖先包 init 链 + 传递闭包内容不变式」批 ----------
    # - HIGH-1（M92 + M96–M99）：祖先链解析只解析叶子 ⇒ `app/domain/__init__.py`
    #   等中间包 init 全部逃出派生守卫；4 条新 pattern 行逐条删除 ⇒ lever 测试须红。
    # - HIGH-2（M93）：传递闭包 BFS 掏空 ⇒ 内容不变式守卫对空集空转（正向钉：
    #   len ≥ 60 + `backend/app/domain/models.py` ∈ 闭包 ⇒ 塌缩即红）。
    # - MEDIUM-3（M95）：If/Try 分支递归与 `pytest_plugins` 派生被关 ⇒ 合成树守卫红。
    # - MEDIUM-4 / M94：钩子检测器掏空 ⇒ 内容不变式对一切文件空转；
    #   检测器自身的双向单元钉（钩子必须认出、纯构造必须放行）须红。
    (
        "M92",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    for i in range(1, len(parts)):",
        "    for i in range(0, 0):",
        (
            "app/tests/unit/test_ci_conftest_chain.py::"
            "test_derivation_includes_parent_package_inits"
        ),
        1,
    ),
    (
        "M93",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    return tuple(sorted(closure))",
        "    return ()",
        (
            "app/tests/unit/test_ci_conftest_chain.py::"
            "test_transitive_closure_files_are_surface_or_hook_free"
        ),
        1,
    ),
    (
        "M94",
        "backend",
        "app/utils/ci_conftest_chain.py",
        # 第二十二轮重锚：裸 `    return tuple(hooks)` 现在出现 2 次
        #（_instantiationHooks 与 _findImportTimeHooksImpl 的收尾），
        # occurrence 1 会被新函数偷走（锚漂移）——用前置行钉死 Impl 收尾。
        # mut 保留 add 行：删掉会让前置 if 失去函数体 ⇒ IndentationError
        #（那是「炸穿」不是「放行」，不算有效变异取证）。
        "\n".join(
            [
                '            add(d.lineno, f"模块级 {d.name}（PEP 562 收集前钩子）")',
                "    return tuple(hooks)",
            ]
        ),
        "\n".join(
            [
                '            add(d.lineno, f"模块级 {d.name}（PEP 562 收集前钩子）")',
                "    return ()",
            ]
        ),
        (
            "app/tests/unit/test_ci_conftest_chain.py::"
            "test_import_time_hook_detector_is_not_vacuous"
        ),
        1,
    ),
    (
        "M95",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        elif isinstance(node, _BRANCH_TYPES):\n            yield from _moduleScopeStatements(node.body)",
        "        elif False:\n            yield from _moduleScopeStatements(node.body)",
        (
            "app/tests/unit/test_ci_conftest_chain.py::"
            "test_scope_recursion_and_plugins_are_derived"
        ),
        1,
    ),
    (
        "M96",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/domain/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M97",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/models/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M98",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/services/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
    (
        "M99",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/infrastructure/llm/__init__\\.py$",',
        "",
        (
            "app/tests/unit/test_ci_surface.py::"
            "test_the_round5_environment_lever_files_are_actually_flagged"
        ),
        1,
    ),
])
