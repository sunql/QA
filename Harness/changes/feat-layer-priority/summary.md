# feat-layer-priority — Schema 层优先（ADS > DWS > DWD > DIM > ODS_DICT）

**Date**: 2026-09-19
**Status**: 设计 SSOT，待执行
**Owner**: sunql
**Risk**: 中（影响所有 chat 调用，但局部变更）

---

## Context

chat_service 当前召回 50 个本体类时，按向量 score 排列，**不感知层**：
- ADS / DWS / DWD / DIM / ODS_BUSINESS 同台竞争
- LLM 容易选错事实表（如 ODS_BPARTNER vs DIM_SUPPLIER 选前者幻觉属性）
- 已有的 `_isOdsBusinessTable` 只兜底过滤 ODS，**没有 DWS/ADS 优先**机制

**用户需求**：
> AIChatService 默认不访问 ODS 层，优先 ADS > DWS > DWD > DIM；除非问题文本里出现 `ODS_*` 表名（显式要求），否则不返 ODS。
> DIM 表用于关联属性，问题含维度词时挂入候选。

**背景**：
- 现网 50 类本体中 ODS=18 / DWD=9 / DIM=3 / **DWS=0 / ADS=0**
- ADS/DWS 暂未建立——但代码骨架先到位，ETL 同步后立即生效
- `class_filter_max_classes=30`（system_config）
- 已有 `_isOdsBusinessTable`（ODS_BUSINESS 过滤）保留兼容

---

## 设计

### 1. 新增辅助函数（chat_service.py）

```python
_LAYER_RANK = {"ADS": 0, "DWS": 1, "DWD": 2, "DIM": 3, "ODS_DICT": 4, "ODS_BUSINESS": 5, "UNKNOWN": 6}

def _getClassLayer(cls: Any) -> str:
    """按 source_table 前缀识别层。"""
    src = (getattr(cls, "source_table", "") or "").upper()
    if src.startswith("ADS_"):  return "ADS"
    if src.startswith("DWS_"):  return "DWS"
    if src.startswith("DWD_"):  return "DWD"
    if src.startswith("DIM_"):  return "DIM"
    if src.startswith("ODS_DIM_"): return "ODS_DICT"
    if src.startswith("ODS_"): return "ODS_BUSINESS"
    return "UNKNOWN"


_ODS_TABLE_PATTERN = re.compile(r"\bODS_[A-Z][A-Z0-9_]*\b")

def _isExplicitOdsRequest(question: str) -> bool:
    """显式 ODS 请求：问题文本含 ODS_<UPPER_NAME> 表名。"""
    return bool(_ODS_TABLE_PATTERN.search((question or "").upper()))


_DIMENSION_HINTS = (
    "维度", "属性", "分类", "编码", "描述", "名称",
    "供应商编号", "物料描述", "物料编码", "供应商名称",
    "物料名称", "物料分类", "供应商分类",
)

def _isDimensionHint(question: str) -> bool:
    """问题含维度词 → 关联 DIM 表。"""
    return any(kw in (question or "") for kw in _DIMENSION_HINTS)
```

### 2. `_selectRelevantClasses` 改造

**当前结构**：召回 hits → Milvus 排序 → 兜底过滤 ODS → 扩边

**改造后**：

```python
async def _selectRelevantClasses(self, session, dto, *, relevantClasses=None):
    """默认按 ADS>DWS>DWD>DIM>ODS_DICT 优先，ODS_BUSINESS 默认排除。

    改造点：
    1. _getClassLayer 辅助层判定
    2. _isExplicitOdsRequest 控制 ODS_BUSINESS 是否参与候选
    3. _isDimensionHint 把所有 DIM 强制纳入
    4. 按 _LAYER_RANK 排序（层内按原 score 顺序）
    5. 扩边仍按现有规则（仅 ODS_BUSINESS 邻居跳过）
    """
    # 现有召回逻辑保留（searchByKeyword 返回 hits）
    # ... (lines 1019-1100 不变) ...

    # ----- 新增：显式 ODS 标记 -----
    explicit_ods = _isExplicitOdsRequest(dto.question)

    # ----- 新增：维度词触发 DIM 全量纳入 -----
    dimension_hint = _isDimensionHint(dto.question)

    # ----- 新增：层优先排序 -----
    layered_classes = self._rankByLayer(
        relevant,  # 当前召回后的类列表
        dimension_hint=dimension_hint,
        session=session,
    )

    # ----- 截取 top K -----
    max_classes = await self._loadClassFilterMaxClasses(session)
    selected = layered_classes[:max_classes]
    truncated = len(layered_classes) > max_classes

    # ----- 现有扩边 -----
    expanded, truncated2 = await self._expandByJoinNeighbors(...)
    truncated = truncated or truncated2

    return (expanded, ClassRecallInfo(...))
```

**新增 `_rankByLayer`**：

