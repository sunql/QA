"""Agent NL2SQL 工具的**快照分支**（P3d D29）单测。

钉住了发布 ⇒ `list_tables` / `describe_table` / `list_joins` 从工具视图
（`ToolOntologyView`）渲染；没钉住 ⇒ 原直读路径（一行不改）。

**输出必须与直读逐字节一致**（G4「工具输出与切换前一致」）——单元层先钉
渲染形状，真实数据的逐字节对等在 D29 的 real-data 验证里做（直读 vs 钉住
对同一本体跑同一工具，比对 JSON）。
"""

from __future__ import annotations

import json

from app.services.agent_tools_nl2sql import (
    _describeTableFromView,
    _listJoinsFromView,
    _listTablesFromView,
)
from app.services.agent_tools_semantic_gate import ToolOntologyView, loadToolOntology


def _manifest() -> dict:
    """与 test_agent_tools_semantic_gate 的 fixture 同一份本体（两文件独立）。"""
    return {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单头",
                "sourceTable": "DWD_PURCHASE_ORDER",
                "parentClassName": None,
                "properties": [
                    {
                        "propertyName": "po_no",
                        "propertyAlias": "单号",
                        "description": "采购订单号",
                        "dataType": "VARCHAR",
                        "sourceColumn": "PO_NO",
                        "isPrimaryKey": True,
                        "isForeignKey": False,
                        "refClassName": None,
                        "isNotNull": True,
                    },
                    {
                        "propertyName": "supplier_id",
                        "propertyAlias": None,
                        "description": None,
                        "dataType": "BIGINT",
                        "sourceColumn": "SUPPLIER_ID",
                        "isPrimaryKey": False,
                        "isForeignKey": True,
                        "refClassName": "供应商",
                        "isNotNull": None,
                    },
                ],
            },
            {
                "className": "供应商",
                "classAlias": None,
                "description": None,
                "sourceTable": "DIM_SUPPLIER",
                "parentClassName": None,
                "properties": [],
            },
        ],
        "joins": [
            {
                "sourceClassName": "采购订单",
                "sourceColumns": ["SUPPLIER_ID"],
                "targetClassName": "供应商",
                "targetColumns": ["SUPPLIER_ID"],
                "joinType": "INNER",
                "relationType": "business",
                "description": None,
            }
        ],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


def _snapshot(manifest=None, releaseId: int = 7):
    import hashlib

    from app.domain.semantic_manifest_schema import canonicalManifestJson
    from app.services.semantic_registry_service import SemanticReleaseSnapshot

    body = manifest if manifest is not None else _manifest()
    canonical = canonicalManifestJson(body)
    return SemanticReleaseSnapshot(
        id=releaseId,
        version="2026.11.15-1",
        domain_code=None,
        status="ACTIVE",
        schema_version="1.0",
        manifest_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        activated_time=None,
        _manifestJson=canonical,
    )


def _view(manifest=None) -> ToolOntologyView:
    import asyncio

    snap = _snapshot(manifest=manifest if manifest is not None else _manifest())
    view = asyncio.run(loadToolOntology(session=None, pinnedReleaseId=7, registry=_snapRegistry(snap)))
    assert view is not None
    return view


def _snapRegistry(snapshot):
    class _R:
        async def getById(self, session, releaseId):
            return snapshot

    return _R()


def _content(result_json: str) -> dict:
    return json.loads(result_json)


# =============================================================================
# list_tables 视图分支
# =============================================================================


def test_list_tables_视图分支_排序去重() -> None:
    """直读口径：按 source_table 排序 + 去重（同一表多类只出现一次）。"""
    manifest = _manifest()
    extra = dict(manifest["classes"][0])
    extra["className"] = "采购订单行"
    extra["sourceTable"] = "DWD_PURCHASE_ORDER"  # 同表第二类 ⇒ 去重
    manifest["classes"].append(extra)
    result = _listTablesFromView(_view(manifest))
    body = _content(result.content)
    assert body == {"tables": ["DIM_SUPPLIER", "DWD_PURCHASE_ORDER"]}
    assert result.is_error is False
    assert result.name == "list_tables"


def test_list_tables_视图分支_跳过无表名的类() -> None:
    manifest = _manifest()
    noTable = dict(manifest["classes"][0])
    noTable["className"] = "无表类"
    noTable["sourceTable"] = None
    manifest["classes"].append(noTable)
    body = _content(_listTablesFromView(_view(manifest)).content)
    assert body == {"tables": ["DIM_SUPPLIER", "DWD_PURCHASE_ORDER"]}


# =============================================================================
# describe_table 视图分支
# =============================================================================


def test_describe_table_视图分支_列与FK引用解析() -> None:
    """列格式与直读一致：名字/别名/类型/列/PK-FK 旗标/FK 目标表名。"""
    result = _describeTableFromView(_view(), table_name="DWD_PURCHASE_ORDER")
    body = _content(result.content)
    assert result.name == "describe_table"
    assert body["table"] == "DWD_PURCHASE_ORDER"
    assert body["column_format"]
    columns = body["columns"]
    # 按 property_name 排序（直读 order_by(OntologyProperty.property_name)）
    assert [c[0] for c in columns] == ["po_no", "supplier_id"]
    poNo = columns[0]
    assert poNo[1] == "单号"  # 别名与名字/列名都不同值 ⇒ 保留（直读口径）
    assert poNo[4] == "PK"
    supplierId = columns[1]
    assert supplierId[4] == "FK"
    assert supplierId[5] == "DIM_SUPPLIER"  # FK 目标表名（视图内按合成 id 解析）


