# 变更：fix-dim-facility-connectivity

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **Phase**：NL2SQL 计划阶段 —— JOIN 连通性
- **状态**：代码完成 + 已部署 + 真机验收通过（2026-10-03）
- **关联变更**：[../../wiki/model-router.md](../../wiki/model-router.md)、
  [fix-reasoning-model-token-budget](../fix-reasoning-model-token-budget/summary.md)
- **迁移版本**：无（本变更不含 DDL）
- **提交**：（待提交时回填）
- **手工操作指引**：[manual-steps.md](./manual-steps.md)

---

## 1. 需求

同一问题，deepseek 正常、MiniMax-M3 整步报废：

> 分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，
> 第二步按收货地点拆分各月供货量，第三步对照同地点其他供应商的供货量变化……

第 2 步报：

```
该步骤查询生成失败：以下表无法通过关联路径连通：DIM_FACILITY，请通过中间表建立 JOIN
```

## 2. 根因（三层，逐层实测确定）

### ① 元数据：`DIM_FACILITY` 是零边孤岛（用户已手补）

`ontology_join` 全库 81 条边，触及 `DIM_FACILITY` 的**0 条**；
`DWD_GOODS_RECEIPT_DTL.RCV_SITE_CODE` 的 `ref_class_id` 为 NULL（外键未建）。

> 2026-10-03 只读复查 prod：属性级外键**用户已补 2 / 4**
> （`DWD_GOODS_RECEIPT_DTL` 的 `RCV_SITE_CODE` / `COM_CODE` 已 `fk=true, ref=1`），
> 但 `DWD_PURCHASE_ORDER_DTL` 的两条未做；且 `DIM_FACILITY.FCY_0` 的
> **主键标记从未落库** —— 该类 117 个属性 `is_primary_key=true` 的为 0，
> 而全库另有 49 个 PK 都在别的类上（不是功能坏了，是这一条没提交成功）。
> 剩余 3 项清单见 `manual-steps.md` §0.1。

⇒ `validateConnectivity` 对**任何**含 `DIM_FACILITY` + 其他表的计划必然失败。
⚠️ 报错文案说「请通过中间表建立 JOIN」——**该建议无法满足**，中间表不存在。

**用户已手补 4 条边，真实 Oracle（THBI 19c）值域验证全部 1.000**：

| 边 | 事实侧 distinct | 维度侧 distinct | 交集 | 重叠率 |
|---|---|---|---|---|
| `DWD_GOODS_RECEIPT_DTL.RCV_SITE_CODE → DIM_FACILITY.FCY_0` | 23 | 41 | 23 | **1.000** |
| `DWD_GOODS_RECEIPT_DTL.COM_CODE → DIM_FACILITY.LEGCPY_0` | 10 | 27 | 10 | **1.000** |
| `DWD_PURCHASE_ORDER_DTL.PUR_SITE_CODE → DIM_FACILITY.FCY_0` | 18 | 41 | 18 | **1.000** |
| `DWD_PURCHASE_ORDER_DTL.COM_CODE → DIM_FACILITY.LEGCPY_0` | 11 | 27 | 11 | **1.000** |

对照组 `RCV_SITE_CODE ∩ FCYNAM_0 = 0`（0.000）证明选列正确、非「全等」假阳性。

#### ⚠️ 但这 4 条边里有 2 条是**扇出边**，必须撤（2026-10-03 复核发现）

值域重叠率 1.000 只证明「每个值都能找到」，**不证明 JOIN 是 1:1**。
补完边后复核 `DIM_FACILITY` 的真实键：

```sql
-- Oracle 数据字典（all_constraints / all_cons_columns）
UK_DIM_FACILITY_LEGCPY_FCY  constraint_type=U  ENABLED
  ├ position 1 → LEGCPY_0
  └ position 2 → FCY_0
主键(P)约束：0 个
```

即**复合唯一键，不是单列主键**。实测各键的选择性：

| 列 | `DIM_FACILITY` 行数 | distinct | 单列唯一？ |
|---|---|---|---|
| `FCY_0` | 41 | **41** | ✅ **唯一**（重复组 0） |
| `LEGCPY_0` | 41 | 27 | ❌ **最多一个公司有 10 个工厂** |

