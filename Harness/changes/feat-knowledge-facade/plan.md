# P4 知识服务 Facade —— 执行计划（D32–D37）

> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §10 ·
> 契约来源：[`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md`](../../../docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md) §6 P4
> 上游：[feat-semantic-nl2sql-call-sites](../feat-semantic-nl2sql-call-sites/plan.md)（P3d，D29–D31）·
> [feat-semantic-agent-snapshot](../feat-semantic-agent-snapshot/plan.md)（P3c）·
> [feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/plan.md)（P2）

| 项 | 值 |
|---|---|
| **变更** | P4 知识服务 Facade：`/api/v1/semantic/knowledge/query` + 采购薄别名 |
| **分支** | `feat/knowledge-facade`（worktree `/private/tmp/qa-graph-mirror` 续用），base = `origin/main` |
| **迁移版本** | **无**（零 DDL：只加路由 + 服务 + 读侧权限推导，不加列不加表） |
| **开关** | `ENABLE_KNOWLEDGE_FACADE`（默认 **false**；关 ⇒ 整组路由不注册 ⇒ 404） |
| **关联变更** | 同上行 |
| **状态** | **D32 完成**（契约 + 骨架 + 20 unit + 13 integration 全绿）；**D33 完成**（图谱路 + 指标路 + 统一权限源 + 域切片；知识相关 unit 109 例 / 集成文件 48 例全绿）；**D34 完成**（RAG 路 + SQL 路 + 四路统一输入协议；知识相关 unit 126 例 / 集成文件 49 例全绿）；**D35 完成**（三轴验收矩阵：身份 / 请求侧收窄 / 引用可回溯 + 错误处理；知识相关 unit 131 例 / 集成文件 54 例全绿）；**D36 完成**（采购薄别名收口 + 试点问题集端到端：试点 **14/14 通过（通过率 100%）**、别名运行时等价判据通过）；**D37 完成**（审查修复批 8 项 + 文档口径统一 + 回归取证：知识相关 unit **172 例** / 集成文件 **59 例**全绿，净新增失败 0）⇒ **G5 通过** |

---

## 1. G5 判据（本批的完成定义）

> **G5**：知识 Facade **跨域可用**；采购薄别名**无逻辑**。

| 判据项 | 口径 |
|---|---|
| 跨域可用 | 同一接口 `/api/v1/semantic/knowledge/query` 换 `domain` 参数即可服务第二个域（**零代码改动**）⇒ P6 的扩展性证据在 P4 就要能跑出来 |
| 采购薄别名无逻辑 | `/api/v1/procurement/knowledge/query` 只做 `domain` 固定转发 + 调用同一个 facade 方法；**别名文件里不许出现任何业务分支** |
| 试点问题集端到端 | 采购试点问题集逐条跑通（通过率 100%，见 §6） |
| 权限过滤用例**双向** | 有权限 → 查得到；无权限 → **查不到**（空结果，不是 403） |
| 引用可回溯到发布版本 | 每条 evidence 都带 `releaseId` + `releaseVersion`；发布面不可用时带**显式降级标记**（不许假装是发布读） |

## 2. 四路编排与「权限同源」

一次查询内四路**共用一个** `KnowledgeScope`（§4）—— 这是「一次查询内四路权限判定必须同源」的实现方式：
**scope 只推导一次**，四路各自拿它做**收窄**，不允许任何一路自己再推导一遍。

| 路 | 语义源 | 按发布读？ | 本批的接线方式 |
|---|---|---|---|
| **图谱路 GRAPH** | 语义层：类 / 属性 / JOIN 关系 | ✅ | 从**钉住的发布 manifest** 的 `classes[]` / `joins[]` 取（`semantic_registry_service.getActive/getById`）**（D33 ✅）** |
| **指标路 METRIC** | 指标 / KPI 定义 | ✅ | 同上，从 manifest 的 `metrics[]` / `kpis[]` 取（**仓内目前零消费方，本批是第一个**）**（D33 ✅）** |
| **RAG 路 RAG** | 向量召回 | ✅（注册表侧） | `searchRegistryEmbeddings(..., registryFilter=RegistryFilter(releaseId=…, domainCodes=…, orgCodes=…, entityTypes=…))`（P3a 闸门；**先判开关 ⇒ 能力未启用时零外部调用**）**（D34 ✅）** |
| **SQL 路 SQL** | 物理层：表 / 列 / 类型 / JOIN 键 | ✅ | `restoreSnapshotSchema`（P2 复原层）+ `nl2sql_schema.buildSchemaText`（**仓内唯一渲染器**），只渲染命中候选类（**不走 NL2SQL 闸门**，见裁决 9）**（D34 ✅）** |

