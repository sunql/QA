# 指标/KPI 纳入向量图管理（Milvus + Neo4j 同步与召回消费）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kpi_catalog 纳入 Milvus 向量与 Neo4j 图管理（存储+同步钩子+对账兜底+消费），并修复回填脚本会清空 metric 向量的既有 bug。

**Architecture:** 沿用本体三集合的 type-routed Milvus 模式新增第 4 个 collection（`kpi_catalog_embeddings`，type=`"kpi"`）；Neo4j 新增 `KpiCatalog` label（MERGE on unified_id）+ `MEASURED_BY` 边；id_mapping `business_object="KPI"`（零 DDL）；CRUD best-effort 钩子 + syncMissing 对账兜底 + admin 手动端点 + 每日 reconcile 计数扩展；匹配器加可选向量候选步（`KPI_MATCH_VECTOR_ENABLED` 默认关）。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / pymilvus / neo4j driver / pytest(+asyncio) / React + antd + vitest。

**Spec:** `docs/superpowers/specs/2026-11-13-kpi-vector-graph-sync-design.md`（§3 存储 / §4 同步 / §5 排雷 / §6 消费 / §7 手工入口 / §8 可观测 / §11 验收）。

## Global Constraints

- **零 DDL**：不新增 Alembic 迁移（`IdMapping.business_object` 是自由 `String(32)`，无 CHECK 约束 ⇒ 注册 `"KPI"` 无需迁移）。
- **零生产写**：不部署生产、不改生产库（生产仍 alembic 0140+旧代码）。
- **本地 Milvus(19530)/Neo4j(7687) 测试禁跑**（是生产）：所有涉及向量/图的测试用 fake double / monkeypatch；真 PG 集成测试连 `TEST_DATABASE_URL`。
- **测试环境变量**（跑 pytest 前必须 export）：
  ```bash
  export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
  export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
  export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
  ```
- **只跑定向 pytest**（全量 unit 27min/28 红基线不可作门禁）；绝不并行两个 pytest；停 pytest 用 SIGINT。
- **dev 5433（qa_metadata）无批准不写**；测试一律用 5434 的 `qa_metadata_test`。
- **开关默认值（固定，不得擅改）**：`KPI_SYNC_ENABLED` env 默认 **on**、system_config 行缺失 ⇒ **on**（方向与既有 KPI 开关相反，注释必须写明）；`KPI_MATCH_VECTOR_ENABLED` env 默认 **off**、system_config 行缺失 ⇒ **off**（fail-closed）。
- **D3 状态口径**：KPI 向量只索引 `PUBLISHED`；流转到 DRAFT/DEPRECATED ⇒ 删向量、**图节点保留**（status 属性更新）；metric 向量/图维持现状。
- **D4**：不引入 outbox/可靠队列；同步一律 best-effort（失败仅 warning，不影响主流程）。
- **metric 向量不进 NL2SQL schema 裁剪**（类召回 `typeFilter="class"` 不动）。
- **防假绿**：阈值/合并守卫做双向断言；断言 `match_method` 值本身（`"vector"`）而非仅命中数。
- **命名**：函数/变量 camelCase、常量 UPPER_SNAKE_CASE、ORM/Pydantic 字段 snake_case（与 DB 列一致）；不可变数据模式。
- **worktree**：实施在 fresh worktree（base = `origin/main`）；新 worktree 先 `uv sync --frozen --extra dev`。
- **commit 格式**：`<type>: <description>`（feat/fix/test/docs/chore）。

## File Structure（新增/修改全景）

| 文件 | 动作 | 责任 |
|---|---|---|
| `backend/app/infrastructure/milvus_client.py` | 修改 | VALID_EMBEDDING_TYPES + 常量/re-export |
| `backend/app/infrastructure/milvus_dual_write.py` | 修改 | `_kpiFields`/`ensureKpiCollection`/insert/delete 路由/external_id 透传 |
| `backend/app/infrastructure/milvus_search.py` | 修改 | kpi 检索分支 + None-sweep 4 集合 |
| `backend/app/infrastructure/milvus_query_helpers.py` | 修改 | `queryKpiEmbeddings` + 全量读 sweep |
| `backend/app/infrastructure/neo4j_client.py` | 修改 | `KpiCatalog` label 白名单 + upsert/link/id-read |
| `backend/app/services/kpi_vector_graph_sync.py` | 新建 | KPI 同步核心（向量+图+best-effort+对账 helper） |
| `backend/app/services/kpi_metric_switches.py` | 修改 | 两个新开关 |
| `backend/app/config.py` | 修改 | 两个 env 字段 |
| `backend/app/services/kpi_catalog_service.py` | 修改 | CRUD 提交后钩子 |
| `backend/scripts/backfill_kpi_id_mapping.py` | 新建 | 存量 KPI id_mapping 幂等回填 |
| `backend/app/services/ontology_service.py` | 修改 | syncMissingGraph 补 Metric 节点 + KPI 图对账 |
| `backend/app/api/v1/ontology.py` | 修改 | GraphSyncMissingResult 字段扩展 |
| `backend/app/api/v1/kpi_catalog.py` | 修改 | 2 个同步端点 + 文档更正 |
| `backend/app/api/v1/kpi_diagnose.py` | 修改 | sync_health + distance |
| `backend/scripts/backfill_milvus_embeddings.py` | 修改 | 4 collection 排雷（既有 bug 修复） |
| `backend/app/services/kpi_semantic_match_service.py` | 修改 | matchRouteVector 向量候选步 |
| `backend/app/services/chat_stream_output.py` | 修改 | `_routeKpi` 向量回落 + `_vectorMatchEnabled` |
| `backend/app/services/knowledge_compiler_service.py` | 修改 | reconcile kpi 计数键 |
| `frontend/src/api/kpiCatalog.ts` | 修改 | 2 个 api 函数 |
| `frontend/src/pages/KpiCatalogPage.tsx` | 修改 | 页头同步按钮 + 行级重新同步 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 修改 | kpiCatalog 命名空间新 key |
| `frontend/src/tests/KpiCatalogPage.test.tsx` | 修改 | 新按钮用例 |
| `Harness/changes/feat-kpi-vector-graph-sync/summary.md` | 新建 | 变更记录 |

---

### Task 1: Milvus 第 4 个 collection（type=`"kpi"`）

**Files:**
- Modify: `backend/app/infrastructure/milvus_client.py:47`（VALID_EMBEDDING_TYPES）、`:668-670`（常量区）、`:756-775`（底部 re-export）
- Modify: `backend/app/infrastructure/milvus_dual_write.py`（schema/ensure/rebuild/insert/delete 路由）
- Modify: `backend/app/infrastructure/milvus_search.py`（检索分支）
- Modify: `backend/app/infrastructure/milvus_query_helpers.py`（全量读）
- Test: `backend/app/tests/unit/test_milvus_kpi_routing.py`（新建）

**Interfaces:**
- Produces（后续任务依赖）: `VALID_EMBEDDING_TYPES` 含 `"kpi"`；`milvus_dual_write._KPI_COLLECTION_NAME = "kpi_catalog_embeddings"`（三个提取模块各自定义同名常量）；`ensureKpiCollection() -> Collection`；`_kpiFields() -> list[FieldSchema]`；`queryKpiEmbeddings() -> list[dict]`；`insertEmbeddingsDual` 记录**可选** `external_id` 键（缺省 `""`，有值透传）；`milvus_client._KPI_COLLECTION_NAME` / `ensureKpiCollection` / `_kpiFields` / `queryKpiEmbeddings` re-export。
- schema 同形：`_ontologyFields() + [external_id VARCHAR(128)]`，与 `_metricFields()` 完全同款。

- [ ] **Step 1: RED — 写失败测试**

```python
"""Milvus type-routed 第 4 个 collection：kpi_catalog_embeddings。

KPI 目录此前完全在向量库之外（模型文档明确排除）；本批把它纳入与本体三集合
同形的 type-routed 管线。全部用 fake double——本地 19530 是生产 Milvus，禁跑真连接。
"""

from __future__ import annotations

import pytest

from app.infrastructure import milvus_client
from app.infrastructure import milvus_dual_write as dual
from app.infrastructure import milvus_query_helpers as helpers
from app.infrastructure import milvus_search as search


class _FakeCollection:
    """记录 insert 数据与 delete 表达式的假集合。"""

    def __init__(self) -> None:
        self.inserted: list[list] = []
        self.deletedExprs: list[str] = []

    def insert(self, data: list) -> None:
        self.inserted.append(data)

    def flush(self) -> None:
        pass


def test_kpi_type_is_valid() -> None:
    assert "kpi" in milvus_client.VALID_EMBEDDING_TYPES


def test_kpi_collection_constants() -> None:
    assert dual._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert search._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert helpers._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"
    assert milvus_client._KPI_COLLECTION_NAME == "kpi_catalog_embeddings"


def test_insert_routes_kpi_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCollection()
    monkeypatch.setattr(dual, "_insertIntoNewCollection", lambda name, records: None)
    # type="kpi" 不再 raise（此前不在 VALID_EMBEDDING_TYPES 会 ValueError）
    dual.insertEmbeddingsDual([
        {"ontology_id": 1, "type": "kpi", "name": "n", "alias": "",
         "description": "", "embedding": [0.0], "external_id": "KPI_X"},
    ])
    dual.insertEmbeddingsDual([{"ontology_id": 1, "type": "kpi", "name": "n",
                                "alias": "", "description": "", "embedding": [0.0]}])


def test_insert_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        dual.insertEmbeddingsDual([{"ontology_id": 1, "type": "bogus",
                                    "name": "n", "embedding": [0.0]}])


def test_external_id_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """_insertIntoNewCollection 必须透传 external_id（KPI 用 kpi_code），缺省给 ""。"""
    fake = _FakeCollection()
    monkeypatch.setattr(dual, "ensureKpiCollection", lambda: fake)
    dual._insertIntoNewCollection(dual._KPI_COLLECTION_NAME, [
        {"ontology_id": 7, "type": "kpi", "name": "a", "alias": "",
         "description": "", "embedding": [0.1], "external_id": "KPI_A"},
        {"ontology_id": 8, "type": "kpi", "name": "b", "alias": "",
         "description": "", "embedding": [0.2]},
    ])
    externalColumn = fake.inserted[0][6]
    assert externalColumn == ["KPI_A", ""]


def test_delete_routes_kpi_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCollection()
    monkeypatch.setattr(dual, "ensureKpiCollection", lambda: fake)
    dual.deleteByOntologyIdDual(7, "kpi")
    assert fake.deletedExprs == ["ontology_id == 7"]


def test_delete_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        dual.deleteByOntologyIdDual(7, "bogus")


def test_search_kpi_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fakeSearch(collectionName, q, topK):
        seen.append(collectionName)
        return []

    monkeypatch.setattr(search, "_searchCollection", fakeSearch)
    monkeypatch.setattr(search, "_connect", lambda: None)
    search.searchEmbeddingsByTypeRouted([0.0], 5, "kpi")
    assert seen == ["kpi_catalog_embeddings"]


def test_search_none_sweep_covers_4_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        search, "_searchCollection", lambda name, q, k: seen.append(name) or []
    )
    monkeypatch.setattr(search, "_connect", lambda: None)
    search.searchEmbeddingsByTypeRouted([0.0], 5, None)
    assert seen == [
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
        "kpi_catalog_embeddings",
    ]


def test_query_helpers_kpi_read(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        helpers, "_queryAllRowsFromCollection", lambda name: called.append(name) or []
    )
    assert helpers.queryKpiEmbeddings() == []
    assert called == ["kpi_catalog_embeddings"]


def test_query_helpers_sweep_covers_4(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        helpers, "_queryAllRowsFromCollection", lambda name: called.append(name) or []
    )
    helpers.listEmbeddingsAcross3Collections()
    assert "kpi_catalog_embeddings" in called
    assert len(called) == 4
```

- [ ] **Step 2: 跑 RED**

```bash
cd <worktree>/backend
pytest app/tests/unit/test_milvus_kpi_routing.py -q
```
预期：`test_kpi_type_is_valid`、常量、路由、passthrough、sweep 各用例 FAIL（AttributeError/ValueError/断言不等），`test_insert_rejects_unknown_type`/`test_delete_rejects_unknown_type` 可能 PASS（合理）。

- [ ] **Step 3: GREEN — 实现**

`milvus_client.py`：
```python
# :47
VALID_EMBEDDING_TYPES = frozenset(("class", "property", "metric", "kpi"))
```
常量区（`:670` 之后追加）：
```python
_KPI_COLLECTION_NAME = "kpi_catalog_embeddings"
```
底部 re-export：dual_write import 块加 `_kpiFields,`、`ensureKpiCollection,`；query_helpers import 块加 `queryKpiEmbeddings,`。

`milvus_dual_write.py`：模块 docstring 的 Provides 列表加 `_kpiFields / ensureKpiCollection`；常量区加 `_KPI_COLLECTION_NAME = "kpi_catalog_embeddings"`；追加：
```python
def _kpiFields() -> list[FieldSchema]:
    """KPI 目录 embedding schema（与本体三集合同形 + external_id=kpi code）。"""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="kpi_catalog.kpi_code"),
    ]


def ensureKpiCollection() -> Collection:
    """Ensure kpi_catalog_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_KPI_COLLECTION_NAME, _kpiFields())
```
`rebuildOntologyCollections` 的 `collections` 元组追加第 4 项 `(_KPI_COLLECTION_NAME, ensureKpiCollection)`，docstring「3 个」改「4 个」。
`insertEmbeddingsDual` 的 by_type 路由追加：
```python
    if "kpi" in by_type:
        _insertIntoNewCollection(_KPI_COLLECTION_NAME, by_type["kpi"])
```
`_insertIntoNewCollection`：集合选择 if/elif 追加
```python
    elif name == _KPI_COLLECTION_NAME:
        collection = ensureKpiCollection()
```
data 最后一列改为透传（同时修复 class/property/metric 恒空串的隐性限制）：
```python
        [r.get("external_id") or "" for r in records],  # external_id: kpi=kpi_code；本体行缺省 ""
```
`deleteByOntologyIdDual` 的 `name_to_ensure` 字典加 `"kpi": ensureKpiCollection,`。

`milvus_search.py`：常量区加 `_KPI_COLLECTION_NAME = "kpi_catalog_embeddings"`；`searchEmbeddingsByTypeRouted` 追加分支 `if typeFilter == "kpi": return _searchCollection(_KPI_COLLECTION_NAME, queryEmbedding, topK)`（放在 metric 分支后）；None-sweep 集合元组追加 `_KPI_COLLECTION_NAME`；docstring Notes 加 `typeFilter == "kpi"` 行。

`milvus_query_helpers.py`：常量区加 `_KPI_COLLECTION_NAME`；追加
```python
def queryKpiEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from kpi_catalog_embeddings."""
    return _queryAllRowsFromCollection(_KPI_COLLECTION_NAME)
```
`listEmbeddingsAcross3Collections` 的 `query_fn` 元组追加 `queryKpiEmbeddings`（**函数名不改**——诊断 API 与 backfill 脚本按旧名引用）。

- [ ] **Step 4: 跑 GREEN**：同 Step 2 命令，预期全 PASS。
- [ ] **Step 5: Commit**
```bash
git add backend/app/infrastructure/milvus_client.py backend/app/infrastructure/milvus_dual_write.py \
        backend/app/infrastructure/milvus_search.py backend/app/infrastructure/milvus_query_helpers.py \
        backend/app/tests/unit/test_milvus_kpi_routing.py
git commit -m "feat: Milvus 新增 kpi_catalog_embeddings type-routed collection（external_id 透传）"
```

---

### Task 2: Neo4j `KpiCatalog` label 与 `MEASURED_BY` 边

**Files:**
- Modify: `backend/app/infrastructure/neo4j_client.py`（`_ALLOWED_LABELS` :22；id-read helpers :147-172 区；Upsert 区 :228-288；Relationship 区 :295-325）
- Test: `backend/app/tests/unit/test_neo4j_kpi_catalog.py`（新建）

**Interfaces:**
- Produces: `neo4j.upsertKpiCatalogNode(unified_id: str, code: str, name: str | None, status: str, owner: str | None, unit: str | None) -> None`；`neo4j.linkKpiMeasuredBy(kpiUid: str, metricUid: str) -> None`；`neo4j.getMetricIds() -> set[str]`；`neo4j.getKpiCatalogIds() -> set[str]`；`deleteNode("KpiCatalog", unified_id)` 白名单放行（既有函数，无需改动）。
- CQL 不可参数化 label ⇒ 一律白名单（同 `_ALLOWED_LABELS` 注释口径）；MERGE 键 = `unified_id`；id-read 用 `COALESCE(unified_id, toString(id))`（新节点恒有 unified_id，COALESCE 仅对齐既有模式）。

