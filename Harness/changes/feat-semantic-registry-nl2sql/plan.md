# P2 NL2SQL 影子接入 —— 执行计划（续接 1109 日级排程 D12–D17）

> 上游基线：`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`（D1–D62）
> 裁决依据：`docs/update/1109/p0/裁决记录.md`（四项，2026-11-09 确认）
> 本文档是**续接计划**：P0/P1a/P1b 已完成，P2 尚未开工（证据见 §0.1）。
> 本文件不含"当前功能实现声明"——WP-ONT-AI（AI 驱动本体发现）是**另一条变更线**，不在本计划内。

---

## 0. 阶段现状与续接判断

### 0.1 已完成 / 未开工（实测证据，2026-11-15 复核）

| 阶段 | 日次 | 实际执行日 | 落点 | 状态 |
|---|---|---|---|---|
| P0 基线与裁决 | D1–D3 | 11-09 | `docs/update/1109/p0/`（环境基线 / 数据基线 / 裁决记录 / R01-R03 v1.1） | ✅ G1 通过 |
| P1a 发布骨架 | D4–D7 | 11-09 23:17–23:59 | PR #114 → `849de29e` | ✅ |
| P1b Registry + API | D8–D11 | 11-10 06:10–10:03 | PR #115 → `4777bfba` | ✅ G2 通过 |
| **P2 NL2SQL 影子接入** | **D12–D17** | **D12 / D14 已完成（11-15）** | 分支 `feat/semantic-registry-nl2sql`；证据 `daily/D12.md`、`daily/D14.md` | 🔄 进行中 |
| P3a–P3d | D18–D31 | 未开工 | — | ⬜ |
| P4 知识 Facade | D32–D37 | 未开工 | — | ⬜ |
| P5 Workflow/Command | D38–D57 | 未开工 | — | ⬜ |
| P6 第二板块 dry-run | D58–D62 | 未开工 | — | ⬜ |

**未开工的硬证据**（`main` @ `10c79bb3` 全文 grep 0 命中）：
`ENABLE_SEMANTIC_REGISTRY_NL2SQL`、`ENABLE_SEMANTIC_REGISTRY_RAG`、`scripts/semantic_schema_shadow_diff.py`、
`semantic_release_id`（ORM 与迁移均无）、`/api/v1/semantic/knowledge/query`、`workflow_*` / `approval_*` 表。
另：`semantic_registry_service` 目前**没有任何生产消费方**（仅 `semantic_releases.py` API + 测试引用）
⇒ P2 是 Registry 的**第一个真实消费方**。

### 0.2 分支策略：**新开分支，不延续旧分支**

| 选项 | 判定 |
|---|---|
| 延续 `feat/semantic-release-a` / `feat/semantic-registry` | ❌ **不可行**：两者已 squash 合入 main（`849de29e` / `4777bfba`），且在 2026-11-15 分支盘点中已删除（凭据 `Harness/changes/branch-audit-2026-11-15.md`）。squash 后分支 tip 不是 main 祖先，续用会带着已进 main 的内容重复一遍 |
| 从 `feat/semantic-release-a` 续 | ❌ 同上；且该分支不含 P1b 的 D8–D11 |
| **从 `origin/main` 新开 `feat/semantic-registry-nl2sql`** | ✅ **采纳**。1109 排程 §0.2 本就规定「每子阶段一个 worktree + 分支」，且预置了分支名 `feat/semantic-registry-nl2sql` |

**理由**（三条，缺一不可）：

1. **子阶段边界**：排程把 P1a/P1b 切成两个分支两个 PR；P2 是第三个子阶段，语义上是「Registry 的第一个消费方」，与 P1b 的「提供 Registry 服务」是生产者/消费者关系，不该混在一个分支里。
2. **squash 语义**：本仓库全走 squash 合并 ⇒ 「从 main 起」等价于「从已合内容起」，不会丢东西；而从旧分支起会重复携带已合并内容，PR diff 失真。
3. **主分支已前进**：P1b 之后 main 走了 #118–#126 + #128 共 7 个 PR，其中 #125（KPI P4b 编译器）、#122（别名解析层 + 迁移 0143）都动过**指标与本体相关代码**。旧分支的基线已经过时，续用必然冲突。

### 0.3 P1b 自身的 4 条保留项（带进 P2，不在本批修）

