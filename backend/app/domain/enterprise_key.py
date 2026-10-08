"""企业统一代理键（enterprise_key）的派生 —— **唯一 SSOT**。

## 为什么独立成模块

这段逻辑原先在 `scripts/sync_entity_mapping_from_thbi.py` 与
`app/services/entity_mapping_service.py` 中各存一份（复制粘贴），两处注释却都写着
「与脚本同源（SSOT）」。注释不是约束，复制粘贴是 —— 改一处必漏一处。

2026-09-30 实测证伪了原设计：
MATERIAL 350922 条在 2³² 空间里**碰撞 12 次**，`ON CONFLICT (entity_type,
enterprise_key, source_system) DO UPDATE` 把 12 对编码静默折叠成 12 行 ——
后者的编码消失、前者的 name 被后者覆盖，且 rowcount 仍报满额，无任何告警。
原注释写的「35w 输入 < 10⁻⁵」比实际乐观了约 6 个数量级（生日公式实为 ≈14.3）。

## 键空间

`stableKey` = SHA-256(code) 前 8 字节 → uint64 → mod KEY_RANGE_SIZE → 加 offset。
按 entity_type 分段，彼此不重叠，且全部避开合成 seed 占用的 100001–500001：

    SUPPLIER  [1_000_000,                1_000_000 + 2⁴⁸)
    MATERIAL  [1_000_000 + 2⁴⁸,          1_000_000 + 2 × 2⁴⁸)
    通用      [1_000_000 + 2 × 2⁴⁸,      1_000_000 + 3 × 2⁴⁸)   ← 未注册的类型

2⁴⁸ ≈ 2.8e14，350922 条在该空间里的碰撞期望为 2.2e-4（对比 2³² 的 14.3）。
上界 8.4e14 远在 BIGINT（9.2e18）之内，还有余量给后续 entity_type 分段。

新增业务对象**不需要改本模块**：未注册的类型走通用 offset，
只需在 seed_business_objects.py 注册业务对象即可。
"""

from __future__ import annotations

import hashlib

# 每个 entity_type 独占的键区间大小。
# 2^48 而不是 2^32：350922 条数据下前者碰撞期望 2.2e-4、后者 14.3（已实测丢 12 行）。
# 这个下限由 test_enterprise_key.py::TestKeySpace 用生日公式钉住。
KEY_RANGE_SIZE = 1 << 48  # 281_474_976_710_656

# 首个分段起点。必须 > 500_001（合成 seed 的键上界，见 seed_entity_mapping.py）。
_BASE_OFFSET = 1_000_000

SUPPLIER_KEY_OFFSET = _BASE_OFFSET
MATERIAL_KEY_OFFSET = SUPPLIER_KEY_OFFSET + KEY_RANGE_SIZE
# 未在 ENTITY_TYPE_OFFSETS 里注册的实体类型走这一段（预留 MATERIAL 之后的下一段）
GENERIC_KEY_OFFSET = MATERIAL_KEY_OFFSET + KEY_RANGE_SIZE

ENTITY_TYPE_OFFSETS: dict[str, int] = {
    "SUPPLIER": SUPPLIER_KEY_OFFSET,
    "MATERIAL": MATERIAL_KEY_OFFSET,
}


def offsetFor(entityType: str) -> int:
    """entity_type → 键区间起点；未注册的类型返回通用段起点。"""
    return ENTITY_TYPE_OFFSETS.get(entityType, GENERIC_KEY_OFFSET)


def stableKey(code: str, *, offset: int) -> int:
    """把任意业务编码映射到 [offset, offset + KEY_RANGE_SIZE) 的稳定正整数。

    同输入必同输出（进程间稳定），重跑同步才能靠 ON CONFLICT 保持幂等。
    取 SHA-256 前 8 字节而非 4 字节：空间越大碰撞越少（见模块 docstring），
    且截断长度变化不改变「同输入同输出」这一受约束的性质。
    """
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    head = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return offset + (head % KEY_RANGE_SIZE)
