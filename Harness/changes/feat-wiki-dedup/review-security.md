# 修复轮安全复审 — feat-wiki-dedup P1

审查对象：`git diff 3fdc5cd`（修复轮全部改动，含身份编码、四个写入方诚实化、ORM 索引声明）。
审查者：`security-reviewer`（opus）。本文由控制者据其结论整理存档（审查者按自身策略未落盘）。

## 结论

**四个修复写法正确。修复轮未引入任何新的 CRITICAL / HIGH。** 一条 LOW 残留需记录（见下）。

## CRITICAL

无。

## HIGH

**修复轮未引入。** 但发现一条**与本变更无关的预存 HIGH**，按规则「不粉饰」记在此处，**不在 P1 范围内修**：

`backend/app/api/v1/documents.py` 的 `/api/v1/documents` 仅由 `getCurrentUser` 守卫 —— **没有** `AclService.assertCanModify`，没有 owner / department / `security_level` 任何过滤。任何通过认证的调用方可以 list / update / delete **全部** 文档。`GET /documents` 返回 `documentId`、`contentHash`、`owner`、`securityLevel`、`storageUrl`。
另注：默认 `AUTH_STUB_ENABLED=1` 下，**不带任何 header** 的请求会被解析为 `roles=("user","admin")`。

⇒ **需要另开变更**（与覆盖率缺口一并列入待办）。P1 不合并且不修，但必须让人类看见。

## MEDIUM

无。

## LOW

### L1 —— 摄取路径的 TOCTOU 残留（未完全关闭）

`findByContentHash` 预检**只关闭了单线程路径**。实际顺序为：
`findByContentHash` → `putSourceObject`(MinIO) → 向量化 → `insertDocumentChunks`(Milvus) → `createDocument`(PG)。

两路相同的字节并发上传时，**双方都能通过预检**，双方都写入 MinIO 与 Milvus，输家在 `createDocument` 撞 `uq_document_catalog_content_hash` → 泛化 409。输家写入的 Milvus 分片用的是**全新的** `DOC-<uuid>`（`rag_service.py:160`），该 ID 永不进入 catalog，且会出现在 `searchDocuments` 里、`document_name` 回退成裸 uuid（`rag_service.py:300-302`）；文件名不同的情况下 MinIO 也会留孤儿。
**机密性影响为零**（孤儿分片承载的与赢家是同一份字节）。
**建议修法**：冲突时补偿删除刚写入的 chunks/object，或在外部写入**之前**先占住 catalog 行。

### L2 —— `IntegrityError` 靠约束名分类

今日可用，已对 DDL 核实：`uq_document_id` 是真实具名约束（`0030_document_catalog.py:60`），`uq_document_catalog_content_hash` 是真实唯一索引（`0061_wiki_dedup.py:79`），PG 对两者都抛出 `duplicate key value violates unique constraint "<name>"`。
**误判无安全后果**：内容冲突文案不含业务数据、**不回显哈希值**；document_id 分支只回显调用方自己的输入。未匹配的 `IntegrityError` 被重新抛出（`document_service.py:163`、`:208`），到达客户端是 FastAPI 默认 `{"detail":"Internal Server Error"}` —— 未开 `debug=True`（`main.py:209-215`），仅注册了 `DomainError` 与限流处理器（`main.py:365-379`），**不泄露 PG 内部结构**。
脆弱点是可用性而非机密性：日后重命名任一索引会静默退化成 500。

### L3 —— 面向用户的文案里嵌了索引名

两条新 409 文案含 `uq_document_catalog_content_hash`。属 schema 内部细节外泄到客户端响应；与既有文案风格一致，无业务数据泄露。

### L4 —— 客户端可设置唯一列（本轮未处理）

`DocumentCreate.content_hash`（`schemas.py:2240`）与 `DocumentUpdate.content_hash`（`:2253`）允许调用方声明任意哈希；0061 之后这可用来**占位**某个哈希，让该内容的合法上传永久 409，且无 ACL 的 `PUT /documents/{id}` 可对任意文档施加。
评为 LOW 仅因为**同一主体本就能覆盖/删除任意文档**，占位并未增加能力。前一轮审查提的第三条建议（不再接受客户端传入 `content_hash`）未采纳。

## Q1 —— 授权链路追查（明确回答）

`findByContentHash`（`document_service.py:111-122`）与两条冲突路径**直接查询 `document_catalog`，无 owner / department / security_level 谓词，也无 ACL 断言**。

**但是：`document_catalog` 上根本不存在可被绕过的调用方数据授权过滤** —— `DocumentService` 内全程没有，包括既有 `GET /api/v1/documents` 背后的 `listDocuments`，后者本就把每个 `documentId` 与 `contentHash` 交给任意已认证调用方。因此新 409 带来的**净新增泄露为零**：攻击者持有候选内容即可算出 `sha256` 并得知「这串字节是否已入库、对应哪个 `document_id`」，而同样的查询经由 `GET /documents` 早已可得（仅受 `limit`/`offset` 限制，并非不可枚举）。**未创造新的枚举信道**；文案甚至不回显 `content_hash` 值。

**前瞻风险（需记录）**：若日后给文档读取路径加上 ACL 或 `securityLevel` 强制，必须在**同一次变更**里一并收紧 `POST /documents`、`PUT /documents/{id}`、`POST /documents/upload`，否则这些 409 会变成绕过口，并成为真正的存在性预言机。

## Q3 —— registrar：干净

`inserted_id = scalar_one_or_none()`；为 `None` 时重新 select 既有行（`wiki_catalog_registrar.py:66-79`）。唯一调用方**完全忽略返回值**（`wiki_import_service.py:248-255` 只返回 `storageUrl`），因此即便回退到 `None` 也不会产生坏 FK 或 `AttributeError`。冲突目标与实际索引一致，ORM 现在声明的也是同一个 `Index(unique=True)`。`DO NOTHING` 不刷新 `owner`，与旧的 SELECT-then-INSERT 语义一致 —— actor 派生的 `owner` 无权限变化。

## Q5 —— 无新增问题

无 SQL 注入：两个新查询走 SQLAlchemy Core/ORM 绑定参数；唯一的裸 `op.execute` DDL 在迁移里，不在请求路径。`findByContentHash` 的实参恒为 `hashContent(content)`，作用于服务端持有的字节（`rag_service.py:126`），**从不是客户端输入**。无新端点、无新模块级状态。
一条范围注记：预检前移后，「重复上传探测」比过去**更便宜**（不再产生 MinIO/Milvus 副作用）并会透露某串精确字节是否在库中 —— 但见 Q1，这并非增量泄露。
