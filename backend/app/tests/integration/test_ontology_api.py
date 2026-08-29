"""本体 API 集成测试（真实 PostgreSQL + mock Neo4j/Milvus）。

覆盖：Class / Property / Metric CRUD、embedding 同步请求参数验证。

Neo4j 和 Milvus 通过 monkeypatch mock，测试不依赖外部服务；
数据层走真实 PostgreSQL，client/dbSession fixtures 由本目录 conftest.py 统一提供
（强制规则：Harness/rules/测试规范.md）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# === mock Neo4j & Milvus（必须在 app 导入前 patch） ===

_neo4j_called: list[str] = []


class _MockNeo4jDriver:
    class _Session:
        def __init__(self, _) -> None:
            pass

        def __enter__(self) -> "_MockNeo4jDriver._Session":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def run(self, cql: str, **params: object) -> list[object]:
            _neo4j_called.append(cql[:200])
            # 返回一个符合 neo4j.IRecord 签名的 mock
            class _Record:
                def __init__(self, data: dict) -> None:
                    self._data = data

                def __getitem__(self, key: str) -> object:
                    return self._data[key]

            class _Nodes:
                def __init__(self, props: dict) -> None:
                    self._props = props

                def __iter__(self):
                    return iter([])

            if "RETURN count(n)" in cql:
                return [_Record({"deleted": 1})]
            if "hasCycle" in cql:
                # 继承环检测：默认无环（具体成环场景由测试 monkeypatch 模拟）
                return [_Record({"hasCycle": False})]
            if "CREATE (c:Class" in cql:
                return [_Record({"c": _Nodes({"id": params.get("id"), "name": params.get("name")})})]
            if "CREATE (p:Property" in cql:
                return [_Record({"p": _Nodes({"id": params.get("id"), "name": params.get("name")})})]
            if "CREATE (m:Metric" in cql:
                return [_Record({"m": _Nodes({"id": params.get("id"), "name": params.get("name")})})]
            return [_Record({"c": _Nodes({}), "properties": []})]

    def session(self) -> _Session:
        return _MockNeo4jDriver._Session(None)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def mockNeo4jAndMilvus(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局 mock：替换 Neo4j driver 和 Milvus 客户端，使测试不依赖外部服务。"""
    import app.infrastructure.neo4j_client as neo4j_module
    import app.infrastructure.milvus_client as milvus_module
    import app.services.ontology_service as ontology_module

    # Mock Neo4j driver
    monkeypatch.setattr(neo4j_module, "_DRIVER", _MockNeo4jDriver())
    monkeypatch.setattr(neo4j_module, "getDriver", lambda: _MockNeo4jDriver())

    # Mock Milvus
    monkeypatch.setattr(milvus_module, "insertEmbeddings", lambda *a, **kw: None)
    monkeypatch.setattr(milvus_module, "deleteByOntologyId", lambda *a, **kw: None)
    monkeypatch.setattr(milvus_module, "searchByEmbedding", lambda *a, **kw: [])
    monkeypatch.setattr(milvus_module, "ensureCollection", lambda: None)

    # Mock ontology_service milvus reference
    monkeypatch.setattr(ontology_module.milvus, "insertEmbeddings", lambda *a, **kw: None)
    monkeypatch.setattr(ontology_module.milvus, "deleteByOntologyId", lambda *a, **kw: None)
    monkeypatch.setattr(ontology_module.milvus, "searchByEmbedding", lambda *a, **kw: [])

    _neo4j_called.clear()


# =============================================================================
# Class tests
# =============================================================================

pytestmark = pytest.mark.asyncio


