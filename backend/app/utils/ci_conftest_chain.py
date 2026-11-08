"""conftest「收集前 import 链」的派生器（第九轮复审 MEDIUM-1 的结构性收口）。

背景：pytest prepend 导入模式下，**每个 conftest 的模块级 import 都在任何测试
收集之前执行** —— 能在那里塞 import 时钩子（monkeypatch 被测代码 / 换掉测试模块 /
改 stdout）的文件就是闸门杠杆。逐轮**枚举文件名**的修法已被实证连漏三层
（第七轮 `scripts/__init__` → 第八轮 `backend/__init__`（尚不存在）→ 第九轮
`backend/app/__init__`（一直在链上））⇒ 本模块改为**派生**：AST 扫描所有
conftest 的模块级 `app.*` / `scripts.*` 导入，解析成仓库相对文件路径；
守卫（`test_ci_conftest_chain.py`）据此断言「每个落点要么在 CI 相关面清单里、
要么在显式豁免清单里」⇒ 未来任何 conftest 新增模块级 import 都必须改守卫
（守卫在 CI 相关面 + CODEOWNERS 里 ⇒ 必须复核）或把落点加进清单。

口径：
- 只看**模块级**（`ast.Module.body` 顶层）导入 —— 函数 / 夹具内的 import 在
  收集之后才执行，不是本类杠杆。
- `from X import name` 解析为两种落点：X 的 `__init__.py`（包执行）与
  `X/name.py`（子模块执行）；两者都解析不到 ⇒ name 是符号不是模块 ⇒ 跳过
  （符号本身执行不了任意代码）。
- 相对导入（`level > 0`）与非 `app.*` / `scripts.*` 的导入不在本类口径内
  （`seed_*` 已由清单通配管住；第三方库改不了仓库文件）。
- conftest 解析失败 ⇒ 抛错（fail-closed：静默跳过 = 该 conftest 的链路消失）。
"""

from __future__ import annotations

import ast
from pathlib import Path

# 只派生这两棵顶层包的导入链：`app.*` 是被测应用（conftest 直接 import），
# `scripts.*` 是测试基建（conftest / `_pg_support` 直接 import）。
_DERIVED_TOP_PREFIXES = ("app", "scripts")


def _resolveModuleFile(backendRoot: Path, moduleName: str) -> str | None:
    """模块名 → 仓库相对文件路径（`backend/…` 前缀，与清单口径一致）。

    先试 `<pkg>.py`（纯模块）再试 `<pkg>/__init__.py`（包）；都不是 ⇒ None
    （name 是符号，不是模块落点）。
    """
    rel = moduleName.replace(".", "/")
    for candidate in (backendRoot / f"{rel}.py", backendRoot / rel / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(backendRoot.parent).as_posix()
    return None


def preCollectionImportPaths(backendRoot: Path) -> dict[str, tuple[str, ...]]:
    """派生 `backendRoot` 下**所有** conftest 的收集前 import 落点。

    返回 `{conftest 仓库相对路径: (落点文件仓库相对路径, …)（升序）}`；
    没有可派生落点的 conftest 不出现在结果里。
    """
    result: dict[str, tuple[str, ...]] = {}
    for conftest in sorted(backendRoot.rglob("conftest.py")):
        relParts = conftest.relative_to(backendRoot).parts[:-1]
        if any(part.startswith(".") for part in relParts):
            continue  # 隐藏目录（.venv 等）不是测试树
        tree = ast.parse(conftest.read_text(encoding="utf-8"))
        files: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] not in _DERIVED_TOP_PREFIXES:
                    continue
                resolved = _resolveModuleFile(backendRoot, node.module)
                if resolved:
                    files.add(resolved)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    resolved = _resolveModuleFile(
                        backendRoot, f"{node.module}.{alias.name}"
                    )
                    if resolved:
                        files.add(resolved)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _DERIVED_TOP_PREFIXES:
                        resolved = _resolveModuleFile(backendRoot, alias.name)
                        if resolved:
                            files.add(resolved)
        if files:
            conftestRel = conftest.relative_to(backendRoot.parent).as_posix()
            result[conftestRel] = tuple(sorted(files))
    return result
