# 变更：治理写入口收口（词条字典 + 本体指标）

- **日期**：2026-11-10
- **作者**：Claude
- **Phase**：Phase 10 工程治理（越权面收敛）
- **状态**：待合并（分支 `fix/governance-write-gate`，base `main@4777bfba`）
- **迁移版本**：无（零 DDL、零生产写、**不部署**）
- **计划**：[plan.md](./plan.md)
- **MEMORY**：[qa-system-term-dictionary-drift.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-term-dictionary-drift.md)

---

## 1. 需求

**用户视角**：词条字典与本体指标是**全公司共享的治理数据** —— 词条全文原样注入每个用户
每次 NL2SQL 计划的 system prompt，指标定义驱动全部取数口径。这两处写入口原先只需登录，
任何普通用户都能改，且改错了没有校验兜底。

**验收标准（可测量）**：

1. 非 admin 调用 5 条写路由（词条 2 + 指标 3）全部 403；读路由仍 200；
2. 词条映射到不存在/已下线类、或属性不属于该类 ⇒ 422，且「不存在」与「已下线」文案可区分；
3. 词条两个映射字段为空 ⇒ 201（自由文本词条不受影响）；
4. 既有词条/指标/本体套件**净新增失败 0**；
5. 前端非 admin 看不到写入口，admin 功能不变；刷新后 admin 身份可恢复；
6. chat 的 `/metric` `/define` `/map` 与**已收口的** REST 写入口（词条 / 指标）同口径：
   非 admin 不产生任何治理数据写入（类/属性的 REST 写入口仍是历史开放面，见 §8.12）；
7. `merge-gate / failure-set-diff` 净新增 0；不部署。

---

## 2. 根因（已实测）

### 2.1 词条字典

- `backend/app/api/v1/term_dictionary.py` 路由级依赖只有 `Depends(getCurrentUser)` ⇒ 任何登录用户可增删；
- `TermDictionaryService.createTerm` 只查 `term` 重名，**完全不校验** `mapped_class_name` /
  `mapped_property_name`。类名是裸字符串（版本化不唯一 ⇒ 无 FK），本体 tombstone / 拆表后词条不会跟着走。

**已发生的真实事故**：「供货量」词条曾映射到已 tombstone 的 `DWD_GOODS_RECEIPT_LINE`，
同一问题因问法不同落到不同表。

**关键实现事实**（修正了最初的设计假设）：`mapped_property_name` 在既有数据里普遍写**物理列名**
（如「供货量」→ `DWD_GOODS_RECEIPT_DTL` / `RCV_QTY_PUU`），消费方
`app/services/report_semantic_context.py` 按物理列匹配，LLM 也写物理列 ⇒ 校验必须按
`property_name` **或** `source_column` 任一命中放行，否则历史词条与真实用法会被新校验打死。

### 2.2 本体指标

- `POST/PUT/DELETE /ontology/metrics` 均无角色门禁，任何人可创建/修改（**含 `status` 发布状态**）/删除；
- `ontology_class` 的 update/delete/restore 走 owner ACL（`AclService.assertCanModify`），
  但 `createClass` / `createProperty` / `updateProperty` 没有任何 ACL（见 §8.12）；
  `ontology_metric` **没有 `object_owner` 列**，无法套用同一套 ACL ⇒ 本批用 admin 门禁
  （要按部门授权需先加列，DDL 另立项）。

### 2.3 顺带发现

- 词条 `definition` / `formulaHint` 无长度上限，而全文无分页、无截断地进 prompt ⇒ 存储型
  DoS / prompt 膨胀通道（本批加 2000 上限）。
- 生产 app（`app.main.createApp()`）与测试 app（`tests/_testapp.py`）是两套手工装配，
  集成测试看不到生产装配的门禁漂移（本批加生产装配守卫）。

---

## 3. 变更清单

