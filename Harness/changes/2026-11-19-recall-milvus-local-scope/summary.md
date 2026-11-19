# 向量检索按本地类 id 收敛到 Milvus `expr`（Gap A）—— 实施结果汇总

- **日期**：2026-11-19（设计）/ 2026-11-20（实施）
- **批次**：`2026-11-19-recall-milvus-local-scope`
- **分支**：`fix/recall-milvus-local-scope`
- **设计**：[design.md](./design.md)（同目录）
- **状态**：**代码已实施**（检索层 + 接线层 + 测试）；**未发版**（生产生效需一次后端镜像发版）

## 一、问题与解法（回顾）

2026-11-19 生产事故的两级补丁都落地后（PR #157 = 数据源过滤 + 关键词锚定，已合 main `d58bb1ad`；
PR #158 = Gap B 锚定救援，已合 main `f07ae867`），**向量检索窗口本身仍然是全局的**。

| 层 | 作用位置 | 覆盖范围 |
|---|---|---|
| 数据源过滤（#157） | **应用侧候选集**（Python） | 候选集从 304 全局类收窄到 94 个本数据源类 |
| Gap B 锚定救援（#158） | 降级路径（`no_match` / `ods_only_hits`） | 向量路径失效时的第二条腿 |
| **Gap A（本批）** | **Milvus 检索窗口**（expr） | **让向量路径本身不失效** |

**根因**：数据源过滤只在候选集这一侧生效，而 `searchEmbeddingsByTypeRouted` 打的是 Milvus
**全局**集合 ⇒ topK 名额被跨源类、墓碑类吃光。生产日志实证（2026-11-19）：
`hits=20 total=94` —— 20 个向量命中里能解析到本地类的为 **0** ⇒ 走 `no_match` 早退 ⇒
兜底把 43 个本地类全塞进 schema ⇒ **召回裁剪被架空**。

**Gap A 修法**：把「本地类 id 集」推进 Milvus 的 `expr`（`ontology_id in [...]`），
让向量窗口与候选集**同口径**。

## 二、提交清单

| 提交 | 内容 |
|---|---|
| `f1e39d3f` | 检索层：`milvus_client.buildClassIdScopeExpr` + `milvus_search`（v1）+ `milvus_registry_v2`（v2）+ `semantic_registry_rag_gate` + `ontology_service` 透传 |
| `e465bf0e` | 生产症状复现守卫（跨源类占满 topK ⇒ 本地类 0 命中） |
| `66682f3d` | wiki 补「第 3 级·向量检索窗口收敛到本地类 id」 |
| `9d78f7dd` | 并入 Gap B（PR #158 的实现，等其合 main 后与 main 对齐） |
| `fe57b374` | 接线层：`chat_recall._selectRelevantClasses` 施加作用域 + 接线单测 4 条 + 集成守卫 1 条 + 替身签名对齐 |

代码改动规模（`git diff origin/main..HEAD`，含 Gap B 的 merge 差异）：
后端 5 个源文件（`milvus_client` / `milvus_search` / `milvus_registry_v2` /
`semantic_registry_rag_gate` / `ontology_service` / `chat_recall`）+ 1 个新测试文件
+ 8 个既有测试文件（替身签名对齐 + 新增守卫）。**零迁移、零表结构变更、零前端改动**。

## 三、三条不可让步的口径

### 3.1 空集 ≠ 不限（三态）

| 作用域 | 语义 | expr |
|---|---|---|
| `None` | **不限**（不施加过滤） | `None`（不传 expr ⇒ 与今天逐字节一致） |
| `[]` | **deny-all**（恒空） | `ontology_id < 0` |
| 非空 | 限定集合 | `ontology_id in [1,2,3]`（去重 + 升序 ⇒ 顺序稳定可复现） |

空集回落成「不限」= 把「本数据源没有类」误读成「全局都能看」⇒ 跨源类又回来。

### 3.2 作用域只落 **class** 集合

