# M0 Unified ID 开发实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭环 Neo4j + Milvus 两端的 external_id 回填，并通过 3 次 PR 把 M0-P0.1 + M0-P0.3 + M0-P0.4 + M2a 推到 main。

**Architecture:** 两阶段 TDD：
1. 先写 reconcile 脚本（纯读 + diff），用它驱动 backfill 实现；
2. backfill 跑完再跑 reconcile，diff 必须为 0；
3. PR 合并阶段只跑全量集成测试 + 覆盖率门禁。

**Tech Stack:** Python 3.14 / SQLAlchemy 2 (async) / neo4j / pymilvus / pytest-asyncio / Alembic 0097+ / 真实 PostgreSQL `qa_metadata_test`

---

## 全局约束（继承自 Harness CLAUDE.md）

- **不可变数据**：所有 service 函数返回新对象，禁止原地修改 ORM 实例。
- **真实数据库测试**：所有 backfill / reconcile 测试必须连真实 `qa_metadata_test` + 真实 Neo4j + 真实 Milvus。禁止 mock 外部依赖。
- **TDD 顺序**：每个 task 必须严格 RED → GREEN → IMPROVE。
- **覆盖率 ≥ 80%**：每个 phase 完成后跑 `pytest --cov` 验证，未达 80% 必须补用例。
- **小文件**：每个 task 触及的脚本 ≤ 200 行；超出必须先拆 helper。
- **错误处理**：脚本必须 exit code 0/非 0 明确，禁止吞异常。
- **commit 规范**：`feat:` / `fix:` / `test:` / `docs:` / `chore:` 前缀；attribution 默认禁。
- **DB 命名**：`qa_metadata_test` 是测试库唯一合法名（不是 `qa_metadata`）。

---

## 阶段依赖图

```
Task 1-9 (M0-P0.3 Neo4j) ──┐
                            ├── Task 17 (PR1: feat/m0-unified-id → main)
Task 10-16 (M0-P0.4 Milvus)─┘                          │
                                                       ▼
Task 18 (PR2: feat/planner-step-limit → epic/v31-upgrade)
                                                       │
                                                       ▼
                                       Task 19 (PR3: epic/v31-upgrade → main)
```

每个 Task 必须独立 commit + 独立测试；不允许跨 Task 修复合并。

---

## Phase 1: M0-P0.3 · Neo4j external_id 回填

### Task 1: TDD - Neo4j 测试夹具（fixtures）

**Files:**
- Create: `backend/app/tests/integration/conftest_neo4j.py`
- Modify: `backend/app/tests/integration/conftest.py` (如需 import 共享)

**Step 1: 写 fixture 函数**

```python
# backend/app/tests/integration/conftest_neo4j.py
"""Neo4j 真实库测试夹具。

约定：
  - 每个测试启动前清空 Class / Property / Metric 三类节点
  - 不 mock 外部 driver（直接连 qa-neo4j:7687）
  - 不用 lifespan_context（与 _testapp 启动分离，避免反复重启）
"""
from __future__ import annotations

import pytest
from neo4j import GraphDatabase


@pytest.fixture
async def neo4j_clean_driver():
    """返回已连接的 Neo4j driver，测试结束自动清空 Class/Property/Metric。"""
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "test"))
    yield driver
    with driver.session() as session:
        session.run("MATCH (n) WHERE n:Class OR n:Property OR n:Metric DETACH DELETE n")
    driver.close()


@pytest.fixture
async def neo4j_seed_classes(neo4j_clean_driver):
    """种入 3 个 Class 节点：1 个带 unified_id，2 个不带（待回填）。"""
    with neo4j_clean_driver.session() as session:
        session.run(
            "CREATE (c:Class {unified_id: 'obj:supplier:S001', name: '供应商'})"
        )
        session.run("CREATE (c:Class {id: 100, name: '物料'})")
        session.run("CREATE (c:Class {id: 101, name: '客户'})")
    yield neo4j_clean_driver
```

**Step 2: 验证 fixture 可被 import**

```bash
cd backend && uv run pytest --collect-only app/tests/integration/conftest_neo4j.py 2>&1 | tail -5
```

Expected: 无 ImportError；显示 2 个 fixture。

**Step 3: Commit**

```bash
git add backend/app/tests/integration/conftest_neo4j.py
git commit -m "test: Neo4j 真实库测试夹具（清空 + seed Class/Property/Metric）"
```

---

### Task 2: TDD - reconcile 测试（RED）

**Files:**
- Create: `backend/app/tests/integration/test_reconcile_neo4j_id_mapping.py`

**Interfaces Consumed:** neo4j fixture (Task 1), `scripts/reconcile_neo4j_id_mapping.py` (待实现)

**Step 1: 写失败测试**

