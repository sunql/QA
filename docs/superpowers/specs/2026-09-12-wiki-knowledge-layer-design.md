# 企业 LLM Wiki 知识层落地设计（Wiki ↔ 本体桥接）

## 概述

将《企业 LLM Wiki 总体架构与落地方案》与 qa-system 现状对齐：让 LLM Wiki 摄入的知识与本体（Ontology）建立关系，形成一套 LLM 可理解、可溯源的知识体系。

本文档先给出现状差距的实测结论，再给出分阶段落地路线，并把**首期可实施的三个阶段（P0 / P1 / P3）**展开为可执行的详细设计。P4 / P2 / P5 仅界定边界，各自另立 spec。

## 目标

1. **知识问答可溯源**：任何一条知识都能回指到源文档的具体位置（页码 / 章节 / 段落）。
2. **提升 NL2SQL 准确率**：Wiki 侧的业务语义（别名、口径、规则）能反哺本体，改进生成质量。
3. **建立 Wiki ↔ 本体桥接**：Wiki 页面产出的知识，能落到本体的 Class / Property 上，而不是两套并行数据。
4. **数据可信**：重复导入不产生副本；每批 LLM 消耗可归因、可复算。

## 非目标

1. **不重建 Wiki 子系统**：M1–M8 机制已落地并在产，本文档是补齐其缺口，不是重写。
2. **不做全自动发布**：知识进入可用状态必须经人工复核（沿用现有 `structure_suggestion` / `knowledge_conflict` 的 HITL 形态）。
3. **不引入 pgvector / 新向量库**：向量检索继续走 Milvus。
4. **本期不做知识版本化**：P3 会按选项 C 落下列（`valid_from` / `valid_to` / `status`），但**本期不写入、不消费、不做时效过滤**。列先就位是为了避免将来二次迁移，不是本期功能。
5. **不做多租户知识隔离**：沿用现有全局共享模型。

---

## 一、现状差距（实测）

以下数字来自 prod `qa_metadata` 实测，非推断。

> **时序说明**：下述 `wiki_page = 557` 是**用户批量删除之前**的快照。用户已确认该删除为主动操作，不回滚，当前表为空。此处保留 557 这组数字，是因为它揭示了数据层的真实形态（75 标题 × 8 任务），这是 P1 去重设计的依据；表为空只是让 P1 的迁移更便宜（见 5.3），不改变设计结论。

### 1.1 已经实现的

- M1–M8 机制代码全部在位，31 个 wiki 端点在线（`/api/v1/wiki/*`）。
- **自动分类（M2）确实在工作**：557 条页面中 556 条有分类结果
  （CONCEPT 235 / OBJECT 144 / PROCESS 67 / DOCUMENT 59 / METRIC 22 / RULE 22 / FAQ 6 / POLICY 1）。
- 导入编排（`wiki_import_service.execute`）是全项目唯一成型的批处理骨架：
  逐项 SAVEPOINT、失败不中断整批、逐页 commit、台账字段。
- 本体侧已有规模：`ontology_class` 27、`ontology_property` 883、`ontology_join` 106、`ontology_relation` 24。

### 1.2 没有实现的（四个断点）

**断点 0 — 数据层是脏的。** 557 行页面实际只有 **75 个不同标题**（8 个导入任务 × 75 页，`source_ref` 全部是同一份文档）。无去重、无源文件留存、无真实业务知识。

**断点 1 — Wiki → 本体桥是空的。** `knowledge_relation = 0`。而 `knowledge_relation.downstream_id` 是 `varchar(128)` 软引用，**没有任何 FK 约束** —— 可以写入指向不存在本体的关系，且无人报错。

**断点 2 — 证据链从未建立。** `knowledge_claim = 0`、`evidence = 0` —— **生产代码里这两张表零写入者**（已穷尽确认）。而机制→落库映射中根本没有它们的位置：

| 机制 | 落库 |
|---|---|
| M1 分类 | `learning_feedback` |
| M2 关系 | `knowledge_relation` |
| M3 冲突 | `knowledge_conflict` |
| M4 结构化 | `structure_suggestion` |
| M5 规则 | `wiki_rule_executable` / `process_workflow` |
| **claim / evidence** | **无** |

**断点 3 — Chat 到不了知识库。** `chat_service.py` 中 wiki / RAG 零命中；L4 agent loop 只用 5 个 NL2SQL 工具，且绕过 ACL。

### 1.3 本体语义也是空的

| 字段 | 填充率 |
|---|---|
| `ontology_property.property_alias` | 883 / 883 |
| `ontology_property.description` | 45 / 883 |
| `ontology_property.business_aliases` | 30 / 883 |
| `ontology_property.allowed_values` | 0 / 883 |
| `ontology_class` 的 class_alias / description / object_owner | 27 / 27 |

`coverage_cell` 216 行全部 `MISSING` / `UNASSIGNED`，且 `sum(page_count) = 0` —— 覆盖率机制在跑，但没有知识可覆盖。

---

## 二、路线与阶段边界（Route 甲，严格排序）