Milvus 的 `ontology_id` 在**类/属性之间共用 id 序列** ⇒ 把 class 的 id 集套到 property 集合上
会误伤（把 id 相同的属性行滤掉）。故 `typeFilter="class"` 施加；`typeFilter=None` 的全集合扫描
**只对 class 集合**施加；property/metric/kpi 集合一律不施加。

### 3.3 无作用域时**调用形态逐字不变**

每一层的边界都做**条件传参**（作用域为 `None` 时不多传 kwarg）：

| 层 | 边界 | 无作用域时的形态 |
|---|---|---|
| 检索层 | `milvus_search` / `milvus_registry_v2` | `expr=None`（不传） |
| 闸门层 | `semantic_registry_rag_gate._routedSearch` | 不多传 `classOntologyIds` |
| 服务层 | `ontology_service._searchRegistryEmbeddings` | 不多传 `classOntologyIds` |
| 接线层 | `chat_recall._selectRelevantClasses` | 不多传 `classOntologyIds` |

**为什么必须**：既有测试替身的签名是 `searchByKeyword(query, *, topK=5, typeFilter=None)`
（另有 registryFilter 在 service 层）。多传一个 kwarg ⇒ 整体抛 `TypeError` ⇒ 被
`chat_recall` 的 `except Exception` 吞成 `search_error` ⇒ 测试**全绿但不再覆盖向量路径**
（假绿：向量分支根本没被执行，降级分支接管一切）。这条不变量在接线层由**严格替身**
（只认三个参数）反证，见 §五。

## 四、实测证据

### 4.1 Milvus `expr` 过滤行为（本地只读探针，Milvus 2.4.6）

| 探针 | 结果 |
|---|---|
| 施加 `ontology_id in [...]` 后 topK 是否欠返回 | **不欠返回**（返回数与施加前一致，且全部落在集合内） |
| 集合内是否有集合外行 | **零个**集合外行 |
| `ontology_id < 0`（deny-all）是否合法 | **合法**，返回恒空 |
| `ontology_id in []` 是否合法 | **不合法**（语法错误）⇒ deny-all 必须用 `< 0` 表达 |
| 是否需要标量索引 | **不需要**（集合量级 304 类，远小于探针上限） |

⚠️ 探针全程**只读**（本地 Milvus 19530 是生产数据面，禁写）。

### 4.2 id 空间一致性

Milvus 的 `ontology_id` = `ontology_class.id`：`syncEmbedding(ontologyId, ...)` 写入的就是类 id，
对账路径 `syncMissingClassEmbeddings` 同口径；命中侧 `hit.id` 直接与 `cls.id` 比对
⇒ **同一 id 空间**，作用域可直接用类 id。

### 4.3 作用域与 `matchedRatio` 阈值的关系

作用域后命中全部落在本地类 ⇒ `matchedRatio` 恒高 ⇒ 只会**更容易**通过
`CLASS_FILTER_HIT_MATCH_MIN` 阈值 ⇒ 不会误触发「命中率过低」告警。

## 五、测试证据

### 5.1 新增用例

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `backend/app/tests/unit/test_milvus_class_id_scope.py`（**新建**） | 21 | expr 三态 / v1 透传 / v2 与治理 expr 合并 / class-only 约束 / **生产症状复现** / 闸门透传 |
| `backend/app/tests/unit/test_recall_anchor_wiring.py`（并入） | +4 | 作用域 = 候选集 id 集 / **无作用域调用形态不变（严格替身反证）** / 空集 = deny-all / 作用域不含跨源类 |
| `backend/app/tests/integration/test_classify_and_recall.py`（并入） | +1 | 窗口模拟替身：跨源类占满窗口 ⇒ 作用域生效后本地类进得来 |

**全部用 fake，不连 Milvus**（本地 19530 是生产数据面 ⇒ 本批一律不连；fixture 同时切断
`_connect` / `hasEmbeddingCollection`）。

### 5.2 定向回归（真实 PostgreSQL 测试库 5434 / `qa_metadata_test`）

