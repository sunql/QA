# 迁移 `0061_wiki_dedup` —— HITL 复审材料

> 按 `Harness/rules/` 的数据模型变更规则，本迁移须**人审通过后方可进入 `main`**。
> 注意：迁移**已在 prod 与测试库双双执行完毕**（`alembic_version = 0061_wiki_dedup`），
> 因此本次复审是**事后确认**，实质问题不是「要不要执行」，而是
> **「已生效的四项 DDL 是否保留，尤其那条唯一索引」**。
>
> 迁移文件：`backend/alembic/versions/0061_wiki_dedup.py`（93 行）
> 落地提交：`ead9dd4`（写入方诚实化）→ `f5ba422`（身份编码）→ `d46f72b`（部署验证）

---

## 1. 四处 DDL（原文 + 目的 + 失败模式）

| # | DDL | 目的 | 失败模式 |
|---|---|---|---|
| 1 | `ALTER TABLE wiki_page ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)` | 让「同 ID 不同内容」与「同文件重跑」在数据层可判定 | 纯加列，可空无默认 ⇒ 对既有行零影响 |
| 2 | `CREATE INDEX IF NOT EXISTS ix_wiki_page_content_hash ON wiki_page (content_hash)` | **非唯一**。服务「这份正文还出现在哪些条目里」的排查查询 | 无。刻意不加唯一：同一段正文出现在两条知识里合法（共享模板），加唯一会把正常写入变成 500 |
| 3 | `CREATE UNIQUE INDEX IF NOT EXISTS uq_document_catalog_content_hash ON document_catalog (content_hash)` | spec §4.7 D2-2 把该唯一约束推迟到 P1「一并做，只付一次迁移成本」，P1 即本次 | **建索引前已查重：两库重复分组均为 0** ⇒ 不会因重复摘要失败。若未来库上有重复，DDL 原子回滚，属安全失败 |
| 4 | `ALTER TABLE wiki_import_task ADD COLUMN IF NOT EXISTS skipped_pages INTEGER NOT NULL DEFAULT 0` | 重复项不再计失败（spec §5.4）。否则「8 条全是重复」会退化成 `success=0, failed=0` 的第三种状态，运维无法区分「没跑」与「跑了但都已入库」 | `NOT NULL` 带 `DEFAULT 0` ⇒ 既有行自动补 0，安全 |

**幂等性**：四项全用 `IF NOT EXISTS`，与 0053–0060 同模式 —— prod 是从 dump 恢复的，重复执行必须是 no-op。
**downgrade 对称**：删列 + 删索引，四项均由本迁移新建，不存在 0060 那种「列早于迁移且带真实数据」的情形。

### 两处同名不同约束**不是笔误**

`wiki_page.content_hash` 非唯一、`document_catalog.content_hash` 唯一 —— 方向刻意相反：

- `wiki_page` 的行 = 一条**知识条目**，共享模板/多部门引用同一条款是**合法**的；
- `document_catalog` 的行 = 一份**源文档**，同一份文件重复上传必须映射到**同一份**文档。

---

## 2. 两库实测状态（2026-09-13 复查，只读查询）

| 项 | prod `qa_metadata` | 测试库 `qa_metadata_test` |
|---|---|---|
| `alembic_version` | `0061_wiki_dedup` | `0061_wiki_dedup` |
| `ix_wiki_page_content_hash` | 存在，**非唯一** ✅ | 存在，**非唯一** ✅ |
| `uq_document_catalog_content_hash` | 存在，**UNIQUE** ✅ | 存在，**UNIQUE** ✅ |
| `wiki_page` 行数 | 0 | 0 |
| `document_catalog` 行数 | 1（`采购与供应商控制程序.pdf`，hash `c1ce343e…`） | 1（`规则.txt`，hash `b7ae4faa…`） |
| `document_catalog` 重复 hash 分组 | **0** | **0** |
| `content_hash IS NULL` 行 | 0 | 0 |

> **一处档案更正**：0061 的模块 docstring 称「测试库 `document_catalog` 为 0 行」——
> 该说法**在迁移执行时成立，现已过时**。测试库现存的 `规则.txt` 那行是**测试残渣**
> （wiki 导入测试写下的登记行），不是生产数据，也不构成重复。不影响迁移正确性，
> 但 docstring 这句话已不再准确。
>
> 另注：`wiki_import_task` 在 prod 有 8 行（历史任务台账），测试库 0 行。

---