> ⚠️ **指标路现状是纯直读**：`kpi_catalog_service` / `kpi_execution_service` / `kpi_sql_compiler` 全链路 **零 release 引用**。
> 但 manifest **已经含** `metrics[]` / `kpis[]`（`semantic_manifest_schema.py:239-240`）⇒ 指标路**按发布读**是**读现成数据**，不需要新建发布机制。

> ⚠️ **四路协议同形（D34 起）**：四路统一收 `RouteInput`（问题 / 域 / **发布面** / **权限口径** / 上限 / 会话），
> 统一返回 `RouteOutcome`（状态 / 证据 / 实体 / 警告 / 降级原因）⇒ 编排层只有**一个**调用点，
> 「四路同一口径」由**构造**保证（`scope` 在 `RouteInput` 里**必填**），不靠约定（见裁决 10）。

## 3. 关键裁决

| # | 裁决 | 理由 |
|---|---|---|
| 1 | **Facade 不调 LLM**：产出「证据 + 确定性编排摘要」，`answerDraft` 由路由结果确定性拼装 | ① 契约里字段名叫 `answerDraft`（草稿），本就是给上层润色的；② 引入 LLM 会带来计量 / 预算 / 评测三张皮，而 G5 的三条验收（试点问题集、权限双向、引用回溯）**在证据层即可客观判定**，不需要主观评分；③ 避免 Facade 与 chat 双份 LLM 编排 |
| 2 | **四路 + 一个通道枚举**：`{GRAPH, METRIC, RAG, SQL}`；D2 的「文档路」落在 **RAG 路内的文档源**（evidence kind = `DOCUMENT`） | 跨板块文档（P4 的上位契约）明确写「四路」；D2 的要求是「文档**空集也要正确返回**」，落在 RAG 路的文档源上即可满足，且**不投入文档解析/入库工作量** |
| 3 | **权限源由本批新建**（`knowledge/permission.py`）：服务端从 `CurrentUser` + `data_domain.primary_org_id` 推导**权威域集合**，请求里的 scope 只能**收窄**不能放宽 | 仓内至今**没有「用户 → 可见域/组织」的策略源**（P3a 的模块文档明写：「现有 ACL 只有 admin 级的 `assertCanModify`」）⇒ P3a 把维度做成了「调用方显式传入」，**策略接线被明确留给后续**。P4 就是那个「后续」——**提前到 D33 落地**（域可见性是「要不要取数」的闸门，不接就等于对任何域都发数据；D35 退化为验收矩阵） |
| 4 | 越权 ⇒ **空结果 + 警告**，不是 403 | 验收口径是「无权限**查不到**」；403 会把「域不可见」与「域不存在」混为一谈，且探测者能靠状态码枚举域 |
| 5 | 发布面不可用 ⇒ **空结果 + 显式降级标记**（`release.degraded = true`），**不直读治理表**（**修订**：初稿写的是「降级直读 + `source=LIVE`」） | 与 P2/P3a 的既有口径一致（坏快照不许炸问答，但不许静默）。**但直读治理表与裁决 9 冲突**：一旦直读，证据就**没有版本可引** ⇒ 与 G5「引用可回溯」直接矛盾（且 `source=LIVE` 会让「每条证据都能回溯到一个发布版本」这条判据形同虚设）⇒ 降级只能是**空 + 标记**。`LIVE` 保留为常量（未来若确有直读语义再启用），**本批不产出** |
| 6 | 采购薄别名 = **独立文件 + 零业务分支**，只 `domain` 固定 + 转发同一个 facade 方法 | G5 第二条判据（「别名无逻辑」）需要**可检查**：别名文件里若出现任何 `if`（除转发参数固定）即为违规 ⇒ 由单测钉住 |
| 7 | 新增路由**必须同时**注册进 `app/main.py` 与 `app/tests/_testapp.py` | 两套 wiring 手工平行维护（`_testapp.py:1-4` 明写）；漏改 `_testapp.py` ⇒ 集成测试 404 |
| 8 | 路由必须注册在 `/api/v1/health` 与 MCP `Mount("/mcp")` **之前** | Starlette 按注册顺序匹配，`Mount("")` 之后注册的路由永久 404（`test_app_wiring.py` 有守卫） |
| 9 | **SQL 路不走 `semantic_schema_gate.renderSchemaWithRelease`**，改走 `restoreSnapshotSchema`（P2 复原层）+ `buildSchemaText`（仓内唯一渲染器） | 那个闸门的开关 `ENABLE_SEMANTIC_REGISTRY_NL2SQL` 是 **NL2SQL 特性开关**（默认关），其降级语义是「直读治理表」⇒ 知识服务一旦直读治理表，**引用就没有版本可引**（与 G5「引用可回溯」直接冲突）。而 manifest **带物理层**（`sourceTable` / `sourceColumn` / `dataType`）⇒ 按发布渲染**不需要开关**，是构造出来的。复用复原层 + 渲染器 ⇒ 快照解析与渲染口径都**只有一份**（不会与 NL2SQL prompt 漂移） |
| 10 | **四路统一输入 `RouteInput`**（`scope` 必填、无默认值） | RAG 路必须拿权限口径（否则过滤无从谈起）；四路签名分叉成四种形状 ⇒ 「同一口径」变成靠约定。改成同形协议后，「四路拿到的是同一个对象」由**构造**保证；编排层只剩一个调用点 |