用户已确认按 **P0 → P1 → P3 → P4 → P2 → P5** 严格顺序推进。

| 阶段 | 内容 | 本文档 |
|---|---|---|
| **P0** | 溯源地基：定位符 + 源文件留存 | **详细设计** |
| **P1** | 去重：内容派生身份 | **详细设计** |
| **P3** | 批量编译器：claim/evidence 写入者 + 批量编排 | **详细设计** |
| P4 | 双消费端：带引用的问答 + NL2SQL 反哺 | 另立 spec |
| P2 | THBI 数据字典线 | 另立 spec |
| P5 | 治理 | 另立 spec |

**阶段原则**（每阶段必须同时满足）：

1. 可独立交付、可独立验证、可独立回滚。
2. 不依赖其他阶段未完成的地基。
3. 每阶段结束系统处于可运行状态。

**为什么 P0 必须排第一**：P3 要写的 `evidence` 表，三个定位列（`page_number` / `section_name` / `paragraph_no`）正是 P0 的产出。P0 不做，P3 写出来的 evidence 就没有出处可填 —— 证据链会变成一堆空定位符，等于没做。P0 是 P3 的输入依赖。

---

## 三、两条不可违反的边界约束

### 3.1 两个 L 轴不得混同

- `authority_level`（L5–L0）：**权威度** —— 这条知识有多可信。
- `document_catalog.security_level`（L1–L3）：**密级** —— 谁能看。

两者正交。把它们混成一个字段，会导致"高密级 = 高权威"这种语义错误，且事后拆分需要数据回填。命名上必须始终带轴前缀区分。

### 3.2 软引用必须做写入侧存在性校验

`knowledge_relation.downstream_id` 无 FK。若不做写入侧校验，可以写入指向不存在本体的关系，而覆盖率指标会把这些幽灵关系算成有效覆盖，**指标被系统性高估且无人察觉**。P3 落地时必须补写入侧校验。

---

## 四、P0 详细设计：溯源地基

### 4.1 组件

**C1 — `TextBlock` 与 `parse_document` 返回类型变更**

现状（`app/services/document_parser.py`，82 行）是 P0 的核心丢失点：`parse_document(content, mime_type, filename) -> str` **返回扁平字符串**。PDF 分支遍历 `reader.pages` 时页码是有的，拼 `"\n".join(texts)` 时被丢弃；DOCX 同理会丢弃段落结构。

改为返回带定位信息的结构：

```python
@dataclass(frozen=True)
class TextBlock:
    text: str
    page_number: int | None      # PDF 有，DOCX/MD 为 None
    section_name: str | None     # 由标题识别填充
    paragraph_no: int | None

async def parse_document(...) -> list[TextBlock]
```

**类型对齐（P0 与 P3 必须一致）**：这里 `page_number` / `paragraph_no` 定为 `int`，而 `evidence` 表现存两列是 `VARCHAR(30)`。P3 迁移会把 evidence 两列改为 `INTEGER`（见 7.5），与文档方案一致 —— 数字列可做区间查询、可校验；非数字情形（如"附录A"）归入 `section_name`，不挤进页码列。

**注意保留**：`UnsupportedFileTypeError` 与 `DocumentParserError` 的分裂是**刻意的**，注释已写明理由（"换格式即可" vs "换文件"）。不得合并。

**C2 — `Chunk` 定位符，复用已存在的扩展点**

`app/services/chunk_splitter.py` 的 `Chunk` 类**已有 `metadata: dict | None = None` 字段，且完全未被使用** —— 这就是现成的扩展点，无需改类结构。`split_by_paragraphs` 的签名从吃扁平 `str` 改为吃 `list[TextBlock]`，`_make_chunk` 把定位符写进 `metadata`。

**C3 — 新增 `app/infrastructure/object_storage.py`**

独立 MinIO 容器 `qa-objects`，独立命名卷 `objects_data`（→ `qa-system_objects_data`），端口 9000/9001。

- 不复用 `qa-milvus-minio`：那是 Milvus 的私有后端，不发布端口，且硬编码 `minioadmin/minioadmin`。
- 凭证通过 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 环境变量注入，**不沿用硬编码模式**。
- 桶 `qa-knowledge-sources`，由 `ensureBucket()` 幂等创建。
- 配套 `scripts/backup_objects.sh`。
- 同步更新 `Harness/rules/数据存储防护.md` 的卷清单。

**C4 — 真实 `storage_url` + `content_hash`，并修掉静默失败**

`app/services/rag_service.py` 两处问题：

- `:167` `content_hash=None` —— 去重键从未写入。
- `:176-178` 静默失败：

```python
except Exception:
    pass  # 更新失败不影响主流程
```

且 `:176` 写的是 `storage_url=f"milvus://{len(chunks)}_chunks"` —— 一个**假 URL**。改为写真实对象存储地址。

**C5 — Wiki 导入路径留存源文件并登记 `document_catalog`**

`wiki_import_service.py` 目前**完全不注册 `document_catalog`**（该表 0 行），只有 RAG 路径注册。两条摄入路径应当共用同一套目录登记。

