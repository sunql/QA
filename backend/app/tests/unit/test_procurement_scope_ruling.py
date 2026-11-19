"""采购范围裁决守卫（M1-1.6 / 准则 H + D1–D3）。

钉住什么
--------
M1-a DoD 第 5 条：「本里程碑**没有新建任何与 ``semantic_release`` / ``workflow_*``
语义重复的表**（守卫用例证明）」。准则 H 的裁决原文在计划文档里 —— 文档挡不住
「下一个人照 V0.3 §7.3 草案把 ``procurement_workflow_definition`` 建出来」，
双写双口径正是裁决要防的事 ⇒ 裁决**数据化**为
``app/domain/procurement_scope_ruling.py``，本文件把它变成可执行红线：

1. **禁建表**（10 张）：任何 ``__tablename__`` / 迁移 ``op.create_table`` / 迁移裸 SQL
   ``CREATE TABLE`` 出现 ⇒ 红；
2. **批准表**（3 张）：必须在对应迁移里**真实** ``CREATE TABLE``（反向空集守卫：
   目录说建、迁移里没有 = 目录漂移）；
3. **延后表**（2 张，M6 前禁建）；
4. **未入册的 ``procurement_*`` 新表**（迁移本地 journal 表除外）⇒ 红
   —— 建表必须先过裁决，不许绕开目录发明 ``procurement_xxx``。

为什么用 AST 不用正则：正则会扫到**自己写的注释**里的表名（0155 的 docstring
就提到改名前名）⇒ 假红；字面量不出现又空集假绿（既有结论：源码级守卫用 AST）。

变异反证：合成违规样本必须被抓到 —— 否则「守卫恒通过」只是「什么都没比」。

为什么本文件不碰 DB：全部是**源码断言**（``app/`` + ``scripts/`` + ``alembic/versions/``）
⇒ 不依赖测试库，也不受 unit conftest 的 TRUNCATE/seed 竞态影响。

守卫的已知边界（评审 LOW-3；当前仓内均不存在，若引入须先扩展扫描器）
----------------------------------------------------------------
1. ``op.create_table`` 首参为**不可解析形态**（函数调用 / f-string / 属性访问）时
   该调用被静默跳过 —— 无 fail-loud 计数器（YAGNI：仓内建表迁移首参只有字面量与
   模块级字符串常量两种形态，见 ``_createTableNames``）；
2. 模块级常量若定义在 ``if`` / ``try`` 块内（``col_offset != 0``）不入册；
3. ``sa.Table("procurement_x", ...)`` 与 ``scripts/*.sql`` 等**非 .py** DDL 不在扫描面。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.domain.procurement_scope_ruling import (
    APPROVED_TABLES,
    DEFERRED_TO_M6_TABLES,
    FORBIDDEN_DUPLICATE_TABLE_NAMES,
    FORBIDDEN_DUPLICATE_TABLES,
    GOVERNED_PROCUREMENT_TABLE_NAMES,
    rulingFor,
)

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCAN_ROOTS = (_BACKEND_ROOT / "app", _BACKEND_ROOT / "scripts", _BACKEND_ROOT / "alembic")
_MIGRATIONS_DIR = _BACKEND_ROOT / "alembic" / "versions"

# 迁移**自带自删**的本地表（如 0158 的 journal）：以 ``procurement_`` 开头但不属于
# V0.3 §7.3 草案 ⇒ 不需要裁决，显式豁免（守卫若不豁免会把合法迁移误判为绕裁决）。
_MIGRATION_LOCAL_TABLES = frozenset({"procurement_object_reconciliation_journal"})

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?\"?(\w+)\"?", re.IGNORECASE
)


def _iterPythonFiles() -> list[pathlib.Path]:
    """扫描根 = app/（**排除 tests/**——守卫自己的反证样本字符串会被扫到 ⇒ self-hit 假红）
    + scripts/ + alembic/。"""
    files: list[pathlib.Path] = []
    for root in _SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            files.append(path)
    return sorted(files)


def _tablenameValues(path: pathlib.Path) -> list[tuple[int, str]]:
    """AST 提取 ``__tablename__ = "..."`` 赋值（含 AnnAssign）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "__tablename__"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.append((node.lineno, node.value.value))
    return found


def _createTableNames(path: pathlib.Path) -> list[tuple[int, str]]:
    """AST 提取迁移里的建表名：``op.create_table("x", ...)`` 首参 + 裸 SQL CREATE TABLE。

    首参允许是**模块级字符串常量变量**（0155 用 ``_TABLE``、0154 用 ``_TABLE_SOURCE``）
    —— 只认字面量会漏掉这种形态 ⇒ 反向空集守卫假红。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    moduleConsts: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create_table" and node.args:
                first = node.args[0]
                name: str | None = None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value
                elif isinstance(first, ast.Name) and first.id in moduleConsts:
                    name = moduleConsts[first.id]
                if name is not None:
                    found.append((node.lineno, name))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # 顶层 `_TABLE = "..."` 常量登记（模块级才可信；函数内赋值不入册）
            if node.col_offset == 0:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    moduleConsts[targets[0].id] = node.value.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _CREATE_TABLE_RE.finditer(node.value):
                found.append((node.lineno, match.group(1)))
    return found


def _offendingDefinitions(tableName: str) -> list[str]:
    """全仓扫描某表名的违规定义点（``relpath:lineno``）。"""
    offenders: list[str] = []
    for path in _iterPythonFiles():
        rel = path.relative_to(_BACKEND_ROOT).as_posix()
        for lineno, value in _tablenameValues(path):
            if value == tableName:
                offenders.append(f"{rel}:{lineno}: __tablename__={value!r}")
        for lineno, value in _createTableNames(path):
            if value == tableName:
                offenders.append(f"{rel}:{lineno}: CREATE TABLE {value}")
    return offenders


# ---------------------------------------------------------------------------
# 目录自身的不变量
# ---------------------------------------------------------------------------


def test_ruling_sets_are_disjoint_and_prefixed() -> None:
    """禁建/批准/延后三集合互斥；表名都在 ``procurement_`` 词表内（批准表除外）。"""
    forbidden = set(FORBIDDEN_DUPLICATE_TABLE_NAMES)
    approved = {r.tableName for r in APPROVED_TABLES}
    deferred = {r.tableName for r in DEFERRED_TO_M6_TABLES}
    assert not forbidden & approved, f"同一张表既禁建又批准：{forbidden & approved}"
    assert not forbidden & deferred and not approved & deferred
    for name in forbidden | deferred:
        assert name.startswith("procurement_"), f"{name} 不在采购词表内"


def test_every_ruling_carries_replacement_and_reason() -> None:
    """裁决必须可复核：替代目标与理由不许空（否则评审时无从裁决）。"""
    for ruling in (*FORBIDDEN_DUPLICATE_TABLES, *APPROVED_TABLES, *DEFERRED_TO_M6_TABLES):
        assert ruling.replacement, f"{ruling.tableName} 缺替代目标"
        assert ruling.reason, f"{ruling.tableName} 缺裁决理由"


@pytest.mark.parametrize("tableName", sorted(GOVERNED_PROCUREMENT_TABLE_NAMES))
def test_governed_names_resolve_to_a_ruling(tableName: str) -> None:
    """管控名单里的每个名字都能查到裁决（rulingFor 不许漏）。"""
    assert rulingFor(tableName) is not None


# ---------------------------------------------------------------------------
# 红线一：禁建表不许出现在任何定义点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tableName", sorted(FORBIDDEN_DUPLICATE_TABLE_NAMES))
def test_forbidden_table_is_not_defined_anywhere(tableName: str) -> None:
    """准则 H 红线：与既有表语义重复的草案表，任何 ``__tablename__`` / 建表都不许出现。"""
    offenders = _offendingDefinitions(tableName)
    assert offenders == [], (
        f"准则 H：{tableName} 已裁决不建（复用 {rulingFor(tableName).replacement}），"
        f"却出现定义点：\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("tableName", sorted(r.tableName for r in DEFERRED_TO_M6_TABLES))
def test_deferred_table_is_not_defined_before_m6(tableName: str) -> None:
    """D3 延后表：M6 裁决前不许建；M6 若要建，**先改目录再动表**。"""
    offenders = _offendingDefinitions(tableName)
    assert offenders == [], f"D3：{tableName} 延后到 M6 决策，M6 前不许建表。定义点：\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------------------
# 红线二：批准表必须真实存在（反向空集守卫）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ruling", APPROVED_TABLES, ids=lambda r: r.tableName)
def test_approved_table_is_actually_created(ruling) -> None:
    """目录说「建」的表，对应迁移里必须真有 CREATE TABLE（目录漂移即红）。"""
    match = re.match(r"0\d{3}", ruling.replacement)
    assert match, f"{ruling.tableName} 的 replacement 未以迁移号开头：{ruling.replacement!r}"
    matches = sorted(_MIGRATIONS_DIR.glob(f"{match.group(0)}_*.py"))
    assert matches, f"迁移 {match.group(0)} 不存在"
    created = {name for path in matches for _, name in _createTableNames(path)}
    assert ruling.tableName in created, (
        f"目录声称 {ruling.tableName} 由 {matches[0].name} 建，但该迁移实际建表：{sorted(created)}"
    )


# ---------------------------------------------------------------------------
# 红线三：``procurement_*`` 新表必须先入册（不许绕开裁决发明表）
# ---------------------------------------------------------------------------


def test_no_untracked_procurement_table_definitions() -> None:
    """任何 ``procurement_*`` 表的定义点必须在裁决目录里（迁移本地表除外）。

    这是准则 H 的**闭环**：只查已入册的名字挡不住「另起名建一张语义重复的表」；
    反向把全部 ``procurement_*`` 定义点收进目录 ⇒ 建表必须先过裁决。
    """
    offenders: list[str] = []
    for path in _iterPythonFiles():
        rel = path.relative_to(_BACKEND_ROOT).as_posix()
        for lineno, value in (*_tablenameValues(path), *_createTableNames(path)):
            if not value.startswith("procurement_"):
                continue
            if value in GOVERNED_PROCUREMENT_TABLE_NAMES:
                continue
            if value in _MIGRATION_LOCAL_TABLES:
                continue
            offenders.append(f"{rel}:{lineno}: {value}")
    assert offenders == [], (
        "出现未入册的 procurement_* 表定义 ⇒ 先在 procurement_scope_ruling.py "
        "补裁决再建表（准则 H：不建重复语义的表）：\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 变异反证：扫描器必须真的能抓（否则以上全绿 = 空集假绿）
# ---------------------------------------------------------------------------


def test_scanner_catches_synthetic_tablename_violation(tmp_path: pathlib.Path) -> None:
    """合成一个定义禁建表的模块，扫描器必须报出（拿真文件跑，不走 mock）。"""
    module = tmp_path / "synthetic_models.py"
    module.write_text(
        "class FakeWorkflowDefinition:\n    __tablename__ = 'procurement_workflow_definition'\n",
        encoding="utf-8",
    )
    found = _tablenameValues(module)
    assert found == [(2, "procurement_workflow_definition")]
    assert _offendingDefinitions("procurement_workflow_definition") == []
    # 把合成文件放进扫描根下再扫 ⇒ 必须被抓到
    scanRoot = _BACKEND_ROOT / "app"
    target = scanRoot / "_synthetic_violation_tmp.py"
    try:
        target.write_text(module.read_text(encoding="utf-8"), encoding="utf-8")
        offenders = _offendingDefinitions("procurement_workflow_definition")
        assert offenders, "扫描器漏报合成违规 ⇒ 守卫空转"
        assert any("__tablename__" in o for o in offenders)
    finally:
        target.unlink(missing_ok=True)


def test_scanner_catches_synthetic_create_table_violation(tmp_path: pathlib.Path) -> None:
    """合成 op.create_table / 裸 SQL 两种形态的禁建表迁移，都必须被抓到。"""
    module = tmp_path / "synthetic_migration.py"
    module.write_text(
        "def upgrade() -> None:\n"
        "    op.create_table('procurement_outbox_event', sa.Column('id', sa.Integer()))\n"
        '    op.execute("CREATE TABLE IF NOT EXISTS procurement_approval_record (id int)")\n',
        encoding="utf-8",
    )
    names = {name for _, name in _createTableNames(module)}
    assert "procurement_outbox_event" in names, "op.create_table 形态漏报"
    assert "procurement_approval_record" in names, "裸 SQL 形态漏报"


def test_scanner_does_not_flag_approved_tables() -> None:
    """反向空集守卫：批准表不许被禁建红线误伤，且其定义点必须真实可扫到。"""
    assert "semantic_release_check" not in FORBIDDEN_DUPLICATE_TABLE_NAMES
    offenders = _offendingDefinitions("semantic_release_check")
    assert offenders, "批准表连定义点都找不到 ⇒ 扫描器坏了"
    # 它只应出现在 ORM 定义与批准迁移里，且裁决可查
    assert rulingFor("semantic_release_check") is not None
    assert all(o.endswith("__tablename__='semantic_release_check'") or "CREATE TABLE" in o for o in offenders)