行数实测（真库，不是估算）：

| 边 | 事实表行数 | JOIN 后行数 | 倍数 | 判定 |
|---|---|---|---|---|
| 收货单 `RCV_SITE_CODE → FCY_0` | 3,558,004 | 3,558,004 | 1.00× | ✅ 安全 |
| 采购单 `PUR_SITE_CODE → FCY_0` | 927,631 | 927,631 | 1.00× | ✅ 安全 |
| 收货单 `COM_CODE → LEGCPY_0` | 3,558,004 | **31,575,628** | **8.87×** | ❌ **扇出** |
| 采购单 `COM_CODE → LEGCPY_0` | 927,631 | **8,082,340** | **8.72×** | ❌ **扇出** |

后果：**任何走这两条边的聚合，供货量 SUM 会被放大约 9 倍**。
这是静默错答（HTTP 200、数字看着合理、量级离谱），比原来那个连通性报错更危险。

复合键修法实测：

| 方案 | 收货单 | 采购单 |
|---|---|---|
| 复合 `(COM_CODE, RCV_SITE_CODE) → (LEGCPY_0, FCY_0)` | 3,557,990（丢 14 行 = 0.0004%） | 927,618（丢 13 行） |
| 单列 `RCV_SITE_CODE → FCY_0` | 3,558,004（无损） | — |

丢的 14 行是「工厂码能匹配上、但公司码对不上」的少数派。

⇒ **建议**：保留 2 条 `*_SITE_CODE → FCY_0`（`FCY_0` 单列即超键，已无损）；
**撤掉 2 条 `COM_CODE → LEGCPY_0`**。若业务上确实需要「按公司拆」，
改用 `DISTINCT` 或复合边，不要用单列公司码 JOIN。

⚠️ 这也**推翻了本文档原先的操作建议**：属性级 FK 标记的 prompt 渲染是
`FK → 目标表`、**不指出目标列**（`nl2sql_schema.py:311-312`）。
给 `COM_CODE` 打 FK 标记等于在 prompt 里**邀请 LLM 去连那条 8.9 倍扇出的列**，
所以 `COM_CODE` 侧（属性 `702` / `835`）的 FK 标记应当**撤销**，
只有 `*_SITE_CODE` 侧（`703` / `836`）才该标。

### ② 校验逻辑：连通性失败**不可自愈**（本次修复）

`generateValidatedPlan` 的重试循环只跑 `validatePlan`（属性归属），
连通性检查在循环**之外**的 `_finalizePlan` 里，失败直接 `raise`，
**不回灌 `initialErrors`**。真机 traceback 印证：

```
nl2sql_service.py:525 in generateValidatedPlan → return self._finalizePlan(...)
nl2sql_plan.py:346  in _finalizePlan         → raise Nl2SqlError(
```

同一道闸门两套语义：属性失败给 LLM 重试机会，连通性失败直接终局。

### ③ Prompt：孤岛类**零信号**（本次修复）

`### JOIN 关系` 段只渲染有边的行，孤岛类**整段缺席**。
「缺席」不等于「不可 JOIN」，schema 文本无任何文字说明 ⇒ 模型把缺席读成了「可以试着连」。

### 为什么 deepseek 能过 —— 不是模型强弱

查 `session_query_state` 全部成功轮次的 SQL：**没有一次 join 过 `DIM_FACILITY`**，
一律用裸 `d.RCV_SITE_CODE` 分组。deepseek 是**绕开**而非**连通**。
分组口径与走维度表等价，所以「deepseek 能跑」为真，但机制不同。

⚠️ 本条结论基于库内存储 SQL 的取证。若用户手上有 deepseek 计划里确实出现
`DIM_FACILITY` 的截图，此结论需修正。

## 3. 变更内容

### 3.1 连通性失败回灌重试（`nl2sql_service.py`）

