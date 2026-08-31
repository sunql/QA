# 变更：feat-rag-pipeline

- **日期**：2026-08-30（初始）/ 2026-08-31（端到端验证 + bug 修复 + SSOT 端口契约）
- **作者**：AI Assistant
- **Phase**：5.2
- **状态**：done
- **Commits**：`277ade2` (feat), `d004d19` (fix), `cbc17d6` (test), `6ea63e3` (SSOT)

## 1. 需求

建立文档 RAG 通道：上传文件 → 解析（PDF/DOCX/TXT/MD）→ 按段落分块 → 生成 embedding → 写入 Milvus `document_embeddings` collection → 通过 `document_id` 关联到 Phase 5.1 的 catalog。同时暴露 `/upload` 和 `/search` 两个端点。

验收标准：
- 上传 → 入库 → 检索 端到端可用
- security_level 过滤生效
- 真实 Milvus + 真实 PG + 真实 HTTP 链路
- 覆盖率 ≥80%

## 2. 设计评审

- Milvus 新集合 `document_embeddings`（独立于 ontology_embeddings / query_embeddings），8 字段含 embedding(1024)
- chunk splitter：按段落切 + 子块（长段落强制切分）+ chunk_size/overlap 可配置
- rag_service：解析 → 分块 → embedding（逐条调 EmbeddingService.generateEmbedding） → Milvus insert → catalog 幂等 upsert
- embedding 用 EmbeddingService.generateEmbedding（单条），不引批量接口（避免与现有签名冲突）

## 3. 数据模型变更

- 无 PG schema 变更（沿用 Phase 5.1 的 document_catalog + document_entity_relation）
- Milvus 新增 collection `document_embeddings`：id, document_id, chunk_id, chunk_text, chunk_sequence, effective_date, security_level, embedding(1024)

## 4. 接口契约变更

挂在 `/api/v1/documents`：
- POST `/api/v1/documents/upload?documentType=...&securityLevel=...`（multipart/form-data）
- POST `/api/v1/documents/search?q=...&topK=...&securityLevel=...`

返回 422 当 RagError（解析失败、空文档、embedding 失败、Milvus 失败）。

## 5. 实现要点

- `app/services/document_parser.py` - parse_document(content, mime_type, filename) → str（PDF/DOCX/TXT/MD）
- `app/services/chunk_splitter.py` - split_by_paragraphs(text, chunk_size=500, overlap=50) → list[Chunk]
- `app/services/rag_service.py` - RagService.ingestDocument / searchDocuments
- `app/infrastructure/milvus_client.py` - _documentFields / ensureDocumentCollection / insertDocumentChunks / searchDocumentChunks
- `app/api/v1/documents.py` - 2 个新路由（upload + search）

## 6. 测试

### 单元（38 个）

- `test_document_service.py` 16 个
- `test_document_parser.py` 6 个（pypdf/python-docx importorskip 兼容缺包）
- `test_chunk_splitter.py` 9 个（89% 覆盖）
- `test_rag_service.py` 7 个（83% 覆盖）

### 集成（17 个）

- `test_document_catalog_api.py` 12 个（Phase 5.1 端点，真实 PG 5433）
- `test_rag_api.py` 5 个（**Phase 5.2 新增端到端，真实 PG + 真实 Milvus**）

### 覆盖率结果

```
chunk_splitter.py        89%  (45 stmts)
document_parser.py       94%  (31 stmts)
document_service.py      91%  (91 stmts)
rag_service.py           83%  (76 stmts)
TOTAL                    88.48%  (≥80% 门槛)
```

## 7. 安全审查

- `parse_document` 仅做 MIME/扩展名判断，无 shell 执行面
- Milvus insert / search 使用 SDK，无字符串拼接 SQL
- `security_level` 用 enum-style 字符串过滤，避免 SQL 注入（`expr = f'security_level == "{securityLevel}"'` 是 Milvus 表达式而非 SQL，注入面有限但需 code-review）
- **未触发 security-reviewer**（无敏感数据 / 无 auth 代码改动）

