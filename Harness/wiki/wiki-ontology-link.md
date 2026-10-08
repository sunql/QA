---
created: 2026-09-28
updated: 2026-09-28
sources:
  - docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md
  - docs/superpowers/plans/2026-09-28-wiki-ontology-link.md
  - backend/app/services/wiki_link_service.py
  - backend/app/services/wiki_injector.py
  - backend/app/services/wiki_chunk_loader.py
  - backend/app/api/v1/admin_wiki_links.py
  - backend/alembic/versions/0091_wiki_ontology_link.py
  - frontend/src/pages/WikiLinksPage.tsx
tags:
  - wiki
  - ontology
  - nl2sql
  - injection
  - admin
  - knowledge-layer
---

# Wiki ↔ Ontology 链接 — 运营指南与架构

把 wiki 页面/段落与 ontology class/property/metric **显式关联**，让 NL2SQL 在生成 SQL
时看到 wiki 业务规则（例如「收货数量按入厂日期计」），避免 LLM 凭默认口径
猜解读。本特性是把 wiki 知识从「检索增强」升级为「业务规则通道」的最后一公里。

> **状态**：Phase 1-4 全完成。Phase 4.5（运营手动绑定）已具备 UI + API + 审计能力，
> 总闸 `WIKI_INJECTION_ENABLED` 默认 `true`（与 §9 spec 一致；可通过 `system_config` 关闭）。
>
> 对应 spec：`docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md`
>
> 对应 plan：`docs/superpowers/plans/2026-09-28-wiki-ontology-link.md`

---

## 0. 一句话结论

运营在 **Admin → Wiki 链接管理** 把 wiki 段落绑定到 ontology class/property/metric，
NL2SQL 引擎在每次生成 SQL 前**自动**把命中的 wiki chunk 注入 system prompt，
LLM 就能拿到「业务口径」而非仅靠 schema 推。失败走 graceful degradation
（与改前等价），prometheus + 结构化日志 + `nl2sql_wiki_trace` 审计三重兜底。

---

## 1. 是什么 / 不是什么

### 1.1 这是

- **业务规则的运营通道**：把 LLM 容易猜错的口径（数量计算口径、同步时点、退货
  流程等）写到 wiki，由运营显式绑定到 ontology 实体上，避免 LLM 凭默认解读。
- **段落级粒度**：一段 wiki 只绑定到一个具体 chunk_id，避免整页注入浪费预算。
- **召回调用的副产品**：与本体召回共生——只有**已被召回**的 ontology 才触发
  wiki 注入，避免噪声膨胀。

### 1.2 这不是

- **不是 wiki 全文检索**。LLM 不知道 wiki 页存在什么，只知道「本次召回的
  ontology 上挂了什么 chunk」。
- **不是 Milvus 向量检索替代**。向量检索负责找 ontology；wiki 链接负责把
  ontology 翻译成业务规则。
- **不是 wiki_chat / wiki_qa_service 的同义**。后者是「整页 Wiki 问答」端点，
  本特性是「NL2SQL 注入通道」。

---

## 2. 数据模型

### 2.1 `wiki_ontology_link` 表（Alembic 0091）

| 列 | 类型 | 备注 |
|---|---|---|
| `id` | `BIGSERIAL` PK | |
| `page_id` | `VARCHAR(64)` FK → `wiki_page.page_id` ON DELETE CASCADE | |
| `chunk_id` | `VARCHAR(64)` NULL | **NULL = 页面级**；非 NULL = 段落级 |
| `ontology_type` | `VARCHAR(16)` | CHECK：`class` / `property` / `metric`（migration 0106 放开） |
| `ontology_id` | `BIGINT` | ontology_class.id / ontology_property.id / ontology_metric.id |
| `weight` | `NUMERIC(3,2)` | `0 ≤ weight ≤ 1`，默认 `1.00` |
| `note` | `VARCHAR(200)` NULL | 备注（运营可见） |
| `created_by` | `BIGINT` | FK → 用户 |
| `created_time` | `TIMESTAMPTZ` | 默认 `NOW()` |
| `revoked_time` | `TIMESTAMPTZ` NULL | **软撤销**：撤销 = 写入时间，保留审计 |

#### 关键索引