## 4. 统一权限模型（`KnowledgeScope`）

```python
@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """一次查询的权威权限口径（四路共用，只推导一次）。

    ⚠️ 内部 dataclass 用 snake_case；**只有跨进程 DTO** 才用 camelCase
    （`CamelModel` 约定，见 `daily/D33.md` §七）。
    """

    domain_codes: tuple[str, ...]    # 服务端推导的可见域（权威，已含请求 scope 的收窄）
    org_codes: tuple[str, ...]       # 允许的组织 code（admin 未收窄时为空）
    entity_types: tuple[str, ...]    # 可见业务对象码（由可见域派生）
    max_security_level: str          # L1 / L2 / L3
    is_admin: bool
```

> **发布面不在 scope 里**：发布面由编排层的 `release_context.resolveKnowledgeRelease`
> 单独解一次（钉住 / ACTIVE / 降级三态），**不放进权限口径** ⇒ 权限与版本是两条正交的轴。

**推导口径（D33 已落地）**

1. `is_admin` = `'admin' in user.roles` ⇒ 可见全部 `enabled` 的域（与仓内 `getAdminOnlyActor` / `requireAdmin` 同口径）。
2. 否则可见域 = `data_domain.enabled AND data_domain.primary_org_id → organizations.code ∈ user.departments`。
3. `entity_types` = `business_object.primary_domain_id ∈ 可见域` 的 `business_object.code`（**活表** ⇒ 授权按当前态）。
4. `max_security_level`：非 admin ⇒ **L1**；admin ⇒ L3。（`document_catalog.security_level` 的既有 L1/L2/L3 语义；用户级安全等级无权威来源 ⇒ L-P4-8）
5. **请求里的 scope 只能收窄**：有效域 = 权威域 ∩ 请求域；请求域 ⊄ 权威域 ⇒ 该域**查不到**（空结果 + 警告，每路状态 `DENIED`）。
   - `entityTypes`：**请求侧收窄**（请求集 ∩ 可见集 ⇒ 交集为空则**空结果 + 警告**，**不回落到「全给」**）；`maxSecurityLevel`：**本批不消费**（见 L-P4-8，显式告警）。

> ⚠️ **fail-closed 边界**：`data_domain.primary_org_id IS NULL` 的域（种子里的 `LOGISTICS`）对**非 admin 一律不可见**。
> 这是刻意的 fail-closed（宁可不给，也不给宽的），已登记为遗留 L-P4-1。

