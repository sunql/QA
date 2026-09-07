"""Phase 4.4 Neo4j label 重命名脚本单元测试（mock Neo4j driver）。

覆盖：
- rename_neo4j_labels 返回每个 old label → 重命名数量的映射
- rename_neo4j_labels 对 Material → ItemMaster / GoodsReceipt → Receipt 执行正确 CQL
- NCR DETACH DELETE 计入 NCR_deleted
- 幂等：第二次运行结果一致（0 变更）

不要求 Neo4j 真实运行。
"""

from __future__ import annotations

import pytest

# =============================================================================
# Mock Neo4j driver（记录 CQL + 参数，供断言）
# =============================================================================


class _MockRecord:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]


class _MockResult:
    """模拟 Neo4j Result 对象（含 .single() 方法）。"""

    def __init__(self, records: list[_MockRecord]) -> None:
        self._records = records

    def single(self) -> _MockRecord:
        """返回单条记录（空列表则抛 StopAsyncIteration）。"""
        if not self._records:
            raise StopAsyncIteration
        return self._records[0]


class _MockSession:
    def __init__(self, call_count: dict[str, int]) -> None:
        self.queries: list[tuple[str, dict]] = []
        self._call_count = call_count

    def __enter__(self) -> "_MockSession":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def run(self, cql: str, **params: object) -> _MockResult:
        self.queries.append((cql, params))
        if "count(n) AS cnt" in cql:
            self._call_count[cql] = self._call_count.get(cql, 0) + 1
            is_first = self._call_count[cql] == 1
            # 首次调用返回节点数据，再次调用返回 0（模拟节点已被处理）
            if is_first and ":Material" in cql:
                return _MockResult([_MockRecord({"cnt": 3})])
            if is_first and ":GoodsReceipt" in cql:
                return _MockResult([_MockRecord({"cnt": 2})])
            if is_first and ":NCR)" in cql:
                return _MockResult([_MockRecord({"cnt": 1})])
            return _MockResult([_MockRecord({"cnt": 0})])
        return _MockResult([])


class _MockDriver:
    def __init__(self) -> None:
        self.sessions: list[_MockSession] = []
        # 跨 session 跟踪调用次数（首次返回数据，再次调用返回 0）
        self._call_count: dict[str, int] = {}

    def session(self) -> _MockSession:
        s = _MockSession(self._call_count)
        self.sessions.append(s)
        return s

    def close(self) -> None:
        pass


@pytest.fixture()
def mock_driver() -> _MockDriver:
    return _MockDriver()


# =============================================================================
# 导入待测模块（脚本本身，不是 neo4j_client）
# =============================================================================


class TestRenameNeo4jLabels:
    def test_returns_dict_with_renamed_counts(self, mock_driver) -> None:
        """返回 dict 包含每个 old label 的重命名数量 + NCR_deleted。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        stats = rename_neo4j_labels(mock_driver)

        assert isinstance(stats, dict)
        assert "Material" in stats
        assert "GoodsReceipt" in stats
        assert "NCR_deleted" in stats
        assert stats["Material"] == 3
        assert stats["GoodsReceipt"] == 2
        assert stats["NCR_deleted"] == 1

    def test_invokes_correct_cql_for_material_rename(self, mock_driver) -> None:
        """Material → ItemMaster 执行正确的 CQL。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        rename_neo4j_labels(mock_driver)

        cqls = [cql for cql, _ in mock_driver.sessions[0].queries]
        material_cql = next(c for c in cqls if ":Material" in c)
        assert "MATCH (n:Material)" in material_cql
        assert "SET n:ItemMaster" in material_cql
        assert "REMOVE n:Material" in material_cql
        assert "RETURN count(n) AS cnt" in material_cql

    def test_invokes_correct_cql_for_goods_receipt_rename(self, mock_driver) -> None:
        """GoodsReceipt → Receipt 执行正确的 CQL。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        rename_neo4j_labels(mock_driver)

        cqls = [cql for cql, _ in mock_driver.sessions[0].queries]
        gr_cql = next(c for c in cqls if ":GoodsReceipt" in c)
        assert "MATCH (n:GoodsReceipt)" in gr_cql
        assert "SET n:Receipt" in gr_cql
        assert "REMOVE n:GoodsReceipt" in gr_cql
        assert "RETURN count(n) AS cnt" in gr_cql

    def test_ncr_delete_invoked(self, mock_driver) -> None:
        """NCR DETACH DELETE 被调用且计入 NCR_deleted。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        stats = rename_neo4j_labels(mock_driver)

        cqls = [cql for cql, _ in mock_driver.sessions[0].queries]
        ncr_cql = next(c for c in cqls if ":NCR)" in c)
        assert "MATCH (n:NCR)" in ncr_cql
        assert "DETACH DELETE n" in ncr_cql
        assert "RETURN count(n) AS cnt" in ncr_cql
        assert stats["NCR_deleted"] == 1

    def test_idempotent_second_run_returns_zeros(self, mock_driver) -> None:
        """第二次运行返回全 0（幂等性验证）。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        # 第一次运行已有节点
        stats1 = rename_neo4j_labels(mock_driver)
        assert stats1["Material"] == 3
        assert stats1["GoodsReceipt"] == 2
        assert stats1["NCR_deleted"] == 1

        # 第二次运行：节点已不存在，同一 driver 再次调用返回 0
        stats2 = rename_neo4j_labels(mock_driver)
        assert stats2["Material"] == 0
        assert stats2["GoodsReceipt"] == 0
        assert stats2["NCR_deleted"] == 0

    def test_renames_dict_keys_are_old_labels(self, mock_driver) -> None:
        """返回 dict 的 key 是 old label，不是 new label。"""
        from scripts.rename_neo4j_labels import rename_neo4j_labels

        stats = rename_neo4j_labels(mock_driver)

        # key 必须是 old label
        assert "Material" in stats
        assert "GoodsReceipt" in stats
        # new label 不在 key 中
        assert "ItemMaster" not in stats
        assert "Receipt" not in stats
