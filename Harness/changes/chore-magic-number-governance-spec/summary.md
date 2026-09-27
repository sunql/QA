# 变更：chore-magic-number-governance-spec（魔数治理规范建立）

- **日期**：2026-09-28
- **作者**：启琳
- **Phase**：「§2.4 LOW 魔数治理」启动批（仅规范 + SSOT 化，**无代码改动**）
- **状态**：done（spec-only）
- **关联**：`chore-config-duplicate-fields`（同日姊妹批：config 重复字段治理）、
  `Harness/rules/魔数治理.md`（新建）、`Harness/wiki/chat-service-assessment.md` §2.4 LOW 行 ⏳ 状态
- **迁移版本**：无

---

## 1. 需求

`chat_service.py` 4916 行 / `nl2sql_service.py` 2589 行远超 800 行软上限；大量编译期魔数
（topK=15、max=30、2000/600 char、阈值 0.3、5 轮等）仅部分已 `system_config` 化。
已治理 3 项（`ENABLE_L4_AGENT_LOOP` / `CLASS_FILTER_MAX_CLASSES` / `ADS_RECALL_WEIGHT`），
未治理 ~15 项编译期常量。

用户授权（2026-09-27 续会话）：「继续 §2.4 LOW（拆分 + 魔数治理）+ §15 残差」；
本次会话切到「**只做评估文档 + 规范**」最小方案（避免单会话未完成风险）。

---

## 2. 设计评审

### 三档决策（governance policy SSOT）

| 档 | 判据 | 例子 |
|---|---|---|
| **治理** | pipeline 行为可调阈值（topK/char-limit/similarity/weight）→ `system_config`，运行时现读 | `_CLASS_FILTER_TOP_K`(15) |
| **不改** | 纯实现细节（regex 模式、SQL 关键字集合、日志 reason） | `_SQL_FENCE_RE`、`REASON_PLAN_REPLY_EMPTY` |
| **判别不清** | 编译期数字但语义是「防御性兜底」而非「可调策略」 | 默认不改；如有 admin 反馈再迁 |

### 读取模式（复用 SSOT）

不变性（4 条）：
- **不缓存**：每次请求现读，admin 改值无需重启
- **失败不阻断**：DB 抖/格式错/非正 → 返 `_DEFAULT` + warning 日志
- **非正抛错**：与 `CLASS_FILTER_MAX_CLASSES` 同口径（闸门静默失效）
- **可读 SQL**：`text()` 直接写 key，不走 ORM（与既有实现保持一致）

### 候选清单（14 项，SSOT 化进规范文档）

按出现频度 / 影响面排序：

| 常量 | 默认值 | 所在文件 | 备注 |
|---|---|---|---|
| `_CLASS_FILTER_TOP_K` | 15 | chat_service.py:202 | 类裁剪向量检索 topK |
| `_CLASS_FILTER_HIT_MATCH_MIN` | 0.5 | chat_service.py:203 | 命中可解析比例下限 |
| `_FEW_SHOT_TOP_K` | 3 | chat_service.py:199 | 历史相似 SQL few-shot 检索 |
| `_FEW_SHOT_SIMILARITY_MIN` | 0.6 | chat_service.py:200 | 相似度下限（低于视为噪音） |
| `_FEW_SHOT_EXAMPLE_LIMIT` | 400 | chat_service.py:201 | 单条示例字符上限 |
| `_CONTEXT_CONTENT_SEGMENT_LIMIT` | 500 | chat_service.py:180 | 单条历史消息正文上限 |
| `_CONTEXT_SQL_SEGMENT_LIMIT` | 500 | chat_service.py:181 | 单条历史 SQL 上限 |
| `_CONTEXT_PROMPT_CHAR_BUDGET` | 4000 | chat_service.py:184 | 拼接总预算 |
| `_STATE_HISTORY_FIELD_LIMIT` | 500 | chat_service.py:198 | q/s 字符上限 |
| `_REFINE_MAX_LIMIT` | 1000 | nl2sql_service.py:717 | 改写 LIMIT 上限 |
| `_VALUE_SAMPLE_VALUE_MAX` | 30 | nl2sql_service.py:126 | 值域采样字符上限 |
| `_OWNER_HINT_MAX_CLASSES` | 3 | nl2sql_service.py:314 | 重试错误时列出类数 |
| `_CRITICAL_DIGEST_MAX_ITEMS` | 50 | nl2sql_service.py:122 | schema digest 项目上限 |
| `_CRITICAL_DIGEST_MAX_DESC_CHARS` | 200 | nl2sql_service.py:123 | digest 描述字符上限 |
| `_NL2SQL_MAX_TOKENS` | 2048 | nl2sql_service.py:109 | LLM max_tokens |

