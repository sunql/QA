好。建议我们直接把这套内容做成一套**可落地的《企业 LLM Wiki 标准与实施模板包》**。

我建议按下面顺序展开，因为前面的模型会直接影响后面的数据库、Compiler 和 Agent：

**第一组：知识模型标准**

1. 《企业 LLM Wiki 总体架构》
    
2. 《企业 Business Object 模型》
    
3. 《Knowledge Page 标准》
    
4. 《Entity/Relation 模型》
    
5. 《企业关系字典》
    
6. 《Knowledge Claim 标准》
    
7. 《Evidence 证据模型》
    
8. 《Knowledge Authority 标准》
    

**第二组：技术实现标准**  
9. 《Knowledge Compiler 设计》  
10. 《Agent Tool API 规范》

**第三组：落地实施**  
11. 《采购域 LLM Wiki MVP》  
12. 《企业 AI Knowledge Layer 技术架构》

每份我都会统一采用：

> **标准定义 → 模型说明 → 字段模板 → 数据字典 → JSON/SQL样例 → 采购域实例 → 使用规则 → 治理要求**

其中第 **11《采购域 LLM Wiki MVP》** 会把前面 1～10 的模型全部串起来，形成一个完整案例，例如：

```text
采购域
│
├── Business Object
│   ├── Material
│   ├── Supplier
│   ├── Purchase Request
│   ├── RFQ
│   ├── Quotation
│   ├── Purchase Order
│   ├── Receipt
│   └── Supplier Evaluation
│
├── Entity
│   ├── MAT-100001
│   ├── SUP-000328
│   └── PO-2026000128
│
├── Relation
│   ├── Supplier ──supplies──> Material
│   ├── PO ──purchases──> Material
│   └── PO ──placed_to──> Supplier
│
├── Knowledge Page
│   ├── Material Wiki
│   ├── Supplier Wiki
│   └── Procurement Rule Wiki
│
├── Knowledge Claim
│   └── “MAT-100001的主供应商为SUP-000328”
│
├── Evidence
│   └── 《合格供应商目录2026》
│
└── Agent
    ├── search
    ├── read
    ├── relation
    ├── claim
    └── evidence
```

这样最终得到的不是普通的“知识库模板”，而是一套可以直接指导**数据治理团队、AI团队、架构团队和业务部门共同建设**的标准。

那我们正式开始。下面先把 **1～4 四份核心标准**做成第一版“可直接用于项目”的模板。后续 5～12 都以这四份为基础。

---

# 1.《企业 LLM Wiki 总体架构》

# 企业 LLM Wiki 总体架构

## 1. 文档目的

建立企业统一的 AI Knowledge Layer，将企业业务对象、业务实体、业务关系、业务规则、知识声明及其证据统一组织，为企业 AI Agent、Copilot、智能问答、决策辅助和业务自动化提供可信知识基础。

核心目标：

> 从“让 LLM 找文档”，升级为“让 LLM 理解企业业务对象、业务关系、业务规则和证据”。

---

## 2. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│                 AI Application Layer                │
│  企业助手 / 采购助手 / 财务助手 / 制造助手 / BI Copilot │
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│                  Agent / Reasoning                  │
│  Intent → Plan → Search → Read → Relation → Verify │
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│                 Knowledge Service                  │
│ Search / Entity / Relation / Claim / Evidence API │
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│                  Enterprise LLM Wiki               │
│                                                     │
│ Business Object                                     │
│      ↓                                              │
│ Entity → Relation → Knowledge Page → Claim          │
│                                      ↓              │
│                                  Evidence            │
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│                 Retrieval Layer                    │
│ Keyword / Semantic / Vector / Entity / Graph      │
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│                Knowledge Compiler                  │
│ Parse → Extract → Link → Verify → Compile → Publish│
└───────────────────────┬─────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│              Enterprise Data Sources               │
│ SAP / PLM / MES / WMS / CRM / OA / DMS / DW / Lake│
│ PDF / Word / Excel / PPT / Web / FAQ / API        │
└─────────────────────────────────────────────────────┘
```

---

## 3. 核心知识对象

企业 LLM Wiki 的核心元模型：

```text
Business Object
       │
       ↓
     Entity
       │
       ├──────── Relation ──────── Entity
       │
       ↓
 Knowledge Page
       │
       ├──────── Claim
       │             │
       │             ↓
       │          Evidence
       │
       └──────── Related Page
```

### 3.1 Business Object

定义“企业有哪些业务对象”。

例如：

- Material
    
- Supplier
    
- Customer
    
- Employee
    
- Purchase Order
    
- Sales Order
    
- BOM
    
- Equipment
    
- Process Plan
    

Business Object 是“类型”，不是具体业务记录。

---

### 3.2 Entity

Business Object 的具体实例。

例如：

```text
Business Object = Material

Entity：
MAT-100001
铝板 6061-T6 2mm
```

---

### 3.3 Relation

描述 Entity 与 Entity 之间的业务关系。

例如：

```text
MAT-100001
    ──supplied_by──>
SUP-000328
```

---

### 3.4 Knowledge Page

面向 LLM 和人阅读的知识页面。

例如：

```text
Page：
WP-MAT-100001

标题：
铝板6061-T6 2mm

包含：
基本定义
关键属性
采购规则
供应商
质量要求
相关业务流程
关联知识
Evidence
```

---

### 3.5 Knowledge Claim

最小可验证知识单元。

例如：

```text
Subject：
MAT-100001

Predicate：
main_supplier

Object：
SUP-000328
```

---

### 3.6 Evidence

证明 Claim 正确的原始依据。

例如：

```text
来源：
《合格供应商目录2026》

章节：
3.2 铝板类供应商

页码：
18

原文：
……
```

---

## 4. LLM Wiki 与传统 RAG 的关系

LLM Wiki 不意味着删除 RAG。

建议采用：

```text
                    Agent
                      │
          ┌───────────┼───────────┐
          ↓           ↓           ↓
       Wiki检索     Entity检索    RAG检索
          │           │           │
          ↓           ↓           ↓
       Page/Claim   Relation    原始文档
          └───────────┼───────────┘
                      ↓
                   Evidence
                      ↓
                   Answer
