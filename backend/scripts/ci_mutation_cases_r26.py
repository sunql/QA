"""CI 判据守卫的**变异清单**（纯数据）—— 第二十六轮评审修复批（M225–M234）。

第二十六轮复审 5 HIGH + 4 MEDIUM 的修复判别：

- H-26-1 通用可调用工厂 + sink 实参（M225/M226）
- H-26-2 被扫函数体内的嵌套 def/class 装饰器（M227）
- H-26-3 scanCallee 作用域映射带调用方作用域（M228）
- H-26-4 裸名装饰器按作用域映射解析（M229）
- M-26-2 四个此前无 fixture/无变异的装饰器 deny 臂（M230–M233）
  （M-26-1 相对导入——判别在 M229；M-26-3 M212 重设计 + 跑者崩溃红检测 +
  腰带子进程真实跑分析器——判别在跑者本身，取证基建不是 deny 线；
  M-26-4 `*_r26` 入 CI 面 pattern 与两反向守卫清单——清单漏改由
  第二十七轮 M-27-3 在 test_ci_surface.py 补上，判别在两反向清单 + M240）

判别点一律取**独占落点**；精识 deny 臂（死后有兜底线接住）的判别 fixture
用**文本归位**断言（`test_round26_deny_rules_are_enforced` 的
_EVIL_R26_EXPECTED_TEXT），放行性变异用 hooks 变空判别——每条变异的
fixture 对应关系写在用例注释里。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r26`，M69 第十二次重锚 + M217 第 2 次重锚）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND26_DENY = _CHAIN_GUARD + "::test_round26_deny_rules_are_enforced"

R26_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M225：H-26-1 通用可调用工厂拒——条件死 ⇒ `functools.partial(int)('2')`
    # 放行（实参无 sink ⇒ sink 臂接不住）⇒ 放行性判别（hooks 变空）。
    (
        "M225",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "and resolvedInnerChain not in _CALLABLE_FACTORY_DENIED",
        "and True",
        (_ROUND26_DENY + "[nested_call_partial_factory]"),
        1,
    ),
    # M226：H-26-1 sink 实参扫描臂死 ⇒ `field_validator('x')(Path('y').unlink)`
    # 被当注册式放行 ⇒ 文本归位（sink 消息消失 ⇒ 兜底「内层被调」文本不同）。
    (
        "M226",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if sinkProblem is not None:",
        "if False:",
        (_ROUND26_DENY + "[nested_call_sink_arg]"),
        1,
    ),
    # M227：H-26-2 被扫函数体内的嵌套 def/class 装饰器动态表达式臂死 ⇒
    # `def _o(): @(_mk() if True else None) def g()` 逃出（模块级预扫只管
    # 模块级 def ⇒ 该 fixture 完全放行）。
    # 第二十八轮 H-27-2 重锚：该臂从平扫 callee.body 顶层改成
    # _moduleScopeStatements 递归（分支体/类体补漏）⇒ 锚随新形态两行
    #（for 行保唯一——line 1500 的 `for s in _moduleScopeStatements` 变量名不同）。
    (
        "M227",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """        for fnStmt in _moduleScopeStatements(list(callee.body)):
            if isinstance(fnStmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):""",
        """        for fnStmt in _moduleScopeStatements(list(callee.body)):
            if False:""",
        (_ROUND26_DENY + "[fn_scope_dynamic_decorator]"),
        1,
    ),
    # M228：H-26-3 scanCallee 作用域映射退回**不带调用方作用域**的旧形态
    # ⇒ fn-local 类 __init__ 里的 fn 作用域 import 再绑定逃出（放行性）。
    (
        "M228",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """        scopeAliases = {
            **aliases,
            **(enclosingAliases or {}),
            **_aliasMap(list(callee.body)),
        }""",
        """        scopeAliases = {
            **aliases,
            **_aliasMap(list(callee.body)),
        }""",
        (_ROUND26_DENY + "[two_level_fn_scope_import]"),
        1,
    ),
    # M229：H-26-4 裸名装饰器解析退回模块级 aliases ⇒ fn 作用域的项目 import
    # 遮蔽模块级外部同名别名时解析到外部根放行（放行性）。M-26-1 相对导入的
    # 项目根判定走同一 _PROJECT_DECORATOR_ROOTS 元组（__relative__ 入元组）
    # ⇒ 该 fixture 的判别独立落在此行。
    (
        "M229",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "decResolvedName = effAliases.get(dec.id)",
        "decResolvedName = aliases.get(dec.id)",
        (_ROUND26_DENY + "[fn_scope_project_decorator_shadow]"),
        1,
    ),
    # M230：M-26-2 「装饰器工厂本身是调用结果」臂死 ⇒ 落「工厂形态静态不可判」
    # 兜底（仍拒但文本不同）⇒ 文本归位判别。⚠️ 该 fixture 同时被「嵌套调用
    # 工厂」线接住（装饰器表达式也是 Call 节点）⇒ 文本归位是唯一判别。
    (
        "M230",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "elif isinstance(callee, ast.Call):",
        "elif False:",
        (_ROUND26_DENY + "[decorator_call_result]"),
        1,
    ),
    # M231：M-26-2 「装饰器工厂形态静态不可判」臂——decChain 换成静态外部链
    # ⇒ Subscript 工厂走 rootProblem（外部 ⇒ None）⇒ 该臂死（其余线是
    # 「下标取可调用」不同文本）⇒ 文本归位判别。sentinel 不用 dunder 形态
    #（`__ext__` 会撞 _FORBIDDEN_DUNDER_SEGMENTS）。
    (
        "M231",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "decChain = _dottedChain(callee)",
        'decChain = "external_static.never"',
        (_ROUND26_DENY + "[decorator_subscript_factory]"),
        1,
    ),
    # M232：M-26-2 「装饰器基底静态不可判」臂——同 M231 手法（Attribute 臂
    # 的 decChain 换静态外部链 ⇒ Call 基底属性装饰器放行）。
    (
        "M232",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "decChain = _dottedChain(dec)",
        'decChain = "external_static.never"',
        (_ROUND26_DENY + "[decorator_attr_base]"),
        1,
    ),
    # M233：M-26-2 「装饰器工厂裸名静态不可判」臂死 ⇒ 未 import 裸名工厂
    # 放行（_decoratorMember 对 Call 形态返回 None ⇒ 无兜底）⇒ 放行性判别。
    # 锚带 isinstance 行保唯一（本地工厂 elif 的条件不同）。
    (
        "M233",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """                                    elif (
                                        isinstance(callee, ast.Name)
                                        and callee.id not in effAliases
                                        and callee.id not in _DECORATOR_BUILTIN_ALLOW
                                    ):""",
        "                                    elif False:",
        (_ROUND26_DENY + "[decorator_unknown_bare_factory]"),
        1,
    ),
    # M234：M-26-1 相对导入哨兵根出 `_PROJECT_DECORATOR_ROOTS` ⇒
    # `from . import models; @models.deco` 解析不到项目根放行（放行性）。
    # 其余 fixture 的链根不含 __relative__ ⇒ 判别独占。
    (
        "M234",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '_PROJECT_DECORATOR_ROOTS = ("app", "scripts", "__relative__")',
        '_PROJECT_DECORATOR_ROOTS = ("app", "scripts", "__never__")',
        (_ROUND26_DENY + "[relative_import_project_decorator]"),
        1,
    ),
]
