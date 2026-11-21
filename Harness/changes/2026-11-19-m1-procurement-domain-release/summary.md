# 变更：2026-11-19-m1-procurement-domain-release（M1-1.3 采购域发布编排）

> 状态：进行中（分两批提交：**A = manifest v2（已完成）**；**B = 域编排 + 回滚 + 采购域校验项（已完成，见 §6.6）**）
> 分支：`feat/procurement-m0`　**未合并 / 未部署**（生产暂不部署，待 M1 全完成 + 本地全绿统一发版）
> 承接：[[M1-1.2 发布校验落表]]（`bc0cac0e`）、[[M1-1.1 采购源绑定行为层]]（`79a2a9da`）

---

## 1. 需求

计划 §1.3：「**采购域发布编排**：`domain_code='PROCUREMENT'` 的编译/校验/激活/回滚；
**复用** `/api/v1/semantic/releases`；manifest 纳入源绑定与采购指标」（2–3 人日）。

同时交付 **M1-a DoD 第 1/3 条**：

1. 管理台可为采购域完成：编辑 → 编译 → **validate 全 PASS 且每条 check 落表可查** →
   activate → 回滚 → 再 activate，**往返后 manifestHash 一致**。
2. 同一输入两次编译**字节一致**。
3. 发布失败场景可定位：构造一条**缺 JOIN 的绑定** → validate 报 ERROR →
   check 表可查到该条 → **不得**激活。

**2026-11-19 用户拍板的三条岔路**（AskUserQuestion）：

| 决策 | 内容 | 影响 |
|---|---|---|
| **D-9** | **本批就把采购源绑定纳入 manifest** ⇒ `SCHEMA_VERSION` v1 → **v2** | 需版本分发 + 迁移改 `schema_version` 默认值 + 旧 v1 ACTIVE 快照按既有设计降级直读 |
| **D-10** | 采购域的**类范围**复用既有 `sliceManifestByDomain` 口径（BO 主/附域锚点 + 父类闭包 + 指标锚定类）+ **引用闭合补齐** | 不自造口径；与知识侧切片同源 |
| **D-11** | **回滚 = 新建副本式**（以历史版本为源新建 DRAFT → VALIDATED → ACTIVE），ARCHIVED 仍是终态 | 天然满足「往返后 manifestHash 一致」；与 `semantic_release_service` 模块文档串已写明的设计一致 |

---

## 2. 设计评审

### 2.1 候选：源绑定怎么进 manifest

| 方案 | 结论 |
|---|---|
| **A. 不进 manifest**，绑定只在 validate 时实时查库做校验 | 改动面最小，但**发布快照不自含绑定信息** ⇒ 违反计划 §1.3 与 M1-b DoD 7；且「已发布的采购语义」无法离线复算 |
| **B. 折进 v1 现有字段**（`class.sourceTable` / `property.sourceColumn`） | **丢真**：一表多绑定、过滤表达式、变换表达式、键序号、schema/数据源都无处放 ⇒ 快照失真 |
| **C. 新增 `sourceBindings` 分区 + `SCHEMA_VERSION` v2**（采纳） | 契约显式升版；v1 快照按自身版本校验；绑定信息自包含且可复算 |

### 2.2 关键取舍 1：`SCHEMA_VERSION` v1 → v2 的连带面

| 面 | 影响 | 处置 |
|---|---|---|
| `semantic_release.schema_version` 的 `server_default` | 迁移 0142 写死 `'v1'`；ORM 与 DB 默认值必须同步（drift 守卫会报） | **迁移 0156** 改默认值为 `'v2'` + 改 ORM 声明（单测 `test_release_orm_default_schema_version_matches` 钉住两者） |
| 库中既有 v1 ACTIVE 快照 | `semantic_schema_adapter.schemaVersionMismatch` 会判「版本漂移」⇒ **降级直读治理表** | **既有设计**（宁可慢一点也不能少给事实），已告警；本批不部署 ⇒ 生产无感 |
| `validateManifest` | 单一 Schema 变成多版本 | 引入 `MANIFEST_SCHEMAS` 注册表 + **按 `manifest.schemaVersion` 分发**；未知版本 ⇒ `ManifestSchemaError`（不是放行） |
| 校验项条数上限 | 绑定级校验项多出 ⇒ 上限公式要**重新从契约推导** | 见 §5.2（`6 + 3×指标上限 + 2×绑定上限`） |
| `semantic_diff_service` | 只比 classes/properties/joins ⇒ 新分区**不参与** diff | **批次 B 已处置**：`sourceBindings` 纳入 diff（`COVERED_SECTIONS`），字段级覆盖过滤表达式 / 白名单 / 属性源列 / 变换 / 键序号 |

### 2.3 关键取舍 2：什么进 manifest，什么不进

| 进（`sourceBindings` 分区） | 不进 | 理由 |
|---|---|---|
| `className` / `datasourceName` / `schemaName` / `sourceTable` / `bindingRole` | 自增 `id` | 字节确定性铁律（`measure.class_id` / `kpis.datasourceId` 已各犯过一次） |
| `filterExpression` / `filterAllowlist` | `validFrom` / `validTo` | 时间戳进快照 ⇒ **两次编译字节不同** ⇒ 直接破 DoD 第 2 条 |
| 每个属性绑定的 `propertyName` / `sourceColumn` / `keyOrdinal` / `transformExpression` | `isCandidate = TRUE` 的**候选**绑定 | **D-3 延伸**：候选不是结论，不得被读路径消费；manifest 就是读路径的数据源 |
| — | `createdBy` / `confirmedBy` / `confirmedTime` | 实例相关残留列，与「换一个环境必须得到同样字节」冲突 |

⇒ **有效期与候选态不进快照**：快照表达「发布这一刻已确认且生效的绑定集合」，
时间窗与候选态留在绑定表（M1-1.1 的 `/effective` 读视图），并在发布期以
**发布级汇总校验行**（WARNING）显式暴露「还有 N 条候选绑定」。

### 2.4 关键取舍 3：采购域发布快照怎么保证契约成立

`sliceManifestByDomain`（既有纯函数，知识侧 P4 在用）切出的切片**可能不满足
Manifest 引用契约**：切片内的指标若引用了切片外的类（分子/分母/数据源类），
v1/v2 的引用校验会报「不存在」⇒ 快照**发不出去**。

三个候选：

| 方案 | 结论 |
|---|---|
| A. 剔除引用越界的指标 | 静默丢指标 ⇒ 采购域发布会悄悄少掉 KPI |
| B. **引用闭合补齐**（采纳）：把被引用类及其父类闭包补进切片，并记一条 WARNING | 契约成立 + 事实不丢；跨域依赖显式暴露 |
| C. 放宽引用校验 | 破契约，不可接受 |

⇒ **D-10 落地口径**：`切片 → 闭合补齐 → domains 分区取「该域 + 被引用到的附域」`。

### 2.5 关键取舍 4：回滚的形态（D-11）

| 方案 | 结论 |
|---|---|
| **A. 新建副本式**（采纳）：以目标历史版本为源新建 DRAFT → VALIDATED → ACTIVE；当前 ACTIVE 被归档 | ARCHIVED 保持终态；审计里「曾经下线」语义不被破坏；新副本 `manifest_hash` 与被回滚版本**逐字相同** ⇒ 天然满足 DoD 第 1 条 |
| B. 状态回退式（ARCHIVED → ACTIVE） | 历史行被「复活」；与 `ck_semantic_release_status` / 状态机文档串冲突 |

副本的 `version` 由调用方显式给出（不复用旧版本号，避免与「版本=内容指纹」的直觉冲突；
域内版本号本来就不唯一：`uq_semantic_release_global_version` 只对 `domain_code IS NULL` 生效）。

---

## 3. 数据模型变更

### 3.1 迁移 0156（`backend/alembic/versions/0156_semantic_manifest_v2.py`）

纯 ALTER，不建表：

```sql
ALTER TABLE semantic_release ALTER COLUMN schema_version SET DEFAULT 'v2';
```

> 迁移号说明：0154（M1-1.1 两张绑定表）→ 0155（M1-1.2 check 表）→ **0156（本批）**
> ⇒ **A4-0（审批面放开 `instance_id`）取 0157**。

### 3.2 Manifest v2 契约（`app/domain/semantic_manifest_schema.py`）

`MANIFEST_SCHEMA`（v1）**原样保留**，新增 `MANIFEST_SCHEMA_V2` = v1 + 新分区：

```jsonc
"sourceBindings": [{
  "className": "DWD_PURCHASE_ORDER_DTL",   // 必填
  "datasourceName": "THBI",                  // 可空（按名字，不含 id）
  "schemaName": "ERP",                       // 可空
  "sourceTable": "PORDER",                   // 必填
  "bindingRole": "BASE",                     // BASE/NUMERATOR/DENOMINATOR/FILTER
  "filterExpression": null,
  "filterAllowlist": [],
  "properties": [{
    "propertyName": "PO_NO",
    "sourceColumn": "PO_NO",
    "keyOrdinal": 1,                          // 可空
    "transformExpression": null
  }]
}]
```

