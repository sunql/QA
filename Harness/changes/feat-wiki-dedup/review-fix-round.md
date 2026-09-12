# 修复轮复审（最终门禁）— feat-wiki-dedup-p1

范围：`3fdc5cd..工作树`（15 个已跟踪文件 + 2 个未跟踪新文件）。
方法：读 diff + 读生产代码 + **独立复核**（编译 DDL、跑漂移检查、重算 page_id 字面量、
直连 PG 查约束名）。**未跑 pytest**（共享测试库；采纳两份报告的执行证据）。

## 1. 结论（closure table）

| # | 发现 | 裁决 | 证据（文件:行） |
|---|------|------|----------------|
| I1 | ORM 未声明两个索引 → 漂移误报 | **CLOSED** | `wiki_models.py:116`、`models.py:1275-1279` |
| M1 | content_hash「可直接比对/同源」不实声称 | **CLOSED** | `wiki_page_service.py:548-556`、`0061_wiki_dedup.py:5-10`、`test_wiki_dedup_api.py:645-651`、spec:271-273 |
| M2 | `DuplicatePageError.page_id` 空载 | **CLOSED** | `wiki_import_service.py:374-382` |
| M3 | 台账不变量无断言 | **CLOSED** | `test_wiki_dedup_api.py:283-285` |
| MEDIUM | NULL `content_hash` 分支无覆盖 | **CLOSED** | `test_wiki_dedup_api.py:358-395` |
| LOW | 前端 fixture `totalPages` 自相矛盾 | **CLOSED** | `AdminWikiImportPage.test.tsx:737` |
| 1a | createDocument 报错原因张冠李戴 | **CLOSED** | `document_service.py:153-163`、`messages_zh.py:473-476` |
| 1b | updateDocument 无 IntegrityError 处理 | **CLOSED** | `document_service.py:200-208` |
| 1c | registrar check-then-act 竞态 | **CLOSED** | `wiki_catalog_registrar.py:53-79` |
| 1d | ingestDocument 写副作用后才 409 + 假 `DOC-<uuid>` | **PARTIAL**（单线程路径闭合，并发路径未闭合） | `rag_service.py:121-133`；残余见 N2 |
| — | 身份公式改长度前缀 + spec 标 superseded | **CLOSED** | `wiki_page_service.py:169-200`、spec:267 |

### I1 独立复核（不采信报告）

- 编译 ORM 元数据，两处新索引的 DDL 与 0061 迁移**逐字一致**：
  - `CREATE UNIQUE INDEX uq_document_catalog_content_hash ON document_catalog (content_hash)`
  - `CREATE INDEX ix_wiki_page_content_hash ON wiki_page (content_hash)`
  - `Index(unique=True)` 与迁移的 `CREATE UNIQUE INDEX` 同形；改用 `UniqueConstraint`
    会额外产生 `pg_constraint` 行，`models.py:1276-1277` 的注释属实。
- 直连 `qa_metadata_test` 跑 `python -m app.infrastructure.schema_drift`（只读）：
  `[db:index]` 列表已**不含**这两个索引（余 24 项全是预存遗留），且无新增
  `[orm:index]` 误报。**I1 真的收敛，不是报告的一面之词。**

### M1 独立复核

四处点名位置全部改为「同算法同格式同列型，但输入不同」。语义与代码一致：
`document_catalog.content_hash` 来自 `hashContent(文件字节)`（`wiki_import_service.py:245`、
`rag_service.py:126`），`wiki_page.content_hash` 来自 `contentHashOf(草稿文本)`。

### 身份公式独立复核

重算四个 pin 字面量，与本 diff 中的期望值**逐一相符**：
`PAGE-UNTITLED-7048C5E6` / `PAGE-SUPPLIER-QUALIFICATION-C719B4B7` /
`PAGE-X`×40`-D8379DE3` / `PAGE-UNTITLED-6B6EF282`；安全审查的反例
`generatePageId("Z\x00","P","W1") != generatePageId("Z","P","\x00W1")` 现已成立
（`PAGE-Z-06C408F6` vs `PAGE-Z-D895972F`）。长度前缀编码确实单射。

## 2. 本轮新引入的缺陷

### N2（MEDIUM，残余，需人工裁决）— 并发重复上传仍留 Milvus 孤儿向量

`rag_service.py:126-133` 的预检是 **TOCTOU**：检查通过后到 `createDocument` 落库之间，
外部写（`:138` MinIO、`:178` Milvus）已经发生。

- **顺序重传（finding 1d 描述的那条路径）**：预检命中 → 在写入前 409，消息点名
  **已存在**的 document_id，MinIO/Milvus 一次都不碰。**这条闭合了。**
- **并发重传（两个请求同时上传同一份字节）**：两边都过预检 → 两边都
  `insertDocumentChunks`（各带**不同**的 `DOC-<uuid>`）→ 一边 win，另一边
  IntegrityError → 409。败者的 chunk 向量**留在 Milvus**，成为无 catalog 行的孤儿；
  此后 `/documents/search` 会命中它们，`document_name` 回退成那个孤儿 id。
  MinIO 侧无孤儿（对象名内容寻址，两边写的是同一个对象）。

**判定：1d 只在单线程路径上闭合，并发路径的孤儿泄漏仍在。** 属可接受残余还是必须再修，
由人工定：在「只让失败诚实、不实现同一文件→同一文档」的裁定下，重排顺序
（先写 catalog 行再做外部副作用）或加咨询锁都是**越权**改动，我没有把它算作未闭合项。
但「孤儿向量」是真实的数据质量缺陷，故单列在此，不作为放行条件。

