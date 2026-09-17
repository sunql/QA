# 变更：feat-wiki-semantic-search（wiki 知识条目语义检索）

- **日期**：2026-09-16
- **Phase**：feature（wiki 检索对齐知识库 RAG 体验：向量语义检索 + 相似度排序）
- **状态**：done（后端已部署 + backfill；前端已 build）
- **触发**：用户对比「wiki 检索 vs 知识库检索」后选择语义检索方案（AskUserQuestion：语义检索/关键词优化/都要/保持现状 → 语义检索）
- **计划文件**：~/.claude/plans/deep-giggling-frost.md（已批准）
- **MEMORY**：qa-system-wiki-semantic-search.md（本会话写入）

---

## 1. 需求

feat-wiki-search-endpoint 的 ILIKE 关键词检索与 RAG 知识库的 Milvus 语义检索体验不一致：换个说法搜不到、无相关性排序。用户要求升级为语义检索。

## 3. 数据模型变更

无 alembic 迁移。新增 Milvus 集合 `wiki_page_embeddings`（运行时 `_ensureCollection` 创建，非迁移管理）：

- 字段（顺序即契约）：`id`(auto_id PK) / `page_id`(64) / `chunk_id`(64) / `chunk_text`(4000) / `chunk_sequence` / `title`(200) / `dimension`(30) / `status`(10) / `embedding`(FLOAT_VECTOR, dim=1024)
- 配置开关：`WIKI_VECTOR_SYNC_ENABLED`（默认 true）——写路径向量同步总闸

## 4. 接口契约变更

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/wiki/pages/semantic-search` | `query` 必填（strip 非空→422）、`dimension?`、`topK`(1-50)；返回 `[{pageId,title,status,dimension,chunkText,chunkSequence,distance,score}]`；Milvus/embedding 故障 → **503** |
| POST | `/api/v1/wiki/vector-sync` | admin-only（getAdminOnlyActor）；全量回填，幂等；返回 `{pages, chunks}` |

score 公式与 RAG 同口径：`1/(1+L2 distance)`。

## 5. 实现要点

- **milvus_client.py**：第 4 套 collection（照 document 模式：`ensureWikiPageCollection` / `insertWikiPageChunks`（位置列表，顺序契约）/ `searchWikiPageChunks`（expr 默认排除 EXPIRED——与知识图谱 graph_data 口径一致）/ `deleteWikiPageChunks`（expr 必须带 page_id 过滤）/ `queryWikiPageChunks`）
- **wiki_vector_service.py**（新，~230 行）：
  - `splitMarkdownBlocks`：按空行切段，段落跟随最近 `#` 标题（section_name）；纯标题段不产出正文块
  - `syncPage`：upsert = 删旧 + embed（逐 chunk 调 EmbeddingService）+ 插新；正文清空只删不插
  - `searchSemantic`：PG 回查覆盖 Milvus 快照（title/status/dimension 以 PG 为 SSOT；回查失败降级快照字段不阻塞）
  - `backfill` / `syncPagesBestEffort`：幂等对账
- **写路径挂钩（best-effort，不阻断 CRUD）**：
  - `wiki_page_service`：createPage / updatePage（`_VECTOR_FIELDS={title,content,status,dimension}` 有变更才重嵌——embedding 是真金白银）/ deletePage / deletePages
  - `wiki_import_service`：任务收尾（SUCCEEDED/PARTIAL）按 pageIds 批量补同步（导入直插 ORM 绕过 service）
  - 失败仅 logger.warning；漂移靠 vector-sync 自愈（Milvus 漂移事故元教训）
  - **开关检查在挂钩入口**：`_vectorSyncEnabled()` false 时零开销（测试环境无 embedding provider，曾实测拖慢集成套件 16s→340s，加开关后 15s）

## 6. 测试

| 套件 | 结果 |
|---|---|
| unit `test_wiki_vector_service.py` 8 用例（切分/标题跟踪/upsert 编排/空正文只删/score 映射/PG 覆盖/缺失降级/回填幂等） | ✅ 8/8 |
| integration `test_milvus_wiki_fields.py` 5 用例（字段契约 + 真实 Milvus 往返/EXPIRED 排除/删除作用域） | ✅ 5/5 |
| integration `test_wiki_api.py` 34 用例（+6：空白 422/topK 边界/契约透传/503 映射/403 admin/回填计数） | ✅ 34/34 |
| frontend vitest `wikiApi.test.ts` 28 用例（+2 semantic 契约） | ✅ 28/28 |
| tsc --noEmit | ✅ 0 |