| 索引名 | 列 | 作用 |
|---|---|---|
| `ix_wol_ontology` | `(ontology_type, ontology_id)` WHERE `revoked_time IS NULL` | NL2SQL 注入端反查（按 ontology 找 page） |
| `ix_wol_page` | `(page_id)` WHERE `revoked_time IS NULL` | Admin 列表按页过滤 |
| `uq_wol_active` | `(page_id, COALESCE(chunk_id,''), ontology_type, ontology_id)` WHERE `revoked_time IS NULL` | **防重复**（页面级与段落级并存需 `chunk_id` 区分） |

### 2.2 `nl2sql_wiki_trace` 表（同迁移 0091）

| 列 | 类型 | 备注 |
|---|---|---|
| `id` | `BIGSERIAL` PK | |
| `session_id` | `VARCHAR(64)` | chat session |
| `question` | `TEXT` | 用户原始问题 |
| `ontology_type` | `VARCHAR(16)` | |
| `ontology_id` | `BIGINT` | |
| `page_id` | `VARCHAR(64)` | |
| `chunk_id` | `VARCHAR(64)` NULL | |
| `prompt_position` | `VARCHAR(32)` | 固定 `after_context` |
| `injected_chars` | `INT` | 实际注入字符数 |
| `score` | `NUMERIC(5,3)` | 该 chunk 的评分 |
| `created_at` | `TIMESTAMPTZ` | 默认 `NOW()` |

> **90 天保留**：定期清理（运维侧）；二期计划 pg_partman。

---

## 3. 运营操作（Admin → Wiki 链接管理）

入口：`/admin/wiki-links`，ACL 复用 `wiki_admin`。

### 3.1 流程

1. **左侧选 wiki 页**：搜索 / 折叠（前端 `WikiLinksPage` 树形面板）
3. **右侧详情**：当前页已绑 ontology 列表（class / property / metric 分 tab）
4. **添加绑定**：点「+ 添加绑定」 → 弹窗
   - **ontology 类型**：切 class / property / metric
   - **搜索 ontology**：按 name / alias（已绑的标 disabled 防重）
   - **scope**：单选「覆盖全页」 / 「仅限此段落（指定 chunk_id）」
   - **weight**：slider 0–1（默认 1.0）
   - **note**：可选 200 字内备注
5. **段落级**：选中页后展开 chunk 列表（标题 + 摘要），点击绑定到具体 chunk
6. **删除（×）**：软撤销 → 立即从下次召回剪枝中消失，但保留审计

### 3.2 何时选页面级 vs 段落级

| 场景 | 推荐 |
|---|---|
| wiki 页就是规则全文（例如《收货作业 SOP》全文几百字都在讲数量口径） | 页面级 |
| wiki 页是综合文档，仅某段讲「数量计算口径」 | 段落级（更精确） |
| wiki 页里有冲突口径（同一概念不同章节说法不同） | 段落级（运营显式挑哪段生效） |

---

## 4. 架构与服务

### 4.1 文件清单

```
backend/app/
├── services/
│   ├── wiki_link_service.py      # CRUD + getLinksByOntology + listLinkableTargets
│   ├── wiki_injector.py          # 纯函数：collectAndScore + renderPromptBlock + getBudget
│   └── wiki_chunk_loader.py      # 加载 chunk_text（PG + Milvus 双查兜底）
├── api/v1/
│   └── admin_wiki_links.py       # /api/v1/admin/wiki-links + /admin/wiki-linkables
└── domain/
    └── models.py                 # +WikiOntologyLink, +Nl2sqlWikiTrace

frontend/src/
├── pages/WikiLinksPage.tsx       # AdminWikiLinksPage（左树 + 右详情 + Modal）
├── api/adminWikiLinks.ts         # 5 API client 方法
├── types/wikiLink.ts             # WikiLink / WikiLinkableTarget / CreateWikiLinkRequest
└── i18n/{zh-CN,en-US}.ts         # menu.item.wikiLinks + wikiLinks.* 全套
```

### 4.2 三个核心服务

#### `WikiLinkService`（admin CRUD）

