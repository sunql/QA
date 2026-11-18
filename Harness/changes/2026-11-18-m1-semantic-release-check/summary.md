# 变更：2026-11-18-m1-semantic-release-check（M1-1.2 发布校验落表）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M1-1.2（采购语义发布 · 发布校验落表）
- **状态**：done
- **关联变更**：[2026-11-18-m1-procurement-binding-tables](../2026-11-18-m1-procurement-binding-tables/summary.md)（M1-1.1a 源绑定两张表，迁移 0154）· [2026-11-18-m1-procurement-binding-service](../2026-11-18-m1-procurement-binding-service/summary.md)（M1-1.1b 绑定行为层）· [feat-semantic-release-a](../feat-semantic-release-a/summary.md)（P1b/D8–D11 发布底座）
- **迁移版本**：`0155_semantic_release_check.py`（32 字符以内 ✓；`down_revision = "0154"`）
- **MEMORY**：[qa-system-m1-semantic-release-check](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-m1-semantic-release-check.md)
- **部署状态**：**未部署**（用户 2026-11-18 决策：生产暂不部署，功能全完成后统一发版）。开发库 5433/qa_metadata 已应用 0154+0155。

---

## 1. 需求

**背景**：语义发布写路径（P1b/D10）只返回一份**聚合**闸门报告（blockers/warnings 文本）。
发布评审要的却是逐项证据：「哪一条校验项失败、失败在哪、严重到什么程度」。
聚合报告回答不了两个问题：

1. **失败不可定位** —— 409 只说「未通过发布闸门（N 项阻断）」，评审要自己猜；
2. **失败不可证** —— M1-a 结构闸第 3 条要求「构造一条缺 JOIN 的绑定 → validate 报 ERROR
   → **check 表可查到该条** → 不得激活」。没有落表，「可查到」无从谈起。

**验收标准（用户视角、可测量）**：

- 发布每走一步（草稿编译 / validate / activate），本次评估的**全部**校验项
  （`PASS` 与 `FAIL` 都落）写进一张新表 `semantic_release_check`，可按发布 id 查；
- validate 被闸门拦下时，该条 ERROR 结论**在表里查得到**，且该发布**不得**被激活；
- 闸门结论与落表行**来自同一份集合**（口径零漂移，单测逐条比对）；
- 读接口能分页，且 `total`/`summary` 覆盖**全部**行（不是当前页）。

## 2. 设计评审

### 候选 A：落表**聚合**结果（只落 blockers/warnings）

| 维度 | 评估 |
|---|---|
| 实现量 | 最小（复用既有 `PublishGateResult`） |
| 「每条 check 落表可查」 | ❌ **不满足**（M1-a 第 3 条明写「check 表可查到该条」） |
| 「通过」与「没跑」可区分 | ❌ 不可区分（PASS 不落 ⇒ 空表） |

**否决**。

### 候选 B：落表逐项结论，但**保留**页内聚合孪生函数

初版实现里，`summarizeChecks(rows)`（内存聚合）与 `summarizeChecksInDb(session, id)`（库内聚合）
并存，API 起初用前者。**评审否决**：两套聚合口径一旦漂移，就会出现
「页面 summary 说 0 个 ERROR、表里却有 ERROR 行」—— 与本特性的核心承诺（零漂移）自相矛盾。
**处置**：删除内存孪生函数，只保留库内聚合一份。

### 候选 C：落表逐项结论 + **覆盖写** + **失败路径也留痕**（采纳）

- 一个 release 一份「当前结论」，每次评估 `DELETE` 旧行 + 写新行（同一事务）；
- 历史轮次由 `audit_log` 承担 ⇒ **失败路径也必须写审计**（见下）；
- 校验项集合**单一事实源**：`evaluateManifestChecks` / `_draftChecks` 各算一次，
  闸门与落表都从它派生。

### 候选 D：只落失败项（PASS 不落）

**否决**：评审看到空表时无法区分「全通过」与「根本没跑」—— 报告失去自证能力。

