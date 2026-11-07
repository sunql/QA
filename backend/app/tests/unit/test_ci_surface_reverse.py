"""「CI 相关面」清单的**文件系统反查守卫**（清单是手写的 ⇒ 必须有人替它盯着）。

背景
----
`_CI_SURFACE_PATTERNS` 是**手写**的 ⇒ 新的 CI 相关文件出现时，
清单不会自己长出条目 ⇒ 守卫**静默失效** ⇒ PR 改了 CI 定义却不被拦。
本仓已经踩过：
- `ci_judge.py` / `ci_report.py` 拆分时新增，一开始没进 CODEOWNERS；
- `pyproject.toml` 原先只认 `^backend/pyproject\\.toml$` ⇒ **任意深度**都漏。

⇒ 三条反查守卫（各自独立失败 ⇒ 不会互相掩盖）：
① pytest 的 ini / conftest 文件（**任意深度**）必须都在 CI 相关面里；
② workflow 里引用到的 compose 文件必须在 CI 相关面里；
③ `pyproject.toml` 在**任意深度**都算 CI 相关面（正面断言）。

⚠️ 2026-11-07 安全评审 CRITICAL-1 的机理（为什么「任意深度」是必须的）：
pytest 找 ini 文件是**从测试路径往上**找（`locate_config`：从调用路径逐级
向上找 `pytest.ini` / `pyproject.toml` / `tox.ini` / `setup.cfg`）⇒
在 `backend/app/tests/` 或 `backend/app/tests/unit/` 放一个
`pyproject.toml`（带 `[tool.pytest.ini_options]`）或 `pytest.ini`
就会**抢先**成为 rootdir 的 ini 文件 ⇒ 它的 `addopts`（例如 `--deselect`）
生效、而 `backend/pyproject.toml` 被**忽略** ⇒ 失败用例根本不产生 FAILED 行
⇒ 假「净新增 0」⇒ 而 CI 还全绿 ⇒ **没有任何二次防线**。

⚠️ 反查守卫自己也可能是**假绿**（扫不到东西 ⇒ 永远绿）⇒
两条「扫描器不是真空」的用例：用 `tmp_path` 造出目标文件 ⇒ 断言扫描器**找得到**
⇒ 再断言它**确实被判成 CI 相关面**。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.utils.ci_surface import detectCiSurfaceChanges

_REPO_ROOT = Path(__file__).resolve().parents[4]

# pytest 可能当成 ini / conftest 用的文件名（任意深度都算数）。
_PYTEST_CONFIG_NAMES = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "conftest.py",
    }
)

# workflow 里 `docker compose -f <file>` / `docker-compose -f <file>` 的取文件形式。
_COMPOSE_REF_PATTERN = re.compile(r"(?:docker[\s-]compose)[^\n]*?-f\s+([^\s'\";]+)")

_SKIP_DIR_NAMES = frozenset({".venv", "node_modules", "__pycache__", ".git", ".pytest_cache"})


def _pytestConfigFilesUnder(root: Path) -> set[str]:
    """`root` 下**任意深度**的 pytest 配置文件（相对 `root` 的路径）。

    ⚠️ 不用 `Path.rglob`：它在不同 Python 版本上对**符号链接目录**的递归
    行为不一致（`backend/.venv` 就是指向别处的符号链接）⇒ 会把 `.venv` 里
    的文件也算进来 ⇒ 假红。⇒ 自己走 `os.walk(followlinks=False)`。
    """
    found: set[str] = set()
    for currentDir, dirNames, fileNames in os.walk(root, followlinks=False):
        dirNames[:] = [name for name in dirNames if name not in _SKIP_DIR_NAMES]
        for fileName in fileNames:
            if fileName not in _PYTEST_CONFIG_NAMES:
                continue
            relative = (Path(currentDir) / fileName).relative_to(root)
            found.add(str(relative))
    return found


def _composeFilesInWorkflows(workflowDir: Path) -> set[str]:
    """workflow 里引用到的 compose 文件（相对仓库根的路径）。"""
    found: set[str] = set()
    for workflowPath in sorted(workflowDir.glob("*.yml")):
        text = workflowPath.read_text(encoding="utf-8")
        for match in _COMPOSE_REF_PATTERN.finditer(text):
            found.add(match.group(1).strip())
    return found


def _missingCiSurfacePaths(paths: set[str]) -> list[str]:
    """这些路径里**没被判成 CI 相关面**的（= 清单漏了的）。"""
    return sorted(path for path in paths if not detectCiSurfaceChanges((path,)))


def test_pyproject_toml_at_any_depth_is_a_ci_surface() -> None:
    """`pyproject.toml` 在**任意深度**都算 CI 相关面（CRITICAL-1 的正面断言）。

    ⚠️ 只认 `backend/pyproject.toml` 是不够的：pytest 的 ini 文件是
    **从测试路径往上**找的 ⇒ `backend/app/tests/pyproject.toml` 会**抢先**
    成为 rootdir 的 ini 文件 ⇒ 它的 `addopts` 生效、`backend/pyproject.toml`
    被忽略 ⇒ `--deselect` 之类的手段就能把失败用例从失败集合里抹掉。
    """
    hits = detectCiSurfaceChanges(
        (
            "backend/pyproject.toml",
            "backend/app/pyproject.toml",
            "backend/app/tests/pyproject.toml",
            "backend/app/tests/unit/pyproject.toml",
        )
    )

    assert len(hits) == 4, f"任意深度的 pyproject.toml 漏了：{hits}"


def test_every_pytest_config_file_in_the_backend_is_a_ci_surface() -> None:
    """`backend/` 下**每一个** pytest 配置文件都必须在 CI 相关面里。

    ⇒ 有人新加了一层 `conftest.py` / `pytest.ini`（忘了加进清单）
    ⇒ 这条守卫立刻红 ⇒ 清单不会静默失效。
    """
    configFiles = _pytestConfigFilesUnder(_REPO_ROOT / "backend")
    assert configFiles, "一个 pytest 配置文件都没扫到（扫描逻辑坏了？）"

    missing = _missingCiSurfacePaths(configFiles)

    assert not missing, f"这些 pytest 配置文件不在 CI 相关面里：{missing}"


def test_every_compose_file_used_by_the_workflows_is_a_ci_surface() -> None:
    """workflow 里用到的 compose 文件必须在 CI 相关面里。

    ⚠️ 为什么它影响**可比性**：integration 分片在 CI 里用
    `docker compose -f docker/docker-compose.yml up milvus-standalone`
    起 Milvus/etcd/minio ⇒ 改这份 compose（改了环境变量、镜像版本、健康检查…）
    ⇒ **integration 的运行环境变了** ⇒ 两次 run 的失败集合**不可比**
    ⇒ 而闸门恰恰要比这两组失败集合。
    """
    composeFiles = _composeFilesInWorkflows(_REPO_ROOT / ".github" / "workflows")
    assert composeFiles, "workflow 里没扫到 compose 文件（正则或目录坏了？）"

    missing = _missingCiSurfacePaths(composeFiles)

    assert not missing, f"这些 compose 文件不在 CI 相关面里：{missing}"


def test_the_pytest_config_scanner_is_not_vacuous(tmp_path: Path) -> None:
    """反查守卫**自己**不能是假绿 ⇒ 造一个嵌套 pytest 配置 ⇒ 断言扫描器找得到。

    ⚠️ 「扫不到东西」的守卫永远绿 ⇒ 必须先证明扫描器真的能扫到。
    """
    nested = tmp_path / "app" / "tests" / "unit"
    nested.mkdir(parents=True)
    (nested / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    found = _pytestConfigFilesUnder(tmp_path)

    assert found == {"app/tests/unit/pytest.ini"}, f"扫描器没扫到造出来的文件：{found}"


def test_a_nested_pytest_config_is_flagged_as_missing_from_the_list() -> None:
    """造出来的**嵌套** pytest 配置 ⇒ 清单必须能判它是 CI 相关面。

    ⚠️ 这是上一条的**对偶**：上一条证明「扫得到」，这条证明「扫到之后会报」。
    ⇒ 两条合起来才排除「反查守卫恒绿」。
    """
    nestedPath = "app/tests/unit/pytest.ini"

    assert detectCiSurfaceChanges((nestedPath,)), "嵌套 pytest.ini 没被判成 CI 相关面"
    assert not _missingCiSurfacePaths({nestedPath})


def test_the_compose_scanner_is_not_vacuous(tmp_path: Path) -> None:
    """compose 反查守卫**自己**不能是假绿 ⇒ 造一个 workflow ⇒ 断言扫描器找得到。"""
    workflowDir = tmp_path / ".github" / "workflows"
    workflowDir.mkdir(parents=True)
    (workflowDir / "ci.yml").write_text(
        "      run: docker compose -f docker/docker-compose.yml up -d milvus-standalone\n",
        encoding="utf-8",
    )

    found = _composeFilesInWorkflows(workflowDir)

    assert found == {"docker/docker-compose.yml"}, f"扫描器没扫到 compose 引用：{found}"
