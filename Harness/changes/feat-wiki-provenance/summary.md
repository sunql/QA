# 变更：feat-wiki-provenance（P0 溯源地基）

- **日期**：2026-09-12
- **作者**：启琳（Claude Code）
- **Phase**：P0 — 源文件留存 + 溯源定位符（P0/P1/P3 三计划的第一份）
- **状态**：done

## 1. 需求

P0 溯源地基，让知识证据链能回指原始文件。此前上传的知识源文件不留底：
`document_catalog.storage_url` 写的是 `milvus://N_chunks` 假 URL、`content_hash` 恒为 NULL，
Milvus chunk 也没有页码/章节/段号定位符。本变更落地三件事：

1. **源文件留存**：上传的源文件按内容寻址写入 MinIO（`app/infrastructure/object_storage.py`）。
2. **真实溯源字段**：`document_catalog` 记真实 `s3://` `storage_url` + 64 位 sha256 `content_hash`。
3. **定位符落库**：Milvus chunk 带 `page_number` / `section_name` / `paragraph_no`。

验收信号（spec §十）：Milvus chunk 带正确 `page_number`；catalog 有真实 `storage_url` + 非空
`content_hash`；MinIO 桶内可回读且字节与上传一致；wiki 上传路径同样留存源文件且 hash 取**文件字节**；
同一文件重复上传不产生第二行 catalog；被拒文件不留痕。全部由本文件 §9 的真实数据验证逐项钉死。

## 2. 设计评审

| 决策 | 结论 | 影响 |
|---|---|---|
| 部署方式 | 新增 `minio` 依赖，`scripts/deploy_backend.sh` 只 `docker cp` app/+scripts/+alembic/ 不装依赖 → **必须完整镜像重建**，不能走 deploy_backend.sh | 见 §8 |
| wiki 落库点 | 用户选定「**preview-file 就落库**」（Task 6）：字节只在 `preview-file` 手里，`execute` 接触不到文件字节；`content_hash`/`storage_url` 全由服务端计算，`execute` 不携带任何溯源字段 | spec §4.1 C5 修正，见 §10 |
| MinIO 故障 | 显式 503，不得降级为「预览成功但其实没存」 | 预览可用性从此与 MinIO 绑定（接受的代价） |
| 摘要口径 | 以 P0 的 `object_storage.hashContent(content: bytes)` 为唯一实现；P1 的 `contentHashOf` 落地时退化为薄包装 | 两份 sha256 会各自漂移 |
| Milvus 集合 | 重建不可逆，非空重建需 `ALLOW_NONEMPTY_REBUILD=1`（人类裁决） | 见 §10 第 4 条 |

## 3. 数据模型变更

- **零 PG 迁移**（spec D2-2）。alembic head 保持 `0060_schema_reconcile`（部署后已复核，见 §8）。
- `document_catalog.storage_url` / `content_hash` 列已存在，本变更只**写入真实值**；`content_hash`
  唯一约束推迟到 P1（`0061_wiki_dedup` 建 `uq_document_catalog_content_hash`）。
- Milvus `document_embeddings` 集合 schema 由 8 字段扩为 11 字段（+`page_number` INT64 /
  `section_name` VARCHAR 200 / `paragraph_no` INT64），缺失值写哨兵 `-1` / `""`（Task 3）。

## 4. 接口契约变更

- `RagService.ingestDocument(..., *, actor: CurrentUser) -> dict`：返回值增 `storage_url` / `content_hash`；
  MinIO 不可用显式抛 `RagError`（不再静默降级）。
- `app/infrastructure/object_storage.py`（新）：`DEFAULT_BUCKET="qa-knowledge-sources"`、
  `hashContent(bytes)->str`、`buildSourceObjectName`、`ensureBucket`、
  `putSourceObject(...)->str`（返回 `s3://<bucket>/<objectName>`）、`getSourceObject(...)->bytes`（同步）。
- `app/infrastructure/milvus_client.py`：`deleteDocumentChunks(documentId)` / `queryDocumentChunks(documentId)`（同步）。
- `app/services/wiki_catalog_registrar.py`（新）：`WikiCatalogRegistrar.upsertByContentHash`
  按 `content_hash` 幂等登记，`document_id=DOC-WIKI-<hash[:12].upper()>`、`document_type=OTHER`。