```

Wiki 是企业知识的“主体”。

RAG 是一种“检索能力”。

---

## 5. 知识生命周期

```text
Raw Data
   ↓
Ingestion
   ↓
Parsing
   ↓
Knowledge Extraction
   ↓
Entity / Relation / Claim
   ↓
Evidence Binding
   ↓
Wiki Page Generation
   ↓
Quality Check
   ↓
Business Review
   ↓
Publish
   ↓
Effective
   ↓
Expired / Superseded
```

---

## 6. 权威性原则

企业知识必须能够回答：

> “这个结论从哪里来的？”

因此重要知识必须具备：

```text
Knowledge
   ↓
Claim
   ↓
Evidence
   ↓
Source
   ↓
Authority
   ↓
Version
   ↓
Effective Date
```

---

## 7. 采购域示例

问题：

> 为什么物料 MAT-100001 最近只能向供应商 SUP-000328 采购？

Agent 应当：

```text
Search Material
       ↓
Read MAT-100001 Page
       ↓
Find supplied_by Relation
       ↓
Search Supplier Qualification
       ↓
Read Supplier Page
       ↓
Check Procurement Rule
       ↓
Query SAP Approved Supplier
       ↓
Verify Evidence
       ↓
Answer
```

而不是简单返回一段相似度最高的 PDF。

---

## 8. 建设原则

### 原则1：对象优先

先定义业务对象，再组织知识。

### 原则2：实体化

重要业务知识必须关联具体 Entity。

### 原则3：关系化

知识不能只存成文本，还需要表达业务关系。

### 原则4：证据化

重要 Claim 必须能够追溯到 Evidence。

### 原则5：版本化

知识必须具有版本、生效时间和失效时间。

### 原则6：权威化

不同来源必须具有明确 Authority Level。

### 原则7：Agent Native

知识组织方式必须支持 Agent 的 Search、Read、Link、Query、Verify。

---

## 9. 总体建设成果

最终形成：

```text
企业 Business Object Catalog
企业 Entity Catalog
企业 Relation Dictionary
企业 Knowledge Page
企业 Knowledge Claim
企业 Evidence Repository
企业 Authority Model
企业 Knowledge Compiler
企业 Agent Tool API
企业 AI Knowledge Layer
```

---

# 2.《企业 Business Object 模型》

# 企业 Business Object 模型

## 1. Business Object 定义

Business Object（业务对象）是企业业务领域中具有明确业务意义、生命周期、属性和关系，并能够被业务流程、业务规则或业务系统独立识别和管理的对象。

简单理解：

> Business Object = 企业业务世界中的“名词”。

例如：

采购域：

```text
物料
供应商
采购申请
询价单
报价单
采购订单
收货单
发票
付款
供应商评价
采购合同
采购制度
```

---

## 2. Business Object 元模型

```text
Domain
  │
  └── Business Object
          │
          ├── Attribute
          │
          ├── Entity
          │      │
          │      └── Relation
          │
          ├── Business Rule
          │
          ├── Process
          │
          ├── Knowledge Page
          │
          └── Evidence
```

---

## 3. Business Object 标准字段

|字段|编码|类型|必填|示例|
|---|---|---|---|---|
|对象编码|object_type|String|是|MATERIAL|
|对象名称|object_name|String|是|物料|
|英文名称|object_name_en|String|是|Material|
|所属领域|domain|String|是|PROCUREMENT|
|对象描述|description|Text|是|企业采购、生产所使用的物料|
|主键属性|key_attribute|String|是|material_code|
|生命周期|lifecycle|String|否|Draft/Active/Obsolete|
|数据来源|source_system|String|否|SAP|
|责任部门|owner_org|String|是|采购部|
|状态|status|Enum|是|ACTIVE|

---

## 4. Business Object 分类

### 4.1 Master Data Object

```text
Material
Supplier
Customer
Employee
Organization
Equipment
```

### 4.2 Transaction Object

```text
Purchase Request
Purchase Order
Sales Order
Goods Receipt
Invoice
Payment
```

### 4.3 Engineering Object

```text
Product
EBOM
MBOM
PBOM
BOP
Process Plan
Operation
Resource
Manufacturing Version
```

### 4.4 Knowledge Object

```text
Policy
Rule
Procedure
Specification
Standard
FAQ
Metric
```

---

## 5. Business Object 与 Entity 的区别

|项目|Business Object|Entity|
|---|---|---|
|含义|对象类型|具体对象|
|示例|Material|MAT-100001|
|是否唯一|否|是|
|是否有业务编码|通常没有|有|
|是否有属性|定义属性|存储属性值|
|是否建立关系|定义允许关系|产生实际关系|

例如：

```text
Business Object：
Material

Entity：
MAT-100001
铝板6061-T6 2mm
```

---

## 6. 采购域 Business Object Catalog

|Object Code|中文|英文|类型|
|---|---|---|---|
|MATERIAL|物料|Material|Master|
|SUPPLIER|供应商|Supplier|Master|
|PURCHASE_REQ|采购申请|Purchase Request|Transaction|
|RFQ|询价单|RFQ|Transaction|
|QUOTATION|报价单|Quotation|Transaction|
|PURCHASE_ORDER|采购订单|Purchase Order|Transaction|
|RECEIPT|收货单|Receipt|Transaction|
|INVOICE|发票|Invoice|Transaction|
|PAYMENT|付款|Payment|Transaction|
|SUPPLIER_EVAL|供应商评价|Supplier Evaluation|Business|
|PROCUREMENT_POLICY|采购制度|Procurement Policy|Knowledge|
|PROCUREMENT_RULE|采购规则|Procurement Rule|Knowledge|

---

## 7. Material Object 示例

### Object Definition

```text
Object Code：
MATERIAL

Object Name：
物料

Domain：
PROCUREMENT / MANUFACTURING

