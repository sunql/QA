# 变更：2026-11-19-m1-procurement-domain-release（M1-1.3 采购域发布编排）

> 状态：进行中（分两批提交：**A = manifest v2（本次，已完成）**；B = 域编排 + 回滚 + 采购域校验项）
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
| `semantic_diff_service` | 只比 classes/properties/joins ⇒ 新分区**不参与** diff | 记为已知缺口（「发布后绑定变了」不可见），不在本批范围 |

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
- `targetKey` 用 `类名@schema.表` 复合键 ⇒ 同一发布同一校验项同一绑定只留一行
  （对上 `uq_semantic_release_check_unique`）。

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
- **`semantic_diff_service` 未覆盖 `sourceBindings`**：已进 `UNCOVERED_SECTIONS`
  并在 API 契约里披露 ⇒ 「发布后绑定变了」在 diff 视图里不可见（本批范围外）。
- **校验项仍只有 6 条**（2 发布级 + 3 指标级）：采购域校验项与上限重推属批次 B。
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

### 7.2 批次 B（待做）

- **unit**：域切片 + 引用闭合；绑定级校验项；上限公式（`10006`）；回滚副本逻辑（hash 逐字相同）。
- **integration**：采购域编辑→编译→validate→activate→回滚→再 activate，往返 hash 一致；
  构造缺 JOIN 的绑定 ⇒ validate 409 + check 表可查到该条 + activate 被拒；
  同一输入两次编译字节一致。
  ⚠️ 新集成用例必须**并进既有 integration 文件**（CI 按「`find | sort` 后下标 mod 4」分片）。
- **§67 真机**：在开发库 5433 走一遍采购域全流程 + 缺陷场景，输出进本文件附录。

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

---

## 8. 安全审查

两轮审查已出结论，**批次 A 可合并**；逐条处置见 §6.5。

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

---

## 9. 部署验证

- 开发库 5433：已应用 0154 / 0155 / **0156**（`column_default = 'v2'::character varying`）。
- 测试库 5434：已应用 0156；`alembic check` 全绿。
  ⚠️ `alembic/env.py` 用 `getSettings().databaseUrl` ⇒ **`TEST_DATABASE_URL` 对 alembic 无效**，
  查测试库必须 `DATABASE_URL=... uv run alembic check`。
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

批次 B（后续提交）：

- [ ] 校验项上限从契约推导（单测钉死公式 `10006`）
- [ ] 缺 JOIN 绑定 ⇒ validate 409 + check 表可查 + 不得激活
- [ ] 回滚往返后 `manifest_hash` 逐字一致
- [ ] 覆盖率 ≥ 80%（本批新增代码）
