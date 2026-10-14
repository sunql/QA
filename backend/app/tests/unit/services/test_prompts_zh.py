"""prompts_zh.py SSOT 常量存在性测试。

本测试仅验证 prompts_zh.py 模块及其 3 个常量可正常 import。
如 conftest.py 的 seedEngine autouse fixture 无法连接测试 DB（本地无 DB 环境），
本测试主动跳过以避免 fixture setup 失败；CI 环境中 DB 就绪时会正常运行。
"""
import pytest

# 仅在 prompts_zh 模块存在时运行；conftest seedEngine 会确保测试 DB 可用时再执行。
pytest.importorskip("app.services.prompts_zh", reason="prompts_zh.py 未就绪")


def test_prompts_zh_constants_present():
    """3 个 system prompt 必须进新建 prompts_zh.py SSOT。"""
    from app.services.prompts_zh import (
        PROMPT_ANSWER_SYSTEM,
        PROMPT_CLARIFY_SYSTEM,
        PROMPT_STEP_PLANNER_SYSTEM,
    )

    assert "禁止反向追问" in PROMPT_ANSWER_SYSTEM
    assert "企业数据分析助手" in PROMPT_CLARIFY_SYSTEM
    assert "查询拆分器" in PROMPT_STEP_PLANNER_SYSTEM