1. stub 鉴权 fail-soft（开关开 + stub 鉴权只 `logger.error` 不 fail-closed）；
2. 完整性异常保持 HTTP 500（`SemanticReleaseIntegrityError`）；
3. 按域版本唯一性**无库侧约束**；
4. 本地 venv 0.136.1 vs `uv.lock` 0.141.1（版本敏感结论以 CI 装配为准）。

---

## 1. 分支与工作树

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
export HTTPS_PROXY=http://127.0.0.1:12450 HTTP_PROXY=http://127.0.0.1:12450 ALL_PROXY=http://127.0.0.1:12450
git fetch origin && git log --oneline -1 origin/main        # 记下基线 SHA，写进 plan 顶部

git worktree add /private/tmp/qa-semantic-p2 \
  -b feat/semantic-registry-nl2sql origin/main

cd /private/tmp/qa-semantic-p2/backend
uv sync --frozen --extra dev        # 必须带 --extra dev，否则没有 pytest
```

**坑（已知）**：

- `backend/.venv` 是**每个 worktree 各自**的，主仓那份不能借用；
- `uv sync --frozen` 不带 `--extra dev` ⇒ `No module named pytest`；
- 跑测试时 traceback 路径应是 `<worktree>/backend/app/...`，不是主仓路径（防 editable 遮蔽）；
- **不要** `git worktree add` 到 `.worktrees/`（前端 `node_modules` 符号链接会污染 gitignore）；
- 前端本批**零改动**（R03 口径：前端 0 人日），不建 `node_modules`。

---

## 2. 开工前必须先补的 4 件事（1109 计划里没有，但会挡住 D13）

### 2.1 dev 库必须先有一行 ACTIVE 发布 ⛔ 阻塞 D13

**现状**：dev（5433 / `qa_metadata`）alembic 已 0143（含 0142 `semantic_release`），但
`semantic_release` 表 **0 行** ⇒ 发布链路（编译 → 校验 → 激活 → 回滚）**从未在真实数据上端到端跑过**。

**为什么阻塞 D13**：影子比对的定义是「同一本体生成快照 schema 文本 vs 直读 schema 文本，差异落报告」。
没有 ACTIVE 快照 ⇒ 快照侧是空的 ⇒ 比对结果没有意义（不是"差异 0"，是"没东西可比"）。

**动作**：D12 内先跑一次真实发布链路，产出一行 ACTIVE 快照并记下 `release_id` 与 `manifest_hash`。
这是**写 dev 库**，不是写生产 ⇒ 不需要 `--allow-prod-write`，也不需要 `pg_dump`。
证据落 `daily/D12.md`。

### 2.2 「与现状逐字一致」的基线要按**当前 main** 重新取证 ⛔ 影响 D12 完成定义

D12 的完成定义是「关闭态与现状输出完全相同（fixture 对照）」。1109 计划写于 11-09，
此后 main 走了 7 个 PR，其中 #122（迁移 0143）、#125（KPI P4b 编译器）改过指标/本体相关代码。

**动作**：D12 先在**当前 main** 上抓一份「直读 schema 文本」基线 fixture，再写适配器；
不引用 11-09 的任何旧结论。

### 2.3 生产迁移债与 P2 分开管 ⛔ 别混

- dev alembic = **0143**；生产 = **0140**。
- `0141`（残余 36 条差异中的 6 项 DDL）+ `0142`（`semantic_release` 表）**均未部署生产**。
- 1109 排程 §0.2 已规定「P0–P4 **不部署生产**」⇒ **P2 不部署**，0141/0142 的生产 DDL
  是**独立评审项**（须把逐条 SQL 给用户过目），**不在 P2 的 PR 里顺手带上**。

### 2.4 外部依赖仍未到位（不阻塞 P2，但要记账）

| 依赖 | 影响 | 状态 |
|---|---|---|
| 采购域核心指标清单（名称/口径/分子分母/负责人） | 校准裁决三的两层字段 | ⬜ 待业务 |
| 存在多源的实体/属性清单（ERP/SRM/合同） | 校准裁决二的 `sources[]` 结构 | ⬜ 待业务（P1-D6 前定字段，已过期） |
| 域划分现状确认（`data_domain` 3 行是否即试点范围） | 校准裁决四的域过滤口径 | ⬜ 待业务 |
| ERP/SRM 接口联调窗口 | P5 才用 | ⬜ 待外部（P2 不涉及） |

> 裁决记录明确「后补，不改变已确认的裁决」⇒ 这三项**不阻塞 P2 开工**，但会影响 P4 的采购别名口径。

---

## 3. 适配器面（实测收敛，2026-11-15）

### 3.1 注入点与窄接口（实测穷举，2026-11-15）

`nl2sql_schema.buildSchemaText`（`backend/app/services/nl2sql_schema.py:264`）的**全部**被读字段
（对 `nl2sql_schema.py` 做 `cls.` / `prop.` / `join.` 穷举，非抽样）：

| 对象 | 被读字段 | 用途（行号） |
|---|---|---|
| 类 | `id` | JOIN 图索引 `:135`/`:392`、`classesById` `:285` |
| 类 | `class_name` | 继承标题、plan 名对齐 `:428`/`:470` |
| 类 | `class_alias` | 类头别名 `:310` |
| 类 | `source_table` | 表名烘焙、孤岛判定 `:304`/`:119` |
| 类 | `parent_class_id` | 父类解析 `:471` |
| 类 | `properties` | 属性遍历 `:321` |
| 类 | `__dict__` | **惰性 `parent` 关系探测** `:466`/`:583` |
| 属性 | `property_name` | 列名 token `:337`/`:512` |
| 属性 | `property_alias` | 别名 token `:338` |
| 属性 | `source_column` | 列名 `:321`；空则标「未映射」 |
| 属性 | `data_type` | 类型 `:352` |
| 属性 | `description` | 说明 `:361` |
| 属性 | `business_aliases` | 业务别名 `:355` |
| 属性 | `is_primary_key` / `is_foreign_key` | PK/FK 标注 `:323`/`:326` |
| 属性 | `ref_class_id` | 外键目标表解析 `:185` |
| 属性 | `__dict__` | **惰性 `ref_class` 关系探测** `:181` |
| JOIN | `source_class_id` / `target_class_id` | 两端表名解析 `:398`/`:399` |
| JOIN | `source_columns` / `target_columns` | JOIN 列渲染 `:403`/`:405`；可达性提示 `:144` |
| JOIN | `description` | JOIN 注释 `:410` |

⇒ **不需要**把 ORM 实体整体复刻：用一个只含上述字段的轻量结构即可承载 manifest 复原结果。

> ⚠️ **`__dict__` 陷阱（实施硬约束）**：类与属性对象**都**必须具备实例 `__dict__`
> （`:181` 探 `prop.__dict__`、`:466` 探 `cls.__dict__`）。
> ⇒ 复原对象**不能用** `@dataclass(slots=True)`、`NamedTuple`、`__slots__` 类
> （会抛 `AttributeError: __dict__`）。
> ⇒ 只能用**普通 class 实例**或 `dataclass`（**不带** `slots=True`）。D12 第一条测试就要钉这条。

> 复原对象里没有 `ref_class` / `parent` 键 ⇒ 自动走 `classesById.get(...)` 分支，**行为一致**
> （需在 D14 用 golden fixture 钉死）。

### 3.2 ⚠️ 作用域陷阱：`classes` 不是只有 schema 文本一个消费方

`pc.classes`（chat 主路径，`_PipelineContext.classes`，源头 `chat_service.py:994` `_selectRelevantClasses`）
在 **6 个文件、11 个调用点**被消费（2026-11-15 逐点 grep 实测）：

| # | 消费方 | 位置 | 对 manifest 复原对象的要求 |
|---|---|---|---|
| 1 | **schema 文本渲染** | `chat_domain.py:102` `buildSchemaText(pc.classes, joins=pc.joins)` | ✅ 只需 §3.1 窄字段 ← **P2 目标** |
| 2 | 值域采样 | `chat_service.py:1025` `_sampleValueDomains`（def `:1124` → `value_sampler.py:89` `_candidates`） | 读 `cls.source_table` / `cls.properties` / `prop.source_column` / `prop.data_type` ⇒ 窄字段**够** |
| 3 | 本体漂移告警 | `chat_service.py:1026` `_buildDriftWarning`（def `:1078` → `schema_introspection_service.py:402` `validateOntologyDrift`） | 比对 `schema_cache`（活库缓存）；读 `source_table`/`source_column` ⇒ 窄字段够，但**依赖活库** |
| 4 | 类召回打分 | `chat_service.py:1352` `_classRecallScores`（def `:1460`） | 读 `getattr(cls, "id")` 与 `getattr(cls, "_recall_score", 0.0)` ⇒ 复原对象**丢失召回分**（回落 0.0） |
| 5 | NL2SQL 生成 | `chat_service.py:1603` / `:1618` `generateValidatedPlan(question, classes, ...)` | 走 `buildSchemaText` 同一条渲染路径 |
| 6 | 不可回答兜底 | `chat_service.py:2101` `_unanswerableAnswerText`（def `:1991`） | 读类名/别名做建议 |
| 7 | 多步主路径 | `chat_multistep.py:273`、`:512` | 逐步骤独立召回，`list(step_classes) + list(pc.classes)` |
| 8 | 流式查询 | `chat_stream_query.py:201` | 同 #6 |
| 9 | 用量统计重试 | `chat_usage.py:108` | 重试时复用 `pc.classes` |
| 10 | 假设服务 | `hypothesis_service.py:307` `extractDriverNames(pc.classes)` | 抽驱动因子名 |

⇒ **本批必须把适配器作用域限定为「schema 文本渲染路径」（#1/#5）**，其余消费方继续读治理表。
理由有三条（缺一不可）：

- **#4 会静默丢召回分**：复原对象没有 `_recall_score` ⇒ `getattr(..., 0.0)` ⇒ 召回打分全部塌成 0.0
  ⇒ **不是报错，是静默降级**（本仓最忌讳的失败模式）。
- **#2/#3 依赖活库/活会话**：漂移校验要 `session` + `ds`，快照侧没有这个上下文。
- **#7/#10 的取类顺序与召回绑定**：多步每步独立召回后与 `pc.classes` 拼接，替换会改召回集合。

这**不违反裁决一**（裁决一说的是「Registry `getActive()` 只读 `semantic_release` 表、不回查治理表」，
不是「所有消费方都必须改读快照」）。把这条写进 D12 的设计说明，避免后续被误扩成全量替换。

> 实施形态建议：适配器返回**仅供 `buildSchemaText` 用的窄视图**（不替换 `_PipelineContext.classes`），
> 即在 `chat_domain.py:102` 处做一次「治理表 classes ↔ 快照 classes」的二选一渲染。
> 这样 #2–#10 一行不动。

---

## 3.3 ⛔ 关键发现：Manifest 是 **name-keyed**，而 `buildSchemaText` 全线 **id-keyed**

> 这是本轮调研最重要的产出，**1109 计划未覆盖**，D12 开工前必须先拍板。

### ① 快照侧（`semantic_compiler.py` 实测）：**无 id、camelCase、名字引用**

Manifest 顶层键：`schemaVersion` / `domains` / `classes` / `joins` / `metrics` / `kpis` / `businessObjects`
（`semantic_compiler.py:88-108`）。**没有任何 id 映射表**。

| 对象 | Manifest 键（实测） | ORM 属性 | 对应关系 |
|---|---|---|---|
| 类 | `className` `classAlias` `description` `sourceTable` `objectType` `objectOwner` `parentClassName` `properties` | `class_name` `class_alias` `description` `source_table` `object_type` `object_owner` `parent_class_id` | **`parentClassName` 是名字，不是 id** |
| 类 | —（**无 `classId`**） | `id` | ⚠️ **缺** |
| 属性 | `propertyName` `propertyAlias` `businessAliases` `description` `dataType` `isPrimaryKey` `isForeignKey` **`refClassName`** `sourceColumn` `unit` `allowedValues` `isNotNull` `minValue` `maxValue` `regexPattern` | `property_name` `property_alias` `business_aliases` `description` `data_type` `is_primary_key` `is_foreign_key` **`ref_class_id`** `source_column` … | **`refClassName` 是名字，不是 id** |
| 属性 | —（**无 `propertyId`**） | `id` | ⚠️ **缺** |
| JOIN | `joinKey` **`sourceClassName`** `sourceColumns` **`targetClassName`** `targetColumns` `joinType` `relationType` `description` | `source_class_id` `target_class_id` `source_columns` `target_columns` `description` | ⚠️ **两端都是名字** |

编译器用 `resolveActiveName(...)`（`semantic_compiler.py:233`/`:257`）把 id 转成**活跃类名**——
这是裁决一「快照自包含、Registry 不回查治理表」的必然结果（快照不能带自增 id，见
记忆 `qa-system-semantic-manifest-determinism`）。

### ② 渲染侧（`nl2sql_schema.py` 实测）：**全线按 id 索引**

- `classesById = {cls.id: cls ...}`（`:285`）
- 父类：`classesById.get(cls.parent_class_id)`（`:471`）
- 外键目标表：`classesById.get(prop.ref_class_id)`（`:185`）
- JOIN 两端：`tableByClassId.get(join.source_class_id)` / `join.target_class_id`（`:398`/`:399`）

⇒ **manifest 无法直接喂给 `buildSchemaText`**。这是 D12 的真正工作量所在，
1109 计划把 D12 写成「适配器 `loadClassesForSchema(domain?)`」低估了这一层。

### ③ 三个可选方案

| 方案 | 做法 | 判定 |
|---|---|---|
| **A. 合成 id（推荐）** | 从 manifest 建 `className → synthetic_id` 映射（按 manifest 内出现顺序，稳定 ⇒ 字节可复现）；类/属性/join 的 id 全部由名字反查该映射 | ✅ **采纳**。改动**只在适配器内部**，`nl2sql_schema.py` 一行不动 |
| B. 改 `buildSchemaText` 为 name-keyed | `classesById` → `classesByName`、`join.source_class_id` → `sourceClassName` | ❌ 侵入 `nl2sql_schema.py`（640+ 行，被 `nl2sql_plan.py`/`nl2sql_service.py` 共用），且要**同时**支持 ORM 路径与快照路径 ⇒ 反而更复杂 |
| C. 快照带真 id | 把 `classId`/`propertyId` 写进 manifest | ❌ **违反裁决一与字节确定性**：自增 id 跨环境不一致（同 记忆 `qa-system-semantic-manifest-determinism` 的 `measure.class_id` 教训） |

**方案 A 的映射细节**：

```
synthetic_id(className) = 稳定序号（manifest 内 classes 数组下标 + 1，或按 className 排序后下标）
class.id                 = synthetic_id(className)
class.parent_class_id    = synthetic_id(parentClassName)   # 名字不在快照子类集内 ⇒ None
prop.ref_class_id        = synthetic_id(refClassName)       # 同上
join.source_class_id     = synthetic_id(sourceClassName)
join.target_class_id     = synthetic_id(targetClassName)
```

⚠️ **名字不在「当前渲染子集」内时必须取 `None`**，而不是回查全局 ——
这与 ORM 路径行为**一致**（`classesById` 只在传入子集里查，查不到就是查不到）。

### ④ 召回子集必须原样保留（否则行为漂移）

`buildSchemaText` 的入参是**召回子集**（`chat_service.py:994` `_selectRelevantClasses`），
且 SQL 阶段还有一次裁剪（`nl2sql_schema.py:235` `pruneClassesForSqlStage`，按
`plan.selectedClasses` ∪ `plan.joins[].sourceClass/targetClass`）。

⇒ 适配器**必须按传入子集的 `class_name` 过滤 manifest**，只渲染子集内的类。
若图省事把整份 manifest 复原 ⇒ schema 文本变长、JOIN 图变密、召回边界被悄悄改写
⇒ **既违反「关闭态与现状逐字一致」，也让 D13 的差异归零永远归不了**。

> 这也解释了为什么方案 C（带真 id）不可行：id 进去了不代表子集能保住。

---

## 4. 与 1109 计划的偏差清单（7 条，需在 D12 内确认）

| # | 1109 计划写法 | 实际需要 | 理由 |
|---|---|---|---|
| ① | D12 适配器 `loadClassesForSchema(domain?)` | 追加「只服务 schema 文本渲染路径」的显式边界 | §3.2 的 11 个调用点 |
| ② | D13 影子比对直接跑 | 先补 §2.1 的 ACTIVE 快照 | 无 ACTIVE 行则比对无意义 |
| ③ | D14「差异归零：只修快照渲染」 | 额外增加「manifest 复原对象 vs ORM 实体的字段等价」golden | §3.1 字段清单是复原正确性的唯一护栏 |
| ④ | D16 落点「优先 `multi_step_run` / `session_query_state`」 | 复核：`models.py` 中**只有 `SessionQueryState`**（`__tablename__ = "session_query_state"`），`multi_step_run` 未在该文件命中 | 落点需在 D16 现场确认，别照抄 |
| ⑤ | P2 不部署生产 | 维持；但 `0141`/`0142` 生产 DDL 独立立项 | §2.3 |
| ⑥ | D12「适配器」隐含「manifest 能直接喂 `buildSchemaText`」 | **需一层 id 合成**（§3.3）——manifest 无 id，`buildSchemaText` 全线按 id 索引 | 快照是 name-keyed，渲染器是 id-keyed |
| ⑦ | D14「差异归零」隐含「差异只在文本层」 | 需额外断言「**召回子集不因快照而扩大**」 | 整份 manifest 复原 ⇒ schema 文本变长、JOIN 图变密 ⇒ 行为漂移（§3.3 ④） |

### 4.1 D12 实测新增的偏差（3 条，2026-11-15）

| # | 计划写法 | 实际需要 | 理由 |
|---|---|---|---|
| ⑧ | D12 只做适配器，比对工具属 D13 | 本日把比对工具一并做了并跑了**首轮** | 工具是「抓基线 fixture」的载体；两套代码路径 ⇒ 基线不可信。首轮报告落 `daily/D13-shadow-diff-round1.md`（D13 的证据顺带产出） |
| ⑨ | 基线 fixture 进 `app/tests/fixtures/` | 基线落 `Harness/changes/.../baseline/`，**不进测试夹具** | 本体一改基线就变 ⇒ 进 CI 变成周期性假红。基线是**证据**不是**断言**；自动化断言改用**对象同一性**（`restored is classes`），比文本相等更强 |
| ⑩ ⛔ | D14 完成定义「差异 = 0」 | **零差异判据必须拆成三个机检指标**（D14 已定稿，见 §4.1-⑩） | 实测：内容差异 **0**、顺序敏感差异 6524 行。根因是治理表侧 `for prop in cls.properties:` **不排序**（物理行序），快照侧按 `propertyName` 排序；快照是 name-keyed，**物理行序信息在编译时已丢失且不可重建**（按 id 排序会违反跨环境字节确定性）。详见 `daily/D12.md` F4 与 `daily/D14.md` 的决断与影响面评估 |

### 4.1-⑩ ⛔ D14 的零差异判据（**2026-11-15 定稿**，D12 的 F4 出路分析结论）

D12 只取证不决策，D14 拍板。三个候选出路：

| 出路 | 做法 | 判定 | 依据 |
|---|---|---|---|
| (a) | 治理表侧也按 `property_name` 排序 ⇒ 两侧逐字一致 | ❌ **否决** | 改的是**生产 prompt**（P2 §2.3 明确 P2 不动生产路径）；且把「今天的行为」改成「今天之后不再存在的行为」⇒ 失去可比基线 |
| (b) | 以「内容等价 = 0」为零差异判据，列序差异**单列计量并显式记录** | ✅ **采纳** | 前提是「直读侧的顺序本身不是契约」——实测成立（见下） |
| (c) | 快照侧保留治理表的列序（编译器按 id 排序） | ❌ **否决** | 违反跨环境字节确定性（记忆 `qa-system-semantic-manifest-determinism`） |

**为什么直读侧的顺序不是契约**（实测，三条）：

1. `OntologyClass.properties` 关系**没有 `order_by`**（`app/domain/models.py:283`）⇒ SQL 语义**不保证**列序，实测是堆物理序（`BOM_DESCRIPTION` 在 `BASE_QTY` 之前）；
2. `OntologyService.listJoins` 按 `OntologyJoin.id` 排序（`ontology_service.py:1281`）= **插入序** ⇒ 跨环境不同（dev 与 prod 的导入顺序不一样）；
3. `OntologyService.listClasses` 按 `class_name` 排序（`ontology_service.py:457`）——**只有类序是契约**，列序与 JOIN 序都不是。

⇒ 追「与直读侧逐字一致」是在追一个**自己会漂**的目标。

**采纳 (b) 后的定稿判据**（全部**机检**，由 `scripts/semantic_schema_shadow_diff.py` 产出）：

| # | 判据 | 口径 | 退出码影响 |
|---|---|---|---|
| 1 | **内容等价 = 0** | 逐**类块**比行多重集（不只是全局多重集——行从类 A 搬到类 B 也是内容差异） | > 0 ⇒ 退出码 1 |
| 2 | **归因缺口 = 0** | 块级内容不等价的块 **+** 顺序差异落在已知差异清单外的块 | > 0 ⇒ 退出码 1 |
| 3 | **块顺序差 = 0** | 块与块之间的先后差（=「哪个类先出现」变了） | > 0 ⇒ 退出码 1 |

**「已知差异」的清单是封闭的**，只有两类（脚本按归因分类自动产出，不写死）：

- **类内列序**（类块内，列与列的先后）；
- **JOIN 条目序**（`### JOIN 关系` / `### 间接 JOIN 路径参考` 段内，条目与条目的先后）。

