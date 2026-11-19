# 召回锚定兜底救援（Gap B）设计

> 承接：[2026-11-19-recall-datasource-filter-anchor](../2026-11-19-recall-datasource-filter-anchor/design.md)（数据源过滤 + 关键词锚定，PR #157）
> 与 [2026-11-19-prod-incident-recall-window-miss](../2026-11-19-prod-incident-recall-window-miss/prod-incident-analysis.md)（2026-11-19 生产问法敏感漏选事故分析）

## 1. 缺陷（生产实证）

PR #157 上线后，生产日志实证：

```
WARNING app.services.recall_datasource_filter:召回候选跨源过滤 dropped=210 total=304 … datasourceId=1
WARNING app.services.chat_recall:本体类回退降级 reason=no_match hits=20 total=94 kept=43 odsFiltered=51 truncated=False
```

链路还原：

1. **跨源过滤生效**：304 个全局类筛到 94 个本数据源类（`dropped=210`）——数据源过滤是对的。
2. **向量命中全灭**：`hits=20` 全部落在跨源/墓碑类上，**0 个能解析到本地 94 类** ⇒ `validHitsCount=0`
   ⇒ `relevant` 为空 ⇒ 走 `no_match` 早退（`chat_recall.py:678-681`）。
3. **锚定从未执行**：锚定块在 `chat_recall.py:707-730`，位于 `no_match` 早退**之后** ⇒
   `mode="fallback"`、`anchoredCount=0`。**为「问法敏感漏选」设计的锚定机制，在最需要它的场景下一次都没跑过。**
4. **剪枝被彻底架空**：兜底把 94−51=43 个本地类全塞进 schema（`kept=43`）⇒ 召回裁剪形同虚设，
   LLM 在 43 张表里选表（生产上答案侥幸正确 —— 宽窗恰好含 DIM_SUPPLIER，不是锚定的功劳）。

⇒ **Gap B**：锚定必须能在「向量路径给不出可用类」时接管。

## 2. 语义决策（用户已拍板）

> 「向量命中为空时，锚定命中则用锚定窗口（窄、准）；锚定也没命中才保留原全量回退（宁可宽不可答不出）。」

| 场景 | `reason` | 向量检索 | 既有行为 | Gap B 后 |
|---|---|---|---|---|
| 检索异常 | `search_error` | 抛异常 | 全量回退 | **不变**（基础设施故障，向量侧不可信 ⇒ 宽窗更稳） |
| 检索 0 命中 | `no_hits` | 成功、0 条 | 全量回退 | **不变**（同上：0 命中更像索引/嵌入异常） |
| 命中解析不出类 | `no_match` | 成功、命中全跨源 | 全量回退 | **锚定命中 ⇒ 锚定窄窗口**；否则全量回退 |
| 命中全是 ODS 业务表 | `ods_only_hits` | 成功、命中全 ODS | 全量回退 | **锚定命中 ⇒ 锚定窄窗口**；否则全量回退 |
| 全库只有 ODS | `no_match_ods_filtered` | — | 全量回退 | **不变**（锚定候选本身已排除 ODS ⇒ 必然无锚定命中） |

判定依据：**锚定是「向量措辞敏感」的底，不是「向量基础设施故障」的底。** 检索成功执行但产不出可用类
⇒ 换个口径（字面匹配）再试一次是合理的；检索本身挂了 ⇒ 宽窗兜底更安全。

## 3. 变更设计

### 3.1 锚定计算下沉为可复用单元

把锚定计算从 `_selectRelevantClasses` 内联块抽成模块级纯函数：

```python
def _anchoredClasses(question: str, allClasses: list[Any], *, maxAnchored: int) -> list[Any]:
    """关键词锚定候选（字面包含匹配），新列表。

    ODS 闸门与向量命中路径同口径：显式点名 ODS 表时保留。
    """
```

正常命中路径改为调用它 ⇒ **行为逐字不变**（同一段候选构造 + 同一上限夹取）。

### 3.2 降级路径：锚定救援优先，两道闸门不通过才回退

新增 `_anchorRescueRecall(...)`，在 `no_match` / `ods_only_hits` 两个分支替代直接 `_fallbackRecall`：

1. 读 `max_classes` 与 `anchorMax = min(ANCHOR_MAX_CLASSES, max_classes)`（同正常路径口径）。
2. `_anchoredMatches(question, allClasses, maxAnchored=anchorMax)`（锚定命中 + 命中强度）。
3. **闸门 1（数据源口径）**：`isDatasourceScoped=False`（未选数据源 `no_datasource`
   或数据源目录缺失 `no_cache`）⇒ 回落原 `_fallbackRecall`。候选集没按数据源收窄时，
   锚定命中可能全是别源的类，窄窗一旦只含别源表就是必然的 ORA-00942（比宽窗更糟）。