| 提交 | 内容 |
|---|---|
| `58dcdd91` | 词条字典写入口 `requireAdmin`；`createTerm` 前置校验映射类存活/属性归属（按属性名或物理列名匹配）；4 条中文错误常量；既有 3 个词条套件改用 admin 头 + 种活类 |
| `b992fdcc` | 本体指标 3 条写路由 `requireAdmin`；5 条门禁用例（含读路径 200 契约） |
| `3515bf34` | `definition` / `formulaHint` 限长 2000（两个字段各一条 422 用例） |
| `3f56ea4f` | 前端三处写入口按 `roles.includes("admin")` 收敛（词条按钮/删除列、指标新增/编辑/删除列），读入口保留 |
| `b5367e2c` | 生产装配路由守卫（写路由必须声明 `requireAdmin` + `getCurrentUser`，读路由不得声明 `requireAdmin`）；路由树展开与依赖探测提取到 `tests/_route_auth_support.py`，三个守卫共用 |
| `7803698c` | 评审整改：`_validateMappings` 拆为 `_resolveLiveClassIds` / `_assertPropertyOnClass`；`createTerm` 先判重名；三条谓词级用例；下线文案去掉 `valid_to` |
| `a1517561` | 评审整改（HIGH-1）：chat `/metric` `/define` `/map` 写入口收紧到 admin（路由门禁可被 `POST /chat` 绕过）；5 条集成用例 + 单测基线回正 |
| `faacee86` | 评审整改（MEDIUM-3）：读路由补 `getCurrentUser` 正断言 |
| `3863d725` | 评审整改（H2）：NL2SQL schema 类头/别名/父类名同标准净化 + 3 条用例 |
| `b0fef836` | 评审整改（MEDIUM-1/HIGH-2）：`useRestoreUser` 刷新补回身份；两个前端测试文件补 admin 种子 |
| `35b095fe` | 第二轮评审（H2 未闭合）：关键口径摘要、间接 JOIN 提示、table= 头、FK 目标、JOIN 列、属性名/物理列名全部同标准净化 + 5 条出口级用例 |
| `b0c5a8bb` | 第二轮评审：chat 门禁补自然语言 + `/chat/stream` 两条用例；actor 契约三条用例显式 `roles=("admin",)`；两条摘要用例改回真实执行路径；权限提示文案错字 |
| 本文件 | 文档 + `Harness/rules/权限与安全规范.md` 增「治理写入口」清单与已知缺口 |

**文件**：

- `backend/app/api/v1/term_dictionary.py`、`backend/app/api/v1/ontology.py`
- `backend/app/services/term_dictionary_service.py`、`backend/app/services/messages_zh.py`
- `backend/app/services/chat_domain.py`（chat 写入口门禁）、`backend/app/services/nl2sql_schema.py`（类头净化）
- `backend/app/domain/schemas.py`（`TermDictionaryCreate` 限长）
- `backend/app/tests/_route_auth_support.py`（新）
- `backend/app/tests/integration/test_governance_write_route_auth.py`（新）
- `backend/app/tests/integration/test_ontology_metric_write_gate.py`（新）
- `backend/app/tests/integration/test_term_dictionary_write_gate.py`（新）
- `backend/app/tests/integration/test_chat_domain_write_gate.py`（新）
- 词条三套件 + 两个既有路由守卫（改为复用公共工具）
- 前端 `TermDictionaryButton.tsx` / `TermDictionaryManager.tsx` / `MetricTab.tsx` + 3 个测试文件
- 前端 `App.tsx` / `hooks/useRestoreUser.ts`（新）+ `tests/useRestoreUser.test.tsx`（新）

---

## 4. 测试证据

真实 PostgreSQL（5434 `qa_metadata_test`）+ 完整 HTTP 链路；Milvus 由
`tests/conftest.py:42` 强制指向 `qa_test`。

