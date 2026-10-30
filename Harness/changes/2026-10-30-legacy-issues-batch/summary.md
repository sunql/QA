# 变更：2026-10-30-legacy-issues-batch

- **日期**：2026-10-30
- **作者**：Claude（与用户确认四项工作范围）
- **Phase**：Phase 6.7 KPI 收尾 + 工程基建（CI / 覆盖率）
- **状态**：done（本地全绿；**未 push / 未建 PR / 未改 prod 任何开关**）
- **分支**：`feat/遗留问题处理`（worktree `feat-kpi-metric-technical-form`，base main @ `1b9f4f71`）
- **迁移版本**：无新迁移（alembic head 仍为 `0137`）
- **关联变更**：
  - predecessor：[2026-10-28-kpi-metric-technical-form](../2026-10-28-kpi-metric-technical-form/summary.md)（本批 = 其 §11 遗留项的工程化收尾）
  - predecessor：[kpi-l1-metric-id](../kpi-l1-metric-id/summary.md)（B3 门禁语义来源）
  - predecessor：[feat-agent-orchestration](../feat-agent-orchestration/summary.md)（同因事故：无 CI ⇒ main 红了没人发现）
- **MEMORY**：[qa-system-legacy-batch-1030](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-legacy-batch-1030.md)

---

## 1. 需求

用户选定四项遗留问题一次处理（分支主题 `feat/遗留问题处理`）：

| # | 需求 | 验收标准 |
|---|---|---|
| R1 | 修 KPI 遗留缺口 | B3 注入面补「无挂靠 metric ⇒ 不注入」门禁；所有文档/脚本/测试的开关口径收敛到 fail-closed |
| R2 | KPI 注入开关灰度打开 | **先关后开**：prod 已写 `KPI_CONTRACT_INJECTION_ENABLED=false`；本批完成演练工具链与手册，真正 flip 待业务方治理列对齐后**人工**执行 |
| R3 | 补 CI | `.github/workflows/ci.yml` 落地，main/PR 自动跑前端与后端，杜绝「main 红着没人发现」（2026-10-13、2026-10-18 两次同因事故） |
| R4 | 覆盖率先行 | 前端 functions ≥ 80% 后才允许启用覆盖率门禁；实测达标后门禁直接开 |

## 2. 设计评审

**R1（B3 门禁 + 口径收敛）**：

| 方案 | 判定 |
|---|---|
| 保持旧口径「无 metric 也注入治理文本」 | ❌ 违反 spec §7.1 Tier C 定义与安全评审 M-3（任意登录用户可建治理列，未挂 metric 的口径文本进入他人 prompt） |
| **无挂靠可用 metric ⇒ Tier C ⇒ 不注入（mismatch 同；unknown 允许）** | ✅ 选定；与 chat 侧 Tier B 面同一决策 |
| 反向只测「不注入」 | ❌ 恒真假绿 —— 必须同用例内给 KPI 挂上 metric 断言「确实注入了」 |

**R2（S5 realdata 语义）**：总闸 fail-closed 后「契约块为空」有两种原因（闸门关 / 对账拦截），不先判闸门 ⇒ 「全 mismatch ⇒ 块为空」恒真（假绿）、「非 mismatch ⇒ 块非空」恒假（假红）。方案：入口先读总闸——prod 关 ⇒ SKIP 且理由写明「空块是闸门不是对账拦截」；隔离测试库关 ⇒ `runWithSwitch` 临时开闸验证、`finally` 原样还原。

**R3（CI 形态）**：

| 方案 | 判定 |
|---|---|
| 单 job 跑全部 | ❌ 后端 unit（26 min）与 integration（297 文件）串行太慢且 env 不同 |
| **3 job：frontend / backend-unit / backend-integration×4 分片** | ✅ integration 按文件字典序分 4 片并行 |
| 对已知失败做 deselect 保绿 | ❌ 实测推翻了旧基线（见 §6），deselect = 无证据掩盖 |

**R4（覆盖率口径）**：vitest 覆盖率分母已排除测试文件自身（`ac933838`），本批补页面级测试后四维全部 ≥ 80% ⇒ 门禁直接启用，不再像旧计划那样「达标前不启用」。

## 3. 数据模型变更

无。`system_config` 的 `KPI_CONTRACT_INJECTION_ENABLED=false` 行已于 2026-10-30 部署时写入 prod（见 predecessor §11.3），本批未做任何库写入。

## 4. 接口契约变更

无对外 API 变更。行为变化（开关关闭时）：nl2sql plan prompt 与 chat Tier B prompt 在「KPI 无挂靠 metric」时不再出现治理契约块 —— 这是 fail-closed 语义的补全，不是新契约。

## 5. 实现要点

