# 设计：文档统一入库 + 原文阅览（需求 1–4）

- 日期：2026-11-21　分支：`feat/document-unified-ingest`（从 `analysis/wiki-document-sharing` 续）
- 前置分析：`Harness/changes/2026-11-21-analysis-wiki-doc-sharing/analysis.md`
- 需求：①wiki 导入的原文件也入文档中心并可看原文；②文档中心上传的文件也可看原文；
  ③wiki 导入的文件可**再次向量化**进 `document_embeddings`（或思考共用一张向量表）；
  ④**通用**的原文件查看方式（excel/word/ppt/pdf/md/html…）

---

## 一、总体结论（先给答案）

| # | 需求 | 方案 | 关键取舍 |
|---|---|---|---|
| 1 | wiki 原文件入文档中心 + 可看原文 | **已入（catalog 层）**，补「可看原文」出口 | 现状已共享 catalog，缺的是出口 |
| 2 | 文档中心文件可看原文 | 加 `GET /documents/{id}/source`（原文字节）+ 通用预览 | — |
| 3 | wiki 文件再次向量化 | **加「向量化」动作**（不新建向量表） | **不建议合并两张向量表**（见 §三） |
| 4 | 通用查看方式 | **服务端统一渲染为 HTML 预览**（`/documents/{id}/preview`） | 不依赖前端 PDF/Office 渲染库 |

## 二、需求 1 & 2：原文阅览

### 2.1 数据准备：catalog 缺两个字段

现在 `storage_url` = `s3://bucket/sources/xx/<hash>/<原文件名>`，**不带 MIME 类型**。
原文阅览要返回正确的 `Content-Type`，且要能判断「能不能浏览器直接看」。

**新增两列**（迁移 0163）：
| 列 | 类型 | 说明 |
|---|---|---|
| `mime_type` | `String(120)` | 上传时的 MIME（wiki 侧也要记）；空则按扩展名推断 |
| `file_size` | `BigInteger` | 字节数（前端显示体积、下载进度） |
| `preview_kind` | `String(20)` | 预渲染类别：`TEXT` / `MARKDOWN` / `HTML` / `PDF` / `OFFICE` / `IMAGE` |

> `preview_kind` 落库而不是每次推断：推断规则会变，落库后**已入库文档的呈现不变**
> （与「catalog 是治理事实源」一致）。

### 2.2 两个端点

```
GET /api/v1/documents/{id}/source      → 原始字节（inline / attachment）
GET /api/v1/documents/{id}/preview     → 统一 HTML 预览（需求 4 的落点）
```

**`/source`**：
- 从 `storage_url` 解析对象名 → `object_storage.getSourceObject`（**这个函数目前是死代码，本设计把它接上**）
- `Content-Type`：catalog.`mime_type` → 缺失则按扩展名推断
- `Content-Disposition`：`inline`（可看）或 `attachment`（下载）
- **ACL 先行**：`DocumentService.getDocument` 已经做密级闸（403）⇒ 直接复用它取行，
  **不许**绕过它自己查库

**中文文件名坑**：`storage_url` 的对象名带原名（`采购与供应商控制程序v1.pdf`）⇒
`Content-Disposition` 必须用 RFC 5987（`filename*=UTF-8''…`），否则下载文件名乱码。

### 2.3 ACL 清单（一个都不能少）

| 维度 | 规则 | 复用 |
|---|---|---|
| 登录 | 必须登录 | `getCurrentUser` |
| 密级 | `security_level` 必须在用户 clearance 内 | `buildClearanceFilter`（**与检索同一套**） |
| 归属 | `owner`（部门）与用户部门匹配，或 admin | 参照 `DocumentService` 现有口径 |
| L3 | `compliance_officer` / `l3_acl` 才能看 L3 | `buildClearanceFilter` 已含 |

⚠️ **原文阅览比检索更敏感**：检索只回 chunk 片段，原文是整份文件。
⇒ 必须**先过 ACL 再读字节**，且**错误语义要区分**：
403（无密级权限）/ 404（不存在）/ 503（对象存储不可用）。

## 三、需求 3：向量化与「共用向量表」的权衡

### 3.1 先看两张表为什么**不该**合并

| | `wiki_page_embeddings` | `document_embeddings` |
|---|---|---|
| 行数 | 65 | 144 |
| 主键维度 | `page_id`（知识条目） | `document_id`（源文件） |
| 特有字段 | `title` / `dimension` / `status` | `effective_date` / `security_level` / `page_number` / `section_name` / `paragraph_no` |
| 检索面 | Wiki Chat（知识问答） | 采购知识检索 / Copilot RAG 路 |
| 生命周期 | 知识条目有 `status`（EXPIRED 要排除） | 文档有 `security_level`（密级闸） |
| 内容粒度 | LLM 抽取后的**结构化知识** | 源文件的**原文片段** |

