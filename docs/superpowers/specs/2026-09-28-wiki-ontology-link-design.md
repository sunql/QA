# Wiki ↔ Ontology 链接：把 Wiki 业务规则注入到 NL2SQL 推理

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal**: 让 NL2SQL 在「已知 schema」之外，还能看到 wiki 里人工维护的业务口径（如「收货数量按入厂日期计」「退货不冲减本月收货」），避免 LLM 凭默认口径猜解读；链接数据由运营在 admin 页手工维护（页面级 + 段落级双层粒度）。

**Architecture**:

- **新表 `wiki_ontology_link`**：把「wiki 页面/段落 ↔ ontology class/property」显式建模为可审计、可运营的关系
- **新服务 `WikiLinkService` / `WikiInjector` / `WikiChunkLoader`**：分别负责 CRUD、召回注入、chunk 文本加载
- **接入点 `chat_service._planAndGenerateSql`**：在 ontology recall 之后、`buildSchemaText` 之前插入 wiki 召回，按 `Σ(weight × recall_score)` 评分去重，受 `WIKI_INJECTION_MAX_CHARS` 预算闸门
- **新 API `/api/v1/admin/wiki-links`**：增删查链接；前端 `AdminWikiLinksPage` 提供左侧 wiki 树 + 右侧绑定面板
- **可观测性**：`nl2sql_wiki_trace` 审计表 + 5 项 Prometheus 指标 + 1 条结构化日志

**Tech Stack**: FastAPI + SQLAlchemy 2.x async + Alembic + Pydantic + Ant Design v5 + TypeScript + React + vitest. 复用既有模式：`KpiCatalogService` 审计模式、`chat_recall._selectRelevantClasses` 召回模式、`system_config` 总闸模式（与 lever H7 embedding-provider-guard 同口径）。

---

## 1. Background

### 1.1 现状：wiki 与 ontology 是两条互不交叉的检索路径

| 路径 | 检索源 | LLM 看到的内容 | Milvus collection |
|---|---|---|---|
| Wiki Q&A (`wiki_qa_service.py`) | `WikiVectorService.searchSemantic` | 纯 wiki 文本 | `wiki_page_embeddings`（page_id / chunk_id / chunk_text / title / embedding，**无 ontology_id 字段**） |
| Chat / NL2SQL (`chat_service.py` → `chat_recall.py`) | `_selectRelevantClasses` + 关键词兜底 | 纯 ontology 结构 | `ontology_embeddings`（ontology_id / type / name / alias / description / embedding） |
| Agent Tool (`agent_tools_wiki.py`) | `wiki_search` / `wiki_read` | LLM 主动调工具 | 同 wiki |

Grep 验证：`wiki_*` 与 `ontology_*` 服务互不引用；`wiki_relation_service.py` 的关系是 wiki-page ↔ wiki-page 的学习反馈，不是 wiki-ontology 桥。

### 1.2 LLM 推理时丢失另一半信息

- Wiki Q&A：看到 wiki 文本，但不知道「供应商」对应哪个 ontology class
- NL2SQL：看到 ontology schema，但不知道业务侧对「收货数量」怎么定义、有没有特殊口径
- 两个路径都丢失了对方那一半信息

### 1.3 触发需求的用户场景

用户提问「上个月供应商准时交付率」时：
- 当前：LLM 拿到 ontology class `DIM_SUPPLIER` + metric `SUPPLIER_OTD`，按默认口径生成 SQL
- 期望：wiki《收货作业 SOP》明确写道「准时 = 入厂日期 - 承诺到货日期 ≤ 0 且退货率 < 2%」，LLM 应在 GROUP BY / WHERE 写入这两条业务口径

---

## 2. Requirements（验收标准）

### 2.1 链接数据 CRUD
- `POST /api/v1/admin/wiki-links` 创建一条页面级或段落级链接（含 ontology 类型、ID、weight、note）
- 重复创建（同一 page+chunk+type+target 未撤销）→ 409 Conflict
- `DELETE /api/v1/admin/wiki-links/{id}` 软撤销（写 `revoked_time`），恢复用「重新创建」
- 删除 wiki 页面 → ON DELETE CASCADE 自动清所有链接

