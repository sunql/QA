# 变更：魔数治理 Phase 2 实施批（9 项 → system_config，0087 + 0088）

- **日期**：2026-09-28
- **作者**：Claude (with user direction 启琳)
- **Phase**：§2.4 LOW 魔数治理（Phase 2）
- **状态**：done（easy tier 5 + medium tier 4；hard tier 6 项 nl2sql 常量未做，见 §7）
- **关联**：规范批 `chore-magic-number-governance-spec`（三档决策 + 14 项清单）、
  `chore-chat-service-file-split`（Phase 1.2，常量随 mixin 落位）、
  `Harness/rules/魔数治理.md`、`Harness/wiki/chat-service-assessment.md` §2.4 LOW 行
- **迁移版本**：`0087_magic_number_config` + `0088_magic_number_config_medium`

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

## 7. 剩余（hard tier，未做）

6 项 nl2sql 常量 usage site 在**无状态的模块函数管线**里，需把 session 穿透进
`nl2sql_service` 门面 → 各模块函数（`buildSchemaText`/`generateQueryPlan`/REFINE 直写等）：

| 常量 | 默认 | 所在模块 |
|---|---|---|
| `_REFINE_MAX_LIMIT` | 1000 | nl2sql_refine.py |
| `_OWNER_HINT_MAX_CLASSES` | 3 | nl2sql_refs.py |
| `_CRITICAL_DIGEST_MAX_ITEMS` | 50 | nl2sql_schema.py |
| `_CRITICAL_DIGEST_MAX_DESC_CHARS` | 200 | nl2sql_schema.py |
| `_VALUE_SAMPLE_VALUE_MAX` | 30 | nl2sql_schema.py |
| `_NL2SQL_MAX_TOKENS` | 2048 | nl2sql_plan.py |

**难点**：与 ChatService 的 mixin 不同，Nl2SqlService 无状态，模块函数无 `self._xxx`，
session 需作为参数穿过 5+ 层调用（门面 → plan/schema/refine），改动面大、回归风险高。
建议单独立批，逐模块穿透 + stash 基线 diff 验收。

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
