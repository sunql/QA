# 向量检索按本地类 id 收敛到 Milvus `expr`（Gap A）—— 实施结果汇总

- **日期**：2026-11-19（设计）/ 2026-11-20（实施 + 评审修复）
- **批次**：`2026-11-19-recall-milvus-local-scope`
- **分支**：`fix/recall-milvus-local-scope`
- **设计**：[design.md](./design.md)（同目录）
- **状态**：**已 squash 合入 main = `d1c20de9`**（PR #159；`merge-gate / failure-set-diff` = success ⇒ 净新增 0）；
  **镜像已构建并自证**（构建源 = 合并后 main `d1c20de9`，前后端两个 tar.gz + sha256 见
  `Harness/changes/2026-11-20-recall-fix-release/summary.md`）⇒ **待用户在生产执行部署**

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
| `f6f5d97b` | 合入 main（Gap B 落地的 squash）+ 补 Gap A summary/设计补记 |
| `b926b5ef` | **评审修复**：钉住路径不施加作用域（HIGH-1）+ id 元素校验 + deny-all 逻辑恒假 + 空窗重试可见化 |

代码改动规模（`git diff origin/main..HEAD`）：
后端 6 个源文件（`milvus_client` / `milvus_search` / `milvus_registry_v2` /
`semantic_registry_rag_gate` / `ontology_service` / `chat_recall`）+ 1 个新测试文件
+ 8 个既有测试文件（替身签名对齐 + 新增守卫）。**零迁移、零表结构变更、零前端改动**。

## 三、评审结论与处置（code-reviewer + security-reviewer）

两轮审查共提出 **1 HIGH / 6 MEDIUM / 8 LOW**（无 CRITICAL）。处置如下 —— 每一条都写明
「改」还是「不改 + 为什么」，避免「评审提了但没人管」。

### 3.1 已修（HIGH + 4 MEDIUM + 2 LOW）

| 编号 | 结论 | 处置 |
|---|---|---|
| **HIGH-1**（安全） | 钉住语义发布时候选类 id 是**合成 id**（按类名排序分配的 1..N），与 Milvus 的 `ontology_id`（`ontology_class.id` 主键）**不同 id 空间** ⇒ 施加作用域会 (a) 把真本地类挡在窗口外、(b) 让合成 id 撞上的跨源/墓碑主键进窗口、(c) 命中 id 与快照类 id 错映射 ⇒ 错类归因 | ✅ **不施加作用域**（`view is None` 才施加）+ `WARNING scene=pinned_scope_skipped` + 回归用例 `test_pinned_release_view_skips_class_id_scope`；设计文档 §2.2/§3/§5 与 wiki 同步更正「同一 id 空间」的结论 |
| MEDIUM-1（安全） | `int()` 强转会静默改写非法 id（`int("12")` / `int(True)`），且 `ValueError` 不在 `ontology_service` 的 except 里 ⇒ 裸 500 | ✅ `buildClassIdScopeExpr` 显式校验（非 int / bool / 负数 ⇒ `ValueError`）；`ontology_service` 把 `ValueError`/`TypeError` 一并翻译成 `MilvusError`（领域异常 ⇒ 4xx） |
| MEDIUM-2（安全） | 「作用域窗口为空 ⇒ 无作用域重试」绕过了作用域不变式，且只记 INFO ⇒ 运维看不见 | ✅ 重试是**有意**的绕过（设计文档已写清三态分流），日志提为 `WARNING` + `scene=class_scope_bypass`（**不用 `reason=`**：回退率按「含 `reason=` 的行」聚合，这里不是召回回退）；设计文档 §3 语义矩阵补该行 |
| MEDIUM-4a（安全） | expr 过长 ⇒ Milvus 解析失败 ⇒ 被吞成 `search_error` ⇒ **静默**退回本批要修的宽窗 | ✅ 作用域 id 数上限 2000（≈ 13.8 KB），超限 ⇒ **显式**降级为不限 + `WARNING scene=class_scope_too_large`（可见），并有单测 |
| MEDIUM-4b（安全） | 超限降级应为「不收窄 + 锚定救援」而非「全局向量窗口」 | ⚠️ **部分采纳**：现降级 = 不限（= 今天的形态，宽窗仍受数据源过滤约束 ⇒ 无跨源泄漏）；不改成锚定救援，是因为锚定救援**要求锚定命中**，无命中时仍回落宽窗 ⇒ 两种写法在「无锚定」时等价，而「有锚定」时不限 + `no_match` 也会落到锚定救援 ⇒ 实际行为一致，不引入新分支 |
| LOW-1（安全） | deny-all 依赖「id 恒为正」这个**数据假设**而非逻辑恒假 | ✅ 改为 `ontology_id < 0 and ontology_id >= 0`（**逻辑恒假**，与数据无关）；只读探针实测语法合法且恒空 |
| LOW-2（安全） | `bool` 是 `int` 子类 ⇒ `int(True)` 会被当作 id 1 | ✅ 随 MEDIUM-1 一并排除 |
| HIGH（code） | 作用域路径下「窗口为空」直接回落宽窗 ⇒ **比今天更宽**（43 个本地类全进 schema） | ✅ 改为「一次无作用域重试」：命中解析不出本地类 ⇒ `no_match` ⇒ 锚定救援窄窗（与今天同状态一致）；重试仍空 ⇒ `no_hits` ⇒ 宽窗（Gap B 语义逐字不变） |
| MEDIUM（code） | 作用域 id 集未去重/排序 | ✅ `sorted({...})`（去重 + 升序 ⇒ 表达式可复现）+ 单测 |
| LOW（code） | 需要生产量级的 Milvus 探针 | ✅ 见 §四（expr 规模 / 不欠返回 / 越界 0 / 大 expr 可解析） |