async def testListClassesEmpty(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ontology/classes")
    assert response.status_code == 200
    assert response.json() == []


async def testCreateClass(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/ontology/classes",
        json={
            "className": "Customer",
            "classAlias": "客户",
            "description": "客户信息表",
            "sourceTable": "t_customer",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["className"] == "Customer"
    assert data["classAlias"] == "客户"
    assert data["id"] > 0


async def testCreateClassDuplicate(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Product", "sourceTable": "t_product"},
    )
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Product", "sourceTable": "t_product2"},
    )
    assert response.status_code == 422
    assert "已存在" in response.json().get("error", "")


async def testGetClass(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Order", "sourceTable": "t_order"},
    )
    classId = create.json()["id"]
    response = await client.get(f"/api/v1/ontology/classes/{classId}")
    assert response.status_code == 200
    assert response.json()["className"] == "Order"


async def testGetClassNotFound(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ontology/classes/99999")
    assert response.status_code == 404


async def testUpdateClass(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Supplier", "sourceTable": "t_supplier"},
    )
    supplierId = create.json()["id"]
    response = await client.put(
        f"/api/v1/ontology/classes/{supplierId}",
        json={"classAlias": "供应商", "description": "供应商主数据"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["classAlias"] == "供应商"
    assert data["description"] == "供应商主数据"
    assert data["className"] == "Supplier"  # 未修改字段保持不变


async def testDeleteClass(client: AsyncClient) -> None:
    """Phase 6：删除为软删除（valid_to=now()），按 id 仍可查得历史版本。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Warehouse", "sourceTable": "t_warehouse"},
    )
    warehouseId = create.json()["id"]
    response = await client.delete(f"/api/v1/ontology/classes/{warehouseId}")
    assert response.status_code == 204
    # 软删除：按 id 仍可查得（valid_to 非空）
    getDeleted = await client.get(f"/api/v1/ontology/classes/{warehouseId}")
    assert getDeleted.status_code == 200
    assert getDeleted.json()["validTo"] is not None
    # 但默认列表不再显示
    listed = await client.get("/api/v1/ontology/classes")
    assert all(r["id"] != warehouseId for r in listed.json())


# =============================================================================
# Class 继承（parent_class_id）测试
# =============================================================================


async def testCreateClassWithParent(client: AsyncClient) -> None:
    parent = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Animal", "sourceTable": "t_animal"},
    )
    parentId = parent.json()["id"]
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Dog", "sourceTable": "t_dog", "parentClassId": parentId},
    )
    assert response.status_code == 201
    assert response.json()["parentClassId"] == parentId
    # 创建时同步建立 SUBCLASS_OF 继承边
    cqls = "\n".join(_neo4j_called)
    assert "MERGE (c)-[:SUBCLASS_OF]->(p)" in cqls


async def testCreateClassRejectsMissingParent(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Orphan", "sourceTable": "t_orphan", "parentClassId": 99999},
    )
    assert response.status_code == 422
    assert "父类" in response.json().get("error", "")


async def testUpdateClassSetsParent(client: AsyncClient) -> None:
    parent = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Vehicle", "sourceTable": "t_vehicle"},
    )
    parentId = parent.json()["id"]
    child = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Car", "sourceTable": "t_car"},
    )
    childId = child.json()["id"]
    _neo4j_called.clear()
    response = await client.put(
        f"/api/v1/ontology/classes/{childId}",
        json={"parentClassId": parentId},
    )
    assert response.status_code == 200
    assert response.json()["parentClassId"] == parentId
    cqls = "\n".join(_neo4j_called)
    # 更新继承边：先删旧边再 MERGE 新边
    assert "MATCH (c:Class {id: $id})-[r:SUBCLASS_OF]->() DELETE r" in cqls
    assert "MERGE (c)-[:SUBCLASS_OF]->(p)" in cqls


async def testUpdateClassClearsParent(client: AsyncClient) -> None:
    parent = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Shape", "sourceTable": "t_shape"},
    )
    parentId = parent.json()["id"]
    child = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Circle", "sourceTable": "t_circle", "parentClassId": parentId},
    )
    childId = child.json()["id"]
    _neo4j_called.clear()
    response = await client.put(
        f"/api/v1/ontology/classes/{childId}",
        json={"parentClassId": None},
    )
    assert response.status_code == 200
    assert response.json()["parentClassId"] is None
    cqls = "\n".join(_neo4j_called)
    # 清空继承：仅删除旧边，不 MERGE 新边
    assert "DELETE r" in cqls
    assert "MERGE (c)-[:SUBCLASS_OF]->(p)" not in cqls


async def testUpdateClassRejectsSelfInheritance(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Loop", "sourceTable": "t_loop"},
    )
    classId = cls.json()["id"]
    response = await client.put(
        f"/api/v1/ontology/classes/{classId}",
        json={"parentClassId": classId},
    )
    assert response.status_code == 422
    assert "自身" in response.json().get("error", "")


async def testUpdateClassRejectsInheritanceCycle(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.infrastructure.neo4j_client as neo4j_module

    parent = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Grand", "sourceTable": "t_grand"},
    )
    parentId = parent.json()["id"]
    child = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Parent", "sourceTable": "t_parent", "parentClassId": parentId},
    )
    childId = child.json()["id"]
    # 模拟环：将 parent 设为 child 时 detectInheritanceCycle 返回 True
    monkeypatch.setattr(neo4j_module, "detectInheritanceCycle", lambda *_a, **_kw: True)
    response = await client.put(
        f"/api/v1/ontology/classes/{parentId}",
        json={"parentClassId": childId},
    )
    assert response.status_code == 422
    assert "环" in response.json().get("error", "")


# =============================================================================
# Class 版本管理（Phase 6）
# =============================================================================


async def testCreateClassStartsAtVersionOne(client: AsyncClient) -> None:
    """新建类 version=1，validFrom 有值，validTo 为 None。"""
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "VersionedClass", "sourceTable": "t_v1"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["version"] == 1
    assert data["validFrom"] is not None
    assert data["validTo"] is None


async def testUpdateClassInPlace(client: AsyncClient) -> None:
    """更新类原地修改：id 不变、version 不递增、validTo 保持 None（版本管理已移除）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "VersionedUpdate", "description": "v1", "sourceTable": "t_vu"},
    )
    firstId = create.json()["id"]
    assert create.json()["version"] == 1

    response = await client.put(
        f"/api/v1/ontology/classes/{firstId}",
        json={"description": "v2"},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["description"] == "v2"
    assert updated["version"] == 1  # 不再版本递增
    assert updated["id"] == firstId  # 主键稳定，非新行
    assert updated["validTo"] is None  # 未软删除

    # 按原 id 仍可查得更新后的类（无历史新行产生）
    getOld = await client.get(f"/api/v1/ontology/classes/{firstId}")
    assert getOld.status_code == 200
    assert getOld.json()["description"] == "v2"
    assert getOld.json()["validTo"] is None


async def testListClassesDefaultExcludesExpired(client: AsyncClient) -> None:
    """listClasses 返回未删除的类（原地更新后仍为同一行）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Expiring", "description": "v1", "sourceTable": "t_exp"},
    )
    firstId = create.json()["id"]
    await client.put(f"/api/v1/ontology/classes/{firstId}", json={"description": "v2"})

    # 默认：仍 1 行（无新版本行），description 已更新
    response = await client.get("/api/v1/ontology/classes")
    assert response.status_code == 200
    rows = [r for r in response.json() if r["className"] == "Expiring"]
    assert len(rows) == 1
    assert rows[0]["id"] == firstId  # 不产生新行
    assert rows[0]["version"] == 1
    assert rows[0]["description"] == "v2"


async def testListClassesIncludeExpiredReturnsSameRow(client: AsyncClient) -> None:
    """?includeExpired=true 与默认一致：版本管理移除后每类仅一行。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "HistoryA", "description": "v1", "sourceTable": "t_ha"},
    )
    firstId = create.json()["id"]
    await client.put(f"/api/v1/ontology/classes/{firstId}", json={"description": "v2"})
    await client.put(f"/api/v1/ontology/classes/{firstId}", json={"description": "v3"})

    listing = await client.get("/api/v1/ontology/classes?includeExpired=true")
    rows = [r for r in listing.json() if r["className"] == "HistoryA"]
    assert len(rows) == 1  # 不再累积历史版本
    assert rows[0]["id"] == firstId
    assert rows[0]["description"] == "v3"


async def testListClassVersionsEndpoint(client: AsyncClient) -> None:
    """GET /classes/{name}/versions 返回该类所有行（原地更新后仅 1 行）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "MultiVer", "description": "v1", "sourceTable": "t_mv"},
    )
    firstId = create.json()["id"]
    await client.put(f"/api/v1/ontology/classes/{firstId}", json={"description": "v2"})

    response = await client.get("/api/v1/ontology/classes/MultiVer/versions")
    assert response.status_code == 200
    versions = response.json()
    assert len(versions) == 1
    assert versions[0]["id"] == firstId
    assert versions[0]["description"] == "v2"


async def testDeleteClassIsSoftDelete(client: AsyncClient) -> None:
    """删除仅软删除当前版本：validTo=now()，不级联删历史版本。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "SoftDel", "description": "v1", "sourceTable": "t_sd"},
    )
    firstId = create.json()["id"]
    await client.put(f"/api/v1/ontology/classes/{firstId}", json={"description": "v2"})
    currentId = (
        await client.get("/api/v1/ontology/classes?includeExpired=true")
    ).json()
    currentRow = next(
        r for r in currentId if r["className"] == "SoftDel" and r["validTo"] is None
    )
    currentId = currentRow["id"]

    response = await client.delete(f"/api/v1/ontology/classes/{currentId}")
    assert response.status_code == 204

    # 默认列表不显示
    listed = await client.get("/api/v1/ontology/classes")
    assert all(r["className"] != "SoftDel" for r in listed.json())
    # includeExpired=true 仍可见历史
    listedAll = await client.get("/api/v1/ontology/classes?includeExpired=true")
    assert any(r["className"] == "SoftDel" for r in listedAll.json())