```python
class WikiLinkService:
    async def getLinksByOntology(
        ontology_pairs: list[tuple[str, int]],   # [(type, id), ...]
    ) -> list[LinkRow]:
        # WHERE (ontology_type, ontology_id) IN ((...))
        # 索引 ix_wol_ontology 命中；空列表 → 直接返 []

    async def getLinksByPage(page_id: str) -> list[LinkRow]: ...

    async def createLink(...) -> LinkRow:
        # 唯一约束违反 → 409 ConflictError
        # ontology_type 非法 → 422 ValidationError
        # page_id 不存在 → 422（FK 前置）

    async def revokeLink(link_id, actor) -> LinkRow: ...  # 软撤销
    async def updateLink(link_id, weight?, note?, actor) -> LinkRow: ...  # 仅改 weight/note

    async def listLinkableTargets(type, query?, limit=50) -> ...:
        # 给 admin 选择器用：按 name/alias 搜索，已绑的标 disabled
```

#### `WikiInjector`（纯函数，便于单测）

```python
class WikiInjector:
    @staticmethod
    def collectAndScore(
        recalledOntologies, linkRows, chunkTexts, budget,
    ) -> list[ScoredChunk]:
        # Step 1: 索引化 recalled
        # Step 2: 按 (page_id, chunk_id) 分组；剪枝未召回 ontology
        # Step 3: 每组 score = Σ(weight × recall_score)；加载文本
        # Step 4: 排序 + 预算截断（末位可截断不丢已有块）

    @staticmethod
    def renderPromptBlock(scoredChunks, charBudget, wikiPageIndex) -> str:
        # 返回完整 markdown 块（含 ### 业务规则补充 header + 适用 + 规则来源 footer）
        # 空 scoredChunks → 返回 ""

    @staticmethod
    async def getBudget(session) -> WikiBudget:
        # 读 system_config 三个 key + WIKI_INJECTION_ENABLED 总闸
        # 任何异常 → 走 _DEFAULTS 回退（不抛出）
```

#### `WikiChunkLoader`（PG + Milvus 双查）

```python
class WikiChunkLoader:
    async def loadChunks(
        session, page_ids, chunk_ids,
    ) -> dict[tuple[str, str], str]:
        # chunk_id 非 NULL → Milvus wiki_page_embeddings 按 (page_id, chunk_id) 取
        # chunk_id NULL → PG wiki_page.content（Markdown 全文）+ 截断到 4000 字符
        # 返回 {(page_id, chunk_id|""): text}；缺失键静默忽略
        # 整页注入过大风险由「运营优先选具体段落」规范约束
```

### 4.3 NL2SQL 接入点（`chat_service._collectWikiBlock`，由 `_planAndGenerateSql` 调用）

```python
# ★ 新增：wiki 召回注入（_collectWikiBlock）
# 收集顺序是「先看配了哪些类型的链接，再决定召回哪些类型」：某类型一条链接都没有时，
# 召回结果必然在 recallIndex 命中检查处被丢弃，白花一次 embedding + 一次 Milvus 检索。

# Step 1：class 分数来自 _selectRelevantClasses 附加的 _recall_score
scored_ontology = self._classRecallScores(classes)

# Step 2：property / metric 按需召回 —— 只有真的配了该类型链接才召回
configured_types = await service.listConfiguredOntologyTypes(session)   # SELECT DISTINCT ontology_type WHERE revoked_time IS NULL
extra_types = {t for t in _WIKI_EXTRA_RECALL_TYPES if t in configured_types}  # ("property", "metric")
scored_ontology.extend(await self._recallExtraOntologyScores(session, question, extra_types))

# Step 3：按 (type, id) 对查 wiki 链接 → 加载 chunk → 评分 → 渲染
pairs = [(o.type, o.id) for o in scored_ontology]
link_rows = await service.getLinksByOntology(session, pairs)
chunk_texts = await WikiChunkLoader().loadChunks(
    session, page_ids=[l.page_id for l in link_rows], chunk_ids=[l.chunk_id for l in link_rows],
)
budget = await WikiInjector.getBudget(session)  # 读 system_config
scored = WikiInjector.collectAndScore(scored_ontology, link_rows, chunk_texts, budget)
wiki_block = WikiInjector.renderPromptBlock(scored, budget.maxChars, page_index)

# 由 _planAndGenerateSql 把 wiki_block 作为 wikiRulesBlock 参数传给两阶段 prompt
```

**失败模式**：任一步异常 → `try/except` log warning + 退化为空块（与改前等价）；
property / metric 单类型召回失败只跳过该类型（warning + continue），不影响 class 与其他类型。
**总闸关闭**：`WIKI_INJECTION_ENABLED=false` → 整个步骤短路，注入 0 chunk。

---

## 5. 算法核心（`WikiInjector.collectAndScore`）

参考 spec §6.1。

