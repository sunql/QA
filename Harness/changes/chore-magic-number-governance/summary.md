# 变更：魔数治理 Phase 2 实施批（15 项全完成，0087 + 0088 + 0089）

- **日期**：2026-09-28
- **作者**：Claude (with user direction 启琳)
- **Phase**：§2.4 LOW 魔数治理（Phase 2）
- **状态**：done（easy tier 5 + medium tier 4 + hard tier 6 = 15/15）
- **关联**：规范批 `chore-magic-number-governance-spec`（三档决策 + 14 项清单）、
  `chore-chat-service-file-split`（Phase 1.2，常量随 mixin 落位）、
  `Harness/rules/魔数治理.md`、`Harness/wiki/chat-service-assessment.md` §2.4 LOW 行
- **迁移版本**：`0087_magic_number_config` + `0088_magic_number_config_medium` + `0089_magic_number_config_hard`

---

## 1. 需求

文件拆分后，9 个编译期阈值仍写死在 `chat_recall.py` / `chat_context.py`。把它们迁入
`system_config` 行，运行期每次现读、admin 改值立即生效，与已治理的
`CLASS_FILTER_MAX_CLASSES`（0078）/ `LLM_CONCURRENCY_LIMIT`（0080）同口径。

> ⚠️ **偏离规范批的种子脚本方案**：规范批 §3 原计划新建 `backend/scripts/seed_system_config.py`。
> 实施时改为 **migration 种子**（`ON CONFLICT (key) DO NOTHING`，复用 0078/0080 模式）——
> 种子脚本无法保证 dev/prod/test 三库幂等同步，migration 随 `alembic upgrade head` 自然落三库，
> 且与既有「已治理 3 项」的迁移种子方式一致（SSOT：`qa-system-seed-upsert-pattern`）。

## 2. 设计评审

**三档决策**（规范批已定，此处只记录实施结论）：
- **治理**（本批 9 项）：pipeline 可调阈值 → `system_config` 现读。
- **不改**：regex 模式、SQL 关键字集合、日志 reason 字符串。
- **判别不清**：默认不改，等 admin 反馈。

**读取模式不变性（4 条）**：不缓存 / 失败不阻断（返 `_DEFAULT` + warning）/ int 非正抛回默认 /
`text()` 直写 key 不走 ORM。float getter 不判非正（`0.0` 是「关闭过滤」的合法值）。

## 3. 实施（9 项，按 usage site 是否已有 session 分两档）

### easy tier（5 项，usage site 已有 session）→ 0087

| key | 默认 | getter | usage site |
|---|---|---|---|
| `CLASS_FILTER_TOP_K` | 15 | `_getClassFilterTopK` | `_selectRelevantClasses` |
| `CLASS_FILTER_HIT_MATCH_MIN` | 0.5 | `_getClassFilterHitMatchMin` | `_selectRelevantClasses` |
| `CONTEXT_CONTENT_SEGMENT_LIMIT` | 500 | `_getContextContentSegmentLimit` | `_buildContextPrompt` |
| `CONTEXT_SQL_SEGMENT_LIMIT` | 500 | `_getContextSqlSegmentLimit` | `_buildContextPrompt` |
| `CONTEXT_PROMPT_CHAR_BUDGET` | 4000 | `_getContextPromptCharBudget` | `_buildContextPrompt` |

### medium tier（4 项，usage site 需穿透 session）→ 0088

| key | 默认 | getter | 穿透方式 |
|---|---|---|---|
| `FEW_SHOT_TOP_K` | 3 | `_getFewShotTopK` | `_buildFewShot(dto, session)` 加 session 参数 |
| `FEW_SHOT_SIMILARITY_MIN` | 0.6 | `_getFewShotSimilarityMin` | 同上 |
| `FEW_SHOT_EXAMPLE_LIMIT` | 400 | `_getFewShotExampleLimit` | 同上 |
| `STATE_HISTORY_FIELD_LIMIT` | 500 | `_getStateHistoryFieldLimit` | `_buildStatePrompt(state, intent, field_limit)` 加 field_limit 参数 |

**签名变化（medium tier 的两个破坏性改动）**：
- `_buildFewShot(self, dto)` → `_buildFewShot(self, dto, session)`；唯一调用点 `chat_service.py:683` 同步。
- `_buildStatePrompt(state, intent)` → `_buildStatePrompt(state, intent, field_limit)`（static）；
  唯一生产调用点 `chat_service.py:833` 改为条件读 + 传参。

