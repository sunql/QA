"""第十四轮复审收口批的**变异清单**（纯数据，M100–M121 共 22 组）。

背景（第十三轮复审 BLOCK 3H+3M+2L）：检测器有**位置盲区**（If.test / except
类型 / 装饰器 / 默认值 / 注解从未拒绝扫描）、拒绝清单可被**别名绕过**
（`import sys as s`）、派生边缺失（相对导入 / `__all__` / 顶层前缀硬编码），
且**测试零判别**（`_DENIED_ROOTS` 清空 / 扫描循环整段删除 ⇒ 守卫仍全绿）。

⇒ 本批每组变异钉住第十三/十四轮的一个修复组件：

| 组 | 组件 | 期望转红 |
|---|---|---|
| M100 | 根链拒绝扫描循环整段删除（MEDIUM-1 点名） | hook_positions[assign_value] |
| M101 | `_isDeniedChain` 恒 False（拒绝清单逻辑失效） | hook_positions[assign_value] |
| M102 | `_aliasMap` 的 Import-asname 映射删除 | hook_positions[import_alias] |
| M103 | `_aliasMap` 的 ImportFrom 映射删除 | hook_positions[from_alias] |
| M104 | Name 的 Load-ctx 判定删除（**假红方向**） | definitional[field_named_input] |
| M105 | 函数/Lambda body 跳过删除（**假红方向**） | definitional[method_body] |
| M106 | 相对导入解析死掉 | derivation_covers_relative… |
| M107 | `from X import *` 的 `__all__` 展开删除 | 同上 |
| M108 | 顶层前缀硬编码重新引入（ImportFrom 边） | 同上 |
| M109 | 顶层前缀硬编码重新引入（Import 边） | 同上 |
| M110 | `pytest_plugins +=` 不再算插件声明 | 同上 |
| M111 | match 臂展开删除 | structural[match_body] |
| M112 | except handler 臂展开删除 | structural[handler_body] |
| M113 | try orelse 臂展开删除 | structural[try_orelse] |
| M114 | try finalbody 臂展开删除 | structural[try_final] |
| M115 | 类体作用域展开删除（派生方向） | class_body_imports_are_derived |
| M116 | 类创建钩子名清单清空 | hook_positions[init_subclass] |
| M117 | metaclass= 检查删除 | hook_positions[metaclass] |
| M118 | model_rebuild 裸 Name 接收者约束删除 | hook_positions[dynamic_receiver] |
| M119 | Attribute 链的别名查找删除 | hook_positions[import_alias] |
| M120 | Name(Load) 的别名查找删除 | hook_positions[from_bare] |
| M121 | Name(Store) 的别名查找删除 | hook_positions[alias_store] |

⚠️ 本文件与跑者同属「CI 相关面」⇒ 必须同时进 `.github/CODEOWNERS` 与
`ci_surface.py` 的 `_CI_SURFACE_PATTERNS`（`ci_mutation_cases*.py` 通配）。
"""

from __future__ import annotations

_CHAIN_GUARD = "app/tests/unit/test_ci_conftest_chain.py"

