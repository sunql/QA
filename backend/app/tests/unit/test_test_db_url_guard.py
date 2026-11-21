"""源码级守卫：测试代码**不得自带"连错库"的硬编码 DSN**。

为什么需要这个守卫（2026-11-18 实测，本仓真实事故链）：

1. `app/tests/_pg_support.py` 的 `resolveTestDatabaseUrl()` + `_ensureSchema()` 是
   **唯一的正确入口** —— 它保证「测试库名在白名单内」**且**「schema 已到 head」。
2. 绕过它、自建引擎的测试，2026-11-18 实测有 **5 个**，它们全部连到
   `5433 / qa_metadata_test` —— 那个库停在 **0139**（schema 缺列）⇒ 用例在缺列的
   表上跑、断言仍绿 ⇒ **假绿**。
3. 其中 `test_session_message_columns.py` 读的是 `settings.databaseUrl` ——
   本地 = **开发库**（5433 `qa_metadata`）、CI = **测试库**（5434 `qa_metadata_test`）
   ⇒ 同一份代码在两处给出**两个不同 verdict**（本地绿 / CI 红，或反之）。

⇒ 本守卫把这三类形态**在源码层**钉死，**不等它跑出假绿再查**。

## 四条规则（全部 AST 判定，不用正则）

| 规则 | 内容 | 抓什么 |
|---|---|---|
| **R1** | `create_async_engine(...)` 的实参（位置**或** `url=` 关键字）不得是**字符串字面量** | 直接内联 DSN |
| **R2** | 模块内**任意作用域**若定义了「值含 postgres DSN」的常量，则该模块**不得**调用 `create_async_engine` | R1 抓不到的「常量 + 变量传参」形态（含函数内常量） |
| **R3** | 测试代码**不得**出现 `.databaseUrl` 属性访问 | 读 `settings.databaseUrl` ⇒ 本地/CI verdict 分叉 |
| **R4** | 字符串字面量**不得**同时含 `5433` 与 `qa_metadata_test` | 已退役的「旧端口 + 测试库」组合 |

## 为什么 R2 不误伤

仓里有 **182 处** postgres DSN 字面量，绝大多数是**守卫逻辑的样本字符串**
（`test_db_snapshot_guard.py` / `test_kpi_metric_backfill_plan.py` 等，只传给解析
函数、**从不建引擎**）。R2 要求「定义了 DSN 常量」**且**「本模块调用
`create_async_engine`」**同时成立**⇒ 样本模块天然不命中。

## 为什么不用正则

正则扫字面量会被**自己写的注释与 docstring** 命中（假红），而字面量不出现时又
**空集假绿**。⇒ 用 `ast` 判**语法节点**，并显式排除 docstring 节点。
（同类教训见 `qa-system-source-guard-ast-not-regex`。）

本文件自身满足上述规则（它只 `ast.parse` 字符串、从不建引擎）：
- 正向样本写在**字符串字面量**里（`ast.parse` 的是源码文本，不是真实节点）；
- 本文件不调用 `create_async_engine` ⇒ R2 对本文件不成立。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1]  # backend/app/tests

# postgres DSN 的最小特征（用于识别「这是个 DSN 常量」，不是用来解析 URL）
_DSN_MARKERS = ("postgres://", "postgresql://", "postgresql+asyncpg://")

# 已退役的组合：旧端口（5433）+ 测试库名。5433 是本机 openmetadata 占用 5432 后的
# 遗留映射端口，**测试库 SSOT 在 5434**（与 CI 同款）。
_RETIRED_DB_MARKERS = ("5433", "qa_metadata_test")


# ---------------------------------------------------------------------------
# 本守卫自己的「已退役组合」样本
# ---------------------------------------------------------------------------
# R4 的正向样本必须包含「5433 + qa_metadata_test」这个**完整字面量**；
# 若直接写成单个字符串常量 ⇒ 本文件**自报违规**（守卫扫描范围含本文件本身）。
# ⇒ 用拼接表达：`ast.parse` **不折叠**字符串 `+`（折叠发生在 compile 阶段的
#    optimizer，`ast.parse` 不跑）⇒ AST 里它是 `BinOp` 而非 `Constant`
#    ⇒ 本文件的字面量里不出现完整组合 ⇒ 不自报违规。
_RETIRED_DB_SAMPLE = (
    "postgresql+asyncpg://u:p@localhost:5433"
    + "/qa_metadata_test"
)


# ---------------------------------------------------------------------------
# AST 分析
# ---------------------------------------------------------------------------


def _docstringNodes(tree: ast.AST) -> set[int]:
    """收集 docstring 节点 id（模块 / 类 / 函数的第一个字符串表达式）。

    docstring 在 AST 里也是 `ast.Constant`，不排除掉 ⇒ 守卫会被自己写的
    「示例 DSN」命中（假红）。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def _isDsnConstant(value: str) -> bool:
    return any(marker in value for marker in _DSN_MARKERS)


