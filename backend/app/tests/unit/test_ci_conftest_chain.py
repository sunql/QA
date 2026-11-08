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
    # HIGH-1：from-import 的 dunder 符号（Name 位置经别名解析进禁止段检查）
    "getattr_via_from_import": (
        "from sys import __getattribute__\n_r = __getattribute__('path').insert(0, 'x')\n"
    ),
    # HIGH-1：裸名 __builtins__（__main__ 里它本身是 dict，只能当根拒）
    "builtins_subscript": "_b = __builtins__['exec']\n_r = _b('import sys')\n",
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
}


@pytest.mark.parametrize("case", sorted(_EVIL_POSITION_SOURCES))
def test_hook_positions_are_denied(tmp_path: Path, case: str) -> None:
    """每个 import 期求值位置都必须被根链拒绝/结构白名单抓住（M100/M101/M119 锚点）。"""
    path = tmp_path / "m.py"
    path.write_text(_EVIL_POSITION_SOURCES[case], encoding="utf-8")

    hooks = findImportTimeHooks(path)

    assert hooks, f"{case} 位置的 import 期钩子没被认出 —— 检测器有位置盲区"


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
