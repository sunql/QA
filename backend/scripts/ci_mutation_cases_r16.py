"""CI 判据守卫的**变异清单**（纯数据）—— 第十六轮收口批（M122–M136）。

第十五轮复审 5H + 2M 的修复（`ci_conftest_chain.py` 重写 `findImportTimeHooks`/
派生器双形态/动态 `__all__` fail-closed）各钉一条变异；判别点一律取**独占落点**
（同一行被多条修复共享时加长锚点，见 M95/M111/M112 第六轮重锚教训）。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r16`）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"

R16_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # ---------- M122–M134：第十五轮 5H + 2M 修复的逐条锚（H-1 → H-4 → M-1） ----------
    # H-1：vararg/kwarg 注解也是求值位置（首轮 `_iterExecNodes` 只扫
    # posonly/args/kwonly ⇒ `def f(*args: sys.path.insert(...))` 逃出）。
    (
        "M122",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            argNodes = (*argNodes, args.vararg)",
        "            pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[vararg_annotation]"),
        1,
    ),
    (
        "M123",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            argNodes = (*argNodes, args.kwarg)",
        "            pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[kwarg_annotation]"),
        1,
    ),
    # H-2：模块根级重绑定（`_t = sys` 后 `_t.path.insert`）要靠别名传播；
    # 传播行被关 ⇒ module_root_binding fixture 红。
    (
        "M124",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                aliases[stmt.targets[0].id] = source",
        "                pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[module_root_binding]"),
        1,
    ),
    # H-2：`__dict__` 下标（`sys.__dict__["modules"]`）经统一 `_isForbidden` 的
    # 禁止 dunder 段检查拒绝；段检查被关 ⇒ dict_subscript fixture 红。
    # （第十八轮重锚：旧 `__dict__` 短路行已被 `_isForbidden` 取代。）
    (
        "M125",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    return any(seg in _FORBIDDEN_DUNDER_SEGMENTS for seg in chain.split("."))',
        "    return False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[dict_subscript]"),
        1,
    ),
    # M-1：拒绝根模块的星号导入整个拒绝（动态 `__all__` fail-closed 的前提）。
    (
        "M126",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            if root in _DENIED_STAR_ROOTS:",
        "            if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[star_from_denied]"),
        1,
    ),
    # H-3：`**{...}` 展开（kw.arg is None ⇒ metaclass 静态不可判 ⇒ 一律拒）。
    (
        "M127",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if kw.arg is None:",
        "        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[metaclass_kwargs]"),
        1,
    ),
    # H-3：类体**任意嵌套深度**（含分支包裹）的钩子名定义扫描。
    (
        "M128",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    for node in _moduleScopeStatements(list(stmt.body)):",
        "    for node in ():",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[init_subclass_in_branch]"),
        1,
    ),
    # H-3：钩子名/允许方法名经属性再绑定（Store 位置在 _REBIND_DENIED_ATTRS）。
    (
        "M129",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if node.attr in _REBIND_DENIED_ATTRS:",
        "                if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[hook_attr_rebind]"),
        1,
    ),
    # H-3：基类是求值表达式（`__mro_entries__` 途径）。
    (
        "M130",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if isinstance(base, (ast.Call, ast.Starred)):",
        "        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[base_call]"),
        1,
    ),
    # H-3：拒绝根里的调用式改类通道。（第二十轮重锚：第十九轮把 `__setattr__`
    # 加进禁止 dunder 段集后，`type.__setattr__`/`object.__setattr__` 根条目被
    # 段集线 supersede ⇒ 纯纵深防御无独占判别点（solo 全量实评测 M131 净漏）；
    # 改钉 `setattr` 裸名根——不带 dunder 段、赋值形态绕开顶层表达式规则。）
    (
        "M131",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    "setattr",',
        '    "",',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[builtin_setattr_call]"),
        1,
    ),
    # H-4：被跳过的函数体经模块级调用执行（同模块 def 的一层被调解析）。
    # （第十八轮重锚：`defs` 全字典改为语句序 `callTargets`。）
    (
        "M132",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        callee = callTargets.get(node.func.id)",
        "                        callee = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[called_def]"),
        1,
    ),
    # H-4：IIFE lambda（`(lambda: sys.path.insert(...))()`）。（第二十轮重锚：
    # 该臂由 if 改 elif。）
    (
        "M133",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                elif isinstance(node.func, ast.Lambda):",
        "                elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[lambda_iife]"),
        1,
    ),
    # H-4：PEP 562 模块级 `__getattr__`/`__dir__`（收集前钩子）。
    (
        "M134",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if d.name in _MODULE_GETATTR_NAMES:",
        "        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[module_getattr]"),
        1,
    ),
    # ---------- M135–M136：派生器修复 ----------
    # H-5：双形态（`foo.py` + `foo/__init__.py`）**两个叶子都收**；改回二选一
    # （elif = 有 .py 就不看包目录）⇒ 双形态派生测试红。
    (
        "M135",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    if leafInit.is_file():",
        "    elif leafInit.is_file():",
        (_CHAIN_GUARD + "::test_dual_form_module_derives_both_leaves"),
        1,
    ),
    # M-1：动态 `__all__` 无法静态展开 ⇒ 抛错 fail-closed；条件被关 ⇒ 抛错测试红。
    (
        "M136",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if not isinstance(value, (ast.List, ast.Tuple)):",
        "        if False:",
        (_CHAIN_GUARD + "::test_dynamic_all_raises_fail_closed"),
        1,
    ),
]