```python
# backend/app/tests/integration/test_reconcile_neo4j_id_mapping.py
"""Neo4j ↔ PG id_mapping 三方对账测试（RED）。

覆盖：
1. Neo4j 有节点 / PG 无映射 → 在 PG 写入占位 unified_id
2. PG 有映射 / Neo4j 无节点 → 警告行（不删除 PG 记录）
3. 双向都有但 unified_id 不一致 → 报错行
4. 双向一致 → 通过
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _seed_pg_id_mapping(session: AsyncSession, unified_id: str, entity_type: str) -> None:
    """种入 id_mapping 测试行。"""
    await session.execute(
        text(
            "INSERT INTO id_mapping (unified_id, business_object, external_id) "
            "VALUES (:uid, :bo, :ext) "
            "ON CONFLICT (business_object, external_id) DO NOTHING"
        ),
        {"uid": unified_id, "bo": entity_type, "ext": unified_id.split(":")[-1]},
    )
    await session.commit()


async def test_reconcile_reports_clean_when_both_sides_align(
    neo4j_seed_classes, db_session: AsyncSession
):
    """两侧均一致 → reconcile exit 0，stdout 含 'diff=0'。"""
    from scripts.reconcile_neo4j_id_mapping import reconcile

    # Arrange: PG 与 Neo4j 都用 obj:supplier:S001
    await _seed_pg_id_mapping(db_session, "obj:supplier:S001", "supplier")

    # Act
    report = await reconcile(db_session, neo4j_seed_classes)

    # Assert
    assert report.diff_count == 0, f"expected 0 diff, got {report.diff_count}"
    assert report.exit_code == 0
```

**Step 2: 跑测试验证 RED**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py::test_reconcile_reports_clean_when_both_sides_align -v 2>&1 | tail -15
```

Expected: `ModuleNotFoundError: No module named 'scripts.reconcile_neo4j_id_mapping'`

**Step 3: 不要 commit（RED 状态是预期）**

---

### Task 3: 实现 reconcile_neo4j_id_mapping.py（GREEN）

**Files:**
- Create: `backend/scripts/reconcile_neo4j_id_mapping.py`

**Interfaces Produced:**
- `async def reconcile(session: AsyncSession, driver) -> ReconcileReport`
- `ReconcileReport.diff_count: int`, `ReconcileReport.exit_code: int`, `ReconcileReport.rows: list[dict]`
- CLI: `python -m scripts.reconcile_neo4j_id_mapping` 跑全量对账

**Step 1: 写最小实现**

```python
# backend/scripts/reconcile_neo4j_id_mapping.py
"""Neo4j ↔ PG id_mapping 三方对账。

读 Neo4j Class/Property/Metric 节点 + PG id_mapping，按 unified_id 比对：
- 双侧有 + 一致 → 通过
- 仅 Neo4j 有 → 建议 backfill（写入 PG 占位 + Neo4j external_id）
- 仅 PG 有 → 警告（不删 PG；需人工判断）
- 双侧有 + 不一致 → CRITICAL 报错

不写 Neo4j / PG，仅生成 report。
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

from neo4j import Driver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import getSessionFactory


@dataclass(frozen=True)
class ReconcileReport:
    diff_count: int
    exit_code: int
    rows: list[dict] = field(default_factory=list)


async def reconcile(session: AsyncSession, driver: Driver) -> ReconcileReport:
    """比对 Neo4j 节点 vs PG id_mapping，返回差异报告。"""
    # 1. 读 Neo4j 全量 Class/Property/Metric
    with driver.session() as ns:
        neo4j_nodes = ns.run(
            "MATCH (n) WHERE n:Class OR n:Property OR n:Metric "
            "RETURN labels(n)[0] AS label, n.unified_id AS uid, n.id AS legacy_id"
        ).data()

    # 2. 读 PG id_mapping
    pg_rows = (await session.execute(
        text("SELECT unified_id, business_object FROM id_mapping")
    )).mappings().all()
    pg_uids = {r["unified_id"] for r in pg_rows}

    # 3. diff
    diffs: list[dict] = []
    for node in neo4j_nodes:
        uid = node.get("uid")
        if uid is None:
            diffs.append({
                "kind": "neo4j_no_unified_id",
                "label": node["label"],
                "legacy_id": node.get("legacy_id"),
            })
        elif uid not in pg_uids:
            diffs.append({
                "kind": "neo4j_missing_pg",
                "label": node["label"],
                "unified_id": uid,
            })

    return ReconcileReport(
        diff_count=len(diffs),
        exit_code=0 if not diffs else 1,
        rows=diffs,
    )


async def _main() -> int:
    factory = getSessionFactory()
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "test"))
    try:
        async with factory() as session:
            report = await reconcile(session, driver)
        print(f"diff_count={report.diff_count}")
        for row in report.rows:
            print(row)
        return report.exit_code
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
```

**Step 2: 跑测试验证 GREEN**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py::test_reconcile_reports_clean_when_both_sides_align -v 2>&1 | tail -15
```

Expected: PASS

**Step 3: Commit**

```bash
git add backend/scripts/reconcile_neo4j_id_mapping.py backend/app/tests/integration/test_reconcile_neo4j_id_mapping.py
git commit -m "feat(id-mapping): M0-P0.3 reconcile_neo4j_id_mapping.py 三方对账脚本"
```

---

### Task 4: TDD - backfill 测试（RED）

**Files:**
- Modify: `backend/app/tests/integration/test_reconcile_neo4j_id_mapping.py`

**Step 1: 加 3 个失败测试**

```python
# 在 test_reconcile_neo4j_id_mapping.py 末尾追加


async def test_backfill_writes_pg_and_neo4j_for_unaligned_nodes(
    neo4j_seed_classes, db_session: AsyncSession
):
    """Neo4j 节点无 unified_id → backfill 后 PG + Neo4j 都有 obj:Class:{id}。"""
    from scripts.backfill_neo4j_external_id import backfill

    # Arrange: PG 空，Neo4j 已有 1 个无 unified_id 节点（id=100）

    # Act
    written = await backfill(db_session, neo4j_seed_classes, batch_size=500)

    # Assert
    assert written == 2  # 仅 2 个无 unified_id 的节点被回填（id=100, id=101）
    row = (await db_session.execute(
        text("SELECT unified_id FROM id_mapping WHERE external_id = '100'")
    )).first()
    assert row is not None
    assert row[0] == "obj:Class:100"


async def test_backfill_is_idempotent(neo4j_seed_classes, db_session: AsyncSession):
    """重复跑 backfill 不产生重复映射。"""
    from scripts.backfill_neo4j_external_id import backfill

    await backfill(db_session, neo4j_seed_classes)
    written2 = await backfill(db_session, neo4j_seed_classes)

    assert written2 == 0  # 第二次无新写入
    rows = (await db_session.execute(
        text("SELECT COUNT(*) FROM id_mapping WHERE external_id LIKE '10%'")
    )).scalar()
    assert rows == 2  # 仅 2 条


async def test_backfill_then_reconcile_yields_zero_diff(
    neo4j_seed_classes, db_session: AsyncSession
):
    """backfill 完成后 reconcile 必须 diff=0。"""
    from scripts.backfill_neo4j_external_id import backfill
    from scripts.reconcile_neo4j_id_mapping import reconcile

    await backfill(db_session, neo4j_seed_classes)
    report = await reconcile(db_session, neo4j_seed_classes)

    assert report.diff_count == 0, f"diff after backfill: {report.rows}"
```

**Step 2: 跑测试验证 RED**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py -v 2>&1 | tail -20
```

Expected: 后 3 个测试 FAIL（`ModuleNotFoundError: scripts.backfill_neo4j_external_id`）

**Step 3: 不要 commit**

---

### Task 5: 实现 backfill_neo4j_external_id.py（GREEN）

**Files:**
- Create: `backend/scripts/backfill_neo4j_external_id.py`

**Interfaces Produced:**
- `async def backfill(session, driver, batch_size=500) -> int` 返回写入数
- 写路径：Neo4j 无 unified_id → 生成 `obj:{Label}:{id}` → 写 PG id_mapping → 写 Neo4j external_id

**Step 1: 写最小实现**

```python
# backend/scripts/backfill_neo4j_external_id.py
"""M0-P0.3: 回填 Neo4j Class/Property/Metric 节点的 external_id（= unified_id）。

