

目前主流现代数据架构也基本呈现出类似趋势：数据平台负责数据摄取、存储、处理、治理和消费基础能力；更上层则逐步强调数据产品、语义层、数据服务以及 AI/检索能力。([AWS 文档](https://docs.aws.amazon.com/solutions/latest/modern-data-architecture-accelerator/architecture-details.html?utm_source=chatgpt.com "Architecture details - Modern Data Architecture Accelerator"))

# 《企业数据底座—数据中台—数据资产—LLM Wiki—企业AI总体架构》

## 一、总体定位

建议不要把目标定义成：

> **“建设一个数据中台。”**

而定义为：

> **建设企业统一数据底座，形成统一的数据资产与数据服务能力，并进一步构建企业知识体系，为BI、业务应用、AI和Agent提供可信的数据与知识基础。**

整体形成：

```text
                         ┌──────────────────────────────┐
                         │       企业应用与智能决策      │
                         │                              │
                         │ BI │ 经营分析 │ AI │ Agent   │
                         │ 企业大脑 │ 智能制造 │ 智能采购 │
                         └──────────────▲───────────────┘
                                        │
                              数据 + 知识 + 能力
                                        │
┌───────────────────────────────────────┴────────────────────────┐
│                    ⑤ 企业知识与AI层                            │
│                                                                │
│  LLM Wiki │ Knowledge Page │ Entity │ Relation │ Claim         │
│  Evidence │ Authority │ Rule │ Knowledge Graph │ RAG/Agent    │
└───────────────────────────────────────▲────────────────────────┘
                                        │
┌───────────────────────────────────────┴────────────────────────┐
│                    ④ 企业数据资产与服务层                       │
│                                                                │
│ Business Object │ 主数据 │ 数据产品 │ 指标 │ 标签 │ 数据服务    │
│ 数据目录 │ 数据资产 │ 数据语义 │ 数据关系 │ API │ SQL           │
└───────────────────────────────────────▲────────────────────────┘
                                        │
┌───────────────────────────────────────┴────────────────────────┐
│                    ③ 数据治理与数据中台层                       │
│                                                                │
│ 数据标准 │ 主数据 │ 元数据 │ 数据质量 │ 数据安全 │ 数据血缘      │
│ 数据模型 │ 指标管理 │ 数据生命周期 │ 数据责任 │ 数据服务管理     │
└───────────────────────────────────────▲────────────────────────┘
                                        │
┌───────────────────────────────────────┴────────────────────────┐
│                    ② 企业数据平台层                             │
│                                                                │
│ 数据集成 │ Lakehouse │ 数据仓库 │ ODS/DWD/DWS/ADS              │
│ 数据开发 │ 数据计算 │ 数据存储 │ 实时数据 │ API │ DataOps       │
└───────────────────────────────────────▲────────────────────────┘
                                        │
┌───────────────────────────────────────┴────────────────────────┐
│                    ① 企业数据源层                               │
│                                                                │
│ ERP │ PLM │ MES │ WMS │ CRM │ SRM │ OA │ IoT │ Excel │ 文件    │
└────────────────────────────────────────────────────────────────┘
```

---

# 二、五层到底分别解决什么问题？

这是整套架构最重要的地方。

|层|核心问题|核心产物|
|---|---|---|
|① 数据源层|数据在哪里？|ERP/PLM/MES等|
|② 数据平台层|数据怎么汇聚、存储、加工？|Lakehouse/数仓/数据管道|
|③ 数据中台/治理层|数据怎么统一、治理、管理？|标准、主数据、质量、血缘|
|④ 数据资产/服务层|数据怎么被复用？|BO、指标、数据产品、API|
|⑤ 知识/AI层|AI如何理解企业？|Wiki、Entity、Relation、Claim、Evidence|

这个划分可以有效避免：

> **把所有东西都叫“数据中台”。**

---

# 三、数据平台：解决“数据基础设施”

数据平台是整个体系的技术基础。

核心包括：

```text
数据采集
   ↓
数据传输
   ↓
数据存储
   ↓
数据计算
   ↓
数据加工
   ↓
数据服务
```

例如：

### 数据存储

```text
Data Lake
Lakehouse
ODS
DWD
DWS
ADS
```

### 数据计算

```text
SQL
Spark
Flink
Python
```

### 数据集成

```text
ERP
PLM
MES
WMS
CRM
SRM
→ CDC / ETL / API
```

### 数据开发

```text
数据开发
任务调度
版本管理
DataOps
监控
```

现代数据平台通常也会把 Lakehouse、治理、DataOps、分析以及AI能力组合在一起，但这些仍属于**平台能力**，不等于企业已经形成了完整的数据资产和业务能力。([AWS 文档](https://docs.aws.amazon.com/solutions/latest/modern-data-architecture-accelerator/architecture-details.html?utm_source=chatgpt.com "Architecture details - Modern Data Architecture Accelerator"))

---

# 四、数据中台：解决“企业数据能力统一”

这一层才真正体现“中台”。

例如企业有五个系统都有供应商：

```text
SAP
SRM
PLM
OA
财务系统
```

数据平台做：

```text
五个系统
   ↓
统一汇聚
   ↓
数据仓库
```

但是数据中台继续做：

```text
                Supplier
                   │
       ┌───────────┼───────────┐
       ↓           ↓           ↓
   统一编码      统一名称      统一分类
       │           │           │
       └───────────┼───────────┘
                   ↓
              统一供应商对象
                   │
       ┌───────────┼───────────┐
       ↓           ↓           ↓
     采购关系    质量关系    财务关系
```

所以：

> **数据平台解决“数据集中”；数据中台解决“数据统一和能力复用”。**

---

# 五、数据资产层：把“数据”变成“可消费产品”

这是很多企业数据建设容易缺失的一层。

例如原来有：

```text
供应商表
采购订单表
收货表
质检表
付款表
```

对于业务来说，这些只是“数据”。

数据资产层把它们包装成：

### 供应商数据产品

```text
供应商基本信息
供应商状态
供应商分类
供应商等级
采购金额
采购次数
准时交付率
质量合格率
付款情况
合作关系
```

业务人员不需要理解底层几十张表。

直接消费：

> **供应商数据产品**

这也是现代数据架构强调 Data Product 的原因之一——数据不仅作为物理表存在，还需要具备明确的业务语义、所有权、质量和消费接口。([Google Cloud Documentation](https://docs.cloud.google.com/architecture/data-mesh?utm_source=chatgpt.com "Architecture and functions in a data mesh  |  Cloud Architecture Center  |  Google Cloud Documentation"))

---

# 六、Business Object是这里非常关键的一层

这正好和你之前设计的体系连接起来。

建议企业统一定义：

```text
Material
Supplier
Customer
Product
Employee
Organization
Equipment
BOM
Purchase Order
Sales Order
Work Order
Production Order
```

例如：

```text
Material
│
├── Material ID
├── Material Name
├── Material Category
├── Material Type
├── Specification
├── Unit
├── Status
├── Lifecycle
├── Supplier
├── BOM
├── Inventory
├── Purchase
├── Quality
└── Related Knowledge
```

这时候：

> **Business Object就是数据平台和业务/AI之间的重要桥梁。**

---

# 七、LLM Wiki不是传统知识库

这是你现在这个架构最值得升级的地方。

传统知识库：

```text
PDF
Word
Excel
网页
↓
Chunk
↓
Embedding
↓
Vector DB
↓
RAG
```

LLM Wiki则应该进一步形成：

```text
Entity
   ↓
Relation
   ↓
Claim
   ↓
Evidence
   ↓
Authority
   ↓
Knowledge Page
```

例如：

```text
供应商A
  │
  ├── supplies → 物料X
  │
  ├── belongs_to → A类供应商
  │
  ├── has_contract → 合同2026-001
  │
  ├── passed → 供应商审核
  │
  └── has_quality_issue → 质量问题Q001
```

然后每个判断都可以追溯：

```text
Claim
  ↓
Evidence
  ↓
原始业务数据/文件/制度/报告
```

这样AI回答：

> “为什么供应商A目前不能进入核心供应商名单？”

不是简单生成一句话，而可以形成：

```text
结论
 ↓
原因
 ↓
相关事实
 ↓
证据
 ↓
原始来源
```

这比单纯RAG更适合作为企业长期知识体系。

---

# 八、最终形成“数据 → 资产 → 知识 → AI”的链路

我建议你以后对客户解释整个体系时，就用这条主线：

```text
企业业务系统
      ↓
    原始数据
      ↓
   数据平台
      ↓
   数据治理
      ↓
  统一Business Object
      ↓
    数据资产
      ↓
   业务语义/关系
      ↓
    企业知识
      ↓
    LLM Wiki
      ↓
    企业AI
      ↓
 AI Agent / 智能决策
```

这条链路实际上把你前面做的几个项目全部串起来了。

---

# 九、你之前提出的“9个统一”放在哪里？

你之前提出：

> **统一业务对象 + 统一数据编码 + 统一数据模型 + 统一业务语义 + 统一指标口径 + 完整数据血缘 + 可信数据质量 + 结构化数据关系 + 非结构化知识关联**

我建议正式定位成：

## 企业AI-Ready数据能力的核心框架

对应关系：

|能力|所属层|
|---|---|
|统一业务对象|数据资产层|
|统一数据编码|数据治理层|
|统一数据模型|数据平台/治理层|
|统一业务语义|数据资产层|
|统一指标口径|数据资产层|
|完整数据血缘|数据治理层|
|可信数据质量|数据治理层|
|结构化数据关系|数据资产/知识层|
|非结构化知识关联|LLM Wiki层|

这样一来，你的体系就非常完整了。

---

# 十、最后给这几个概念一个“标准定义”

以后做PPT、方案、汇报时，可以直接采用下面的定义：

### 数据平台 Data Platform

> 面向企业数据采集、存储、计算、加工、治理和服务的数据技术基础设施，为企业数据资产和数据应用提供统一的数据处理与技术支撑能力。

### 数据中台 Data Middle Platform

> 面向企业级数据共享与能力复用，对跨系统、跨业务域的数据进行统一标准、统一模型、统一治理和统一服务，形成可复用的企业数据能力。

### 数据资产 Data Asset

> 经标准化、治理、确权、质量评价和业务语义定义后，可被发现、理解、复用和消费的数据资源及其服务化能力。

### 企业知识层 Enterprise Knowledge Layer

> 将企业结构化数据、业务规则、制度文档、经验知识及业务关系组织为可理解、可追溯、可验证的企业知识体系。

### LLM Wiki

> 以企业业务对象、实体、关系、知识主张和证据为核心组织企业知识，并通过大语言模型提供企业知识理解、检索、推理和生成能力的知识体系。

### 企业AI

> 基于企业可信数据、数据资产和企业知识，通过大模型、Agent和业务工具实现分析、问答、预测、决策辅助及业务执行的智能化应用体系。

---

## 最终一句话

现在这套体系，应该叫：

> **“企业数据底座与AI-Ready数据能力体系建设”**

架构主线则是：

**数据源 → 数据平台 → 数据治理/中台 → 数据资产/服务 → 企业知识/LLM Wiki → 企业AI/Agent**

这会比传统的“数据中台”概念更准确，也更能把你目前正在做的**主数据、数据治理、Business Object、AI-Ready、LLM Wiki和企业大脑**统一到一个架构里。