### 反模式（写入规范）

- 把阈值放进 Pydantic `Settings`（改值需重启）
- 用 ORM 缓存 `SystemConfig.getByKey`（同样违背即时生效）
- 统一 `config_service.getXxx`（失去就近阅读性）
- 治理 `re.compile(...)` regex 模式（admin 改了反而挂）
- 治理日志 reason 字符串（改名等于改监控口径）
- 默认值改了但未走种子脚本（dev/prod 配置漂移）

---

## 3. 数据模型变更

无。

`system_config` 表已存在（migration 0052）；admin UI 已支持 value 编辑；
本批只新增种子脚本（**未实施**，下次会话）：

```python
# backend/scripts/seed_system_config.py（下次会话创建）
# ON CONFLICT DO UPDATE 幂等模式（参考 qa-system-seed-upsert-pattern）
```

---

## 4. 接口契约变更

无。本次只新建规范文档 + 评估文档状态变更。

---

## 5. 实现要点

- **本次会话 0 改动**：
  - 新建 `Harness/rules/魔数治理.md`（规范文档）
  - 新建本 summary.md（SSOT）
  - 修改 `Harness/wiki/chat-service-assessment.md` §2.4 LOW 行（⏳ 状态）
  - 修改 `Harness/wiki/chat-service-assessment.md` §0（新增 2026-09-28 §2.4 启动段）

---

## 6. 测试

无（规范文档，不触发测试）。下次会话执行 Phase 1 拆分时新增。

---

## 7. 安全审查

无新增攻击面（规范文档）。

---

## 8. 部署验证

**无部署**——本次仅文档改动，前端/后端均无需 rebuild。

---

## 9. 关联

- **完整计划**：`/Users/sunql/.claude/plans/rosy-beaming-phoenix.md`（§2.4 LOW 3 阶段）
- **规范文档**：[`Harness/rules/魔数治理.md`](../../rules/魔数治理.md)
- **评估文档**：`Harness/wiki/chat-service-assessment.md` §2.4 LOW ⏳ 状态 + §0 启动段
- **同模式 SSOT**：`chat_service._getClassFilterMaxClasses`（line 1037-1073）、
  `chat_service._getAdsRecallWeight`（line 1075-1100）、`chat_service._isL4AgentLoopEnabled`（line 1018-1036）
- **memory 待登记**：`qa-system-magic-number-governance`（下次会话 Phase 1 拆分时登记）

---

## SSOT 校验清单

- [x] `Harness/rules/魔数治理.md` 三档决策 + 14 项候选清单 + 反模式
- [x] 复用 SSOT 模式：`_getClassFilterMaxClasses` / `_getAdsRecallWeight` / `_isL4AgentLoopEnabled`
- [x] 评估文档 §2.4 LOW 行 ⏳ 状态（规范已落地、14 项已 SSOT 化）
- [x] 评估文档 §0 新增 2026-09-28 §2.4 启动段
- [x] 0 代码改动（git diff --stat 仅 `Harness/rules/` + `Harness/wiki/` + `Harness/changes/`）
- [x] 0 部署（前端/后端均不 rebuild）
- [x] Phase 1 拆分 + Phase 2 治理 + Phase 3 §15 残差 → 下次会话执行