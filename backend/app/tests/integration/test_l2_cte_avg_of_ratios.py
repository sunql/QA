"""Task 2.4 L2 CTE 端到端集成测试（SDD 模式）。

测试 L2 路径生成 PO 完成率 CTE SQL，真实 PG 跑通。

覆盖场景：
- LLM 生成含 WITH ... AS ( SELECT ... AVG(ratio) ... ) 的 CTE SQL
- SQL 在 PG 沙箱执行，验证 schema 和数值范围

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:pass@localhost:5433/qa_metadata_test \
        uv run pytest app/tests/integration/test_l2_cte_avg_of_ratios.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from app.domain.models import OntologyClass, OntologyProperty
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.services.nl2sql_service import Nl2SqlService


# ---------------------------------------------------------------------------
# Fake LLM：按阶段返回预设 SQL（与 unit test 的 _FakeLlm 保持一致）
# ---------------------------------------------------------------------------


class _FakeLlm:
    """按顺序弹出预置回复的假客户端，记录每次调用的消息与 kwargs。

    签名与 BaseLlmClient.complete 一致：messages: list[LlmMessage]。
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []
        self.kwargsCalls: list[dict] = []

    async def complete(
        self, messages: list[LlmMessage], *, model: str | None = None,
        temperature: float | None = None, maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        self.calls.append([(m.role, m.content) for m in messages])
        self.kwargsCalls.append(dict(kwargs))
        content = self._responses.pop(0)
        return LlmResponse(
            content=content,
            modelName=model or "test-model",
            promptTokens=10,
            completionTokens=5,
            totalTokens=15,
        )


# ---------------------------------------------------------------------------
# Test data：PG 沙箱 mock po_lines 表（模拟 THBI Oracle po_lines schema）
# ---------------------------------------------------------------------------

_PO_LINES_DDL = """
DROP TABLE IF EXISTS po_lines_sandbox;
CREATE TABLE po_lines_sandbox (
    id                  SERIAL PRIMARY KEY,
    supplier_id         VARCHAR(50)  NOT NULL,
    purchase_qty        NUMERIC(18,4) NOT NULL DEFAULT 0,
    received_qualified_qty NUMERIC(18,4) NOT NULL DEFAULT 0
);
-- 正常行：received_qualified_qty > 0
INSERT INTO po_lines_sandbox (supplier_id, purchase_qty, received_qualified_qty) VALUES
    ('S001', 100, 80),   -- 80%
    ('S001', 200, 160),  -- 80%
    ('S002', 150, 45),   -- 30%
    ('S002',  50, 15),   -- 30%
    ('S003', 300, 300);  -- 100% (刚好收完)
-- 边界：purchase_qty = 0 → NULLIF 避免除零
INSERT INTO po_lines_sandbox (supplier_id, purchase_qty, received_qualified_qty) VALUES
    ('S004', 0, 0);
-- 边界：received_qualified_qty = 0 → 被过滤，不纳入 AVG
INSERT INTO po_lines_sandbox (supplier_id, purchase_qty, received_qualified_qty) VALUES
    ('S005', 100, 0);
"""


# ---------------------------------------------------------------------------
# Sandbox cleanup fixture（autouse）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _cleanup_po_lines_sandbox(dbSession):
    """每个测试前后建表/清表，保证测试隔离。

    使用独立的 po_lines_sandbox 表（不在业务表列表中），不干扰其他集成测试。
    """
    from app.infrastructure import database as dbModule

    factory = dbModule.getSessionFactory()
    async with factory() as session:
        # 建表（幂等）
        for stmt in _PO_LINES_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await session.execute(text(stmt))
        await session.commit()

    yield

    # 测试后清表（保留表结构）
    async with factory() as session:
        await session.execute(text("DELETE FROM po_lines_sandbox"))
        await session.execute(text("ALTER SEQUENCE po_lines_sandbox_id_seq RESTART WITH 1"))
        await session.commit()


# ---------------------------------------------------------------------------
# Ontology class fixture（模拟 THBI po_lines 实体）
# ---------------------------------------------------------------------------

def _make_po_lines_class() -> OntologyClass:
    """构建 po_lines 本体类（对应 THBI po_lines 表）。"""
    return OntologyClass(
        class_name="po_lines",
        class_alias="采购订单行",
        source_table="po_lines_sandbox",
        description="采购订单行明细（Task 2.4 mock 表）",
        properties=[
            OntologyProperty(
                property_name="supplier_id",
                property_alias="供应商编号",
                data_type="VARCHAR",
                source_column="supplier_id",
            ),
            OntologyProperty(
                property_name="purchase_qty",
                property_alias="采购数量",
                data_type="NUMERIC",
                source_column="purchase_qty",
            ),
            OntologyProperty(
                property_name="received_qualified_qty",
                property_alias="已收合格数量",
                data_type="NUMERIC",
                source_column="received_qualified_qty",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Test 1：L2 CTE AVG of ratios（完整验证）
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_l2_generates_cte_sql_for_avg_of_ratios(client, dbSession):
    """L2 路径生成 PO 完成率 CTE SQL，真实 PG + 假 LLM 跑通。

    流程：
    1. _cleanup_po_lines_sandbox 建表并灌入 mock 数据
    2. 构造 po_lines 本体类 + fake LLM（返回预设 CTE SQL）
    3. 调 Nl2SqlService.generateSql()
    4. 验证返回的 SQL 含 WITH + AVG + po_lines 引用
    5. 在 PG 跑 SQL，验证结果 schema 和 completion_rate 范围 [0, 100]
    """
    cls = _make_po_lines_class()
    nl2sql = Nl2SqlService()
    llm = _FakeLlm(
        responses=[
            # Plan 阶段回复（不被本测试直接验证，但 generateSql 需要）
            '{"selectedClasses":["po_lines"],"selectedProperties":["supplier_id"],'
            '"aggregations":[{"function":"AVG","property":"supplier_id",'
            '"alias":"completion_rate","formula":'
            '"WITH line_ratios AS (SELECT supplier_id, '
            'received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio '
            'FROM po_lines_sandbox WHERE received_qualified_qty > 0) '
            'SELECT supplier_id, AVG(ratio) AS completion_rate '
            'FROM line_ratios GROUP BY supplier_id"}],'
            '"groupBy":["supplier_id"],"sortBy":[],"joins":[],"conditions":[]}',
            # SQL 阶段回复
            """```sql
WITH line_ratios AS (
  SELECT supplier_id,
         received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio
  FROM po_lines_sandbox
  WHERE received_qualified_qty > 0
)
SELECT supplier_id,
       AVG(ratio) * 100 AS completion_rate
FROM line_ratios
GROUP BY supplier_id
```""",
        ]
    )
    model_config = SimpleNamespace(model_name="test-model")

    result = await nl2sql.generateSql(
        question="供应商 PO 完成率",
        classes=[cls],
        llmClient=llm,
        modelConfig=model_config,
        datasourceType="POSTGRESQL",
    )

    sql = result.sql
    # 1. 验证 SQL 结构：WITH CTE + AVG 聚合
    assert "WITH" in sql.upper(), f"Expected CTE (WITH), got: {sql}"
    assert "AVG(" in sql.upper(), f"Expected AVG aggregation, got: {sql}"
    # 2. 验证引用了 po_lines_sandbox 表
    assert "po_lines_sandbox" in sql.lower(), f"Expected po_lines_sandbox reference, got: {sql}"
    # 3. 验证有 NULLIF（防除零）
    assert "NULLIF" in sql.upper(), f"Expected NULLIF for division-by-zero guard, got: {sql}"

    # 4. 在 PG 沙箱执行 SQL
    exec_result = await dbSession.execute(text(sql))
    rows = exec_result.fetchall()
    assert len(rows) > 0, f"Expected rows, got empty result. SQL: {sql}"

    # 5. 验证结果 schema：每行有 supplier_id + completion_rate
    for row in rows:
        assert hasattr(row, "_mapping"), f"Row has no _mapping: {row}"
        data = row._mapping
        assert "supplier_id" in data, f"Missing supplier_id in row: {data}"
        assert "completion_rate" in data, f"Missing completion_rate in row: {data}"

    # 6. 验证数值范围：completion_rate 应在 [0, 100]（已乘 100）
    for row in rows:
        data = row._mapping
        rate = data["completion_rate"]
        if rate is not None:
            assert 0 <= float(rate) <= 100, (
                f"completion_rate {rate} out of range [0, 100]. Row: {data}"
            )

    # 7. 验证 S001 / S002 / S003 有结果（各自两条明细行合并后）
    supplier_ids = {row._mapping["supplier_id"] for row in rows}
    assert "S001" in supplier_ids, f"S001 should have result. Got: {supplier_ids}"
    assert "S002" in supplier_ids, f"S002 should have result. Got: {supplier_ids}"
    assert "S003" in supplier_ids, f"S003 should have result. Got: {supplier_ids}"
    # S004 / S005 应无结果（purchase_qty=0 或 received_qualified_qty=0）
    assert "S004" not in supplier_ids, "S004 (purchase_qty=0) should not appear"
    assert "S005" not in supplier_ids, "S005 (received_qualified_qty=0) should not appear"

    # 8. 验证具体 completion_rate 值（补强断言）
    rate_by_supplier = {row._mapping["supplier_id"]: row._mapping["completion_rate"] for row in rows}
    assert abs(float(rate_by_supplier["S001"]) - 80.0) < 0.01, (
        f"S001 expected 80%, got {rate_by_supplier['S001']}"
    )
    assert abs(float(rate_by_supplier["S002"]) - 30.0) < 0.01, (
        f"S002 expected 30%, got {rate_by_supplier['S002']}"
    )
    assert abs(float(rate_by_supplier["S003"]) - 100.0) < 0.01, (
        f"S003 expected 100%, got {rate_by_supplier['S003']}"
    )


# ---------------------------------------------------------------------------
# Test 2：多聚合别名场景（验证别名共存不互相干扰）
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_l2_cte_sql_with_multiple_aggregation_aliases(client, dbSession):
    """多聚合别名场景：CTE 内算 ratio，最终 SELECT 用 AVG。

    与 Test 1 的区别：SQL 使用更简洁的 CTE 别名（r 而非 line_ratios），
    验证 NL2SqlService 能处理不同的 CTE 别名命名。
    断言具体 completion_rate 值，确保 AVG 计算正确。
    """
    cls = _make_po_lines_class()
    nl2sql = Nl2SqlService()
    llm = _FakeLlm(
        responses=[
            '{"selectedClasses":["po_lines"],"selectedProperties":["supplier_id"],'
            '"aggregations":[{"function":"AVG","property":"supplier_id",'
            '"alias":"completion_rate","formula":'
            '"WITH r AS (SELECT supplier_id, received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio '
            'FROM po_lines_sandbox WHERE received_qualified_qty > 0) '
            'SELECT supplier_id, AVG(ratio) AS completion_rate FROM r GROUP BY supplier_id"}],'
            '"groupBy":["supplier_id"],"sortBy":[],"joins":[],"conditions":[]}',
            """```sql
WITH r AS (
  SELECT supplier_id,
         received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio
  FROM po_lines_sandbox
  WHERE received_qualified_qty > 0
)
SELECT supplier_id,
       AVG(ratio) * 100 AS completion_rate
FROM r
GROUP BY supplier_id
```""",
        ]
    )
    model_config = SimpleNamespace(model_name="test-model")

    result = await nl2sql.generateSql(
        question="各供应商 PO 完成率是多少",
        classes=[cls],
        llmClient=llm,
        modelConfig=model_config,
        datasourceType="POSTGRESQL",
    )

    sql = result.sql
    assert "WITH" in sql.upper(), f"Expected CTE, got: {sql}"
    assert "AVG(ratio)" in sql.upper() or "AVG(RATIO)" in sql.upper(), (
        f"Expected AVG(ratio), got: {sql}"
    )

    # 执行验证
    exec_result = await dbSession.execute(text(sql))
    rows = exec_result.fetchall()
    assert len(rows) == 3, f"Expected 3 suppliers (S001,S002,S003), got {len(rows)}: {rows}"

    # 补强断言：验证具体 completion_rate 值
    rate_by_supplier = {row._mapping["supplier_id"]: row._mapping["completion_rate"] for row in rows}
    assert "S001" in rate_by_supplier, f"S001 missing. Got: {rate_by_supplier}"
    assert "S002" in rate_by_supplier, f"S002 missing. Got: {rate_by_supplier}"
    assert "S003" in rate_by_supplier, f"S003 missing. Got: {rate_by_supplier}"

    assert abs(float(rate_by_supplier["S001"]) - 80.0) < 0.01, (
        f"S001 expected 80%, got {rate_by_supplier['S001']}"
    )
    assert abs(float(rate_by_supplier["S002"]) - 30.0) < 0.01, (
        f"S002 expected 30%, got {rate_by_supplier['S002']}"
    )
    assert abs(float(rate_by_supplier["S003"]) - 100.0) < 0.01, (
        f"S003 expected 100%, got {rate_by_supplier['S003']}"
    )
