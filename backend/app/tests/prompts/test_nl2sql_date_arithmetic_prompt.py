"""NL2SQL prompt 必须注入 dateArithmeticRule（2026-10-08 补）。

研究 finding 实锤：LLM 在 Oracle 上写出 `DATEADD(month, -12, CURRENT_DATE)`
→ ORA-00904 → 0 行 → 报告 data 章节无 chart 块。

`SqlDialect.dateArithmeticRule` 是 SSOT（不在 prompt 里硬编码）：
- 三个 dialect 必须有非空规则
- `_buildSystemPrompt` 必须把它当作第 11+ 条规则拼到 system prompt
- 不允许在 prompt 文本里另起一套日期算术规则（否则改一处忘改另一处）
"""
from __future__ import annotations

from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import _SQL_DIALECTS
from app.services.nl2sql_prompts import _buildSystemPrompt


def _empty_schema() -> str:
    return "（当前没有可用表结构，请判断问题并直接说明无法回答）"


def test_Oracle_prompt_必须含_ADD_MONTHS_禁用_DATEADD() -> None:
    """Oracle dialect 拼出来的 prompt 必须含 `ADD_MONTHS` 且**显式禁用** `DATEADD`。

    防 LLM 凭训练语料默认用 SQL Server 语法；防 prompt 维护者另起规则与
    `SqlDialect.dateArithmeticRule` 漂移。
    """
    dialect = _SQL_DIALECTS[DataSourceType.ORACLE]
    prompt = _buildSystemPrompt(
        schemaText=_empty_schema(),
        dialect=dialect,
        schemaPrefix="ZJTH.",
    )
    assert "ADD_MONTHS" in prompt, (
        "Oracle prompt 必须含 ADD_MONTHS 提示词，否则 LLM 会默认用 DATEADD 报错 ORA-00904"
    )
    assert "DATEADD" in prompt, (
        "Oracle prompt 必须**显式禁用** DATEADD，仅靠推荐函数不足以让 LLM 改习惯"
    )


def test_Oracle_prompt_必须显式禁_MySQL_函数_且给_TO_CHAR_示例() -> None:
    """2026-10-08 补：研究复盘实证 LLM 在 19c Oracle 上仍写 MySQL 语法
    `DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m')` → ORA-00907。
    仅禁 DATEADD 不够——必须禁 DATE_FORMAT/DATE_SUB/INTERVAL/CURDATE 并给正确模板。
    """
    dialect = _SQL_DIALECTS[DataSourceType.ORACLE]
    prompt = _buildSystemPrompt(
        schemaText=_empty_schema(),
        dialect=dialect,
        schemaPrefix="ZJTH.",
    )
    for banned in ("DATE_FORMAT", "DATE_SUB", "DATE_ADD", "INTERVAL", "CURDATE"):
        assert banned in prompt, (
            f"Oracle prompt 必须显式禁用 MySQL 函数 {banned}（让 LLM 看不到就还会用）"
        )
    # 必须有正确示例
    assert "TO_CHAR" in prompt and "ADD_MONTHS" in prompt, (
        "Oracle prompt 必须含 TO_CHAR + ADD_MONTHS 正确示例（让 LLM 有模板抄）"
    )


def test_PostgreSQL_prompt_必须含_INTERVAL_禁用_DATEADD() -> None:
    dialect = _SQL_DIALECTS[DataSourceType.POSTGRESQL]
    prompt = _buildSystemPrompt(
        schemaText=_empty_schema(),
        dialect=dialect,
        schemaPrefix=None,
    )
    assert "INTERVAL" in prompt
    assert "DATEADD" in prompt


def test_MySQL_prompt_必须含_DATE_SUB_或_DATE_ADD_禁用_DATEADD() -> None:
    dialect = _SQL_DIALECTS[DataSourceType.MYSQL]
    prompt = _buildSystemPrompt(
        schemaText=_empty_schema(),
        dialect=dialect,
        schemaPrefix=None,
    )
    assert "DATE_SUB" in prompt or "DATE_ADD" in prompt
    assert "DATEADD" in prompt
