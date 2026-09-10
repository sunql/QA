

> **不要建设一个“LLM 版文档库”，而是建设“业务对象 Wiki + 企业知识图谱 + Evidence 证据链 + Agent 检索层”。**
> 
> RAG 不废掉，而是从“知识库主体”变成 Wiki 的一个辅助检索能力。

---

# 企业 LLM Wiki 总体架构与落地方案

## 1. 项目定位

### 1.1 建设目标

建设面向企业 AI 应用的统一知识底座，将企业现有的：

- 制度文件
    
- 管理规范
    
- 产品资料
    
- 物料主数据
    
- BOM
    
- 工艺
    
- 供应商
    
- 客户
    
- 采购订单
    
- ERP/PLM/MES 数据
    
- 数据标准
    
- 指标口径
    
- FAQ
    
- 操作手册
    
- 历史业务经验
    

统一编译为可被 LLM / Agent 理解、检索、关联和推理的 **Enterprise LLM Wiki**。

最终形成：

> **业务对象标准化 + 知识页面化 + 知识关系化 + 证据可追溯 + 知识版本化 + Agent 可推理**

---

# 2. 总体架构

建议采用七层架构。

```text
┌─────────────────────────────────────────────────────────────┐
│                         AI 应用层                            │
│ 企业问答 │ 采购助手 │ 供应商助手 │ 研发助手 │ 数据助手 │ Agent │
├─────────────────────────────────────────────────────────────┤
│                       Agent / Reasoning                      │
│ Query理解 │ 任务拆解 │ Wiki Search │ Read │ Link Traversal │
│ SQL Agent │ API Agent │ Evidence验证 │ Answer Generation │
├─────────────────────────────────────────────────────────────┤
│                    Knowledge Service Layer                   │
│ Wiki Service │ Entity Service │ Relation Service            │
│ Evidence Service │ Search Service │ Version Service          │
├─────────────────────────────────────────────────────────────┤
│                     Enterprise LLM Wiki                     │
│                                                             │
│ Knowledge Page │ Business Object │ Entity │ Relation        │
│ Claim │ Evidence │ Source │ Version │ Authority              │
├─────────────────────────────────────────────────────────────┤
│                     Retrieval Layer                          │
│ Full Text │ BM25 │ Vector │ Hybrid │ Graph │ SQL │ API       │
├─────────────────────────────────────────────────────────────┤
│                     Knowledge Compiler                       │
│ 文档解析 │ 实体抽取 │ 关系抽取 │ 知识总结 │ 页面生成         │
│ 冲突检测 │ 来源绑定 │ 版本识别 │ 人工审核 │ 发布             │
├─────────────────────────────────────────────────────────────┤
│                     Enterprise Data                          │
│ SAP │ PLM │ MES │ WMS │ CRM │ OA │ DWH │ Data Lake │ Files   │
└─────────────────────────────────────────────────────────────┘
```

---

# 3. 核心设计原则

## 3.1 从“文档中心”转变为“知识中心”

传统 RAG：

```text
文档
 ↓
Chunk
 ↓
Embedding
 ↓
Vector DB
 ↓
Top K
 ↓
LLM
```

LLM Wiki：

```text
文档
 ↓
知识编译
 ↓
Knowledge Page
 ↓
Entity
 ↓
Relation
 ↓
Evidence
 ↓
Wiki Network
 ↓
Agent Reasoning
```

---

## 3.2 从“相似度检索”转向“检索 + 阅读 + 关系探索”

Agent 不只拥有：

```text
search()
```

而应该拥有：

```text
search()
read()
links()
backlinks()
find_entity()
find_relation()
get_evidence()
get_source()
get_version()
query_sql()
call_api()
```

---

## 3.3 Wiki 是知识主体，RAG 是检索能力之一

建议形成：

```text
                Enterprise Knowledge
                       │
        ┌──────────────┼──────────────┐
        ↓              ↓              ↓
       Wiki           RAG            Data
        │              │              │
    Page/Entity       Chunk        SQL/API
        │              │              │
        └──────────────┼──────────────┘
                       ↓
                     Agent
```

因此：

> 不建议把传统 RAG 完全删除。

---

# 4. Knowledge Page 模型

## 4.1 Page 基本结构

每一个知识页面建议采用统一模板：

```text
Knowledge Page
│
├── Identity
│   ├── Page ID
│   ├── Page Type
│   ├── Business Domain
│   └── Business Object
│
├── Definition
│   ├── Name
│   ├── Definition
│   ├── Business Meaning
│   └── Scope
│
├── Attributes
│   ├── Attribute
│   ├── Data Type
│   ├── Value
│   └── Business Meaning
│
├── Relations
│   ├── Parent
│   ├── Children
│   ├── Related Objects
│   └── Business Process
│
├── Rules
│   ├── Business Rules
│   ├── Calculation Rules
│   └── Constraints
│
├── Lifecycle
│
├── Evidence
│
├── Source
│
├── Authority
│
├── Version
│
├── Change History
│
└── Related Knowledge
```

---

# 5. Wiki Page 数据模型

