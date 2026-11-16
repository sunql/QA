# 变更：feat-knowledge-facade

- **日期**：2026-11-16
- **作者**：Claude
- **Phase**：P4 知识服务 Facade（D32–D37）
- **状态**：**in-review**（PR 待合；bypass squash 合并后切 `done`）⇒ **P4 / G5 完成**
- **关联变更**：[feat-semantic-nl2sql-call-sites](../feat-semantic-nl2sql-call-sites/summary.md)（P3d，工具 / 召回接 Registry）·
  [feat-semantic-agent-snapshot](../feat-semantic-agent-snapshot/summary.md)（P3c，Agent 快照）·
  [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b，图谱发布镜像）·
  [feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/summary.md)（P2，schema 渲染面）·
  [feat-semantic-registry-rag](../feat-semantic-registry-rag/summary.md)（P3a，向量面）
- **迁移版本**：无（**零 DDL**）
- **MEMORY**：[qa-system-knowledge-facade](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-knowledge-facade.md)

---

## 1. 需求

**背景**：P2/P3 已经把「语义发布」做成了**按发布读**的机制（schema 渲染 / 向量过滤 / 图谱镜像 /
工具与召回），但**没有任何一个面向用户提问的读侧消费面**——知识只在治理界面里看，问不出来。
同时跨板块扩展要求「**同一接口换域参数**即可服务下一个板块」，采购侧还要求一条**冻结路径**的薄别名。

**验收标准（G5）**（用户视角、可测量）：

| # | 判据 | 口径 |
|---|---|---|
| G5-1 | **跨域可用** | 同一接口 `/api/v1/semantic/knowledge/query` 换 `domain` 参数即可服务第二个域，**零代码改动** |
| G5-2 | **采购薄别名无逻辑** | `/api/v1/procurement/knowledge/query` 只做 `domain` 固定转发；别名文件里**不许出现任何业务分支** |
| G5-3 | **试点问题集端到端** | 试点问题集逐条跑通，**通过率 100%**（三项判据同时成立） |
| G5-4 | **权限过滤双向** | 有权限 → 查得到；无权限 → **查不到**（空结果，**不是 403**） |
| G5-5 | **引用可回溯** | 每条 evidence 带 `releaseId` + `releaseVersion`；发布面不可用时带**显式降级标记** |

## 2. 设计评审

**候选 A：Facade 内调 LLM 出答案** vs **候选 B：只出证据 + 确定性编排摘要**（采纳）

| 维度 | A 调 LLM | B 确定性编排 |
|---|---|---|
| 通过率可判定 | ❌ 主观评分（G5-3 要 100%） | ✅ 证据命中 / 权限结果 / 引用可回溯**客观判定** |
| 计量 / 预算 / 评测 | ❌ 引入三张皮，且与 chat 重复编排 | ✅ 零新增面 |
| 契约契合 | — | ✅ 字段本叫 `answerDraft`（草稿，给上层润色） |
| 结论 | ❌ | ✅ **采纳** |

**候选 Ⅱ：权限源放在请求里（调用方显式传） vs **服务端权威推导**（采纳）

| 维度 | 请求显式传 | 服务端推导 |
|---|---|---|
| 越权风险 | ❌ 调用方自己声明有权 | ✅ 域可见性由 `data_domain.primary_org_id → organizations.code ∈ user.departments` 推导 |
| 现状 | P3a 明确「策略接线留给后续」 | P4 就是那个后续（**提前到 D33 落地**） |
| 结论 | ❌ | ✅ **采纳**（请求里的 scope **只能收窄**） |

**候选 Ⅲ：发布面不可用时（a）报错（b）假装有版本（c）降级直读 + 显式标记** ⇒ 采纳 **(c)**，但**实现口径收紧**：
降级 = **空结果 + 显式降级标记**，**不直读治理表**（直读 ⇒ 证据没有版本可引 ⇒ 与「引用可回溯」直接矛盾）。
`source=LIVE` 因此**本批不产出**（常量保留）。
（与 P2/P3a 既有口径一致：坏快照不许炸问答，但不许静默。）

**候选 Ⅳ：域可见性与发布面的先后** ⇒ **先判可见性，再解发布面**（D35）
⇒ 域不可见时**一路都不跑**、**连发布面都不解** ⇒ 版本号在不在不能当「域存不存在」的探针。

**候选 Ⅴ：试点「别名无逻辑」怎么证** —— ① 只看源码；② AST 守卫（无分支）；③ **AST + 运行时等价**（采纳）
⇒ ② 只能证「没有分支」，证不了「同一逻辑」；③ 用**真实数据**跑两遍并逐项比对响应。

## 3. 数据模型变更

**PG 侧：无**（零 DDL）。本批只新增**服务层、路由与读侧权限推导**，不加表不加列不加索引。

试点数据集（**隔离测试库** `qa_metadata_test_pilot`，**生产未播种**）复用迁移 0040 已种的
5 个本体类与 3 个数据域，只补别名 / 属性 / JOIN / 指标 / KPI / 业务对象；
`data_domain.primary_org_id` 是**权限推导的唯一抓手**（`PROCUREMENT→PROCUREMENT_ORG`、
`QUALITY→QUALITY_ORG`、`LOGISTICS` 保持 NULL ⇒ fail-closed 样本）。

## 4. 接口契约变更

| 契约 | 内容 |
|---|---|
| `POST /api/v1/semantic/knowledge/query` | 请求：`domain` / `question` / `channels` / `entityHints` / `topK` / `releaseId` / `scope`（**只收窄**）；响应：裸 DTO（`release` / `answerDraft` / `entities` / `evidence` / `routeResults` / `warnings`），**不套 `{success,data}` 信封** |
| `POST /api/v1/procurement/knowledge/query` | **采购薄别名**：请求体少 `domain`（固定 `PROCUREMENT`），其余逐字一致；**别名文件零业务分支** |
| `routeResults[].status` | `OK` / `EMPTY`（查了没命中）/ `DEGRADED`（能力不可用）/ `DENIED`（**域不可见**）——**EMPTY 与 DENIED 必须分开** |
| `evidence[].source` | **本批恒为 `RELEASE`**（发布读，带 `releaseId` + `releaseVersion`）；`LIVE` 是**保留常量，本批不产出** ⇒ 降级时是**零证据 + 显式降级标记**，不是「无版本的证据」 |
| `scope.entityTypes` | **真的收窄**（请求侧 ∩ 可见集 ⇒ 交集为空则**空结果 + 警告**）；`scope.maxSecurityLevel` **本批不被消费**（用户级安全等级无权威来源 ⇒ **显式告警**，不默默忽略） |
| 错误码 | 参数非法 → 422；未登录 → 401；限流 → 429；**域不可见 → 200 + 空结果 + 警告**（不 403） |
| 开关 | `ENABLE_KNOWLEDGE_FACADE`（默认 **false** ⇒ 整组路由不注册 ⇒ 404） |

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/app/domain/knowledge_schemas.py` | 请求 / 响应 DTO（含 `scope` 收窄口径） |
| `backend/app/services/knowledge/facade.py` | **编排层**：解 scope → 判域可见性 → 解发布面 → 跑四路 → 组装响应；单路异常兜底降级 |
| `backend/app/services/knowledge/permission.py` | **统一权限源**：域可见性 / 组织 / 实体类型 / 安全等级；请求 scope 只收窄 |
| `backend/app/services/knowledge/domain_slice.py` | 发布 manifest → **域切片**（类 / JOIN / 指标 / KPI），跨域 JOIN 是边界 |
| `backend/app/services/knowledge/release_context.py` | 发布面解析（钉住 / ACTIVE / 降级三态）；**钉住的发布先做可见性校验**（域不匹配 / 非 admin 看不到该状态 ⇒ 降级，且**不区分「不存在」与「无权」**） |
| `backend/app/services/knowledge/{matching,graph_route,metric_route,rag_route,sql_route}.py` | 匹配与四路实现（`RouteInput` → `RouteOutcome`） |
| `backend/app/services/knowledge/pilot_eval.py` | **纯函数**判定模块（试点判据可单测） |
| `backend/app/api/v1/{knowledge_facade,procurement_knowledge}.py` | 两个路由（别名薄转发） |
| `backend/scripts/knowledge_pilot_seed.py` | 试点数据集播种 + **真实发布写路径**（草稿→校验→激活）；**权限语义变更默认拒绝**（改绑域主组织 / 替换同域生效发布 ⇒ 要 `--force`） |
| `backend/scripts/knowledge_pilot_runner.py` | 试点跑批（**只读**）+ JSON / Markdown 报告 |

**关键实现点**

- 四路**统一收 `RouteInput`（`scope` 必填）** ⇒ 「四路同一口径」由**构造**保证，不靠约定；
- 编排层**只有一个**调用点，四路各自 try/except ⇒ 单路炸不拖垮整次查询；
- 域切片让「换 domain 换结果」成为**可被数据证明**的事实（同一问题在两域结果不同）；
- 采购薄别名**零分支**（AST 守卫）+ 运行时等价判据（真实数据两跑逐项比对）；
- **钉住的发布也要授权**：`getById` **不查域也不查状态**（裸 `session.get`）⇒ 编排层补校验（域匹配 + 非 admin 只见 ACTIVE），
  拒绝时与「发布不存在」**同一条消息** ⇒ 不给「它存不存在」的探针；
- **收窄要诚实**：`scope.entityTypes` **真的收窄**（空交集 ⇒ 空结果 + 警告，不回落成「全给」）；
  `scope.maxSecurityLevel` **本批不消费** ⇒ **显式告警**而不是默默忽略。

## 6. 测试

| 文件 | 用例 | 关键场景 |
|---|---|---|
| `test_knowledge_pilot_eval.py`（新增） | 16 | 五项判据正反例 + 问题集解析（`scope` 读取 / 缺省 / 非法）+ **「一路都没结果」不算有权查**（原判据把它当 VISIBLE ⇒ 假绿） |
| `test_knowledge_release_context.py`（**新增**） | 11 | 钉住发布的**授权**：同域 ACTIVE 可用 / 跨域拒绝（**admin 也拒绝**）/ 非 admin 拒绝 DRAFT·VALIDATED·ARCHIVED / admin 允许 DRAFT / **缺失与越权同一条消息**（无存在性探针） |
| `test_knowledge_procurement_alias.py` | 5 | AST 守卫（自身也能抓到分支）+ 不引业务服务 + 只暴露一个端点 + 别名固定域 |
| `test_knowledge_permission_matrix.py` | 6 | 三轴矩阵：scope 同源 / 收窄唯一入口 / 域不可见四路 DENIED 且一路不跑 / 不解发布面 / 降级剥版本 / 真路由逐条带版本 |
| `test_knowledge_facade_service.py` | — | 编排层：`releaseResolver` 收到的 admin 标志取自 scope；`entityTypes` 收窄真生效（收窄到 SUPPLIER ⇒ METRIC=EMPTY）/ 收窄到不可见类型 ⇒ 空结果 + 警告 / `maxSecurityLevel` 报为「被忽略」 |
| `test_knowledge_domain_slice.py` | — | `entityTypes` 收窄（保留父类闭包及其指标）/ `()` ⇒ 空切片 / 未知域 ⇒ 空切片 |
| 既有知识相关 unit | — | 知识相关 unit **全绿**（D37 回归取证见 §8） |
| `test_semantic_release_api.py`（**既有文件**追加，不新建） | 59 | 逐路引用可回溯 / 请求侧收窄致域不可见 / 实体提示不放宽 / **钉住发布的可见性（集成层）** |
| `test_route_auth_guard.py`（既有） | 4 | 开关打开时新路由**必须挂鉴权**⇒ 全绿 |
| **试点跑批（真实数据）** | **14 条** | **通过率 100%**（三项判据同时成立；采购域再加 `alias_equivalence`） |

**TDD**：D32–D35 各批按 RED→GREEN→IMPROVE；D36 的 `scope` 支持曾先实现后补测试 ⇒
做了 **RED 验证**（临时改回 `scope=None` ⇒ 两条用例真红）再还原。

**回归**：净新增失败 **0**（见 §8）；`test_app_wiring.py::test_no_route_registered_after_catch_all_mount`
的红**在 origin/main 上同样红**（守卫找 `Mount("")`，实际 `main.py` 是 `Mount("/mcp")`）⇒ **非本批引入**，
按「不改既有红基线」原则登记为跟进项，不在本批修。

## 7. 安全审查

- **新增端点**：`/api/v1/semantic/knowledge/query` + 采购薄别名；鉴权 = 任意登录用户 +
  改密守卫 + 限流（`enforceRouteLimit`）；**只读**（facade 内部全是 SELECT），**不新增写路径**；
- **越权面**：域不可见 ⇒ 四路 `DENIED` + 零证据 + 警告，**且不侧漏域是否存在**（连发布面都不解）；
  请求 `scope` **只能收窄**（组织 / 实体类型取交集；**安全等级本批不消费** ⇒ 显式告警，不假装收窄了）；
  `primary_org_id IS NULL` 的域对非 admin **fail-closed**；
- **钉住的发布要授权**：`getById` 裸查（不查域 / 不查状态）⇒ 编排层补校验；
  拒绝时与「不存在」**同一条消息**（细节只进服务端日志）⇒ 不给存在性探针；
- **越权 ⇒ 空结果而非 403**：避免用状态码枚举域；
- **凭据**：本批无新增凭据；试点脚本口令一律 `maskDsn` 打码，DSN 由
  `$(docker exec … printenv POSTGRES_PASSWORD)` 代入，**从不打印**；
- **目标库**：播种脚本走仓内白名单守卫（`qa_metadata` / `qa_metadata_test*`），正式库需显式
  `--allow-prod-write`；本批只在**隔离测试库** `qa_metadata_test_pilot` 跑，**生产零写入**；
- **SQL**：facade 内部只经 ORM / 既有渲染器，**不拼接用户 SQL**（SQL 路只渲染命中候选类的 schema 片段）；
- **自查结论**：无 CRITICAL / HIGH 遗留。

## 8. 部署验证

**零 DDL、不部署**：开关 `ENABLE_KNOWLEDGE_FACADE` 默认 **false** ⇒ 整组路由不注册 ⇒ 对外 404；
未部署时行为与现状逐字一致。

**真实数据验证（试点跑批，隔离库 `qa_metadata_test_pilot`）**

```
$ PILOT_DATABASE_URL="postgresql+asyncpg://qa_user:$(docker exec qa-pg-a1 printenv POSTGRES_PASSWORD)@localhost:5434/qa_metadata_test_pilot" \
  uv run python -m scripts.knowledge_pilot_runner --out ../Harness/changes/feat-knowledge-facade/daily/pilot_report.json \
    --markdown ../Harness/changes/feat-knowledge-facade/daily/pilot_report.md
目标库：postgresql+asyncpg://qa_user:***@localhost:5434/qa_metadata_test_pilot
试点跑批：14/14 通过（通过率 100%，判据 100%）
  ✅ P-01 供应商交付天数｜GRAPH=OK、METRIC=OK、RAG=DEGRADED、SQL=OK
  ✅ P-06 来料合格率｜GRAPH=EMPTY、METRIC=EMPTY、RAG=DEGRADED、SQL=EMPTY   （采购域：质量域被切掉）
  ✅ P-07 来料合格率｜GRAPH=OK、METRIC=OK、RAG=DEGRADED、SQL=OK              （质量域：同一问题命中）
  ✅ P-08/P-09/P-10/P-11/P-13｜四路全 DENIED（无权 / 无主组织 / 请求侧收窄）
  ✅ P-12｜四路全 DEGRADED（域可见但无发布面）
  ✅ P-14 今天天气怎么样｜GRAPH=EMPTY、METRIC=EMPTY、RAG=DEGRADED、SQL=EMPTY
```

- **复跑同结果**（同一命令再跑一次仍 14/14 / 100%）⇒ 幂等且可复现；
- 引用版本：采购域 `pilot-2026.11.16-1`、质量域 `pilot-2026.11.16-2`；
  `DENIED` / `DEGRADED` 各格**不带版本号**；
- **路由装配实证**：真实 app（`ENABLE_KNOWLEDGE_FACADE=true`）里两个知识端点已注册
  （`/api/v1/semantic/knowledge/query`、`/api/v1/procurement/knowledge/query`），
  MCP 挂载点是路由表**最后一条** ⇒ 新路由不在 catch-all 之后；
- **回归**：知识相关 unit **172 例全绿**；集成 `test_semantic_release_api.py` **59 例全绿**；
  `test_route_auth_guard.py` 4 例全绿；净新增失败 **0**
  （唯一红项 `test_app_wiring.py::test_no_route_registered_after_catch_all_mount`
  **在 origin/main 上同样红** ⇒ 基线红，非本批引入）。
- **本机全量 unit 不作门禁**：`pytest app/tests/unit`（已 `--ignore` 已知红基线文件）跑到 83% 用 35 分钟以上，
  CPU 仅 5 分钟（阻塞在 Milvus / 测试库的已建立连接上）⇒ 本机全量不可作判据；
  回归判据走 **CI 分片 unit + merge-gate 的失败集合对差**（判 **净新增失败 0**，不要求全绿）。

## 9. 关联

- **每日证据**：[`daily/D32.md`](daily/D32.md) · [`daily/D33.md`](daily/D33.md) ·
  [`daily/D34.md`](daily/D34.md) · [`daily/D35.md`](daily/D35.md) · [`daily/D36.md`](daily/D36.md)
- **清单**：[`plan.md`](plan.md)（G5 判据 / 四路编排 / 10 条关键裁决 / 统一权限模型 / 试点口径 / 边界 L-P4-1..12）
- **试点资产**：[`daily/pilot_questions.json`](daily/pilot_questions.json)（14 条）·
  [`daily/pilot_report.md`](daily/pilot_report.md) · [`daily/pilot_report.json`](daily/pilot_report.json)
- **Wiki**：[`Harness/wiki/architecture.md`](../../wiki/architecture.md)（知识服务 Facade 章节）·
  [`Harness/wiki/api-reference.md`](../../wiki/api-reference.md)（两个端点与四路状态语义）
- **Rules**：[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md) ·
  [`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md) ·
  [`Harness/rules/测试规范.md`](../../rules/测试规范.md)
- **Memory**：`qa-system-knowledge-facade`

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B × 2 组以上）
- [x] 第 3 段零 DDL（无迁移）
- [x] 第 7 段安全自查（新增只读端点；越权面已封；凭据打码）
- [x] 第 8 段真实数据验证贴出（试点 14/14 = 100%，复跑一致）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] wiki 已更新；MEMORY 已添加
- [x] 真实数据验证脚本已跑通（`scripts/knowledge_pilot_runner.py`，只读、可复跑）