**落在哪一步（2026-09-12 用户选定）**：`execute()` 的输入是客户端回传的**纯文本草稿**，**从不接触文件字节** —— 它在结构上就存不了源文件，算不出文件哈希。字节只在 `POST /wiki/import/preview-file` 手里。因此：

- **在 `preview-file` 落库**：存对象 + 登记一行 `document_catalog`，`content_hash` 与 `storage_url` **全由服务端从真实字节算出**。
- `execute` **不携带任何溯源字段** —— 签名与职责不变。
- **不得让客户端回传 hash / url**：本仓库已有同一原则的先例 —— `WikiImportFileParseRead` 的 docstring 明确写 `source_type` 由后端判定而非前端自报（「让调用方自报等于允许『上传 .pdf 却记成 MARKDOWN』，台账就不可信了」）。`content_hash` / `storage_url` 是同一性质的溯源字段，交由调用方回传会犯同一个错。
- **留存必须排在解析成功之后**：反过来的话，每个损坏/格式不符的文件都会在对象存储里留下永远无人引用的垃圾。

**契约变更（须诚实记录）**：`preview-file` 的契约原写「不落库、不调模型」，此后为「**落对象存储与文档目录**、不调模型」。模块级与端点级两处 docstring 必须同改 —— 契约背离比改动本身更危险。

**已知代价（接受）**：预览了但未导入的文件同样会留存。内容寻址使重复预览不产生重复对象，catalog 登记再按 `content_hash` 幂等一层。**「查看」走既有文档库**（`GET /documents` 读 `document_catalog`），P0 不新增前端、不新增端点。

### 4.2 数据流

```
文件上传
  → parse_document → list[TextBlock]（带页码/章节/段号）
  → 源文件存入 MinIO qa-objects → 真实 storage_url + content_hash
  → split_by_paragraphs(list[TextBlock]) → Chunk(text, metadata={定位符})
  → embedding → Milvus（collection 需重建，见 4.4）
  → document_catalog 登记（真实 url + hash）

Wiki 导入路径：`preview-file` 走 C1 + C3 + C5（留存源文件 + 登记 catalog，哈希取自**文件字节**）；
`execute` 只建 Page 与分类，不接触字节，故**不带任何溯源字段**
```

### 4.3 影响面

生产调用方 3 处（`rag_service`、`wiki_import_service`、`api/v1/wiki_import.py`），测试文件 3 个。改动可控。

### 4.4 Milvus collection 需重建

`app/infrastructure/milvus_client.py` 的 `_documentFields()`（`:337-346`）**没有定位符字段**。三个约束：

- `_ensureCollection`（`:104-113`）**集合已存在就直接返回** —— 只改 `_documentFields()` 不生效，必须重建集合。
- `CollectionSchema(fields=fields, ...)`（`:118`）**未设 `enable_dynamic_field`**（默认 False），不能靠动态字段绕过。
- 好消息：该 collection 当前为空，重建零成本。

**因此 P0 是"零 PG 迁移"，但包含一次 Milvus 集合重建。** 这是两件不同的事，不要混淆。

### 4.5 错误处理

- 解析层：`UnsupportedFileTypeError` / `DocumentParserError` 分别映射到不同的用户提示。
- 存储层：MinIO 不可用必须**显式失败**，不得降级为静默跳过（C4 修的就是这个模式）。
  在 `preview-file` 上这意味着留存失败即 **503**，**不得**降级为「预览成功但其实没存」；
  代价是预览可用性从此与 MinIO 绑定 —— 这是「预览即持久化」的固有代价，接受它。
- 目录登记：沿用现有既有策略，但失败必须留日志上下文。

### 4.6 测试策略

按 `Harness/rules/测试规范.md`：真实 PostgreSQL + 完整 API 链路，禁止 sqlite。

- 解析层：`TextBlock` 定位符正确性 —— PDF 页码连续性、DOCX 段号、MD 章节识别。
- 存储层：`ensureBucket` 幂等；上传后 `storage_url` 可回读（真实 MinIO，由 P0 Task 7 真实数据脚本承担）。
- 集成层（P0 Task 6）：**假 MinIO 客户端 + 真实 PostgreSQL + 完整 API 链路**，覆盖 `preview-file` 留存 ——
  hash 取自**文件字节**、响应契约不被顺带改宽、同内容幂等、不同内容确实两行、被拒文件不留痕。
  外部服务走假实现与既有「假 LLM 客户端」（`_INVOKER_CLIENT`）同一思路；真实端到端由 Task 7 承担。
- 端到端：上传一份已知 PDF → 断言 Milvus 中的 chunk 带正确 `page_number`。

### 4.7 决策

| # | 决策 | 结论 |
|---|---|---|
| D2-1 | 对象存储形态 | **独立 MinIO 容器 + 独立命名卷**（已确认） |
| D2-2 | P0 是否含 PG 迁移 | **零迁移**。`content_hash` 唯一约束推迟到 P1 一并做，只付一次迁移成本 |