| 套件 | 结果 |
|---|---|
| 词条定向（write_gate 15 + api + supply_qty + governance 路由守卫） | **39 passed** |
| 指标门禁 `test_ontology_metric_write_gate.py` | **5 passed** |
| chat 写门禁 `test_chat_domain_write_gate.py` | **7 passed**（含自然语言 + 流式两条） |
| 路由守卫三件套（governance + semantic release + agent orchestration） | **19 passed** |
| 目标回归（词条 4 + 本体 2 + 路由守卫 3 + chat 集成 1 + chat unit 3） | **305 passed, 12 failed**（失败集合见 §6，与基线逐条一致） |
| 前端 3 个写入口测试文件 | **23 passed** |
| 前端 OntologyPage + MessageItem | **89 passed** |
| 前端 App + useRestoreUser | **11 passed**；`npx tsc -b` exit 0 |
| schema 相关单测（nl2sql_schema + nl2sql_service） | **208 passed**（新增 5 条渲染出口用例） |
| chat 三套单测 | **7 failed, 191 passed**（失败条目与 HEAD 基线逐条一致） |

---

## 5. 变异取证（证明测试会红）

| 变异 | 预期红 | 实测 |
|---|---|---|
| M1：`createTerm` 去掉 `_validateMappings` 调用 | 引用完整性 4 条 | **4 红** |
| M2：词条路由去掉 `requireAdmin` | 词条门禁 2 条 | **2 红** |
| M3：`POST /term-dictionary` 与 `PUT /ontology/metrics/{id}` 换回 `getCurrentUser` | 恰好这两条参数化守卫用例 | **2 红 / 其余 8 绿** |
| P1：`_resolveLiveClassIds` 去掉 `valid_to IS NULL` | `test_property_on_tombstoned_row_is_rejected` | **1 红** |
| P2：`_assertPropertyOnClass` 去掉 `class_id.in_(liveIds)` | `test_same_named_property_on_other_class_is_rejected` | **1 红** |
| P3：存在同名的下线行就整类判死（过度拦截） | `test_mapping_ignores_tombstoned_sibling_rows` | **1 红** |
| M4：chat 门禁条件短路为 `if False` | 5 条非 admin 拒绝用例（含新增自然语言/流式） | **5 红 / 读路径 + admin 2 绿** |

P1–P3 均为「改一行 → 指定用例红 → 立即还原并复跑全绿」的受控变异，用来证明三条谓词
用例各自钉住一个分支，而不是同一分支的重复断言。**注**：三条用例是**单独**运行时各 1 红；
跑全量时 P1/P2 会连带让依赖同一谓词的用例一起红，结论不变。
M4 证明新增的两条入口用例真正钉在门禁上，而不是恰好被其它分支拦下。

H2 第二轮（`35b095fe`）的 5 条出口用例按 TDD 先写后修：改前 **5 红**、改后 **5 绿**，
每条对应一个独立渲染出口（摘要限定名 / 间接 JOIN 提示 / table= 头 + FK 目标 /
JOIN 列 / 属性名 + 物理列名）。

---

## 6. 失败集合对差（合并判据）

目标回归 `12 failed, 302 passed`，失败集合：

```
test_ontology_api.py::testListClassesEmpty
test_ontology_api.py::testUpdateClass
test_ontology_api.py::testUpdateClassSyncsNeo4j
test_ontology_api.py::testSyncMissingEmbeddings
test_ontology_unified_id.py::testCreateClass_neo4j_receives_unified_id
test_chat_service.py::TestChatService::test_class_filter_logs_successful_pruning
test_chat_service.py::TestBuildAnswerPrompt::test_empty_data_injects_hint
test_chat_service.py::TestBuildAnswerPrompt::test_summary_includes_total_and_columns
test_chat_service.py::TestBuildAnswerPrompt::test_summary_includes_numeric_stats
test_chat_service.py::TestBuildAnswerPrompt::test_summary_27_rows_not_truncated_v2
test_chat_service_stream.py::TestStreamingPipeline::test_answer_stream_emits_error_after_partial_tokens
test_chat_service_stream.py::TestStreamingPipeline::test_stream_chunk_timeout_emits_error
```

**受控对照**：

1. 把 `backend/app/api/v1/ontology.py` 恢复为 base 版本（门禁撤销）后重跑本体套件，
   5 条失败**逐条相同**（`5 failed, 53 passed`）。
2. chat 写门禁落地后先测出 `21 failed / 177 passed`，对照 HEAD 基线 `7 failed / 191 passed`
   ⇒ 净新增 14 条（全部因为单测直调 `processMessage` 不带用户）。按生产链路（`POST /chat`
   必然带登录用户）把用例改为显式传 admin、匿名用例改为「被拒」后回到 **7 failed / 191 passed**，
   失败条目与基线逐条一致。