### 5.1 评分公式

```
score(chunk) = Σ( weight × recall_score ) for lnk in chunk.links
```

- `weight` 来自 `wiki_ontology_link.weight`（运营可调）
- `recall_score` 来自本体召回阶段的 `ScoredOntology.recall_score`
- 同一 chunk 关联多个 ontology → 只占一个 token 席位，score 自然叠加

### 5.2 去重（3 类）

| 场景 | 行为 |
|---|---|
| 同一 chunk 关联多个 ontology | 合并到一个 `ScoredChunk`，`applied_to` 列出全部 |
| 整页链接（chunk_id NULL）与段落链接（chunk_id=X）共存 | 都进同一 key 空间，按 `(page, chunk)` 区分（`uq_wol_active` 用 `COALESCE(chunk_id,'')` 防重） |
| 未召回 ontology 的链接 | 剪枝（Step 2 跳过，注入量不被噪声撑大） |

### 5.3 预算截断

```
budget = WikiBudget(
    maxChars=2000,        # 字符上限
    maxChunks=5,          # 块数上限（再硬保）
    minRecallScore=0.0,   # 召回分阈值
)
```

- 排序：`scored.sort(score DESC)`
- 截断：累计字符 + 当前块 > `maxChars - HEADER_OVERHEAD(120)` → 截断末位文字（不丢已有块）
- 块数：累计块数 ≥ `maxChunks` → 截断
- 配置异常：`_DEFAULTS` dict 兜底（不抛出）

### 5.4 Prompt 渲染格式（`renderPromptBlock`）

```
### 业务规则补充（来自 Wiki · 取自 N 条规则 / 关联 M 个 ontology 实体）

1. [wiki:p024:c005] 收货数量按入厂日期计，与 PO 行收货时间取大值。
   适用：class=DIM_SUPPLIER · property=RECEIPT_QTY

2. [wiki:p007:c012] 供应商主数据每月初同步，T-1 日 02:00 入仓生效。
   适用：class=DIM_SUPPLIER

3. [wiki:p024:c006] 退货不冲减本月收货数量，单独走 RTV_FLOW。
   适用：property=RECEIPT_QTY

[规则来源] wiki:p024《收货作业 SOP》/ wiki:p007《供应商主数据管理》

### 重要
- 上述业务规则可能与 schema 默认口径冲突，请优先遵循 wiki 规则。
- wiki 规则不覆盖 schema 引用合法性（仍以 ontology_class_id / property_id 为准）。
```

空 `scoredChunks` → 整个 section 不渲染（与改前完全等价）。

---

## 6. 配置键（`system_config`）

| key | default | 说明 |
|---|---|---|
| `WIKI_INJECTION_ENABLED` | `true` | **总闸**。false → 整条链路短路，注入 0 chunk |
| `WIKI_INJECTION_MAX_CHARS` | `2000` | 单次 NL2SQL 注入字符上限 |
| `WIKI_INJECTION_MAX_CHUNKS` | `5` | 单次注入 chunk 数量上限（再硬保） |
| `WIKI_INJECTION_MIN_RECALL_SCORE` | `0.0` | 召回 score 低于此值的 ontology 不参与 wiki 链接 |

> **代码层兜底**（`backend/app/services/wiki_injector.py:62` `_DEFAULTS`）：
> 即使 `system_config` 行缺失 / 异常，`WikiInjector.getBudget` 走 `_DEFAULTS` dict 兜底，
> 不抛出。运维可在 Admin → system_config 编辑这 4 个 key（与既有魔数治理 0087/0088/0089 同口径）。
>
> 当前 `backend/app/config.py` **未声明** 这些字段（仅在 `_DEFAULTS` + `getBudget` 运行时读），
> 这是有意的：避免「声明值 ≠ 生效值」漂移（[chore-config-duplicate-fields](../changes/chore-config-duplicate-fields/summary.md) 同口径），
> SSOT 留在 `system_config` 表 + `_DEFAULTS`。

---

## 7. 可观测性

### 7.1 Prometheus 指标

| 指标 | 标签 | 告警 |
|---|---|---|
| `wiki_injector_inject_total` | `result=hit\|miss\|error` | `error > 0.5%` 持续 5min |
| `wiki_injector_injected_chars` | histogram | `p95 > 1800` 持续 1h |
| `wiki_injector_truncated_chunks_total` | counter | `> 20%` 注入被截断 |
| `nl2sql_token_usage` | 既有 | prompt 增量 `> +15%` 对比基线 |
| `nl2sql_success_rate` | 既有 | `-2%` 告警（regression 探针） |