### 关键取舍 1：`severity` × `result` 正交

`severity` = 该校验项**失败时**的严重级别（`ERROR` 阻断发布 / `WARNING` 治理债）；
`result` = 本次结论（`PASS`/`FAIL`）。⇒ `PASS` 行仍带 `severity`，
「全 PASS 的报告」也能直接告诉读者每项的风险面。

### 关键取舍 2：上限必须**从契约推导**，且截断必须显式标记

初版写死 `MAX_CHECKS_PER_RELEASE = 6000`，而契约允许的上限是
`1（完整性）+ 1（Schema）+ 2000 指标 × 3 = 6002` ⇒ **off-by-2**：合法的 manifest 也会被截断，
而截断丢的是**尾部**校验项 —— 尾部往往正是最后一条指标的 ERROR 结论行（安全评审 HIGH-1）。

**处置**：上限改为 `2 + 3 × MAX_MANIFEST_METRICS`（`MAX_MANIFEST_METRICS` 从
`semantic_manifest_schema` 导出，是契约 `metrics` 数组的 `maxItems`）；
且截断**不再静默** —— 超限时保留上限-1 条 + 追加一条 `CHECKS_TRUNCATED`
（`severity=ERROR` / `result=FAIL`）合成行 ⇒ 表里查得到「报告被截断」，
闸门把它当阻断项（**fail-closed**），且闸门与落表用的是**同一个集合**。

### 关键取舍 3：失败路径**不写** `audit_log` ⇒ 证据被覆盖后彻底消失

**评审 HIGH**：唯一写 `audit_log` 的地方是**成功**的状态迁移（`_applyStatus`）。
而 `replaceChecks` 先 `DELETE` 旧报告 ⇒ 被闸门拦下的发布，等治理数据修好后重新 validate，
上一轮的 FAIL 行被删、`audit_log` 里也无痕 ⇒
「那次被哪一条拦下的」再也查不到 —— **而这正是这张表被建起来要回答的问题**。

**处置**：失败路径补一条审计。动作词用既有的 `UPDATE`
（审计动作是**封闭词表**：服务层 `_VALID_ACTIONS` + DB `ck_audit_log_action` CHECK 双重限制，
新增 action 要改 ORM CHECK + 占一个新迁移号 ⇒ 代价与收益不成比例）；
阻断事实写在 `after` 里（`blocked=true` + `phase` + `blockerCount` + `blockers` +
`errorCount` + `checkCount`），before/after 都给。

## 3. 数据模型变更

**新增表 `semantic_release_check`**（迁移 `0155_semantic_release_check.py`，纯加法 DDL，无 DROP）：

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `release_id` | BIGINT | NOT NULL，FK → `semantic_release.id` **ON DELETE CASCADE** | 发布 id；删发布 ⇒ 证据随之消失（收尾不留痕） |
| `check_code` | VARCHAR(64) | NOT NULL，CHECK 非空 | 机器可读校验项编码 |
| `check_item` | VARCHAR(200) | NOT NULL，CHECK 非空 | 中文项名 |
| `severity` | VARCHAR(10) | NOT NULL，CHECK ∈ {`ERROR`,`WARNING`} | 失败时的严重级别 |
| `result` | VARCHAR(10) | NOT NULL，CHECK ∈ {`PASS`,`FAIL`} | 本次结论 |
| `target_type` | VARCHAR(32) | 可空 | `RELEASE` / `METRIC` |
| `target_key` | VARCHAR(200) | 可空 | 被校验对象（发布级为空） |
| `detail` | TEXT | NOT NULL | 结论明细 |
| `checked_time` | TIMESTAMPTZ | NOT NULL，server_default `now()` | |
| `checked_by` | VARCHAR(64) | 可空 | 操作人 |
| `created_time` / `updated_time` | TIMESTAMPTZ | NOT NULL，默认 `now()` | `TimestampMixin` |

**索引 / 约束**：