- `POST /wiki/import/preview-file` 契约改写：原「不落库」→「**落对象存储与文档目录**，不调模型」。

## 5. 实现要点

关键文件：

- `backend/app/infrastructure/object_storage.py`（Task 4）— 内容寻址对象名
  `sources/<hash[:2]>/<hash>/<name>`，文件名净化防路径穿越；配置缺失即抛 `ObjectStorageError`，不降级。
- `backend/app/infrastructure/milvus_client.py`（Task 3）— `_documentFields()` 11 字段 + `_locatorInt()`
  哨兵写入 + `deleteDocumentChunks` / `queryDocumentChunks`。
- `backend/app/services/rag_service.py`（Task 5）— ingest 流程重排：解析 → **源文件留存** → 分块 →
  embedding → Milvus → catalog；删除两处静默吞异常的死代码。
- `backend/app/services/wiki_import_service.py` + `wiki_catalog_registrar.py`（Task 6）—
  `persistSourceFile` 在解析成功后留存 + 登记；owner 由 `actor.departments[0]` 派生。
- `backend/scripts/rebuild_document_collection.py`（Task 3）— drop + 以 11 字段重建，非空需
  `ALLOW_NONEMPTY_REBUILD=1`。
- `backend/scripts/wiki_provenance_realdata.py`（本任务 Step 1）— 真实数据门禁脚本，走真实
  PDF → parse → chunk → embed → Milvus → catalog → MinIO 全链路，末端逐项断言，跑完自清。

依赖：新增 `minio`（实机 7.2.20）、`reportlab`（5.0.0，门禁脚本生成样例 PDF 用）。

## 6. 测试

- Task 1-6 各自 TDD（RED→GREEN），集成测试走真实 PostgreSQL 5433 + 真实 Milvus，
  MinIO 客户端用假实现（`_FakeMinio` / `fakeMinio` fixture），真实 MinIO 端到端由 §9 门禁承担。
- Task 6：`test_wiki_import_catalog.py` 7 passed（留存+hash 取文件字节 / 响应契约不变 /
  同内容幂等 / 不同内容两行 / 被拒不留痕 / owner 派生 ×2）。
- 本任务（Task 7）新增 `backend/scripts/wiki_provenance_realdata.py`：非 pytest，真实链路门禁，
  结果见 §9。

## 7. 安全审查

- 未触发 security-reviewer：无鉴权/支付/敏感数据逻辑改动。MinIO 凭据走 `docker/.env`
  （gitignored）与容器 env，未提交任何凭据。
- 对象名净化（`object_storage._UNSAFE_NAME_RE`）阻断路径分隔符与 `..` 越权；对象名按
  `sources/<hash[:2]>/<hash>/<name>` 内容寻址。
- 本任务未触碰任何鉴权路径；门禁脚本仅以 `actor=CurrentUser(userId="provenance-gate")` 走一次
  真实 ingest，写入后自清。

## 8. 部署验证

完整镜像重建（非 deploy_backend.sh）+ 起 `qa-objects` + 重建集合，冒烟如下（真实输出）：

```
$ docker tag qa-system-backend:latest qa-system-backend:pre-p0
$ docker image inspect qa-system-backend:pre-p0 --format '{{.Id}}'
sha256:aa002414018236b3ce7e91942cfd6344c991a53ef24cfc2183b9e6957cb07f4e

$ docker compose -f docker/docker-compose.yml build backend
 Image qa-system-backend Built

$ docker compose -f docker/docker-compose.yml up -d qa-objects backend
 Image minio/minio:RELEASE.2024-06-13T22-53-53Z Pulled
 Volume qa-system_objects_data Created
 Container qa-objects Started
 Container qa-backend Recreated
 Container qa-backend Started

$ docker exec qa-backend python -c "import minio; print('minio ok')"
minio ok

$ docker exec qa-backend alembic current
0060_schema_reconcile (head)
```

- `qa-objects` 健康（healthcheck `mc ready local` 通过）；`qa-backend` 健康。
- alembic head 复核 = `0060_schema_reconcile`，**未发生迁移移动**（P0 零迁移）。
- 回滚路径（**仅镜像**）：`qa-system-backend:pre-p0` = 重建前的旧镜像（`aa0024140182`），
  `docker tag qa-system-backend:pre-p0 qa-system-backend:latest && docker compose ... up -d backend`
  可回滚**代码**；P0 无 PG 迁移，`document_catalog` 列结构不变。
