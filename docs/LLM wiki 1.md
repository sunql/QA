可以。你说的 **LLM Wiki** 如果指 2026 年提出的 **LLM-Wiki / Retrieval-as-Reasoning** 思路，那么它和传统“文档切 Chunk → Embedding → 向量库 → TopK → LLM”的 RAG 确实是两种不同的知识组织方式。

2026 年的 LLM-Wiki 论文提出的核心思想是：**不要把企业知识看成一堆等待搜索的文本 Chunk，而是把知识编译成一个可以被 Agent 主动“搜索、阅读、沿链接继续探索”的 Wiki。** Wiki 页面之间存在双向链接，并且系统可以通过持续的 Error Book 进行结构和语义修正。论文在多个多跳问答数据集上报告了相对 Dense RAG、GraphRAG 等方法的提升。([arXiv](https://arxiv.org/abs/2605.25480?utm_source=chatgpt.com "Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki"))

但我建议你不要理解成：

> **LLM Wiki = 完全不要 RAG**

更准确的是：

> **LLM Wiki = 用“结构化知识页面 + 关系链接 + Agent检索/阅读/推理”替代传统的“向量 TopK Chunk 检索”。**

---

# 一、先把两种架构区别清楚

传统 RAG：

```text
企业文档
  ↓
解析
  ↓
Chunk切分
  ↓
Embedding
  ↓
Vector DB
  ↓
TopK
  ↓
LLM
  ↓
答案
```

最大的问题是：

**知识本身没有被真正整理。**

例如企业有：

```text
《采购管理制度》
《供应商管理制度》
《物料主数据标准》
《ERP操作手册》
《采购订单管理规范》
《来料检验规范》
```

传统 RAG 最后实际上可能只是：

```text
Chunk 103
Chunk 289
Chunk 562
Chunk 781
```

LLM 得到的是一堆“相关文本片段”。

---

而 LLM Wiki：

```text
原始文档
   ↓
LLM知识编译
   ↓
知识页面
   ↓
实体/关系/引用/上下位关系
   ↓
Wiki知识网络
   ↓
Agent Search
   ↓
Read Page
   ↓
Follow Link
   ↓
继续检索
   ↓
综合推理
   ↓
答案 + 证据
```

例如：

```text
采购管理
│
├── 供应商管理
│    ├── 供应商准入
│    ├── 供应商分类
│    ├── 供应商评价
│    └── 供应商淘汰
│
├── 采购订单
│    ├── 采购申请
│    ├── 采购询价
│    ├── 采购订单
│    └── 采购收货
│
├── 物料
│    ├── 物料主数据
│    ├── 物料分类
│    ├── 物料编码
│    └── 替代料
│
└── 质量
     ├── IQC
     ├── 来料检验
     └── 不合格品
```

这已经不是“文档库”，而是**企业知识系统**。

---

# 二、我建议你搭建的不是简单 Wiki，而是“企业 LLM Wiki”

结合你之前在做的：

> 统一业务对象 + 统一数据编码 + 统一数据模型 + 统一业务语义 + 统一指标口径 + 数据血缘 + 数据质量 + 结构化关系 + 非结构化知识

实际上非常适合升级成：

# 企业 AI Knowledge Wiki

我建议整体架构设计成下面这样。

```text
                  ┌──────────────────────┐
                  │       用户/Agent       │
                  └──────────┬───────────┘
                             ↓
                  ┌──────────────────────┐
                  │     Agent Router      │
                  │  Query理解/任务拆解    │
                  └──────────┬───────────┘
                             ↓
       ┌─────────────────────┼─────────────────────┐
       ↓                     ↓                     ↓
  Wiki Search            Wiki Read             Wiki Link
       │                     │                     │
       ↓                     ↓                     ↓
  页面检索              页面全文阅读          关系路径探索
       └─────────────────────┼─────────────────────┘
                             ↓
                    ┌────────────────┐
                    │  Knowledge Wiki │
                    └────────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       ↓                     ↓                     ↓
  Knowledge Page       Entity/Relation       Evidence
       │                     │                     │
       ↓                     ↓                     ↓
   业务知识              知识关系             原始依据
                             │
                             ↓
                    ┌────────────────┐
                    │ Source Documents│
                    └────────────────┘
```

这里最关键的变化是：

**Vector DB 从“知识主体”降级为“辅助检索工具”。**

---

# 三、Wiki Page 到底应该长什么样？

这是整个方案最核心的地方。

不要简单做成：

```markdown
# 采购订单

采购订单是企业采购活动中的正式业务单据......
```

而应该建立标准化的 **Knowledge Page Model**。

例如：

```markdown
# 物料主数据

## 基本信息

知识类型：业务对象
业务域：供应链
对象类型：Material
编码：MDM-MATERIAL

## 定义

物料主数据是企业对采购、生产、库存、销售等业务
共同使用的物料基础信息。

## 核心属性

| 属性 | 类型 | 说明 |
|---|---|---|
| MaterialCode | String | 物料编码 |
| MaterialName | String | 物料名称 |
| MaterialType | Enum | 物料类型 |
| MaterialGroup | Enum | 物料分类 |
| UOM | Enum | 基本计量单位 |

## 业务关系

- 属于 → 物料分类
- 被使用于 → BOM
- 被采购 → 采购订单
- 被库存管理 → 库存
- 被生产使用 → 生产订单

## 生命周期

创建 → 审核 → 启用 → 冻结 → 停用

## 相关知识

[[物料分类]]
[[物料编码规则]]
[[BOM]]
[[采购订单]]
[[供应商]]
[[物料状态]]

## 数据来源

PLM
SAP
MDM

## 权威来源

《物料主数据管理制度 V3.2》

## 更新时间

2026-08-30

## 责任部门

供应链管理部
```

这样一个页面就不再只是文本。

它同时具备：

**定义 + 属性 + 关系 + 生命周期 + 来源 + 权威性 + 关联知识。**

---

# 四、Wiki 最重要的不是 Page，而是 Link

这也是它和普通知识库最大的区别。

例如：

```text
物料
 ↓
BOM
 ↓
生产订单
 ↓
工艺路线
 ↓
工作中心
 ↓
设备
```

建立关系：

```text
Material
   │
   ├── classified_by → MaterialCategory
   │
   ├── used_in → BOM
   │
   ├── purchased_by → PurchaseOrder
   │
   └── produced_by → ProductionOrder
```

进一步：

```text
BOM
 │
 ├── contains → Material
 ├── belongs_to → Product
 └── manufactured_by → ProcessPlan
```

再进一步：

```text
ProcessPlan
 │
 ├── contains → Routing
 ├── contains → Operation
 ├── uses → Resource
 └── applies_to → ManufacturingVersion
```

你会发现，这和你之前设计的：

> EBOM / MBOM / PBOM / BOP / Resource / Manufacturing Version

天然可以融合。

---

# 五、所以你完全可以把企业 Wiki 做成“业务对象 Wiki”

这个方向我反而非常推荐你。

不要：

> 一个文档一个 Wiki Page

而是：

> **一个企业业务对象一个 Wiki Page**

例如：

### 物料 Wiki

```text
MATERIAL-100001
```

包含：

```text
基本属性
分类
编码
描述
单位
状态
生命周期
替代料
采购信息
库存信息
BOM关系
供应商关系
质量要求
相关制度
相关操作手册
相关FAQ
历史变更
```

---

### BOM Wiki

```text
BOM-100023
```

包含：

```text
产品
BOM类型
版本
状态
父项
子项
数量
替代料
生效日期
失效日期
来源系统
EBOM关系
MBOM关系
工艺关系
```

---

### 供应商 Wiki

```text
SUPPLIER-000328
```

包含：

```text
供应商基本信息
供应商分类
供应物料
供应产品
合作工厂
质量等级
绩效评价
采购历史
不合格记录
合同
认证
风险
```

这样就开始从：

**Knowledge Base**

升级成：

# Enterprise Knowledge Graph + Wiki

---

# 六、传统 RAG 与 LLM Wiki 的核心差异

|维度|传统RAG|LLM Wiki|
|---|---|---|
|知识单元|Chunk|Knowledge Page|
|组织方式|文档集合|知识网络|
|检索|Similarity Search|Search + Read + Link|
|关系|弱|强|
|多跳问题|较弱|强|
|可解释性|Chunk引用|Page + Relationship + Source|
|知识维护|更新文档|更新知识页面|
|知识复用|一般|很强|
|Agent适配|一般|很适合|
|向量库依赖|高|可降低|
|知识治理|较弱|强|
|人工维护|文档维护|页面维护|
|数据模型|通常没有|可以建立|
|权威来源|Metadata|Page级治理|
|版本|文档版本|Knowledge Version|
|生命周期|文档生命周期|Knowledge Lifecycle|

不过要注意一个现实问题：

**LLM Wiki 并不是所有场景都比 RAG 好。**

最近的对比研究也发现，Wiki 在跨文档综合和逐条证据支持方面有优势，但在简单事实查询和查询成本上，并不一定全面优于 RAG。([arXiv](https://arxiv.org/abs/2605.18490?utm_source=chatgpt.com "Vector RAG vs LLM-Compiled Wiki: A Preregistered Comparison on a Small Multi-Domain Research"))

所以企业落地最好是：

# Wiki First + Hybrid Retrieval

而不是：

# Wiki 100% 替代 RAG

---

# 七、企业真正应该采用的架构

我建议：

```text
                    用户问题
                       │
                       ↓
                ┌─────────────┐
                │ Query Agent │
                └──────┬──────┘
                       │
          ┌────────────┼────────────┐
          ↓            ↓            ↓
      Wiki Search   Vector Search  SQL/API
          │            │            │
          ↓            ↓            ↓
      Knowledge     原始文档       企业数据
       Pages          Chunk
          │            │            │
          └────────────┼────────────┘
                       ↓
                 Evidence Layer
                       ↓
                  LLM Reasoning
                       ↓
                 Answer + Citation
```

也就是说：

**Wiki 是第一知识层。**

但：

- 文档检索 → RAG
    
- 业务对象 → Wiki
    
- 结构化数据 → SQL
    
- 实时数据 → API
    
- 复杂关系 → Graph
    
- 多跳推理 → Agent
    

统一给 LLM。

这才是真正适合企业的架构。

---

# 八、Wiki 的底层数据结构怎么设计？

我建议至少建立 7 类核心对象。

```text
Knowledge
Entity
Relation
Document
Evidence
Version
Source
```

进一步：

### 1. Knowledge

知识页面。

```text
knowledge_id
title
type
domain
content
status
version
owner
authority
effective_date
```

### 2. Entity

企业实体。

```text
Material
Product
Customer
Supplier
BOM
Process
Equipment
Employee
Organization
Contract
Order
```

### 3. Relation

关系。

```text
Material --belongs_to--> MaterialCategory

Material --used_in--> BOM

BOM --belongs_to--> Product

BOM --manufactured_by--> ProcessPlan

ProcessPlan --contains--> Operation

Operation --uses--> Resource
```

### 4. Evidence

证据。

```text
knowledge_id
source_document
page
paragraph
source_system
source_record
```

这一步非常重要。

因为 Wiki 不能变成：

> LLM自己编了一堆企业知识。

必须能够追溯：

```text
Wiki知识
   ↓
Evidence
   ↓
原始制度
   ↓
原始文件
   ↓
原始系统数据
```

---

# 九、再增加一个很重要的东西：Knowledge Authority

企业知识最容易出现的问题是：

```text
A制度：
采购审批金额 > 100万，需要总经理审批

B制度：
采购审批金额 > 50万，需要总经理审批
```

普通 RAG：

```text
两个Chunk都检索出来
```

LLM：

> 看起来两个都对……

这就是企业 AI 最大的风险之一。

Wiki应该增加：

```text
Authority
```

例如：

```text
知识：
采购审批金额

当前规则：
> 100万元

权威来源：
《采购管理制度 V3.2》

优先级：
★★★★★

适用范围：
中国区

生效日期：
2026-01-01

废止规则：
《采购管理制度 V3.1》
```

这样 Agent 才知道：

> **哪个知识可以相信。**

---

# 十、再进一步：建立 Knowledge Lifecycle

我建议：

```text
Draft
 ↓
Review
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

并且每个知识页面有：

```text
Owner
Reviewer
Authority
Effective Date
Expire Date
Version
Source
Confidence
```

这样就和你之前做的**主数据治理、数据质量治理**连接起来了。

---

# 十一、知识生成过程也要改变

传统：

```text
PDF
 ↓
Chunk
 ↓
Embedding
```

LLM Wiki：

```text
PDF / Word / Excel / ERP / PLM / SAP
             ↓
       Document Parser
             ↓
       LLM Knowledge Compiler
             ↓
       ┌─────┼─────┐
       ↓     ↓     ↓
     Entity Relation Claim
       │     │     │
       └─────┼─────┘
             ↓
        Wiki Generator
             ↓
       Human Review
             ↓
       Published Wiki
```

这其实就是“知识编译”。

Microsoft 的 GraphRAG 也是类似思想：从文档中抽取实体和关系，并形成层次化社区及摘要，而不是只做普通向量切片。([Microsoft GitHub](https://microsoft.github.io/graphrag/index/architecture/?utm_source=chatgpt.com "Architecture - GraphRAG"))

---

# 十二、Agent 怎么访问 Wiki？

这是 LLM Wiki 和普通 Wiki 最大的技术区别。

不要给 Agent 一个：

```text
search()
```

而是提供：

```text
search()
read()
links()
backlinks()
find_entity()
find_relation()
get_source()
get_version()
```

例如用户问：

> “为什么这个物料不能用于这个产品？”

Agent：

```text
search("物料A")
        ↓
read(Material A)
        ↓
links()
        ↓
BOM-100023
        ↓
read(BOM-100023)
        ↓
links()
        ↓
Product-X
        ↓
read(Product-X)
        ↓
Quality Rule
        ↓
read(Quality Rule)
```

最后得出：

> 物料 A 不能用于 Product-X，是因为该产品 BOM 的当前有效版本要求材料等级为 XX，而物料 A 的材料等级为 YY。

这就不是传统 RAG 的：

```text
找5个最相似Chunk
```

而是：

> **沿着知识网络进行推理。**

这也是 LLM-Wiki 所强调的 Retrieval-as-Reasoning。([arXiv](https://arxiv.org/abs/2605.25480?utm_source=chatgpt.com "Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki"))

---

# 十三、你的企业其实特别适合这种方式

结合你现在做的数据底座，我会建议你把整个体系重新定义成：

# AI-Ready Enterprise Knowledge Architecture

分成五层。

```text
┌──────────────────────────────────────┐
│              AI / Agent              │
├──────────────────────────────────────┤
│       Reasoning / Planning Layer     │
├──────────────────────────────────────┤
│          Enterprise LLM Wiki         │
│                                      │
│ Knowledge / Entity / Relation        │
│ Evidence / Authority / Version       │
├──────────────────────────────────────┤
│       Retrieval & Data Services      │
│ Wiki / Vector / Graph / SQL / API    │
├──────────────────────────────────────┤
│          Enterprise Data             │
│ SAP / PLM / MES / CRM / WMS / OA     │
│ Data Warehouse / Data Lake           │
└──────────────────────────────────────┘
```

你之前定义的：

> 统一业务对象
> 
> - 统一数据编码
>     
> - 统一数据模型
>     
> - 统一业务语义
>     
> - 统一指标口径
>     
> - 数据血缘
>     
> - 数据质量
>     
> - 结构化关系
>     
> - 非结构化知识关联
>     

实际上正好可以作为 **LLM Wiki 的底座**。

---

# 十四、我甚至建议你不要把它叫“知识库”

如果企业准备正式建设，我更建议叫：

### 企业知识 Wiki

或者：

### Enterprise Knowledge Fabric

或者中文：

# 企业知识底座

因为它最终应该覆盖：

```text
制度知识
产品知识
物料知识
客户知识
供应商知识
工艺知识
设备知识
质量知识
组织知识
业务流程知识
数据知识
指标知识
```

然后通过关系把它们连接起来。

---

# 十五、技术选型可以这样做

一个中型企业不需要一开始搞得特别复杂。

### MVP版本

```text
Markdown / JSON
        ↓
Git / Object Storage
        ↓
PostgreSQL
        ↓
全文搜索
        ↓
LLM
        ↓
Agent
```

知识页面：

```text
Markdown
```

关系：

```text
YAML / JSON
```

例如：

```yaml
id: material_100001
type: material
domain: supply_chain

links:
  - type: belongs_to
    target: material_category_01

  - type: used_in
    target: bom_100023

  - type: supplied_by
    target: supplier_000328
```

之后规模起来，再增加：

```text
PostgreSQL
+
OpenSearch
+
Vector DB
+
Graph DB
```

实际上 GraphRAG 本身也支持向量、全文、混合检索以及图遍历等不同能力，并不要求所有知识都塞进一种存储。([Microsoft GitHub](https://microsoft.github.io/graphrag/index/architecture/?utm_source=chatgpt.com "Architecture - GraphRAG"))

---

# 十六、我最推荐你的最终方案

不要做：

```text
             ┌→ Vector DB
文档 → Chunk ┤
             └→ LLM
```

而做：

```text
                    ┌─────────────┐
                    │   企业数据   │
                    └──────┬──────┘
                           ↓
                  Knowledge Compiler
                           ↓
              ┌────────────┴────────────┐
              ↓                         ↓
       Knowledge Wiki              Evidence
              │                         │
       ┌──────┼──────┐                  │
       ↓      ↓      ↓                  │
    Entity Relation Page                │
       │      │      │                  │
       └──────┼──────┘                  │
              ↓                         │
         Knowledge Graph                │
              │                         │
              └──────────┬──────────────┘
                         ↓
                    Agent Retrieval
                         │
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
       Wiki Read      Vector RAG      SQL/API
          └──────────────┼──────────────┘
                         ↓
                    LLM Reasoning
                         ↓
                   Answer + Evidence
```

**这会比单纯建设 RAG 知识库更适合你现在的数据底座建设方向。**

而且有一个非常关键的认知：

> **RAG解决的是“从哪里找资料”，LLM Wiki解决的是“企业到底知道什么、这些知识之间有什么关系、哪个知识有效、为什么可信，以及Agent应该沿着什么路径继续找”。**

所以我不会建议你把 RAG 完全废掉，而是把它从**主架构**降级成 **LLM Wiki 的一种检索能力**。

如果按你现在的企业数据治理项目继续往下做，我建议下一步直接设计一套 **《企业 LLM Wiki 总体架构与落地方案》**，具体到 **Wiki Page 模型、业务对象模型、关系模型、知识编译流程、Agent Tool API、数据库表结构、技术栈以及“采购域”完整样例**。这样就可以直接从你之前的“统一业务对象/语义/指标/血缘/知识关联”方案落到技术实现层。