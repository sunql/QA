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

第十七轮复审（BLOCK 5H+1L，全带 PoC 与闭包校准）再补五路（第十八轮落地）：
- **禁止 dunder 段（HIGH-1）**：`sys.__getattribute__(...)`、`cls.__class__`
  爬链、`sys.__dict__[...]`——点分链**任意段**命中 `_FORBIDDEN_DUNDER_SEGMENTS`
  即拒（此前只有字面量 `__dict__` 段短路，且 Name 位置只查拒绝根）；裸名
  `__builtins__` 入拒绝根。
- **类实例化/方法调用执行方法体（HIGH-2）**：`_x = X()` 跑
  `__new__`/`__init__`/`__post_init__`；`X.m()`/`X().m()`/`@D.deco` 跑同名
  方法体；嵌套类 `Outer.Inner()` 同理；类体钩子名**赋值形态**
  （`__init_subclass__ = classmethod(lambda ...)`）拒绝；with/for/就地运算符
  作用于本地类实例（隐式 dunder 无显式 Call 节点）与模块级三参 `type()`
  fail-closed。类体描述符赋值 `attr = D()` 追 D 的 `__set_name__`。
- **调用目标按语句序（HIGH-3）**：`_f = m`、`_g = _h` 名字别名与「调用点之前
  定义的那个 def」都追（全量 dict 是"最后定义者胜"，会漏重定义前的调用）。
- **`model_rebuild` 白名单接收者感知（HIGH-4）**：本地类重定义了同名方法 ⇒
  调用扫其体（白名单只对「没有本地同名定义」的机制调用放行）。
- **PEP 562 钩子的赋值/注解形态（HIGH-5）**：模块级
  `__getattr__ = lambda ...` 与 def 形态一样是收集前钩子，一律拒。

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
    ast.AugAssign,
    ast.Global,
    ast.Nonlocal,
    ast.Pass,
    getattr(ast, "TypeAlias", None),
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
    "vars",
    "type.__setattr__",
    "object.__setattr__",
    "object.__getattribute__",
    # 第十七轮 HIGH-1：裸名 `__builtins__['exec']`（`__main__` 里它本身是 dict，
    # 不带点分链 ⇒ 只能当根拒；与 getattr/vars/globals 同族的任意代码通道）
    "__builtins__",
)

# 顶层裸表达式（Expr-Call）只允许这些方法名，且**接收者必须是裸 Name**
# （`get_module().model_rebuild()` 这类动态接收者是结构问题——第十三轮 LOW-2；
# 点分接收者 `app.evil.model_rebuild()` 同样不放行）。
_ALLOWED_EXPR_METHODS = ("model_rebuild",)

# 类体里的「类创建钩子」：class 语句执行（import 期）时就会跑（第十三轮 HIGH-2）。
# 描述符 `__get__`/`__set__` 不算——那是实例访问期执行，不是类创建期。
_CLASS_CREATION_HOOK_NAMES = (
    "__init_subclass__",
    "__set_name__",
    "__class_getitem__",
    "__mro_entries__",
)

# `from X import *` 的 X 根模块在这批里 ⇒ 星号导入整个拒绝（第十五轮 HIGH-2/
# M-1：星号把拒绝根的名字直接灌进模块命名空间，`from os import *` 后裸写
# `environ[...] = ...` 静态追不到 ⇒ 只能从源头拒。真实闭包 0 条星号导入。）
_DENIED_STAR_ROOTS = (
    "os",
    "sys",
    "subprocess",
    "importlib",
    "ctypes",
    "runpy",
    "builtins",
    "shutil",
    "socket",
    "signal",
    "_pytest",
    "monkeypatch",
)

# Store 到这些属性名 = 对允许机制的再绑定/劫持（第十五轮 H-3/H-4）。
_REBIND_DENIED_ATTRS = (*_CLASS_CREATION_HOOK_NAMES, *_ALLOWED_EXPR_METHODS)


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