### 3.2 明确不改（附理由）

| 编号 | 不改的理由 |
|---|---|
| MEDIUM-3（安全） | 建议把 `except Exception` 收敛到 `(MilvusError, OSError, ValueError)`。**不改**：① 召回是**增强**而非硬依赖，收敛后一个未预期的异常类型会把「问答应答不出」变成 **500**（生产上更糟）；② 现写法 `logger.warning(..., exc_info=True)` **已带完整堆栈** ⇒ 异常类型在日志里可见，不是「静默吞掉」。要改「编程错误不被降级掩盖」，正确做法是**另加一条 ERROR 级计数/告警**，而不是改降级语义 |
| LOW-3（安全） | 日志只带计数与 scene 名，无 DSN/口令/类名 ⇒ 合规；`recall_datasource_filter` 写 `source_table` 是既有行为，本批不动 |
| LOW-4（安全） | 文档漂移 ⇒ 已修（design.md §2.1/§2.2/§3/§4.6/§5/§6/§7/§8 + wiki 第 3 级） |

## 四、实测证据

### 4.1 Milvus `expr` 过滤行为（本地只读探针，Milvus 2.4.6，`default` 库 59 行）

| 探针 | 结果 |
|---|---|
| 施加 `ontology_id in [...]` 后是否欠返回 | **不欠返回**（作用域 30 个 id、`limit=59`（= 集合大小）⇒ 期望 30 行、实返 30 行） |
| 集合内是否有集合外行 | **零个**集合外行（`query` 与 `search` 两条路径都是 0 越界） |
| `ontology_id < 0 and ontology_id >= 0`（逻辑恒假） | **合法**，恒空 |
| `ontology_id in []` | **不合法**（语法错误）⇒ deny-all 必须用恒假式表达 |
| 是否需要标量索引 | **不需要**（集合量级 304 类） |
| expr 规模 | 30 id ⇒ 99 B；**94 id（生产量级）⇒ 396 B**；304 id ⇒ 1.6 KB；2000 id ⇒ 13.8 KB；10000 id ⇒ 48.9 KB **均可被 Milvus 解析** |

⚠️ 探针全程**只读**（本地 Milvus 19530 是生产数据面，禁写）。
⚠️ 本地集合只有 59 行（生产 304 类 + 重复向量）⇒ 探针证明的是**过滤语义正确**，
**不是**生产量级下的召回完备性 ⇒ 生产侧靠灰度日志观测（命中数 / 越界 id 数）。

### 4.2 id 空间一致性

Milvus 的 `ontology_id` = `ontology_class.id`：`syncEmbedding(ontologyId, ...)` 写入的就是类 id，
对账路径 `syncMissingClassEmbeddings` 同口径；命中侧 `hit.id` 直接与 `cls.id` 比对
⇒ **直读治理表路径**同一 id 空间。

⚠️ **例外（既有缺陷，本批不修）**：会话**钉住语义发布**时候选类来自快照，其 id 是
**合成 id**（`semantic_schema_adapter._synthesizeIds` 按类名排序分配 1..N）⇒ 与主键不同空间
⇒ 该路径**不施加作用域**（否则会放大错空间问题，见 §三 HIGH-1）。

### 4.3 作用域与 `matchedRatio` 阈值的关系

作用域后命中全部落在本地类 ⇒ `matchedRatio` 恒高 ⇒ 只会**更容易**通过
`CLASS_FILTER_HIT_MATCH_MIN` 阈值 ⇒ 不会误触发「命中率过低」告警。

## 五、测试证据

### 5.1 新增用例

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `backend/app/tests/unit/test_milvus_class_id_scope.py`（**新建**） | 24 | expr 三态 / **deny-all 逻辑恒假（对正/负/0 id 恒不命中）** / **非法元素 ⇒ ValueError** / v1 透传 / v2 与治理 expr 合并 / class-only 约束 / **生产症状复现** / 闸门透传 |
| `backend/app/tests/unit/test_recall_anchor_wiring.py`（并入） | +9 | 作用域 = 候选集 id 集 / **无作用域调用形态不变（严格替身反证）** / 空集 = deny-all / 作用域不含跨源类 / **id 去重+升序** / **超上限 ⇒ 显式降级+告警** / **空窗 ⇒ 无作用域重试（自愈/救援/宽窗三态）** / **钉住视图 ⇒ 不施加作用域** |
| `backend/app/tests/integration/test_classify_and_recall.py`（并入） | +1 | 窗口模拟替身：跨源类占满窗口 ⇒ 作用域生效后本地类进得来 |

