"""conftest「收集前 import 链」的派生器与钩子检测器（第九/十二/十三轮复审的结构性收口）。

第九轮（MEDIUM-1）：AST 扫所有 conftest 的**模块级**导入，守卫
（`test_ci_conftest_chain.py`）断言每个落点「在 CI 相关面清单或显式豁免」——
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

第十三轮复审再补三类洞（位置 / 别名 / 派生边）：
- **位置全覆盖（HIGH-1）**：import 期求值不只发生在「语句本身」——
  `if sys.path.insert(0, "x"):`、`except <expr>:`、装饰器参数、默认值、注解、
  `with open(...)` 的 item、match 的 guard 都在 import 期执行。⇒ 结构白名单与
  根链拒绝**解耦**：白名单只决定「语句形态是否定义性」，根链拒绝对**所有**
  语句的 import 期表达式统一跑（`_iterExecNodes`：全子树，函数/Lambda 的
  body 除外——那要等到调用才执行）。
- **别名（HIGH-2）**：`import sys as s`、`from os import environ as e`、
  `from sys import path` 后裸写 `path.insert(...)` ⇒ 根链先过 `_aliasMap`
  映射再匹配拒绝清单。拒绝清单同时加宽：os 执行族、runpy / ctypes /
  builtins / shutil / socket / signal —— 任何一个在 import 期出现都不属于
  「定义性」；pathlib **不**拒（顶层 `Path(...)` 构造是常态，拒了全是假红）。
- **派生边（HIGH-3）**：相对导入按所在包拼绝对名（以前 `level == 0` 过滤把
  相对导入整条边丢了）；`from X import *` 展开 X 的 `__all__` 字符串
  （CPython 会连带 import 这些子模块）；顶层前缀不再硬编码 app/scripts ——
  「能解析到 backend/ 下文件」的任意模块都派生（解析不到的名字在
  `_resolveModuleFiles` 自然返回空集）。

**检测范围（口径，勿过度声明）**：本模块检测的是 **conftest 收集前 import
链的闭包**。**测试模块自身的模块级产品 import 不在派生范围**——那需要把闭包
扩到全部测试文件（实测 465 个、其中十余个一次性脚本带真实 `sys.path.insert`
钩子），超出本守卫的仲裁面预算；该弱化残余与「普通测试文件削弱」同类，见
change 记录 §5。类体的方法体、函数内重绑定别名同样不追（文档化局限）。

其余口径：
- 「模块级」= 不落进函数体的语句：If/Try/With/For/While/Match 的分支递归看
  （`try: import x` 在收集前同样执行），函数体不看；类体**看**（类体语句也是
  import 时执行），方法体不看。
- `from X import name` 解析为两种落点：X 的 `__init__.py`（包执行）与
  `X/name.py`（子模块执行）；两者都解析不到 ⇒ name 是符号不是模块 ⇒ 跳过。
- `pytest_plugins`（Assign/AugAssign，字符串或字符串清单）声明的插件也在
  收集前加载 ⇒ 一并解析。
- 解析失败 / 语法错误 ⇒ 抛错（fail-closed：静默跳过 = 该链路消失）。
"""

from __future__ import annotations

import ast
from pathlib import Path

# 分支语句（模块级作用域展开时递归进其分支体）。TryStar/Match 用 getattr 收集，
# 避免旧解释器 AttributeError（本仓库 3.11+，两者都有）。
_BRANCH_TYPES = tuple(
    t
    for t in (
        ast.If,
        ast.Try,
        getattr(ast, "TryStar", None),
        ast.With,
        ast.AsyncWith,
        ast.For,
        ast.While,
    )
    if t is not None
)

# 顶层语句允许出现的形态（findImportTimeHooks 的结构白名单）：
# 导入 / 定义 / 常量与单例赋值 / docstring / 受控的分支（递归看分支体）。
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
# 第十三轮 HIGH-2 加宽执行族 sink；pathlib 不拒（见模块 docstring）。
_DENIED_ROOTS = (
    "sys.path",
    "sys.modules",
    "sys.settrace",
    "sys.setprofile",
    "sys.audit",
    "os.environ",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "os.fork",
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
    "input",
    "breakpoint",
    "_pytest",
    "monkeypatch",
    "subprocess",
    "runpy",
    "ctypes",
    "builtins",
    "shutil",
    "socket",
    "signal",
)

# 顶层裸表达式（Expr-Call）只允许这些方法名，且**接收者必须是裸 Name**
# （`get_module().model_rebuild()` 这类动态接收者是结构问题——第十三轮 LOW-2；
# 点分接收者 `app.evil.model_rebuild()` 同样不放行）。
_ALLOWED_EXPR_METHODS = ("model_rebuild",)