- `ix_semantic_release_check_release` (release_id) —— 按发布取；
- `uq_semantic_release_check_unique` (release_id, check_code, `COALESCE(target_key,'')`)
  —— **表达式索引**：发布级校验项的 `target_key` 是 NULL，
  普通唯一索引在 PG 里 NULL 互不相等 ⇒ 同一条发布级结论能落多行。
  用 `COALESCE` 把 NULL 归一成 `''` 才拦得住。
  ⚠️ `alembic check` **只比名不比式** ⇒ 表达式内容靠单测逐字钉住
  （`app/tests/unit/test_semantic_release_check_models.py`）；
- 3 个 CHECK：`severity` 枚举、`result` 枚举、`check_code`/`check_item` 非空。

**迁移链**：0154（M1-1.1 源绑定两张表）→ **0155（本批）**。
⚠️ **A4-0 也需要 0155，两条必须串行排号**（A4-0 取下一个空号 0156）。

## 4. 接口契约变更

**新增表 / 枚举**（无 API 破坏性变更）：

- `SemanticReleaseCheckRead`：id / releaseId / checkCode / checkItem / severity / result /
  targetType / targetKey / detail / checkedTime / checkedBy（camelCase JSON，源于 `CamelModel`）；
- `SemanticReleaseCheckSummary`：total / passed / failed / errorCount / warningCount；
- `SemanticReleaseCheckPage`：rows / total / summary。

**端点变更**：`GET /api/v1/semantic/releases/{releaseId}/checks`

| 变更 | 之前 | 之后 |
|---|---|---|
| 分页 | 无（一次全取） | `limit`（1~200，默认 50）/ `offset`（≥0） |
| `total` | `len(rows)`（= 当前页） | `countChecks`（= 全部行） |
| `summary` | 页内内存聚合 | 库内聚合（覆盖全部行） |

⚠️ `summary` 覆盖全部行是**刻意的**：否则翻到第二页、看到「这一页没有 ERROR」就会误判
「整体通过」。分页面必须回答整份报告的结论。

**状态机变化**：无。异常契约沿用既有约定：

| 失败原因 | 异常 | HTTP |
|---|---|---|
| 快照完整性错 | `SemanticReleaseIntegrityError` | 500 |
| Manifest 契约错 | `ManifestSchemaError`（ValueError 系） | 500 |
| 发布闸门阻断 | `ConflictError` | 409 |

**副作用（新增）**：validate / activate **失败路径会提交本 session**（证据先行），
调用方不得把这两个方法包进更大的事务，否则「抛异常 ⇒ 外层回滚」会把刚落下的证据一起回滚。

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/app/domain/semantic_release_check_models.py` | ORM（10 列 + 2 索引 + 3 CHECK + FK CASCADE） |
| `backend/app/domain/semantic_manifest_schema.py` | 导出 `MAX_MANIFEST_METRICS = 2000`（契约 `maxItems`） |
| `backend/app/services/semantic_release_checks.py` | **纯评估逻辑**：校验项目录、`evaluateManifestChecks`、`checksToGateResult`、`capChecks`（不查库不写库） |
| `backend/app/services/semantic_release_check_service.py` | 落表与查询：`replaceChecks` / `listChecks` / `countChecks` / `summarizeChecksInDb` / `toCheckDict` |
| `backend/app/services/semantic_release_service.py` | 编排：`createDraft`（单一事实源）、`markValidated` / `activate`（失败路径先落证据再抛）、`_recordBlockedAudit` |
| `backend/app/api/v1/semantic_releases.py` | `GET /{releaseId}/checks`（分页 + 全量 summary） |
| `backend/scripts/semantic_release_check_realdata.py` | §67 门禁真机验证脚本（幂等、可复现、自建自删） |

**关键算法 1：单一事实源**（`createDraft`）

```python
draftChecks = _draftChecks(manifest)        # 算一次
gate = checksToGateResult(draftChecks)       # 闸门从它派生
...
await semanticReleaseCheckService.replaceChecks(session, row.id, draftChecks, actor=actor)
```

⇒ 结构上排除「闸门算一遍、落表再算一遍 ⇒ 两边静默错位」。

**关键算法 2：证据先行的失败路径**

```python
evaluation = _evaluateReleaseChecks(row)          # 不抛异常，把失败原因放进返回值
await semanticReleaseCheckService.replaceChecks(...)   # 先落表
if evaluation.failure is not None:
    await _recordBlockedAudit(...)                # 再留审计
    await session.commit()                        # 提交证据
    raise evaluation.failure                      # 最后抛原异常（契约不变）