建议定义如下核心字段。

|字段|说明|
|---|---|
|page_id|Wiki 页面唯一ID|
|page_code|页面业务编码|
|page_type|页面类型|
|title|页面标题|
|domain|业务域|
|object_type|业务对象类型|
|entity_id|对应业务实体|
|summary|LLM生成摘要|
|content|Markdown/结构化内容|
|status|Draft/Review/Published/Expired|
|authority_level|权威等级|
|owner|知识责任人|
|reviewer|审核人|
|source_count|来源数量|
|confidence|知识可信度|
|version|页面版本|
|effective_from|生效时间|
|effective_to|失效时间|
|created_at|创建时间|
|updated_at|更新时间|

---

# 6. Knowledge Page 类型

建议第一阶段定义 12 类。

|类型|示例|
|---|---|
|BUSINESS_OBJECT|物料、供应商、客户|
|CONCEPT|采购、BOM、库存|
|RULE|采购审批规则|
|PROCESS|采购流程|
|POLICY|采购管理制度|
|PRODUCT|产品|
|MATERIAL|物料|
|SUPPLIER|供应商|
|DOCUMENT|制度文件|
|FAQ|常见问题|
|METRIC|采购金额、供应商交付率|
|DATA_OBJECT|数据表、数据集|

后续再增加：

```text
EQUIPMENT
PROCESS_PLAN
BOM
OPERATION
RESOURCE
ORGANIZATION
CUSTOMER
CONTRACT
ORDER
QUALITY_RULE
```

---

# 7. 企业业务对象模型

这是整个项目最重要的模型。

建议采用：

```text
业务域
 ↓
业务对象
 ↓
业务实体
 ↓
属性
 ↓
关系
 ↓
知识
 ↓
证据
```

例如采购域：

```text
采购域
│
├── 物料
├── 供应商
├── 采购申请
├── 询价
├── 采购合同
├── 采购订单
├── 收货
├── 入库
├── 发票
├── 付款
└── 供应商评价
```

---

# 8. Business Object 模型

例如 Material：

```json
{
  "object_type": "Material",
  "object_code": "MAT",
  "business_domain": "SupplyChain",
  "display_name": "物料",
  "key": "material_code",
  "attributes": [
    "material_code",
    "material_name",
    "material_category",
    "material_group",
    "base_uom",
    "status",
    "specification",
    "brand",
    "grade"
  ]
}
```

---

# 9. Entity 模型

Entity 是 Business Object 的具体实例。

例如：

```text
Business Object：
Material

Entity：
MAT-100001
```

实体：

```json
{
  "entity_id": "MAT-100001",
  "object_type": "Material",
  "code": "MAT-100001",
  "name": "铝板 6061-T6 2mm",
  "attributes": {
    "material_category": "金属原材料",
    "material_grade": "6061-T6",
    "thickness": "2mm",
    "uom": "KG"
  },
  "source_system": "SAP"
}
```

---

# 10. Relation 模型

关系采用：

```text
Subject
+
Relation
+
Object
```

例如：

```text
MAT-100001
    │
    ├── belongs_to ──> 金属原材料
    │
    ├── supplied_by ──> SUP-000328
    │
    ├── used_in ──> BOM-100023
    │
    ├── purchased_by ──> PO-20260901
    │
    └── inspected_by ──> IQC-RULE-001
```

---

# 11. Relation 标准

建议统一定义关系字典。

| Relation     | 中文     | 示例        |
| ------------ | ------ | --------- |
| belongs_to   | 属于     | 物料→分类     |
| contains     | 包含     | BOM→物料    |
| used_in      | 用于     | 物料→BOM    |
| supplied_by  | 由...供应 | 物料→供应商    |
| purchased_by | 被采购    | 物料→采购订单   |
| applies_to   | 适用于    | 规则→物料     |
| governed_by  | 受...管理 | 采购→制度     |
| defined_by   | 由...定义 | 指标→指标口径   |
| derived_from | 来源于    | Wiki→文档   |
| supersedes   | 替代     | V3.2→V3.1 |
| related_to   | 关联     | 知识→知识     |
| depends_on   | 依赖     | 流程→数据     |
| produces     | 产生     | 采购流程→采购订单 |
| consumes     | 消耗     | 生产订单→物料   |

---

# 12. Relation 必须带属性

不要只保存：

```text
Material --supplied_by--> Supplier
```

应该保存：

```json
{
  "subject": "MAT-100001",
  "relation": "supplied_by",
  "object": "SUP-000328",
  "properties": {
    "plant": "1000",
    "valid_from": "2026-01-01",
    "valid_to": null,
    "priority": 1,
    "status": "ACTIVE",
    "source": "SAP"
  }
}
```

这样才能处理企业真实业务中的：

- 多工厂
    
- 多版本
    
- 生效日期
    
- 替代关系
    
- 主供应商
    
- 历史关系
    

---

# 13. Evidence 证据模型

LLM Wiki 必须解决一个核心问题：

> “你说的这个知识，到底从哪里来的？”

因此所有重要知识必须绑定 Evidence。