**全部用 fake，不连 Milvus**（本地 19530 是生产数据面 ⇒ 本批一律不连；fixture 同时切断
`_connect` / `hasEmbeddingCollection`）。

### 5.2 定向回归（真实 PostgreSQL 测试库 5434 / `qa_metadata_test`）

| 测试集 | 本分支 | 基线 | 失败集合对差 |
|---|---|---|---|
| `unit/`（`test_chat_service` + `test_recall_*` + `test_classify_and_recall` + `test_class_recall_info_contract` + `test_milvus_class_id_scope`） | **5 failed / 399 passed** | 同样 5 条 | 失败集合**逐条一致** ⇒ 净新增 **0** |
| `integration/`（6 个受影响文件） | **13 failed / 80 passed** | 同样 13 条（79 passed） | 失败集合**逐条一致** ⇒ 净新增 **0**（+1 passed = 本批新增集成用例） |
| `unit/` **全量**（1657 用例） | 由 **`merge-gate / failure-set-diff`** 在 CI 上裁决（失败集合对 main 最近一次完整 run 净新增 0） | — | 见 §5.3 |

**基线取证方法（可复现）**：在 `/tmp/gapbase` 建 **detached 临时 worktree**（`9d78f7dd`），
以**同一组环境变量**（`/tmp/qatest_env.sh`：5434 测试库 / 7688 neo4j 等）跑同一命令，
逐条比对失败集合。**禁裸 pytest**（unit 测试需要 `TEST_DATABASE_URL`，禁 sqlite）。
⚠️ **禁并发两个 pytest**（同库冲突会产生 47–84 条 ERROR，伪造成「净新增」）。

**已知红基线（既有，非本批引入，逐条一致）**：

- `unit/test_chat_service.py::TestChatService::test_class_filter_logs_successful_pruning`
  （logger 名不匹配类）及 `TestBuildAnswerPrompt` 的 4 条（JSON 解析）
- `unit/test_chat_service_stream.py::TestStreamingPipeline` 的 2 条（JSON 解析）
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
Gap B 的 `no_hits` 语义（「基础设施故障不接管 ⇒ 宽窗」）**逐字不变** —— 作用域路径下 `no_hits`
的成因多了一种「本地类没有向量」，那里先做一次无作用域重试分流，仍空才落到宽窗。
两者的关系见 design.md §7。

## 七、遗留挂账（沿用 #157 批次的既有欠账，本批未动）

- Milvus 写路径：墓碑向量未删 + 2.47× 重复向量（本批只在 class 检索窗口内绕开，不修写路径）。
- few-shot 未按窗口约束（`query_embeddings` 检索不带窗口约束）。
- 校验器「窗口未覆盖 vs 本体不存在」措辞。
- `chat_recall.py` 体量超 800 行上限（既有欠账，#157 批次 M1 已立项）。
- 研究入口的 BO 臂仍是 **Milvus 全局检索（无数据源维度）** ⇒ 本批的作用域**不覆盖** BO 臂
  （作用域只在 class 集合 + 对话链路的类召回接线点注入）⇒ 根治需另批。
- **钉住语义发布路径的 id 空间错配**（既有缺陷，评审 HIGH-1 暴露）：快照类 id 是合成 id，
  与 Milvus 主键 / `classById` 的键都不同空间 ⇒ 该路径的向量命中按 id 解析本就不可靠。
  本批不施加作用域以免放大；根治需「快照类名 → 真实主键」映射 + 命中 id 回译（另批立项）。
- 召回降级「编程错误 vs 基础设施故障」的分类告警（评审 MEDIUM-3 建议）—— 需要一条 ERROR 级
  计数/告警通道，本批不改降级语义（见 §3.2）。

## 八、发版提示

本批**仅代码 + 文档**，未部署。生产生效需一次**后端镜像**发版（前端无改动 ⇒ 不需要前端镜像）。
发版 runbook 复用 `Harness/changes/2026-11-19-recall-datasource-filter-anchor/deploy-prod.md`
（按合并后的 main 重算 sha256；等价性复核**按路径**：`git diff <merge> <src> -- backend/ frontend/ docker/`）。

⚠️ 上线前必跑 #157 批次的 **A1/A2/B1/C1 探针**（数据源覆盖度 / 跨源 DIM 存在性 / BO 臂跨源活类）。

⚠️ 上线后必看**三个新观测点**（缺任一项 ⇒ 修复静默失效且没人知道）：

1. `hits` 是否**全部落在本地类**（作用域生效则命中解析率应 ≈ 100%，`reason=no_match` 归零）；
2. 是否出现 `scene=class_scope_bypass`（作用域窗口为空 ⇒ 本地类没有向量 / expr 过严 ⇒ **要查**）；
3. 是否出现 `scene=pinned_scope_skipped`（钉住路径**不**收窄 ⇒ 该路径的向量命中仍是老口径）。