```

⚠️ `getDb` 依赖在异常时 `rollback` ⇒ **证据必须先 commit**，否则随事务一起消失。

**关键算法 3：截断的 fail-closed**（`capChecks`）

```python
if len(checks) <= MAX_CHECKS_PER_RELEASE: return checks
kept = list(checks[: MAX_CHECKS_PER_RELEASE - 1])
kept.append(PublishCheck(code="CHECKS_TRUNCATED", severity=SEVERITY_ERROR,
                          result=RESULT_FAIL, targetType=TARGET_RELEASE, ...))
return tuple(kept)
```

**依赖注入点**：`getDb`（`Depends`）、`getCurrentUser`（写路径取 `actor` / `departments`）。

**校验项目录**（新增校验项只加常量，不改表结构）：

| code | targetType | severity | 判据 |
|---|---|---|---|
| `SNAPSHOT_INTEGRITY` | RELEASE | ERROR | 快照规范化 / 体量 / 哈希一致 |
| `MANIFEST_SCHEMA` | RELEASE | ERROR | Manifest 满足 v1 契约 |
| `METRIC_SOURCE_PRESENT` | METRIC | WARNING | 指标有源类引用 |
| `METRIC_NUMERATOR_LANDING` | METRIC | WARNING/ERROR | 分子度量有落点类 |
| `METRIC_DENOMINATOR_LANDING` | METRIC | WARNING/ERROR | 分母度量有落点类（RATIO/DIFFERENCE 才有意义） |
| `CHECKS_TRUNCATED` | RELEASE | ERROR | 结论条数超上限（报告不完整 ⇒ 阻断） |

## 6. 测试

**入口**：`./scripts/run_local_tests.sh`（唯一入口，5434/qa_metadata_test + Neo4j 7688）。

**新增 / 变更用例**：

- `app/tests/unit/test_semantic_release_check_models.py`（8 条）—— ORM ↔ 迁移**双向**钉死：
  列名 / 类型 / nullable / 注释 / 索引 / CHECK 表达式 / FK 的 `ON DELETE CASCADE`；
  ⚠️ `alembic check` 只比名不比式 ⇒ 表达式索引用 `index.expressions[-1]` 逐字断言。
- `app/tests/unit/test_semantic_release_checks.py`（24 条）—— 纯逻辑：逐项分级、
  闸门零漂移（`checksToGateResult(evaluatePublishChecks(m)) == evaluatePublishGate(m)` 逐条比对）、
  确定性、容错、去重；**本批新增 4 条 `capChecks` 用例**（上限从契约推导 / 上限内不截断 /
  截断产生 ERROR 合成行 / 闸门与落表同源）。
- `app/tests/unit/test_semantic_release_check_service.py`（3 条，本批新增）—— `toCheckDict` 字段映射
  与「不返回 ORM 实例」。
- `app/tests/unit/test_semantic_release_service.py`（+3 条）—— `_mapCreateIntegrityError`
  **按约束归因**：check 表约束 ⇒ 自己的冲突文案（不得误报「版本已存在」）；
  release 自己的唯一索引 ⇒ 仍是版本冲突。
- `app/tests/integration/test_semantic_release_service.py`（+3 条，本批新增）—— **失败路径留痕**：
  validate 被拦 ⇒ 恰好一条审计、`blocked=true`、`phase="validate"`、记下 `blockers`；
  activate 被拦 ⇒ `phase="activate"`、状态原地不动；成功路径**不得**多写 blocked 审计。
  另：`test_replace_checks_truncates_beyond_the_cap` 改为 monkeypatch **checks 模块**
  （`capChecks` 在那里读常量；打在错模块上会得到「没截断」的假绿）并期望 `CHECKS_TRUNCATED` 合成行。
- `app/tests/integration/test_semantic_release_api.py`（+3 条，本批新增）—— 分页：
  `total`/`summary` 覆盖全部行（翻页不改结论）、越界参数 422、
  409 响应里的阻断项数 == 表里 FAIL/ERROR 行数（同一份事实源）。

**结果**：

```
157 passed（unit 55 + integration 96 + 新增 6）
覆盖率（4 个新增/改动模块）：97.67%（门槛 80%）
  semantic_release_checks.py        100%
  semantic_release_check_service.py  98%
  semantic_release_service.py        96%
  semantic_release_check_models.py  97%
