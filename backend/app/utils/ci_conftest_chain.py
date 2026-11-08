"""conftest「收集前 import 链」的派生器（第九/十二轮复审的结构性收口）。

第九轮（MEDIUM-1）：AST 扫所有 conftest 的**模块级** `app.*` / `scripts.*` 导入，
守卫（`test_ci_conftest_chain.py`）断言每个落点「在 CI 相关面清单或显式豁免」——
逐轮枚举文件名已被实证连漏三层，改为派生。

第十二轮复审补两类洞（枚举/一层派生仍然不够）：
- **祖先包 init 链**：`from app.domain.models import X` 会**先执行**
  `app/__init__.py`、`app/domain/__init__.py` 再执行叶子模块 —— 落点必须解析
  **整条祖先链**，不只叶子（否则 `app/domain/__init__.py` 这类文件逃出）。
- **传递闭包**：闭包不能止于 conftest 自己的顶层 —— 落点模块自己还会 import
  别的模块（`app.domain.models` 又拉起 `models/rbac`、`wiki_*_models` 等
  数十个文件，它们同样在收集前执行）。`transitivePreCollectionPaths` 沿
  import 边 BFS 到不动点；守卫对闭包内每个文件断言「在清单里**或**模块顶层
  只含定义性代码」（`findImportTimeHooks`）—— 58 个产品文件全入清单会让所有
  ORM / 服务 PR 判退出码 6 ⇒ 内容不变式取代逐文件枚举。

口径（两级派生共用）：
- 「模块级」= 不落进函数/类体的语句：`If`/`Try`/`With`/`For`/`While` 的 body
  递归看（`try: import x` 在收集前同样执行），`FunctionDef`/`AsyncFunctionDef`
  /`ClassDef` 的 **body 不看**（函数体在调用时才执行；类体由本模块的语句规则
  单独看）。
- `from X import name` 解析为两种落点：X 的 `__init__.py`（包执行）与
  `X/name.py`（子模块执行）；两者都解析不到 ⇒ name 是符号不是模块 ⇒ 跳过。
- `pytest_plugins = ["m"]`（字符串或字符串清单）声明的插件也在收集前加载 ⇒ 一并解析。
- 非 `app.*` / `scripts.*` 顶层前缀不派生：conftest 本身在 CI 相关面清单里
  （任意深度 conftest pattern）⇒ conftest 新增别的顶层 import 本身就是命中；
  `seed_*` 已由清单通配管住；第三方库改不了仓库文件。
- 解析失败 / 语法错误 ⇒ 抛错（fail-closed：静默跳过 = 该链路消失）。
"""

from __future__ import annotations

import ast
from pathlib import Path

# 只派生这两棵顶层包的导入链：`app.*` 是被测应用（conftest 直接 import），
# `scripts.*` 是测试基建（conftest / `_pg_support` 直接 import）。
_DERIVED_TOP_PREFIXES = ("app", "scripts")

# 顶层语句允许出现的形态（findImportTimeHooks 的白名单）：
# 导入 / 定义 / 常量与单例赋值 / docstring / 受控的 If-Try（递归看 body）。
_ALLOWED_STATEMENTS = (
    ast.Import,
    ast.ImportFrom,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Assign,
    ast.AnnAssign,
    ast.Pass,
)

# 收集前执行的**已知钩子机制**（按根链拒绝，denylist 而非 purity 宣称：
# 顶层纯构造——getLogger / re.compile / 单例构造——是常态，全拒会假红）。
_DENIED_ROOTS = (
    "sys.path",
    "sys.modules",
    "os.environ",
    "importlib",
    "setattr",
    "delattr",
    "getattr",
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "open",
    "_pytest",
    "monkeypatch",
    "subprocess",
)

# 顶层裸表达式（Expr-Call）只允许这些方法名（pydantic v2 的模型重建惯例）。
_ALLOWED_EXPR_METHODS = ("model_rebuild",)


def _dottedChain(node: ast.expr) -> str | None:
    """`sys.path.insert` → `sys.path.insert`；根不是 Name ⇒ None（动态表达式）。"""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _isDeniedChain(chain: str) -> bool:
    return any(chain == root or chain.startswith(f"{root}.") for root in _DENIED_ROOTS)