## 8. 部署验证（2026-08-31 真实环境端到端）

| 步骤 | 期望 | 实际 |
|---|---|---|
| alembic 0030 head | 已应用 | ✅ |
| `/api/v1/documents` POST | 201 + 完整 DTO | ✅ |
| `/api/v1/documents` GET | 200 + 列表 | ✅ |
| `/api/v1/documents/upload` (txt) | 201 + chunks 入库 | ✅ |
| `/api/v1/documents/search?q=...` | 200 + 命中刚入库文档 | ✅ |
| Milvus 集合字段 | 8 字段含 embedding | ✅ |

## 9. 关联

- 依赖：`feat-document-catalog-model` (Phase 5.1)
- 下游：`feat-supplier-360-ads` (Phase 5.3) 通过 RAG 检索供应商合同
- 设计稿：`Harness/wiki/architecture.md` Phase 5 段

## 10. 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| Milvus 集合命名 | `document_embeddings`（独立 collection） | 与 ontology/query 集合隔离，schema 独立演进 |
| Chunk 切分粒度 | 段落 + 子块（长段落强制切分） | 段落保语义完整，长段落避免单 chunk 超 4000 字符 |
| Embedding 调用方式 | 逐条 generateEmbedding(str) | EmbeddingService 现有 API 单条签名，不引批量包装 |
| Catalog 幂等 | listDocuments + filter document_id | 数据量小时 OK；大表场景需改 getDocument(by document_id) 查询 |

## 11. 端到端验证发现的 Bug（已修复）

### Bug #1 — `_getEmbeddingService()` 引用不存在的工厂函数

- **现象**：uvicorn 启动成功，但 POST `/upload` → 500 `ImportError: cannot import name 'getEmbeddingService'`
- **根因**：`rag_service.py:34` 调 `from app.services.embedding_service import getEmbeddingService`，但 `embedding_service.py` 里只有 `EmbeddingService` 类，没有这个工厂函数
- **修复**：改为 `EmbeddingService()`（commit `d004d19`）
- **回归保护**：`test_rag_api.py` 5 个用例覆盖真实环境 upload→search（commit `cbc17d6`）

### Bug #2 — `embed_texts([...])` 接口错位

- **现象**：Bug #1 修复后再次 `/upload` → 422 `'EmbeddingService' object has no attribute 'embed_texts'`
- **根因**：`rag_service.py` 假设有 `embed_texts(list[str]) -> list[list[float]]`，但 `EmbeddingService` 实际只有 `generateEmbedding(str) -> list[float]`（单条签名）
- **修复**：循环逐条调用 `generateEmbedding`；test mock 同步更新（commit `d004d19`）

### 根因总结

TDD 流程跳过了"在真实环境跑一次"步骤：

1. 单测用 `patch("app.services.rag_service._getEmbeddingService", ...)` 绕过了真实模块导入 → ImportError 被 mock 吸收
2. 单测用 `mock_emb_svc.embed_texts = AsyncMock(...)` 设置了一个不存在的方法 → AttributeError 永远不触发

**38/38 单测全过、production 100% 500。** 这种 bug 类型（mock 绕过 + 不存在的接口名）单测无法捕获，必须真实环境跑一次。

### 后续根因修复（commit `6ea63e3`）

发现这次端到端 bug 与之前 5432/5433 端口漂移是同类问题——开发假设与运行环境不一致，过去靠口口相传。SSOT 端口契约修复把这类环境契约问题固化为代码契约 + 双层 fail-fast。

### Phase 5.1 验证补充

复测 Phase 5.1 的 catalog 端到端：12/12 集成测试全过、curl POST/GET 全 2xx——catalog 自身无 bug，本次 bug 仅在 Phase 5.2 新增的 RAG 路径。

---

**Phase 5.2 当前真实状态：✅ 端到端可用、bug 已修复、回归保护已建。**