| 块 | 文件 | 要点 |
|---|---|---|
| B3 门禁 | `backend/app/services/kpi_contract_reconcile.py` | `kpi.metric_id` 为 None 或 metric 不存在 ⇒ `continue`（Tier C），log warning；mismatch 同样不注入 |
| 总闸收敛 | `backend/app/services/kpi_metric_switches.py` | `isKpiContractInjectionEnabled` 固定读 `SWITCH_CONTRACT_INJECTION`，不再开放 `key` 参数（两条注入面不可被调用方拆成不同决策） |
| S5 语义 | `backend/scripts/kpi_metric_realdata_inject.py` | 入口判总闸；prod SKIP 三态理由；测试库 `runWithSwitch`（helper 抽到 `kpi_metric_realdata_db.py` 共享，S4 复用） |
| 文档口径 | `Harness/wiki/kpi-metric-technical-form.md`、predecessor summary §8/§11、`kpi_metric_backfill.py`、`docs/kpi-metric-backfill-review.md` | 「默认 true」旧口径全部改为 fail-closed；env 同名变量明确为紧急杀闸（置 true 不会打开灰度） |
| chat actor | `backend/app/services/chat_domain.py`、`backend/app/services/actor_departments.py`（`77b42531`） | `/define` `/map` 补传 actor ⇒ 修自 2026-09-03 起生产 TypeError；tuple/string 归一化统一一处 |
| CI | `.github/workflows/ci.yml`（`f9ca88f5`） | frontend（tsc + coverage 80% 四维门禁）/ backend-unit（PG16 服务容器 + `alembic upgrade head` + `uv sync --frozen`）/ backend-integration（PG + Neo4j 7688 + 隔离 Milvus `MILVUS_TEST_DB_NAME=qa_ci`，297 文件 4 分片）；**无任何 deselect** |
| 前端测试 | 5 个测试文件（`2fcbee05`，+4165 行） | AdminWikiPagesPage 37 / BulkImportModal 14 / DataQualityPage.extra 36 新文件 + 两文件扩充；`AdminWikiPagesPage.handleCreate` validateFields 挪进 try（修 unhandled rejection，同 AgentRegistryPage 既有教训），测试文件级 `afterEach` 零泄漏守卫（行为 RED 验证过） |

## 6. 测试

| 套件 | 结果 |
|---|---|
| KPI integration 3 文件 | 40 passed |
| realdata 相关 unit | 53 passed（含新 `test_kpi_metric_realdata_s5_switch.py` 3 分支：闸开直跑 / 测试库临时开闸 / prod SKIP 理由） |
| `test_kpi_metric_backfill_plan.py` | 63 passed（审核清单断言同步 fail-closed + 反向断言禁「默认 true」回潮） |
| 前端 5 文件定向 | 137 passed（修复后三轮收敛：5 失败 → 1 → 0） |
| 前端全量 + coverage | **222 文件 / 2198 tests 全绿**；覆盖率 **lines 83.46 / functions 82.43 / branches 84 / statements 83.46**（四维 ≥ 80 ⇒ `vitest.config.ts` 既有门禁可直接生效） |
| `tsc -b` | 0 错误（修掉 2 处 `Array.prototype.at` 需要 ES2022 的类型错误） |
| ruff | 0 告警 |
| 后端全量 unit（本分支） | **15 failed / 5029 passed** —— 1 条是本批 KPI 测试（已修），14 条为 main 既有红（见下） |

**基线三角定位（重要，修正了 MEMORY 旧记录）**：在与 main 完全相同的环境下复跑，main @ `1b9f4f71` 同样 14 条失败（test_chat_service 4 / test_chat_service_stream 2 / test_datasource_pool 2 / test_dependencies 1 / test_intent_classify_order 1 / test_no_sqlite_fallback 1 / test_rate_limit 3），且**不是** MEMORY 记录的「28 红全在 test_refine_shortcuts」——`test_refine_shortcuts.py` 在 main 上已 59/59 全绿（旧基线被近期合并修复，基线漂移）。最小 env（不 source `.env`）下 14 条依旧失败 ⇒ 非 env 伪影。CI 因此**不做任何 deselect**：首跑如实暴露这 14 条，修复属独立批次。

## 7. 安全审查

security-reviewer（haiku，2026-10-30）对 `1b9f4f71..HEAD` 全量后端 diff + ci.yml 的审查结论：**无 CRITICAL / 无 HIGH / 无阻断项**。四个重点：① prod 库写入守卫链完整未被削弱（S5 临时开关 prod 不可达 + 反向单测守卫）；② fail-closed 门禁不可绕过（两扇面共用同一决策，全仓 grep 无第二处直读 `SWITCH_CONTRACT_INJECTION`；Tier C 门禁修掉了原「无 metric 的 KPI 直接进注入集」的真实越权注入面）；③ ci.yml 干净（最小权限、无 secrets、无 `pull_request_target`、CI 假值仅存在于 runner 本机 ephemeral 容器）；④ chat actor 修复未引入注入/越权（departments 来自服务端而非客户端自由文本）。