Key Attribute：
material_code
```

### Attributes

|属性|编码|类型|示例|
|---|---|---|---|
|物料编码|material_code|String|MAT-100001|
|物料名称|material_name|String|铝板|
|材质|material_grade|String|6061-T6|
|厚度|thickness|Decimal|2|
|宽度|width|Decimal|1500|
|长度|length|Decimal|3000|
|计量单位|uom|String|MM|
|物料分类|category|String|METAL|
|状态|status|Enum|ACTIVE|

---

## 8. Business Object 标准规则

### Rule 01

一个业务对象必须具有唯一 Object Code。

### Rule 02

一个业务对象必须定义唯一业务主键。

### Rule 03

业务对象属性不得与具体系统字段简单等同。

例如：

```text
Business Attribute：
material_code

SAP：
MARA-MATNR

PLM：
Item.ItemCode

MES：
MaterialNo
```

三个系统字段都映射到统一 Business Attribute。

---

## 9. Business Object 与企业数据体系

```text
Business Object
      ↓
Master Data
      ↓
Transaction Data
      ↓
Process Data
      ↓
Analytical Data
      ↓
Knowledge
      ↓
AI Knowledge
```

Business Object 是连接企业数据和企业知识的核心桥梁。

---

# 3.《Knowledge Page 标准》

# Knowledge Page 标准

## 1. 定义

Knowledge Page 是 Enterprise LLM Wiki 中面向人和 Agent 的标准化知识载体。

一个 Page 应当回答：

> “这个对象是什么？有什么属性？与谁有关？有什么规则？依据是什么？”

---

## 2. Page 标准结构

```text
Header
  ↓
Definition
  ↓
Attributes
  ↓
Relations
  ↓
Business Rules
  ↓
Lifecycle
  ↓
Knowledge Claims
  ↓
Evidence
  ↓
Related Knowledge
  ↓
Change History
```

---

## 3. Page 基础字段

|字段|类型|必填|示例|
|---|---|--:|---|
|page_id|String|✓|WP-MAT-100001|
|page_code|String|✓|MAT-100001|
|page_type|Enum|✓|MATERIAL|
|domain|String|✓|PROCUREMENT|
|object_type|String|✓|MATERIAL|
|entity_id|String|✓|MAT-100001|
|title|String|✓|铝板6061-T6 2mm|
|summary|Text|✓|用于……|
|content|Markdown|✓|……|
|status|Enum|✓|EFFECTIVE|
|authority_level|Enum|✓|L4|
|confidence|Decimal|✓|0.96|
|owner|String|✓|采购部|
|reviewer|String|否|XXX|
|version|String|✓|V1.2|
|effective_from|Date|✓|2026-01-01|
|effective_to|Date|否||
|created_at|DateTime|✓||
|updated_at|DateTime|✓||

---

## 4. Page 类型

### 核心类型

```text
BUSINESS_OBJECT
ENTITY
MATERIAL
SUPPLIER
CUSTOMER
PRODUCT
EQUIPMENT
BOM
PROCESS
RULE
POLICY
STANDARD
PROCEDURE
FAQ
METRIC
DOCUMENT
DATA_OBJECT
```

---

## 5. 标准 Page 模板

```markdown
# {{title}}

## 1. 基本信息

Page ID：
Object Type：
Entity ID：
Domain：
Status：
Version：

## 2. 业务定义

{{definition}}

## 3. 关键属性

| 属性 | 值 |
|---|---|
| {{attribute}} | {{value}} |

## 4. 业务关系

- {{relation}} → {{entity}}

## 5. 业务规则

{{rules}}

## 6. 生命周期

{{lifecycle}}

## 7. Knowledge Claims

{{claims}}

## 8. Evidence

{{evidence}}

## 9. 相关知识

{{related_pages}}

## 10. 变更历史

{{change_history}}
```

---

## 6. 采购域 Material Page 示例

### WP-MAT-100001

**标题：**

> 铝板 6061-T6 2mm

### 基本信息

```text
Page ID：WP-MAT-100001
Object Type：MATERIAL
Entity ID：MAT-100001
Domain：PROCUREMENT
Status：EFFECTIVE
Version：V1.2
Authority：L4
```

### 业务定义

用于企业 XX 产品结构件制造的铝板材料。

### 关键属性

|属性|值|
|---|---|
|物料编码|MAT-100001|
|材质|6061-T6|
|厚度|2mm|
|宽度|1500mm|
|长度|3000mm|
|单位|张|
|分类|铝板|

### 业务关系

```text
MAT-100001
   │
   ├──supplied_by──> SUP-000328
   │
   ├──alternate_supplier──> SUP-000421
   │
   ├──belongs_to──> 铝板类
   │
   └──inspected_by──> 铝板来料检验规则
```

### 采购规则

```text
仅允许向当前有效合格供应商采购。

主供应商：
SUP-000328

备选供应商：
SUP-000421
```

### Evidence

```text
《合格供应商目录2026》
《铝板采购技术规范V2.1》
SAP Approved Supplier List
```

---

## 7. Page 质量要求

一个正式 Page 至少应该满足：

```text
Identity      ✓
Definition    ✓
Attributes    ✓
Relations     ✓
Authority     ✓
Version       ✓
Evidence      ✓
Owner         ✓
Status        ✓
```

---

## 8. Page 与 RAG Chunk 的区别

传统 RAG：

```text
PDF
 ↓
Chunk 01
Chunk 02
Chunk 03
 ↓
Vector Search
```

LLM Wiki：

```text
Business Object
      ↓
Entity
      ↓
Knowledge Page
 ├── Attributes
 ├── Relations
 ├── Rules
 ├── Claims
 └── Evidence
