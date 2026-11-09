"""CI 判据守卫的**变异清单**（纯数据）—— 第三十四轮（终轮）评审修复批（M280–M308）。

第三十三轮复审 6 HIGH（H-33-1..H-33-6）的修复判别：

- H-33-1 实参通道（M280=容器下标元素抽取杀死；M281=星号展开杀死；
  M282=容器登记杀死；M283=不透明结果名杀死；M284–M287=fn 作用域
  模块 def/模块类别名/延迟绑定/不可判值名杀死；M288=重绑定 import 名杀死）
- H-33-2 本地被调别名（M289=fbModuleDefs 前置参数通道杀死；
  M290=fbModuleClassAliases 同）
- H-33-3 sink 成员写入（M291=属性/下标 Store 目标登记杀死；
  M292/M293=属性/下标成员作实参杀死；M294=嵌套工厂实参成员杀死；
  M295=成员直呼杀死；M306=海象判定登记杀死；M307=嵌套解包递归杀死）
- H-33-4 装饰器成员（M303=嵌套链类别名根兜底杀死；M304=fn 合并类别名
  表杀死；M305=工厂调用形态成员解析杀死）
- H-33-5 fn 作用域不可判（M299=语句序冲突登记杀死；M300=不可判值登记
  杀死；M301=fbUnresolved 消费臂杀死；M308=fn 容器不可判登记杀死）
- H-33-6 白名单遮蔽（M298=for 目标重绑定杀死；M302=形参遮蔽杀死；
  M296/M297=成员目标延迟可调用「消费/登记」两臂）

判别点一律取**独占落点**（29 组全部先经模拟探针实证「杀死 ⇒ 目标
fixture 变空集 0 hooks」——无兜底臂接住）。数据格式与规矩同
`ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后, 期望转红的
用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行本轮起覆盖 `*_r34`）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND34_DENY = _CHAIN_GUARD + "::test_round34_deny_rules_are_enforced"

R34_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M280：H-33-1 容器元素抽取（下标）杀死 ⇒ `map(_box[0], …)` 逃逸。
    (
        "M280",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if containerBase is not None and containerBase in callableContainers:",
        "                        if False:",
        (_ROUND34_DENY + "[f1_list_container_arg]"),
        1,
    ),
    # M281：H-33-1 星号展开杀死 ⇒ `map(*_box)` 逃逸。
    (
        "M281",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if isinstance(sub.value, ast.Name) and sub.value.id in callableContainers:",
        "                        if False:",
        (_ROUND34_DENY + "[f1_star_container_arg]"),
        1,
    ),
    # M282：H-33-1 容器登记杀死 ⇒ 下标/星号两种抽取都失去来源。
    (
        "M282",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                callableContainers.add(containerTarget.id)",
        "                pass",
        (_ROUND34_DENY + "[f1_list_container_arg]"),
        1,
    ),
    # M283：H-33-1 不透明结果名实参资格杀死 ⇒ `_r = _mk(); map(_r, …)` 逃逸。
    (
        "M283",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in opaqueNames",
        "                        or False",
        (_ROUND34_DENY + "[f1_opaque_result_arg]"),
        1,
    ),
    # M284：H-33-1 fn 作用域模块 def 别名实参资格杀死。
    (
        "M284",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnModuleDefs",
        "                        or False",
        (_ROUND34_DENY + "[f1_fn_module_def_alias_arg]"),
        1,
    ),
    # M285：H-33-1 fn 作用域模块类别名实参资格杀死。
    (
        "M285",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnModuleClassAliases",
        "                        or False",
        (_ROUND34_DENY + "[f1_fn_module_class_alias_arg]"),
        1,
    ),
    # M286：H-33-1 fn 作用域延迟绑定实参资格杀死。
    (
        "M286",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnDeferred",
        "                        or False",
        (_ROUND34_DENY + "[f1_fn_deferred_arg]"),
        1,
    ),
    # M287：H-33-1 fn 作用域不可判值实参资格杀死。
    (
        "M287",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnUnresolved",
        "                        or False",
        (_ROUND34_DENY + "[f1_fn_unresolved_arg]"),
        1,
    ),
    # M288：H-33-1 重绑定 import 名实参资格杀死。
    (
        "M288",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in reboundImports",
        "                        or False",
        (_ROUND34_DENY + "[f1_rebound_import_arg]"),
        1,
    ),
    # M289：H-33-2 fbModuleDefs 前置参数通道杀死 ⇒ `_h(_f)` 转交逃逸。
    (
        "M289",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or node.func.id in fbModuleDefs",
        "                        or False",
        (_ROUND34_DENY + "[h332_module_def_callee]"),
        1,
    ),
    # M290：H-33-2 fbModuleClassAliases 前置参数通道杀死。
    (
        "M290",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or node.func.id in fbModuleClassAliases",
        "                        or False",
        (_ROUND34_DENY + "[h332_module_class_alias_callee]"),
        1,
    ),
    # M291：H-33-3 属性/下标 Store 目标的 sink 成员登记杀死。
    (
        "M291",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                                sinkMembers.add(spreadKey)",
        "                                pass",
        (_ROUND34_DENY + "[f3_attr_store_sink]"),
        1,
    ),
    # M292：H-33-3 属性成员作普通调用实参的资格杀死。
    (
        "M292",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if memberKey is not None and memberKey in sinkMembers:",
        "                        if False:",
        (_ROUND34_DENY + "[f3_attr_store_arg]"),
        1,
    ),
    # M293：H-33-3 下标成员作普通调用实参的资格杀死。
    (
        "M293",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if subscriptMemberKey is not None and subscriptMemberKey in sinkMembers:",
        "                        if False:",
        (_ROUND34_DENY + "[f3_subscript_store_arg]"),
        1,
    ),
    # M294：H-33-3 嵌套工厂实参的 sink 成员资格杀死。
    (
        "M294",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if factoryMemberKey is not None and sinkMembers is not None and factoryMemberKey in sinkMembers:",
        "                if False:",
        (_ROUND34_DENY + "[f3_factory_member_arg]"),
        1,
    ),
    # M295：H-33-3 sink 成员目标直呼杀死 ⇒ `_n.x()` 在 fn 体内执行。
    (
        "M295",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    if memberCallKey is not None and memberCallKey in sinkMembers:",
        "                    if False:",
        (_ROUND34_DENY + "[f3_fn_direct_member_call]"),
        1,
    ),
    # M296：H-33-6 延迟可调用的成员目标直呼杀死。
    (
        "M296",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    if memberCallKey is not None and memberCallKey in deferredMembers:",
        "                    if False:",
        (_ROUND34_DENY + "[f6_attr_target_deferred]"),
        1,
    ),
    # M297：H-33-6 延迟可调用的成员目标登记杀死。
    (
        "M297",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            deferredMembers.add(memberKey)",
        "                            pass",
        (_ROUND34_DENY + "[f6_attr_target_deferred]"),
        1,
    ),
    # M298：H-33-6 for 目标重绑定 import 名登记杀死 ⇒ 白名单根遮蔽看不见。
    (
        "M298",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    if loopName.id in aliases:",
        "                    if False:",
        (_ROUND34_DENY + "[f6_for_target_shadow_whitelist]"),
        1,
    ),
    # M299：H-33-5 fn 作用域语句序冲突登记杀死 ⇒ `_h = _evil; def _h` 被掩。
    (
        "M299",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if (\n"
        "                    fnScopeStmt.name in fnModuleDefs\n"
        "                    or fnScopeStmt.name in fnModuleClassAliases\n"
        "                    or fnScopeStmt.name in fnClassAliases\n"
        "                    or fnScopeStmt.name in fnDeferred\n"
        "                    or fnScopeStmt.name in fnInstances\n"
        "                    or fnScopeStmt.name in fnUnresolved\n"
        "                ):",
        "                if False:",
        (_ROUND34_DENY + "[f5_fn_order_masking]"),
        1,
    ),
    # M300：H-33-5 fn 作用域不可判值（IfExp）登记杀死。
    (
        "M300",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if isinstance(fnValue, (ast.IfExp, ast.BoolOp, ast.Subscript)):",
        "                if False:",
        (_ROUND34_DENY + "[f5_fn_ifexp_binding]"),
        1,
    ),
    # M301：H-33-5 fbUnresolved 消费臂杀死（与 M300 共判别，判别点各自独占）。
    (
        "M301",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    elif fnBindings is not None and node.func.id in fbUnresolved:",
        "                    elif False:",
        (_ROUND34_DENY + "[f5_fn_ifexp_binding]"),
        1,
    ),
    # M302：H-33-6 形参遮蔽登记杀死 ⇒ `def _o(Field): Field(_f)` 洗白。
    (
        "M302",
        "backend",
        "app/utils/ci_conftest_chain.py",
        # 第三十四轮 M146 取证回归：形参登记改为 `getattr(callee, "args")`
        # 防御（非函数兜底节点无 `.args`）⇒ 锚点随之更新，判别不变。
        "        fnArgsNode = getattr(callee, \"args\", None)\n"
        "        if fnArgsNode is not None:\n"
        "            fnShadowed.update(fnArg.arg for fnArg in _fnAllArgs(fnArgsNode))",
        "        pass",
        (_ROUND34_DENY + "[f6_param_shadow_whitelist]"),
        1,
    ),
    # M303：H-33-4 嵌套链根的模块类别名兜底杀死。
    (
        "M303",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if cls is None and classAliases is not None:",
        "        if False:",
        (_ROUND34_DENY + "[f4_module_class_alias_root]"),
        1,
    ),
    # M304：H-33-4 fn 作用域合并类别名表回退成只看 fn-local 表。
    (
        "M304",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '                            {**fnBindings["classAliases"], **fnBindings["moduleClassAliases"]}',
        '                            {**fnBindings["classAliases"]}',
        (_ROUND34_DENY + "[f4_fn_alias_to_module_class]"),
        1,
    ),
    # M305：H-33-4 装饰器工厂调用形态的成员解析杀死。
    (
        "M305",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                                    elif isinstance(callee, ast.Attribute):",
        "                                    elif False:",
        (_ROUND34_DENY + "[f4_fn_local_class_factory_call]"),
        1,
    ),
    # M306：H-33-3 if/while 判定里的海象 sink 登记杀死。
    (
        "M306",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                _sinkTaintSpread(node.test, sinkTainted)",
        "                pass",
        (_ROUND34_DENY + "[f3_walrus_if_sink]"),
        1,
    ),
    # M307：H-33-3 嵌套解包目标的递归展开杀死。
    (
        "M307",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            yield from _targetNamesIn(element)",
        "            yield from ()",
        (_ROUND34_DENY + "[f3_nested_tuple_for_sink]"),
        1,
    ),
    # M308：H-33-5 fn 作用域含本地可调用的容器登记杀死。
    (
        "M308",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                elif isinstance(fnValue, (ast.Dict, ast.List, ast.Set, ast.Tuple)) and _fnContainsLocalCallable(fnValue):",
        "                elif False:",
        (_ROUND34_DENY + "[f1_fn_container_arg]"),
        1,
    ),
]