def test_describe_table_视图分支_表不存在时与直读同错误() -> None:
    result = _describeTableFromView(_view(), table_name="NO_SUCH")
    body = _content(result.content)
    assert result.is_error is True
    assert body["error"] == "TABLE_NOT_FOUND"


def test_describe_table_视图分支_别名与列名相同时清空() -> None:
    """直读口径：alias in (property_name, source_column) ⇒ 输出空串。"""
    manifest = _manifest()
    prop = dict(manifest["classes"][0]["properties"][0])
    prop["propertyAlias"] = "PO_NO"  # 与 source_column 相同 ⇒ 视为无别名
    prop["propertyName"] = "po_number"
    manifest["classes"][0]["properties"] = [prop]
    result = _describeTableFromView(_view(manifest), table_name="DWD_PURCHASE_ORDER")
    columns = _content(result.content)["columns"]
    assert columns[0][0] == "po_number"
    assert columns[0][1] == ""


# =============================================================================
# list_joins 视图分支
# =============================================================================


def test_list_joins_视图分支_边渲染与直读同形() -> None:
    result = _listJoinsFromView(_view(), tables=None)
    body = _content(result.content)
    assert body["count"] == 1
    assert body["edges"] == ["DWD_PURCHASE_ORDER.SUPPLIER_ID -> DIM_SUPPLIER.SUPPLIER_ID"]


def test_list_joins_视图分支_按表过滤() -> None:
    result = _listJoinsFromView(_view(), tables=["DIM_SUPPLIER"])
    assert _content(result.content)["count"] == 1
    result2 = _listJoinsFromView(_view(), tables=["NO_MATCH"])
    assert _content(result2.content)["count"] == 0


def test_list_joins_视图分支_非INNER带类型标注() -> None:
    manifest = _manifest()
    join = dict(manifest["joins"][0])
    join["joinType"] = "LEFT"
    manifest["joins"] = [join]
    result = _listJoinsFromView(_view(manifest), tables=None)
    assert _content(result.content)["edges"] == [
        "DWD_PURCHASE_ORDER.SUPPLIER_ID -> DIM_SUPPLIER.SUPPLIER_ID (LEFT)"
    ]


def test_list_joins_视图分支_反向重复边去重() -> None:
    """A→B 与 B→A 同列 ⇒ 只留一条（直读口径）。"""
    manifest = _manifest()
    reverse = {
        "sourceClassName": "供应商",
        "sourceColumns": ["SUPPLIER_ID"],
        "targetClassName": "采购订单",
        "targetColumns": ["SUPPLIER_ID"],
        "joinType": "INNER",
        "relationType": "business",
        "description": None,
    }
    manifest["joins"].append(reverse)
    result = _listJoinsFromView(_view(manifest), tables=None)
    assert _content(result.content)["count"] == 1


def test_list_joins_视图分支_tables_传单个字符串不逐字符迭代() -> None:
    result = _listJoinsFromView(_view(), tables="DIM_SUPPLIER")
    assert _content(result.content)["count"] == 1


# =============================================================================
# D29 接线：钉住 id 的**穿透链**（runtime → dispatch → handler → 闸门）
# =============================================================================


async def test_dispatch_透传_pinnedReleaseId_到闸门(monkeypatch) -> None:
    """runtime 给的钉住 id 必须一路传到闸门（传丢 = 假钉住 = 假切换）。"""
    import app.services.agent_tools_nl2sql as nl2sql

    captured: list[int | None] = []

    async def fakeGate(session, *, pinnedReleaseId, registry=None):
        captured.append(pinnedReleaseId)
        return None

    monkeypatch.setattr(nl2sql, "loadToolOntology", fakeGate)
    from app.infrastructure.llm.base_client import ToolCall

    tc = ToolCall(id="t1", name="list_tables", args={})
    await nl2sql.dispatch_tool_call(tc, session=None, pinnedReleaseId=7)
    await nl2sql.dispatch_tool_call(tc, session=None)
    assert captured == [7, None]


async def test_dispatch_默认不钉住(monkeypatch) -> None:
    import app.services.agent_tools_nl2sql as nl2sql

    captured: list[int | None] = []

    async def fakeGate(session, *, pinnedReleaseId, registry=None):
        captured.append(pinnedReleaseId)
        return None

    monkeypatch.setattr(nl2sql, "loadToolOntology", fakeGate)
    from app.infrastructure.llm.base_client import ToolCall

    tc = ToolCall(id="t1", name="describe_table", args={"table_name": "DWD_PURCHASE_ORDER"})
    await nl2sql.dispatch_tool_call(tc, session=None)
    assert captured == [None]


async def test_dispatchSingleTool_透传_state_里的钉住(monkeypatch) -> None:
    """L4 loop 的 state['pinned_release_id'] 必须传到 dispatch（线程不断）。"""
    import app.services.agent_runtime_service as runtime
    from types import SimpleNamespace

    captured: list[int | None] = []

    async def fakeDispatch(tc_adapter, *, session, pinnedReleaseId=None):
        captured.append(pinnedReleaseId)
        return SimpleNamespace(is_error=False, content="{}")

    monkeypatch.setattr(runtime, "dispatch_tool_call", fakeDispatch)
    tc = SimpleNamespace(id="t1", name="list_tables", args={})
    await runtime._dispatchSingleTool(
        tc=tc,
        session=None,
        executor=SimpleNamespace(),
        pinned_release_id=9,
    )
    assert captured == [9]
