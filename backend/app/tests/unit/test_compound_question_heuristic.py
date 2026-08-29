"""并列复合问题启发式单元测试（2026-08-17 真实回归）。

无显式分步信号但语义多步的问题（如"查询 A、查询 B、查询 C"）应当被识别为多步。
启发式严格守约：必须 ≥2 段、每段含至少 1 个查询动词、且不含禁用短语。
"""

from __future__ import annotations

import pytest

from app.services.chat_service import _looks_like_compound_question


class TestCompoundQuestionHeuristic:
    """_looks_like_compound_question：并列复合问题检测。"""

    @pytest.mark.parametrize("question", [
        # 三段并列（用户实际场景，2026-08-17 bug）：头+列表模式
        "查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量",
        # 加号分隔
        "查询 A + 查询 B + 查询 C",
        # 顿号分隔（多动词并列）
        "统计供应商数量、计算订单总额、分析采购趋势",
        # "和"连接（多动词）
        "查询供应商 A 和 查询供应商 B 的收货数量",
        # 三段头+列表
        "查询 A, B, C 的差异",
    ])
    def test_compound_question_triggered(self, question: str) -> None:
        assert _looks_like_compound_question(question) is True, f"应触发：{question}"

    @pytest.mark.parametrize("question", [
        # 单条查询（含连接词但只有 1 个查询动词）
        "对比 2024 和 2025 年的销售额",
        "比较各年份的营收",
        # 排序修饰（"按金额降序"不是查询动词）
        "统计各供应商的收货数量，按金额降序",
        "查询各门店营收，按月度分组",
        # 单动词 + 连接词连接实体（"和"右侧无动词）
        "查询 A 和 B 的差异",
        "查询供应商 A 和供应商 B",
        # 单条无连接词
        "查询最近一周的采购订单",
        "统计总订单数",
        "你好",
        "什么是销售额",
    ])
    def test_single_question_not_triggered(self, question: str) -> None:
        assert _looks_like_compound_question(question) is False, f"应不触发：{question}"

    @pytest.mark.parametrize("question", [
        "用一条 SQL 查询 A 和 B",
        "一条 SQL 解决 A 和 B",
        "不要拆分，分别查 A 和 B",  # 虽然含"和"但禁用短语
        "单条查询 A 和 B",
    ])
    def test_deny_phrases_skip_heuristic(self, question: str) -> None:
        """用户明确不要拆分时跳过启发式（让单条 SQL 路径处理）。"""
        # 注：含"和"但禁用短语优先 → 不触发
        assert _looks_like_compound_question(question) is False, f"应被禁用短语拦截：{question}"

    def test_empty_question_not_triggered(self) -> None:
        assert _looks_like_compound_question("") is False
        assert _looks_like_compound_question("   ") is False