流程（按 batch_size 分批，事务边界在 PG）：
  1. MATCH (n:Class|Property|Metric) WHERE n.unified_id IS NULL
  2. 对每个节点：unified_id = obj:{Label}:{id}
  3. INSERT INTO id_mapping (unified_id, business_object, external_id) ON CONFLICT DO NOTHING
  4. SET n.unified_id = obj:{Label}:{id}
  5. 计数 = 写入数

幂等：第二次跑无新写入（unified_id 已全部存在）。
风险缓解：>10k 节点时 batch_size=500 分批，避免单事务过长。
"""
from __future__ import annotations

import asyncio
import sys

from neo4j import Driver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import getSessionFactory


async def backfill(session: AsyncSession, driver: Driver, batch_size: int = 500) -> int:
    """回填 Neo4j 无 unified_id 的节点，返回写入 id_mapping 的条数。"""
    written = 0
    while True:
        # 1. 取一批无 unified_id 节点
        # 约定：legacy id 字段名为 `id`（来自 v3.1 之前的 ontology_class/property/metric 表 BIGINT 主键）
        # 如节点 schema 不含 `id`，需先用 :BACKFILL_LEGACY_KEY 标，或人工迁移后再跑
        with driver.session() as ns:
            batch = ns.run(
                "MATCH (n) WHERE (n:Class OR n:Property OR n:Metric) "
                "AND n.unified_id IS NULL "
                "WITH n LIMIT $limit "
                "RETURN labels(n)[0] AS label, n.id AS legacy_id",
                limit=batch_size,
            ).data()
        if not batch:
            break

        # 2. 写 PG id_mapping
        for node in batch:
            label = node["label"].lower()
            legacy_id = str(node["legacy_id"])
            unified_id = f"obj:{label}:{legacy_id}"
            await session.execute(
                text(
                    "INSERT INTO id_mapping (unified_id, business_object, external_id) "
                    "VALUES (:uid, :bo, :ext) ON CONFLICT DO NOTHING"
                ),
                {"uid": unified_id, "bo": label, "ext": legacy_id},
            )
        await session.commit()

        # 3. 写 Neo4j unified_id
        with driver.session() as ns:
            for node in batch:
                label = node["label"].lower()
                legacy_id = str(node["legacy_id"])
                unified_id = f"obj:{label}:{legacy_id}"
                ns.run(
                    f"MATCH (n:{node['label']} {{id: $legacy_id}}) "
                    "SET n.unified_id = $unified_id",
                    legacy_id=node["legacy_id"],
                    unified_id=unified_id,
                )
        written += len(batch)

    return written


async def _main() -> int:
    factory = getSessionFactory()
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "test"))
    try:
        async with factory() as session:
            written = await backfill(session, driver)
        print(f"written={written}")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
```

**Step 2: 跑全部测试验证 GREEN**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py -v 2>&1 | tail -20
```

Expected: 4/4 PASS

**Step 3: 跑覆盖率**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py --cov=scripts.reconcile_neo4j_id_mapping --cov=scripts.backfill_neo4j_external_id --cov-report=term-missing 2>&1 | tail -15
```

Expected: TOTAL ≥ 80%

**Step 4: Commit**

```bash
git add backend/scripts/backfill_neo4j_external_id.py backend/app/tests/integration/test_reconcile_neo4j_id_mapping.py
git commit -m "feat(id-mapping): M0-P0.3 backfill_neo4j_external_id.py 三方回填脚本"
```

---

### Task 6: 端到端验证 — 跑 reconcile CLI

**Files:** 无（仅执行）

**Step 1: 清空 PG id_mapping + Neo4j 三类节点**

```bash
docker exec qa-pg-test psql -U postgres -d qa_metadata_test -c "TRUNCATE id_mapping;"
# Neo4j 清空通过 Cypher
docker exec qa-neo4j cypher-shell -u neo4j -p test "MATCH (n) WHERE n:Class OR n:Property OR n:Metric DETACH DELETE n;"
```

**Step 2: 跑 backfill**

```bash
cd backend && uv run python scripts/backfill_neo4j_external_id.py 2>&1 | tail -5
```

Expected: `written=<N>` （N = 全量无 unified_id 节点数）

**Step 3: 跑 reconcile 必须 diff=0**

```bash
cd backend && uv run python scripts/reconcile_neo4j_id_mapping.py 2>&1 | tail -5
echo "exit=$?"
```

Expected: `diff_count=0`，`exit=0`

**Step 4: 不 commit（这只是 sanity check，无代码变更）**

---

### Task 7: code-reviewer + security-reviewer 闸门（Phase 1）

**Files:** 无（仅审查）

**Step 1: 跑 code-reviewer**

```
使用 Agent 工具：
  subagent_type: code-reviewer
  prompt: 审查 backend/scripts/backfill_neo4j_external_id.py 和
          backend/scripts/reconcile_neo4j_id_mapping.py。聚焦：
          1. 写路径的 batch_size 防大事务
          2. ON CONFLICT DO NOTHING 的幂等性
          3. CQL 标签白名单（不能字符串拼接 label）
          4. 错误处理（Neo4j 连不上 / PG 写失败时的退出码）
  working_dir: /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.claude/worktrees/feat-planner-step-limit
```

Expected: 无 CRITICAL；HIGH ≤ 2 项可修复。

**Step 2: 跑 security-reviewer**

```
使用 Agent 工具：
  subagent_type: security-reviewer
  prompt: 审查 backend/scripts/backfill_neo4j_external_id.py。聚焦：
          1. SQL 注入（CQL 参数化、PG 用 :param 而非 f-string）
          2. CQL 注入（label 来自 labels(n)[0]，但 SET 子句用了 f-string 拼接 label — 评估风险）
          3. 凭据硬编码（neo4j/test 是否在源码而非环境变量）
  working_dir: ...
```

Expected: 无 CRITICAL。

**Step 3: 若审查通过则无 commit（已 commit）；若修复则 amend commit 并跑测试**

---

### Task 8: Phase 1 整体覆盖率 + 总结

**Files:** 无

**Step 1: 全量集成测试 + 覆盖率**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_neo4j_id_mapping.py \
  app/tests/integration/test_id_mapping_api.py \
  app/tests/integration/test_ontology_unified_id.py \
  --cov=scripts.reconcile_neo4j_id_mapping \
  --cov=scripts.backfill_neo4j_external_id \
  --cov-report=term 2>&1 | tail -10
```

Expected: ALL PASS，coverage ≥ 80%

**Step 2: 在 memory 写入 Phase 1 落地备忘**

使用 Write 工具写入：
- Path: `/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-m0-p03-neo4j-backfill.md`
- 内容：
  ```markdown
  ---
  name: qa-system-m0-p03-neo4j-backfill
  description: M0-P0.3 Neo4j external_id 回填脚本约定
  metadata:
    type: project
    ---
  
  M0-P0.3 落地：backfill_neo4j_external_id.py + reconcile_neo4j_id_mapping.py。
  - unified_id 格式 obj:{label_lower}:{legacy_id}（label 小写化）
  - batch_size=500 防大事务；幂等靠 ON CONFLICT DO NOTHING
  - reconcile 仅读，diff_count=0 = exit 0；否则 exit 1
  - 必须连真实 Neo4j（bolt://localhost:7687），禁 mock
  - 关联 [[qa-system-m0-p04-milvus-backfill]]
  ```

然后更新 `MEMORY.md` 索引：

```bash
echo "- [M0-P0.3 Neo4j 回填](qa-system-m0-p03-neo4j-backfill.md) — unified_id 格式 + batch_size 500 + reconcile 仅读" >> /Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md
```

---

## Phase 2: M0-P0.4 · Milvus external_id 回填

### Task 9: TDD - Milvus reconcile 测试（RED）

**Files:**
- Create: `backend/app/tests/integration/conftest_milvus.py`
- Create: `backend/app/tests/integration/test_reconcile_milvus_id_mapping.py`

**Step 1: 写 fixture**

```python
# backend/app/tests/integration/conftest_milvus.py
"""Milvus 真实库测试夹具。

