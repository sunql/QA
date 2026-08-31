



# 《企业 AI-Ready 数据标准体系》

### —— 企业数据仓库、数据治理与 AI 数据底座建设规范

---

# 1. 总体定位

## 1.1 建设目标

本标准体系的目标不是单纯建设一个用于报表和 BI 的数据仓库，而是建设企业统一的：

> **数据管理体系 + 数据仓库体系 + 数据治理体系 + AI 数据底座体系。**

总体目标：

```text
业务系统数据
        ↓
企业统一数据
        ↓
企业统一业务语义
        ↓
可信数据资产
        ↓
AI 可理解的数据与知识
        ↓
BI + Analytics + ML + LLM + Agent
```

最终形成：

```text
              ┌────────────────────┐
              │   企业业务系统       │
              │                    │
              │ PLM / ERP / MES     │
              │ WMS / QMS / SRM     │
              │ CRM / APS / EAM     │
              │ HR / FIN / IoT      │
              └─────────┬──────────┘
                        │
                        ▼
              ┌────────────────────┐
              │    企业数据接入层    │
              │ CDC / API / ETL     │
              │ FILE / IoT / Stream │
              └─────────┬──────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────┐
│                    数据仓库层                       │
│                                                    │
│   ODS → DIM + DWD → DWS → ADS                     │
│                                                    │
└──────────────────────┬─────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌────────────┐  ┌────────────┐  ┌────────────┐
│ Semantic   │  │ Feature    │  │ Knowledge  │
│ Layer      │  │ Layer      │  │ Layer      │
│ 语义层      │  │ 特征层      │  │ 知识层      │
└─────┬──────┘  └─────┬──────┘  └─────┬──────┘
      └───────────────┼───────────────┘
                      ▼
              ┌─────────────────┐
              │   AI Platform   │
              │                 │
              │ LLM / RAG       │
              │ ML / Agent      │
              │ Prediction      │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │   AI 应用层      │
              │                 │
              │ BI / Dashboard  │
              │ 智能问答         │
              │ 智能分析         │
              │ 智能预警         │
              │ 智能决策         │
              └─────────────────┘
```

---

# 2. 企业数据架构总体框架

建议采用 **“五层数据仓库 + 三层 AI 数据能力 + 横向数据治理体系”**。

```text
                        AI APPLICATION
                              │
                    ┌─────────┴─────────┐
                    │                   │
                  ADS                AI/Agent
                    │                   │
                    └─────────┬─────────┘
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
         SEMANTIC          FEATURE         KNOWLEDGE
          语义层            特征层            知识层
             │                │                │
             └────────────────┼────────────────┘
                              │
                             DWS
                              │
                       DIM + DWD
                              │
                             ODS
                              │
                       SOURCE SYSTEM
```

横向贯穿：

```text
┌───────────────────────────────────────────────┐
│              DATA GOVERNANCE                  │
│                                               │
│ 数据标准 │ 主数据 │ 元数据 │ 质量 │ 血缘       │
│ 数据安全 │ 权限 │ 生命周期 │ 指标 │ AI治理     │
└───────────────────────────────────────────────┘
```

---

# 3. 标准体系总体目录

建议正式形成以下 **12 个一级标准模块**。

```text
AI-Ready 企业数据标准体系
│
├── 01 数据架构标准
├── 02 数据仓库分层标准
├── 03 数据模型设计标准
├── 04 数据命名与编码标准
├── 05 主数据与一致性维度标准
├── 06 数据质量管理标准
├── 07 元数据与数据血缘标准
├── 08 指标与业务语义标准
├── 09 数据安全与权限标准
├── 10 AI 特征数据标准
├── 11 AI 知识数据标准
└── 12 AI 数据治理与生命周期标准
```

---

# 第一部分：数据架构标准

## 4. 数据架构原则

企业数据架构必须遵循：

### 4.1 Single Source of Truth

> 同一业务事实必须建立统一可信数据来源。

例如：

```text
销售订单
→ DWD_SALES_ORDER

库存
→ DWD_INVENTORY_SNAPSHOT

生产报工
→ DWD_PRODUCTION_REPORT
```

禁止：

```text
销售部门一套销售数据
财务部门一套销售数据
工厂自己一套销售数据
```

---

### 4.2 Conformed Dimension

所有主题域共享一致性维度。

例如：

```text
DIM_PRODUCT
      │
      ├── SALES
      ├── PURCHASE
      ├── PRODUCTION
      ├── INVENTORY
      ├── QUALITY
      └── COST
```

---

### 4.3 AI Understandable

