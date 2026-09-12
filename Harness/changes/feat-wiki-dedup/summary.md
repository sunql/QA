# 变更：feat-wiki-dedup（P1 去重：内容派生身份）

- **日期**：2026-09-12（修复轮与门禁跨至 2026-09-13）
- **作者**：启琳（Claude Code）
- **Phase**：P1（《企业 LLM Wiki 知识层落地设计》§五）
- **状态**：in-review —— 等待人工 review。`0061_wiki_dedup` 是数据模型变更，**属 HITL，未人工过目不得合并**
- **分支**：`feat/wiki-dedup-p1`，基分支 `feat/wiki-provenance-p0`（P0 尚未合并）
- **SSOT**：`docs/superpowers/specs/2026-09-12-wiki-knowledge-layer-design.md` §五
- **计划**：`docs/superpowers/plans/2026-09-12-wiki-dedup-p1.md`

## 1. 需求

**背景（含一处诚实修正）。** 设计阶段的测量结论是：`wiki_page` 曾积累数百行而不同标题只有 75 个 —— 同一份 75 页文档被 8 个导入任务反复入库，产生约 7.8 份副本。根因是 `generatePageId` 的后缀用 `uuid4()[:8]`，同一标题每次算出不同的 `page_id`，导入路径既有的冲突检查因此**永不命中**。

> **该数字今天无法复现。** 2026-09-12 复核：prod `qa_metadata.wiki_page` 与测试库均为 **0 行**，且自 `wiki_page` 在 prod 出现（09-12 07:49 首个含该表的 dump）起，**每一个 dump 都是 0 行**；`pg_stat_user_tables` 显示 `n_tup_ins = n_tup_del = 596`（真实 DELETE，不是 volume 销毁 —— 后者会重置统计）。`wiki_import_task` 的 8 行仍在，说明那 8 次导入**确实跑过**，但它们的页面行已不在任何现存库或备份中。删除者的身份没有证据，**不做猜测**。本节据此把「557 行 / 75 标题」记为**历史测量**，而非当前 prod 状态。

**目标。** 同一份文件重跑 N 次，知识条目恒为一份。

**验收标准（spec §十 P1）。** 同一份文件导入两次 → 第二次数入 `skippedPages` 且不落副本；`wiki_learning_models.py` 关于幂等重放的那句声称由可执行测试钉死。

**非目标。**
- 不做 P3 的知识版本化；不建独立去重表；不读 `wiki_import_task.page_ids`。
- **不做「同一文件 → 同一份 `document_catalog` 文档」。** 见 §2 裁决 D1-5。

## 2. 设计评审

- **D1 —— 身份内容派生。** `page_id = "PAGE-" + slug(title) + "-" + sha256(identity(source_ref, title, content))[:8].upper()`。**零新机制**：复用导入路径既有的 `page_id` 冲突检查与 `uq_wiki_page_page_id`，重复项根本落不了库。
- **D1-1 —— 「重复」的判定。** 重复 = `page_id` 撞车 **且** `content_hash` 相等；不等或任一侧为 NULL → 仍计冲突（失败）。理由：`[:8]` 只有 32 bit，只按 ID 判定会把碰撞到的**另一份**文档静默丢掉。宁可报错，不静默丢。
- **D1-2 —— `POST /wiki/pages` 撞号仍返回 409。** 单条显式创建与批处理重跑是两种语义，不合并。
- **D1-3 —— PATCH content 重算 `content_hash`，`page_id` 不变。** 前者不重算会让跳过分支基于过期哈希判定；后者被 `knowledge_claim.page_id` / `knowledge_relation.upstream_page_id` 以 FK 引用，改 ID 会让已确认的关系悬空。
- **D1-4 —— 身份编码改用长度前缀（修订单，经人工批准）。** spec §5.2 写的是 `sha256(source_ref \x00 title \x00 content)`。该编码**并非单射**：U+0000 在 JSON 字符串与 Python `str` 中均合法，`generatePageId("Z\x00","P","W1")` 与 `generatePageId("Z","P","\x00W1")` 实测撞出同一个 ID。今天不可利用，只是因为 PostgreSQL 恰好拒绝 `text` 列的零字节 —— 是巧合，不是设计。实现改为**每字段 UTF-8 编码后前缀 8 字节大端长度再拼接**，单射 by construction。原 docstring 的「NUL 使字段边界无法伪造」论证是**错的**，已删。spec §5.2 已就地标注 superseded。**批准理由**：`wiki_page` 当前为空，重编号零成本；有真实数据后同一变更需附带全表回填。
- **D1-5 —— 唯一索引爆炸半径只做「诚实化」（修订单，经人工批准）。** `0061` 新增的唯一索引 `uq_document_catalog_content_hash` 让四个**既有**写入方出现未处理的新失败面（其中一条是客户端可触发的 500）。裁决原文：**「P1 内修，但只让失败诚实」**。因此本轮修的是**失败的报告方式**，不是失败本身：重复上传同一份文件**仍然**会被拒、仍然不会映射到同一份文档。这一点被明确记为边界，不得读作「上传层已去重」。
- **多视角意见。** `code-reviewer`（终审 APPROVE）、`python-reviewer`（Warning）、`security-reviewer`（无 CRITICAL/HIGH）、终审（opus）。