**诚实记录**：PG 的备份 cron 当前静默失效（launchd 契约断裂，根因是 `/etc/crontab` 缺失，非早先猜测的 FDA）。用户已于 2026-09-12 决定维持手动备份。因此 `scripts/backup_objects.sh` 同样以手动形态交付，此缺口写入 summary §9，不粉饰。

---

## 五、P1 详细设计：去重

### 5.1 根因

`generatePageId(title)`（`wiki_page_service.py:148-157`）只吃标题，末尾拼随机后缀：

```python
suffix = uuid.uuid4().hex[:8].upper()      # ← 随机，非内容派生
```

完整失效链：

```
_importOne(:316) → generatePageId(title) → 新 ID
  → 冲突检查(:449-453) 永不命中
  → uq_wiki_page_page_id(0053:54) 永不冲突
  → 副本必落库
```

**关键判断：P1 不是"把 `page_ids` 读起来"，而是让 `page_id` 成为内容的函数。** `page_ids` 是结果，不是手段。

### 5.2 设计

`page_id` 派生改为内容派生：

```
PAGE-<slug(title)>-<sha256(source_ref \x00 title \x00 content)[:8].upper()>
```

同一份文件重跑 → 同 hash → 同 `page_id` → **现有冲突检查与 `uq_wiki_page_page_id` 原样生效，零新机制**。

同时落列 `content_hash VARCHAR(64) NULL` 存原值。它有两个独立用途，不是为派生服务的：

1. 给 P0 的 `document_catalog.content_hash` 提供同源值。
2. 让"同标题不同内容"可判定 —— 现在这种情况与"同文件重跑"表现完全一样（都是 ID 冲突），无法区分。

### 5.3 迁移与门禁

需要一次 Alembic 迁移（加列 + 索引）。按 `Harness/rules/数据库环境使用规范.md`：**直接改 prod `qa_metadata`，先备份 `wiki_page_20260912`**。规则不区分轻重，即使只是 `ADD COLUMN`。

**时间窗口**：`wiki_page` 当前为空（用户主动批量删除）。**这是做此迁移最便宜的时刻** —— 有真实数据之后，同一迁移需附带全表回填。P1 应排在重新导入任何知识之前。

### 5.4 分类学修正

现在重复项走 `ConflictError` → 计入**失败** → `continue`（`wiki_import_service.py:323-329`）。P1 之后，重复应当计入**成功但跳过**。台账语义必须同步改，否则运维看到的失败数假性偏高。

### 5.5 验收

新增集成测试，钉死 `wiki_learning_models.py:172-173` docstring **今天只声称、并不存在的**幂等重放：

> "本表是幂等可重放的作业台账：`page_ids` 记录产出的知识条目业务键，失败重跑时用来跳过已成功项"

同一份文件导入两次 → `wiki_page` 恰 1 行。然后修掉这句不实文档。

---

## 六、P3 详细设计：批量编译器

### 6.1 三个前置修复

不修就等于把 bug 放大 N 倍。

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 1 | 三个机制内部各自 `commit()`，破坏调用方事务 | `relation_discovery.py:314`、`structure_suggester.py:411`、`conflict_detector.py:450`（+ `wiki_structure_service.py:102`） | 拔掉。契约参照 `progressive_upgrader.py:17-22` —— 该文件头已写明此坑并点名 M4/M5 |
| 2 | M4 无"已跑过"短路 | `relation_discovery.py` 全文件无 `_hasPending` | 照抄 `structure_suggester.py:333-336`。无短路时重跑会**先付 LLM 钱**，再被 `on_conflict_do_nothing` 丢弃结果 |
| 3 | 端点构造 invoker 不传 fallback、不绑 `bindImportTask` | `wiki.py:321-325`、`:393-397`、`:505-509` | 批量入口必须绑定作业 id，否则 token 行归因断链 |

### 6.2 编排层

新建 `app/services/wiki_compile_service.py`，**形状照搬 `wiki_import_service.execute`**（`:252-387`）—— 全项目唯一成型的批处理：逐项 SAVEPOINT（`:315`）、失败 `continue` 不中断整批（`:323-329`）、逐页 commit（`:333-334`）。

需要新增"取全部 page id"的迭代器，绕开 `listPages` 的 200 上限（项目里目前没有此方法）。

该服务不持有内存待办列表，其作业状态与续跑依据是台账表，见 7.2。

### 6.3 claim / evidence 写入者

这是 P3 的实质产出。两张表**生产代码零写入者**（已穷尽确认，仅测试文件手写 SQL）。

新增抽取步骤：

- 新 prompt：`learning/prompts/extract_claim_v1.txt`
- 新 `mechanism="CLAIM"`。**已验证 `LEARNING_MECHANISMS` 是纯 Python 常量**，`wiki_token_usage.mechanism` 无 CHECK 约束 —— **加值无需迁移**。

**顺序是强制的**：`evidence.claim_id` 是 `NOT NULL` FK（`0053:91`），所以必须先产出 claim，再为每条 claim 绑 evidence。

