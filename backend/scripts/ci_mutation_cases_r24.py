"""CI 判据守卫的**变异清单**（纯数据）—— 第二十五轮评审修复批（M216–M224）。

第二十四轮复审 2C + 2H + 2M + 3L 的修复判别：装饰器 fail-closed 三路（预扫
白名单收窄 C-24-1 / 本地工厂臂 / lambda 工厂臂）、Store 基底 fail-closed 与
effAliases 作用域单点（C-24-2）、嵌套调用工厂可证性（H-24-1）、不透明绑定
兜底（H-24-1 applyBinding else）、原生扩展遮蔽 pattern（H-24-2）、反向守卫
清单补 r23（M-24-2）、字面量接收者豁免（M-24-1）。

判别点一律取**独占落点**；精识 deny 臂（死后有兜底线接住）的判别 fixture
用**文本归位**断言（`test_round25_deny_rules_are_enforced` 的
_EVIL_R25_EXPECTED_TEXT），放行性变异用 hooks 变空判别——每条变异的
fixture 对应关系写在用例注释里。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r24`，M69 第十一次重锚）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND25_DENY = _CHAIN_GUARD + "::test_round25_deny_rules_are_enforced"
_ROUND25_CLEAN = _CHAIN_GUARD + "::test_round25_clean_forms_pass"

R24_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M216：H-24-2 原生扩展遮蔽 pattern（CPython EXT_SUFFIX 优先于同名 .py；
    # 第二十六轮 H-26-5 泛化到 backend 全域 ⇒ 第 2 次重锚）
    (
        "M216",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/.*\\.(?:so|pyd)$",',
        'r"^backend/.*\\.(?:soNEVER|pyd)$",',
        (_SURFACE_GUARD + "::test_round23_smuggling_path_classes_are_ci_surface"),
        1,
    ),
    # M217：M-24-2 反向守卫清单——变异脚本 pattern 只删 `|_r23` ⇒ 清单里的
    # r23 条目转红（r24/r26 等其余分支不受影响 ⇒ 判别独占）。
    # 第二十六轮第 2 次重锚：pattern 行新增 `|_r26`。
    (
        "M217",
        "backend",
        "app/utils/ci_surface.py",
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16'
        '|_r18|_r20|_r21|_r23|_r24|_r26)?)\\.py$",',
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16'
        '|_r18|_r20|_r21|_r24|_r26)?)\\.py$",',
        (_SURFACE_GUARD
         + "::test_detectCiSurfaceChanges_flags_the_mutation_evidence_script"),
        1,
    ),
    # M218：C-24-1 预扫白名单收窄——回退成旧元组 ⇒ IfExp 装饰器逃出预扫
    # 且 scanExec 装饰器臂不认（非 Call/Attribute/Name）⇒ 完全放行。
    (
        "M218",
        "backend",
        "app/utils/ci_conftest_chain.py",
        # 第二十六轮重锚（同 M169）：scanCallee 新增同判据臂（H-26-2）⇒ 单行锚
        # 成其子串 ⇒ 带前导 for 行（12 空格缩进）唯一钉住模块级预扫臂。
        "            for dec in stmt.decorator_list:\n"
        "                if not isinstance(dec, (ast.Name, ast.Attribute, ast.Call)):",
        # ⚠️ 回退目标是 round-24 之前的**正向**白名单（无 not）——写成
        # `not isinstance(...)` 语义反而更宽（拒掉一切非这三形态的表达式）。
        # mut 保留 for 头（锚跨两行 ⇒ mut 也要两行，否则 for 体悬空 IndentationError）。
        "            for dec in stmt.decorator_list:\n"
        "                if isinstance(dec, (ast.Lambda, ast.Subscript, ast.Starred)):",
        (_ROUND25_DENY + "[decorator_ifexp]"),
        1,
    ),
    # M219：C-24-1 本地 def 工厂臂死 ⇒ 落「裸名静态不可判」兜底（仍拒但文本
    # 不同）⇒ 文本归位判别。
    (
        "M219",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """                                    elif (
                                        isinstance(callee, ast.Name)
                                        and (
                                            callee.id in callTargets
                                            or callee.id in ambiguousNames
                                        )
                                    ):""",
        "                                    elif False:",
        (_ROUND25_DENY + "[decorator_local_factory]"),
        1,
    ),
    # M220：C-24-1 lambda 工厂臂死 ⇒ 落「形态静态不可判」兜底 ⇒ 文本归位判别。
    # 锚带注释行（`if isinstance(callee, ast.Lambda):` 在 lambda 被调臂另有
    # 一处 ⇒ 双行锚保唯一）。
    (
        "M220",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """                            if isinstance(callee, ast.Lambda):
                                # `@(lambda f: f)(hook)` —— 返回的装饰器闭包体不可静态判定""",
        """                            if False:
                                # `@(lambda f: f)(hook)` —— 返回的装饰器闭包体不可静态判定""",
        (_ROUND25_DENY + "[decorator_lambda_factory]"),
        1,
    ),
    # M221：H-24-1 嵌套调用工厂可证性臂死 ⇒ 内层本地 def 放行（pick 体干净、
    # 绑定值未被使用 ⇒ 无兜底线接住）⇒ hooks 变空判别。
    (
        "M221",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if not isProvenRegistration:",
        "                        if False:",
        (_ROUND25_DENY + "[nested_call_local_factory]"),
        1,
    ),
    # M222：C-24-2 Store 基底 fail-closed 臂死 ⇒ 下标基底再绑定放行
    # （`.foo` 不在 _REBIND_DENIED_ATTRS ⇒ 无其他线接住）。
    (
        "M222",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    if not isStaticChain:",
        "                    if False:",
        (_ROUND25_DENY + "[store_subscript_base]"),
        1,
    ),
    # M223：H-24-1 applyBinding 不透明兜底 else 臂死 ⇒ lambda 调用结果绑定
    # 不记不透明 ⇒ unknown 放行语义放行后续调用 ⇒ hooks 变空判别。
    # 锚带注释行（裸 `else:` 在 applyBinding 内多处）。
    (
        "M223",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """                else:
                    # 第二十五轮评审 H-24-1：func 为 lambda/嵌套调用/下标等""",
        """                elif False:
                    # 第二十五轮评审 H-24-1：func 为 lambda/嵌套调用/下标等""",
        (_ROUND25_DENY + "[opaque_lambda_result]"),
        1,
    ),
    # M224：M-24-1 字面量接收者豁免条件死 ⇒ `'a'.replace(...)` 被拒
    # ⇒ clean 用例红（放行性变异的反向：豁免丢失）。path_write_text 的
    # 接收者是 Call 非 Constant ⇒ 判别独占。
    (
        "M224",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            if isinstance(node.func.value, ast.Constant):",
        "                            if False:",
        (_ROUND25_CLEAN + "[constant_replace]"),
        1,
    ),
]
