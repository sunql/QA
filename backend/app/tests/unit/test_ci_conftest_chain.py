"""conftest 收集前 import 链的**派生式**守卫（第九轮复审 MEDIUM-1 + 第十二轮收口）。

枚举式清单已被实证连漏三层（`scripts/__init__` → `backend/__init__`（尚不存在）
→ `backend/app/__init__`（一直在链上却三处全漏））⇒ 本守卫不枚举文件，而是
**派生**：用 `ci_conftest_chain.preCollectionImportPaths` 扫出所有 conftest 的
模块级 `app.*` / `scripts.*` 导入落点（**含祖先包 init 链** —— 第十二轮复审
HIGH-1：`from app.domain.models import X` 会先执行 `app/domain/__init__.py`），
断言每个落点**要么**被 `detectCiSurfaceChanges` 命中（在 CI 相关面清单里）
**要么**在底部的显式豁免清单里。

⇒ 未来任何 conftest 新增模块级 import，都必须改这份守卫（本文件在 CI 相关面 +
  CODEOWNERS 里 ⇒ 必须复核）或把落点加进清单 ⇒ 链条被冻结在复核之下。
⇒ 变异取证：M91（掏空派生器）、M92（只解析叶子、丢祖先 init 链）⇒ 守卫须红。

第十二轮复审 HIGH-2 的传递闭包 + 内容不变式见文件后半（M93/M94/M95）。
"""

from __future__ import annotations

from pathlib import Path

from app.utils.ci_conftest_chain import (
    findImportTimeHooks,
    preCollectionImportPaths,
    transitivePreCollectionPaths,
)
from app.utils.ci_surface import detectCiSurfaceChanges

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKEND_ROOT = _REPO_ROOT / "backend"

# 显式豁免（第九轮复审 MEDIUM-1 仲裁）：这些是**被测产品代码**，模块级代码是
# 定义性的（ORM 模型类 / FastAPI 依赖声明 / LRU 缓存类），import 时钩子的攻击面
# 显著小于 harness 配置文件（settings / engine / crypto —— 那批已全部入 CI 相关面，
# 见 `ci_surface._CI_SURFACE_PATTERNS` 的「conftest 收集前 import」块）；且把日常
# ORM / 路由 PR 的必改文件入清单 ⇒ 全部判退出码 6 ⇒ 假拦代价不成比例。
# ⚠️ 改这份清单 = 改守卫口径 ⇒ 本文件在 CI 相关面 + CODEOWNERS 里 ⇒ 必须复核。
# ⚠️ 「定义性」不是宣称而是被钉住的：豁免文件全在传递闭包里 ⇒
#   `test_transitive_closure_files_are_surface_or_hook_free` 的
#   `findImportTimeHooks` 检查对它们同样生效（第十二轮复审 MEDIUM-4）。
_EXEMPT_PRE_COLLECTION_IMPORTS = frozenset({
    "backend/app/dependencies.py",
    "backend/app/domain/models.py",
    "backend/app/infrastructure/llm/embedding_provider_factory.py",
    "backend/app/services/agent_binding_cache.py",
    "backend/app/services/agent_tool_config_registry.py",
})

# harness 配置文件**不允许**进豁免清单（反向钉）。
_HARNESS_CONFIG_FILES = (
    "backend/app/config.py",
    "backend/app/infrastructure/__init__.py",
    "backend/app/infrastructure/database.py",
    "backend/app/infrastructure/security/__init__.py",
    "backend/app/infrastructure/security/crypto.py",
)

# 第十二轮复审 HIGH-1 实测逃出的**祖先包 init**（已逐个入清单 + CODEOWNERS）。
# 前三个是 level-1 落点（conftest 直接 `from app.domain.models import …` 等）；
# `app/models/__init__.py` 没有 conftest 直接 import `app.models.*`，但它经
# `dependencies.py` 的传递链进入闭包 ⇒ 由传递闭包测试钉（M92 + M93 各自钉住）。
# 在这里正面钉住「派生器确实把它们扫出来了」—— 祖先链解析被退化（M92）⇒ 本组
# 断言红；pattern 被删 ⇒ lever 测试红（M96–M99）。
_DERIVED_PARENT_PACKAGE_INITS = (
    "backend/app/domain/__init__.py",
    "backend/app/services/__init__.py",
    "backend/app/infrastructure/llm/__init__.py",
)


def test_every_pre_collection_conftest_import_is_ci_surface_or_reviewed() -> None:
    """派生出的每个 conftest 收集前 import 落点必须有授权落点（清单或豁免）。"""
    mapping = preCollectionImportPaths(_BACKEND_ROOT)

    assert mapping, "派生结果为空（backendRoot 路径写错了？）"
    assert len(mapping) >= 4, f"conftest 数量异常：{sorted(mapping)}"

    problems: dict[str, list[str]] = {}
    for conftest, files in mapping.items():
        unguarded = [
            path
            for path in files
            if not detectCiSurfaceChanges((path,))
            and path not in _EXEMPT_PRE_COLLECTION_IMPORTS
        ]
        if unguarded:
            problems[conftest] = unguarded

    assert not problems, (
        "这些 conftest 的收集前 import 落点既不在 CI 相关面清单、也不在豁免清单"
        "⇒ 把落点加进 `_CI_SURFACE_PATTERNS` / CODEOWNERS，"
        f"或给出理由进 `_EXEMPT_PRE_COLLECTION_IMPORTS`：{problems}"
    )