```

Page 是业务知识的组织单位，而 Chunk 是检索单元。

二者可以同时存在。

---

# 4.《Entity / Relation 模型》

# Entity / Relation 模型

## 1. 模型目的

建立企业统一的实体与关系表达方式，使 Agent 可以从“搜索文本”进一步升级到“理解企业业务关系”。

---

## 2. Entity 模型

Entity 是 Business Object 的具体实例。

### 标准字段

|字段|类型|示例|
|---|---|---|
|entity_id|String|ENT-MAT-100001|
|object_type|String|MATERIAL|
|entity_code|String|MAT-100001|
|entity_name|String|铝板6061-T6 2mm|
|attributes|JSON|{...}|
|source_system|String|SAP|
|source_id|String|100001|
|status|Enum|ACTIVE|
|valid_from|Date|2026-01-01|
|valid_to|Date||
|created_at|DateTime||
|updated_at|DateTime||

---

## 3. Entity 示例

```json
{
  "entity_id": "ENT-MAT-100001",
  "object_type": "MATERIAL",
  "entity_code": "MAT-100001",
  "entity_name": "铝板6061-T6 2mm",
  "attributes": {
    "material_grade": "6061-T6",
    "thickness": 2,
    "width": 1500,
    "length": 3000,
    "uom": "张"
  },
  "source_system": "SAP",
  "status": "ACTIVE"
}
```

---

# 4. Relation 模型

Relation 用于描述两个 Entity 之间的业务关系。

标准表达：

```text
Subject
   +
Relation
   +
Object
```

即：

```text
Subject ──Relation──> Object
```

例如：

```text
MAT-100001
    ──supplied_by──>
SUP-000328
```

---

## 5. Relation 标准字段

|字段|类型|示例|
|---|---|---|
|relation_id|String|REL-000001|
|subject_id|String|ENT-MAT-100001|
|relation_type|String|SUPPLIED_BY|
|object_id|String|ENT-SUP-000328|
|properties|JSON|{"priority":"MAIN"}|
|source_id|String|DOC-2026-001|
|confidence|Decimal|0.98|
|valid_from|Date|2026-01-01|
|valid_to|Date||
|status|Enum|ACTIVE|

---

## 6. 采购域 Relation 示例

### Supplier → Material

```text
SUP-000328
    ──supplies──>
MAT-100001
```

### Material → Supplier

```text
MAT-100001
    ──supplied_by──>
SUP-000328
```

### Purchase Order → Supplier

```text
PO-2026000128
    ──placed_to──>
SUP-000328
```

### Purchase Order → Material

```text
PO-2026000128
    ──purchases──>
MAT-100001
```

### Supplier → Supplier Evaluation

```text
SUP-000328
    ──evaluated_by──>
EVAL-2026-00328
```

---

## 7. Relation 属性

关系本身也可以具有属性。

例如：

```text
MAT-100001
   ──supplied_by──>
SUP-000328
```

关系属性：

```json
{
  "supplier_type": "MAIN",
  "priority": 1,
  "plant": "1000",
  "currency": "CNY",
  "lead_time": 15,
  "min_order_qty": 100,
  "valid_from": "2026-01-01",
  "valid_to": null
}
```

因此 Relation 不是简单的“连线”，而是一个完整业务对象。

---

## 8. Relation 三元组

统一采用：

```text
Subject → Predicate → Object
```

例如：

```text
MAT-100001
→ supplied_by
→ SUP-000328
```

又例如：

```text
PO-2026000128
→ purchases
→ MAT-100001
```

---

## 9. 关系方向

关系必须定义方向。

推荐：

```text
Material → supplied_by → Supplier
Supplier → supplies → Material
```

而不是简单：

```text
Material ↔ Supplier
```

因为 Agent 推理需要明确语义。

---

## 10. Relation JSON

```json
{
  "relation_id": "REL-000001",
  "subject": {
    "entity_id": "ENT-MAT-100001",
    "object_type": "MATERIAL"
  },
  "predicate": "SUPPLIED_BY",
  "object": {
    "entity_id": "ENT-SUP-000328",
    "object_type": "SUPPLIER"
  },
  "properties": {
    "supplier_type": "MAIN",
    "priority": 1,
    "plant": "1000"
  },
  "confidence": 0.98,
  "status": "ACTIVE"
}
```

---

## 11. Agent 使用关系

用户问：

> MAT-100001有哪些供应商？

Agent：

```text
Entity Search
     ↓
MAT-100001
     ↓
Relation Query
     ↓
SUPPLIED_BY
     ↓
SUP-000328
SUP-000421
     ↓
Supplier Page
     ↓
返回结果
```

用户进一步问：

> 为什么现在只能采购 SUP-000328？

Agent继续：

```text
MAT-100001
    ↓
SUPPLIED_BY
    ↓
SUP-000328
SUP-000421
    ↓
Check Supplier Qualification
    ↓
Check Effective Date
    ↓
Check Procurement Rule
    ↓
Check SAP Approved Supplier
    ↓
Evidence Verification
    ↓
Answer
```

这就是 Entity + Relation 对 Agent 推理能力的价值。

---

## 12. Entity / Relation 建模原则

### 原则1

Business Object 定义“是什么”。

### 原则2

Entity 定义“具体是哪一个”。

### 原则3

Relation 定义“它和谁有什么业务关系”。

### 原则4

Relation 必须可追溯。

### 原则5

Relation 必须支持有效期。

### 原则6

Relation 可以具有业务属性。

### 原则7

重要 Relation 必须绑定 Evidence。

---

# 13. 四个核心对象的最终关系

```text
Business Object
      │
      │ defines
      ↓
    Entity
      │
      │ connects
      ↓
   Relation
      │
      │ described by
      ↓
 Knowledge Page
      │
      │ contains
      ↓
 Knowledge Claim
      │
      │ supported by
      ↓
   Evidence
```

这条链路是整个企业 LLM Wiki 的核心元模型。

---



**5.《企业关系字典》**  
→ 统一 `supplied_by / belongs_to / used_in / produces / consumes / governed_by...`

**6.《Knowledge Claim 标准》**  
→ 把一句业务知识拆成可验证的最小单元。

**7.《Evidence 证据模型》**  
→ 把 Claim 追溯到 SAP、PLM、制度、Excel、PDF 的具体证据位置。

**8.《Knowledge Authority 标准》**  
→ 解决“SAP 和 Excel 冲突时听谁的”“旧制度和新制度冲突怎么办”等企业实际问题。

**这四份实际上是整个 LLM Wiki 与普通 RAG 最大的区别所在。**



---

# 5.《企业关系字典》

# 企业关系字典

## 1. 目的

建立企业统一 Relation Vocabulary，避免不同系统、不同业务域对同一种业务关系使用不同名称。

例如：

```text
供应商提供物料
供应商供货物料
物料供应商
物料由供应商提供
Supplier-Material
```

在 Wiki 中统一表达为：

```text
SUPPLIER
   ──SUPPLIES──>