## 3. 数据模型变更

迁移 `backend/alembic/versions/0061_wiki_dedup.py`，`revision = "0061_wiki_dedup"`，`down_revision = "0060_schema_reconcile"`。四步 DDL，全部幂等（`IF NOT EXISTS`），`downgrade` 对称可逆：

| DDL | 方向 | 理由 |
|---|---|---|
| `wiki_page.content_hash VARCHAR(64) NULL` | 可空 | P1 **不回填**；历史行为 NULL 是正常状态 |
| `ix_wiki_page_content_hash` | **非唯一** | 一行 = 一条知识，共享模板合法 |
| `uq_document_catalog_content_hash` | **唯一** | 一行 = 一份源文档（spec §4.7 推迟到 P1 的 D2-2） |
| `wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0` | — | §5.4 台账语义 |

**两处同名索引方向相反，不是笔误。** 若把 `wiki_page.content_hash` 也做成唯一：共享模板的正常写入会 500，且「同 ID 同内容 → 跳过」分支**永远走不到**。`document_catalog` 之所以要唯一，是因为其行与源文档一一对应。

**交接项（P0）。** P0 **不得**重复创建 `uq_document_catalog_content_hash`。原计划设想「P0 写 `document_catalog.content_hash` 时的唯一冲突处理留到 P0 做」——**该前提已被证伪**：P0（`b553196` / `cbe34b9`）是本分支的**祖先**，不是后继。冲突处理已在 P1 内落地（见 §5）。

**备份（按 `Harness/rules/数据库环境使用规范.md` 手动执行）。** 实际路径在**主 checkout** `backups/pg/`（计划里写的 `/tmp/...` 有误），时间 09-12 23:21：

- `backups/pg/wiki_page_20260912.sql`（3,888 B —— 拍到的就是空表）
- `backups/pg/wiki_import_task_20260912.sql`（19,649 B —— 含那 8 行）
- `backups/pg/document_catalog_20260912.sql`（4,215 B）

**drift 校验。** 两个新索引已在 ORM 显式声明（`WikiPage.__table_args__` / `DocumentCatalog`），生成的 DDL 与迁移**逐字节一致**（`Index(unique=True)` 而非 `UniqueConstraint`，因为 0061 建的是 `CREATE UNIQUE INDEX` 不是约束）。drift 检查器不再报这两项；剩余 24 条 `extra_index` 警告是 0061 之前的历史遗留，与本变更无关。

## 4. 接口契约变更

- `POST /api/v1/wiki/import/execute` 响应新增 **`skippedPages`**（纯新增，向后兼容）。
- **语义变更**：重复项由 `failedPages` 改计 `skippedPages`，任务状态由 `FAILED`/`PARTIAL` 改 `SUCCEEDED`。运维看到的失败数不再因重跑虚高。
- `POST /api/v1/wiki/pages` 未提供 `pageId` 时，生成的 ID 由随机后缀改为**内容派生** —— 这是**行为变更**，已由字面量断言钉死（见 §6）。
- 无新增/删除端点，无路径变更。
- 冲突文案修正（不新增端点）：`document_catalog` 的内容重复不再误报为「document_id 已存在」，并新增两条文案 `MSG_DOCUMENT_CONTENT_DUPLICATE` / `MSG_DOCUMENT_CONTENT_EXISTS`。