例如：

```text
Knowledge
   ↓
Evidence
   ↓
Source Document
   ↓
Page / Paragraph
```

Evidence：

```json
{
  "evidence_id": "EV-100023",
  "page_id": "WIKI-PO-001",
  "source_type": "DOCUMENT",
  "source_id": "DOC-2026-001",
  "page_number": 18,
  "paragraph": 4,
  "quote": "采购金额超过100万元须经总经理审批",
  "confidence": 0.98
}
```

---

# 14. Authority 权威模型

企业知识不能只看相似度。

建议建立：

```text
Authority Level

L5：正式制度/法律/标准
L4：正式业务规范
L3：系统主数据/业务数据
L2：业务部门确认知识
L1：AI推导知识
L0：未经验证
```

例如：

```text
采购审批金额 = 100万元

Authority = L5
Source = 《采购管理制度V3.2》
Status = Effective
```

Agent 应优先采用高权威、当前有效的知识。

---

# 15. Knowledge Version

所有重要知识必须版本化。

```text
V1.0
 ↓
V2.0
 ↓
V3.0
 ↓
V3.1
 ↓
V3.2
```

同时维护：

```text
created
effective
expired
superseded
```

例如：

```text
采购管理制度 V3.1
       │
       └── superseded_by
                  ↓
采购管理制度 V3.2
```

---

# 16. Knowledge Compiler

知识编译是 LLM Wiki 的核心生产线。

总体流程：

```text
原始数据
   ↓
数据接入
   ↓
文档解析
   ↓
内容清洗
   ↓
结构识别
   ↓
实体识别
   ↓
关系抽取
   ↓
规则抽取
   ↓
Knowledge Claim
   ↓
Source/Evidence绑定
   ↓
Knowledge Page生成
   ↓
冲突检测
   ↓
质量评分
   ↓
人工审核
   ↓
发布
```

---

# 17. Knowledge Compiler 输入

支持：

```text
PDF
Word
Excel
PPT
HTML
Markdown
邮件
FAQ
数据库
SAP
PLM
MES
WMS
CRM
数据仓库
数据湖
API
```

---

# 18. 文档知识编译

例如原始文档：

```text
《采购管理制度V3.2》
```

LLM Compiler 自动识别：

```text
Document
   │
   ├── Concept
   │
   ├── Rule
   │
   ├── Process
   │
   ├── Role
   │
   ├── Organization
   │
   └── Approval Rule
```

例如：

```text
采购金额 > 100万
→ 总经理审批
```

生成：

```text
Rule:
PurchaseAmountApprovalRule

Condition:
purchase_amount > 1000000

Action:
require_approval("General Manager")

Source:
采购管理制度V3.2
```

---

# 19. 冲突检测

这是企业 Wiki 必须具备的能力。

例如：

```text
制度A：
采购 > 50万 → 总经理审批

制度B：
采购 > 100万 → 总经理审批
```

Compiler 不允许直接覆盖。

生成：

```text
Conflict
├── Claim A
├── Claim B
├── Source A
├── Source B
└── Resolution Status
```

然后：

```text
AI检测
 ↓
业务专家确认
 ↓
确定权威来源
 ↓
更新 Wiki
```

---

# 20. 知识质量评分

建议建立 Knowledge Score：

```text
Knowledge Score
=
Source Authority
× Evidence Completeness
× Freshness
× Consistency
× Human Verification
```

可以具体拆成：

|指标|权重|
|---|--:|
|来源权威性|25%|
|证据完整性|20%|
|时效性|15%|
|数据一致性|15%|
|人工审核|15%|
|关系完整性|10%|

---

# 21. Agent Tool API

建议对 Agent 暴露标准工具。

## 21.1 wiki_search

```http
POST /api/wiki/search
```

请求：

```json
{
  "query": "采购金额超过多少需要总经理审批？",
  "domain": "procurement",
  "top_k": 10
}
```

返回：

```json
{
  "results": [
    {
      "page_id": "RULE-PO-001",
      "title": "采购审批规则",
      "score": 0.96,
      "authority": "L5",
      "status": "EFFECTIVE"
    }
  ]
}
```

---

# 22. wiki_read

```http
GET /api/wiki/pages/{page_id}
```

返回：

```json
{
  "page_id": "RULE-PO-001",
  "title": "采购审批规则",
  "summary": "...",
  "content": "...",
  "relations": [],
  "evidence": [],
  "version": "3.2"
}
```

---

# 23. wiki_links

```http
GET /api/wiki/pages/{page_id}/links
```

返回：

```json
{
  "links": [
    {
      "relation": "governed_by",
      "target": "POLICY-PO-001"
    },
    {
      "relation": "applies_to",
      "target": "PO"
    }
  ]
}
```

---

# 24. entity_search

```http
POST /api/entities/search
```

例如：

```json
{
  "object_type": "Material",
  "keyword": "6061铝板"
}
```

---

# 25. relation_query

```http
POST /api/graph/query
```

例如：

```json
{
  "start": "MAT-100001",
  "relation": "supplied_by",
  "depth": 2
}
```

