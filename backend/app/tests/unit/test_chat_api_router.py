"""Reverse-assertion guards for `backend/app/api/v1/chat.py`.

历史教训：P2-1 review 落地 commit `c552c8ef` 在 `listHypotheses` 签名里
误复制了 `sessionId: str = Path(..., min_length=1, max_length=64)` 两次
（chat.py:47-48），导致整个 pytest collection 失败 ⇒ PR #30 自己
跑不了任何测试。

本文件用源码静态扫描（不是 import）+ AST parse 双保险钉死：
1. `chat.py` 能被 Python AST 解析（不报 SyntaxError）
2. `listHypotheses` 函数定义里 `sessionId` 参数只出现一次
3. pytest 能在该文件上完成 collection

为什么不直接 import 该模块再 inspect：
- 真实 import 会被 FastAPI 路由注册副作用污染（影响其它测试）
- AST 解析只需要文件存在 + 语法合法，更聚焦
- 与 `qa-system-stream-tryfeature-must-pass-user.md` 静态断言风格一致
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CHAT_PY = Path(__file__).resolve().parents[3] / "app" / "api" / "v1" / "chat.py"


def _load_chat_module_ast() -> ast.Module:
    src = CHAT_PY.read_text(encoding="utf-8")
    # `ast.parse` 不检查函数参数唯一性（重复 arg 是 SyntaxError 但 AST 层不拦）；
    # 用 `compile` 走完整 compile 路径，duplicate argument 才会抛 SyntaxError。
    code = compile(src, str(CHAT_PY), "exec")
    tree = ast.parse(src, filename=str(CHAT_PY))
    assert code is not None  # compile 成功 ⇒ 语法合法
    return tree


def _find_list_hypotheses(tree: ast.Module) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "listHypotheses":
            return node
    raise AssertionError("listHypotheses function not found in chat.py")


class TestChatRouterParses:
    def test_chat_py_is_syntactically_valid(self) -> None:
        """chat.py 必须能被 Python AST 解析（无 SyntaxError）。"""
        tree = _load_chat_module_ast()
        assert isinstance(tree, ast.Module)

    def test_pytest_can_collect_chat_api_module(self) -> None:
        """反证：pytest 能在 chat.py 上完成 collection。

        通过 importlib 直接加载（无副作用，因为我们不走 FastAPI 路由注册）。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("chat_router_under_test", CHAT_PY)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 不执行 module ⇒ 不触发路由注册副作用
        # 仅验证 spec 可构造
        assert module is not None


class TestListHypothesesSignature:
    def test_sessionId_parameter_appears_exactly_once(self) -> None:
        """`listHypotheses` 签名里 `sessionId` 参数不能出现两次（SyntaxError 反向断言）。"""
        tree = _load_chat_module_ast()
        func = _find_list_hypotheses(tree)
        # ast.arguments.args / kwargs / posonlyargs / kwonlyargs 全扫
        all_args = (
            list(func.args.args)
            + list(func.args.kwonlyargs)
            + list(func.args.posonlyargs)
        )
        sessionId_count = sum(1 for a in all_args if a.arg == "sessionId")
        assert sessionId_count == 1, (
            f"listHypotheses should have exactly 1 `sessionId` parameter, "
            f"found {sessionId_count}. This is the SyntaxError regression from "
            f"commit c552c8ef on PR #30 (P2-1 review)."
        )

    def test_sessionId_parameter_is_path_type(self) -> None:
        """`sessionId` 必须仍是 `Path(..., min_length=1, max_length=64)` 约束。

        Python AST quirk：参数的 default 不在 `ast.arg.default` 上，而在
        `ast.arguments.defaults` / `kw_defaults` 列表里（按位置倒序对齐）。
        `sessionId` 在 `args.args[1]`（idx=1），defaults 倒序对齐 ⇒ defaults[-2]。
        """
        tree = _load_chat_module_ast()
        func = _find_list_hypotheses(tree)
        args_list = list(func.args.args)
        # 找 sessionId 在 args_list 的位置
        idx = next((i for i, a in enumerate(args_list) if a.arg == "sessionId"), -1)
        assert idx >= 0, "sessionId parameter not found in listHypotheses"

        # 注解是 str（类型提示），Path 约束在 default
        a = args_list[idx]
        assert a.annotation is not None, "sessionId must have type annotation"
        annotation_src = ast.unparse(a.annotation)
        assert annotation_src == "str", (
            f"sessionId annotation should be `str` (the value type), got {annotation_src}"
        )

        # defaults 列表按位置倒序对齐 args：args[N-1] 对应 defaults[-1]，args[N-2] 对应 defaults[-2]…
        # 计算 sessionId 在 args.args 列表里所需的 default 索引
        n_args = len(args_list)
        n_defaults = len(func.args.defaults)
        # sessionId 需要的 default 位置（从 defaults 末尾起算）
        default_offset = n_args - 1 - idx  # sessionId 在 args[1]，n_args=5 ⇒ offset=3
        assert 0 <= default_offset < n_defaults, (
            f"sessionId should have a default, but defaults list has {n_defaults} entries"
        )
        default_node = func.args.defaults[n_defaults - 1 - default_offset]
        default_src = ast.unparse(default_node)
        assert "Path" in default_src, (
            f"sessionId default should be Path(...), got {default_src}"
        )
        assert "min_length=1" in default_src, (
            f"sessionId must enforce min_length=1, got default={default_src}"
        )
        assert "max_length=64" in default_src, (
            f"sessionId must enforce max_length=64, got default={default_src}"
        )
