"""conftest 收集前 import 链的**派生式**守卫（第九/十二/十三/十四轮复审收口）。

枚举式清单已被实证连漏三层（`scripts/__init__` → `backend/__init__`（尚不存在）
→ `backend/app/__init__`（一直在链上却三处全漏））⇒ 本守卫不枚举文件，而是
**派生**：用 `ci_conftest_chain.preCollectionImportPaths` 扫出所有 conftest 的
模块级导入落点（**含祖先包 init 链** —— 第十二轮复审 HIGH-1：`from
app.domain.models import X` 会先执行 `app/domain/__init__.py`），断言每个落点
**要么**被 `detectCiSurfaceChanges` 命中（在 CI 相关面清单里）**要么**在底部
的显式豁免清单里。

⇒ 未来任何 conftest 新增模块级 import，都必须改这份守卫（本文件在 CI 相关面 +
  CODEOWNERS 里 ⇒ 必须复核）或把落点加进清单 ⇒ 链条被冻结在复核之下。
⇒ 变异取证：M91（掏空派生器）、M92（只解析叶子、丢祖先 init 链）⇒ 守卫须红。

第十二轮复审 HIGH-2 的传递闭包 + 内容不变式见文件中部（M93/M94/M95）。

第十三轮复审 HIGH-1/2/3 的收口（M100–M121）：
- **位置全覆盖**：import 期求值不只发生在「语句本身」—— Assign 值、If.test、
  except 类型、装饰器参数、默认值、注解、with item、match guard 都在收集前
  执行 ⇒ 每个位置一条 evil fixture（`test_hook_positions_are_denied`）；
  结构白名单与根链拒绝**解耦** ⇒ 另有**分支臂内**结构问题的逐臂测试
  （`test_structural_problem_in_every_branch_arm_is_caught` —— MEDIUM-2 的
  逐臂判别：M95/M111–M114 各自只杀一条臂 ⇒ 各自的参数必须红）。
- **别名**：`import sys as s` / `from os import environ as e` / `from sys
  import path` 后裸写 `path.insert(...)` ⇒ 根链先过别名映射再匹配拒绝清单。
- **派生边**：相对导入、`from X import *`（展开 `__all__`）、任意能解析到
  backend/ 下文件的顶层前缀、`pytest_plugins +=`（M106–M110）。
- **类创建钩子**：`metaclass=` 与 `__init_subclass__` 族在 class 语句执行
  （import 期）时就会跑（M116/M117）。
- **检测范围口径**：检测器覆盖 conftest 链闭包；**测试模块自身的模块级产品
  import 不在派生范围**（change 记录 §5 残余清单 —— 与「普通测试文件削弱」同类）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


# ---- 第十三/十四轮复审收口：位置全覆盖 / 别名 / 类创建钩子 / 派生边 ----

# 结构上允许（或位于允许形态内部）、只有**根链拒绝**能抓住的 import 期钩子。
# 每个位置一条 fixture（第十三轮 HIGH-1；MEDIUM-1 的 Assign 位置含在内）。
_EVIL_POSITION_SOURCES: dict[str, str] = {
    "assign_value": "import sys\nX = sys.path.insert(0, 'x')\n",
    "if_test": "import sys\nif sys.path.insert(0, 'x') or True:\n    pass\n",
    "handler_type": "try:\n    pass\nexcept __import__('os'):\n    pass\n",
    "decorator_arg": (
        "import sys\n"
        "def deco(f):\n    return f\n\n"
        "@deco(sys.path.insert(0, 'x'))\n"
        "def g():\n    pass\n"
    ),
    "default_value": "import sys\ndef f(x=sys.path.insert(0, 'x')):\n    pass\n",
    "annotation": "import sys\ndef f(x: sys.modules['evil']) -> None:\n    pass\n",
    "with_item": "with open('x') as f:\n    pass\n",
    "subscript_chain": "import sys\nsys.modules['evil'] = object()\n",
    # HIGH-2 别名：结构干净的位置（Assign / 别名重绑定），只有别名解析能抓住
    "import_alias": "import sys as s\nX = s.path.insert(0, 'x')\n",
    "from_alias": "from os import environ as e\ne['HOOK'] = '1'\n",
    "from_bare": "from sys import path\nX = path[0]\n",
    "alias_store": "from os import environ as e\ne = 2\n",
    # HIGH-2 类创建钩子
    "metaclass": "class M(type):\n    pass\n\nclass X(metaclass=M):\n    pass\n",
    "init_subclass": "class Base:\n    def __init_subclass__(cls):\n        cls.hooked = True\n",
    # LOW-2：动态接收者的「允许方法名」不放行。
    # ⚠️ 第二十轮 fixture 改构造器接收者形态：原 get_x() 工厂形态会被
    # 「动态接收者 fail-closed」线（M153 的线）罩住 ⇒ 顶层豁免行的
    # 「裸名接收者」约束失去独占判别点。本地类实例化不在动态拒绝之列、
    # model_rebuild 成员良性 ⇒ 顶层规则的 Name 接收者约束成为唯一拦线。
    "dynamic_receiver": (
        "class X:\n"
        "    @classmethod\n    def model_rebuild(cls):\n        pass\n\n"
        "X().model_rebuild()\n"
    ),
    # ---- 第十六轮（5H + 2M）收口 fixtures ----
    # H-1：vararg/kwarg 注解也是求值位置（首轮只扫 posonly/args/kwonly）
    "vararg_annotation": 'import sys\ndef f(*args: sys.path.insert(0, "x")):\n    ...\n',
    "kwarg_annotation": 'import sys\ndef f(**kw: sys.path.insert(0, "x")):\n    ...\n',
    # H-2：模块根级重绑定绕过别名链（_t = sys 后 _t.path.insert）
    "module_root_binding": "import sys\n_t = sys\n_r = _t.path.insert(0, 'x')\n",
    # H-2：__dict__ 下标拿模块命名空间
    "dict_subscript": "import sys\n_m = sys.__dict__['modules']\n",
    # M-1：拒绝根模块的星号导入（__all__ 动态展开 fail-closed 的前提）
    "star_from_denied": "from os import *\nenviron['HOOK'] = '1'\n",
    # H-3：metaclass 经 **kwargs 展开（kw.arg is None，静态不可判）
    "metaclass_kwargs": 'class M(type):\n    pass\n\nclass X(**{"metaclass": M}):\n    pass\n',
    # H-3：钩子定义藏在分支臂里
    "init_subclass_in_branch": (
        "class Base:\n    if True:\n        def __init_subclass__(cls):\n            cls.hooked = True\n"
    ),
    # H-3：钩子名经属性再绑定（Store 位置在 _REBIND_DENIED_ATTRS）
    "hook_attr_rebind": "class Base:\n    pass\n\nBase.__init_subclass__ = None\n",
    # H-3：基类是求值表达式（__mro_entries__ 途径）
    # 基类是求值表达式（__mro_entries__ 途径）—— 不带钩子 def，
    # 保证「基类 Call」检查是该 fixture 的独占判别点（否则 M130 变异不转红）
    "base_call": (
        "def get_x():\n    return ()\n\nclass Y(get_x()):\n    pass\n"
    ),
    # H-3：调用式改类（第二十轮重锚：`type.__setattr__` 根条目被第十九轮的
    # `__setattr__` 禁段 supersede ⇒ 纯纵深防御无独占判别点，改钉 `setattr`
    # 裸名根——不带 dunder 段、且顶层表达式规则不适用（赋值形态））
    "builtin_setattr_call": (
        "class B:\n    pass\n\n_r = setattr(B, '__init_subclass__', None)\n"
    ),
    # H-4：被跳过的函数体经模块级调用执行
    "called_def": "import sys\ndef m():\n    sys.path.insert(0, 'x')\n\n_x = m()\n",
    # H-4：IIFE lambda
    "lambda_iife": "import sys\n_h = (lambda: sys.path.insert(0, 'x'))()\n",
    # H-4：PEP 562 模块级 __getattr__（收集前钩子）
    "module_getattr": (
        "import importlib\n\n\ndef __getattr__(name):\n"
        "    return importlib.import_module('app.evil')\n"
    ),
    # LOW-2 补：允许方法名经属性再绑定也不放行
    "allowed_method_rebind": "class X:\n    pass\n\nX.model_rebuild = None\n",
    # ---- 第十八轮（第十七轮复审 5H + 1L）收口 fixtures ----
    # HIGH-1：__getattribute__ 链（任意属性通道；拒绝根里没有 sys.__getattribute__，
    # 只有禁止 dunder 段检查能抓）
    "dunder_getattribute": "import sys\n_r = sys.__getattribute__('path').insert(0, 'x')\n",
    # HIGH-1：from-import 的 dunder 符号（Name 位置经别名解析进禁止段检查）。
    # 刻意用 List 包裹裸名 Load：直赋值 `_r = __getattribute__` 会触发
    # 「模块根再绑定传播 + Store 分支拒绝」把 M137 的别名解析线罩成假绿；
    # `.insert(...)` 调用链会被动态根洗白检查先拦（第二十二轮两度实证）。
    "getattr_via_from_import": (
        "from sys import __getattribute__\n_r = [__getattribute__]\n"
    ),
    # HIGH-1：裸名 __builtins__（__main__ 里它本身是 dict，只能当根拒）。
    # 刻意止步于下标取值：后续 `_b(...)` 会触发「不透明名被调用」兜底线，
    # 同样把 M138 的拒绝根线罩成假绿（第二十二轮实证）。
    "builtins_subscript": "_b = __builtins__['exec']\n",
    # HIGH-3：调用目标的名字别名（_f = m 后 _f()）
    "called_def_alias": (
        "import sys\ndef m():\n    sys.path.insert(0, 'x')\n\n_f = m\n_x = _f()\n"
    ),
    # HIGH-3：语句序 —— 调用点之前定义的那个被执行（最后定义者胜 ⇒ 漏扫）
    "called_def_before_redef": (
        "import sys\ndef m():\n    sys.path.insert(0, 'x')\n\n_x = m()\n\ndef m():\n    pass\n"
    ),
    # HIGH-3：lambda 的名字别名（_g = _h 后 _g()）
    "called_lambda_alias": (
        "import sys\n_h = lambda: sys.path.insert(0, 'x')\n_g = _h\n_x = _g()\n"
    ),
    # HIGH-4：本地类重定义 model_rebuild（白名单形态，只有成员体扫描能抓）
    "model_rebuild_local_override": (
        "import sys\n"
        "class X:\n"
        "    @classmethod\n    def model_rebuild(cls):\n        sys.path.insert(0, 'x')\n\n"
        "X.model_rebuild()\n"
    ),
    # HIGH-2：类实例化跑 __init__
    "class_instantiation_init": (
        "import sys\nclass X:\n    def __init__(self):\n        sys.path.insert(0, 'x')\n\n_x = X()\n"
    ),
    # HIGH-2：实例方法调用（Assign 位置，结构白名单不拦 ⇒ 只有成员体扫描能抓）
    "class_member_call": (
        "import sys\nclass X:\n    def refresh(self):\n        sys.path.insert(0, 'x')\n\n"
        "_r = X().refresh()\n"
    ),
    # HIGH-2：嵌套类实例化（Outer.Inner()）
    "nested_class_instantiation": (
        "import sys\nclass Outer:\n    class Inner:\n        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n\n_x = Outer.Inner()\n"
    ),
    # HIGH-2：类体钩子名赋值形态（classmethod(lambda ...)）
    "init_subclass_assign": (
        "class Base:\n    __init_subclass__ = classmethod(lambda cls, **kw: None)\n\n"
        "class Sub(Base):\n    pass\n"
    ),
    # HIGH-2：with 作用于本地类实例（__enter__/__exit__ 隐式 dunder）
    "with_local_instance": (
        "import sys\nclass C:\n    def __enter__(self):\n        sys.path.insert(0, 'x')\n"
        "    def __exit__(self, *a):\n        return False\n\nwith C():\n    pass\n"
    ),
    # HIGH-2：就地运算符作用于本地类实例（__iadd__ 隐式 dunder）
    "augassign_local_instance": "import sys\nclass X:\n    pass\n\n_x = X()\n_x += 1\n",
    # HIGH-2：模块级三参 type() 动态建类（dict 里的可调用体追不完 ⇒ fail-closed）
    "type_three_arg": "def hook(self):\n    pass\n\nX = type('X', (), {'__init__': hook})\n",
    # HIGH-2：类体描述符赋值触发 __set_name__（`attr = D()`；被类创建钩子 def
    # 形态检查抓住 —— setter 扫描分支纯纵深防御，无独占判别点，见 M147 注）
    "set_name_via_descriptor": (
        "import sys\nclass D:\n    def __set_name__(self, owner, name):\n"
        "        sys.path.insert(0, 'x')\n\nclass Y:\n    attr = D()\n"
    ),
    # HIGH-5：PEP 562 钩子的赋值/注解形态（绑定本身即收集前钩子，体良性也拒）
    "module_getattr_assign": "__getattr__ = lambda name: []\n",
    "module_getattr_annassign": "__getattr__: object = lambda name: []\n",
    # HIGH-2：for 作用于本地类实例（__iter__/__next__ 隐式 dunder；
    # 独占落点：只有 For-iter 分支能抓，体里无可调用物）
    "for_local_instance": (
        "import sys\nclass It:\n    def __iter__(self):\n        sys.path.insert(0, 'x')\n"
        "        return iter(())\n\nfor _ in It():\n    pass\n"
    ),
    # HIGH-2：装饰器经类成员执行方法体（@D.deco —— 装饰在 import 期运行）
    "decorator_class_member": (
        "import sys\nclass D:\n    @staticmethod\n    def deco(cls):\n"
        "        sys.path.insert(0, 'x')\n        return cls\n\n@D.deco\nclass C:\n    pass\n"
    ),
    # ---- 第十九/二十轮复审收口 fixtures（每条对准一个检测点；排他性见变异组 M152–M174）----
    # HIGH-1：动态根属性链摸到禁段（lambda.__globals__）——最小链，避免 __builtins__ 根另判
    "lambda_globals_spine": "_g = (lambda: 0).__globals__\n",
    # HIGH-2：本地 def 工厂做接收者 ⇒ 成员不可静态解析
    "dynamic_receiver_local_factory": (
        "import sys\n"
        "class X:\n"
        "    def refresh(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "def make():\n"
        "    return X()\n"
        "_r = make().refresh()\n"
    ),
    # HIGH-3（绕过面）：下标取函数再调用
    "subscript_call": (
        "import sys\n"
        "def f():\n"
        "    sys.path.insert(0, 'x')\n"
        "_d = {'f': f}\n"
        "_r = _d['f']()\n"
    ),
    # HIGH-3：构造器接收者上的本地类成员调用（X().model_rebuild() 改名体）
    "ctor_member_call": (
        "import sys\n"
        "class X:\n"
        "    @classmethod\n"
        "    def model_rebuild(cls):\n"
        "        sys.path.insert(0, 'x')\n"
        "_r = X().model_rebuild()\n"
    ),
    # HIGH-2：setattr 禁段（object.__setattr__ 走根拒绝，sys.__setattr__ 走段拒绝）
    "setattr_segment": "import sys\n_r = sys.__setattr__('path', [])\n",
    # HIGH-3（重命名面）：实例变量上的本地类方法调用（model_rebuild 改名/换接收者）
    "instance_method_call": (
        "import sys\n"
        "class X:\n"
        "    @classmethod\n"
        "    def model_rebuild(cls):\n"
        "        sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "_x.model_rebuild()\n"
    ),
    # HIGH-4：类体名字绑定函数（__init__ = evil）
    "init_name_assign": (
        "import sys\n"
        "def evil(self):\n"
        "    sys.path.insert(0, 'x')\n"
        "class X:\n"
        "    __init__ = evil\n"
        "_x = X()\n"
    ),
    # HIGH-4：staticmethod/classmethod 包装
    "staticmethod_member": (
        "import sys\n"
        "def evil():\n"
        "    sys.path.insert(0, 'x')\n"
        "class Y:\n"
        "    model_rebuild = staticmethod(evil)\n"
        "Y.model_rebuild()\n"
    ),
    # HIGH-4：type() 三参别名（_t = type 绕过真名判定）
    "type_alias_three_arg": (
        "import sys\n"
        "def hook(self):\n"
        "    sys.path.insert(0, 'x')\n"
        "_t = type\n"
        "X = _t('X', (), {'__init__': hook})\n"
        "_x = X()\n"
    ),
    # HIGH-5：条件重定义 ⇒ 被调解析有歧义
    "cond_redef": (
        "import sys\n"
        "if True:\n"
        "    def m():\n"
        "        sys.path.insert(0, 'x')\n"
        "else:\n"
        "    def m():\n"
        "        pass\n"
        "_x = m()\n"
    ),
    # HIGH-5：类重定义 ⇒ 实例化目标有歧义
    "class_redef": (
        "import sys\n"
        "class X:\n"
        "    def __init__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "class X:\n"
        "    pass\n"
    ),
    # HIGH-5：装饰器引用尚未被覆盖绑定的旧名字
    "decorator_prev_binding": (
        "import sys\n"
        "def m(f):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return f\n"
        "\n"
        "@m\n"
        "def m():\n"
        "    pass\n"
    ),
    # HIGH-5：lambda 重定义 ⇒ 被调解析有歧义
    "lambda_redef": (
        "import sys\n"
        "_h = lambda: sys.path.insert(0, 'x')\n"
        "_h = lambda: None\n"
        "_x = _h()\n"
    ),
    # MEDIUM-3：实例下标 ⇒ __getitem__
    "instance_subscript": (
        "import sys\n"
        "class X:\n"
        "    def __getitem__(self, k):\n"
        "        sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "_y = _x[0]\n"
    ),
    # MEDIUM-3：实例真值判定 ⇒ __bool__/__len__
    "instance_truthiness": (
        "import sys\n"
        "class X:\n"
        "    def __bool__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return False\n"
        "_x = X()\n"
        "if _x:\n"
        "    pass\n"
    ),
    # MEDIUM-3：实例作为调用实参 ⇒ __iter__/__len__ 等协议方法
    "instance_as_arg": (
        "import sys\n"
        "class X:\n"
        "    def __iter__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return iter(())\n"
        "_r = list(X())\n"
    ),
    # MEDIUM-4：PEP 562 __getattr__ 经元组解包绑定
    "pep562_tuple_unpack": "__getattr__, _x = (lambda name: [], 1)\n",
    # MEDIUM-4：PEP 562 __getattr__ 经 from-import 别名绑定
    "pep562_import_alias": "from x import y as __getattr__\n",
    # MEDIUM-1：lambda 装饰器（装饰期执行 lambda 体）
    "lambda_decorator": (
        "import sys\n"
        "@(lambda f: (sys.path.insert(0, 'x'), f)[1])\n"
        "def g():\n"
        "    pass\n"
    ),
    # MEDIUM-2：被调链深超上限 ⇒ fail-closed（静默截断=漏检）
    "callee_depth_cap": (
        "import sys\n"
        "def f1():\n    f2()\n"
        "def f2():\n    f3()\n"
        "def f3():\n    f4()\n"
        "def f4():\n    f5()\n"
        "def f5():\n    f6()\n"
        "def f6():\n    f7()\n"
        "def f7():\n    f8()\n"
        "def f8():\n    f9()\n"
        "def f9():\n    sys.path.insert(0, 'x')\n"
        "_x = f1()\n"
    ),
    # MEDIUM-5：sys.meta_path 安装 import 钩子
    "meta_path_install": (
        "import sys\n"
        "class F:\n"
        "    pass\n"
        "_r = sys.meta_path.insert(0, F())\n"
    ),
    # MEDIUM-3：实例属性读 ⇒ @property getter
    "instance_property_read": (
        "import sys\n"
        "class X:\n"
        "    @property\n"
        "    def p(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return 1\n"
        "_x = X()\n"
        "_r = _x.p\n"
    ),
    # 收口：本地接收者上未定义成员的调用（继承属性不可静态排除 ⇒ fail-closed）
    "missing_member_call": (
        "class X:\n"
        "    pass\n"
        "_x = X()\n"
        "_r = _x.nope()\n"
    ),
    # ---- 第二十二轮（第二十一轮复审 2C + 9H + 2M + 1L）收口 fixtures ----
    # C1：守卫模块根链（rebind 守卫函数让内容不变式空转）
    "guard_module_import": (
        "import app.utils.ci_conftest_chain as _c\n"
        "_r = _c.findImportTimeHooks\n"
    ),
    # C2：动态根属性链的危险后缀段（[os][0].environ 洗白）
    "dynamic_env_launder": "import os\n_l = [os]\n_r = _l[0].environ\n",
    "dynamic_path_launder": "import sys\n_l = [sys]\n_r = _l[0].path\n",
    # H1：本地类别名实例化（_c = X; _c()）
    "class_alias_call": (
        "import sys\n"
        "class X:\n"
        "    def __init__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "_c = X\n"
        "_x = _c()\n"
    ),
    # H1：本地类别名作装饰器接收者（@_D.deco）
    "class_alias_decorator": (
        # 类别名的**成员**装饰器（`@_D.deco`）：判别点= _decoratorMember 的
        # classAliases 回退（M203）。裸名装饰器形态见 class_alias_bare_decorator。
        "import sys\n"
        "class D:\n"
        "    def deco(self, f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "_D = D\n"
        "@_D.deco\n"
        "def g():\n"
        "    pass\n"
    ),
    "class_alias_bare_decorator": (
        # 类别名作**裸名**装饰器（`@_D`）⇒ 装饰 = 实例化 ⇒ __init__ 在
        # import 期执行；判别点= scanExec 装饰器臂的 classAliases 分支（M178）。
        # _decoratorMember 只处理 Attribute 形态 ⇒ 裸名只走这一条线（独占）。
        "import sys\n"
        "class D:\n"
        "    def __init__(self, f):\n"
        "        sys.path.insert(0, 'x')\n"
        "_D = D\n"
        "@_D\n"
        "def g():\n"
        "    pass\n"
    ),
    # H2：可调用实例 _x()（__call__ 体 import 期执行）。⚠️ 调用必须藏在
    # 被扫函数体内（装饰器函数 _d 的体）：顶层裸 `_x()` 会被「顶层表达式」
    # 结构白名单线兜住 ⇒ 破坏 M179 判别点独占性（solo 取证实证）。
    "callable_instance_call": (
        "import sys\n"
        "class X:\n"
        "    def __call__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "def _d(f):\n"
        "    _x()\n"
        "    return f\n"
        "@_d\n"
        "def g():\n"
        "    pass\n"
    ),
    # H2：可调用实例作装饰器（@_x ⇒ __call__）
    "instance_decorator": (
        "import sys\n"
        "class X:\n"
        "    def __call__(self, f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "_x = X()\n"
        "@_x\n"
        "def g():\n"
        "    pass\n"
    ),
    # H3：实例化结果的属性读取（X().p ⇒ property getter）
    "ctor_property_read": (
        "import sys\n"
        "class X:\n"
        "    @property\n"
        "    def p(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return 1\n"
        "_r = X().p\n"
    ),
    # H4：super() 接收者（继承链返回对象静态不可判）
    "super_receiver_call": (
        "import sys\n"
        "class A:\n"
        "    def m(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "class B(A):\n"
        "    def __init__(self):\n"
        "        super().m()\n"
        "_b = B()\n"
    ),
    # H5：实例属性赋值（__setattr__）。⚠️ 类体必须全干净：载荷放 __init__
    # 会被实例化钩子线（_instantiationHooks）兜住 ⇒ 破坏 M183 判别点独占性
    # （solo 取证实证）；判别点= 赋值语句本身的 fail-closed 拒绝。
    "instance_setattr_store": (
        "class X:\n"
        "    v = 1\n"
        "_x = X()\n"
        "_x.attr = 1\n"
    ),
    # H5：f-string 插值（__format__）
    "fstring_format": (
        "import sys\n"
        "class X:\n"
        "    def __format__(self, spec):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return ''\n"
        "_r = f'{X()}'\n"
    ),
    # H5：dict 键（__hash__）
    "dict_key_hash": (
        "import sys\n"
        "class X:\n"
        "    def __hash__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return 0\n"
        "_r = {X(): 1}\n"
    ),
    # H5：元组解包迭代（__iter__/__next__）
    "tuple_unpack_iter": (
        "import sys\n"
        "class X:\n"
        "    def __iter__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "_a, _b = _x\n"
    ),
    # H6：成员绑定歧义（条件两臂同名方法，第一臂良性 ⇒ 只有歧义线能抓）
    "member_conditional_arm": (
        "import sys\n"
        "class X:\n"
        "    if True:\n"
        "        def m(self):\n"
        "            pass\n"
        "    else:\n"
        "        def m(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "_x = X()\n"
        "_r = _x.m()\n"
    ),
    # H6：实例化钩子绑定歧义（条件两臂 __init__，第一臂良性）
    "hook_conditional_arm": (
        "import sys\n"
        "class X:\n"
        "    if True:\n"
        "        def __init__(self):\n"
        "            pass\n"
        "    else:\n"
        "        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "_x = X()\n"
    ),
    # H7：type 别名链（_u = _t，_t = type）
    "type_alias_chain": (
        "import sys\n"
        "_t = type\n"
        "_u = _t\n"
        "X = _u('X', (), {'m': lambda s: sys.path.insert(0, 'x')})\n"
    ),
    # H8：类体 with 作用于本地类实例（__enter__/__exit__）
    "class_body_with_instance": (
        "import sys\n"
        "class X:\n"
        "    def __enter__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return self\n"
        "    def __exit__(self, *a):\n"
        "        return False\n"
        "class C:\n"
        "    _x = X()\n"
        "    with _x:\n"
        "        pass\n"
    ),
    # H8：类体真值判定（__bool__）
    "class_body_truthiness": (
        "import sys\n"
        "class X:\n"
        "    def __bool__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return True\n"
        "class C:\n"
        "    _x = X()\n"
        "    if _x:\n"
        "        pass\n"
    ),
    # H9：函数体内局部类遮蔽模块类（实例化目标静态不可判）
    "fn_local_class_shadow": (
        "import sys\n"
        "class X:\n"
        "    pass\n"
        "def m():\n"
        "    class X:\n"
        "        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "    return X()\n"
        "_x = m()\n"
    ),
    # H9：函数体内局部类（非遮蔽）实例化（__init__ 体被调）
    "fn_local_class_call": (
        "import sys\n"
        "def m():\n"
        "    class C:\n"
        "        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "    return C()\n"
        "_x = m()\n"
    ),
    # H9：被调函数体内 with 作用于实例（语句级检查）
    "callee_with_instance": (
        "import sys\n"
        "class Ctx:\n"
        "    pass\n"
        "def m():\n"
        "    _c = Ctx()\n"
        "    with _c:\n"
        "        pass\n"
        "_x = m()\n"
    ),
    # M2：函数体内 import 别名重绑定（e → os.environ）
    "fn_local_import_alias": (
        "def m():\n"
        "    from os import environ as e\n"
        "    e['HOOK'] = '1'\n"
        "_x = m()\n"
    ),
    # M1：不透明绑定传播（IfExp 值 ⇒ _g 不可判 ⇒ 调用 fail-closed）。
    # ⚠️ 调用必须藏在被扫函数体内：顶层裸 `_g()` 会被「顶层表达式」结构
    # 白名单线兜住 ⇒ 破坏 M196 判别点独占性（solo 取证实证）。
    "opaque_ifexp_call": (
        "import sys\n"
        "def f1():\n"
        "    sys.path.insert(0, 'x')\n"
        "def f2():\n"
        "    pass\n"
        "_g = f1 if True else f2\n"
        "def _d(f):\n"
        "    _g()\n"
        "    return f\n"
        "@_d\n"
        "def g():\n"
        "    pass\n"
    ),
    # 不透明绑定的名字被调用（_p = _d.get('f')）。调用同样藏进被扫函数体
    # （理由同 opaque_ifexp_call，M197 判别点独占性）。
    "opaque_name_call": (
        "_d = {}\n"
        "_p = _d.get('f')\n"
        "def _d2(f):\n"
        "    _p()\n"
        "    return f\n"
        "@_d2\n"
        "def g():\n"
        "    pass\n"
    ),
    # 不透明绑定的名字作装饰器
    "opaque_name_decorator": (
        "_d = {}\n"
        "_p = _d.get('f')\n"
        "@_p\n"
        "def g():\n"
        "    pass\n"
    ),
    # 不透明绑定的容器下标赋值（__setitem__ 静态不可判）
    "opaque_subscript_store": (
        "_d = {}\n"
        "_e = _d.get('x')\n"
        "_e['K'] = 'V'\n"
    ),
    # H6：函数体内局部类多重定义（先恶后良 ⇒ 只有 None 线能抓）
    "fn_local_class_redef": (
        "import sys\n"
        "def m():\n"
        "    class C:\n"
        "        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "    class C:\n"
        "        def __init__(self):\n"
        "            pass\n"
        "    return C()\n"
        "_x = m()\n"
    ),
    # H6：装饰器成员绑定歧义（@_D.deco 两臂，第一臂良性）
    "decorator_member_ambiguous": (
        "import sys\n"
        "class D:\n"
        "    if True:\n"
        "        def deco(self, f):\n"
        "            return f\n"
        "    else:\n"
        "        def deco(self, f):\n"
        "            sys.path.insert(0, 'x')\n"
        "            return f\n"
        "_D = D\n"
        "@_D.deco\n"
        "def g():\n"
        "    pass\n"
    ),
    # H6：嵌套类钩子绑定歧义（Outer.Inner 的 __init__ 两臂，第一臂良性）
    "nested_class_hook_ambiguous": (
        "import sys\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        if True:\n"
        "            def __init__(self):\n"
        "                pass\n"
        "        else:\n"
        "            def __init__(self):\n"
        "                sys.path.insert(0, 'x')\n"
        "_x = Outer.Inner()\n"
    ),
    # 本地类直接作装饰器（@X ⇒ 实例化钩子）
    "class_direct_decorator": (
        "import sys\n"
        "class X:\n"
        "    def __init__(self):\n"
        "        sys.path.insert(0, 'x')\n"
        "@X\n"
        "def g():\n"
        "    pass\n"
    ),
}

# 第二十二轮 M158/M159：拒绝必须归位到钩子真实位置（精识臂的独占贡献；
# 兜底 fail-closed 只会拒在调用点 ⇒ 位置断言是这两条精识线的变异判别点）。
_EXPECTED_HOOK_POSITIONS: dict[str, str] = {
    "init_name_assign": "line 3",
    "staticmethod_member": "line 3",
}

# 第二十四轮全量取证净漏 5（M133/M154/M178/M180/M200）：第二十四轮新增的
# fail-closed 兜底臂（func-form else / 装饰器裸名静态不可判）把五条精识臂的
# fixture 罩住了 —— 变异后守卫落到兜底仍拦 ⇒ 只断言非空假绿（「冗余 deny 线」
# 第四例，M172/M183/M186 同族）。M158/M159 的「归位」原则从**位置**扩展到
# **根链/形态文本**：精识臂的独占贡献是指认钩子根链（sys.path）或可执行目标
# 形态（下标取可调用）；兜底只报「静态不可判」不指认 ⇒ 文本归位断言即判别点。
# （M154 不能用 "sys.path"：下标臂本身拒在调用点不指认根链 ⇒ 用臂专属形态文本；
#   也不能用裸 "下标"——兜底报文的形态枚举里恰好含「下标」二字。）
_EXPECTED_HOOK_ROOTS: dict[str, str] = {
    "lambda_iife": "sys.path",
    "subscript_call": "下标取可调用",
    "class_alias_bare_decorator": "sys.path",
    "instance_decorator": "sys.path",
    "class_direct_decorator": "sys.path",
}


@pytest.mark.parametrize("case", sorted(_EVIL_POSITION_SOURCES))
def test_hook_positions_are_denied(tmp_path: Path, case: str) -> None:
    """每个 import 期求值位置都必须被根链拒绝/结构白名单抓住（M100/M101/M119 锚点）。"""
    path = tmp_path / "m.py"
    path.write_text(_EVIL_POSITION_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks, f"{case} 位置的 import 期钩子没被认出 —— 检测器有位置盲区"
    # 第二十二轮：精识臂（类体钩子绑定的 Name/描述符识别）被关时守卫会
    # 退到「fail-closed 拒在调用点」——仍然拦但不指认钩子真实位置 ⇒ 只断言
    # 非空会假绿（M158/M159 实证）。这两个 fixture 额外钉死归位。
    expected = _EXPECTED_HOOK_POSITIONS.get(case)
    if expected is not None:
        assert any(
            expected in hook for hook in hooks
        ), f"{case} 的拒绝没有归位到钩子真实位置（{expected}）：{hooks}"
    # 第二十四轮：精识臂被兜底罩住时（M133/M154/M178/M180/M200 实证）拒绝文本
    # 不再指认钩子根链/可执行目标形态 ⇒ 文本归位断言（见 _EXPECTED_HOOK_ROOTS 注释）。
    expectedRoot = _EXPECTED_HOOK_ROOTS.get(case)
    if expectedRoot is not None:
        assert any(
            expectedRoot in hook for hook in hooks
        ), f"{case} 的拒绝没有归位到钩子根链/形态（{expectedRoot}）：{hooks}"


# 定义性构造必须**放行**（守卫双向：检测器收紧过头 = 闭包内容不变式假红）。
_CLEAN_SOURCES: dict[str, str] = {
    "getLogger": "import logging\nLOG = logging.getLogger(__name__)\n",
    "os_path_read": "import os\nP = os.path.join('a', 'b')\n",
    "pathlib": "from pathlib import Path\nP = Path('x')\n",
    "model_rebuild_bare": (
        "class X:\n"
        "    @classmethod\n    def model_rebuild(cls):\n        pass\n\nX.model_rebuild()\n"
    ),
    # 函数/方法/lambda 体要等调用才执行 —— 体内的钩子不算 import 期
    "method_body": "import sys\nclass C:\n    def m(self):\n        sys.path.insert(0, 'x')\n",
    "function_body": "import sys\ndef m():\n    sys.path.insert(0, 'x')\n",
    "lambda_body": "import os\nF = lambda x: os.environ.get(x)\n",
    # 字段名恰好与拒绝清单同名（Store 上下文）不是引用（第十四轮校准实证）
    "field_named_input": "class M:\n    input: str = 'x'\n",
    "field_named_eval": "class N:\n    eval: int = 0\n",
    # match 的 guard 不进结构白名单（M-2 修复），良性 guard 必须放行
    "match_guard_benign": "match 1:\n    case _ if 1 > 0:\n        pass\n",
    # 第十六轮 M-2：结构白名单补齐的语句形态必须放行
    "augassign": "COUNT = 0\nCOUNT += 1\n",
    "type_alias": "type Alias = int\n",
    "global_stmt": "X = 1\n\ndef f():\n    global X\n    X = 2\n",
    # ---- 第十九/二十轮收口对应的良性惯用法（fail-closed 不得误伤）----
    # 字面量方法调用：接收者是 Constant ⇒ 无法承载钩子
    "str_join": "S = \",\".join(['a', 'b'])\n",
    # 导入构造器的属性链：BigInteger().with_variant(...) 是仓内真实惯用法
    "imported_ctor_chain": (
        "from sqlalchemy import BigInteger, Integer\n"
        "P = BigInteger().with_variant(Integer, 'sqlite')\n"
    ),
    # 本地类上无 override 的 model_rebuild ⇒ 成员缺失但 attr 在豁免清单
    "local_model_rebuild_inherit": "class M:\n    pass\n\nM.model_rebuild()\n",
    # 导入函数返回值的链式调用：getLogger(...).setLevel(...)。注意必须是赋值
    # 形态——裸顶层调用语句本就被「顶层表达式」规则拒绝（与本 fixture 无关）
    "imported_result_call": (
        "import logging\n"
        "RESULT = logging.getLogger('x').setLevel(10)\n"
    ),
    # 本地 lambda 直接调用：callTargets 命中 ⇒ 体内被扫，良性 ⇒ 放行
    "lambda_call_plain": "_f = lambda: 1\n_x = _f()\n",
    # 实例方法调用、成员已定义且体良性 ⇒ 扫描后放行
    "instance_method_clean": (
        "class X:\n"
        "    def refresh(self):\n"
        "        return 1\n"
        "_x = X()\n"
        "_r = _x.refresh()\n"
    ),
    # ---- 第二十二轮良性惯用法（fail-closed 不得误伤）----
    # 模块级容器字面量构造 + 方法调用（不透明豁免：Dict/List/Set 不记）
    "container_literal_methods": (
        "HANDLERS = {'a': 1, 'b': 2}\n"
        "_k = tuple(HANDLERS.keys())\n"
        "_n = len(HANDLERS)\n"
    ),
    # 类别名链但目标类体良性（_c = X; _c() ⇒ __init__ 体被扫后放行）
    "class_alias_clean": (
        "class X:\n"
        "    def __init__(self):\n"
        "        self.v = 1\n"
        "_c = X\n"
        "_x = _c()\n"
    ),
    # dict 值位放本地类实例（值位不是协议位；实例化钩子线已覆盖 __init__）
    "dict_value_instance": (
        "class Rule:\n"
        "    def __init__(self, op):\n"
        "        self.op = op\n"
        "RULES = {'A': Rule('lt'), 'B': Rule('gt')}\n"
    ),
    # 函数体内局部类遮蔽模块类、体良性（遮蔽是确定性语义 ⇒ 覆盖层解析后放行）
    "fn_local_class_clean": (
        "class X:\n"
        "    pass\n"
        "def m():\n"
        "    class X:\n"
        "        def __init__(self):\n"
        "            self.v = 1\n"
        "    return X()\n"
        "_x = m()\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_CLEAN_SOURCES))
def test_definitional_constructs_pass(tmp_path: Path, case: str) -> None:
    """定义性构造必须放行（M104/M105 锚点：body 跳过 / Load-ctx 判定被删 ⇒ 红）。"""
    path = tmp_path / "m.py"
    path.write_text(_CLEAN_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks == (), f"{case} 被误报：{hooks}"


# 结构问题（顶层裸表达式）逐**臂**放置 —— MEDIUM-2 逐臂判别：M95/M111–M114
# 各自只杀一条臂的展开 ⇒ 各自的参数必须红（basicConfig 不在拒绝清单里，
# 唯一的抓手是 `_moduleScopeStatements` 把臂内语句展开成结构检查）。
_ARM_SOURCES: dict[str, str] = {
    "top_level": "import logging\nlogging.basicConfig(level=1)\n",
    "if_body": "import logging\nif True:\n    logging.basicConfig(level=1)\n",
    "if_orelse": (
        "import logging\nif False:\n    pass\nelse:\n    logging.basicConfig(level=1)\n"
    ),
    "try_body": "import logging\ntry:\n    logging.basicConfig(level=1)\nexcept OSError:\n    pass\n",
    "handler_body": (
        "import logging\ntry:\n    pass\nexcept OSError:\n    logging.basicConfig(level=1)\n"
    ),
    "try_orelse": (
        "import logging\ntry:\n    pass\nexcept OSError:\n    pass\n"
        "else:\n    logging.basicConfig(level=1)\n"
    ),
    "try_final": (
        "import logging\ntry:\n    pass\nfinally:\n    logging.basicConfig(level=1)\n"
    ),
    "for_body": "import logging\nfor _ in []:\n    logging.basicConfig(level=1)\n",
    "while_body": "import logging\nwhile False:\n    logging.basicConfig(level=1)\n",
    "with_body": (
        "from contextlib import nullcontext\nimport logging\n"
        "with nullcontext():\n    logging.basicConfig(level=1)\n"
    ),
    "match_body": "import logging\nmatch 1:\n    case 1:\n        logging.basicConfig(level=1)\n",
    # match 的 guard 是表达式不是语句（第十六轮 M-2 修复）：结构白名单不再扫 guard，
    # 其内容改由拒绝扫描（_iterExecNodes 全子树）覆盖 ⇒ 本臂改用拒绝轴 evil 钉住。
    "match_guard": (
        "import sys\nmatch 1:\n    case _ if sys.path.insert(0, 'x'):\n        pass\n"
    ),
    "class_body": "import logging\nclass C:\n    logging.basicConfig(level=1)\n",
}


@pytest.mark.parametrize("case", sorted(_ARM_SOURCES))
def test_structural_problem_in_every_branch_arm_is_caught(tmp_path: Path, case: str) -> None:
    """每条分支臂里的顶层裸表达式都必须被结构白名单抓住（M95/M111–M114 锚点）。"""
    path = tmp_path / "m.py"
    path.write_text(_ARM_SOURCES[case], encoding="utf-8")

    assert findImportTimeHooks(path), f"{case} 里的顶层裸表达式没被认出 —— 分支展开有盲区"


def test_derivation_covers_relative_star_and_any_top_prefix(tmp_path: Path) -> None:
    """第十三轮 HIGH-3 派生边（M106–M110 锚点）：相对导入按所在包拼绝对名、
    `from X import *` 展开 `__all__`、任意能解析到 backend/ 下文件的顶层前缀
    都派生（不再硬编码 app/scripts）、`pytest_plugins +=` 同样算插件声明。"""
    backend = tmp_path / "backend"
    (backend / "tools").mkdir(parents=True)
    (backend / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "tools" / "helper.py").write_text("H = 1\n", encoding="utf-8")
    # ⚠️ `solo` 只能从 Import 语句边到达（from-import 边不含它）⇒ M109 的判别点
    (backend / "tools" / "solo.py").write_text("O = 1\n", encoding="utf-8")
    pkg = backend / "app" / "pkg"
    pkg.mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("__all__ = ['sub']\n", encoding="utf-8")
    (pkg / "sub.py").write_text("S = 1\n", encoding="utf-8")
    (backend / "app" / "sibling.py").write_text("B = 1\n", encoding="utf-8")
    (backend / "app" / "plug.py").write_text("P = 1\n", encoding="utf-8")
    (backend / "app" / "conftest.py").write_text(
        "from app.pkg import *\n"
        "from . import sibling\n"
        "from ..tools import helper\n"
        "import tools.helper\n"
        "import tools.solo\n"
        "pytest_plugins += ['app.plug']\n",
        encoding="utf-8",
    )

    mapping = preCollectionImportPaths(backend)

    assert len(mapping) == 1, f"conftest 没被扫到：{mapping}"
    derived = {path for files in mapping.values() for path in files}
    assert derived == {
        "backend/app/__init__.py",
        "backend/app/pkg/__init__.py",
        "backend/app/pkg/sub.py",
        "backend/app/sibling.py",
        "backend/tools/__init__.py",
        "backend/tools/helper.py",
        "backend/tools/solo.py",
        "backend/app/plug.py",
    }, f"派生边缺失或多出：{sorted(derived)}"


def test_class_body_imports_are_derived(tmp_path: Path) -> None:
    """类体 import 也在收集前执行（M115 锚点：类体作用域展开被删 ⇒ 本测试红）。"""
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "app" / "inner.py").write_text("I = 1\n", encoding="utf-8")
    (backend / "app" / "classy.py").write_text(
        "class C:\n    import app.inner\n", encoding="utf-8"
    )
    (backend / "conftest.py").write_text("import app.classy\n", encoding="utf-8")

    closure = transitivePreCollectionPaths(backend)

    assert "backend/app/inner.py" in closure, (
        "类体 import 没被派生 —— 类体作用域展开被删除/退化？"
    )


def test_dual_form_module_derives_both_leaves(tmp_path: Path) -> None:
    """同目录同时有 `foo.py` 与 `foo/__init__.py` ⇒ **两个叶子都进闭包**（第十五轮 H-5）。

    CPython `FileFinder` 先试包目录再试 `.py`：派生器若二选一（尤其优先 `.py`），
    攻击者保留无害 `foo.py`、新增恶意 `foo/__init__.py` 即可让真实执行点逃出闭包。
    收超集只会让闭包偏大（fail-closed 方向）。
    """
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "app" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    (backend / "app" / "foo").mkdir()
    (backend / "app" / "foo" / "__init__.py").write_text(
        "import sys\nsys.path.insert(0, 'x')\n", encoding="utf-8"
    )
    (backend / "conftest.py").write_text("import app.foo\n", encoding="utf-8")

    mapping = preCollectionImportPaths(backend)
    derived = {path for files in mapping.values() for path in files}

    assert "backend/app/foo.py" in derived, f".py 叶子丢失：{sorted(derived)}"
    assert "backend/app/foo/__init__.py" in derived, (
        f"包目录叶子丢失 —— 双形态退回了二选一（H-5 回归）：{sorted(derived)}"
    )


def test_dynamic_all_raises_fail_closed(tmp_path: Path) -> None:
    """`__all__` 非静态字面量 ⇒ 派生必须抛错（第十五轮 M-1：动态星号展开 fail-open）。

    CPython 实测 `from X import *` 会连带 import `__all__` 里的子模块；静态展开
    判不了 ⇒ 宁可炸出来人工裁决，也不静默漏边。
    """
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "app" / "star.py").write_text(
        # ⚠️ 必须是 set 字面量（第十七轮 LOW-1）：ListComp 会让变异副本在
        # `.elts` 上 AttributeError（异常型转红 = 非独占判别）；ast.Set 有
        # .elts ⇒ 变异副本静默放行（干净的 fail-open），转红才锚定在
        # 「fail-closed 条件被移除」本身。
        "__all__ = {'x'}\n", encoding="utf-8"
    )
    (backend / "conftest.py").write_text("from app.star import *\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="__all__"):
        preCollectionImportPaths(backend)


# ---- 第二十二轮 L1：AST 规模预算 / 递归溢出 fail-closed ----


def test_ast_node_budget_fail_closed(tmp_path: Path) -> None:
    """超预算大文件的整文件 hook 行（M199 锚点：预算条件被删 ⇒ 红）。

    预算线的判别点：节点数超 `_MAX_AST_NODES` ⇒ 返回整文件 fail-closed 行，
    而不是部分分析（部分分析 = 大文件里藏的钩子逃出）。变异把条件关掉 ⇒
    大文件被正常分析（体是定义性的）⇒ 无 hook ⇒ 本用例红。
    """
    from app.utils.ci_conftest_chain import _MAX_AST_NODES

    # ~4 节点/行（Assign + Name + Store + Constant）⇒ 行数取预算 / 4 再放宽，
    # 确保**计数**超线而单测体积可控；内容全部是定义性赋值
    # （否则预算线之外的线也会红）。
    lineCount = _MAX_AST_NODES // 4 + 1000
    body = "".join(f"_v{i} = {i}\n" for i in range(lineCount))
    path = tmp_path / "big.py"
    path.write_text(body, encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks, "超预算大文件没有返回 fail-closed hook 行 ⇒ 预算线失效"
    assert "fail-closed" in hooks[0]


def test_deep_nesting_recursion_fail_closed(tmp_path: Path) -> None:
    """深嵌套 AST 的 RecursionError 转 hook 行（M200 锚点：包装被删 ⇒ 红）。

    深属性链（5000 段）解析没问题，但递归遍历助手（_iterExecNodes/
    _resolveMember）会溢出；包装层把异常转成显式 hook 行。
    变异把 `return _findImportTimeHooksImpl(path)` 改成 `return ()` ⇒
    RecursionError 直接炸穿 ⇒ 本用例以异常态失败（计入转红）。
    （缩进嵌套不可用：CPython 硬限 100 层缩进，Parse 阶段就 IndentationError；
    括号嵌套同样在 Parse 阶段被 SyntaxError 拦下——都到不了检测器的递归。）
    """
    chain = "a" + ".b" * 5000
    body = f"_r = {chain}.m()\n"
    path = tmp_path / "deep.py"
    path.write_text(body, encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks, "深嵌套文件没有返回 fail-closed hook 行 ⇒ 包装层失效"
    assert "fail-closed" in hooks[0]


# ============================================================================
# 第二十三轮评审修复（第二十四轮）：C-4 sink 赋值形态 / C-5·C-6 模块根属性
# 再绑定 / H-1 被调形态 fail-closed / H-2 装饰器 fail-closed。每组 deny 配
# 一条 clean 反向锚（真实闭包校准通过的同形态），防止 deny 加宽变 vacuous。
# ============================================================================

_EVIL_R24_SOURCES: dict[str, str] = {
    # C-4：拒绝根成员**赋值形态**调用（此前只有裸表达式/裸调用形态被拦）。
    # 判别线 = _DENIED_ROOTS 的 "os.chdir" 条目（chdir 不在 _DENIED_CALL_ATTRS
    # ⇒ 本 fixture 是该条的独占判别；remove/unlink 等有 call-attrs 兜底，
    # 不能当根条目的判别）。
    "os_chdir_assign": (
        "import os\n"
        "_r = os.chdir('/tmp')\n"
    ),
    "os_remove_assign": (
        "import os\n"
        "_r = os.remove('/tmp/whatever')\n"
    ),
    # C-4：动态根（Call 接收者）上的 sink 成员
    "path_write_text": (
        "from pathlib import Path\n"
        "_r = Path('/tmp/whatever').write_text('boom')\n"
    ),
    # C-4：unknown 接收者（外部构造赋出的名字）上的 sink 成员
    # （_DENIED_CALL_ATTRS 臂）。⚠️ 接收者必须落 unknown：Call 接收者
    # （`get_handler().unlink()`）会先撞上「动态接收者 fail-closed」既有
    # 线 ⇒ 本线拿不到独占判别（第二十四轮探针实证）。
    "unknown_recv_sink": (
        "from pathlib import Path\n"
        "_p = Path('/tmp/whatever')\n"
        "_r = _p.unlink()\n"
    ),
    # C-5：模块根属性再绑定（别名解析出的根 ⇒ 接口可被换假）
    "module_root_attr_rebind": (
        "import logging\n"
        "logging.getLogger = lambda name: None\n"
    ),
    # C-5：sys.stdout 再绑定（拒绝根成员 Store）
    "sys_stdout_rebind": (
        "import sys\n"
        "sys.stdout = open('/tmp/whatever', 'w')\n"
    ),
    # H-1：被扫函数体内、被调形态静态不可判（IfExp 作被调）。
    # 宿主函数必须真的被扫（模块级 `_r = entry()` 进入 callTargets 扫描）
    # —— 函数体不可达 ⇒ dispatch 根本不经过 ⇒ fixture 假绿。
    "fn_callee_ifexp": (
        "import sys\n"
        "def pick():\n"
        "    return None\n"
        "def entry():\n"
        "    _r = (pick if True else None)()\n"
        "_r = entry()\n"
    ),
    # H-2：from-import 的项目代码作裸名装饰器（别名解析到 app. 根）
    "cross_file_project_decorator": (
        "from app.evil import hook\n"
        "@hook\n"
        "def f():\n"
        "    pass\n"
    ),
    # H-2：未 import、非内置白名单的裸名装饰器（静态不可判）
    "unresolvable_bare_decorator": (
        "@ghost\n"
        "def f():\n"
        "    pass\n"
    ),
    # H-2：项目模块属性装饰器（Call/Attribute 形态解析到 app. 根）
    "project_attr_decorator": (
        "import app.mod\n"
        "@app.mod.hook\n"
        "def f():\n"
        "    pass\n"
    ),
}

_CLEAN_R24_SOURCES: dict[str, str] = {
    # 第二十四轮校准实证的真实形态：嵌套 Call 作被调（pydantic 注册式）。
    # ⚠️ 第二十五轮评审 H-24-1 后收紧：内层被调是**本地 def** ⇒ 返回的可执行
    # 目标不可证 ⇒ 拒（evil fixture `nested_call_local_factory` 钉住）；clean
    # 形态必须是「内层被调解析到**外部 import**」（真实闭包的 field_validator
    # 就来自 pydantic）⇒ 放行。静态分析 fixture，不真 import pydantic。
    "nested_call_callee": (
        "from pydantic import field_validator\n"
        "def _v(v):\n"
        "    return v\n"
        "class M:\n"
        "    _check = field_validator('x')(_v)\n"
    ),
    # H-2 白名单：内置装饰器三件套
    "builtin_decorators": (
        "class C:\n"
        "    @staticmethod\n"
        "    def s():\n"
        "        return 1\n"
        "    @classmethod\n"
        "    def c(cls):\n"
        "        return 2\n"
        "    @property\n"
        "    def p(self):\n"
        "        return 3\n"
    ),
    # H-2：from-import 的外部模块装饰器（别名解析为外部根 ⇒ 放行）
    "from_import_external_decorator": (
        "from functools import lru_cache\n"
        "@lru_cache(maxsize=2)\n"
        "def f():\n"
        "    return 1\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_EVIL_R24_SOURCES))
def test_round24_deny_rules_are_enforced(tmp_path: Path, case: str) -> None:
    """第二十四轮新 deny 线逐形态钉住（C-4/C-5/H-1/H-2）。

    每条线的变异判别点：对应 deny 臂被删/条件反转 ⇒ 该 fixture 放行 ⇒ 红。
    """
    path = tmp_path / "m.py"
    path.write_text(_EVIL_R24_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    # 只断言非空：各 deny 线报文格式不一（拒绝根线是「顶层引用 X」、
    # fail-closed 臂另有措辞）。⚠️ 本批 fixture **不**逐条承担变异判别：
    # 断言比 round-25 批弱（round-25 的精识臂用文本归位断言，见
    # test_round25_deny_rules_are_enforced）；os_remove_assign /
    # sys_stdout_rebind 与拒绝根线是双重覆盖。防 vacuous 仍由
    # M204–M215 变异取证承担（判别点落在各自精识臂的锚上）。
    assert hooks, f"{case} 没被认出 ⇒ 对应 deny 线失效"


@pytest.mark.parametrize("case", sorted(_CLEAN_R24_SOURCES))
def test_round24_clean_forms_pass(tmp_path: Path, case: str) -> None:
    """第二十四轮 deny 加宽的反向锚：真实闭包里的合法形态必须放行。

    deny 臂加宽到误伤这些形态 ⇒ 校准/本用例红（防 vacuous 反向）。
    """
    path = tmp_path / "m.py"
    path.write_text(_CLEAN_R24_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks == (), f"{case} 被误报：{hooks}"


def test_analyzer_primitives_not_swapped_in_process() -> None:
    """C-6 腰带锚：守卫自己的解析原语未被进程内换假。

    C-6 场景（payload 先执行再换掉 ast.parse/Path.read_text 让分析器失明）
    的上游逃逸已被 C-4 deny 扩展关闭；本条是独立于扫描逻辑的完整性断言
    —— 原语被换 ⇒ 本测试直接红，不需要经过扫描。
    """
    import ast
    from pathlib import Path

    assert ast.parse.__module__ == "ast"
    assert Path.read_text.__module__ == "pathlib"
    assert ast.walk.__module__ == "ast"


def test_analyzer_primitives_not_swapped_in_fresh_interpreter() -> None:
    """第二十五轮评审 C-24-2：in-process 腰带可被伪造（换假实现 + 改
    `__module__`）⇒ 信任锚必须在**全新解释器**里重验——payload 没跑过、
    `__module__` 欺骗无意义。sitecustomize / conftest 级别的持久换假仍会
    在子进程里现形（那正是要抓的）。

    第二十六轮评审 M-26-3 加固：子进程此前只验原始三件套，**没有 import
    分析器、没有重跑守卫判定** ⇒ in-process 换假（同名函数提前进 `sys.modules`
    / 改 `path.read_text` 返回干净源码）两道都过。现在子进程里真实 import
    分析器并对合成 evil 源码跑 `findImportTimeHooks`，断言拒绝文本归位。
    """
    import subprocess
    import sys

    backendRoot = str(Path(__file__).resolve().parents[3])
    probe = (
        "import pathlib, sys, tempfile\n"
        f"sys.path.insert(0, {backendRoot!r})\n"
        "from app.utils.ci_conftest_chain import findImportTimeHooks\n"
        "src = (\n"
        "    'import sys\\n'\n"
        "    \"sys.path.insert(0, 'evil')\\n\"\n"
        ")\n"
        "tmp = pathlib.Path(tempfile.mkdtemp())\n"
        "evil = tmp / 'evil_mod.py'\n"
        "evil.write_text(src, encoding='utf-8')\n"
        "hooks = findImportTimeHooks(evil)\n"
        "assert hooks, 'fresh-interpreter analyzer flagged nothing ⇒ 换假生效'\n"
        "assert any('sys.path' in h for h in hooks), hooks\n"
        "print('BELT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程失败：{proc.stderr}"
    assert "BELT_OK" in proc.stdout, proc.stdout

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ast, pathlib, sys; "
            "print(ast.parse.__module__, pathlib.Path.read_text.__module__, "
            "ast.walk.__module__)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"子进程失败：{proc.stderr}"
    assert proc.stdout.split() == ["ast", "pathlib", "ast"], proc.stdout


# ============================================================================
# 第二十五轮评审修复（C-24-1/C-24-2/H-24-1/M-24-1）的判别 fixtures。
# 与 round-24 批的区别：evil 用例断言**拒绝文本归位**（只断言非空时，
# 兜底 fail-closed 线会用不同文本接住 ⇒ 精识 deny 臂的变异拿不到判别）。
# ============================================================================

_EVIL_R25_SOURCES: dict[str, str] = {
    # C-24-1：装饰器为动态表达式（IfExp）——预扫白名单收窄（Name/Attribute/
    # Call 之外全拒）的判别形态。变异 = 预扫回退到旧元组（Lambda/Subscript/
    # Starred）⇒ IfExp 逃出预扫、scanExec 装饰器臂也不认（非 Call/Attribute/
    # Name）⇒ 完全放行。
    "decorator_ifexp": (
        "import sys\n"
        "def _d(f):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return f\n"
        "@(_d if True else None)\n"
        "def g():\n"
        "    pass\n"
    ),
    # C-24-1：本地 def 工厂装饰器（`@_factory()`）——返回的嵌套闭包体在装饰
    # 期真实执行且不可静态判定。变异 = 本地工厂臂死 ⇒ 落到「裸名静态不可判」
    # 兜底（仍拒但**文本不同**）⇒ 文本归位断言才拿得到判别。
    "decorator_local_factory": (
        "import sys\n"
        "def _factory():\n"
        "    def _deco(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _deco\n"
        "@_factory()\n"
        "def g():\n"
        "    pass\n"
    ),
    # C-24-1：lambda 工厂装饰器。变异 = lambda 臂死 ⇒ 落「形态静态不可判」
    # 兜底 ⇒ 文本归位。
    "decorator_lambda_factory": (
        "@(lambda f: f)(None)\n"
        "def g():\n"
        "    pass\n"
    ),
    # H-24-1：嵌套调用工厂的内层被调是本地 def（`pick(sys)('/tmp')`，pick
    # 返回接收者）⇒ 返回的可执行目标不可证。变异 = isProvenRegistration 臂
    # 死 ⇒ 无任何 deny（pick 体干净、返回值绑定不透明但未被使用）⇒ 放行。
    # ⚠️ 故意不写 `_f = pick(sys); _f('/tmp/x')` 的后续调用——那会撞上
    # 「不透明名被调用」既有线，判别不独占。
    "nested_call_local_factory": (
        "import sys\n"
        "def pick(x):\n"
        "    return x\n"
        "_r = pick(sys)('/tmp')\n"
    ),
    # C-24-2：Store 基底非静态链（Subscript 基底）⇒ fail-closed。
    # ⚠️ 用 `.foo`（不在 _REBIND_DENIED_ATTRS）⇒ 非名字/属性链 deny 臂是
    # 唯一判别线。
    "store_subscript_base": (
        "import sys\n"
        "[sys][0].foo = None\n"
    ),
    # C-24-2：函数作用域 import 的模块根再绑定——effAliases 单点（模块级
    # aliases 看不到 fn 内 import）的判别形态。变异 = 该行 effAliases 退回
    # aliases ⇒ fn 作用域 import 逃出 ⇒ 完全放行。
    "store_fn_scope_import": (
        "def _m():\n"
        "    import logging\n"
        "    logging.getLogger = lambda name: None\n"
        "_r = _m()\n"
    ),
    # H-24-1：func 为 lambda 的调用结果绑定 ⇒ 不透明 ⇒ 被调用时拒。变异 =
    # applyBinding 的 else 臂死 ⇒ _f 不记不透明 ⇒ unknown 放行语义放行
    # （unknown 名被调不拒）⇒ 完全放行。
    "opaque_lambda_result": (
        "class C:\n"
        "    def remove(self):\n"
        "        pass\n"
        "_f = (lambda a: a.remove)(C)\n"
        "_r = _f('/tmp/x')\n"
    ),
}

# 文本归位断言（None = 只断言 hooks 非空——判别点是**放行性**变异，
# 基线文本无关紧要，变异后 hooks 变空即红）。
_EVIL_R25_EXPECTED_TEXT: dict[str, str | None] = {
    "decorator_ifexp": "装饰器为动态表达式",
    "decorator_local_factory": "装饰器工厂是本地函数",
    "decorator_lambda_factory": "装饰器工厂为 lambda",
    "nested_call_local_factory": "嵌套调用工厂",
    "store_subscript_base": "属性赋值基底静态不可判",
    "store_fn_scope_import": "模块根属性再绑定",
    "opaque_lambda_result": None,
}

_CLEAN_R25_SOURCES: dict[str, str] = {
    # M-24-1：字面量接收者上的 sink 成员名是良性形态（`'a'.replace(...)`）
    # ⇒ 豁免。变异 = 豁免条件死 ⇒ 本 fixture 被拒 ⇒ clean 用例红。
    "constant_replace": (
        "_r = 'a'.replace('a', 'b')\n"
    ),
    # H-24-1 的反向锚：内层被调解析到**外部 import**（pydantic 注册式
    # 惯用法，真实闭包 11 处互异行）⇒ 注册式豁免放行。
    # ⚠️ 第二十六轮评审 H-26-1 后 `functools.partial(int)('2')` **不再是**
    # 合法形态——通用可调用工厂能把任意 sink 绑进返回值 ⇒ 拒（evil fixture
    # `nested_call_partial_factory` 钉住），反向锚换成 pydantic 注册式。
    "nested_call_external": (
        "from pydantic import field_validator\n"
        "def _v(v):\n"
        "    return v\n"
        "_r = field_validator('x')(_v)\n"
    ),
    # C-24-1 的反向锚：外部模块属性工厂装饰器（functools.lru_cache）
    # ⇒ 解析到外部根 ⇒ 放行。
    "call_decorator_external": (
        "import functools\n"
        "@functools.lru_cache(maxsize=1)\n"
        "def g():\n"
        "    return 1\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_EVIL_R25_SOURCES))
def test_round25_deny_rules_are_enforced(tmp_path: Path, case: str) -> None:
    """第二十五轮新 deny 线逐形态钉住（C-24-1/C-24-2/H-24-1）。

    精识 deny 臂的变异判别点靠**文本归位**：臂死后兜底 fail-closed 线
    （措辞不同）接住 ⇒ 只断言非空会假绿。文本归位在 _EVIL_R25_EXPECTED_TEXT；
    放行性变异（基线文本无关、变异后 hooks 变空）的条目为 None。
    """
    path = tmp_path / "m.py"
    path.write_text(_EVIL_R25_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    expectedText = _EVIL_R25_EXPECTED_TEXT[case]
    if expectedText is None:
        assert hooks, f"{case} 没被认出 ⇒ 对应 deny 线失效"
    else:
        assert any(expectedText in hook for hook in hooks), (
            f"{case} 的拒绝文本没有归位到「{expectedText}」：{hooks}"
            " ⇒ 精识 deny 臂失效（兜底线接住了）"
        )


@pytest.mark.parametrize("case", sorted(_CLEAN_R25_SOURCES))
def test_round25_clean_forms_pass(tmp_path: Path, case: str) -> None:
    """第二十五轮 deny 加宽的反向锚：真实闭包里的合法形态必须放行。

    deny 臂加宽到误伤这些形态 ⇒ 校准/本用例红（防 vacuous 反向）。
    """
    path = tmp_path / "m.py"
    path.write_text(_CLEAN_R25_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks == (), f"{case} 被误报：{hooks}"


# ============================================================================
# 第二十六轮评审修复（H-26-1..H-26-5 + M-26-1/M-26-2）的判别 fixtures。
# 全部经探针实测：修复前 hooks == ()（逃逸实证），修复后拒绝文本归位。
# ============================================================================

_EVIL_R26_SOURCES: dict[str, str] = {
    # H-26-1：通用可调用工厂——内层被调解析到 functools（外部 import）但
    # 返回的是把任意可调用绑好的目标。变异 = 工厂条件死 ⇒ 放行（实参无
    # sink ⇒ sink 臂接不住）⇒ hooks 变空。
    "nested_call_partial_factory": (
        "import functools\n"
        "_r = functools.partial(int)('2')\n"
    ),
    # H-26-1：注册式豁免的实参里绑好 sink 成员引用（外层 Call 的实参子树，
    # 探针实证逃逸）。变异 = sink 臂死 ⇒ 落「内层被调」兜底（文本不同）。
    "nested_call_sink_arg": (
        "from pydantic import field_validator\n"
        "from pathlib import Path\n"
        "_r = field_validator('x')(Path('y').unlink)\n"
    ),
    # H-26-2：被扫函数体内的嵌套 def 装饰器为动态表达式（IfExp）——模块级
    # 预扫只管模块级 ⇒ 此前整段逃逸。变异 = 函数体臂死 ⇒ 完全放行
    #（_decoratorMember 对 IfExp 不报歧义，探针实证）。
    "fn_scope_dynamic_decorator": (
        "import sys\n"
        "def _mk():\n"
        "    def _d(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _d\n"
        "def _o():\n"
        "    @(_mk() if True else None)\n"
        "    def g():\n"
        "        pass\n"
        "_r = _o()\n"
    ),
    # H-26-3：调用方作用域别名不进被调 fn-local 类方法的作用域映射 ⇒
    # `flakes.hook = None`（fn 作用域 import 的项目接口换假）逃逸。变异 =
    # 作用域映射退回旧形态（不带 enclosingAliases）⇒ hooks 变空。
    "two_level_fn_scope_import": (
        "def _o():\n"
        "    from app.utils import flakes\n"
        "    class _C:\n"
        "        def __init__(self):\n"
        "            flakes.hook = None\n"
        "    _C()\n"
        "_r = _o()\n"
    ),
    # H-26-4：fn 作用域的项目 import 遮蔽模块级外部同名别名——裸名装饰器
    # 按模块级 aliases 解析 ⇒ 解析到 functools 放行（装饰期真实执行的是
    # 项目 hook）。变异 = 解析退回 aliases ⇒ hooks 变空。
    "fn_scope_project_decorator_shadow": (
        "from functools import lru_cache as dec\n"
        "def _o():\n"
        "    from app.utils.flakes import hook as dec\n"
        "    @dec\n"
        "    def g():\n"
        "        pass\n"
        "    return g\n"
        "_r = _o()\n"
    ),
    # M-26-1：相对导入只可能指向本仓库 ⇒ 按项目代码拒绝（此前 _aliasMap
    # 静默跳过 ⇒ 根当外部放行）。变异 = __relative__ 出项目根元组 ⇒ 放行。
    "relative_import_project_decorator": (
        "from . import models\n"
        "@models.deco\n"
        "def g():\n"
        "    pass\n"
    ),
    # M-26-2：四个此前无 fixture/无变异的装饰器 deny 臂。文本归位是唯一
    # 判别（call_result/subscript_factory 有兜底线同拦，见变异注释）。
    "decorator_call_result": (
        "import sys\n"
        "def _mk():\n"
        "    def _d(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _d\n"
        "@(_mk())(None)\n"
        "def g():\n"
        "    pass\n"
    ),
    "decorator_subscript_factory": (
        "import sys\n"
        "def _d(f):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return f\n"
        "_arr = [_d]\n"
        "@_arr[0](None)\n"
        "def g():\n"
        "    pass\n"
    ),
    "decorator_attr_base": (
        "import sys\n"
        "def _mk():\n"
        "    def _d(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _d\n"
        "@_mk().hook\n"
        "def g():\n"
        "    pass\n"
    ),
    "decorator_unknown_bare_factory": (
        "@unknown_factory(None)\n"
        "def g():\n"
        "    pass\n"
    ),
}

# 文本归位断言（与 round-25 同规矩：精识 deny 臂死后兜底线会用不同文本
# 接住 ⇒ 只断言非空假绿）。
_EVIL_R26_EXPECTED_TEXT: dict[str, str] = {
    "nested_call_partial_factory": "嵌套调用工厂的内层被调",
    "nested_call_sink_arg": "嵌套调用工厂实参携带 sink 成员",
    "fn_scope_dynamic_decorator": "装饰器为动态表达式",
    "two_level_fn_scope_import": "模块根属性再绑定",
    "fn_scope_project_decorator_shadow": "解析到项目/禁止代码",
    "relative_import_project_decorator": "解析到项目/禁止代码",
    "decorator_call_result": "装饰器工厂本身是调用结果",
    "decorator_subscript_factory": "装饰器工厂形态静态不可判",
    "decorator_attr_base": "装饰器基底静态不可判",
    "decorator_unknown_bare_factory": "装饰器工厂裸名静态不可判",
}


@pytest.mark.parametrize("case", sorted(_EVIL_R26_SOURCES))
def test_round26_deny_rules_are_enforced(tmp_path: Path, case: str) -> None:
    """第二十六轮评审修复的 deny 线逐形态钉住（全部探针实测逃逸后收口）。"""
    path = tmp_path / "m.py"
    path.write_text(_EVIL_R26_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    expectedText = _EVIL_R26_EXPECTED_TEXT[case]
    assert any(expectedText in hook for hook in hooks), (
        f"{case} 的拒绝文本没有归位到「{expectedText}」：{hooks}"
        " ⇒ 精识 deny 臂失效（兜底线接住了）"
    )


# ---------- 第二十七轮评审修复批：R28 攻击构造（探针先行，修复前实测全部逃逸） ----------
_EVIL_R28_SOURCES: dict[str, str] = {
    # H-27-1：sink 在外层工厂被调的 func 子树（`field_validator(Path('y')
    # .write_text)(…)` 的 write_text 在 callNode.func 的实参里）
    "nested_call_sink_in_factory_args": (
        "from pydantic import field_validator\n"
        "from pathlib import Path\n"
        "_r = field_validator(Path('y').write_text)(lambda v: v)\n"
    ),
    # H-27-2：分支体内的嵌套 def 动态装饰器（平扫顶层看不见）
    "fn_scope_dynamic_decorator_nested_block": (
        "import sys\n"
        "def _mk():\n"
        "    def _d(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _d\n"
        "def _o():\n"
        "    if True:\n"
        "        @(_mk() if True else None)\n"
        "        def g():\n"
        "            pass\n"
        "_r = _o()\n"
    ),
    # H-27-2：类体内的嵌套 def 动态装饰器
    "fn_scope_dynamic_decorator_class_body": (
        "import sys\n"
        "def _mk():\n"
        "    def _d(f):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return f\n"
        "    return _d\n"
        "def _o():\n"
        "    class _C:\n"
        "        @(_mk() if True else None)\n"
        "        def m(self):\n"
        "            pass\n"
        "    return _C\n"
        "_r = _o()\n"
    ),
    # H-27-3：IIFE lambda 丢调用方作用域别名（fn 作用域 import 再绑定）
    "iife_fn_scope_import": (
        "def _o():\n"
        "    import sys as s\n"
        "    _r = (lambda: s.path.insert(0, 'x'))()\n"
        "    return _r\n"
        "_r = _o()\n"
    ),
    # H-27-3：IIFE lambda 丢调用方本地类（fn-local 类 __init__ 携 sink）
    "iife_local_class": (
        "import sys\n"
        "def _o():\n"
        "    class _C:\n"
        "        def __init__(self):\n"
        "            sys.path.insert(0, 'x')\n"
        "    _r = (lambda: _C())()\n"
        "    return _r\n"
        "_r = _o()\n"
    ),
    # M-27-5：itemgetter 工厂（import 期执行 _c.__getitem__，可携 sink）
    "nested_call_itemgetter_factory": (
        "import operator, sys\n"
        "class _C:\n"
        "    def __getitem__(self, k):\n"
        "        sys.path.insert(0, 'x')\n"
        "        return k\n"
        "_c = _C()\n"
        "_r = operator.itemgetter(0)(_c)\n"
    ),
}

# 文本归位：每条攻击必须由**本轮新开的精识 deny 臂**接住（兜底线接住 =
# 臂失效），断言具体消息子串。
_EVIL_R28_EXPECTED_TEXT: dict[str, str] = {
    "nested_call_sink_in_factory_args": "嵌套调用工厂实参携带 sink 成员",
    "fn_scope_dynamic_decorator_nested_block": "装饰器为动态表达式",
    "fn_scope_dynamic_decorator_class_body": "装饰器为动态表达式",
    "iife_fn_scope_import": "顶层引用 sys.path.insert",
    "iife_local_class": "顶层引用 sys.path.insert",
    "nested_call_itemgetter_factory": "嵌套调用工厂的内层被调",
}


@pytest.mark.parametrize("case", sorted(_EVIL_R28_SOURCES))
def test_round28_deny_rules_are_enforced(tmp_path: Path, case: str) -> None:
    """第二十七轮评审修复的 deny 线逐形态钉住（全部探针实测逃逸后收口）。"""
    path = tmp_path / "m.py"
    path.write_text(_EVIL_R28_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    expectedText = _EVIL_R28_EXPECTED_TEXT[case]
    assert any(expectedText in hook for hook in hooks), (
        f"{case} 的拒绝文本没有归位到「{expectedText}」：{hooks}"
        " ⇒ 精识 deny 臂失效（兜底线接住了）"
    )


# ---------- 第三十轮评审修复批：R30 攻击构造（探针先行，修复前实测全部逃逸） ----------
_EVIL_R30_SOURCES: dict[str, str] = {
    # H-29-1：外部执行器 list(map(本地def)) —— import 期执行 _f
    "executor_map_list_local_def": (
        "import sys\n"
        "def _f(_):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return _\n"
        "_r = list(map(_f, [0]))\n"
    ),
    # H-29-1：cast 返回可执行目标再调用（内层 cast 调用走 Name 臂拒绝）
    "executor_cast_call": (
        "import sys\n"
        "from typing import cast\n"
        "def _f(_):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return _\n"
        "_r = cast(int, _f)()\n"
    ),
    # H-29-1：lambda 实参
    "executor_map_lambda": (
        "import sys\n"
        "_r = list(map(lambda _: sys.path.insert(0, 'x'), [0]))\n"
    ),
    # H-29-1：Attribute 执行器 functools.reduce（未知接收者臂）
    "executor_functools_reduce": (
        "import sys\n"
        "import functools\n"
        "def _f(a, b):\n"
        "    sys.path.insert(0, 'x')\n"
        "    return a\n"
        "_r = functools.reduce(_f, [0])\n"
    ),
    # H-29-2：模块级 def 经 callTargets，调用方 fn 作用域别名遮蔽
    "scope_mask_module_def": (
        "import sys as s\n"
        "def _g():\n"
        "    s.path.insert(0, 'x')\n"
        "def _o():\n"
        "    import json as s\n"
        "    _g()\n"
        "_r = _o()\n"
    ),
    # H-29-2：模块级类实例化（localClasses 落点）被调用方遮蔽
    "scope_mask_module_class": (
        "import sys as s\n"
        "class _Mc:\n"
        "    def __init__(self):\n"
        "        s.path.insert(0, 'x')\n"
        "def _o():\n"
        "    import json as s\n"
        "    _Mc()\n"
        "_r = _o()\n"
    ),
    # H-29-2：类别名实例化（classAliases 落点）被调用方遮蔽
    "scope_mask_class_alias": (
        "import sys as s\n"
        "class _Mc:\n"
        "    def __init__(self):\n"
        "        s.path.insert(0, 'x')\n"
        "_c = _Mc\n"
        "def _o():\n"
        "    import json as s\n"
        "    _c()\n"
        "_r = _o()\n"
    ),
    # H-29-3：fn-local 类成员作装饰器（_decoratorMember 只查模块表）
    "fn_local_class_decorator": (
        "import sys\n"
        "def _o():\n"
        "    class _C:\n"
        "        @staticmethod\n"
        "        def deco(fn):\n"
        "            sys.path.insert(0, 'x')\n"
        "            return fn\n"
        "    @_C.deco\n"
        "    def g():\n"
        "        pass\n"
        "    return g\n"
        "_r = _o()\n"
    ),
    # M-29-5：sink 别名洗白（污染名经赋值绑进工厂实参）
    "sink_alias_launder": (
        "from pydantic import field_validator\n"
        "from pathlib import Path\n"
        "def _v(v):\n"
        "    return v\n"
        "_w = Path('y').write_text\n"
        "_r = field_validator('x')(_w)\n"
    ),
}

# 文本归位：executor 四形态必须由 H-29-1 的精识臂接住；scope-mask 三形态 +
# fn-local 装饰器由被调扫描接住；sink 洗白必须由 M-29-5 的精识臂接住
# （同调用的「内层被调」兜底文本不能当判别点——那是恒红的兜底线）。
_EVIL_R30_EXPECTED_TEXT: dict[str, str] = {
    "executor_map_list_local_def": "调用实参携带本地可调用",
    "executor_cast_call": "调用实参携带本地可调用",
    "executor_map_lambda": "调用实参携带本地可调用",
    "executor_functools_reduce": "调用实参携带本地可调用",
    "scope_mask_module_def": "顶层引用 sys.path.insert",
    "scope_mask_module_class": "顶层引用 sys.path.insert",
    "scope_mask_class_alias": "顶层引用 sys.path.insert",
    "fn_local_class_decorator": "顶层引用 sys.path.insert",
    "sink_alias_launder": "嵌套调用工厂实参引用 sink 绑定名",
}

# 放行性反向锚：真实闭包普查（2026-11-09，63 处/5 链）钉住的五条注册惯例，
# 新 deny 臂引入后必须仍然 0 hooks（误报 = 校准即炸）。
_CLEAN_R30_SOURCES: dict[str, str] = {
    "clean_pydantic_registration": (
        "from pydantic import field_validator\n"
        "def _v(v):\n"
        "    return v\n"
        "_r = field_validator('x')(_v)\n"
    ),
    "clean_mapped_column": (
        "from sqlalchemy.orm import mapped_column\n"
        "from datetime import datetime\n"
        "def _utcnow():\n"
        "    return datetime.now()\n"
        "class _M:\n"
        "    created = mapped_column(_utcnow)\n"
    ),
    "clean_field_default_factory_lambda": (
        "from pydantic import Field\n"
        "class _M:\n"
        "    x = Field(default_factory=lambda: 1)\n"
    ),
    "clean_depends": (
        "from fastapi import Depends\n"
        "def getCurrentUser():\n"
        "    return 1\n"
        "_d = Depends(getCurrentUser)\n"
    ),
    "clean_before_validator": (
        "from pydantic import BeforeValidator\n"
        "def _v(x):\n"
        "    return x\n"
        "_t = BeforeValidator(_v)\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_EVIL_R30_SOURCES))
def test_round30_deny_rules_are_enforced(tmp_path: Path, case: str) -> None:
    """第三十轮评审修复的 deny 线逐形态钉住（全部探针实测逃逸后收口）。"""
    path = tmp_path / "m.py"
    path.write_text(_EVIL_R30_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    expectedText = _EVIL_R30_EXPECTED_TEXT[case]
    assert any(expectedText in hook for hook in hooks), (
        f"{case} 的拒绝文本没有归位到「{expectedText}」：{hooks}"
        " ⇒ 精识 deny 臂失效（兜底线接住了）"
    )


@pytest.mark.parametrize("case", sorted(_CLEAN_R30_SOURCES))
def test_round30_inert_callable_registration_forms_pass(tmp_path: Path, case: str) -> None:
    """H-29-1 白名单的五条注册惯例必须仍然放行（新 deny 臂零误报）。"""
    path = tmp_path / "m.py"
    path.write_text(_CLEAN_R30_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks == (), f"{case} 被误报：{hooks}"