**验收矩阵（D35 落地）**：三轴 = **身份**（域可见 / 不可见 / 无主组织 / 不可见时一路都不跑且不解发布面）·
**请求侧收窄**（收窄只发生在权限源一处 / 无权组织致域不可见 / 实体提示不放宽）·
**引用可回溯**（逐条证据带 `releaseId` + `releaseVersion` + `source=RELEASE`；降级 ⇒ **零证据** + 显式降级标记 ⇒ 不存在「无版本的证据」）。
逐格证据见 [`daily/D35.md`](daily/D35.md) §三。

## 5. 日次任务卡

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| **D32** | 契约设计：`/api/v1/semantic/knowledge/query` 请求/响应/错误码/权限上下文；骨架 + 单测 | 契约文档 + DTO + 路由骨架 + 单测 | ✅ **完成**（证据见 [`daily/D32.md`](daily/D32.md)）：契约落文档；**无业务逻辑落地**（骨架只回空编排，路由已注册、鉴权已挂）；20 unit + 13 integration 全绿 |
| **D33** | 图谱路 + 指标路组合 | 组合服务 + 测试 | ✅ **完成**（证据见 [`daily/D33.md`](daily/D33.md)）：图谱路（类/属性/JOIN）+ 指标路（指标/KPI）按**发布快照**召回；域切片绕道业务对象（manifest 无「类↔域」字段）；**统一权限源提前落地**（域可见性是取数闸门）；新增路由状态 `DENIED` |
| **D34** | RAG 路 + SQL 路组合 | 组合服务 + 测试 | ✅ **完成**（证据见 [`daily/D34.md`](daily/D34.md)）：RAG 路（按发布 + 按权限**下推**过滤，开关关 ⇒ 零外部调用的 fail-closed 降级）+ SQL 路（按发布复原渲染物理 schema）；四路统一输入协议 `RouteInput`（`scope` 必填 ⇒ 同源由构造保证）；编排层单路异常兜底降级 |
| **D35** | 统一权限判定（四路同一口径）+ 引用回溯到发布版本 + 错误处理 | 权限层 + 测试 | ✅ **完成**（证据见 [`daily/D35.md`](daily/D35.md)）：三轴验收矩阵（身份 / 请求侧收窄 / 引用可回溯）——四路拿到**同一个** scope 对象（身份断言）；域不可见 ⇒ 四路 `DENIED` + 空 + 警告 + **一路都不跑**且**连发布面都不解**；请求侧收窄**只在权限源一处**；逐条证据带 `releaseId` / `releaseVersion` / `source=RELEASE`，降级则**剥版本号**；新增 unit 6 例（知识相关 unit 131 例 / 集成文件 54 例全绿） |
| **D36** | 采购薄别名 + 试点问题集端到端 | 别名路由 + 试点报告 | ✅ **完成**（证据见 [`daily/D36.md`](daily/D36.md)）：试点问题集 **14 条 / 通过率 100%**（判据 = 权限结果 + 证据命中 + 引用可回溯，三项同时成立）；采购薄别名除 AST 守卫（无分支）外补**运行时等价**判据（两边响应逐路状态 + 证据集合一致）；真实发布写路径播种（两域各一版 ACTIVE）；知识相关 unit 147 例全绿 |
| **D37** | 回归 + 文档 + PR | 变更记录、PR | ✅ **完成**（证据见 [`daily/D37.md`](daily/D37.md)）：审查修复批 8 项（**钉住的发布补授权** / 收窄要诚实 / 试点判据假绿 / 播种脚本 `--force` 守卫 / 别名守卫去 CWD 依赖 / JOIN 标题用类名 / 报告相对路径 / `_seed` 拆小）+ stub 鉴权启动自检；文档口径统一（降级 = **空 + 标记**，`LIVE` 保留不产出）；知识相关 unit **172 例** / 集成文件 **59 例**全绿；试点重跑 **14/14 = 100%**；净新增失败 **0** ⇒ **G5 通过** |

## 6. 试点问题集口径（D36）