- [ ] **Step 1: RED — 写失败测试**

```python
"""Neo4j KpiCatalog 节点与 MEASURED_BY 边（fake driver，禁连真 Neo4j）。"""

from __future__ import annotations

import pytest

from app.infrastructure import neo4j_client


class _FakeSession:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def run(self, cql: str, **params):
        self._recorder.append((cql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


class _FakeDriver:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def session(self) -> _FakeSession:
        return _FakeSession(self._recorder)


def _patch(monkeypatch: pytest.MonkeyPatch) -> list:
    recorder: list = []
    monkeypatch.setattr(neo4j_client, "getDriver", lambda: _FakeDriver(recorder))
    return recorder


def test_kpi_catalog_label_whitelisted() -> None:
    assert "KpiCatalog" in neo4j_client._ALLOWED_LABELS


def test_upsert_merges_on_unified_id(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(monkeypatch)
    neo4j_client.upsertKpiCatalogNode(
        unified_id="obj:KPI:1", code="KPI_OTD", name="准时交付率",
        status="PUBLISHED", owner="qa", unit="%",
    )
    cql, params = recorder[0]
    assert "MERGE (k:KpiCatalog {unified_id: $unified_id})" in cql
    assert params == {
        "unified_id": "obj:KPI:1", "code": "KPI_OTD", "name": "准时交付率",
        "status": "PUBLISHED", "owner": "qa", "unit": "%",
    }


def test_link_measured_by(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch(monkeypatch)
    neo4j_client.linkKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")
    cql, params = recorder[0]
    assert "MERGE (k)-[:MEASURED_BY]->(m)" in cql
    assert params == {"kpiUid": "obj:KPI:1", "metricUid": "obj:METRIC:5"}


def test_get_kpi_catalog_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        def __iter__(self):
            return iter([{"uid": "obj:KPI:1"}, {"uid": "obj:KPI:2"}])

    class _RunSession(_FakeSession):
        def run(self, cql, **params):
            self._recorder.append((cql, params))
            return _Result()

    class _RunDriver:
        def session(self):
            return _RunSession([])

    monkeypatch.setattr(neo4j_client, "getDriver", lambda: _RunDriver())
    ids = neo4j_client.getKpiCatalogIds()
    assert ids == {"obj:KPI:1", "obj:KPI:2"}


def test_delete_node_accepts_kpi_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """deleteNode 白名单放行 KpiCatalog（图节点删除靠它）。"""
    recorder = _patch(monkeypatch)
    neo4j_client.deleteNode("KpiCatalog", "obj:KPI:1")
    cql, params = recorder[0]
    assert "(n:KpiCatalog {unified_id: $unified_id})" in cql
```

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/backend
pytest app/tests/unit/test_neo4j_kpi_catalog.py -q
```
预期：`test_kpi_catalog_label_whitelisted` FAIL（`"KpiCatalog" not in ...`），其余 FAIL（AttributeError: 函数不存在）。

- [ ] **Step 3: GREEN — 实现**

`neo4j_client.py`：
```python
# :22
_ALLOWED_LABELS = frozenset({"Class", "Property", "Metric", "KpiCatalog"})
```
id-read helpers 区（`getPropertyIds` 后）追加两个函数（结构与 `getClassIds` 同款）：
```python
def getMetricIds() -> set[str]:
    """读全部 Metric 节点 unified_id（对账用；COALESCE 兼容旧节点 id 属性）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            str(record["uid"])
            for record in session.run(
                "MATCH (m:Metric) RETURN COALESCE(m.unified_id, toString(m.id)) AS uid"
            )
        }


def getKpiCatalogIds() -> set[str]:
    """读全部 KpiCatalog 节点 unified_id（对账用）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            str(record["uid"])
            for record in session.run(
                "MATCH (k:KpiCatalog) RETURN COALESCE(k.unified_id, toString(k.id)) AS uid"
            )
        }
```
Upsert 区（`upsertMetricNode` 后）追加：
```python
def upsertKpiCatalogNode(
    unified_id: str,
    code: str,
    name: str | None,
    status: str,
    owner: str | None,
    unit: str | None,
) -> None:
    """按 unified_id 幂等创建/更新 KpiCatalog 节点属性（D3：全状态都建节点）。"""
    driver = getDriver()
    cql = """
        MERGE (k:KpiCatalog {unified_id: $unified_id})
        SET k.code = $code, k.name = $name, k.status = $status,
            k.owner = $owner, k.unit = $unit
        RETURN k
    """
    with driver.session() as session:
        session.run(
            cql, unified_id=unified_id, code=code, name=name,
            status=status, owner=owner, unit=unit,
        )
```
Relationship 区（`linkMetricDerivedFrom` 后）追加：
```python
def linkKpiMeasuredBy(kpiUid: str, metricUid: str) -> None:
    """KpiCatalog -[:MEASURED_BY]-> Metric（按 unified_id 匹配；MERGE 幂等）。"""
    driver = getDriver()
    cql = """
        MATCH (k:KpiCatalog {unified_id: $kpiUid}), (m:Metric {unified_id: $metricUid})
        MERGE (k)-[:MEASURED_BY]->(m)
    """
    with driver.session() as session:
        session.run(cql, kpiUid=kpiUid, metricUid=metricUid)
```

- [ ] **Step 4: 跑 GREEN**：同 Step 2 命令，全 PASS。
- [ ] **Step 5: Commit**
```bash
git add backend/app/infrastructure/neo4j_client.py backend/app/tests/unit/test_neo4j_kpi_catalog.py
git commit -m "feat: Neo4j KpiCatalog label + MEASURED_BY 边 + unified_id 读数"
```

---

### Task 3: `kpi_vector_graph_sync.py` 同步核心

**Files:**
- Create: `backend/app/services/kpi_vector_graph_sync.py`
- Test: `backend/app/tests/unit/test_kpi_vector_graph_sync.py`（新建）
- Test: `backend/app/tests/integration/test_kpi_graph_sync_integration.py`（新建，真 PG + fake neo4j/milvus）

**Interfaces:**
- Consumes: Task 1 的 `milvus.deleteByOntologyIdDual / insertEmbeddingsDual / VALID_EMBEDDING_TYPES`、Task 2 的 `neo4j.upsertKpiCatalogNode / linkKpiMeasuredBy / deleteNode / getKpiCatalogIds`、既有 `IdMappingService.register/resolveByExternal`（register **内部 commit**，与 createMetric 既有模式一致）、`EmbeddingService.generateEmbedding(text) -> list[float]`（async）、`getSessionFactory()`。
- Produces（Task 4/6/7/8 依赖）:
  - `KPI_BUSINESS_OBJECT = "KPI"`（str）；`KPI_NEO4J_LABEL = "KpiCatalog"`
  - `kpiEmbeddingText(kpi: KpiCatalog, metricAlias: str | None) -> str` = `" ".join(filter(None, [kpi_name, metric_alias, *(semantic_keywords or []), business_definition, unit]))`
  - `syncKpiGraphNow(session: AsyncSession, kpiId: int) -> str`（返回 unified_id；无映射时注册）
  - `syncKpiVectorNow(session: AsyncSession, kpiId: int) -> None`（删旧向量；仅 PUBLISHED 重插）
  - `deleteKpiVectorNow(kpiId: int) -> None`；`deleteKpiGraphNow(session: AsyncSession, kpiId: int) -> None`
  - `syncKpiGraphBestEffort(kpiId: int) -> None`；`syncKpiVectorBestEffort(kpiId: int) -> None`；`deleteKpiVectorBestEffort(kpiId: int) -> None`；`deleteKpiGraphBestEffort(kpiId: int) -> None`（各开自己的 session，`asyncio.create_task` + 模块级 `_PENDING_KPI_SYNC_TASKS` 强引用 + done_callback discard，镜像 `ontology_service._PENDING_SYNC_TASKS` 模式）
  - `reconcileKpiGraph(session) -> dict`（Task 6）

**D3 状态语义实现要点**：`syncKpiVectorNow` = 先 `deleteByOntologyIdDual(kpiId, "kpi")`，再查状态——PUBLISHED 才重插；DRAFT/DEPRECATED 保持「已删除」状态（向量消失、图节点保留）。图路径**无条件** upsert（status 属性随节点更新）。

- [ ] **Step 1: RED — 单测（文本构造 + best-effort 装配）**

```python
"""KPI 向量/图同步核心：文本构造与 best-effort 装配（fake double，禁连真 Milvus/Neo4j）。"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.models import KpiCatalog
from app.services import kpi_vector_graph_sync as sync


def _kpi(**over) -> KpiCatalog:
    base = dict(
        id=1, kpi_code="KPI_OTD", kpi_name="准时交付率",
        business_definition="按期到货的采购单占比", unit="%",
        semantic_keywords=["交付", "OTD"], status="PUBLISHED", metric_id=None,
    )
    base.update(over)
    return KpiCatalog(**base)


def test_embedding_text_full() -> None:
    text = sync.kpiEmbeddingText(_kpi(), "on-time delivery")
    assert text == "准时交付率 on-time delivery 交付 OTD 按期到货的采购单占比 %"


def test_embedding_text_skips_empty_segments() -> None:
    text = sync.kpiEmbeddingText(
        _kpi(semantic_keywords=None, business_definition=None, unit=None), None
    )
    assert text == "准时交付率"


def test_embedding_text_is_stable() -> None:
    assert sync.kpiEmbeddingText(_kpi(), None) == sync.kpiEmbeddingText(_kpi(), None)


@pytest.mark.asyncio
async def test_sync_kpi_vector_published_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class _FakeEmbedding:
        async def generateEmbedding(self, text: str) -> list[float]:
            calls.append(("embed", text))
            return [0.1, 0.2]

    async def _fakeNow(session, kpiId):
        calls.append(("vector", kpiId))

    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    # best-effort 装配：spawn 出的任务能被 _PENDING_KPI_SYNC_TASKS 持握（防 GC 丢失）
    sync._spawn(_fakeNow(None, 1))
    await asyncio.sleep(0)
    assert ("vector", 1) in calls
    assert sync._PENDING_KPI_SYNC_TASKS == set()  # done 后回收


def test_constants() -> None:
    assert sync.KPI_BUSINESS_OBJECT == "KPI"
    assert sync.KPI_NEO4J_LABEL == "KpiCatalog"
```

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
pytest app/tests/unit/test_kpi_vector_graph_sync.py -q
```
预期：FAIL（ModuleNotFoundError: kpi_vector_graph_sync）。

- [ ] **Step 3: GREEN — 实现（完整文件）**

```python
"""KPI 目录向量/图同步（feat-kpi-vector-graph-sync，设计 §3/§4）。

镜像 ontology_service 的 best-effort 同步模式：
- 向量：type="kpi" → kpi_catalog_embeddings（只索引 PUBLISHED，D3）；
- 图：KpiCatalog 节点（全状态，MERGE on unified_id）+ MEASURED_BY 边（metric 缺失不建边）；
- id_mapping：business_object="KPI"，unified_id=obj:KPI:{id}（零 DDL）。

best-effort：钩子路径失败只告警，不影响 kpi_catalog_service 主流程（D4 无 outbox）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.domain.schemas import IdMappingCreate
from app.infrastructure import neo4j
from app.infrastructure.milvus_dual_write import (
    deleteByOntologyIdDual,
    insertEmbeddingsDual,
)
from app.services.embedding_service import EmbeddingService
from app.services.id_mapping_service import IdMappingService

logger = logging.getLogger(__name__)

KPI_BUSINESS_OBJECT = "KPI"
KPI_NEO4J_LABEL = "KpiCatalog"
KPI_PG_TABLE = "kpi_catalog"

# 后台同步任务的强引用（create_task 弱引用会被 GC，需持握防丢失）
_PENDING_KPI_SYNC_TASKS: set[asyncio.Task] = set()


def kpiEmbeddingText(kpi: KpiCatalog, metricAlias: str | None) -> str:
    """KPI 向量文本：名称 + 挂靠指标别名 + 语义关键词 + 业务定义 + 单位（设计 §3.1）。"""
    return " ".join(
        filter(
            None,
            [
                kpi.kpi_name,
                metricAlias or "",
                *(kpi.semantic_keywords or []),
                kpi.business_definition or "",
                kpi.unit or "",
            ],
        )
    )