**MEDIUM ×2（既有问题，非本批引入 → 登记为后续批次，本批不仓促改）**：
1. `chat_domain.py` 4 处 actor 用 `user.userId` 而非 `dbUserId`（stub AUTH_MODE 下 departments 可经 `X-User-Id` 伪造；本批让 /define /map 首次落库、扩大了可写面）——改身份格式波及 audit_log 全链路，需独立小设计。
2. B3 注入的治理文本（numerator/denominator/grain/unit 各 120 字符）只截断无反注入消毒（fence neutralize / 结构剥离）——Tier C 门禁已消掉最大一块，残余面待独立批次。

**LOW ×3**：③ `runWithSwitch` 无内置 prod 断言 → **本批已修**（TDD：落库实测库名 == `qa_metadata` 即 SystemExit 且零开关读写，`test_run_with_switch_refuses_prod_database_regardless_of_isProd` RED→GREEN）；④ `actor_departments` 对契约外类型（set）降级为脏串 + 部门名含逗号往返有损 → 随 MEDIUM-1 批次处理；⑤ S5 总闸开时 prod 会跑 `cache.ensureFresh`（既有行为，`--allow-prod-write` 已前置闸住，备忘）。

- B3 门禁针对的正是安全评审 M-3（治理列任意登录用户可建 ⇒ 未对账文本不得进他人 prompt）——本批是其落地。
- CI 密钥全部使用 CI 作用域假值（`ci-postgres-only` 等），无仓库 secret 依赖；Milvus 仅在 integration job 内起**隔离容器**（`MILVUS_TEST_DB_NAME=qa_ci`），不触本机 19530 生产实例。密钥扫描：全量 diff 无硬编码密钥。

## 8. 部署验证

本批不含需部署的运行时行为变更以外的代码（B3 门禁与 actor 修复需要随下次镜像重建生效；CI 与测试不进运行时）。真库只读演练：

```bash
bash /tmp/run_realdata_dev.sh --allow-prod-write   # 目标库守卫要求的只读运行方式
# RESULT: SKIP steps=9 passed=6 failed=0 skipped=3
# S5 正确 SKIP，理由=总闸关闭（「空块的原因是闸门，不是对账拦截」）
# S3 实测 12/12 mismatch（与 PLAN 登记偏差一致）
```

dev `qa_metadata`（与 prod 同名，走 prod 路径）实测：kpi=12 / metric=12 / linked=12 / alembic=0137 / `KPI_CONTRACT_INJECTION_ENABLED=false`。无 `--apply-backfill`，无生产写入。

**下次部署步骤**（后端镜像重建后生效）：`docker compose build --no-cache backend && up -d backend`；冒烟 `curl /openapi.json | jq '.paths | keys'`。前端无运行时改动，无需重建。**CI 首跑**在 push 后由 GitHub Actions 执行，预期暴露 §6 的 14 条既有红（修复为独立批次）。

**R2 真正打开灰度的前置**（全部满足后由**人工**写 `system_config` value=true，不由脚本）：
1. 业务方完成治理列对齐（A 档 4 条先行，12/12 mismatch 现状见 predecessor §10.2）；
2. 隔离测试库演练通过（S5 审核模拟路径）；
3. 部署含 B3 门禁的新后端镜像。

## 9. 关联

- **Wiki**：[kpi-metric-technical-form](../../wiki/kpi-metric-technical-form.md)（§8 开关表 fail-closed 口径）
- **Rules**：[测试规范](../../rules/测试规范.md)（真实 PG 强制）、[开发流程规范](../../rules/开发流程规范.md)
- **Memory**：[qa-system-legacy-batch-1030](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-legacy-batch-1030.md)
- **关联变更**：[2026-10-28-kpi-metric-technical-form](../2026-10-28-kpi-metric-technical-form/summary.md)、[feat-agent-orchestration](../feat-agent-orchestration/summary.md)（无 CI 同因事故）、[kpi-l1-metric-id](../kpi-l1-metric-id/summary.md)
- **commits**：`27c345c3`（总闸 fail-closed）→ `77b42531`（actor 修复）→ `ac933838`（API 层测试）→ `b3abdebe`（B3 门禁）→ `c9e77bb2`（文档口径）→ `2fcbee05`（前端测试+rejection 修复）→ `8a133802`（KPI 清单断言）→ `f9ca88f5`（CI）

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段非空，无占位
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段无迁移（无需文件名校验）
- [x] 第 7 段 security-reviewer 结论已回填（无 CRITICAL/HIGH；2 MEDIUM 登记为后续批次；LOW-3 已修）
- [x] 第 8 段真库只读演练输出贴出（本批无运行时部署）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] wiki 已更新（kpi-metric-technical-form.md 开关表）
- [x] MEMORY 索引已加
- [x] 真实数据验证：realdata 只读跑通（本批无 DB schema 改动）
