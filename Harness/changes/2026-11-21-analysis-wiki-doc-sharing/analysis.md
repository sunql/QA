# 分析：wiki 上传的资料 与 文档中心上传的资料 —— 能否共享？原文能否阅览？

- 日期：2026-11-21　分支：`analysis/wiki-document-sharing`（**纯分析，无代码改动**）
- 方法：读代码 + 查生产形态的 dev 库（PG / Milvus / MinIO）+ 真实 HTTP 探测
- 结论一句话：**「登记」共享、「检索」不共享、「原文」两边都不能阅览。**

---

## 一、两条链路的事实

| 环节 | Wiki 导入（`/api/v1/wiki/import/preview-file`） | 文档中心（`/api/v1/documents/upload`） |
|---|---|---|
| 解析 | `parse_document`（同款） | `parse_document`（同款） |
| 源文件留存 | ✅ MinIO **内容寻址**（`buildSourceObjectName`） | ✅ MinIO **内容寻址**（同一个函数） |
| `document_catalog` 登记 | ✅ `upsertByContentHash`（`ON CONFLICT DO NOTHING`） | ✅ `createDocument` |
| 文档编号 | `DOC-WIKI-<hash前12位>` | `DOC-<uuid前12位>` |
| `document_type` | **固定 `OTHER`**（不分类） | 用户可选（CONTRACT / PURCHASE_SPEC …） |
| **Milvus 向量** | ❌ **完全不生成 chunk/embedding** | ✅ chunk + embedding 入 `document_embeddings` |
| 密级 | ❌ 无（catalog 默认 L1） | ✅ 上传时指定 L1/L2/L3 |
| 生效日期 | ❌ 无 | ✅ 可填 |
| 重复上传同一文件 | 幂等（同 hash 复用同一 catalog 行 + 同一对象） | **409 已存在**（`content_hash` 唯一索引） |

**关键代码依据**：
- 内容寻址共用：`app/infrastructure/object_storage.py::buildSourceObjectName/hashContent/putSourceObject`
- wiki 侧落库：`app/services/wiki_import_service.py::persistSourceFile` → `app/services/wiki_catalog_registrar.py::upsertByContentHash`
- 文档中心侧：`app/services/rag_service.py::ingestDocument`（第 4 步 embedding、第 5 步 Milvus）
- wiki 侧**无**对应步骤 —— `wiki_import_service.py` 全文不含 chunk/embedding/Milvus

## 二、问题 1：能否共享？

### 2.1 ✅ 共享的部分：**登记层共享同一张表**

两条链路都写 `document_catalog`，都存 MinIO **内容寻址**的同一份字节。所以：

- **文件实体共享**：同一份文件从任一边上传，MinIO 里只有**一份**对象（内容寻址天然去重）。
- **目录可见**：wiki 导入的文件**会出现在文档中心列表里**。实测（`GET /api/v1/documents`）：
  ```
  DOC-WIKI-3E0C1374E234 | 采购域业务流程对象.xlsx | OTHER
  DOC-WIKI-F9F24A0FA8B5 | 采购指标开发V1.4.xlsx    | OTHER
  DOC-WIKI-DD51544F3ED0 | GBZ+253-2026.pdf         | OTHER
  DOC-7DF4B953ADD1      | sample_policy.txt        | PURCHASE_SPEC
  DOC-203B7FEB25B4      | 采购与供应商控制程序.pdf     | OTHER
  DOC-1A6D70F78F58      | TH-MDM-01-…V1.0.docx     | OTHER
  ```
- **反向也共享**：wiki 侧 `upsertByContentHash` 遇同 hash 会**复用**已有行 ⇒ 若文档中心先上传过同一文件，wiki 导入时**不会**产生第二行（但也不会把它升级成 CONTRACT/PURCHASE_SPEC 类型）。

### 2.2 ❌ 不共享的部分：**检索层完全不通**

`document_catalog` 只是**目录**；真正被 RAG 检索到，靠的是 **Milvus `document_embeddings` 里的 chunk**。

实测 Milvus `document_embeddings`（144 行）按文档分组：