async def _loadKpiWithMetricAlias(
    session: AsyncSession, kpiId: int
) -> tuple[KpiCatalog | None, str | None]:
    """加载 KPI 行 + 挂靠 metric 别名（outerjoin，metric 缺失/无别名返 None）。"""
    stmt = (
        select(KpiCatalog, OntologyMetric.metric_alias)
        .outerjoin(OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id)
        .where(KpiCatalog.id == kpiId)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def _resolveKpiUid(session: AsyncSession, idMappingSvc: IdMappingService, kpi: KpiCatalog) -> str:
    """解析/注册 KPI unified_id（resolveByExternal → register 兜底，镜像 syncMissingGraph 模式）。"""
    row = await idMappingSvc.resolveByExternal(
        session, KPI_BUSINESS_OBJECT, str(kpi.id)
    )
    if row is not None:
        return row.unified_id
    mapping = await idMappingSvc.register(
        session,
        IdMappingCreate(business_object=KPI_BUSINESS_OBJECT, external_id=str(kpi.id)),
        pg_table=KPI_PG_TABLE,
        pg_id=str(kpi.id),
    )
    return mapping.unified_id


async def syncKpiGraphNow(session: AsyncSession, kpiId: int) -> str:
    """幂等建/更新 KpiCatalog 节点 + MEASURED_BY 边；返回 unified_id。"""
    kpi = await session.get(KpiCatalog, kpiId)
    if kpi is None:
        raise ValueError(f"KPI 不存在: id={kpiId}")
    idMappingSvc = IdMappingService()
    unifiedId = await _resolveKpiUid(session, idMappingSvc, kpi)
    neo4j.upsertKpiCatalogNode(
        unified_id=unifiedId,
        code=kpi.kpi_code,
        name=kpi.kpi_name,
        status=kpi.status,
        owner=kpi.owner,
        unit=kpi.unit,
    )
    if kpi.metric_id is not None:
        metricRow = await idMappingSvc.resolveByExternal(
            session, "METRIC", str(kpi.metric_id)
        )
        if metricRow is not None:
            neo4j.linkKpiMeasuredBy(unifiedId, metricRow.unified_id)
    return unifiedId


async def syncKpiVectorNow(session: AsyncSession, kpiId: int) -> None:
    """单条向量重建：删旧 → PUBLISHED 才重插（D3：DRAFT/DEPRECATED 只删不插）。"""
    kpi, metricAlias = await _loadKpiWithMetricAlias(session, kpiId)
    if kpi is None:
        raise ValueError(f"KPI 不存在: id={kpiId}")
    deleteByOntologyIdDual(kpi.id, "kpi")
    if kpi.status != "PUBLISHED":
        logger.info("KPI id=%d status=%s 非 PUBLISHED，向量保持删除", kpiId, kpi.status)
        return
    vec = await EmbeddingService().generateEmbedding(kpiEmbeddingText(kpi, metricAlias))
    insertEmbeddingsDual([{
        "ontology_id": kpi.id,
        "type": "kpi",
        "name": kpi.kpi_name,
        "alias": metricAlias or "",
        "description": kpi.business_definition or "",
        "embedding": vec,
        "external_id": kpi.kpi_code,
    }])


async def deleteKpiVectorNow(kpiId: int) -> None:
    deleteByOntologyIdDual(kpiId, "kpi")


async def deleteKpiGraphNow(session: AsyncSession, kpiId: int) -> None:
    """删图节点（无 unified_id 映射时无事可做——从未入图）。"""
    row = await IdMappingService().resolveByExternal(
        session, KPI_BUSINESS_OBJECT, str(kpiId)
    )
    if row is None:
        return
    neo4j.deleteNode(KPI_NEO4J_LABEL, row.unified_id)


# ---------------------------------------------------------------------------
# best-effort 装配（钩子用；各自开 session，失败仅告警）
# ---------------------------------------------------------------------------

async def _runQuietly(coro: Coroutine[Any, Any, None]) -> None:
    try:
        await coro
    except Exception:
        logger.warning("KPI 向量/图后台同步失败", exc_info=True)


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(_runQuietly(coro))
    _PENDING_KPI_SYNC_TASKS.add(task)
    task.add_done_callback(_PENDING_KPI_SYNC_TASKS.discard)


def _ownSession() -> AsyncSession:
    from app.infrastructure.database import getSessionFactory

    return getSessionFactory()()


async def syncKpiGraphBestEffort(kpiId: int) -> None:
    _spawn(_runGraphOwnSession(kpiId))


async def _runGraphOwnSession(kpiId: int) -> None:
    async with _ownSession() as session:
        await syncKpiGraphNow(session, kpiId)


async def syncKpiVectorBestEffort(kpiId: int) -> None:
    _spawn(_runVectorOwnSession(kpiId))


async def _runVectorOwnSession(kpiId: int) -> None:
    async with _ownSession() as session:
        await syncKpiVectorNow(session, kpiId)


async def deleteKpiVectorBestEffort(kpiId: int) -> None:
    _spawn(_runDeleteVector(kpiId))


async def _runDeleteVector(kpiId: int) -> None:
    await deleteKpiVectorNow(kpiId)


async def deleteKpiGraphBestEffort(kpiId: int) -> None:
    _spawn(_runDeleteGraphOwnSession(kpiId))


async def _runDeleteGraphOwnSession(kpiId: int) -> None:
    async with _ownSession() as session:
        await deleteKpiGraphNow(session, kpiId)
```

- [ ] **Step 4: 跑 GREEN**：同 Step 2 命令，全 PASS。
- [ ] **Step 5: RED — 集成测试（真 PG，fake neo4j/milvus）**

`backend/app/tests/integration/test_kpi_graph_sync_integration.py`：

```python
"""KPI 图/向量同步集成：真 PG（5434 qa_metadata_test），neo4j/milvus 用 fake double。

本地 19530/7687 是生产实例，禁连；graph/milvus 调用全部落到内存 recorder。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete as saDelete

from app.domain.models import IdMapping, KpiCatalog
from app.services import kpi_vector_graph_sync as sync
from app.services.id_mapping_service import IdMappingService

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def seedKpi(integrationDbSession):
    kpi = KpiCatalog(
        kpi_code="KPI_SYNC_TEST", kpi_name="同步测试", status="PUBLISHED",
        match_threshold=Decimal("0.75"),
    )
    integrationDbSession.add(kpi)
    await integrationDbSession.commit()
    return kpi


@pytest.fixture()
def fakeStores(monkeypatch):
    recorder: dict[str, list] = {"upsert": [], "link": [], "delete": [],
                                 "insert": [], "milvusDelete": []}

    class _FakeNeo4j:
        @staticmethod
        def upsertKpiCatalogNode(**kw):
            recorder["upsert"].append(kw)

        @staticmethod
        def linkKpiMeasuredBy(kpiUid, metricUid):
            recorder["link"].append((kpiUid, metricUid))

        @staticmethod
        def deleteNode(label, unified_id):
            recorder["delete"].append((label, unified_id))

    class _FakeEmbedding:
        async def generateEmbedding(self, text):
            return [0.0] * 4

    monkeypatch.setattr(sync, "neo4j", _FakeNeo4j)
    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    monkeypatch.setattr(
        sync, "insertEmbeddingsDual", lambda records: recorder["insert"].extend(records)
    )
    monkeypatch.setattr(
        sync, "deleteByOntologyIdDual",
        lambda oid, typ: recorder["milvusDelete"].append((oid, typ)),
    )
    return recorder


async def cleanupKpiMapping(integrationDbSession, kpiId: int) -> None:
    """id_mapping business_object='KPI' 测试残留必须清掉（共享真 PG 库卫生）。"""
    await integrationDbSession.execute(
        saDelete(IdMapping).where(IdMapping.business_object == "KPI",
                                  IdMapping.pg_id == str(kpiId))
    )
    await integrationDbSession.commit()


async def test_graph_sync_registers_mapping_and_upserts(
    seedKpi, fakeStores, integrationDbSession
):
    kpi = seedKpi
    try:
        uid = await sync.syncKpiGraphNow(integrationDbSession, kpi.id)
        assert uid == f"obj:KPI:{kpi.id}"
        row = await IdMappingService().resolveByExternal(
            integrationDbSession, "KPI", str(kpi.id)
        )
        assert row is not None and row.pg_table == "kpi_catalog"
        assert fakeStores["upsert"][0]["code"] == "KPI_SYNC_TEST"
        assert fakeStores["upsert"][0]["status"] == "PUBLISHED"
    finally:
        await cleanupKpiMapping(integrationDbSession, kpi.id)


async def test_vector_sync_published_then_draft(
    seedKpi, fakeStores, integrationDbSession
):
    kpi = seedKpi
    try:
        await sync.syncKpiVectorNow(integrationDbSession, kpi.id)
        # PUBLISHED：删一次 + 插一次（external_id=kpi_code）
        assert fakeStores["milvusDelete"] == [(kpi.id, "kpi")]
        assert len(fakeStores["insert"]) == 1
        assert fakeStores["insert"][0]["external_id"] == "KPI_SYNC_TEST"
        assert fakeStores["insert"][0]["type"] == "kpi"
        assert fakeStores["insert"][0]["ontology_id"] == kpi.id
        # 转 DRAFT：再跑一次 ⇒ 删两次、不再插入（D3）
        kpi.status = "DRAFT"
        await integrationDbSession.commit()
        await sync.syncKpiVectorNow(integrationDbSession, kpi.id)
        assert fakeStores["milvusDelete"] == [(kpi.id, "kpi"), (kpi.id, "kpi")]
        assert len(fakeStores["insert"]) == 1
    finally:
        await cleanupKpiMapping(integrationDbSession, kpi.id)
```

> conftest 的真 PG session fixture 名以 `app/tests/integration/` 下既有 KPI 集成套件（`test_kpi_catalog_*.py` 及其 conftest）实际使用的名字为准（示例用 `integrationDbSession`，若既有命名不同，照抄既有命名，不要新造 fixture 体系）。

- [ ] **Step 6: 跑集成 GREEN**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_kpi_graph_sync_integration.py -q
```
预期：PASS。
- [ ] **Step 7: Commit**
```bash
git add backend/app/services/kpi_vector_graph_sync.py \
        backend/app/tests/unit/test_kpi_vector_graph_sync.py \
        backend/app/tests/integration/test_kpi_graph_sync_integration.py
git commit -m "feat: kpi_vector_graph_sync 同步核心（向量 PUBLISHED 口径 + 图全状态 + best-effort）"
```

---

### Task 4: 开关 + kpi_catalog_service CRUD 钩子

**Files:**
- Modify: `backend/app/config.py:241-247`（Settings 新字段区）
- Modify: `backend/app/services/kpi_metric_switches.py`（新开关）
- Modify: `backend/app/services/kpi_catalog_service.py`（钩子）
- Test: `backend/app/tests/unit/test_kpi_sync_switches.py`（新建）
- Test: `backend/app/tests/unit/test_kpi_catalog_hooks.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 4 个 `*BestEffort` 函数；Task 3 的 `KPI_BUSINESS_OBJECT`。
- Produces: `config.Settings.kpiSyncEnabled: bool`（default True，alias `KPI_SYNC_ENABLED`）、`config.Settings.kpiMatchVectorEnabled: bool`（default False，alias `KPI_MATCH_VECTOR_ENABLED`）；`kpi_metric_switches.SWITCH_KPI_SYNC = "KPI_SYNC_ENABLED"`、`SWITCH_MATCH_VECTOR = "KPI_MATCH_VECTOR_ENABLED"`、`async isKpiSyncEnabled(session) -> bool`（**默认 True**）、`async isKpiMatchVectorEnabled(session) -> bool`（fail-closed False，Task 9 消费）。

**开关方向说明（必须写进 docstring）**：`isKpiSyncEnabled` 与既有 `isKpiContractInjectionEnabled` 等的方向**相反**——system_config 行**缺失/非法/异常 ⇒ on**（同步是存储面能力，缺行不应让新建 KPI 静默失去向量/图；灰度关闭是一次显式数据操作），env 仍作一键止血（false ⇒ off）。`isKpiMatchVectorEnabled` 沿用 fail-closed 惯例（缺失 ⇒ off，向量召回改变 chat 行为，默认关）。

- [ ] **Step 1: RED — 开关单测**

```python
"""KPI 同步/向量匹配开关：env 杀闸 + system_config 灰度（方向见各 docstring）。"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from app.services import kpi_metric_switches as switches


class _FakeResult:
    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def scalar_one_or_none(self):
        return self._cfg


class _FakeNested:
    def __enter__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeSession:
    """满足 readKpiMetricSwitch 的最小面：begin_nested + execute。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def begin_nested(self):
        return _FakeNested()

    async def execute(self, *_a, **_k):
        return _FakeResult(self._cfg)


@pytest.mark.asyncio
async def test_sync_enabled_env_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiSyncEnabled=False),
    )
    assert await switches.isKpiSyncEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_sync_enabled_default_on_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方向反转：system_config 行缺失 ⇒ on（与 fail-closed 开关相反）。"""
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiSyncEnabled=True),
    )
    assert await switches.isKpiSyncEnabled(_FakeSession(None)) is True


@pytest.mark.asyncio
async def test_sync_enabled_row_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiSyncEnabled=True),
    )
    cfg = SimpleNamespace(value="false")
    assert await switches.isKpiSyncEnabled(_FakeSession(cfg)) is False


@pytest.mark.asyncio
async def test_match_vector_default_off_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fail-closed：行缺失 ⇒ off（消费面灰度惯例）。"""
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiMatchVectorEnabled=True),
    )
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_match_vector_env_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiMatchVectorEnabled=False),
    )
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(None)) is False


@pytest.mark.asyncio
async def test_match_vector_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        switches, "getSettings",
        lambda: SimpleNamespace(kpiMatchVectorEnabled=True),
    )
    cfg = SimpleNamespace(value="true")
    assert await switches.isKpiMatchVectorEnabled(_FakeSession(cfg)) is True
```

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
pytest app/tests/unit/test_kpi_sync_switches.py -q
```
预期：全部 FAIL（AttributeError: 无 isKpiSyncEnabled / isKpiMatchVectorEnabled）。

- [ ] **Step 3: GREEN — 开关实现**

`backend/app/config.py`（`:247` `kpiMetricTierAEnabled` 之后追加）：
```python
    # KPI 向量/图同步开关（feat-kpi-vector-graph-sync）：env 只作一键止血
    kpiSyncEnabled: bool = Field(default=True, alias="KPI_SYNC_ENABLED")
    # KPI 匹配器向量候选步灰度开关（默认关：向量召回改变 chat 行为）
    kpiMatchVectorEnabled: bool = Field(
        default=False, alias="KPI_MATCH_VECTOR_ENABLED"
    )
```

`backend/app/services/kpi_metric_switches.py`（常量区追加；文件末尾追加两个函数）：
```python
SWITCH_KPI_SYNC = "KPI_SYNC_ENABLED"
SWITCH_MATCH_VECTOR = "KPI_MATCH_VECTOR_ENABLED"
```
```python
async def isKpiSyncEnabled(session: AsyncSession) -> bool:
    """KPI 向量/图同步钩子开关（env 杀闸 + system_config 灰度）。

    **方向与既有开关相反**：system_config 行**缺失 / 非法 / 读取异常 ⇒ on**。
    理由：同步是存储面能力——缺行不应让新建 KPI 静默失去向量/图（旧行为是
    「完全没有同步」，缺行回落 on 才是「无行为变化」）；关同步是一次显式数据
    操作。env ``KPI_SYNC_ENABLED=false`` 仍是一键止血（需重启）。
    """
    if not getSettings().kpiSyncEnabled:
        logger.debug("KPI 同步 env 紧急杀闸关闭：跳过向量/图钩子")
        return False
    return await readKpiMetricSwitch(session, SWITCH_KPI_SYNC, True)


async def isKpiMatchVectorEnabled(session: AsyncSession) -> bool:
    """KPI 匹配器向量候选步灰度开关（env 杀闸 + system_config 灰度，fail-closed）。

    与 ``isKpiMatchContainmentEnabled`` 同构（向量召回改变 chat 行为 ⇒
    行缺失 / 非法 / 读取异常 ⇒ off；开 = 显式灰度操作）。
    """
    if not getSettings().kpiMatchVectorEnabled:
        logger.debug("KPI 向量匹配 env 紧急杀闸关闭，纯文本匹配")
        return False
    return await readKpiMetricSwitch(session, SWITCH_MATCH_VECTOR, False)
```

- [ ] **Step 4: 跑 GREEN**：同 Step 2，全 PASS。

- [ ] **Step 5: RED — 钩子单测**

```python
"""kpi_catalog_service CRUD 提交后的向量/图同步钩子（全部打 stub，不碰真库外存储）。"""

from __future__ import annotations

import pytest

from app.dependencies import CurrentUser
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services import kpi_catalog_service as svc_module
from app.services.kpi_catalog_service import KpiCatalogService

pytestmark = pytest.mark.asyncio


def _actor() -> CurrentUser:
    return CurrentUser(userId=1, username="admin", isAdmin=True, departments=None)


def _createDto(code: str = "KPI_HOOK", status: str = "PUBLISHED") -> KpiCatalogCreate:
    return KpiCatalogCreate(
        kpi_code=code, kpi_name="钩子测试", business_definition="d",
        formula="f", numerator="n", denominator="d", grain="g", unit="u",
        status=status, datasource_id=1,
    )


class _Recorder:
    def __init__(self) -> None:
        self.graphSynced: list[int] = []
        self.vectorSynced: list[int] = []
        self.vectorDeleted: list[int] = []
        self.graphDeleted: list[int] = []


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """stub 掉开关与 4 个 best-effort 函数（挂进 kpi_catalog_service 命名空间）。"""
    rec = _Recorder()

    async def _on(session):
        return True

    async def _graph(kpiId):
        rec.graphSynced.append(kpiId)

    async def _vector(kpiId):
        rec.vectorSynced.append(kpiId)

    async def _delVector(kpiId):
        rec.vectorDeleted.append(kpiId)

    async def _delGraph(kpiId):
        rec.graphDeleted.append(kpiId)

    monkeypatch.setattr(svc_module, "isKpiSyncEnabled", _on)
    monkeypatch.setattr(svc_module.kpi_sync, "syncKpiGraphBestEffort", _graph)
    monkeypatch.setattr(svc_module.kpi_sync, "syncKpiVectorBestEffort", _vector)
    monkeypatch.setattr(svc_module.kpi_sync, "deleteKpiVectorBestEffort", _delVector)
    monkeypatch.setattr(svc_module.kpi_sync, "deleteKpiGraphBestEffort", _delGraph)
    return rec
```

用例（接在 fixtures 后；`integrationDbSession`/建 KPI 的 fixture 与既有 `test_kpi_catalog_*.py` 同款，datasource 需先经公共 API 种出 id=1 的活跃 DataSource——照抄既有套件的 seed 方式）：

```python
async def test_create_published_syncs_graph_and_vector(integrationDbSession, recorder):
    await KpiCatalogService().createKpi(
        integrationDbSession, _createDto(status="PUBLISHED"), _actor()
    )
    assert recorder.graphSynced and recorder.vectorSynced


async def test_create_draft_syncs_graph_only(integrationDbSession, recorder):
    await KpiCatalogService().createKpi(
        integrationDbSession, _createDto(status="DRAFT"), _actor()
    )
    assert recorder.graphSynced and recorder.vectorSynced == []


async def test_update_publish_draft_syncs_vector(integrationDbSession, recorder):
    service = KpiCatalogService()
    kpi = await service.createKpi(integrationDbSession, _createDto(status="DRAFT"), _actor())
    recorder.graphSynced.clear(); recorder.vectorSynced.clear()
    await service.updateKpi(
        integrationDbSession, kpi.id, KpiCatalogUpdate(status="PUBLISHED"), _actor()
    )
    assert recorder.vectorSynced == [kpi.id]
    assert recorder.vectorDeleted == []


async def test_update_unpublish_deletes_vector_keeps_graph(integrationDbSession, recorder):
    service = KpiCatalogService()
    kpi = await service.createKpi(integrationDbSession, _createDto(status="PUBLISHED"), _actor())
    recorder.vectorSynced.clear()
    await service.updateKpi(
        integrationDbSession, kpi.id, KpiCatalogUpdate(status="DRAFT"), _actor()
    )
    assert recorder.vectorDeleted == [kpi.id]
    assert recorder.vectorSynced == []
    assert recorder.graphSynced == [kpi.id]  # 图节点保留，status 更新


async def test_delete_deletes_vector_and_graph(integrationDbSession, recorder):
    service = KpiCatalogService()
    kpi = await service.createKpi(integrationDbSession, _createDto(status="PUBLISHED"), _actor())
    recorder.graphSynced.clear(); recorder.vectorSynced.clear()
    await service.deleteKpi(integrationDbSession, kpi.id, _actor())
    assert recorder.vectorDeleted == [kpi.id]
    assert recorder.graphDeleted == [kpi.id]


async def test_switch_off_disables_hooks(
    integrationDbSession, recorder, monkeypatch
):
    async def _off(session):
        return False

    # monkeypatch 叠加：覆盖 recorder fixture 里的 _on，pytest 自动还原。
    monkeypatch.setattr(svc_module, "isKpiSyncEnabled", _off)
    await KpiCatalogService().createKpi(
        integrationDbSession, _createDto(status="PUBLISHED"), _actor()
    )
    assert recorder.graphSynced == []
    assert recorder.vectorSynced == []
```

- [ ] **Step 6: 跑 RED**
```bash
cd <worktree>/backend
pytest app/tests/unit/test_kpi_catalog_hooks.py -q
```
预期：钩子用例 FAIL（svc_module 无 isKpiSyncEnabled / kpi_sync 属性）。

- [ ] **Step 7: GREEN — 钩子实现**

`kpi_catalog_service.py` 顶部 import 区追加：
```python
from app.services import kpi_vector_graph_sync as kpi_sync
from app.services.kpi_metric_switches import isKpiSyncEnabled
```
`createKpi`（`refreshOne` 之后、`logger.info` 之前插入）：
```python
        if await isKpiSyncEnabled(session):
            await kpi_sync.syncKpiGraphBestEffort(entity.id)
            if entity.status == "PUBLISHED":
                await kpi_sync.syncKpiVectorBestEffort(entity.id)
```
`updateKpi`：`entity = await self.getKpi(session, id)` 后立刻捕获 `statusBefore = entity.status`；commit + refresh + `refreshOne` 之后插入：
```python
        if await isKpiSyncEnabled(session):
            await kpi_sync.syncKpiGraphBestEffort(entity.id)
            if entity.status == "PUBLISHED":
                await kpi_sync.syncKpiVectorBestEffort(entity.id)
            elif statusBefore == "PUBLISHED":
                # PUBLISHED → DRAFT/DEPRECATED：删向量、图节点保留（D3）
                await kpi_sync.deleteKpiVectorBestEffort(entity.id)
```
`deleteKpi`（`onKpiChanged` 之后、`logger.info` 之前插入）：
```python
        if await isKpiSyncEnabled(session):
            await kpi_sync.deleteKpiVectorBestEffort(entity.id)
            await kpi_sync.deleteKpiGraphBestEffort(entity.id)
```
模块 docstring 若描述同步能力可顺手更正（非必须）。

- [ ] **Step 8: 跑 GREEN**：`pytest app/tests/unit/test_kpi_catalog_hooks.py app/tests/unit/test_kpi_sync_switches.py -q` 全 PASS；再跑邻近回归：
```bash
pytest app/tests/unit/test_kpi_match_route.py app/tests/unit/test_kpi_match_cache.py -q
```
- [ ] **Step 9: Commit**
```bash
git add backend/app/config.py backend/app/services/kpi_metric_switches.py \
        backend/app/services/kpi_catalog_service.py \
        backend/app/tests/unit/test_kpi_sync_switches.py \
        backend/app/tests/unit/test_kpi_catalog_hooks.py
git commit -m "feat: KPI 同步开关（KPI_SYNC_ENABLED/KPI_MATCH_VECTOR_ENABLED）+ CRUD 提交后 best-effort 钩子"
```

---

### Task 5: 存量 KPI id_mapping 回填脚本

**Files:**
- Create: `backend/scripts/backfill_kpi_id_mapping.py`
- Test: `backend/app/tests/integration/test_backfill_kpi_id_mapping.py`（新建，真 PG）

**Interfaces:**
- Consumes: Task 3 的 `KPI_BUSINESS_OBJECT` / `KPI_PG_TABLE`；`IdMappingService.register/resolveByExternal`（register 内部 commit）。
- Produces: `async backfillKpiIdMappings(session, apply: bool) -> dict`（键：`totalKpis / alreadyMapped / registered / failed / failures`）；CLI `--apply --allow-prod-write` 双闸。

- [ ] **Step 1: RED — 集成测试**

```python
"""存量 KPI id_mapping 回填：真 PG，幂等（重复跑 registered=0）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete as saDelete