所有数据必须具备：

```text
名称
定义
粒度
单位
来源
负责人
业务规则
血缘
```

原则：

> **没有定义的数据，不得作为 AI 正式决策依据。**

---

# 第二部分：数据仓库分层标准

## 5. ODS 标准

### 定位

> 原始业务数据接入层。

命名：

```text
ODS_<SOURCE_SYSTEM>_<SOURCE_OBJECT>
```

例如：

```text
ODS_ERP_ITMMASTER
ODS_ERP_SORDER
ODS_PLM_PRODUCT
ODS_MES_PRODUCTION_REPORT
```

要求：

- 保留来源数据
    
- 保留来源系统
    
- 保留业务主键
    
- 支持数据追溯
    
- 原则上不改变业务语义
    

统一技术字段：

```text
SRC_SYSTEM
SRC_OBJECT
SRC_PK
SRC_CREATE_TIME
SRC_UPDATE_TIME
ETL_BATCH_ID
LOAD_TIME
IS_DELETE
```

---

# 6. DIM 标准

DIM 定位：

> **企业统一业务实体与分析语义层。**

核心维度：

```text
DIM_DATE
DIM_PRODUCT
DIM_MATERIAL
DIM_CUSTOMER
DIM_SUPPLIER
DIM_ORGANIZATION
DIM_PLANT
DIM_WAREHOUSE
DIM_PROCESS
DIM_OPERATION
DIM_EQUIPMENT
DIM_TOOLING
DIM_PROJECT
```

统一主键：

```text
XXX_KEY
```

例如：

```text
PRODUCT_KEY
CUSTOMER_KEY
PLANT_KEY
```

统一业务编码：

```text
XXX_CODE
```

统一名称：

```text
XXX_NAME
```

---

# 7. DWD 标准

DWD 定位：

> 企业标准业务事实明细层。

命名：

```text
DWD_<DOMAIN>_<BUSINESS_PROCESS>
```

例如：

```text
DWD_SALES_ORDER
DWD_PURCHASE_ORDER
DWD_PRODUCTION_ORDER
DWD_PRODUCTION_REPORT
DWD_INVENTORY_TRANSACTION
DWD_QUALITY_INSPECTION
```

每张 DWD 必须定义：

```text
业务过程
业务粒度
业务主键
维度关联
度量指标
来源系统
刷新频率
```

例如：

```text
TABLE：
DWD_PRODUCTION_REPORT

GRAIN：
一条记录代表：
一个工单
+
一个工序
+
一次报工
```

---

# 8. DWS 标准

定位：

> 企业主题域公共数据和公共指标层。

例如：

```text
DWS_SALES_DAILY
DWS_PRODUCTION_DAILY
DWS_INVENTORY_DAILY
DWS_QUALITY_DAILY
DWS_COST_MONTHLY
```

原则：

```text
可复用
统一口径
避免面向单一报表
```

---

# 9. ADS 标准

定位：

> 面向具体应用的数据服务层。

例如：

```text
ADS_EXECUTIVE_OPERATION
ADS_SALES_DASHBOARD
ADS_PRODUCTION_DASHBOARD
ADS_INVENTORY_ALERT
ADS_QUALITY_ALERT
```

ADS 可以面向：

```text
BI
Dashboard
API
AI Application
Agent
Alert
```

---

# 第三部分：数据模型设计标准

## 10. 数据对象分类

企业数据对象统一分为：

```text
Master Data
Reference Data
Transaction Data
Event Data
Snapshot Data
Metric Data
Document Data
Knowledge Data
Feature Data
```

建议中文对应：

|类型|中文|
|---|---|
|Master|主数据|
|Reference|参考数据|
|Transaction|交易数据|
|Event|事件数据|
|Snapshot|快照数据|
|Metric|指标数据|
|Document|文档数据|
|Knowledge|知识数据|
|Feature|特征数据|

---

# 11. 数据粒度标准

每张事实表必须定义：

```text
GRAIN
```

例如：

```text
DWD_SALES_ORDER

GRAIN =
一个销售订单行
```

```text
DWD_INVENTORY_TRANSACTION

GRAIN =
一次库存变动
```

禁止：

> 表有数据，但不知道“一行代表什么”。

---

# 第四部分：数据命名与编码标准

## 12. 表命名

统一：

```text
<LAYER>_<DOMAIN>_<OBJECT>
```

例如：

```text
DIM_PRODUCT
DWD_SALES_ORDER
DWS_SALES_DAILY
ADS_SALES_DASHBOARD
```

---

## 13. 字段命名