```

**`alembic check`**：测试库（5434/qa_metadata_test）**绿**（`No new upgrade operations detected`）。
⚠️ 开发库（5433）报漂移，但那是**历史残留的备份表**（`wiki_page_20260930` /
`PORDER` / `BPSUPPLIER` 等 9 张），`scripts/check_schema_drift.py` 明确归类为
「DB 多余对象（历史残留，1 类），默认不阻断启动」，**与本批无关**（`semantic_release_check` 不在其中）。

## 7. 安全审查

触发 `security-reviewer`（新增表 + 新读写端点 + 用户输入进入 DDL/查询路径）。

| 级别 | 结论 | 处置 |
|---|---|---|
| **HIGH** | 上限写死 `6000` < 契约允许的 `6002` ⇒ 合法 manifest 被截断，**丢的是尾部**（往往正是最后一条指标的 ERROR 结论行） | ✅ 已修：上限从 `MAX_MANIFEST_METRICS` 推导；截断改为显式 `CHECKS_TRUNCATED` ERROR 合成行（fail-closed） |
| **HIGH** | 失败路径不写 `audit_log` ⇒ 证据被覆盖写后彻底消失 | ✅ 已修：`_recordBlockedAudit`（action 复用封闭词表里的 `UPDATE`，before/after 都给） |
| **MEDIUM** | 闸门与落表若各自算一遍 ⇒ 静默错位 | ✅ 已修：单一事实源（`draftChecks` 一份算两处） |
| **LOW** | `_mapCreateIntegrityError` 一律报「版本冲突」⇒ 撞 check 表约束时误导调用方 | ✅ 已修：按 `exc.orig.diag.constraint_name` 归因 |
| **LOW** | `SemanticReleaseCheckPage` 名叫 Page 但没有分页 | ✅ 已修：`limit`/`offset` + `total`/`summary` 覆盖全部行 |
| — | SQL 注入 | ✅ 无。全部走 SQLAlchemy 表达式参数化；无字符串拼接 SQL |
| — | 越权 | ✅ 端点整组挂在 `requireAdmin` 下；校验结论含治理元数据（谁查的、拦在哪一条）⇒ 非 admin 403 |
| — | 输入校验 | ✅ `limit`/`offset` 由 FastAPI `ge/le` 约束；`releaseId` 1~`MAX_RELEASE_ID` |
| — | 敏感信息 | ✅ 落表内容是校验结论，不含 DSN/口令/密钥 |
| — | 迁移安全 | ✅ 纯加法 DDL，无 DROP；FK CASCADE 只影响本表 |

**结论**：0 CRITICAL，0 未处置 HIGH/MEDIUM/LOW。

## 8. 部署验证

**未部署**（用户决策：生产暂不部署，功能全完成后统一发版）。
开发库 5433/qa_metadata 已应用 0154 + 0155。

**真机验证脚本**（§67 开发门禁）：
`backend/scripts/semantic_release_check_realdata.py`

```
cd backend
DATABASE_URL='postgresql+asyncpg://<user>:<pass>@localhost:5433/qa_metadata' \
    uv run python -m scripts.semantic_release_check_realdata --allow-prod-write