合并的代价（这些不是"字段补一下"能解决的）：
1. **密级维度不同**：文档向量必须带 `security_level` 供检索过滤；知识向量没有密级概念。
   合并后每行都得填一个值 ⇒ 要么全填 L1（**降密**），要么给 wiki 知识引入密级（超出本期范围）。
2. **过滤语义不同**：wiki 检索排 `status=EXPIRED`，文档检索排 `security_level`。
   合表后两个过滤条件要么都生效（⇒ 文档向量被 wiki 的 status 过滤误伤）
   要么都不生效（⇒ **EXPIRED 知识泄漏**）。
3. **主键维度不同**：`page_id` vs `document_id`，同一个 `chunk_id` 命名空间会撞。
4. **写入时机不同**：wiki 向量由 LLM 抽取后写入；文档向量由入库时切分写入。

⇒ **结论：不合并。保持两张表，各自的过滤维度与生命周期独立。**

### 3.2 但「知识复用」要解决——这就是需求 3 的真正含义

用户要的是「同一份资料，wiki 抽出的知识**和**原文片段**都能**被检索到」。
这**不需要**合表，只需要**同一份文件在两张表里各有一份投影**：

```
                        ┌─→ wiki_page_embeddings   （LLM 抽取的结构化知识）
同一份源文件（MinIO 一份字节）
                        └─→ document_embeddings     （原文 chunk 切片）
```

**wiki 导入的文件要进 `document_embeddings`，缺的是这一步**（现在完全没有）。
做法**不是**"重新上传"，而是**复用已存的源文件**：

```
POST /api/v1/documents/{id}/vectorize
  ├─ 读 catalog 行（密级闸在此：L3 文档只有有权限的人能向量化）
  ├─ 从 MinIO 读回**已存的源文件字节**（不重新上传、不重新解析用户文件）
  ├─ 切块 → embedding → 写 document_embeddings
  ├─ 幂等：同一 document_id 已有向量 ⇒ 跳过或先删后建
  └─ 返回 {documentId, chunks, status}
```

**幂等怎么做**：`document_embeddings` 目前只有 `document_id` 字段，**没有唯一索引**
⇒ 重复调用会产生重复 chunk。两种做法：
- (a) 先按 `document_id` 删旧向量再插（需 `deleteDocumentChunks` 能力，Milvus 支持 delete by expr）
- (b) 加 `content_hash` 列 + 唯一约束
⇒ 选 **(a)**：Milvus 已有 `searchDocumentChunks` 的 expr 过滤能力，
且**不引入 schema 变更**（本期已有 0163 迁移，避免叠加）。

### 3.3 谁该有权点「向量化」？

| 角色 | 能否向量化 | 理由 |
|---|---|---|
| 文档 `owner` 部门成员 | ✅ | 自己的文件 |
| admin | ✅ | 运维/补齐 |
| 普通用户 | ❌ | 会消耗 embedding 额度（**花钱**）⇒ 不能是任意登录用户可触发的动作 |

⇒ **写 ACL**（按 owner/admin），并且**要限流**（embedding 批量调用）。

### 3.4 前端入口

文档中心列表加一列「向量化状态」：
- `未向量化`（wiki 导入的文件）→ 显示「向量化」按钮
- `已向量化（N chunk）` → 只读展示

⚠️ **这需要 catalog 知道向量状态**：不能靠实时查 Milvus（列表页 200 行文档
= 200 次 Milvus 查询）⇒ **在 `document_catalog` 加一列 `vectorized_at`（时间戳）**
（迁移 0163 一并加），向量化成功时写入。

## 四、需求 4：通用查看方式

### 4.1 候选方案对比

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 浏览器直接开原件** | `/source` 返回原件字节 + 正确 MIME | 零加工、**PDF 原生完美** | **Office（docx/xlsx/pptx）浏览器打不开**；md 也不渲染 |
| **B. 前端渲染库** | 前端引入 mammoth/xlsx/react-pdf 等 | 交互好 | **依赖重**（xlsx 1MB+、pdf.js 400KB）；前端体积爆炸；PDF.js 还要 worker 配置 |
| **C. 服务端统一转 HTML** | 服务端把任意格式转成一份 HTML 返回 | **一个端点解决全部格式**；前端零依赖；可复用服务端已有的解析库 | 服务端要写渲染器；Office 渲染保真度有限（但比纯文本好） |
| **D. 服务端转 PDF** | LibreOffice 转 PDF | 保真度最高 | **要装 LibreOffice**（镜像 +500MB）；慢（每次 3–10s）；中文字体要装 |

### 4.2 选定：**C 为主 + A 兜底**

