"""P3a D20：影子召回比对脚本的纯逻辑单测（**不连 Milvus**）。

两条不可让步的口径：
1. **过滤正确 + 差异可解释** —— v2 的过滤检索必须等于**精确真值**
   （先按范围筛、再按精确 L2 取 topK）；过滤掉的行必须能归因到
   「未归域 / 归了别的范围 / v2 里没有」，不能是一句「少了 N 条」；
2. **无未授权泄漏** —— 带范围过滤的召回里，**不得**出现范围外的行
   （也不得出现「未归域」的行 —— 未归域在范围过滤下必须 fail-closed）。

⚠️ import 路径必须是 ``scripts.diag_registry_v2_shadow``（包内导入）。
"""

from __future__ import annotations

from scripts import diag_registry_v2_shadow as shadow

# ontology_id → 域码（模拟 v2 行里带的治理维度）
_DIMS: dict[int, tuple[str, ...]] = {
    1: ("PROCUREMENT",),
    2: ("PROCUREMENT",),
    3: ("QUALITY",),
    4: (),
}


class Test泄漏检测:
    def test_范围内且已归域的行不算泄漏(self) -> None:
        leaks = shadow.detectLeakage([1, 2], ("PROCUREMENT",), _DIMS)

        assert leaks == []

    def test_范围外的行算泄漏(self) -> None:
        leaks = shadow.detectLeakage([1, 3], ("PROCUREMENT",), _DIMS)

        assert leaks == [3]

    def test_未归域的行在范围过滤下算泄漏(self) -> None:
        """未归域行必须被范围过滤排除；出现即为泄漏（fail-closed 被破坏）。"""
        leaks = shadow.detectLeakage([4], ("PROCUREMENT",), _DIMS)

        assert leaks == [4]

    def test_v2里不存在的行算泄漏(self) -> None:
        """召回里出现 v2 没有的 id ⇒ 数据不自洽，必须报出来。"""
        leaks = shadow.detectLeakage([999], ("PROCUREMENT",), _DIMS)

        assert leaks == [999]


class Test范围结果比对:
    """v2 过滤结果 vs **精确真值**（不是 vs v1 的 ANN 结果）。

    实测（2026-11-15）：v1 集合的 HNSW 近似检索会漏掉真近邻
    （同一段文本被写进多个 ``ontology_id`` ⇒ 向量有 698 组逐字节相同，
    图遍历陷在重复簇里），**v1 ANN 的误差比过滤本身的影响还大**。
    所以「差异可解释」的判据必须是：对同一查询，
    **v2 的过滤检索 == 精确 L2 真值（先按范围筛、再取 topK）**。

    `_DIMS` 的 id：1/2 ∈ PROCUREMENT，3 ∈ QUALITY，4 未归域。
    """

    _ALL_DISTANCES = {1: 0.0, 2: 0.2, 3: 0.5, 4: 0.9}

    def test_v2与真值完全一致时无差异(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0), (2, 0.2)],
            v1Hits=[(3, 0.5)],
            v2Hits=[(1, 0.0), (2, 0.2)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["v2Missing"] == []
        assert result["v2Extra"] == []
        assert result["leaks"] == []

    def test_v2少召回真值里的行被记为漏召回(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0), (2, 0.2)],
            v1Hits=[(1, 0.0), (2, 0.2)],
            v2Hits=[(1, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["v2Missing"] == [2]

    def test_v2多召回真值外的行被记为多召回(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0)],
            v1Hits=[(1, 0.0)],
            v2Hits=[(1, 0.0), (2, 0.2)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["v2Extra"] == [2]

    def test_并列距离不算多召回(self) -> None:
        """并列（距离相等）只是排序差异，不是「多召回」。"""
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0)],
            v1Hits=[(1, 0.0)],
            v2Hits=[(1, 0.0), (2, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances={1: 0.0, 2: 0.0, 3: 0.5, 4: 0.9},
        )

        assert result["v2Extra"] == []

    def test_未归域的基线行归因到unmapped(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0)],
            v1Hits=[(1, 0.0), (4, 0.9)],
            v2Hits=[(1, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["attribution"]["unmapped"] == 1

    def test_归他域的基线行归因到otherScope(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0)],
            v1Hits=[(1, 0.0), (3, 0.5)],
            v2Hits=[(1, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["attribution"]["otherScope"] == 1

    def test_基线超窗的行归因到baselineOver(self) -> None:
        """范围内、但不在真值 topK 里 ⇒ 基线 ANN 超窗（可解释）。"""
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0)],
            v1Hits=[(1, 0.0), (2, 0.2)],
            v2Hits=[(1, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["attribution"]["baselineOver"] == 1

    def test_范围内却没进v2结果的行归因到v2漏召回(self) -> None:
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0), (2, 0.2)],
            v1Hits=[(1, 0.0), (2, 0.2)],
            v2Hits=[(1, 0.0)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["attribution"]["v2MissingInScope"] == 1

    def test_基线漏掉的真值行归因到baselineMissed(self) -> None:
        """真值里有、v1 ANN 里没有 ⇒ 基线近似误差（可解释）。"""
        result = shadow.compareScopeResults(
            truthTopK=[(1, 0.0), (2, 0.2)],
            v1Hits=[(1, 0.0)],
            v2Hits=[(1, 0.0), (2, 0.2)],
            scopeValues=("PROCUREMENT",),
            dims=_DIMS,
            allDistances=self._ALL_DISTANCES,
        )

        assert result["baselineMissed"] == [2]


class Test范围构造:
    """影子脚本的过滤表达式**必须走生产构造器**。

    若脚本自己写一份表达式 ⇒ 转义规则 / 哨兵语义会与生产分叉 ⇒
    影子比对「验证的」就不是线上真正跑的那条路径 ⇒ 证据失效。
    这里用「含引号的字面量是否被转义」做探针：生产构造器会转义，
    脚本自建的那份**不会**。
    """

    def test_域范围走生产构造器(self) -> None:
        expr = shadow._scopeExprFor(shadow.DIMENSION_DOMAIN, ("PROCUREMENT",))

        assert expr == 'array_contains(domain_codes, "PROCUREMENT")'

    def test_组织范围走生产构造器(self) -> None:
        expr = shadow._scopeExprFor(shadow.DIMENSION_ORG, ("ORG_A",))

        assert expr == 'array_contains(org_codes, "ORG_A")'

    def test_实体范围走生产构造器(self) -> None:
        expr = shadow._scopeExprFor(shadow.DIMENSION_ENTITY, ("SUPPLIER",))

        assert expr == 'entity_type == "SUPPLIER"'

    def test_多值范围用or连接(self) -> None:
        expr = shadow._scopeExprFor(
            shadow.DIMENSION_DOMAIN, ("PROCUREMENT", "QUALITY")
        )

        assert expr == (
            'array_contains(domain_codes, "PROCUREMENT") or '
            'array_contains(domain_codes, "QUALITY")'
        )

    def test_含引号的域码按生产规则转义(self) -> None:
        """生产构造器会转义字面量；脚本自建的那份不会 ⇒ 这条是「是否委托」的探针。"""
        expr = shadow._scopeExprFor(shadow.DIMENSION_DOMAIN, ('A"B',))

        assert expr == 'array_contains(domain_codes, "A\\"B")'

    def test_空范围不构造表达式(self) -> None:
        assert shadow._scopeExprFor(shadow.DIMENSION_DOMAIN, ()) is None
