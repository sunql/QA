"""采购本体对象目录（M1-1.4）单测 + **迁移漂移守卫**。

钉住三件事
----------
1. **目录自身的不变量**：10 个对象、处置穷举、复活名单 / 新增 BO 名单**被对象认领**
   （不被任何对象认领的类 ⇒ 幽灵改动）。
2. **迁移 0158 与目录一致**：迁移是**自包含**的（不 import ``app.*``）⇒ 两边各有一份
   决策数据 ⇒ **必须**有守卫钉住，否则两边各改一边 ⇒ 生产数据与设计文档静默分叉。
3. **守卫不是空集**（变异反证）：把迁移源码改坏后，守卫**必须**报出来
   —— 否则「守��恒通过」只是「什么都没比」。

为什么用「importlib 加载迁移模块」而不是 AST / 正则
--------------------------------------------------
迁移模块**只有常量定义与函数定义**（无 import 期副作用）⇒ 直接加载拿到的是**运行期真值**
⇒ 比 AST 取字面量更强（AST 只能拿到写法，运行期值才是不一致的真凶），
也比正则强（正则会扫到注释里的类名 ⇒ 假红）。**唯一**的代价是迁移源码里
import 了 ``app.*`` 就不能再这么干 —— 那正是守卫要防的事（单测另行断言）。

为什么不新建 integration 测试文件
------------------------------
CI 集成测试按「排序后下标 mod 4」切分 ⇒ 新建文件会重排分片 ⇒ 假 net-new。
本文件是 unit（路径哈希分片）⇒ 新增安全。
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

from app.domain.procurement_object_catalog import (
    ACTIVE_HEADER_CLASSES,
    NEW_BUSINESS_OBJECTS,
    NOT_BUILT_OBJECTS,
    PROCUREMENT_OBJECT_SPECS,
    REPOINTED_BUSINESS_OBJECTS,
    REVIVED_CLASS_NAMES,
    V03_OBJECT_COUNT,
    ObjectDisposition,
    allHeaderClassNames,
    businessObjectHeaderClasses,
    newBusinessObjectCodes,
    objectSpec,
    revivedClassNames,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _BACKEND_ROOT / "alembic" / "versions"
_MIGRATION_STEM = "0158_procurement_object_reconciliation"


def _loadMigrationModule(source: str | None = None):
    """加载迁移模块；``source`` 非空时用改写后的源码（变异反证用）。

    迁移模块**只有常量与函数**（无 import 期副作用）⇒ 直接 exec 即可拿到运行期真值。
    """
    path = _MIGRATIONS_DIR / f"{_MIGRATION_STEM}.py"
    assert path.exists(), f"迁移文件不存在：{path}"
    text = path.read_text(encoding="utf-8") if source is None else source
    module = types.ModuleType("_mig_0158")
    module.__file__ = str(path)
    exec(compile(text, str(path), "exec"), module.__dict__)
    return module


def _drift(module) -> list[str]:
    """迁移与目录的差异清单（空 = 一致）。"""
    problems: list[str] = []
    migrationRevive = tuple(sorted(module._REVIVE_CLASS_NAMES))
    if migrationRevive != revivedClassNames():
        problems.append(
            f"复活名单不一致：迁移={migrationRevive} 目录={revivedClassNames()}"
        )
    migrationBo = tuple(
        (row[0], row[1], row[2], row[3], row[4]) for row in module._NEW_BUSINESS_OBJECTS
    )
    catalogBo = tuple(
        (spec.code, spec.name, spec.headerClassName, spec.graphLabel, spec.description)
        for spec in NEW_BUSINESS_OBJECTS
    )
    if migrationBo != catalogBo:
        problems.append(f"新增 BO 不一致：迁移={migrationBo} 目录={catalogBo}")
    migrationRepoint = tuple(
        (row[0], row[1], row[2]) for row in module._REPOINTED_BUSINESS_OBJECTS
    )
    catalogRepoint = tuple(
        (spec.businessObjectCode, spec.fromClassName, spec.toClassName)
        for spec in REPOINTED_BUSINESS_OBJECTS
    )
    if migrationRepoint != catalogRepoint:
        problems.append(f"改指不一致：迁移={migrationRepoint} 目录={catalogRepoint}")
    return problems


# ---------------------------------------------------------------------------
# 目录自身的不变量
# ---------------------------------------------------------------------------


def test_catalog_covers_exactly_the_ten_v03_objects() -> None:
    """V0.3 §3.2 是 10 个对象 ⇒ 目录必须不多不少。"""
    assert len(PROCUREMENT_OBJECT_SPECS) == V03_OBJECT_COUNT


def test_object_keys_are_unique() -> None:
    keys = [spec.objectKey for spec in PROCUREMENT_OBJECT_SPECS]
    assert len(set(keys)) == len(keys)


def test_every_object_carries_a_substantive_rationale() -> None:
    """处置必须写清理由（否则「核对」退化成「清单」）。"""
    for spec in PROCUREMENT_OBJECT_SPECS:
        assert len(spec.rationale) >= 20, f"{spec.objectKey} 的理由过短"


def test_every_disposition_value_is_exercised() -> None:
    """处置枚举的每个取值都要有对象在用（否则枚举里有死分支）。"""
    used = {spec.disposition for spec in PROCUREMENT_OBJECT_SPECS}
    assert used == set(ObjectDisposition)


def test_revived_classes_are_all_claimed_by_an_object() -> None:
    """复活名单里的每个类都要被某个对象认领（否则是幽灵改动）。"""
    claimed = {
        name for spec in PROCUREMENT_OBJECT_SPECS for name in spec.revivedClassNames
    }
    assert claimed == set(REVIVED_CLASS_NAMES)


def test_new_business_objects_are_all_claimed_by_an_object() -> None:
    claimed = {
        code
        for spec in PROCUREMENT_OBJECT_SPECS
        for code in spec.addedBusinessObjectCodes
    }
    assert claimed == {spec.code for spec in NEW_BUSINESS_OBJECTS}


def test_every_bo_header_class_is_active_or_revived() -> None:
    """BO 头类必须**最终是活跃类**：要么本来就活跃，要么本批复活。"""
    activeOrRevived = set(ACTIVE_HEADER_CLASSES) | set(REVIVED_CLASS_NAMES)
    for code, headerClass in businessObjectHeaderClasses().items():
        assert headerClass in activeOrRevived, f"BO {code} 的头类 {headerClass} 不可用"


def test_header_class_names_are_consistent_with_the_specs() -> None:
    assert set(allHeaderClassNames()) <= set(ACTIVE_HEADER_CLASSES) | set(
        REVIVED_CLASS_NAMES
    )


def test_new_business_object_codes_are_uppercase_and_short_enough() -> None:
    """``business_object.code`` 是 String(20) 且有 CHECK ``code = upper(code)``。"""
    for code in newBusinessObjectCodes():
        assert code == code.upper()
        assert len(code) <= 20


def test_contract_is_not_built_and_names_the_gap() -> None:
    """CONTRACT：明确不建 + 写清「源缺口 / 不虚构源表」+ 替代承载面。"""
    spec = objectSpec("CONTRACT")
    assert spec is not None
    assert spec.disposition is ObjectDisposition.NOT_BUILT
    assert spec.businessObjectCode is None
    assert "不虚构源表" in spec.rationale

    notBuilt = {item.objectKey: item for item in NOT_BUILT_OBJECTS}
    contract = notBuilt["CONTRACT"]
    assert "document_catalog" in contract.carriedBy
    assert "DWD_PURCHASE_CONTRACT" in contract.reason


def test_not_built_objects_all_declare_a_carrying_surface() -> None:
    """「不建」必须写清由谁承载（否则等于静默丢对象）。"""
    for item in NOT_BUILT_OBJECTS:
        assert item.carriedBy.strip(), f"{item.objectKey} 没写承载面"
        assert item.reason.strip(), f"{item.objectKey} 没写理由"


def test_supplier_performance_is_derived_and_not_built() -> None:
    spec = objectSpec("SUPPLIER_PERFORMANCE")
    assert spec is not None
    assert spec.disposition is ObjectDisposition.NOT_BUILT
    assert "Derived" in spec.rationale


# ---------------------------------------------------------------------------
# 迁移 0158 ↔ 目录：漂移守卫
# ---------------------------------------------------------------------------


def test_migration_matches_the_catalog() -> None:
    """迁移的决策数据必须与目录逐字一致。"""
    module = _loadMigrationModule()
    problems = _drift(module)
    assert not problems, "；".join(problems)


def test_migration_revision_chain_is_linear() -> None:
    module = _loadMigrationModule()
    assert module.revision == "0158"
    assert module.down_revision == "0157"


def test_migration_does_not_import_application_code() -> None:
    """迁移必须**自包含**：不 import ``app.*``（生产回滚时不能依赖应用代码）。"""
    path = _MIGRATIONS_DIR / f"{_MIGRATION_STEM}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.split(".")[0] == "app"], imported


def test_guard_catches_a_mutated_migration() -> None:
    """**变异反证**：把迁移里的复活名单改坏 ⇒ 守卫必须报出来。

    没有这一条，上面的守卫可能只是「什么都没比」的假绿。
    """
    path = _MIGRATIONS_DIR / f"{_MIGRATION_STEM}.py"
    original = path.read_text(encoding="utf-8")
    mutated = original.replace('\n    "DWD_SUPPLIER_PAYMENT",\n', "\n")
    assert mutated != original, "变异没生效（源码形态变了？）"
    module = _loadMigrationModule(mutated)
    problems = _drift(module)
    assert problems, "守卫没抓到变异 ⇒ 守卫是空集假绿"
    assert "复活名单不一致" in problems[0]


def test_guard_catches_a_mutated_business_object() -> None:
    """变异反证（新增 BO 侧）：改一个 BO 码 ⇒ 守卫必须报出来。"""
    path = _MIGRATIONS_DIR / f"{_MIGRATION_STEM}.py"
    original = path.read_text(encoding="utf-8")
    mutated = original.replace('\n        "POL",\n', '\n        "POLX",\n')
    assert mutated != original, "变异没生效（源码形态变了？）"
    module = _loadMigrationModule(mutated)
    problems = _drift(module)
    assert problems, "守卫没抓到变异 ⇒ 守卫是空集假绿"
    assert "新增 BO 不一致" in problems[0]


def test_guard_compares_non_empty_sets() -> None:
    """**正向守卫**：目录与迁移的名单都非空 ⇒ 守卫不是「空集相等」。"""
    module = _loadMigrationModule()
    assert module._REVIVE_CLASS_NAMES, "迁移的复活名单是空的"
    assert module._NEW_BUSINESS_OBJECTS, "迁移的新增 BO 列表是空的"
    assert module._REPOINTED_BUSINESS_OBJECTS, "迁移的改指列表是空的"
    assert REVIVED_CLASS_NAMES
    assert NEW_BUSINESS_OBJECTS
    assert REPOINTED_BUSINESS_OBJECTS


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_revived_class_names_are_sorted_and_stable() -> None:
    assert revivedClassNames() == tuple(sorted(REVIVED_CLASS_NAMES))


def test_object_spec_returns_none_for_unknown_key() -> None:
    assert objectSpec("NOT_A_PROCUREMENT_OBJECT") is None


def test_business_object_header_classes_cover_repoint_targets() -> None:
    mapping = businessObjectHeaderClasses()
    for spec in REPOINTED_BUSINESS_OBJECTS:
        assert mapping[spec.businessObjectCode] == spec.toClassName


@pytest.mark.parametrize("key", [spec.objectKey for spec in PROCUREMENT_OBJECT_SPECS])
def test_every_object_key_resolves(key: str) -> None:
    assert objectSpec(key) is not None
