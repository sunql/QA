"""CI 判据守卫的**变异清单**（纯数据）—— 第二十轮收口批（M152–M174）。

第十九轮复审 7H + 3M + 1L 的修复（`ci_conftest_chain.py`：动态根属性链禁段
`_dynamicRootForbiddenSegment`、`__setattr__` 禁段、`_resolveMember` 三态接收者
解析（实例变量/嵌套链/构造器/字面量）、类体名字绑定（Name/staticmethod/三参
type 别名）、条件重定义歧义（ambiguousNames/ambiguousClasses）、装饰器预扫先于
applyBinding、PEP 562 全绑定形态、被调链深 fail-closed、sys.meta_path 根、实例
属性读/下标/真值/实参协议路径、本地未定义成员 fail-closed）各钉一条变异。

判别点一律取**独占落点**。两条设计性依赖，评审时别当巧合：
1. `model_rebuild` 豁免（`_ALLOWED_EXPR_METHODS`）把「成员解析线」的变异判别
   买了出来——成员被解析关闭后 fixture 走豁免放行 ⇒ 转红，不会被「未定义成员
   fail-closed」线罩住假绿（M140/M156/M157/M159 同理）。
2. `@D.deco` 成员解析收敛进 `_decoratorMember` 单点（主循环预扫与 scanExec
   两处共用）——跑者只替换第 N 次出现，双份同文锚不死两处 ⇒ fixture 不转红。

数据格式与规矩同 `ci_mutation_cases.py`（(编号, 作用域, 文件, 原文, 变异后,
期望转红的用例, occurrence)；跑者在 `ci_mutation_evidence.py`）。

⚠️ 本文件与跑者同属「CI 相关面」（改了它就能让守卫恒绿）
⇒ 必须同时在 `.github/CODEOWNERS` 与 `ci_surface._CI_SURFACE_PATTERNS`
（`ci_mutation_` pattern 行已覆盖 `*_r20`）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"

R20_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    # ---------- HIGH-1/HIGH-2：动态根禁段 + setattr 段 ----------
    # 动态根（Lambda/Subscript/Call/…）属性链的禁段扫描；扫描行被关
    # ⇒ `(lambda: 0).__globals__` 逃出。（最小链 fixture：不带 __builtins__
    # 下标——那会被根拒绝线另判，破坏独占性。）
    (
        "M152",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            seg = _dynamicRootForbiddenSegment(node)",
        "            seg = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[lambda_globals_spine]"),
        1,
    ),
    # `__setattr__` 禁段条目被删 ⇒ `sys.__setattr__('path', [])` 逃出
    # （object.__setattr__ 走根拒绝不受影响 ⇒ 无新增假红）。
    (
        "M155",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    "__setattr__",',
        '    "",',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[setattr_segment]"),
        1,
    ),
    # sys.meta_path 根条目被删 ⇒ import 钩子安装逃出。
    (
        "M171",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    "sys.meta_path",',
        '    "",',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[meta_path_install]"),
        1,
    ),
    # ---------- HIGH-3：_resolveMember 三态接收者解析 ----------
    # 实例变量解析（`_x = X(); _x.m()`）；instanceVars 臂被关 ⇒ 改名实例逃出
    # （member 缺 + model_rebuild 豁免 ⇒ 放行 ⇒ 转红）。
    (
        "M156",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        cls = localClasses.get(recv.id) or instanceVars.get(recv.id)",
        "        cls = localClasses.get(recv.id)",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_method_call]"),
        1,
    ),
    # 构造器接收者（`X().m()`）的本地类成员解析被关 ⇒ ctor 改名体逃出。
    # （第二十二轮重锚：第 round-22 批给 `_classMember` 串接 `ambiguity` 参数，
    # 旧锚行加了第 4 参——H6 成员歧义 fail-closed 的载体；锚随行更新。）
    (
        "M157",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            member = _classMember(localClasses[recv.func.id], func.attr, callTargets, ambiguity)",
        "            member = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[ctor_member_call]"),
        1,
    ),
    # 本地 def/lambda 工厂接收者（`make().m()`）⇒ dynamic fail-closed；
    # 调用点的 dynamic 臂被关 ⇒ 逃出。
    (
        "M153",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '                    if status == "dynamic":',
        "                    if False:",
        (_CHAIN_GUARD
         + "::test_hook_positions_are_denied[dynamic_receiver_local_factory]"),
        1,
    ),
    # 下标取函数再调用（`d['f']()`）；func Subscript 臂被关 ⇒ 逃出。
    (
        "M154",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if isinstance(node.func, ast.Subscript):",
        "                if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[subscript_call]"),
        1,
    ),
    # 本地接收者上未定义成员的调用（继承/property 途径静态追不到）
    # ⇒ fail-closed；豁免例外检查被关 ⇒ `_x.nope()` 逃出。
    (
        "M174",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if node.func.attr not in _ALLOWED_EXPR_METHODS:",
        "                        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[missing_member_call]"),
        1,
    ),
    # ---------- HIGH-4：类体名字绑定 ----------
    # `__init__ = evil`（类体 Name 值绑定）；Name 值解析臂被关 ⇒ 逃出。
    (
        "M158",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            if isinstance(value, ast.Name) and callTargets and value.id in callTargets:",
        "            if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[init_name_assign]"),
        1,
    ),
    # `model_rebuild = staticmethod(evil)`；staticmethod/classmethod 解包臂被关
    # ⇒ 逃出（member 缺 + model_rebuild 豁免 ⇒ 放行 ⇒ 转红）。
    (
        "M159",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '                and value.func.id in ("staticmethod", "classmethod")',
        "                and False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[staticmethod_member]"),
        1,
    ),
    # 三参 type() 的别名形态（`_t = type; X = _t('X', (), {...})`）；别名臂被关
    # ⇒ 逃出（真名直接形态由 M144 钉，不受影响）。
    (
        "M160",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '                and (node.func.id == "type" or node.func.id in typeAliases)',
        '                and node.func.id == "type"',
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[type_alias_three_arg]"),
        1,
    ),
    # ---------- HIGH-5：条件重定义歧义 + 装饰器预扫 ----------
    # def 条件重定义（if/else 同名 def）⇒ ambiguousNames；def 臂登记被关
    # ⇒ 被调解析落到末位 def（良性体）⇒ 逃出。
    (
        "M161",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                ambiguousNames.add(stmt.name)",
        "                pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[cond_redef]"),
        1,
    ),
    # lambda 条件重定义；lambda 臂登记被关 ⇒ 同上逃出。
    (
        "M173",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        ambiguousNames.add(t.id)",
        "                        pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[lambda_redef]"),
        1,
    ),
    # 类重定义 ⇒ ambiguousClasses；实例化点的歧义臂被关 ⇒ 落到末位类（空体）
    # ⇒ 逃出。
    (
        "M162",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                        if node.func.id in ambiguousClasses:",
        "                        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[class_redef]"),
        1,
    ),
    # 装饰器引用旧绑定（`def m: evil / @m def m`）：主循环预扫在 applyBinding
    # 之前解析 @m ⇒ 命中旧 m；预扫（Name 臂 + _decoratorMember 臂）被关
    # ⇒ scanExec 阶段解析到新 m（良性体）⇒ 逃出。⚠️ 锚必须带
    # `for dec in stmt.decorator_list:` 前缀行——16 空格条件行是 scanExec
    # 副本（20 空格）的子串，裸锚会替换错处（solo 取证实证）；且必须整块
    # if/else 一起换，否则留下孤儿 else ⇒ SyntaxError（同样 solo 实证）。
    (
        "M163",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            for dec in stmt.decorator_list:\n"
        "                if isinstance(dec, ast.Name) and dec.id in callTargets:\n"
        "                    scanCallee(callTargets[dec.id], 0)\n"
        "                else:\n"
        "                    member = _decoratorMember(dec, localClasses, callTargets)\n"
        "                    if member is not None and not isinstance(member, ast.ClassDef):\n"
        "                        scanCallee(member, 0)",
        "            for dec in stmt.decorator_list:\n"
        "                pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[decorator_prev_binding]"),
        1,
    ),
    # lambda 装饰器（装饰期执行 lambda 体）；动态装饰器臂被关 ⇒ 逃出
    # （lambda 体对 _iterExecNodes 不可见 ⇒ 无第二判别线）。
    (
        "M169",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if isinstance(dec, (ast.Lambda, ast.Subscript, ast.Starred)):",
        "                if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[lambda_decorator]"),
        1,
    ),
    # （装饰器经类成员 `@D.deco` 由 r18 的 M150 钉——锚 _decoratorMember 单点，
    # 本批不重复。）
    # ---------- MEDIUM：实例协议路径 + PEP 562 + 深度 + 根 ----------
    # 实例下标/运算/比较（`_x[0]` ⇒ __getitem__）；形态元组被清空 ⇒ 逃出。
    (
        "M164",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                    (ast.Subscript, ast.BinOp, ast.Compare, ast.UnaryOp, ast.BoolOp),",
        "                    (),",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_subscript]"),
        1,
    ),
    # 实例真值判定（`if _x:` ⇒ __bool__/__len__）；test 检查被关 ⇒ 逃出。
    (
        "M165",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            if _usesLocalInstance(test, localClasses, instanceVars):",
        "            if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_truthiness]"),
        1,
    ),
    # 实例作为调用实参（`list(X())` ⇒ __iter__）；实参扫描被关 ⇒ 逃出。
    (
        "M166",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                            _usesLocalInstance(a, localClasses, instanceVars) for a in argNodes",
        "                            False for a in argNodes",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[instance_as_arg]"),
        1,
    ),
    # M172 已退休（第二十二轮）：其锚线（实例属性读取独立 deny 块）与
    # 第二十二轮 H3 块（`_usesLocalInstance`，M181 钉）条件严格超集重复
    # ⇒ 双线同拦拿不到独占变异取证；冗余 deny 块已从守卫删除，`_x.p`
    # 形态由 H3 块统一拒绝，fixture instance_property_read 保留。
    # PEP 562 经元组解包/with/for 目标绑定（Store-Name walk）；walk 臂被关
    # ⇒ `__getattr__, _x = (lambda, 1)` 逃出。
    (
        "M167",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            and node.id in _MODULE_GETATTR_NAMES",
        "            and False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[pep562_tuple_unpack]"),
        1,
    ),
    # PEP 562 经 from-import 别名（`from x import f as __getattr__`）；
    # Import 臂被关 ⇒ 逃出。
    (
        "M168",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            if (alias.asname or alias.name) in _MODULE_GETATTR_NAMES:",
        "            if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[pep562_import_alias]"),
        1,
    ),
    # 被调链深超上限 ⇒ fail-closed；报错行被关 ⇒ 静默截断 ⇒ 深处 evil 逃出
    # （第二十轮 MEDIUM-2 的精确语义：cap 还在、只是不报）。
    (
        "M170",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '            add(getattr(callee, "lineno", 0), f"被调链深超过 {_MAX_CALLEE_DEPTH} ⇒ fail-closed")',
        "            pass",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[callee_depth_cap]"),
        1,
    ),
]