from app.domain.models import IdMapping, KpiCatalog
from app.services.id_mapping_service import IdMappingService
from scripts.backfill_kpi_id_mapping import backfillKpiIdMappings

pytestmark = pytest.mark.asyncio

_TEST_CODES = ("KPI_BF_TEST_A", "KPI_BF_TEST_B")


async def _cleanup(integrationDbSession) -> None:
    await integrationDbSession.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "KPI",
            IdMapping.pg_table == "kpi_catalog",
        )
    )
    await integrationDbSession.commit()


@pytest.fixture()
def seedKpis(integrationDbSession):
    made: list[KpiCatalog] = []
    for code in _TEST_CODES:
        kpi = KpiCatalog(
            kpi_code=code, kpi_name=f"回填 {code}", status="PUBLISHED",
            match_threshold=Decimal("0.75"),
        )
        integrationDbSession.add(kpi)
        made.append(kpi)
    await integrationDbSession.commit()
    return made


async def test_backfill_registers_and_is_idempotent(seedKpis, integrationDbSession):
    try:
        first = await backfillKpiIdMappings(integrationDbSession, apply=True)
        assert first["registered"] >= len(seedKpis)
        assert first["failed"] == 0
        second = await backfillKpiIdMappings(integrationDbSession, apply=True)
        # 幂等：复跑不再注册任何新行
        assert second["registered"] == 0
        row = await IdMappingService().resolveByExternal(
            integrationDbSession, "KPI", str(seedKpis[0].id)
        )
        assert row is not None
        assert row.unified_id == f"obj:KPI:{seedKpis[0].id}"
    finally:
        await _cleanup(integrationDbSession)
```

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_backfill_kpi_id_mapping.py -q
```
预期：FAIL（ModuleNotFoundError: scripts.backfill_kpi_id_mapping）。

- [ ] **Step 3: GREEN — 实现脚本**

```python
"""存量 KPI 目录 id_mapping 回填（幂等，feat-kpi-vector-graph-sync §3.3）。

KPI 此前从未注册 unified_id；向量/图同步需要 obj:KPI:{id}。本脚本为全部
kpi_catalog 行（含 DRAFT/DEPRECATED——id_mapping 是身份层，全量注册；
向量索引才按 PUBLISHED 过滤，见 kpi_vector_graph_sync）补注册 id_mapping。

用法（本地 dev）：
    uv run python scripts/backfill_kpi_id_mapping.py --dry-run
    uv run python scripts/backfill_kpi_id_mapping.py --apply
生产（发版 runbook 步骤，需双闸）：
    uv run python scripts/backfill_kpi_id_mapping.py --apply --allow-prod-write
缺 --allow-prod-write 且 Settings.isProduction ⇒ 拒绝执行（fail-closed）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.models import KpiCatalog
from app.infrastructure.database import getSessionFactory
from app.services.id_mapping_service import IdMappingService
from app.services.kpi_vector_graph_sync import KPI_BUSINESS_OBJECT, KPI_PG_TABLE


async def backfillKpiIdMappings(session: AsyncSession, apply: bool) -> dict:
    """幂等回填：已注册跳过，未注册补 register。返回摘要 dict。"""
    idMappingSvc = IdMappingService()
    rows = (await session.execute(select(KpiCatalog))).scalars().all()
    registered = 0
    alreadyMapped = 0
    failed = 0
    failures: list[dict] = []
    for kpi in rows:
        existing = await idMappingSvc.resolveByExternal(
            session, KPI_BUSINESS_OBJECT, str(kpi.id)
        )
        if existing is not None:
            alreadyMapped += 1
            continue
        if not apply:
            print(f"  [dry-run] 将注册 id={kpi.id} code={kpi.kpi_code}")
            continue
        try:
            mapping = await idMappingSvc.register(
                session,
                IdMappingCreate(
                    business_object=KPI_BUSINESS_OBJECT,
                    external_id=str(kpi.id),
                ),
                pg_table=KPI_PG_TABLE,
                pg_id=str(kpi.id),
            )
            registered += 1
            print(f"  ok id={kpi.id} code={kpi.kpi_code} uid={mapping.unified_id}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append({"kpiId": kpi.id, "error": str(exc)})
            print(f"  FAIL id={kpi.id} code={kpi.kpi_code}: {exc}")
    summary = {
        "totalKpis": len(rows),
        "alreadyMapped": alreadyMapped,
        "registered": registered,
        "failed": failed,
        "failures": failures,
    }
    print(
        f"KPI id_mapping 回填: 共 {summary['totalKpis']}，"
        f"已存在 {alreadyMapped}，新注册 {registered}，失败 {failed}"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写入（缺省 dry-run）")
    parser.add_argument(
        "--allow-prod-write", action="store_true",
        help="生产环境写库第二闸（isProduction 且缺此项 ⇒ 拒绝）",
    )
    args = parser.parse_args(argv)
    settings = getSettings()
    if settings.isProduction and args.apply and not args.allow_prod_write:
        print("拒绝：生产环境需显式 --allow-prod-write（fail-closed）")
        return 2
    return asyncio.run(_run(apply=args.apply))


async def _run(apply: bool) -> int:
    factory = getSessionFactory()
    async with factory() as session:
        summary = await backfillKpiIdMappings(session, apply=apply)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

顶部 import 区补 `from sqlalchemy import select` 与 `from app.domain.schemas import IdMappingCreate`（上面函数体已用到）。

- [ ] **Step 4: 跑 GREEN**：同 Step 2，全 PASS。
- [ ] **Step 5: dry-run 冒烟（只读 dev 5433 不写库；如环境未起则跳过并记录）**
```bash
cd <worktree>/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5433/qa_metadata' \
  uv run python scripts/backfill_kpi_id_mapping.py --dry-run