---

# 26. evidence_get

```http
GET /api/evidence/{evidence_id}
```

返回：

```json
{
  "source": "采购管理制度V3.2",
  "page": 18,
  "paragraph": 4,
  "content": "..."
}
```

---

# 27. SQL Agent

企业知识不能全部 Wiki 化。

例如：

> “今年采购金额最高的10家供应商是谁？”

这不是 Wiki 查询。

Agent 应调用：

```text
query_sql()
```

例如：

```json
{
  "sql": "SELECT supplier_id, SUM(amount) ..."
}
```

所以 Agent 工具体系应是：

```text
Wiki Tools
├── wiki_search
├── wiki_read
├── wiki_links
├── entity_search
├── relation_query
└── evidence_get

Data Tools
├── query_sql
├── query_metric
└── query_api
```

---

# 28. Agent 推理流程

用户：

> “为什么供应商A最近不能采购物料X？”

Agent：

```text
Step 1
search("供应商A")
        ↓
Supplier Wiki

Step 2
search("物料X")
        ↓
Material Wiki

Step 3
links(Supplier A)
        ↓
Supplier Evaluation

Step 4
links(Material X)
        ↓
Approved Supplier

Step 5
read(Quality Rule)

Step 6
query_sql(Purchase History)

Step 7
Evidence Verification

Step 8
LLM Reasoning
```

最终回答：

```text
供应商A当前不能采购物料X。

主要原因：
1. 供应商当前状态为“暂停采购”
2. 最近一次质量评价等级为C
3. 物料X要求供应商等级至少为B
4. 采购系统当前未存在有效的供应商-物料批准关系

依据：
《供应商管理制度V5.1》
《物料采购准入规则V2.3》
SAP供应商主数据
SAP采购批准关系
```

这就是 Wiki + Graph + SQL + Evidence 的价值。

---

# 29. 数据库总体设计

建议第一阶段采用：

```text
PostgreSQL
      │
      ├── Wiki
      ├── Entity
      ├── Relation
      ├── Evidence
      ├── Source
      ├── Version
      └── Audit
```

搜索：

```text
OpenSearch
```

图：

```text
Neo4j
```

对象存储：

```text
S3 / MinIO
```

数据湖：

```text
Iceberg
```

---

# 30. PostgreSQL 核心表

## wiki_page

```sql
CREATE TABLE wiki_page (
    page_id         VARCHAR(64) PRIMARY KEY,
    page_code       VARCHAR(128),
    page_type       VARCHAR(50),
    domain          VARCHAR(100),
    object_type     VARCHAR(100),
    entity_id       VARCHAR(128),
    title           VARCHAR(500),
    summary         TEXT,
    content         TEXT,
    status          VARCHAR(30),
    authority_level VARCHAR(10),
    confidence      DECIMAL(5,4),
    owner           VARCHAR(100),
    reviewer        VARCHAR(100),
    version         VARCHAR(30),
    effective_from  TIMESTAMP,
    effective_to    TIMESTAMP,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
);
```

---

# 31. business_object

```sql
CREATE TABLE business_object (
    object_type     VARCHAR(100) PRIMARY KEY,
    object_name     VARCHAR(200),
    domain          VARCHAR(100),
    description     TEXT,
    key_attribute   VARCHAR(100),
    status          VARCHAR(30)
);
```

---

# 32. entity

```sql
CREATE TABLE entity (
    entity_id       VARCHAR(128) PRIMARY KEY,
    object_type     VARCHAR(100),
    entity_code     VARCHAR(200),
    entity_name     VARCHAR(500),
    attributes      JSONB,
    source_system   VARCHAR(100),
    source_id       VARCHAR(200),
    status          VARCHAR(30),
    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
);
```

---

# 33. relation

```sql
CREATE TABLE relation (
    relation_id     BIGSERIAL PRIMARY KEY,
    subject_id      VARCHAR(128),
    relation_type   VARCHAR(100),
    object_id       VARCHAR(128),
    properties      JSONB,
    source_id       VARCHAR(128),
    confidence      DECIMAL(5,4),
    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP,
    status          VARCHAR(30)
);
```

---

# 34. evidence

```sql
CREATE TABLE evidence (
    evidence_id     VARCHAR(128) PRIMARY KEY,
    page_id         VARCHAR(128),
    source_id       VARCHAR(128),
    source_type     VARCHAR(50),
    page_number     INT,
    section_name    VARCHAR(500),
    paragraph_no    INT,
    content         TEXT,
    content_hash    VARCHAR(128),
    confidence      DECIMAL(5,4),
    created_at      TIMESTAMP
);
```

---

# 35. source_document

```sql
CREATE TABLE source_document (
    source_id       VARCHAR(128) PRIMARY KEY,
    source_name     VARCHAR(500),
    source_type     VARCHAR(50),
    source_system   VARCHAR(100),
    file_uri        TEXT,
    document_version VARCHAR(50),
    authority_level VARCHAR(10),
    effective_from  TIMESTAMP,
    effective_to    TIMESTAMP,
    status          VARCHAR(30),
    content_hash    VARCHAR(128),
    created_at      TIMESTAMP
);
```