**引用闸（v2 追加）**：绑定 `className` 必须存在于 `classes`；绑定的 `propertyName`
必须存在于该类属性；同一绑定的 `keyOrdinal` 不得重复；同一
`(className, datasourceName, schemaName, sourceTable, bindingRole)` 不得重复。

**契约上限**：`MAX_MANIFEST_SOURCE_BINDINGS = 2000`、
`MAX_MANIFEST_BINDING_PROPERTIES = 500`（两者都从 v2 Schema 导出，供 §5.2 推导）。

---

## 4. 接口契约变更

**复用** `/api/v1/semantic/releases`，只加一条路由：

| 路由 | 说明 |
|---|---|
| `POST /api/v1/semantic/releases` | **既有**；`domain_code='PROCUREMENT'` ⇒ 编译**域切片**快照（不再是全量） |
| `POST /api/v1/semantic/releases/{id}/rollback` | **新增**；body `{version, note?}`；语义见 §2.5 |

响应体沿用 `SemanticReleaseOperationRead`（`release` + `blockers` + `warnings`），
校验结论仍从 `GET /{releaseId}/checks` 取（分页 + 全量 summary）。

**连带的一处契约扩大（不是本特性路由，但是批次 B 的直接后果）**：
`GET /api/v1/ontology-discovery/diff` 的 `coveredFields` 响应**多出 `sourceBindings` 分区**
（diff 引擎把绑定分区纳入覆盖）⇒ 消费方若对「披露分区集合」做了相等断言，需要同步。
方向是**扩大披露**（多披露安全：让消费方能自己判断「比了哪些字段」）。

---

## 5. 实现要点

### 5.1 校验项目录（v2）

| code | 目标 | severity | 判据 |
|---|---|---|---|
| `SNAPSHOT_INTEGRITY` | RELEASE | ERROR | 快照规范化 / 体量 / 哈希一致 |
| `MANIFEST_SCHEMA` | RELEASE | ERROR | 满足 **v2** 契约 |
| `CHECKS_TRUNCATED` | RELEASE | ERROR | 结论条数超上限（报告不完整 ⇒ 阻断） |
| `DOMAIN_SLICE_EMPTY` | RELEASE | ERROR | 域切片为空（不能发布空采购快照） |
| `DOMAIN_SLICE_CROSS_DOMAIN` | RELEASE | WARNING | 为满足引用契约补齐了跨域依赖类 |
| `SOURCE_BINDING_CANDIDATE` | RELEASE | WARNING | 仍有未确认（候选）绑定（D-3 治理债） |
| `METRIC_SOURCE_PRESENT` | METRIC | WARNING | 指标有源类引用 |
| `METRIC_NUMERATOR_LANDING` | METRIC | WARNING/ERROR | 分子度量有落点类 |
| `METRIC_DENOMINATOR_LANDING` | METRIC | WARNING/ERROR | 分母度量有落点类（RATIO/DIFFERENCE） |
| `SOURCE_BINDING_JOIN_MISSING` | BINDING | **ERROR** | 绑定所属类在**整个本体**里零 JOIN 边（孤岛）⇒ 发布出去也走不通 |
| `SOURCE_BINDING_PROPERTY_UNBOUND` | BINDING | WARNING | 类已绑源表，但有属性没有 `sourceColumn` 绑定 |

- **「孤岛」判据取全本体口径，不取切片口径**：否则「只连跨域边」的类会被误判成孤岛
  ⇒ 误阻断合法发布。切片内的跨域边差异另由 `DOMAIN_SLICE_CROSS_DOMAIN` 提示。
- `targetKey` 用 `类名@数据源.schema.表#角色` 复合键 ⇒ 同一发布同一校验项同一绑定只留一行
  （对上 `uq_semantic_release_check_unique`）。
  ⚠️ **必须带角色与数据源**（批次 B 实施期自查发现）：一个类可以绑到同一张表的多个角色
  （BASE / NUMERATOR …），也可以在**两个数据源**里各绑一次；少一个维度 ⇒ 两行撞唯一索引
  ⇒ 整次发布 500。
  ⚠️ 列宽是 `String(200)`：超宽时改用「类名摘要(64) + 完整身份 sha256[:16]」，
  **不截断**（截断会让两个不同的绑定撞成同一个键）。

### 5.2 上限必须从契约推导（v2）

> ⚠️ 本节属**批次 B**：批次 A 只把**契约上限常量**加上
> （`MAX_MANIFEST_SOURCE_BINDINGS = 2000`、`MAX_MANIFEST_BINDING_PROPERTIES = 500`），
> 校验项侧仍是 M1-1.2 的 `2 + 3 × 2000 = 6002`（发布级仍只有 2 条：
> 快照完整性 + Manifest 契约）。批次 B 加 4 条发布级 + 2 条绑定级时**必须**重推为下式，
> 否则「合法 manifest 也被截断」。

```
MAX_RELEASE_LEVEL_CHECKS  = 6                       # 发布级（代码常量，见 §5.1）
MAX_CHECKS_PER_RELEASE    = 6
                          + 3 × MAX_MANIFEST_METRICS        # 每指标最多 3 条
                          + 2 × MAX_MANIFEST_SOURCE_BINDINGS # 每绑定最多 2 条
                          = 6 + 6000 + 4000 = 10006
```

⚠️ 绑定级校验项**不**按候选数计（候选不进快照 ⇒ 数量无契约上限）⇒
候选只出**一条发布级汇总行**（detail 带总数 + 前 5 个类名），从根上避免再次出现
「合法输入被静默截断」（M1-1.2 的 off-by-2 教训）。

### 5.3 闸门与落表零漂移（沿用 M1-1.2）

`checksToGateResult(evaluateReleaseChecks(...))` == `evaluatePublishGate(...)`；
闸门与落表用**同一个集合**（`capChecks` 之后的那一份）。

### 5.4 证据先行（沿用 M1-1.2）

校验失败 ⇒ **先提交 check 行 + blocked 审计，再抛原异常**；
`markValidated` / `activate` / `rollback` 三个入口一致。

---

## 6. 批次 A 实施记录（manifest v2）

批次 A = §3.2 的契约 + 编译器绑定分区 + 迁移 0156 + golden 与下游测试面；
批次 B = §2.4 的域切片/引用闭合 + §5.1 的采购域校验项 + §2.5 的回滚。

### 6.1 落点

| 关注点 | 落点 |
|---|---|
| 多版本 Schema 注册表 + 版本分发 | `app/domain/semantic_manifest_schema.py`：`MANIFEST_SCHEMAS` / `_deriveV2` / `validateManifest` 按 `manifest.schemaVersion` 取 Schema；**未知版本直接拒绝** |
| 版本字面量（**叶子模块**） | `app/domain/semantic_manifest_version.py`（零 import）：ORM 与契约两侧共用同一份版本真值，且**不形成 import 环** |
| 绑定分区编译 | `app/services/semantic_compiler_bindings.py`（独立文件：口径独立 —— D-3、有效期、名字化引用） |
| 编译器接线 | `app/services/semantic_compiler.py`：`compileFromRows` 增 `sourceBindings` / `sourceBindingProperties` 两个入参，输出增 `"sourceBindings"` 分区 |
| 落库默认值 | `app/domain/semantic_release_models.py` 的 `schema_version` 改用 `server_default=SCHEMA_VERSION`（不再写死 `'v1'`） |
| 迁移 | `backend/alembic/versions/0156_semantic_manifest_v2.py`（纯 ALTER 默认值；downgrade 回 `'v1'`） |
| diff 披露 | `app/services/semantic_diff_service.py` 的 `MANIFEST_SECTIONS` 增 `sourceBindings` ⇒ 自动落进 `UNCOVERED_SECTIONS`（API 契约里显式披露「未覆盖」） |
| 校验项文案 | `app/services/semantic_release_checks.py`：PASS detail 写**实际版本**（`Manifest 满足 {version} 契约`），不再写死 v1 |

### 6.2 六个必须避开的坑（实施期实际踩到的）

1. **版本分发是连锁面，不是改一行常量**：ORM `server_default`、DB 默认值、
   adapter 的降级闸、校验项上限、diff 披露**五处**都要跟着处置；
   只改 `SCHEMA_VERSION` ⇒ 迁移与 ORM 不一致（drift 守卫报）、
   夹具行版本与正文版本不一致（被判漂移）。
2. **快照里不许出现实例相关列**：`valid_from` / `valid_to` / `confirmed_time` /
   `confirmed_by` / 自增 `id` 一律不进 ⇒ 同一治理内容两次编译字节一致
   （`measure.class_id` / `kpis.datasourceId` 已各犯过一次，这是第三次的机会成本）。
3. **候选 ≠ 结论（D-3）**：只收 `is_candidate = False` **且** `valid_to IS NULL` 的绑定；
   墓碑类（`valid_to` 非空）及其属性整体丢弃 —— **丢弃不是报错**：
   墓碑类上的绑定是「历史」，报错会把一次正常发布挡死。
4. **引用闸的「空属性类」漏洞**：判定属性是否属于该类时用
   `allowed is not None` 而不是 `and allowed` —— 后者让「空属性集合」
   被当成「不检查」⇒ 张冠李戴的属性名溜过去。