```python
def _rankByLayer(
    self,
    classes: list,
    *,
    dimension_hint: bool,
    session: AsyncSession,
) -> list:
    """按层优先排序；DIMS 拉满。"""
    # 1. 拉全量 DIM 类（如 dimension_hint=True）
    if dimension_hint:
        dim_classes = self._fetchAllDimClasses(session)  # SELECT id WHERE source_table LIKE 'DIM_%'
        classes = list({c.id: c for c in (classes + dim_classes)}.values())

    # 2. 排序：layer rank asc, 保留原顺序
    def _sortKey(cls):
        layer = _getClassLayer(cls)
        rank = _LAYER_RANK.get(layer, _LAYER_RANK["UNKNOWN"])
        return rank
    return sorted(classes, key=_sortKey)
```

### 3. NL2SQL prompt 改造

`_buildPlanUserPrompt` 在 schema 段后追加：

```python
_LAYER_PRIORITY_HINT = """
【Schema 选表优先级】
默认按以下顺序选择事实表：
1. ADS_ 应用视图（预聚合，最快）
2. DWS_ 汇总表
3. DWD_ 明细表
4. DIM_ 维度表（仅用于 JOIN 关联获取属性，不作主事实表）
ODS_ 业务原始表仅在问题显式要求访问 ODS_* 表时使用。
如存在 ADS / DWS 视图，应优先使用而非 DWD 明细。
"""

def _buildPlanUserPrompt(question, schema_str, ...):
    base = ...
    if _AGGREGATE_HINT_KEYWORDS not detected (避免与已有 hint 冲突):
        base += _LAYER_PRIORITY_HINT
    return base
```

### 4. 不动区域

- `_isOdsBusinessTable` 保留兼容，已有 110 单测不动
- `_expandByJoinNeighbors` 邻居过滤仍按 ODS 跳
- `class_filter_max_classes` 上限不改
- `searchByKeyword` Milvus 排序不动（仍按 score）

---

## 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/chat_service.py` | 新增 `_getClassLayer / _isExplicitOdsRequest / _isDimensionHint / _rankByLayer`；改造 `_selectRelevantClasses` |
| `backend/app/services/nl2sql_service.py` | `_buildPlanUserPrompt` 加 `_LAYER_PRIORITY_HINT` |
| `backend/app/tests/unit/test_chat_service.py` | 新 `TestClassLayerSelection`（5 例）+ 已有 ODS 测试 fixture 适配 |
| `backend/app/tests/unit/test_nl2sql_service.py` | 新 `TestSchemaLayerPriorityHint`（2 例）|
| `Harness/changes/feat-layer-priority/summary.md` | 本文件 |
| `memory/qa-system-layer-priority.md` | 新建 |
| `memory/MEMORY.md` | 加索引 |

---

## 测试设计

### TestClassLayerSelection

```python
class TestClassLayerSelection:
    def test_ads_layer_ranked_first(self):
        # mocks: ADS_X (id=80), DWS_Y (id=70), DWD_Z (id=60), DIM_W (id=50), ODS_BUSINESS_V (id=40)
        classes = [...]  # 5 个类
        ranked = _rankByLayer(classes, dimension_hint=False, session=mock)
        assert [_getClassLayer(c) for c in ranked] == ["ADS", "DWS", "DWD", "DIM", "ODS_BUSINESS"]

    def test_ods_excluded_by_default(self):
        # mocks: hit 含 ODS_BUSINESS; question="B019 供应商编号"
        classes = [...]  # 含 ODS_BUSINESS
        # _isExplicitOdsRequest("B019 供应商编号") = False
        # ranked 后不含 ODS_BUSINESS
        ...

    def test_ods_included_when_explicit_request(self):
        # question="ODS_BPARTNER 里有什么供应商"
        # _isExplicitOdsRequest = True → ODS_BUSINESS 保留
        ...

    def test_dimension_hint_includes_all_dim(self):
        # 召回中 DIM 类 score 低；question="物料描述是什么"
        # dimension_hint=True → DIM_IMATERIAL 被强制纳入
        ...

    def test_no_match_with_layer_priority(self):
        # 召回全为空（id=999）
        # 走 no_match 全量回退，_rankByLayer 仍然按层排序
        ...
```

### TestSchemaLayerPriorityHint

```python
class TestSchemaLayerPriorityHint:
    def test_layer_priority_hint_injected(self):
        question = "3 月供货量最多的三种物料"
        prompt = _buildPlanUserPrompt(question, schema)
        assert "Schema 选表优先级" in prompt
        assert "ADS_" in prompt
        assert "DWD_" in prompt

    def test_layer_priority_hint_absent_when_aggregate(self):
        # 与已有 _AGGREGATE_HINT_KEYWORDS 互斥时
        ...
```

---

## 验收

- [ ] 单测全绿（新 + 已有 110 例）
- [ ] 集成测端到端 schema 段包含优先级
- [ ] 真机 `B019 圣特供应商编号是多少` → DIM_SUPPLIER 在前
- [ ] 真机 `ODS_BPARTNER 里有什么供应商` → ODS_BPARTNER 在候选
- [ ] 真机 `物料描述` → DIM_IMATERIAL 在候选
- [ ] 真机 `3月供货量最多的物料` → 优先 DWD（当前无 ADS/DWS）