## 3. 影响面：谁会撞上这条唯一索引

`document_catalog` 的写入方共 **4 条路径**，P1 已让**全部 4 条**对约束有正确反应：

| 路径 | 入口 | P1 后的行为 |
|---|---|---|
| 建文档 | `POST /api/v1/documents` | `IntegrityError` 按约束名分类 → 内容重复 409 / document_id 重复 409（`document_service.py`） |
| 改文档 | `PUT /api/v1/documents/{id}` | P1 **新增**了原本缺失的 `IntegrityError` 处理（此前会裸 500） |
| 上传文档 | `POST /api/v1/documents/upload` → `rag_service` | 冲突预检**前移到** MinIO 写入与 Milvus 向量化**之前**，409 并指名已存在的 `document_id` |
| Wiki 导入登记 | `wiki_catalog_registrar.upsertByContentHash` | `INSERT … ON CONFLICT DO NOTHING`（原 SELECT-then-INSERT 是 check-then-act 竞态） |

**读取方**（不受约束影响）：`listDocuments`、`getDocument`、`findByContentHash`、`graph_relation_service` 的 JOIN。

### ⚠️ 部署前曾存在的危险中间态（已消除，供复盘）

prod 的 `alembic_version` 早已是 `0061`（**唯一索引已生效**），而容器一度仍跑 P1 之前的代码：
旧代码对这条唯一索引**无任何处理** ⇒ 重复上传会抛未捕获的 `IntegrityError` → 500，
且此时 MinIO 对象与 Milvus 分片**已经写下**，留下孤儿。
已于 2026-09-13 部署消除（见 `summary.md` §8）。**教训：迁移与代码必须同批上线。**

---

## 4. 待裁决：唯一索引把「客户端可写列」变成了全局锁

这是本次复审**唯一实质新增风险**，请重点裁决。

- `DocumentCreate.content_hash` 与 `DocumentUpdate.content_hash` **允许调用方直接声明任意哈希值**；
- 0061 之前 `content_hash` 无唯一约束，**随便写是良性的**；
- 0061 之后该列**唯一** ⇒ 保留一行 `content_hash = X` 即可**永久占位**，
  此后任何**哈希为 X 的合法内容**上传/导入都会被 409 挡下。

且 `PUT /api/v1/documents/{id}` **没有 ACL**（仅 `getCurrentUser`；默认 `AUTH_STUB_ENABLED=1` 下**不带任何 header** 的请求被解析为 `roles=("user","admin")`）⇒ 任意调用方可对**任意文档**施加该占位。

**这与安全审查给的 LOW 评级不矛盾但值得重新权衡**：当时判 LOW 的理由是「同一主体本就能覆盖/删除任意文档，未增能力」。该理由针对的是**改动已有数据**；而这里新增的是**阻断未来导入特定字节的能力** —— 删文档做不到这件事（删了反而释放该哈希），必须**留着**那行才行。**机密性无损，可用性上确有净新增。**

**建议**（P1 未采纳，前轮审查提过）：不再接受客户端传入 `content_hash`，一律由服务端按文件字节计算。
若裁决「保留索引但不改接口」，则应把这条写进已知接受风险；若要修，建议**与 `/api/v1/documents` 的 ACL 缺失一并修**（同一张表、同一批接口）。

---

## 5. 回滚代价

`0061_wiki_dedup` 的 `downgrade()` 会**一并**执行四项反向操作：
删 `skipped_pages`、删唯一索引、删 `ix_wiki_page_content_hash`、删 `wiki_page.content_hash`。

⚠️ 因此**「只想撤掉唯一索引、保留另外三项」不能靠 `downgrade`**，需手写一条
`DROP INDEX uq_document_catalog_content_hash`。请先明确要撤到哪一档。

数据损失面：`wiki_page` 当前 **0 行** ⇒ 删 `content_hash` 列**不丢任何已回填数据**（本来就没有回填）。
`document_catalog` 的 1 行是 P0 上传路径写入的，其 `content_hash` 值不因删索引而变。

---

## 6. 复审清单

- [ ] 四项 DDL 是否全部保留？
- [ ] 若保留唯一索引：**§4 的客户端可写列问题**是接受、还是同变更内修（收紧 `content_hash` 入参 + 补 `/documents` ACL）？
- [ ] §2 指出的 docstring 过时表述（测试库 0 行）是否随本次一并更正？
- [ ] 「迁移与代码必须同批上线」是否写入部署规范，避免中间态复发？
