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
    # 第十九轮 MEDIUM-3：import 机制 sink——装进 meta_path/path_hooks 的 finder
    # 会劫持后续全部 import（含失败集合面），import 期出现即不属于定义性。
    "sys.meta_path",
    "sys.path_hooks",
    "sys.path_importer_cache",
    "sys.settrace",
    "sys.setprofile",
    "sys.audit",
    # 第二十三轮评审 C-4/C-5：进程/文件系统/证据通道 sink——赋值式
    # （`_r = os.remove('x')`）此前逃逸（顶层裸调用被结构规则拦、赋值式只查
    # 拒绝根 ⇒ 清单没有就放行）。这批根在真实 76 文件闭包 0 处（校准实证）。
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.makedirs",
    "os.removedirs",
    "os.chdir",
    "os.fchdir",
    "os.truncate",
    "os.putenv",
    "os.unsetenv",
    "os.open",
    "os.read",
    "os.write",
    "os.fdopen",
    "os.dup",
    "os.dup2",
    "os._exit",
    "os.abort",
    "os.kill",
    "sys.exit",
    "sys.stdout",
    "sys.stderr",
    "sys.__stdout__",
    "sys.__stderr__",
    # 第二十三轮评审 C-5：pytest reporter 重绑 + `os._exit(0)` 可把失败集合
    # 任意伪造（FAILED 行抑制 + 伪汇总行 + exit 0）——`pytest` 根不能整个拒
    # （conftest 链大量 `pytest.fixture`），钉 reporter 入口。
    "pytest.TerminalReporter",
    # 第二十三轮评审 C-6：分析器自致盲——`ast.parse` / `Path.read_text` 被换
    # 成假实现后检测器对自己看到空 AST。这批属性 Store 由下方「模块根属性
    # 再绑定」规则统一拒绝，这里钉全模块根（真实闭包 0 处，校准实证）。
    "ast",
    "io",
    "tempfile",
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

# 第二十二轮 C1：守卫自身模块根链。闭包文件在 import 期 rebind 守卫函数
# （`_c.findImportTimeHooks = _spy`，选择性跳过自己的文件）会让内容不变式
# 整体空转 ⇒ 守卫模块链整个拒绝——import 别名、属性读、属性 Store 全部
# 经 `_isForbidden` 命中。真实闭包 0 处 import 这些模块（§2.10.19 校准）。
_GUARD_MODULE_ROOTS = (
    "app.utils.ci_conftest_chain",
    "app.utils.ci_surface",
    "app.utils.ci_failure_set",
    "app.utils.ci_gh",
)

# 第二十二轮 C2：拒绝根的**危险后缀段**——静态根的 `os.environ`/`sys.path`
# 由 `_DENIED_ROOTS` 前缀拒；把根洗进动态包装（`[os][0].environ`）后取属性，
# 段上必须等价拒绝，否则洗白通道整个敞开。只对**动态根**链生效（静态根
# 走根清单；良性动态根如 `Path(__file__).parent` 段全不命中 ⇒ 校准零假红）。
_DYNAMIC_DENIED_SEGMENTS = (
    "environ",
    "path",
    "modules",
    "meta_path",
    "path_hooks",
    "path_importer_cache",
    "settrace",
    "setprofile",
    "audit",
    "system",
    "popen",
    "argv",
    # （第二十四轮：C-4 的 sink 成员段扩展在此回退——实证冗余。动态根
    # **调用**走 Call 分派的「动态接收者 ⇒ fail-closed」（第十九轮既有
    # 线），`Path(...)` 这类外部构造接收者实测落 unknown ⇒ 由
    # `_DENIED_CALL_ATTRS` 管（M209 判别）。两条既有线全覆盖 ⇒ 段集扩展
    # 拿不到独占变异判别，且是死代码 ⇒ 删。）
)

# 第二十三轮评审 C-4 余量：**unknown 接收者**上的文件系统/进程 sink 成员
# （外部构造/外部模块名作接收者，成员解析不出 ⇒ 此前静默放行 ⇒ 仍拒）。
# 刻意不含 `write`/`writelines`（太通用——文件句柄只能来自被拒的
# `open`/`sys.stdout`/`Path(...).write_text`，这些已有各自的拒绝线；
# `.write` 进集会误伤真实闭包）。真实 76 文件闭包 0 处（校准实证）。
_DENIED_CALL_ATTRS = frozenset(
    {
        "remove",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "makedirs",
        "removedirs",
        "write_text",
        "write_bytes",
        "putenv",
        "unsetenv",
        "_exit",
        "abort",
        "kill",
        "truncate",
    }
)

# 第二十三轮评审 H-2：装饰器在 def/class 语句执行期被调用 ⇒ 装饰器表达式
# 本身就是 import 期执行的代码。裸名装饰器只放行 Python 内置三件套；
# 经别名解析落到项目代码（app./scripts.）或拒绝链的一律拒；未 import、
# 非内置、无任何静态绑定的裸名 = 静态不可判 ⇒ fail-closed。真实 76 文件
# 闭包校准：Name 装饰器仅 classmethod/staticmethod/property（内置）+
# abstractmethod/functools.lru_cache（from-import ⇒ 别名解析为外部根），
# Call/Attribute 装饰器 57 处根全部是 pytest/dataclasses/functools/abc 等
# 外部模块（change 记录 §2.10.20）⇒ 本规则零假红。
_DECORATOR_BUILTIN_ALLOW = frozenset(
    {
        "classmethod",
        "staticmethod",
        "property",
    }
)

_PROJECT_DECORATOR_ROOTS = ("app", "scripts", "__relative__")

# 第二十六轮评审 M-26-1：相对导入只可能指向**本仓库**（包内）⇒ 一律映射到
# 相对哨兵根（归入项目根拒绝），不再静默跳过（`from . import models` 后
# `@models.deco` 探针实证逃逸）。
_RELATIVE_IMPORT_ROOT = "__relative__"

# 第二十六轮评审 H-26-1：嵌套调用工厂豁免（注册式惯用法）可被**通用可调用
# 工厂**洗白——`functools.partial(Path('x').unlink)()` 的内层被调解析到
# functools（外部 import）⇒ 豁免臂放行，但返回的是把任意 sink 成员绑好的
# 可执行目标（探针实证逃逸）。这批组合子不经过任何项目代码就能包装任意
# 可调用 ⇒ 豁免臂不放行（真实闭包 0 处，校准实证）。
_CALLABLE_FACTORY_DENIED = (
    "functools.partial",
    "operator.methodcaller",
    "operator.attrgetter",
)


def _callFactorySinkProblem(callNode: ast.Call) -> str | None:
    """嵌套调用工厂的实参子树里出现 sink 成员引用 ⇒ 返回拒绝消息，否则 None。

    第二十六轮评审 H-26-1：豁免臂此前只判内层被调的根，
    `field_validator('x')(Path('y').unlink)` 实参里绑好的 sink 方法引用
    整段不看（探针实证逃逸）。"""
    for arg in (*callNode.args, *(kw.value for kw in callNode.keywords)):
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Attribute) and sub.attr in _DENIED_CALL_ATTRS:
                return (
                    f"嵌套调用工厂实参携带 sink 成员 .{sub.attr}"
                    " ⇒ 可执行目标不可证 ⇒ fail-closed"
                )
    return None


def _decoratorRootProblem(chain: str, effAliases: dict[str, str]) -> str | None:
    """装饰器工厂链解析到项目/禁止代码 ⇒ 返回拒绝消息，否则 None。

    第二十五轮 C-24-1 收敛成单点：Call 装饰器（Name/Attribute 工厂）与
    Attribute 装饰器共用同一解析——双份同文锚会让变异取证拿不到独占判别
    （跑者只替换第 N 次出现，第二十二轮实证）。"""
    rootName = chain.split(".")[0]
    resolved = effAliases.get(rootName, rootName)
    if resolved.split(".")[0] in _PROJECT_DECORATOR_ROOTS or _isForbidden(chain):
        return (
            f"装饰器 {chain} 解析到项目/禁止代码（{resolved}）"
            " ⇒ 装饰期执行 ⇒ fail-closed"
        )
    return None

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

# 第二十二轮 L1：AST 规模预算——超大规模文件递归遍历会 RecursionError，
# 此前异常直接炸穿守卫（fail-open：守卫崩 = 该文件逃出内容不变式）。
# 预算迭代计数（不用递归的 ast.walk 也行，但 iter_child_nodes 显式更清晰）。
_MAX_AST_NODES = 100_000


