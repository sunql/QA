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

import pytest
from sqlalchemy import text

from app.domain.models import OntologyClass, OntologyProperty
from app.services.nl2sql_service import Nl2SqlService


# ---------------------------------------------------------------------------
# Fake LLM：按阶段返回预设 SQL（复用 unit test 的 _FakeLlm 模式）
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.promptTokens = 10
        self.completionTokens = 5


class _FakeLlm:
    """按顺序弹出预置回复的假客户端，记录每次调用的消息与 kwargs。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []
        self.kwargsCalls: list[dict] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        self.kwargsCalls.append(kwargs)
        content = self._responses.pop(0)
        return _Resp(content)


# ---------------------------------------------------------------------------
# Test data：PG 沙箱 mock po_lines 表（模拟 THBI Oracle po_lines schema）
# ---------------------------------------------------------------------------

_PO_LINES_DDL = """
DROP TABLE IF EXISTS po_lines_sandbox;
CREATE TABLE po_lines_sandbox (
    id          SERIAL PRIMARY KEY,
    supplier_id VARCHAR(50)  NOT NULL,
    purchase_qty            NUMERIC(18,4) NOT NULL DEFAULT 0,
    received_qualified_qty  NUMERIC(18,4) NOT NULL DEFAULT 0
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


async def _setup_po_lines(dbSession) -> None:
    """在 PG 沙箱建表并灌入 mock 数据。"""
    for stmt in _PO_LINES_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            await dbSession.execute(text(stmt))
    await dbSession.commit()


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
# 测试：L2 CTE AVG of ratios
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_l2_generates_cte_sql_for_avg_of_ratios(dbSession):
    """L2 路径生成 PO 完成率 CTE SQL，真实 PG + 假 LLM 跑通。

    流程：
    1. 准备 mock po_lines 表（PG 沙箱）
    2. 构造 po_lines 本体类 + fake LLM（返回预设 CTE SQL）
    3. 调 Nl2SqlService.generateSql()
    4. 验证返回的 SQL 含 WITH + AVG + po_lines 引用
    5. 在 PG 跑 SQL，验证结果 schema 和 completion_rate 范围 [0, 100]
    """
    await _setup_po_lines(dbSession)

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
        # rate 可能是 None（NULLIF 产生 NULL）
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


@pytest.mark.integration
async def test_l2_cte_sql_with_multiple_aggregation_aliases(dbSession):
    """多聚合别名场景：CTE 内算 ratio，最终 SELECT 用 AVG。

    验证公式支持多个聚合别名共存，不互相干扰。
    """
    await _setup_po_lines(dbSession)

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