- **集合 schema 无法回滚**：Milvus `document_embeddings` 已被 drop + 重建为 11 字段，
  旧镜像只认 8 字段 schema。镜像回滚后旧代码对 11 字段集合的 insert/query 会因字段
  不匹配抛 Milvus `DataNotMatchException`（已实测），**上传在镜像回滚后即坏**。要恢复
  上传必须同时用 `rebuild_document_collection.py` 把集合重建成旧 8 字段（同样丢弃现有
  实体、同样不可逆），或干脆只滚代码不滚集合。

## 9. 真实数据验证报告（2026-09-12）

**验证脚本**：`backend/scripts/wiki_provenance_realdata.py`。走真实摄入链路（真实 PDF → parse →
chunk → embed → Milvus → catalog → MinIO），在链路末端逐项断言溯源信息真的留下了，跑完自清。

### 9.1 集合重建（Step 2，真实输出）

```
$ docker exec -e ALLOW_NONEMPTY_REBUILD=1 qa-backend python scripts/rebuild_document_collection.py
已删除旧集合 document_embeddings（8 行）
已重建集合 document_embeddings
```

（说明：集合在 Task 3 已由宿主机重建为 11 字段；本任务部署时集合里只剩 8 行**测试残留**
——`DOC-P0-TEST` ×2 + 6 行 `DOC-<12hex>` 单 chunk-0，已逐一核对 `document_catalog` **零行对应**，
即全部是孤儿测试垃圾，非真实数据。重建把它们清掉并重建成同构 11 字段集合。）

### 9.2 门禁脚本（Step 3，真实输出）

脚本自身的 print 输出逐字如下（容器 SQLAlchemy engine 的 INFO echo 日志与 PyMilvus
弃用告警被省略，未改动脚本输出内容）。**此处的运行是最终交付版脚本**（含后续加固的
`catalog.content_hash` 与计算值/对象名 hash 段比对断言）：

```
ingest.chunks       = 2
ingest.storage_url  = s3://qa-knowledge-sources/sources/f3/f319fbad1c1aff8fef9df785c1b5316b8c29cd54b746a7501e975ecda737e9e5/provenance-sample.pdf
ingest.content_hash = f319fbad1c1aff8fef9df785c1b5316b8c29cd54b746a7501e975ecda737e9e5
milvus.chunk_count  = 2
milvus.page_numbers = [1, 2]
catalog.storage_url = s3://qa-knowledge-sources/sources/f3/f319fbad1c1aff8fef9df785c1b5316b8c29cd54b746a7501e975ecda737e9e5/provenance-sample.pdf
catalog.content_hash= f319fbad1c1aff8fef9df785c1b5316b8c29cd54b746a7501e975ecda737e9e5
minio.read_back     = 2916 bytes（与上传一致）

✅ P0 溯源地基真实数据验证通过（已清理本次写入）
```

> **`content_hash` 跨次运行会变，这不是缺陷**：样本 PDF 由 reportlab 生成，而 reportlab 会把
> `CreationDate` 时间戳写进 PDF，故每次生成的**字节**都不同，sha256 随之不同（另一次运行得到
> `7d04300b…`）。所以门禁断言的是**同一运行内**的一致性 —— `catalog.content_hash` 等于本次
> `ingest.content_hash`、也等于对象名中的 hash 段 —— 而不是跨运行的稳定值。若要跨运行稳定，
> 需要让样本 PDF 字节确定化（例如固定 CreationDate），这不在 P0 范围内。

### 9.3 自清复核（真实输出）

```
$ docker exec qa-postgres psql -U qa_user -d qa_metadata \
    -c "SELECT document_id FROM document_catalog WHERE document_id = 'DOC-P0-PROVENANCE-GATE';"
 document_id
-------------
(0 rows)

$ docker exec qa-backend python -c "... queryDocumentChunks('DOC-P0-PROVENANCE-GATE') ..."
chunks = data: [], extra_info: {}
```

### 9.4 结论

- ✅ Milvus chunk 页集合**恰好**为 `[1, 2]`（两页正文各成 chunk，非哨兵 -1）；`paragraph_no` 非哨兵；
  PDF 的 `section_name` 全为空串（哨兵，符合契约）。