# 点分链**任意段**命中即拒的 dunder（第十七轮 HIGH-1）：它们把静态链抹掉
# （`sys.__dict__[...]`）或提供任意属性/代码执行通道（`sys.__getattribute__(...)`、
# `cls.__class__` 层层爬、`__globals__`/`__code__`/`__subclasses__`）。以前只有
# 字面量 `__dict__` 段短路，`sys.__getattribute__` 这类链整个逃出。
# `__builtins__` 不在此列——裸名场景由 _DENIED_ROOTS 管（变异 M138 独占落点）。
# 真实闭包校准：76 文件里这些段在模块级出现 0 次（change 记录 §2.10.18）。
_FORBIDDEN_DUNDER_SEGMENTS = (
    "__dict__",
    "__getattribute__",
    "__class__",
    "__globals__",
    "__code__",
    "__subclasses__",
    "__mro__",
    "__bases__",
)


def _isForbidden(chain: str) -> bool:
    """拒绝清单命中，或链上任一段是抹掉静态链/任意执行通道的 dunder。"""
    if _isDeniedChain(chain):
        return True
    return any(seg in _FORBIDDEN_DUNDER_SEGMENTS for seg in chain.split("."))


def _moduleScopeStatements(body: list[ast.stmt]):
    """展开模块级作用域：分支语句递归进分支体/except 子句/match case，不进函数体。

    类体单独按同一规则看（类体语句也是 import 时执行）；方法体不看。
    """
    for node in body:
        yield node
        if isinstance(node, ast.Match):
            for case in node.cases:
                # guard 是表达式不是语句，不进结构白名单（第十五轮 M-2：进白名单
                # 会让任何带 guard 的模块级 match 假红）；其表达式由拒绝扫描经
                # `_iterExecNodes`（全子树）覆盖。
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
                else:
                    root = alias.name.split(".")[0]
                    aliases[root] = root
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            for alias in stmt.names:
                if alias.name != "*":  # 星号导入的名字静态未知（源头由星号拒绝管）
                    aliases[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Name)
        ):
            # 模块根再绑定：`_t = sys` ⇒ `_t.path.insert(...)` 要能追到 sys.path
            # （第十五轮 HIGH-2；只传播静态可判定的裸 Name 单赋值，按语句序覆盖）。
            source = aliases.get(stmt.value.id)
            if source:
                aliases[stmt.targets[0].id] = source
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
        argNodes: tuple[ast.arg, ...] = (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if args.vararg is not None:
            argNodes = (*argNodes, args.vararg)
        if args.kwarg is not None:
            argNodes = (*argNodes, args.kwarg)
        for a in argNodes:
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
    + 叶子（`app/domain/models.py` 与/或 `app/domain/models/__init__.py`）。
    **双形态都存在时两个都收**（第十五轮 HIGH-5）：CPython 的 `FileFinder` 在同一
    目录**先试包目录再试 .py**，此前派生器优先 `.py` 的方向与它相反 ⇒ 攻击者
    保留无害 `foo.py`、新增恶意 `foo/__init__.py` 即可让真实执行点逃出闭包；
    收超集只会让闭包偏大（fail-closed 方向）。
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
    if leafInit.is_file():
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
    **存在 `__all__` 但无法静态判定 ⇒ 抛错**（第十五轮 M-1：动态 `__all__` 的
    星号展开会漏掉真实会被 import 的子模块 ⇒ 那是 fail-open 洞，不是「广度」
    问题 —— CPython 实测会连带 import `__all__` 里的子模块）。真实闭包 61 处
    `__all__` 全部为静态字面量 ⇒ 零代价。"""
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
        if not isinstance(value, (ast.List, ast.Tuple)):
            raise RuntimeError(
                f"{moduleName} 的 __all__ 是动态构造（{ast.unparse(value)[:40]}），"
                "无法静态展开星号导入 ⇒ fail-closed"
            )
        for e in value.elts:
            if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
                raise RuntimeError(
                    f"{moduleName} 的 __all__ 含非常量元素（{ast.unparse(e)[:40]}）"
                    " ⇒ fail-closed"
                )
            entries.append(e.value)
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
    """根链拒绝判定：命中拒绝清单/禁止 dunder 段 ⇒ 返回真名链（先过别名）。"""
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load):
            target = aliases.get(node.id, node.id)
            return target if _isForbidden(target) else None
        # Store/Del：只认「import 进来的别名」的重绑定（`from os import environ
        # as e` 后 `e = 2`）；裸同名赋值/字段注解（如字段恰好叫 input）是假红
        # —— 第十四轮校准实证，不查。
        aliased = aliases.get(node.id)
        if aliased and _isForbidden(aliased):
            return aliased
        return None
    if isinstance(node, ast.Attribute):
        chain = _dottedChain(node)
        if chain is None:
            return None
        if _isForbidden(chain):
            # `sys.__getattribute__` / `sys.__dict__[...]`：禁止段抹掉静态链尾
            # ⇒ 整条链拒绝（第十七轮 HIGH-1：别名解析前后各查一遍——解析可能
            # 换掉根，解析后的 full 再查一次段）
            return chain
        root, _, rest = chain.partition(".")
        target = aliases.get(root, root)
        full = f"{target}.{rest}" if rest else target
        return full if _isForbidden(full) else None
    return None


def _classCreationHookMessages(stmt: ast.ClassDef) -> tuple[tuple[int, str], ...]:
    """类**创建**（import 期）就会执行的钩子位置（第十三轮 HIGH-2 / 第十五轮 H-3）。

    覆盖：`metaclass=` 关键字、`**{...}` 展开（kw.arg is None ⇒ metaclass
    静态不可判 ⇒ 一律拒）、基类是调用/星号表达式（`__mro_entries__` 在类创建
    期执行）、类体**任意嵌套深度**（含分支包裹）的钩子名定义。"""
    out: list[tuple[int, str]] = []
    for kw in stmt.keywords:
        if kw.arg is None:
            out.append((stmt.lineno, "类创建钩子 **kwargs 展开（metaclass 静态不可判）"))
        elif kw.arg == "metaclass":
            out.append((stmt.lineno, f"类创建钩子 metaclass= {ast.unparse(kw.value)[:40]}"))
    for base in stmt.bases:
        if isinstance(base, (ast.Call, ast.Starred)):
            out.append(
                (base.lineno, f"类基类是求值表达式 {ast.unparse(base)[:40]}（__mro_entries__ 途径）")
            )
    for node in _moduleScopeStatements(list(stmt.body)):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _CLASS_CREATION_HOOK_NAMES
        ):
            out.append((node.lineno, f"类创建钩子 {node.name}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # 第十七轮 HIGH-2：钩子名的**赋值/注解形态**同样在类创建期执行
            # （`__init_subclass__ = classmethod(lambda cls, **kw: ...)`、
            # `__set_name__ = lambda self, owner, name: ...`）——以前只认 def 形态。
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(getattr(t, "id", None) in _CLASS_CREATION_HOOK_NAMES for t in targets):
                out.append((node.lineno, f"类创建钩子赋值形态 {ast.unparse(node)[:40]}"))
    return tuple(out)


def _moduleLevelDefs(body: list[ast.stmt]):
    """模块级（含分支体内）的 def 定义节点；类体不算（方法要等调用/实例化）。

    与 `_moduleScopeStatements` 的差别：不递归进 ClassDef 体（类体里的 def 是
    方法），但**会**收 If/Try 等分支体里的 def（import 期定义）。"""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from _moduleLevelDefs(case.body)
        elif isinstance(node, _BRANCH_TYPES):
            yield from _moduleLevelDefs(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _moduleLevelDefs(handler.body)
            yield from _moduleLevelDefs(getattr(node, "orelse", []))
            yield from _moduleLevelDefs(getattr(node, "finalbody", []))


_MAX_CALLEE_DEPTH = 8

# PEP 562 的模块级 `__getattr__`/`__dir__`：包 init 里定义它 + 另一文件
# `from pkg import missing` 就会在收集前执行其体（第十五轮 H-4 实测）。
_MODULE_GETATTR_NAMES = ("__getattr__", "__dir__")

# 类实例化在 import 期执行的方法体（第十七轮 HIGH-2）：`_x = X()` 跑
# `__new__`/`__init__`，dataclass 再跑 `__post_init__`。真实闭包 18 处本地类
# 实例化扫这些方法体新增 0 条红（change 记录 §2.10.18 校准）。
_INSTANCE_HOOK_NAMES = ("__new__", "__init__", "__post_init__")


def _classBodyStatements(body: list[ast.stmt]):
    """类体作用域：分支递归、**嵌套类体不递归**（其成员不是外层类的属性）。

    与 `_moduleScopeStatements` 的差别：不递归进嵌套 ClassDef——
    `_classMember` 找的是「外层类身上可 `X.attr` 访问的成员」。"""
    for node in body:
        yield node
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from _classBodyStatements(case.body)
        elif isinstance(node, _BRANCH_TYPES):
            yield from _classBodyStatements(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _classBodyStatements(handler.body)
            yield from _classBodyStatements(getattr(node, "orelse", []))
            yield from _classBodyStatements(getattr(node, "finalbody", []))


def _moduleLevelClasses(body: list[ast.stmt]):
    """模块级（含分支体内）的 class 定义；类体里的嵌套类不算（非模块全局名）。"""
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # 函数体不看（类定义在函数体内不进模块命名空间）
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from _moduleLevelClasses(case.body)
        elif isinstance(node, _BRANCH_TYPES):
            yield from _moduleLevelClasses(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _moduleLevelClasses(handler.body)
            yield from _moduleLevelClasses(getattr(node, "orelse", []))
            yield from _moduleLevelClasses(getattr(node, "finalbody", []))


def _classMember(cls: ast.ClassDef, name: str) -> ast.AST | None:
    """类体里名字为 name 的成员定义节点（方法 def / lambda 绑定 / 嵌套类）。"""
    for node in _classBodyStatements(list(cls.body)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                any(getattr(t, "id", None) == name for t in targets)
                and isinstance(node.value, ast.Lambda)
            ):
                return node.value
    return None


def _instantiationHooks(cls: ast.ClassDef) -> tuple[ast.AST, ...]:
    """实例化（import 期 `_x = X()`）会执行的方法定义节点。"""
    members = (_classMember(cls, name) for name in _INSTANCE_HOOK_NAMES)
    return tuple(m for m in members if m is not None and not isinstance(m, ast.ClassDef))


def _receiverMember(func: ast.Attribute, localClasses: dict[str, ast.ClassDef]) -> ast.AST | None:
    """Attribute 调用 `X.m()` / `X().m()` 的接收者解析到本地类成员。

    接收者是本地类名或「本地类实例化调用」⇒ 返回同名成员（def / lambda /
    嵌套类）；否则 None（import 进来的类——如 pydantic 模型——不解析，
    `model_rebuild()` 白名单机制保持原样，第十七轮 HIGH-4 的本地重定义
    场景由「本地类有同名定义则扫其体」覆盖）。"""
    recv = func.value
    if isinstance(recv, ast.Name):
        cls = localClasses.get(recv.id)
    elif isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name):
        cls = localClasses.get(recv.func.id)
    else:
        return None
    if cls is None:
        return None
    return _classMember(cls, func.attr)


def _usesLocalInstance(expr: ast.expr, localClasses: dict[str, ast.ClassDef], instanceVars: set[str]) -> bool:
    """表达式子树里出现本地类实例化或已知实例变量。"""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in localClasses:
            return True
        if isinstance(node, ast.Name) and node.id in instanceVars:
            return True
    return False


def _instanceExecProblem(stmt: ast.stmt, localClasses: dict[str, ast.ClassDef], instanceVars: set[str]) -> str | None:
    """模块级 with/for/就地运算符作用于本地类实例 ⇒ 隐式 dunder 在 import 期执行。

    `__enter__`/`__exit__`/`__iter__`/`__next__`/`__iadd__` 不对应任何显式
    Call 节点，静态只能整段 fail-closed（真实闭包这些形态 0 处）。"""
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        if any(
            _usesLocalInstance(item.context_expr, localClasses, instanceVars)
            for item in stmt.items
        ):
            return "模块级 with 作用于本地类实例 ⇒ __enter__/__exit__ 在 import 期执行 ⇒ fail-closed"
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        if _usesLocalInstance(stmt.iter, localClasses, instanceVars):
            return "模块级 for 迭代本地类实例 ⇒ __iter__/__next__ 在 import 期执行 ⇒ fail-closed"
    elif isinstance(stmt, ast.AugAssign):
        if _usesLocalInstance(stmt.target, localClasses, instanceVars) or _usesLocalInstance(
            stmt.value, localClasses, instanceVars
        ):
            return "模块级就地运算符作用于本地类实例 ⇒ __iadd__ 等 dunder 在 import 期执行 ⇒ fail-closed"
    return None


def findImportTimeHooks(path: Path) -> tuple[str, ...]:
    """模块顶层「定义性之外」的 import 期代码（空 tuple = 定义性）。

    两条独立轴（第十三轮 HIGH-1：拒绝扫描与结构白名单**解耦**）：
    - 结构白名单：语句形态是否定义性（Expr 只放 docstring 与裸 Name 接收者的
      `model_rebuild()`）；
    - 根链拒绝：对**每条**语句的 import 期表达式统一跑（If.test / except 类型 /
      装饰器 / 默认值 / 注解（含 *args/**kwargs）/ with item / match guard
      全覆盖），先过别名映射（含模块根再绑定传播），链上任一段命中
      `_FORBIDDEN_DUNDER_SEGMENTS` 即拒（第十七轮 HIGH-1）。
    再加：类创建钩子（metaclass / `__init_subclass__` 族的 def 与**赋值**形态 /
    基类求值）、**import 期被调用的同模块 def/lambda 体**（按**语句序**解析
    调用目标：`_f = m`、`_g = _h` 名字别名与「调用点之前定义的那个」都追——
    第十七轮 HIGH-3）、**本地类实例化/方法调用执行的方法体**（`_x = X()` 跑
    `__new__`/`__init__`/`__post_init__`，`X.m()`/`X().m()`/`@D.deco` 跑同名
    方法体，嵌套类 `Outer.Inner()` 同理；类体钩子名赋值形态、with/for/就地
    运算符作用于本地类实例、模块级三参 `type()` 一律 fail-closed——第十七轮
    HIGH-2/HIGH-4）、模块级 `__getattr__`/`__dir__`（PEP 562 收集前钩子，
    def 与赋值/注解形态都拒——第十七轮 HIGH-5）、来自拒绝根的星号导入、
    对钩子/允许方法属性名的再绑定。
    局限（文档化）：嵌套函数体内的调用不追；模块 docstring「方法体不看」指
    **未被调用**的方法（clean fixture `method_body` 钉住）——被 import 期
    实例化/调用执行的方法体在本函数覆盖范围内。
    守卫用它在传递闭包上断言内容不变式（第十二轮复审 HIGH-2/MEDIUM-4）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _aliasMap(tree.body)
    localClasses = {c.name: c for c in _moduleLevelClasses(tree.body)}
    classBodyIds = {
        id(s)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for s in node.body
    }
    hooks: list[str] = []
    seen: set[str] = set()

    def add(line: int, detail: str) -> None:
        message = f"line {line}: {detail}"
        if message not in seen:
            seen.add(message)
            hooks.append(message)

    # 调用目标按**语句序**登记（第十七轮 HIGH-3：全量 dict 是"最后定义者胜"，
    # `def m: evil / _x = m() / def m: pass` 会解析到第二个 def ⇒ 漏扫；
    # `_f = m`、`_g = _h` 的名字别名此前根本不登记 ⇒ 同样漏）。
    callTargets: dict[str, ast.AST] = {}
    instanceVars: set[str] = set()

    def applyBinding(stmt: ast.stmt) -> None:
        if id(stmt) in classBodyIds:
            return  # 类体绑定不是模块全局名（方法/类属性）
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callTargets[stmt.name] = stmt
            return
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if value is None:
            return
        if isinstance(value, ast.Lambda):
            for t in targets:
                if isinstance(t, ast.Name):
                    callTargets[t.id] = value
        elif isinstance(value, ast.Name):
            bound = callTargets.get(value.id)
            if bound is not None:
                for t in targets:
                    if isinstance(t, ast.Name):
                        callTargets[t.id] = bound
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in localClasses
        ):
            # 本地类实例化 ⇒ 记实例变量（with/for/就地运算符 dunder 的判据）
            for t in targets:
                if isinstance(t, ast.Name):
                    instanceVars.add(t.id)

    visited: set[int] = set()

    def scanCallee(callee: ast.AST, depth: int) -> None:
        if depth > _MAX_CALLEE_DEPTH or id(callee) in visited:
            return
        visited.add(id(callee))
        if isinstance(callee, ast.Lambda):
            scanExec(_iterExecNodes(callee.body), depth)
            return
        for stmt in callee.body:
            scanExec(_iterExecNodes(stmt), depth)

    def scanExec(nodes, depth: int) -> None:
        for node in nodes:
            detail = _denyMessage(node, aliases)
            if detail:
                add(getattr(node, "lineno", 0), f"顶层引用 {detail}")
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if node.attr in _REBIND_DENIED_ATTRS:
                    add(getattr(node, "lineno", 0), f"顶层再绑定 {node.attr}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in getattr(node, "decorator_list", []):
                    if isinstance(dec, ast.Name) and dec.id in callTargets:
                        scanCallee(callTargets[dec.id], depth + 1)
                    elif (
                        isinstance(dec, ast.Attribute)
                        and isinstance(dec.value, ast.Name)
                        and dec.value.id in localClasses
                    ):
                        # `@D.deco`：装饰器在 def/class 语句执行期被调用
                        member = _classMember(localClasses[dec.value.id], dec.attr)
                        if member is not None and not isinstance(member, ast.ClassDef):
                            scanCallee(member, depth + 1)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Lambda):
                    scanCallee(node.func, depth + 1)  # IIFE：lambda 体在 import 期执行
                elif isinstance(node.func, ast.Name):
                    if node.func.id in localClasses:
                        # 本地类实例化：__new__/__init__/__post_init__ 在 import 期执行
                        for hook in _instantiationHooks(localClasses[node.func.id]):
                            scanCallee(hook, depth + 1)
                    else:
                        callee = callTargets.get(node.func.id)
                        if callee is not None:
                            scanCallee(callee, depth + 1)
                elif isinstance(node.func, ast.Attribute):
                    member = _receiverMember(node.func, localClasses)
                    if isinstance(member, ast.ClassDef):
                        # 嵌套类实例化（Outer.Inner()）
                        for hook in _instantiationHooks(member):
                            scanCallee(hook, depth + 1)
                    elif member is not None:
                        scanCallee(member, depth + 1)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "type"
                and len(node.args) >= 3
            ):
                # `X = type('X', (), {...})` 动态建类：dict 里的可调用体静态
                # 追不完 ⇒ fail-closed（真实闭包 0 处）
                add(node.lineno, "模块级三参 type() 动态建类 ⇒ fail-closed")

    for stmt in _moduleScopeStatements(tree.body):
        problem = _structuralProblem(stmt)
        if problem:
            add(stmt.lineno, problem)
        if (
            isinstance(stmt, ast.ImportFrom)
            and stmt.level == 0
            and stmt.module
            and stmt.names[0].name == "*"
        ):
            root = stmt.module.split(".")[0]
            if root in _DENIED_STAR_ROOTS:
                add(stmt.lineno, f"星号导入拒绝根模块 {root}")
        applyBinding(stmt)
        scanExec(_iterExecNodes(stmt), 0)
        if (
            isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.AugAssign))
            and id(stmt) not in classBodyIds
        ):
            problem = _instanceExecProblem(stmt, localClasses, instanceVars)
            if problem:
                add(stmt.lineno, problem)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and id(stmt) not in classBodyIds:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(getattr(t, "id", None) in _MODULE_GETATTR_NAMES for t in targets):
                # 第十七轮 HIGH-5：PEP 562 钩子的赋值/注解形态（lambda 等）
                add(stmt.lineno, "模块级 __getattr__/__dir__ 绑定（PEP 562 收集前钩子）")
        if isinstance(stmt, ast.ClassDef):
            for line, detail in _classCreationHookMessages(stmt):
                add(line, detail)
            for node in stmt.body:
                # 类体描述符赋值 `attr = D()` ⇒ D 的 `__set_name__` 在类创建期执行
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id in localClasses
                    ):
                        setter = _classMember(localClasses[value.func.id], "__set_name__")
                        if setter is not None:
                            scanCallee(setter, 0)
    for d in _moduleLevelDefs(tree.body):
        if d.name in _MODULE_GETATTR_NAMES:
            add(d.lineno, f"模块级 {d.name}（PEP 562 收集前钩子）")
    return tuple(hooks)