**常量改名**：`_FEW_SHOT_*` → `_FEW_SHOT_*_DEFAULT`、`_STATE_HISTORY_FIELD_LIMIT` →
`_STATE_HISTORY_FIELD_LIMIT_DEFAULT`（与 easy tier 的 `_CLASS_FILTER_TOP_K_DEFAULT` 等命名一致），
`chat_service.py` re-export 同步更新，既有测试 import 零改动。

## 4. 数据模型变更

- `0087_magic_number_config.py`：seed 5 个 easy-tier key。
- `0088_magic_number_config_medium.py`：seed 4 个 medium-tier key。
- 均 `ON CONFLICT (key) DO NOTHING`（幂等），`downgrade()` 对应 `DELETE WHERE key IN (...)`。
- 两库（prod `qa_metadata` + test `qa_metadata_test`）随 `alembic upgrade head` 同步。

## 5. 接口契约变更

无对外 API 变更。`system_config` 表新增 9 行，admin UI 已支持 value 编辑（复用既有）。

## 6. 测试

**新增**（`test_chat_service.py`，parametrized 复用既有 governance 用例）：
- `test_int_config_getter_uses_db_value` + 3（`_getFewShotTopK`/`_getFewShotExampleLimit`/`_getStateHistoryFieldLimit`）
- `test_int_config_getter_falls_back_on_missing_or_invalid` + 3（同上）
- `test_config_getter_falls_back_on_db_error` + 4（含 `_getFewShotSimilarityMin`）
- `test_float_config_getter_few_shot_similarity_min`（新，验证 `0.0` 合法）

**回归判定（git stash 基线 diff，非「全绿」）**：
- `test_chat_service.py`：36 失败集**完全不变**（0 新增 / 0 修复）。
- `test_chat_service_state.py`：9 失败集**完全不变**（修复了 medium tier 引入的 7 个
  `_buildStatePrompt` 2-arg TypeError 回归，回到基线）。
- `test_chat_service_stream.py`：10 失败集**完全不变**。

**踩坑**：`_buildStatePrompt` 签名加 `field_limit` 后，`test_chat_service.py`
`TestStatePlanObservability` 2 例 + `test_chat_service_state.py` 7 例仍用旧 2-arg 调用，
运行时 TypeError → 通过 stash diff 精准定位并逐个补第三参 `_STATE_HISTORY_FIELD_LIMIT_DEFAULT`。

## 7. hard tier（6 项，已完成 → 0089）

hard tier 的难点是：usage site 在**无状态 nl2sql 模块函数管线**里，模块函数
无 `self._xxx`，无法直接读 `system_config`。方案：session 只穿透到 **async 编排层**
（`generateSql` / `generateValidatedPlan`），在编排层现读阈值，然后把解析后的值
作为**可选参数**传给纯函数 `buildSchemaText` / `validatePlan` / `_extractLimit`。
既有调用（不传新参数）自动落默认值，700+ 测试零回归。

| key | 默认 | getter | 透传路径 |
|---|---|---|---|
| `REFINE_MAX_LIMIT` | 1000 | `_readRefineConfig` | `generateSql` → `applyRefineDirect(maxLimit=)` |
| `OWNER_HINT_MAX_CLASSES` | 3 | `_readPlanConfig` | `generateValidatedPlan` → `validatePlan(ownerHintMaxClasses=)` |
| `CRITICAL_DIGEST_MAX_ITEMS` | 50 | `_readSchemaConfig` | `generateSql` → `buildSchemaText(digestMaxItems=)` |
| `CRITICAL_DIGEST_MAX_DESC_CHARS` | 200 | `_readSchemaConfig` | 同上 |
| `VALUE_SAMPLE_VALUE_MAX` | 30 | `_readSchemaConfig` | 同上 |
| `NL2SQL_MAX_TOKENS` | 2048 | `_readSchemaConfig` | `generateSql` 内部 `maxTokens` 变量 |

**改动文件**（9 文件，+414/−68）：
- `nl2sql_schema.py`：3 常量改 `_DEFAULT`；`buildSchemaText` / `_formatSampleValue` /
  `_buildCriticalColumnsDigest` 加可选参数。
- `nl2sql_refs.py`：`_OWNER_HINT_MAX_CLASSES` → `_DEFAULT`；`_propertyOwnerHint` 加 `maxClasses`。
- `nl2sql_refine.py`：`_REFINE_MAX_LIMIT` → `_DEFAULT`；`_extractLimit` / `applyRefineDirect` 加 `maxLimit`。
- `nl2sql_plan.py`：`_NL2SQL_MAX_TOKENS` / `_NL2SQL_TRUNCATION_BACKOFF` → `_DEFAULT`；
  `generateQueryPlan` 加 `maxTokens` + `ownerHintMaxClasses`；`validatePlan` 加 `ownerHintMaxClasses`。