---

# 36. knowledge_claim

建议额外增加 Claim 表。

```sql
CREATE TABLE knowledge_claim (
    claim_id        VARCHAR(128) PRIMARY KEY,
    subject_id      VARCHAR(128),
    predicate       VARCHAR(200),
    object_value    TEXT,
    object_type     VARCHAR(50),
    source_id       VARCHAR(128),
    evidence_id     VARCHAR(128),
    confidence      DECIMAL(5,4),
    authority_level VARCHAR(10),
    status          VARCHAR(30),
    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP
);
```

Claim 是：

> LLM 从原始材料中提取出来的“最小知识单元”。

例如：

```text
Supplier-A
HAS_STATUS
Suspended
```

---

# 37. 为什么需要 Claim？

因为：

```text
Document
```

太粗。

```text
Page
```

仍然太粗。

```text
Claim
```

才是真正可验证的知识单元。

例如：

```text
供应商A暂停采购
```

可以绑定：

```text
Claim
 ↓
Evidence
 ↓
Document
 ↓
Page 18
 ↓
Paragraph 3
```

这对企业 AI 的可信性非常重要。

---

# 38. 向量索引

Wiki Page 仍然需要 embedding。

但 embedding 对象不再只是 Chunk。

建议：

```text
Page Embedding
Entity Embedding
Claim Embedding
Document Chunk Embedding
```

形成：

```text
Knowledge Page
      ↓
Summary Embedding

Entity
      ↓
Description Embedding

Claim
      ↓
Claim Embedding

Original Document
      ↓
Chunk Embedding
```

---

# 39. 搜索策略

采用四路检索：

```text
Keyword Search
      +
Semantic Search
      +
Entity Search
      +
Graph Traversal
```

最后：

```text
Candidate
 ↓
Authority Filtering
 ↓
Version Filtering
 ↓
Business Scope Filtering
 ↓
Rerank
 ↓
Evidence Validation
 ↓
LLM
```

---

# 40. 推荐搜索评分

可以设计：

```text
Final Score =
0.25 × Keyword Score
+
0.25 × Semantic Score
+
0.20 × Entity Score
+
0.15 × Graph Score
+
0.10 × Authority Score
+
0.05 × Freshness Score
```

实际项目上线后通过评测集调整。

---

# 41. 采购域 Wiki 模型

采购域建议第一批建设。

总体模型：

```text
                 Procurement
                     │
      ┌──────────────┼──────────────┐
      ↓              ↓              ↓
   Material       Supplier       Employee
      │              │
      │              ├── Evaluation
      │              ├── Qualification
      │              └── Contract
      │
      ├──────────────┐
      ↓              ↓
 Purchase Request  RFQ
      │              │
      └───────┬──────┘
              ↓
        Purchase Order
              │
          ┌───┴────┐
          ↓        ↓
       Receipt   Invoice
          │
          ↓
        Payment
```

---

# 42. 采购域核心 Business Objects

第一阶段建议：

```text
Material
Supplier
PurchaseRequest
RFQ
Quotation
PurchaseOrder
Receipt
Invoice
Payment
SupplierEvaluation
PurchaseContract
ProcurementPolicy
ProcurementRule
```

---

# 43. 采购域核心关系

```text
PurchaseRequest
    ├── requests → Material
    ├── requested_by → Employee
    └── converted_to → PurchaseOrder

PurchaseOrder
    ├── purchases → Material
    ├── placed_to → Supplier
    ├── based_on → Quotation
    ├── governed_by → ProcurementPolicy
    └── results_in → Receipt

Supplier
    ├── supplies → Material
    ├── evaluated_by → SupplierEvaluation
    └── governed_by → SupplierPolicy

Material
    ├── belongs_to → MaterialCategory
    ├── supplied_by → Supplier
    └── inspected_by → QualityRule
```

---

# 44. 采购域 Wiki Page 示例：物料

```markdown
# MAT-100001 铝板 6061-T6 2mm

## 基本信息

对象类型：Material
业务域：采购
物料编码：MAT-100001
物料名称：铝板 6061-T6 2mm

## 属性

材质：6061-T6
厚度：2mm
单位：KG
物料类别：金属原材料
状态：启用

## 采购关系

主供应商：
SUP-000328

备选供应商：
SUP-000421

## 质量要求

必须满足：
GB/T XXXX

## 采购规则

最低采购批量：
500 KG

## 关联知识

[[物料分类]]
[[铝材采购规范]]
[[来料检验规范]]
[[供应商准入规则]]

## 数据来源

SAP

## 权威来源

《原材料采购管理规范V2.1》
```

---

# 45. 采购域 Wiki Page 示例：供应商

```markdown
# SUP-000328 XX铝材有限公司

## 基本信息

供应商编码：SUP-000328
供应商名称：XX铝材有限公司
供应商类型：原材料供应商

## 状态

正常

## 供应物料

MAT-100001
MAT-100002
MAT-100008

## 供应商等级

A级

## 质量评价

最近一次评价：
A

## 交付表现

OTD：96.8%

## 关联知识

[[供应商准入规则]]
[[供应商评价规则]]
[[铝板采购规范]]
```

