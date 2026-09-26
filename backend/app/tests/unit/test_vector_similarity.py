"""向量 L2 距离 → 相似度 单源函数测试（H6）。

覆盖：
- distanceToSimilarity 的口径与边界：负距离按 0（不得 > 1）、round 到 4 位、大距离趋近 0
- 四处调用点必须复用同一函数（源码断言，防公式再次分叉——本批修的就是分叉）

背景：该公式曾在 embedding / ontology / rag / wiki_vector 四处各写一遍且口径不同
（仅 rag 无 max(0)、仅 wiki 无 round），故源码断言是本条的**反复发闸门**而非冗余。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.services import vector_similarity
from app.services.vector_similarity import distanceToSimilarity

_SERVICES_DIR = Path(vector_similarity.__file__).resolve().parent

# 四个调用点（文件名常量：读源码做契约断言）
_CALL_SITES = (
    "embedding_service.py",
    "ontology_service.py",
    "rag_service.py",
    "wiki_vector_service.py",
)

# 内联公式特征串：调用点不得再出现
_INLINE_FORMULA = "1.0 / (1.0 +"


def _readSource(name: str) -> str:
    return (_SERVICES_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        (0, 1.0),  # 完全相同：int 入参也要能归一
        (0.0, 1.0),
        (1.0, 0.5),
        (-0.5, 1.0),  # 负距离（异常数据）按 0：修前 rag 路径会得 > 1
        (-1.0, 1.0),
        (3.0, 0.25),
        (Decimal("3"), 0.25),  # NUMERIC 列回读的是 Decimal
        (9.0, 0.1),
        (1000.0, 0.001),
    ],
)
def test_distance_to_similarity_maps_l2_distance_into_unit_interval(
    distance: float, expected: float
) -> None:
    assert distanceToSimilarity(distance) == expected


def test_distance_to_similarity_rounds_to_four_decimals() -> None:
    """wiki 语义检索此前不 round，同一条向量在不同页面显示不同精度。"""
    assert distanceToSimilarity(2.0) == 0.3333


def test_all_call_sites_reference_the_single_source_function() -> None:
    missing = [name for name in _CALL_SITES if "distanceToSimilarity" not in _readSource(name)]
    assert missing == [], f"这些调用点未复用单源函数: {missing}"


def test_no_call_site_inlines_the_similarity_formula() -> None:
    offenders = [name for name in _CALL_SITES if _INLINE_FORMULA in _readSource(name)]
    assert offenders == [], f"这些文件仍在内联相似度公式: {offenders}"


def test_embedding_service_no_longer_defines_a_private_duplicate() -> None:
    from app.services import embedding_service

    assert not hasattr(embedding_service, "_distanceToSimilarity")