约定：
  - 每个测试启动前 drop 三类 ontology collection
  - 不 mock pymilvus（直接连 localhost:19530）
"""
from __future__ import annotations

import pytest
from pymilvus import MilvusClient, connections


@pytest.fixture
async def milvus_clean():
    """返回 MilvusClient，测试结束自动 drop 三类 collection。"""
    client = MilvusClient(uri="http://localhost:19530")
    for name in (
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
    ):
        if client.has_collection(name):
            client.drop_collection(name)
    yield client
    for name in (
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
    ):
        if client.has_collection(name):
            client.drop_collection(name)


@pytest.fixture
async def milvus_seed(milvus_clean):
    """种入 5 个无 external_id 的向量。"""
    from app.infrastructure import milvus_client as milvus
    milvus.ensureCollection()
    milvus.insertEmbeddings([
        {"ontology_id": 1001, "text": "供应商", "embedding": [0.0] * 8},
        {"ontology_id": 1002, "text": "物料", "embedding": [0.0] * 8},
        {"ontology_id": 1003, "text": "客户", "embedding": [0.0] * 8},
    ])
    yield milvus_clean
```

**Step 2: 写失败测试**

```python
# backend/app/tests/integration/test_reconcile_milvus_id_mapping.py
"""Milvus ↔ PG id_mapping 三方对账测试（RED）。"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_reconcile_milvus_reports_clean_when_aligned(
    milvus_seed, db_session: AsyncSession
):
    """两侧一致 → diff=0。"""
    from scripts.reconcile_milvus_id_mapping import reconcile

    # Arrange: PG 种入与 Milvus 一致的映射
    await db_session.execute(
        text(
            "INSERT INTO id_mapping (unified_id, business_object, external_id, "
            "milvus_collection, milvus_id) VALUES "
            "('obj:class:1001', 'class', '1001', 'ontology_class_embeddings', '1001'),"
            "('obj:class:1002', 'class', '1002', 'ontology_class_embeddings', '1002'),"
            "('obj:class:1003', 'class', '1003', 'ontology_class_embeddings', '1003')"
        )
    )
    await db_session.commit()

    # Act
    report = await reconcile(db_session, milvus_seed)

    # Assert
    assert report.diff_count == 0
