# 变更：Phase 5.5 知识缺口操作入口（GraphInsightsPanel 可操作性）

- **日期**：2026-09-14
- **作者**：AI 助手
- **状态**：done
- **触发**：用户反馈「知识缺口 tab 没有任何操作入口，下一步在系统层面需要怎么处理」

## 1. 背景

Phase 3 Graph Insights 扫描三类知识缺口（MISSING_DIMENSION / ISOLATED_PAGE /
SPARSE_COMMUNITY），但只展示不操作 —— 用户看到告警却没法在系统层面响应。

Phase 5.5 设计原则：**两步预览**（LLM 调一次 → 不写库 → 用户在 Modal 看一眼 →
确认才落库）。避免自动污染知识网络 —— 用户看不懂的 gap 不会因为系统自动
「修一下」反而变得更糟。

## 2. 后端新增

### 2.1 MISSING_DIMENSION → `/pages/{pageId}/classify/preview` + `PATCH /pages/{pageId}`

- **预览**：`POST /wiki/pages/{pageId}/classify/preview`
  - 调 `AutoClassifier().classify()`（机制 1 分类器），**不写库**
  - 返回 `WikiClassifyPreviewRead`：primary + confidence + alternatives + reason
  - 模型不可用 → 503；模型建议不在白名单 → primary=null，前端展示「无可建议」
- **确认**：复用现有 `PATCH /pages/{pageId}`（加 `autoClassification` 字段，见 §4）

### 2.2 ISOLATED_PAGE → `/pages/{pageId}/relations/suggest` + `POST /relations/discover`

- **预览**：`POST /wiki/pages/{pageId}/relations/suggest`
  - `RelationDiscovery.discoverForPage(dryRun=True)` — 跑引用检测 + LLM 实体抽取，
    **不 commit**，把「即将写入」的提案返给前端
  - 返回 `WikiRelationsSuggestRead`：candidates + total + classExtractionStatus + droppedGhosts
  - 前端 Modal 展示候选（含 target title + reason），用户确认后调 `/relations/discover`
- **确认**：复用现有 `POST /pages/{pageId}/relations/discover`（真正落库）

### 2.3 SPARSE_COMMUNITY → `/communities/{key}/topic-suggest` + `PATCH /communities/{key}`

- **迁移**：`0070_community_topic` — `knowledge_community` 加 `topic VARCHAR(200)` 列
  （ORM 同步加 `topic: Mapped[str | None]`）
- **服务**：`CommunityTopicSuggester`（机制 `TOPIC` 加入 `LEARNING_MECHANISMS` 白名单）
  - 输入：社区内 page 标题列表（最多 30 个，只用标题不用正文）
  - 输出：≤20 字主题字符串，前端 Modal 预览
- **预览**：`POST /wiki/graph/communities/{communityKey}/topic-suggest`
  - 返回 `WikiCommunityTopicSuggestRead`：topic + pageCount + pageTitles
  - 让用户判断「这是基于以下 N 个条目标题得出的主题」可信度
- **确认**：`PATCH /wiki/graph/communities/{communityKey}`
  - body `{"topic": "..."}` 写入；`{"topic": null}` 清空
  - **未传 topic 字段** → no-op（`model_fields_set` 判定，区分「不改」与「清空」）
- **回显**：`KnowledgeGapRead` / `KnowledgeCommunityRead` 加 `topic` 字段；
  insights scan 时回显社区已有 topic，前端在 gap 列表直接展示绿色 Tag

### 2.4 insight_service 修复：purgeStaleInsights

原实现是空操作（YAGNI 注释），导致已修复的 gap（如用户补了 dimension）的旧
insight 行永远留在表里，前端 list 接口继续把已消除的 gap 展示出来。

新实现：rescan 末尾调 `purgeStaleInsights`，按本次 scan 的存活 key 集合
（surprisingKeys / gapKeys / bridgeKeys）删除不再存在的行。

## 3. 前端

### 3.1 GraphInsightsPanel 重构

每条 gap 加操作按钮（按 kind 分支）：
- `MISSING_DIMENSION` → 「重分类」按钮
- `ISOLATED_PAGE` → 「查找关联」按钮
- `SPARSE_COMMUNITY` → 「建议主题」按钮

点击打开对应 Modal：
- **ClassifyModal**：primary 建议 + alternatives 单选按钮组 → 确认 PATCH
- **RelationsModal**：候选关系列表（relationType + targetTitle + confidence + reason）
  → 确认 POST /discover
- **TopicModal**：建议主题 + 标题清单（前 10 条）→ 确认 PATCH topic

### 3.2 API 客户端

- `api/wikiPages.ts`：+ `previewWikiPageClassify` / `suggestWikiRelations`
- `api/wikiGraph.ts`：+ `previewCommunityTopic` / `updateCommunityTopic`

### 3.3 i18n

中英双语新增 ~20 条文案（按钮、Modal 标题、成功/失败提示）。