def _countAstNodes(tree: ast.AST) -> int:
    """迭代统计 AST 节点数（计数本身不递归 ⇒ 深嵌套文件不会先炸在计数上）。"""
    count = 0
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(ast.iter_child_nodes(node))
    return count


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
# 真实闭包校准：76 文件里这些段在模块级出现 0 次（change 记录 §2.10.17）。
_FORBIDDEN_DUNDER_SEGMENTS = (
    "__dict__",
    "__getattribute__",
    # 第十九轮 HIGH-2：`sys.__setattr__('path', [...])` / 实例 `__setattr__` 是
    # 普通属性访问上的调用，拒绝根只匹配 `type.__setattr__`/`object.__setattr__`
    # 字面根 ⇒ 模块/实例通道整个敞开。`object.__setattr__` 已被根清单拒，
    # 段集加宽不新增那路假红（真实闭包校准 0 处，change 记录 §2.10.18）。
    "__setattr__",
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
    # 第二十二轮 C1：守卫模块链（import/属性读/属性 Store 一体拒绝）
    if any(chain == root or chain.startswith(f"{root}.") for root in _GUARD_MODULE_ROOTS):
        return True
    return any(seg in _FORBIDDEN_DUNDER_SEGMENTS for seg in chain.split("."))


def _dynamicRootForbiddenSegment(node: ast.Attribute) -> str | None:
    """根不是 Name 的属性链只查**禁止段**。

    第十九轮 HIGH-1：`(lambda: 0).__globals__['__builtins__']['exec']`、
    `[sys][0].__getattribute__('path')` 的根是 Lambda/Subscript ⇒
    `_dottedChain` 返回 None ⇒ 此前整条链放行（`_isForbidden` 不可达）。
    根不可静态解析（不查拒绝根、不过别名——那是 `_dottedChain` 的语义），
    但 Attribute 脊柱上的**段**仍可查；`Path(__file__).parent` 这类良性
    动态根段全不命中 ⇒ 真实闭包校准零假红（§2.10.18）。
    第二十二轮 C2：段集加宽到拒绝根的**危险后缀**（`_DYNAMIC_DENIED_SEGMENTS`）
    ——`[os][0].environ` / `[sys][0].path` 把拒绝根洗进动态包装，静态根的
    前缀拒绝不可达，段上等价拒绝。"""
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        if cur.attr in _FORBIDDEN_DUNDER_SEGMENTS or cur.attr in _DYNAMIC_DENIED_SEGMENTS:
            return cur.attr
        cur = cur.value
    return None


def _dynamicRootForbiddenChainRoot(
    node: ast.Attribute, aliases: dict[str, str]
) -> str | None:
    """动态根属性链的根子树里出现「拒绝链的名字」⇒ 洗白通道拒绝。

    第二十二轮 C2 变体 C：`_m = [sys][0].modules; _os = _m['os']` 里
    `sys` 本身不在拒绝清单（裸 `os.getcwd()` 良性），但把它包装成动态根
    后取危险属性 = 等价引用拒绝根 ⇒ 根子树任一 Name 经别名映射命中
    `_isForbidden` 即整链拒绝。良性动态根（`Path(__file__).parent`、
    `logging.getLogger('x').setLevel`）子树名字不在拒绝链 ⇒ 不命中。"""
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    for sub in ast.walk(cur):
        if isinstance(sub, ast.Name):
            target = aliases.get(sub.id, sub.id)
            if _isForbidden(target):
                return target
    return None


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
    `path.insert(...)` 同样要能追到 sys.path）。只看模块级；函数内重绑定
    不追（文档化局限，见模块 docstring）。相对导入一律映射到
    `_RELATIVE_IMPORT_ROOT` 哨兵（第二十六轮评审 M-26-1：相对导入只可能
    指向本仓库 ⇒ 按项目代码拒绝，此前静默跳过是逃逸通道）。
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
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level == 0 and stmt.module:
                moduleName: str | None = stmt.module
            elif stmt.level > 0:
                # 第二十六轮评审 M-26-1：相对导入只可能指向本仓库 ⇒ 映射到
                # 相对哨兵根（_PROJECT_DECORATOR_ROOTS 含它 ⇒ 按项目代码拒）。
                moduleName = _RELATIVE_IMPORT_ROOT
            else:
                moduleName = None
            if moduleName is not None:
                for alias in stmt.names:
                    if alias.name != "*":  # 星号导入的名字静态未知（源头由星号拒绝管）
                        aliases[alias.asname or alias.name] = (
                            f"{moduleName}.{alias.name}"
                        )
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
            # 第十九轮 HIGH-1：动态根（Lambda/Subscript/Call/…）此前直接放行；
            # 第二十二轮 C2：危险后缀段 + 根子树拒绝链名字（洗白通道）一并拒
            seg = _dynamicRootForbiddenSegment(node)
            if seg:
                return f"动态根属性链 .{seg}（根非 Name ⇒ 拒绝根/别名不可达）"
            chainRoot = _dynamicRootForbiddenChainRoot(node, aliases)
            if chainRoot:
                return f"动态根属性链引用拒绝链 {chainRoot}（根非 Name ⇒ 洗白通道拒绝）"
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
# 实例化扫这些方法体新增 0 条红（change 记录 §2.10.17 校准）。
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


def _classMember(
    cls: ast.ClassDef,
    name: str,
    callTargets: dict[str, ast.AST] | None = None,
    ambiguity: set[str] | None = None,
) -> ast.AST | None:
    """类体里名字为 name 的成员定义节点。

    def / lambda 绑定 / 嵌套类之外（第十九轮 HIGH-4）：`__init__ = evil` 的
    **Name 值**经 callTargets 解析到被指函数；`staticmethod(evil)` /
    `classmethod(evil)` 解析到实参所指函数——此前这三类成员对
    `_classMember` 不可见 ⇒ 实例化/成员调用整体逃出。解析不了 ⇒ None。

    第二十二轮 H6 歧义硬化（`ambiguity` 传入时启用）：
    - 同名成员**多重定义**（条件两臂各一个 def/赋值）⇒ 运行时"执行的臂"
      静态不可判 ⇒ 记歧义，调用方 fail-closed；
    - 绑定值静态不可判的形态（`__init__ = [_evil][0]` 下标、
      `staticmethod(lambda: ...)`、元组解包目标 `__init__, _x = evil, 1`、
      其他未识别值）⇒ 同样记歧义 fail-closed（此前静默返回 None/扫错臂）。"""
    matches: list[ast.AST] = []
    unjudgeable = False
    for node in _classBodyStatements(list(cls.body)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            matches.append(node)
            continue
        if isinstance(node, ast.ClassDef) and node.name == name:
            matches.append(node)
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            hit = any(
                getattr(t, "id", None) == name
                or (
                    isinstance(t, ast.Tuple)
                    and any(getattr(e, "id", None) == name for e in t.elts)
                )
                for t in targets
            )
            if not hit:
                continue
            value = node.value
            if value is None:
                continue  # 纯注解无绑定
            if isinstance(value, ast.Name) and callTargets and value.id in callTargets:
                matches.append(callTargets[value.id])
            elif isinstance(value, ast.Lambda):
                matches.append(value)
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in ("staticmethod", "classmethod")
                and value.args
            ):
                # 描述符实参：Name（经 callTargets）解析到被指函数；
                # lambda 体可静态扫 ⇒ 直接扫；其余实参 ⇒ fail-closed。
                arg = value.args[0]
                if isinstance(arg, ast.Name) and callTargets and arg.id in callTargets:
                    matches.append(callTargets[arg.id])
                elif isinstance(arg, ast.Lambda):
                    matches.append(arg)
                else:
                    unjudgeable = True
            else:
                # 第二十二轮 H6：其余值形态（Subscript/Attribute/未知 Call/
                # Constant/…）静态不可判 ⇒ fail-closed
                unjudgeable = True
    if ambiguity is not None and (len(matches) > 1 or unjudgeable):
        ambiguity.add(name)
    if unjudgeable:
        return None
    return matches[0] if matches else None


def _instantiationHooks(
    cls: ast.ClassDef,
    callTargets: dict[str, ast.AST] | None = None,
    ambiguity: set[str] | None = None,
) -> tuple[ast.AST, ...]:
    """实例化（import 期 `_x = X()`）会执行的方法定义节点。

    第二十二轮 H6：任一实例化钩子的绑定静态不可判（条件臂/未识别值形态）
    ⇒ `ambiguity` 非空，调用方 fail-closed——返回值只含可静态扫的成员。"""
    hooks: list[ast.AST] = []
    for name in _INSTANCE_HOOK_NAMES:
        member = _classMember(cls, name, callTargets, ambiguity)
        if member is not None and not isinstance(member, ast.ClassDef):
            hooks.append(member)
    return tuple(hooks)


def _resolveMember(
    func: ast.Attribute,
    localClasses: dict[str, ast.ClassDef],
    instanceVars: dict[str, ast.ClassDef],
    callTargets: dict[str, ast.AST],
    opaqueNames: set[str] | None = None,
    classAliases: dict[str, str] | None = None,
    ambiguity: set[str] | None = None,
) -> tuple[ast.AST | None, str]:
    """Attribute 调用 `X.m()` / `X().m()` / `_x.m()` 的接收者解析。

    返回 `(成员, 状态)`，状态三态：
    - `"local"`：接收者是本地类名 / 类别名（第二十二轮 H1）/ 已知实例变量
      （第十九轮 HIGH-3：实例变量此前不解析 ⇒ 方法调用整体逃出；
      `model_rebuild` 白名单的改名实例绕过与此同源）/ 本地类实例化调用。
      成员未定义 ⇒ `(None, "local")` ⇒ 调用方 fail-closed（继承 /
      property 途径静态追不到）。
    - `"dynamic"`：接收者是其他表达式（`make().m()`、`f()[0].m()`）或
      **不透明绑定的名字**（第二十二轮：`_s = sys if … else …` 洗成裸名后
      的接收者 ⇒ unknown 放行被洗白链利用，改 fail-closed）⇒ 调用方
      fail-closed。
    - `"unknown"`：接收者是 import 进来的名字（pydantic 模型的
      `model_rebuild()` 白名单机制保持原样）⇒ 不拒。"""
    recv = func.value
    if isinstance(recv, ast.Name):
        if opaqueNames and recv.id in opaqueNames:
            return None, "dynamic"
        cls = localClasses.get(recv.id) or instanceVars.get(recv.id)
        if cls is None and classAliases and recv.id in classAliases:
            cls = localClasses.get(classAliases[recv.id])
        if cls is None:
            return None, "unknown"
        member = _classMember(cls, func.attr, callTargets, ambiguity)
        if ambiguity and func.attr in ambiguity:
            return None, "dynamic"
        return member, "local"
    if isinstance(recv, ast.Attribute):
        # `os.path.join(...)` 的接收者是 Attribute 链（根 Name）：根是本地类/
        # 实例 ⇒ 沿链解析（`Outer.Inner.m()` 的嵌套类成员）；根是 import 进来
        # 的名字（os / logging / app.models.Foo）⇒ unknown 放行；根是动态
        # 表达式 ⇒ dynamic fail-closed。
        chainRoot: ast.expr = recv
        while isinstance(chainRoot, ast.Attribute):
            chainRoot = chainRoot.value
        if not isinstance(chainRoot, ast.Name):
            return None, "dynamic"
        if opaqueNames and chainRoot.id in opaqueNames:
            return None, "dynamic"
        cls = localClasses.get(chainRoot.id) or instanceVars.get(chainRoot.id)
        if cls is None and classAliases and chainRoot.id in classAliases:
            cls = localClasses.get(classAliases[chainRoot.id])
        if cls is None:
            return None, "unknown"
        member, status = _resolveMember(
            recv, localClasses, instanceVars, callTargets, opaqueNames, classAliases,
            ambiguity,
        )
        if isinstance(member, ast.ClassDef):
            nested = _classMember(member, func.attr, callTargets, ambiguity)
            if ambiguity and func.attr in ambiguity:
                return None, "dynamic"
            return nested, "local"
        return (None, "local") if status == "local" else (None, "unknown")
    if isinstance(recv, ast.Call):
        if isinstance(recv.func, ast.Name) and recv.func.id in ("super", "type"):
            # 第二十二轮 H4：`super().pwn()` / `type(self).pwn()` 的返回对象
            # 静态不可判（继承链/动态类）⇒ 成员调用 fail-closed；继承方法体
            # 此前经 unknown 放行整个逃出。
            return None, "dynamic"
        if isinstance(recv.func, ast.Name) and recv.func.id in localClasses:
            member = _classMember(localClasses[recv.func.id], func.attr, callTargets, ambiguity)
            if ambiguity and func.attr in ambiguity:
                return None, "dynamic"
            return member, "local"
        if isinstance(recv.func, ast.Name):
            # 构造器是 import 进来的名字（`BigInteger().with_variant(...)` 这类
            # SQLAlchemy 惯用法）⇒ unknown 放行；**本地 def/lambda 作构造器**
            # （`make().m()`，make 在 callTargets）⇒ 返回对象静态不可判 ⇒
            # dynamic fail-closed（第十九轮 HIGH-1 余量的精确边界）。
            if recv.func.id in callTargets:
                return None, "dynamic"
            return None, "unknown"
        # 构造器本身是属性链（`logging.getLogger('x').setLevel(...)`，真实闭包
        # logging 惯用法）：链根是 import 进来的名字（不在本地类/实例/被调表）
        # ⇒ unknown 放行；链根本地 ⇒ 返回对象静态不可判 ⇒ dynamic。
        callRoot: ast.expr = recv.func
        while isinstance(callRoot, ast.Attribute):
            callRoot = callRoot.value
        if (
            isinstance(callRoot, ast.Name)
            and callRoot.id not in localClasses
            and callRoot.id not in instanceVars
            and callRoot.id not in callTargets
        ):
            return None, "unknown"
        return None, "dynamic"
    if isinstance(recv, ast.Constant):
        # `",".join(...)` 这类字面量方法调用（真实闭包 models.py 的 CHECK 约束
        # 构造）——字面量的方法体是解释器内置，静态无害 ⇒ unknown 放行。
        return None, "unknown"
    return None, "dynamic"


def _decoratorMember(
    dec: ast.expr,
    localClasses: dict[str, ast.ClassDef],
    callTargets: dict[str, ast.AST],
    classAliases: dict[str, str] | None = None,
    ambiguity: set[str] | None = None,
) -> ast.AST | None:
    """`@D.deco` 装饰器成员解析——主循环预扫与 scanExec 两处共用。

    刻意收敛成单点：两处调用共享同一解析，变异取证（M150）才有一个
    独占锚点（跑者只替换第 N 次出现，双份同文锚不死两处 ⇒ fixture 不转红）。
    第二十二轮 H1：接收者可以是本地类别名（`_D = X; @_D.deco`）；
    第二十二轮 H6：成员歧义经 `ambiguity` 传出，调用方 fail-closed。"""
    if (
        isinstance(dec, ast.Attribute)
        and isinstance(dec.value, ast.Name)
        and (
            dec.value.id in localClasses
            or (classAliases is not None and dec.value.id in classAliases)
        )
    ):
        clsName = (
            classAliases.get(dec.value.id, dec.value.id)
            if classAliases is not None
            else dec.value.id
        )
        cls = localClasses.get(clsName)
        if cls is not None:
            return _classMember(cls, dec.attr, callTargets, ambiguity)
    return None


def _assignTargetNames(targets: list[ast.expr]):
    """赋值目标里的裸名（含元组解包一层；第十九轮 HIGH-3 实例变量记账用）。"""
    for t in targets:
        if isinstance(t, ast.Name):
            yield t
        elif isinstance(t, ast.Tuple):
            for e in t.elts:
                if isinstance(e, ast.Name):
                    yield e


def _usesLocalInstance(expr: ast.expr, localClasses: dict[str, ast.ClassDef], instanceVars: dict[str, ast.ClassDef]) -> bool:
    """表达式子树里出现本地类实例化或已知实例变量。"""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in localClasses:
            return True
        if isinstance(node, ast.Name) and node.id in instanceVars:
            return True
    return False


def _usesOpaqueName(expr: ast.expr, opaqueNames: set[str]) -> bool:
    """表达式子树里出现不透明绑定的名字（第二十二轮 C2/H3 洗白判据）。"""
    return any(isinstance(n, ast.Name) and n.id in opaqueNames for n in ast.walk(expr))


def _usesLocalInstanceInProtocolPosition(
    node: ast.expr, localClasses: dict[str, ast.ClassDef], instanceVars: dict[str, ast.ClassDef]
) -> bool:
    """本地类实例出现在**隐式协议触发位**（第二十二轮 H5 校准修正）。

    f-string 插值 ⇒ __format__/__str__；dict **键** ⇒ __hash__（值位只是
    存储，`_Rule(...)` 实例化作值是常态且已由实例化钩子线覆盖）；推导式的
    首个 **iter** ⇒ __iter__（元素位只是绑定名字）；Starred 的 value
    ⇒ __iter__。整子树粗判会把值位/元素位的实例化误判成协议触发。"""
    if isinstance(node, ast.JoinedStr):
        return any(
            _usesLocalInstance(v.value, localClasses, instanceVars)
            for v in node.values
            if isinstance(v, ast.FormattedValue)
        )
    if isinstance(node, ast.Dict):
        return any(
            k is not None and _usesLocalInstance(k, localClasses, instanceVars)
            for k in node.keys
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return any(
            _usesLocalInstance(g.iter, localClasses, instanceVars)
            for g in node.generators
        )
    if isinstance(node, ast.Starred):
        return _usesLocalInstance(node.value, localClasses, instanceVars)
    return False


def _instanceExecProblem(stmt: ast.stmt, localClasses: dict[str, ast.ClassDef], instanceVars: dict[str, ast.ClassDef]) -> str | None:
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


def _pep562BindingProblem(stmt: ast.stmt) -> str | None:
    """模块级把 `__getattr__`/`__dir__` 绑定到名字的**任意**形态 ⇒ 收集前钩子。

    第十九轮 HIGH-7：此前只认 Assign/AnnAssign 的 Name 目标与 def 形态——
    元组解包（`__getattr__, _x = (lambda n: [], 1)`）、walrus、for/with 目标、
    `from X import f as __getattr__` 全逃出。统一改判：
    - import 别名直接查 alias 名；
    - 其余语句按「语句内 Store 上下文的名字」判（元组解包 / walrus / for /
      with 的目标都是 Store Name，`ast.walk` 全覆盖）。
    类体绑定不算（类级 `__getattr__` 是实例缺属性的合法机制，非模块级
    PEP 562 钩子——与 def 形态检查 `_moduleLevelDefs` 不递归类体一致）。"""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for alias in stmt.names:
            if (alias.asname or alias.name) in _MODULE_GETATTR_NAMES:
                return f"import 别名绑定 {alias.asname or alias.name}（PEP 562 收集前钩子）"
        return None
    for node in ast.walk(stmt):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in _MODULE_GETATTR_NAMES
        ):
            return f"模块级 __getattr__/__dir__ 绑定（PEP 562 收集前钩子，{type(stmt).__name__} 形态）"
    return None


def findImportTimeHooks(path: Path) -> tuple[str, ...]:
    """`_findImportTimeHooksImpl` 的 fail-closed 包装（第二十二轮 L1）。

    深嵌套 AST（如数千层括号/调用链）会让递归遍历助手抛 RecursionError——
    此前异常直接炸穿守卫 ⇒ 该文件整条内容不变式消失（fail-open）。包装层
    把任何递归溢出转成显式 hook 行（守卫判红），而非让守卫崩掉。
    """
    try:
        return _findImportTimeHooksImpl(path)
    except RecursionError:
        return (
            "line 0: 递归深度超限（深嵌套 AST）⇒ 分析静态不可判 ⇒ fail-closed",
        )


def _findImportTimeHooksImpl(path: Path) -> tuple[str, ...]:
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
    if _countAstNodes(tree) > _MAX_AST_NODES:
        # 第二十二轮 L1：超预算文件的分析代价与递归风险都不可接受
        # ⇒ fail-closed（整文件一条 hook 行），不做部分分析。
        return (
            f"line 0: AST 节点数超过 {_MAX_AST_NODES} ⇒ 分析静态不可判 ⇒ fail-closed",
        )
    aliases = _aliasMap(tree.body)
    localClasses: dict[str, ast.ClassDef] = {}
    # 第十九轮 HIGH-5：同名类多重定义 ⇒ `X()` 实例化到哪个 body 静态不可判
    # ⇒ fail-closed（局部 dict 是"最后定义者胜"）。
    ambiguousClasses: set[str] = set()
    for c in _moduleLevelClasses(tree.body):
        if c.name in localClasses:
            ambiguousClasses.add(c.name)
        localClasses[c.name] = c
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
    # 第十九轮 HIGH-5：名字的多重 def/lambda 绑定（if/else 两臂、先定义后重定义）
    # 是"最后定义者胜"静态不可判 ⇒ 调用一律 fail-closed。
    ambiguousNames: set[str] = set()
    # 第十九轮 HIGH-3：实例变量 → 本地类（此前是 set，接收者解析不到类）。
    instanceVars: dict[str, ast.ClassDef] = {}
    # 第十九轮 HIGH-4：`_t = type` 的别名（三参 type() 检查按真名判的前提）。
    typeAliases: set[str] = set()
    # 第二十二轮 H1：本地类别名（`_c = X` ⇒ `_c()` 实例化 / `@_D.deco` 成员）。
    classAliases: dict[str, str] = {}
    # 第二十二轮 C2/H3/M1：不透明绑定——值形态静态不可判（IfExp/BoolOp/
    # Subscript/Attribute/本地工厂调用/容器字面量/…）⇒ 目标名记入；
    # 后续作接收者/可调用/下标基底一律 fail-closed（「unknown 放行」被
    # 洗白链利用的通道由此关闭）。
    opaqueNames: set[str] = set()

    def applyBinding(stmt: ast.stmt) -> None:
        if id(stmt) in classBodyIds:
            return  # 类体绑定不是模块全局名（方法/类属性）
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name in callTargets and callTargets[stmt.name] is not stmt:
                ambiguousNames.add(stmt.name)
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
                    if t.id in callTargets and callTargets[t.id] is not value:
                        ambiguousNames.add(t.id)
                    callTargets[t.id] = value
        elif isinstance(value, ast.Name):
            bound = callTargets.get(value.id)
            if bound is not None:
                for t in targets:
                    if isinstance(t, ast.Name):
                        callTargets[t.id] = bound
            if value.id == "type" or value.id in typeAliases:
                # 第二十二轮 H7：type 别名**链**传播（`_u = _t`，_t 已是别名）
                # ——此前只认 `value.id == "type"` 直呼，两级别名逃出三参检查。
                for t in _assignTargetNames(targets):
                    typeAliases.add(t.id)
            if value.id in localClasses:
                # 第二十二轮 H1：本地类别名（`_c = X`）；源类歧义 ⇒ 不透明
                # fail-closed（实例化目标静态不可判）。
                if value.id in ambiguousClasses:
                    for t in _assignTargetNames(targets):
                        opaqueNames.add(t.id)
                else:
                    for t in _assignTargetNames(targets):
                        classAliases[t.id] = value.id
            elif value.id in classAliases:
                # 第二十二轮 H1：类别名链传播（`_d = _c`）
                for t in _assignTargetNames(targets):
                    classAliases[t.id] = classAliases[value.id]
            if value.id in opaqueNames:
                for t in _assignTargetNames(targets):
                    opaqueNames.add(t.id)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in localClasses
        ):
            # 本地类实例化 ⇒ 记实例变量 → 类（with/for/就地运算符 dunder 与
            # 实例方法/属性接收者解析的判据）；元组解包按位对齐保守记入。
            cls = localClasses[value.func.id]
            for t in _assignTargetNames(targets):
                instanceVars[t.id] = cls
        elif isinstance(value, ast.Tuple):
            for t in targets:
                if (
                    isinstance(t, ast.Tuple)
                    and len(t.elts) == len(value.elts)
                ):
                    for tn, ve in zip(t.elts, value.elts):
                        if (
                            isinstance(tn, ast.Name)
                            and isinstance(ve, ast.Call)
                            and isinstance(ve.func, ast.Name)
                            and ve.func.id in localClasses
                        ):
                            instanceVars[tn.id] = localClasses[ve.func.id]
        # 第二十二轮 C2/H3/M1：静态不可判定的绑定值 ⇒ 目标名记入不透明集合。
        # 已识别形态（不记）：常量 / lambda / Name（上面分支处理）/ 本地类
        # 实例化（含元组逐位）/ import 进来的构造器或模块方法调用
        # （`BigInteger()`、`logging.getLogger(...)` —— 其方法链由
        # _resolveMember 的 unknown 语义放行，是文档化豁免）；其余
        # （IfExp/BoolOp/Subscript/Attribute/本地工厂调用/容器字面量/…）
        # ⇒ opaque，作接收者/可调用/下标基底时 fail-closed。
        if value is not None and not isinstance(
            value, (ast.Lambda, ast.Name, ast.Constant, ast.Dict, ast.List, ast.Set)
        ):
            # 容器字面量（Dict/List/Set）不记不透明（第二十二轮校准）：真实闭包
            # 模块级 dict/set 构造后调 `.keys()`/`.get()` 是常态；洗白路径已有
            # 独立覆盖——`_d[k]()` 下标调用拒（M154 线）、`_f = _d[k]` 值形态
            # 是 Subscript ⇒ _f 记不透明、`_d[k] = v` 是本地 dict 的良性形态。
            if isinstance(value, ast.Call):
                func = value.func
                if isinstance(func, ast.Name):
                    if func.id in callTargets or func.id in ambiguousNames:
                        # 本地工厂调用 / 歧义构造器 ⇒ 返回对象静态不可判
                        for t in _assignTargetNames(targets):
                            opaqueNames.add(t.id)
                elif isinstance(func, ast.Attribute):
                    root: ast.expr = func
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if not (isinstance(root, ast.Name) and root.id in aliases):
                        # 属性链根不是 import 名（`object.__new__(X)`）
                        for t in _assignTargetNames(targets):
                            opaqueNames.add(t.id)
                else:
                    # 第二十五轮评审 H-24-1：func 为 lambda/嵌套调用/下标等
                    # 的调用结果绑定（`_f = (lambda a: a.remove)(os)`）⇒ 返回
                    # 对象静态不可判 ⇒ 不透明（作可调用基底时由「不透明名被
                    # 调用」拒）。
                    for t in _assignTargetNames(targets):
                        opaqueNames.add(t.id)
            elif isinstance(value, ast.Tuple):
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Tuple)
                    and len(targets[0].elts) == len(value.elts)
                ):
                    for tn, ve in zip(targets[0].elts, value.elts):
                        if isinstance(tn, ast.Name) and not (
                            isinstance(ve, ast.Constant)
                            or (
                                isinstance(ve, ast.Call)
                                and isinstance(ve.func, ast.Name)
                                and ve.func.id in localClasses
                            )
                        ):
                            opaqueNames.add(tn.id)
                else:
                    for t in _assignTargetNames(targets):
                        opaqueNames.add(t.id)
            else:
                for t in _assignTargetNames(targets):
                    opaqueNames.add(t.id)

    visited: set[int] = set()
    # 第十九轮 HIGH-6 余量：实例方法调用的 func Attribute 已按成员解析处理，
    # 记下 id 让「实例属性读取」通则跳过（generator 是先序 ⇒ Call 先于其子节点）。
    resolvedAttrIds: set[int] = set()

    def scanCallee(
        callee: ast.AST,
        depth: int,
        enclosingAliases: dict[str, str] | None = None,
    ) -> None:
        if depth > _MAX_CALLEE_DEPTH:
            # 第十九轮 MEDIUM-2：此前静默截断 ⇒ 深链整段逃出；改 fail-closed
            add(getattr(callee, "lineno", 0), f"被调链深超过 {_MAX_CALLEE_DEPTH} ⇒ fail-closed")
            return
        if id(callee) in visited:
            return
        visited.add(id(callee))
        if isinstance(callee, ast.Lambda):
            scanExec(_iterExecNodes(callee.body), depth)
            return
        # 第二十二轮 H9：函数体内局部类（含遮蔽模块类名）此前整个逃出
        # ——fn-local 重定义/遮蔽 ⇒ None（实例化目标静态不可判）；正常
        # fn-local 类 ⇒ 覆盖层（函数作用域内遮蔽模块类，语义与 Python 一致）。
        fnClasses: dict[str, ast.ClassDef | None] = {}
        for c in _moduleLevelClasses(list(callee.body)):
            # 遮蔽模块类名不算歧义（Python 作用域语义确定解析到 fn-local）
            # ——覆盖层直接给 fn-local 类 ⇒ 钩子体可扫；只有**函数体内多重
            # 定义** ⇒ 静态不可判 ⇒ None（fail-closed）。
            if c.name in fnClasses:
                fnClasses[c.name] = None
            else:
                fnClasses[c.name] = c
        effectiveClasses = {**localClasses, **fnClasses}
        # 第二十二轮 H9：函数体内实例变量登记（with/for/真值判定的判据）。
        fnInstances: dict[str, ast.ClassDef] = {}
        for s in _moduleScopeStatements(list(callee.body)):
            if isinstance(s, (ast.Assign, ast.AnnAssign)):
                sTargets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if (
                    isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and isinstance(
                        effectiveClasses.get(s.value.func.id), ast.ClassDef
                    )
                ):
                    for t in _assignTargetNames(sTargets):
                        fnInstances[t.id] = effectiveClasses[s.value.func.id]
        effInstances = {**instanceVars, **fnInstances}
        # 第二十二轮 M2：函数体内 import 别名重绑定此前不追（文档化局限）
        # ——`def _e(): from os import environ as e; e[...] = ...` 整段逃出
        # ⇒ 函数体别名并入作用域映射，scanExec 的拒绝判定按作用域查。
        # 第二十六轮评审 H-26-3：被调的作用域映射必须带上**调用方的作用域
        # 别名**（fn-local 类 __init__ 经 scanExec(…, scopeAliases) 的实例化
        # 钩子进来）——此前只并模块级映射 ⇒ `def _o(): from … import flakes;
        # class _C: __init__ 里 flakes.hook = None` 整段逃出（探针实证）。
        # 顺序：内层自身 import > 调用方作用域 > 模块级。
        scopeAliases = {
            **aliases,
            **(enclosingAliases or {}),
            **_aliasMap(list(callee.body)),
        }
        for stmt in callee.body:
            fnProblem = _instanceExecProblem(stmt, effectiveClasses, effInstances)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # 第二十六轮评审 H-26-2：被扫函数体内的嵌套 def/class 装饰器
                # 此前只有模块级预扫管 ⇒ `def _o(): @(_mk() if True else None)
                # def g(): …` 经 callTargets 进来整段逃出（IfExp/下标两形态
                # 探针实证）。判据与模块级预扫一致（动态表达式 ⇒ 装饰期执行）。
                for dec in stmt.decorator_list:
                    if not isinstance(dec, (ast.Name, ast.Attribute, ast.Call)):
                        add(
                            stmt.lineno,
                            "装饰器为动态表达式 ⇒ 装饰期执行 ⇒ fail-closed",
                        )
            if fnProblem:
                add(stmt.lineno, fnProblem)
            if isinstance(stmt, (ast.If, ast.While, ast.Match)):
                fnTest = (
                    stmt.test if isinstance(stmt, (ast.If, ast.While)) else stmt.subject
                )
                if _usesLocalInstance(fnTest, effectiveClasses, effInstances):
                    add(
                        stmt.lineno,
                        "真值判定作用于本地类实例 ⇒ __bool__/__len__ 在 import 期执行 ⇒ fail-closed",
                    )
            if isinstance(stmt, ast.ClassDef):
                for line, detailMsg in _classCreationHookMessages(stmt):
                    add(line, detailMsg)
            scanExec(_iterExecNodes(stmt), depth, fnClasses, scopeAliases)

    def scanExec(
        nodes,
        depth: int,
        classes: dict[str, ast.ClassDef | None] | None = None,
        aliasesMap: dict[str, str] | None = None,
    ) -> None:
        # 第二十五轮 C-24-2：作用域别名映射单点——模块级扫描退化为模块映射，
        # 被调函数体扫描（scanCallee）传入 scopeAliases。此前 Store 根判定/
        # 装饰器解析直接用模块级 `aliases`，函数体内 import 的别名
        # （`def _m(): import pathlib; pathlib.Path.read_text = ...`）逃出。
        effAliases = aliases if aliasesMap is None else aliasesMap
        for node in nodes:
            detail = _denyMessage(node, effAliases)
            if detail:
                add(getattr(node, "lineno", 0), f"顶层引用 {detail}")
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if node.attr in _REBIND_DENIED_ATTRS:
                    add(getattr(node, "lineno", 0), f"顶层再绑定 {node.attr}")
                else:
                    # 第二十三轮评审 C-5/C-6：**模块根属性再绑定**——
                    # `sys.stdout = None` / `ast.parse = _fake` /
                    # `pytest.TerminalReporter.summary_failures = ...` /
                    # `Path.read_text = ...`（根是 import 别名）把 stdlib/
                    # 工具模块的接口换成假实现 ⇒ 证据通道与分析器双双致盲。
                    # 根是 import 别名（含函数作用域 import，effAliases）⇒ 拒
                    # （与成员名无关，fail-closed）；`self.x = 1` / `_p.attr = 1`
                    # （根非 import 名）不受影响（真实 76 文件闭包模块级
                    # Attribute Store 0 处，校准实证）。
                    chain = _dottedChain(node)
                    isStaticChain = chain is not None
                    if isStaticChain:
                        root = chain.partition(".")[0]
                        if root in effAliases or _isForbidden(root):
                            add(
                                getattr(node, "lineno", 0),
                                f"模块根属性再绑定 {chain} ⇒ 接口可被换假 ⇒ fail-closed",
                            )
                    if not isStaticChain:
                        # 第二十五轮评审 C-24-2：Store 基底不是静态名字/属性链
                        #（`[_d][0].foo = ...` / `f().x = ...`）⇒ 目标对象静态
                        # 不可判 ⇒ 接口可被换假 ⇒ fail-closed。
                        add(
                            getattr(node, "lineno", 0),
                            "属性赋值基底静态不可判（非名字/属性链）"
                            " ⇒ 接口可被换假 ⇒ fail-closed",
                        )
            if (
                isinstance(
                    node,
                    (ast.Subscript, ast.BinOp, ast.Compare, ast.UnaryOp, ast.BoolOp),
                )
                and _usesLocalInstance(node, localClasses, instanceVars)
            ):
                # 第十九轮 HIGH-6：隐式协议 dunder（__getitem__/__add__/__lt__/
                # __neg__/__contains__…）无显式 Call 节点 ⇒ 整段 fail-closed
                add(
                    getattr(node, "lineno", 0),
                    "实例参与下标/运算/比较/真值取反 ⇒ 隐式协议 dunder 在 import 期执行 ⇒ fail-closed",
                )
            # 注：旧的「实例属性读取」独立 deny 块（第十九轮 HIGH-6 的裸名
            # 接收者形态）已删——下方 H3 块的 `_usesLocalInstance` 对 Name
            # 接收者同样命中（Name in instanceVars ⇒ True），条件严格超集
            # ⇒ 双线同拦拿不到独占变异取证（第二十二轮 M172 实证），由
            # H3 块统一拒绝（变异 M181 钉 H3）。
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and id(node) not in resolvedAttrIds
                and _usesLocalInstance(node.value, localClasses, instanceVars)
            ):
                # 第二十二轮 H3：实例化结果直接属性读取（`_p = X().p`）⇒
                # property getter 在 import 期执行——此前读取通则只认裸名
                # 接收者，`X().p` 的 Call 值形态逃出。
                add(
                    getattr(node, "lineno", 0),
                    "实例化结果的属性读取 ⇒ property/__getattr__ 可能执行 ⇒ fail-closed",
                )
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id in instanceVars
            ):
                # 第二十二轮 H5：实例属性赋值（`_x.y = 1`）⇒ __setattr__ 在
                # import 期执行——Store 分支此前只查 _REBIND_DENIED_ATTRS。
                add(
                    getattr(node, "lineno", 0),
                    "实例属性赋值 ⇒ __setattr__ 在 import 期执行 ⇒ fail-closed",
                )
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and _usesOpaqueName(node.value, opaqueNames)
            ):
                # 第二十二轮 C2：不透明绑定的容器下标赋值（`_e['K'] = 'V'`，
                # _e 洗自 `[os][0].environ`）⇒ __setitem__ 静态不可判。
                add(
                    getattr(node, "lineno", 0),
                    "不透明绑定的容器下标赋值 ⇒ __setitem__ 静态不可判 ⇒ fail-closed",
                )
            if isinstance(
                node,
                (
                    ast.JoinedStr,
                    ast.Dict,
                    ast.ListComp,
                    ast.SetComp,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.Starred,
                ),
            ) and _usesLocalInstanceInProtocolPosition(node, localClasses, instanceVars):
                # 第二十二轮 H5：隐式协议节点补齐——f-string（__format__）、
                # dict 键（__hash__）、推导/解包（__iter__）；M164 锚定的
                # 下标/运算/比较元组刻意保持原样（变异判别点独占）。
                add(
                    getattr(node, "lineno", 0),
                    "实例参与 f-string/dict/推导/解包 ⇒ __format__/__hash__/__iter__ 隐式 dunder ⇒ fail-closed",
                )
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], (ast.Tuple, ast.Starred))
                and _usesLocalInstance(node.value, localClasses, instanceVars)
            ):
                # 第二十二轮 H5：元组解包迭代（`_a, _b = _x`）⇒ __iter__/__next__
                add(
                    getattr(node, "lineno", 0),
                    "元组解包迭代本地类实例 ⇒ __iter__/__next__ 在 import 期执行 ⇒ fail-closed",
                )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in getattr(node, "decorator_list", []):
                    if isinstance(dec, ast.Name) and dec.id in callTargets:
                        scanCallee(callTargets[dec.id], depth + 1, aliasesMap)
                    else:
                        # `@D.deco` / `@factory(...)`：装饰器在 def/class 语句
                        # 执行期被调用。第二十三轮评审 H-2 + 第二十五轮评审
                        # C-24-1：装饰器按**整体求值**建模——Call 装饰器先判
                        # 工厂形态（lambda/嵌套调用 ⇒ 返回的装饰器闭包体静态
                        # 不可判），再解析工厂链根（项目/禁止 ⇒ 拒，外部 ⇒ 放
                        # 行，本地 def 工厂 ⇒ 拒）。裸名（Name）装饰器**不**
                        # 在这里判——由下方第二段 if 链（实例/类别名/本地类/
                        # 不透明/裸名 fail-closed）独占判定，保证每条 deny 臂
                        # 的变异判别点独占。
                        if isinstance(dec, ast.Call):
                            callee = dec.func
                            if isinstance(callee, ast.Lambda):
                                # `@(lambda f: f)(hook)` —— 返回的装饰器闭包体不可静态判定
                                add(
                                    dec.lineno,
                                    "装饰器工厂为 lambda ⇒ 返回的装饰器静态不可判"
                                    " ⇒ 装饰期执行 ⇒ fail-closed",
                                )
                            elif isinstance(callee, ast.Call):
                                # `@(f())(x)` —— 工厂本身是调用结果 ⇒ 不可判
                                add(
                                    dec.lineno,
                                    "装饰器工厂本身是调用结果 ⇒ 返回的装饰器静态不可判"
                                    " ⇒ 装饰期执行 ⇒ fail-closed",
                                )
                            else:
                                decChain = _dottedChain(callee)
                                if decChain is None:
                                    add(
                                        dec.lineno,
                                        "装饰器工厂形态静态不可判（非名字/属性链）"
                                        " ⇒ 装饰期执行 ⇒ fail-closed",
                                    )
                                else:
                                    rootProblem = _decoratorRootProblem(
                                        decChain, effAliases
                                    )
                                    if rootProblem is not None:
                                        add(dec.lineno, rootProblem)
                                    elif (
                                        isinstance(callee, ast.Name)
                                        and (
                                            callee.id in callTargets
                                            or callee.id in ambiguousNames
                                        )
                                    ):
                                        # 第二十五轮 C-24-1：本地工厂装饰器
                                        #（`@g()`，g 是本地 def）—— g 的体可扫，
                                        # 但返回的嵌套闭包体（注册式装饰器）在
                                        # 装饰期真实执行且不可静态判定（实测
                                        # `@factory()` 内层 deco 写文件逃逸）。
                                        add(
                                            dec.lineno,
                                            "装饰器工厂是本地函数 ⇒ 返回的装饰器闭包体"
                                            "不可静态判定 ⇒ 装饰期执行 ⇒ fail-closed",
                                        )
                                    elif (
                                        isinstance(callee, ast.Name)
                                        and callee.id not in effAliases
                                        and callee.id not in _DECORATOR_BUILTIN_ALLOW
                                    ):
                                        # 未 import 且非内置白名单的裸名工厂
                                        add(
                                            dec.lineno,
                                            "装饰器工厂裸名静态不可判（未 import 且非内置白名单）"
                                            " ⇒ 装饰期执行 ⇒ fail-closed",
                                        )
                        elif isinstance(dec, ast.Attribute):
                            decChain = _dottedChain(dec)
                            if decChain is None:
                                add(
                                    dec.lineno,
                                    "装饰器基底静态不可判（非名字/属性链）"
                                    " ⇒ 装饰期执行 ⇒ fail-closed",
                                )
                            else:
                                rootProblem = _decoratorRootProblem(decChain, effAliases)
                                if rootProblem is not None:
                                    add(dec.lineno, rootProblem)
                        decAmbiguity: set[str] = set()
                        member = _decoratorMember(
                            dec, localClasses, callTargets, classAliases, decAmbiguity
                        )
                        if decAmbiguity:
                            # 第二十二轮 H6：装饰器成员绑定静态不可判 ⇒ fail-closed
                            add(
                                dec.lineno,
                                "装饰器成员绑定静态不可判（条件臂/未识别形态）⇒ fail-closed",
                            )
                        if member is not None and not isinstance(member, ast.ClassDef):
                            scanCallee(member, depth + 1, aliasesMap)
                    if isinstance(dec, ast.Name) and dec.id not in callTargets:
                        # 第二十二轮 H1/H2：实例/本地类（含别名）/不透明名作装饰器
                        if dec.id in instanceVars:
                            callMember = _classMember(
                                instanceVars[dec.id], "__call__", callTargets
                            )
                            if callMember is not None and not isinstance(callMember, ast.ClassDef):
                                scanCallee(callMember, depth + 1, aliasesMap)
                        elif dec.id in classAliases:
                            for hook in _instantiationHooks(
                                localClasses[classAliases[dec.id]], callTargets
                            ):
                                scanCallee(hook, depth + 1, aliasesMap)
                        elif dec.id in localClasses:
                            for hook in _instantiationHooks(localClasses[dec.id], callTargets):
                                scanCallee(hook, depth + 1, aliasesMap)
                        elif dec.id in opaqueNames:
                            add(dec.lineno, "不透明绑定的名字作装饰器 ⇒ 装饰期执行 ⇒ fail-closed")
                        else:
                            # 第二十三轮评审 H-2：裸名装饰器 fail-closed。
                            # 走到这里 = 该名不在实例/类别名/本地类/不透明
                            # 绑定里：要么经别名解析（from-import）要么完全
                            # 无静态绑定。解析到项目根/拒绝链 ⇒ 拒；完全无
                            # 绑定且非内置白名单 ⇒ 静态不可判 ⇒ 拒。
                            # 第二十六轮评审 H-26-4：按**作用域**映射解析——
                            # 此前用模块级 aliases ⇒ fn 作用域的项目 import
                            # 遮蔽模块级外部同名别名时
                            #（`from functools import lru_cache as dec` +
                            # `def _o(): from app.utils.flakes import hook
                            # as dec; @dec`）解析到外部根放行（探针实证）。
                            decResolvedName = effAliases.get(dec.id)
                            if decResolvedName is None:
                                if dec.id not in _DECORATOR_BUILTIN_ALLOW:
                                    add(
                                        dec.lineno,
                                        "装饰器裸名静态不可判（未 import 且非内置白名单）"
                                        " ⇒ 装饰期执行 ⇒ fail-closed",
                                    )
                            elif (
                                decResolvedName.split(".")[0] in _PROJECT_DECORATOR_ROOTS
                                or _isForbidden(decResolvedName)
                            ):
                                add(
                                    dec.lineno,
                                    f"装饰器 {dec.id} 解析到项目/禁止代码（{decResolvedName}）"
                                    " ⇒ 装饰期执行 ⇒ fail-closed",
                                )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    # 调用路径对 func Attribute 的读取有决定权（成员已解析/
                    # 拒绝/放行其一，先序遍历 ⇒ 本处先于 func 子节点）——
                    # 属性读取通则让位，否则解析线的变异会被读取通则罩住假绿。
                    resolvedAttrIds.add(id(node.func))
                if isinstance(node.func, ast.Subscript):
                    # 第十九轮 HIGH-1 余量：`d['exec'](...)` 下标取可调用
                    add(
                        node.lineno,
                        "下标取可调用再调用 ⇒ 可执行目标静态不可判 ⇒ fail-closed",
                    )
                elif isinstance(node.func, ast.Lambda):
                    scanCallee(node.func, depth + 1, aliasesMap)  # IIFE：lambda 体在 import 期执行
                elif isinstance(node.func, ast.Name):
                    if classes is not None and node.func.id in classes:
                        # 第二十二轮 H9：函数体内局部类实例化——遮蔽/重定义
                        # （值为 None）⇒ 静态不可判 fail-closed；正常 fn-local
                        # 类 ⇒ 实例化钩子在 import 期（被调时）执行。
                        fnCls = classes[node.func.id]
                        if fnCls is None:
                            add(
                                node.lineno,
                                "函数体内局部类多重定义 ⇒ 实例化目标静态不可判 ⇒ fail-closed",
                            )
                        else:
                            fnInstAmbiguity: set[str] = set()
                            for hook in _instantiationHooks(
                                fnCls, callTargets, fnInstAmbiguity
                            ):
                                scanCallee(hook, depth + 1, aliasesMap)
                            if fnInstAmbiguity:
                                add(
                                    node.lineno,
                                    f"类 {node.func.id} 的钩子绑定静态不可判（条件臂/未识别形态）⇒ fail-closed",
                                )
                    elif node.func.id in localClasses:
                        if node.func.id in ambiguousClasses:
                            # 第十九轮 HIGH-5：同名类多重定义 ⇒ 实例化目标静态不可判
                            add(
                                node.lineno,
                                f"类 {node.func.id} 多重定义 ⇒ 实例化目标静态不可判 ⇒ fail-closed",
                            )
                        else:
                            # 本地类实例化：__new__/__init__/__post_init__ 在 import 期执行
                            instAmbiguity: set[str] = set()
                            for hook in _instantiationHooks(
                                localClasses[node.func.id], callTargets, instAmbiguity
                            ):
                                scanCallee(hook, depth + 1, aliasesMap)
                            if instAmbiguity:
                                # 第二十二轮 H6：钩子绑定静态不可判 ⇒ 实例化语义不可判
                                add(
                                    node.lineno,
                                    f"类 {node.func.id} 的钩子绑定静态不可判（条件臂/未识别形态）⇒ fail-closed",
                                )
                    elif node.func.id in classAliases:
                        # 第二十二轮 H1：本地类别名实例化（`_c = X; _c()`）——
                        # 语义与 `_c` 所指类直接实例化相同 ⇒ 扫同一组实例化钩子。
                        for hook in _instantiationHooks(
                            localClasses[classAliases[node.func.id]], callTargets
                        ):
                            scanCallee(hook, depth + 1, aliasesMap)
                    elif node.func.id in opaqueNames:
                        # 第二十二轮：不透明绑定的名字被调用 ⇒ 可执行目标静态
                        # 不可判（`_p = _d['f']; _p()` 洗白通道）⇒ fail-closed。
                        add(
                            node.lineno,
                            "不透明绑定的名字被调用 ⇒ 可执行目标静态不可判 ⇒ fail-closed",
                        )
                    elif node.func.id in instanceVars:
                        # 第二十二轮 H2：可调用实例 `_x()` ⇒ __call__ 体在
                        # import 期执行——此前 Name 分支不查 instanceVars。
                        callMember = _classMember(
                            instanceVars[node.func.id], "__call__", callTargets
                        )
                        if callMember is not None and not isinstance(callMember, ast.ClassDef):
                            scanCallee(callMember, depth + 1, aliasesMap)
                    else:
                        if node.func.id in ambiguousNames:
                            # 第十九轮 HIGH-5：def/lambda 多重绑定 ⇒ 目标静态不可判
                            add(
                                node.lineno,
                                f"调用目标 {node.func.id} 多重定义（分支/重定义）⇒ 静态不可判 ⇒ fail-closed",
                            )
                        callee = callTargets.get(node.func.id)
                        if callee is not None:
                            scanCallee(callee, depth + 1, aliasesMap)
                        # 第十九轮 HIGH-6：实例作为调用参数 ⇒ __str__/__iter__ 等协议
                        argNodes = (*node.args, *(kw.value for kw in node.keywords))
                        if any(
                            _usesLocalInstance(a, localClasses, instanceVars) for a in argNodes
                        ):
                            add(
                                node.lineno,
                                "本地类实例作为调用参数 ⇒ __str__/__iter__ 等协议可能执行 ⇒ fail-closed",
                            )
                elif isinstance(node.func, ast.Attribute):
                    memberAmbiguity: set[str] = set()
                    member, status = _resolveMember(
                        node.func, localClasses, instanceVars, callTargets,
                        opaqueNames, classAliases, memberAmbiguity,
                    )
                    # 注：memberAmbiguity 非空时 _resolveMember 内部已把该成员
                    # 转成 "dynamic" 返回（三处歧义点各自紧跟 return dynamic）
                    # ⇒ 由下方 `status == "dynamic"` 统一 fail-closed，这里
                    # 不再重复判（重复线拿不到独占变异取证，第二十二轮实证）。
                    if status == "dynamic":
                        # 第十九轮 HIGH-1 余量：`make().m()` 动态接收者
                        add(
                            node.lineno,
                            "动态接收者（非裸名/本地类实例化）上的方法调用 ⇒ 返回对象静态不可判 ⇒ fail-closed",
                        )
                    elif isinstance(member, ast.ClassDef):
                        # 嵌套类实例化（Outer.Inner()）
                        nestedAmbiguity: set[str] = set()
                        for hook in _instantiationHooks(member, callTargets, nestedAmbiguity):
                            scanCallee(hook, depth + 1, aliasesMap)
                        if nestedAmbiguity:
                            # 第二十二轮 H6：钩子绑定静态不可判 ⇒ 实例化语义不可判
                            add(
                                node.lineno,
                                "嵌套类钩子绑定静态不可判（条件臂/未识别形态）⇒ fail-closed",
                            )
                    elif member is not None:
                        resolvedAttrIds.add(id(node.func))
                        scanCallee(member, depth + 1, aliasesMap)
                    elif status == "local":
                        # 本地类/实例上未定义的成员 ⇒ 继承/property 途径。
                        # 例外：`model_rebuild` 机制调用（本地 pydantic 类继承
                        # BaseModel 的形态在真实闭包存在）——白名单语义本就是
                        # 「本地未重定义才放行」，本地重定义场景上面已扫其体。
                        if node.func.attr not in _ALLOWED_EXPR_METHODS:
                            add(
                                node.lineno,
                                "本地类/实例上未定义的成员调用 ⇒ 继承/property 途径 ⇒ fail-closed",
                            )
                    else:
                        # unknown 接收者（成员解析不出）：sink 成员仍拒
                        # （第二三轮评审 C-4 余量——`_p = Path('x'); _r =
                        # _p.unlink()` 赋值式逃逸），其余放行（真实闭包的
                        # dict.get 等形态，校准约束）。
                        if node.func.attr in _DENIED_CALL_ATTRS:
                            # 第二十五轮评审 M-24-1：字面量接收者是良性形态
                            #（`'a'.replace('a','b')`）⇒ 豁免；变量绑定的
                            # 字符串仍 fail-closed（需要常量传播才可证，
                            # 文档化局限）。其余接收者（Path(...) 构造、
                            # 未知变量）fail-closed。
                            if isinstance(node.func.value, ast.Constant):
                                pass
                            else:
                                add(
                                    node.lineno,
                                    f"成员调用 .{node.func.attr} 是文件系统/进程 sink ⇒ fail-closed",
                                )
                else:
                    # 第二十三轮评审 H-1：Call 分派此前只认 Name/Attribute/
                    # Lambda/Subscript 四形态，其余（IfExp/NamedExpr/Call/
                    # BinOp/…作被调）**静默跳过** ⇒ `(m if True else None)()`
                    # 在被扫函数体里逃逸（顶层裸调用被结构规则拦，赋值/
                    # 被调体内可达）。fail-closed：不认识的被调形态一律拒。
                    # **例外（第二十五轮 H-24-1 收紧）**：func 本身是 Call
                    # （`f()(...)` 注册式）——仅当**内层被调**解析到外部
                    # import（别名根非项目/非禁止）才放行：真实闭包的
                    # pydantic `field_validator(...)（_v)` 注册惯用法（第二十
                    # 四轮复审实测 11 处互异行）。内层是本地 def / lambda /
                    # 更深嵌套 / 不可判形态 ⇒ 返回的可执行目标不可证
                    # ⇒ fail-closed（`pick(os)('/tmp/x')` 返回 os.remove 的
                    # 逃逸通道实证——「内层经同一分派递归判定」只对内层
                    # 调用本身成立，对「返回可调用再调用」不成立）。
                    if not isinstance(node.func, ast.Call):
                        add(
                            node.lineno,
                            "被调形态静态不可判"
                            "（非裸名/成员/lambda/下标/嵌套调用）⇒ fail-closed",
                        )
                    else:
                        innerCallee = node.func.func
                        innerChain = (
                            _dottedChain(innerCallee)
                            if isinstance(innerCallee, (ast.Name, ast.Attribute))
                            else None
                        )
                        isProvenRegistration = False
                        if innerChain is not None:
                            innerRoot = innerChain.split(".")[0]
                            innerResolved = effAliases.get(innerRoot, innerRoot)
                            # 第二十六轮评审 H-26-1：通用可调用工厂（partial/
                            # methodcaller/attrgetter）与实参 sink 成员引用
                            # 都让「返回的可执行目标」不可证——组合子能把
                            # 任意 sink 绑进返回值（探针实证逃逸）。
                            resolvedInnerChain = (
                                innerResolved + innerChain[len(innerRoot):]
                            )
                            if (
                                innerRoot in effAliases
                                and innerResolved.split(".")[0]
                                not in _PROJECT_DECORATOR_ROOTS
                                and not _isForbidden(innerChain)
                                and resolvedInnerChain not in _CALLABLE_FACTORY_DENIED
                            ):
                                # sink 实参扫描只在工厂条件通过后做——两个
                                # 消息归位互不抢（partial 形态两条件同时
                                # 命中时由工厂兜底消息接住）。
                                sinkProblem = _callFactorySinkProblem(node)
                                if sinkProblem is not None:
                                    add(node.lineno, sinkProblem)
                                else:
                                    isProvenRegistration = True
                        if not isProvenRegistration:
                            add(
                                node.lineno,
                                "嵌套调用工厂的内层被调静态不可判/本地/禁止"
                                " ⇒ 返回的可执行目标不可证 ⇒ fail-closed",
                            )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (node.func.id == "type" or node.func.id in typeAliases)
                and len(node.args) >= 3
            ):
                # `X = type('X', (), {...})` 动态建类：dict 里的可调用体静态
                # 追不完 ⇒ fail-closed（真实闭包 0 处）。第十九轮 HIGH-4：
                # `type` 可被别名（`_t = type`）⇒ 按别名映射后的真名判。
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
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 第十九轮 HIGH-5：装饰器求值发生在「被装饰名绑定**之前**」——
            # applyBinding 之后再解析会把 `@m def m` 的 @m 解到新 m ⇒ 漏旧绑定
            # ⇒ 装饰器解析必须在 applyBinding 之前做。
            for dec in stmt.decorator_list:
                if not isinstance(dec, (ast.Name, ast.Attribute, ast.Call)):
                    # 第十九轮 MEDIUM-1 + 第二十五轮评审 C-24-1：装饰器为动态
                    # 表达式（lambda/下标/IfExp/BoolOp/NamedExpr/Starred/…）
                    # ⇒ 装饰期任意代码执行 ⇒ fail-closed。白名单 = 静态可
                    # 解析三形态（Name 走第二段 if 链，Call/Attribute 走
                    # scanExec 装饰器臂）。
                    add(dec.lineno, "装饰器为动态表达式 ⇒ 装饰期执行 ⇒ fail-closed")
            for dec in stmt.decorator_list:
                if isinstance(dec, ast.Name) and dec.id in callTargets:
                    scanCallee(callTargets[dec.id], 0)
                else:
                    member = _decoratorMember(dec, localClasses, callTargets)
                    if member is not None and not isinstance(member, ast.ClassDef):
                        scanCallee(member, 0)
        applyBinding(stmt)
        scanExec(_iterExecNodes(stmt), 0)
        if (
            isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.AugAssign))
            and id(stmt) not in classBodyIds
        ):
            problem = _instanceExecProblem(stmt, localClasses, instanceVars)
            if problem:
                add(stmt.lineno, problem)
        if (
            isinstance(stmt, (ast.If, ast.While, ast.Match))
            and id(stmt) not in classBodyIds
        ):
            # 第十九轮 HIGH-6：`if _x:` / `while _x:` / `match _x:` ⇒ __bool__/__len__
            test = stmt.test if isinstance(stmt, (ast.If, ast.While)) else stmt.subject
            if _usesLocalInstance(test, localClasses, instanceVars):
                add(
                    stmt.lineno,
                    "模块级真值判定作用于本地类实例 ⇒ __bool__/__len__ 在 import 期执行 ⇒ fail-closed",
                )
        if id(stmt) not in classBodyIds:
            pep = _pep562BindingProblem(stmt)
            if pep:
                add(stmt.lineno, pep)
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
                        setter = _classMember(
                            localClasses[value.func.id], "__set_name__", callTargets
                        )
                        if setter is not None:
                            scanCallee(setter, 0)
            # 第二十二轮 H8：类体语句级检查（此前 with/for/就地运算符与真值
            # 判定只在模块级跑、显式豁免类体 ⇒ 类体形态整个逃出）。类体局部
            # 实例变量（`_x = X()` 类属性）先登记再检查。
            cbVars: dict[str, ast.ClassDef] = {}
            for s in _moduleScopeStatements(stmt.body):
                if isinstance(s, (ast.Assign, ast.AnnAssign)):
                    v = s.value
                    cbTargets = s.targets if isinstance(s, ast.Assign) else [s.target]
                    if (
                        isinstance(v, ast.Call)
                        and isinstance(v.func, ast.Name)
                        and v.func.id in localClasses
                    ):
                        for t in _assignTargetNames(cbTargets):
                            cbVars[t.id] = localClasses[v.func.id]
            cbInstances = {**instanceVars, **cbVars}
            for s in _moduleScopeStatements(stmt.body):
                cbProblem = _instanceExecProblem(s, localClasses, cbInstances)
                if cbProblem:
                    add(s.lineno, cbProblem)
                if isinstance(s, (ast.If, ast.While, ast.Match)):
                    cbTest = s.test if isinstance(s, (ast.If, ast.While)) else s.subject
                    if _usesLocalInstance(cbTest, localClasses, cbInstances):
                        add(
                            s.lineno,
                            "类体真值判定作用于本地类实例 ⇒ __bool__/__len__ 在 import 期执行 ⇒ fail-closed",
                        )
    for d in _moduleLevelDefs(tree.body):
        if d.name in _MODULE_GETATTR_NAMES:
            add(d.lineno, f"模块级 {d.name}（PEP 562 收集前钩子）")
    return tuple(hooks)