MATERIAL
```

---

# 2. Relation 分类

建议企业关系字典分为 8 类：

|类型|说明|
|---|---|
|隶属关系|谁属于谁|
|组成关系|谁由谁组成|
|业务关系|谁与谁发生业务|
|供应关系|谁供应谁|
|流程关系|谁产生谁|
|引用关系|谁引用谁|
|治理关系|谁受谁管理|
|数据关系|谁来源于谁|

---

# 3. 核心关系字典

|Relation Code|中文|Subject|Object|示例|
|---|---|---|---|---|
|BELONGS_TO|属于|Entity|Entity|物料→物料分类|
|CONTAINS|包含|Entity|Entity|BOM→物料|
|PART_OF|是组成部分|Entity|Entity|零件→产品|
|SUPPLIES|供应|Supplier|Material|供应商→物料|
|SUPPLIED_BY|由供应|Material|Supplier|物料→供应商|
|PURCHASES|采购|PO|Material|PO→物料|
|PURCHASED_FROM|向…采购|PO|Supplier|PO→供应商|
|REQUESTS|申请|Purchase Request|Material|申请→物料|
|CONVERTED_TO|转换为|Request|PO|采购申请→采购订单|
|BASED_ON|基于|PO|Quotation|PO→报价单|
|RESULTS_IN|产生|PO|Receipt|PO→收货|
|EVALUATED_BY|被评价|Supplier|Evaluation|供应商→评价|
|GOVERNED_BY|受…治理|Entity|Policy|采购→采购制度|
|DEFINED_BY|由…定义|Entity|Standard|物料→技术标准|
|INSPECTED_BY|由…检验|Material|Quality Rule|物料→质量规则|
|PRODUCES|生产|Process|Material|工序→产品|
|CONSUMES|消耗|Process|Material|工序→原料|
|USED_IN|用于|Material|Product|物料→产品|
|DERIVED_FROM|来源于|Entity|Entity|MBOM→EBOM|
|SUPERSEDES|替代|Entity|Entity|新物料→旧物料|
|REFERENCES|引用|Document|Document|制度→标准|
|APPLIES_TO|适用于|Rule|Entity|规则→物料|
|DEPENDS_ON|依赖|Entity|Entity|工艺→设备|
|HAS_ATTRIBUTE|具有属性|Entity|Attribute|物料→材质|

---

# 4. Relation 命名规范

统一采用：

```text
动词 + 业务语义
```

例如：

```text
SUPPLIES
PURCHASES
PRODUCES
CONSUMES
GOVERNED_BY
DEFINED_BY
```

不建议：

```text
supplier_material
material_supplier_relation
relation01
供应关系
```

技术层使用英文 Code，业务展示使用中文名称。

---

# 5. 关系必须定义方向

例如：

```text
Supplier
   ──SUPPLIES──>
Material
```

反向关系：

```text
Material
   ──SUPPLIED_BY──>
Supplier
```

二者是语义相反的两个 Relation Code，而不是数据库中简单反转。

---

# 6. Relation 属性

复杂关系必须支持属性。

例如：

```text
MAT-100001
 ──SUPPLIED_BY──>
SUP-000328
```

附加：

```json
{
  "supplier_type": "MAIN",
  "priority": 1,
  "plant": "1000",
  "lead_time": 15,
  "min_order_qty": 100,
  "valid_from": "2026-01-01",
  "valid_to": null
}
```

---

# 7. 采购域关系网络

```text
Supplier
   │
   │ supplies
   ↓
Material
   │
   │ used_in
   ↓
Product

Purchase Request
   │
   │ converted_to
   ↓
Purchase Order
   │
   ├── purchases ──> Material
   │
   ├── purchased_from ──> Supplier
   │
   ├── based_on ──> Quotation
   │
   └── results_in ──> Receipt

Supplier
   │
   └── evaluated_by ──> Supplier Evaluation

Material
   │
   ├── governed_by ──> Procurement Rule
   └── inspected_by ──> Quality Rule
```

---

# 8. Relation 治理要求

每个 Relation 至少定义：

- Relation Code
    
- 中文名称
    
- Subject Object
    
- Object Object
    
- 方向
    
- Cardinality
    
- 是否允许多值
    
- 是否有有效期
    
- 是否需要 Evidence
    
- 数据来源
    
- 责任部门
    

---

# 9. Cardinality

例如：

```text
Supplier ──SUPPLIES──> Material
```

通常：

```text
Supplier 1 : N Material
Material N : N Supplier
```

而：

```text
Material ──BELONGS_TO──> Material Category
```

通常：

```text
Material N : 1 Category
```

---

# 6.《Knowledge Claim 标准》

# Knowledge Claim 标准

## 1. 定义

Knowledge Claim 是企业知识体系中**最小可验证的知识单元**。

核心思想：

> 不把“整篇文章”作为可信单位，而把一个具体事实、规则或判断作为可验证单位。

---

# 2. Claim 基本结构

```text
Subject
+
Predicate
+
Object
+
Evidence
+
Authority
+
Validity
```

例如：

```text
MAT-100001
    │
    ├── main_supplier
    ↓