```
预期：打印 12 条左右存量 KPI 的「将注册」行，无写操作。
- [ ] **Step 6: Commit**
```bash
git add backend/scripts/backfill_kpi_id_mapping.py backend/app/tests/integration/test_backfill_kpi_id_mapping.py
git commit -m "feat: 存量 KPI id_mapping 幂等回填脚本（--apply --allow-prod-write 双闸）"
```

---

### Task 6: `syncMissingGraph` 补 Metric 节点 + KPI 图对账

**Files:**
- Modify: `backend/app/services/kpi_vector_graph_sync.py`（追加 `reconcileKpiGraph`）
- Modify: `backend/app/services/ontology_service.py:1837-2006`（`syncMissingGraph`）
- Modify: `backend/app/api/v1/ontology.py:849-865`（`GraphSyncMissingResult`）
- Test: `backend/app/tests/integration/test_kpi_reconcile_graph.py`（新建，真 PG + fake neo4j）

**Interfaces:**
- Consumes: Task 2 `neo4j.getMetricIds / getKpiCatalogIds / upsertMetricNode / linkMetricDerivedFrom / upsertKpiCatalogNode / linkKpiMeasuredBy`；Task 3 的 `_resolveKpiUid`（模块私有，reconcile 在同模块内调用）；既有 `IdMappingService`。
- Produces:
  - `kpi_vector_graph_sync.reconcileKpiGraph(session) -> dict`，键：`totalKpis / missingKpiCount / syncedKpiCount / failedCount / failures: list[dict(entityType="kpi", entityId, error)]`。语义：全部 KPI 行解析 uid（缺失注册）；节点缺失才 upsert 并计数；MEASURED_BY 恒 MERGE（幂等，不计数）。
  - `syncMissingGraph` 返回 dict 新增键：`totalMetrics / missingMetricCount / syncedMetricCount`（Metric **图节点**口径）、`totalKpis / missingKpiCount / syncedKpiCount`（来自 reconcileKpiGraph），`failedCount/failures` 汇总三类。
  - `GraphSyncMissingResult` 新增字段（带默认 0，老响应不破坏）：`totalMetrics: int = 0`、`missingMetricCount: int = 0`、`syncedMetricCount: int = 0`、`totalKpis: int = 0`、`missingKpiCount: int = 0`、`syncedKpiCount: int = 0`。

- [ ] **Step 1: RED — 集成测试（真 PG，fake neo4j）**

```python
"""syncMissingGraph 的 Metric/KPI 图节点对账段（真 PG + fake neo4j）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.models import IdMapping, KpiCatalog, OntologyMetric
from app.services import kpi_vector_graph_sync as sync
from app.services.id_mapping_service import IdMappingService
from app.services.ontology_service import OntologyService

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def neo4jRecorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """fake neo4j 全套读/写（禁连真 7687）。"""
    rec: dict[str, list] = {
        "kpiUpsert": [], "metricUpsert": [], "measuredBy": [], "derivedFrom": [],
        "kpiIds": set(), "metricIds": set(),
    }

    class _FakeNeo4j:
        @staticmethod
        def getKpiCatalogIds():
            return set(rec["kpiIds"])

        @staticmethod
        def getMetricIds():
            return set(rec["metricIds"])

        @staticmethod
        def getClassIds():
            return set()

        @staticmethod
        def getPropertyIds():
            return set()

        @staticmethod
        def getJoinPairs():
            return set()

        @staticmethod
        def getRelationTriples():
            return set()

        @staticmethod
        def upsertKpiCatalogNode(**kw):
            rec["kpiUpsert"].append(kw)
            rec["kpiIds"].add(kw["unified_id"])

        @staticmethod
        def upsertMetricNode(**kw):
            rec["metricUpsert"].append(kw)
            rec["metricIds"].add(kw["unified_id"])

        @staticmethod
        def linkKpiMeasuredBy(kpiUid, metricUid):
            rec["measuredBy"].append((kpiUid, metricUid))

        @staticmethod
        def linkMetricDerivedFrom(metricUid, classUid):
            rec["derivedFrom"].append((metricUid, classUid))

        # syncMissingGraph 还会触达的既有写函数：record 一律 no-op
        @staticmethod
        def upsertClassNode(**kw):
            pass

        @staticmethod
        def upsertPropertyNode(**kw):
            pass

        @staticmethod
        def reconcileClassSubclassOf(*a, **k):
            pass

        @staticmethod
        def linkClassHasProperty(*a, **k):
            pass

        @staticmethod
        def linkPropertyReferences(*a, **k):
            pass

        @staticmethod
        def linkClassJoin(*a, **k):
            pass

        @staticmethod
        def reconcileClassRelation(*a, **k):
            pass

    monkeypatch.setattr(sync, "neo4j", _FakeNeo4j)
    from app.services import ontology_service as osvc
    monkeypatch.setattr(osvc, "neo4j", _FakeNeo4j)
    return rec


@pytest.fixture()
def seedGraphRows(integrationDbSession):
    metric = OntologyMetric(
        metric_name="同步率", formula="SUM(a)/SUM(b)", agg_function="ratio",
    )
    integrationDbSession.add(metric)
    await integrationDbSession.flush()
    kpi = KpiCatalog(
        kpi_code="KPI_RECON_TEST", kpi_name="对账测试", status="PUBLISHED",
        metric_id=metric.id, match_threshold=Decimal("0.75"),
    )
    integrationDbSession.add(kpi)
    await integrationDbSession.commit()
    return metric, kpi


async def _cleanup(integrationDbSession, metricId: int, kpiId: int) -> None:
    """按 seed 出的真实 id 精确清理 id_mapping（禁止全表删）。"""
    from sqlalchemy import delete as saDelete

    await integrationDbSession.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "KPI",
            IdMapping.pg_id == str(kpiId),
        )
    )
    await integrationDbSession.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "METRIC",
            IdMapping.pg_id == str(metricId),
        )
    )
    await integrationDbSession.commit()
```

用例（接在 fixtures 后）：
```python
async def test_reconcile_kpi_graph_idempotent(
    seedGraphRows, neo4jRecorder, integrationDbSession
):
    metric, kpi = seedGraphRows
    try:
        # 预置 METRIC id_mapping（模拟既有 metric 已注册，KPI 未注册）
        await IdMappingService().register(
            integrationDbSession,
            IdMappingCreate(business_object="METRIC", external_id=str(metric.id)),
            pg_table="ontology_metric", pg_id=str(metric.id),
        )
        first = await sync.reconcileKpiGraph(integrationDbSession)
        assert first["missingKpiCount"] == 1
        assert first["syncedKpiCount"] == 1
        assert first["failedCount"] == 0
        assert neo4jRecorder["kpiUpsert"][0]["unified_id"] == f"obj:KPI:{kpi.id}"
        # MEASURED_BY：metric_id 挂靠 + METRIC 映射已注册 ⇒ 建边
        assert neo4jRecorder["measuredBy"] == [(f"obj:KPI:{kpi.id}", f"obj:METRIC:{metric.id}")]
        second = await sync.reconcileKpiGraph(integrationDbSession)
        assert second["syncedKpiCount"] == 0  # 幂等：节点已在，不再 upsert
    finally:
        await _cleanup(integrationDbSession, metric.id, kpi.id)


async def test_sync_missing_graph_covers_metric_and_kpi(
    seedGraphRows, neo4jRecorder, integrationDbSession
):
    metric, kpi = seedGraphRows
    try:
        summary = await OntologyService().syncMissingGraph(integrationDbSession)
        assert summary["totalMetrics"] >= 1
        assert summary["syncedMetricCount"] >= 1
        assert summary["syncedKpiCount"] >= 1
        assert summary["totalKpis"] >= 1
        assert summary["failedCount"] == 0
        assert any(m["unified_id"] == f"obj:METRIC:{metric.id}"
                   for m in neo4jRecorder["metricUpsert"])
    finally:
        await _cleanup(integrationDbSession, metric.id, kpi.id)
```

（顶部 import 补 `from app.domain.schemas import IdMappingCreate`。）

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_kpi_reconcile_graph.py -q
```
预期：FAIL（AttributeError: reconcileKpiGraph 不存在；summary 键 KeyError）。

- [ ] **Step 3: GREEN — 实现**

`kpi_vector_graph_sync.py` 末尾追加：
```python
async def reconcileKpiGraph(session: AsyncSession) -> dict[str, Any]:
    """KPI 图对账兜底：解析/注册 uid → 补缺失节点 → MEASURED_BY 恒 MERGE（幂等）。

    与 syncMissingGraph 的类/属性段同语义：只补缺失、不删陈旧；单条失败不中断。
    """
    from sqlalchemy import select as saSelect

    idMappingSvc = IdMappingService()
    kpis = list((await session.execute(saSelect(KpiCatalog))).scalars().all())
    existingUids = neo4j.getKpiCatalogIds()
    syncedKpis = 0
    failures: list[dict[str, Any]] = []
    for kpi in kpis:
        try:
            unifiedId = await _resolveKpiUid(session, idMappingSvc, kpi)
            if unifiedId not in existingUids:
                neo4j.upsertKpiCatalogNode(
                    unified_id=unifiedId,
                    code=kpi.kpi_code,
                    name=kpi.kpi_name,
                    status=kpi.status,
                    owner=kpi.owner,
                    unit=kpi.unit,
                )
                syncedKpis += 1
            if kpi.metric_id is not None:
                metricRow = await idMappingSvc.resolveByExternal(
                    session, "METRIC", str(kpi.metric_id)
                )
                if metricRow is not None:
                    neo4j.linkKpiMeasuredBy(unifiedId, metricRow.unified_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KPI 图对账失败 id=%d code=%s: %s", kpi.id, kpi.kpi_code, exc)
            failures.append({"entityType": "kpi", "entityId": kpi.id, "error": str(exc)})
    return {
        "totalKpis": len(kpis),
        # 与 syncMissingGraph 既有键同语义（= 本次补齐数，见 ontology_service.py
        # missingClassCount: syncedClasses 的先例）
        "missingKpiCount": syncedKpis,
        "syncedKpiCount": syncedKpis,
        "failedCount": len(failures),
        "failures": failures,
    }
```

`ontology_service.py` `syncMissingGraph`（`:1852` 读 Neo4j 现状处加一行）：
```python
        existingMetricUids = neo4j.getMetricIds()
```
属性段之后、`_reconcileJoins` 之前插入 Metric 段：
```python
        syncedMetrics = 0
        for metric in metrics:
            metric_uid_row = await id_mapping_svc.resolveByExternal(
                session, "METRIC", str(metric.id)
            )
            if metric_uid_row is None:
                try:
                    mapping = await id_mapping_svc.register(
                        session,
                        IdMappingCreate(
                            business_object="METRIC",
                            external_id=str(metric.id),
                        ),
                        pg_table="ontology_metric",
                        pg_id=str(metric.id),
                    )
                    metric_uid = mapping.unified_id
                except Exception as exc:  # noqa: BLE001
                    failures.append({
                        "entityType": "metric", "entityId": metric.id,
                        "error": f"id_mapping 注册失败: {exc}",
                    })
                    continue
            else:
                metric_uid = metric_uid_row.unified_id

            if metric_uid in existingMetricUids:
                continue
            try:
                neo4j.upsertMetricNode(
                    unified_id=metric_uid,
                    name=metric.metric_name,
                    alias=metric.metric_alias,
                    formula=metric.formula,
                    aggFunction=metric.agg_function,
                )
                if metric.target_class_id:
                    class_uid_row = await self._resolveClassUid(
                        session, id_mapping_svc, metric.target_class_id
                    )
                    if class_uid_row:
                        neo4j.linkMetricDerivedFrom(metric_uid, class_uid_row)
                syncedMetrics += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "图对账：指标入图失败 id=%d name=%s: %s",
                    metric.id, metric.metric_name, exc,
                )
                failures.append({
                    "entityType": "metric", "entityId": metric.id, "error": str(exc),
                })
```
`syncMissingGraph` 开头取数处加 `metrics = await self.listMetrics(session)`；join/relation reconcile 之后追加 KPI 段与汇总：
```python
        kpiSummary = await kpi_sync.reconcileKpiGraph(session)
        failures = [*failures, *kpiSummary["failures"]]
```
（顶部 import `from app.services import kpi_vector_graph_sync as kpi_sync`——若构成循环 import，改为方法体内延迟 import，与 `_syncMissingClassEmbeddingsNow` 的延迟 import 同款。）
返回 dict 追加键：
```python
            "totalMetrics": len(metrics),
            "missingMetricCount": syncedMetrics,
            "syncedMetricCount": syncedMetrics,
            "totalKpis": kpiSummary["totalKpis"],
            "missingKpiCount": kpiSummary["missingKpiCount"],
            "syncedKpiCount": kpiSummary["syncedKpiCount"],
```
日志行同步补 metric/kpi 计数（可读性）。

`ontology.py` `GraphSyncMissingResult` 追加字段（默认 0）：
```python
    totalMetrics: int = 0
    missingMetricCount: int = 0
    syncedMetricCount: int = 0
    totalKpis: int = 0
    missingKpiCount: int = 0
    syncedKpiCount: int = 0
```

- [ ] **Step 4: 跑 GREEN**：同 Step 2 全 PASS；回归既有图对账套件：
```bash
pytest app/tests/unit/test_milvus_kpi_routing.py -q
pytest app/tests/integration/test_kpi_graph_sync_integration.py app/tests/integration/test_backfill_kpi_id_mapping.py -q
```
再跑既有 `syncMissingGraph` 相关测试（`grep -rl "syncMissingGraph\|GraphSyncMissingResult" app/tests/` 找到的文件，定向跑）——**净新增失败必须为 0**。
- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kpi_vector_graph_sync.py backend/app/services/ontology_service.py \
        backend/app/api/v1/ontology.py backend/app/tests/integration/test_kpi_reconcile_graph.py
git commit -m "feat: syncMissingGraph 补 Metric 图节点 + KpiCatalog 图对账（响应字段扩展）"
```

---

### Task 7: admin 手动同步端点 + 诊断同步健康

**Files:**
- Modify: `backend/app/services/kpi_vector_graph_sync.py`（追加 `syncMissingKpi`）
- Modify: `backend/app/api/v1/kpi_catalog.py`（模块 docstring 更正 + 2 个新端点 + `KpiSyncMissingResult`）
- Modify: `backend/app/api/v1/kpi_diagnose.py`（`KpiSyncHealth` + `sync_health` + `distance`）
- Test: `backend/app/tests/integration/test_kpi_sync_endpoints.py`（新建）+ `test_kpi_diagnose.py` 追加

**Interfaces:**
- Consumes: Task 3 全部函数（`syncKpiVectorNow / deleteKpiVectorNow / reconcileKpiGraph`）；Task 1 `queryKpiEmbeddings`、Task 1 `insertEmbeddingsDual`；既有 admin 依赖（`getAdminOnlyActor`，与 `kpi_catalog.py` 现有端点同款）。
- Produces:
  - `kpi_vector_graph_sync.syncMissingKpi(session) -> dict`，键（扁平）：`vectorPublishedTotal / vectorMissingCount / vectorSyncedCount / vectorOrphanCount / graphMissingCount / graphSyncedCount / failedCount / failures: list[dict]`。语义：向量段 = 期望集（PUBLISHED）− Milvus 现有（`queryKpiEmbeddings()` 返回的 `ontology_id` 键）→ 缺失批量补（`syncKpiVectorNow` 逐条）+ 孤儿删（Milvus 有、PG 非 PUBLISHED/不存在）；随后调 `reconcileKpiGraph`。**幂等**：复跑 counts 全 0。
  - 端点 `POST /api/v1/kpi-catalog/embeddings/sync-missing`（admin）→ 200 `KpiSyncMissingResult`；`POST /api/v1/kpi-catalog/{id}/embedding`（admin）→ 200 `{resynced: true}`；非 PUBLISHED ⇒ 409（`detail` 说明只索引 PUBLISHED）。
  - `kpi_diagnose.py`：`KpiSyncHealth`（`kpiPublishedPgCount / kpiVectorCount / kpiGraphNodeCount / errors: list[str]`）；`KpiDiagnoseRead.sync_health: KpiSyncHealth | None = None`；`KpiDiagnoseMatch.distance: float | None = None`。

- [ ] **Step 1: RED — 端点集成测试（真 PG + fake Milvus/Neo4j，fakeStores recorder 复用 Task 3 同款）**

```python
"""kpi-catalog 手动同步端点（admin 门禁 + 409 语义 + 幂等）。"""

from __future__ import annotations

import pytest

from app.domain.models import KpiCatalog
from app.services import kpi_vector_graph_sync as sync

pytestmark = pytest.mark.asyncio


async def test_sync_missing_endpoint_idempotent(
    client, adminHeaders, seedPublishedKpi, fakeKpiStores, integrationDbSession
):
    """复跑第二轮 counts 全 0（验收标准 3）。"""
    # 预置 Milvus：期望集内已有该 KPI 向量
    fakeKpiStores["milvusPresent"].add(str(seedPublishedKpi.id))
    fakeKpiStores["kpiIds"].add(f"obj:KPI:{seedPublishedKpi.id}")

    first = await client.post(
        "/api/v1/kpi-catalog/embeddings/sync-missing", headers=adminHeaders
    )
    assert first.status_code == 200
    body = first.json()
    assert body["vectorMissingCount"] == 0
    assert body["graphMissingCount"] == 0

    second = await client.post(
        "/api/v1/kpi-catalog/embeddings/sync-missing", headers=adminHeaders
    )
    body2 = second.json()
    assert body2["vectorMissingCount"] == 0
    assert body2["vectorSyncedCount"] == 0
    assert body2["vectorOrphanCount"] == 0
    assert body2["graphSyncedCount"] == 0


async def test_sync_missing_requires_admin(client, userHeaders):
    resp = await client.post(
        "/api/v1/kpi-catalog/embeddings/sync-missing", headers=userHeaders
    )
    assert resp.status_code in (401, 403)


async def test_resync_endpoint_409_for_draft(
    client, adminHeaders, seedDraftKpi, fakeKpiStores
):
    resp = await client.post(
        f"/api/v1/kpi-catalog/{seedDraftKpi.id}/embedding", headers=adminHeaders
    )
    assert resp.status_code == 409
    assert "PUBLISHED" in resp.json()["detail"]


async def test_resync_endpoint_rebuilds_vector(
    client, adminHeaders, seedPublishedKpi, fakeKpiStores
):
    resp = await client.post(
        f"/api/v1/kpi-catalog/{seedPublishedKpi.id}/embedding",
        headers=adminHeaders,
    )
    assert resp.status_code == 200
    assert resp.json()["resynced"] is True
    # delete-first 后按 PUBLISHED 重插：先删后插各一次
    assert str(seedPublishedKpi.id) in fakeKpiStores["vectorDeletes"]
    assert str(seedPublishedKpi.id) in fakeKpiStores["vectorInserts"]
```

（`client / adminHeaders / userHeaders / seedPublishedKpi / seedDraftKpi / fakeKpiStores` 与 Task 3/4 的既有 conftest fixture 同名同款——`fakeKpiStores` 提供 `milvusPresent: set[str]`、`kpiIds: set[str]`、`vectorDeletes: set[str]`、`vectorInserts: set[str]`，并 monkeypatch `sync.neo4j` 与 `sync.milvus`（`queryKpiEmbeddings` 返回 `milvusPresent`，`deleteByOntologyIdDual` 记录进 `vectorDeletes`，`insertEmbeddingsDual` 记录进 `vectorInserts`）。若 Task 3 的 fixture 命名不同，以 Task 3 实际实现为准并保持一致。）

- [ ] **Step 2: 跑 RED**
```bash
pytest app/tests/integration/test_kpi_sync_endpoints.py -q
```
预期：404（路由不存在）。
- [ ] **Step 3: GREEN — `syncMissingKpi` 实现（`kpi_vector_graph_sync.py` 追加）**

```python
async def syncMissingKpi(session: AsyncSession) -> dict[str, Any]:
    """全量对账：向量（PUBLISHED 期望集 vs Milvus 现有）+ 图（reconcileKpiGraph）。幂等。"""
    from sqlalchemy import select as saSelect

    failures: list[dict[str, Any]] = []
    kpis = list((await session.execute(saSelect(KpiCatalog))).scalars().all())
    published = [k for k in kpis if k.status == "PUBLISHED"]
    presentIds: set[str] = set()
    try:
        presentIds = {
            str(r["ontology_id"])
            for r in await milvus.queryKpiEmbeddings()
            if r.get("ontology_id") is not None
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("syncMissingKpi 读 Milvus 失败: %s", exc)
        failures.append({"entityType": "milvus", "entityId": None, "error": str(exc)})

    publishedIds = {str(k.id) for k in published}
    missing = [k for k in published if str(k.id) not in presentIds]
    orphanIds = presentIds - publishedIds
    vectorSynced = 0
    for kpi in missing:
        try:
            await syncKpiVectorNow(session, kpi.id)  # missing 已是 PUBLISHED-only ⇒ 必重插
            vectorSynced += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("syncMissingKpi 补向量失败 id=%d: %s", kpi.id, exc)
            failures.append({"entityType": "kpi", "entityId": kpi.id, "error": str(exc)})
    for orphanId in sorted(orphanIds):
        try:
            await deleteKpiVectorNow(int(orphanId))  # 纯 Milvus 删除，无 DB 写
        except Exception as exc:  # noqa: BLE001
            logger.warning("syncMissingKpi 删孤儿失败 id=%s: %s", orphanId, exc)
            failures.append({"entityType": "kpi", "entityId": orphanId, "error": str(exc)})

    graph = await reconcileKpiGraph(session)
    failures = [*failures, *graph["failures"]]
    # 孤儿删除走 release+load 才对后续检索可见（Milvus delete 可见性既有约定：
    # refreshCollection = release + load + wait_for_loading_complete，milvus_client.py:597）
    if orphanIds:
        refreshCollection(ensureKpiCollection())
    return {
        "vectorPublishedTotal": len(published),
        "vectorMissingCount": len(missing),
        "vectorSyncedCount": vectorSynced,
        "vectorOrphanCount": len(orphanIds),
        "graphMissingCount": graph["missingKpiCount"],
        "graphSyncedCount": graph["syncedKpiCount"],
        "failedCount": len(failures),
        "failures": failures,
    }
```

> `deleteKpiVectorNow(kpiId)` 为纯 Milvus 删除（Task 3 签名，无 DB 写）⇒ 孤儿删除无需 session commit；`reconcileKpiGraph` 内部的 id_mapping 注册自带 commit（Task 6 语义）。端点层的 `await session.commit()` 兜底未决变更。孤儿删除后的可见性用 `milvus_client.refreshCollection(collection)`（`milvus_client.py:597`）——它内部就是 release + load + `wait_for_loading_complete`；**不要**自造 `releaseCollectionsForTypes` 之类函数（milvus_client 无此 API）。`ensureKpiCollection` 为 Task 1 产出（`milvus_dual_write`，re-export 自 milvus_client）。

`kpi_catalog.py`：模块 docstring 第一行改为「KPI 目录 API——向量（Milvus）与图（Neo4j）同步纳入本体治理（2026-11-13 起 KPI 与类/属性同权）」；追加响应模型与端点：
```python
class KpiSyncMissingResult(BaseModel):
    """sync-missing 对账结果（扁平键，幂等复跑全 0）。"""

    vectorPublishedTotal: int
    vectorMissingCount: int
    vectorSyncedCount: int
    vectorOrphanCount: int
    graphMissingCount: int
    graphSyncedCount: int
    failedCount: int
    failures: list[dict]


@router.post("/embeddings/sync-missing", response_model=KpiSyncMissingResult)
async def syncMissingKpiEmbeddings(
    session: SessionDep,
    actor: ActorDep = Depends(getAdminOnlyActor),
) -> KpiSyncMissingResult:
    """全量对账 KPI 向量+图（删孤儿/补缺失），幂等。"""
    result = await kpi_sync.syncMissingKpi(session)
    await session.commit()
    return KpiSyncMissingResult(**result)


@router.post("/{kpi_id}/embedding")
async def resyncKpiEmbedding(
    kpi_id: int,
    session: SessionDep,
    actor: ActorDep = Depends(getAdminOnlyActor),
) -> dict:
    """单条重建向量（delete-first + PUBLISHED 重插）。非 PUBLISHED ⇒ 409。"""
    kpi = await session.get(KpiCatalog, kpi_id)
    if kpi is None:
        raise HTTPException(status_code=404, detail="KPI 不存在")
    if kpi.status != KpiStatus.PUBLISHED:
        raise HTTPException(
            status_code=409,
            detail=f"仅 PUBLISHED 状态的 KPI 建向量（当前 {kpi.status}）",
        )
    await kpi_sync.syncKpiVectorNow(session, kpi_id)
    await session.commit()
    return {"resynced": True}
```
**路由顺序**：两个新端点放在文件里所有 `/{kpi_id}` 动态路由**之前**（`/embeddings/sync-missing` 若放动态路由后面会被 `/embeddings` 段吞——与文件头既有 route-ordering 注释同款，追加一行说明）。

- [ ] **Step 4: GREEN — 诊断页同步健康 + distance（`kpi_diagnose.py`）**

```python
class KpiSyncHealth(BaseModel):
    """KPI 同步健康快照（best-effort：取数失败进 errors，不 500）。

    有意偏离模块头「异常不吞」承诺：diagnose 的匹配段是故障即 500，
    而同步健康是巡检信息——Milvus/Neo4j 不可用时应仍能返回 PG 口径。
    """

    kpiPublishedPgCount: int
    kpiVectorCount: int | None
    kpiGraphNodeCount: int | None
    errors: list[str]


async def _kpiSyncHealth(session: AsyncSession) -> KpiSyncHealth:
    from sqlalchemy import func, select

    errors: list[str] = []
    pgCount = int(
        (await session.execute(
            select(func.count(KpiCatalog.id)).where(KpiCatalog.status == "PUBLISHED")
        )).scalar_one()
    )
    vectorCount: int | None = None
    graphCount: int | None = None
    try:
        rows = await milvus.queryKpiEmbeddings()
        vectorCount = len(rows)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"milvus: {exc}")
    try:
        graphCount = len(neo4j.getKpiCatalogIds())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"neo4j: {exc}")
    return KpiSyncHealth(
        kpiPublishedPgCount=pgCount,
        kpiVectorCount=vectorCount,
        kpiGraphNodeCount=graphCount,
        errors=errors,
    )
```
`KpiDiagnoseMatch` 追加 `distance: float | None = None`；`_matchOf` 签名加 `distance: float | None = None` 末位参数并透传；`diagnoseKpi` 的向量 fallback 分支（Task 9 加）命中处传 `distance=candidate.distance`；`KpiDiagnoseRead` 追加 `sync_health: KpiSyncHealth | None = None`；`diagnoseKpi` 末尾（成功路径）`read.sync_health = await _kpiSyncHealth(session)`（包 try/except 置 None——巡检信息不阻塞诊断主流程，errors 进 health.errors）。
测试追加到 `test_kpi_diagnose.py`：fake Milvus 抛异常 ⇒ `sync_health.errors` 非空且整体 200；正常 ⇒ 三计数齐全。

- [ ] **Step 5: 跑 GREEN**
```bash
pytest app/tests/integration/test_kpi_sync_endpoints.py app/tests/integration/test_kpi_diagnose.py -q
```
全 PASS。
- [ ] **Step 6: Commit**
```bash
git add backend/app/services/kpi_vector_graph_sync.py backend/app/api/v1/kpi_catalog.py \
        backend/app/api/v1/kpi_diagnose.py backend/app/tests/integration/test_kpi_sync_endpoints.py \
        backend/app/tests/integration/test_kpi_diagnose.py
git commit -m "feat: kpi-catalog 手动同步端点（sync-missing/单条重建）+ 诊断同步健康"
```

---

### Task 8: 回填脚本排雷——4 collection 全覆盖 + metric/kpi 段

**Files:**
- Modify: `backend/scripts/backfill_milvus_embeddings.py`
- Test: `backend/app/tests/unit/test_backfill_milvus_script.py`（新建，monkeypatch 禁连真 Milvus）+ 集成断言并入 `test_kpi_graph_sync_integration.py` 旁路新文件 `backend/app/tests/integration/test_backfill_expected_from_pg.py`

**Interfaces:**
- Consumes: Task 1 `_KPI_COLLECTION_NAME`（经 `milvus_client` re-export）、`insertEmbeddingsDual`；Task 3 `kpi_sync.syncKpiVectorNow`；既有 `IdMappingService.mapExternalIds`、`_metricFields`、`_readAllRowsWithVectors` 现状（`:100-129`）。
- Produces:
  - `_readAllRowsWithVectors` 读 **4** 个 collection（`_CLASS/_PROPERTY/_METRIC/_KPI_COLLECTION_NAME`）。
  - `_expectedFromPg(session) -> tuple[dict, dict, dict]`：`(expected, expectedDetails, entityMaps)`——expected 键 = 4 类型 → uid 列表；expectedDetails 同键 → 列表（metric: `metric_name/metric_alias/agg_function`；kpi: 经 metric join 取 `metricAlias`）；entityMaps 仅 class/property/metric 三键（外部 id 映射）。
  - `_cleanup` 的 stale 判定/删除循环、rebuild 写回循环全部按 4 类型执行。
  - 最终 insert 的 `external_id` 列填**读取保留值**（cleanup 前旧行里有值就沿用；kpi 固定 `kpi_code`；class/property/metric 走 `entityMaps` 查不到补 `""`）。
- **语义钉住**：`_cleanup` 修复前 drop 3 集合只写回 2 类 ⇒ **一跑清空 metric 向量**（spec §5 排雷）；修复后 4 类全恢复（验收标准 4）。

- [ ] **Step 1: RED — 单测（禁连真 Milvus：monkeypatch `milvus_client` 的集合确保函数为 no-op）**

```python
"""回填脚本 4-collection 排雷：cleanup 不再清空 metric/kpi 向量。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import backfill_milvus_embeddings as bf  # noqa: E402


