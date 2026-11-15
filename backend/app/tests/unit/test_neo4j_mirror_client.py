"""发布镜像写路径的单测（P3b D22）。

**为什么单独一个文件**：`app/infrastructure/neo4j_client.py` 已 1024 行（超出
「文件 ≤ 800 行」的仓内约定），镜像写路径自成一体（独立的 label 白名单 + 关系类型
白名单 + release_id 守卫）⇒ 独立模块，driver 仍复用 `neo4j_client.getDriver()`，
不重复连接逻辑。

不需要外部 Neo4j 服务：**写目标闸门**在访问 driver 前即抛错；计数/CQL 断言用
替身 driver（并把写目标指到测试实例端口）。
"""

from __future__ import annotations

import pytest

from app.infrastructure import neo4j_mirror_client as mirror

_TEST_TARGET_URI = "bolt://localhost:7688"
_PROD_TARGET_URI = "bolt://localhost:7687"


class _FakeResult:
    """最小 neo4j Result 替身：只提供 single() / 迭代。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def single(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """记录 CQL 与参数，按 countOf 决定返回的计数。

    `record` / `records` 用于读路径（台账读取）：给了就按它返回，
    不给就走计数口径（与既有测试的默认行为一致）。
    """

    def __init__(self, countOf, *, record=None, records=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._countOf = countOf
        self._record = record
        self._records = records

    def run(self, cql: str, **params):
        self.calls.append((cql, params))
        if self._records is not None:
            return _FakeResult(self._records)
        if self._record is not None:
            return _FakeResult([self._record])
        rows = params.get("rows") or []
        return _FakeResult([{"cnt": self._countOf(len(rows))}])

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeDriver:
    def __init__(self, countOf=None, *, record=None, records=None) -> None:
        self.countOf = countOf or (lambda n: n)
        self.sessionObj = _FakeSession(self.countOf, record=record, records=records)

    def session(self) -> _FakeSession:
        return self.sessionObj


def _useTestTarget(monkeypatch, driver: _FakeDriver | None = None) -> _FakeDriver:
    """把写目标指到**测试实例**（默认拒绝写非测试目标）+ 装上替身 driver。"""
    fakeDriver = driver or _FakeDriver()
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: fakeDriver)
    return fakeDriver


def _nodeRow(unifiedId: str, releaseId: int = 7) -> dict:
    return {
        "unified_id": unifiedId,
        "release_id": releaseId,
        "release_version": "v1.2.0",
        "mirror_of": unifiedId,
        "name": "x",
        "alias": None,
        "description": None,
        "sourceTable": None,
    }


def _edgeRow(relType: str = "JOIN") -> dict:
    return {
        "relType": relType,
        "sourceType": "class",
        "sourceUid": "obj:CLASS:1",
        "targetType": "class",
        "targetUid": "obj:CLASS:2",
    }


# =============================================================================
# 守卫：镜像只能写 Release* label
# =============================================================================


def testUpsertReleaseMirrorNodesRejectsNonReleaseType() -> None:
    # 主图 label / 任意字符串都必须拒绝（Neo4j 无写目标闸门 ⇒ 守卫是唯一防线）
    for bad in ("Class", "Property", "Metric", "KpiCatalog", "Class; DROP", "User"):
        with pytest.raises(ValueError, match="Invalid release mirror entity type"):
            mirror.upsertReleaseMirrorNodes(bad, 7, "v1.2.0", [_nodeRow("obj:CLASS:1")])


def testUpsertReleaseMirrorNodesRejectsRowsWithoutUnifiedId() -> None:
    for badRows in ([{"release_id": 7}], [{"unified_id": "  "}], [{"unified_id": 123}]):
        with pytest.raises(ValueError, match="unified_id"):
            mirror.upsertReleaseMirrorNodes("class", 7, "v1.2.0", badRows)


def testUpsertReleaseMirrorNodesRejectsForeignReleaseId() -> None:
    # 行里的 release_id 与参数不一致 ⇒ 拒写（防「写到别的发布里去」）
    with pytest.raises(ValueError, match="release_id"):
        mirror.upsertReleaseMirrorNodes(
            "class", 7, "v1.2.0", [_nodeRow("obj:CLASS:1", releaseId=9)]
        )


def testUpsertReleaseMirrorNodesRejectsUnifiedIdOfWrongBusinessObject() -> None:
    """属性 id 不能写成 ReleaseClass（语义错位：图上看着正常，对账时才炸）。"""
    with pytest.raises(ValueError, match="不匹配"):
        mirror.upsertReleaseMirrorNodes("class", 7, "v1.2.0", [_nodeRow("obj:PROPERTY:1")])


def testUpsertReleaseMirrorNodesRejectsRowWithoutReleaseFields() -> None:
    row = _nodeRow("obj:CLASS:1")
    del row["mirror_of"]
    with pytest.raises(ValueError, match="mirror_of"):
        mirror.upsertReleaseMirrorNodes("class", 7, "v1.2.0", [row])


def testUpsertReleaseMirrorEdgesRejectsUnknownRelationType() -> None:
    with pytest.raises(ValueError, match="Invalid release mirror relation type"):
        mirror.upsertReleaseMirrorEdges(7, [_edgeRow(relType="SUPPLIES")])


def testUpsertReleaseMirrorEdgesRejectsUnknownEntityType() -> None:
    edge = _edgeRow()
    edge["sourceType"] = "Class"
    with pytest.raises(ValueError, match="Invalid release mirror entity type"):
        mirror.upsertReleaseMirrorEdges(7, [edge])


def testUpsertReleaseMirrorEdgesRejectsMismatchedEndpointTypes() -> None:
    """HAS_PROPERTY 只能是 类→属性；写成 类→类 必须拒绝。"""
    edge = _edgeRow(relType="HAS_PROPERTY")
    with pytest.raises(ValueError, match="端点类型"):
        mirror.upsertReleaseMirrorEdges(7, [edge])


def testUpsertReleaseMirrorEdgesRejectsRowsWithoutEndpoints() -> None:
    for missing in ("sourceUid", "targetUid"):
        edge = _edgeRow()
        edge[missing] = "  "
        with pytest.raises(ValueError, match=missing):
            mirror.upsertReleaseMirrorEdges(7, [edge])


# =============================================================================
# 写目标闸门（库层：所有调用方都生效，不只是脚本）
# =============================================================================


def testUpsertReleaseMirrorNodesRefusesNonTestTarget(monkeypatch) -> None:
    """默认只准写测试实例；生产目标必须被拦（Neo4j 无备份 ⇒ 误写不可逆）。"""
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: _FakeDriver())
    with pytest.raises(ValueError, match="拒绝向"):
        mirror.upsertReleaseMirrorNodes("class", 7, "v1.2.0", [_nodeRow("obj:CLASS:1")])


def testUpsertReleaseMirrorEdgesRefusesNonTestTarget(monkeypatch) -> None:
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: _FakeDriver())
    with pytest.raises(ValueError, match="拒绝向"):
        mirror.upsertReleaseMirrorEdges(7, [_edgeRow()])


def testPruneReleaseMirrorRefusesNonTestTarget(monkeypatch) -> None:
    """裁剪是破坏性操作 ⇒ 目标闸门必须同样生效。"""
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: _FakeDriver())
    with pytest.raises(ValueError, match="拒绝向"):
        mirror.pruneReleaseMirror(7, set(), confirm=True)


def testExplicitAllowProdWritePassesTheGate(monkeypatch) -> None:
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    driver = _FakeDriver()
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    written = mirror.upsertReleaseMirrorNodes(
        "class", 7, "v1.2.0", [_nodeRow("obj:CLASS:1")], allowProdWrite=True
    )
    assert written == 1


# =============================================================================
# 写出的 CQL：只碰镜像 label
# =============================================================================


def testUpsertReleaseMirrorNodesUsesReleaseLabelOnly(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    mirror.upsertReleaseMirrorNodes("class", 7, "v1.2.0", [_nodeRow("obj:CLASS:1")])
    cql = driver.sessionObj.calls[0][0]
    assert ":ReleaseClass" in cql
    # 主图 label 绝不出现在 CQL 里（"ReleaseClass" 含 "Class" 子串 ⇒ 先剔除镜像 label）
    from app.domain.semantic_graph_mirror_spec import RELEASE_MIRROR_LABELS

    withoutMirrorLabels = cql
    for label in RELEASE_MIRROR_LABELS.values():
        withoutMirrorLabels = withoutMirrorLabels.replace(f":{label}", "")
    for mainLabel in (":Class", ":Property", ":Metric", ":KpiCatalog"):
        assert mainLabel not in withoutMirrorLabels
    # 幂等键必须落在 MERGE 上（不是只出现在 SET 里）
    assert (
        "MERGE (n:ReleaseClass {unified_id: row.unified_id, release_id: $releaseId})"
        in cql
    )
    assert "mirror_of" in cql
    assert "release_id" in cql


def testUpsertReleaseMirrorEdgesDetectsMissingEndpoints(monkeypatch) -> None:
    # 端点缺失 ⇒ 命中的边数少于请求数 ⇒ 必须报错，不能静默少写
    driver = _FakeDriver(countOf=lambda n: n - 1)
    _useTestTarget(monkeypatch, driver)
    with pytest.raises(ValueError, match="端点缺失"):
        mirror.upsertReleaseMirrorEdges(7, [_edgeRow()])


def testUpsertReleaseMirrorEdgesWritesAllRowsWhenEndpointsExist(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    written = mirror.upsertReleaseMirrorEdges(7, [_edgeRow(), _edgeRow()])
    assert written == 2
    cql = driver.sessionObj.calls[0][0]
    assert ":ReleaseClass" in cql
    # 边幂等键 = 两端节点 + 关系类型；两端 MATCH 都必须限定 release_id
    assert "MERGE (a)-[r:JOIN]->(b)" in cql
    assert cql.count("release_id: $releaseId") == 2


# =============================================================================
# 计数与清理
# =============================================================================


def testCountReleaseMirrorNodesReturnsPerTypeCounts(monkeypatch) -> None:
    _useTestTarget(monkeypatch)
    counts = mirror.countReleaseMirrorNodes(7)
    assert set(counts) == {"class", "property", "metric", "kpi"}
    assert all(value >= 0 for value in counts.values())


def testPruneReleaseMirrorRequiresExplicitConfirmation(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    with pytest.raises(ValueError, match="确认"):
        mirror.pruneReleaseMirror(7, {"obj:CLASS:1"}, confirm=False)
    assert driver.sessionObj.calls == []


def testPruneReleaseMirrorDeletesOnlyUnplannedNodes(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    mirror.pruneReleaseMirror(7, {"obj:CLASS:1"}, confirm=True)
    cql = driver.sessionObj.calls[0][0]
    assert "DETACH DELETE" in cql
    assert ":ReleaseClass" in cql
    assert "release_id" in cql


def testPruneReleaseMirrorAlsoDeletesStaleEdgesBetweenKeptNodes(monkeypatch) -> None:
    """两端都保留、但已不在计划里的边（例如 manifest 删掉了一条 JOIN）也必须清掉。"""
    driver = _useTestTarget(monkeypatch)
    mirror.pruneReleaseMirror(7, {"obj:CLASS:1", "obj:CLASS:2"}, confirm=True)
    edgeCalls = [
        cql for cql, _ in driver.sessionObj.calls if "DELETE r" in cql
    ]
    assert edgeCalls, "裁剪必须单独清理残留边"
    assert ":ReleaseClass" in edgeCalls[0]
    assert "NOT (a.unified_id IN $keep AND b.unified_id IN $keep)" in edgeCalls[0]


def testCountReleaseMirrorEdgesReturnsPerRelationTypeCounts(monkeypatch) -> None:
    _useTestTarget(monkeypatch)
    counts = mirror.countReleaseMirrorEdges(7)
    assert set(counts) == {
        "HAS_PROPERTY",
        "SUBCLASS_OF",
        "JOIN",
        "DERIVED_FROM",
        "MEASURED_BY",
        "REFERENCES",
    }
    assert all(value >= 0 for value in counts.values())


def testCountReleaseMirrorEdgesConstrainsBothEndpointLabels(monkeypatch) -> None:
    """统计口径必须与写入口径一致（两端 label 固定 ⇒ 不扫整图、不误统计主图）。"""
    driver = _useTestTarget(monkeypatch)
    mirror.countReleaseMirrorEdges(7)
    cqls = [cql for cql, _ in driver.sessionObj.calls]
    assert any(
        ":ReleaseClass)-[r:HAS_PROPERTY]->(b:ReleaseProperty" in cql for cql in cqls
    )
    assert all("MATCH (a)-[r]->(b)" not in cql for cql in cqls)


# =============================================================================
# 物化作业台账（P3b D23）
# =============================================================================
#
# 台账节点**不是镜像实体**：独立 label（MirrorJobState / MirrorJobBatch）⇒
# 对账时「镜像实体数」不受台账影响；台账写入同样受写目标闸门保护。


def _stateRow(**overrides) -> dict:
    row = {
        "release_id": 7,
        "release_version": "2026.11.09-1",
        "status": "materialized",
        "manifest_hash": "a" * 64,
        "node_count": 3,
        "edge_count": 2,
        "unresolved_count": 0,
        "started_at": "2026-11-15T10:00:00+00:00",
        "updated_at": "2026-11-15T10:00:05+00:00",
        "finished_at": "2026-11-15T10:00:05+00:00",
        "error": "",
    }
    row.update(overrides)
    return row


def testWriteMirrorJobStateUsesLedgerLabelOnly(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    mirror.writeMirrorJobState(_stateRow())
    cql = driver.sessionObj.calls[0][0]
    assert ":MirrorJobState" in cql
    # 台账绝不能长成主图实体（否则会被消费方当成本体）
    for mainLabel in (":Class", ":Property", ":Metric", ":KpiCatalog"):
        assert mainLabel not in cql


def testWriteMirrorJobStateRefusesNonTestTarget(monkeypatch) -> None:
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: _FakeDriver())
    with pytest.raises(ValueError, match="拒绝向"):
        mirror.writeMirrorJobState(_stateRow())


def testWriteMirrorJobBatchUsesLedgerLabelOnly(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    mirror.writeMirrorJobBatch(
        {
            "release_id": 7,
            "batch_no": "B2",
            "kind": "nodes",
            "status": "succeeded",
            "count": 1,
            "elapsed_ms": 12,
            "error": "",
            "manifest_hash": "a" * 64,
            "updated_at": "2026-11-15T10:00:02+00:00",
        }
    )
    cql = driver.sessionObj.calls[0][0]
    assert ":MirrorJobBatch" in cql
    assert "batch_no" in cql


def testWriteMirrorJobBatchRefusesNonTestTarget(monkeypatch) -> None:
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _PROD_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: _FakeDriver())
    with pytest.raises(ValueError, match="拒绝向"):
        mirror.writeMirrorJobBatch(
            {
                "release_id": 7,
                "batch_no": "B1",
                "kind": "nodes",
                "status": "running",
            }
        )


def testReadMirrorJobStateReturnsProperties(monkeypatch) -> None:
    driver = _FakeDriver(record={"s": _stateRow()})
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    state = mirror.readMirrorJobState(7)
    assert state is not None
    assert state["status"] == "materialized"
    assert state["manifest_hash"] == "a" * 64
    assert state["node_count"] == 3


def testReadMirrorJobStateReturnsNoneWhenAbsent(monkeypatch) -> None:
    driver = _FakeDriver(records=[])
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    assert mirror.readMirrorJobState(7) is None


def testReadMirrorJobBatchesReturnsRows(monkeypatch) -> None:
    driver = _FakeDriver(
        records=[
            {"b": {"release_id": 7, "batch_no": "B1", "kind": "nodes", "status": "succeeded"}},
            {"b": {"release_id": 7, "batch_no": "B2", "kind": "nodes", "status": "failed"}},
        ]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    batches = mirror.readMirrorJobBatches(7)
    assert [batch["batch_no"] for batch in batches] == ["B1", "B2"]
    assert batches[1]["status"] == "failed"


def testCountStaleReleaseMirrorNodesCountsOnlyOutsideKeep(monkeypatch) -> None:
    driver = _useTestTarget(monkeypatch)
    mirror.countStaleReleaseMirrorNodes(7, {"obj:CLASS:1"})
    cqls = [cql for cql, _ in driver.sessionObj.calls]
    assert any(":ReleaseClass" in cql for cql in cqls)
    assert all("NOT n.unified_id IN $keep" in cql for cql in cqls)


# =============================================================================
# D24：镜像**全量读**（对账脚本的输入；只读，不走写目标闸门）
# =============================================================================


def testReadReleaseMirrorNodesReturnsRows(monkeypatch) -> None:
    driver = _FakeDriver(
        records=[
            {"n": {"unified_id": "obj:CLASS:1", "name": "采购订单", "release_id": 7}},
            {"n": {"unified_id": "obj:CLASS:2", "name": "供应商", "release_id": 7}},
        ]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    rows = mirror.readReleaseMirrorNodes("class", 7)
    assert [row["unified_id"] for row in rows] == ["obj:CLASS:1", "obj:CLASS:2"]
    cql, params = driver.sessionObj.calls[0]
    assert ":ReleaseClass" in cql
    assert params["releaseId"] == 7


def testReadReleaseMirrorNodesRejectsNonMirrorType(monkeypatch) -> None:
    """`Class` 不是镜像 label ⇒ 拒绝（读路径也守住「镜像只读 Release*」）。"""
    driver = _useTestTarget(monkeypatch)
    with pytest.raises(ValueError):
        mirror.readReleaseMirrorNodes("Class", 7)
    assert driver.sessionObj.calls == []


def testReadReleaseMirrorEdgesReturnsTypedTriples(monkeypatch) -> None:
    driver = _FakeDriver(
        records=[
            {"rel_type": "JOIN", "source_uid": "obj:CLASS:1", "target_uid": "obj:CLASS:2"},
            {"rel_type": "HAS_PROPERTY", "source_uid": "obj:CLASS:1", "target_uid": "obj:PROPERTY:1"},
        ]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    edges = mirror.readReleaseMirrorEdges(7)
    assert ("JOIN", "obj:CLASS:1", "obj:CLASS:2") in edges
    assert ("HAS_PROPERTY", "obj:CLASS:1", "obj:PROPERTY:1") in edges
    cql, params = driver.sessionObj.calls[0]
    # relType **不可**拼进 CQL（label/relType 注入防线）⇒ 必须走参数化白名单
    assert "type(r) IN $relTypes" in cql
    assert set(params["relTypes"]) <= {"HAS_PROPERTY", "SUBCLASS_OF", "JOIN",
                                       "DERIVED_FROM", "MEASURED_BY", "REFERENCES"}
    assert params["releaseId"] == 7


def testReadReleaseMirrorEdgesConstrainsBothEndpointsToMirrorLabels(monkeypatch) -> None:
    """两端都必须带 Release* label 约束 ⇒ 不扫主图边。"""
    driver = _FakeDriver(records=[])
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    mirror.readReleaseMirrorEdges(7)
    cql, _ = driver.sessionObj.calls[0]
    assert ":ReleaseClass" in cql and ":ReleaseProperty" in cql


# =============================================================================
# D25：发布钉住的**本体读**（读侧切换的镜像侧；与主图 neo4j_client 读函数同形）
# =============================================================================


def testGetReleaseClassIdsReturnsMirrorIds(monkeypatch) -> None:
    driver = _FakeDriver(
        records=[{"uid": "obj:CLASS:1"}, {"uid": "obj:CLASS:2"}]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    ids = mirror.getReleaseClassIds(7)
    assert ids == {"obj:CLASS:1", "obj:CLASS:2"}
    cql, params = driver.sessionObj.calls[0]
    assert ":ReleaseClass" in cql
    assert params["releaseId"] == 7


def testGetReleasePropertyIdsAndMetricIdsUseOwnLabels(monkeypatch) -> None:
    driver = _FakeDriver(records=[{"uid": "obj:PROPERTY:1"}])
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    assert mirror.getReleasePropertyIds(7) == {"obj:PROPERTY:1"}
    assert ":ReleaseProperty" in driver.sessionObj.calls[0][0]
    driver2 = _FakeDriver(records=[{"uid": "obj:METRIC:1"}])
    monkeypatch.setattr(mirror, "getDriver", lambda: driver2)
    assert mirror.getReleaseMetricIds(7) == {"obj:METRIC:1"}
    assert ":ReleaseMetric" in driver2.sessionObj.calls[0][0]


def testGetReleaseKpiCatalogIdsUsesKpiLabel(monkeypatch) -> None:
    driver = _FakeDriver(records=[{"uid": "obj:KPI:1"}])
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    assert mirror.getReleaseKpiCatalogIds(7) == {"obj:KPI:1"}
    assert ":ReleaseKpiCatalog" in driver.sessionObj.calls[0][0]


def testGetReleaseJoinPairsReturnsEndpointPairs(monkeypatch) -> None:
    driver = _FakeDriver(
        records=[
            {"sourceUid": "obj:CLASS:1", "targetUid": "obj:CLASS:2"},
            {"sourceUid": "obj:CLASS:1", "targetUid": "obj:CLASS:3"},
        ]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    pairs = mirror.getReleaseJoinPairs(7)
    assert pairs == {("obj:CLASS:1", "obj:CLASS:2"), ("obj:CLASS:1", "obj:CLASS:3")}
    cql, _ = driver.sessionObj.calls[0]
    assert "[:JOIN]" in cql


def testGetReleaseRelationTriplesReadsSubclassOnly(monkeypatch) -> None:
    """镜像的 class→class 非 JOIN 边只有 SUBCLASS_OF ⇒ 只读到它。

    语义关系边（ontology_relation）不在镜像（manifest 无 relations 分区，D22 C-4）
    ⇒ 按发布读图在关系维度的覆盖 = SUBCLASS_OF，**这是显式范围声明**（L-2/R-2）。
    """
    driver = _FakeDriver(
        records=[{"sourceUid": "obj:CLASS:1", "targetUid": "obj:CLASS:2", "relType": "SUBCLASS_OF"}]
    )
    monkeypatch.setattr(mirror, "_effectiveTargetUri", lambda: _TEST_TARGET_URI)
    monkeypatch.setattr(mirror, "getDriver", lambda: driver)
    triples = mirror.getReleaseRelationTriples(7)
    assert triples == {("obj:CLASS:1", "obj:CLASS:2", "SUBCLASS_OF")}
    cql, _ = driver.sessionObj.calls[0]
    assert "[:SUBCLASS_OF]" in cql