R14_MUTATIONS: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "M100",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        scanExec(_iterExecNodes(stmt), 0)",
        "        scanExec((), 0)",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[assign_value]"),
        1,
    ),
    (
        "M101",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '    return any(chain == root or chain.startswith(f"{root}.") for root in _DENIED_ROOTS)',
        "    return False",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[assign_value]"),
        1,
    ),
    (
        "M102",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                if alias.asname:",
        "                if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[import_alias]"),
        1,
    ),
    # 第二十六轮评审 M-26-1 重锚：_aliasMap 的 ImportFrom 分支拆成了
    # 内层 `if stmt.level == 0 and stmt.module:`（相对导入占 elif 臂）；
    # 杀绝对 from-import 的模块名赋值 ⇒ from_alias fixture 放行（判别不变）。
    (
        "M103",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "                moduleName: str | None = stmt.module",
        "                moduleName = None",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[from_alias]"),
        1,
    ),
    (
        "M104",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if isinstance(node.ctx, ast.Load):",
        "        if True:",
        (_CHAIN_GUARD + "::test_definitional_constructs_pass[field_named_input]"),
        1,
    ),
    (
        "M105",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        return  # body 不看",
        "        pass",
        (_CHAIN_GUARD + "::test_definitional_constructs_pass[method_body]"),
        1,
    ),
    (
        "M106",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    if node.level - 1 > len(package):",
        "    if True:",
        (_CHAIN_GUARD + "::test_derivation_covers_relative_star_and_any_top_prefix"),
        1,
    ),
    (
        "M107",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '                if alias.name == "*":',
        "                if False:",
        (_CHAIN_GUARD + "::test_derivation_covers_relative_star_and_any_top_prefix"),
        1,
    ),
    (
        "M108",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        if isinstance(node, ast.ImportFrom):",
        (
            "        if isinstance(node, ast.ImportFrom) and "
            '(node.module or "").split(".")[0] in ("app", "scripts"):'
        ),
        (_CHAIN_GUARD + "::test_derivation_covers_relative_star_and_any_top_prefix"),
        1,
    ),
    (
        "M109",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        elif isinstance(node, ast.Import):",
        "        elif False:",
        (_CHAIN_GUARD + "::test_derivation_covers_relative_star_and_any_top_prefix"),
        1,
    ),
    (
        "M110",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):",
        "    elif isinstance(node, ast.AnnAssign):",
        (_CHAIN_GUARD + "::test_derivation_covers_relative_star_and_any_top_prefix"),
        1,
    ),
    (
        "M111",
        "backend",
        "app/utils/ci_conftest_chain.py",
        # 第十八轮重锚：`_classBodyStatements` 复制了同形态（for/yield/Match），
        # 锚点带上 docstring 尾句（`方法体不看` 唯一）作独占落点。
        "    类体单独按同一规则看（类体语句也是 import 时执行）；方法体不看。\n"
        '    """\n'
        "    for node in body:\n"
        "        yield node\n"
        "        if isinstance(node, ast.Match):",
        "    类体单独按同一规则看（类体语句也是 import 时执行）；方法体不看。\n"
        '    """\n'
        "    for node in body:\n"
        "        yield node\n"
        "        if False:",
        (_CHAIN_GUARD + "::test_structural_problem_in_every_branch_arm_is_caught[match_body]"),
        1,
    ),
    (
        "M112",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '            for handler in getattr(node, "handlers", []):\n                yield from _moduleScopeStatements(handler.body)',
        "            for handler in ():\n                yield from _moduleScopeStatements(handler.body)",
        (_CHAIN_GUARD + "::test_structural_problem_in_every_branch_arm_is_caught[handler_body]"),
        1,
    ),
    (
        "M113",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '            yield from _moduleScopeStatements(getattr(node, "orelse", []))',
        "            pass",
        (_CHAIN_GUARD + "::test_structural_problem_in_every_branch_arm_is_caught[try_orelse]"),
        1,
    ),
    (
        "M114",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '            yield from _moduleScopeStatements(getattr(node, "finalbody", []))',
        "            pass",
        (_CHAIN_GUARD + "::test_structural_problem_in_every_branch_arm_is_caught[try_final]"),
        1,
    ),
    (
        "M115",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        elif isinstance(node, ast.ClassDef):",
        "        elif False:",
        (_CHAIN_GUARD + "::test_class_body_imports_are_derived"),
        1,
    ),
    (
        "M116",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '_CLASS_CREATION_HOOK_NAMES = (\n    "__init_subclass__",\n    "__set_name__",\n    "__class_getitem__",\n    "__mro_entries__",\n)',
        "_CLASS_CREATION_HOOK_NAMES = (),",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[init_subclass]"),
        1,
    ),
    (
        "M117",
        "backend",
        "app/utils/ci_conftest_chain.py",
        '        elif kw.arg == "metaclass":',
        "        elif False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[metaclass]"),
        1,
    ),
    (
        "M118",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            and isinstance(value.func.value, ast.Name)",
        "            and True",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[dynamic_receiver]"),
        1,
    ),
    (
        "M119",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "        target = aliases.get(root, root)",
        "        target = root",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[import_alias]"),
        1,
    ),
    (
        "M120",
        "backend",
        "app/utils/ci_conftest_chain.py",
        "            target = aliases.get(node.id, node.id)",
        "            target = node.id",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[from_bare]"),
        1,
    ),
    (
        "M121",
        "backend",
        "app/utils/ci_conftest_chain.py",
        # 第十八轮重锚：拒绝判定统一进 `_isForbidden`（禁止 dunder 段 + 拒绝清单）。
        "        if aliased and _isForbidden(aliased):",
        "        if False:",
        (_CHAIN_GUARD + "::test_hook_positions_are_denied[alias_store]"),
        1,
    ),
]
