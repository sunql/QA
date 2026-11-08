"""CI 判据守卫的**变异清单**（纯数据）—— 第十八轮收口批（M137–M151）。

第十七轮复审 5H + 1L 的修复（`ci_conftest_chain.py`：统一 `_isForbidden` 禁止
dunder 段 / `__builtins__` 根、语句序 `callTargets`、本地类成员解析与接收者感知
白名单、实例化/with/for/就地运算/装饰器的 import 期执行路径、PEP 562 全形态）
各钉一条变异；判别点一律取**独占落点**（fixture 里不能有与钉住的检测路径无关
的违规——第十六轮 `...` 抢判别教训）。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r18`）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"

R18_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # ---------- M137–M141：HIGH-1（dunder 通道）+ HIGH-3（被调解析） ----------
    # H-1：Name(Load) 的拒绝判定走统一 `_isForbidden`；判定行被关
    # ⇒ from-import 的 dunder 符号（`from sys import __getattribute__`）逃出。
    (
        "M137",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            return target if _isForbidden(target) else None",
        "            return None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[getattr_via_from_import]"),
        1,
    ),
    # H-1：`__builtins__` 拒绝根（裸名下标 `__builtins__['exec']`）；根条目被删
    # ⇒ builtins_subscript fixture 红。
    (
        "M138",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    "__builtins__",',
        '    "",',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[builtins_subscript]"),
        1,
    ),
    # H-3：名字别名传播（`_f = m` 后 `_f()`）依赖 applyBinding 的
    # Name-value 绑定；绑定行被关 ⇒ called_def_alias fixture 红。
    (
        "M139",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            bound = callTargets.get(value.id)",
        "            bound = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[called_def_alias]"),
        1,
    ),
    # H-4：接收者感知白名单——本地类同名成员体扫描；成员解析行被关
    # ⇒ 本地类重定义 model_rebuild 逃出。（第二十轮重锚：_receiverMember 并入
    # _resolveMember，成员缺失 + model_rebuild 豁免 ⇒ 转红判别仍独占。）
    (
        "M140",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '        return _classMember(cls, func.attr, callTargets), "local"',
        '        return None, "local"',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[model_rebuild_local_override]"),
        1,
    ),
    # H-2：类实例化跑 `__init__`（`_x = X()`）；实例化钩子循环被关
    # ⇒ class_instantiation_init fixture 红。（第二十轮重锚：循环改多行。）
    (
        "M141",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[node.func.id], callTargets\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1)",
        "                            pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_instantiation_init]"),
        1,
    ),
    # ---------- M142–M148：HIGH-2（import 期执行路径） ----------
    # H-2：类创建钩子的赋值/注解形态（`__init_subclass__ = classmethod(...)`）；
    # 检查被关 ⇒ init_subclass_assign fixture 红。
    (
        "M142",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '            if any(getattr(t, "id", None) in _CLASS_CREATION_HOOK_NAMES for t in targets):',
        "            if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[init_subclass_assign]"),
        1,
    ),
    # H-2：with 作用于本地类实例（`__enter__`/`__exit__` 隐式 dunder）；
    # context_expr 检查被关 ⇒ with_local_instance fixture 红。
    (
        "M143",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            _usesLocalInstance(item.context_expr, localClasses, instanceVars)",
        "            False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[with_local_instance]"),
        1,
    ),
    # H-2：就地运算符（`_x += 1` ⇒ `__iadd__`）；整个 AugAssign 臂被关
    # ⇒ augassign_local_instance fixture 红。
    (
        "M148",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    elif isinstance(stmt, ast.AugAssign):",
        "    elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[augassign_local_instance]"),
        1,
    ),
    # H-2：for 作用于本地类实例（`__iter__`/`__next__`）；iter 检查被关
    # ⇒ for_local_instance fixture 红。
    (
        "M149",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if _usesLocalInstance(stmt.iter, localClasses, instanceVars):",
        "        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[for_local_instance]"),
        1,
    ),
    # H-2：模块级三参 `type()` 动态建类（dict 里的可调用体追不完 ⇒ fail-closed）；
    # 参数个数判别被关 ⇒ type_three_arg fixture 红。
    (
        "M144",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                and len(node.args) >= 3",
        "                and False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[type_three_arg]"),
        1,
    ),
    # H-3：名字别名传播的 **lambda 源**变体（`_g = _h`，_h 是模块级 lambda）；
    # 绑定行被关 ⇒ called_lambda_alias fixture 红。（与 M139 同锚不同 fixture
    # —— def 别名与 lambda 别名是两条独立判别点，M100/M101 先例。）
    # ⚠️ 原计划钉 `__set_name__` 的 setter 扫描行，solo 取证暴露该 fixture
    # 无独占落点：本地类定义 `__set_name__` 本身就被类创建钩子 def 形态检查
    # 抓住（`__set_name__` ∈ _CLASS_CREATION_HOOK_NAMES）⇒ setter 扫描分支
    # 对本地类永不可能先于 def 形态触发（纯纵深防御，无独立判别点）。
    (
        "M147",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            bound = callTargets.get(value.id)",
        "            bound = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[called_lambda_alias]"),
        1,
    ),
    # H-2：嵌套类实例化（`Outer.Inner()`）；成员是嵌套类的分支被关
    # ⇒ nested_class_instantiation fixture 红。（第二十轮重锚：if → elif，
    # 8 空格版属于 _resolveMember 的 Attribute 链分支，不能混锚。）
    (
        "M146",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    elif isinstance(member, ast.ClassDef):",
        "                    elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[nested_class_instantiation]"),
        1,
    ),
    # H-2：装饰器经类成员执行方法体（`@D.deco` 装饰在 import 期运行）；
    # 成员解析被关 ⇒ decorator_class_member fixture 红。（第二十轮重锚：
    # 主循环预扫与 scanExec 两处共用 _decoratorMember 单点 ⇒ 锚 helper 的
    # 解析行，死一处=死两处。）
    (
        "M150",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        return _classMember(localClasses[dec.value.id], dec.attr, callTargets)",
        "        return None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[decorator_class_member]"),
        1,
    ),
    # ---------- M145/M151：HIGH-5 + 语句序 ----------
    # H-5：PEP 562 模块级 `__getattr__`/`__dir__` 的赋值/注解形态
    # （pytest 自身收集即触发）；检查被关 ⇒ module_getattr_assign fixture 红。
    # （第二十轮重锚：applyBinding 旧检查并入 _pep562BindingProblem 主循环
    # 接线，旧锚行已不存在。）
    (
        "M145",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            pep = _pep562BindingProblem(stmt)",
        "            pep = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[module_getattr_assign]"),
        1,
    ),
    # H-3：语句序——def 先注册进 callTargets 再扫描调用点（`def m: evil /
    # _x = m() / def m: pass` 必须命中第一个 def）；主循环绑定行被关
    # ⇒ called_def_before_redef fixture 红。
    (
        "M151",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        applyBinding(stmt)",
        "        pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[called_def_before_redef]"),
        1,
    ),
]
