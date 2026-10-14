"""enterprise_key 派生契约（app/domain/enterprise_key.py）。

为什么有这个文件：这段逻辑原先在 `scripts/sync_entity_mapping_from_thbi.py` 与
`app/services/entity_mapping_service.py` **各存一份**（复制粘贴），注释却写着
「同源（SSOT）」。2026-09-30 实测证伪 —— MATERIAL 350922 条在 2³² 空间里碰撞 12 次，
`ON CONFLICT ... DO UPDATE` 把 12 对编码静默折叠成 12 行（后者编码消失、name 被覆盖）。
故收敛为单一模块，并用「真实碰撞对」钉住键空间下限。
"""

from __future__ import annotations

from app.domain.enterprise_key import (
    ENTITY_TYPE_OFFSETS,
    GENERIC_KEY_OFFSET,
    KEY_RANGE_SIZE,
    MATERIAL_KEY_OFFSET,
    SUPPLIER_KEY_OFFSET,
    offsetFor,
    stableKey,
)

# 2026-09-30 探针实测：2³² 空间下 MATERIAL 350922 条里 12 个 key 各含两个编码。
# 这些是真实业务码（Oracle DIM_IMATERIAL.ITMREF_0），不是构造样本 ——
# 用它们做回归夹具，是为了让「扩空间」这个改动的收益可被断言，而不是靠注释声称。
_REAL_COLLIDING_PAIRS: list[tuple[str, str]] = [
    ("201014011003290000B", "AJC0004616"),
    ("3002803246XKU24A", "TH24034OP10803"),
    ("90101B0170220001", "TH23104OP00603"),
    ("TH16127146", "TH17024617"),
    ("TH16142589", "TH23115OP10PH09"),
    ("TH17119OP30406", "TH23044OP00LB02"),
    ("TH18041OP40200", "TH23010OP00PSP02"),
    ("TH18079895", "TH19100OP20882"),
    ("TH19116OP50823", "TH24103OP30806"),
    ("TH19117OP40B203", "TH24096OP40803"),
]


class TestStableKey:
    def test_is_deterministic(self):
        # 重跑同步必须派生出同一 key，否则 ON CONFLICT 失去幂等语义
        assert stableKey("ACME-001", offset=SUPPLIER_KEY_OFFSET) == stableKey(
            "ACME-001", offset=SUPPLIER_KEY_OFFSET
        )

    def test_falls_inside_offset_range(self):
        key = stableKey("SUP-001", offset=SUPPLIER_KEY_OFFSET)
        assert SUPPLIER_KEY_OFFSET <= key < SUPPLIER_KEY_OFFSET + KEY_RANGE_SIZE

    def test_distinct_codes_yield_distinct_keys_in_sample(self):
        keys = {stableKey(f"SUP-{i:06d}", offset=SUPPLIER_KEY_OFFSET) for i in range(2000)}
        assert len(keys) == 2000

    def test_same_code_differs_across_entity_types(self):
        supplier = stableKey("X", offset=SUPPLIER_KEY_OFFSET)
        material = stableKey("X", offset=MATERIAL_KEY_OFFSET)
        assert supplier != material


class TestKeySpace:
    """键空间下限 —— 这组断言是「别再缩回去」的闸门。"""

    def test_range_size_bound_keeps_real_scale_collision_below_threshold(self):
        """350922 条（2026-09-30 真实 MATERIAL 规模）在 2³² 下期望碰撞 14.3 个 key。

        用生日公式把「空间够不够」变成可断言的门槛：期望碰撞数 < 10⁻³。
        2³² → 14.3（实测兑现 12 个，12 行数据丢失）；2⁴⁸ → 2.2e-4。
        """
        n = 350_922
        p = n * n / (2 * KEY_RANGE_SIZE)
        assert p < 1e-3

    def test_real_colliding_pairs_no_longer_collide(self):
        """2³² 下实测碰撞的 10 对真实编码，在现键空间下必须两两不同。"""
        for left, right in _REAL_COLLIDING_PAIRS:
            assert stableKey(left, offset=MATERIAL_KEY_OFFSET) != stableKey(
                right, offset=MATERIAL_KEY_OFFSET
            ), f"{left} 与 {right} 仍然碰撞"

    def test_entity_type_ranges_are_disjoint_and_ascending(self):
        assert SUPPLIER_KEY_OFFSET + KEY_RANGE_SIZE == MATERIAL_KEY_OFFSET
        assert MATERIAL_KEY_OFFSET + KEY_RANGE_SIZE == GENERIC_KEY_OFFSET

    def test_offsets_avoid_synthetic_seed_range(self):
        """合成 seed 占 100001–500001（seed_entity_mapping），派生键不得落进去。"""
        assert SUPPLIER_KEY_OFFSET > 500_001

    def test_all_offsets_plus_range_fit_in_bigint(self):
        assert GENERIC_KEY_OFFSET + KEY_RANGE_SIZE < 2**63 - 1


class TestOffsetFor:
    def test_registered_types_map_to_their_offset(self):
        assert offsetFor("SUPPLIER") == SUPPLIER_KEY_OFFSET
        assert offsetFor("MATERIAL") == MATERIAL_KEY_OFFSET

    def test_unregistered_type_falls_back_to_generic_offset(self):
        # 故意设计：新增业务对象不必改本模块，seed_business_objects 注册即可
        assert offsetFor("CONTRACT") == GENERIC_KEY_OFFSET
        assert offsetFor("任意新类型") == GENERIC_KEY_OFFSET

    def test_registry_matches_public_constants(self):
        assert ENTITY_TYPE_OFFSETS == {
            "SUPPLIER": SUPPLIER_KEY_OFFSET,
            "MATERIAL": MATERIAL_KEY_OFFSET,
        }