## 5. 实现要点

- `backend/app/services/wiki_page_service.py` —— `contentHashOf()`、`_identityDigest()`（长度前缀）、`generatePageId(title, sourceRef, content)`。`createPage` 落 `content_hash`；`updatePage` 在 `content` 分支重算哈希、**不动 `page_id`**。
- `backend/app/domain/exceptions.py` —— `DuplicatePageError(ConflictError)`。**catch 顺序必须排在 `ConflictError` 之前**（子类），顺序反了等于没修。
- `backend/app/services/wiki_import_service.py` —— `_importOne` 三分支：同哈希 → `DuplicatePageError`（跳过）；异哈希/NULL → `ConflictError`（计失败）；无既有行 → 创建。跳过分支日志输出 `e.page_id`，跳过可追溯到具体条目。
- 前端 `AdminWikiImportPage` —— 台账新增「跳过」列 + 结果摘要带 `{skipped}`，zh/en 两套 i18n。
- **写入方诚实化（D1-5，本轮新增）：**
  - `document_service.createDocument` / `updateDocument` —— 捕获 `IntegrityError` 并按**约束名**区分「内容重复」与「document_id 重复」，各自给出准确文案；`updateDocument` 此前**完全没有**处理，构成客户端可触发的 500。
  - `wiki_catalog_registrar` —— SELECT-then-INSERT 改为原子 `INSERT ... ON CONFLICT DO NOTHING RETURNING id`，消除 check-then-act 竞态，并保持「同哈希返回既有行」的幂等语义。
  - `rag_service.ingestDocument` —— 冲突预检（`DocumentService.findByContentHash`）移到 `putSourceObject`(MinIO) / `insertDocumentChunks`(Milvus) **之前**；原实现在**写完外部存储之后**才返回 409，留下孤儿对象与向量，且 409 文案里的 `DOC-<uuid>` 是**凭空捏造、从未创建**的 ID。现在文案指向真实存在的冲突文档。
  - **诚实边界（安全复审 L1 / 代码复审 N2）**：上述预检只关闭了**单线程**路径。两路相同字节**并发**上传时双方都能通过预检、双方都写入外部存储，输家才在 PG 撞唯一索引 → 残留一个永不入库的 `DOC-<uuid>` Milvus 分片（会出现在 `searchDocuments` 里、`document_name` 回退为裸 uuid）。**MinIO 侧取决于文件名**：对象名是 `sources/{hash[:2]}/{hash}/{filename}`（内容寻址**加文件名**），故同名无孤儿、**不同名会留孤儿**。**机密性影响为零**（同一份字节）。残留修法：冲突时补偿删除，或把 catalog 行提前到外部写入之前。**本变更不修。**
- 依赖方向：P1 不依赖 P0/P3。

## 6. 测试

**新增/修改的测试**

| 文件 | 内容 |
|---|---|
| `app/tests/unit/test_wiki_page_id.py` | 10 条 —— 确定性、字面公式、中文 slug 折叠、长度上界、哈希口径、**注入性反例** |
| `app/tests/integration/test_wiki_dedup_api.py` | 14 条 —— 迁移 DDL 生效、创建/更新落哈希、重放全跳过、同标题异内容两条、**NULL 哈希撞号必判失败**、台账不变量、跳过不依赖 `page_ids` |
| `app/tests/integration/test_wiki_dedup_p1_writers.py` | 5 条（新文件）—— 四个写入方的诚实失败；其中 1d 断言**拒绝后不残留 MinIO/Milvus 孤儿产物**（仅断言状态码抓不到该缺陷） |
| `app/tests/unit/test_rag_service.py` | +1 条：预检在外部写入前短路 |
| `test_wiki_api.py` / `test_wiki_import_api.py` | 既有测试更新（ID 确定性、语义随 D1-1 变更） |

