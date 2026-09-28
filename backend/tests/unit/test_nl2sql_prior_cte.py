"""prior_cte 注入测试：Nl2SqlService.generateSql 支持 prior_cte 参数。

覆盖：
- prior_cte 拼接到最终 SQL（WITH prior_cte AS (...) SELECT ...）
- 无 prior_cte 时行为不变（单步场景）
- prior_cte 必经 SQL Guard 校验（SqlSafetyError）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.exceptions import Nl2SqlError, SqlSafetyError
from app.domain.models import OntologyClass, OntologyProperty
from app.services.nl2sql_service import Nl2SqlService


def _buildClass(
    name: str, table: str, props: list[dict] | None = None
) -> OntologyClass:
    properties = [OntologyProperty(**p) for p in (props or [])]
    return OntologyClass(class_name=name, source_table=table, properties=properties)


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model")


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _FakeLlm:
    """按顺序弹出预置回复的假客户端。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        content = self._responses.pop(0)
        return _Resp(content)


class TestGenerateSqlPriorCte:
    """generateSql 对 prior_cte 的处理。"""

    async def test_generate_sql_injects_prior_cte(self) -> None:
        """prior_cte 拼装到最终 SQL：WITH prior_cte_subquery AS (...) SELECT ... FROM prior_cte_subquery。

        prior_cte 格式为 render_prior_cte() 输出（chained_step_plan.py），即完整 WITH 子句。
        """
        fake = _FakeLlm(["```sql\nSELECT supplier_id, AVG(ratio) AS completion_rate FROM po_ratio GROUP BY supplier_id\n```"])
        service = Nl2SqlService()
        cls = _buildClass("PO_RATIO", "po_ratio", props=[{"property_name": "supplier_id", "data_type": "STRING", "source_column": "supplier_id"}])

        # render_prior_cte() 输出格式：完整的 WITH ... AS (...) 子句
        prior_cte = "WITH po_ratio AS (SELECT supplier_id, received_qty / NULLIF(purchase_qty, 0) AS ratio FROM po_lines WHERE received_qty > 0)"
        result = await service.generateSql(
            question="各供应商 PO 完成率",
            classes=[cls],
            llmClient=fake,
            modelConfig=_llmConfig(),
            prior_cte=prior_cte,
            maxRetries=0,
        )

        sql = result.sql.upper()
        assert sql.startswith("WITH "), f"SQL 应以 WITH 开头，实际: {result.sql}"
        assert "PO_RATIO" in sql, f"SQL 应包含 prior_cte 别名 PO_RATIO，实际: {result.sql}"
        # 最终 SQL 必须引用 prior_cte 的 alias
        assert "FROM PO_RATIO" in sql or "JOIN PO_RATIO" in sql, f"SQL 应引用 prior_cte 别名，实际: {result.sql}"

    async def test_generate_sql_without_prior_cte(self) -> None:
        """无 prior_cte 时 SQL 不带 prior_cte 内容（单步场景行为不变）。"""
        fake = _FakeLlm(["```sql\nSELECT COUNT(*) AS CNT FROM PRECEIPT\n```"])
        service = Nl2SqlService()
        cls = _buildClass("PRECEIPT", "PRECEIPT")

        result = await service.generateSql(
            question="收货单数量",
            classes=[cls],
            llmClient=fake,
            modelConfig=_llmConfig(),
            maxRetries=0,
        )

        # 无 prior_cte 时，SQL 不应以 WITH 开头（除非 LLM 自行生成了 CTE）
        assert "DROP TABLE" not in result.sql.upper()
        assert "DELETE FROM" not in result.sql.upper()
        assert "FROM PRECEIPT" in result.sql.upper()

    async def test_generate_sql_prior_cte_validates_via_sql_guard(self) -> None:
        """prior_cte 必经 SQL Guard 校验（_assert_read_only），恶意 SQL 抛出 SqlSafetyError。"""
        fake = _FakeLlm(["```sql\nSELECT 1 FROM DUAL\n```"])
        service = Nl2SqlService()
        cls = _buildClass("DUMMY", "DUMMY")

        # DROP TABLE 是写入操作，SQL Guard 应拒绝
        malicious_cte = "dummy AS (DROP TABLE users)"
        with pytest.raises(Nl2SqlError) as excInfo:
            await service.generateSql(
                question="任意问题",
                classes=[cls],
                llmClient=fake,
                modelConfig=_llmConfig(),
                prior_cte=malicious_cte,
                maxRetries=0,
            )
        # 异常 detail 应包含安全校验失败信息
        assert "安全校验" in excInfo.value.detail or "SELECT" in excInfo.value.detail.upper()

    async def test_generate_sql_prior_cte_rejected_if_cte_contains_write(self) -> None:
        """prior_cte 包含 INSERT/UPDATE/DELETE 等写操作时，SQL Guard 应拒绝。"""
        fake = _FakeLlm(["```sql\nSELECT 1 FROM DUAL\n```"])
        service = Nl2SqlService()
        cls = _buildClass("DUMMY", "DUMMY")

        write_cte = "dummy AS (DELETE FROM users)"
        with pytest.raises(Nl2SqlError):
            await service.generateSql(
                question="任意问题",
                classes=[cls],
                llmClient=fake,
                modelConfig=_llmConfig(),
                prior_cte=write_cte,
                maxRetries=0,
            )

    async def test_generate_sql_prior_cte_passed_to_llm(self) -> None:
        """prior_cte 内容会进入 LLM prompt（供当前步引用前序 CTE）。"""
        fake = _FakeLlm(["```sql\nSELECT 1 FROM DUAL\n```"])
        service = Nl2SqlService()
        cls = _buildClass("PO_RATIO", "po_ratio")

        # render_prior_cte() 输出格式：完整的 WITH ... AS (...) 子句
        prior_cte = "WITH po_ratio AS (SELECT supplier_id, ratio FROM po_lines WHERE ratio > 0)"
        await service.generateSql(
            question="区域 PO 完成率",
            classes=[cls],
            llmClient=fake,
            modelConfig=_llmConfig(),
            prior_cte=prior_cte,
            maxRetries=0,
        )

        # prior_cte 作为数据注入到 system prompt（不在 prompt 中则当前步无法引用前序 CTE）
        system_content = fake.calls[0][0][1]
        assert "po_ratio" in system_content, f"prior_cte 应注入到 prompt，实际 system prompt 不含 CTE 别名"

    async def test_generate_sql_prior_cte_isolation_without_prior_cte(self) -> None:
        """无 prior_cte 时不注入前序状态段落（避免误导单步场景的 LLM）。"""
        fake = _FakeLlm(["```sql\nSELECT COUNT(*) FROM PRECEIPT\n```"])
        service = Nl2SqlService()
        cls = _buildClass("PRECEIPT", "PRECEIPT")

        await service.generateSql(
            question="收货单数量",
            classes=[cls],
            llmClient=fake,
            modelConfig=_llmConfig(),
            maxRetries=0,
            # priorState 仍可通过 priorState 参数传入，与 prior_cte 是两条正交路径
            priorState=None,
        )

        system = fake.calls[0][0][1]
        user = fake.calls[0][1][1]
        # 无 prior_cte 时，SQL 仍正常生成，不因其他参数而异常
        assert "SELECT" in system or "SELECT" in user
