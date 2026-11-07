"""前端「CI 定义」文件必须全在「CI 相关面」里（2026-11-07 安全评审 CRITICAL-1）。

背景（为什么这是一条**完整绕过链**）
----------------------------------
`ci.yml` 的 `Frontend / typecheck + coverage` 是一个**非分片** job
⇒ 它失败时只产出一个**不透明**的失败 ID（`job:<job 名>`）
⇒ **前端的失败照样进失败集合对差**（不是「前端不管」）。

⇒ 而前端的「测试定义」文件原先**一条都不在** `_CI_SURFACE_PATTERNS` 里
⇒ PR 可以：

- 在 `frontend/vitest.config.ts` 的 `exclude` 里加一条 ⇒ 把失败的用例排除掉
  ⇒ 前端 job 变绿 ⇒ 失败集合与基线**完全相同** ⇒ 假「净新增 0」；
- 改 `frontend/src/tests/setup.ts`（`setupFiles`）⇒ 公共前置里的断言被跳过；
- 改 `frontend/package.json` 的 `test:coverage` 脚本 / `package-lock.json`
  （依赖版本）⇒ 绕过 coverage 门禁或换掉测试框架版本；
- 改 `frontend/tsconfig*.json`（`tsc -b` 的编译口径）⇒ 类型检查形同虚设。

⇒ 而 `.github/CODEOWNERS` 里**没有任何 `frontend/` 条目** ⇒ 复核人也不存在
⇒ **这是一条完整的绕过链** ⇒ 清单必须覆盖前端的 CI 定义文件。

⚠️ 本守卫与 `test_ci_surface_reverse.py` 同款纪律：**清单是手写的**
⇒ 必须有人替它盯着 ⇒ 从**文件系统反查** ⇒ 新增前端配置文件时立刻转红。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.tests.unit.test_ci_codeowners import _effectiveOwner
from app.utils.ci_surface import detectCiSurfaceChanges

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FRONTEND_DIR = _REPO_ROOT / "frontend"

_SKIP_DIR_NAMES = frozenset({"node_modules", "dist", ".git", ".vite", "coverage", "e2e"})

# 前端 CI 定义文件的名字形态（**任意深度**都算数）。
# ⚠️ 只认**会被工具真正加载**的扩展名（js/mjs/cjs/ts/mts/cts/jsx/tsx）
# ⇒ 不认 `.d.ts`（类型声明文件，不是配置源）⇒ 也不认 `vite.config.d.ts`。
_CONFIG_NAME_PATTERN = re.compile(r"^[\w.-]+\.config\.(?:js|mjs|cjs|ts|mts|cts|jsx|tsx)$")
_LOCK_NAMES = frozenset(
    {"package.json", "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"}
)
_TSCONFIG_NAME_PATTERN = re.compile(r"^tsconfig[\w.-]*\.json$")
_SETUP_NAME_PATTERN = re.compile(r"^setup[\w.-]*\.[tj]sx?$")
# ⚠️ vitest 2.x **自动发现**的 workspace/projects 配置文件（第三轮安全评审 C-1）：
# `vitest` 在配置目录根按 `WORKSPACES_NAMES = ["vitest.workspace", "vitest.projects"]`
# 探测，扩展名 `ts/mts/cts/js/mjs/cjs/json`；存在时**取代**根 `vitest.config.ts`
# 的项目解析 ⇒ project 的 `test.exclude` 能把失败用例整个排除掉
# ⇒ 名字形态既不符合 `*.config.*` 也不符合 `tsconfig*` ⇒ 必须单独认。
_WORKSPACE_NAME_PATTERN = re.compile(
    r"^vitest\.(?:workspace|projects)\.(?:ts|mts|cts|js|mjs|cjs|json)$"
)
_BUILDINFO_NAME_PATTERN = re.compile(r"^[\w.-]+\.tsbuildinfo$")
_EXTRA_DEFINITION_NAMES = frozenset({".npmrc"})


def _frontendFilesUnder(root: Path, prefix: str) -> set[str]:
    """`root` 下**任意深度**的普通文件 ⇒ 路径 = `prefix` + 相对 `root` 的路径。

    ⚠️ **路径必须相对传入的 `root`**（不是相对仓库根）：夹具 `tmp_path` 在仓库外
    ⇒ 按仓库根算会抛 `ValueError`。
    ⚠️ 不用 `Path.rglob`：它对**符号链接目录**的递归行为在不同 Python 版本上
    不一致（`frontend/node_modules` 里到处是链接）⇒ 会把依赖树里的文件也算进来
    ⇒ 假红。⇒ 自己走 `os.walk(followlinks=False)`。
    """
    found: set[str] = set()
    for currentDir, dirNames, fileNames in os.walk(root, followlinks=False):
        dirNames[:] = [name for name in dirNames if name not in _SKIP_DIR_NAMES]
        for fileName in fileNames:
            relative = (Path(currentDir) / fileName).relative_to(root)
            found.add(f"{prefix}/{relative}")
    return found


def _frontendCiDefinitionFiles(root: Path, prefix: str) -> set[str]:
    """前端 CI 定义文件（配置 / 锁文件 / tsconfig / setup）的集合。"""
    candidates: set[str] = set()
    for relPath in _frontendFilesUnder(root, prefix):
        name = relPath.rsplit("/", 1)[-1]
        if (
            name in _LOCK_NAMES
            or _CONFIG_NAME_PATTERN.match(name)
            or _TSCONFIG_NAME_PATTERN.match(name)
            or _SETUP_NAME_PATTERN.match(name)
            or _WORKSPACE_NAME_PATTERN.match(name)
            or _BUILDINFO_NAME_PATTERN.match(name)
            or name in _EXTRA_DEFINITION_NAMES
        ):
            candidates.add(relPath)
    return candidates


def _missingCiSurfacePaths(paths: set[str]) -> list[str]:
    """这些路径里**没被判成 CI 相关面**的（= 清单漏了的）。"""
    return sorted(path for path in paths if not detectCiSurfaceChanges((path,)))


def _vitestSetupFiles() -> set[str]:
    """`frontend/vitest.config.ts` 里声明的 `setupFiles`（相对仓库根）。

    ⚠️ `setupFiles` 是**断言的公共前置** ⇒ 改它就能让断言不生效
    ⇒ 但它**不一定**符合「config / setup 命名」形态 ⇒ 必须单独反查。
    """
    configPath = _FRONTEND_DIR / "vitest.config.ts"
    if not configPath.is_file():
        return set()
    text = configPath.read_text(encoding="utf-8")
    match = re.search(r"setupFiles\s*:\s*\[([^\]]*)\]", text)
    if match is None:
        return set()
    declared: set[str] = set()
    for raw in match.group(1).split(","):
        entry = raw.strip().strip('"').strip("'")
        if not entry:
            continue
        candidate = str((_FRONTEND_DIR / entry).resolve())
        declared.add(str(Path(candidate).relative_to(_REPO_ROOT)))
    return declared


def test_every_frontend_ci_definition_file_is_a_ci_surface() -> None:
    """前端**每一个** CI 定义文件都必须判成 CI 相关面。

    ⇒ 有人新加了 `frontend/jest.config.ts` / `frontend/tsconfig.build.json`
    （忘了加进清单）⇒ 这条守卫立刻红 ⇒ 清单不会静默失效。
    """
    definitionFiles = _frontendCiDefinitionFiles(_FRONTEND_DIR, "frontend")
    assert definitionFiles, "一个前端 CI 定义文件都没扫到（扫描逻辑坏了？）"

    missing = _missingCiSurfacePaths(definitionFiles)

    assert not missing, f"这些前端 CI 定义文件不在 CI 相关面里：{missing}"


def test_the_frontend_setup_files_declared_by_vitest_are_ci_surfaces() -> None:
    """`vitest.config.ts` 声明的 `setupFiles` 必须判成 CI 相关面。

    ⚠️ 它们**不一定**符合「setup 命名」形态（可以叫 `global-setup.ts`、
    `bootstrap.ts`…）⇒ 只能从**配置里反查**，不能靠文件名枚举。
    """
    setupFiles = _vitestSetupFiles()
    assert setupFiles, "没从 vitest.config.ts 里读到 setupFiles（正则坏了？）"

    missing = _missingCiSurfacePaths(setupFiles)

    assert not missing, f"这些 setupFiles 不在 CI 相关面里：{missing}"


def test_the_frontend_ci_files_are_actually_flagged() -> None:
    """**正面断言**：前端的 CI 定义文件确实被判成 CI 相关面。

    ⚠️ 反查守卫自己可能是**假绿**（扫不到东西 ⇒ 永远绿）⇒ 这里补一条
    「清单确实认这些文件」的正面断言，并要求扫出来的集合**非空**。
    """
    hits = detectCiSurfaceChanges(
        (
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/vitest.config.ts",
            "frontend/vite.config.ts",
            "frontend/playwright.config.ts",
            "frontend/tsconfig.json",
            "frontend/tsconfig.node.json",
            "frontend/tsconfig.node.tsbuildinfo",
            "frontend/.npmrc",
            "frontend/src/tests/setup.ts",
        )
    )

    assert len(hits) == 10, f"前端 CI 定义文件没被判成 CI 相关面：{hits}"


def test_a_nested_frontend_config_is_flagged(tmp_path: Path) -> None:
    """**任意深度**的前端配置（`frontend/src/x/foo.config.ts`）也算 CI 相关面。

    ⚠️ 反向守卫：有人把配置文件放到子目录 ⇒ 只认顶层的清单会漏。
    """
    nested = "frontend/src/features/foo.config.ts"

    assert detectCiSurfaceChanges((nested,)), "嵌套的前端配置没被判成 CI 相关面"


def test_the_frontend_scanner_is_not_vacuous(tmp_path: Path) -> None:
    """反查守卫**自己**不能是假绿 ⇒ 造一个前端配置 ⇒ 断言扫描器找得到。

    ⚠️ 「扫不到东西」的守卫永远绿 ⇒ 必须先证明扫描器真的能扫到。
    """
    nested = tmp_path / "frontend" / "src" / "features"
    nested.mkdir(parents=True)
    (nested / "widget.config.ts").write_text("export default {}\n", encoding="utf-8")

    found = _frontendFilesUnder(tmp_path / "frontend", "frontend")

    assert "frontend/src/features/widget.config.ts" in found, (
        f"扫描器没扫到造出来的文件：{sorted(found)}"
    )


def test_a_lock_file_under_a_nested_directory_is_found(tmp_path: Path) -> None:
    """嵌套目录里的锁文件也算 CI 定义（依赖版本变了 ⇒ 环境变了 ⇒ 不可比）。"""
    nested = tmp_path / "frontend" / "packages" / "ui"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text("{}\n", encoding="utf-8")

    found = _frontendCiDefinitionFiles(tmp_path / "frontend", "frontend")

    assert "frontend/packages/ui/package.json" in found


def test_the_frontend_ci_definition_files_are_covered_by_codeowners() -> None:
    """前端 CI 定义文件必须有 CODEOWNER（清单是判据，授权面是复核落点）。

    ⚠️ CODEOWNERS 是**最后匹配者生效**，且支持通配 ⇒ 不能只看「路径字面在
    文件里出现过」（`frontend/vitest.workspace.*` 这类通配条目管的是**还不存在**的
    未来文件）⇒ 按 `_effectiveOwner` 算**生效归属**。
    """
    definitionFiles = _frontendCiDefinitionFiles(_FRONTEND_DIR, "frontend") | _vitestSetupFiles()
    assert definitionFiles, "没扫到任何前端 CI 定义文件（扫描逻辑坏了？）"

    missing = sorted(
        path for path in definitionFiles if _effectiveOwner(path) != "@sunql"
    )

    assert not missing, f"这些前端 CI 定义文件没有 CODEOWNER：{missing}"


def test_the_vitest_workspace_entries_are_covered_by_codeowners() -> None:
    """vitest 自动发现的 workspace/projects 文件必须在授权面里（评审 C-1）。

    ⚠️ 仓库里**现在没有**这类文件（没有就不扫得到，上面那条守卫管不到）
    ⇒ 通配条目「管未来文件」的责任必须有人钉住 ⇒ 对**还不存在的名字**直接断言
    生效归属 —— 有人删掉 `frontend/vitest.workspace.*` 这条 ⇒ 这里立刻红。
    """
    for name in (
        "frontend/vitest.workspace.ts",
        "frontend/vitest.workspace.mts",
        "frontend/vitest.workspace.json",
        "frontend/vitest.projects.js",
        "frontend/vitest.projects.cjs",
    ):
        assert _effectiveOwner(name) == "@sunql", f"{name} 不在 CODEOWNERS 授权面里"


def test_vitest_workspace_and_projects_files_are_ci_surfaces() -> None:
    """**正面断言**：vitest 自动发现的 workspace/projects 文件判成 CI 相关面（C-1）。

    存在 workspace 文件时 vitest **不再走**根 `vitest.config.ts` 的项目解析
    ⇒ project 的 `test.exclude` 能把失败用例整个排除 ⇒ 前端 job 假绿
    ⇒ 失败集合对差判「净新增 0」⇒ **完整绕过链**。
    """
    hits = detectCiSurfaceChanges(
        (
            "frontend/vitest.workspace.ts",
            "frontend/vitest.workspace.mts",
            "frontend/vitest.workspace.json",
            "frontend/vitest.projects.js",
            "frontend/vitest.projects.cjs",
            "frontend/packages/ui/vitest.workspace.ts",
        )
    )

    assert len(hits) == 6, f"vitest workspace/projects 文件没被判成 CI 相关面：{hits}"


def test_the_workspace_scanner_is_not_vacuous(tmp_path: Path) -> None:
    """反查守卫对 workspace 名字形态**真的扫得到**（不是恒绿）。"""
    (tmp_path / "vitest.workspace.ts").write_text("export default []\n", encoding="utf-8")

    found = _frontendCiDefinitionFiles(tmp_path, "frontend")

    assert "frontend/vitest.workspace.ts" in found, f"扫描器没认出 workspace 文件：{sorted(found)}"


def test_the_vitest_setup_files_declared_are_read_from_the_real_config() -> None:
    """反查守卫的**数据来源**本身要钉住（防止它「恰好扫到空集」）。

    ⚠️ 若 `vitest.config.ts` 改了 `setupFiles` 的写法（例如从数组改成对象），
    正则会扫不到 ⇒ 守卫恒绿 ⇒ 静默失效 ⇒ 必须钉住「确实读到了东西」。
    """
    setupFiles = _vitestSetupFiles()

    assert setupFiles, "vitest.config.ts 里没读到 setupFiles ⇒ 反查守卫恒绿"

    # ⚠️ 2026-11-08 第三轮评审 L-2：原第二条断言写成了
    # `assert json.dumps(sorted(setupFiles))` —— `json.dumps([])` 是 `"[]"`，
    # **恒真** ⇒ 是一条什么都没验的空断言，已删除（上一行才是有效断言）。