**实测结果（本轮修复后，真实 PG + 完整 API 链路）**：`test_wiki_page_id` 10 · `test_wiki_dedup_api` 14 · `test_wiki_dedup_p1_writers` 5 · `test_wiki_api` 22 · `test_rag_service` 14 · `test_document_catalog_api` 34 · `test_wiki_import_api` + `test_wiki_import_catalog` 50 · `test_rag_api` 6 · `test_check_schema_drift` 33 · 另 8 个 wiki 集成文件 243 —— **全绿**。

**前端**：`AdminWikiImportPage.test.tsx` vitest 27 passed；`tsc --noEmit` exit 0。

**覆盖率（记录为预存状态，经人工裁决放行）。** 计划给的门禁口径实测 **68.10%**，未达 80%。已核实该门禁**在结构上不可能被本变更影响**：本变更的模块只占分母 19,424 行的 417 行（2.1%），即使 100% 覆盖也拉不动不到 1 个百分点；缺口来自其他 feature 的 0% 覆盖文件。同时 unit 套件有 25 条失败，经 base 对比（`git worktree` 出 `e140dba` 逐文件复跑）确认**全部预存**：4 条 `rag_qa_service` 失败在 base 复现出**相同的 test ID 与相同的报错行**（`TypeError: 'MagicMock' object can't be awaited` @ `rag_qa_service.py:118`），其余为 `FeatureRuleRegistry 未 warmUp` @ `feature_rule_registry.py:98`，且与本次 12 个改动文件**零交集**。裁决原文：**「记录为预存状态，放行本变更，另开变更修覆盖率」** —— 本变更不阻塞，覆盖率缺口另开独立变更处理。

## 7. 安全审查

**触发项**：数据库 DDL / DML；文件系统操作（MinIO）；外部 API（Milvus）。`0061` 属 HITL，须人工 review。

**首轮结论**：无 CRITICAL / HIGH；1 条 MEDIUM（唯一索引让三个写入方出现未处理的失败面）+ 3 条 LOW。MEDIUM 已按 D1-5 在 P1 内处置。

**检查项**：迁移幂等（`IF NOT EXISTS`）与可逆性（`downgrade` 对称）；无 SQL 拼接（全部 ORM 或参数绑定）；错误文案不泄露内部结构。

**修复轮复审**：本目录 `review-security.md`（安全）与 `review-fix-round.md`（终审）

- **结论**：四个修复写法正确；**修复轮未引入新的 CRITICAL / HIGH**。
- 授权链路追查（本次最关心的一项）：`findByContentHash` 与两条冲突路径确实**直接查 `document_catalog`、无 ACL 谓词**，但该表上**本就不存在**可被绕过的调用方数据授权过滤（既有 `GET /api/v1/documents` 已把全部 `documentId` / `contentHash` 交给任意已认证调用方）⇒ 新 409 的**净新增泄露为零，未创造枚举信道**，文案也不回显哈希值。**前瞻风险**：日后给文档读取路径加 ACL 时，必须在同一次变更内一并收紧 `POST /documents`、`PUT /documents/{id}`、`POST /documents/upload`，否则这些 409 会变成绕过口。
- 残留 LOW：L1（摄取 TOCTOU，见 §5 与 §9 限制 7）、L2（约束名分类脆弱但无安全后果，未匹配置重新抛出且不泄露 PG 内部结构）、L3（文案含索引名）、L4（客户端可设置 `content_hash`，可占位；同一主体本就能删任意文档，未增能力）。
- **预存 HIGH（非本变更引入，P1 不修）**：`backend/app/api/v1/documents.py` 的 `/api/v1/documents` 仅由 `getCurrentUser` 守卫，**无任何 ACL 或 owner/department/security_level 过滤** ⇒ 任意已认证调用方可 list / update / delete **全部**文档；且默认 `AUTH_STUB_ENABLED=1` 下**不带任何 header** 的请求被解析为 `roles=("user","admin")`。**须另开变更**。

## 8. 部署验证

**已于 2026-09-13 部署（人工授权后执行）。** 部署前存在一段**危险的中间态**：prod 的 `alembic_version` 早已是 `0061_wiki_dedup`（唯一索引 `uq_document_catalog_content_hash` 已在库上生效），而容器仍跑 P1 之前的代码 —— 旧代码对该唯一索引无任何处理，重复上传会抛未捕获的 `IntegrityError` → 500，且此时 MinIO/Milvus 的副作用已经写下。这正是当时把部署当作优先事项的原因。