### 2.2 NL2SQL 注入
- ontology 召回得到 N 个 (type, id, recall_score) → 查 wiki_ontology_link → 取 page_id/chunk_id 候选
- 评分公式 `Σ(weight × recall_score)`，按 score 降序、预算闸门 `WIKI_INJECTION_MAX_CHARS=2000`、`WIKI_INJECTION_MAX_CHUNKS=5` 截断
- 注入位置：`_buildTwoStagePrompt` 中 `CONTEXT_BLOCK` 之后、`FEW_SHOT` 之前
- 未召回 ontology 关联的链接 → 剪枝（不污染 prompt）
- 链接数据异常 / chunk 缺失 / 任何失败 → 返回空 list，NL2SQL 主流程零影响

### 2.3 Admin UI
- 左侧 wiki 页面树（按 dimension 分组，搜索/筛选）
- 右侧详情面板：当前页已绑 ontology（class / property tab）+ 段落级绑定列表
- 「+ 添加绑定」弹窗：ontology 选择器（带 name/alias 搜索 + 防重） + weight slider + note
- 「覆盖全页」 vs 「仅限此段落」单选决定 chunk_id 是否必填
- ACL：复用 `wiki_admin` 角色

### 2.4 可观测性
- `nl2sql_wiki_trace` 表记录每次注入的 `(session_id, ontology_id, page_id, chunk_id, prompt_position)`，保留 90 天
- 5 项 Prometheus 指标：inject_total / injected_chars_avg / truncated_chunks_total / token_usage 增量 / NL2SQL success_rate
- 1 条结构化日志：`[wiki_injector] session=... recalled_ontology_ids=[...] matched_links=... injected_chars=...`

### 2.5 测试覆盖
- 12 个单元测试（`test_wiki_link_service.py`）覆盖评分 / 去重 / 预算 / 渲染
- 6 个集成测试（`test_wiki_link_injector_e2e.py`）覆盖端到端 / 总闸 / ACL / 审计 / 并发
- 后端覆盖率 ≥ 80% 不退步

---

## 3. Technical Choices

### 3.1 决策表

| 决策点 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 落地场景 | (a) Wiki Q&A 注入 ontology 术语 (b) NL2SQL 注入 wiki 规则 (c) 双向 (d) Agent Tool | (b) NL2SQL | 用户首推；最高价值（避免错误 SQL） |
| 链接数据源 | (a) LLM 自动生成 (b) 查询时 LLM 抽取 (c) Embedding 跨空间 (d) 人工 admin 页 | (d) 人工 admin 页 | 用户首推；准确率可控、运营可控 |
| 链接粒度 | (a) 页面级 (b) chunk 级 (c) 双层（页面默认 + 段落补充） | (c) 双层 | 用户首推；运营负担适中 + 召回精度高 |
| 链接范围 | (a) 仅 class (b) class+property (c) +metric (d) +graph | (b) class+property | 用户首推；覆盖最常见 wiki 业务规则场景 |
| 召回时机 | (a) Ontology 后注入 (b) 并行召回 (c) Ontology 前导 (d) 仅 Answer | (a) Ontology 后注入 | 用户首推；链接表 JOIN 依赖 recalled_ids，剪枝干净 |
| 注入形态 | (a) 扁平注入 (b) 去重+评分+预算 (c) 按 ontology 分组 | (b) 去重+评分+预算 | 推荐项；token 控制强、可演进到 C |
| 评分公式 | (a) `weight × recall_score` 求和 (b) 取 max (c) 取平均 | (a) 求和 | 多 ontology 加权自然；chunk 越被召越靠前 |
| 截断策略 | (a) 末位丢弃 (b) 末位截断 (c) 截断到段落边界 | (b) 末位截断 | 不丢整块；保留 markdown header 元数据 |
| 总闸 | (a) 部署即上线 (b) system_config 总闸灰度 | (b) 总闸灰度 | 与 lever H7 同口径；可快速回滚 |
| 链接表 PK | (a) BIGSERIAL (b) VARCHAR (page_id+ontology_id+chunk_id) | (a) BIGSERIAL | 与现有 audit 表一致；软撤销友好 |

### 3.2 多视角

- **数据层**：FK + CHECK + 唯一索引（`WHERE revoked_time IS NULL`） = 数据完整性
- **服务层**：剪枝 + 预算 + graceful degradation = 健壮性
- **前端层**：双层粒度 UI + 弹窗选择器 + 防重 = 运营友好
- **可观测性**：trace 表 + Prometheus + 结构化日志 = 可调试可回滚

---

## 4. Data Model

### 4.1 新表 `wiki_ontology_link`（Alembic 0088）