### 主键

```text
XXX_KEY
```

### 编码

```text
XXX_CODE
```

### 名称

```text
XXX_NAME
```

### 数量

```text
XXX_QTY
```

### 金额

```text
XXX_AMT
```

### 比率

```text
XXX_RATE
```

### 日期

```text
XXX_DATE
```

### 时间

```text
XXX_TIME
```

### 布尔标识

```text
IS_XXX
```

例如：

```text
IS_CURRENT
IS_DELETE
IS_VALID
```

---

# 第五部分：主数据与一致性维度标准

## 14. 企业统一实体

建议建立企业统一实体模型：

```text
Customer
Supplier
Product
Material
Project
Organization
Plant
Warehouse
Equipment
Tooling
Process
Operation
Employee
```

每个核心实体必须具备：

```text
ENTITY_KEY
ENTITY_CODE
ENTITY_NAME
ENTITY_STATUS
SRC_SYSTEM
VALID_FROM
VALID_TO
IS_CURRENT
```

---

## 15. 跨系统编码映射

建议建立：

```text
DIM_ENTITY_MAPPING
```

例如：

|ENTITY_KEY|SYSTEM|SOURCE_CODE|
|---|---|---|
|10001|ERP|A001|
|10001|MES|M001|
|10001|PLM|P001|

实现：

```text
多个系统编码
        ↓
企业统一实体
        ↓
ENTITY_KEY
```

---

# 第六部分：数据质量管理标准

## 16. 数据质量维度

统一定义：

```text
Completeness
Accuracy
Consistency
Uniqueness
Timeliness
Validity
```

即：

```text
完整性
准确性
一致性
唯一性
及时性
有效性
```

---

## 17. 数据质量评分

建议关键数据集维护：

```text
QUALITY_SCORE
```

例如：

```text
完整性：98%
准确性：95%
一致性：99%
及时性：92%

综合质量评分：96
```

AI 应用读取数据时可以获得：

```text
Data Quality Score
```

未来 AI 输出可以明确：

> “该结论基于质量评分 96 的生产数据。”

---

# 第七部分：元数据与数据血缘标准

## 18. 元数据要求

每张表必须登记：

```text
TABLE_NAME
BUSINESS_NAME
BUSINESS_DEFINITION
DATA_OWNER
DOMAIN
GRAIN
REFRESH_FREQUENCY
SOURCE_SYSTEM
SECURITY_LEVEL
```

每个字段必须登记：

```text
COLUMN_NAME
BUSINESS_NAME
BUSINESS_DEFINITION
DATA_TYPE
UNIT
NULLABLE
SOURCE_FIELD
```

---

## 19. 数据血缘

统一血缘：

```text
Source
  ↓
ODS
  ↓
DIM / DWD
  ↓
DWS
  ↓
ADS
  ↓
KPI / AI
```

例如：

```text
ERP.SORDER
    ↓
ODS_ERP_SORDER
    ↓
DWD_SALES_ORDER
    ↓
DWS_SALES_DAILY
    ↓
KPI_SALES_REVENUE
```

---

# 第八部分：指标与业务语义标准

## 20. 企业业务术语库

建立：

```text
BUSINESS_GLOSSARY
```

包括：

```text
TERM_CODE
TERM_NAME
TERM_EN_NAME
DEFINITION
SYNONYM
OWNER
```

例如：

```text
TERM_NAME：
完工数量

SYNONYM：
产量
完成数量
Output Qty

DEFINITION：
通过规定工序并完成确认的合格产品数量。
```

---

## 21. 企业指标中心

建立：

```text
KPI_CATALOG
```

每个 KPI 必须定义：

```text
KPI_CODE
KPI_NAME
BUSINESS_DEFINITION
FORMULA
SOURCE_TABLE
DIMENSIONS
UNIT
OWNER
VERSION
```

例如：

```text
KPI_PRODUCTION_OEE
```

AI 可以通过指标中心知道：

> OEE 的定义、算法和数据来源。

---

# 第九部分：AI 特征数据标准

## 22. Feature Layer

建议建立：

```text
FEATURE_<DOMAIN>_<OBJECT>
```

例如：

```text
FEATURE_PRODUCT_DEMAND
FEATURE_CUSTOMER_BEHAVIOR
FEATURE_EQUIPMENT_HEALTH
FEATURE_QUALITY_RISK
```

每个特征必须定义：

```text
FEATURE_NAME
FEATURE_DEFINITION
ENTITY_KEY
VALUE
CALCULATION_LOGIC
WINDOW
VERSION
SOURCE
```

