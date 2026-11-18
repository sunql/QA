"""G1 问卷摄入包一致性检查器 —— 入口（v5）。

用法：

    python3 backend/scripts/check_procurement_answer_package.py [--repo-root PATH]

退出码契约（**反证脚本依赖它**，v5 修 MEDIUM-1）：

- ``0`` —— 全部通过
- ``1`` —— 发现不一致（**产物层问题**：产物缺失 / 产物损坏 / **产物结构缺陷** / 断言不一致）
- ``2`` —— **检查器自身崩了**（脚本加载失败 / 解析层自身异常）⇒ **不算「抓到缺陷」**

v5 之前的契约是双向破裂的：

| 情形 | v4 | v5 | 后果 |
|---|---|---|---|
| 产物缺文件 / 坏 JSON | 1 ✅ | 1 ✅ | — |
| 产物**结构缺陷**（少一个槽位键） | **2** ❌ | **1** ✅ | v4 把「产物坏了」说成「工具坏了」 |
| 前检自身抛异常 | **traceback + 1** ❌ | **2** ✅ | v4 把「工具坏了」说成「产物坏了」 |

⇒ 处置：**结构前检独立成层**（`_schema.py`，产物层 ⇒ 退出码 1），
前检与结构前检各自包 try/except（工具层 ⇒ 退出码 2）。
⚠️ Python 未捕获异常本身就以 **1** 退出 ⇒ 反证脚本必须额外识别 traceback
（见 `_mutations._toolCrashed`），否则「崩溃」会被当成「抓到缺陷」。

为什么拆多个文件：16 项断言 + 解析层超过 800 行（项目硬上限）⇒ 按仓库既有惯例
（`X.py` + `X_lib.py`，如 `kpi_metric_realdata.py` / `kpi_metric_realdata_lib.py`）拆开：
`_lib`=解析层、`_schema`=结构前检层、`_checks_a`=槽位/表格断言（1–8）、`_checks_b`=登记/文档断言（9–16）。

为什么用 `importlib.util.spec_from_file_location` 而不是 `from scripts.X_lib import ...`：
`backend/scripts/__init__.py` 会 `import scripts.seed_business_objects` ⇒ 把整套 app 依赖拖进来；
本检查器必须**只依赖标准库**，才能在任何环境（含没装依赖的生产机）上秒级复跑。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

#: 检查器自身故障的前缀（反证脚本靠它区分「抓到缺陷」与「脚本崩了」）。
CRASH_PREFIX = "检查器异常"


def _loadSibling(name: str):
    """按文件路径加载同目录模块（不触发 `scripts/__init__.py`）。"""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _defaultRepoRoot() -> Path:
    """`<repo>/backend/scripts/xxx.py` ⇒ `<repo>`。"""
    return Path(__file__).resolve().parents[2]


def _preflight(repoRoot: Path, layerA, schemaLayer) -> tuple[int, list[str]]:
    """前检（产物前检 + 结构前检）。

    返回（退出码, 问题清单）：退出码 0 = 可继续；1 = **产物层**问题（可继续读，但
    后续断言的结论不可信 ⇒ 直接终止）；2 = **工具层**故障。

    ⚠️ 两层都必须区分归因：产物坏了 ⇒ 退出码 1；检查器自己坏了 ⇒ 退出码 2。
    """
    try:
        missing = layerA.lib.preflight(repoRoot)
    except Exception as error:  # noqa: BLE001 —— 前检本身抛异常 = 工具层故障
        return 2, [f"{CRASH_PREFIX}：产物前检自身异常 —— {type(error).__name__}: {error}"]
    if missing:
        return 1, missing

    try:
        structure = schemaLayer.run(repoRoot)
    except Exception as error:  # noqa: BLE001 —— 结构前检自身异常 = 工具层故障
        return 2, [f"{CRASH_PREFIX}：结构前检自身异常 —— {type(error).__name__}: {error}"]
    if structure:
        return 1, structure
    return 0, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1 问卷摄入包一致性检查（v5 · 16 项断言）")
    parser.add_argument("--repo-root", type=Path, default=_defaultRepoRoot())
    args = parser.parse_args(argv)

    repoRoot = args.repo_root.resolve()
    pkg = repoRoot / "docs/update/1118/M0/answers"

    try:
        layerA = _loadSibling("check_procurement_answer_package_checks_a")
        layerB = _loadSibling("check_procurement_answer_package_checks_b")
        schemaLayer = _loadSibling("check_procurement_answer_package_schema")
    except Exception as error:  # noqa: BLE001 —— 入口自身的失败也必须是人话
        print(f"{CRASH_PREFIX}：加载断言层失败 —— {error!r}")
        return 2

    # 前检：缺文件 / 坏 JSON / 结构缺陷都是**产物层问题**（退出码 1），
    # 不能让检查器崩成退出码 2（那是「工具坏了」，归因反了）。
    code, problems = _preflight(repoRoot, layerA, schemaLayer)
    if problems:
        for problem in problems:
            print(f"    - {problem}")
        print("-" * 72)
        print(f"产物前检未通过（{len(problems)} 处），本轮结论不可用。")
        return code

    results: list[tuple[str, list[str]]] = []
    for name, function in layerA.CHECKS + layerB.CHECKS:
        # 逐项独立执行：单项崩了不影响其余项（崩溃要变成可读的失败项，而不是吞掉整轮）。
        try:
            problems = function(repoRoot, pkg)
        except Exception as error:  # noqa: BLE001
            problems = [f"{CRASH_PREFIX}（{name}）：{type(error).__name__}: {error}"]
        results.append((name, problems))

    failed = [(name, problems) for name, problems in results if problems]
    crashed = any(
        problem.startswith(CRASH_PREFIX)
        for _name, problems in failed
        for problem in problems
    )

    for name, problems in results:
        print(f"{'✅' if not problems else '❌'} {name}")
        for problem in problems:
            print(f"    - {problem}")

    print("-" * 72)
    if not failed:
        print(f"全部通过：{len(results)} 项检查无差异。")
        return 0
    total = sum(len(problems) for _name, problems in failed)
    print(f"发现 {total} 处不一致（{len(failed)}/{len(results)} 项检查未通过）。")
    if crashed:
        print(f"{CRASH_PREFIX}：检查器自身有故障，本轮结论不可用。")
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