#### 双形态产出（D3-1 = 选项 C）

抽取 prompt **一次调用产出两种表示**：

```json
{
  "claim_text":   "供应商A因质量问题自2026年1月起暂停采购资格",
  "claim_type":   "FACT",
  "subject_id":   "SUPPLIER-A",          // 可为 null
  "predicate":    "HAS_STATUS",
  "object_value": "Suspended",
  "object_type":  "BusinessObject",
  "confidence":   0.86
}
```

- `claim_text` / `claim_type` 必填，沿用现实现有列，语义不变。
- 三元组各列**可空**：抽得出就写，抽不出留 `NULL`。`subject_id` 允许自由文本，不要求解析成功。
- 一次调用同时产出两形态，**成本与只产三元组等同** —— 这是选项 C 相对选项 B 没有额外开销的原因。

**双表示一致性规则**（B 没有的新负担，必须显式定）：

三元组**仅在抽取时写入**。人工编辑 `claim_text` 时**不自动重抽**三元组，而是把该 claim 的三元组标记为 stale（新增 `triple_stale BOOLEAN NOT NULL DEFAULT FALSE`），由人工决定是否触发重抽。

理由：自动重抽会把一次人工校对变成一次不可控的 LLM 调用，且可能在用户不知情时改掉已复核的结论。宁可显式标记、批量重抽，也不要静默漂移。

### 6.4 `selectin` 性能悬崖（本节最重要的发现）

`WikiPage.claims`（`wiki_models.py:163-168`）与 `KnowledgeClaim.evidences`（`:208-213`）**都是 `lazy="selectin"`**。今天两张表是空的，所以完全看不出来。

P3 一写入数据即刻暴露：

- 每次 `WikiPage` 实体加载都会顺带 SELECT 这两张表（加载点：`wiki_page_service.py:258,272,315,358,372,387`）
- `evidence.content` 是 Text —— 逐字原文摘录，可能很大
- 列 50 页 → 1 + 若干 + N 条查询，且拖着大文本载荷

**必须在 P3 同一变更内降级**，不能事后补：两张表都改回 `lazy="select"`，仅在真正需要的端点（`GET /wiki/pages/{pageId}/claims`）显式 `selectinload`。纯 ORM 改动，无迁移。

**连带项**：`test_wiki_batch_delete_api.py:786-800` 有一条反退化测试，断言批量删除路径的 SQL 里不得出现 `evidence`。降级后会动到它。

### 6.5 `authority_level` 白名单

现在 L5–L0 无任何校验，且从不参与排序/过滤 —— 仅 3 处使用：写入（`wiki_page_service.py:433`）、透传（`wiki_schemas.py:119`、`agent_tools_wiki.py:236`）、复制进 `rule.authority_chain`（`progressive_upgrader.py:255`）。

加 Python 常量白名单 + Pydantic 校验，**沿用 `LEARNING_MECHANISMS` 的做法不加 DB CHECK** —— 与既有约定一致，且省一次迁移。常量命名须体现轴语义，避免与 `security_level` 混淆（见 3.1）。

---

## 七、P3 台账表设计（D3-2 决策：建表）

### 7.1 两层结构

单表 + `page_ids` 数组列 = 原样重造 `wiki_import_task` 已踩过的坑：一个 blob 列，写入方 3 处、读取方 0 处。用户已明确要求通过数据表处理，因此这张表必须**自己承担续跑**，而不是存一份谁都不读的清单。

**`wiki_compile_task`（作业层）**

| 列 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK | |
| status | VARCHAR(20) NOT NULL default PENDING | 复用 `IMPORT_TASK_STATUSES` 五值（`wiki_learning_models.py:53-58`），语义一致 |
| scope | VARCHAR(30) | ALL / PAGE_IDS / DIMENSION —— 让作业范围可复现 |
| selected_model_id / fallback_model_id | BIGINT FK `llm_config` | 照 import task 形状；**从 schema 层修掉"端点从不传 fallback"** |
| total_items / success_items / skipped_items / failed_items | INT NOT NULL default 0 | `skipped` 为新增计数（P1 后重复=跳过≠失败） |
| total_cost_usd | NUMERIC(12,6) NOT NULL default 0 | |
| error_message | TEXT | |
| created_by_user_id | BIGINT | |
| created_time / started_time / **heartbeat_time** / finished_time | TIMESTAMPTZ | heartbeat 见 7.3 |

**`wiki_compile_item`（逐项层 —— 续跑靠它）**

| 列 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK | |
| task_id | BIGINT NOT NULL FK → task ON DELETE CASCADE | |
| page_id | VARCHAR(64) NOT NULL | 软引用，与 `wiki_page.page_id` 同约定 |
| status | VARCHAR(20) NOT NULL default PENDING | PENDING / RUNNING / DONE / SKIPPED / FAILED |
| mechanism_counts | JSONB | 本页各机制产出数（RELATE/CONFLICT/STRUCTURE/CLAIM）；**给 UI 看的结果摘要**，不是续跑机制 |
| attempt_count | INT NOT NULL default 0 | |
| error_message | TEXT | |
| started_time / finished_time | TIMESTAMPTZ | |
| | **UNIQUE (task_id, page_id)** | **幂等从约定升级为 DB 不变量** |

