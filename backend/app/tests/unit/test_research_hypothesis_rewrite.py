"""A2 (2026-10-08)：研究侧假设 SQL 拦截后单次 LLM 重写。

背景：refactor 后守卫扩 LIMIT/INTERVAL，拦到违规时**只短路**写 error 进 finding，
LLM 从不被反馈，导致 finding.rows=[] 报告 data 章节无 chart。本模块提供
``rewriteHypothesisSqlWithHint``：把 hint 当 prompt 喂回 LLM，让 LLM 重写 SQL
（硬限 1 次重试，避免 docstring 警告的"反复重写"风险）。
"""
from __future__ import annotations

import pytest

from app.infrastructure.llm.base_client import LlmMessage
from app.services.research_hypothesis_adapter import rewriteHypothesisSqlWithHint


class _FakeLlm:
    """最简单的假 LLM：返回预设 raw 文本。"""

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls: list[list[LlmMessage]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return self._raw


_ORACLE_LIMIT_SQL = (
    "SELECT rcv_month, SUM(rcv_qty) AS total "
    "FROM DWD_GOODS_RECEIPT_DTL "
    "GROUP BY rcv_month ORDER BY rcv_month DESC LIMIT 12"
)
_ORACLE_INTERVAL_SQL = (
    "SELECT supplier_code, supplier_name, "
    "SUM(CASE WHEN r.rcv_month = DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month') "
    "THEN r.rcv_qty ELSE 0 END) AS qty_m1 "
    "FROM DWD_GOODS_RECEIPT_DTL r GROUP BY supplier_code, supplier_name"
)
_ORACLE_HINT = (
    "上一轮生成的 SQL 包含 oracle dialect 不允许的函数/语法，已被守卫拦截。\n"
    "请重新生成 SQL，**只用**目标 dialect 的原生写法，**不要**用以下函数：\n"
    "- LIMIT：用 FETCH FIRST N ROWS ONLY（12c+）或 ROWNUM 子查询（11g）替代"
)


@pytest.mark.asyncio
async def test_rewrite_LLM_返回_FETCH_FIRST_取代_LIMIT_通过() -> None:
    """happy path：LLM 看到 hint 后把 LIMIT 12 改写成 FETCH FIRST 12 ROWS ONLY。
    改写后必须通过 isValidVerificationSql（首 token SELECT + 无 DML/DDL）。
    """
    rewritten_sql = (
        "SELECT rcv_month, SUM(rcv_qty) AS total\n"
        "FROM DWD_GOODS_RECEIPT_DTL\n"
        "GROUP BY rcv_month\n"
        "ORDER BY rcv_month DESC\n"
        "FETCH FIRST 12 ROWS ONLY"
    )
    fake = _FakeLlm(f"```sql\n{rewritten_sql}\n```")
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    assert result is not None
    assert "FETCH FIRST" in result
    assert "LIMIT" not in result.upper()
    # 喂回的 prompt 必须包含原 SQL + hint
    user_msg = fake.calls[0][1].content
    assert _ORACLE_LIMIT_SQL in user_msg
    assert _ORACLE_HINT in user_msg


@pytest.mark.asyncio
async def test_rewrite_LLM_返回_ADD_MONTHS_取代_INTERVAL_通过() -> None:
    """happy path：LLM 看到 INTERVAL 提示后改用 ADD_MONTHS。"""
    rewritten_sql = (
        "SELECT supplier_code, supplier_name,\n"
        "  SUM(CASE WHEN r.rcv_month = ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1)\n"
        "           THEN r.rcv_qty ELSE 0 END) AS qty_m1\n"
        "FROM DWD_GOODS_RECEIPT_DTL r\n"
        "GROUP BY supplier_code, supplier_name"
    )
    fake = _FakeLlm(f"```sql\n{rewritten_sql}\n```")
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_INTERVAL_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    assert result is not None
    assert "ADD_MONTHS" in result
    assert "INTERVAL" not in result.upper()


@pytest.mark.asyncio
async def test_rewrite_LLM_返_空_返_None() -> None:
    """LLM 返空（无内容）→ 返 None（让 caller 走 fallback 不重写）。"""
    fake = _FakeLlm("")
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    assert result is None


@pytest.mark.asyncio
async def test_rewrite_LLM_返_无_fence_返_None() -> None:
    """LLM 返裸文本（无 ```sql fence）→ 返 None（不冒险塞未授权 SQL 进 finding）。"""
    fake = _FakeLlm("SELECT 1 FROM dual WHERE LIMIT 12")  # 没 fence
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    assert result is None


@pytest.mark.asyncio
async def test_rewrite_LLM_返_非法_SQL_返_None() -> None:
    """LLM 改写后仍是禁语法（fence 内 SQL 通过 fence 剥离但不通过 isValidVerificationSql）→ 返 None。
    防 LLM 在 fence 里又写 `LIMIT 12`（虽然 hint 说了不要）。
    """
    fake = _FakeLlm(
        "```sql\nSELECT 1 FROM dual ORDER BY x LIMIT 12\n```"
    )
    # fence 剥后内容 = "SELECT 1 FROM dual ORDER BY x LIMIT 12"，但仍含 LIMIT
    # → 重写器应**不**信任 LLM 输出，直接返 None（让 caller 走 fallback）
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    # 关键：绝不能把 `LIMIT 12` 漏出来
    assert result is None or "LIMIT" not in result.upper()


@pytest.mark.asyncio
async def test_rewrite_LLM_抛_异常_返_None_不冒泡() -> None:
    """LLM 抛 RuntimeError / 超时 → rewriter 返 None，不冒泡。
    契约：rewrite 是 best-effort，失败不能影响主流程。
    """

    class _BoomLlm:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("LLM 上游挂了")

    result = await rewriteHypothesisSqlWithHint(
        _BoomLlm(), _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    assert result is None


@pytest.mark.asyncio
async def test_rewrite_prompt_注入_dialect_名_便于_LLM_认知() -> None:
    """system prompt 必须明确说出目标 dialect 名（oracle / mysql / postgresql），
    避免 LLM 用训练语料里更常见的 MySQL 风格。
    """
    fake = _FakeLlm("```sql\nSELECT 1 FROM dual\n```")
    await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    sys_msg = fake.calls[0][0].content.lower()
    assert "oracle" in sys_msg
    # user message 也要包含 hint（违规原因）
    user_msg = fake.calls[0][1].content
    assert "FETCH FIRST" in user_msg or "ROWNUM" in user_msg


@pytest.mark.asyncio
async def test_rewrite_未知_datasourceType_拒绝_返_None() -> None:
    """2026-10-08 安全 review：未知 datasourceType 必须拒绝，不能跳过 checkSqlDialect。

    攻击场景：datasourceType="mssql"（或任何 DataSourceType enum 外的值）→ 旧实现走
    `except ValueError: return sql` 分支跳过第三层防御，攻击者可以用跨 dialect 函数
    （如 `DROP TABLE`）绕过 dialect-specific banned-functions 检查。
    """
    fake = _FakeLlm(
        "```sql\nSELECT 1; DROP TABLE users; --\n```"  # 即使 fence 合法也必须拒
    )
    result = await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="mssql"
    )
    assert result is None


@pytest.mark.asyncio
async def test_rewrite_user_prompt_包含_END_sentinel_防_injection() -> None:
    """2026-10-08 安全 review：原 SQL 块之后必须有结构性终止 sentinel，
    防 adversarial originalSql（"```" 提早闭合 fence + 注入指令）越过第一层 fence 校验。

    原 SQL 是 LLM 自己前一轮生成的，注入风险低（不像用户输入），但防御深度原则要求
    "显式边界"而不是依赖 LLM 配合。
    """
    fake = _FakeLlm("```sql\nSELECT 1 FROM dual\n```")
    await rewriteHypothesisSqlWithHint(
        fake, _ORACLE_LIMIT_SQL, _ORACLE_HINT, datasourceType="oracle"
    )
    user_msg = fake.calls[0][1].content
    assert "END_OF_ORIGINAL_SQL" in user_msg, (
        "原 SQL fence 后必须有终止 sentinel，否则 adversarial LLM 写 ``` 闭合后会注入指令"
    )