出现第三类 ⇒ 归因缺口 > 0 ⇒ 退出码 1（**必须修**，不许并进「已知差异」）。

> ⛔ **封闭性必须「结构上」成立，不能只靠当天的数据成立**（D14 自查发现并已修，
> 详见 `daily/D14.md` §4.3）：若按「不是 JOIN 段 ⇒ 类块」做**排除法**分类，
> 任何**新段落**（渲染器里已有「关键过滤口径摘要」「间接 JOIN 路径参考」两段
> 非类块段落）的顺序差异都能借「类块」的名义混进已知差异清单 ⇒ 第三类差异
> 绕过退出码 1。⇒ 现在的分类是**正面认类块**（类块标题恒带 `: table=`），
> 认不出的块一律进**归因缺口** ⇒ 退出码 1（吵闹），而不是静默放行（安静）。


**「快照侧类名集合 == 治理表侧类名集合（顺序亦一致）」的口径澄清**：这里的「顺序」指**渲染出来的块顺序**，不是 manifest 内部的类存放顺序——复原层按**调用方的召回子集顺序**复原（`restoreSnapshotSchema` 保留 `classNames` 顺序），manifest 内部顺序**到不了输出**。故判据落成「块顺序差 = 0」。

**实测（dev 5433 / `qa_metadata`，只读）**：