新增 `_planIssues(plan, classes, joins, *, ownerHintMaxClasses)`：把
`validatePlan` 与 `validateConnectivity(supplementJoinPath(...))` 合成一个 issues 列表。
`generateValidatedPlan` 的循环改为统一消费它，连通性错误与属性错误同等回灌 `initialErrors`。

关键点：

- **语义等价性已验证**：`attempts = max(1, maxPlanAttempts)` 循环，
  校验次数与 LLM 调用次数均与改造前一致；`maxPlanAttempts` 为 0/1 的边界行为不变。
- **属性级错误优先返回**：计划马上会被重写，再补 JOIN 算连通性是白算。
- `_planIssues` 保持**同步**（方法体内无 await），`planCfg` 只传 `ownerHintMaxClasses`
  一个标量而非整个 dict（私有方法不隐式依赖配置表形状）。
- 删除了原循环外那条 `计划校验最终失败` 日志 —— `attempt=attempts` 已是同一事件的最终留痕，
  保留会重复打。已确认无测试/文档断言旧文案。

### 3.2 Prompt 孤岛标记（`nl2sql_schema.py`）

新增 `_islandTables(classes, joins)` + 常量 `_ISLAND_MARKER`，
`buildSchemaText` 对召回集内零边类在类头追加 `[无关联边，不可跨表JOIN]`。

**与 `validateConnectivity` 严格同口径**：图建不起来（`joins` 为空 / 所有边两端都不在召回集）
时校验本就放行（`validateConnectivity` 首行 `if not graph: return []`），
此时**一个类都不标** —— 否则 prompt 会劝退一个校验根本不拦的类，
口径分裂比标错更糟。

⚠️ 实现过程中我**先写错了测试**：原用例断言「边的两端不在召回集时召回集内的类仍应被标」，
与上述同口径原则矛盾。改的是测试不是实现。

成本实测：**11 tokens / 每个孤岛**。真机该步召回集剩 2 个孤岛（class 30/31），
即 +22 tokens ≈ schema 文本（~6.4k tokens）的 **0.34%**。

### 3.3 顺带修掉的真缺陷：`validateConnectivity` 点错表名

回归阶段发现 —— 这个缺陷**早于本次变更就存在**，但因为连通性错误从不回灌给 LLM，
点错名字没有后果；本次把它接进自愈回路后，后果从「消息不好看」升级为
「LLM 照着错误提示砍掉错误的类」，**本可自愈的失败变成死局**。

**症状**：同一个失败计划，报出的「不连通表」在不同进程里**不一样**。

```
PYTHONHASHSEED=0 => passed
PYTHONHASHSEED=1 => failed      ← 同一份代码、同一份数据
```

**根因**：旧实现

```python
start = next(iter(tablesWithSource))   # ← 从 set 里随便取一个
visited = {start}
... BFS ...
disconnected = tablesWithSource - visited
```

`tablesWithSource` 是 `set[str]`，**字符串 set 的迭代顺序受 `PYTHONHASHSEED` 影响**。
起点若落在孤岛 `T_FACILITY` 上，BFS 只 visits 到它自己，于是报
「无法通过关联路径连通：**T_RECEIPT**」—— 点的是唯一连通的那个表，
真正的孤岛反而没被点名。

**修复**：改为求连通分量，取「主分量」，其余分量才是问题表。三个关键点：

1. **遍历必须走整图**，不能只在选中的表里走 —— `supplementJoinPath` 允许经
   **未被选中但在召回集内**的类中转（那正是「中间表」）。连通性判定若不许中转，
   就会把「能绕过去」误判成「不连通」，与补边逻辑自相矛盾。
2. **排序键是整图可达节点数，不是选中的表数**（这条我第一版写错过）：
   只数选中的表时，孤岛（1 个选中）与「事实表 + 中转维度表」（选中 1 个、
   经中转可达 2 个）**同大小**，字典序最小的孤岛反而当选主分量 —— 正好选反。
3. **确定性**：起点按字典序遍历 + 严格大于才替换 ⇒ 与 hash seed 无关。
   报错里的表名也加 `sorted()`。

