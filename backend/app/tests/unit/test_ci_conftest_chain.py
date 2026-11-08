"""conftest 收集前 import 链的**派生式**守卫（第九轮复审 MEDIUM-1）。

枚举式清单已被实证连漏三层（`scripts/__init__` → `backend/__init__`（尚不存在）
→ `backend/app/__init__`（一直在链上却三处全漏））⇒ 本守卫不枚举文件，而是
**派生**：用 `ci_conftest_chain.preCollectionImportPaths` 扫出所有 conftest 的
模块级 `app.*` / `scripts.*` 导入落点，断言每个落点**要么**被
`detectCiSurfaceChanges` 命中（在 CI 相关面清单里）**要么**在底部的显式豁免清单里。

⇒ 未来任何 conftest 新增模块级 import，都必须改这份守卫（本文件在 CI 相关面 +
  CODEOWNERS 里 ⇒ 必须复核）或把落点加进清单 ⇒ 链条被冻结在复核之下。
⇒ 变异取证：M91（把派生器掏空 ⇒ 本守卫必须转红）。
"""

from __future__ import annotations

from pathlib import Path

from app.utils.ci_conftest_chain import preCollectionImportPaths
from app.utils.ci_surface import detectCiSurfaceChanges

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKEND_ROOT = _REPO_ROOT / "backend"

# 显式豁免（第九轮复审 MEDIUM-1 仲裁）：这些是**被测产品代码**，模块级代码是
# 定义性的（ORM 模型类 / FastAPI 依赖声明 / LRU 缓存类），import 时钩子的攻击面
# 显著小于 harness 配置文件（settings / engine / crypto —— 那批已全部入 CI 相关面，
# 见 `ci_surface._CI_SURFACE_PATTERNS` 的「conftest 收集前 import」块）；且把日常
# ORM / 路由 PR 的必改文件入清单 ⇒ 全部判退出码 6 ⇒ 假拦代价不成比例。
# ⚠️ 改这份清单 = 改守卫口径 ⇒ 本文件在 CI 相关面 + CODEOWNERS 里 ⇒ 必须复核。
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
