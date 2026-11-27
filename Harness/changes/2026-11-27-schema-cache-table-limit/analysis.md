---
created: 2026-11-27
updated: 2026-11-27
---

# 分析：数据源 11「schema 表数量 20494 超过上限 3000」

- **分支**：`fix/schema-cache-table-limit`（基线 `origin/main` = `ad94efeb`）
- **现象**：在「数据源」页对数据源 11 点「缓存 Schema」⇒
  `数据源 11 schema 表数量 20494 超过上限 3000：请检查 schema owner 范围，或调整上限配置`，
  浏览器 F12 显示 `400 (Bad Request)`。
- **性质**：**分析**（本文只出结论与选项，未改任何产品代码）。

## 1. 报错从哪来

| 层 | 位置 | 事实 |
|---|---|---|
| 上限定义 | `backend/app/config.py:120` | `schemaMaxTables: int = Field(default=3000, alias="SCHEMA_MAX_TABLES")` |
| 判定点 | `backend/app/services/schema_introspection_service.py:315-322`（全量 `introspect`）/ `:348-355`（分批 `introspectBatched`，**跑批之前**就判） | `len(tables) > maxTables` ⇒ 抛 `DataSourceError` |
| HTTP 映射 | `backend/app/domain/exceptions.py:230-254` `statusForError` | `DataSourceError` 落到**默认 400** ⇒ 与 F12 的 400 一致 |
| 入口 | `POST /api/v1/datasources/{id}/introspect`、`POST .../introspect-jobs` | 同步 / 异步两条路都走同一个 service |

**运行态**：容器里**没有设** `SCHEMA_MAX_TABLES` ⇒ 用的是默认 3000。
且该 env **在 `backend/.env.example` / `docker/.env` 里都没有条目** ⇒
报错文案让人「调整上限配置」，但**运维根本不知道这个旋钮存在**（文档缺口）。

## 2. 20494 是怎么来的（直连 Oracle 实测，非估算）

数据源 11 = **「LQ- Supply」**（Oracle，`10.179.1.81:1522/ORCLPDB`，用户 `lc019999`，
2026-11-25 新建，`is_active=t`）。直连该实例实测：

```
ALL_TABLES（连接用户可见）           = 20245 张
  其中 LC019999 自有                 = 19999
       SYS 141 / MDSYS 56 / XDB 35 / SYSTEM 6 / CTXSYS 5 / OUTLN 3
ALL_TAB_COLUMNS, owner='LC019999'   = 20494 张   ← 与报错数字逐字对上
视图数（ALL_VIEWS, owner='LC019999'）= 495
```

⇒ **20494 = 19999 张表 + 495 个视图**（内省口径走 `ALL_TAB_COLUMNS`，含视图）。

### 2.1 「请检查 schema owner 范围」这条提示在本数据源上**无效**

Oracle 的 owner 取自**连接用户名**（`_normalizeSchemaName`：显式 owner 优先，缺省
`UPPER(username)`）；而 `listSchemas`（`GET /datasources/{id}/schemas`）列出的是
该连接**可见的全部 owner**。实测可见 owner 只有上面 7 个 —— **除它自己以外全是系统
schema** ⇒ **没有更小的业务 owner 可切**。

⇒ 这不是「选错了 owner」，而是「**这个 owner 本身就有近 2 万张表**」。

## 3. 这个 schema 是什么？—— 与本系统的本体体系**零交集**

LC019999 的表名样例（实测）：

```
AIANSWERS  AIBOTSKILLREL  AIBOTSUBJECTREL  AICAGENT  AICAGENTAUTH
AICAGENTKNOWLEDGEBASE  AICAGENTMODELINFO  AICAGENTPROMPT  AICAGENTTOOL
AICCOMPONENT  AICHATBOTS ...
```

⇒ 这是一套 **AI 平台（智能体 / 知识库 / 对话）的库**，命名体系与本系统的
`DWD_* / DIM_* / ODS_*` 完全不同。

**与本体的交集实测为 0**：

```
本体引用的物理表（去重）              = 78 张
其中在 LC019999 下存在的               = 0 张
LC019999 下 DWD_/DIM_/ODS_/MST_/TRD_ 前缀表 = 0 张
```

## 4. schema_cache 到底给谁用？—— 决定了「缓存下来有没有用」

| 消费者 | 需要缓存里的什么 | 位置 |
|---|---|---|
| 跨源幽灵表过滤 | **只要表名** | `services/recall_datasource_filter.py` |
| 本体表/列漂移校验 | 表名 + **被引用表的列** | `services/schema_introspection_service.py:757+` |
| 数据层分布 `/layers` | **只要表名** | `api/v1/datasource.py:255+` |
| 本体导入向导（选表建类） | 表名 + **该表的列** | 前端数据源页 / 本体页 |
| NL2SQL 提示词 | —— | `services/chat_service.py:1139`：**「本体是 LLM 渲染 schema 的唯一来源」**，schema_cache 只用于生成漂移告警 |

⇒ **2 万张表从不被注入 prompt**。schema_cache 是「物理真相字典」，不是问数的前提。

## 5. 现状：数据源 11 从来没缓存成功过

```
schema_cache 现有 5 行：ds1/THBI 216 张(150KB)、ds1/ZJTH 1680 张(642KB)、
                       ds1/OUTLN 3 张、ds2 52 张、ds10 217 张
⇒ **没有 ds11 的行**（一次都没成功）
```
最大的成功样本是 ZJTH 的 1680 张 / 642KB。

## 6. 调大上限的代价（实测数据 + 外推）