def _moduleScopeStatements(body: list[ast.stmt]):
    """展开模块级作用域：If/Try/With/For/While 的分支递归，不进函数/类体。

    类体单独按同一规则看（类体语句也是 import 时执行）；方法体不看。
    """
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While)):
            yield from _moduleScopeStatements(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _moduleScopeStatements(handler.body)
            yield from _moduleScopeStatements(getattr(node, "orelse", []))
            yield from _moduleScopeStatements(getattr(node, "finalbody", []))
        elif isinstance(node, ast.ClassDef):
            yield from _moduleScopeStatements(node.body)


def _iterImportTimeNodes(stmt: ast.stmt):
    """语句内所有 import 期求值的节点（含装饰器/默认值；**不含**函数体）。"""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for decorator in stmt.decorator_list:
            yield decorator
        args = stmt.args
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if a.annotation:
                yield a.annotation
        for d in (*args.defaults, *args.kw_defaults):
            if d:
                yield d
        if stmt.returns:
            yield stmt.returns
        return  # 函数体在调用时才执行 —— 不看
    yield stmt
    for child in ast.iter_child_nodes(stmt):
        yield from _iterImportTimeNodes(child)


def _resolveModuleFiles(backendRoot: Path, moduleName: str) -> tuple[str, ...]:
    """模块名 → import 它会执行的**全部**仓库相对文件（祖先包 init 链 + 叶子）。

    `app.domain.models` → `app/__init__.py`、`app/domain/__init__.py`（存在的才收）
    + `app/domain/models.py`（叶子；纯模块优先于包，与「先试 .py 再试包」一致）。
    """
    parts = moduleName.split(".")
    out: list[str] = []
    for i in range(1, len(parts)):
        pkgInit = backendRoot.joinpath(*parts[:i], "__init__.py")
        if pkgInit.is_file():
            out.append(pkgInit.relative_to(backendRoot.parent).as_posix())
    leafPy = backendRoot.joinpath(*parts).with_suffix(".py")
    leafInit = backendRoot.joinpath(*parts, "__init__.py")
    if leafPy.is_file():
        out.append(leafPy.relative_to(backendRoot.parent).as_posix())
    elif leafInit.is_file():
        out.append(leafInit.relative_to(backendRoot.parent).as_posix())
    return tuple(out)


def _importedModuleNames(path: Path) -> tuple[str, ...]:
    """一个文件在**模块级作用域**（含 If/Try 分支）导入的 app.*/scripts.* 模块名
    + `pytest_plugins` 字符串清单里声明的前缀匹配插件。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in _moduleScopeStatements(tree.body):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in _DERIVED_TOP_PREFIXES:
                names.append(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        names.append(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _DERIVED_TOP_PREFIXES:
                    names.append(alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(getattr(t, "id", None) == "pytest_plugins" for t in targets):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                entries: list[object] = [value.value]
            elif isinstance(value, (ast.List, ast.Tuple)):
                entries = list(value.elts)
            else:
                entries = []
            names.extend(
                e.value
                for e in entries
                if isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and e.value.split(".")[0] in _DERIVED_TOP_PREFIXES
            )
    return tuple(names)


def _conftestFiles(backendRoot: Path) -> list[Path]:
    out: list[Path] = []
    for conftest in sorted(backendRoot.rglob("conftest.py")):
        relParts = conftest.relative_to(backendRoot).parts[:-1]
        if any(part.startswith(".") for part in relParts):
            continue  # 隐藏目录（.venv 等）不是测试树
        out.append(conftest)
    return out


def preCollectionImportPaths(backendRoot: Path) -> dict[str, tuple[str, ...]]:
    """派生 `backendRoot` 下**所有** conftest 的收集前 import 落点（一层）。

    返回 `{conftest 仓库相对路径: (落点文件仓库相对路径, …)（升序）}`；
    没有可派生落点的 conftest 不出现在结果里。落点含**祖先包 init 链**。
    """
    result: dict[str, tuple[str, ...]] = {}
    for conftest in _conftestFiles(backendRoot):
        files: set[str] = set()
        for moduleName in _importedModuleNames(conftest):
            files.update(_resolveModuleFiles(backendRoot, moduleName))
        if files:
            conftestRel = conftest.relative_to(backendRoot.parent).as_posix()
            result[conftestRel] = tuple(sorted(files))
    return result


def transitivePreCollectionPaths(backendRoot: Path) -> tuple[str, ...]:
    """传递闭包：conftest 收集前会执行的**全部**仓库文件（沿 import 边到不动点）。

    一层落点（含祖先 init）再做 BFS —— 落点模块自己的模块级 import 也发生在
    收集前。返回升序 tuple（仓库相对路径）。
    """
    closure: set[str] = set()
    frontier: list[str] = []
    for files in preCollectionImportPaths(backendRoot).values():
        for f in files:
            if f not in closure:
                closure.add(f)
                frontier.append(f)
    while frontier:
        current = Path(backendRoot.parent) / frontier.pop()
        for moduleName in _importedModuleNames(current):
            for resolved in _resolveModuleFiles(backendRoot, moduleName):
                if resolved not in closure:
                    closure.add(resolved)
                    frontier.append(resolved)
    return tuple(sorted(closure))


def findImportTimeHooks(path: Path) -> tuple[str, ...]:
    """模块顶层「定义性之外」的 import 期代码（空 tuple = 定义性）。

    白名单语句形态 + 已知钩子机制根链拒绝（见模块 docstring 与常量注释）。
    守卫用它在传递闭包上断言内容不变式（第十二轮复审 HIGH-2/MEDIUM-4）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hooks: list[str] = []
    for stmt in _moduleScopeStatements(tree.body):
        if isinstance(stmt, _ALLOWED_STATEMENTS):
            pass
        elif isinstance(stmt, ast.Expr):
            value = stmt.value
            isDocstring = isinstance(value, ast.Constant) and isinstance(value.value, str)
            isAllowedCall = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in _ALLOWED_EXPR_METHODS
            )
            if not (isDocstring or isAllowedCall):
                hooks.append(f"line {stmt.lineno}: 顶层表达式 {ast.unparse(stmt)[:60]}")
            continue
        elif isinstance(stmt, (ast.If, ast.Try)):
            continue  # 分支体已由 _moduleScopeStatements 展开逐句看
        else:
            hooks.append(f"line {stmt.lineno}: 顶层 {type(stmt).__name__} {ast.unparse(stmt)[:60]}")
            continue
        for node in _iterImportTimeNodes(stmt):
            if isinstance(node, ast.Name) and node.id in _DENIED_ROOTS:
                hooks.append(f"line {node.lineno}: 顶层引用 {node.id}")
            elif isinstance(node, ast.Attribute):
                chain = _dottedChain(node)
                if chain and _isDeniedChain(chain):
                    hooks.append(f"line {node.lineno}: 顶层引用 {chain}")
    return tuple(hooks)