async def testCreateClassWithDuplicateNameRejected(client: AsyncClient) -> None:
    """版本管理移除后：存在未删除的同名类时，新建同名类被拒绝（不再产生版本分支）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "DupName", "description": "v1", "sourceTable": "t_dn"},
    )
    assert create.status_code == 201
    # 同名单类未被删除 → 新建同名类冲突
    duplicate = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "DupName", "description": "v1", "sourceTable": "t_dn"},
    )
    assert duplicate.status_code == 422


# =============================================================================
# Property tests
# =============================================================================

async def testCreateProperty(client: AsyncClient) -> None:
    # 先建 Class
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Inventory", "sourceTable": "t_inventory"},
    )
    classId = cls.json()["id"]

    response = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "stock_quantity",
            "propertyAlias": "库存数量",
            "dataType": "INT",
            "sourceColumn": "stock_qty",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["propertyName"] == "stock_quantity"
    assert data["dataType"] == "INT"
    assert data["classId"] == classId


async def testCreatePropertyClassNotFound(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": 99999,
            "propertyName": "qty",
            "dataType": "INT",
        },
    )
    assert response.status_code == 404


async def testListPropertiesByClass(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Region", "sourceTable": "t_region"},
    )
    classId = cls.json()["id"]
    await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "region_name", "dataType": "STRING"},
    )
    await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "region_code", "dataType": "STRING"},
    )

    response = await client.get(f"/api/v1/ontology/classes/{classId}/properties")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    names = {d["propertyName"] for d in data}
    assert "region_name" in names
    assert "region_code" in names


async def testUpdateProperty(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Category", "sourceTable": "t_category"},
    )
    classId = cls.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "cat_name", "dataType": "STRING"},
    )
    propId = prop.json()["id"]

    response = await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"propertyAlias": "分类名称", "isPrimaryKey": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["propertyAlias"] == "分类名称"
    assert data["isPrimaryKey"] is True


async def testDeleteProperty(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Brand", "sourceTable": "t_brand"},
    )
    classId = cls.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "brand_name", "dataType": "STRING"},
    )
    propId = prop.json()["id"]

    response = await client.delete(f"/api/v1/ontology/properties/{propId}")
    assert response.status_code == 204


# =============================================================================
# Metric tests
# =============================================================================

async def testCreateMetric(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "SaleOrder", "sourceTable": "t_sale_order"},
    )
    classId = cls.json()["id"]

    response = await client.post(
        "/api/v1/ontology/metrics",
        json={
            "metricName": "sales_amount",
            "metricAlias": "销售总额",
            "formula": "SUM(order_amount)",
            "aggFunction": "SUM",
            "targetClassId": classId,
            "dimensionDefaults": {"region": "default_region", "product_category": "default_cat"},
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["metricName"] == "sales_amount"
    assert data["formula"] == "SUM(order_amount)"
    assert data["dimensionDefaults"] == {"region": "default_region", "product_category": "default_cat"}


async def testListMetrics(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Purchase", "sourceTable": "t_purchase"},
    )
    await client.post(
        "/api/v1/ontology/metrics",
        json={"metricName": "purchase_cnt", "formula": "COUNT(id)", "aggFunction": "COUNT"},
    )
    response = await client.get("/api/v1/ontology/metrics")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["metricName"] == "purchase_cnt"


async def testUpdateMetric(client: AsyncClient) -> None:
    metric = await client.post(
        "/api/v1/ontology/metrics",
        json={"metricName": "order_cnt", "formula": "COUNT(id)", "aggFunction": "COUNT"},
    )
    metricId = metric.json()["id"]
    response = await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json={"metricAlias": "订单总量", "aggFunction": "SUM"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["metricAlias"] == "订单总量"
    assert data["aggFunction"] == "SUM"


async def testDeleteMetric(client: AsyncClient) -> None:
    metric = await client.post(
        "/api/v1/ontology/metrics",
        json={"metricName": "stock_val", "formula": "SUM(qty*price)", "aggFunction": "SUM"},
    )
    metricId = metric.json()["id"]
    response = await client.delete(f"/api/v1/ontology/metrics/{metricId}")
    assert response.status_code == 204


# =============================================================================
# Embedding sync tests
# =============================================================================

async def testSyncEmbeddingValidation(client: AsyncClient) -> None:
    # 无效 type 值
    response = await client.post(
        "/api/v1/ontology/embeddings/sync",
        json={
            "ontologyId": 1,
            "type": "invalid",
            "name": "test",
            "embedding": [0.1] * 1536,
        },
    )
    assert response.status_code == 422

    # embedding 不是列表
    response = await client.post(
        "/api/v1/ontology/embeddings/sync",
        json={
            "ontologyId": 1,
            "type": "class",
            "name": "test",
            "embedding": "not-a-list",
        },
    )
    assert response.status_code == 422


async def testSyncEmbeddingSuccess(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/ontology/embeddings/sync",
        json={
            "ontologyId": 42,
            "type": "class",
            "name": "TestClass",
            "alias": "测试类",
            "description": "一个测试类",
            "embedding": [0.05] * 1536,
        },
    )
    # Milvus 被 mock，不抛异常则成功
    assert response.status_code == 204


# =============================================================================
# Neo4j 同步测试（#65：update 时同步节点属性与关系）
# =============================================================================


async def testUpdateClassSyncsNeo4j(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Supplier", "sourceTable": "t_supplier"},
    )
    supplierId = create.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/classes/{supplierId}",
        json={"classAlias": "供应商", "description": "供应商主数据"},
    )
    cqls = "\n".join(_neo4j_called)
    # 更新触发 MERGE 节点属性同步
    assert "MERGE (c:Class {id: $id})" in cqls
    assert "c.name = $name" in cqls
    assert "c.sourceTable = $sourceTable" in cqls


async def testUpdateClassToleratesNeo4jFailure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.infrastructure.neo4j_client as neo4j_module

    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Warehouse", "sourceTable": "t_warehouse"},
    )
    warehouseId = create.json()["id"]
    monkeypatch.setattr(
        neo4j_module,
        "upsertClassNode",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("neo4j down")),
    )
    # Neo4j 失败不阻断 PG 更新（best-effort）
    response = await client.put(
        f"/api/v1/ontology/classes/{warehouseId}",
        json={"classAlias": "仓库"},
    )
    assert response.status_code == 200
    assert response.json()["classAlias"] == "仓库"


async def testUpdatePropertySyncsNeo4j(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Category", "sourceTable": "t_category"},
    )
    classId = cls.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "cat_name", "dataType": "STRING"},
    )
    propId = prop.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"propertyAlias": "分类名称", "isPrimaryKey": True},
    )
    cqls = "\n".join(_neo4j_called)
    assert "MERGE (p:Property {id: $id})" in cqls
    assert "p.isPrimaryKey = $isPrimaryKey" in cqls


async def testUpdatePropertyRelinksReferences(client: AsyncClient) -> None:
    classA = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Order", "sourceTable": "t_order"},
    )
    classB = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "OrderDetail", "sourceTable": "t_order_detail"},
    )
    classAId = classA.json()["id"]
    classBId = classB.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classAId,
            "propertyName": "order_no",
            "dataType": "STRING",
            "isForeignKey": True,
            "refClassId": classAId,
        },
    )
    propId = prop.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"refClassId": classBId},
    )
    cqls = "\n".join(_neo4j_called)
    # 引用类变化时：先删除旧 REFERENCES 关系，再建立新关系
    assert "DELETE r" in cqls
    assert "MERGE (p)-[:REFERENCES]->(c)" in cqls


async def testUpdatePropertyClearsReferencesWhenForeignKeyDisabled(
    client: AsyncClient,
) -> None:
    classA = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "OrderLine", "sourceTable": "t_order_line"},
    )
    classAId = classA.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classAId,
            "propertyName": "order_no",
            "dataType": "STRING",
            "isForeignKey": True,
            "refClassId": classAId,
        },
    )
    propId = prop.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"isForeignKey": False},  # ref_class_id 未变但外键已取消
    )
    cqls = "\n".join(_neo4j_called)
    # 只删除旧边，不因残留的 ref_class_id 重建 REFERENCES
    assert "DELETE r" in cqls
    assert "MERGE (p)-[:REFERENCES]->(c)" not in cqls


async def testUpdatePropertyEnsuresHasPropertyEdge(client: AsyncClient) -> None:
    cls = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Division", "sourceTable": "t_division"},
    )
    classId = cls.json()["id"]
    prop = await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": "div_name", "dataType": "STRING"},
    )
    propId = prop.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"propertyAlias": "部门名称"},
    )
    cqls = "\n".join(_neo4j_called)
    # 每次 update 都幂等 MERGE HAS_PROPERTY，自愈 create 中断导致的缺失边
    assert "MERGE (c)-[:HAS_PROPERTY]->(p)" in cqls


async def testUpdateMetricSyncsNeo4j(client: AsyncClient) -> None:
    metric = await client.post(
        "/api/v1/ontology/metrics",
        json={"metricName": "order_cnt", "formula": "COUNT(id)", "aggFunction": "COUNT"},
    )
    metricId = metric.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json={"metricAlias": "订单总量", "aggFunction": "SUM"},
    )
    cqls = "\n".join(_neo4j_called)
    assert "MERGE (m:Metric {id: $id})" in cqls
    assert "m.aggFunction = $aggFunction" in cqls


async def testUpdateMetricRelinksDerivedFrom(client: AsyncClient) -> None:
    classA = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "Invoice", "sourceTable": "t_invoice"},
    )
    classB = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "InvoiceDetail", "sourceTable": "t_invoice_detail"},
    )
    classAId = classA.json()["id"]
    classBId = classB.json()["id"]
    metric = await client.post(
        "/api/v1/ontology/metrics",
        json={
            "metricName": "amount",
            "formula": "SUM(amount)",
            "aggFunction": "SUM",
            "targetClassId": classAId,
        },
    )
    metricId = metric.json()["id"]
    _neo4j_called.clear()
    await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json={"targetClassId": classBId},
    )
    cqls = "\n".join(_neo4j_called)
    # 目标类变化时重建 DERIVED_FROM 关系
    assert "DELETE r" in cqls
    assert "MERGE (m)-[:DERIVED_FROM]->(c)" in cqls


# =============================================================================
# Semantic search tests
# =============================================================================


async def _stubEmbedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入假 embedding 生成，避免测试调用真实 LLM。"""
    from app.api.v1 import ontology as ontology_api

    async def fakeGenerateEmbedding(text: str) -> list[float]:
        return [0.1] * 1536

    monkeypatch.setattr(
        ontology_api._embeddingService, "generateEmbedding", fakeGenerateEmbedding
    )