---

# 46. 采购域 Wiki Page 示例：规则

```markdown
# 采购审批规则

## 规则编号

PO-RULE-001

## 条件

采购订单金额 > 100万元

## 审批要求

必须经过总经理审批。

## 适用范围

中国区采购业务。

## 来源

《采购管理制度V3.2》

## 权威等级

L5

## 生效日期

2026-01-01

## 当前状态

Effective
```

---

# 47. 采购问题的 Agent 推理示例

用户：

> “MAT-100001 为什么现在只能向 SUP-000328 采购？”

Agent：

```text
1. read(Material)
        ↓
2. links(Material)
        ↓
3. find Supplier relationships
        ↓
4. read Supplier
        ↓
5. read Supplier Qualification
        ↓
6. read Material Procurement Rule
        ↓
7. query SAP approved supplier relation
        ↓
8. evidence validation
        ↓
9. answer
```

回答：

```text
MAT-100001 当前只有 SUP-000328
存在有效采购批准关系。

原因：
1. SUP-000328 为该物料当前主供应商；
2. SUP-000421 的供应商资格已于2026-07-31到期；
3. SUP-000512 虽然供应该类物料，但尚未通过该物料的准入认证；
4. SAP 当前有效批准供应商关系仅保留 SUP-000328。

依据：
《原材料供应商准入规则V3.1》
供应商资格记录
SAP批准供应商关系
```

---

# 48. 技术架构

推荐第一阶段采用：

```text
                    LLM
                     │
                Agent Framework
                     │
              Knowledge API
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
   PostgreSQL     OpenSearch     Neo4j
        │            │            │
    Wiki/Entity   Search/Vector   Graph
        │            │            │
        └────────────┼────────────┘
                     ↓
                Object Storage
                  MinIO/S3
                     │
                     ↓
             Enterprise Systems
```

---

# 49. PostgreSQL

负责：

```text
Wiki Page
Entity
Claim
Evidence
Source
Version
Authority
Audit
Metadata
```

优点：

- 结构化数据成熟
    
- JSONB 支持灵活属性
    
- 事务能力强
    
- 适合作为系统主数据库
    

如果第一阶段规模不大，也可以直接使用 PostgreSQL + pgvector，把向量检索先收敛到一个数据库中。

---

# 50. OpenSearch

负责：

```text
全文检索
BM25
Semantic Search
Vector Search
Hybrid Search
Rerank
```

推荐采用：

```text
Keyword
+
Vector
+
Filter
```

的 Hybrid Search。

---

# 51. Neo4j

负责：

```text
Entity
Relation
Graph Traversal
Multi-hop Query
```

例如：

```text
Material
 ↓
BOM
 ↓
Product
 ↓
Supplier
 ↓
Quality
 ↓
Customer
```

适合解决：

> “A 与 B 之间有什么关系？”

而不是：

> “哪篇文档里出现过A？”

---

# 52. Object Storage

用于保存：

```text
原始PDF
Word
Excel
PPT
图片
附件
原始数据文件
解析后的Markdown
页面快照
```

推荐：

```text
MinIO
```

企业已有云环境则使用：

```text
S3 / OSS / COS / OBS
```

---

# 53. Data Lake

如果企业已经在建设数据底座：

```text
SAP
PLM
MES
WMS
CRM
     ↓
ODS
     ↓
DWD
     ↓
DWS
     ↓
Data Lake / Iceberg
```

LLM Wiki 不应该复制这些业务数据。

而应该：

> **引用业务数据的标准实体和查询接口。**

即：

```text
Wiki
  ↓
Entity ID
  ↓
Data Service
  ↓
Data Lake / DWH
```

---

# 54. 推荐技术栈

## MVP

```text
Backend:
Python + FastAPI

Database:
PostgreSQL

Vector:
pgvector

Search:
OpenSearch

Graph:
Neo4j

Object:
MinIO

Workflow:
Temporal / Airflow

LLM:
企业私有LLM / API

Embedding:
企业Embedding Model

Frontend:
React / Next.js

Agent:
LangGraph / 自研Agent Runtime
```

---

# 55. 为什么 MVP 不建议一开始堆太多组件？

第一阶段：

```text
PostgreSQL
+
pgvector
+
OpenSearch
+
MinIO
```

已经可以完成：

```text
Wiki
Entity
Evidence
全文
向量
Hybrid Search
```

Graph DB 可以在关系复杂后再引入。

如果一开始就：

```text
Kafka
Spark
Flink
Iceberg
Neo4j
Milvus
OpenSearch
ES
Redis
...
```

很容易把项目变成基础设施项目，而不是 AI 知识项目。

---

# 56. 企业部署架构

建议：

