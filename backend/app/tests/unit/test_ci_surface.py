"""「CI 相关面」判定：两次 run 跑的代码在 CI 相关面上是否不同。

攻击路径：PR 用 `pytest_collection_modifyitems`（conftest）把失败用例跳过、
或在 workflow 里加 `--deselect`、或在分片清单里把含新失败的测试文件踢出去
⇒ 失败用例**根本不产生 FAILED 行** ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」
⇒ 而 GitHub 上 CI 还**全绿** ⇒ 没有二次防线。

⚠️ 2026-11-07 N6 第三版：判定集合 = `A ∪ P`
（`A` = main 在 `[M_base, M_T]` 的内容推进、`P` = PR 自己改的文件），
**不是**「PR 头 vs 基线」，也**不是**「测试合并 commit vs 基线」。
两个集合分开保存**只用于措辞**（谁的责任、要不要重新跑 CI）。
设计推导见 `app/utils/ci_surface.py` 的模块 docstring。
"""

from __future__ import annotations

from pathlib import Path

from app.utils import ci_gh, ci_surface

_BASE_RUN_ID = "111"
_PR_RUN_ID = "222"


def test_detectCiSurfaceChanges_flags_workflow_and_conftest() -> None:
    """CI 相关面 = workflow / conftest / pytest 配置 / 分片清单 / 判定工具自身。"""
    changed = (
        ".github/workflows/ci.yml",
        "backend/conftest.py",
        "backend/app/tests/integration/conftest.py",
        "backend/pyproject.toml",
        "backend/app/utils/ci_shard.py",
        "backend/app/utils/ci_failure_set.py",
        "backend/app/services/chat_service.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert ".github/workflows/ci.yml" in flagged
    assert "backend/conftest.py" in flagged
    assert "backend/app/tests/integration/conftest.py" in flagged
    assert "backend/pyproject.toml" in flagged
    assert "backend/app/utils/ci_shard.py" in flagged
    assert "backend/app/utils/ci_failure_set.py" in flagged
    assert "backend/app/services/chat_service.py" not in flagged


def test_detectCiSurfaceChanges_covers_every_tool_module() -> None:
    """判定工具自身的模块也在「CI 相关面」里 ⇒ 改判定逻辑必须人工确认。"""
    changed = (
        "backend/app/utils/ci_text.py",
        "backend/app/utils/ci_gh.py",
        "backend/app/utils/ci_surface.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_covers_judgment_and_report_modules() -> None:
    """**判定模块**（`ci_judge.py`）与**报告模块**（`ci_report.py`）也必须算 CI 相关面。

    ⚠️ 这两个模块是判据工具**拆分时新增**的 —— 拆完必须同步进相关面清单，
    否则「拆分」这个动作本身就凿出了一个新的洞：

    - 改 `ci_judge.py` 就能改「什么算跑完了 / 什么算不可信」⇒ 例如把
      `_COMPLETED_STATUSES` 放宽 ⇒ **被取消的 job**（失败集合是空的）
      被判成「可用」⇒ 参与对差 ⇒ 假「净新增 0」；
    - 改 `ci_report.py` 就能把「净新增失败用例」清单从报告里抹掉
      ⇒ 操作者看到「可合并」却看不到新增了哪些 ⇒ 「把失败藏起来」。

    ⇒ 工具的**每一个**模块都必须在清单里。
    """
    changed = (
        "backend/app/utils/ci_judge.py",
        "backend/app/utils/ci_report.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_every_ci_tool_module_is_in_the_surface_list() -> None:
    """工具目录里**每一个** `ci_*.py` 都必须在相关面清单里。

    枚举式清单的通病：新增模块时忘了把它加进去 ⇒ 守卫**静默失效**
    （本批就踩过：`ci_judge.py` / `ci_report.py` 是拆分时新增的，
    一开始没进清单 ⇒ 改判定逻辑不会被判「不可比」）。

    ⇒ 改成**从文件系统反查**：清单漏了谁，本用例直接转红。
    """
    toolDir = Path(__file__).resolve().parents[2] / "utils"
    moduleNames = sorted(path.name for path in toolDir.glob("ci_*.py"))
    assert moduleNames, "没找到任何 ci_*.py 模块（路径写错了？）"

    expected = {f"backend/app/utils/{name}" for name in moduleNames}
    flagged = ci_surface.detectCiSurfaceChanges(sorted(expected))

    assert set(flagged) == expected


def test_detectCiSurfaceChanges_ignores_unrelated_files() -> None:
    assert ci_surface.detectCiSurfaceChanges(("backend/app/services/chat_service.py",)) == ()


def test_detectCiSurfaceChanges_flags_conftest_at_any_depth() -> None:
    """**任何深度**的 `conftest.py` 都必须算 CI 相关面。

    旧实现只枚举了 `backend/conftest.py` 与 `backend/app/tests/**/conftest.py`
    ⇒ 漏掉 `backend/app/conftest.py` —— 而 pytest 会从 rootdir 往下**逐层**
    收集 conftest ⇒ 那一层的 `pytest_collection_modifyitems` 对
    `backend/app/tests/**` 全部生效 ⇒ PR 在那里剔除失败用例
    ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」⇒ 而 CI 还全绿。

    ⇒ 判定必须改成**正向规则**（basename 是 conftest.py 就算），
    而不是枚举「今天有哪几个 conftest」。
    """
    changed = (
        "backend/app/conftest.py",
        "backend/app/tests/unit/conftest.py",
        "backend/conftest.py",
        "conftest.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_pytest_config_at_any_depth() -> None:
    """`pytest.ini` / `setup.cfg` / `tox.ini` 同样按**任意深度**判定。

    这些文件能改 `testpaths` / `addopts`（如加 `--deselect`）⇒ 失败用例
    根本不产生 FAILED 行 ⇒ 与 conftest 同族。
    """
    changed = (
        "backend/pytest.ini",
        "backend/app/tests/unit/pytest.ini",
        "backend/app/tests/integration/setup.cfg",
        "backend/tox.ini",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_uv_lock() -> None:
    """`backend/uv.lock` 改了依赖版本 ⇒ 测试环境变了 ⇒ 失败集合不再可比。"""
    flagged = ci_surface.detectCiSurfaceChanges(("backend/uv.lock",))

    assert flagged == ("backend/uv.lock",)


def test_detectCiSurfaceChanges_flags_the_mutation_evidence_script() -> None:
    """**变异取证脚本**属于 CI 相关面 ⇒ 改它就能让守卫恒绿。

    攻击路径：`ci_mutation_evidence.py` 决定「哪些变异算转红」
    ⇒ 把判据从「pytest 退出码 1」放宽成「退出码非 0」
    ⇒ 收集失败（退出码 2）也算「转红」⇒ 取证全绿 ⇒ 守卫形同虚设。

    ⇒ 它与守卫用例**同族** ⇒ 必须进 CI 相关面 + CODEOWNERS。

    ⚠️ 清单（`ci_mutation_cases.py`，纯数据）**同族**：改清单可以把某组变异
    指向一个没人守的地方 ⇒ 照样恒绿 ⇒ 两个文件都要命中。
    """
    for relPath in (
        "backend/scripts/ci_mutation_evidence.py",
        "backend/scripts/ci_mutation_cases.py",
    ):
        flagged = ci_surface.detectCiSurfaceChanges((relPath,))

        assert flagged == (relPath,), f"{relPath} 没被算进 CI 相关面"


def test_detectCiSurfaceChanges_flags_flake_whitelist_data_file() -> None:
    """**白名单数据文件**本身也属于 CI 相关面。

    白名单直接决定「哪些失败用例不作判据」⇒ 改它 = 改判据
    ⇒ 与工具模块同族：PR 改白名单 ⇒ 退出码 6 ⇒ 须人工确认。

    兜底不止这一层：闸门 workflow 由 `workflow_run` 触发 ⇒ checkout 检出的
    是**默认分支** ⇒ PR 分支里的白名单改动**根本不生效**。
    """
    changed = (".github/ci-known-flaky-tests.json",)

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_guard_test_files() -> None:
    """**守卫用例本身**属于 CI 相关面 ⇒ 改守卫 = 解除守卫。

    攻击路径：PR 把「拒绝文件级 node ID」那条断言删掉 ⇒ 白名单就能塞进
    文件级 ID ⇒ 一个文件的失败全部豁免 ⇒ 而**守卫自己不会再红**
    （断言被删了）⇒ CI 全绿 ⇒ 没有二次防线。

    ⇒ 这两类文件（守卫用例 + 授权面）是这条链上**仅剩的人工检查点**
    ⇒ 必须在 CI 相关面里。
    """
    changed = (
        "backend/app/tests/unit/test_ci_flakes.py",
        "backend/app/tests/unit/test_ci_failure_set.py",
        "backend/app/tests/unit/test_ci_report.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_codeowners() -> None:
    """**CODEOWNERS** 属于 CI 相关面 ⇒ 改授权面 = 换复核人。

    个人仓库的 CODEOWNERS 没有强制力（装不了 users bypass）⇒ 它是**唯一**
    的人工复核点 ⇒ 把某条 CI 路径的复核人去掉 ⇒ 判据就没人把关了。
    """
    changed = (".github/CODEOWNERS",)

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_guard_test_files_at_any_depth() -> None:
    """守卫用例在**任意深度**都算 CI 相关面。

    旧清单只认 `backend/app/tests/unit/test_ci_*.py` ⇒ 把守卫挪到
    `backend/app/tests/integration/test_ci_x.py` 就同时逃出
    ①「CI 相关面」判定 与 ②CODEOWNERS 反查（后者也只扫 `unit/` 目录）
    ⇒ **改守卫 = 解除守卫**，而没人复核。

    ⇒ 与 conftest / pytest 配置同款纪律：**按 basename 判定，不枚举目录**。
    """
    changed = (
        "backend/app/tests/integration/test_ci_guard.py",
        "backend/app/tests/unit/test_ci_flakes.py",
        "backend/app/tests/e2e/test_ci_e2e_guard.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert set(flagged) == set(changed)


def test_detectCiSurfaceChanges_flags_the_shared_guard_support_module() -> None:
    """守卫用例的**共享替身**属于 CI 相关面 ⇒ 改替身 = 改守卫的观测对象。

    攻击路径：`_ci_gate_support.py` 提供 `stubShards` / `redLogWith` 等替身
    ⇒ PR 把 `redLogWith` 改成**永远返回空失败集合**
    ⇒ 所有「失败集合对差」用例都看到「净新增 0」⇒ 守卫**恒绿**
    ⇒ 而守卫自己不会再红（断言被替身骗过）⇒ 没有二次防线。

    ⇒ 替身文件与守卫用例**同族** ⇒ 必须一起进 CI 相关面。
    """
    changed = (
        "backend/app/tests/unit/_ci_gate_support.py",
        "backend/app/tests/unit/_agent_orchestration_runner_support.py",
    )

    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert "backend/app/tests/unit/_ci_gate_support.py" in flagged
    assert "backend/app/tests/unit/_agent_orchestration_runner_support.py" not in flagged


def test_renaming_a_ci_surface_file_is_still_detected(monkeypatch) -> None:
    """**改名**也算「改了 CI 相关面」—— 组合守卫（真实攻击路径）。

    2026-11-06 安全审查 H-1 实测：GitHub compare 对改名只给**一个**条目
    （新路径 `filename` + 旧路径 `previous_filename`）⇒ 旧实现只取 `filename`
    ⇒ 把 `.github/ci-known-flaky-tests.json` 改名成 `docs/notes.json`
    就能让它从改动清单里消失 ⇒ **静默绕过**退出码 6 的人工仲裁
    （实测：`flagged=False`）。

    ⇒ 现在的实现走**树对树**：改名 = 旧路径删除 + 新路径新增 ⇒ **两个路径都在清单里**
    ⇒ 旧路径不会消失 ⇒ 守卫照样命中。
    """
    trees = {
        "c" * 40: {".github/ci-known-flaky-tests.json": "1" * 40},
        "b" * 40: {"docs/notes.json": "2" * 40},
    }
    monkeypatch.setattr(ci_gh, "fetchMergeBaseSha", lambda repo, base, head: "c" * 40)
    monkeypatch.setattr(ci_gh, "fetchTreePathShas", lambda repo, sha: trees[sha])

    changed = ci_gh.fetchChangedFiles("sunql/QA", "a" * 40, "b" * 40)
    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert ".github/ci-known-flaky-tests.json" in flagged


# --------------------------------------------------------------------------
# 替身：两次 run 的 head 信息 + main 的 tip
# --------------------------------------------------------------------------


def _baseInfo(**overrides) -> ci_gh.RunHeadInfo:
    """一个「可信的基线 run」（main 上的 push run）。"""
    defaults = {
        "headSha": "a" * 40,
        "headBranch": "main",
        "headRepo": "sunql/QA",
        "event": "push",
        "createdAt": "2026-11-06T10:00:00Z",
        "runStartedAt": "2026-11-06T10:00:00Z",
    }
    defaults.update(overrides)
    return ci_gh.RunHeadInfo(**defaults)


def _prInfo(**overrides) -> ci_gh.RunHeadInfo:
    """PR run 的 head 信息（= `pull_request` 事件）。"""
    defaults = {
        "headSha": "b" * 40,
        "headBranch": "feat/merge-gate-net-diff",
        "headRepo": "sunql/QA",
        "event": "pull_request",
        "createdAt": "2026-11-06T19:38:13Z",
        "runStartedAt": "2026-11-06T19:38:13Z",
    }
    defaults.update(overrides)
    return ci_gh.RunHeadInfo(**defaults)


def _patchRuns(
    monkeypatch,
    baseInfo: ci_gh.RunHeadInfo | None = None,
    prInfo: ci_gh.RunHeadInfo | None = None,
    mainTipSha: str = "e" * 40,
    firstParentTipSha: str | None = None,
) -> None:
    """把两次 run 的 head 信息与 main 的 tip（`M_T`）换成固定值。"""
    resolvedBase = baseInfo or _baseInfo()
    resolvedPr = prInfo or _prInfo()
    # ⚠️ **两条腿默认给不同的 sha**（2026-11-07 安全评审 HIGH-1）：
    # 两条腿若返回同一个 sha ⇒ 并集退化成单腿 ⇒ 「union 退化成单腿」这个
    # 回归**永远测不出来**（守卫恒绿）⇒ 那是 fail-open 方向 ⇒ 必须让
    # 默认替身就产出两个不同的底座。
    resolvedFirstParent = firstParentTipSha or ("c" * 40)

    def fakeHeadInfo(repo: str, runId: str) -> ci_gh.RunHeadInfo:
        return resolvedBase if runId == _BASE_RUN_ID else resolvedPr

    monkeypatch.setattr(ci_surface, "fetchRunHeadInfo", fakeHeadInfo)
    monkeypatch.setattr(ci_surface, "fetchMainTipShaAt", lambda repo, cutoffIso: mainTipSha)
    # ⚠️ `M_T` 有**两条腿**（run 腿 + 第一父链腿）⇒ 两条都得替掉
    # ⇒ 否则第一父链那条腿会真的去 shell 调 `gh`（网络 + 副作用）。
    monkeypatch.setattr(
        ci_surface, "fetchMainFirstParentTipAt", lambda repo, cutoffIso: resolvedFirstParent
    )
    # ⚠️ **祖先关系核对**（2026-11-07 安全评审 HIGH-2）：基线必须是 main 侧底座的
    #   祖先 ⇒ 默认替身返回「基线就是 merge-base」⇒ 正常路径。
    monkeypatch.setattr(
        ci_surface, "fetchMergeBaseSha", lambda repo, baseSha, headSha: baseSha
    )


# --------------------------------------------------------------------------
# 「判定集合 = A ∪ P」（2026-11-07 N6 第三版）
# --------------------------------------------------------------------------


def test_resolveCiSurface_judges_on_the_union_of_main_advance_and_pr_changes(monkeypatch) -> None:
    """判定集合 = `A ∪ P` —— main 的推进**与** PR 的改动都要算进来。

    ⚠️ 只算 `P`（三点归因）会 **fail-open**：
    main 改了 CI 文件、PR 没跟上 ⇒ 那次 PR run 跑的却是**新** CI
    ⇒ 两次 run 的失败集合根本不可比 ⇒ 必须拦下。

    ⚠️ 只算 `A` 会**假拦**：PR 自己改的 CI 文件反而看不见。

    ⇒ 判定 = 两者之并；**分开保存只用于措辞**。
    """
    _patchRuns(monkeypatch)
    monkeypatch.setattr(
        ci_surface,
        "fetchNetChangedFiles",
        lambda repo, baseSha, headSha: (".github/workflows/ci.yml",),
    )
    monkeypatch.setattr(
        ci_surface,
        "fetchChangedFiles",
        lambda repo, baseSha, headSha: ("backend/app/services/nl2sql_dialects.py",),
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is None
    assert info.mainAdvanceFiles == (".github/workflows/ci.yml",)
    assert info.changedFiles == ("backend/app/services/nl2sql_dialects.py",)
    assert info.netChangedFiles == (
        ".github/workflows/ci.yml",
        "backend/app/services/nl2sql_dialects.py",
    )
    # ⚠️ main 改的 CI 文件 ⇒ 归因不到本 PR，但**必须**拦
    assert info.unattributedCiFiles == (".github/workflows/ci.yml",)
    assert ci_surface.detectCiSurfaceChanges(info.netChangedFiles) == (
        ".github/workflows/ci.yml",
    )


def test_resolveCiSurface_flags_a_ci_file_the_pr_itself_changed(monkeypatch) -> None:
    """PR **确实**改了 CI 相关面 ⇒ 必须命中（`A ∪ P` 不能把真命中吃掉）。"""
    _patchRuns(monkeypatch)
    monkeypatch.setattr(
        ci_surface,
        "fetchNetChangedFiles",
        lambda repo, baseSha, headSha: ("backend/app/services/nl2sql_dialects.py",),
    )
    monkeypatch.setattr(
        ci_surface,
        "fetchChangedFiles",
        lambda repo, baseSha, headSha: (".github/workflows/ci.yml",),
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is None
    assert ci_surface.detectCiSurfaceChanges(info.changedFiles) == (".github/workflows/ci.yml",)
    assert ci_surface.detectCiSurfaceChanges(info.netChangedFiles) == (".github/workflows/ci.yml",)
    # PR 自己改的 ⇒ 归因得到 ⇒ 不算「归因不到」
    assert info.unattributedCiFiles == ()


def test_resolveCiSurface_uses_the_main_tip_as_the_merge_base_side(monkeypatch) -> None:
    """`P` 的三点 diff 必须从 `M_T` 起算 ⇒ 只含 PR 自己的改动（不含 main 的）。"""
    captured: dict[str, str] = {}

    def fakeChanged(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        captured["base"] = baseSha
        captured["head"] = headSha
        return ()

    _patchRuns(monkeypatch, mainTipSha="e" * 40)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", fakeChanged)
    monkeypatch.setattr(
        ci_surface, "fetchNetChangedFiles", lambda repo, baseSha, headSha: ()
    )

    ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert captured["base"] == "e" * 40
    assert captured["head"] == "b" * 40


def test_resolveCiSurface_compares_the_main_advance_against_the_baseline_tree(monkeypatch) -> None:
    """`A` = 树对树差 `M_base` ↔ `M_T` ⇒ 基线树与 main 的推进两端都要对。

    ⚠️ `M_T` 有**两条腿**（run 腿 + 第一父链腿）⇒ 差分被调用**两次**
    ⇒ 断言要落在「**两次都拿基线树当底**」上，而不是「只调了一次」。
    """
    captured: list[tuple[str, str]] = []

    def fakeNet(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        captured.append((baseSha, headSha))
        return ()

    _patchRuns(monkeypatch, mainTipSha="e" * 40)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())
    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", fakeNet)

    ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    # 两条腿的底都是基线树（`M_base`）。
    assert captured, "根本没有做树对树差分"
    assert all(base == "a" * 40 for base, _ in captured), captured
    # 两条腿的头分别是 run 腿与第一父链腿（默认替身给的是**不同**的 sha）。
    assert {head for _, head in captured} == {"e" * 40, "c" * 40}, captured


def test_resolveCiSurface_reports_the_main_tip_it_used(monkeypatch) -> None:
    """判定结果里要带上 `M_T`（报告要印：PR 的 CI 实际跑的合并底座）。"""
    _patchRuns(monkeypatch, mainTipSha="e" * 40)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())
    monkeypatch.setattr(
        ci_surface, "fetchNetChangedFiles", lambda repo, baseSha, headSha: ()
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is None
    assert info.mainTipSha == "e" * 40
    assert info.baseSha == "a" * 40
    assert info.prSha == "b" * 40


def test_resolveCiSurface_derives_the_main_tip_cutoff_from_created_at(monkeypatch) -> None:
    """上界必须用 **`created_at`**（事件派发时刻），不是 `run_started_at`。

    ⚠️ 2026-11-07 代码评审 CRITICAL-1：`pull_request` 事件的**合并 commit
    在事件派发那一刻就被冻结**了 ⇒ 复跑**不会**重新计算它
    ⇒ 复跑检出的仍是**同一个**合并 commit ⇒ 它的 main 侧底座是
    **派发那一刻**的 main。

    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的第一父 = `83581e10`（= 派发那一刻的 main）；attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`。

    ⇒ 用 `run_started_at` ⇒ `M_T` 取到**派发之后**的 main ⇒ `A` 腿把
    「派发之后 main 才推进的 CI 改动」也算进来 ⇒ 判定集偏大 ⇒ 假拦；
    而闸门侧若同样用它取基线 ⇒ `A` 变成空集 ⇒ 判「可合并」⇒ **fail-open**。
    """
    captured: dict[str, str] = {}

    def fakeMainTip(repo: str, cutoffIso: str) -> str:
        captured["cutoff"] = cutoffIso
        return "e" * 40

    _patchRuns(
        monkeypatch,
        prInfo=_prInfo(
            createdAt="2026-11-04T20:38:19Z",
            runStartedAt="2026-11-04T22:24:11Z",
        ),
    )
    monkeypatch.setattr(ci_surface, "fetchMainTipShaAt", fakeMainTip)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())
    monkeypatch.setattr(
        ci_surface, "fetchNetChangedFiles", lambda repo, baseSha, headSha: ()
    )

    ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    # ⚠️ 上界 = `created_at`（事件派发时刻）**原值**（**不加宽限**）
    #    ⇒ 与闸门侧（`merge-gate.yml` 的 `PR_RUN_CREATED_AT`）**完全一致**。
    assert captured["cutoff"].startswith("2026-11-04T20:38:19")
    # ⚠️ 绝不能是 run_started_at（那是**派发之后**的时刻
    #    ⇒ 取到更晚的 main ⇒ 基线树 ≠ 执行树 ⇒ fail-open）
    assert not captured["cutoff"].startswith("2026-11-04T22:24:11")


def test_resolveCiSurface_fails_closed_when_the_main_tip_is_unknown(monkeypatch) -> None:
    """**判不出 `M_T`** ⇒ 保守拦下（`error` 非空）。

    ⚠️ 不能退回「基线树 vs PR 头树」：那正是 fail-open 的来源
    ⇒ 退回它 = 把本批要消除的洞带回来。
    """

    def boom(repo: str, cutoffIso: str) -> str:
        raise RuntimeError("找不到 main 的 push run")

    _patchRuns(monkeypatch)
    monkeypatch.setattr(ci_surface, "fetchMainTipShaAt", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is not None
    assert "找不到 main 的 push run" in info.error
    assert info.netChangedFiles == ()
    assert info.changedFiles == ()


def test_resolveCiSurface_fails_closed_when_the_pr_run_has_no_timestamp(monkeypatch) -> None:
    """PR run 的 `run_started_at` 与 `created_at` 都缺失 ⇒ 算不出上界 ⇒ 保守拦下。

    ⚠️ 空时间戳会让「不晚于上界」恒真 ⇒ `M_T` 退化成「main 上最新的 run」
    ⇒ 两次 run 的代码差异被放大 ⇒ **系统性假拦**。
    """
    _patchRuns(monkeypatch, prInfo=_prInfo(createdAt="", runStartedAt=""))

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is not None
    assert info.netChangedFiles == ()
    assert info.changedFiles == ()


def test_resolveCiSurface_returns_error_instead_of_raising(monkeypatch) -> None:
    """拉取失败**不能抛异常** —— 失败要以 `error` 字段返回。

    调用方据此走「判定不出来 ⇒ 保守拦下」的分支，而不是崩在网络错误上。
    """

    def boom(repo: str, runId: str) -> ci_gh.RunHeadInfo:
        raise RuntimeError("gh 挂了")

    monkeypatch.setattr(ci_surface, "fetchRunHeadInfo", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is not None
    assert "gh 挂了" in info.error
    assert info.baseSha is None
    assert info.prSha is None


def test_resolveCiSurface_reports_main_advance_failure(monkeypatch) -> None:
    """`A` 拉不到 ⇒ `error` 非空，但两次 run 的 sha 仍然要带回来（报告要印）。"""

    def boom(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        raise RuntimeError("trees 挂了")

    _patchRuns(monkeypatch)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())
    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is not None
    assert "trees 挂了" in info.error
    assert info.baseSha == "a" * 40
    assert info.changedFiles == ()
    assert info.netChangedFiles == ()


def test_resolveCiSurface_reports_pr_change_failure(monkeypatch) -> None:
    """`P` 拉不到 ⇒ **判定不出来** ⇒ 保守拦下（`error` 非空）。

    ⚠️ 不能静默退回只算 `A`：PR 自己改的 CI 文件会整个消失 ⇒ fail-open。
    """

    def boom(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        raise RuntimeError("compare 挂了")

    _patchRuns(monkeypatch)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", boom)
    monkeypatch.setattr(
        ci_surface, "fetchNetChangedFiles", lambda repo, baseSha, headSha: ()
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is not None
    assert "compare 挂了" in info.error
    assert info.changedFiles == ()
    assert info.netChangedFiles == ()


# --------------------------------------------------------------------------
# 基线可信性（2026-11-07 安全评审 HIGH-1）
# --------------------------------------------------------------------------


def test_resolveCiSurface_rejects_a_base_run_that_is_not_a_push_run(monkeypatch) -> None:
    """基线**不是 push run** ⇒ 不可信 ⇒ 对差的前提不成立。

    ⚠️ GitHub 的 `runs?branch=<name>` 过滤匹配的是 `head_branch`、**不区分事件**
    ⇒ 一个**名叫 main 的 fork 分支**发起的 PR 也会命中 `branch=main`
    ⇒ 它的 run 可能被当成「main 的基线」⇒ 基线 = 攻击者自己的代码
    ⇒ 失败集合对差被中和 ⇒ 闸门失去意义。
    """
    _patchRuns(monkeypatch, baseInfo=_baseInfo(event="pull_request"))

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
    assert "push" in info.baseTrustError
    assert info.error is not None
    assert info.changedFiles == ()


def test_resolveCiSurface_rejects_a_base_run_that_is_not_on_main(monkeypatch) -> None:
    """基线不在 main 上 ⇒ 不可信。"""
    _patchRuns(monkeypatch, baseInfo=_baseInfo(headBranch="feat/other"))

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
    assert "main" in info.baseTrustError


def test_resolveCiSurface_rejects_a_base_run_from_another_repository(monkeypatch) -> None:
    """基线来自**别的仓库**（fork 上一个同名的 main 分支）⇒ 不可信。"""
    _patchRuns(monkeypatch, baseInfo=_baseInfo(headRepo="someone/QA"))

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
    assert "someone/QA" in info.baseTrustError


def test_resolveCiSurface_accepts_a_base_run_whose_repo_differs_only_in_case(monkeypatch) -> None:
    """仓库名**大小写不同**不算「别的仓库」⇒ 不得误判不可信（否则白拦）。

    GitHub 的仓库名大小写不敏感，API 侧会原样返回 `Sunql/QA` 这类形态。
    """
    _patchRuns(monkeypatch, baseInfo=_baseInfo(headRepo="Sunql/QA"))
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())
    monkeypatch.setattr(
        ci_surface, "fetchNetChangedFiles", lambda repo, baseSha, headSha: ()
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is None
    assert info.error is None


def test_resolveCiSurface_does_not_fetch_diffs_when_the_base_is_untrusted(monkeypatch) -> None:
    """基线不可信 ⇒ **不再**去采集改动清单（对差的前提已经没了）。"""
    _patchRuns(monkeypatch, baseInfo=_baseInfo(event="pull_request"))

    def boom(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        raise AssertionError("基线不可信时不该再采集改动清单")

    monkeypatch.setattr(ci_surface, "fetchChangedFiles", boom)
    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", boom)
    monkeypatch.setattr(
        ci_surface,
        "fetchMainTipShaAt",
        lambda repo, cutoffIso: (_ for _ in ()).throw(
            AssertionError("基线不可信时不该再取 main 的 tip")
        ),
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None


def test_resolveCiSurface_returns_shas_and_changed_files(monkeypatch) -> None:
    """正常路径 ⇒ 拿到两次 run 的 head sha 与改动文件清单。"""
    _patchRuns(monkeypatch)
    monkeypatch.setattr(
        ci_surface,
        "fetchChangedFiles",
        lambda repo, baseSha, headSha: (".github/workflows/ci.yml",),
    )
    monkeypatch.setattr(
        ci_surface,
        "fetchNetChangedFiles",
        lambda repo, baseSha, headSha: (".github/workflows/ci.yml",),
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is None
    assert info.baseSha == "a" * 40
    assert info.prSha == "b" * 40
    assert info.changedFiles == (".github/workflows/ci.yml",)
    assert info.netChangedFiles == (".github/workflows/ci.yml",)
    assert info.unattributedCiFiles == ()


def test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file() -> None:
    """**反向守卫**：真实存在的变异取证脚本必须被清单覆盖。

    与 `test_every_ci_tool_module_is_in_the_surface_list` 同款纪律 ——
    清单漏了谁，从文件系统反查时直接转红。
    （2026-11-08 第五轮安全评审 LOW-6：`cases_core` 拆出后这里曾漏 ——
    从 enum 里去掉 `|cases_core` 会让本用例红 ⇒ M69。）
    """
    scriptsDir = Path(__file__).resolve().parents[3] / "scripts"

    for relPath in (
        "backend/scripts/ci_mutation_evidence.py",
        "backend/scripts/ci_mutation_cases.py",
        "backend/scripts/ci_mutation_cases_core.py",
    ):
        assert (scriptsDir / Path(relPath).name).exists(), (
            f"变异取证脚本不在预期位置（{relPath}）⇒ 清单可能已经过期"
        )

        flagged = ci_surface.detectCiSurfaceChanges((relPath,))

        assert flagged == (relPath,), f"{relPath} 没被算进 CI 相关面"


def test_the_round5_environment_lever_files_are_actually_flagged() -> None:
    """**正面断言**：第五–九轮评审的「环境定义杠杆」文件确实被判成 CI 相关面。

    这些文件都在 CI 里**真正被执行或加载**（不是名义配置）⇒ 换掉它们 =
    换掉测试环境 ⇒ 失败集合不可比 ⇒ 却可能判「净新增 0」。反查守卫认不出
    「仓库里现在没有的文件」⇒ 这批按路径**正面**钉住（M70–M99 变异）。

    ⚠️ 本文件钉的是 **31 个路径**（其中 `.env` 家族 / `.python-version` / `uv.toml` /
    versions 形态钉 / `backend/__init__.py` 等 **7 条是形态钉**——仓库里现在没有这些
    文件，断言的是清单 pattern 的**接受面**）；`frontend/.env` / `.env.test` 两条
    形态钉在 `test_ci_surface_frontend_reverse.py` 的正面断言里（合计口径见
    summary §2.10.10–§2.10.14）。
    """
    levers = (
        # 第五轮 HIGH-1：CI 两个 job 前都执行 `alembic upgrade head`，env.py 是真正跑迁移的脚本。
        "backend/alembic/env.py",
        # 第六轮 HIGH-1：迁移脚本本体 —— `upgrade()` 执行的也是收集前的任意 Python。
        "backend/alembic/versions/0140_ontology_property_unit.py",
        "backend/alembic/versions/b1ea6cb32588_run_state.py",
        # 第七轮 HIGH-2：alembic 接受 `[^/]+` 的 revision 文件名（`+` 实测会被 import）
        # ⇒ 清单必须同宽；这条是**形态钉**（文件尚不存在）。
        "backend/alembic/versions/0141_probe+shape.py",
        # 第七轮 HIGH-1：`scripts.*` 包 init —— conftest / `_pg_support` 的
        # `from scripts.… import …` 都先执行它（收集前）。
        "backend/scripts/__init__.py",
        # 第七轮 MEDIUM：pytest prepend 导入链上的测试包 init（现在是空文件，
        # 未来子目录由通配管）。
        "backend/app/tests/__init__.py",
        "backend/app/tests/unit/__init__.py",
        # 第八轮 HIGH-1：`backend/__init__.py` 现在**不存在** —— 新增它就把 pytest
        # prepend 的 pkg_root 顶到仓库根 ⇒ `backend/__init__.py`（现在也还不存在）
        # 在任何收集前执行。
        "backend/__init__.py",
        # 第九轮 HIGH-1：`backend/app/__init__.py` —— conftest 以 `app.…` 导入 ⇒
        # 这是 prepend 链上**现存且每次都执行**的最高一层包 init。
        "backend/app/__init__.py",
        # 第九轮 MEDIUM-1：conftest 模块级 import 的 harness 配置文件（settings /
        # engine / crypto）—— import 即执行；落点由 `ci_conftest_chain` 派生、
        # `test_ci_conftest_chain.py` 钉住，这里正面再钉一遍。
        "backend/app/config.py",
        "backend/app/infrastructure/__init__.py",
        "backend/app/infrastructure/database.py",
        "backend/app/infrastructure/security/__init__.py",
        "backend/app/infrastructure/security/crypto.py",
        # 第十二轮 HIGH-1：conftest `from app.domain.models import …` 会**先执行**
        # 祖先包 init 链（`ci_conftest_chain._resolveModuleFiles` 派生、
        # `test_ci_conftest_chain.py` 正面钉住）；`app/models/__init__.py` 虽无
        # level-1 落点，但经 `dependencies.py` 传递链进入闭包，一并钉住（M96–M99
        # 各自一条 pattern ⇒ 删除型变异可逐个钉）。
        "backend/app/domain/__init__.py",
        "backend/app/models/__init__.py",
        "backend/app/services/__init__.py",
        "backend/app/infrastructure/llm/__init__.py",
        # 第五轮 MEDIUM-2：conftest `load_dotenv(override=True)` ⇒ 覆盖 CI 注入的全部变量。
        "backend/.env",
        "backend/.env.local",
        # 第五轮 MEDIUM-3 + 第六轮 MEDIUM-1：uv 按 .python-version / uv.toml（含仓库根）
        # 选解释器 ⇒ 盖过 setup-python（uv 沿父链发现配置）。
        "backend/.python-version",
        "backend/uv.toml",
        "uv.toml",
        # 第五轮 LOW-5 + 第六轮 MEDIUM-2：塑形测试库 / registry 的测试基建与 seed 脚本
        # （backend/ 根的 seed_ontology 由 conftest **模块级** import ⇒ 收集前执行）。
        "backend/app/tests/_neo4j_support.py",
        "backend/app/tests/_testapp.py",
        "backend/scripts/seed_business_objects.py",
        "backend/scripts/seed_data_domain.py",
        "backend/scripts/seed_feature_rules.py",
        "backend/seed_ontology.py",
        "backend/seed_models.py",
        # 第六轮 LOW-1：测试库断言的依赖模块。
        "backend/scripts/kpi_metric_db_guard.py",
    )

    hits = ci_surface.detectCiSurfaceChanges(levers)

    assert len(hits) == len(levers), f"环境杠杆文件没被判成 CI 相关面：{set(levers) - set(hits)}"


def test_the_judgment_set_is_the_union_not_just_one_side() -> None:
    """**判定集合 = `A ∪ P`**，不是「只算 PR 的改动」也不是「只算 main 的推进」。

    ⚠️ 这是第三版设计的**核心断言**，因此用一条**直接读实现**的用例钉住：
    只改 `_buildSurface` 里那一行集合运算（把并集写成交集/单边），
    上面的行为用例未必全红（取决于喂进去的样本）⇒ 这里补一条
    「集合运算本身就是并集」的守卫。
    """
    from app.utils.ci_surface import _buildSurface

    surface = _buildSurface(
        baseSha="a" * 40,
        prSha="b" * 40,
        mainTipSha="e" * 40,
        mainAdvance=(".github/workflows/ci.yml", "backend/app/services/x.py"),
        prChanged=("backend/app/utils/ci_shard.py", "backend/app/services/x.py"),
    )

    # 并集 ⇒ 三条都在；交集只会剩一条；单边都会少一条
    assert set(surface.netChangedFiles) == {
        ".github/workflows/ci.yml",
        "backend/app/services/x.py",
        "backend/app/utils/ci_shard.py",
    }
    # 归因不到本 PR 的 = 判定集里的 CI 文件 − PR 自己改的 CI 文件
    assert surface.unattributedCiFiles == (".github/workflows/ci.yml",)


# --------------------------------------------------------------------------
# 两条腿取**并集**（2026-11-07 安全评审 HIGH-1）
# --------------------------------------------------------------------------


def test_the_main_advance_is_the_union_of_both_legs(monkeypatch) -> None:
    """`A` = **两条腿各自算出的推进的并集** —— 任一条腿的成果都不能丢。

    ⚠️ 2026-11-07 安全评审 HIGH-1：`M_T` 有两条取法（run 腿 / 第一父链腿），
    各自都可能偏旧 ⇒ 单用一条会漏掉 main 的推进 ⇒ 两次 run 的 CI 定义
    可能其实不同 ⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ **fail-open**。

    ⇒ 本用例让**两条腿返回不同的 sha**、各自带**不同的**推进文件
    ⇒ 判定集必须包含**两边都有**。
    """
    _patchRuns(monkeypatch, mainTipSha="e" * 40, firstParentTipSha="c" * 40)

    def fakeNet(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        if headSha == "e" * 40:
            return (".github/workflows/ci.yml",)
        return ("frontend/vitest.config.ts",)

    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", fakeNet)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.error is None
    assert set(info.mainAdvanceFiles) == {
        ".github/workflows/ci.yml",
        "frontend/vitest.config.ts",
    }, "并集退化成了单腿 ⇒ 漏掉一条腿的推进 ⇒ fail-open"


def test_the_union_of_the_legs_is_actually_exercised(monkeypatch) -> None:
    """反向守卫：**只算一条腿**的实现必须在这条用例上转红。

    ⚠️ 「守卫会红」≠「守卫在测你想测的东西」：若两条腿恰好返回同一个 sha
    （或两条腿算出的推进恰好相同）⇒ 并集与单腿**不可区分** ⇒ 守卫恒绿。
    ⇒ 本用例显式让两条腿产出**不同的**结果 ⇒ 单腿实现必然漏掉一条。
    """
    _patchRuns(monkeypatch, mainTipSha="e" * 40, firstParentTipSha="c" * 40)

    def fakeNet(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        if headSha == "e" * 40:
            return ("backend/app/utils/ci_shard.py",)
        return (".github/workflows/ci.yml",)

    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", fakeNet)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", lambda repo, baseSha, headSha: ())

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    # 只算 run 腿 ⇒ 漏掉 `.github/workflows/ci.yml` ⇒ 失败集合不可比却不拦 ⇒ fail-open
    assert ".github/workflows/ci.yml" in info.mainAdvanceFiles
    assert "backend/app/utils/ci_shard.py" in info.mainAdvanceFiles


# --------------------------------------------------------------------------
# 基线必须是 main 侧底座的**祖先**（2026-11-07 安全评审 HIGH-2）
# --------------------------------------------------------------------------


def test_resolveCiSurface_fails_closed_when_the_base_is_not_an_ancestor(monkeypatch) -> None:
    """基线**不是** main 侧底座的祖先 ⇒ `A` 腿不可信 ⇒ **fail-closed**。

    ⚠️ 不是祖先只有两种可能，两种都致命：
    ① **main 被 force-push / 改写** ⇒ 基线树与执行树分属两条历史 ⇒ 不可比；
    ② 基线挑错了（挑到派发**之后**才创建的 run）⇒ 基线比执行树还新
    ⇒ `A` 腿算出「main 回退了」⇒ 漏掉 main 的真实推进 ⇒ **fail-open**。
    ⇒ 判不出 ⇒ 退出码 3（`baseTrustError` 非空）⇒ 须复跑。
    """
    _patchRuns(monkeypatch)
    monkeypatch.setattr(
        ci_surface, "fetchMergeBaseSha", lambda repo, baseSha, headSha: "f" * 40
    )

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
    assert "祖先" in info.baseTrustError
    assert info.error is not None
    assert info.netChangedFiles == ()


def test_resolveCiSurface_fails_closed_when_the_ancestor_check_cannot_be_run(
    monkeypatch,
) -> None:
    """祖先关系**核对不了**（API 挂了）⇒ 同样 fail-closed（不得当成「是祖先」）。"""
    _patchRuns(monkeypatch)

    def boom(repo: str, baseSha: str, headSha: str) -> str:
        raise RuntimeError("compare 挂了")

    monkeypatch.setattr(ci_surface, "fetchMergeBaseSha", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
    assert "compare 挂了" in info.baseTrustError
    assert info.netChangedFiles == ()


def test_resolveCiSurface_does_not_diff_when_the_base_is_not_an_ancestor(monkeypatch) -> None:
    """祖先关系不成立 ⇒ **不再**去采集改动清单（`A` 腿本来就不可信）。"""

    def boom(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        raise AssertionError("祖先关系不成立时不该再采集改动清单")

    _patchRuns(monkeypatch)
    monkeypatch.setattr(
        ci_surface, "fetchMergeBaseSha", lambda repo, baseSha, headSha: "f" * 40
    )
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", boom)
    monkeypatch.setattr(ci_surface, "fetchNetChangedFiles", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", _BASE_RUN_ID, _PR_RUN_ID)

    assert info.baseTrustError is not None