```sql
CREATE TABLE wiki_ontology_link (
    id            BIGSERIAL PRIMARY KEY,
    page_id       VARCHAR(64) NOT NULL,
    chunk_id      VARCHAR(64) NULL,           -- NULL = 页面级；非 NULL = 段落级
    ontology_type VARCHAR(16) NOT NULL,       -- 'class' | 'property'
    ontology_id   BIGINT NOT NULL,
    weight        NUMERIC(3,2) NOT NULL DEFAULT 1.00,
    note          VARCHAR(200) NULL,
    created_by    BIGINT NOT NULL,
    created_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_time  TIMESTAMPTZ NULL,
    FOREIGN KEY (page_id) REFERENCES wiki_page(page_id) ON DELETE CASCADE,
    CONSTRAINT chk_link_type CHECK (ontology_type IN ('class','property')),
    CONSTRAINT chk_link_weight CHECK (weight >= 0 AND weight <= 1),
    CONSTRAINT chk_link_granularity CHECK (
        chunk_id IS NULL OR length(chunk_id) <= 64
    )
);

CREATE INDEX ix_wol_ontology ON wiki_ontology_link(ontology_type, ontology_id)
    WHERE revoked_time IS NULL;
CREATE INDEX ix_wol_page ON wiki_ontology_link(page_id)
    WHERE revoked_time IS NULL;
CREATE UNIQUE INDEX uq_wol_active ON wiki_ontology_link(page_id, chunk_id, ontology_type, ontology_id)
    WHERE revoked_time IS NULL;
```

### 4.2 新审计表 `nl2sql_wiki_trace`（Alembic 0088 同迁移）

```sql
CREATE TABLE nl2sql_wiki_trace (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(64) NOT NULL,
    question        TEXT NOT NULL,
    ontology_type   VARCHAR(16) NOT NULL,
    ontology_id     BIGINT NOT NULL,
    page_id         VARCHAR(64) NOT NULL,
    chunk_id        VARCHAR(64) NULL,
    prompt_position VARCHAR(32) NOT NULL,    -- 'after_context' 等
    injected_chars  INT NOT NULL,
    score           NUMERIC(5,3,1) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_nlwt_session ON nl2sql_wiki_trace(session_id, created_at);
-- 90 天保留：定时清理或 pg_partman（运维侧，二期）
```

---

## 5. Service Architecture

### 5.1 文件结构

```
backend/app/
├── services/
│   ├── wiki_link_service.py          # CRUD + 列表（admin 用）
│   ├── wiki_injector.py              # 纯函数：collectAndScore + renderPromptBlock
│   └── wiki_chunk_loader.py          # 加载 chunk_text（PG + Milvus 双查兜底）
├── api/v1/
│   └── admin_wiki_links.py           # admin API 路由
├── domain/
│   └── models.py                     # +WikiOntologyLink, +Nl2sqlWikiTrace ORM 类
└── alembic/versions/
    └── 0088_wiki_ontology_link.py    # 迁移

frontend/src/pages/admin/
└── WikiLinksPage.tsx                 # admin UI
```

### 5.2 服务接口

```python
# wiki_link_service.py
class WikiLinkService:
    async def getLinksByOntology(
        ontology_pairs: list[tuple[str, int]],   # [(type, id), ...]
    ) -> list[LinkRow]:
        # WHERE (ontology_type, ontology_id) IN ((...)) — PG 元组 IN 语法
        # 索引 ix_wol_ontology 命中；空列表 → 直接返 []

    async def getLinksByPage(page_id: str) -> list[LinkRow]: ...

    async def createLink(
        page_id: str, ontology_type: str, ontology_id: int,
        chunk_id: str | None, weight: Decimal, note: str | None,
        actor: CurrentUser,
    ) -> LinkRow:
        # 唯一约束违反 → 409 ConflictError
        # 类型非法 → 422 ValidationError
        # page_id 不存在 → 422 ValidationError（FK 前置）

    async def revokeLink(link_id: int, actor: CurrentUser) -> LinkRow: ...

    async def listLinkableTargets(
        type: str, query: str | None, limit: int = 50,
    ) -> list[TargetItem]: ...
        # 给 admin 弹窗选择器用：按 name/alias 搜索
        # 返回 [{id, name, alias, type}]，已绑的标 disabled


# wiki_injector.py
class WikiInjector:
    @staticmethod
    def collectAndScore(
        recalledOntologies: list[ScoredOntology],
        linkRows: list[LinkRow],
        chunkTexts: dict[tuple[str, str], str],
        budget: WikiBudget,
    ) -> list[ScoredChunk]: ...

    @staticmethod
    def renderPromptBlock(
        scoredChunks: list[ScoredChunk],
        charBudget: int,
        wikiPageIndex: dict[str, str],  # page_id -> title
    ) -> str:
        # 返回完整 markdown 块（含 ### 业务规则补充 header）
        # 空 scoredChunks → 返回 ""


# wiki_chunk_loader.py
class WikiChunkLoader:
    async def loadChunks(
        session: AsyncSession,
        page_ids: list[str], chunk_ids: list[str | None],
    ) -> dict[tuple[str, str], str]:
        # chunk_id 非 NULL → Milvus wiki_page_embeddings 按 (page_id, chunk_id) 取 chunk_text
        # chunk_id NULL → PG wiki_page.content（Markdown 全文）+ 截断到 _PAGE_CONTENT_MAX_CHARS=4000
        # 返回 {(page_id, chunk_id|""): text}，缺失键静默忽略
        # 整页注入过大风险由「页面级链接优先选具体段落」的运营规范约束 + 截断兜底
```

