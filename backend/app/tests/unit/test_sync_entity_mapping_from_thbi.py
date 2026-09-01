"""sync_entity_mapping_from_thbi.py 单测。

覆盖契约：
- _stableKey：同输入必同输出（进程间稳定）+ 落在声明的 [offset, offset+1M) 区间
- _mapping：构造的 dict 与 EntityMapping 模型字段一致
- _buildAllRows：SUPPLIER/MATERIAL 各自带正确 offset
- syncEntityMappings dry-run：不写库，统计正确
- syncEntityMappings 真写：调 ON CONFLICT DO NOTHING，统计 inserted/skipped 正确

不依赖 THBI Oracle / 真实 PG；adapter 与 session 均 fake。
集成 / smoke 由 headless Playwright + docker 跑真实后端验证。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.sync_entity_mapping_from_thbi as sync_mod
from scripts.sync_entity_mapping_from_thbi import (
    _MATERIAL_KEY_OFFSET,
    _SUPPLIER_KEY_OFFSET,
    _buildAllRows,
    _mapping,
    _stableKey,
    syncEntityMappings,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """记录每次 execute 调用，模拟 ON CONFLICT 行为。

    - 已有 (entity_type, enterprise_key, source_system) → rowcount=0（DB 跳过）
    - 新组合 → rowcount=1（DB 写入）
    """

    def __init__(self) -> None:
        self.existing: set[tuple[str, int, str]] = set()
        self.executed: list[Any] = []

    @staticmethod
    def _extractRows(stmt: Any) -> list[dict[str, Any]]:
        """SQLAlchemy Insert 的两种形态：

        - 单值 .values(**m) → _values = immutabledict[Column, BindParameter]
        - executemany .values([m1, m2]) → _multi_values = ([{Column: literal}],)
        统一抽取成 [{col_name: literal}, ...] 的 list。
        """
        raw = getattr(stmt, "_values", None)
        if raw:
            return [{col.name: bind.value for col, bind in raw.items()}]
        multi = getattr(stmt, "_multi_values", None)
        if multi:
            # multi 是 (tuple_of_dicts,) — 每个 dict 是 {Column: literal}
            batch = multi[0] if isinstance(multi, tuple) and multi else multi
            return [{col.name: v for col, v in row.items()} for row in batch]
        return []

    async def execute(self, stmt: Any) -> _Result:
        self.executed.append(stmt)
        rows = self._extractRows(stmt)
        if not rows:
            return _Result(rowcount=0)
        inserted = 0
        for values in rows:
            key = (
                values.get("entity_type"),
                int(values.get("enterprise_key", 0)),
                values.get("source_system"),
            )
            if key in self.existing:
                continue
            self.existing.add(key)
            inserted += 1
        return _Result(rowcount=inserted)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _FakeAdapter:
    """Fake THBI adapter：返回预置 supplier_code / material_code。"""

    def __init__(self, suppliers: list[str], materials: list[str]) -> None:
        self._suppliers = suppliers
        self._materials = materials

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        up = sql.upper()
        if "DWD_SUPPLIER" in up:
            return [{"SUPPLIER_CODE": c} for c in self._suppliers]
        if "DWD_MATERIAL" in up:
            return [{"MATERIAL_CODE": c} for c in self._materials]
        raise AssertionError(f"unexpected SQL: {sql}")


# -----------------------------------------------------------------------------
# _stableKey
# -----------------------------------------------------------------------------


class TestStableKey:
    def test_returns_value_in_offset_range(self) -> None:
        key = _stableKey("SUP-001", offset=800_000)
        assert 800_000 <= key < 800_000 + (1 << 32)

    def test_is_deterministic_across_calls(self) -> None:
        a = _stableKey("ACME-001", offset=800_000)
        b = _stableKey("ACME-001", offset=800_000)
        assert a == b

    def test_different_codes_yield_different_keys(self) -> None:
        # 4G 空间 + SHA-256：1000 个输入 0 碰撞概率
        keys = {_stableKey(f"SUP-{i:06d}", offset=800_000) for i in range(1, 1001)}
        assert len(keys) == 1000

    def test_material_offset_independent_from_supplier(self) -> None:
        # 同一字符串在 SUPPLIER vs MATERIAL offset 下必得不同 key
        s = _stableKey("X", offset=_SUPPLIER_KEY_OFFSET)
        m = _stableKey("X", offset=_MATERIAL_KEY_OFFSET)
        assert s != m


# -----------------------------------------------------------------------------
# _mapping / _buildAllRows
# -----------------------------------------------------------------------------


class TestBuildAllRows:
    def test_mapping_fields(self) -> None:
        row = _mapping(sync_mod.EntityType.SUPPLIER, "ACME-001", offset=800_000)
        assert row["entity_type"] == sync_mod.EntityType.SUPPLIER
        assert row["enterprise_code"] == "ACME-001"
        assert row["source_system"] == sync_mod.SourceSystem.ERP
        assert row["source_key"] == "ACME-001"
        assert row["source_code"] == "ACME-001"
        assert row["match_rule"] == sync_mod.MatchRule.MDM_MASTER
        assert row["expiry_date"] is None

    def test_builds_supplier_and_material_rows(self) -> None:
        rows = _buildAllRows(
            supplierCodes=["S1", "S2"],
            materialCodes=["M1"],
        )
        assert len(rows) == 3
        types = [r["entity_type"] for r in rows]
        assert types.count(sync_mod.EntityType.SUPPLIER) == 2
        assert types.count(sync_mod.EntityType.MATERIAL) == 1

    def test_supplier_and_material_keys_in_distinct_ranges(self) -> None:
        rows = _buildAllRows(
            supplierCodes=["S1", "S2"],
            materialCodes=["M1", "M2"],
        )
        supplier_keys = {r["enterprise_key"] for r in rows if r["entity_type"] == sync_mod.EntityType.SUPPLIER}
        material_keys = {r["enterprise_key"] for r in rows if r["entity_type"] == sync_mod.EntityType.MATERIAL}
        # SUPPLIER 区间 [800000, 800000 + 2^32)；MATERIAL 区间偏移 2^32
        supplier_top = 800_000 + (1 << 32)
        material_top = supplier_top + (1 << 32)
        for k in supplier_keys:
            assert 800_000 <= k < supplier_top
        for k in material_keys:
            assert supplier_top <= k < material_top


# -----------------------------------------------------------------------------
# syncEntityMappings — dry-run
# -----------------------------------------------------------------------------


class TestSyncDryRun:
    def test_dry_run_does_not_insert(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])

        stat = _run(
            syncEntityMappings(session, adapter, dryRun=True),
        )

        assert stat == {
            "suppliers": 2,
            "materials": 1,
            "planned": 3,
            "inserted": 0,
            "skipped": 0,
        }
        # dry-run 也应触发 SELECT，但不应有任何写入
        assert session.executed == []  # execute_read_only 走的 adapter，不是 session


# -----------------------------------------------------------------------------
# syncEntityMappings — 真写 + 幂等
# -----------------------------------------------------------------------------


class TestSyncIdempotent:
    def test_first_run_inserts_all(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])

        stat = _run(
            syncEntityMappings(session, adapter, dryRun=False),
        )

        assert stat["inserted"] == 3
        assert stat["skipped"] == 0
        assert stat["planned"] == 3

    def test_second_run_skips_existing(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])

        # 第一次写入：全 inserted
        _run(syncEntityMappings(session, adapter, dryRun=False))

        # 第二次：相同 supplier/material → 同一 enterprise_key → 全 skipped
        stat2 = _run(syncEntityMappings(session, adapter, dryRun=False))
        assert stat2["inserted"] == 0
        assert stat2["skipped"] == 3

    def test_new_supplier_after_first_run_inserts_only_new(self) -> None:
        session = _FakeSession()
        adapter1 = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])
        _run(syncEntityMappings(session, adapter1, dryRun=False))

        # 第二次：THBI 多了 S3，物料没变
        adapter2 = _FakeAdapter(suppliers=["S1", "S2", "S3"], materials=["M1"])
        stat = _run(syncEntityMappings(session, adapter2, dryRun=False))

        assert stat["inserted"] == 1  # 只新增 S3
        assert stat["skipped"] == 3


# -----------------------------------------------------------------------------
# _fetchSupplierCodes / _fetchMaterialCodes — 去重 + 空串过滤
# -----------------------------------------------------------------------------


class TestFetchDedup:
    def test_supplier_dedup_and_trim(self) -> None:
        adapter = _FakeAdapter(
            suppliers=["S1", "  ", "S1", "S2"],
            materials=[],
        )
        codes = _run(sync_mod._fetchSupplierCodes(adapter))
        assert codes == ["S1", "S2"]

    def test_material_dedup_and_trim(self) -> None:
        adapter = _FakeAdapter(
            suppliers=[],
            materials=["M1", "M2", "M1"],
        )
        codes = _run(sync_mod._fetchMaterialCodes(adapter))
        assert codes == ["M1", "M2"]
