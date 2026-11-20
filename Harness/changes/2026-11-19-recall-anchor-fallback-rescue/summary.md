# 召回锚定兜底救援（Gap B）— 变更记录

> 设计：[design.md](./design.md)
> 承接：PR #157（数据源过滤 + 关键词锚定，已合 main `d58bb1ad`）
> 分支：`fix/recall-anchor-rescue`（从 `origin/main` `7dc9d874` 切出）—— **已 squash 合入 main = `f07ae867`**；
> 发版 runbook：`Harness/changes/2026-11-20-recall-fix-release/deploy-prod.md`

## 一句话

向量路径给不出可用类时（生产实证 `reason=no_match`），**关键词锚定接管**，窗口从 43 类宽窗收敛到锚定类；
锚定没命中（或证据不足、或候选集未按数据源收窄）才保留原全量回退（宁可宽不可答不出）。

## 变更内容

| 文件 | 变更 |
|---|---|
| `backend/app/services/recall_keyword_anchor.py` | 新增 `AnchoredMatch`（命中类 + 强度：精确等值 / 片段长度）与 `selectAnchoredMatches`；`selectAnchoredClasses` 改为其兼容层（**行为逐字不变**） |
| `backend/app/services/recall_datasource_filter.py` | 新增 `isRecallDatasourceScoped(披露)`（候选集是否已按数据源收窄） |
| `backend/app/services/chat_recall.py` | ①`_anchoredMatches` / `_anchoredClasses`（锚定候选构造下沉，正常路径行为不变）+ 证据闸门 `_isWeakAnchorEvidence`；②`_selectRelevantClasses(..., isDatasourceScoped=False)`；③新增 `_anchorRescueRecall(...)`（锚定救援窗口 + 两道闸门）；④`no_match` / `ods_only_hits` 两个降级分支改走救援入口 |
| `backend/app/services/chat_service.py` | ①候选集已收窄时传 `isDatasourceScoped=True`；②`mode="rescue"` 纳入「不可回答」专项提示（独立文案，不谎报「向量无命中」） |
| `backend/app/services/chat_multistep.py` | 步骤级召回同样传 `isDatasourceScoped`（原先丢弃了披露） |
| `backend/app/services/messages_zh.py` | 新增 `MSG_CHAT_UNANSWERABLE_ANCHOR_RESCUE` |
| `backend/app/domain/schemas.py` | `ClassRecallInfo` 文档补 `mode="rescue"` 取值与 hitCount 口径 |
| `frontend/src/types/chat.ts` | `mode` 联合类型加 `"rescue"` |
| `frontend/src/components/chat/MessageItem.tsx` | `mode==="rescue"` 出 warning 级降级提示（原先降级信号会整体消失） |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 新增 `messageItem.classRecallRescue` |
| `backend/app/tests/unit/test_recall_keyword_anchor.py` | 新增命中强度明细 4 例 |
| `backend/app/tests/unit/test_recall_anchor_wiring.py` | 新增/改造 `TestAnchorFallbackRescue`（闸门 1/2、维度词补拉、扩边失败、步骤级救援、ODS 分支日志）与 `TestMultistepAnchorRescue` |
| `backend/app/tests/integration/test_classify_and_recall.py` | 救援集成守卫重写（带数据源 + schema_cache + 跨源幽灵类）+ 未收窄不接管守卫 |
| `frontend/src/tests/MessageItem.test.tsx` | 新增 `mode="rescue"` 渲染用例 |
| `Harness/changes/2026-11-19-recall-anchor-fallback-rescue/design.md` | 本批 SSOT（设计/语义/闸门/披露/日志口径/风险） |
| `Harness/wiki/nl2sql-engine.md` | 「类召回窗口」章节补降级路径语义（锚定救援 + 两道闸门） |

## 关键语义