4. **闸门 2（证据强度）**：锚定为空 ⇒ 回落原 `_fallbackRecall`（**行为完全不变**）；
   唯一命中且是 2 字弱匹配（非精确等值、片段 < 3 字，如「公司」「数量」）⇒ 同样回落
   （误锚定会独占整个窗口，比宽窗更糟 ⇒ 用户口径「宁可宽不可答不出」）。
5. **闸门通过** ⇒ 救援窗口 =
   锚定类（置顶，命中强度序）
   + 层优先排序的维度词 DIM 补拉（与正常路径同口径；`allowedClasses` = 已过滤候选集
   ⇒ DIM 补拉不越数据源）
   + 锚定类的 JOIN 目录 1-hop 扩边（扩边异常 ⇒ 退化为未扩边的锚定窗口，不让整问失败）
   ⇒ 截断到 `max_classes`，`_recall_score = 0.0`（无向量分）。

**闸门的数据源口径怎么来的**：三个调用点（`chat_recall.classifyAndRecall` /
`chat_service._buildPipelineContext` / `chat_multistep._recallForStep`）都已经调用
`applyDatasourceRecallFilter` 拿到披露 dict ⇒ 用
`recall_datasource_filter.isRecallDatasourceScoped(披露)` 判定（`datasourceFilterSkipped is None`
⇒ 已按数据源收窄），经 `_selectRelevantClasses(..., isDatasourceScoped=...)` 传进救援路径。
`_selectRelevantClasses` 的该参数**默认 False**（未接线 ⇒ 假定没收窄 ⇒ 保守不收窄）。

### 3.3 披露口径

| 字段 | 救援路径取值 | 含义 |
|---|---|---|
| `mode` | `"rescue"` | **新增取值**。`"recall"` 在 schema 里定义为「纯向量召回」，`"fallback"` 的前端文案是「已按数仓分层顺序选取数据表」——两者都与锚定窄窗不符 ⇒ 谎报。`"rescue"` 让前端能对「降级接管」单独告警 |
| `hitCount` | `len(candidates)` | 候选类数（锚定 + 维度词 DIM 补拉，截断前口径）⇒ 纯向量命中数 ≈ `hitCount - anchoredCount`（维度词补拉时该差值偏大，恒 ≥ 纯向量命中数） |
| `anchoredCount` | `len(anchored)` | 锚定纳入数量（前端披露） |
| `classCount` | `len(expanded)` | 实际注入 schema 的类数（含 JOIN 扩边） |
| `truncated` | 候选数超窗口 或 扩边截断 | 同既有口径（锚定数恒 ≤ `max_classes`，不构成截断源） |

### 3.4 日志口径（**唯一 `reason=` 出口**不破）

既有铁律：「每个降级事件恰好一行含 `reason=`」——回退率按行聚合，多打一行就翻倍。

⇒ 救援成功时**不调用** `_fallbackRecall`，而是**自己打那一行 `reason=`**：

```
本体类回退降级 reason=no_match hits=20 total=94 kept=2 odsFiltered=51 truncated=False anchored=2
```

- `reason=` 值沿用**原降级场景**（`no_match` / `ods_only_hits`）⇒ 回退率分母不变、场景归因不变。
- 新增尾字段 `anchored=N` ⇒ 运维可区分「锚定救援」与「普通全量回退」（普通回退行没有该字段）。
- `reason=` 行**恰好一行**（救援路径不再打 `_fallbackRecall` 的那行）✔ 铁律不破。
- `odsFiltered` 与回退路径**同口径**（`total - 非 ODS 候选数`）⇒ 两条降级行的
  `kept=` / `odsFiltered=` 可直接对账。
- 闸门跳过（未收窄 / 证据不足）时走 `_fallbackRecall`（那一行 `reason=` 由它打），
  本函数只补一行 **info 级 `scene=`** 痕迹（`scene=` 不是 `reason=`，不污染回退率聚合）：

```
INFO 本体类锚定救援跳过：候选集未按数据源收窄 scene=anchor_rescue_unscoped
INFO 本体类锚定救援跳过：锚定证据不足 scene=anchor_rescue_weak
```

### 3.5 前端（**本批有改动**）