守卫用例 `test_connectivity_failure_self_heals` 现在同时断言
「点名 T_FACILITY」**且**「不出现 T_RECEIPT」；另有参数化用例在
8 个 `PYTHONHASHSEED` 子进程里复跑，锁死确定性。

> 教训与记忆 `sdd-plan-stale-count-anchors` 同源：**断言要锚在稳定身份上**。
> 这里的「身份」是**报错点名的表**，它此前是不稳定的 —— 测试通过与否取决于
> 进程启动时的 hash seed。若当时只看「单跑一次绿」就收工，这个 bug 会带着
> 「测试全绿」的假象进生产。

### 3.4 界面补「引用类」选择器（前端）

**起因**：排查中发现外键 Checkbox 早就存在（`PropertyTab.tsx:342-346`），
但没有引用类选择器 ⇒ 勾了外键也填不了指向谁，属性停在 `ref_class_id=NULL`。
DQ 规则生成器的报错文案本身就写着「请到本体属性管理页设置 ref_class_id」
（`data_quality_rule_generator.py:242`）—— 说明**那页本就该有这个字段，是缺口不是有意省略**。

| 文件 | 改动 |
|---|---|
| `PropertyTab.tsx` | 引用类下拉（复用 `classOptions` helper）+ 提示文案 + 回填 + payload |
| `types/ontology.ts` | `OntologyPropertyUpdate.refClassId?: number` → `number \| null` |
| `i18n/{zh-CN,en-US}.ts` | 各 5 个 key |

两个必守的实现约定：

1. **清空引用类发 `null` 不发 `undefined`** —— 后端 `OntologyPropertyUpdate` 走
   `exclude_unset`，`undefined` = 「不修改」，用户清空选择器后旧引用类残留。
   与 `disableThinking` 的 `false`/`undefined` 是同一类坑。
2. **勾外键不选引用类 → 拦截保存** —— 防造出 `is_foreign_key=true + ref_class_id=NULL`
   这个会让 DQ 规则生成器把属性列进 `blocked[]` 的组合。

下拉列出**全部**类而非按 JOIN 边收窄：外键语义独立于 JOIN 边，
有些外键只服务参照完整性校验、不参与 NL2SQL 跨表关联。

## 4. 测试

| 文件 | 用例数 | 覆盖要点 |
|---|---|---|
| `unit/test_schema_island_marker.py` | 8 | 孤岛被标 / 连通类**不**标（双向）/ joins 为空不标 / 图空不标 / 部分召回 / 标记非禁用语 |
| `integration/test_connectivity_retry.py` | 4 | 连通性失败自愈（第 2 轮换类，且**点名孤岛而非连通表**）/ 耗尽重试抛错 / `supplementJoinPath` 幂等 / `_finalizePlan` 守卫仍在 |
| `frontend/src/tests/PropertyTab.test.tsx` | +4 | 回填 / **清空发 null 而非 undefined** / 勾外键无引用类被拦 / 选中后带上 id |

### 测试作者踩过的坑

- **下拉标签是「别名（物理名）」，不含 `source_table`** —— `classOptions` →
  `ontologyObjectLabel`（`frontend/src/utils/ontologyLabel.ts:12-22`）。
  测试里 mock 的别名是「工厂」、类名是「Facility」，所以断言写的是 `工厂（Facility）`；
  **真库上 `DIM_FACILITY` 显示的是「公司及工厂的信息（DIM_FACILITY）」**。
  按物理表名断言必挂；手册里也不能照抄测试的 mock 文案。
- **「FK 仍勾选时清空引用类」被守卫正确拦下** —— 该场景非法（正是守卫要防的组合）。
  改用「FK 已关、引用类残留」的真实收尾场景测 `null` 语义。
- `listPropertiesByClass` 对多个类返回同一份属性 ⇒ `getByText` 命中多行而超时。
- **单跑绿 ≠ 稳定**：`test_connectivity_failure_self_heals` 单独跑必绿，
  跟别的套件一起跑才红 —— 因为 Python 进程每次启动的 hash seed 不同。
  凡是断言「报错内容」的测试，都要复跑多个 seed 才算数。

## 5. 回归证据