def test_read_all_rows_covers_four_collections(monkeypatch):
    """读取面必须含 kpi collection（修复前只读 3 个）。"""
    seen: list[str] = []

    class _FakeCol:
        def __init__(self, name):
            self.name = name

        def query(self, **kw):
            seen.append(self.name)
            return []

    class _FakeMilvus:
        def __init__(self):
            self.collections = {}
            self._ensureCollection = lambda name, fields: self.collections.setdefault(
                name, _FakeCol(name)
            )

    monkeypatch.setattr(bf.milvus, "MilvusClient", _FakeMilvus, raising=False)
    # _readAllRowsvectors 内部按 4 个集合常量逐个 ensure+query
    bf._readAllRowsWithVectors()
    assert any("kpi" in n for n in seen), seen


def test_cleanup_regen_loop_covers_kpi(monkeypatch):
    """stale-regen 删除循环必须遍历 4 类型（含 kpi）。"""
    deleted: list[tuple[str, str]] = []

    class _FakeNeo4j:
        @staticmethod
        def deleteByOntologyIdDual(uid, etype):
            deleted.append((etype, uid))

    monkeypatch.setattr(bf, "neo4j", _FakeNeo4j, raising=False)
    # 构造一行「Milvus 有、期望集也有」的 kpi 行 + 一行 metric 行，
    # 调 bf 的 stale 判定入口（脚本真实函数名，见脚本 `_cleanup` 内循环调用处）
    bf._runStaleRegen(  # 实施时以 `_cleanup` 内实际循环所在函数名为准
        present={"kpi": [{"ontology_id": "7"}],
                 "metric": [{"ontology_id": "3"}],
                 "class": [], "property": []},
        expected=bf._emptyExpected(),
        neo4jClient=_FakeNeo4j,
    )
    types = {t for t, _ in deleted}
    assert types == {"kpi", "metric"}  # 修复前只会是 {"class","property"} 的对偶缺失