例如：

```text
FEATURE_NAME：
PRODUCT_30D_SALES_QTY

DEFINITION：
产品最近30天销售数量

ENTITY：
PRODUCT_KEY

WINDOW：
30D
```

---

## 23. 特征生命周期

```text
Raw Data
   ↓
Feature Engineering
   ↓
Feature Validation
   ↓
Feature Store
   ↓
Model Training
   ↓
Online / Batch Serving
```

要求：

> 训练特征与线上推理特征必须保持定义一致。

---

# 第十部分：AI 知识数据标准

## 24. Knowledge Layer

知识来源：

```text
SOP
工艺文件
FMEA
Control Plan
8D
检验规范
设备手册
维修记录
客户投诉
企业制度
```

每份文档必须标准化：

```text
DOCUMENT_ID
DOCUMENT_TYPE
DOCUMENT_NAME
VERSION
STATUS
OWNER
EFFECTIVE_DATE
SOURCE_SYSTEM
```

---

## 25. 文档业务关联

文档不能成为孤立文件。

例如：

```text
FMEA
 │
 ├── PRODUCT_KEY
 ├── PROCESS_KEY
 └── OPERATION_KEY
```

8D：

```text
8D_REPORT
 │
 ├── CUSTOMER_KEY
 ├── PRODUCT_KEY
 └── DEFECT_KEY
```

---

# 26. RAG 数据标准

用于 AI 检索的数据建议增加：

```text
CHUNK_ID
DOCUMENT_ID
CHUNK_TEXT
CHUNK_SEQUENCE
EMBEDDING_VERSION
EFFECTIVE_DATE
SECURITY_LEVEL
```

同时必须保留：

```text
原始文档
↓
文档版本
↓
文本分块
↓
向量
```

确保 AI 回答可追溯。

---

# 第十一部分：企业知识关系标准

建议逐步建立：

```text
ENTITY_RELATION
```

结构：

```text
SOURCE_ENTITY_TYPE
SOURCE_ENTITY_KEY

RELATION_TYPE

TARGET_ENTITY_TYPE
TARGET_ENTITY_KEY

VALID_FROM
VALID_TO
```

例如：

```text
PRODUCT
   ── BELONGS_TO ──→ PROJECT

PRODUCT
   ── USES ──→ MATERIAL

PRODUCT
   ── PRODUCED_BY ──→ PROCESS

PROCESS
   ── CONTAINS ──→ OPERATION

OPERATION
   ── USES ──→ EQUIPMENT
```

这将成为后续知识图谱的基础。

---

# 第十二部分：AI 数据治理

## 27. AI 数据可信度

建议建立：

```text
AI_DATA_TRUST_LEVEL
```

例如：

|等级|定义|
|---|---|
|A|可直接用于关键分析|
|B|可用于一般分析|
|C|仅供参考|
|D|不允许 AI 自动决策|

可信度可以综合：

```text
数据质量
+
数据及时性
+
来源可信度
+
口径稳定性
```

---

## 28. AI 输出可解释性

AI 输出必须能够追溯：

```text
AI Answer
    ↓
KPI
    ↓
DWS
    ↓
DWD
    ↓
ODS
    ↓
Source
```

对于 RAG：

```text
AI Answer
    ↓
Knowledge Chunk
    ↓
Document
    ↓
Document Version
```

原则：

> **AI 的结论必须能够说明“依据是什么”。**

---

# 29. 数据安全与 AI 权限

AI 不应拥有无限数据访问权限。

建议：

```text
USER
  ↓
ROLE
  ↓
DATA_PERMISSION
  ↓
AI_PERMISSION
```

AI Agent 调用数据时必须继承：

> **用户权限，而不是 AI 自身最高权限。**

例如：

```text
集团领导
→ 全集团数据

工厂经理
→ 本工厂数据

普通员工
→ 授权数据
```

---

# 30. AI 数据标准成熟度模型

建议分为五级。

## Level 1：Data Available

```text
数据存在
```

特点：

```text
Excel
系统数据库
数据孤岛
```

---

## Level 2：Data Standardized

```text
数据标准化
```

建立：

```text
ODS
DIM
DWD
```

---

## Level 3：Data Governed

```text
数据治理
```

建立：

```text
质量
元数据
血缘
主数据
```

---

## Level 4：AI Ready

```text
AI Ready Data
```

增加：

```text
Semantic Layer
Feature Layer
Knowledge Layer
```

---

## Level 5：AI Native Enterprise

```text
AI Native Data Enterprise
```

实现：