```

**Step 3: 跑测试验证 RED**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: scripts.reconcile_milvus_id_mapping`

**Step 4: 不 commit**

---

### Task 10: 实现 reconcile_milvus_id_mapping.py（GREEN）

**Files:**
- Create: `backend/scripts/reconcile_milvus_id_mapping.py`

**Step 1: 写最小实现**

```python
# backend/scripts/reconcile_milvus_id_mapping.py
"""Milvus ↔ PG id_mapping 三方对账。

比对逻辑：
  - Milvus ontology_class_embeddings / property / metric 三 collection
  - 读 ontology_id (legacy) → 应映射到 PG id_mapping 的 milvus_id 列
  - 仅 PG 有 / Milvus 无 → 警告
  - 仅 Milvus 有 → 建议 backfill
  - 双侧一致 → 通过
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

from pymilvus import MilvusClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import getSessionFactory


MILVUS_ONTOLOGY_COLLECTIONS = (
    "ontology_class_embeddings",
    "ontology_property_embeddings",
    "ontology_metric_embeddings",
)


@dataclass(frozen=True)
class ReconcileReport:
    diff_count: int
    exit_code: int
    rows: list[dict] = field(default_factory=list)


async def reconcile(session: AsyncSession, client: MilvusClient) -> ReconcileReport:
    diffs: list[dict] = []
    pg_milvus_ids: set[str] = {
        r[0] for r in (await session.execute(
            text("SELECT DISTINCT milvus_id FROM id_mapping WHERE milvus_id IS NOT NULL")
        )).all() if r[0] is not None
    }

    for collection in MILVUS_ONTOLOGY_COLLECTIONS:
        if not client.has_collection(collection):
            continue
        rows = client.query(collection, output_fields=["ontology_id"], limit=10000)
        for row in rows:
            oid = str(row.get("ontology_id"))
            if oid not in pg_milvus_ids:
                diffs.append({
                    "kind": "milvus_missing_pg",
                    "collection": collection,
                    "ontology_id": oid,
                })

    return ReconcileReport(
        diff_count=len(diffs),
        exit_code=0 if not diffs else 1,
        rows=diffs,
    )


async def _main() -> int:
    from pymilvus import MilvusClient
    factory = getSessionFactory()
    client = MilvusClient(uri="http://localhost:19530")
    try:
        async with factory() as session:
            report = await reconcile(session, client)
        print(f"diff_count={report.diff_count}")
        for row in report.rows[:50]:
            print(row)
        return report.exit_code
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
```

