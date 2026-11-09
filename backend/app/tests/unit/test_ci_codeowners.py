"""CODEOWNERS 必须覆盖「CI 相关面」的**每一个**文件。

为什么这是硬约束
----------------
CODEOWNERS 是**合并授权的落点**：GitHub 的「必需复核人」按它算。
而失败集合对差工具只能挡住**偶然**的假绿（cancelled / 夭折 / OOM / 改 conftest），
**挡不住「作者主动伪造 stdout」** ⇒ 合并授权必须落在**人**身上
（依据：`Harness/changes/fix-ci-integration-hardening/summary.md` §7 的 C-1 / H-1 / H-2）。

⇒ 缺了 CODEOWNERS ⇒ 上面第 ② 条落空 ⇒ 工具的「可合并」结论就成了唯一防线。

为什么**从文件系统反查**
----------------------
枚举式清单的通病：新增模块时忘了把它加进去 ⇒ 守卫**静默失效**
（本仓已经踩过：`ci_judge.py` / `ci_report.py` 是拆分时新增的，一开始没进 CODEOWNERS）。

⇒ 三条反查守卫：① 工具模块 ② 工具的守卫用例 ③ 白名单数据文件。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CODEOWNERS = _REPO_ROOT / ".github" / "CODEOWNERS"

# 白名单数据文件：它直接决定「哪些失败用例不作判据」⇒ 改它 = 改判据。
_FLAKE_WHITELIST = ".github/ci-known-flaky-tests.json"


def _ownedPaths() -> set[str]:
    """CODEOWNERS 里列出的路径集合（跳过注释与空行）。"""
    paths: set[str] = set()
    for rawLine in _CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = rawLine.strip()
        if not line or line.startswith("#"):
            continue
        # 形如 `path @owner`（可能多个 owner）⇒ 取第一段路径。
        path = line.split()[0]
        paths.add(path)
    return paths


def test_codeowners_file_exists() -> None:
    """CODEOWNERS 必须在（路径写错 ⇒ 后面三条守卫会**假绿**）。"""
    assert _CODEOWNERS.is_file(), f"找不到 CODEOWNERS：{_CODEOWNERS}"


def test_every_ci_tool_module_has_a_codeowner() -> None:
    """工具目录里**每一个** `ci_*.py` 都必须有 CODEOWNER。

    改判定逻辑 = 改「可合并」的判据 ⇒ 必须有人复核。
    """
    toolDir = _REPO_ROOT / "backend" / "app" / "utils"
    modulePaths = {
        f"backend/app/utils/{path.name}" for path in toolDir.glob("ci_*.py")
    }
    assert modulePaths, "没找到任何 ci_*.py 模块（路径写错了？）"

    owned = _ownedPaths()

    assert modulePaths <= owned, f"CODEOWNERS 漏了：{sorted(modulePaths - owned)}"


def test_every_ci_guard_test_has_a_codeowner() -> None:
    """工具的**守卫用例**也必须有 CODEOWNER —— 改守卫 = 解除守卫。

    （守卫用例能改掉闸门的判据：把「白名单命中 ⇒ 豁免」改回「一律拦下」，
    或把「白名单损坏 ⇒ 退出码 3」改成「当成空白名单」⇒ 失败关闭被解除。）

    ⚠️ 必须按**任意深度**反查（`rglob`），不能只扫 `unit/`：
    2026-11-06 安全审查 M-2 实测，旧实现只扫 `backend/app/tests/unit/`
    ⇒ 把守卫挪到 `integration/` 就同时逃出「CI 相关面」判定与 CODEOWNERS
    ⇒ 改守卫 = 解除守卫，且**没有人**复核。

    ⚠️ 还要覆盖守卫用例的**共享替身**（`_ci_*_support.py`）：
    替身提供「失败集合」「分片划分」的观测对象 ⇒ 改替身就能让守卫**恒绿**
    ⇒ 它与守卫用例**同族** ⇒ 必须一起被授权面管住。
    """
    testRoot = _REPO_ROOT / "backend" / "app" / "tests"
    testPaths = {
        str(path.relative_to(_REPO_ROOT)) for path in testRoot.rglob("test_ci_*.py")
    }
    testPaths |= {
        str(path.relative_to(_REPO_ROOT)) for path in testRoot.rglob("_ci_*_support.py")
    }
    assert testPaths, "没找到任何 test_ci_*.py（路径写错了？）"

    owned = _ownedPaths()

    assert testPaths <= owned, f"CODEOWNERS 漏了：{sorted(testPaths - owned)}"


def test_flake_whitelist_data_file_has_a_codeowner() -> None:
    """白名单**数据文件**必须有 CODEOWNER —— 它是判据的一部分。

    兜底是两层：① 这里（授权面）② 闸门从**默认分支** checkout
    ⇒ PR 分支里的白名单改动不生效。
    """
    owned = _ownedPaths()

    assert _FLAKE_WHITELIST in owned


def test_the_mutation_evidence_script_has_a_codeowner() -> None:
    """**变异取证脚本**必须有 CODEOWNER —— 它决定「哪些变异算转红」。

    攻击路径（2026-11-07 安全评审 MEDIUM-3）：改这个脚本，把「转红」判据从
    「pytest 退出码 1」放宽成「退出码非 0」⇒ 收集失败（退出码 2）也算转红
    ⇒ 取证全绿 ⇒ **守卫恒绿**且没人复核。

    ⇒ 它与工具模块、守卫用例**同族** ⇒ 必须进授权面。

    ⚠️ 清单（`ci_mutation_cases.py`，纯数据）与跑者**同族**：
    改清单可以把某组变异指向一个没人守的地方 ⇒ 照样恒绿 ⇒ 两者都要有 CODEOWNER。
    """
    owned = _ownedPaths()

    for relPath in (
        "backend/scripts/ci_mutation_evidence.py",
        "backend/scripts/ci_mutation_cases.py",
        "backend/scripts/ci_mutation_cases_core.py",
        "backend/scripts/ci_mutation_cases_r14.py",
        "backend/scripts/ci_mutation_cases_r16.py",
        "backend/scripts/ci_mutation_cases_r18.py",
        "backend/scripts/ci_mutation_cases_r20.py",
        "backend/scripts/ci_mutation_cases_r21.py",
        "backend/scripts/ci_mutation_cases_r23.py",
        "backend/scripts/ci_mutation_cases_r24.py",
    ):
        assert (_REPO_ROOT / relPath).is_file(), f"找不到变异取证脚本：{relPath}"
        assert relPath in owned, f"{relPath} 没有 CODEOWNER（改它就能让守卫恒绿）"


# ---------------------------------------------------------------------------
# 2026-11-06 代码评审 M-5：CODEOWNERS 是「**最后匹配者生效**」
# ---------------------------------------------------------------------------


def _effectiveOwner(path: str) -> str | None:
    """按 GitHub CODEOWNERS 语义算出某个路径的**生效** owner。

    ⚠️ GitHub 的规则是「**最后匹配者生效**」⇒ 文件末尾追加一行 `* @someone-else`
    就能把**所有**路径的生效 owner 换掉。

    ⇒ 只检查「路径在文件里出现过」是不够的 ⇒ 必须算**生效归属**。

    规则（GitHub 官方）：
    - 模式**不含** `/` ⇒ 匹配任意层级（对文件名匹配）；
    - 模式**含** `/` ⇒ 相对仓库根；
    - `*` 不跨 `/`，`**` 跨 `/`；
    - 结尾 `/` ⇒ 目录前缀匹配。
    """
    owner: str | None = None
    for rawLine in _CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = rawLine.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern = parts[0]
        owners = parts[1:]
        if _patternMatches(pattern, path):
            owner = owners[-1] if owners else None
    return owner


def _patternMatches(pattern: str, path: str) -> bool:
    """CODEOWNERS 的模式匹配（够用的子集）。"""
    pat = pattern.strip()
    anchored = pat.startswith("/") or ("/" in pat.rstrip("/"))
    if pat.startswith("/"):
        pat = pat[1:]
    isDir = pat.endswith("/")
    pat = pat.rstrip("/")

    out: list[str] = []
    index = 0
    while index < len(pat):
        char = pat[index]
        if char == "*":
            if pat[index : index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        out.append(re.escape(char))
        index += 1
    body = "".join(out)
    if isDir:
        body = f"{body}/.*"
    if not anchored:
        body = f"(?:.*/)?{body}"
    return re.match(f"^{body}$", path) is not None


def test_the_effective_owner_of_every_controlled_path_is_the_expected_one() -> None:
    """受控路径的**生效** owner 必须是预期的人（不是「文件里出现过」就算数）。

    ⚠️ 2026-11-06 代码评审 M-5：旧守卫只查「路径在 CODEOWNERS 里出现过」
    ⇒ 在文件**末尾**追加一行 `* @someone-else` ⇒ 全部 CI 路径（工具模块、守卫用例、
    白名单数据文件）的**生效**复核人被换掉 ⇒ 守卫仍绿 ⇒ 授权面整体漂移。
    """
    expected = "@sunql"
    controlledPaths = {
        f"backend/app/utils/{path.name}"
        for path in (_REPO_ROOT / "backend" / "app" / "utils").glob("ci_*.py")
    }
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend" / "app" / "tests").rglob("test_ci_*.py")
    }
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend" / "app" / "tests").rglob("_ci_*_support.py")
    }
    controlledPaths |= {_FLAKE_WHITELIST}
    controlledPaths |= {"backend/scripts/ci_mutation_evidence.py"}
    controlledPaths |= {"backend/scripts/ci_mutation_cases.py"}
    controlledPaths |= {"backend/scripts/ci_mutation_cases_core.py"}
    controlledPaths |= {
        "backend/scripts/ci_mutation_cases_r14.py",
        "backend/scripts/ci_mutation_cases_r16.py",
        "backend/scripts/ci_mutation_cases_r18.py",
        "backend/scripts/ci_mutation_cases_r20.py",
        "backend/scripts/ci_mutation_cases_r21.py",
        "backend/scripts/ci_mutation_cases_r23.py",
        "backend/scripts/ci_mutation_cases_r24.py",
    }
    controlledPaths |= {".github/CODEOWNERS"}
    # 第五轮安全评审（HIGH-1 / MEDIUM-2 / MEDIUM-3 / LOW-5）：CI 真正执行或加载的
    # 环境定义杠杆 —— env.py、.env、解释器选择、测试基建与 seed 脚本。
    controlledPaths |= {"backend/alembic/env.py"}
    controlledPaths |= {"backend/.env", "backend/.env.example"}
    controlledPaths |= {"backend/.python-version", "backend/uv.toml"}
    controlledPaths |= {
        "backend/app/tests/_pg_support.py",
        "backend/app/tests/_neo4j_support.py",
        "backend/app/tests/_testapp.py",
        "backend/app/tests/milvus_isolation.py",
    }
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend" / "scripts").glob("seed_*.py")
    }
    # 第六轮复审（HIGH-1 / MEDIUM-1 / MEDIUM-2 / LOW-1）：迁移脚本本体、仓库根 uv.toml、
    # backend/ 根的 seed 同族、测试库断言的依赖模块。
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py")
    }
    controlledPaths |= {"uv.toml"}
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend").glob("seed_*.py")
    }
    controlledPaths |= {"backend/scripts/kpi_metric_db_guard.py"}
    # 第七轮复审（HIGH-1 / MEDIUM）：scripts 包 init（conftest/_pg_support 的
    # `from scripts.… import …` 都先执行它）与 pytest prepend 导入链上的测试包 init。
    controlledPaths |= {"backend/scripts/__init__.py"}
    controlledPaths |= {
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "backend" / "app" / "tests").rglob("__init__.py")
    }
    # 第八轮复审（HIGH-1）：backend 包 init —— 现在不存在，但新增它就把 pytest
    # prepend 的 pkg_root 顶到仓库根 ⇒ 任何收集前执行（形态钉，管未来文件）。
    controlledPaths |= {"backend/__init__.py"}
    # 第九轮复审（HIGH-1 + MEDIUM-1）：app 包 init（prepend 链上现存且每次执行的一层）
    # 与 conftest 收集前 import 的 harness 配置文件（settings / engine / crypto）。
    controlledPaths |= {"backend/app/__init__.py"}
    controlledPaths |= {
        "backend/app/config.py",
        "backend/app/infrastructure/__init__.py",
        "backend/app/infrastructure/database.py",
        "backend/app/infrastructure/security/__init__.py",
        "backend/app/infrastructure/security/crypto.py",
    }
    # 第十二轮复审（HIGH-1）：conftest 收集前 import 链的**祖先包 init**
    # （`ci_conftest_chain` 派生 + `test_ci_conftest_chain.py` 钉住）。
    controlledPaths |= {
        "backend/app/domain/__init__.py",
        "backend/app/models/__init__.py",
        "backend/app/services/__init__.py",
        "backend/app/infrastructure/llm/__init__.py",
    }
    assert controlledPaths, "受控路径集合为空（路径写错了？）"

    wrong = {
        path: _effectiveOwner(path)
        for path in sorted(controlledPaths)
        if _effectiveOwner(path) != expected
    }
    assert not wrong, (
        f"这些路径的生效 owner 不是 {expected}：{wrong}"
        "⇒ CODEOWNERS 里有多余的模式（最后匹配者生效）把授权面换掉了"
    )


def test_a_trailing_catch_all_would_take_the_ownership_away() -> None:
    """反向守卫：`* @someone-else` 追加在末尾**确实**会接管全部路径。

    ⇒ 这条用例证明上一条守卫**不是假绿**（若模式匹配写错 ⇒ 永远绿）。
    """
    assert _patternMatches("*", "backend/app/utils/ci_flakes.py")
    assert _patternMatches("*", ".github/CODEOWNERS")


def test_every_ci_surface_file_in_the_pre_collection_closure_has_a_codeowner() -> None:
    """派生守卫（第十二轮 LOW-5）：CI 相关面清单里「收集前闭包」内的文件必须有 owner。

    CI 相关面与 CODEOWNERS 是两份人工维护的清单 ⇒ 会漂移：某文件进了
    `_CI_SURFACE_PATTERNS`（改它 = 退出码 6）却没有 owner ⇒ 仲裁通道无人被请求复核。
    这里用 `ci_conftest_chain.transitivePreCollectionPaths` **派生**收集前会执行的
    文件集，对其中被 CI 相关面钉住的每一个断言 `_effectiveOwner` 非空 —— 新增
    pattern 而忘加 CODEOWNERS 会在派生集扩到该文件时自动红。
    """
    from app.utils.ci_conftest_chain import transitivePreCollectionPaths
    from app.utils.ci_surface import detectCiSurfaceChanges

    backendRoot = _REPO_ROOT / "backend"
    closure = transitivePreCollectionPaths(backendRoot)
    assert len(closure) >= 60, f"闭包塌缩（派生器被掏空？）：{len(closure)}"

    ownerless = [
        path
        for path in closure
        if detectCiSurfaceChanges((path,)) and _effectiveOwner(path) is None
    ]
    assert not ownerless, (
        f"这些 CI 相关面文件（收集前闭包内）在 CODEOWNERS 里没有 owner：{ownerless}"
    )