试点集 = 一组固定问题（14 条），每条带**期望证据**（类名 / 属性 / 指标名 / KPI 码 / JOIN 键）与**期望权限结果**。

- 通过率判据 = **100%**：每条问题的**证据命中** + **权限结果** + **引用可回溯**三项**同时**成立才算通过。
  （Facade 是确定性编排 ⇒ 通过率可客观判定，不依赖主观评分；P4 不引入 LLM 就没有「答案好不好」这一维。）
- 试点集与运行器落在 `Harness/changes/feat-knowledge-facade/daily/` + `backend/scripts/`（**真实数据脚本**，可复跑）。
- **判定逻辑单独成纯函数模块** `app/services/knowledge/pilot_eval.py`（可单测）；脚本只做取数 / 打印。
- **数据集要成对**：同一问题在两个域各问一次（P-06 采购域 EMPTY / P-07 质量域 OK）⇒ 「域切片真的切掉了别的域」由**数据**证明，而不是靠论证。
- **别名等价是第四项判据**（仅采购域）：主接口与薄别名的响应必须**逐路状态 + 证据 ref 集合一致** ⇒ AST 守卫只证「没有分支」，这一项才证「同一逻辑」。
- 问题集与跑批结果：[`daily/pilot_questions.json`](daily/pilot_questions.json) / [`daily/pilot_report.md`](daily/pilot_report.md)。

## 7. 契约要点（D32 交付物）

**请求** `POST /api/v1/semantic/knowledge/query`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `domain` | str | ✅ | 数据域 code（跨板块扩展的**唯一**域参数 ⇒ 跨域可用的抓手） |
| `question` | str | ✅ | 1..500 字 |
| `channels` | list[str] | ✖ | 缺省 = 四路全开；取值 `GRAPH/METRIC/RAG/SQL` |
| `entityHints` | list[{type,code}] | ✖ | 实体提示（供应商 / 物料 …） |
| `topK` | int | ✖ | 1..50，缺省 8 |
| `releaseId` | int | ✖ | 钉住某版发布；缺省 = 该域 ACTIVE（无则**降级为空 + 显式标记**）。⚠️ 钉住的发布**不可见**（域不匹配 / 非 admin 看不到该状态）⇒ 同样降级为空 + 标记，且**不泄露「它存在」** |
| `scope` | {orgCodes, entityTypes, maxSecurityLevel} | ✖ | **收窄**请求（不得放宽，见裁决 3）。⚠️ `entityTypes` **真的收窄**（请求侧 ∩ 可见集 ⇒ 空则返回空结果 + 警告）；`maxSecurityLevel` **本批未消费**（用户级安全等级无权威来源 ⇒ 该维度被忽略并**显式告警**） |

**响应**（裸 DTO，**不套 `{success,data}` 信封** —— 仓内成功响应一律裸 DTO）

| 字段 | 说明 |
|---|---|
| `question` / `domain` | 回显 |
| `release` | `{id, version, manifestHash, degraded}`；**降级时 `id=null` 且 `degraded=true`** |
| `answerDraft` | 确定性编排摘要（非 LLM 生成） |
| `entities[]` | 命中的实体（类型 / 编码 / 名称） |
| `evidence[]` | `{channel, kind, ref, title, snippet, releaseId, releaseVersion, source}`；`source` **本批恒为 `RELEASE`**（`LIVE` 是**保留常量**，本批**不产出** ⇒ 降级时是**空证据**，不是「无版本的证据」） |
| `routeResults[]` | `{channel, status, itemCount, degradedReason, warnings[]}`；`status ∈ OK / EMPTY / DEGRADED / PENDING / DENIED`（`DENIED` = 域不可见；`EMPTY` = 查了但没命中 ⇒ 两者**必须分开**） |
| `warnings[]` | 全局警告（域不可见 / 路由降级 / 空集 …） |

**错误码**（复用仓内领域异常，**不新造 HTTP 语义**）