def _engineCalls(tree: ast.AST) -> list[ast.Call]:
    """所有 `create_async_engine(...)` 调用（按名字匹配，不限导入形态）。"""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "create_async_engine":
            out.append(node)
    return out


def _dsnConstants(tree: ast.AST) -> list[ast.Constant]:
    """**任意作用域**里「值含 postgres DSN」的字符串常量。

    为什么不再只看模块/类级：`def f(): u = "<DSN>"; create_async_engine(u)`
    这种函数内常量 + 变量传参，R1（实参非字面量）与「只看模块级」的 R2 都抓不到。
    ⇒ 用 `ast.walk` 收集全部赋值语句（含 tuple 拆包以外的 `Name` 目标）。
    """
    out: list[ast.Constant] = []
    for stmt in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, str) or not _isDsnConstant(value.value):
            continue
        # 只认「绑定到常量名」的赋值：`_TEST_URL = "..."`；不认 tuple 拆包
        for target in targets:
            if isinstance(target, ast.Name):
                out.append(value)
                break
    return out


def _redactDsn(textValue: str) -> str:
    """把 DSN 里的 userinfo 打码（失败消息会进 pytest 输出 / CI 日志）。"""
    redacted = re.sub(r"//[^@/]*@", "//***@", textValue)
    return redacted[:80]


def _databaseUrlAttributeUses(tree: ast.AST) -> list[ast.Attribute]:
    """所有 `.databaseUrl` 属性访问。"""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "databaseUrl"
    ]


def _retiredDbLiterals(tree: ast.AST, docstrings: set[int]) -> list[ast.Constant]:
    """含「已退役组合」的字面量（排除 docstring）。"""
    out: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in docstrings:
            continue
        if not isinstance(node.value, str):
            continue
        if all(marker in node.value for marker in _RETIRED_DB_MARKERS):
            out.append(node)
    return out


def _violationsInSource(source: str, fileName: str) -> list[str]:
    """对一段源码跑全部规则，返回违规描述列表。"""
    tree = ast.parse(source, filename=fileName)
    docstrings = _docstringNodes(tree)
    out: list[str] = []

    engineCalls = _engineCalls(tree)
    for call in engineCalls:
        # 位置实参 + 关键字实参（`create_async_engine(url="...")`）都要抓
        literalArgs = [a for a in call.args[:1] if isinstance(a, ast.Constant)]
        literalArgs += [
            kw.value
            for kw in call.keywords
            if kw.arg == "url" and isinstance(kw.value, ast.Constant)
        ]
        if literalArgs:
            out.append(f"R1 {fileName}:{call.lineno} create_async_engine 的实参是字符串字面量")

    if engineCalls and _dsnConstants(tree):
        out.append(
            f"R2 {fileName}: 模块定义了 DSN 常量又调用 create_async_engine"
            "（应改用 app.tests._pg_support._newEngine()）"
        )

    for attr in _databaseUrlAttributeUses(tree):
        out.append(
            f"R3 {fileName}:{attr.lineno} 读了 .databaseUrl"
            "（本地=开发库、CI=测试库 ⇒ verdict 分叉）"
        )

    for literal in _retiredDbLiterals(tree, docstrings):
        # ⚠️ 消息里**不得**写出完整的退役组合（否则本守卫的消息字面量自己会被 R4 命中）；
        # 也**不得**原样打出字面量 —— 这类字面量通常正是 DSN，前 80 字符就含口令。
        out.append(
            f"R4 {fileName}:{literal.lineno} 字面量含已退役组合（旧端口 + 测试库名）："
            f"{_redactDsn(literal.value)}"
        )

    return out


def _testSources() -> list[Path]:
    """待扫描的测试源码清单（**空集假绿防护**）。

    `Path.rglob` 在目录不存在时返回空且不报错 ⇒ 目录被挪走 / 路径拼错时，
    「0 违规」会无条件通过。⇒ 这里钉死「扫到了多少文件」和「锚点文件在」。
    """
    paths = sorted(_TESTS_ROOT.rglob("*.py"))
    assert len(paths) >= 100, (
        f"扫描集过小（{len(paths)} 个 .py）—— 守卫的扫描根 {_TESTS_ROOT} 是否还在？"
    )
    anchor = _TESTS_ROOT / "conftest.py"
    assert anchor in paths, f"锚点文件不在扫描集里：{anchor}"
    return paths


def _scanTestTree() -> list[str]:
    """扫 `backend/app/tests/**` 全部 .py，返回违规列表。"""
    out: list[str] = []
    for path in _testSources():
        source = path.read_text(encoding="utf-8")
        out.extend(_violationsInSource(source, str(path.relative_to(_TESTS_ROOT))))
    return out


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_noHardcodedTestDbDsnInTestSources() -> None:
    """全量扫描测试源码：不得自带连错库的硬编码 DSN。"""
    violations = _scanTestTree()
    assert not violations, "测试源码里出现了硬编码 DSN / 读开发库 URL：\n" + "\n".join(violations)