### 7.2 结构化日志

```
[wiki_injector] session=xxx recalled_ontology_ids=[12,47] matched_links=8
                after_pruning=5 budget_chars=2000 injected_chars=1847
                truncated=true
```

### 7.3 审计（`nl2sql_wiki_trace`）

「某个 SQL 生成时引用了哪些 wiki chunk」可查询（90 天保留）。回溯问题场景：
运营误绑 → LLM 拿到错误规则 → SQL 生成有偏差 → 查 `nl2sql_wiki_trace` 锁定问题 chunk。

```sql
SELECT page_id, chunk_id, injected_chars, score, created_at
FROM nl2sql_wiki_trace
WHERE session_id = :session_id
ORDER BY created_at;
```

### 7.4 审计（`audit_log`）

admin 端 4 个写操作（CREATE / UPDATE weight+note / DELETE 软撤销）都进
`audit_log`，写入路径与 `wiki_admin` ACL 联动，参见 [audit-log-system.md](audit-log-system.md)。

---

## 8. 测试覆盖（80%+ 强制）

| 套件 | 用例数 | 覆盖 |
|---|---|---|
| `test_wiki_link_service.py` | 15 | CRUD + 软撤销 + 唯一约束 + 类型/权重校验 |
| `test_wiki_injector.py` | 10+ | 评分公式 / 多 ontology 去重 / 整页 vs 段落并存 / 未召回剪枝 / 末位截断 / max_chunks 上限 / 配置异常回退 / 渲染格式 / 空场景 / 字符预算 |
| `test_wiki_chunk_loader.py` | 6+ | PG + Milvus 双查 / chunk_id NULL 走 PG / 缺失键静默忽略 / 整页截断 |
| `test_wiki_link_admin_api.py` | 集成 | /api/v1/admin/wiki-links CRUD + ACL + DTO |
| `test_wiki_link_injector_e2e.py` | 6 | 端到端（chat → SQL）/ 总闸关 / stale ontology 跳过 / ACL 拦截 / trace 落库 / 并发 |

**关键断言**（spec §11.3）：

- 注入到 prompt 的 wiki 块字符数 ≤ `WIKI_INJECTION_MAX_CHARS`
- `nl2sql_wiki_trace` 行数 = 注入的 chunk 数（且 `prompt_position='after_context'`）
- mock LLM 时，接收的 system prompt 中**包含且仅包含**触发本次召回的 wiki chunks
- 撤销链接后下次 chat 调用注入量减少对应数量

---

## 9. Rollout 状态

| Phase | 内容 | 状态 |
|---|---|---|
| 1 | schema + 后台 service（不动 UI、不入流水线），`WIKI_INJECTION_ENABLED=true` 默认 | ✅ 完成 |
| 2 | Admin UI + 运营手动绑定（5–10 个高频 wiki 页） | ✅ 完成 |
| 3 | 总闸灰度（监控 24h NL2SQL 成功率 + token 增量） | ✅ 试点已过 |
| 4 | 审计保留 + 文档 + 收尾（**本文档**） | ✅ 完成 |

---

## 10. 故障排查