| document_id | chunk 数 | 来源 |
|---|---:|---|
| `DOC-203B7FEB25B4`（采购与供应商控制程序.pdf） | 84 | 文档中心 |
| `DOC-1A6D70F78F58`（TH-MDM-01…docx） | 28 | 文档中心 |
| `DOC-7DF4B953ADD1`（sample_policy.txt） | 1 | 文档中心 |
| `DOC-WIKI-3E0C1374E234` | **0** | wiki 导入 |
| `DOC-WIKI-F9F24A0FA8B5` | **0** | wiki 导入 |
| `DOC-WIKI-DD51544F3ED0` | **0** | wiki 导入 |

⇒ **wiki 导入的资料在采购知识检索 / RAG / Copilot 里完全查不到**（实测
`POST /procurement/knowledge/documents/search` 只返回 `DOC-203B7FEB25B4` 的证据）。

### 2.3 为什么两边行为不一样（不是 bug，是设计差异，但留下了坑）

wiki 导入的产物是**知识条目**（`wiki_page` + `knowledge_claim`），走 LLM 抽取后进
`wiki_page_embeddings`（Wiki Chat 的检索面）。文档中心的产物是**原始文档 chunk**，
走 `document_embeddings`（采购知识检索面）。

两条检索面**互不相通**。于是：

- wiki 导入的文件在「文档中心」里**看得见但搜不着**（有 catalog 行、无向量）；
- 采购 Copilot 只能看到文档中心上传的那部分；
- 用户视角 = 「我明明上传了，为什么问不出来」。

### 2.4 若要真正共享，需要做什么（**未实施，仅列**）

| 方案 | 做法 | 代价 / 风险 |
|---|---|---|
| **A. 共享目录（现状）** | 保持现状，两边都能看见彼此的 catalog 行 | 检索面不通 ⇒ 体验割裂（当前） |
| **B. 共享检索** | wiki 导入时**也生成 chunk+向量**，入 `document_embeddings`（或统一到一张向量表） | 要跑 embedding（花钱/耗时）；**密级闸**：wiki 侧目前没有密级概念，向量行的 `security_level` 只能填 L1 ⇒ 制度类 L3 文档经 wiki 侧上传会**降密**（安全风险） |
| **C. 共享原文 + 双向可见** | 统一两条上传的**元数据口径**（类型/密级/生效日），让 wiki 侧也能声明类型与密级 | 需要改 wiki 导入的表单与 catalog 登记（`document_type` 现在写死 OTHER） |

**我的建议**：**C + B 的组合，且顺序是 C → B**。理由：
1. 先把 wiki 侧补上 `document_type` / `securityLevel` / `effectiveDate` 三个字段（C），
   否则向量化的密级闸无从谈起（会静默降密）；
2. 再做 wiki 侧向量化（B），让「文档中心上传」与「wiki 上传」在检索面等价；
3. **但要注意重复**：同一份文件先在文档中心上传过、再经 wiki 导入，
   会在两个向量表里各有一份 chunk ⇒ 检索可能**重复命中**。需要用
   `content_hash` 做跨表去重键（`document_embeddings` 目前只有 `document_id`，
   **没有** `content_hash` 列 ⇒ 去重需要先加列或做应用层判定）。

## 三、问题 2：上传的文件能否原文阅览？

### 3.1 事实：**原文一直都在，但没有任何出口**

- **MinIO 里确实存着原件**（实测 `qa-objects` 容器）：
  ```
  /data/qa-knowledge-sources/sources/00/0091f4cad386b3ee23d4f16d40fa9f13a107103139ba8049e37f4d7e04b69cf2/采购与供应商控制程序v1.pdf/
  /data/qa-knowledge-sources/sources/07/07c89035ff9a74e3a34d8c6ed1c1718693f1bdaea376258ea7cbb51bc4c8ee46/P5.1供应商管理程序采购控制程序260627.xls/
  ```