SUP-000328
```

---

# 3. Claim 类型

建议分为：

|类型|示例|
|---|---|
|FACT|MAT-100001材质为6061-T6|
|RELATION|MAT-100001由SUP-000328供应|
|RULE|采购金额超过100万元需要总经理审批|
|DEFINITION|Material是企业采购和生产使用的物料|
|STATUS|SUP-000328当前为合格供应商|
|METRIC|SUP-000328年度准时交付率96.5%|
|EVENT|PO-2026000128已完成收货|
|DERIVED|供应商A近三个月交付表现下降|

---

# 4. Claim 标准字段

|字段|示例|
|---|---|
|claim_id|CLM-000001|
|subject_id|MAT-100001|
|predicate|MAIN_SUPPLIER|
|object_id|SUP-000328|
|object_value||
|claim_type|RELATION|
|authority_level|L4|
|confidence|0.98|
|valid_from|2026-01-01|
|valid_to||
|source_id|DOC-2026-001|
|evidence_id|EVD-000001|
|status|ACTIVE|

---

# 5. Claim 示例

### Claim 001

```json
{
  "claim_id": "CLM-000001",
  "claim_type": "RELATION",
  "subject": "MAT-100001",
  "predicate": "MAIN_SUPPLIER",
  "object": "SUP-000328",
  "authority_level": "L4",
  "confidence": 0.98,
  "valid_from": "2026-01-01",
  "evidence_id": "EVD-000001",
  "status": "ACTIVE"
}
```

---

# 6. Claim 与 Relation 的区别

这是整个模型中特别重要的区别。

### Relation

描述企业对象之间存在关系：

```text
Material
   ──supplied_by──>
Supplier
```

### Claim

描述一个“可以被证明的知识断言”：

```text
MAT-100001的主供应商是SUP-000328。
```

所以：

```text
Relation = 业务关系模型

Claim = 可验证知识声明
```

一个 Relation 可以产生多个 Claim。

例如：

```text
MAT-100001
 ──SUPPLIED_BY──>
SUP-000328
```

可能存在：

```text
Claim 1：
SUP-000328是主供应商

Claim 2：
SUP-000328适用于工厂1000

Claim 3：
SUP-000328交付周期15天

Claim 4：
SUP-000328资格有效期至2026-12-31
```

---

# 7. Claim 必须可追溯

标准链：

```text
Claim
 ↓
Evidence
 ↓
Source
 ↓
Document Version
 ↓
Effective Date
```

不能只有：

```text
Claim：
供应商A是合格供应商
```

而没有来源。

---

# 8. Claim 冲突

例如：

```text
Claim A：
MAT-100001主供应商 = SUP-000328

来源：
《合格供应商目录V3》

Claim B：
MAT-100001主供应商 = SUP-000421

来源：
《采购部门Excel》
```

系统不能简单覆盖。

应建立：

```text
CONFLICT
 ├── Claim A
 ├── Claim B
 └── Resolution
```

由 Authority Model 判断最终有效 Claim。

---

# 9. Claim 生命周期

```text
EXTRACTED
   ↓
VALIDATING
   ↓
REVIEWED
   ↓
APPROVED
   ↓
ACTIVE
   ↓
SUPERSEDED
   ↓
EXPIRED
```

---

# 10. Claim 质量评分

建议：

```text
Claim Score =
Authority × 30%
+ Evidence × 25%
+ Freshness × 15%
+ Consistency × 15%
+ Human Review × 15%
```

---

# 11. Agent 使用 Claim

用户：

> MAT-100001现在的主供应商是谁？

Agent 不仅返回：

> SUP-000328

而应该形成：

```text
答案：
SUP-000328。

依据：
《合格供应商目录2026》

有效期：
2026-01-01至今

状态：
ACTIVE

可信等级：
L4
```

这就是 Claim + Evidence 的价值。

---

# 7.《Evidence 证据模型》

# Evidence 证据模型

## 1. 定义

Evidence 是支持 Knowledge Claim 的原始或可验证数据证据。

目标：

> 让任何一个重要 AI 答案，都能够回答“依据在哪里”。

---

# 2. Evidence 来源

企业 Evidence 可以来自：

```text
制度文件
技术标准
业务规范
SAP
PLM
MES
WMS
CRM
OA
Excel
PDF
Word
PPT
数据库
API
数据仓库
数据湖
业务人员确认
```

---

# 3. Evidence 模型

```text
Source Document
      ↓
Document Version
      ↓
Evidence
      ↓
Knowledge Claim
      ↓
Knowledge Page
```

---

# 4. Evidence 字段

|字段|示例|
|---|---|
|evidence_id|EVD-000001|
|source_id|DOC-2026-001|
|source_type|POLICY|
|page_id|WP-MAT-100001|
|claim_id|CLM-000001|
|page_number|18|
|section|3.2|
|paragraph_no|4|
|content|SUP-000328为合格供应商……|
|content_hash|SHA256...|
|confidence|0.99|
|captured_at|2026-09-10|
|status|VALID|

---

# 5. Evidence 示例

```json
{
  "evidence_id": "EVD-000001",
  "source_id": "DOC-2026-001",
  "source_name": "合格供应商目录2026",
  "source_type": "DOCUMENT",
  "page_number": 18,
  "section": "3.2 铝板类供应商",
  "paragraph_no": 4,
  "content": "SUP-000328为MAT-100001当前有效供应商",
  "content_hash": "sha256:xxxxx",
  "confidence": 0.99,
  "status": "VALID"
}
```

---

# 6. Evidence 粒度

推荐：

```text
Document
 ↓
Chapter
 ↓
Section
 ↓
Paragraph
 ↓
Table
 ↓
Cell
```

不要只保存：

```text
《采购制度.pdf》
```

而应尽量定位到：

```text
第18页
第3.2节
第4段
```

如果来自 Excel：

```text
Sheet：
供应商目录

Row：
328

Column：
资格状态
```

如果来自 SAP：

```text
System：
SAP

Table：
LFM1

Supplier：
SUP-000328

Field：
LOEVM
```

---

# 7. Structured Evidence

并非所有 Evidence 都来自文档。

例如：

```text
Claim：
SUP-000328当前为合格供应商
```

Evidence：

```text
SAP Supplier Master
+
SAP Supplier Qualification
```

因此建议 Evidence 类型：

```text
DOCUMENT
DATABASE
API
SYSTEM_RECORD
USER_CONFIRMATION
CALCULATION
MODEL_DERIVED
```

---

# 8. Evidence 与实时数据

对于实时业务数据：

```text
Claim：
PO-2026000128已经完成收货
```

Evidence：

```text
SAP Goods Receipt
Document：
5000123456