5. **夹具的版本必须跟 DB 默认值一致**：测试夹具写 v1 形状 + 行 `schema_version`
   取 DB 默认 v2 ⇒ 行与正文版本不一致 ⇒ 被 `schemaVersionMismatch` 判成漂移
   （看起来像「发布后快照读不出来」，其实是夹具写错）。
6. **硬编码 `"v1"` 的下游夹具**：批次 A 扫了全仓 `schemaVersion: "v1"`，
   逐条判定「是当前版本」还是「故意造旧版本」，前者改 `SCHEMA_VERSION` 动态取值、
   后者保留并写清意图（见 §6.3）。

### 6.3 旧版本夹具的判定口径

| 夹具 | 判定 | 处置 |
|---|---|---|
| `app/tests/_knowledge_manifest.py` 的 `sampleManifest()` | 「当前契约样本」（被知识侧四路 + 契约闸共用） | 改 `SCHEMA_VERSION` + 补 `sourceBindings: []`（v2 必填段） |
| `test_semantic_schema_gate.py` 的畸形快照 | **假绿风险**：写死 v1 ⇒ 版本闸先判漂移 ⇒ 「畸形」分支一次都跑不到 | 改 `SCHEMA_VERSION`（让畸形成为唯一触发原因） |
| `test_semantic_release_checks.py` / `test_semantic_release_service.py` 的 `_manifest()` | 残缺 manifest（只有 metrics 段），版本值对指标层校验项无影响 | 改 `SCHEMA_VERSION`（避免下次升版再集体排查） |
| `test_semantic_registry_service.py`（unit + integration）的默认快照 | 「一条普通快照行」 | 改 `SCHEMA_VERSION` |
| integration 的「v1 旧版本快照原样回传」用例 | **故意造旧版本**（证明 Registry 不替它盖当前版本） | 保留 `"v1"`，独立常量 `_V1_MINIMAL_MANIFEST` + 注释写清意图 |
| `SchemaCache.schema_version` / 发现快照的 `schemaVersion` / `DataDomain.manifest_json` | **别的版本命名空间**（数据源 schema 缓存 / 发现快照结构版本 / 域的任意 JSON） | 不动 |

### 6.4 批次 A 已知缺口（批次 B 或更晚处理）

- **本批只「入档」，不接读路径**：`sourceBindings` 分区已进快照，
  但 schema 渲染（`buildSchemaText` / `restoreSnapshotSchema`）仍只消费
  `classes[].sourceTable` 这条**旧的单绑定** ⇒ 采购绑定目前是**归档事实**，
  不是已生效的运行时口径。谁把分区接进渲染（多表/多 schema/过滤表达式怎么表达）
  属于后续里程碑，**不得在本批顺手接**（渲染口径变化会牵动 NL2SQL 提示词与影子比对基线）。
- **v1 存量快照会被判漂移**：`semantic_schema_adapter.schemaVersionMismatch`
  只认当前版本 ⇒ 库里还挂着的 v1 ACTIVE 快照会**降级直读治理表**
  （既有设计：宁可慢一点也不能少给事实）。⇒ **发版顺序见 §9**（原先写的
  「先产 v2 快照再切代码」**不可执行**，见 §6.5 的 F3）。
- ~~**`semantic_diff_service` 未覆盖 `sourceBindings`**~~：**批次 B 已处置**
  ⇒ `sourceBindings` 进入 `COVERED_SECTIONS`（字段级：过滤表达式 / 白名单 /
  属性源列 / 变换表达式 / 键序号），「发布后绑定变了」在 diff 视图里可见。
- ~~**校验项仍只有 6 条**（2 发布级 + 3 指标级）~~：**批次 B 已处置**
  ⇒ 校验项目录扩到 **11 条**（§5.1），上限重推为 `10006`（§5.2）。
- **同一类可有多条 BASE 绑定**（多落点是合法设计）⇒ 契约**不收紧**，
  但**读路径必须消歧**（拿到多条 BASE 时按什么取）—— 读路径接线时必须显式定口径，
  否则「这条类指哪张表」是未定义行为。登记在此，避免接线时漏掉。

### 6.5 两轮审查的处置（code-reviewer + security-reviewer）

结论：**批次 A 可合并**，但两轮审查各点了若干处；下面逐条落处置。

#### 已修（本批内）

| # | 来源 | 问题 | 处置 |
|---|---|---|---|
| **HIGH-1** | code-review | v2 Schema 与注册表键都挂在**可变常量** `SCHEMA_VERSION` 上 ⇒ 下次升版（v3）会把 v2 从注册表里抹掉、v2 契约被改写成 v3 ⇒ 已发布的 v2 快照当场失效 | 版本字面量**冻结**：新增叶子模块 `app/domain/semantic_manifest_version.py`（`SCHEMA_VERSION_V1/V2` + `SCHEMA_VERSION`）；注册表按**字面量**登记；`_deriveV2` 的 const 用 `SCHEMA_VERSION_V2`；新增 `_assertCurrentVersionRegistered()`（**import 期**就炸）；单测钉死「每一版自述自己的版本」 |
| **MEDIUM-2** | code-review | 引用闸用「版本 ≠ v1」判断要不要查 `sourceBindings` ⇒ 未来任何不带该分区的版本会 **KeyError ⇒ 500** | 「哪一版含绑定分区」改成**版本配置** `MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS`；读取用 `manifest.get("sourceBindings") or []` |
| **LOW-1** | code-review | 编译器不校验「属性绑定的属性属于该绑定所属的类」⇒ 错配要到 validate 阶段才拦 ⇒ **一个坏行阻断所有域的发布**，且报错指向「第 N 条属性」而不是哪条绑定 | 编译期在 `compileSourceBindings` 里按 `binding.id` 抛 `SourceBindingCompileError`（带绑定 id 与类 id）；库侧没有这个约束（FK 只指向属性与绑定）⇒ 绕过写路径就能造出来 |
| **LOW-2** | code-review | 绑定内属性排序键无并列兜底 ⇒ 理论上字节不确定（库里属性名无唯一约束） | 排序键覆盖**全部出参字段**（`propertyName` / `sourceColumn` / `keyOrdinal` / `transformExpression`）⇒ 并列时两条内容相同 ⇒ 顺序不影响字节；**不用 id 兜底**（id 跨环境不同 ⇒ 会破坏字节一致性）。另在引用闸加「同一绑定内 `propertyName` 不得重复」 |
| **LOW-3** | code-review | `_sourceBindingSectionErrors` 65 行 > 项目「函数 < 50 行」 | 拆成 `_sourceBindingSectionErrors` + `_propertyNamesByClass` + `_bindingIdentityErrors` + `_bindingPropertyErrors` |
| **LOW-4** | code-review | ORM 层新增了一条指向 schema/service 层的导入边 ⇒ 形成**结构上**的 import 环，只靠导入顺序侥幸成立 | `SCHEMA_VERSION` 移到**零 import 的叶子模块** `app/domain/semantic_manifest_version.py`；ORM 与契约两侧都从那里取。实测：`import app.domain.models` 不再连带加载 `app.domain.schemas` / `jsonschema` / 三个 service registry（app 模块数 33 ⇒ 23） |
| **F1** | security | 表达式安全只在**入口**校验 ⇒ 绕过写路径入库的行会把越权表达式带进快照 | **编译期复核**：过滤表达式复用 `validateFilterExpression(expr, allowlist)`、列级变换复用 `validateTransformExpression(expr, sourceColumn)`（都是纯函数）⇒ 表达式安全从「入口校验」升级为**发布不变量** |
| **F5** | security | `not row.is_candidate` 在字段为 `None` 时 **fail-open** | 改成 `row.is_candidate is False`（未知 ⇒ 不发布）；另加「`valid_from` 不得晚于此刻」（未来才生效的绑定**还没生效** ⇒ 不进快照；库侧 `valid_from` 是 `now()` 默认值，这条只防绕过写路径手写未来时间） |
| **F6** | security | jsonschema 的错误消息**内嵌被拒值**（可达数十 KB）⇒ 落进 `semantic_release_check.detail` | 单条错误消息截断到 500 字符（`_truncateMessage`）+ 单测钉死 |
| **F7** | security | 「迁移字面量 == SCHEMA_VERSION」式测试**不可写**（迁移必须冻结） | 改成**集成测试断言 DB 实际默认值**：`information_schema.columns.column_default` == 当前版本（并进既有 integration 文件，不新增文件以免打乱 CI 分片） |

#### 不改行为（登记理由）

| # | 来源 | 问题 | 结论 |
|---|---|---|---|
| **F2** | security | 同一 `className` 可有多条 BASE 绑定 | **合法设计**（多落点）⇒ 不收紧契约；登记「读路径必须消歧」（见 §6.4） |
| **F3** | security | `schemaVersionMismatch` 与 `MANIFEST_SCHEMAS` 的不对称 | **有意**：校验问「按自身版本是否合法」，消费问「当前渲染器能否忠实读出」⇒ 两者答案本就可以不同。保留 runbook 的发版顺序（已按 MEDIUM-3 更正，见 §9） |
| **F4** | security | `/effective` 两个端点（仅登录）对全员暴露物理表/列/表达式 | M1-1.1 既有面，本批未新增 ⇒ **批次 B 决策**「发布后绑定对谁可见」 |