续跑查询就是读这张表本身：

```sql
SELECT page_id FROM wiki_compile_item
WHERE task_id = ? AND status IN ('PENDING','FAILED');
```

**它不会像 `page_ids` 那样烂掉** —— 因为没人读它的话续跑功能直接不工作，问题立刻暴露，而非静默躺在库里。

### 7.2 作业生命周期（服务层与台账的关系）

`wiki_compile_service` 不持有内存中的待办列表 —— **待办列表就是 `wiki_compile_item` 表本身**。四步：

```
1. 建作业   INSERT wiki_compile_task (status=PENDING, scope=...)
2. 播种明细 INSERT wiki_compile_item × N (status=PENDING)
            scope=ALL      → N = 全量 page_id（新迭代器，绕开 listPages 200 上限）
            scope=PAGE_IDS → N = 请求显式传入的 page_id 列表
            scope=DIMENSION→ N = 该维度下的 page_id
            ※ scope 只决定"明细怎么播种"，页面清单本身始终落在 wiki_compile_item
3. 处理     task.status=RUNNING
            SELECT page_id FROM wiki_compile_item
              WHERE task_id=? AND status IN ('PENDING','FAILED')
            → 逐项 SAVEPOINT → 处理 → 更新该项 status / mechanism_counts
            → 每项完成 bump task.heartbeat_time
4. 收尾     task.status = SUCCEEDED / PARTIAL / FAILED（复用 IMPORT_TASK_STATUSES 语义）
            汇总 success_items / skipped_items / failed_items / total_cost_usd
```

**续跑 = 重跑第 3 步。** 因为第 3 步只取 `PENDING`/`FAILED`，已完成的项不会被重复处理，也不会重复产生 LLM 成本。

`scope` 与明细的关系是**一次性播种 vs 持久明细**：`scope` 记录"这批是怎么来的"（可审计、可复现），`wiki_compile_item` 记录"这批具体有哪些页、各自什么状态"（续跑依据）。P0/P1 的教训正在于此 —— `page_ids` 把这两件事混在一个 blob 列里，于是两件事都没做成。

### 7.3 僵尸 RUNNING：把教训编进设计

`wiki_import_task` 的 RUNNING 僵尸（容器重建 → 兜底 except 不执行 → 永久 RUNNING）已产出 `repair_stuck_import_tasks.py` 这个修复脚本。新表**不该再造一个**。做法是回收语义内建：

- 常量 `STALE_RUNNING_MINUTES = 30`
- 作业恢复时先执行：
  ```sql
  UPDATE wiki_compile_item SET status='PENDING'
  WHERE task_id = ? AND status='RUNNING'
    AND started_time < now() - interval '30 min';
  ```
- 每完成一项 bump 一次 `wiki_compile_task.heartbeat_time`，作业层同规则判定
- 于是"僵尸"退化为"等待回收"，无需人工介入

### 7.4 Token 归因

**已验证 `wiki_token_usage.import_task_id` 有真 FK** 指向 `wiki_import_task(id)`（`ON DELETE SET NULL`），compile 作业 id **塞不进去**。新增独立列：

```sql
ALTER TABLE wiki_token_usage ADD COLUMN compile_task_id BIGINT NULL
  REFERENCES wiki_compile_task(id) ON DELETE SET NULL;
CREATE INDEX ix_wiki_token_usage_compile_task ON wiki_token_usage(compile_task_id);
```

`import_task_id` 及其 FK 原样不动。两个作业体系各自独立计量，成本可按 task 归属。

### 7.5 迁移清单

| 对象 | 动作 |
|---|---|
| `wiki_compile_task` | 新建表 |
| `wiki_compile_item` | 新建表 |
| `wiki_token_usage.compile_task_id` | 加列 + FK + 索引 |
| **`knowledge_claim`** | **加 10 个可空列**（D3-1 选项 C）：`subject_id` / `predicate` / `object_value` / `object_type` / `confidence` / `authority_level` / `status` / `valid_from` / `valid_to` / `triple_stale` |
| **`evidence`** | **加 2 个可空列**：`content_hash` / `confidence` |
| **`evidence.page_number`** | **`VARCHAR(30)` → `INTEGER`**（与 P0 的 `TextBlock.page_number: int` 对齐，见 4.1）；现有 0 行，无回填风险 |

按 `数据库环境使用规范.md` 直接改 prod `qa_metadata`，**备份 `wiki_token_usage_20260912` 与 `knowledge_claim_20260912`**（被改的既有表；两表当前均 0 行，备份是走流程而非救命）。新表无数据可备份。

**为何所有新列均可空**：选项 C 的实质是**扩展而非替换** —— 现有列不动、现有读路径（`GET /claims`）不破，新列为严格超集。`subject_id` 初期大量为 `NULL` 是预期行为，不是缺陷；见 §八。