### 5.3 NL2SQL 接入点

修改 `chat_service._planAndGenerateSql`：

```python
async def _planAndGenerateSql(self, session, dto, pc):
    # 既有：ontology recall
    recalledOntologies = await self._recallOntology(pc.question, pc.datasourceId)

    # ★ 新增：wiki 召回注入
    wikiInjector = WikiInjector()
    linkRows = await WikiLinkService().getLinksByOntology(
        [(o.type, o.id) for o in recalledOntologies if o.type in ("class", "property")],
    )
    chunkLoader = WikiChunkLoader()
    chunkTexts = await chunkLoader.loadChunks(
        session,
        page_ids=[lnk.page_id for lnk in linkRows],
        chunk_ids=[lnk.chunk_id for lnk in linkRows],
    )
    budget = wikiInjector.getBudget()  # 读 system_config
    scored = wikiInjector.collectAndScore(recalledOntologies, linkRows, chunkTexts, budget)
    wikiPageIndex = await self._loadWikiPageTitles([s.page_id for s in scored])
    wikiRulesBlock = wikiInjector.renderPromptBlock(scored, budget.maxChars, wikiPageIndex)

    # 既有：拼两阶段 prompt
    messages = self._buildTwoStagePrompt(
        schema=schemaText,
        context=contextBlock,
        wikiRulesBlock=wikiRulesBlock,   # ← 新增参数
        fewShot=fewShot,
        question=pc.question,
    )
    # ... 既有 LLM 调用 + 计量
```

**失败模式**：上述 4 步（linkRows / chunkTexts / scored / wikiRulesBlock）中任何异常 → catch + log warning + 退化为空块（与改前等价）。

---

## 6. Algorithm

### 6.1 评分 + 去重 + 预算（`WikiInjector.collectAndScore`）

```python
def collectAndScore(recalledOntologies, linkRows, chunkTexts, budget):
    # Step 1: 索引化 recalled ontologies
    recallIndex = {(o.type, o.id): o.recall_score for o in recalledOntologies}

    # Step 2: 按 (page_id, chunk_id) 分组；剪枝未召回 ontology
    groups: dict[tuple[str, str], list[LinkRow]] = {}
    for lnk in linkRows:
        if lnk.ontology_type not in ("class", "property"):
            continue
        if (lnk.ontology_type, lnk.ontology_id) not in recallIndex:
            continue
        key = (lnk.page_id, lnk.chunk_id or "")
        groups.setdefault(key, []).append(lnk)

    # Step 3: 每组计算 score + 加载文本
    scored = []
    for (page_id, chunk_id), lnks in groups.items():
        score = sum(
            float(lnk.weight) * recallIndex[(lnk.ontology_type, lnk.ontology_id)]
            for lnk in lnks
        )
        text = chunkTexts.get((page_id, chunk_id)) or chunkTexts.get((page_id, "")) or ""
        if not text:
            continue
        scored.append(ScoredChunk(
            page_id=page_id, chunk_id=chunk_id or None,
            text=text, score=score,
            appliedTo=[(lnk.ontology_type, lnk.ontology_id) for lnk in lnks],
        ))

    # Step 4: 排序 + 预算截断
    scored.sort(key=lambda c: c.score, reverse=True)
    kept, usedChars = [], 0
    for c in scored:
        blockLen = _estimateBlockChars(c)
        if usedChars + blockLen > budget.maxChars:
            remain = budget.maxChars - usedChars - _META_OVERHEAD
            if remain > _MIN_TRUNCATE_REMAINS:  # 80
                kept.append(c.withTextTruncated(c.text[:remain] + "…"))
            break
        usedChars += blockLen
        kept.append(c)
        if len(kept) >= budget.maxChunks:
            break
    return kept
```