| 情形 | 异常 | HTTP |
|---|---|---|
| 参数非法（域为空 / 通道非法 / 长度越界 / releaseId 非法） | `ValidationError` | 422 |
| 未登录 / token 失效 | `HTTPException(AUTH_*)` | 401 |
| 限流 | `RateLimitExceeded` | 429 |
| **域不可见** | —— | **200 + 空结果 + 警告**（裁决 4） |

## 8. 关键设计风险与遗留

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| L-P4-1 | `primary_org_id IS NULL` 的域对非 admin 不可见（种子里的 `LOGISTICS`） | 刻意的 fail-closed；后续若要放开需另立「域可见性策略」卡 |
| L-P4-2 | 权限源的口径是**域级**（`data_domain.primary_org_id`），**不是**行级 / 列级 | 仓内没有行级 / 列级策略源；本批只到域级，登记为边界 |
| L-P4-3 | RAG 路依赖 `ENABLE_SEMANTIC_REGISTRY_RAG` + Milvus v2 集合；两者任缺 ⇒ **fail-closed 降级**（**不是**空结果） | 与 P3a 口径一致（声明了作用域而收窄能力不可用 ⇒ 拒绝，**不许**回落成不过滤的召回）；降级原因落在 `routeResults[].degradedReason`。D34 实现：**先判开关 ⇒ 零外部调用**（不发起 embedding、不查 Milvus） |
| L-P4-4 | 指标路改走 manifest ⇒ **不再读 `kpi_catalog` 活表**（含 `status` / `owner` 等治理元数据的最新态） | 发布读拿到的就是**发布时刻**的口径（这正是「引用可回溯」的意义）；治理最新态仍由治理界面提供 |
| L-P4-5 | SQL 路只渲染**命中候选类**的 schema 片段，不做全量 schema 文本 | 全量 schema 文本是 NL2SQL prompt 用的，不是知识服务的；LLM 生成 SQL 仍留在 chat / agent 既有通道 |
| L-P4-6 | 业务实体子图（Supplier→Material→PO→GR→IQC）**无发布版本**（只直读 Neo4j 主图） | 本批图谱路**只做语义层**（类/属性/JOIN）；业务实体多跳遍历不进 Facade，另立卡 |
| L-P4-7 | 文档源按 D2 实测为空集（`document_catalog` ≈ 0 行、`document_embeddings` 未创建） | 按「空集正确返回」实现与验收；文档入库工作独立排期 |
| L-P4-8 | **用户级安全等级无权威来源**（`document_catalog.security_level` 是**文档**的属性） | 按「admin ⇒ L3、其余 ⇒ L1」取**权威值**。⚠️ **请求里的 `scope.maxSecurityLevel` 本批不被消费**：用户级等级无权威来源 ⇒ 收窄它没有语义 ⇒ 编排层**显式告警**（`MSG_SCOPE_SECURITY_LEVEL_IGNORED`）而不是默默忽略；若将来引入「按组织的安全等级」，改 `knowledge/permission.py` 一处即可 |
| R-P4-1 | 新增集成测试文件会触发 CI 分片轮转（排序下标 mod 4）⇒ 假「净新增失败」 | **不新增集成测试文件**；集成用例并入既有文件；新增文件只允许在 `unit/` |
| L-P4-9 | **RAG 路的查询向量没有落库计量**（`TokenUsageService.recordUsage` 需要 sessionId / modelConfigId / cost，而 Facade 是无会话的编排入口） | 本批**记 token 用量日志**（`embedWithUsage` 拿得到 token 与模型名），**不写假账**；落库计量留待后续（或改由 chat 通道承担 RAG 调用） |
| L-P4-10 | **RAG 路的 `OK` 态在集成环境跑不出来**（P3a 开关 `ENABLE_SEMANTIC_REGISTRY_RAG` 默认关 + Milvus v2 集合需先回填） | 这是**设计内**的 fail-closed（集成里 RAG 恒为 `DEGRADED` + 原因）；RAG 正向格由**单测**（注入闸门替身）覆盖；待测试环境开关打开 + v2 数据就位后补集成正向用例（D35 登记，见 [`daily/D35.md`](daily/D35.md) §八） |
| L-P4-11 | **试点指标 / KPI 是 `DRAFT`**（治理闸门要数据源 + 对账，开关默认关） | 刻意：直写 `PUBLISHED` 等于伪造没走过的治理状态；知识服务读的是**语义发布快照**，与指标的治理状态正交 ⇒ 「按发布读」照样成立（D36 登记，见 [`daily/D36.md`](daily/D36.md) §九） |
| L-P4-12 | 试点数据集**只落在隔离测试库**（`qa_metadata_test_pilot`），**生产未播种** | 试点是**验收装置**，不是生产数据；上生产需另立「试点数据治理」卡（含指标口径的业务签字）（D36 登记） |
| M-P4-1 | 知识查询**无读审计**（读侧不写审计流水，只有治理写路径有） | 本批只读且**未部署**（开关默认关）⇒ 登记为后续卡；上线前需补「谁查了哪个域 / 哪版发布」的流水，否则事后追责无据 |
| M-P4-2 | 目标库守卫**只看库名**（`^qa_metadata$\|^qa_metadata_test($|_)`），**不校验主机 / 实例** | 另一台机器上的同名库也会被放行 ⇒ 真机执行必须人工确认 DSN 的**主机与端口**（DSN 打码后仍要看 host） |
| M-P4-3 | RAG 过滤的**空维度语义 = 拒一切**（`RegistryFilter` 某维度为空元组 ⇒ 该维度交集为空 ⇒ 必然召回不到） | 与「不声明该维度」严格区分：`None` = 不过滤该维度，空元组 = 声明了一个**空的**允许集。方向是 fail-closed（宁可召回不到）⇒ 登记以免后来者把空集读成「不过滤」 |
| M-P4-4 | **计时侧信道**：域不可见时一路都不跑（连发布面都不解）⇒ 响应时间短于「查了但没命中」 | 只泄露「有无权限」的粗粒度信号；状态码 / 证据 / 警告三者已完全一致（不靠 403 区分）⇒ 本批不处理；若将来要求连耗时都无差，需统一注入固定延迟（代价大、收益小） |