```

> 本测试的 fake 双打面较宽（脚本直接连 Milvus）——实施时按脚本真实 import 结构调整 monkeypatch 锚点（`bf.milvus` / `bf.neo4j` 的实际模块引用），**断言保持两条钉死**：读取面含 kpi、cleanup 循环含 kpi。fake 细节允许适配，断言不允许弱化。

- [ ] **Step 2: RED — 集成测试 `_expectedFromPg`（真 PG）**

```python
"""_expectedFromPg 的 PG 期望集（metric/kpi 段）真库断言。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.models import KpiCatalog, OntologyMetric

pytestmark = pytest.mark.asyncio


async def test_expected_from_pg_includes_metric_and_published_kpi(
    integrationDbSession,
):
    metric = OntologyMetric(
        metric_name="回填率", formula="SUM(x)/SUM(y)", agg_function="ratio",
        metric_alias="fillRate",
    )
    integrationDbSession.add(metric)
    await integrationDbSession.flush()
    kpi = KpiCatalog(
        kpi_code="KPI_BF_TEST", kpi_name="回填测试", status="PUBLISHED",
        metric_id=metric.id, match_threshold=Decimal("0.75"),
    )
    integrationDbSession.add(kpi)
    await integrationDbSession.commit()
    try:
        import backfill_milvus_embeddings as bf

        expected, details, entityMaps = await bf._expectedFromPg(integrationDbSession)
        assert metric.id in {r["id"] for r in expected["metric"]}
        assert any(
            d.get("agg_function") == "ratio" and d.get("metric_alias") == "fillRate"
            for d in details["metric"]
        )
        assert kpi.id in {r["id"] for r in expected["kpi"]}
        assert any(d.get("metricAlias") == "fillRate" for d in details["kpi"])
        # DRAFT 的 KPI 不进期望集（D3：向量只索引 PUBLISHED）
        assert "kpi" not in entityMaps  # entityMaps 只有 class/property/metric
    finally:
        from sqlalchemy import delete as saDelete

        await integrationDbSession.execute(
            saDelete(KpiCatalog).where(KpiCatalog.kpi_code == "KPI_BF_TEST")
        )
        await integrationDbSession.execute(
            saDelete(OntologyMetric).where(OntologyMetric.metric_name == "回填率")
        )
        await integrationDbSession.commit()
```
（`sys.path` 注入与单测同款；若脚本用 `asyncio.run` 包装同步入口，`_expectedFromPg` 按 async 提取时直接可 await——实施时对齐。）

- [ ] **Step 3: 跑 RED**
```bash
pytest app/tests/unit/test_backfill_milvus_script.py app/tests/integration/test_backfill_expected_from_pg.py -q
```
预期：FAIL（脚本无 `_expectedFromPg`、读取面只有 3 集合）。
- [ ] **Step 4: GREEN — 脚本改造**

1. `_readAllRowsWithVectors`（`:100-129`）：集合清单从 3 个常量扩为 4 个（kpi 的 `ontology_id` = `str(kpi.id)`）。
2. 新函数 `_expectedFromPg(session)`：class/property 期望沿用现 `_cleanup` 内联逻辑（`:223-230`）抽出；metric 期望 = `OntologyMetric` 全表（维持现状口径：DRAFT 也建向量）；kpi 期望 = `KpiCatalog.status == "PUBLISHED"`，alias 取 `OntologyMetric.metric_alias`（outerjoin `kpi.metric_id`）。kpi 的 embedding 文本直接调 `kpi_sync.kpiEmbeddingText`，保证与钩子路径逐字一致。
3. `_backfill`（`:132-194`）：在 class/property 写回后追加 metric 段（镜像 `ontology_service._syncMetricEmbeddingNow` 的文本与字段：`name=metric_name, alias=metric_alias, description=agg_function`）与 kpi 段（PUBLISHED-only：`await kpi_sync.syncKpiVectorNow(session, kpi.id)` 逐条，复用钩子路径）。
4. `_cleanup`（`:197-395`）：stale 判定与 `_expectedFromPg` 对齐（从内联逻辑改为调用它）；stale-regen 删除循环与 rebuild 写回循环均遍历 4 类型。既有 `deleteByOntologyIds` no-op shim 在此无害（rebuild 步会重写一切）——保留 no-op 调用行并在旁边注释说明，**不改为 dual delete**（避免双删风暴）。
5. 更正过期注释 `:210-211`「metric 只删不插」→「metric/kpi 均在 rebuild 段写回（4 collection 全恢复）」。
6. 最终 insert（`:171` external_id 列 `["" for _ in records]`）改为按类型取值：kpi = `kpi_code`；class/property/metric = 读取保留值优先、`entityMaps` 查询兜底、再兜底 `""`。

- [ ] **Step 5: 跑 GREEN**：Step 3 两条命令全 PASS；再跑 `pytest app/tests/unit/test_milvus_kpi_routing.py app/tests/integration/test_kpi_graph_sync_integration.py -q` 净新增 0。
- [ ] **Step 6: Commit**
```bash
git add backend/scripts/backfill_milvus_embeddings.py \
        backend/app/tests/unit/test_backfill_milvus_script.py \
        backend/app/tests/integration/test_backfill_expected_from_pg.py
git commit -m "fix: 回填脚本 cleanup/rebuild 覆盖 4 collection，补 metric/kpi 写回段（拆清空 metric 向量的雷）"
```

---

### Task 9: 匹配器向量候选（KPI_MATCH_VECTOR_ENABLED，默认关）

**Files:**
- Modify: `backend/app/services/kpi_semantic_match_service.py`
- Modify: `backend/app/services/chat_stream_output.py:349-374`（`L1L4PreambleMixin._routeKpi`）
- Modify: `backend/app/api/v1/kpi_diagnose.py`（诊断 fallback 分支）
- Test: `backend/app/tests/unit/test_kpi_match_vector.py`（新建）

**Interfaces:**
- Consumes（**真实 API，已核实**）：
  - `KpiSemanticMatchService.matchRoute(self, question: str) -> KpiMatchRoute`（**实例方法，只收 question，无 session 参数**；`:321`）
  - `getKpiSemanticMatchService() -> KpiSemanticMatchService`（`:458`）
  - `KpiMatchRoute(kind: Literal["hit","ambiguous","none"], matches: tuple[KpiMatchResult, ...])`（**字段名是 `matches`，不是 `results`**；`:133-147`）
  - `KpiMatchResult(code, confidence, layer="l1_match", kpi_name=None, method="keyword", matched_phrase=None)`（**字段名是 `code` / `confidence` / `method`**；`:33-51`）——本任务给它**新增** `distance: float | None = None`
  - `get_kpi_match_cache() -> KpiMatchCache`（**snake_case 访问器**，`kpi_match_cache.py:388`）；缓存方法：`is_warmUp() -> bool`（**不是 `isFresh()`**）、`getAll() -> list[KpiCatalog]`（**ORM 对象列表，不是 dict**）、`metricAliasOf(code) -> str | None`、`findByAnyKeyword(kws)`
  - `EmbeddingService.generateEmbedding(text) -> list[float]`（**不是 `embedText`**；`embedding_service.py:61`）
  - `searchEmbeddingsByTypeRouted(queryEmbedding, topK, typeFilter)`（Task 1，**位置参数**）
  - `milvus_client.refreshCollection(collection, waitForLoad=True)`（`milvus_client.py:597`）—— release+load 强制 delete 可见
- Produces:
  - `KpiMatchResult` 新增字段 `distance: float | None = None`（vector 候选携带；文本候选恒 None）。
  - 常量 `KPI_VECTOR_DISTANCE_MAX: Final[float] = 0.5`、`KPI_VECTOR_TOP_K: Final[int] = 5`。
  - `KpiSemanticMatchService.matchRouteVector(self, question: str) -> KpiMatchRoute`：先跑 `self.matchRoute(question)`；`kind != "none"` 直接返回（包含式赢）；仅 `none` 时取向量候选补位。**开关不在本函数内判**（调用方 gate）；缓存冷（`not is_warmUp()`）⇒ 直接返回 base，不碰向量。
  - chat `L1L4PreambleMixin` 新增 `_vectorMatchEnabled(self, session) -> bool`（默认 `await isKpiMatchVectorEnabled(session)`）——测试可覆写；`_routeKpi` 在 `matchRoute` 返回 `kind == "none"` 且开关开时改走 `matchRouteVector`；v1 路径（`_containmentEnabled` 为 False）不动。
- 语义规则（spec §6，双向断言）：阈值超限丢弃；0 候选 ⇒ none；1 候选 ⇒ `hit` 且 `method == "vector"`；>1 ⇒ `ambiguous`（按 `code` 升序，不猜）；Milvus/embedding 异常 ⇒ 静默回落纯文本（返回 base）；metric 命中经 `kpi.metric_id` 反查绑定 KPI，与 kpi 直检候选按 `code` 去重（取 min distance）。
- **口径说明**：KPI 缓存只收 `status=PUBLISHED`（`kpi_match_cache.warmUp` 的 where）⇒ 向量候选天然只覆盖 PUBLISHED，与 D3 同口径。

- [ ] **Step 1: RED — 单测（fake 缓存 + fake EmbeddingService/search，禁连真 Milvus）**

```python
"""matchRouteVector：向量候选只补 none、阈值双向、异常静默回落。"""

from __future__ import annotations

import pytest

from app.services import kpi_semantic_match_service as svc
from app.services.kpi_match_cache import get_kpi_match_cache

pytestmark = pytest.mark.asyncio


class _StubKpi:
    """最小 KpiCatalog 替身（只需 id/code/name/metric_id）。"""

    def __init__(self, id: int, kpi_code: str, kpi_name: str, metric_id: int | None = None):
        self.id = id
        self.kpi_code = kpi_code
        self.kpi_name = kpi_name
        self.metric_id = metric_id


@pytest.fixture()
def warmCache(monkeypatch):
    """预灌缓存：3 条 PUBLISHED KPI（其中 1 条绑 metric）。"""
    cache = get_kpi_match_cache()
    kpis = [
        _StubKpi(101, "KPI_A", "指标A", None),
        _StubKpi(102, "KPI_B", "指标B", 55),
        _StubKpi(103, "KPI_C", "指标C", None),
    ]
    monkeypatch.setattr(cache, "is_warmUp", lambda: True)
    monkeypatch.setattr(cache, "getAll", lambda: list(kpis))
    return cache, kpis


@pytest.fixture()
def fakeVectorSearch(monkeypatch):
    """fake 向量检索：按 typeFilter 返回预设命中。"""
    state = {"kpi": [], "metric": [], "raise": False}
    calls: list[str] = []

    def _fakeSearch(query_embedding, top_k, typeFilter):
        calls.append(typeFilter)
        if state["raise"]:
            raise RuntimeError("milvus down")
        return list(state[typeFilter])[:top_k]

    class _FakeEmbeddingSvc:
        @staticmethod
        async def generateEmbedding(text: str):
            return [0.1, 0.2]

    monkeypatch.setattr(svc, "EmbeddingService", _FakeEmbeddingSvc, raising=False)
    monkeypatch.setattr(svc, "searchEmbeddingsByTypeRouted", _fakeSearch, raising=False)
    return state, calls


async def test_containment_wins_vector_not_called(warmCache, fakeVectorSearch):
    """包含式命中 ⇒ 向量检索根本不发起（双向守卫）。"""
    cache, kpis = warmCache
    state, calls = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": 0.1}]
    # 让 matchRoute 命中：缓存 KPI_A 的关键词包含在问句里
    cache.findByAnyKeyword = lambda kws: [kpis[0]]
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("我的指标A是多少")
    assert route.kind == "hit"
    assert route.matches[0].method == "containment"
    assert calls == []


async def test_single_vector_hit(warmCache, fakeVectorSearch):
    state, _ = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": 0.32}]
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_A"
    assert route.matches[0].method == "vector"
    assert route.matches[0].distance == 0.32


async def test_metric_hit_reverse_binds_kpi(warmCache, fakeVectorSearch):
    """metric 向量命中 → kpi.metric_id 反查 → 归到绑定 KPI（KPI_B 绑 metric 55）。"""
    state, _ = fakeVectorSearch
    state["metric"] = [{"ontology_id": "55", "distance": 0.28}]
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_B"
    assert route.matches[0].method == "vector"
    assert route.matches[0].distance == 0.28


async def test_over_threshold_dropped(warmCache, fakeVectorSearch):
    """双向反例：distance > KPI_VECTOR_DISTANCE_MAX ⇒ 丢弃 ⇒ none。"""
    state, _ = fakeVectorSearch
    state["kpi"] = [{"ontology_id": "101", "distance": svc.KPI_VECTOR_DISTANCE_MAX + 0.01}]
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "none"


async def test_two_hits_ambiguous(warmCache, fakeVectorSearch):
    state, _ = fakeVectorSearch
    state["kpi"] = [
        {"ontology_id": "103", "distance": 0.30},
        {"ontology_id": "101", "distance": 0.35},
    ]
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "ambiguous"
    codes = [m.code for m in route.matches]
    assert codes == sorted(codes)  # 确定性排序


async def test_milvus_failure_silent_fallback(warmCache, fakeVectorSearch):
    state, _ = fakeVectorSearch
    state["raise"] = True
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "none"  # 静默回落，不抛


async def test_cache_cold_returns_none(fakeVectorSearch, monkeypatch):
    """缓存冷 ⇒ none（不触发向量检索）。"""
    cache = get_kpi_match_cache()
    monkeypatch.setattr(cache, "is_warmUp", lambda: False)
    state, calls = fakeVectorSearch
    svcInst = svc.getKpiSemanticMatchService()
    route = await svcInst.matchRouteVector("完全不沾边的问句")
    assert route.kind == "none"
    assert calls == []
```

> `getKpiSemanticMatchService()` 返回**绑定了全局 cache 单例**的 service（`:458-462`）⇒ 测试里 monkeypatch 单例方法即可生效。`matchRoute` 的包含式命中依赖 `findByAnyKeyword` + `_phrasesOf` 的真实逻辑；本测试用 `cache.findByAnyKeyword` 桩让 `matchRoute` 走到 hit 分支。**实施时若 `matchRoute` 走的是 `_by_code`/`_by_keyword` 内部字典而非 `findByAnyKeyword`，改桩点为该内部字典**（读 `kpi_semantic_match_service.py:321-379` 对齐），断言不变。

- [ ] **Step 2: 跑 RED**
```bash
pytest app/tests/unit/test_kpi_match_vector.py -q
```
预期：FAIL（`matchRouteVector` / `distance` 字段 / 常量不存在）。
- [ ] **Step 3: GREEN — matcher 实现（`kpi_semantic_match_service.py`）**

1. `KpiMatchResult` dataclass 追加字段（带默认值，不破坏既有构造）：
```python
    distance: float | None = None
```
2. 模块级常量：
```python
KPI_VECTOR_DISTANCE_MAX: Final[float] = 0.5
KPI_VECTOR_TOP_K: Final[int] = 5
```
3. `KpiSemanticMatchService` 新增两个方法（放在 `matchRoute` 之后）：
```python
    async def _vectorCandidates(self, question: str) -> list[KpiMatchResult]:
        """问题 → Milvus kpi/metric 两路 topK 候选（失败抛出，由调用方决定回落）。"""
        queryVector = await EmbeddingService().generateEmbedding(question)
        kpiHits = await asyncio.to_thread(
            searchEmbeddingsByTypeRouted, queryVector, KPI_VECTOR_TOP_K, "kpi"
        )
        metricHits = await asyncio.to_thread(
            searchEmbeddingsByTypeRouted, queryVector, KPI_VECTOR_TOP_K, "metric"
        )
        cache = get_kpi_match_cache()
        allKpis = cache.getAll()
        idToKpi = {str(kpi.id): kpi for kpi in allKpis}
        candidates: dict[str, KpiMatchResult] = {}
        for source, hits in (("kpi", kpiHits), ("metric", metricHits)):
            for hit in hits:
                distance = float(hit.get("distance", 1.0))
                if distance > KPI_VECTOR_DISTANCE_MAX:
                    continue
                kpi = idToKpi.get(str(hit.get("ontology_id")))
                if kpi is None and source == "metric":
                    # metric 命中 → 反查绑定 KPI（KpiCatalog.metric_id）
                    kpi = next(
                        (k for k in allKpis
                         if k.metric_id is not None
                         and str(k.metric_id) == str(hit["ontology_id"])),
                        None,
                    )
                if kpi is None:
                    continue
                result = KpiMatchResult(
                    code=kpi.kpi_code,
                    confidence=round(max(0.0, 1.0 - distance), 4),
                    kpi_name=kpi.kpi_name,
                    method="vector",
                    distance=distance,
                )
                prev = candidates.get(kpi.kpi_code)
                if prev is None or (prev.distance or 1.0) > distance:
                    candidates[kpi.kpi_code] = result
        return sorted(candidates.values(), key=lambda r: r.code)

    async def matchRouteVector(self, question: str) -> KpiMatchRoute:
        """向量候选兜底路由：包含式赢、向量只补 none。开关由调用方 gate。"""
        base = await self.matchRoute(question)
        if base.kind != "none":
            return base
        if not get_kpi_match_cache().is_warmUp():
            return base  # 缓存冷：守卫既有语义，不碰向量
        try:
            candidates = await self._vectorCandidates(question)
        except Exception:
            logger.warning("向量候选失败，静默回落纯文本", exc_info=True)
            return base
        if not candidates:
            return base
        if len(candidates) == 1:
            return KpiMatchRoute(kind="hit", matches=tuple(candidates))
        return KpiMatchRoute(kind="ambiguous", matches=tuple(candidates))
```
4. 顶部 import 补 `asyncio`、`Final`、`EmbeddingService`、`searchEmbeddingsByTypeRouted`、`get_kpi_match_cache`（按文件既有 import 风格）。
- [ ] **Step 4: GREEN — chat 与诊断接入**

`chat_stream_output.py`：
```python
    async def _vectorMatchEnabled(self, session: AsyncSession) -> bool:
        """override 点（测试可覆写）；生产 = 灰度开关。"""
        return await isKpiMatchVectorEnabled(session)
```
`_routeKpi`（`:364-374`）的 containment 分支改为：
```python
        await self._ensureKpiCacheFresh(session)
        route = await self._kpiMatcher.matchRoute(question)
        if route.kind == "none" and await self._vectorMatchEnabled(session):
            route = await self._kpiMatcher.matchRouteVector(question)
        return route
```
（v1 分支 `self._kpiMatcher.match(question)` 不动。）

`kpi_diagnose.py`：在 containment 分支（`:231-236`）与 v1 分支（`:237-239`）之间插向量 fallback——`route.kind == "none"` 且 `await isKpiMatchVectorEnabled(session)` ⇒ 改用 `matchRouteVector` 结果；命中项的 `distance` 传进 `KpiDiagnoseMatch.distance`（Task 7 已加该字段）。诊断**不吞异常**：这里不 catch，故障 500，与模块承诺一致。

- [ ] **Step 5: 跑 GREEN**：Step 2 全 PASS；回归 `pytest app/tests/unit/test_kpi_match_route.py app/tests/unit/test_kpi_match_cache.py app/tests/unit/test_kpi_semantic_match_service.py -q` 净新增 0（第三个文件名为实测名，不存在则跳过）。
- [ ] **Step 6: Commit**
```bash
git add backend/app/services/kpi_semantic_match_service.py backend/app/services/chat_stream_output.py \
        backend/app/api/v1/kpi_diagnose.py backend/app/tests/unit/test_kpi_match_vector.py
git commit -m "feat: KPI 匹配器向量候选兜底（KPI_MATCH_VECTOR_ENABLED 默认关，包含式赢/阈值丢弃/歧义澄清）"
```

---

### Task 10: 每日 reconcile 计数扩展（kpi 向量数 / 图节点数）

**Files:**
- Modify: `backend/app/services/knowledge_compiler_service.py:599-669`（`_compareCounts` / `_countPg` / `_countNeo4j` / `_countMilvus`）
- Test: `backend/app/tests/unit/test_reconcile_kpi_counts.py`（新建）

**Interfaces:**
- Consumes: `KpiCatalog` ORM；Task 1 `_KPI_COLLECTION_NAME`（经 `milvus_dual_write` import）；Task 2 `neo4j.getKpiCatalogIds()`；既有 `_milvusCollectionCount(name)`（`:662-669`）。
- Produces:
  - `_countPg` 计数 dict 新增键：`kpi_published`（`status == "PUBLISHED"` 硬编码字面量 + 注释说明为何不走枚举）、`kpi_total`。
  - `_countNeo4j` 新增键 `kpi`；`_countMilvus` 新增键 `kpi`。
  - `_compareCounts` 在 `_TYPE_TO_BUSINESS_OBJECT` 循环后追加 **kpi 专用比对段**（kpi 键**刻意不进** `_TYPE_TO_BUSINESS_OBJECT`——会把 KPI 卷进 unified-id 解析守卫的既有 ripple，spec 排雷）。比对口径：`kpi_published` ↔ milvus `kpi`；`kpi_total` ↔ neo4j `kpi`；任一侧 None（store 不可用）跳过不告警。

- [ ] **Step 1: RED — 单测（monkeypatch 四个计数方法，不连真库）**

```python
"""reconcile 计数扩展：kpi_published↔milvus、kpi_total↔neo4j。"""

from __future__ import annotations

import pytest

from app.services import knowledge_compiler_service as kcs


def _svcWithCounts(monkeypatch, pg, neo, mil):
    """monkeypatch 三个计数方法为注入 dict 的 async 假实现（None=该 store 不可用）。"""
    svc = kcs  # 实施时若计数挂在实例方法上，改为实例化真实类并对实例 monkeypatch

    async def _fakePg(session):
        return pg

    async def _fakeNeo(session):
        return neo

    async def _fakeMil(session):
        return mil

    monkeypatch.setattr(kcs, "_countPg", _fakePg, raising=False)
    monkeypatch.setattr(kcs, "_countNeo4j", _fakeNeo, raising=False)
    monkeypatch.setattr(kcs, "_countMilvus", _fakeMil, raising=False)
    return svc


async def test_kpi_mismatch_produces_warning(monkeypatch):
```python
async def test_kpi_mismatch_produces_warning(monkeypatch):
    svc = _svcWithCounts(monkeypatch,
                         pg={"kpi_published": 12, "kpi_total": 15},
                         neo={"kpi": 15}, mil={"kpi": 10})
    result = await svc._compareCounts(None)
    texts = [w["detail"] for w in result["warnings"]]
    assert any("kpi_published" in t and "milvus" in t.lower() for t in texts)


async def test_kpi_parity_silent(monkeypatch):
    svc = _svcWithCounts(monkeypatch,
                         pg={"kpi_published": 12, "kpi_total": 15},
                         neo={"kpi": 15}, mil={"kpi": 12})
    result = await svc._compareCounts(None)
    assert not any("kpi" in w["detail"] for w in result["warnings"])


async def test_none_store_tolerated(monkeypatch):
    svc = _svcWithCounts(monkeypatch,
                         pg={"kpi_published": 12, "kpi_total": 15},
                         neo=None, mil=None)
    result = await svc._compareCounts(None)
    assert not any("kpi" in w["detail"] for w in result["warnings"])
```
`_svcWithCounts` 已在上方定义：monkeypatch `_countPg/_countNeo4j/_countMilvus` 为返回注入 dict 的 async 函数；`None` 表示让真实方法抛/返回 None 侧容错路径（以现有 None 处理手法为准）。`_compareCounts` 返回结构与 warning 字段名以 `knowledge_compiler_service.py:599-614` 现状为准（实施时读源码对齐字段名，勿臆造 `warnings["detail"]`）。

- [ ] **Step 2: 跑 RED**
```bash
pytest app/tests/unit/test_reconcile_kpi_counts.py -q
```
预期：FAIL（计数 dict 无 kpi 键）。
- [ ] **Step 3: GREEN — 实现**

`_countPg`（`:616-625`）追加：
```python
    # D3：向量只索引 PUBLISHED（与匹配缓存同口径）；字面量避免枚举 import 期耦合
    counts["kpi_published"] = int((await session.execute(
        select(func.count(KpiCatalog.id)).where(KpiCatalog.status == "PUBLISHED")
    )).scalar_one())
    counts["kpi_total"] = int((await session.execute(
        select(func.count(KpiCatalog.id))
    )).scalar_one())
```
`_countNeo4j`（`:627-634`）追加（与 metric 同款防御）：
```python
    try:
        counts["kpi"] = len(neo4j.getKpiCatalogIds())
    except Exception:  # noqa: BLE001
        counts["kpi"] = None
```
`_countMilvus`（`:637-643`）追加：
```python
    counts["kpi"] = _milvusCollectionCount(_KPI_COLLECTION_NAME)
```
（顶部 import 补 `from app.infrastructure.milvus_dual_write import _KPI_COLLECTION_NAME`、`from app.domain.models import KpiCatalog`——按文件既有 import 风格放置。）
`_compareCounts`（`:599-614`）在既有循环后追加：
```python
        # KPI 专用比对（不进 _TYPE_TO_BUSINESS_OBJECT：会把 KPI 卷进 unified-id
        # 解析守卫的既有 ripple）。kpi_published↔milvus[kpi]、kpi_total↔neo4j[kpi]
        # storeNames/storeCounts 为 `_compareCounts` 内既有局部变量（milvus/neo4j 计数
        # dict，键名见 :599-614 现状）；kpiPublished/kpiTotal 取自 _countPg 返回 dict
        for pgKey, storeCounts, storeLabel in (
            ("kpi_published", milvusCounts, "milvus"),
            ("kpi_total", neo4jCounts, "neo4j"),
        ):
            pgValue = pgCounts.get(pgKey)
            storeValue = storeCounts.get("kpi") if storeCounts else None
            if pgValue is None or storeValue is None:
                continue  # store 不可用（None 容错路径）⇒ 跳过不告警
            if pgValue != storeValue:
                warnings.append({
                    "entityType": "kpi",
                    "detail": (
                        f"kpi {pgKey} 不一致: pg={pgValue} "
                        f"{storeLabel}={storeValue}"
                    ),
                })
```
> 具体变量名以 `:599-614` 现状的 counts 传参结构为准；比对逻辑 = 两侧非 None 且不等 ⇒ 追加一条 warning（detail 含两侧数值与 store 名），与既有类/属性 mismatch 告警同格式。None ⇒ skip。

- [ ] **Step 4: 跑 GREEN**：Step 2 全 PASS；回归既有 reconcile 套件（`grep -rl "_compareCounts\|_countMilvus" app/tests/` 定向）净新增 0。
- [ ] **Step 5: Commit**
```bash
git add backend/app/services/knowledge_compiler_service.py backend/app/tests/unit/test_reconcile_kpi_counts.py
git commit -m "feat: 每日 reconcile 计数扩展 kpi 向量/图节点（kpi_published↔milvus、kpi_total↔neo4j）"
```

---

### Task 11: KPI 目录页同步按钮（admin-only）+ i18n

**Files:**
- Modify: `frontend/src/api/kpiCatalog.ts`
- Modify: `frontend/src/pages/KpiCatalogPage.tsx`
- Modify: `frontend/src/i18n/locales/zh-CN.ts`（kpiCatalog namespace，约 `:2051`）+ `en-US.ts` 镜像
- Test: `frontend/src/tests/KpiCatalogPage.test.tsx`（追加）

**Interfaces:**
- Consumes: Task 7 两个端点（`POST /kpi-catalog/embeddings/sync-missing`、`POST /kpi-catalog/{id}/embedding`）；页面既有 `isAdmin`（约 `:68`）、`message`（约 `:15`）、`listKpis` API。
- Produces: `syncMissingKpi(): Promise<KpiSyncMissingResult>`、`resyncKpiEmbedding(id: number): Promise<void>`；i18n 键 `syncAllButton / resyncButton / syncAllNone / syncAllDone / syncAllPartial / resyncDone`（zh/en 双语，`i18n.test.ts` 强制 parity）。

- [ ] **Step 1: RED — 组件测试追加（mock `kpiCatalog.ts` 模块，含既有 `listKpis` mock）**

```tsx
// 追加到 KpiCatalogPage.test.tsx 末尾（mock 声明区补两个函数）
// vi.mock 补充：
//   syncMissingKpi: vi.fn(),
//   resyncKpiEmbedding: vi.fn(),

describe('KPI 目录页 同步按钮', () => {
  it('admin 见页头「向量/图同步」按钮，点击调 syncMissingKpi 并提示完成', async () => {
    mockAuthAdmin();
    vi.mocked(api.syncMissingKpi).mockResolvedValue({
      vectorPublishedTotal: 12, vectorMissingCount: 0, vectorSyncedCount: 0,
      vectorOrphanCount: 0, graphMissingCount: 0, graphSyncedCount: 0,
      failedCount: 0, failures: [],
    });
    renderPage();
    const btn = await screen.findByRole('button', { name: /向量\/图同步/ });
    await userEvent.click(btn);
    expect(api.syncMissingKpi).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/无缺失/)).toBeInTheDocument();
  });

  it('非 admin 不渲染同步按钮', () => {
    mockAuthUser(); // 普通用户
    renderPage();
    expect(screen.queryByRole('button', { name: /向量\/图同步/ })).toBeNull();
  });

  it('行级「重新同步」点击调 resyncKpiEmbedding(id)', async () => {
    mockAuthAdmin();
    vi.mocked(api.resyncKpiEmbedding).mockResolvedValue(undefined);
    renderPage();
    const btns = await screen.findAllByRole('button', { name: /重新同步/ });
    await userEvent.click(btns[0]);
    expect(api.resyncKpiEmbedding).toHaveBeenCalledWith(expect.any(Number));
    expect(await screen.findByText(/已重新同步/)).toBeInTheDocument();
  });
});
```
（`mockAuthAdmin / mockAuthUser / renderPage` 为该测试文件既有 helper——实施时读文件头对齐实名与既有 mock 结构；`findByText` 锚点用 i18n 文案，与既有测试手法一致。）

- [ ] **Step 2: 跑 RED**
```bash
cd <worktree>/frontend && npx vitest run src/tests/KpiCatalogPage.test.tsx
```
预期：FAIL（按钮不存在）。
- [ ] **Step 3: GREEN — API + 页面 + i18n**

`kpiCatalog.ts` 追加：
```ts
export interface KpiSyncMissingResult {
  vectorPublishedTotal: number;
  vectorMissingCount: number;
  vectorSyncedCount: number;
  vectorOrphanCount: number;
  graphMissingCount: number;
  graphSyncedCount: number;
  failedCount: number;
  failures: Array<{ entityType?: string; entityId?: number | string; error: string }>;
}

export async function syncMissingKpi(): Promise<KpiSyncMissingResult> {
  const res = await http.post(`${BASE}/embeddings/sync-missing`);
  return res.data;
}

export async function resyncKpiEmbedding(id: number): Promise<void> {
  await http.post(`${BASE}/${id}/embedding`);
}
```
（`http` / `BASE` 用该文件既有 import 与常量实名；snake_case 字段为后端 JSON 契约**有意**保留——与项目「camelModel 出、snake 契约入」惯例核对：若项目前端统一 camel 化，则按该文件既有 DTO 处理手法对齐，两处保持一致。）

`KpiCatalogPage.tsx`：
- 状态：`const [syncingAll, setSyncingAll] = useState(false); const [syncingIds, setSyncingIds] = useState<Set<number>>(new Set());`（不可变更新：`new Set(prev)` 复制后 add/delete）。
- 页头（admin 区域内，与既有 admin 按钮同排）：
```tsx
{isAdmin && (
  <Button
    type="default"
    loading={syncingAll}
    onClick={async () => {
      setSyncingAll(true);
      try {
        const r = await api.syncMissingKpi();
        if (r.vectorMissingCount + r.graphMissingCount + r.vectorOrphanCount === 0) {
          message.success(t('kpiCatalog.syncAllNone'));
        } else if (r.failedCount === 0) {
          message.success(t('kpiCatalog.syncAllDone', {
            vector: r.vectorSyncedCount, graph: r.graphSyncedCount,
          }));
        } else {
          message.warning(t('kpiCatalog.syncAllPartial', { failed: r.failedCount }));
        }
      } catch {
        message.error(t('kpiCatalog.syncAllFailed'));
      } finally {
        setSyncingAll(false);
      }
    }}
  >
    {t('kpiCatalog.syncAllButton')}
  </Button>
)}
```
- 行操作列（admin 行内，复用既有 admin-only 列条件）：
```tsx
{isAdmin && (
  <Button
    type="link"
    size="small"
    loading={syncingIds.has(record.id)}
    onClick={async () => {
      setSyncingIds(prev => new Set(prev).add(record.id));
      try {
        await api.resyncKpiEmbedding(record.id);
        message.success(t('kpiCatalog.resyncDone', { code: record.kpiCode }));
      } catch {
        message.error(t('kpiCatalog.resyncFailed'));
      } finally {
        setSyncingIds(prev => {
          const next = new Set(prev);
          next.delete(record.id);
          return next;
        });
      }
    }}
  >
    {t('kpiCatalog.resyncButton')}
  </Button>
)}
```
（`record.kpiCode` / 列结构 / admin 条件写法以页面现状为准；409 处理：`catch` 中读 `error.response?.status === 409` 时提示「仅 PUBLISHED 建向量」——文案键 `resyncNotPublished`。）

i18n（zh-CN / en-US 两处同键）：
```ts
// zh-CN kpiCatalog namespace 追加
syncAllButton: '向量/图同步',
resyncButton: '重新同步',
syncAllNone: '无缺失：向量与图均已是最新',
syncAllDone: '同步完成：补向量 {vector} 条、补图 {graph} 条',
syncAllPartial: '同步完成但有 {failed} 条失败，详见后端日志',
syncAllFailed: '同步失败，请稍后重试',
resyncDone: '{code} 已重新同步',
resyncFailed: '重新同步失败，请稍后重试',
resyncNotPublished: '仅 PUBLISHED 状态的 KPI 建向量',
// en-US 镜像（意译即可，键一一对应）
```

- [ ] **Step 4: 跑 GREEN**
```bash
npx tsc --noEmit && npx vitest run src/tests/KpiCatalogPage.test.tsx src/tests/i18n.test.ts
```
全 PASS（tsc 0 错误基线）。
- [ ] **Step 5: Commit**
```bash
git add frontend/src/api/kpiCatalog.ts frontend/src/pages/KpiCatalogPage.tsx \
        frontend/src/i18n/locales/zh-CN.ts frontend/src/i18n/locales/en-US.ts \
        frontend/src/tests/KpiCatalogPage.test.tsx
git commit -m "feat: KPI 目录页 admin 同步入口（页头全量对账 + 行级重新同步）+ i18n"
```

---

### Task 12: 定向回归 + 变更记录

**Files:**
- Create: `Harness/changes/feat-kpi-vector-graph-sync/summary.md`
- 无代码改动（纯验证 + 文档）

- [ ] **Step 1: 后端定向回归（真 PG 5434 + 环境变量 export 同前；只跑定向，禁全量）**
```bash
cd <worktree>/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'

pytest app/tests/unit/test_milvus_kpi_routing.py \
       app/tests/unit/test_neo4j_kpi_catalog.py \
       app/tests/unit/test_kpi_vector_graph_sync.py \
       app/tests/unit/test_kpi_sync_switches.py \
       app/tests/unit/test_kpi_catalog_hooks.py \
       app/tests/unit/test_kpi_match_vector.py \
       app/tests/unit/test_kpi_match_route.py \
       app/tests/unit/test_kpi_match_cache.py \
       app/tests/unit/test_reconcile_kpi_counts.py \
       app/tests/unit/test_backfill_milvus_script.py \
       app/tests/unit/test_milvus_dual_write.py \
       app/tests/unit/test_milvus_search.py -q
```
（最后两个为 Milvus/Neo4j 基础设施邻近套件，实施时以实际存在文件名为准，不存在则跳过。）
```bash
pytest app/tests/integration/test_kpi_graph_sync_integration.py \
       app/tests/integration/test_kpi_reconcile_graph.py \
       app/tests/integration/test_kpi_sync_endpoints.py \
       app/tests/integration/test_backfill_expected_from_pg.py \
       app/tests/integration/test_kpi_diagnose.py -q
```
再加 `grep -rl "syncMissingGraph\|GraphSyncMissingResult\|_compareCounts" app/tests/` 命中的邻近套件定向跑。**判据：净新增失败 0**（对照 main 基线；抖动按白名单流程取证，不复跑洗绿）。
- [ ] **Step 2: 前端回归**
```bash
cd <worktree>/frontend && npx tsc --noEmit && npx vitest run src/tests/KpiCatalogPage.test.tsx src/tests/i18n.test.ts
```
- [ ] **Step 3: 写变更记录 `Harness/changes/feat-kpi-vector-graph-sync/summary.md`**

结构（按仓库既有 changes 格式）：
1. **背景**：用户诉求 + 现状表（类/属性 ✅、metric 3 缺口、kpi_catalog 全外）→ 引 spec。
2. **变更清单**：12 个 commit 一行一条（Milvus 4th collection / Neo4j KpiCatalog / 同步服务 / 开关+钩子 / id_mapping 回填脚本 / syncMissingGraph 扩展 / 手动端点+诊断 / 回填排雷 / 匹配器向量候选 / reconcile 计数 / 前端入口）。
3. **验收证据**：spec §11 六条逐条对应证据（测试文件名+用例名；第 6 条贴净新增 0 的失败集合对比）。
4. **开关**：`KPI_SYNC_ENABLED`（默认开，system_config 行缺失=开的方向反转说明）、`KPI_MATCH_VECTOR_ENABLED`（默认关）。
5. **未做的事**：不部署生产（生产发版硬闸门另行决策）、id_mapping 存量 12 条回填 dev 5433 实跑待用户批准、outbox 队列不做、NL2SQL schema 裁剪不消费 metric 向量、P5/P4b 后续子项目。
6. **部署后人工冒烟清单**（本批不执行）：KPI 新建→向量/图出现→诊断可见；转 DRAFT→向量消失图节点保留；sync-missing 幂等复跑 0；开灰度→语义相近问句命中 match_method=vector；回填脚本 --cleanup 后 4 集合恢复。

- [ ] **Step 4: Commit**
```bash
git add Harness/changes/feat-kpi-vector-graph-sync/summary.md
git commit -m "docs: 指标/KPI 纳入向量图管理 变更记录（验收证据+开关+冒烟清单）"
```

---

## 文件清单（总）

| 文件 | 动作 |
|---|---|
| `backend/app/infrastructure/milvus_client.py` | `VALID_EMBEDDING_TYPES` + 常量 + re-export |
| `backend/app/infrastructure/milvus_dual_write.py` | kpi 路由/ensure/external_id 透传 |
| `backend/app/infrastructure/milvus_search.py` | kpi 检索分支 + None-sweep 4 集合 |
| `backend/app/infrastructure/milvus_query_helpers.py` | kpi 查询 + 全量列表 4 集合 |
| `backend/app/infrastructure/neo4j_client.py` | `_ALLOWED_LABELS` + KpiCatalog upsert/读/删 + MEASURED_BY |
| `backend/app/services/kpi_vector_graph_sync.py` | **新建**：同步服务全部函数 |
| `backend/app/services/kpi_metric_switches.py` | 2 个新开关（方向反转注释） |
| `backend/app/config.py` | 2 个 Field |
| `backend/app/services/kpi_catalog_service.py` | CRUD best-effort 钩子 |
| `backend/app/services/ontology_service.py` | syncMissingGraph metric+kpi 段 |
| `backend/app/services/kpi_semantic_match_service.py` | 向量候选 + distance |
| `backend/app/services/chat_stream_output.py` | `_vectorMatchEnabled` + `_routeKpi` fallback |
| `backend/app/services/knowledge_compiler_service.py` | reconcile kpi 计数 |
| `backend/app/api/v1/kpi_catalog.py` | docstring 更正 + 2 端点 + 模型 |
| `backend/app/api/v1/kpi_diagnose.py` | sync_health + distance |
| `backend/app/api/v1/ontology.py` | GraphSyncMissingResult 6 字段 |
| `backend/scripts/backfill_kpi_id_mapping.py` | **新建**：id_mapping 回填 CLI |
| `backend/scripts/backfill_milvus_embeddings.py` | 4 collection 排雷修复 |
| `frontend/src/api/kpiCatalog.ts` | 2 API + DTO |
| `frontend/src/pages/KpiCatalogPage.tsx` | admin 同步按钮 |
| `frontend/src/i18n/locales/{zh-CN,en-US}.ts` | 9 键 |
| 测试文件 ×11 | 见各任务 |
| `Harness/changes/feat-kpi-vector-graph-sync/summary.md` | 变更记录 |

**零 Alembic 迁移**（id_mapping.business_object 为自由 String(32)，无 CHECK 约束）。
