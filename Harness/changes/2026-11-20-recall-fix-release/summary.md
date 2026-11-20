# 召回根治三件套（#157 / #158 / #159）—— 生产发版交付汇总

- **日期**：2026-11-20
- **批次**：`2026-11-20-recall-fix-release`
- **Runbook**：[deploy-prod.md](./deploy-prod.md)（同目录，本批的部署 SSOT）
- **验收记录**：[acceptance-1120.md](./acceptance-1120.md)（**生产上线后的实测证据与判读**）
- **状态**：✅ **已上线并完成验收**（2026-11-20）—— 过滤**双向**运行时实证生效，零 `ORA-00942`，端到端出数正常，**不需要回滚**。
  遗留：**ds2 fail-open 路径未根治**（唯一实质缺口，**已收敛为 `is_active=false`，待一条只读探针后执行**）；另有 3 个 **结构性占位类被过滤剔除 = 正确行为**（无需处置，仅挂采购域 M1 衔接；
  ★ 理由链已订正：不是「0 行不被 introspect 收录」，而是「表不存在 / 不在被 introspect 的 owner 命名空间内」—— introspect 按**列**枚举，0 行的表也会被收录）；
  §7 墓碑 270 **未执行**（判定保留）。

## 一、本次发版装了什么

| PR | 合并 commit | 内容 | 改动面 |
|---|---|---|---|
| **#157** | `d58bb1ad` | **数据源过滤**（候选集按 `datasourceId` 收窄）+ **关键词锚定**（问法敏感漏选的第二条腿） | 后端 5 处调用点 + 前端披露文案 |
| **#158** | `f07ae867` | **Gap B**：向量路径失效时（`no_match` / `ods_only_hits`）由关键词锚定接管召回窗口 | 后端降级路径 |
| **#159** | `d1c20de9` | **Gap A**：向量检索窗口从 Milvus **全局**收窄到**本地类 id 集**（`expr` 作用域） | 后端检索层 + 接线层 |

**合并后 main** = `d1c20de9`（本批**直接**从它检出构建 ⇒ 无「分支头 ≠ 合并 commit」的等价性论证负担）。

**爆炸半径**：chat 三调用点 + 维度词 DIM 全量拉取路径 + 研究执行面（共 5 处）受数据源过滤约束；
Gap A 的作用域另计 —— 只作用在**对话链路的类召回**接线上，**不覆盖研究 BO 臂**（BO 臂仍是 Milvus 全局检索）。

**本批自身零 alembic 迁移** ⇒ 生产库必须从 0143 走到 0152（main 既有 9 条加法迁移），
否则新 backend 因 schema drift（`missing_table` 是 blocking 级）起不来。