- `nl2sql_service.py`：新增 `_readIntConfig` / `_readSchemaConfig` / `_readPlanConfig` /
  `_readRefineConfig` + 3 个 `_readXxxConfigOrDefault` 门面实例方法；`generateSql` /
  `generateValidatedPlan` 加 `session` 参数；`validatePlan` / `applyRefineDirect` 门面方法加透传。
  门面 474 → 644 行（仍在 800 内）；末尾新增 re-export 块。
- `chat_service.py`：`_twoStageGenerate` 加 `session` 参数；`_planAndGenerateSql` 的
  `_twoStageGenerate` 调用点传 `session=session`。

**测试更新**：
- `test_nl2sql_service.py`：import 改名 + 新增 `TestNl2SqlConfigGetter` 14 例
  （6 参数化 DB 值 + 6 参数化 fallback + DB 错 + session=None）。
- `test_nl2sql_transient_retry.py`：`_NL2SQL_MAX_TOKENS` → `_DEFAULT`（5 处）。
- `test_refine_shortcuts.py`：`_REFINE_MAX_LIMIT` → `_DEFAULT`（import + 1 断言）。

**关键设计决策**：
- `_NL2SQL_TRUNCATION_BACKOFF` 是**派生常量**（`_NL2SQL_MAX_TOKENS * 2`），不单独
  配置；`generateSql` 内部 `truncationBackoff = maxTokens * 2`，随 NL2SQL_MAX_TOKENS
  自动翻倍。
- `validatePlan` / `buildSchemaText` / `applyRefineDirect` 的既有测试调用
  （700+ 处，经 `Nl2SqlService()` 或模块函数）**零改动**——新参数全是可选 keyword，
  默认落 `_DEFAULT`。
- `generateValidatedPlan` / `generateSql` 的 `session` 参数默认 `None`，
  测试不传时走默认值路径（与生产 session 传入时行为分离）。

**回归判定（git stash 基线 diff）**：
- `test_chat_service.py`（36 失败）/ `test_chat_service_state.py`（9 失败）/
  `test_chat_service_stream.py`（10 失败）失败集**完全不变**。
- `test_nl2sql_service.py` 122 → 136 passed（新增 14 getter 测试全绿）。
- `test_query_plan_validation.py`（49）/ `test_refine_shortcuts.py`（59）/
  `test_nl2sql_transient_retry.py`（11）/ `test_scope_row_limit.py`（41）/
  `test_property_ref_normalize.py`（22）/ `test_join_graph.py`（31）**全绿零回归**。
- 349 例 nl2sql 相关单测全绿（69s）。

## 8. 部署验证

- 后端：`./scripts/deploy_backend.sh`（灌 `app/` + `scripts/` + `alembic/`，SSOT：
  `qa-system-stale-container-deploy`）+ `alembic upgrade head`（两库）。
- 前端：不变。
- admin 验证：`/admin/system-config` 应含 9 个新 key，改值后下一条请求立即生效。

## 9. 关联

- **规范批**：`chore-magic-number-governance-spec`（三档决策 + 14 项清单 + 反模式）
- **规范文档**：`Harness/rules/魔数治理.md`
- **评估文档**：`Harness/wiki/chat-service-assessment.md` §2.4 LOW 行（9/14 治理）
- **完整计划**：`/Users/sunql/.claude/plans/rosy-beaming-phoenix.md`
- **同模式 SSOT**：`_getClassFilterMaxClasses`（0078 已治理）、`qa-system-seed-upsert-pattern`
- **memory**：`qa-system-magic-number-governance`（本次登记）

---

## SSOT 校验清单

- [x] 9 项 → system_config（5 easy + 4 medium），getter 模式复用 SSOT
- [x] 迁移种子 `ON CONFLICT (key) DO NOTHING`（非种子脚本，偏离已记录）
- [x] 常量改名 `_*_DEFAULT` 命名统一，re-export 同步，既有 import 零改动
- [x] 签名变化 `_buildFewShot(dto, session)` / `_buildStatePrompt(state, intent, field_limit)` 全调用点同步
- [x] 回归判定 = git stash 基线 diff（3 文件失败集完全不变）
- [x] hard tier 6 项未做，明确登记为下一批