```text
                  API Gateway
                       │
          ┌────────────┼────────────┐
          ↓            ↓            ↓
       Web App       Agent API    Admin
                       │
                  Wiki Service
                       │
       ┌───────────────┼──────────────┐
       ↓               ↓              ↓
   PostgreSQL      OpenSearch       Neo4j
       │               │              │
       └───────────────┼──────────────┘
                       ↓
                     MinIO
                       │
                Knowledge Compiler
                       │
             ┌─────────┼─────────┐
             ↓         ↓         ↓
            SAP       PLM       Documents
```

---

# 57. 安全模型

企业 Wiki 必须考虑权限。

不能出现：

```text
用户A
→ search
→ 得到所有知识
```

建议：

```text
User
 ↓
Organization
 ↓
Role
 ↓
Domain
 ↓
Knowledge Permission
```

例如：

```text
采购部：
采购知识 ✓

研发部：
研发知识 ✓
采购价格 ✗

财务部：
采购价格 ✓
供应商合同 ✓
```

---

# 58. Knowledge ACL

Page 增加：

```text
visibility
security_level
allowed_roles
allowed_orgs
```

例如：

```json
{
  "security_level": "L3",
  "allowed_roles": [
    "PROCUREMENT_MANAGER",
    "FINANCE_MANAGER"
  ]
}
```

Agent 检索时必须进行权限过滤。

---

# 59. Knowledge Lifecycle

```text
Draft
 ↓
AI Generated
 ↓
Pending Review
 ↓
Business Review
 ↓
Approved
 ↓
Published
 ↓
Effective
 ↓
Expired
 ↓
Archived
```

AI 生成知识不能直接进入：

```text
Effective
```

必须根据知识等级决定是否人工审核。

---

# 60. 人机协同

建议：

```text
AI负责：
发现
抽取
总结
关联
生成
检测冲突

人负责：
确认
授权
裁决
发布
```

尤其是：

```text
规则
制度
审批
财务
质量
安全
合规
```

必须设置人工审核。

---

# 61. 知识治理组织

建议建立：

```text
企业知识委员会
        │
        ├── 数据治理团队
        ├── IT/AI团队
        ├── 业务专家
        └── 数据/知识管理员
```

每个业务域设置：

```text
Domain Owner
Knowledge Owner
Knowledge Reviewer
Technical Owner
```

例如采购域：

```text
采购总监
 ↓
采购知识Owner
 ↓
采购业务专家
 ↓
数据治理团队
 ↓
AI平台团队
```

---

# 62. 项目实施路线

## Phase 0：架构设计

2周

输出：

```text
企业知识架构
业务域划分
Business Object模型
Relation字典
Knowledge Page标准
Evidence标准
权限模型
```

---

## Phase 1：采购域 MVP

4周

建设：

```text
Material
Supplier
Purchase Order
Procurement Rule
Procurement Policy
```

实现：

```text
Wiki
Search
Entity
Relation
Evidence
Agent
```

---

## Phase 2：系统数据接入

4~6周

接入：

```text
SAP
PLM
采购制度
采购规范
供应商资料
物料主数据
```

实现：

```text
自动知识编译
Entity Matching
Relation Extraction
Evidence Binding
```

---

## Phase 3：Agent

4周

建设：

```text
Procurement Agent
Supplier Agent
Material Agent
Data Agent
```

---

## Phase 4：企业级推广

8~12周

扩展：

```text
研发
生产
质量
仓储
销售
财务
设备
```

最终形成：

```text
Enterprise Knowledge Wiki
```

---

# 63. 第一阶段不要追求“全企业知识”

建议只做一个业务域：

# 采购域

原因：

采购域同时具备：

```text
主数据
+
业务流程
+
制度
+
结构化数据
+
非结构化文档
+
业务关系
+
实时数据
```

非常适合作为 LLM Wiki 的验证场景。

---

# 64. 采购域 MVP 数据范围

建议第一批：

### 主数据

```text
物料
供应商
组织
人员
```

### 业务对象

```text
采购申请
询价
报价
采购订单
收货
发票
付款
```

### 知识

```text
采购制度
供应商制度
物料采购规则
审批规则
质量规则
FAQ
```

---

# 65. MVP 验收指标

不要只看：

```text
回答是否正确
```

建议建立：

|指标|目标|
|---|--:|
|Wiki覆盖率|≥80%|
|Entity识别准确率|≥95%|
|Relation准确率|≥90%|
|Evidence绑定率|≥95%|
|权威知识命中率|≥95%|
|问答准确率|≥90%|
|引用正确率|≥95%|
|无依据回答率|≤3%|
|平均响应时间|≤5秒|

---

# 66. 最终企业知识架构

最终建议形成：

```text
                    Enterprise AI
                         │
                    Agent Platform
                         │
               ┌─────────┴─────────┐
               │ Knowledge Router  │
               └─────────┬─────────┘
                         │
       ┌─────────────────┼──────────────────┐
       ↓                 ↓                  ↓
 Enterprise Wiki      Data Agent        API Agent
       │                 │                  │
       ↓                 ↓                  ↓
 Knowledge Graph       DWH/Lake           ERP/MES
       │
 ┌─────┼─────┬─────┬─────┐
 ↓     ↓     ↓     ↓     ↓
Page Entity Claim Evidence Source
 │
 ↓
Business Objects
 │
 ├── Material
 ├── Supplier
 ├── Product
 ├── BOM
 ├── Process
 ├── Equipment
 ├── Customer
 ├── Order
 └── Organization
```