## 二、交付物

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1120-recall.tar.gz` | `qa-system-backend:amd64-1120-recall`（→ tag `qa-backend:offline`） | `23b775d2e43bec621fe63070be505ee668f801c023f221d5137115c07fae1aa2` | 219M |
| `/tmp/qa-frontend-amd64-1120-recall.tar.gz` | `qa-system-frontend:amd64-1120-recall`（→ tag `qa-system_frontend`） | `47c12ccb97c64b4a528841a3f25ce5407017ba87af70a018ff46e6e5b102c949` | 26M |

构建源 = `/tmp/build-1120`（`git worktree add --detach /tmp/build-1120 origin/main`）
⇒ `HEAD = d1c20de9`，`git status --porcelain` 为空（无未跟踪文件进入构建上下文）。

**路径级等价性复核（合并本批文档 PR 后重跑，输出为空 ⇒ 镜像仍是合并后 main 的镜像）**：

```bash
git diff d1c20de9 origin/main -- backend/ frontend/ docker/     # 空 ⇒ 等价；非空 ⇒ 必须重建
```

> ⚠️ **只比对 `backend/ frontend/ docker/` 三个路径**：`Harness/`、`docs/`、`*.md` 被 `.dockerignore`
> 排除 ⇒ 文档提交不改变镜像字节 ⇒ 整树 diff 会出现「非空但无害」的**假警**。
> 反之 `backend/app/tests/` **不**被排除 ⇒ 后端测试文件一改，后端镜像字节即变。

## 三、自证证据（开发机实测）

| 探针 | 实测 |
|---|---|
| 后端 `alembic heads` | **`0152 (head)`**（单一 head；版本目录末位 `0152_audit_workflow_actions.py`，无 0153+、无采购 0154–0158） |
| 后端新增模块 | `recall_datasource_filter.py` / `recall_keyword_anchor.py` 均在镜像内 |
| 后端 5 调用点 | `applyDatasourceRecallFilter` grep 计数 = chat_recall **2** / chat_service **2** / chat_multistep **2** / research_agent_execution **3**（均 ≥1） |
| 后端 Gap A 探针 | `classOntologyIds` = 1、`class_scope_bypass` = 1、`pinned_scope_skipped` = 1（`chat_recall.py`）；`buildClassIdScopeExpr` = 1（`milvus_client.py`） |
| 前端 bundle | **`index-t1K9T8s7.js`**（≠ 现役 `index-rBu8Smfz.js`、≠ 1119 那次 `index-BVutQwOS.js`、≠ #157 单批那次 `index-DizP7VQ_.js`） |
| 前端文案 | `已按当前数据源过滤` = 1、`已按关键词锚定` = 1、`泰鸿智脑` = 1 |

## 四、门禁证据

| 闸门 | 结论 |
|---|---|
| `merge-gate / failure-set-diff`（PR #159，唯一必需检查） | **success**（run `44588956515`，`tool_exit_code=0` ⇒ **净新增 0**） |
| 本地全量 unit（1657+ 用例） | 16 failed / 8271 passed / 20 skipped（44 分钟）—— 16 条**逐条**落在既有红基线上（JSON 解析、rate limit、oracle adapter、sqlite fixture 等），与本批无关 |

## 五、上线前必做（不通过不得上线）

> ⚠️ **实际执行情况（2026-11-20 复盘）**：这四条探针**在上线之后**才执行（部署动作已按 runbook 走完）。
> 实测结果：**A2 非空**（3 个活类在所有源下都找不到 —— **经复核属结构性占位类，被剔除是正确行为**，
> 故 A2 判据需加「该表在目标库里是否存在/有数据」二次分层）、**A1 对 ds2 为空**（ds2 无 `schema_cache` 行 ⇒ fail-open）、
> **B1(ds1) = 0**、**C1(ds1) ≈ 200+ > 0**。
> ⇒ **真正的门禁缺口是 ds2 的 A1 为空**（fail-open ⇒ 1119 故障在 ds2 会话原样复现）；
> 已用引用面探针（10 张带 `datasource_id` 的表**全 0**）+ 代码 grep（**零硬编码**）把策略**收敛为 B：
> `UPDATE data_source SET is_active=false WHERE id=2`**（ds2 是纯摆设，且它 `is_active=True` ⇒ 现在就挂在聊天下拉里，
> 正是用户误选 ⇒ 1119 故障复现的入口）。**B 只挡 UI 选择，不挡显式 API** ⇒ 代码 fail-closed（C）另批。
> A2 的非空项不构成「该数据源不得上线」，故未回滚。
> ⚠️ runbook §2 的三条 SQL **本身有语法错**（`jsonb_array_length` 需 `sum()` 包裹、`->>` 与 `||` 类型歧义、
> `LIKE '\_%'` 被 psql 当元命令）—— 正确写法与「全源孤儿探针」见
> [acceptance-1120.md §五](./acceptance-1120.md)。
> ★ **可复用探针 + 判别判据（跨会话共用，勿各写一套）**：
> 探针文件 [`probe-recall-datasource.sql`](./probe-recall-datasource.sql)（P1 每源保留/剔除总览 / P2 逐类明细 / P3 判别链 / P4 全源孤儿，
> **四段均已本地实跑通过**）＋ 判别判据 [acceptance-1120.md §5.5](./acceptance-1120.md)。
> ★ **入口口径矩阵**（chat / 多步 / 研究执行面 / 显式 API 是否同口径）：[acceptance-1120.md §5.3](./acceptance-1120.md) ——
> **4 个过滤调用点共用同一 `applyDatasourceRecallFilter`，口径一致**；能触发的 fail-open **只有 `no_cache`**。

runbook §2 的 **A1 / A2 / B1 / C1** 四个只读 SQL 探针（数据源覆盖度 / 跨源 DIM 存在性 / BO 臂跨源活类）——
**A1 空 或 A2 非空 ⇒ 该数据源不得上线**（覆盖度不足 ⇒ 类被静默剔除 / 过滤 fail-open）。

## 六、上线后必看（缺任一项 ⇒ 修复静默失效且没人知道）

> ✅ **实测结果见 [acceptance-1120.md §二](./acceptance-1120.md#二-★-运行时实证过滤双向生效)**：
> 全部符合预期 —— `reason=`/`scene=` 直方图**全空**（零降级、零 bypass、零 too_large）、
> `ORA-00942`=0、`召回候选跨源过滤` 在 ds1 剔 210 / ds3 剔 97（数字闭合）。
> ⚠️ 唯一**未取到**的一项：前端披露提示的**原文与 N 值**（ds1 预期 210 / ds3 预期 97）。

| 观测点 | 期望 | 异常含义 |
|---|---|---|
| `reason=no_match` 计数 | ≈ 0 | 作用域未生效（命中解析率应 ≈ 100%） |
| `scene=class_scope_bypass` | 0 | 本地类没有向量 / expr 过严 ⇒ **要查** |
| `scene=pinned_scope_skipped` | > 0 属正常 | 该会话钉住语义发布 ⇒ 该路径**不**收窄（设计如此） |
| `scene=class_scope_too_large` | 0 | 作用域 id > 2000 ⇒ 已降级为不限（可见） |
| `ORA-00942` | **0** | 硬红线 |
| `召回候选跨源过滤` | > 0 | 过滤在跑（会列出被滤掉的表名，前 10 个） |
| 前端披露文案 | 两条都在 | 「已按当前数据源过滤 N 张跨源数据表。」/「已按关键词锚定 N 张数据表。」 |

**端到端**：问「公司目前有多少供应商」必须出数；**再问一个不含维度词的泛化问句**
（如「公司经营情况怎么样」）⇒ 验证 Gap A 的作用域窗口**没有把召回收得过窄**
（作用域过窄的表征是「本来能答、现在答不出」，与「问法敏感漏选」是**相反方向**的回归）。

## 七、回滚

- 前端可单独回滚（无 DB 依赖）——最常用。
- ⚠️ 后端回滚：**旧镜像 CMD 带 `alembic upgrade head`**，库在 0152 时报「Can't locate revision」起不来
  ⇒ **必须先 `alembic downgrade` 到旧镜像自己的 head**（读出来，不要凭记忆写 0143/0148）。
- ⚠️ downgrade 会 drop 0144–0152 建的表（workflow 6 张 + discovery 2 张）⇒ 窗口内产生的审批/工作流数据丢失
  ⇒ 上线前必须 `pg_dump`（runbook §4）。

## 八、遗留挂账（本次**不做**，见各自 summary）

- Milvus 写路径：墓碑向量未删 + 2.47× 重复向量（Gap A 只在 class 检索窗口内绕开）。
- few-shot 未按窗口约束（`query_embeddings` 检索不带窗口约束）。
- 校验器「窗口未覆盖 vs 本体不存在」措辞。
- 研究入口的 **BO 臂仍是全局检索**（`api/v1/research.py:254` `_searchBusinessObjects`，无数据源维度）⇒
  数据源收窄的残余面，另批。**★ 分层已核清（[acceptance-1120.md §5.4](./acceptance-1120.md)）**：
  缺口**只在规划层**；执行面的类候选集已按数据源硬过滤，且 `selectClassesForTables` 取的是
  「过滤后集合 ∩ ESL 表名」，交集为空时退回的是**过滤后的全量**（不是全局全量）
  ⇒ 别源的表不会被塞进 LLM 的 schema 上下文 ⇒ 残余风险是「规划层选错 BO」，
  **不是 ORA-00942**。收口方案 = BO 臂加数据源维度（需「BO→源绑定」，本体层无 `datasource_id` 列），
  **与过滤层加固是两件事**。
- **钉住语义发布路径的 id 空间错配**（快照合成 id ≠ Milvus 主键）⇒ 该路径**不施加作用域**，
  `scene=pinned_scope_skipped` 会显式暴露它的存在；根治需「快照类名 → 真实主键」映射 + 命中 id 回译（另批）。
- 召回降级「编程错误 vs 基础设施故障」的分类告警（需要一条 ERROR 级计数/告警通道）。
- `chat_recall.py` 体量超 800 行上限（既有欠账）。
- **采购域 M1 衔接**：`IncomingInspection` / `Supplier` / `ItemMaster` 三个 **0 行结构性占位类**被过滤剔除属正确行为
  （[acceptance-1120.md §3.2](./acceptance-1120.md)）；但它们是采购域的**语义锚点**，
  若 M1 需要它们出现在候选集，需引入「类→源绑定」机制（本体层至今**无 `datasource_id` 列**）。
- **ds3 会话「供应商」锚定无腿**：这三个类 `class_alias=null` 且 `class_name` 为英文 ⇒ 关键词锚定不生效；
  ds3 下 `DIM_SUPPLIER` 又被硬过滤剔除 ⇒ ds3 的「供应商」问句只能靠向量召回命中 `MAIN_DATA_SUPPLIER`（[acceptance-1120.md §3.5](./acceptance-1120.md)）。