`app/tests/unit` 全量（真实 PG `qa_metadata_test`，`localhost:5434`，串行）：

```
48 failed, 3555 passed, 1 skipped in 862.79s
```

⚠️ 48 这个红是**既有基线**，不是本次引入。与 HEAD 基线 worktree 逐条比对身份：

```bash
grep -E "^(FAILED|ERROR)" /tmp/unit_final.txt | sed 's/ - .*//' | sort -u > /tmp/ids_final.txt
diff /tmp/ids_base.txt /tmp/ids_final.txt     # → 空
```

**48 条身份逐条完全一致 ⇒ 零回归。**（只比计数不算证据 —— 记忆
`sdd-plan-stale-count-anchors`：计划里的过期计数会被后续任务拿当真。）

前置的定向套件：`unit/test_nl2sql_plan_gate.py` + `test_nl2sql_service.py` +
`unit/test_schema_island_marker.py` + `integration/test_connectivity_retry.py`
⇒ **306 passed**。

前端：`PropertyTab.test.tsx` 13 passed；相关三套件 52 passed；
`npm run build`（含类型检查）通过；`docker compose build --no-cache frontend`
后镜像已确认含新代码（容器内 bundle 命中 `refClassRequired`）。

确定性守卫：`test_connectivity_failure_self_heals` 在 8 个 `PYTHONHASHSEED`
子进程下全绿。

## 6. 部署与验收

### 6.1 手工元数据操作（界面级手册见 `manual-steps.md`）—— ✅ 已于 2026-10-03 执行完毕

全部走 API（`DELETE /ontology/joins/{id}` ×2 + `PUT /ontology/properties/{id}` ×3），
不走 SQL，以保留审计写入。落库复核：

- [x] **A. 删除扇出边 83、84** → `ontology_join` 85 → **83** 条；
      触及 `class_id=1` 的只剩 **82、85**（源列均为 `FCY_0`）
- [x] **B. 撤销属性 702 的 FK** → `is_foreign_key=false, ref_class_id=NULL`
- [x] **C. 补属性 836 的 FK** → `is_foreign_key=true, ref_class_id=1`
- [x] **D. 补属性 2 的主键** → `is_primary_key=true`
- [x] 属性 703 保持 `fk=true, ref=1`（原本就正确）；属性 835 保持无 FK（有意为之）

最终状态（与 `manual-steps.md` §7 自查表逐格一致）：

| ID | 类 · 列 | pk | fk | ref |
|---|---|---|---|---|
| `2` | DIM_FACILITY · `FCY_0` | ✅ t | f | - |
| `702` | 收货单 · `COM_CODE` | f | ✅ f | - |
| `703` | 收货单 · `RCV_SITE_CODE` | f | t | 1 |
| `835` | 采购单 · `COM_CODE` | f | ✅ f | - |
| `836` | 采购单 · `PUR_SITE_CODE` | f | ✅ t | 1 |

回滚脚本：[`rollback-metadata.sql`](./rollback-metadata.sql)（含边 83/84 的完整原始行）。

⚠️ **Neo4j 未同步，且本来就不完整**：图里只有 **8 条 JOIN 边**（PG 83 条），
`JOIN` 关系**不携带列信息**，`Class` 节点大多无属性。
NL2SQL 读的是 PG，本次修复不受影响；但图不能用作 JOIN 真相源。

### 6.2 代码部署

- [x] `./scripts/deploy_backend.sh` → 快照 `backups/container/20261003_190343`，`Application startup complete.`
- [x] 容器跑的是新代码：`nl2sql_plan.py` / `nl2sql_schema.py` / `nl2sql_service.py` /
      `schemas.py` / `models.py` 宿主与容器 **SHA-256 逐一一致**；无 `/app/app/app` 嵌套
- [x] `docker compose build --no-cache frontend`（已在开发机完成，容器内 bundle 已验证含新代码）
- [x] 无 DDL；prod `alembic current` 已是 `0109 (head)`，重启未应用新迁移

### 6.3 真机验收（2026-10-03，原句 + datasourceId=1）

