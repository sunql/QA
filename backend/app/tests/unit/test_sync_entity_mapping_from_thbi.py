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
from app.domain.enterprise_key import (
    KEY_RANGE_SIZE,
    MATERIAL_KEY_OFFSET,
    SUPPLIER_KEY_OFFSET,
    stableKey,
)
from scripts.sync_entity_mapping_from_thbi import (
    _buildAllRows,
    _mapping,
    syncEntityMappings,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """记录每次 execute 调用，模拟 ON CONFLICT DO UPDATE 行为（PG：insert 或 update 都计入 rowcount=1）。

    与 DO NOTHING 区别：所有行都「受影响」（即使 name 与原值相同 PG 也算 0；本 fake 简化
    为一律按 PG 默认「有变化」返 1）。
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
            batch = multi[0] if isinstance(multi, tuple) and multi else multi
            return [{col.name: v for col, v in row.items()} for row in batch]
        return []

    async def execute(self, stmt: Any) -> _Result:
        self.executed.append(stmt)
        rows = self._extractRows(stmt)
        # DO UPDATE：每行都受影响，rowcount == len(rows)
        for values in rows:
            self.existing.add(
                (
                    values.get("entity_type"),
                    int(values.get("enterprise_key", 0)),
                    values.get("source_system"),
                )
            )
        return _Result(rowcount=len(rows))

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _FakeAdapter:
    """Fake THBI adapter：返回预置 supplier_code / supplier_name / material descriptions。

    表名/列名对齐 THBI 真表（2026-09-30 实查）：`DIM_SUPPLIER(BPSNUM_0/BPSNAM_0)`、
    `DIM_IMATERIAL(ITMREF_0/ITMDES1_0..3)` —— 旧的 `DWD_SUPPLIER`/`DWD_MATERIAL`
    在库里已不存在（ORA-00942）。列键小写：适配器统一下沉（business_db_pool.py:639）。
    """

    def __init__(
        self,
        suppliers: list[tuple[str, str | None]] | list[str] | None = None,
        materials: list[tuple[str, str | None]] | list[str] | None = None,
    ) -> None:
        # 兼容旧用法：传 [str, ...] 时自动补 None name
        self.sqls: list[str] = []
        self._suppliers: list[tuple[str, str | None]] = (
            [(c, None) for c in suppliers]
            if suppliers and isinstance(suppliers[0], str)
            else (suppliers or [])
        )
        self._materials: list[tuple[str, str | None]] = (
            [(c, None) for c in materials]
            if materials and isinstance(materials[0], str)
            else (materials or [])
        )

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        up = sql.upper()
        if "DIM_SUPPLIER" in up:
            # 键取 SQL 的**别名**（`BPSNUM_0 AS supplier_code`）并下沉小写 ——
            # 与适配器行为一致：它把 Oracle 返回的列名统一 lower()。
            return [
                {"supplier_code": c, "supplier_name": n}
                for c, n in self._suppliers
            ]
        if "DIM_IMATERIAL" in up:
            return [
                {
                    "material_code": c,
                    "description_1": n,
                    "description_2": None,
                    "description_3": None,
                }
                for c, n in self._materials
            ]
        raise AssertionError(f"unexpected SQL: {sql}")


# -----------------------------------------------------------------------------
# _stableKey
# -----------------------------------------------------------------------------


class TestStableKey:
    """脚本经 SSOT 模块派生键（本文件的 range 断言只钉「脚本确实在用该 offset」）。

    键空间本身的性质（确定性 / 碰撞下限 / 真实碰撞对回归）由
    `test_enterprise_key.py` 覆盖，此处不重复。
    """

    def test_returns_value_in_offset_range(self) -> None:
        key = stableKey("SUP-001", offset=SUPPLIER_KEY_OFFSET)
        assert SUPPLIER_KEY_OFFSET <= key < SUPPLIER_KEY_OFFSET + KEY_RANGE_SIZE

    def test_script_derived_rows_use_the_ssot_offsets(self) -> None:
        """脚本构造的行必须落在 SSOT 的区间里 —— 防止有人在本脚本里再写一份 offset。"""
        rows = _buildAllRows(suppliers=[("S1", None)], materials=[("M1", None)])
        supplier = next(r for r in rows if r["entity_type"] == "SUPPLIER")
        material = next(r for r in rows if r["entity_type"] == "MATERIAL")
        assert SUPPLIER_KEY_OFFSET <= supplier["enterprise_key"] < MATERIAL_KEY_OFFSET
        assert (
            MATERIAL_KEY_OFFSET
            <= material["enterprise_key"]
            < MATERIAL_KEY_OFFSET + KEY_RANGE_SIZE
        )


# -----------------------------------------------------------------------------
# _mapping / _buildAllRows
# -----------------------------------------------------------------------------


class TestBuildAllRows:
    def test_mapping_fields(self) -> None:
        row = _mapping("SUPPLIER", "ACME-001", offset=800_000, name="Acme Co.")
        assert row["entity_type"] == "SUPPLIER"
        assert row["enterprise_code"] == "ACME-001"
        assert row["source_system"] == sync_mod.SourceSystem.ERP
        assert row["source_key"] == "ACME-001"
        assert row["source_code"] == "ACME-001"
        assert row["match_rule"] == sync_mod.MatchRule.MDM_MASTER
        assert row["expiry_date"] is None
        assert row["name"] == "Acme Co."

    def test_mapping_name_normalized(self) -> None:
        # 空白被 strip；None/空串落库为 NULL（不写空字符串）
        assert _mapping("SUPPLIER", "X", offset=800_000, name="   ")["name"] is None
        assert _mapping("SUPPLIER", "X", offset=800_000, name=None)["name"] is None
        long = "a" * 300
        assert len(_mapping("SUPPLIER", "X", offset=800_000, name=long)["name"]) == 200

    def test_builds_supplier_and_material_rows(self) -> None:
        rows = _buildAllRows(
            suppliers=[("S1", None), ("S2", None)],
            materials=[("M1", None)],
        )
        assert len(rows) == 3
        types = [r["entity_type"] for r in rows]
        assert types.count("SUPPLIER") == 2
        assert types.count("MATERIAL") == 1

    def test_supplier_and_material_keys_in_distinct_ranges(self) -> None:
        rows = _buildAllRows(
            suppliers=[("S1", None), ("S2", None)],
            materials=[("M1", None), ("M2", None)],
        )
        supplier_keys = {r["enterprise_key"] for r in rows if r["entity_type"] == "SUPPLIER"}
        material_keys = {r["enterprise_key"] for r in rows if r["entity_type"] == "MATERIAL"}
        for k in supplier_keys:
            assert SUPPLIER_KEY_OFFSET <= k < MATERIAL_KEY_OFFSET
        for k in material_keys:
            assert MATERIAL_KEY_OFFSET <= k < MATERIAL_KEY_OFFSET + KEY_RANGE_SIZE


# -----------------------------------------------------------------------------
# syncEntityMappings — dry-run
# -----------------------------------------------------------------------------


class TestSyncDryRun:
    def test_dry_run_does_not_insert(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=[("S1", "S1 Inc"), ("S2", None)], materials=[("M1", "Material 1")])

        stat = _run(
            syncEntityMappings(session, adapter, dryRun=True),
        )

        assert stat == {
            "suppliers": 2,
            "materials": 1,
            "planned": 3,
            "affected": 0,
        }
        # dry-run 也应触发 SELECT，但不应有任何写入
        assert session.executed == []  # execute_read_only 走的 adapter，不是 session


# -----------------------------------------------------------------------------
# syncEntityMappings — 真写 + 幂等
# -----------------------------------------------------------------------------


class TestSyncIdempotent:
    def test_first_run_affects_all(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])

        stat = _run(
            syncEntityMappings(session, adapter, dryRun=False),
        )

        # DO UPDATE：每个 row 都计入 affected（无论新 insert 还是 conflict update）
        assert stat["affected"] == 3
        assert stat["planned"] == 3

    def test_second_run_also_affects_all(self) -> None:
        session = _FakeSession()
        adapter = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])

        _run(syncEntityMappings(session, adapter, dryRun=False))
        # 第二次：DO UPDATE 把所有行重新「过一遍」（PG 实际看 name 是否变化返 0/1），
        # fake 简化为一律 1。语义上：重跑会刷新 name（如 THBI 改名）
        stat2 = _run(syncEntityMappings(session, adapter, dryRun=False))
        assert stat2["affected"] == 3

    def test_new_supplier_after_first_run(self) -> None:
        session = _FakeSession()
        adapter1 = _FakeAdapter(suppliers=["S1", "S2"], materials=["M1"])
        _run(syncEntityMappings(session, adapter1, dryRun=False))

        adapter2 = _FakeAdapter(suppliers=["S1", "S2", "S3"], materials=["M1"])
        stat = _run(syncEntityMappings(session, adapter2, dryRun=False))

        assert stat["affected"] == 4  # S1/S2 更新 + M1 更新 + S3 插入


# -----------------------------------------------------------------------------
# _fetchSupplierCodes / _fetchMaterialCodes — 去重 + 空串过滤
# -----------------------------------------------------------------------------


class TestSourceTables:
    """源表契约：THBI 的 `DWD_SUPPLIER` / `DWD_MATERIAL` 已不存在，真表是 DIM_*。

    2026-09-30 实查：`THBI.DWD_SUPPLIER` → ORA-00942（表或视图不存在）；
    `THBI.DIM_SUPPLIER(BPSNUM_0, BPSNAM_0)` 3500 行、
    `THBI.DIM_IMATERIAL(ITMREF_0, ITMDES1_0..3)` 350922 行 —— 与 2026-09-16 那次
    同步落库的行数（3500 / 350925）对得上。

    这条用例钉住「脚本查哪张表、取哪些列」：源表再被改名时，失败会发生在测试里，
    而不是上线后 dry-run 显示 0 行（或抛 ORA-00942 才发现）。
    """

    def test_supplier_query_targets_dim_supplier(self) -> None:
        adapter = _FakeAdapter(suppliers=[("S1", "S1 Inc")], materials=[])

        _run(sync_mod._fetchSupplierCodes(adapter))

        sql = adapter.sqls[0].upper()
        assert "THBI.DIM_SUPPLIER" in sql
        assert "BPSNUM_0" in sql and "BPSNAM_0" in sql

    def test_material_query_targets_dim_imaterial(self) -> None:
        adapter = _FakeAdapter(suppliers=[], materials=[("M1", "Steel")])

        _run(sync_mod._fetchMaterialCodes(adapter))

        sql = adapter.sqls[0].upper()
        assert "THBI.DIM_IMATERIAL" in sql
        assert "ITMREF_0" in sql
        for desc in ("ITMDES1_0", "ITMDES2_0", "ITMDES3_0"):
            assert desc in sql


class TestFetchDedup:
    def test_supplier_dedup_and_trim(self) -> None:
        adapter = _FakeAdapter(
            suppliers=[("S1", "S1 Inc"), ("  ", None), ("S1", "dup"), ("S2", None)],
            materials=[],
        )
        out = _run(sync_mod._fetchSupplierCodes(adapter))
        assert out == [("S1", "S1 Inc"), ("S2", None)]

    def test_supplier_name_blank_normalized_to_none(self) -> None:
        adapter = _FakeAdapter(suppliers=[("S1", "   ")], materials=[])
        out = _run(sync_mod._fetchSupplierCodes(adapter))
        assert out == [("S1", None)]

    def test_material_dedup_concat_desc(self) -> None:
        # 物料 description_1/2/3 非空拼接；全空 → None
        class _MAdapter(_FakeAdapter):
            async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
                if "DIM_IMATERIAL" in sql.upper():
                    # 键取 SQL 别名（ITMREF_0 AS material_code 等）并下沉小写
                    return [
                        {"material_code": "M1", "description_1": "Steel", "description_2": "AISI 304", "description_3": None},
                        {"material_code": "M2", "description_1": "Copper", "description_2": None, "description_3": "wire"},
                        {"material_code": "M3", "description_1": None, "description_2": None, "description_3": None},
                        {"material_code": "M1", "description_1": "dup", "description_2": None, "description_3": None},
                    ]
                return await super().execute_read_only(sql)
        out = _run(sync_mod._fetchMaterialCodes(_MAdapter()))
        assert out == [
            ("M1", "Steel AISI 304"),
            ("M2", "Copper wire"),
            ("M3", None),
        ]