```
GET /api/v1/documents/{id}/preview  →  HTML（服务端渲染）
GET /api/v1/documents/{id}/source   →  原件字节（下载 / PDF 直接看）
```

**preview 按 `preview_kind` 分派**：

| kind | 格式 | 渲染方式 | 保真度 |
|---|---|---|---|
| `MARKDOWN` | md | `markdown` 库渲染（**已是依赖**）+ 安全 HTML | 高 |
| `HTML` | html | **沙箱化**：剥 `<script>`/`<iframe>`/内联事件，限死外链 | 中（安全优先） |
| `TEXT` | txt/csv | `<pre>` 转义 | 高 |
| `PDF` | pdf | **不转**：返回原件字节让浏览器原生 PDF 查看器渲染（保真 100%、零服务端成本） | 最高 |
| `OFFICE` | docx/xlsx/pptx | 服务端用**已有解析库**渲染：docx→标题/段落/表格；xlsx→每 sheet 一张表；pptx→每页标题+要点 | 中（结构在、样式不在） |
| `IMAGE` | png/jpg | `<img>`（原件字节 + 限尺寸） | 最高 |

**为什么 PDF 不转**：PDF 的价值就是版式，转一遍必丢；而浏览器原生 PDF
查看器是**零成本、零依赖、100% 保真**。这是唯一该用「原件直出」的格式。

**为什么 Office 用服务端渲染而不是前端库**：
- 前端已装 `mammoth/xlsx` 之类要 **+1MB 打包体积**（且 PDF.js 要 worker/跨域配置）
- 服务端**已有** `python-docx` / `openpyxl` / `python-pptx`（都是现成依赖）
- 预览是**只读展示**，不需要前端交互能力

### 4.3 HTML 预览的**安全**要求（必须写进设计）

服务端把用户文件渲染成 HTML 再吐给浏览器 ⇒ **XSS 风险**（上传的 html/svg 里带
`<script>` 就能打前端）。必须：

1. **HTML 源文件**：剥离 `<script>` / `<iframe>` / `<object>` / `<embed>` /
   `on*` 事件属性 / `javascript:` URL；外链加 `rel="noopener"`；**用 iframe sandbox 包裹**
2. **Markdown 渲染**：同样走 HTML 消毒
3. **Office 渲染**：输出的是**我们自己生成**的结构（转义后的文本）⇒ 安全
4. **SVG**：视同 HTML 剥离脚本（SVG 可带 `<script>`）
5. 预览页整体用 `Content-Security-Policy` + `X-Content-Type-Options: nosniff`

⇒ 需要一个**后端 HTML 消毒器**。依赖里没有 bleach ⇒ 要么加依赖，要么手写
（用 `html.parser` 做白名单）。**倾向加 `bleach`**（维护成本远低于手写消毒器，
且消毒器本身就是高危代码）。

## 五、落地顺序（每步都可独立验收）

| 步 | 内容 | 迁移 | 验收 |
|---|---|---|---|
| **S1** | 迁移 0163：catalog 加 `mime_type`/`file_size`/`preview_kind`/`vectorized_at`；两个上传路径都写这四列 | 0163 | 目录元数据齐全 |
| **S2** | `GET /documents/{id}/source`（原件 + ACL + 中文文件名） | — | 能下载/内联看原件 |
| **S3** | `GET /documents/{id}/preview`（统一 HTML 预览，6 类分派 + 消毒） | — | 6 种格式都能看 |
| **S4** | `POST /documents/{id}/vectorize`（读已存源文件 → 向量化进 document_embeddings，幂等 + ACL + 限流） | — | wiki 文件能被检索到 |
| **S5** | 前端：文档中心加「原文」「预览」「向量化」三个入口 + 向量化状态列 | — | 前端能操作 |
| **S6** | 采购知识检索证据卡加「查看原文片段出处」链接（可选） | — | 证据可回溯 |

## 六、待确认（**这些会影响实现，请拍板**）

1. **HTML 预览是否允许显示外部图片/链接？**
   我倾向：图片**禁止**外链（防跟踪/SSRF 视觉），链接**允许但加 noopener**。
2. **Office 预览要不要保留页/幻灯片分隔与表格结构？**
   我倾向：要（docx 标题层级 + 表格、xlsx 每个 sheet 分区、pptx 每页一块）。
3. **向量化的触发方式**：只做手动按钮，还是也提供"wiki 导入完成后自动向量化"？
   我倾向：**只做手动**（自动 = 每次导入都花钱，且用户可能只想抽知识不想全文检索）。
4. **`preview_kind` 推断规则**：以落库时的 MIME 为准，缺失则按扩展名。
   老数据（6 份）需要**一次回填脚本**（按 storage_url 里的扩展名推）。
