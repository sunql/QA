"""CI 判据守卫的**变异清单**（纯数据）—— 第三十轮评审修复批（M241–M246）。

第三十轮复审 3 HIGH + 3 MEDIUM + 6 LOW 的修复判别：

- H-29-1 外部执行器 + 本地可调用实参 fail-closed（M243=Name 臂、
  M246=Attribute 未知接收者臂；`_INERT_CALLABLE_ARG_CHAINS` 白名单由
  真实闭包普查钉死：63 处/5 链，放行性反向锚 = 五条注册惯例 0 hooks）
- H-29-2 模块级被调不再接收调用方作用域（M242=callTargets 落点；
  其余 11 站点的判别由既有 M141/M228 等锚覆盖）
- H-29-3 fn-local 类成员装饰器（M244=合并类表回退成模块表）
- M-29-5 sink 别名洗白（M245=污点登记条件杀死 ⇒ `_w` 洗白逃逸）
- M-27-3 同款反向守卫（M241=删 pattern 里的 `|_r28`；M69 第十四次、
  M217 第四次、M240 第二次重锚均为本轮 pattern 加宽连带，判别不变）

判别点一律取**独占落点**；精识 deny 臂（死后有兜底线接住）的判别 fixture
用**文本归位**断言（`test_round30_deny_rules_are_enforced` 的
_EVIL_R30_EXPECTED_TEXT），放行性变异用 hooks 变空判别——每条变异的
fixture 对应关系写在用例注释里。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行本轮起覆盖 `*_r30`，M241 同款反向守卫）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND30_DENY = _CHAIN_GUARD + "::test_round30_deny_rules_are_enforced"

R30_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M241：pattern 行删 `|_r28` ⇒ r28 批脱出 CI 相关面 ⇒ pattern 反向守卫
    #（其清单元组含 r28，本轮已补）转红。
    (
        "M241",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16|_r18|_r20|_r21|_r23|_r24|_r26|_r28|_r30)?)\\.py$",',
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16|_r18|_r20|_r21|_r23|_r24|_r26|_r30)?)\\.py$",',
        (_SURFACE_GUARD
         + "::test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file"),
        1,
    ),
    # M242：H-29-2 callTargets 落点回退成 threading ⇒ 模块级 def 被调用方
    # fn 作用域别名遮蔽（scope_mask_module_def：`import json as s` 遮蔽
    # `import sys as s`）⇒ hooks 变空判别。
    (
        "M242",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """                        callee = callTargets.get(node.func.id)
                        if callee is not None:
                            # 第二十九轮评审 HIGH-29-2：callTargets 只含模块级
                            # def ⇒ 被调词法作用域是模块，不叠调用方作用域别名。
                            scanCallee(callee, depth + 1, None, None)""",
        """                        callee = callTargets.get(node.func.id)
                        if callee is not None:
                            # 第二十九轮评审 HIGH-29-2：callTargets 只含模块级
                            # def ⇒ 被调词法作用域是模块，不叠调用方作用域别名。
                            scanCallee(callee, depth + 1, aliasesMap, classes)""",
        (_ROUND30_DENY + "[scope_mask_module_def]"),
        1,
    ),
    # M243：H-29-1 Name 臂实参检查杀死 ⇒ `list(map(_f, [0]))` 放行
    # ⇒ hooks 变空判别。
    (
        "M243",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            nameExecutorProblem = _callableArgProblem(node, effAliases)",
        "                            nameExecutorProblem = None",
        (_ROUND30_DENY + "[executor_map_list_local_def]"),
        1,
    ),
    # M244：H-29-3 装饰器类表回退成模块表 ⇒ fn-local `_C.deco` 成员解析失败
    # ⇒ deco 体（sys.path.insert）不被扫 ⇒ hooks 变空判别。
    (
        "M244",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            {**localClasses, **(classes or {})},",
        "                            localClasses,",
        (_ROUND30_DENY + "[fn_local_class_decorator]"),
        1,
    ),
    # M245：M-29-5 污点登记条件杀死 ⇒ `_w = Path('y').write_text` 不再登记
    # ⇒ `_w` 经工厂实参洗白逃逸 ⇒ hooks 变空判别。
    (
        "M245",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    and assignValue.attr in _DENIED_CALL_ATTRS",
        "                    and False",
        (_ROUND30_DENY + "[sink_alias_launder]"),
        1,
    ),
    # M246：H-29-1 Attribute 未知接收者臂实参检查杀死 ⇒
    # `functools.reduce(_f, [0])` 放行 ⇒ hooks 变空判别。
    (
        "M246",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            executorProblem = _callableArgProblem(node, effAliases)",
        "                            executorProblem = None",
        (_ROUND30_DENY + "[executor_functools_reduce]"),
        1,
    ),
]
