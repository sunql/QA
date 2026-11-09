"""CI 判据守卫的**变异清单**（纯数据）—— 第二十七轮评审修复批（M235–M240）。

第二十七轮复审 3 HIGH + 5 MEDIUM + 1 LOW 的修复判别：

- H-27-1 sink 扫描面含外层被调 func 子树（M235）
- M-27-5 operator.itemgetter 入通用可调用工厂拒绝元组（M236）
- H-27-3 IIFE lambda 带上调用方作用域别名（M237）与本地类（M238）
- H-27-2 被扫函数体装饰器检查改 _moduleScopeStatements 递归（M239；
  M227 同步重锚）
- M-27-3 反向守卫清单补 r26（M240——删 pattern 里的 `|_r26`）
  （M-27-2 跑者 `total != occurrence` fail-closed 与 M-27-1 M195/M228
  跨效应注释的判别在跑者测试与注释本身，不是 deny 线；
  M-27-4 fn 作用域 def/lambda callTargets 不登记为既有欠账入 §5）

判别点一律取**独占落点**；精识 deny 臂（死后有兜底线接住）的判别 fixture
用**文本归位**断言（`test_round28_deny_rules_are_enforced` 的
_EVIL_R28_EXPECTED_TEXT），放行性变异用 hooks 变空判别——每条变异的
fixture 对应关系写在用例注释里。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行本轮起覆盖 `*_r28`，M240 同款反向守卫）。
"""

from __future__ import annotations

_SURFACE_GUARD = "app/tests/unit/test_ci_surface.py"
_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"
_ROUND28_DENY = _CHAIN_GUARD + "::test_round28_deny_rules_are_enforced"

R28_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # M235：H-27-1 sink 扫描面退回 args/keywords ⇒ func 子树里的 sink
    #（`field_validator(Path('y').write_text)(lambda v: v)`）无人接住
    #（第二十九轮 LOW-29-7 更正：变异后 hooks 变**空**，不是「只剩内层
    # 被调文本」——内层 `Path('y').write_text` 是 Assign 的 value，不走
    # 工厂调用臂）⇒ 文本归位断言转红（实参 sink 的 regress fixture
    # 不受影响——args 仍在扫描面 ⇒ 判别独占）。
    (
        "M235",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """    for subNode in (
        callNode.func,
        *callNode.args,
        *(kw.value for kw in callNode.keywords),
    ):""",
        """    for subNode in (
        *callNode.args,
        *(kw.value for kw in callNode.keywords),
    ):""",
        (_ROUND28_DENY + "[nested_call_sink_in_factory_args]"),
        1,
    ),
    # M236：M-27-5 itemgetter 出拒绝元组 ⇒ `operator.itemgetter(0)(_c)`
    #（__getitem__ 携 sink）放行 ⇒ hooks 变空判别。
    (
        "M236",
        "backend",
        "app/utils/ci_conftest_chain.py",
        """    "operator.attrgetter",
    # 第二十七轮评审 M-27-5：itemgetter 与 methodcaller/attrgetter 同族——
    # `operator.itemgetter(0)(_c)` 在 import 期执行 `_c.__getitem__`，
    # __getitem__ 可携 sink（探针实证逃逸）。
    "operator.itemgetter",
)""",
        """    "operator.attrgetter",
)""",
        (_ROUND28_DENY + "[nested_call_itemgetter_factory]"),
        1,
    ),
    # M237：H-27-3 IIFE lambda 别名穿透死 ⇒
    # `def _o(): import sys as s; (lambda: s.path.insert(0,'x'))()` 逃出
    # ⇒ hooks 变空判别。
    (
        "M237",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                aliasesMap={**aliases, **(enclosingAliases or {})},",
        "                aliasesMap=None,",
        (_ROUND28_DENY + "[iife_fn_scope_import]"),
        1,
    ),
    # M238：H-27-3 IIFE lambda 本地类穿透死 ⇒ `(lambda: _C())()`（_C 为
    # fn-local 类、__init__ 携 sink）逃出 ⇒ hooks 变空判别。
    (
        "M238",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                classes=enclosingClasses,",
        "                classes=None,",
        (_ROUND28_DENY + "[iife_local_class]"),
        1,
    ),
    # M239：H-27-2 递归退回平扫 ⇒ 分支体/类体内的嵌套 def 动态装饰器
    #（`def _o(): if True: @(_mk() …) def g()`）逃出 ⇒ hooks 变空判别
    #（顶层形态仍被平扫接住 ⇒ 反向 M227 死亡不影响本判别；正向 M227
    # 杀臂时本判别连带转红——宽效应备案，同 M-27-1 M195/M228 先例）。
    (
        "M239",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        for fnStmt in _moduleScopeStatements(list(callee.body)):",
        "        for fnStmt in list(callee.body):",
        (_ROUND28_DENY + "[fn_scope_dynamic_decorator_nested_block]"),
        1,
    ),
    # M240：M-27-3 反向守卫清单漏 r26 的同款漏面——pattern 删 `|_r26` ⇒
    # 真实文件反查用例红（r26 数据文件存在但不再被判进 CI 相关面）。
    # 判别独占：其余 pattern 分支不动。
    (
        "M240",
        "backend",
        "app/utils/ci_surface.py",
        # 第二次重锚（第三十轮）：新增 `_r30` 批后 pattern 行随之加宽。
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16'
        '|_r18|_r20|_r21|_r23|_r24|_r26|_r28|_r30)?)\\.py$",',
        'r"^backend/scripts/ci_mutation_(?:evidence|cases(?:_core|_r14|_r16'
        '|_r18|_r20|_r21|_r23|_r24|_r28|_r30)?\\.py$",',
        (_SURFACE_GUARD
         + "::test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file"),
        1,
    ),
]