#### 批次 B 的前置提醒（来自 code-review）

- **`DomainSlice` 必须扩展**：现有 `app/services/knowledge/domain_slice.py` 的
  `DomainSlice` 只带 `classes/joins/businessObjects/metrics/kpis` ——
  **既没有 `domains`，也没有 `sourceBindings`** ⇒ v2 的 `sourceBindings` 是必填段
  （实测缺了就报 `'sourceBindings' is a required property`）⇒ 不扩这个结构，
  采购域发布会永远发不出去。
- 校验项上限重推为 `10006`（§5.2）+ 绑定纳入 diff 引擎。
- 绑定级校验项应把「属性归属错配 / 表达式不合规」**定位到具体绑定**
  （本批的编译期异常是 fail-closed 抛错，会阻断整次发布 —— 能定位到绑定 id，
  但没有落表成 check 行）。

### 6.6 批次 B 实施记录（域编排 + 回滚）

#### 落点

| 文件 | 性质 | 职责 |
|---|---|---|
| `app/services/semantic_release_check_types.py` | **新增**（叶子） | `PublishCheck` / `PublishGateResult` 与 severity / result / target 常量；**零业务依赖**；另含**公共绑定键渲染器** `bindingTargetKey()`（校验侧与差异侧共用，见 §6.7 LOW-1） |
| `app/services/semantic_release_domain_checks.py` | **新增** | 域级（切片为空 / 跨域补齐）+ 绑定级（孤岛 / 属性未绑列）+ 候选汇总行；纯函数 |
| `app/services/procurement_binding_candidates.py` | **新增** | 活库里**仍然开放**的候选绑定计数与类名（候选不进快照 ⇒ 只能实时查） |
| `app/services/semantic_domain_slice.py` | **新增** | 发布切片（`sliceManifestForRelease` / `sliceToManifest`）；`app/services/knowledge/domain_slice.py` 改为**转发层**（保持旧 import 路径可用） |
| `app/services/semantic_release_checks.py` | 改造 | 新增 5 个 code / 5 个 item / `TARGET_BINDING`；上限重推 `10006`；`evaluateManifestChecks(..., context=)` |
| `app/services/semantic_release_support.py` | **新增**（支撑层） | 输入校验 / 状态迁移判定之外的落库辅助（校验项评估、审计留痕、撞库翻译）；从 `semantic_release_service` **整块搬出**（纯搬移，无逻辑改动） |
| `app/services/semantic_release_service.py` | 改造 | 域切片接线（`createDraft` 存切片）+ `context` 传递 + **`rollback()`** |
| `app/services/semantic_diff_service.py` | 改造 | `sourceBindings` 纳入 diff（F9）；绑定身份补 `datasourceName` 维度 |
| `app/domain/semantic_release_schemas.py` / `app/api/v1/semantic_releases.py` / `app/services/messages_zh.py` | 改造 | 回滚请求体 / 路由 / 中文文案 |
| `alembic/versions/0157_semantic_release_domain_version.py` | **新增**（迁移） | `uq_semantic_release_domain_version (version, domain_code) WHERE domain_code IS NOT NULL` —— 域作用域版本号唯一（§6.7 MEDIUM-3） |

#### 四个关键设计

1. **三层模块拆分（避免 import 环）**
   域级模块要 import `PublishCheck`，而 checks 模块又要 import 域级模块
   ⇒ 类型必须住**叶子模块**（`semantic_release_check_types`），否则形成结构上的环
   （批次 A 的 LOW-4 同类问题）。

2. **JOIN 邻居闭包取不动点 ⇒ 切片幂等 ⇒ 校验项可复算**
   发布快照存的是**切片**，而「跨域补齐了什么」**不在**切片里 ⇒ 若只在编译那一刻算一次，
   validate / activate 阶段就复算不出来（证据不可复算）。
   解法：**闭合是幂等的** —— 对切片再切一次得到同一份 ⇒ 校验项只依赖**已存快照**。
   不动点保证「类在、边不在」的自相矛盾类不会出现（邻居若自己也有绑定，它的邻居同样要补）。

3. **候选数用 `None` 语义**
   `ReleaseCheckContext.candidateBindingCount: int | None`：
   `None` = **没查过** ⇒ **不出这一行**（不写一句没依据的「没有候选绑定」）；
   `0` = 查过且为 0 ⇒ 出 PASS 行。

4. **孤岛判据取全本体口径**
   切片阶段把**有绑定类的一跳 JOIN 邻居**也拉进来 ⇒ 「切片里有没有边」与
   「本体里有没有边」**完全等价** ⇒ 不会误阻断「只连跨域边」的合法发布。

5. **编排层与支撑层分开（文件体量）**
   批次 B 落地后 `semantic_release_service.py` 涨到 **851 行** ⇒ 超过项目上限（800）。
   拆分口径：**编排层**（`semantic_release_service`：编译 → 切片 → 落库 → 状态迁移 → 审计）
   与**支撑层**（`semantic_release_support`：输入校验 / 校验项评估 / 审计留痕 / 撞库翻译）分开。
   ⚠️ 分层方向的坑：编排层 import 支撑层 ⇒ **`ReleaseEvaluation` 必须住支撑层**
   （否则支撑层要反向 import 编排层 ⇒ 环）；而状态机判定 `isTransitionAllowed`
   相反 —— 它**留在编排层**（支撑层不需要它）。
   拆分后（实测 `wc -l`）：编排层 **480** 行 / 支撑层 **619** 行（代码行 398 / 514）——
   两个文件都**未超 800 行上限**；拆分的**主体**是纯搬移，审查后补进了
   `blockedVersion` / `_buildRollbackCopy` / `_validateNote` 三个纯函数与
   域作用域版本号查重（见 §6.7）。

#### 实施期自查发现并修正的三处缺陷

| # | 缺陷 | 后果 | 处置 |
|---|---|---|---|
| **B-1** | 绑定级校验项的 `targetKey` 缺**数据源**维度 | 同一类同表在两个数据源各绑一次 ⇒ 两行撞 `uq_semantic_release_check_unique` ⇒ **整次发布 500** | `targetKey` 改成 `类名@数据源.schema.表#角色`（五元组）；diff 侧 `_bindingKey` 同步补 `datasourceName` |
| **B-2** | `context=None` 时也输出「没有候选绑定」PASS 行 | 写了一句**没依据的结论**（比缺一行更坏） | 改成 `candidateBindingCount: int | None`，`None` 时**不出行** |
| **B-3** | 超宽 `targetKey` 打算直接截断 | 截断会让两个不同的绑定撞成同一个键 | 改成「类名摘要(64) + 完整身份 sha256[:16]」，**不截断** |

#### 与批次 A 审查提醒的对应

- 「`DomainSlice` 必须扩展（带 `domains` / `sourceBindings`）」⇒ 已扩展
  （`app/services/semantic_domain_slice.py`，`domains` / `sourceBindings` / `closureClassNames` 三个新字段）。
- 「校验项上限重推为 `10006`」⇒ 已重推，并改由**契约常量推导**（单测钉死公式）。
- 「绑定纳入 diff 引擎（F9）」⇒ 已纳入：`sourceBindings` 从 `UNCOVERED_SECTIONS`
  移到 `COVERED_SECTIONS`，字段级覆盖 `filterExpression` / `filterAllowlist` /
  `properties[].sourceColumn` / `transformExpression` / `keyOrdinal`。
- 「属性归属错配 / 表达式不合规应定位到具体绑定」⇒ **部分处置**：属性**未绑列**
  已落表成 `SOURCE_BINDING_PROPERTY_UNBOUND`（WARNING，带绑定 `targetKey`）；
  「属性归属错配 / 表达式不合规」仍是**编译期 fail-closed 抛错**（批次 A 的 F1 / LOW-1），
  未落表成 check 行 —— 登记为后续缺口，不在本批扩。

---

### 6.7 批次 B 的两轮审查与处置（code-reviewer + security-reviewer）

批次 B 落地后请了 `code-reviewer` 与 `security-reviewer` 各一轮。结论：
**无 CRITICAL**；HIGH 与可修的 MEDIUM/LOW **全部处置完毕**，只有 4 条**明确
登记为设计决策 / 后续缺口**（不修，但写明理由）。

#### 处置表