| 模式 | 内容差异 | 归因缺口 | 块顺序差 | 顺序差异归因 |
|---|---|---|---|---|
| `none`（全集，唯一完整覆盖 JOIN 段） | **0** ✅ | **0** ✅ | **0** ✅ | 类块 37 + JOIN 段 1 |
| `each`（37 个逐类子集 = 生产召回口径） | **0** ✅ | **0** ✅ | **0** ✅ | 类块 37 |

影响面评估见 `daily/D14.md`（含残余风险登记 R-1 / R-2）。

---


## 5. D12–D17 任务卡

> 沿用 1109 §0.1 约定：每天一个可验证产出；日终证据落 `daily/Dnn.md`；
> 不允许静默跨日；不压缩验收步骤。

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| **D12** | ① 建 worktree + 分支 + venv；② 按 §2.2 抓当前 main 的「直读 schema 文本」基线 fixture；③ 按 §2.1 跑通真实发布链路拿一行 ACTIVE 快照；④ **id 合成层**（§3.3 方案 A）：`className → synthetic_id` 稳定映射 + camelCase→snake_case 字段映射 + `refClassName`/`parentClassName` 反查；⑤ 复原对象工厂（普通 class，**带 `__dict__`**，不用 slots）；⑥ 适配器 `loadClassesForSchema(domain?)`：**按召回子集的 `class_name` 过滤 manifest**；开关关 / 无发布 ⇒ **直读治理表（与现状逐字一致）** | 适配器 + id 合成层 + 双路径单测 + 基线 fixture + `daily/D12.md` | 关闭态输出与**当前 main** 逐字一致（fixture 对照）；ACTIVE 快照已入库且 `manifest_hash` 校验通过；**子集大小不因快照而变**（断言条数与类名清单） |
| **D13** | 影子比对工具 `scripts/semantic_schema_shadow_diff.py`：同一本体生成「快照 schema 文本」与「直读 schema 文本」，差异落报告；测试环境跑首轮并归档。**评审增补**（2026-11-15）：① 退出码三态（0 = 内容 0 **且** 顺序 0 / 1 = 有内容差异 / 3 = 内容 0 但顺序 > 0）；② **子集模式** `--subset-mode each\|groups`（生产渲染的是召回子集，只比全集发现不了子集语义下的偏差）；③ 基线只在**比对后且内容差异 = 0** 时写 | 脚本 + 首轮报告 | 报告含差异条数与逐条定位；报告非空（否则说明比对没真跑）。**D12 已顺带产出**：`scripts/semantic_schema_shadow_diff.py` + `daily/D13-shadow-diff-round1.md`（内容差异 0 / 顺序差异 6524 行 / 类集合 37=37）；改写后复跑：全集与 **37 个逐类子集**内容差异均 = 0 |
| **D14** | 差异归零：只修快照渲染（排序 / 格式 / 缺字段），**不改 join 图 / 采样 / digest 算法**；补 §4-③ 的字段等价 golden + §4-⑦ 的**召回子集不变量**断言（条数 + 类名清单逐项相等）。**⛔ 判据按 §4-⑩ 拆成两个指标**：内容等价（行多重集）必须 = 0；顺序等价需逐条给出结论（出路见 `daily/D12.md` F4 的 (a)/(b)/(c)）。**golden 已提前到 D12 落地**（评审要求：D15 接线前必须先钉死 ORM-vs-复原等价）。**✅ 11-15 完成**：决断采纳出路 (b)，三条判据（内容等价 / 归因缺口 / 块顺序差）全部机检、全部实测 0；**无需改渲染**（内容差异本来就是 0） | 修正提交 + 归零报告 + golden | **内容差异 = 0**（首轮已是 0，改写后逐类子集亦为 0）；golden 钉死窄字段清单（`app/tests/unit/test_semantic_schema_equivalence.py`，14 条，**D12 已落地**）；**快照侧类名集合 == 治理表侧类名集合**（顺序亦一致）；顺序差异要么归零、要么显式记录为已知差异并给出影响面评估 —— **实测：内容等价 0 / 归因缺口 0 / 块顺序差 0；已知差异封闭为「类内列序」「JOIN 条目序」两类（第三类 ⇒ 退出码 1）**。证据 `daily/D14.md` |
| **D15** | 接入主路径：`nl2sql_service` / `nl2sql_plan` 经适配器取类（**仅 schema 文本路径**，见 §3.2）；开关 `ENABLE_SEMANTIC_REGISTRY_NL2SQL` 默认关 | 接入提交 + 定向测试 | 开关开/关两态下既有 NL2SQL 测试全过；反向守卫：开关开但快照缺字段 ⇒ 报错而非静默降级 |
| **D16** | 运行记录钉住发布：加可空 `semantic_release_id`（落点现场确认，见 §4-④）+ 重放一致性测试；手写迁移（禁 autogenerate、只加法 DDL）+ downgrade 往返验证 | 迁移 + 测试 | 同一请求重放读取同一发布；空值兼容旧数据；迁移 upgrade→downgrade→upgrade 往返通过 |
| **D17** | 内部切换演练（测试环境）+ chat/multistep 定向回归 + PR + 变更记录 | 演练报告、PR、`Harness/changes/feat-semantic-registry-nl2sql/summary.md` | 失败集合净新增 0；**G3 通过** |