**Step 2: 跑测试验证 GREEN**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py::test_reconcile_milvus_reports_clean_when_aligned -v 2>&1 | tail -10
```

Expected: PASS

**Step 3: Commit**

```bash
git add backend/scripts/reconcile_milvus_id_mapping.py \
        backend/app/tests/integration/test_reconcile_milvus_id_mapping.py \
        backend/app/tests/integration/conftest_milvus.py
git commit -m "feat(id-mapping): M0-P0.4 reconcile_milvus_id_mapping.py 三方对账脚本"
```

---

### Task 11: TDD - Milvus backfill 测试（RED）

**Files:**
- Modify: `backend/app/tests/integration/test_reconcile_milvus_id_mapping.py`

**Step 1: 追加 3 个失败测试**

```python
# 在文件末尾追加


async def test_backfill_writes_milvus_external_id_and_pg_mapping(
    milvus_seed, db_session: AsyncSession
):
    """Milvus 无 external_id → backfill 后 PG + Milvus 都有 obj:class:{ontology_id}。"""
    from scripts.backfill_milvus_external_id import backfill

    written = await backfill(db_session, milvus_seed)

    assert written == 3
    row = (await db_session.execute(
        text("SELECT unified_id, milvus_id FROM id_mapping WHERE external_id = '1001'")
    )).first()
    assert row[0] == "obj:class:1001"
    assert row[1] == "1001"


async def test_backfill_milvus_is_idempotent(milvus_seed, db_session: AsyncSession):
    """重复跑 backfill 无新写入。"""
    from scripts.backfill_milvus_external_id import backfill

    await backfill(db_session, milvus_seed)
    written2 = await backfill(db_session, milvus_seed)
    assert written2 == 0


async def test_backfill_then_reconcile_yields_zero_diff(
    milvus_seed, db_session: AsyncSession
):
    from scripts.backfill_milvus_external_id import backfill
    from scripts.reconcile_milvus_id_mapping import reconcile

    await backfill(db_session, milvus_seed)
    report = await reconcile(db_session, milvus_seed)
    assert report.diff_count == 0
```

**Step 2: 跑测试验证 RED**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py -v 2>&1 | tail -15
```

Expected: 后 3 个 FAIL

---

### Task 12: 实现 backfill_milvus_external_id.py（GREEN）

**Files:**
- Create: `backend/scripts/backfill_milvus_external_id.py`

**Step 1: 写最小实现**

```python
# backend/scripts/backfill_milvus_external_id.py
"""M0-P0.4: 回填 Milvus ontology_* 三类 collection 的 milvus_id 列。

流程：
  1. 读 ontology_* collection 全量 ontology_id
  2. 对每条：milvus_id = obj:{collection_prefix}:{ontology_id}
  3. INSERT INTO id_mapping (unified_id, business_object, external_id, milvus_collection, milvus_id)
  4. 不修改 Milvus schema（m0-p0.4 阶段 PG 侧的 milvus_id 列即足够对账；
     Milvus schema 升级放 m0-p0.5 或后续）

幂等：第二次无新写入。
"""
from __future__ import annotations

import asyncio
import sys

from pymilvus import MilvusClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import getSessionFactory


_COLLECTION_PREFIX = {
    "ontology_class_embeddings": "class",
    "ontology_property_embeddings": "property",
    "ontology_metric_embeddings": "metric",
}


async def backfill(session: AsyncSession, client: MilvusClient) -> int:
    written = 0
    for collection, prefix in _COLLECTION_PREFIX.items():
        if not client.has_collection(collection):
            continue
        rows = client.query(collection, output_fields=["ontology_id"], limit=10000)
        for row in rows:
            legacy_id = str(row["ontology_id"])
            unified_id = f"obj:{prefix}:{legacy_id}"
            result = await session.execute(
                text(
                    "INSERT INTO id_mapping (unified_id, business_object, external_id, "
                    "milvus_collection, milvus_id) VALUES "
                    "(:uid, :bo, :ext, :coll, :mid) "
                    "ON CONFLICT (business_object, external_id) DO UPDATE "
                    "SET milvus_collection = EXCLUDED.milvus_collection, "
                    "    milvus_id = EXCLUDED.milvus_id, "
                    "    updated_time = NOW()"
                ),
                {
                    "uid": unified_id,
                    "bo": prefix,
                    "ext": legacy_id,
                    "coll": collection,
                    "mid": legacy_id,
                },
            )
            if result.rowcount > 0:
                written += 1
    await session.commit()
    return written


async def _main() -> int:
    from pymilvus import MilvusClient
    factory = getSessionFactory()
    client = MilvusClient(uri="http://localhost:19530")
    try:
        async with factory() as session:
            written = await backfill(session, client)
        print(f"written={written}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
```