---

## 八、claim schema 决策：扩展而非替换（选项 C）

### 8.1 两套 schema 的实际差异

| | 原方案文档 | 当前实现 |
|---|---|---|
| claim 主键 | `claim_id` 业务键 | `id` BIGINT 代理键 |
| **主语** | **`subject_id` + `predicate` + `object_value` + `object_type`** | **无** —— 仅 `claim_text` 自由文本 |
| claim 挂靠 | `source_id` + `evidence_id` | `page_id` FK → `wiki_page` |
| evidence 挂靠 | `page_id`（页面锚定，**可被多条 claim 共享**） | `claim_id` NOT NULL FK（claim 子行，**不可共享**） |
| 置信度 | `confidence DECIMAL(5,4)` | 无 |
| 权威度位置 | **claim 上** | **page 上**（claim 级不存在） |
| 时效 | `valid_from` / `valid_to` | 无 |
| 定位符类型 | `page_number INT` / `paragraph_no INT` | `VARCHAR(30)` / `VARCHAR(30)` |

### 8.2 为何文档的 schema 更贴近远期目标

关键在 `subject_id`——**它是 Wiki 知识与本体架构的接合键**。文档 §37 自述 Claim 是"真正可验证的知识单元"，例子是 `Supplier-A HAS_STATUS Suspended`。

当前实现里 claim 是一句自由文本，**没有可寻址主语**，后果有三：

1. **无法按主体检索**：做不到 `WHERE subject_id = 'SUPPLIER-A'`。要回答"这家供应商的全部知识"，只能靠向量相似度近似。
2. **每次绑定本体都要重新抽取**：claim 与本体之间无持久连接，只能查询时让 LLM 现抽主语。不确定、不可索引、不可校验。
3. **文档 §39 的检索管线跑不起来**：该管线含显式 `Authority Filtering` 一阶，而权威度现挂在 page 上，claim 级不存在，也没有 `confidence` —— 页级过滤代替不了 claim 级过滤。

**决策（D3-1，修订）：采用选项 C —— 扩展而非替换。**

`claim_text` / `claim_type` / `embedding_ref` 全部保留，**追加可空列**：`subject_id` / `predicate` / `object_value` / `object_type` / `confidence` / `authority_level` / `status` / `valid_from` / `valid_to` / `triple_stale`。

理由：

1. **纯追加迁移**，不动现有列，`GET /claims` 与 `claim_text` 照常工作，无数据丢失。
2. **P3 本就要写迁移**，加可空列几乎零边际成本 —— 最贵的部分（实体解析层）得以推迟，但**地基方向一次做对**。
3. **抽取 prompt 只写一次**即产出双形态，永远不会付第二遍 LLM 成本。
4. `subject_id` **允许为 NULL 与自由文本**，不要求实体层先就位。
5. **缺口变为可测量**：`SELECT count(*) WHERE subject_id IS NULL` 就是实体解析层的精确待办量 —— 一个真实信号，会驱动 P4/P5 的优先级，优于"以后可能需要"的猜测。
6. 严格超集：若三元组实践证明 LLM 抽不准，列留空即可，不破坏任何东西。

### 8.3 选项 C 相对选项 B 的真实差异

B（直接上文档三元组）与 C 的**列终态相同**，C 是其超集。但两者有三处非时序性差异：

1. **`claim_text` 的去留**（终态差异）。B 的 claim 就是三元组，原句消失。三元组装不下"因质量问题自 2026-01 起"这类限定与语气，而带引用的问答要展示给人的正是原句；且**三元组可能抽错、原句不会** —— 只留三元组，一次抽取错误即静默污染知识库且无从发现。故即使成熟期，B 的终态在引用质量上是回退。
2. **实体层依赖**（能力差异）。`subject_id` 解析不到真东西时就只是占位符。**选项 C 不省掉实体层**，省掉的是"盲建"与"重付抽取成本"。实体层落地前，C 在能力上接近现状 —— 但它让未解析的 `subject_id` 值本身成为实体层的需求说明书（B 必须在动手前定死 `subject_id` 指向谁）。
3. **双表示一致性负担**（C 的新增成本）。有句子又有三元组就有"哪个是真的"问题。规则见 6.3：仅抽取时写入，人工编辑标记 `triple_stale`，不自动重抽。

**关系是 C ⊃ B，不是 C → B。** C 能在实体层落地后收敛到 B（删列）；B 要拿回原句必须重跑全部抽取、再付一遍 LLM 成本。

### 8.4 关于 `evidence` 挂靠方式：保留实现

文档的 `claim → evidence_id`（单数）意味一条 claim 只能引一条证据。当前实现是 `evidences` 列表（一对多），**在引用完整性上反而更好** —— 跨两段综合出的结论能同时引两段。故 **evidence 挂靠方式保留实现**，仅统一类型并补 `content_hash` / `confidence`（见 7.5）。

代价是 `evidence.content` 在一条段落支撑多条 claim 时会重复存储。当前无数据可评估，不预优化；若实测膨胀显著再议。