async def testSearchRejectsEmptyQuery(client: AsyncClient) -> None:
    # q 为空 -> min_length=1 校验失败
    response = await client.get("/api/v1/ontology/search", params={"q": ""})
    assert response.status_code == 422


async def testSearchReturnsHits(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stubEmbedding(monkeypatch)
    import app.services.ontology_service as ontology_module

    monkeypatch.setattr(
        ontology_module.milvus,
        "searchByEmbedding",
        lambda *a, **kw: [
            {
                "ontology_id": 1,
                "type": "class",
                "name": "客户",
                "alias": "Customer",
                "description": "客户信息",
                "distance": 0.5,
            },
            {
                "ontology_id": 7,
                "type": "metric",
                "name": "销售额",
                "alias": None,
                "description": None,
                "distance": 1.0,
            },
        ],
    )

    response = await client.get(
        "/api/v1/ontology/search", params={"q": "客户销售", "topK": 5}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0] == {
        "id": 1,
        "type": "class",
        "name": "客户",
        "alias": "Customer",
        "description": "客户信息",
        "score": round(1.0 / (1.0 + 0.5), 4),  # 0.6667
    }
    assert data[1]["id"] == 7
    assert data[1]["alias"] is None
    assert data[1]["score"] == 0.5  # 1/(1+1.0)


async def testSearchTypeFilterForwarded(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stubEmbedding(monkeypatch)
    import app.services.ontology_service as ontology_module

    captured: dict = {}

    def fakeSearch(embedding, topK=5, typeFilter=None):
        captured["topK"] = topK
        captured["typeFilter"] = typeFilter
        return []

    monkeypatch.setattr(ontology_module.milvus, "searchByEmbedding", fakeSearch)

    response = await client.get(
        "/api/v1/ontology/search", params={"q": "客户", "type": "metric"}
    )
    assert response.status_code == 200
    assert response.json() == []
    assert captured["typeFilter"] == "metric"


async def testSearchMilvusFailureReturns400(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stubEmbedding(monkeypatch)
    from pymilvus.exceptions import MilvusException

    import app.services.ontology_service as ontology_module

    def raisingSearch(*a, **kw):
        raise MilvusException("connection lost")

    monkeypatch.setattr(ontology_module.milvus, "searchByEmbedding", raisingSearch)

    response = await client.get("/api/v1/ontology/search", params={"q": "客户"})
    # MilvusError -> DomainError -> 全局处理器转 400
    assert response.status_code == 400
    assert "向量检索失败" in response.json().get("error", "")
