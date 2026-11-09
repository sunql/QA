"""CI 判据守卫的**变异清单**（纯数据）—— 第三十二轮评审修复批（M247–M279）。

第三十二轮复审 6 HIGH（F1–F6）+ 1 MEDIUM（F7）的修复判别：

- F1 实参子树递归（M247=ast.walk 杀死 ⇒ `*[_f, 0]` 星号展开逃逸；
  M249=本地类成员引用判定杀死；M250/M251=fn 作用域 def/类成员资格杀死）
- F1C 参数通道（M252=Name 臂前置参数检查杀死；M267=成员解析落点的
  参数通道杀死——`_c.method(_f)` 经形参转交）
- F2 sink 成员实参（M248=`_DENIED_CALL_ATTRS` 成员资格杀死）
- F3 污点传播（M264=taint 登记杀死 ⇒ `_w` 一跳别名洗白；M265/M266=
  For/With spreadPairs 分支杀死）
- F4 装饰器（M256=fnClassAliases 登记杀死——死后兜底线接住但**文本
  归位**变「静态不可判」，判别不变；M257=_decoratorMember 链环杀死；
  M258=合并类表优先级杀死 ⇒ 模块别名遮蔽 fn-local）
- F5 fn 作用域被调（M253=fbDefs 臂杀死；M254/M255=fnModuleDefs/
  fnModuleClassAliases 直接登记杀死——`_h = _g` 的别名链分支不受影响，
  判别仍独占；M278=fbModuleDefs 站点 threading 回退 ⇒ 遮蔽逃逸）
- F6 白名单链根（M259=延迟可调用登记杀死；M260=Name 臂 deny 杀死，
  与 M259 共判别；M261=Attribute 接收者 deny 杀死；M262/M263=
  fnShadowed/reboundImports 遮蔽校验杀死——移除「白名单仅模块级」
  gate 后遮蔽校验是**单一防线**，判别独占）
- F7 站点判别补齐（第三十轮 docstring 曾把「其余站点由 M141/M228 覆盖」
  写过头——实测 9 个 scanCallee 落点站点无变异判别。本批补齐全部
  **非冗余**站点：M268/M269=localClasses/classAliases 实例化站点
  （判别复用 R30 的 scope_mask_module_class / scope_mask_class_alias）；
  M270=instanceVars __call__ 站点；M271=嵌套类钩子站点；M272–M274=
  装饰器链三站点；M277=fbModuleClassAliases 站点；M275/M276=fb 臂
  条件杀死（fn-local 类别名/实例）。模块级 prescan 的 2 个站点
  （callTargets 装饰器链与 Attribute 链）与 scanExec 装饰器臂**冗余**
  （同语句两遍扫，杀死其一判别仍绿）——防御纵深，不设变异，事实
  记录于 summary.md。）
- M279 = M241 同款反向守卫（删 pattern 里的 `|_r32`）。

判别点一律取**独占落点**（全部 13 个站点/臂变异先经模拟探针实证
「杀死 ⇒ 判定 fixture 逃逸」）；死后有兜底线接住的精识臂用**文本归位**
断言（`test_round32_deny_rules_are_enforced` 的 _EVIL_R32_EXPECTED_TEXT）。
数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文,
变异后, 期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行本轮起覆盖 `*_r32`，M279 同款反向守卫）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND30_DENY = _CHAIN_GUARD + "::test_round30_deny_rules_are_enforced"
_ROUND32_DENY = _CHAIN_GUARD + "::test_round32_deny_rules_are_enforced"

R32_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M247：F1 实参子树递归杀死 ⇒ 只查直接实参 ⇒ `list(map(*[_f, [0]]))`
    # 星号展开逃逸 ⇒ 文本归位判红。
    (
        "M247",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                for sub in ast.walk(a):",
        "                for sub in [a]:",
        (_ROUND32_DENY + "[f1_starred]"),
        1,
    ),
    # M248：F2 sink 成员实参杀死 ⇒ `list(map(Path('y').write_text, [0]))`
    # 放行 ⇒ 文本归位判红。
    (
        "M248",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if sub.attr in _DENIED_CALL_ATTRS:",
        "                        if False:",
        (_ROUND32_DENY + "[f2_map_write_text]"),
        1,
    ),
    # M249：F1 本地类成员资格杀死 ⇒ `map(_C._f, [0])`（ast.walk 必访根
    # Name，根名经成员资格链接住）放行。（成员引用臂与 Name 成员资格
    # 检测等价，冗余臂已删——判别点独占收敛。）
    (
        "M249",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in localClasses",
        "                        or False",
        (_ROUND32_DENY + "[f1_local_class_member_arg]"),
        1,
    ),
    # M250：F1 fn 作用域 def 成员资格杀死 ⇒ `list(map(_f, [0]))`（fn 内
    # def）放行。
    (
        "M250",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnDefs",
        "                        or False",
        (_ROUND32_DENY + "[f1_fn_local_def]"),
        1,
    ),
    # M251：F1 fn 作用域类成员资格杀死 ⇒ fn 内类别名实参放行。
    (
        "M251",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or sub.id in fnClasses",
        "                        or False",
        (_ROUND32_DENY + "[f1_fn_local_class]"),
        1,
    ),
    # M252：F1C Name 臂前置参数检查杀死 ⇒ `_run(_f)`（本地被调形参转交）
    # 放行。
    (
        "M252",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        paramProblem = _callableArgProblem(node, effAliases, fnBindings)",
        "                        paramProblem = None",
        (_ROUND32_DENY + "[param_channel_module_def]"),
        1,
    ),
    # M253：F5 fbDefs 臂杀死 ⇒ fn-local def 直接调用（`_f()`）不扫体。
    (
        "M253",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    elif fnBindings is not None and node.func.id in fbDefs:",
        "                    elif False:",
        (_ROUND32_DENY + "[f5_fn_local_def_call]"),
        1,
    ),
    # M254：F5 fnModuleDefs 直接登记杀死 ⇒ `_h = _g`（模块 def 的 fn 别名，
    # 源在 callTargets 走本分支）不可解析 ⇒ 逃逸。（别名链 `_h2 = _h` 走
    # 传递分支，不受影响 ⇒ 判别仍独占。）
    (
        "M254",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            fnModuleDefs[tid] = srcId",
        "                            pass",
        (_ROUND32_DENY + "[f5_alias_to_module_def]"),
        1,
    ),
    # M255：F5 fnModuleClassAliases 直接登记杀死 ⇒ `_X = _Mc`（模块类的
    # fn 别名）不可解析 ⇒ 逃逸。
    (
        "M255",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            fnModuleClassAliases[tid] = srcId",
        "                            pass",
        (_ROUND32_DENY + "[f5_alias_to_module_class]"),
        1,
    ),
    # M256：F4 fnClassAliases 登记杀死 ⇒ fn-local 类别名著器不可解析 ⇒
    # 落到 H-2「裸名装饰器 fail-closed」兜底线（仍拒但文本变「静态不可判」）
    # ⇒ 文本归位判红。
    (
        "M256",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                                fnClassAliases[tid] = srcId",
        "                                pass",
        (_ROUND32_DENY + "[f4_class_alias_decorator]"),
        1,
    ),
    # M257：F4B _decoratorMember 链环杀死 ⇒ `@_ca.deco`（_ca 别名指向类的
    # 基类）解析不到子类成员 ⇒ deco 体不扫 ⇒ hooks 变空判红。
    (
        "M257",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        for attr in reversed(chainAttrs):",
        "        for attr in []:",
        (_ROUND32_DENY + "[f4_nested_class_chain_decorator]"),
        1,
    ),
    # M258：F4C 合并类表优先级杀死 ⇒ 模块类别名遮蔽 fn-local 类的分支失效
    # ⇒ 解析到模块类（良性）⇒ deco 体不扫 ⇒ hooks 变空判红。
    (
        "M258",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    if baseName in localClasses:",
        "    if False:",
        (_ROUND32_DENY + "[f4_module_alias_shadows_fn_local]"),
        1,
    ),
    # M259：F6 延迟可调用登记杀死 ⇒ `_r = staticmethod(_f); _r()` 不进
    # 延迟集合 ⇒ 调用不被拦（或落不透明兜底文本变化）⇒ 判红。
    (
        "M259",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        deferredCallables.add(t.id)",
        "                        pass",
        (_ROUND32_DENY + "[f6_staticmethod_result_call]"),
        1,
    ),
    # M260：F6 Name 臂延迟 deny 杀死 ⇒ 登记仍在但调用不拦 ⇒ 同判别
    #（与 M259 共判别：两条独立防线各拿同一 fixture 取证）。
    (
        "M260",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    if node.func.id in deferredCallables or node.func.id in fbDeferred:",
        "                    if False:",
        (_ROUND32_DENY + "[f6_staticmethod_result_call]"),
        1,
    ),
    # M261：F6 Attribute 接收者 deny 杀死 ⇒ `_r.__func__()`（白名单工厂
    # 结果的成员调用）放行。
    (
        "M261",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        and node.func.value.id in deferredCallables",
        "                        and False",
        (_ROUND32_DENY + "[f6_staticmethod_dunder_func]"),
        1,
    ),
    # M262：F6 fnShadowed 遮蔽校验杀死 ⇒ `pd = _E(); pd.Field(_f)`（fn 内
    # 重绑定链根）白名单放行 ⇒ 逃逸。（移除「白名单仅模块级」gate 后，
    # 遮蔽校验是该 fixture 的单一防线 ⇒ 判别独占。）
    (
        "M262",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or root in fnShadowed",
        "                        or False",
        (_ROUND32_DENY + "[f6_fn_local_shadow_chain_root]"),
        1,
    ),
    # M263：F6 reboundImports 遮蔽校验杀死 ⇒ `import pydantic as pd;
    # pd = 'x'`（链根重绑定为常量——不进 instanceVars/opaque，只有
    # reboundImports 拦）白名单放行 ⇒ 逃逸。（f6_module_rebind_chain_root
    # 的 `pydantic = _E()` 同时进 instanceVars + reboundImports 双防线，
    # 不作独占判别——防御纵深，fixture 仍留在文本归位集。）
    (
        "M263",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        or root in reboundImports",
        "                        or False",
        (_ROUND32_DENY + "[f6_module_rebind_constant]"),
        1,
    ),
    # M264：F3 taint 登记杀死 ⇒ `_w = Path('y').unlink` 不再登记 ⇒
    # `_w2 = _w` 一跳别名洗白逃逸。
    (
        "M264",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            sinkTainted.add(t.id)",
        "                            pass",
        (_ROUND32_DENY + "[f3_one_hop_alias]"),
        1,
    ),
    # M265：F3 For spreadPairs 分支杀死 ⇒ `for _w in [Path('y').unlink]`
    # 循环目标不登记 ⇒ 洗白逃逸。
    (
        "M265",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    spreadPairs = [(node.iter, [node.target])]",
        "                    spreadPairs = []",
        (_ROUND32_DENY + "[f3_for_target]"),
        1,
    ),
    # M266：F3 With spreadPairs 分支杀死 ⇒ `with Path('y').unlink as _w`
    # 目标不登记 ⇒ 洗白逃逸。
    (
        "M266",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        for item in node.items",
        "                        for item in []",
        (_ROUND32_DENY + "[f3_with_target]"),
        1,
    ),
    # M267：F1C 成员解析落点的参数通道杀死 ⇒ `_c.method(_f)` 的 _f 经
    # 形参转交不被查 ⇒ 逃逸（探针实证唯一 deny 线）。
    (
        "M267",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        memberParamProblem = _callableArgProblem(\n"
        "                            node, effAliases, fnBindings\n"
        "                        )",
        "                        memberParamProblem = None",
        (_ROUND32_DENY + "[param_channel_member_resolved]"),
        1,
    ),
    # M268：F7 localClasses 实例化站点回退成 threading ⇒ 模块级类钩子被
    # 调用方 fn 作用域别名遮蔽（scope_mask_module_class：`import json as s`
    # 遮蔽 `import sys as s`）⇒ hooks 变空判红。（判别 fixture 在 R30 块。）
    (
        "M268",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[node.func.id], callTargets, instAmbiguity\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, None, None)",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[node.func.id], callTargets, instAmbiguity\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND30_DENY + "[scope_mask_module_class]"),
        1,
    ),
    # M269：F7 classAliases 实例化站点回退成 threading ⇒ 判别同上
    #（scope_mask_class_alias，R30 块）。
    (
        "M269",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        for hook in _instantiationHooks(\n"
        "                            localClasses[classAliases[node.func.id]], callTargets\n"
        "                        ):\n"
        "                            scanCallee(hook, depth + 1, None, None)",
        "                        for hook in _instantiationHooks(\n"
        "                            localClasses[classAliases[node.func.id]], callTargets\n"
        "                        ):\n"
        "                            scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND30_DENY + "[scope_mask_class_alias]"),
        1,
    ),
    # M270：F7 instanceVars __call__ 站点（Name 臂）回退成 threading ⇒
    # `_x()` 的 __call__ 体被调用方遮蔽 ⇒ 逃逸。
    (
        "M270",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            # 第二十九轮评审 HIGH-29-2：instanceVars 是模块级表\n"
        "                            # ⇒ __call__ 词法作用域是模块，不叠调用方作用域别名。\n"
        "                            scanCallee(callMember, depth + 1, None, None)",
        "                            # 第二十九轮评审 HIGH-29-2：instanceVars 是模块级表\n"
        "                            # ⇒ __call__ 词法作用域是模块，不叠调用方作用域别名。\n"
        "                            scanCallee(callMember, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[sm_instance_call]"),
        1,
    ),
    # M271：F7 嵌套类钩子站点回退成 threading ⇒ `_O._I()` 的钩子体被
    # 调用方遮蔽 ⇒ 逃逸。
    (
        "M271",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        for hook in _instantiationHooks(member, callTargets, nestedAmbiguity):\n"
        "                            scanCallee(hook, depth + 1, None, None)",
        "                        for hook in _instantiationHooks(member, callTargets, nestedAmbiguity):\n"
        "                            scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[sm_nested_class_hook]"),
        1,
    ),
    # M272：F7 装饰器链 instanceVars 站点回退成 threading ⇒ `@_x` 的
    # __call__ 体被调用方遮蔽 ⇒ 逃逸。
    (
        "M272",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                                scanCallee(callMember, depth + 1, None, None)",
        "                                scanCallee(callMember, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[sm_decorator_instance]"),
        1,
    ),
    # M273：F7 装饰器链 classAliases 站点回退成 threading ⇒ `@_ca` 的
    # 实例化钩子被调用方遮蔽 ⇒ 逃逸。
    (
        "M273",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[classAliases[dec.id]], callTargets\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, None, None)",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[classAliases[dec.id]], callTargets\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[sm_decorator_class_alias]"),
        1,
    ),
    # M274：F7 装饰器链 localClasses 站点回退成 threading ⇒ `@_Mc` 的
    # 实例化钩子被调用方遮蔽 ⇒ 逃逸。
    (
        "M274",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            for hook in _instantiationHooks(localClasses[dec.id], callTargets):\n"
        "                                scanCallee(hook, depth + 1, None, None)",
        "                            for hook in _instantiationHooks(localClasses[dec.id], callTargets):\n"
        "                                scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[sm_decorator_local_class]"),
        1,
    ),
    # M275：F7 fbClassAliases 臂杀死 ⇒ fn-local 类别名实例化（`_X = _Mc;
    # _X()`）整段逃出（探针实证唯一 deny 线）。
    (
        "M275",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    elif fnBindings is not None and node.func.id in fbClassAliases:",
        "                    elif False:",
        (_ROUND32_DENY + "[sm_fn_class_alias_instantiation]"),
        1,
    ),
    # M276：F7 fbInstances 臂杀死 ⇒ fn-local 实例 `__call__`（`_i = _Mc();
    # _i()`）整段逃出。
    (
        "M276",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    elif fnBindings is not None and node.func.id in fbInstances:",
        "                    elif False:",
        (_ROUND32_DENY + "[sm_fn_instance_call]"),
        1,
    ),
    # M277：F7 fbModuleClassAliases 站点回退成 threading ⇒ fn 内模块类
    # 别名实例化（`_X = _Mc; _X()`）被调用方遮蔽 ⇒ 逃逸。
    (
        "M277",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[moduleAliasCls], callTargets\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, None, None)",
        "                            for hook in _instantiationHooks(\n"
        "                                localClasses[moduleAliasCls], callTargets\n"
        "                            ):\n"
        "                                scanCallee(hook, depth + 1, aliasesMap, classes)",
        (_ROUND32_DENY + "[scope_mask_fn_module_class_alias]"),
        1,
    ),
    # M278：F7 fbModuleDefs 站点回退成 threading ⇒ fn 内模块 def 别名
    #（`_h = _g; _h()`）被调用方遮蔽 ⇒ 逃逸。
    (
        "M278",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        scanCallee(\n"
        "                            callTargets[fbModuleDefs[node.func.id]], depth + 1, None, None\n"
        "                        )",
        "                        scanCallee(\n"
        "                            callTargets[fbModuleDefs[node.func.id]], depth + 1, aliasesMap, classes\n"
        "                        )",
        (_ROUND32_DENY + "[scope_mask_fn_module_def_alias]"),
        1,
    ),
    # M279：pattern 行删 `|_r32` ⇒ r32 批脱出 CI 相关面 ⇒ pattern 反向
    # 守卫（其清单元组含 r32，本轮已补）转红。（M241 同款。）
    (
        "M279",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16|_r18|_r20|_r21|_r23|_r24|_r26|_r28|_r30|_r32)?)\\.py$",',
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16|_r18|_r20|_r21|_r23|_r24|_r26|_r28|_r30)?)\\.py$",',
        (_SURFACE_GUARD
         + "::test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file"),
        1,
    ),
]