| 场景 | reason | 变更前 | 变更后 |
|---|---|---|---|
| 检索异常 | `search_error` | 全量回退 | **不变** |
| 检索 0 命中 | `no_hits` | 全量回退 | **不变** |
| 命中解析不出类 | `no_match` | 全量回退（43 类宽窗） | 锚定命中且过闸门 ⇒ 锚定窄窗口；否则全量回退 |
| 命中全是 ODS 业务表 | `ods_only_hits` | 全量回退 | 同上 |

判定依据：**锚定是「向量措辞敏感」的底，不是「向量基础设施故障」的底。**

### 两道闸门（任一不过 ⇒ 回落原全量回退）

1. **数据源口径**：候选集未按数据源收窄（`no_datasource` / `no_cache`）⇒ 不接管
   （锚定命中可能全是别源的类，窄窗一旦只含别源表就是必然的 ORA-00942）。
2. **证据强度**：唯一锚定命中且是 2 字弱匹配（非精确等值、片段 < 3 字，如「公司」「数量」）
   ⇒ 不接管（误锚定会独占整个窗口 ⇒ 比宽窗更糟）。

## 披露与日志

- `mode="rescue"`（**新增取值**；`"recall"` 在 schema 里是「纯向量召回」、`"fallback"` 的前端文案是
  「已按数仓分层顺序选取数据表」——两者都与锚定窄窗不符 ⇒ 谎报）。
- 前端：`mode==="rescue"` ⇒ warning 级「数据表智能召回已降级：本次按关键词锚定选取了 N 张数据表…」。
  （原设计写「前端零改动」是错的：既有 info 提示与跨源过滤提示三态互斥，生产场景
  `datasourceFiltered=210>0` 会优先命中跨源分支 ⇒ 锚定披露根本渲染不出来 ⇒ 降级信号整体消失。）
- `anchoredCount` = 锚定纳入数；`hitCount` = 候选类数（锚定 + 维度词 DIM 补拉，截断前口径）。
- 日志**恰好一行**含 `reason=`（铁律不破：回退率按行聚合，多打一行就翻倍）：

```
本体类回退降级 reason=no_match hits=20 total=94 kept=2 odsFiltered=51 truncated=False anchored=2
```

- 闸门跳过时走 `_fallbackRecall`（那一行 `reason=` 由它打），本路径只补一行 **info 级 `scene=`** 痕迹
  （`scene=` 不是 `reason=`，不污染回退率聚合）：
  `scene=anchor_rescue_unscoped` / `scene=anchor_rescue_weak`。
- `odsFiltered` 与回退路径同口径（`total - 非 ODS 候选数`）⇒ 两条降级行可直接对账。

## 验证

| 项 | 结果 |
|---|---|
| 锚定接线 + 锚定强度单测 | 34 passed |
| 召回集成（`test_classify_and_recall.py`） | 13 passed / 1 failed，失败项为**既有基线红**（`test_reclassify_with_prior_state`，意图分类，召回无关）⇒ 净新增 0 |
| `test_chat_service.py` | 见下方「回归」 |
| 前端 `MessageItem.test.tsx` | 见下方「回归」 |

## 已知取舍（不在本批修）

- **窄窗独占**：救援窗口 = 锚定类 + JOIN 1-hop 邻居 + 维度词 DIM 补拉，非锚定且非邻居的本地类
  不可达 ⇒ 可能漏掉答案所在表。用户已拍板「窄、准」语义；宽窗不是「更安全」只是「更多选择」
  （LLM 仍可能选错表）。彻底解法 = Gap A。
- `chat_recall.py` 体量超 800 行上限（**既有欠账**，本批前已 934 行，本批 +185 ⇒ 1119 行）⇒ 拆分列为技术债。
- **Gap A**（向量检索按本地类 id 收敛到 Milvus `expr`）：根因修复，**单独分支** `fix/recall-milvus-local-scope`。
- 既有挂账三项：few-shot 未按窗口约束、Milvus 墓碑向量写入未删 + 2.47× 重复向量、校验器「窗口未覆盖 vs 本体不存在」措辞。