## 7. 安全审查

- semantic-search 只读；query 走 service（无 SQL 拼接风险）；503 detail 不含敏感信息
- vector-sync admin-only；backfill 只读 PG + upsert Milvus
- 写路径挂钩全部 best-effort：向量基础设施故障不放大为业务 500

## 8. 部署验证

```bash
./scripts/deploy_backend.sh            # 后端（app/ + alembic/ 灌入 + 重启）
docker compose build --no-cache \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend
docker compose up -d frontend          # bundle grep semantic-search = 1
POST /api/v1/wiki/vector-sync          # backfill 实测：36 页 → 778 chunks，13 分钟
```

端到端对照（同一查询「厂家合作有什么门槛」）：关键词版 total=0；语义版命中
3 条（5.1采购与供应商控制流程 53.3% / Sheet3 53.3% / 04采购域主数据模型 52.8%）。

## 7.1 backfill 性能注记

embedding 走 omlx（localhost:8888，容器内 host.docker.internal）逐 chunk 调用，
36 页 778 chunk 耗时 13 分钟（~1s/请求，串行）。条目量大时应并行化或批量 embed
（EmbeddingClient.embed 本就支持 batch，syncPage 可改分批）。当前量级可接受，
暂不优化。

## 9. 关联与偏离

### 计划偏离的解除（2026-09-16 同日晚，feat-wiki-agent-semantic-metering）

原偏离「agent 工具 wiki_search 未升级语义」**已解除**——用户确认要做计量：

- `embedding_client.embedWithUsage`：从 SDK response.usage 取 prompt_tokens（provider 未报则 0，绝不估算）
- `embedding_service.embedWithUsage`：门面版，返回 (向量, tokens, 模型名) 三元组
- `LEARNING_MECHANISMS` 新增 **RETRIEVE** 机制
- `_meterEmbedding`：独立短命 session（getSessionFactory）落 wiki_token_usage，best-effort；tokens<=0 短路不落行；cost 恒 0（embedding_provider 表无单价字段，本地模型免费，落 0 不编造）
- `wiki_vector_service` 的 syncPage / searchSemantic 全部计量（覆盖 REST 检索、写路径挂钩、回填——核心约束 #3「每次 LLM 调用必须记录」）
- `_wikiSearchHandler` 语义优先：同页多 chunk 去重取最高分；WikiVectorError/任何异常 → 降级关键词（mode 字段区分 semantic/keyword）；ToolResult tokens 恒 0（计量已在台账，避免双口径）
- ToolResult tokens=0 的理由：AgentRunRead 的 tokens 与 wiki_token_usage 台账是两个口径，都填会重复计数；台账是 SSOT

**部署坑 +1**：agent_definition 在 prod 库是空的（seed 从未跑过）——
`docker exec qa-backend python scripts/seed_agents.py` 补 9 Agent / 7 绑定，
WIKI_SEARCH_AGENT 端到端验证通过（mode=semantic，5 hits，
wiki_token_usage 落 model_name=bge-m3-mlx-8bit prompt_tokens=7）。

**测试**：unit +3（计量/去重/降级/零命中不降级，共 11）+ agent search tool +3；
embedding_service 门面 +embedWithUsage。全部绿。

### 元教训（新增）

- **门面层会吞掉底层新能力**：给 EmbeddingClient 加 embedWithUsage 后，
  agent 路径实际走的是 EmbeddingService 门面——没有透传方法，token 恒 0、
  计量静默短路（best-effort 又把异常吞了，零日志）。真机验证计量行时才发现。
  教训：给「client → 门面 → service」三层链路加能力时，每一层都要验证。

### 关联

- [feat-wiki-search-endpoint](../feat-wiki-search-endpoint/summary.md)（关键词版，本特性的降级路径）
- 记忆：Milvus 本体向量漂移事故（best-effort + backfill 自愈设计的出处）、qa-system-two-dbs（真实库纪律）

### 元教训

- **写路径挂钩必须配开关**：best-effort 不等于零成本——provider 超时会拖垮
  整个测试套件（20 倍减速）。外部依赖挂钩一律加 `*_ENABLED` 总闸。
- **计划评审要读模块头约束**：agent 工具「不调 LLM」写在 agent_tools_wiki.py
  文档字符串里，计划阶段没读就排了升级任务。SSOT 散在代码注释里，动手前
  先扫目标文件的模块头。