Posting Date：
2026-09-10
```

Agent 查询时应实时获取，而不是把所有交易数据复制进 Wiki。

---

# 9. Evidence 有效性

Evidence 必须支持：

```text
VALID
INVALID
EXPIRED
REVOKED
SUPERSEDED
```

例如：

```text
采购制度V3.1
   ↓
SUPERSEDED
   ↓
采购制度V3.2
```

因此 V3.1 的 Evidence 仍然存在，但不能支撑当前有效规则。

---

# 10. Evidence 与 Citation

Agent 最终回答：

```text
MAT-100001当前主供应商为SUP-000328。

依据：
《合格供应商目录2026》
第18页，第3.2节。

同时SAP当前有效供应商记录显示：
SUP-000328。
```

这样用户看到的是：

```text
Answer
 ↓
Claim
 ↓
Evidence
 ↓
Source
```

而不是单纯的：

```text
Answer
 ↓
“据相关资料显示……”
```

---

# 8.《Knowledge Authority 标准》

# Knowledge Authority 标准

## 1. 目的

建立企业知识的权威等级，解决：

> “不同来源说法不一致时，到底相信谁？”

这是企业 LLM Wiki 必须具备的能力。

---

# 2. Authority 五级模型

|Level|名称|典型来源|可信度|
|---|---|---|---|
|L5|法规/正式标准|法律、国家标准、行业强制标准|极高|
|L4|企业正式制度|企业制度、正式规范、技术标准|高|
|L3|企业系统事实|SAP、PLM、MES、WMS等正式系统|高|
|L2|业务确认知识|部门确认、专家确认|中|
|L1|AI推导知识|LLM推理、模型总结|较低|
|L0|未验证知识|未确认资料、推测|不可信|

---

# 3. 权威优先级

默认：

```text
L5
 ↓
L4
 ↓
L3
 ↓
L2
 ↓
L1
 ↓
L0
```

但是不能简单理解为：

> L4永远比L3高。

因为两者回答的问题可能不同。

例如：

```text
采购制度：
谁有审批权限？
→ L4

SAP：
PO-2026000128实际审批人是谁？
→ L3
```

因此 Authority 必须结合业务语境。

---

# 4. Authority 属性

每个 Source 至少定义：

|字段|示例|
|---|---|
|authority_level|L4|
|owner|采购部|
|issuer|公司管理委员会|
|effective_from|2026-01-01|
|effective_to||
|approval_status|APPROVED|
|version|V3.2|
|scope|全公司|
|review_cycle|12个月|

---

# 5. 冲突处理模型

例如：

```text
来源A：
《采购制度V3.1》
审批金额 > 100万
需要总经理审批

来源B：
《采购制度V3.2》
审批金额 > 200万
需要总经理审批
```

系统：

```text
Conflict Detection
       ↓
Compare Authority
       ↓
Compare Version
       ↓
Compare Effective Date
       ↓
Determine Current Claim
```

最终：

```text
V3.2
Effective Date：
2026-01-01

Status：
ACTIVE
```

V3.1：

```text
SUPERSEDED
```

---

# 6. Authority 冲突规则

建议采用以下优先级：

```text
① 是否有效
② 是否在业务适用范围内
③ Authority Level
④ Version
⑤ Effective Date
⑥ 数据新鲜度
⑦ 业务确认
```

---

# 7. 系统事实与制度冲突

这是企业场景中特别重要的情况。

例如：

制度规定：

```text
所有采购必须有合格供应商。
```

SAP实际数据：

```text
某供应商资格已经过期
```

Agent 不应该简单说：

> “制度规定供应商合格，所以供应商可以采购。”

而应：

```text
Policy Claim
+
SAP Current Status
```

综合判断：

> 制度要求供应商必须保持有效资格；当前 SAP 记录显示 SUP-000421 资格已过期，因此当前不应作为有效采购供应商。

---

# 8. Authority 与 Claim

推荐：

```text
Claim
 ├── Authority Level
 ├── Evidence
 ├── Source
 ├── Version
 ├── Effective Date
 └── Scope
```

所以 Claim 不是简单的：

```text
事实 = True
```

而是：

```text
Fact
+
Source
+
Authority
+
Time
+
Scope
```

---

# 9. Agent 答案策略

Agent 回答企业问题时：

### 高可信答案

```text
L5/L4 Evidence
+
L3 System Evidence
+
一致
```

可以直接回答。

### 中可信答案

```text
L2 Business Confirmation
```

应标明：

> 经业务部门确认。

### 低可信答案

```text
L1 AI Derived
```

应标明：

> 根据现有数据推断，尚未经业务确认。

### 无可信依据

```text
L0
```

应回答：

> 当前知识库没有足够证据确认该结论。

---

# 这一批 5～8 做完以后，整个体系就完整很多了

现在可以把前 8 个模型合起来看：

```text
                 Business Object
                       │
                       ↓
                     Entity
                       │
                 ┌─────┴─────┐
                 ↓           ↓
              Relation    Attribute
                 │
                 ↓
            Knowledge Page
                 │
                 ↓
          Knowledge Claim
                 │
          ┌──────┴──────┐
          ↓             ↓
       Evidence      Authority
          │             │
          ↓             ↓
       Source        Version/Scope
```

这实际上形成了一个很重要的闭环：

> **“业务对象 → 业务实体 → 业务关系 → 知识页面 → 知识声明 → 证据 → 权威性”**

接下来 **9～12** 就从“知识模型”进入“真正怎么运行”。

尤其第 **9《Knowledge Compiler 设计》** 是关键：它要解决**PDF、Word、Excel、SAP、PLM、MES、数据库中的原始数据，如何自动编译成上面这套 Entity / Relation / Page / Claim / Evidence**。

然后第 **10《Agent Tool API》**解决 Agent 如何调用它们，第 **11《采购域 LLM Wiki MVP》**做完整落地样板，第 **12《企业 AI Knowledge Layer 技术架构》**把所有东西收敛成最终系统架构。



### 9.《Knowledge Compiler 设计》

重点解决：

**企业原始数据 → 可被 Agent 理解和推理的 Wiki 知识**

核心链路：

```text
PDF / Word / Excel
SAP / PLM / MES / WMS
数据库 / API / 数据湖
        ↓
   Data Ingestion
        ↓
 Document / Record Parsing
        ↓
 Entity Extraction
        ↓
 Relation Extraction
        ↓
 Claim Extraction
        ↓
 Evidence Binding
        ↓
 Knowledge Page Compilation
        ↓
 Quality Check
        ↓
 Authority Assessment
        ↓
 Version / Publish
        ↓
      LLM Wiki