- `mode` 新增 `"rescue"` 取值（`frontend/src/types/chat.ts`）。
- `MessageItem.tsx`：warning 级 Alert 的条件从 `truncated || mode==="fallback"` 扩到
  **含 `mode==="rescue"`**，文案用新增 i18n `messageItem.classRecallRescue`
  （zh-CN / en-US 各一条），文案里带锚定张数。

**为什么不能「前端零改动」**：既有 info 提示（`anchoredCount>0` ⇒「已按关键词锚定 N 张数据表。」）
与跨源过滤提示是**三态互斥**的同一个 Alert，生产主场景 `datasourceFiltered=210>0` 会**优先**
命中跨源分支 ⇒ 锚定披露根本渲染不出来 ⇒ 用户只看到「过滤了 210 张跨源表」，
看不到「本次窗口被降级收窄」⇒ 静默降级。改前 `mode="fallback"` 必出 warning，改后若不复现
warning ⇒ 降级信号**整体消失**。⇒ 必须新增 `mode` 取值 + warning 文案。

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 锚定误命中（问句常用词恰是某个类名的子串）⇒ 窗口过窄 ⇒ 答不出 | ① 只在**向量路径失效**时接管（正常路径不变）；② 闸门 2（唯一 2 字弱匹配 ⇒ 不接管）；③ 锚定上限 `ANCHOR_MAX_CLASSES`（默认 5）；④ 排序取**最长命中片段**优先（"供应商"(3 字) 压过 "公司"(2 字)）；⑤ JOIN 扩边补邻居；⑥ 维度词 DIM 补拉（与正常路径同口径） |
| **窄窗独占**：救援窗口只有锚定类 + 其 JOIN 邻居，非锚定且非邻居的本地类不可达 ⇒ 比宽窗确定更窄，可能漏掉答案所在表 | 用户已拍板「窄、准」语义（宁可宽不可答不出是**锚定没命中时**的兜底，不是锚定命中后的补位）；缓解 = JOIN 1-hop 扩边 + 维度词 DIM 补拉 + 闸门 2（弱锚定不接管）。**已知取舍**：宽窗里 LLM 仍可能选错表（宽窗不是「更安全」，只是「更多选择」）。彻底解法是 Gap A（向量命中本来就落在本地类上） |
| 锚定未命中 ⇒ 行为退化 | 完全走原 `_fallbackRecall`（既有断言逐字不变） |
| 与 2026-09-19 ODS_BPARTNER 事故闸门冲突 | 锚定候选沿用同一 ODS 闸门（显式点名 ODS 表时保留） |
| 跨源幽灵表被锚定捞回 | 锚定池 = 已过滤的本地类集（过滤发生在 `_selectRelevantClasses` 之前）⇒ **过滤生效时**物理上捞不回跨源类；**过滤未生效时**（未选数据源 / 目录缺失）由闸门 1 挡住（不收窄 ⇒ 保持原宽窗） |
| 步骤级召回（多步第二步）也被收窄 | 步骤召回与共享召回合并（`step_classes ∪ pc.classes`）⇒ 共享召回的宽窗仍在；步骤召回同样受闸门 1/2 约束（候选集已按数据源过滤） |
| `chat_recall.py` 体量超限（1119 行 > 800 行上限） | **既有欠账**（本批前已 934 行），本批 +185。拆分（`chat_recall.py` 按职责切模块）列为后续技术债，本批不动（生产缺陷修复优先，避免无关重构） |

## 5. 测试策略

- 单测（`backend/app/tests/unit/test_recall_anchor_wiring.py`，新增 `TestAnchorFallbackRescue`）：
  锚定命中⇒窄窗口 / 无锚定⇒保持全量回退 / `search_error` 与 `no_hits` 行为不变 /
  `ods_only_hits` 救援 / 上限夹取 / 日志恰好一行 `reason=`。
- 集成（`backend/app/tests/integration/test_classify_and_recall.py`）：
  向量命中全跨源（解析不出本地类）+ 问句含本地类别名 ⇒ 窗口收敛到锚定类且不含跨源类。

## 6. 与 Gap A 的关系

Gap A（向量检索按本地类 id 收敛到 Milvus `expr`）解决的是**根因**（跨源类吃掉 topK 窗口）。
Gap B 解决的是**兜底**（向量路径失效时的第二条腿）。两者互补：

- Gap A 生效 ⇒ `no_match` 大幅减少 ⇒ 救援路径很少触发。
- Gap A 未生效/失效（Milvus 侧条件不支持、墓碑向量未清、重复向量吃窗口）⇒ Gap B 兜住。

⇒ 排期：**先 Gap B（小改动、可独立验证）→ 再 Gap A（改向量检索契约）**。