| 级别 | 结论 | 处置 | 证据 |
|---|---|---|---|
| **HIGH** | **JOIN 邻居闭包与父类闭包不是联合不动点** ⇒ 带绑定的父类被判成**孤岛** ⇒ 合法发布被 ERROR 阻断 | 四条闭包来源（度量引用 / 属性引用 `refClassName` / 有绑定类的一跳 JOIN 邻居 / 父类闭包）合成**一个联合不动点**（`semantic_domain_slice._closure` 迭代到稳定 ⇒ 父类若自己也有绑定，它的 JOIN 邻居同样进来） | `test_semantic_domain_slice.py` 不动点 + 幂等用例；切片单测 **20 条**全绿 |
| **HIGH** | **属性引用没闭**（`properties[].refClassName`）⇒ v2 引用契约判「不存在」⇒ **该域永远发不出去** | `_propertyRefClassNames` 进闭包（表引用型字典是**常见建模**，不是假想场景） | 单测（属性引用闭包）+ 集成用例 |
| **HIGH** | `ManifestSchemaError` 是 `ValueError` 而非 `DomainError` ⇒ `main.py` 只注册 `DomainError` ⇒ 裸抛 ⇒ **500**（该 422 的地方 500） | 服务层把契约错误翻译成 `ValidationError`（**422**） | 集成用例（契约违例 ⇒ 422 而非 500） |
| **HIGH** | **diff 两侧不同口径**（基线 = 域 ACTIVE 行（切片），候选 = 全域编译结果）⇒ 两侧**不可比**，差异报告在说谎 | 候选侧按**同一个域**切一次（`sliceManifestToDomain`）；`scope` 如实披露 `DOMAIN` / `GLOBAL` | `test_diff_compares_the_domain_slice_on_both_sides` + `test_diff_of_a_global_release_reports_the_global_scope`；**变异反证**：把候选侧裁剪改成 `if False and ...` ⇒ 该用例红 ⇒ 非空转 |
| **MEDIUM-3** | **域作用域不做版本号查重** ⇒ 同域可存在重复版本号（ORM 注释原写「按域发布上线时再补索引」，那个时点到了） | ① 服务层 `_requireVersionFree` 在域作用域也查重；② 迁移 **0157** 建库侧唯一索引 `uq_semantic_release_domain_version (version, domain_code) WHERE domain_code IS NOT NULL`（查重是「先查后写」，并发下会同时通过 ⇒ 库侧索引才是兜底） | 集成用例「同域同版本 ⇒ 409；**跨域同版本 ⇒ 允许**」；0157 建索引前先探测存量重复（重复 ⇒ **报错不自动修**，重复版本号必须人工裁决） |
| **LOW-1** | 绑定 `targetKey` 的渲染器**校验侧与差异侧各写一份** ⇒ 迟早漂移 ⇒ 差异报告里的一个绑定名**查不到 check 表里对应的行** | 抽出**公共渲染器** `bindingTargetKey()` 住叶子模块 `semantic_release_check_types`；两侧都委托它 | 单测（超宽 ⇒ `类名摘要(64) + sha256[:16]`，**不截断**）；集成用例（差异侧与 check 表同一键） |
| **LOW-2** | `semantic_release_checks.py` 转出了 10 个**域级**常量 ⇒ 两处都能 import ⇒ 「迟早有一处改了另一处没改」 | 删除转出：域级常量**只在域级模块**定义；消费方（测试 / 报告渲染）直接 import 域级模块 | `ruff check` 全绿；域级单测的 import 改到叶子模块 |
| **LOW-5** | 回滚**允许以 ACTIVE 为来源** | ⛔ **不改**（设计决策）：回滚的语义是「以历史版本为源新建副本」（D-11）⇒ 源的**状态**不参与合法性判定，只参与「源不得为 DRAFT」这一条；以 ACTIVE 为源 = 复制当前线上版本再发一遍，是合法的「重发」动作 | 见 §2.5（D-11 副本式回滚） |

#### 本轮**自查**发现并修掉的一处真实不一致（非 reviewer 提出）

`markValidated` / `activate` 的**候选统计**此前**未按快照里的类收窄**（只有草稿阶段收窄了）
⇒ 同一份快照在草稿阶段说「0 个候选」、validate 阶段说「3 个候选」
⇒ **证据不可复算**（同一份快照两个口径）。
现两处都改为按 `_manifestClassNames(row.manifest_json)` 收窄 ⇒ 与草稿阶段**同一口径**。
证据：集成用例 `test_candidate_count_is_scoped_to_the_snapshot_classes`
（别域的候选 ⇒ 草稿行 PASS **且** validate 行仍 PASS）。

#### 登记为「不修」的四条（设计决策 / 后续缺口）

| 编号 | 内容 | 不修的理由 |
|---|---|---|
| **M-1** | 跨域补齐（`DOMAIN_SLICE_CROSS_DOMAIN`）只报 **WARNING**，不阻断 | 跨域依赖是**事实**（本体里就是这么连的），阻断会让合法发布发不出去；改为**显式暴露**（WARNING + 类名清单）⇒ 评审可见、可追责。⚠️ 代价：跨域依赖可以无声地扩大一个域的发布面 ⇒ 后续若要收紧，先在**治理侧**确认依赖是否可接受 |
| **L-3** | 属性**归属错配** / 表达式**不合规**没有落表成 check 行 | 两者都是**编译期 fail-closed 抛错**（批次 A 的 F1 / LOW-1）⇒ 根本到不了「校验项评估」这一步 ⇒ 落表无对象。要落表得先把「抛错」改成「收集」，那是**闸门口径改造**（超出本批范围，且会牵动 M1-1.2 的「闸门与落表零漂移」判据） |
| **L-4 / L-5** | 回滚副本的**版本号**在失败路径上用**合成版本号**（`请求版本#b<副本id>`）释放用户请求的版本号 | 副本已落库（证据先行：先提交 check 行 + blocked 审计，再抛原异常）⇒ 它占着用户请求的版本号 ⇒ 用户拿同一个版本号重试只会收到误导性的「版本已存在」。合成版本号受 `semantic_release.version` 列宽（32）约束 ⇒ 必须截断**前缀**而保留副本 id（`#b<id>`），否则两个副本会撞。`blockedVersion()` 是纯函数 ⇒ 单测覆盖（长度 / 保留 id / 区分不同 id） |
| **L-6** | `semantic_diff_service.py` 涨到 **794 行**（项目上限 800，只剩 6 行余量；HEAD 时 642） | 本批把 `sourceBindings` 纳入 diff（+152 行）⇒ 已逼近上限但**未越限**。拆分为 `semantic_diff_bindings.py`（绑定分区 diff）属于**纯搬移** ⇒ 与批次 B 的支撑层拆分同一手法；本批不再扩 ⇒ 登记为下一轮的第一件事。⚠️ 再往这个文件加一行就要先拆 |

---

---

## 7. 测试

### 7.1 批次 A（已落地）

- **unit**：
  - `test_semantic_manifest_schema.py`：注册表覆盖 v1/v2；v1 快照仍按 v1 通过；
    v1 携带 `sourceBindings` 被拒；未知版本拒绝；绑定类不存在 / 属性张冠李戴 /
    空属性类被拒 / `keyOrdinal` 重复 / 绑定主键重复（§5 条）；
    **新增**：注册表里每一版自述自己的版本（防 HIGH-1 的通用守卫）、
    v2 契约冻结（`$id` / `title` / const 不跟「当前版本」走）、
    「哪一版含绑定分区」是版本配置、同一绑定内 `propertyName` 重复被拒、
    结构错误消息截断（F6）。
  - `test_semantic_compiler_bindings.py`（新增 8 条）：已确认生效进快照、
    候选排除、已失效排除、墓碑类排除、墓碑属性排除、类不存在抛错、
    数据源不存在抛错、**输出顺序与输入顺序无关**（字节一致）；
    **审查后新增 7 条**：属性归属错配抛错（带绑定 id）、过滤表达式越权被拒、
    有表达式无白名单被拒、列级变换越权被拒、未来生效的绑定不进快照、
    未来生效的属性绑定不进快照、`is_candidate` 为未知（None）时不发布（fail-closed）。
  - `test_semantic_manifest_golden.py`：golden 更新 + `sourceBindings` 分区键集合的
    **反向守卫**（逐 section 断言非空）+ **禁实例列**守卫
    （`validFrom` / `validTo` / `confirmedBy` / `confirmedTime` / `isCandidate` /
    `createdBy` / `createdTime` / `updatedTime` / `classId` / `datasourceId` /
    `sourceBindingId` / `propertyId` 一个都不许出现）—— 禁键清单单点定义在
    `app/tests/unit/_semantic_rows.py::FORBIDDEN_MANIFEST_KEYS`（键名级守卫与
    文本级守卫共用 ⇒ 两处不会各漏各的）。
  - 下游面：diff 的未覆盖分区集合、adapter 的版本闸、shadow diff 的版本闸、
    release service / api 的行版本与正文版本一致。
- **integration**（真实 PG 5434 + 完整 HTTP 链路）：`test_semantic_release_service.py`
  与 `test_semantic_release_api.py` 的 `_MINIMAL_MANIFEST` 改 v2 形状
  （含 `sourceBindings: []`），行 `schema_version` 与正文 `schemaVersion` 同为 `SCHEMA_VERSION`；
  `test_semantic_registry_service.py` **新增**：DB 实际默认值
  （`information_schema.column_default`）== 当前契约版本（F7）。
- **迁移**：0156 已在 **5433 与 5434 双库**应用（`column_default = 'v2'::character varying`）；
  `DATABASE_URL=...5434/qa_metadata_test uv run alembic check` ⇒ **No new upgrade operations detected**。

### 7.2 批次 B（已落地）