- **后端有读回能力**：`app/infrastructure/object_storage.py::getSourceObject` ✅
- **但它零调用方**：`grep -rn "getSourceObject" app/` 只命中定义本身 ⇒ **死代码**。
- **没有 HTTP 出口**：文档路由只有 list/get/create/update/delete/relations/upload/search/qa，
  **没有** `download` / `preview` / `content` 端点；
  wiki 侧同样只有 `preview-file`（**只回解析后的文本**，不回原件）。
- **前端没有入口**：`storageUrl` 只在 `types/document.ts` 里声明过，
  `DocumentsPage.tsx` 的表格**没有** storageUrl 列、**没有**预览/下载按钮；
  `ProcurementKnowledgePage` 同理。
- **前端拿到的 `storageUrl` 也没用**：`s3://qa-knowledge-sources/sources/...`
  是**内部对象存储 URI**，浏览器打不开（MinIO 不对外暴露这个 schema）。

### 3.2 结论

| 问题 | 答案 |
|---|---|
| 原文有没有存？ | ✅ 存了（两条链路都存，内容寻址去重） |
| 界面上能点开看原件吗？ | ❌ **不能**（无后端端点 + 无前端入口） |
| 只能看到抽取后的文本/片段吗？ | ✅ 只能看到：wiki 侧是解析回填的纯文本；文档中心侧是 RAG 检索返回的 chunk 片段 |
| PDF 的表格/版式能还原吗？ | ❌ 不能 —— 抽取是**有损**的（`parse_document` 只取文本 + 定位符） |

### 3.3 若要做「原文阅览」（**未实施，仅列**）

最小可用路径（**不含** presigned URL 那种「把 MinIO 暴露出去」的做法）：

1. **后端加一个端点**：`GET /api/v1/documents/{id}/source`（或 `/content`）
   - 从 `document_catalog.storage_url` 解析对象名 → `getSourceObject` 读回字节；
   - **必须**先做 ACL：密级闸（`buildClearanceFilter`）+ 归属/部门；
   - 返回原始字节 + `Content-Type`（按 catalog 或上传时记录的类型）+ `Content-Disposition: inline; filename=…`；
   - PDF 用 `inline` 让浏览器内嵌预览，Office 类用 `attachment` 下载。
2. **前端加入口**：文档列表加一列「原文」→ 新窗口打开该端点；
   采购知识检索的证据卡片加「查看原文片段出处」链接。
3. **两个必须先解决的问题**：
   - **`storage_url` 里没有 MIME 类型**（只有 `s3://bucket/objectName`）⇒ 响应类型
     要么落库（catalog 加列），要么按扩展名推断（`.pdf`/`.docx`/`.xlsx`/`.txt`…）；
   - **`storage_url` 的对象名带中文原名**（`…/采购与供应商控制程序v1.pdf`）⇒
     HTTP 头里的 `filename` 要正确编码（RFC 5987 `filename*=UTF-8''…`），
     否则中文文件名下载下来是乱码。

## 四、给决策者的一句话

- **「能否共享」**：登记层**已经共享**（同一张表 + 同一份对象），检索层**没有共享**
  （wiki 侧不生成向量）⇒ 现状是「看得见、搜不着」。
- **「能否原文阅览」**：原文**一直在对象存储里**，但**没有出口**（后端读回函数是死代码，
  前端无入口）⇒ 现状是「存着、看不了」。
- 这两个问题**都不需要动数据结构**就能做（除了向量化去重可能要给
  `document_embeddings` 加 `content_hash`），但都要**先补 ACL/密级**，
  否则等于给文档开后门。

## 五、本次分析的边界（诚实交代）

- 只在 **dev 栈**验证（数据量小、密级全是 L1）；生产有 L2/L3 文档与
  `restricted_access` 用户 ⇒ 原文阅览的 ACL 路径**必须**在生产再验一次。
- `wiki_import_service` 的 `document_type` 写死 `OTHER` 是**代码事实**；
  但线上是否有人手工改过 catalog 行的类型（UPDATE）未查。
- 未验证 `wiki_page_embeddings` 里 wiki 侧 chunk 的具体字段与可检索性
  （与本问题无关，只确认「wiki 内容走 Wiki Chat 检索面」这一分工）。