---

# 67. 与企业数据底座的最终关系

你现有的数据底座可以继续保持：

```text
ODS
 ↓
DWD
 ↓
DWS
 ↓
ADS
```

旁边增加：

```text
              Enterprise Data Foundation
                         │
        ┌────────────────┴────────────────┐
        ↓                                 ↓
 Structured Data                   Knowledge Data
        │                                 │
 DWH / Data Lake                    LLM Wiki
        │                                 │
 SQL/API                         Page/Entity/Relation
        │                                 │
        └──────────────┬──────────────────┘
                       ↓
                    AI Agent
```

因此：

> **LLM Wiki 不是替代数据仓库，也不是替代数据湖，更不是简单替代 RAG。**

它是把：

> **结构化企业数据 + 非结构化企业知识 + 业务对象 + 业务关系**

统一暴露给 LLM/Agent 的**企业知识语义层**。

---

# 68. 最终定位

建议把整个体系正式定义为：

## Enterprise AI Knowledge Layer

中文：

# 企业 AI 知识底座

由四个核心部分组成：

```text
① Business Object Layer
统一业务对象

② Knowledge Wiki
统一企业知识

③ Knowledge Graph
统一业务关系

④ Evidence Layer
统一知识证据
```

上面再提供：

```text
Agent
RAG
SQL
API
Graph Reasoning
```

---

# 69. 最终一句话架构

整个方案可以浓缩为：

```text
统一业务对象
      +
统一知识页面
      +
统一业务关系
      +
统一证据来源
      +
统一知识版本
      +
统一权限
      +
Hybrid Retrieval
      +
Agent Reasoning
      =
Enterprise LLM Wiki
```

而它最终解决的不是：

> “如何让 LLM 找到一段文档？”

而是：

> **“让 LLM 真正理解企业有什么业务对象、这些对象是什么、彼此有什么关系、哪些规则当前有效、知识来自哪里，以及遇到复杂问题时应该沿着什么路径进行推理。”**

---

# 70. 建议的第一批交付物

如果正式启动项目，建议最终形成以下 12 个核心成果：

1. 《企业 LLM Wiki 总体架构》
    
2. 《企业 Business Object 模型》
    
3. 《Knowledge Page 标准》
    
4. 《Entity/Relation 模型》
    
5. 《企业关系字典》
    
6. 《Knowledge Claim 标准》
    
7. 《Evidence 证据模型》
    
8. 《Knowledge Authority 标准》
    
9. 《Knowledge Compiler 设计》
    
10. 《Agent Tool API 规范》
    
11. 《采购域 LLM Wiki MVP》
    
12. 《企业 AI Knowledge Layer 技术架构》
    

其中最值得优先做的不是 UI，而是：

> **Business Object + Page Model + Relation Model + Evidence Model + Agent API**

这五个东西一旦定下来，后面的数据库、Graph、Search、Agent、前端都可以围绕它们展开。

### 技术选型上的几个补充判断

- **OpenSearch** 很适合承担 Wiki 的全文 + 向量 + Hybrid Search；官方文档目前明确支持关键词与语义搜索组合，并支持 BM25、向量和混合检索。([OpenSearch Documentation](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/?utm_source=chatgpt.com "Hybrid search | OpenSearch Documentation"))
    
- **Neo4j** 适合承担 Entity/Relation 和多跳关系查询；关系本身就是图模型提供上下文和复杂查询能力的核心。([Neo4j Graph Intelligence Platform](https://neo4j.com/docs/graphql/current/relationships/?utm_source=chatgpt.com "Relationships - Neo4j GraphQL Library"))
    
- 如果第一期数据量不大，**PostgreSQL + pgvector** 可以把 Wiki 元数据、Entity、Evidence 和向量先收敛在一起，避免一开始引入过多组件；pgvector 本身支持向量字段和近邻检索。([GitHub](https://github.com/pgvector/pgvector?utm_source=chatgpt.com "GitHub - pgvector/pgvector: Open-source vector similarity search for Postgres · GitHub"))
    
- 你之前规划的数据湖如果采用 **Iceberg**，建议继续作为结构化数据底座，不要让 LLM Wiki 复制一份业务数据；Wiki 只保存业务对象标识、语义、关系和知识，需要事实数据时通过 SQL/API 回查数据湖/数仓。Iceberg 本身负责表元数据、Schema/Partition 演进和快照等数据湖能力。([冰山](https://iceberg.apache.org/spec/?utm_source=chatgpt.com "Spec - Apache Iceberg™"))
    

**结合你现在的数据底座建设，我尤其建议把“统一业务对象 + 统一业务语义 + 结构化数据关系 + 非结构化知识关联”直接升级为这套 LLM Wiki 的 Business Object Layer。** 这样你前面做的数据治理工作不会与 AI 知识库割裂，而是直接变成企业 AI 的语义底座。