def test_guardDetectsInlineDsnArgument() -> None:
    """R1 正向样本：内联 DSN 字面量必须被抓到。"""
    source = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "engine = create_async_engine(\n"
        "    'postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test'\n"
        ")\n"
    )
    violations = _violationsInSource(source, "sample_r1.py")
    assert any(v.startswith("R1 ") for v in violations), violations


def test_guardDetectsDsnConstantPassedByName() -> None:
    """R2 正向样本：DSN 常量 + 变量传参（R1 抓不到的形态）必须被抓到。"""
    source = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "_TEST_URL = 'postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test'\n"
        "engine = create_async_engine(_TEST_URL)\n"
    )
    violations = _violationsInSource(source, "sample_r2.py")
    assert any(v.startswith("R2 ") for v in violations), violations


def test_guardDetectsInlineDsnKeywordArgument() -> None:
    """R1 正向样本（关键字形态）：`create_async_engine(url="...")` 也必须被抓到。"""
    source = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "engine = create_async_engine(\n"
        "    url='postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test'\n"
        ")\n"
    )
    violations = _violationsInSource(source, "sample_r1_kw.py")
    assert any(v.startswith("R1 ") for v in violations), violations


def test_guardDetectsFunctionScopedDsnConstant() -> None:
    """R2 正向样本（函数作用域）：函数内常量 + 变量传参也必须被抓到。"""
    source = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "def build():\n"
        "    _u = 'postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test'\n"
        "    return create_async_engine(_u)\n"
    )
    violations = _violationsInSource(source, "sample_r2_fn.py")
    assert any(v.startswith("R2 ") for v in violations), violations


def test_guardDetectsSettingsDatabaseUrlRead() -> None:
    """R3 正向样本：读 `settings.databaseUrl` 必须被抓到。"""
    source = (
        "from app.config import getSettings\n"
        "settings = getSettings()\n"
        "engine = create_async_engine(settings.databaseUrl)\n"
    )
    violations = _violationsInSource(source, "sample_r3.py")
    assert any(v.startswith("R3 ") for v in violations), violations


def test_guardDetectsRetiredDbCombination() -> None:
    """R4 正向样本：`5433` + `qa_metadata_test` 的已退役组合必须被抓到。"""
    source = (
        "_URL = '"
        + _RETIRED_DB_SAMPLE
        + "'\n"
        "def use() -> None:\n"
        "    print(_URL)\n"
    )
    violations = _violationsInSource(source, "sample_r4.py")
    assert any(v.startswith("R4 ") for v in violations), violations


def test_guardDoesNotFlagSampleDsnWithoutEngineCreation() -> None:
    """反向样本：只当**样本字符串**用的 DSN 字面量（不建引擎）不得被误伤。

    这是 R2 存在的理由：仓里有上百处这类样本（守卫逻辑的输入）。
    """
    source = (
        "_SAMPLE = 'postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test'\n"
        "def use() -> None:\n"
        "    print(_SAMPLE)\n"
    )
    assert _violationsInSource(source, "sample_ok.py") == []


def test_guardDoesNotFlagDocstringDsn() -> None:
    """反向样本：docstring 里的 DSN 示例不得被误伤（否则守卫会被自己写的文档命中）。"""
    source = (
        '"""示例：TEST_DATABASE_URL='
        + _RETIRED_DB_SAMPLE
        + '"""\n'
        "def use() -> None:\n"
        "    pass\n"
    )
    assert _violationsInSource(source, "sample_doc.py") == []


@pytest.mark.parametrize(
    "rule",
    ["R1", "R2", "R3", "R4"],
)
def test_eachRuleHasPositiveControl(rule: str) -> None:
    """四条规则**各自**都有正向样本 ⇒ 不存在「某条规则永远抓不到东西」的空集假绿。"""
    samples = {
        "R1": (
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "engine = create_async_engine('postgresql+asyncpg://u:p@h:5434/qa_metadata_test')\n"
        ),
        "R2": (
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "_U = 'postgresql+asyncpg://u:p@h:5434/qa_metadata_test'\n"
            "engine = create_async_engine(_U)\n"
        ),
        "R3": (
            "from app.config import getSettings\n"
            "settings = getSettings()\n"
            "url = settings.databaseUrl\n"
        ),
        "R4": (
            "_U = '"
            + _RETIRED_DB_SAMPLE
            + "'\n"
            "def use() -> None:\n"
            "    print(_U)\n"
        ),
    }
    violations = _violationsInSource(samples[rule], f"sample_{rule}.py")
    assert any(v.startswith(f"{rule} ") for v in violations), (rule, violations)