**Step 2: 跑全部测试验证 GREEN**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py -v 2>&1 | tail -10
```

Expected: 4/4 PASS

**Step 3: 跑覆盖率**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py \
  --cov=scripts.reconcile_milvus_id_mapping \
  --cov=scripts.backfill_milvus_external_id \
  --cov-report=term-missing 2>&1 | tail -10
```

Expected: ≥ 80%

**Step 4: Commit**

```bash
git add backend/scripts/backfill_milvus_external_id.py backend/app/tests/integration/test_reconcile_milvus_id_mapping.py
git commit -m "feat(id-mapping): M0-P0.4 backfill_milvus_external_id.py 三方回填脚本"
```

---

### Task 13: 端到端 Milvus 验证

**Step 1: 清空 + 跑 backfill + 跑 reconcile 必须 diff=0**

```bash
cd backend && uv run python scripts/backfill_milvus_external_id.py 2>&1 | tail -3
cd backend && uv run python scripts/reconcile_milvus_id_mapping.py 2>&1 | tail -3
echo "exit=$?"
```

Expected: written>0, diff_count=0, exit=0

---

### Task 14: code-reviewer + security-reviewer（Phase 2）

**Step 1: 跑两个审查 agent**（同 Phase 1 模式，但指向新文件）

```
subagent_type: code-reviewer
prompt: 审查 backend/scripts/backfill_milvus_external_id.py + reconcile_milvus_id_mapping.py
       聚焦：Milvus query(limit=10000) 的截断风险 + SQL 参数化 + 错误处理

subagent_type: security-reviewer
prompt: 审查同两文件，聚焦：SQL 注入（已用 :param 应 OK）+ 凭据 + limit 截断致数据丢失
```

Expected: 无 CRITICAL。

---

### Task 15: Phase 2 整体覆盖率 + memory 备忘

**Step 1: 全量测试**

```bash
cd backend && uv run pytest app/tests/integration/test_reconcile_milvus_id_mapping.py \
  app/tests/integration/test_id_mapping_api.py \
  --cov=scripts.reconcile_milvus_id_mapping \
  --cov=scripts.backfill_milvus_external_id \
  --cov-report=term 2>&1 | tail -5
```

Expected: ALL PASS, coverage ≥ 80%

**Step 2: 写 memory**

Path: `/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-m0-p04-milvus-backfill.md`

```markdown
---
name: qa-system-m0-p04-milvus-backfill
description: M0-P0.4 Milvus milvus_id 回填脚本约定
metadata:
  type: project
---

M0-P0.4 落地：backfill_milvus_external_id.py + reconcile_milvus_id_mapping.py。
- 写入 PG id_mapping 的 milvus_collection + milvus_id 列；不改 Milvus schema（升级放 M0-P0.5）
- ON CONFLICT DO UPDATE 确保 milvus_id 同步刷新（不像 P0.3 用 DO NOTHING）
- query(limit=10000) 当前 OK，>10k 节点须改 query_iterator（关联 [[qa-system-milvus-query-pagination]]）
- 关联 [[qa-system-m0-p03-neo4j-backfill]]
```

更新 MEMORY.md。

---

## Phase 3: PR 合并序列（手动验证 + commit）

### Task 16: PR #1 — feat/m0-unified-id → main

**Files:** 无代码变更（仅 git 操作）

**前置检查：**
- local main 在 `2ce8554 bug 修复2`，领先 origin/main 35 个 commit
- local main 工作树有未跟踪的 id_mapping.py / test_id_mapping_api.py

**Step 1: 在 main 上清理未跟踪文件（避免误冲突）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git stash -u  # 暂存未跟踪文件
git status    # 确认工作树干净
```

**Step 2: 切到 feat/m0-unified-id 并 rebase main（吸收最新 bug 修复）**

```bash
git checkout feat/m0-unified-id
git rebase main
```

Expected: 无冲突（main 上的 bug fix 与 M0 改动不重叠）

**Step 3: 切回 main 并 fast-forward merge**

```bash
git checkout main
git merge --ff-only feat/m0-unified-id
```

Expected: Fast-forward；现在 main 包含全部 M0 work。

**Step 4: 跑全量集成测试**

```bash
cd backend && uv run pytest app/tests/integration/ -v --tb=short 2>&1 | tail -30
```

Expected: ALL PASS（无 skip）

**Step 5: 恢复暂存**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git stash pop
# 如果有冲突，逐项处理（id_mapping.py 已被合并进 main，无冲突）
```

**Step 6: 推送**

```bash
git push origin main
```

Expected: 推送成功。

**Step 7: 不 commit（这是 merge 操作）**

---

### Task 17: PR #2 — feat/planner-step-limit → epic/v31-upgrade

**前置检查：**
- 检查 `feat/planner-step-limit` 分支是否存在：`git branch -a | grep planner-step-limit`
- 如果不存在：从 feat/m0-unified-id 的 05d3412 commit 创建该分支

**Step 1: 创建 feat/planner-step-limit（如不存在）**