| 测试集 | 本分支 | 基线 `9d78f7dd`（Gap A 分支 + Gap B） | 失败集合对差 |
|---|---|---|---|
| `unit/test_chat_service.py` + `unit/test_recall_*.py` + `unit/test_classify_and_recall.py` + `unit/test_class_recall_info_contract.py` + `unit/test_milvus_class_id_scope.py` | **5 failed / 400 passed** | **5 failed / 356 passed** | 失败集合**逐条一致** ⇒ 净新增 **0**（+44 passed = 本批新增用例） |
| `integration/`（6 个受影响文件） | **13 failed / 80 passed** | **13 failed / 79 passed** | 失败集合**逐条一致** ⇒ 净新增 **0**（+1 passed = 本批新增集成用例） |

**基线取证方法（可复现）**：在 `/tmp/gapbase` 建 **detached 临时 worktree**（`9d78f7dd`），
以**同一组环境变量**（`/tmp/qatest_env.sh`：5434 测试库 / 7688 neo4j 等）跑同一命令，
逐条比对失败集合。**禁裸 pytest**（unit 测试需要 `TEST_DATABASE_URL`，禁 sqlite）。

**已知红基线（既有，非本批引入，逐条一致）**：

- `unit/test_chat_service.py::TestChatService::test_class_filter_logs_successful_pruning`
  （logger 名不匹配类）及 `TestBuildAnswerPrompt` 的 4 条（JSON 解析）
- `integration/test_classify_and_recall.py::test_reclassify_with_prior_state`（main「匿名写收紧」导致）
- `integration/test_multistep_global_filter.py` 的 3 条、`integration/test_chat_follow_up_cascade.py` 的 8 条、
  `integration/test_chat_service_state.py::test_refine_shortcut_rewrites_sql_without_llm`

### 5.3 门禁

`merge-gate / failure-set-diff`（必检项）—— 判据 = 失败集合对最近一次 main 完整 run **净新增 0**。

## 六、一个必须如实记录的副作用（Gap B 救援路径的可达性）

施加作用域后，**作用域 = 候选集 id 集**，而命中解析用的 `classById` 也建自**同一个候选集**
⇒ 落在作用域内的命中**必然可解析** ⇒ 正常路径下 `no_match` 分支**不可达**。

⇒ **Gap B 的 `no_match` 锚定救援在生产里基本不再触发**（它退化为防御层：Milvus 侧不支持过滤、
集合缺失、id 空间漂移等异常场景的兜底）。`ods_only_hits` 救援分支仍可达（ODS 过滤发生在解析**之后**）。

**这不是回退**：Gap A 让向量路径直接命中本地类，Gap B 的窄窗救援不需要被触发。
两者的关系见 design.md §7（互补、不冲突、共用 `isDatasourceScoped` 标志）。

## 七、遗留挂账（沿用 #157 批次的既有欠账，本批未动）

- Milvus 写路径：墓碑向量未删 + 2.47× 重复向量（本批只在 class 检索窗口内绕开，不修写路径）。
- few-shot 未按窗口约束（`query_embeddings` 检索不带窗口约束）。
- 校验器「窗口未覆盖 vs 本体不存在」措辞。
- `chat_recall.py` 体量超 800 行上限（既有欠账，#157 批次 M1 已立项）。
- 研究入口的 BO 臂仍是 **Milvus 全局检索（无数据源维度）** ⇒ 本批的作用域**不覆盖** BO 臂
  （作用域只在 class 集合 + 对话链路的类召回接线点注入）⇒ 根治需另批。

## 八、发版提示

本批**仅代码 + 文档**，未部署。生产生效需一次**后端镜像**发版（前端无改动 ⇒ 不需要前端镜像）。
发版 runbook 复用 `Harness/changes/2026-11-19-recall-datasource-filter-anchor/deploy-prod.md`
（按合并后的 main 重算 sha256；等价性复核**按路径**：`git diff <merge> <src> -- backend/ frontend/ docker/`）。

⚠️ 上线前必跑 #157 批次的 **A1/A2/B1/C1 探针**（数据源覆盖度 / 跨源 DIM 存在性 / BO 臂跨源活类）。
