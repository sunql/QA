# 变更：M2-3 采购文档检索 `POST /procurement/knowledge/documents/search`

- 日期：2026-11-20　分支：`feat/procurement-knowledge-documents`（堆叠 off `feat/knowledge-evidence-contract`）

## 内容（设计稿 §8.3 第三端点，M2-1 契约的回填方）
- **新路由** `app/api/v1/procurement_knowledge_documents.py`：
  `question / documentTypes[] / entityKeys[]（TYPE:KEY 形式）/ effectiveAt / topK(1..20)` ⇒
  `list[KnowledgeEvidenceRead]`（kind=DOCUMENT）；
- **检索复用** `RagService.searchDocuments`（Milvus chunk 检索 + **Task 4.3 密级闸经
  user 透传**——本批不重复实现密级过滤）；编排过滤与证据映射在
  `services/knowledge/document_search`（searchFn 可注入，测试无 Milvus 依赖）；
- **fail-closed 过滤**：无 catalog 行的 hit **丢弃**（密级/有效期无法核验）；
  非 ACTIVE 丢弃；effectiveAt 过滤（生效期未知=保留，未来生效=剔除）；
  entityKeys 按 `document_entity_relation` 命中任一即保留；
- **M2-1 契约回填**：`security.level`（catalog 密级）/ `sourceRef.documentId` /
  `validAt`（catalog 生效日升 datetime）/ `confidence`（1 - distance）；
- **provenance**：`source=LIVE`——文档不在本体发布面但 `catalog.version` 持久可溯
  （与治理表「无版本可引」不同）；`release_id/version` 置空。

## 契约订正（M2-1）
`KnowledgeEvidenceSourceRef.document_id` int ⇒ **str**（document_id 是 String(50)
文档编号，非整数主键）。

## 过程坑（入档）
`str(枚举)` 对 `(str, Enum)` 返回 `"ClassName.X"` ⇒ documentType 比较 /
security.level 回填一律用 `.value`。

## 已知限制（登记）
Milvus 侧不支持按文档类型/实体过滤 ⇒ 先 topK 取回后内存过滤，过滤后条数可能
少于 topK（先取后滤的固有形态；要精确分面需 Milvus schema 扩列，另批）。

## 测试（8 用例，新 unit 文件）+ 双接线守卫
fail-closed 丢无 catalog 行 / 非 ACTIVE 丢 / 类型过滤 / 生效期（保留未知）/ 实体关联 /
契约回填 ×2（security/validAt/sourceRef/confidence + 未知生效日）/ 双接线守卫。
知识面回归 73 passed；ruff 净新增 0（B008 ×2 = 仓内路由惯例）。