**不变量**：
- 同一 chunk 关联多个 ontology → 只占一个 token 席位，score 自然叠加
- 整页链接（`chunk_id IS NULL`）和段落链接共存 → 都进同一 key 空间合并
- 预算超限 → 截断最末一块文字，不丢已有块
- 任何失败 → 返回 `[]`（与改前等价）

### 6.2 Prompt 渲染格式

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

空 scoredChunks → 不渲染此 block（与改前完全等价）。

---

## 7. Admin API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/admin/wiki-links` | GET | 列表（filter: page_id / ontology_type / ontology_id / active） |
| `/api/v1/admin/wiki-links` | POST | 创建（body 见 §5.2） |
| `/api/v1/admin/wiki-links/{id}` | DELETE | 软撤销 |
| `/api/v1/admin/wiki-links/{id}` | PATCH | 编辑 weight / note（不改 page/chunk/ontology） |
| `/api/v1/admin/wiki-linkables` | GET | ontology 选择器数据（query: type + q） |

ACL：复用 `wiki_admin`；审计写入 `audit_log`。

---

## 8. Frontend

`AdminWikiLinksPage.tsx`：
- 左侧 wiki 树（按 dimension 分组，搜索/筛选）
- 右侧详情面板：当前页已绑 ontology（class / property tab）+ 段落级绑定列表
- 「+ 添加绑定」弹窗：type 切换 + 选择器（带搜索 + 防重） + weight slider + note + 全页/段落单选
- 段落预览：选中 page 后显示 chunk 列表（标题 + 摘要），点击绑定到具体 chunk
- ACL：复用 wiki_admin

复用既有模式：`AdminKpiCatalogPage`、`AdminEntityMappingPage`、`AdminToolsPage`。

---

## 9. Configuration

`system_config` 新增（与 lever H7 embedding-provider-guard 同口径）：

| key | default | 说明 |
|---|---|---|
| `WIKI_INJECTION_ENABLED` | `true` | 总闸 |
| `WIKI_INJECTION_MAX_CHARS` | `2000` | 单次 NL2SQL 调用注入字符上限 |
| `WIKI_INJECTION_MAX_CHUNKS` | `5` | 单次注入的 chunk 上限（再硬保） |
| `WIKI_INJECTION_MIN_RECALL_SCORE` | `0.0` | 召回 score 低于此值的 ontology 不参与 wiki 链接 |

读取函数：`WikiInjector.getBudget()` 走 `system_config`（与 `_getContextCharBudget` 同口径）。

---

## 10. Observability

### 10.1 Prometheus 指标

| 指标 | 标签 | 告警 |
|---|---|---|
| `wiki_injector_inject_total` | `result=hit\|miss\|error` | `error > 0.5%` 持续 5min |
| `wiki_injector_injected_chars` | histogram | `p95 > 1800` 持续 1h |
| `wiki_injector_truncated_chunks_total` | counter | `> 20%` 注入被截断 |
| `nl2sql_token_usage` | 既有 | prompt 增量 `> +15%` 对比基线 |
| `nl2sql_success_rate` | 既有 | `-2%` 告警（regression 探针） |

### 10.2 结构化日志

```
[wiki_injector] session=xxx recalled_ontology_ids=[12,47] matched_links=8
                after_pruning=5 budget_chars=2000 injected_chars=1847
                truncated=true
```

### 10.3 审计

`nl2sql_wiki_trace` 表保留 90 天，可查询「某个 SQL 生成时引用了哪些 wiki chunk」。

---

## 11. Testing

### 11.1 单元测试（`test_wiki_link_service.py`，12 用例）