---

## 九、Harness 变更管理映射

按用户要求，每次升级或改动均按变更管理执行，开发过程受控。

### 9.1 十阶段工作流映射

每阶段（P0 / P1 / P3）独立走一遍：

| 阶段 | 本项目的落地物 |
|---|---|
| 需求分析 | 本文档对应章节 |
| 专家评审 | `code-reviewer` / `security-reviewer`（触发条件见 9.3） |
| 设计 | 本文档 + 该阶段 spec 拆分 |
| TDD RED | 先写测试，期望失败 |
| GREEN | 最小实现 |
| IMPROVE | 重构至覆盖率 ≥ 80% |
| 代码审查 | `code-reviewer` |
| 覆盖率验证 | ≥ 80% |
| 部署验证 | 真实数据验证（见 9.2） |
| 记录变更 | `Harness/changes/<feature>/summary.md` 九段模板 |

### 9.2 三道门禁

1. **开发门禁**：每轮需 `scripts/<feature>_realdata.py` 真实数据验证脚本 + summary §9「真实数据验证报告」。**缺失即该变更不通过。**
2. **部署门禁**：跨环境 `alembic_version` 一致性校验。
3. **备份门禁**：schema 变更前 14 天备份快照（P1 / P3 各触发一次）。

### 9.3 HITL 点

本项目触发两类，需人工确认：

1. **数据模型变更**（P1 加列、P3 建两表）：migration 脚本须人工 review。
2. **成本阈值变更**：P3 引入批量 LLM 编译，`total_cost_usd` 的量级变化需人工确认。

`security-reviewer` 强制触发场景（已命中）：文件系统操作（P0 对象存储）、数据库查询（P1/P3 迁移）、外部 API 调用（MinIO）、**用户输入入口的落库副作用**（P0 `preview-file`：上传字节 → 对象存储 + `document_catalog` 写入，含文件名净化以防路径穿越写进对象名）。

---

## 十、验收信号

每阶段的"做完了"判据，均可实测：

| 阶段 | 验收信号 |
|---|---|
| P0 | **RAG 路径**：上传一份已知 PDF → Milvus chunk 带正确 `page_number`；`document_catalog` 有真实 `storage_url` + 非空 `content_hash`；MinIO 桶内可回读且字节一致。**wiki 路径**：`preview-file` 上传同一份文件 → 同样留存源文件并登记 catalog，`content_hash` 取自**文件字节**（非草稿文本）；同一文件重复上传不产生第二行；被拒文件（格式不符）不留任何痕迹 |
| P1 | 同一文件导入两次 → `wiki_page` 恰 1 行；`wiki_learning_models.py:172-173` 的声称由测试钉死 |
| P3 | `knowledge_claim` / `evidence` 非零且 evidence 定位符非空；**claim 三元组双形态产出**（`claim_text` 与 `subject_id`/`predicate`/`object_value` 同批非空）；`SELECT count(*) FROM knowledge_claim WHERE subject_id IS NULL` 有确定值（实体层待办量）；`wiki_compile_item` 可续跑（杀进程后重跑只处理未完成项）；`wiki_token_usage.compile_task_id` 有值；`explain` 显示 wiki 列表查询不再拖 evidence |

---

## 十一、风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| P0 改 `parse_document` 返回类型 | 破坏 2 个生产调用方 + 3 个测试文件 | 影响面已确认可控；TDD 先改测试 |
| Milvus 集合重建 | 若 collection 非空则需数据迁移 | 已确认当前为空，零成本；执行前再验一次 |
| 备份 cron 静默失效 | schema 变更前无自动快照 | 手动执行 `./scripts/backup_pg.sh`；写入 summary §9 |
| P3 批量 LLM 成本 | 全量页面编译成本不可预估 | 先小批量试跑 + `preflight()` 前置校验；`total_cost_usd` 累积可见 |
| 软引用幽灵关系 | 覆盖率指标系统性高估 | 3.2 写入侧存在性校验 |
| `selectin` 降级遗漏端点 | 某处原本依赖自动加载的代码静默失效 | 降级 + 显式 `selectinload`；既有反退化测试兜底 |
| **三元组抽取准确率未知** | 抽错的主语会污染按主体检索的结果 | 双形态保留原句可比对；`confidence` 列落盘；`subject_id` 允许 NULL，宁可留空不硬猜 |
| **双表示漂移**（选项 C 特有） | `claim_text` 被人工编辑后三元组与句子不一致 | 6.3 规则：三元组仅抽取时写入，编辑时置 `triple_stale`，不自动重抽 |
| `evidence.page_number` 改 INT | 若历史数据含非数字页码会迁移失败 | 该表当前 0 行，无回填风险（执行前复查） |

---

## 十二、待办

- [ ] 本文档经用户审阅通过
- [ ] 按 P0 / P1 / P3 分别拆分实施计划（writing-plans）
- [ ] P0 实施前确认 Milvus collection 仍为空
- [ ] P1 / P3 迁移前手动执行备份