| 现象 | 排查路径 |
|---|---|
| **注入无效果** | 1) `SELECT value FROM system_config WHERE key='WIKI_INJECTION_ENABLED'` 应为 `'true'`；2) 后端日志查 `[wiki_injector]` 是否出现 + `matched_links` 是否非空；3) 检查 `wiki_ontology_link` 是否软撤销（`revoked_time IS NULL`）；4) 确认 ontology 是否被召回（`recallIndex` 不命中 → 剪枝） |
| **注入截断严重（chunks 总丢最后几条）** | 调大 `WIKI_INJECTION_MAX_CHARS`（默认 2000） / `WIKI_INJECTION_MAX_CHUNKS`（默认 5）；或运营降低个别 `weight`；注意 obs `wiki_injector_truncated_chunks_total > 20%` 告警 |
| **wiki 链接创建报 409** | 唯一约束冲突：`page_id + COALESCE(chunk_id,'') + ontology_type + ontology_id` 已存在（可能软撤销的复活不感知，需先 DELETE 撤销的再 CREATE） |
| **wiki 链接创建报 422** | `ontology_type` 非法（仅 class/property/metric）或 `weight` 越界（0–1）或 `page_id` 不存在（FK 前置） |
| **wiki 链接报 403** | 缺 `wiki_admin` ACL；前端检查当前用户角色 |
| **trace 表查不到某次会话** | `nl2sql_wiki_trace` 仅在成功注入时写；总闸关 / 召回为空 / 异常降级 → 不写行 |
| **prompt 找不到 wiki 规则但 wiki 已绑** | 1) 检查 `revoked_time IS NULL`；2) 检查 ontology 是否在本次召回（`recallIndex`）；3) 检查 `WIKI_INJECTION_MIN_RECALL_SCORE` 是否过高；5) 看 chunkLoader 是否取到文本（PG/Milvus 缺失） |
| **token 增量异常（>+15%）** | 调小 `WIKI_INJECTION_MAX_CHARS` 至 1500 或 1000；或运营合并多 wiki 页到单 chunk；查 obs 指标 `nl2sql_token_usage` 对比基线 |
| **Milvus chunk 取不到（段落级）** | 1) `SELECT chunk_id FROM wiki_page_embeddings WHERE page_id=...` 是否存在；2) 同步任务是否跑过；3) 兜底走 PG `wiki_page.content`（仅整页级才有效，段落级则缺失） |

---

## 11. 未来改进（已识别，挂账中）

1. **NL2SQL prompt 成本模型**（[qa-system-nl2sql-token-cost-model](../changes/qa-system-nl2sql-token-cost-model.md)）：
   nl2sql 占总 LLM 用量 94.9%，wiki 注入每步增加 `weight × recall_score` 排序后的若干 chunk，
   在多步场景下叠加 (N 步 × 注入量)。长期优化：按 plan 裁剪 schema（SQL 阶段省 43%）。
2. **Milvus round-trip 残留**（[qa-system-milvus-delete-visibility-flake](../changes/qa-system-milvus-delete-visibility-flake.md)）：
   `WikiChunkLoader` 段落级查询用 `(page_id, chunk_id)` 双键，命中 Milvus 偶发延迟可见性窗口；
   当前靠 `flush` 单次 8-25s 兜底，不阻塞主流程但偶发会让 trace 短时少写。Milvus 2.4.6 升级
   或自定义一致性级别后可消。
3. **chunk 段落 ID 稳定性**：当前 `chunk_id` 由 wiki 导入时按内容哈希生成；wiki 内容改了
   段落 ID 可能变（链接失效靠下次召回剪枝兜底）。未来：绑定时存 `chunk_hash`，迁移时自动改链。
4. **运营绑定推荐**：基于本体召回热度反向推荐「这个 ontology 该绑到哪几个 wiki 段落」，
   减少人工翻 wiki 工作量。
5. **批量绑定 API**：当前 `POST /wiki-links` 单条；高频运营场景需批量（CSV 上传）。

---

## 12. 相关文档与变更

### Wiki
- [nl2sql-engine.md](nl2sql-engine.md) — NL2SQL 引擎整体（含本体召回 + 两阶段 prompt）
- [chat-service-capabilities.md](chat-service-capabilities.md) — Chat 服务能力盘点（§3.6 wiki 注入章节）
- [chat-service-assessment.md](chat-service-assessment.md) — Chat 服务 SSOT 评估
- [data-model.md](data-model.md) — 元数据模型（`ontology_class` / `wiki_page` SSOT）
- [audit-log-system.md](audit-log-system.md) — 审计日志系统（admin 写入路径覆盖）

### Memory
- [qa-system-llm-metering-blindspots](../memory/qa-system-llm-metering-blindspots.md)
- [qa-system-llm-failure-tokens](../memory/qa-system-llm-failure-tokens.md)
- [qa-system-llm-prompt-cache-cost](../memory/qa-system-llm-prompt-cache-cost.md)
- [qa-system-sql-guard-side-channel](../memory/qa-system-sql-guard-side-channel.md)
- [qa-system-feature-rule-config](../memory/qa-system-feature-rule-config.md)
- [qa-system-wiki-knowledge-layer-plans](../memory/qa-system-wiki-knowledge-layer-plans.md)

### Spec / Plan
- [spec §6.1](../../docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md) — 算法
- [spec §10](../../docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md) — Observability
- [spec §12](../../docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md) — Rollout
- [spec §13](../../docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md) — Risks