```

其中最重要的是建立：

> **Document → Entity → Relation → Claim → Evidence → Page**

这一套“知识编译链”。

---

### 10.《Agent Tool API 规范》

建议不要让 Agent 直接访问数据库，而是提供标准化知识工具：

```text
search_wiki()
search_entity()
get_entity()
get_page()
get_claim()
get_evidence()
get_relations()
traverse_relation()
get_history()
check_conflict()
query_business_data()
```

例如：

```json
{
  "tool": "get_entity",
  "entity_type": "Material",
  "entity_id": "MAT-100001"
}
```

返回：

```json
{
  "entity": "MAT-100001",
  "name": "铝板6061-T6 2mm",
  "status": "ACTIVE",
  "authority": "L3",
  "relations": [
    {
      "predicate": "SUPPLIED_BY",
      "object": "SUP-000328"
    }
  ],
  "evidence": [
    "SAP.MARA",
    "PLM.Material"
  ]
}
```

这样 Agent 的回答就不再只是：

> “从几个 Chunk 中生成答案”

而变成：

> “找到业务对象 → 查询关系 → 获取事实 → 检查证据 → 推理 → 给出答案”。

---

### 11.《采购域 LLM Wiki MVP》

建议把采购作为第一个 MVP，非常合适。

核心 Business Object：

```text
Material
Supplier
Purchase Request
RFQ
Quotation
Purchase Order
Receipt
Invoice
Payment
Supplier Evaluation
Purchase Contract
Procurement Policy
Procurement Rule
```

形成采购知识网络：

```text
供应商
   │
   ├── SUPPLIES ──→ 物料
   │                  │
   │                  ├── BELONGS_TO → 物料分类
   │                  ├── USED_IN → BOM
   │                  └── PURCHASED_FROM → Supplier
   │
   ├── QUOTES ──→ RFQ
   │                │
   │                └── RESULTS_IN → Purchase Order
   │
   └── EVALUATED_BY → Supplier Evaluation
```

最终可以支持：

> “6061-T6 铝板目前有哪些合格供应商？”

> “供应商 SUP-000328 最近一年的采购金额是多少？”

> “这个物料有哪些替代供应商？”

> “为什么这张采购订单需要总经理审批？”

> “当前采购审批规则与2025年的规则有什么变化？”

这类问题才是 LLM Wiki 相对于普通 RAG 真正有价值的地方。

---

### 12.《企业 AI Knowledge Layer 技术架构》

最终建议把整个体系定义为：

```text
                 AI Application
                       │
             ┌─────────┴─────────┐
             │                   │
          Copilot             AI Agent
             │                   │
             └─────────┬─────────┘
                       ↓
                 Agent Runtime
                       │
             ┌─────────┴─────────┐
             │                   │
        Reasoning Engine     Tool Calling
             │                   │
             └─────────┬─────────┘
                       ↓
             ┌───────────────────┐
             │ Knowledge Service │
             └─────────┬─────────┘
                       ↓
        ┌─────────────────────────────┐
        │       Enterprise LLM Wiki  │
        │                             │
        │ Page / Entity / Relation    │
        │ Claim / Evidence / Rule     │
        └──────────────┬──────────────┘
                       ↓
        ┌─────────────────────────────┐
        │      Retrieval Layer        │
        │                             │
        │ Keyword / Vector / Graph    │
        │ Hybrid / Semantic Search    │
        └──────────────┬──────────────┘
                       ↓
        ┌─────────────────────────────┐
        │     Knowledge Compiler      │
        └──────────────┬──────────────┘
                       ↓
 ┌─────────────────────────────────────────┐
 │ Enterprise Data & Knowledge             │
 │                                         │
 │ SAP / PLM / MES / WMS / CRM / OA       │
 │ DB / DWH / Lakehouse / PDF / Word      │
 │ Excel / Manuals / Standards / Policies │
 └─────────────────────────────────────────┘
```

其中最核心的变化是：

**传统架构**

```text
业务系统
   ↓
文档
   ↓
Chunk
   ↓
Embedding
   ↓
Vector DB
   ↓
RAG
   ↓
LLM
```

升级为：

```text
业务系统
   ↓
Knowledge Compiler
   ↓
Business Object
   ↓
Entity + Relation
   ↓
Claim + Evidence
   ↓
Knowledge Page
   ↓
Enterprise LLM Wiki
   ↓
Agent Tools
   ↓
Reasoning
   ↓
LLM
```

而 **Vector RAG 不需要废弃**，它应该退居为 Knowledge Layer 的一个检索能力：

```text
                 AI Knowledge Layer
                        │
        ┌───────────────┼────────────────┐
        ↓               ↓                ↓
   Wiki Search     Graph Search     Vector Search
        │               │                │
        └───────────────┼────────────────┘
                        ↓
                   Agent Reasoning
```

这样最终形成的不是单纯的“新型知识库”，而是：

> **Enterprise AI Knowledge Layer = 企业业务对象模型 + 企业知识 Wiki + 知识图谱 + Evidence 证据链 + Hybrid Retrieval + Agent Tools**

这套架构与你前面在做的**主数据、统一业务对象、统一编码、数据模型、业务语义、指标、血缘、质量以及非结构化知识关联**其实可以很好地衔接起来。

下一步最值得做的是把 **9–12 四份文档继续展开到“字段级模板 + API级定义 + 数据库DDL级设计 + 采购域完整实例”**，这样就可以直接作为一个真正的 LLM Wiki 建设项目蓝图。