def test_the_harness_config_imports_are_guarded_not_exempt() -> None:
    """反向守卫：harness 配置文件必须**在清单里**且**不在**豁免清单里。

    （豁免清单被「顺手」塞进 settings / crypto ⇒ 派生守卫照样绿 ⇒ 链上最高价值
    的杠杆失去授权落点。）
    """
    for path in _HARNESS_CONFIG_FILES:
        assert path not in _EXEMPT_PRE_COLLECTION_IMPORTS, path
        assert detectCiSurfaceChanges((path,)) == (path,), (
            f"{path} 必须在 `_CI_SURFACE_PATTERNS` 里（conftest 收集前执行）"
        )


def test_derivation_includes_parent_package_inits() -> None:
    """派生器必须解析**祖先包 init 链**，不只叶子模块（第十二轮 HIGH-1，M92 锚点）。

    `from app.domain.models import X` 先执行 `app/domain/__init__.py` —— 只解析
    叶子的派生器会让这些中间包 init 全部逃出守卫（正向存在性断言 ⇒ 退化即红）。
    """
    mapping = preCollectionImportPaths(_BACKEND_ROOT)
    derived = {path for files in mapping.values() for path in files}

    for path in _DERIVED_PARENT_PACKAGE_INITS:
        assert path in derived, (
            f"{path} 没被派生出来 —— 祖先包 init 链解析被退化/删除了？"
        )
        assert detectCiSurfaceChanges((path,)) == (path,), (
            f"{path} 被派生出来了却不在 `_CI_SURFACE_PATTERNS` 里"
        )

    # `app/models/__init__.py`（闭包内非 level-1 落点的那颗）：必须在传递闭包里
    # 且被 CI 相关面钉住（M93 锚点 + M98 pattern 删除锚点）。
    closure = transitivePreCollectionPaths(_BACKEND_ROOT)
    assert "backend/app/models/__init__.py" in closure
    assert detectCiSurfaceChanges(("backend/app/models/__init__.py",))


def test_transitive_closure_files_are_surface_or_hook_free() -> None:
    """传递闭包内容不变式（第十二轮 HIGH-2 + MEDIUM-4，M93 锚点）。

    落点模块自己的 import 链也在收集前执行（`app.domain.models` 拉起数十个
    models/services 文件）。把它们全入清单会让日常 ORM/服务 PR 全判退出码 6
    ⇒ 改用**内容不变式**：闭包内每个文件要么已被 CI 相关面钉住（改它 = 退出码 6
    仲裁），要么模块顶层只含定义性代码（`findImportTimeHooks` 为空）⇒ 塞
    import 期钩子会直接让本守卫红（失败集合净新增 ⇒ 闸门拦）。
    """
    closure = transitivePreCollectionPaths(_BACKEND_ROOT)

    assert len(closure) >= 60, f"传递闭包塌缩（派生器被掏空？）：{len(closure)}"
    assert "backend/app/domain/models.py" in closure, (
        "闭包里没有 domain/models.py —— BFS 没跑起来？"
    )

    problems: dict[str, tuple[str, ...]] = {}
    for path in closure:
        if detectCiSurfaceChanges((path,)):
            continue
        hooks = findImportTimeHooks(_REPO_ROOT / path)
        if hooks:
            problems[path] = hooks[:3]

    assert not problems, (
        "这些收集前会执行的文件既不在 CI 相关面清单、顶层又含 import 期代码"
        "⇒ 入清单/CODEOWNERS，或把模块级代码收进函数/类定义："
        f"{problems}"
    )


def test_import_time_hook_detector_is_not_vacuous(tmp_path: Path) -> None:
    """检测器自身的双向单元钉（M94 锚点）：钩子必须被认出、纯构造必须放行。

    （检测器被掏空 ⇒ 上一条内容不变式对一切文件空转 ⇒ 全绿假象。）
    """
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import sys\n"
        "sys.path.insert(0, 'x')\n",
        encoding="utf-8",
    )
    assert findImportTimeHooks(evil), "sys.path 注入没被认出 —— 检测器失效"

    clean = tmp_path / "clean.py"
    clean.write_text(
        "import logging\n"
        "LOG = logging.getLogger(__name__)\n"
        "TAGS = frozenset({'a'})\n",
        encoding="utf-8",
    )
    assert findImportTimeHooks(clean) == (), f"纯构造被误报：{findImportTimeHooks(clean)}"


def test_scope_recursion_and_plugins_are_derived(tmp_path: Path) -> None:
    """If/Try 分支里的 import 与 `pytest_plugins` 清单也必须被派生（M95 锚点）。

    （只看 `tree.body` 顶层的派生器漏掉 `try: import x` 与插件声明 ⇒
    第十二轮复审 MEDIUM-3。合成 backend 树直接喂 `preCollectionImportPaths`。）
    """
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "app" / "config.py").write_text("X = 1\n", encoding="utf-8")
    (backend / "app" / "plug.py").write_text("Y = 2\n", encoding="utf-8")
    (backend / "conftest.py").write_text(
        "try:\n"
        "    import app.config\n"
        "except ImportError:\n"
        "    pass\n"
        "pytest_plugins = ['app.plug']\n",
        encoding="utf-8",
    )

    mapping = preCollectionImportPaths(backend)

    assert len(mapping) == 1, f"conftest 没被扫到：{mapping}"
    derived = {path for files in mapping.values() for path in files}
    assert derived == {
        "backend/app/__init__.py",
        "backend/app/config.py",
        "backend/app/plug.py",
    }, f"try 分支 import / pytest_plugins 没被派生：{derived}"