### G3 判据（沿用 1109）

影子差异 0；切换前后回放结果一致；SQL Guard 链原样；开关可秒级回落。

---

## 6. 门禁与合并判据

| 项 | 口径 |
|---|---|
| 合并判据 | **CI 失败集合对差净新增 0**（不要求全绿；main 基线本身是红的）。`cancelled` / `UNUSABLE_JOBS` ⇒ 整轮复跑 |
| 基线取法 | **PR 事件派发前**最后一次完成的 main run（不能随手拿一个，否则分片轮转造成的计数变化会被误判成净新增） |
| 新增 integration 测试文件 | 会导致分片按「排序下标 mod 4」轮转 ⇒ 失败**计数**变化不是净新增，判据看 **nodeid 集合** |
| 迁移 | 手写、禁 autogenerate、只用加法 DDL、必须做 downgrade 往返验证 |
| 开关 | 一律默认关闭；P2 不部署生产 |
| 评审 | 写完立即 `code-reviewer` + `security-reviewer`；CRITICAL/HIGH 必须清零 |
| 探针文件命名 | 不能叫 `test_ci_*`（会落进 CI 面的对差口径） |

---

## 7. 每日证据与提交规范

- 日终证据：`Harness/changes/feat-semantic-registry-nl2sql/daily/Dnn.md`
  （当日做了什么 / 证据 / 未完成项 / 明日依赖）。