## 4. 关键设计决策

### 4.1 PATCH `/pages/{id}` 加 `autoClassification` 字段

**问题**：前端通过 PATCH 写 dimension 时，如果不同步写 `auto_classification`，
scan 的 MISSING_DIMENSION 判定（`dimension IS NULL AND auto_classification IS NULL`）
会把已写 dimension 但 auto_classification 仍为 NULL 的页继续报为缺口。

**修法**：`WikiPageUpdate` 加 `auto_classification: _UnsetType | dict | None` 字段；
前端 Modal 确认时把 LLM 预览建议一并 PATCH 进去。

### 4.2 dryRun 模式

`RelationDiscovery.discoverForPage(dryRun=True)` 跳过 `_persistCandidates`（它会
commit），把过滤后的提案塞进 `rawProposals` 字段返回。预览会话随请求关闭自动
回滚，不需要手工 cleanup。

### 4.3 主题建议只用标题不用正文

`CommunityTopicSuggester` 输入是社区内 page 标题列表（最多 30 个）。原因：
- 主题取决于「讲什么」不取决于「怎么说」
- 正文拉进来会让 prompt 变长、token 成本变高，且对主题命名无增量信号

### 4.4 PATCH `/communities/{key}` 的 topic 语义

- body 含 `topic` 字段（字符串或 null）→ 写入 / 清空
- body 不含 `topic` 字段 → no-op

用 Pydantic v2 `model_fields_set` 判定「字段是否出现在请求体」，不能用
`model_dump(exclude_unset=True)`（`_UnsetType` 的序列化器有坑）。

## 5. 测试

### 5.1 后端集成测试（11 个用例，全部 PASS）

`app/tests/integration/test_wiki_gap_actions_api.py`：

- MISSING_DIMENSION：preview 不写库 / 404 / PATCH 写 dimension + autoClassification
- ISOLATED_PAGE：suggest 不写库 / 404 / suggest 后 discover 落库
- SPARSE_COMMUNITY：topic-suggest 不写库 / 404 / PATCH 写 topic / PATCH 未传 no-op /
  PATCH null 清空

回归：Phase 2/3 原有 10 个 graph 测试全过（无破坏）。

### 5.2 前端测试（3 个用例，全部 PASS）

`src/tests/GraphInsightsPanel.test.tsx`：

- MISSING_DIMENSION → 点重分类 → 预览 → 确认 → PATCH
- ISOLATED_PAGE → 点查找关联 → 预览 → 确认 → POST /discover
- SPARSE_COMMUNITY → 点建议主题 → 预览 → 确认 → PATCH topic

### 5.3 端到端验证

通过 curl 完整走通 MISSING_DIMENSION gap 消除链路：
1. 造一个 dimension=null 的 page
2. 重算 insights → gap 出现
3. `classify/preview` → 返回建议维度 POLICY
4. `PATCH` 写入 dimension + autoClassification
5. 重算 insights → gap 消失（purge 生效）

## 6. 改动文件清单

**后端**：
- `app/api/v1/wiki.py`：+ classify/preview / relations/suggest 端点
- `app/api/v1/wiki_graph.py`：+ topic-suggest / PATCH communities 端点
- `app/domain/wiki_schemas.py`：+ WikiClassifyPreviewRead / WikiRelationSuggestCandidate /
  WikiRelationsSuggestRead / WikiCommunityTopicSuggestRead / WikiCommunityUpdateRequest /
  WikiCommunityTopicUpdateRead；WikiPageUpdate 加 auto_classification；KnowledgeGapRead 加 topic
- `app/domain/wiki_models.py`：KnowledgeCommunity 加 topic 字段
- `app/domain/wiki_learning_models.py`：LEARNING_MECHANISMS 加 "TOPIC"
- `app/services/learning/relation_discovery.py`：discoverForPage 加 dryRun 参数 + rawProposals
- `app/services/learning/community_topic_suggester.py`：新建（CommunityTopicSuggester 服务）
- `app/services/learning/prompts/community_topic_v1.txt`：新建（主题建议 prompt）
- `app/services/learning/insight_service.py`：purgeStaleInsights 真正实现 + SPARSE_COMMUNITY
  gap 回显 topic
- `alembic/versions/0070_community_topic.py`：新建迁移

**前端**：
- `src/components/wiki/GraphInsightsPanel.tsx`：+ 3 个 Modal + 操作按钮
- `src/api/wikiPages.ts`：+ previewWikiPageClassify / suggestWikiRelations
- `src/api/wikiGraph.ts`：+ previewCommunityTopic / updateCommunityTopic
- `src/types/wikiPages.ts`：WikiPageUpdate 加 autoClassification
- `src/types/wikiGraph.ts`：KnowledgeGap 加 topic
- `src/i18n/zh-CN.ts` / `en-US.ts`：+ ~20 条文案
- `src/tests/GraphInsightsPanel.test.tsx`：新建 3 个 Modal 测试
