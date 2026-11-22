# 实施：文档统一入库 + 原文阅览（需求 1–4，S1→S6）

- 日期：2026-11-21/22　分支：`analysis/wiki-document-sharing`（设计在 `design.md`）
- 设计：`Harness/changes/2026-11-21-analysis-wiki-doc-sharing/design.md`
- 提交序列：S1 `b59f5656` → S2 `405f4a25` → S3 `e73022ba` → S4 `272520bf`
  → S5 `385249f6` → S6 `3c4e4cfd`

## 一、需求逐条对照

| # | 需求 | 落地 | 验收 |
|---|---|---|---|
| 1 | wiki 导入的原文件也入文档中心 + 可看原文 | **本来就在同一张 `document_catalog`**（分析已证）；S2/S3 补上「可看原文」出口 | ✅ 目录里 3 个 `DOC-WIKI-*` 都可取原件/预览 |
| 2 | 文档中心上传的文件也可看原文 | S2 `/source` + S3 `/preview` | ✅ 真机：PDF 3.8MB 原样返回 |
| 3 | wiki 文件可再次向量化进 `document_embeddings`；或思考共用向量表 | **不合并向量表**（理由见 §三）；S4 提供「读回已存源文件 → 向量化」 | ✅ 真机：wiki 文件从 0 命中 → 命中 19 chunk |
| 4 | 通用查看方式（excel/word/ppt/pdf/md/html…） | S3 `/preview` 六类统一渲染 | ✅ md⇒HTML、pdf⇒302 原件；单测覆盖 docx/xlsx/pptx |

## 二、需求 3 的最终结论（重要）

**两张向量表不合并**，理由是**语义级**差异（不是字段数量问题）：

| | `wiki_page_embeddings` | `document_embeddings` |
|---|---|---|
| 主键维度 | `page_id`（知识条目） | `document_id`（源文件） |
| 特有字段 | `title`/`dimension`/**`status`** | `effective_date`/**`security_level`**/`page_number` |
| 过滤语义 | 排 `EXPIRED` 知识 | **密级闸**（L1/L2/L3） |

合并后：
1. wiki 侧没有密级概念 ⇒ 每行都得填 `security_level` ⇒ 只能填 L1 ⇒ **制度文档被静默降密**；
2. 两个过滤条件要么都生效（文档向量被 wiki 的 `status` 误伤）要么都不生效（**EXPIRED 知识泄漏**）；
3. `page_id` vs `document_id` 主键命名空间会撞。

**但知识复用照样达成**——同一份文件在两张表里各有一份投影：

```
同一份源文件（MinIO 一份字节，内容寻址天然去重）
   ├─→ wiki_page_embeddings   （wiki 侧 LLM 抽取的结构化知识）
   └─→ document_embeddings     （S4：原文 chunk 切片）
```

S4 的关键是**不重新上传**：从 MinIO 读回**已存的字节** → 切块 → embedding → 写向量。

## 三、关键实现决策

| 决策 | 理由 |
|---|---|
| `preview_kind` **落库**而非每次推断 | 推断规则会变 ⇒ 落库后已入库文档的呈现不变 |
| PDF **不转**，302 到原件 | PDF 的价值就是版式，转一遍必丢；浏览器原生查看器零成本 100% 保真 |
| Office **服务端**渲染（已有 python-docx/openpyxl/python-pptx） | 前端装 mammoth/xlsx/pdf.js 要 +1MB 打包体积；服务端这些库都是既有依赖 |
| 用 **bleach** 而非手写消毒器 | 消毒器是高危代码，手写必漏（属性名大小写、命名空间、畸形标签、编码绕过） |
| 外链图片**禁止** | 跟踪像素 / 视觉 SSRF（用户确认） |
| 向量化**只做手动** | 自动 = 每次导入都花钱，且用户可能只想抽知识不想全文检索（用户确认） |
| 向量化**写 ACL**（owner/admin） | 消耗 embedding 额度 = 花钱，不能任意登录用户触发 |
| 幂等靠**先删旧向量再写** | `document_embeddings` 无唯一约束，否则重复调用产生重复 chunk |
| `source`/`preview` 接受**自增 id 或业务编号** | 采购知识证据里存的是业务编号（DOC-xxx，可读可引用） |
| 纯文本用 `html.escape` 而非 bleach | bleach 的 `strip=True` 会把 `1 < 2` 吞成 `1 ` |

## 四、新增/变更清单

**迁移 0163**（纯加法）：`document_catalog` 加 `mime_type` / `file_size` /
`preview_kind` / `vectorized_at`（+ CHECK 约束）。

**新端点**：
| 端点 | 作用 |
|---|---|
| `GET /documents/{key}/source` | 原件字节（inline / attachment），中文名 RFC 5987 |
| `GET /documents/{key}/preview` | 统一 HTML 预览（PDF/图片 ⇒ 302 到原件） |
| `POST /documents/{key}/vectorize` | 读回已存源文件 → 向量化（幂等 + 写 ACL） |

**新服务**：`document_preview_kinds`（类别单一事实源）、`document_source_service`
（storage_url 解析 + 读取 + 响应头）、`document_preview_renderer`（六类渲染）、
`html_sanitizer`（消毒）、`document_vectorize_service`（向量化编排）。

**新脚本**：`backfill_document_preview_metadata`（回填既有行；体积**实际去对象
存储读**，向量化状态**查 Milvus 有没有 chunk** ⇒ 真实证据，不猜）。

**新增依赖**：`bleach>=6.0.0`（⚠️ 生产镜像需重建）。

**前端**：文档中心加「原文/预览/向量化」入口 + 向量化状态列；采购知识证据卡
加「预览」链接。

## 五、验证

- 后端：**118 passed**（新增用例 62：消毒 10 + 渲染 8 + 源文件 10 + 预览类别 17
  + 端点 18）
- 前端：**2507 passed**（249 文件；新增 17）
- 既有红基线：`test_document_catalog_api` 的 2 个失败（**stash 前后同样失败**，
  L2/L3 clearance 在测试库拿不到）——非本次引入
- 真机：
  - 原文：PDF 3.8MB 原样返回，`content-type: application/pdf`，中文名 RFC 5987
  - 预览：PDF ⇒ 302；md ⇒ 自包含 HTML（含 CSP + nosniff）
  - 向量化：wiki 导入的 `DOC-WIKI-F9F24A0FA8B5` ⇒ **19 chunk**；
    此后采购知识检索**能命中**（此前 0 命中）

## 六、遗留与注意

1. **`bleach` 是新增依赖** ⇒ 生产发版要重建后端镜像；`uv.lock` 已更新。
2. **迁移 0163** 需要上生产（Alembic 链 0162→0163，纯加法）。
3. `document_catalog` 的 `owner` 现有值多为 `procurement`（部门码）⇒ 向量化 ACL
   按「owner ∈ 用户部门」判定；若生产里有 `owner` 为空的历史行，**任何非 admin
   都不能向量化**（需先补 owner）。
4. **wiki 侧不生成向量这件事本身没改** ⇒ wiki 导入的文件仍需**手动**点一次
   「向量化」才进 `document_embeddings`（按用户确认：只做手动）。
5. 前端 i18n 键集合与基线**完全一致**（zh/en 差异 3 个是既有基线，未新增偏差）。
6. 未做：页面刷新后恢复线程、向量化的进度反馈（当前是按钮 loading）。