| 项 | 状态 |
|---|---|
| prod `qa_metadata` `alembic_version` | `0061_wiki_dedup` ✅ |
| 测试库 `alembic_version` | `0061_wiki_dedup` ✅ |
| 容器内后端代码 | ✅ 已更新（`./scripts/deploy_backend.sh`，快照 `backups/container/20260913_010415`，输出「✅ 启动成功」） |
| 容器内代码保真 | ✅ 5 个文件逐个 md5 host↔容器 一致；容器内 grep 到 `to_bytes(8)`（长度前缀身份编码）与 `uq_document_catalog_content_hash`（约束名分类） |
| `POST /api/v1/wiki/import/execute` 真机冒烟 | ✅ 见下 |
| 前端 bundle 含新 i18n key | ✅ 见下 |

**后端冒烟（真实栈，同一份文件跑两次）**：第 1 次 → HTTP 201、`successPages=1`、`pageIds=["PAGE-P1-94DF31B3"]`；第 2 次 → HTTP 201、`successPages=0`、`skippedPages=1`、`pageIds=[]`，`wiki_page` 仍为 1 行。冒烟数据已清理（删 `PAGE-P1-94DF31B3` 1 行、`source_ref='smoke-p1.md'` 任务 2 行），prod 复原为 `wiki_page=0 / document_catalog=1 / import_task=8`。

**前端部署**：`docker compose -f docker/docker-compose.yml build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend` 后 `up -d frontend`（容器 Recreate）。**对照探针法**验证：服务端口是 **5173**（非 80），`index.html` 引用的 entry chunk `assets/index-BflcEfLu.js` 内两个**只在 P1 之后才存在**的片段均命中 —— `skippedPages`（`types/wikiImport.ts`，P1 新增；基线处 grep 计数 **0**）与 `条，跳过`（zh-CN `resultCounts` 模板的新增片段；基线处 **0**）。两段出现在**被服务的** chunk 里即证明新 bundle 已生效。

> 一处**方法更正**（留痕，勿重蹈）：最初用的探针是 `resultCounts`，**该探针无效**。`git log -S"resultCounts"` 只列出 `e52cf9a`，因为 `-S` 比对的是**出现次数**：`e52cf9a` 引入该 key（0→1），而 P1 的 `3fdc5cd` 只改了它的**值**（加 `跳过 {skipped}`），次数不变故不被 `-S` 捕获。基线处该 key 已存在 ⇒ 「bundle 里有 resultCounts」对旧 bundle 同样成立，**不构成对照**。凡探针必须两侧都验：P1 后存在 **且** P1 前不存在。经 nginx 的 `/api/v1/health` 返回 **200**（已验证 nginx 未因后端 IP 变化而 502）。

部署命令统一走 `./scripts/deploy_backend.sh`（一次灌 `app/` + `scripts/` + `alembic/` + 快照，可 `--rollback`）；前端必须 `docker compose build` 后 `up -d`，裸 `docker build -t` 会因 tag 不匹配而继续跑旧 bundle；compose 文件只在 `docker/` 下，仓库根没有。

## 9. 真实数据验证报告（开发门禁，缺此节即不通过）

**脚本**：`backend/scripts/wiki_dedup_realdata.py`（579 行，已提交 `f5ba422`）

```bash
cd backend && PYTHONPATH=$PWD \
  DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  <main>/backend/.venv/bin/python scripts/wiki_dedup_realdata.py
```

**完整输出已存档**：本目录 `realdata-run.txt`（11 KB，含每一步的 SQL 与逐条 PASS）。结论行：

```
REALDATA RESULT: PASS
```

**写库闸（经审阅确认为真实拦截，非装饰）**：未设 `DATABASE_URL` → 拒绝；非 PostgreSQL → 拒绝；库名 ≠ `qa_metadata_test` → 拒绝（原文：「生产库 qa_metadata 曾发生过整库误删事故，本脚本绝不写入非测试库」）。脚本复用 `app.tests._testapp.buildTestApp` + `httpx.ASGITransport`，**没有第二套 HTTP 客户端**。遵循计划规则：**绝不 TRUNCATE 全库**，只清理自有行。