### N1（LOW）— registrar 的返回类型契约在冲突兜底路径上不成立

`wiki_catalog_registrar.py:66-79`：`on_conflict_do_nothing` 命中冲突时
`inserted_id is None`，走 `:73-79` 的回查 `... .scalars().first()`。该表达式**可以返回
None**（例如冲突行在同一语句与回查之间被删），而函数签名承诺 `-> DocumentCatalog`。
今天**不炸**：唯一调用点 `wiki_import_service.py:248` 丢弃了返回值。
但「两个并发调用都会拿到同一行」（`:42` docstring）这句话对未来调用方不成立。
建议把返回类型放宽为 `DocumentCatalog | None` 并对 None 显式抛错，或至少断言。

### N3（LOW）— 新测试 docstring 断言了一个复现不出来的生产事实

`test_wiki_dedup_api.py:363-365` 称「『行存在但哈希为 NULL』是**生产里最常见**的碰撞
形状」。progress.md 的实测数据是：prod `qa_metadata.wiki_page` **当前 0 行**，且自该表
出现以来每个 dump 都是 0 行（表是空着建出来的），09-11 的 dump 里根本没有这张表。
即生产里既没有 NULL 历史行、也没有任何碰撞，这句话没有依据。
守卫本身（`existingHash is not None`）是对的、测试的价值不受影响，但**这是与上一轮
「删掉不实声称」同一族的措辞问题**，出现在本轮新增的 docstring 里。

### N4（LOW）— 约束名字符串匹配的稳定性与漏网分支

`str(exc.orig)` 里找约束名在本技术栈上**是稳的**：asyncpg 原样透出 PG 消息
（`duplicate key value violates unique constraint "..."`）。已直连 PG 核实
`uq_document_id` 是真实 `pg_constraint`（`contype='u'`），`_UQ_DOCUMENT_ID` 的值正确；
新测试也证明了 content_hash 索引名出现在错误消息里。两点保留：

1. **两边都不匹配 → 裸 `IntegrityError` 上抛 → 500**（`document_service.py:163`、
   `:208`）。上一版 `createDocument` 是把**任何** IntegrityError 都冒充成「编号重复」
   409，现在未知约束诚实变 500。方向正确，但意味着 `document_catalog` 将来新增唯一
   约束时会出现 500 而无人被告知——建议在 re-raise 分支加一条 `logger.error` 带 `exc`。
2. `_UQ_DOCUMENT_ID`（`:41`）**没有任何测试覆盖**：正常编号重复在 `:131-137` 的预查
   就被拦下，该常量只在竞态路径生效。常量本身我已核实正确，但改个名不会有测试变红。
3. `rag_service.ingestDocument` 的 `Raises:` 段（`:109-110`）只列了 `RagError`，新增的
   `ConflictError`（→409）未记；同函数 docstring 也仍写「6. 写入 document_catalog」。

## 3. 已核对为「无新缺陷」的点

- `on_conflict_do_nothing(index_elements=["content_hash"])`：索引存在且可推断，冲突时
  DO NOTHING、由随后的 SELECT 取回既有行，**幂等语义与旧 SELECT 分支等价**
  （`test_registrar_upsert_same_hash_returns_same_row` 断言 catalog 仍 1 行且 id 不变）。
  `document_id` 冲突（48-bit 前缀撞车）会抛 IntegrityError——与旧实现 flush 时同样抛，
  非回归。
- 新代码行**无一条超过 110 字符**（用字符数而非字节数复核过；`awk` 对中文会按字节虚报）。
- `Index(unique=True)` 的 ORM 声明**未**改变生成 DDL（见 I1）。
- 预检抛 `ConflictError` 不污染事务（无写），`documents.py:208-231` 未捕获它 →
  经 `DomainError` 处理器映射 409，`body["error"]` 形状与测试断言一致（`main.py:375`、
  `_testapp.py:91`）。
- 前端 `{TASK, totalPages: 3, ...}` 与 `skippedPages: 3` 已自洽，断言技法未削弱。

## 4. 非阻塞观察（不计入 findings）

- `wiki_models.py:124` 的「与 document_catalog.content_hash **同口径**」是**未改动的**
  旧行，不在 M1 点名的四处之列，语义上也弱于「可直接比对」，故不作为未闭合项；
  但它与已更正的措辞并存，读者最先看到的是它。若追求 SSOT 干净可顺手加「但输入不同」。
- 「重复即 409」使 `POST /documents/upload` 对**同文件重试**非幂等（客户端超时重试会
  拿到 409 而非 200）。上一版同样 409（只是文案错），故非回归；但值得写进 summary 风险段。
- `ingestDocument`（~168 行）与 `wiki_import_service.execute`（~160 行）超 50 行规则：
  预存，本变更使其更长，已由 python-reviewer 记录，不属本轮。

## 5. 两个裁决

1. **逐项闭合**：上表 11 项中 10 项 **CLOSED**，1 项（1d）**PARTIAL** —— 单线程路径
   闭合、并发路径仍有孤儿向量（N2）。其余 9 条原始 finding 全部在 diff 里找到落点，
   未采信任何「报告说改了就改了」。
2. **本轮整体**：**APPROVE（可进人工 review / 合并）** —— 0 CRITICAL、0 HIGH。
   新引入的 4 条为 1 MEDIUM（N2，残余且有明确边界）+ 3 LOW。N2 不阻塞 P1 放行，
   但**必须在 summary 风险段向用户明示**：去重只防顺序重传，并发重传仍会留孤儿向量。