⇒ 两处**净新增失败 0**。后 7 条为既有基线（4 条 `TestBuildAnswerPrompt` 与摘要/提示文案变更
相关、2 条流式超时用例、1 条裁剪日志用例），与本批改动无关，见 §8 遗留。

**这 5 条为什么早就红**：`tests/integration/conftest.py:186` 的 `dbSession` 夹具会种
`HEADER_CLASS_NAMES`（`scripts/seed_business_objects.py:24`，含 **`Supplier`**）：

- `testListClassesEmpty` 断言类列表为空 —— 实际看到 5 个 `createdBy='seed-test-header'` 的类；
- `testUpdateClass` / `testCreateClass_neo4j_receives_unified_id` 再建 `Supplier` ⇒ 重名 422 ⇒
  后续 `KeyError: 'id'`；
- `testSyncMissingEmbeddings` / `testUpdateClassSyncsNeo4j` 同源。

属**测试卫生问题**（seed 与「空库」断言互斥），与本批改动无关，见 §8 遗留。

---

## 7. 评审处置

code-reviewer 与 security-reviewer 各自独立评审后**收敛到同一条 HIGH**（chat 斜杠命令绕过
路由门禁）。逐条处置：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | HIGH | `POST /chat` 只要求登录，`/metric` `/define` `/map` 直调本体写服务 ⇒ 路由层 `requireAdmin` 被完全绕过 | 已修（`a1517561`）：门禁下沉到 `_handleDomainCommand`，非 admin 写意图返回友好 answer（200，无侧信道）；5 条集成用例 + 单测回正 |
| 2 | HIGH | 前端两处既有测试未种 admin 身份，写入口隐藏后必红（假绿风险：用 queryBy 断言「不存在」会掩盖真实缺陷） | 已修（`b0fef836`）：模块级 admin 种子；89 条用例全绿 |
| 3 | MEDIUM | `partialize` 只持久化 token ⇒ 管理员刷新后 `user === null`，写入口消失 | 已修（`b0fef836`）：新增 `useRestoreUser`（有 token 无 user 补 `/auth/me`），4 条 hook 用例 |
| 4 | MEDIUM | 引用完整性只有「类不存在 / 属性不属于」各一条，未分别钉住 `valid_to` 与 `class_id` 两个谓词 | 已修（`7803698c`）：3 条谓词用例 + 3 条受控变异（§5 P1–P3） |
| 5 | MEDIUM | 读路由只断言「不含 requireAdmin」，未断言仍要求登录 | 已修（`faacee86`）：补 `getCurrentUser` 正断言 |
| 6 | LOW | `_validateMappings` 约 58 行，超出「函数 < 50 行」 | 已修（`7803698c`）：拆为两个私有方法 |
| 7 | LOW | 重名检查排在映射校验之后，报错顺序不符合「表级事实优先」 | 已修（`7803698c`）：先判重名 |
| 8 | LOW | 下线文案把 `valid_to` 列名暴露给最终用户 | 已修（`7803698c`）：改为「已下线，请改用当前存活的类名」 |
| 9 | H2（安全，相邻） | `buildSchemaText` 类头/别名/父类名裸拼，`<` 与换行可逃逸/注入指令行 | 已修（`3863d725`）：同标准净化 + 3 条用例 |
| 10 | H3 / M4 / M5 / M6 / LOW | stub 鉴权默认放行、词典总量预算与分页、类下线后的存量悬空词条与 TOCTOU、术语空白归一 | **延后**，见 §8 |