**8 次导入的三元组（逐次实测）**

| 次 | 状态 | success | skipped | failed |
|---|---|---|---|---|
| 1 | SUCCEEDED | 75 | 0 | 0 |
| 2–8 | SUCCEEDED | 0 | 75 | 0 |

其余步骤：同 `page_id` 异内容 → `FAILED` / failed=1 / skipped=0；同 `page_id` 同内容 → `SUCCEEDED` / skipped=1；删光台账后重放 → success=0 / skipped=75，自有行数仍 75。

**真实 SQL 与计数**：`count(*) FROM wiki_page` = 75；`count(DISTINCT title)` = 75；`count(*) WHERE content_hash IS NULL` = 0。

**真实数据暴露的问题与修复。** 门禁第一次运行以 `REALDATA RESULT: FAIL` 结束，但**失败在脚本、不在产品代码**：脚本按**全库绝对值**断言 `count(*) = 75`，而 `qa_metadata_test` 是共享测试库、长期驻留 2 行外来数据（实测 total=77、distinct title=76），绝对值永远不可能匹配。8 次导入循环的行为本身**完全正确**。修复：断言收窄到脚本自有行（`WHERE page_id = ANY(:ids)`），全局计数仍查询并**仅作上下文打印**。修复后复跑 PASS。

**已知限制（不粉饰）**
1. **`source_ref` 参与身份派生** → 首次导入留空、二次填文件名会产生**两条**。已由 `test_same_content_different_source_ref_is_two_entries` 钉死。缓解：向导层要求必填 `sourceRef`，或后续把来源归一化为上传文件的内容哈希。
2. **M4 边界（跨变更）**：在 `0061` 之前就已入库的行 `content_hash IS NULL`。P1 **不回填**，因此一个历史文件在 P1 之后**第一次重导不会跳过，而是计冲突失败**，需人工处置。这是 D1-1「内容不可判定时宁可报错」的刻意方向，但升级后第一次重导会看到失败数上升 —— 运维需知悉。
3. **纯中文标题 slug 折叠为 `UNTITLED`**，可读性净损失（唯一性由哈希兜底）。
4. **`[:8]` 32 bit 截断**：碰撞概率极低，后果是**另一份文档被判冲突计失败**，不是静默丢弃 —— 刻意的失败方向。
5. **同一份文件重复上传仍会产生第二条 `document_catalog` 尝试并被拒**（D1-5 边界）。上传层去重**不在本次范围**。
6. **备份 cron 仍静默失效**，本次备份为手动执行；需要备份时直接跑 `./scripts/backup_pg.sh`。
7. **并发摄取仍会留孤儿（安全复审 L1 / 代码复审 N2，未修）**：相同字节并发上传时预检双双通过，输家在 PG 撞唯一索引后才失败，其 Milvus 分片留存并会出现在检索结果里（`document_name` 回退为裸 uuid）；MinIO 仅**文件名不同**时留孤儿。机密性影响为零，见 §5 与 §7。

**本变更不修、已登记待办的独立问题（不得因合并本变更而遗忘）**
- 覆盖率门禁 68.10% 与 unit 套件 25 条预存失败 —— 按裁决另开变更。
- `/api/v1/documents` 无 ACL（预存 HIGH）—— 另开变更。
- 摄入并发孤儿（L1）—— 可选补偿删除或先占 catalog 行。

## 9'. 关联

- 设计稿：`docs/superpowers/specs/2026-09-12-wiki-knowledge-layer-design.md` §五（§5.2 公式已标注 superseded）
- 计划：`docs/superpowers/plans/2026-09-12-wiki-dedup-p1.md`
- 评审记录：本目录 `review-fix-round.md`、`review-security.md`、`realdata-run.txt`；过程台账在 `.superpowers/sdd/2026-09-12-wiki-dedup-p1/progress.md`（**gitignored 临时工作区**，不作为长期 SSOT —— 需长期留存的证据已复制到本目录）
- 规则：`Harness/rules/数据库环境使用规范.md`、`Harness/rules/测试规范.md`、`Harness/rules/开发流程规范.md`
- 前置变更：`Harness/changes/feat-wiki-knowledge/summary.md`（本次修复其遗留的副本问题）