**元数据体量（直连该 Oracle 实例实测）**：

| 指标 | 实测值 |
|---|---|
| 表数（含视图） | 20494 |
| 列数（表×列） | **301121**（平均 14.7 列/表） |
| 表注释条数 | 20535 |
| 列注释条数 | **301806** |
| 主键约束 | 5309 |
| 外键约束 | 10 |

**缓存体积外推**（用同类 Oracle 样本的「字节/列」比例）：

| 样本 | 字节 | 表 | 列 | 字节/列 |
|---|---|---|---|---|
| ZJTH（Oracle） | 642012 | 1680 | 73207 | 8.77 |
| THBI（Oracle） | 150042 | 216 | 9547 | 15.7 |
| ds10（MySQL） | 139611 | 217 | 4807 | 29.0 |

⇒ LC019999（301121 列）**≈ 2.6 MB（按 ZJTH 比例）～ 4.7 MB（按 THBI 比例）** 单行 JSONB。
（注：不能按「表数」外推 —— LC019999 平均只有 14.7 列/表，而 ZJTH 有 43.6 列/表。）

**内省耗时**：5 类元数据查询 × ⌈20494/100⌉ = **205 批 ⇒ 1025 次 Oracle 往返**，
其中「表注释 + 列注释」两项要拉 **32 万条**注释 ⇒ **注释查询是耗时大头**。

**前端**：`GET /datasources/11/schema` 一次返回数 MB JSON，「数据源」页还要把 2 万行
渲染成表格。

**护栏是全局 env** ⇒ 调高会同时放宽**所有**数据源的上限。

## 7. 实测（2026-11-27，本机直连该 Oracle 实例）

| 指标 | 值 | 来源 |
|---|---|---|
| 表数 | 20494 | `ALL_TAB_COLUMNS` 直查 |
| 列数 | 301121 | `ALL_TAB_COLUMNS` 直查 |
| 注释（表/列） | 20535 / 301806 | `ALL_TAB_COMMENTS` / `ALL_COL_COMMENTS` 直查 |
| 内省耗时（分批，batch=100） | **未测**（实测到一半被中止，见 §11） | — |
| JSON 体积 | ≈ 2.6–4.7 MB（外推，比例见 §6） | 样本外推 |

## 8. 结论

1. **报错不是 bug，是护栏正常工作**：它挡住的是「把 2 万张表全量塞进 schema_cache」。
2. **但真正的根因不在上限**：数据源 11 名为「LQ- Supply」，内容却是一套 AI 平台的库，
   与本系统的采购本体**零交集**。即便把上限调到 25000、缓存成功，
   **业务价值仍是 0**（本体里没有它的类 ⇒ 问不出东西；缓存只多一行 8 MB）。
3. **「调大上限」是治标**；「owner 范围」这条提示在本数据源上无效（没有更小的 owner）。

## 9. 选项（按推荐度）

| 选项 | 做法 | 代价 | 评价 |
|---|---|---|---|
| **A. 先确认指向** | 与业务确认：11 指的是不是「LQ-Supply」那个库？若指错 ⇒ 改指向/停用 | 分钟级 | ✅ **先做这个** |
| **B. 窄采集（真修）** | 新增采集模式：**全量表名 + 仅本体引用表的列/注释**；导入向导改为按表名**点查**列 | 中等（service+API+前端+守卫+集成测试） | ✅ 若确定要接入这套 AI 平台，走这条 |
| **C. 调 `SCHEMA_MAX_TABLES`** | env 调到 ≥25000 + 重启 | 8 MB 缓存 + 长耗时 + 8 MB 响应 + **全局护栏放宽** | ⚠️ 只有在「就是要全量字典」时才用 |
| **D. 什么都不做** | 数据源 11 保持不可用 | 零 | ✅ 现状等价（它本来就问不出东西） |

### 选项 C 的附带项（若真要调）
- `SCHEMA_MAX_TABLES` **必须写进 `backend/.env.example`**（现在没有 ⇒ 旋钮不可发现）；
- 建议**改成按数据源生效**（如 `data_source` 表加一列，或按 owner 粒度），
  而不是全局 env —— 全局 env 会把 2 万张表的上限传染给所有源；
- 上限放大后 `introspectBatched` 的 205 批 × 5 查询需要**异步 job**路径
  （同步接口会超时）。

## 10. 尚未确认（需要业务/运维输入）

1. 数据源 11 到底指向哪个库？「LQ- Supply」是**供应链库**还是**供应商的 AI 平台库**？
2. 这个源在系统里**打算做什么用**？（问数 ⇒ 需要先建本体；只做字典 ⇒ 不必缓存）
3. 生产环境是否也有这个数据源？（生产报错 ⇒ 需要同口径排查）

## 11. 附：本分析的复现脚本（不入仓）

- `/tmp/probe2.py` / `probe3.py` / `probe4.py` / `probe5.py`：owner 分布、表数口径、
  本体交集、表名样例
- `/tmp/measure_ds11.py`：用仓内 `SchemaIntrospectionService.introspectBatched`
  跑真实内省，量耗时与体积（临时把 `schemaMaxTables` 抬到 30000，**不改仓内文件**）
  —— **该次测量被中止**（业务侧已知原因，分析不再继续）⇒ 耗时数字缺失，
  体积改为按同类 Oracle 样本外推（§6）

## 12. 状态

**本文是「记录」，不是「方案」**：2026-11-27 业务侧已自行定位原因，分析到此为止。
分支 `fix/schema-cache-table-limit` **只含本文档**，无产品代码改动。