```

**输出（真实治理数据 · 2026-11-18 实测）**：

- 真实清单：**59 类 / 12 指标（MEASURE 3 / RATIO 9）/ 12 KPI / 14 JOIN / 5 域**；
  指标状态全 DRAFT；
- 逐项校验 **33 条**（分子落点 12 全败 / 分母落点 9 全败 / 源类 3 过 9 败）；
- 闸门：blockers 0 / warnings 20（封顶）/ canPublish **true**；
- 落表 **35 行**，5 个 check code（passed 5 / failed 30 / errorRows 0）；
- 缺陷样本（真实比率指标翻成 PUBLISHED 且清空落点类 + 重算哈希 ⇒ 完整性过、闸门硬阻断）：
  - activate 被 **409** 拦下（2 项 ERROR）；
  - 状态停在 **VALIDATED**；
  - check 表里查到那 **2 条 ERROR 行**；
  - `audit_log` 里查到 **1 条 blocked 审计**（`action=UPDATE` / `phase=activate` / `blockerCount=2`）；
- 收尾：删 2 个发布 ⇒ CASCADE ⇒ **remainingCheckRows = 0**；
- 连跑 3 次幂等；开发库最终仍只有原 2 个发布 + 0 check 行。

**脚本安全**：`DATABASE_URL` 必须显式在环境变量里（argv 只接受**环境变量名**，不接受字面量）；
写库闸走 `scripts/kpi_metric_db_guard.assertSafeTarget`（`qa_metadata` 需 `--allow-prod-write`）；
`databaseNameOf` 打码库名；`_silenceSqlEcho()` 关 SQL 回显。

## 9. 关联

- **计划**：[`docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md`](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md)
  §M1 表 1.2 行 + **M1-a 结构闸第 3 条**（validate ERROR 可落表定位）
- **排期**：[`docs/update/1118/智能采购平台_V0.3落地实施计划_执行排期_1118.md`](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_执行排期_1118.md) §W5
- **Wiki**：[`Harness/wiki/data-model.md`](../../wiki/data-model.md)（§M1 增量：发布校验结论表）
- **Rules**：[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md) ·
  [`Harness/rules/测试规范.md`](../../rules/测试规范.md) ·
  [`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md)
- **上游变更**：[M1-1.1a 源绑定两张表](../2026-11-18-m1-procurement-binding-tables/summary.md)（0154）·
  [M1-1.1b 绑定行为层](../2026-11-18-m1-procurement-binding-service/summary.md)·
  [P1b 语义发布底座](../feat-semantic-release-a/summary.md)（D8–D11）
- **下游**：M1-1.3 采购域发布编排（**待建**）⇒ 采购域校验项（含「缺 JOIN 的绑定」）
  接进**同一张 check 表**（机制先立，判据后加）
- **Memory**：`qa-system-m1-semantic-release-check`

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C / D 四候选 + 三个关键取舍）
- [x] 第 3 段迁移文件名 ≤ 32 字符（`0155_semantic_release_check.py` = 31）
- [x] 第 7 段触发了 security-reviewer ⇒ 填了 HIGH/MEDIUM/LOW 处置表（0 CRITICAL）
- [x] 第 8 段真实数据验证脚本输出贴出（59 类 / 12 指标 / 35 行落表 / 409 阻断 / 2 条 ERROR 行 / 1 条 blocked 审计 / 残留 0）
- [x] 第 9 段 ≥ 3 个跨文件链接（计划 / 排期 / Wiki / Rules / 上游变更 / Memory）
- [x] 相关 wiki 文档已更新（`Harness/wiki/data-model.md` 整节）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 添加
- [x] 涉及真实 SQL/DB 改动 ⇒ `scripts/semantic_release_check_realdata.py` 已跑通（exit 0，幂等 3 次）