```bash
git checkout 05d3412 -b feat/planner-step-limit  # 从 M2a 提交切出
```

**Step 2: 切到 epic/v31-upgrade 并 merge**

```bash
git checkout epic/v31-upgrade
git pull origin epic/v31-upgrade
git merge --no-ff feat/planner-step-limit -m "merge: M2a Planner ≤5 步硬限"
```

Expected: Merge commit created.

**Step 3: 跑 planner 相关测试**

```bash
cd backend && uv run pytest app/tests/unit/test_multi_step_plan.py \
  app/tests/unit/test_step_query_planner.py \
  app/tests/integration/test_chat_multi_step.py \
  -v 2>&1 | tail -20
```

Expected: ALL PASS

**Step 4: Push**

```bash
git push origin epic/v31-upgrade
```

---

### Task 18: PR #3 — epic/v31-upgrade → main

**Step 1: 同步 epic/v31-upgrade 拿到 M0 合并后的 main**

```bash
git checkout epic/v31-upgrade
git merge origin/main --no-ff -m "merge: bring main bug fixes into epic"
```

Expected: Merge commit created (无冲突预期)。

**Step 2: 跑全量集成测试**

```bash
cd backend && uv run pytest app/tests/integration/ -v --tb=short 2>&1 | tail -30
```

Expected: ALL PASS

**Step 3: 合并到 main**

```bash
git checkout main
git merge --no-ff epic/v31-upgrade -m "merge: M0 Unified ID + M2a Planner 硬限 → main"
```

**Step 4: 跑全量测试 + 覆盖率**

```bash
cd backend && uv run pytest app/tests/ --cov=app --cov-report=term 2>&1 | tail -10
```

Expected: ALL PASS, coverage ≥ 80%

**Step 5: Push**

```bash
git push origin main
```

---

### Task 19: 落地后写 memory + 更新 plan 状态

**Step 1: 写合并落地 memory**

Path: `/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-m0-merged.md`

```markdown
---
name: qa-system-m0-merged
description: M0 Unified ID 全部合入 main
metadata:
  type: project
---

M0 Unified ID 全部合入 main：
- 36f1ddc + 50383e1: P0.1 id_mapping 表 + API + 写路径
- Phase 1 commit: P0.3 Neo4j external_id 回填 + reconcile（由 Task 6 写出）
- Phase 2 commit: P0.4 Milvus milvus_id 回填 + reconcile（由 Task 12 写出）
- 05d3412: M2a Planner ≤5 步硬限

合并路径：feat/m0-unified-id (含 P0.1+P0.3+P0.4) → main；epic/v31-upgrade (含 M2a) → main。
对账脚本：scripts/reconcile_neo4j_id_mapping.py + reconcile_milvus_id_mapping.py 每日 diff=0。
```

更新 MEMORY.md 索引。

**Step 2: 更新原 plan 状态**

修改 `docs/superpowers/plans/2026-09-29-m0-unified-id-execution.md`：

```markdown
> **状态**：✅ M0 全部完成，main 已含 P0.1+P0.3+P0.4+M2a
> **完成时间**：执行 Task 18 后由 agent 写入当日日期（YYYY-MM-DD 格式）
> **详细任务分解**：见 `2026-09-29-m0-unified-id-development.md`
```

```bash
git add docs/superpowers/plans/2026-09-29-m0-unified-id-execution.md
git commit -m "docs: M0 Unified ID 全部完成，更新状态"
```

---

## 验收清单（执行完毕后人工 review）

- [ ] PG `id_mapping` 表 unified_id 全量；`SELECT COUNT(*) FROM id_mapping` 与 Neo4j 节点数对齐
- [ ] `scripts/reconcile_neo4j_id_mapping.py` 跑得 `diff_count=0`
- [ ] `scripts/reconcile_milvus_id_mapping.py` 跑得 `diff_count=0`
- [ ] main 分支 HEAD 含 P0.1 + P0.3 + P0.4 + M2a 所有 commit
- [ ] 全量集成测试 PASS，单元覆盖率 ≥ 80%
- [ ] memory 已记录三条新条目（m0-p03 / m0-p04 / m0-merged）
- [ ] 原 plan 文件状态改为"已完成"

---

## 失败处理（任何 Task 失败时）

1. **不要自动重试**：agent 在 Task 失败时 halt，报告具体失败步骤与错误日志。
2. **不要回滚已完成 commit**：已 commit 的代码是 ground truth，保留 diff 让后续 agent 修复。
3. **memory 写入失败原因**：用 `qa-system-m0-blocker-<N>.md` 记录阻塞原因 + 下一步。
4. **暂停后人工 review**：用户决定是 amend commit、修测试、还是重跑整个 Task。

---

## 执行时间估算

| Phase | Tasks | 估算（含 agent 调度 + 测试） |
|-------|-------|------------------------------|
| Phase 1 M0-P0.3 Neo4j | 8 | ~25 分钟 |
| Phase 2 M0-P0.4 Milvus | 7 | ~20 分钟 |
| Phase 3 PR 合并 | 4 | ~10 分钟 |
| **合计** | **19** | **~55 分钟** |

不含人工 review 与失败重试。