### 第二轮（scoped 复审）

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 11 | HIGH | H2 未闭合：同一个 `class_name` / `property_name` / `source_table` 仍裸拼在关键口径摘要、间接 JOIN 提示、`table=` 头、FK 目标、JOIN 列、列行 | 已修（`35b095fe`）：6 个出口统一 `_sanitizeSchemaField`，5 条出口级用例（先红后绿） |
| 12 | MEDIUM | actor 契约 3 条写用例只靠 `DEFAULT_STUB_ROLES` 默认携带 admin 才走到写路径，收口默认角色后会误红 | 已修（`b0c5a8bb`）：显式 `roles=("admin",)`，不依赖默认值 |
| 13 | MEDIUM | 两条 `/metric` 单测没传用户，实际断言的是**拒绝分支**，成功路径失去覆盖 | 已修（`b0c5a8bb`）：传 `_ADMIN_USER`，恢复领域命令真实执行 |
| 14 | LOW | 权限提示「请联系统管理员」缺字 | 已修（`b0c5a8bb`）：「请联系系统管理员」 |
| 15 | LOW | §5 的 P1/P2 标「1 红」只在单条运行时成立 | 已在 §5 加注说明（结论不变） |
| 16 | HIGH（安全，范围外） | 类/属性/导入的 REST 写路由仍只要求登录：`POST /ontology/classes`、`POST /ontology/properties`、`PUT/DELETE /ontology/properties/{id}`、`POST /datasources/{id}/import`；且 `createClass` / `createProperty` / `updateProperty` 连 owner ACL 都没有 | **延后**（见 §8.12）：本批批准范围只有词条 + 指标；收口到 admin 会改变 owner-ACL 语义，属产品决策，规则文档已显式标注「已知缺口」，不再宣称 REST 全量同口径 |

---

## 8. 明确不做的事 / 遗留

1. **存量悬空映射词条不体检**：本批只管写入；生产已有词条是否指向死类需单独扫一遍再决定修法。
2. **不做指标按部门授权**：需要给 `ontology_metric` 加 `object_owner` 列（DDL），另立项。
3. **不改 stub 鉴权 fail-soft**：`DEFAULT_STUB_ROLES = ("user","admin")` 的默认放行是全仓跨特性治理问题。
4. **前端收敛只是体验层**：后端 403 才是最终裁决；旧缓存页面点到写操作会吃 403。
5. **`messages_zh.py` 行数已接近 800 行上限**（评审 LOW）：按域拆分是独立任务，本批不拆。
6. **5 条既有本体用例仍红**（§6）：需把 `test_ontology_api.py` / `test_ontology_unified_id.py`
   改成不假设空库（或换独立类名），不在本批范围。
7. **chat unit 7 条基线红**（§6）：4 条摘要/提示文案断言、2 条流式超时、1 条裁剪日志，
   属既有漂移，与本批无交集。
8. **词典总量预算与分页（M4）**：单字段限长 2000 只挡住单条膨胀；`buildDictionaryText`
   仍会把全表拼进 prompt，总量无上限、API 无分页。需按条数/字节做预算裁剪，另立项。
9. **存量悬空词条与 TOCTOU（M5）**：写入校验只管「此刻存活」，类之后被 tombstone 时
   既有词条仍悬空；校验与写入之间类状态也可能变化（本批用单事务读 + 写，未加锁）。
   与 §8.1 的存量体检一并立项。
10. **术语空白归一（M6）**：`term` 未做 `strip`/大小写归一，`"占比 "` 与 `"占比"` 可并存。
    属数据规范问题，需先确认是否已有外部写入依赖原样字符串。
11. **不部署**：生产当前停在 alembic `0140`，本批零迁移，随下次常规发版生效即可。
12. **类/属性/导入的 REST 写入口未收口（待立项）**：`POST /ontology/classes`、
    `POST /ontology/properties`、`PUT/DELETE /ontology/properties/{id}`、
    `POST /datasources/{id}/import` 仍只要求登录；`createClass` / `createProperty` /
    `updateProperty` 也没有 owner ACL（只有 class 的 update/delete/restore 有）。
    这些入口写的是同一批共享治理数据。修法有两条互斥路线：
    ① 全体 `requireAdmin`（与指标一致，但会剥夺非 admin owner 的既有编辑能力）；
    ② 给 `ontology_class` 之外的写面补齐 owner ACL（需定义属性的 owner 归属）。
    属产品决策，需单独确认后再动；在那之前**不要**把「chat 已收口」表述成写面全收口。