- **unit**（新增三个文件 + 三个既有文件补断言）：
  - `test_semantic_domain_slice.py`（**新增**，**20** 条）：域内类 / 指标引用闭合补齐 /
    **属性引用闭合**（`properties[].refClassName`）/ JOIN 两端在内 / 有绑定类的邻居拉入 /
    **联合不动点**（A(绑定)—B—C(绑定) ⇒ C 必须进切片；**带绑定的父类 ⇒ 父类的邻居也要进**）/
    绑定分区过滤 / `domains` 分区 / 未知域空切片 / **幂等**（对切片再切一次同一份）/
    `sliceToManifest` 过契约 / 分区顺序稳定 / 可省略 `sourceBindings`。
  - `test_semantic_release_domain_checks.py`（**新增**，**16** 条）：空切片 ERROR /
    跨域补齐 WARNING / 无补齐 PASS / 全局发布无域行 / 候选 WARNING / 候选 0 ⇒ PASS /
    **没查过候选数 ⇒ 不出行** / 孤岛 ERROR / 有边 PASS / `targetKey` 含数据源与角色 /
    同表两数据源两键 / 属性未绑列 WARNING / 全绑 PASS / 无绑定无行 /
    **同一类多条绑定分摊映射 ⇒ 不算债**（假阳性防护）。
  - `test_semantic_release_support.py`（**新增**，**10** 条 —— 支撑层**纯函数**）：
    `blockedVersion()`（后缀 `#b<副本id>` / 落在列宽 32 内 / **保留 id** / 区分不同 id）/
    `_buildRollbackCopy()`（正文**逐字复制** / **深拷贝不共享对象** / 新版本号与 note）/
    `_validateNote()`（2001 拒绝 / 2000 接受 / None 接受）。
    ⚠️ 支撑层此前**零单测**（纯搬移）⇒ 本批补上 ⇒ 阻断回滚路径（当前设计下几乎不可达）
    以**纯函数**覆盖，而不是硬造一条集成路径。
  - `test_semantic_diff_service.py`（+14 条）：绑定新增（ADDITIVE）/ 删除（BREAKING）/
    过滤表达式变更（BREAKING）/ 白名单变更（BREAKING）/ 属性源列变更（BREAKING）/
    变换表达式变更（BREAKING）/ 键序号变更（COSMETIC）/ 属性新增（ADDITIVE）/
    属性删除（BREAKING）/ 同表两数据源不挤成一条 / 同表两角色不挤成一条 / 入参不被就地修改。
  - `test_semantic_release_checks.py`：上限断言改为**从契约推导**（`6 + 3×指标 + 2×绑定`）。
  - `test_ontology_discovery_api.py`（**契约面连带**）：`sourceBindings` 进入
    `COVERED_SECTIONS` ⇒ 发现接口的 `coveredFields` 披露**多一个分区** ⇒
    原断言 `== {classes, properties, joins}` 必然红 ⇒ 断言改成**含 `sourceBindings`**，
    并补一条「`sourceBindings` 分区必须披露比了哪些字段」。
    ⚠️ 这是**对外契约的扩大**（披露面），不是测试放宽：多披露比少披露安全
    （「比了哪些字段」披露得越全，消费方越能自己判断能不能当「无破坏性变更」看）。
- **integration**（**并进既有文件**，不新建 integration 文件 ——
  CI 按「`find | sort` 后下标 mod 4」分片，新文件会重排分片 ⇒ 假 net-new；
  ⚠️ **新建 unit 文件是安全的**：unit 侧已改「路径 sha256 哈希」分片）：
  - `test_semantic_release_service.py`（+8 ⇒ **37** 条）：域草稿只存该域切片 / 域无锚点 ⇒ 阻断 /
    绑定类零 JOIN 边 ⇒ validate 409 且 check 表可查到该条 / 候选绑定 ⇒ WARNING 不阻断
    且候选不进快照 / 回滚副本 hash 逐字相同 / 回滚源不得为 DRAFT /
    回滚归档旧 ACTIVE 且源保持 ARCHIVED / 回滚撞全局版本号 ⇒ 409。
    **审查后补 6 条**：note 超长 ⇒ 422 / **同域同版本 ⇒ 409 且跨域同版本 ⇒ 允许** /
    回滚源 `schema_version` 过期 ⇒ 409 / 回滚源**不是该域的有效切片** ⇒ 409 /
    **候选统计按快照类收窄**（别域的候选 ⇒ 草稿行 PASS **且** validate 行仍 PASS）/
    域级版本号查重。
  - `test_semantic_release_api.py`（+3 ⇒ **79** 条）：回滚走完整 HTTP 链路
    （副本 hash 与源逐字相同、`no-store`）/ 回滚草稿 ⇒ 409 / 回滚不存在 ⇒ 404 /
    回滚路由对非 admin ⇒ 403。
    **审查后补 3 条**：回滚副本落 **CREATE 审计**（`after_json.rolledFromReleaseId` == 源 id、
    `requestedVersion`、`status == "DRAFT"`）/ **diff 两侧同口径**（`scope == "DOMAIN"`、
    `baselineDomainCode` / `baselineReleaseId`、**别域的类不出现在 changes**）/
    **全局发布的 diff 披露 GLOBAL 作用域**（`baselineDomainCode is None`）。
  - `test_semantic_release_route_auth.py`：期望路由集合 8 ⇒ **9** 条
    （新增 `("POST", "{prefix}/{releaseId}/rollback")`），测试名同步改 `..._expected_nine`。
- **支撑层拆分（纯搬移）**：`semantic_release_support.py` 抽出后**未新增业务用例** ——
  搬移的正确性由「既有集成用例全绿」保证（`rollback` / `createDraft` / 撞库翻译
  三条路径都跑在既有 integration 用例里）；审查后**补了 10 条纯函数单测**
  （`blockedVersion` / `_buildRollbackCopy` / `_validateNote`）。
  ⚠️ 单测里 `releaseServiceModule._mapCreateIntegrityError` 的调用路径**保持可用**
  （支撑层函数在编排层命名空间里再导出）。
- **实测**（`./scripts/run_local_tests.sh`）：
  批次 B 定向 **14 个文件（11 unit + 3 integration）⇒ 354 passed**（unit 232 / integration 122），
  退出码 0。逐文件：unit 新文件 20 / 16 / 10 / 8；
  integration `test_semantic_release_service.py` **37**、
  `test_semantic_release_api.py` **79**、`test_semantic_release_route_auth.py` **6**。
- **覆盖率**（同一次运行加 `--cov`）：本批 8 个模块合计 **95.16%**（逐模块见 §7.4.1）。
- **§67 真机**：**未做**（待 §11 的本地全绿闸门一起清；本批只跑本地测试栈）。

### 7.3 批次 A 回归扫描与失败归因（2026-11-19）

扫描口径：`./scripts/run_local_tests.sh app/tests/unit app/tests/integration -k "semantic or manifest or diff or release or knowledge or ontology"`
⇒ **1659 passed / 32 failed**（耗时 18 分 08 秒）。

**32 个失败无一由批次 A 引入**，三类归因（按可复核性排序）：

1. **路由守卫漂移（真缺陷，源自上一个提交 `bc0cac0e` M1-1.2）**：
   `test_semantic_release_route_auth.py::test_routes_are_exactly_the_expected_seven`
   报「路由条数 8 ≠ 7」——M1-1.2 新增了 `GET /{releaseId}/checks`
   （校验结论分页读），但**没有把新路由登记进守卫的期望集合**。
   守卫的设计意图是「集合相等：漏/多/改名都会红」⇒ 加路由必须同步登记。
   **本批已修**：期望集合补 `("GET", f"{_PREFIX}/{{releaseId}}/checks")`，
   期望条数 7 ⇒ **8**，测试名同步改为 `..._expected_eight`（4 读 + 4 写）。
2. **本地 embedding / LLM 服务未启动（环境）**：`11434`（Ollama）与 `507`（oMLX）
   都**不在线** ⇒ 依赖向量化的用例报「Embedding 调用失败: Connection error.」，
   进而在召回处走降级路径 ⇒ 断言随之失败（`test_chat_agent_run.py`、
   `test_ontology_*` 的一部分）。
3. **测试库有种子数据，而用例假定空表（结构性错配）**：
   `test_knowledge_compiler_reconcile.py` 三条用例假定 `ontology_class` **为空**
   （期望 `class == 0` / `== 2` / `== 1`），而本地测试库**已 seed 5 类 ERP 采购类**
   （`Supplier` / `ItemMaster` / `PurchaseOrder` / `Receipt` / `IncomingInspection`，
   M0 迁移 0153 引入）⇒ 实测 5 / 7 / 6。同源地，`test_ontology_reconcile_sync.py`
   的向量条数断言也是「期望 2 实得 7」＝ 种子 5 + 用例自建 2。
   ⇒ **不是本批引入的失败，是「本地全绿」闸门前必须处理的既有欠账**（见 §11）。

**批次 A 自身面全绿（可复核）**：
`git diff HEAD --stat -- <全部失败用例文件及其被测模块>` ⇒ **输出为空**
⇒ 失败用例的代码与被测代码**与 HEAD 逐字相同** ⇒ 结构上不可能由批次 A 引入。
批次 A 相关面定向复跑：`503 passed`（11 个 unit 文件 + 6 个 integration 文件，
含语义发布路由守卫与采购绑定路由守卫），退出码 0。

**遗留（本批不修，归 §11）**：第 2、3 类失败会让 M1 收尾时的「本地全绿」闸门
过不去；它们不是回归，而是**环境欠账 + 用例对种子数据的错误假设**。