# 类体里的「类创建钩子」：class 语句执行（import 期）时就会跑（第十三轮 HIGH-2）。
# 描述符 `__get__`/`__set__` 不算——那是实例访问期执行，不是类创建期。
_CLASS_CREATION_HOOK_NAMES = ("__init_subclass__", "__set_name__", "__class_getitem__")


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
    """展开模块级作用域：分支语句递归进分支体/except 子句/match case，不进函数体。

    类体单独按同一规则看（类体语句也是 import 时执行）；方法体不看。
    """
    for node in body:
        yield node
        if isinstance(node, ast.Match):
            for case in node.cases:
                if case.guard:
                    yield from _moduleScopeStatements([case.guard])
                yield from _moduleScopeStatements(case.body)
        elif isinstance(node, _BRANCH_TYPES):
            yield from _moduleScopeStatements(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _moduleScopeStatements(handler.body)
            yield from _moduleScopeStatements(getattr(node, "orelse", []))
            yield from _moduleScopeStatements(getattr(node, "finalbody", []))
        elif isinstance(node, ast.ClassDef):
            yield from _moduleScopeStatements(node.body)


def _aliasMap(body: list[ast.stmt]) -> dict[str, str]:
    """模块级 import 建立的「别名 → 真名」映射（第十三轮 HIGH-2）。

    `import sys as s` ⇒ s→sys；`from os import environ as e` ⇒ e→os.environ；
    **无别名的 from-import 也映射**（`from sys import path` 后裸写
    `path.insert(...)` 同样要能追到 sys.path）。只看模块级；函数内重绑定、
    相对导入的别名不追（文档化局限，见模块 docstring）。
    """
    aliases: dict[str, str] = {}
    for stmt in _moduleScopeStatements(body):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            for alias in stmt.names:
                aliases[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
    return aliases


def _iterExecNodes(node: ast.AST):
    """import 期会执行的节点：全子树，函数/lambda 的 body 除外（调用时才执行）。

    第十三轮 HIGH-1：函数的装饰器 / 参数默认值 / 注解 / 返回值注解要**递归**
    覆盖——`@d(__import__("app.evil"))`、`def f(x=sys.path.insert(0, "x"))`
    这些位置以前只 yield 节点本身不进子树 ⇒ 漏扫。
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        yield node
        args = node.args
        for dec in getattr(node, "decorator_list", []):
            yield from _iterExecNodes(dec)
        for d in (*args.defaults, *args.kw_defaults):
            if d is not None:
                yield from _iterExecNodes(d)
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if a.annotation:
                yield from _iterExecNodes(a.annotation)
        if getattr(node, "returns", None):
            yield from _iterExecNodes(node.returns)
        return  # body 不看
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _iterExecNodes(child)


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


def _packageOf(path: Path, backendRoot: Path) -> tuple[str, ...]:
    """导入文件自身的包路径（点分模块元组）。`__init__.py` 的包 = 其目录链。"""
    parts = list(path.relative_to(backendRoot).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]  # 去掉文件名，留目录链
    return tuple(parts)


def _absoluteModuleName(node: ast.ImportFrom, package: tuple[str, ...]) -> str | None:
    """相对导入 → 绝对模块名；越出 backend 顶层（如脚本目录里 `from .. import x`）
    解析不了 ⇒ None（放弃这条边，文档化局限）。"""
    if node.level == 0:
        return node.module or ""
    if node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        return ".".join((*base, node.module))
    return ".".join(base) if base else None


def _allEntries(backendRoot: Path, moduleName: str) -> tuple[str, ...]:
    """模块 `__all__` 清单里的字符串（`from X import *` 会连带 import 的子模块名）。

    只认模块级 `__all__ = [...]` / `+= [...]` 里能静态看到的字符串常量；
    动态构造 ⇒ 空集（fail-open 只影响派生广度，钩子检测仍兜底）。"""
    files = _resolveModuleFiles(backendRoot, moduleName)
    if not files:
        return ()
    tree = ast.parse((backendRoot.parent / files[-1]).read_text(encoding="utf-8"))
    entries: list[str] = []
    for stmt in _moduleScopeStatements(tree.body):
        if not isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if not any(getattr(t, "id", None) == "__all__" for t in targets):
            continue
        value = stmt.value
        elts = value.elts if isinstance(value, (ast.List, ast.Tuple)) else ()
        entries.extend(
            e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
    return tuple(entries)


def _pytestPluginsEntries(node: ast.stmt) -> tuple[str, ...]:
    """`pytest_plugins = "m"` / `= ["m"]` / `+= ["m"]` 里声明的插件模块名。"""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return ()
    if not any(getattr(t, "id", None) == "pytest_plugins" for t in targets):
        return ()
    value = node.value
    if value is None:
        return ()
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        entries: list[object] = [value.value]
    elif isinstance(value, (ast.List, ast.Tuple)):
        entries = list(value.elts)
    else:
        entries = []
    return tuple(
        e.value for e in entries if isinstance(e, ast.Constant) and isinstance(e.value, str)
    )


def _importedModuleNames(path: Path, backendRoot: Path) -> tuple[str, ...]:
    """一个文件在模块级作用域导入的、能解析到 backend/ 下文件的模块名
    + `pytest_plugins` 声明的插件（解析不了的候选名在 `_resolveModuleFiles`
    自然返回空集，故这里不做顶层前缀过滤 —— 第十三轮 HIGH-3）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _packageOf(path, backendRoot)
    names: list[str] = []
    for node in _moduleScopeStatements(tree.body):
        if isinstance(node, ast.ImportFrom):
            moduleName = _absoluteModuleName(node, package)
            if not moduleName:
                continue
            names.append(moduleName)
            for alias in node.names:
                if alias.name == "*":
                    names.extend(
                        f"{moduleName}.{entry}" for entry in _allEntries(backendRoot, moduleName)
                    )
                else:
                    names.append(f"{moduleName}.{alias.name}")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        else:
            names.extend(_pytestPluginsEntries(node))
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
        for moduleName in _importedModuleNames(conftest, backendRoot):
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
        for moduleName in _importedModuleNames(current, backendRoot):
            for resolved in _resolveModuleFiles(backendRoot, moduleName):
                if resolved not in closure:
                    closure.add(resolved)
                    frontier.append(resolved)
    return tuple(sorted(closure))


def _structuralProblem(stmt: ast.stmt) -> str | None:
    """顶层语句的结构白名单：不属于定义性形态 ⇒ 一句话问题（None = 允许）。

    分支语句形态本身放行（分支体已由 `_moduleScopeStatements` 展开逐句看，
    其 test / handler.type / with item 由根链拒绝统一覆盖）。"""
    if isinstance(stmt, _ALLOWED_STATEMENTS):
        return None
    if isinstance(stmt, (*_BRANCH_TYPES, ast.Match)):
        return None
    if isinstance(stmt, ast.Expr):
        value = stmt.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return None  # docstring
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr in _ALLOWED_EXPR_METHODS
            and isinstance(value.func.value, ast.Name)
        ):
            return None  # `SomeModel.model_rebuild()`（裸 Name 接收者）
        return f"顶层表达式 {ast.unparse(stmt)[:60]}"
    return f"顶层 {type(stmt).__name__} {ast.unparse(stmt)[:60]}"


def _denyMessage(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """根链拒绝判定：命中拒绝清单 ⇒ 返回真名链（第十三轮 HIGH-2 先过别名）。"""
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load):
            target = aliases.get(node.id, node.id)
            return target if _isDeniedChain(target) else None
        # Store/Del：只认「import 进来的别名」的重绑定（`from os import environ
        # as e` 后 `e = 2`）；裸同名赋值/字段注解（如字段恰好叫 input）是假红
        # —— 第十四轮校准实证，不查。
        aliased = aliases.get(node.id)
        if aliased and _isDeniedChain(aliased):
            return aliased
        return None
    if isinstance(node, ast.Attribute):
        chain = _dottedChain(node)
        if chain is None:
            return None
        root, _, rest = chain.partition(".")
        target = aliases.get(root, root)
        full = f"{target}.{rest}" if rest else target
        return full if _isDeniedChain(full) else None
    return None


def _classCreationHookMessages(stmt: ast.ClassDef) -> tuple[tuple[int, str], ...]:
    """类**创建**（import 期）就会执行的钩子位置（第十三轮 HIGH-2）。"""
    out: list[tuple[int, str]] = []
    for kw in stmt.keywords:
        if kw.arg == "metaclass":
            out.append((stmt.lineno, f"类创建钩子 metaclass= {ast.unparse(kw.value)[:40]}"))
    for node in stmt.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _CLASS_CREATION_HOOK_NAMES
        ):
            out.append((node.lineno, f"类创建钩子 {node.name}"))
    return tuple(out)