- ✅ catalog `storage_url` 为真实 `s3://qa-knowledge-sources/...`（非 `milvus://` 假 URL）。
- ✅ catalog `content_hash` 为 64 位十六进制 sha256，且**与本次 `ingest.content_hash` 及对象名中的
  hash 段三者一致**（初版门禁只验 64 位形状、不比对取值，已加固 —— 否则日后若有人改成对**文本**
  而非文件字节求 hash，门禁仍会全绿，而那正是本变更要钉死的口径）。
- ✅ MinIO 读回 2916 字节与上传字节逐字节一致。
- ✅ 门禁写入在正式库与共享集合零残留。

## 10. 已知缺口

1. **备份门禁缺口**：PG 备份 cron 已确认静默失效（launchd 契约断裂，根因是 `/etc/crontab` 缺失），
   用户 2026-09-12 决定维持手动；`scripts/backup_objects.sh` 同样以手动形态交付。此缺口不粉饰。
2. **spec §4.1 C5 修正**：原「Wiki 导入路径也注册 document_catalog」暗示挂在 `execute`，
   但 `execute` 只接触客户端回传草稿文本、拿不到文件字节。用户选定「preview-file 就落库」，
   `execute` 不携带溯源字段（详见 Task 6 brief 的「设计决定」段）。
3. **部署方式变更**：本变更不能走 `scripts/deploy_backend.sh`（不装依赖），必须完整镜像重建。
4. **Milvus 集合重建不可逆**：首次重建发生在 Task 3（commit `95bcc18`），丢弃旧 8 字段集合的
   **333 条** entity，已于 2026-09-12 19:53 导出快照到
   `backups/milvus/document_embeddings_20260912_1953.json.gz`（`num_entities: 333`）。快照只存
   标量字段与向量本身，**恢复需另写导入脚本**，故「有快照」≠「可一键回滚」。本任务部署时再清
   8 条测试残留（见 §9.1）。**此条如实记录，不得写成「无数据损失」**。MinIO 里的源文件不受回滚影响。
5. **Task 6 遗留 finding（推迟到 P1）**：`WikiCatalogRegistrar.upsertByContentHash` 用
   SELECT-then-INSERT，**无数据库级唯一约束兜底**。`document_catalog.content_hash` 无唯一索引
   （`uq_document_id` 是唯一约束，今天恰好覆盖同一面，因 `document_id` 由 `content_hash` 派生——
   但那是**偶然耦合**，非强制不变量）。同一字节的两个并发上传可都通过 SELECT 再各自 INSERT。
   修复需 PG 迁移（`uq_document_catalog_content_hash`），而 P0 是零迁移计划，故推迟到 **P1**，
   其 `0061_wiki_dedup` 迁移恰好建该索引。P0 不改代码、不加索引。
6. **跨页 chunk 定位符缺陷（✅ 已修复，fix round 1）**：初版门禁在两页样本上打印
   `chunk_count = 1` / `page_numbers = [1]` 却照样通过 —— 暴露了 `split_by_paragraphs` 的跨页合并
   缺陷：chunk 正文含第 2 页文字却被标成 `page_number: 1`，P3 的 `evidence` 引用会指错页。
   修复：`chunk_splitter.py` 累积时遇页码不同的块先 flush 再另起（chunk 不跨页；`page_number` 为
   `None` 的 DOCX/MD 不切页）；门禁改断言页集合**恰好** `[1, 2]` + PDF 的 `section_name` 为空串。
   修复后门禁打印 `chunk_count = 2` / `page_numbers = [1, 2]`（见 §9.2）。
7. **DOCX 中文数字样式标题不识别（已知限制，不在 P0 处理）**：中文版 Word 用数字样式
   ID（`1`/`2`/`3` 对应标题 1/2/3），`document_parser._isHeadingParagraph` 只认
   `Heading<n>` 形式，这类文档的 `section_name` 退化为 None（页码与段号不受影响）。

## 11. 关联

- 计划：`.superpowers/sdd/2026-09-12-wiki-provenance-p0/`（task-1~7 brief/report + progress）
- 姊妹计划：P1 去重（`0061_wiki_dedup`）、P3 编译层（`claim`/`evidence` 写入）
- 规则：`Harness/rules/开发流程规范.md`（每轮真实数据验证门禁）、`Harness/rules/数据库环境使用规范.md`
- Wiki：`Harness/wiki/data-model.md`（`document_catalog`）、`Harness/wiki/operations-runbook.md`（端口契约与部署）