- 提交：conventional commits；**只 `git add` 本批文件**，禁 `git add -A`
  （工作树里有其他批次的未跟踪文件：`docs/update/`、`scripts/apply_term_dictionary_id5.sh`、
  `Harness/changes/2026-11-14-prod-release-1114/` 等）。
- 合并后不部署；需要生产 DDL 的时机单独评审。

---

## 8. 不在本计划内（明确划界）

| 项 | 处置 |
|---|---|
| **WP-ONT-AI《AI 驱动本体发现与持续演进》** | 1109 文档自述「需经过变更评审，不默认计入原有工期与工作量」⇒ **另立变更**，不塞进 P2。该 md 目前在仓库根目录未跟踪，建议移入 `docs/update/1109/` 后随文档批次入库 |
| P3a RAG 过滤 / P3b Neo4j 镜像 / P3c Agent 快照 / P3d 其余调用点 | P2 之后，按 1109 排程顺序；P3d 的 `agent_tools_nl2sql`（`:124`/`:202`）与 `chat_recall`（`:460`）取类点**留给 P3d**，P2 只碰 `chat_service` 主路径 |
| `0141` / `0142` 生产 DDL | 独立评审项（逐条 SQL 过目），不在 P2 PR 内 |
| 术语词典 / KPI 治理的未决项 | 属另一条线（#118–#126 已合），不混入 P2 |
