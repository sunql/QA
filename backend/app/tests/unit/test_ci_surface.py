"""「CI 相关面」判定：PR 改了这些文件 ⇒ 两次 run 的失败集合不再可比。

攻击路径：PR 用 `pytest_collection_modifyitems`（conftest）把失败用例跳过、
或在 workflow 里加 `--deselect`、或在分片清单里把含新失败的测试文件踢出去
⇒ 失败用例**根本不产生 FAILED 行** ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」
⇒ 而 GitHub 上 CI 还**全绿** ⇒ 没有二次防线。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.utils import ci_gh, ci_surface


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


def test_resolveCiSurface_returns_error_instead_of_raising(monkeypatch) -> None:
    """拉取失败**不能抛异常** —— 失败要以 `error` 字段返回。

    调用方据此走「判定不出来 ⇒ 保守拦下」的分支，而不是崩在网络错误上。
    """

    def boom(repo: str, runId: str) -> str:
        raise RuntimeError("gh 挂了")

    monkeypatch.setattr(ci_surface, "fetchRunHeadSha", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", "111", "222")

    assert info.error is not None
    assert "gh 挂了" in info.error
    assert info.baseSha is None
    assert info.prSha is None


def test_resolveCiSurface_returns_shas_and_changed_files(monkeypatch) -> None:
    """正常路径 ⇒ 拿到两次 run 的 head sha 与改动文件清单。"""
    monkeypatch.setattr(ci_surface, "fetchRunHeadSha", lambda repo, runId: "a" * 40)
    monkeypatch.setattr(
        ci_surface,
        "fetchChangedFiles",
        lambda repo, baseSha, headSha: (".github/workflows/ci.yml",),
    )

    info = ci_surface.resolveCiSurface("sunql/QA", "111", "222")

    assert info.error is None
    assert info.baseSha == "a" * 40
    assert info.prSha == "a" * 40
    assert info.changedFiles == (".github/workflows/ci.yml",)


def test_resolveCiSurface_reports_compare_failure(monkeypatch) -> None:
    """compare 拉不到 ⇒ `error` 非空，但两次 run 的 sha 仍然要带回来（报告要印）。"""

    def boom(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
        raise RuntimeError("compare 挂了")

    monkeypatch.setattr(ci_surface, "fetchRunHeadSha", lambda repo, runId: "a" * 40)
    monkeypatch.setattr(ci_surface, "fetchChangedFiles", boom)

    info = ci_surface.resolveCiSurface("sunql/QA", "111", "222")

    assert info.error is not None
    assert info.baseSha == "a" * 40
    assert info.changedFiles == ()


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


def test_renaming_a_ci_surface_file_is_still_detected() -> None:
    """**改名**也算「改了 CI 相关面」—— 组合守卫（真实攻击路径）。

    2026-11-06 安全审查 H-1 实测：GitHub compare 对改名只给**一个**条目
    （新路径 `filename` + 旧路径 `previous_filename`）⇒ 旧实现只取 `filename`
    ⇒ 把 `.github/ci-known-flaky-tests.json` 改名成 `docs/notes.json`
    就能让它从改动清单里消失 ⇒ **静默绕过**退出码 6 的人工仲裁
    （实测：`flagged=False`）。

    ⇒ 本用例钉的是**整条链**：compare 解析 → 改动清单 → CI 相关面判定。
    """
    payload = json.dumps(
        {
            "files": [
                {
                    "filename": "docs/notes.json",
                    "previous_filename": ".github/ci-known-flaky-tests.json",
                }
            ]
        }
    )

    changed = ci_gh._parseChangedFiles(payload)
    flagged = ci_surface.detectCiSurfaceChanges(changed)

    assert ".github/ci-known-flaky-tests.json" in flagged


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