## 9. 交付物清单

| 类型 | 文件 |
|---|---|
| 新增 | `backend/app/domain/knowledge_schemas.py` |
| 新增 | `backend/app/services/knowledge/{__init__,permission,matching,domain_slice,release_context,route_outcome,route_input,graph_route,metric_route,rag_route,sql_route,facade}.py` |
| 新增 | `backend/app/services/knowledge/pilot_eval.py`（**纯函数**判定模块：试点判据可单测） |
| 新增 | `backend/app/api/v1/knowledge_facade.py`、`backend/app/api/v1/procurement_knowledge.py` |
| 修改 | `backend/app/main.py`、`backend/app/tests/_testapp.py`、`backend/app/config.py` |
| 新增（unit，安全） | `backend/app/tests/unit/test_knowledge_facade_contract.py`、`test_knowledge_facade_service.py`、`test_knowledge_procurement_alias.py`、`test_knowledge_permission.py`、`test_knowledge_permission_matrix.py`、`test_knowledge_matching.py`、`test_knowledge_domain_slice.py`、`test_knowledge_graph_route.py`、`test_knowledge_metric_route.py`、`test_knowledge_rag_route.py`、`test_knowledge_sql_route.py`、`test_knowledge_pilot_eval.py` |
| 新增（共享夹具） | `backend/app/tests/_knowledge_manifest.py`（unit 与 integration **共用**一份发布快照样本） |
| 集成（并入既有文件） | `backend/app/tests/integration/test_semantic_release_api.py`（追加用例，不新建文件） |
| 路由装配守卫 | `backend/app/tests/integration/test_app_wiring.py`（**既有**文件：保证没有路由落在 catch-all Mount 之后） |
| 脚本 | `backend/scripts/knowledge_pilot_seed.py`（试点数据集播种 + **真实发布写路径**）、`backend/scripts/knowledge_pilot_runner.py`（真实数据试点跑批，**只读**） |
| 试点资产 | `Harness/changes/feat-knowledge-facade/daily/pilot_questions.json`（14 条）、`daily/pilot_report.json` / `daily/pilot_report.md`（跑批报告） |
| 记录 | 本目录 `plan.md` / `daily/D32..D37.md` / `summary.md` |