| 模型 | step1 SQL | error | 行数 |
|---|---|---|---|
| MiniMax-M3（modelId=9） | `JOIN THBI.DIM_FACILITY f ON f.FCY_0 = d.RCV_SITE_CODE` | None | 43 |
| deepseek（modelId=1） | **无 JOIN**（仍按裸 `d.RCV_SITE_CODE` 分组） | None | 43 |

- ✅ 第 2 步成功，且 `f.FCYNAM_0 AS SITE_NAME` 让地点显示为可读名称而非 `C1`/`D9` 编码
- ✅ **两个模型的 SQL 都不含 `COM_CODE = LEGCPY_0`**（扇出边已撤，未被绕回来）
- ✅ **数值层无扇出**（比看 SQL 更硬的证据）：

  | 模型 | step0 各月合计 | step1 按地点拆分合计 | 比值 |
  |---|---|---|---|
  | MiniMax-M3 | 43,445,269 | 43,445,269 | **1.000000** |
  | deepseek | 43,445,269 | 43,445,269 | **1.000000** |

  JOIN 维度表后 43 行拆分的总和与不 JOIN 的 9 行月度总和**分毫不差**。

- ✅ **deepseek 行为未变**（仍不 JOIN）—— 再次实证「deepseek 能跑是因为**绕开**，
  不是因为连通」。本次它恰好给出了与走维度表**相同的口径与数值**。

⚠️ **自愈路径本次未被走到**：`计划校验未通过` 在日志中出现 **0 次**。
元数据修好后 MiniMax 第一轮就选对了表，压根没触发连通性失败 ——
这是更好的结果，但**不构成自愈代码在真机生效的证据**。
自愈的证据只有集成测试（`test_connectivity_failure_self_heals`，含 8 个 hash seed）。
要真机触发，需构造仍处孤岛的场景（如 `DWD_CARRIER`），本次未做。

（附带观察：日志出现 2 次 `PLAN_REPLY_JSON_INVALID` / `PLAN_REPLY_EMPTY`，
属 JSON 解析层重试，已自行恢复，与本变更无关。）

## 7. 不在本次范围

- **其余 5 个非 ODS 孤岛**：`DWD_CARRIER` /
  `DWD_PURCHASE_QUOTATION_YEARLY_DTL` / `DWD_SUPPLIER_PAYMENT_LINE` /
  `DWD_SUPPLIER_PRICE_LIST_CONFIG` / `DWD_SUPPLIER_PRICE_LIST_HEADER`。
  代码层的自愈 + 孤岛标记能兜住，补边才是治本。
- ~~`DWD_BUSINESS_PARTNER` / `DWD_CUSTOMER`~~：实际是软删除墓碑（validTo 非空），
  已在 `fix-class-tombstone-restore` 单独处理（提供 restore 端点 + 预览期占名提示），
  不再算孤岛。
- **`probeDeadEdges` 在 Oracle 上必然 500**：`ORA-00933` —— 探针 SQL 用了 `LIMIT 10000`
  （`ontology_join_health_service.py:135,138`），Oracle 19c 不认。
  后果：**系统自带的死边巡检工具在唯一真实数据源上是坏的**，无法用它验证新补的边。
  本次 4 条边是另写探针在真库上验的。
- **`backfillRelations` 会造自引用边**：`SAGE_X3_REFERENCE_MAP["FCY_0"] = ("FACILITY","FCY_0")`，
  而 `FCY_0` 就在 `DIM_FACILITY` 自己身上，循环里没有「源类 == 目标类」保护。
  且该映射表不含 `RCV_SITE_CODE` / `PUR_SITE_CODE` / `COM_CODE` / `LEGCPY_0`，对本问题无效。
  ⚠️ **不要点「一键补关系」**。
- **月度缺月无守卫**：需先查清 THBI 数据现状。

## 8. 相关记忆

`qa-system-dim-supplier-join-island`（孤岛事故的原始记录）、`qa-system-thinking-off-degrades-plan`、
`qa-system-probe-authoring-gotcha`、`qa-system-guard-false-positive-tests`、
`qa-system-ontology-property-admin`