### 7.4 批次 B 回归扫描与失败归因（2026-11-19）

扫描口径：`./scripts/run_local_tests.sh app/tests/unit app/tests/integration -k "semantic or manifest or diff or release"`
⇒ **1197 passed / 3 failed**（耗时 10 分 38 秒）。

**3 个失败逐个归因**：

| # | 用例 | 归因 | 处置 |
|---|---|---|---|
| 1 | `unit/test_ontology_discovery_api.py::test_diff响应披露字段级覆盖范围` | **批次 B 引入（真缺陷，已修）**：`sourceBindings` 进 `COVERED_SECTIONS` ⇒ `coveredFields` 披露多一个分区，而断言钉的是**集合相等** `== {classes, properties, joins}` ⇒ 必然红 | 断言改成含 `sourceBindings`，并补「`sourceBindings` 分区必须披露比了哪些字段」（§7.2） |
| 2 | `integration/test_chat_agent_run.py::test_chat_semantic_routing_stream_done_carries_card` | **环境**（§11 第 2 类）：本地 embedding 服务不在线 ⇒ `LlmClientError: Embedding 调用失败: Connection error.` ⇒ 召回走降级路径 | 与批次 B 无关（批次 B 未触及 chat / embedding / 召回）；归 §11 |
| 3 | `integration/test_knowledge_compiler_reconcile.py::test_reconcile_real_pg_empty_diff_zero_writes_audit` | **环境**（§11 第 3 类）：用例假定 `ontology_class` 为空（期望 `class == 0`），测试库已 seed **5 类** ⇒ 实得 5 | 与批次 B 无关；归 §11 |

**批次 B 自身面全绿**：定向复跑 9 个文件（6 unit + 3 integration）⇒ **263 passed**，退出码 0。

#### 7.4.1 审查处置后的复扫（2026-11-19，本批最终口径）

处置完 §6.7 的全部 HIGH / MEDIUM / LOW 之后**重新扫了一遍**（同一口径，另加 `--cov`）：

```
./scripts/run_local_tests.sh app/tests/unit app/tests/integration \
  -k "semantic or manifest or diff or release" \
  --cov=<批次 B 的 8 个模块> --cov-report=term-missing
```

⇒ **1225 passed / 2 failed**（10598 deselected，耗时 10 分 08 秒）。

| # | 用例 | 归因 | 与上轮对比 |
|---|---|---|---|
| 1 | `integration/test_chat_agent_run.py::test_chat_semantic_routing_stream_done_carries_card` | **环境**（§11 第 2 类）：本地 embedding 服务不在线 | **与上轮同一个**（不是本批引入） |
| 2 | `integration/test_knowledge_compiler_reconcile.py::test_reconcile_real_pg_empty_diff_zero_writes_audit` | **环境**（§11 第 3 类）：用例假定 `ontology_class` 为空，测试库已 seed 5 类 | **与上轮同一个** |

⇒ **净新增失败 = 0**（上轮的 3 个失败里，唯一由批次 B 引入的那个已修 ⇒ 从 3 降到 2，
少的两个都是环境欠账）；**净新增通过 = +28**（1197 → 1225：本轮补的用例 + 新增
`test_semantic_release_check_types.py` 8 条）。

**批次 B 自身面全绿**：定向复跑 **14 个文件（11 unit + 3 integration）⇒ 354 passed**，
退出码 0（unit 232 / integration 122）。

**覆盖率**（同一次运行加 `--cov`）：批次 B 的 **8 个模块合计 95.16%**（≥ 80% 闸门）：

| 模块 | 覆盖率 | 未覆盖行 |
|---|---|---|
| `semantic_release_checks` | **100%** | — |
| `semantic_release_check_types` | **100%**（补了 `test_semantic_release_check_types.py` 8 条后） | — |
| `semantic_domain_slice` | 97% | 96, 162, 165, 198-199, 454 |
| `semantic_release_support` | 97% | 141-142, 174-175, 272 |
| `semantic_release_domain_checks` | 97% | 212, 234, 352, 386 |
| `procurement_binding_candidates` | 95% | 71, 80 |
| `semantic_diff_service` | 93% | 420, 469, 477-480, 487, 491-492, 497, 500-501, 510, 512, 514, 519, 528, 534-535, 605 |
| `semantic_release_service` | 90% | 219-221, 339-342, 442-456, 464-466 |

⚠️ `semantic_release_check_types` 在本轮复扫里是 **95%**（第 69-70 行 = **超宽键**的
摘要分支**没有测试**）⇒ 补了 `test_semantic_release_check_types.py`（8 条：五元组完整性 /
角色与数据源可区分 / **超宽不截断** / 渲染稳定 / 闸门结论 `canPublish`）⇒ 回到 **100%**。
（这正是「文档里写了『超宽不截断』但没有用例钉住」的典型：口径写在注释里 ≠ 被验证。）

---

## 8. 安全审查

两轮审查已出结论，**批次 A 可合并**；逐条处置见 §6.5。**批次 B 的两轮审查结论与逐条处置见 §6.7**，
批次 B 新增攻击面的复核见 §8.1。

- **code-reviewer**：0 CRITICAL / 1 HIGH / 2 MEDIUM / 4 LOW。
  HIGH（版本常量未冻结）已修；MEDIUM-2（未来版本 500）已修；
  MEDIUM-3（发版顺序不可执行）改的是 SSOT 文本，见 §9；
  4 条 LOW 全部已修（归属校验 / 排序兜底 / 函数拆分 / import 环）。
- **security-reviewer**：批次 A 可合并，列出 F1–F9。
  F1 / F5 / F6 / F7 已修；F2 / F3 / F4 登记理由不改行为；
  F8 / F9（上限重推 + 绑定纳入 diff）归批次 B。

复核过的「无问题面」：v2 分区输出字段逐个查过（无自增 id / 无时间戳 / 无实例态）、
绑定排序键在库级唯一约束下是**全序**、v2 引用闸无绕过路径（逐条构造探测：
类不存在 / 属性张冠李戴 / 空属性类 / `keyOrdinal` 重复 / 绑定主键重复（含 null 字段）
/ v1 夹带绑定段 / 未知版本 / 缺版本号 / v2 缺绑定段 ⇒ 全部被拒）、
`/api/v1/semantic/releases` 整组仍挂 `requireAdmin`、
迁移 0156 up/down 对称且**只改默认值不动存量数据**。

### 8.1 批次 B 的安全面

批次 B 的两轮审查（`code-reviewer` + `security-reviewer`）结论与**逐条处置**见 §6.7。
批次 B 新增的**攻击面**只有三处，逐处复核过：

| 面 | 复核结论 |
|---|---|
| **回滚接口**（`POST /{releaseId}/rollback`） | 与既有发布路由**同挂 `requireAdmin`**（路由守卫期望集合 9 条里含它）；body 只有 `version` / `note` 两个字段，**不接受** manifest 正文 ⇒ 客户端无法借回滚注入任意快照；`note` 有长度闸（2000，Text 列无库侧兜底 ⇒ 服务层 422）；源不得为 DRAFT（409）；源 `schema_version` 必须等于当前 `SCHEMA_VERSION`（否则契约已漂移 ⇒ 409）；源必须是**该域的有效切片**（可复算判据：重切一次类集合相同）⇒ 409。审计留痕（`action="CREATE"`，`after_json.rolledFromReleaseId`） |
| **域切片** | 切片是**子集**（fail-closed：归不了属的一律排除）⇒ 发布出去的快照**不会**多带别的域的东西；唯一会多带的是**引用闭合补齐**（度量 / 属性 / JOIN 邻居 / 父类）⇒ 补齐集合**显式落表**（`DOMAIN_SLICE_CROSS_DOMAIN`，WARNING，带类名清单）⇒ 不是暗带 |
| **绑定纳入快照**（v2） | 绑定区只带**已确认**的绑定；**候选不进快照**（D-3）⇒ 未确认的绑定不会被生产读路径消费；候选数**实时查**且「没查过就不出结论」；绑定正文里的 `filterExpression` / `filterAllowlist` 仍受批次 A 的**编译期表达式安全复核**（F1）约束 |

⚠️ **0157 迁移是纯加法 DDL**（只建一个部分唯一索引）⇒ 不动存量数据；
但建索引前会**探测存量重复**，重复 ⇒ **报错不自动修**（重复版本号必须人工裁决）。

---

## 9. 部署验证

- 开发库 5433：已应用 0154 / 0155 / **0156** / **0157**（`column_default = 'v2'::character varying`；
  `semantic_release` 上已有 `uq_semantic_release_domain_version`）。
- 测试库 5434：已应用 0156 / **0157**；`alembic check` 全绿（`No new upgrade operations detected`）。
  ⚠️ `alembic/env.py` 用 `getSettings().databaseUrl` ⇒ **`TEST_DATABASE_URL` 对 alembic 无效**，
  查测试库必须 `DATABASE_URL=... uv run alembic check`。
  ⚠️ **0157 建索引前会探测存量重复**：两个库实测「同域同版本号」重复 **0 条** ⇒ 建索引成功。