| 用例 | 覆盖 |
|---|---|
| `test_collect_score_sums_weight_x_recall` | 评分公式 |
| `test_collect_dedup_same_chunk_multi_ontology` | 多 ontology 合并 |
| `test_collect_dedup_page_level_vs_chunk_level` | 整页 vs 段落并存 |
| `test_collect_filter_unrecalled_ontology` | 召回未命中剪枝 |
| `test_budget_max_chars_truncates_tail` | 末位截断 |
| `test_budget_max_chunks_caps_count` | max_chunks 上限 |
| `test_budget_negative_uses_default` | 配置异常回退 |
| `test_renderer_includes_meta_lines` | prompt 块结构 |
| `test_renderer_omits_block_when_empty` | 空场景 |
| `test_renderer_caps_chars` | 渲染预算 |
| `test_link_crud_revoke_soft_delete` | 软删除 |
| `test_link_unique_constraint` | 重复约束 |

### 11.2 集成测试（`test_wiki_link_injector_e2e.py`，6 用例）

走真实 PG + Milvus + 真实 ontology_class / wiki_page 数据：

| 用例 | 覆盖 |
|---|---|
| `test_full_pipeline_chat_to_sql_with_wiki_rule` | 端到端 |
| `test_wiki_link_disabled_returns_no_block` | 总闸关 |
| `test_wiki_link_stale_ontology_skipped` | ontology 删除后失效 |
| `test_wiki_acl_blocks_user_access` | ACL 拦截 |
| `test_wiki_trace_records_audit` | 审计落库 |
| `test_concurrent_link_create_no_deadlock` | 并发 |

### 11.3 关键断言

- 注入到 prompt 的 wiki 块字符数 ≤ `WIKI_INJECTION_MAX_CHARS`（实测 1900–2000）
- `nl2sql_wiki_trace` 行数 = 注入的 chunk 数（且 `prompt_position='after_context'`）
- mock LLM 时，接收的 system prompt 中**包含且仅包含**触发本次召回的 wiki chunks 文本
- 撤销链接后下次 chat 调用注入量减少对应数量

---

## 12. Rollout

### Phase 1：schema + 后台 service（不动 UI、不入流水线）
- Alembic 0088 创建表（默认空）
- 后台 service 类就位、单测全绿
- `WIKI_INJECTION_ENABLED=false`（system_config 默认）
- **验证**：migration + 单测；UI 无可见变化；NL2SQL 行为零差异

### Phase 2：admin UI + 运营手动绑定 5–10 个高频 wiki 页
- 前端 `AdminWikiLinksPage` 上线
- 运营手工绑定若干「供应商收货」「采购订单」「退货流程」页到 DIM_SUPPLIER / RECEIPT_QTY 等
- 仍 `WIKI_INJECTION_ENABLED=false`

### Phase 3：开总闸灰度
- system_config 改 `WIKI_INJECTION_ENABLED=true`（仅 prod 灰度）
- 监控 24h 关键指标（NL2SQL 成功率、token 增量、平均 SQL 生成时间）
- 异常立即回滚（`UPDATE system_config SET ... = false`）

### Phase 4：审计 + 文档 + 收尾
- `nl2sql_wiki_trace` 数据保留 90 天（定时清理）
- 写 wiki 文档 `Harness/wiki/wiki-ontology-link.md`（含运营指南）
- 更新 `Harness/agents/owner.md` 索引

---

## 13. Risks

| 风险 | 缓解 |
|---|---|
| 运营误绑 → LLM 拿到错误规则 | chunk 级绑定（更精细）+ `note` 字段 + audit trace + 可视化预览 |
| wiki 页删除 → 外键 ON DELETE CASCADE 自动清 | 测试覆盖 |
| ontology 编辑（改 alias/合并）→ 链接失效但表里还在 | 软撤销 `revoked_time`，下次召回剪枝（代码已加） |
| Prompt 注入导致 token 爆增 | `WIKI_INJECTION_MAX_CHARS` 总闸 + 监控 + 告警 |
| 与现有 `chat_recall` 顺序耦合（schema 改了） | 新增独立 service，零侵入 |

---

## 14. Open Questions

无（所有关键决策已通过 brainstorming 锁定）。

---

## 15. References

- 现有 chat 能力盘点：`Harness/wiki/chat-service-capabilities.md`
- 现有 NL2SQL 引擎：`Harness/wiki/nl2sql-engine.md`
- 现有 Wiki Chat 架构：参见 `backend/app/services/wiki_qa_service.py` + `backend/app/infrastructure/milvus_client.py:594-617`
- Embedding provider 守卫（总闸模式参考）：`qa-system-embedding-provider-guard`
- 失败模式 graceful degradation 模式：参考 `qa-system-h5-h3-recall-fallback`
- 审计 outbox 模式：参考 `business_object_registry_design.md`