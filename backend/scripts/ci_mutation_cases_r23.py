"""CI 判据守卫的**变异清单**（纯数据）—— 第二十三轮评审修复批（M204–M215）。

第二十三轮复审 6C + 2H + 2M + 3L 的修复（CI 面四类夹带路径 C-1/C-2/C-3、
拒绝根/动态段/call-attrs 三层 sink 扩展 C-4、模块根属性再绑定 C-5/C-6、
被调形态 fail-closed H-1、装饰器 fail-closed H-2、CODEOWNERS 漂移 M-2）
各钉一条变异；判别点一律取**独占落点**（fixture 与判别线的对应关系在
`test_ci_conftest_chain.py` 第二十四轮段落与 `test_ci_surface.py` 的
round23 lever 用例注释里逐条写明）。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r23`，M69 第十次重锚）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND24_DENY = _CHAIN_GUARD + "::test_round24_deny_rules_are_enforced"
_ROUND25_DENY = _CHAIN_GUARD + "::test_round25_deny_rules_are_enforced"

R23_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M204：C-1 字节码 pattern（.pyc 后缀；re.match 语义 ⇒ `.*` 前缀）
    (
        "M204",
        "backend",
        "app/utils/ci_surface.py",
        'r".*\\.pyc$",',
        'r".*\\.pycNEVER$",',
        (_SURFACE_GUARD + "::test_round23_smuggling_path_classes_are_ci_surface"),
        1,
    ),
    # M205：C-1 __pycache__ 目录 pattern（re.match 语义 ⇒ `(?:^|.*/)`）
    (
        "M205",
        "backend",
        "app/utils/ci_surface.py",
        'r"(?:^|.*/)__pycache__/",',
        'r"(?:^|.*/)__pycache__NEVER/",',
        (_SURFACE_GUARD + "::test_round23_smuggling_path_classes_are_ci_surface"),
        1,
    ),
    # M206：C-2 同名包目录遮蔽 pattern
    (
        "M206",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/utils/ci_[^/]+/",',
        'r"^backend/app/utils/ci_NEVER[^/]+/",',
        (_SURFACE_GUARD + "::test_round23_smuggling_path_classes_are_ci_surface"),
        1,
    ),
    # M207：C-3 父包 init pattern
    (
        "M207",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/app/utils/__init__\\.py$",',
        'r"^backend/app/utils/__init__NEVER\\.py$",',
        (_SURFACE_GUARD + "::test_round23_smuggling_path_classes_are_ci_surface"),
        1,
    ),
    # M208：C-4 拒绝根 sink 条目（os.chdir 不在 _DENIED_CALL_ATTRS ⇒ 独占判别）
    (
        "M208",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '"os.chdir",',
        '"os.chdirNEVER",',
        (_ROUND24_DENY + "[os_chdir_assign]"),
        1,
    ),
    # M209：C-4 动态根 sink 成员（第二十五轮 L-24-3 更正注释：段集扩展
    # 已回退，本锚 = _DENIED_CALL_ATTRS 的 "write_text" 条目，非禁段扫描）。
    (
        "M209",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '"write_text",',
        '"write_textNEVER",',
        (_ROUND24_DENY + "[path_write_text]"),
        1,
    ),
    # M210：C-4 unknown 接收者 sink 成员（_DENIED_CALL_ATTRS 臂）。
    # occurrence 1：段集扩展回退后 `"unlink",` 仅此一处。
    (
        "M210",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '"unlink",',
        '"unlinkNEVER",',
        (_ROUND24_DENY + "[unknown_recv_sink]"),
        1,
    ),
    # M211：C-5/C-6 模块根属性再绑定 deny 块（第二十五轮 C-24-2 重设计：
    # 锚与变异都落在 effAliases 单点行——变异回退成模块级 aliases 只放走
    # **函数作用域 import** 的再绑定（store_fn_scope_import 判别），模块级
    # 形态不受影响 ⇒ 判别独占。原 `if False:` 形态对 module_root_attr_rebind
    # 判别已被本形态 superset 覆盖）。
    (
        "M211",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if root in effAliases or _isForbidden(root):",
        "if root in aliases or _isForbidden(root):",
        (_ROUND25_DENY + "[store_fn_scope_import]"),
        1,
    ),
    # M212：H-1 被调形态 fail-closed（嵌套 Call 例外臂的判别开关）。
    # 第二十六轮评审 M-26-3 重设计：原变异 `if not isinstance(...)` →
    # `if False:` 会让 `else:` 臂直接吃掉 IfExp 形态 ⇒ node.func.func 在
    # 下一行 AttributeError ⇒ **崩溃红**（退出码 1 + FAILED 行都满足，但
    # 证明的是「变异让分析器崩了」，不是「守卫抓到逃逸」）。改为把该臂的
    # add 换成 pass：臂死 ⇒ IfExp 放行 ⇒ hooks 变空 ⇒ 干净转红。
    (
        "M212",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """add(
                            node.lineno,
                            "被调形态静态不可判"
                            "（非裸名/成员/lambda/下标/嵌套调用）⇒ fail-closed",
                        )""",
        "pass",
        (_ROUND24_DENY + "[fn_callee_ifexp]"),
        1,
    ),
    # M213：H-2 Call/Attribute 装饰器解析根臂（项目根判定条件；变异只关
    # 项目根判定，保留 _isForbidden——该 fixture 的链不在拒绝清单 ⇒ 放行）
    (
        "M213",
        "backend",
        "app/utils/ci_conftest_chain.py",
        'if resolved.split(".")[0] in _PROJECT_DECORATOR_ROOTS or _isForbidden(chain):',
        'if False:',
        (_ROUND24_DENY + "[project_attr_decorator]"),
        1,
    ),
    # M214：H-2 裸名装饰器——from-import 项目根臂
    (
        "M214",
        "backend",
        "app/utils/ci_conftest_chain.py",
        'decResolvedName.split(".")[0] in _PROJECT_DECORATOR_ROOTS',
        'decResolvedName.split(".")[0] in ("__never__",)',
        (_ROUND24_DENY + "[cross_file_project_decorator]"),
        1,
    ),
    # M215：H-2 裸名装饰器——未解析裸名 fail-closed 臂
    (
        "M215",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "if dec.id not in _DECORATOR_BUILTIN_ALLOW:",
        "if False:",
        (_ROUND24_DENY + "[unresolvable_bare_decorator]"),
        1,
    ),
]
