"""CI 判据守卫的**变异清单**（纯数据）—— 第二十二轮收口批（M175–M202）。

第二十一轮复审 2C + 9H + 2M + 1L 的修复（守卫模块根链 / 动态根危险段 /
类别名与可调用实例 / 协议节点补齐 / 成员歧义 fail-closed / type 别名链 /
类体与被调体语句级检查 / fn 局部类覆盖层 / 函数体别名作用域 / 不透明绑定 /
AST 预算与递归包装）各钉一条变异；判别点一律取**独占落点**。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r21`）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"

R21_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M175：C1 守卫模块根链
    (
        "M175",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if any(chain == root or chain.startswith(f\"{root}.\") for root in _GUARD_MODULE_ROOTS):",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[guard_module_import]"),
        1,
    ),
    # M176：C2 动态根危险后缀段
    (
        "M176",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "or cur.attr in _DYNAMIC_DENIED_SEGMENTS",
        "or False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[dynamic_env_launder]"),
        1,
    ),
    # M177：H1 类别名实例化臂
    (
        "M177",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif node.func.id in classAliases:",
        "elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_alias_call]"),
        1,
    ),
    # M178：H1 类别名裸名装饰器臂
    (
        "M178",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif dec.id in classAliases:",
        "elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_alias_bare_decorator]"),
        1,
    ),
    # M179：H2 可调用实例调用臂
    (
        "M179",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif node.func.id in instanceVars:",
        "elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[callable_instance_call]"),
        1,
    ),
    # M180：H2 可调用实例装饰器臂
    (
        "M180",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if dec.id in instanceVars:",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_decorator]"),
        1,
    ),
    # M181：H3 实例化结果属性读取
    (
        "M181",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "and _usesLocalInstance(node.value, localClasses, instanceVars)",
            "            ):",
            "                # 第二十二轮 H3",
        ]),
        "\n".join([
            "and False):",
            "                # 第二十二轮 H3",
        ]),
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[ctor_property_read]"),
        1,
    ),
    # M182：H4 super/type 接收者
    (
        "M182",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "recv.func.id in (\"super\", \"type\")",
        "False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[super_receiver_call]"),
        1,
    ),
    # M183：H5 实例属性赋值
    (
        "M183",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "and isinstance(node.ctx, ast.Store)",
            "                and isinstance(node.value, ast.Name)",
            "                and node.value.id in instanceVars",
        ]),
        "\n".join([
            "and isinstance(node.ctx, ast.Store)",
            "                and isinstance(node.value, ast.Name)",
            "                and False",
        ]),
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_setattr_store]"),
        1,
    ),
    # M184：H5 协议节点元组
    (
        "M184",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "ast.JoinedStr,",
            "                    ast.Dict,",
            "                    ast.ListComp,",
            "                    ast.SetComp,",
            "                    ast.DictComp,",
            "                    ast.GeneratorExp,",
            "                    ast.Starred,",
        ]),
        "ast.Starred,",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[fstring_format]"),
        1,
    ),
    # M185：H5 元组解包迭代
    (
        "M185",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "and isinstance(node.targets[0], (ast.Tuple, ast.Starred))",
        "and False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[tuple_unpack_iter]"),
        1,
    ),
    # M186：H6 成员歧义→dynamic 转换（Name 接收者臂）
    (
        "M186",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "member = _classMember(cls, func.attr, callTargets, ambiguity)",
            "        if ambiguity and func.attr in ambiguity:",
            "            return None, \"dynamic\"",
        ]),
        "\n".join([
            "member = _classMember(cls, func.attr, callTargets, ambiguity)",
            "        if False:",
            "            return None, \"dynamic\"",
        ]),
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[member_conditional_arm]"),
        1,
    ),
    # M187：H6 实例化钩子歧义
    (
        "M187",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if instAmbiguity:",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[hook_conditional_arm]"),
        1,
    ),
    # M188：H6 嵌套类钩子歧义
    (
        "M188",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if nestedAmbiguity:",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[nested_class_hook_ambiguous]"),
        1,
    ),
    # M189：H6 装饰器成员歧义
    (
        "M189",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if decAmbiguity:",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[decorator_member_ambiguous]"),
        1,
    ),
    # M190：H7 type 别名链
    (
        "M190",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if value.id == \"type\" or value.id in typeAliases:",
        "if value.id == \"type\":",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[type_alias_chain]"),
        1,
    ),
    # M191：H8 类体语句级检查
    (
        "M191",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "cbProblem = _instanceExecProblem(s, localClasses, cbInstances)",
        "cbProblem = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_body_with_instance]"),
        1,
    ),
    # M192：H9 被调体语句级检查
    (
        "M192",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "fnProblem = _instanceExecProblem(stmt, effectiveClasses, effInstances)",
        "fnProblem = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[callee_with_instance]"),
        1,
    ),
    # M193：H9 fn 局部类覆盖层臂
    (
        "M193",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if classes is not None and node.func.id in classes:",
        "if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[fn_local_class_call]"),
        1,
    ),
    # M194：H9 fn 局部类重定义 fail-closed
    (
        "M194",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "add(",
            "                                node.lineno,",
            "                                \"函数体内局部类多重定义 ⇒ 实例化目标静态不可判 ⇒ fail-closed\",",
            "                            )",
        ]),
        "pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[fn_local_class_redef]"),
        1,
    ),
    # M195：M2 函数体别名作用域
    (
        "M195",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "scopeAliases = {**aliases, **_aliasMap(list(callee.body))}",
        "scopeAliases = aliases",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[fn_local_import_alias]"),
        1,
    ),
    # M196：M1 不透明绑定登记 else
    (
        "M196",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "\n".join([
            "            else:",
            "                for t in _assignTargetNames(targets):",
            "                    opaqueNames.add(t.id)",
        ]),
        "\n".join([
            "            else:",
            "                pass",
        ]),
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[opaque_ifexp_call]"),
        1,
    ),
    # M197：不透明名调用臂
    (
        "M197",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif node.func.id in opaqueNames:",
        "elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[opaque_name_call]"),
        1,
    ),
    # M198：不透明名装饰器臂
    (
        "M198",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "add(dec.lineno, \"不透明绑定的名字作装饰器 ⇒ 装饰期执行 ⇒ fail-closed\")",
        "pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[opaque_name_decorator]"),
        1,
    ),
    # M199：不透明容器下标赋值
    (
        "M199",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "and _usesOpaqueName(node.value, opaqueNames)",
        "and False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[opaque_subscript_store]"),
        1,
    ),
    # M200：本地类直接装饰器臂
    (
        "M200",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif dec.id in localClasses:",
        "elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_direct_decorator]"),
        1,
    ),
    # M201：L1 AST 节点预算
    (
        "M201",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if _countAstNodes(tree) > _MAX_AST_NODES:",
        "if False:",
        (_CHAIN_GUARD + "::test_ast_node_budget_fail_closed"),
        1,
    ),
    # M202：L1 RecursionError 包装
    (
        "M202",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "return _findImportTimeHooksImpl(path)",
        "return ()",
        (_CHAIN_GUARD + "::test_deep_nesting_recursion_fail_closed"),
        1,
    ),
    # M203：H1 _decoratorMember 类别名回退
    (
        "M203",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "or (classAliases is not None and dec.value.id in classAliases)",
        "or False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_alias_decorator]"),
        1,
    ),
]
