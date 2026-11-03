"""「CI 相关面」判定：PR 改了这些文件 ⇒ 两次 run 的失败集合不再可比。

攻击路径：PR 用 `pytest_collection_modifyitems`（conftest）把失败用例跳过、
或在 workflow 里加 `--deselect`、或在分片清单里把含新失败的测试文件踢出去
⇒ 失败用例**根本不产生 FAILED 行** ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」
⇒ 而 GitHub 上 CI 还**全绿** ⇒ 没有二次防线。
"""

from __future__ import annotations

from app.utils import ci_surface


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