- ⚠️ **开发库 5433 的 `alembic check` 不是绿的**（**既有欠账，非本批引入**）：
  差异项全部是**历史备份表**（`wiki_page_20260930` / `session_message_bak_20260930` /
  `knowledge_claim_20260930` / `evidence_20260930` / `session_query_state_20260930` /
  `ontology_class_pre_pruning_20260918` / `PORDER` / `PORDERQ` / `BPSUPPLIER`）
  与两个索引/外键的表达式差异（`ix_wiki_compile_task_created` /
  `fk_wiki_token_usage_compile_task`）⇒ **没有一项属于 `semantic_release`** ⇒ 与本批无关。
  测试库（5434）**无**这些备份表 ⇒ 全绿 ⇒ 闸门口径**以测试库为准**。
- **生产未部署**（用户决策：功能全完成 + 本地全绿后统一发版）。

### 9.1 发版顺序（**已更正**，code-review MEDIUM-3）

原先写的「**先产出一份 v2 快照再切代码**」**不可执行**：
v2 快照只能由已经产出 `SCHEMA_VERSION="v2"` 的代码编译出来，
而 v1 代码的 Schema 是 `additionalProperties: false` ⇒ **老代码产不出 v2 快照**。

可执行的顺序只有：

1. **先切代码**（新版本上线，此时库里还挂着 v1 ACTIVE 快照）；
2. 立刻**建一份 v2 发布并 activate**（旧 v1 被归档）；
3. 第 1 步到第 2 步之间的**窗口期**：NL2SQL 的 schema 渲染**降级直读治理表**
   （`semantic_schema_adapter.schemaVersionMismatch` 判漂移 ⇒ 走直读路径）。

窗口期的性质：**功能正确、性能回退**（每次请求打 WARNING），不是故障。
窗口期长度 = 一次「建发布 + validate + activate」的操作时间（分钟级）。
若必须零窗口 ⇒ 只能**先手工造一份 v2 manifest 的发布行**（绕过编译器），
但那要绕过发布路径写库 ⇒ 违反「版本漂移必须暴露而不是抹平」的设计 ⇒ **不做**。

> ⚠️ 另有两条同源的诊断噪音（不是故障）：① 校验面按版本分发 ⇒ 库里的 v1 快照
> 在 `MANIFEST_SCHEMA` 校验项上**判 PASS**（它按 v1 契约确实合法），
> 而渲染面说「这份快照当前读不了」⇒ 两个面对同一份快照给出相反结论，
> 排查时以「渲染面」为准（它决定实际生效路径）。

---

## 10. 关联

- 计划：`docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md` §M1（1.3 / M1-a）
- 问卷产物：`docs/update/1118/M0/answers/`（G1 登记 172 题、41 类绑定范围）
- 既有 SSOT：`Harness/changes/2026-11-18-m1-semantic-release-check/summary.md`（M1-1.2）、
  `Harness/changes/2026-11-18-m1-procurement-binding-service/summary.md`（M1-1.1）
- Wiki：`Harness/wiki/data-model.md`（M1 增量节）

---

## 11. 本地全绿闸门的已知阻塞项（非本批引入，但收尾前必须清）

用户决策「**功能全完成 + 本地全绿后统一部署**」⇒ 这三项必须在 M1 收尾前处置，
否则「全绿」永远达不到。登记在此，避免下一棒把它误判成新回归。

| # | 阻塞项 | 现象 | 处置方向（未实施） |
|---|---|---|---|
| B-1 | 本地 embedding / LLM 服务未启动 | `11434`（Ollama）与 `507`（oMLX）不在线 ⇒ 向量化用例「Connection error」 | 起本地模型服务；或在测试夹具里把 embedding 换成确定性 stub（**不得**把真实调用塞进 CI） |
| B-2 | 用例假定空表，但测试库有种子数据 | `test_knowledge_compiler_reconcile.py` 期望 `ontology_class` 为空，实得 5 类 ERP 采购类 | 用例改成**相对断言**（记录基线行数再比差值），而不是绝对值 `0`；或让用例自建隔离 schema |
| B-3 | Milvus 测试库向量残留 | `test_ontology_reconcile_sync.py` 期望 2 条向量，实得 7（种子 5 + 用例 2） | 同 B-2：相对断言 / 用例级清理 |

⚠️ 这三项**不得**用「清空测试库」的方式糊过去：种子数据是 M0 迁移 0153 校准过
的 ERP 词表来源，清掉会连带破坏 A1 本地栈同构的取证基线。

---

## SSOT 校验清单（合并前必查）

批次 A（本次提交）：

- [x] 迁移 0156 应用成功；测试库 `alembic check` 全绿
- [x] `SCHEMA_VERSION` 与 ORM `server_default`、DB 默认值三处一致
- [x] v1 快照仍按 v1 Schema 校验通过（版本分发不误伤历史）
- [x] 未知 `schemaVersion` ⇒ `ManifestSchemaError`（不放行）
- [x] 两次编译同一输入 ⇒ 字节一致（含 `sourceBindings` 分区）
- [x] golden 反向守卫含新分区键集合 + 禁实例列
- [x] 残留 `"v1"` 夹具逐条判定（当前版本 ⇒ 动态取值；旧版本 ⇒ 保留并注释）
- [x] **版本字面量冻结**：注册表按字面量登记 + 每版自述自己的版本（升版不再抹掉旧版）
- [x] **表达式安全是发布不变量**：过滤/变换表达式在编译期复核（F1）
- [x] **候选判据 fail-closed**：`is_candidate` 必须显式为假（F5）
- [x] **错误消息截断**（F6）+ **DB 默认值集成断言**（F7）
- [x] 属性归属错配在编译期定位到绑定 id（LOW-1）
- [x] ORM 不再 import 契约模块（import 环已断，实测 app 模块数 33 ⇒ 23）
- [x] **回归扫描归因**：32 个失败逐条归因（路由守卫漂移 1 + 环境 2 类），无一由本批引入
      （`git diff HEAD` 对失败用例面为空 + 定向复跑 503 passed，§7.3）
- [x] **补上 M1-1.2 遗留的路由守卫漂移**：期望集合 7 ⇒ **8** 条（新增
      `GET /{releaseId}/checks`），守卫重新有牙

批次 B（本次提交）：

- [x] 校验项上限从契约推导（单测钉死公式 `10006`）
- [x] 缺 JOIN 绑定 ⇒ validate 409 + check 表可查 + 不得激活
- [x] 回滚往返后 `manifest_hash` 逐字一致
- [x] 覆盖率 ≥ 80%（本批新增代码）：定向 8 模块实测 **95.16%**（§7.4.1 逐模块表）
      （`semantic_release_checks` 100% / `semantic_release_check_types` 100% /
      `semantic_domain_slice` 97% / `semantic_release_support` 97% /
      `semantic_release_domain_checks` 97% / `procurement_binding_candidates` 95% /
      `semantic_diff_service` 93% / `semantic_release_service` 90%）
- [x] **编排层 / 支撑层切分**：`semantic_release_service.py` 851 ⇒ **480** 行
      （拆出 `semantic_release_support.py` **619** 行），两个文件都未超 800 行上限
- [x] **回归扫描归因**：批次 B 扫描 3 个失败（净新增 1 + 环境 2），
      净新增（披露面扩大 ⇒ 集合相等断言红）**已修**，其余 2 个为 §11 既有欠账（§7.4）；
      **处置后复扫 ⇒ 1225 passed / 2 failed，净新增失败 = 0**（§7.4.1）
- [x] **单元拆分不引入新用例**：支撑层为纯搬移，靠既有集成用例兜底
      （`rollback` / `createDraft` / 撞库翻译三条路径）
- [x] **两轮审查的 HIGH 全部处置**（§6.7）：
      ① 四条闭包取**一个联合不动点**（属性引用 / JOIN 邻居 / 父类交替迭代）
      ⇒ 带绑定的父类不再被判孤岛、合法发布不再被误阻断；
      ② 属性引用（`refClassName`）也闭 ⇒ 该域不再「永远发不出去」；
      ③ `ManifestSchemaError` ⇒ `ValidationError`（**422 而非 500**）；
      ④ diff 两侧**同口径**（候选侧按同一个域切片）⇒ 差异报告不再在说谎
- [x] **域作用域版本号唯一**（迁移 **0157** + 服务层查重双保险）：
      同域同版本 ⇒ 409；**跨域同版本 ⇒ 允许**（版本号标识「哪一次发布」）
- [x] **绑定键渲染器只有一份**（`semantic_release_check_types.bindingTargetKey`）：
      校验侧与差异侧共用 ⇒ 差异报告里的绑定名**查得到** check 表里对应的那行
- [x] **候选统计按「已存快照里的类」收窄**（草稿 / validate / activate 三处同口径）
      ⇒ 同一份快照在三个阶段给出**同一个**候选数（证据可复算）
- [x] **支撑层补单测**（10 条纯函数）：`blockedVersion` / `_buildRollbackCopy` / `_validateNote`
- [x] **文件体量复核**：`semantic_diff_service.py` 642 ⇒ **794** 行（上限 800，只剩 6 行余量）
      ⇒ 登记为 L-6（下一轮先拆 `semantic_diff_bindings.py`），本批不再往它加行