```text
AI Agent
+
Knowledge Graph
+
Predictive Model
+
Automated Decision
```

---

# 31. 最终标准体系架构

建议最终企业架构采用：

```text
┌─────────────────────────────────────────────────────────────┐
│                      BUSINESS SYSTEMS                       │
│ PLM │ ERP │ MES │ WMS │ QMS │ SRM │ CRM │ EAM │ HR │ IoT  │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                           ODS                               │
│              原始数据 / 历史数据 / 数据追溯                  │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
              ┌────────────┴────────────┐
              ▼                         ▼
┌──────────────────────┐   ┌─────────────────────────────────┐
│        DIM           │   │               DWD               │
│ 企业统一实体与维度     │   │         标准业务事实明细         │
└──────────┬───────────┘   └──────────────┬──────────────────┘
           └───────────────┬──────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                           DWS                               │
│              主题数据 / 公共指标 / 统一口径                  │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
      ┌────────────────────┼────────────────────┐
      ▼                    ▼                    ▼
┌─────────────┐     ┌─────────────┐      ┌─────────────┐
│ SEMANTIC    │     │ FEATURE     │      │ KNOWLEDGE   │
│ 语义层       │     │ 特征层       │      │ 知识层       │
│             │     │             │      │             │
│ 术语         │     │ ML Feature  │      │ Document    │
│ 指标         │     │ FeatureStore│      │ Vector      │
│ 规则         │     │ Prediction  │      │ Graph       │
└──────┬──────┘     └──────┬──────┘      └──────┬──────┘
       └───────────────────┼────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                         AI PLATFORM                         │
│        LLM / RAG / ML / Agent / Forecast / Optimization     │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
             ┌─────────────┴──────────────┐
             ▼                            ▼
┌────────────────────────┐    ┌──────────────────────────────┐
│          ADS           │    │       AI APPLICATION         │
│ BI / Dashboard / API   │    │ 问答 / 分析 / 预测 / Agent    │
└────────────────────────┘    └──────────────────────────────┘


═══════════════════════════════════════════════════════════════
DATA GOVERNANCE
═══════════════════════════════════════════════════════════════

Data Standard
Master Data
Metadata
Data Quality
Data Lineage
Metric Governance
Security
Data Ownership
AI Governance
Lifecycle Management
```

---

# 32. 最终建议的制度文件体系

建议最终正式形成以下文件：

```text
01 《企业数据架构标准》
02 《数据仓库分层建设规范》
03 《数据模型设计规范》
04 《数据库、表及字段命名规范》
05 《企业主数据与一致性维度管理规范》
06 《企业数据编码标准》
07 《数据质量管理规范》
08 《元数据与数据目录管理规范》
09 《数据血缘管理规范》
10 《企业业务术语管理规范》
11 《企业指标管理规范》
12 《AI 特征数据管理规范》
13 《AI 知识数据管理规范》
14 《AI 数据权限与安全规范》
15 《AI 数据可信度与可解释性规范》
16 《AI 数据生命周期管理规范》
```

---

# 33. 建议的实施优先级

不建议一次性建设所有内容。

建议分为四个阶段。

## 第一阶段：数据基础标准化

```text
ODS
DIM
DWD
```

优先完成：

```text
主数据统一
编码统一
数据粒度
数据模型
```

---

## 第二阶段：数据治理

完成：

```text
数据质量
元数据
血缘
数据责任
指标管理
```

---

## 第三阶段：AI-Ready

建设：

```text
Business Glossary
Semantic Layer
Feature Layer
Knowledge Layer
```

---

## 第四阶段：AI 应用

建设：

```text
智能问答
经营分析
需求预测
库存预测
质量预测
预测性维护
AI Agent
```

---

# 最终核心结论

这套体系最核心的变化是把企业数据能力从：

```text
数据 → 报表
```

升级为：

```text
数据
 ↓
标准
 ↓
语义
 ↓
知识
 ↓
智能
```

因此，对于你当前的制造业企业环境，我建议未来的数据平台不要定位成：

> **“数据仓库项目”**

而应该定位为：

> **“企业数据资产与 AI 数据底座建设项目”**

其中最核心的基础是：

```text
统一业务对象
+
统一数据编码
+
统一数据模型
+
统一业务语义
+
统一指标口径
+
完整数据血缘
+
可信数据质量
+
结构化数据关系
+
非结构化知识关联
```

只有先完成这些基础，后续无论使用 LLM、RAG、Agent，还是需求预测、质量预测、预测性维护，AI 才真正能够**理解企业数据、理解企业业务，并基于可信数据进行分析和决策**。