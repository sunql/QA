# 变更：feat-document-catalog-model

- **日期**：2026-08-30
- **作者**：AI Assistant
- **Phase**：5.1
- **状态**：done
- **Commit**：`91c829c`

## 1. 需求

建立非结构化文档元数据目录（Document Catalog），关联到业务实体（供应商/物料/PO/NCR）。是 Phase 5 的基础底座——后续 RAG（5.2）/ Supplier 360°（5.3）/ 8D 报告检索等所有"文档→业务"场景都依赖此 catalog。

验收标准：
- 2 张表 + 完整 CRUD + 关联去重
- 真实 PG 5433 集成测试通过
- 覆盖率 ≥80%

## 2. 设计评审

- 2 张表拆分：`document_catalog`（文档元数据）+ `document_entity_relation`（多对多关联），避免在 catalog 表里塞 JSON 数组
- document_id 用业务编号（VARCHAR unique），主键 id 仍是 bigint（与既有 ORM 风格一致）
- 关联去重：`unique(document_id, entity_type, entity_key, relation_type)`，重复入关联返回 409

## 3. 数据模型变更

- 新增 `document_catalog` 表（0030 migration）：
  - id, document_id (unique), document_name, document_type (CONTRACT/8D_REPORT/AUDIT_REPORT/SPEC/SOP/...)
  - version, status (ACTIVE/EXPIRED), owner, effective_date, security_level (L1/L2/L3)
  - storage_url, content_hash, created_time, updated_time
- 新增 `document_entity_relation` 表：
  - id, document_id (FK), entity_type (SUPPLIER/MATERIAL/PO/IQC), entity_key (BIGINT)
  - relation_type (CONTRACT/8D_REPORT/AUDIT_REPORT/SPEC/SOP)
  - unique (document_id, entity_type, entity_key, relation_type)

## 4. 接口契约变更

挂在 `/api/v1/documents`：
- GET `/api/v1/documents`（过滤 type/securityLevel/status）
- GET/POST/PUT/DELETE `/api/v1/documents/{documentId}`
- GET `/api/v1/documents/relations`
- GET/POST/DELETE `/api/v1/documents/relations/{relationId}`

## 5. 实现要点

- `backend/alembic/versions/0030_document_catalog.py` - Alembic 迁移
- `backend/app/services/document_service.py` - DocumentService（listDocuments/createDocument/updateDocument/deleteDocument + 关联 4 端点）
- `backend/app/api/v1/documents.py` - 9 个 REST 路由
- `backend/app/main.py` + `backend/app/tests/_testapp.py` - 注册 router

## 6. 测试

- 单元：`test_document_service.py` 16 个用例
- 集成：`test_document_catalog_api.py` 12 个用例（真实 PG 5433）
- 全部通过

## 7. 安全审查

- API 层都走 `getCurrentUser` 鉴权（Phase 5 还没接 JWT/IdP，沿用 stub auth）
- 关联创建带去重约束（unique index），无 SQL 注入面
- 未触发 security-reviewer（无敏感数据）

## 8. 部署验证

- alembic 0030 head 应用成功
- 9 路由在 OpenAPI 中可见
- curl POST/GET/LIST 全部 2xx

## 9. 关联

- Phase 5.2 RAG：写完 ingestDocument 后用 document_id 关联 catalog
- Phase 5.3 Supplier 360°：从 catalog 拉供应商合同/8D
- 数据模型详见 `Harness/wiki/data-model.md`（Document 段）

## 11. 后续发现

2026-08-31 验证发现 + Phase 5.2 commit 留下的接口错位 bug（`rag_service` 引用不存在的 `getEmbeddingService` 工厂和 `embed_texts` 方法）在 Phase 5.1 catalog 层无影响——catalog 自身端到端无 bug。修复见 `Harness/changes/feat-rag-pipeline/summary.md` §11。