def findImportTimeHooks(path: Path) -> tuple[str, ...]:
    """模块顶层「定义性之外」的 import 期代码（空 tuple = 定义性）。

    两条独立轴（第十三轮 HIGH-1：拒绝扫描与结构白名单**解耦**）：
    - 结构白名单：语句形态是否定义性（Expr 只放 docstring 与裸 Name 接收者的
      `model_rebuild()`）；
    - 根链拒绝：对**每条**语句的 import 期表达式统一跑（If.test / except 类型 /
      装饰器 / 默认值 / 注解 / with item / match guard 全覆盖），先过别名映射。
    再加类创建钩子（metaclass / `__init_subclass__` 族）。
    守卫用它在传递闭包上断言内容不变式（第十二轮复审 HIGH-2/MEDIUM-4）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _aliasMap(tree.body)
    hooks: list[str] = []
    seen: set[str] = set()

    def add(line: int, detail: str) -> None:
        message = f"line {line}: {detail}"
        if message not in seen:
            seen.add(message)
            hooks.append(message)

    for stmt in _moduleScopeStatements(tree.body):
        problem = _structuralProblem(stmt)
        if problem:
            add(stmt.lineno, problem)
        for node in _iterExecNodes(stmt):
            detail = _denyMessage(node, aliases)
            if detail:
                add(getattr(node, "lineno", stmt.lineno), f"顶层引用 {detail}")
        if isinstance(stmt, ast.ClassDef):
            for line, detail in _classCreationHookMessages(stmt):
                add(line, detail)
    return tuple(hooks)
