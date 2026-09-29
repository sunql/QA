# 安全批次 · 路由鉴权收口（46 条匿名可达路由）

> 建档：2026-09-30
> 触发：MB4 规划的 A1 批次在核实 TD-5 时，实测发现范围远超登记值
> 状态：**执行中**（用户 2026-09-30 拍板：单独安全批次，先做；先安全后 TD-1）
> 关联：`Harness/changes/2026-09-28-arch-upgrade-v31/tech-debt.md` TD-5

---

## 一、结论

生产容器实测 `AUTH_MODE=real`（`docker exec qa-backend printenv AUTH_MODE` → `real`），
但 **46 条非公开路由在无任何鉴权头时仍可达**，其中 **15 条是写/删**。

- 8 个 router 声明为 `APIRouter()` 或 `APIRouter(dependencies=[])`，无 router 级依赖
- 受影响端点上也无逐端点的 `Depends(getCurrentUser)`
- **没有全局鉴权中间件**（`main.py` 只挂了 `CORSMiddleware` + `SlowAPIMiddleware`）
- **没有第二层兜底**：受影响服务（`TermDictionaryService.deleteTerm`、
  `DataQualityRuleParamsService.create/update/delete`、`DataLineageService.*`、
  `OntologyService.sync*`、`WikiCompileService.runTask`）均无 actor 参数、无 ACL

⇒ 匿名调用者**直接抵达服务层**。

---

## 二、证据（为什么这些数字可信）

### 2.1 静态分析在本仓不可信，实测才可信

`app.routes` 含 49 个 `fastapi.routing._IncludedRouter` 包装对象，`path=None`，
且 `routes` / `app` / `router` 属性一律 `hasattr=False` ⇒ 朴素的依赖链扫描**会整片漏掉**。
本批次期间两轮静态分析都得出过错误结论（一次误报 67 条，一次只报出 1 条）。

### 2.2 权威方法：展开 `_IncludedRouter` 后走依赖图

`_IncludedRouter` 的 `__dict__` 提供 `original_router`（真正的 `APIRouter`）与
`include_context`（含 `prefix` 与 `dependencies`）。展开后：

- 枚举到 **389 条 APIRoute**
- 无 `getCurrentUser` 的 **52 条条目**（去重后 50 条）
- 扣除 4 条应公开 ⇒ **46 条 findings**

此结果与独立的实测探测（`app.openapi()` 路径表 + real 模式 + 无头请求）**逐条吻合**。

### 2.3 应公开的 4 条（whitelist）

| 方法 | 路径 | 理由 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/auth/login` | 登录 |
| GET | `/api/v1/auth/password-policy` | 登录页需在未登录时读取密码策略 |
| GET | `/api/v1/data-quality/reports/share/{token}` | 按 token 公开分享报告（token 门控），**加鉴权会打断对外分享** |

---

## 三、发现清单（46 条）

### CRITICAL — 匿名写/删（15 条）

| 方法 | 路径 | 文件:行 | 危害 |
|---|---|---|---|
| DELETE | `/api/v1/term-dictionary/{id}` | `term_dictionary.py:41` | 匿名删除 NL2SQL 术语，业务术语（「实际到货」「占比」等公式）静默消失 |
| POST | `/api/v1/dq-rule-params/rules` | `data_quality_rule_params.py:51` | 匿名创建 DQ 规则 |
| PUT | `/api/v1/dq-rule-params/rules/{rule_id}` | `:59` | 匿名改写 `rule_expression`/`threshold`/`severity` ⇒ 可把阈值改成恒 PASS |
| DELETE | `/api/v1/dq-rule-params/rules/{rule_id}` | `:68` | **硬删**（区别于主 DQ 路由的软删）⇒ 规则永久消失 |
| POST | `/api/v1/lineage/edges` | `data_lineage.py:64` | 匿名注入伪造血缘边，误导影响面分析 |
| PUT | `/api/v1/lineage/edges/{edgeId}` | `:89` | 匿名改写任意血缘边字段（含 `owner`） |
| DELETE | `/api/v1/lineage/edges/{edgeId}` | `:100` | 匿名软删血缘边 |
| POST | `/api/v1/lineage/edges/extract` | `:74` | 匿名触发血缘抽取（CPU/DB 滥用面） |
| POST | `/api/v1/ontology/embeddings/sync` | `ontology.py:769` | **RAG 向量投毒**：匿名覆写任意本体类/属性/指标的 embedding，污染语义检索与 NL2SQL 本体召回 |
| POST | `/api/v1/wiki/compile/tasks/{taskId}/run` | `wiki_compile.py:50` | 匿名触发 LLM 编译 ⇒ **成本燃烧 / DoS** |
| POST | `/api/v1/data-quality/rules/{ruleId}/evaluate` | `data_quality.py:172` | 匿名触发评估（SQL 走只读适配器，危害限于资源滥用 + 无速率限制） |
| POST | `/api/v1/data-quality/rules/evaluate-batch` | `:183` | 同上 |
| POST | `/api/v1/ontology/embeddings/sync-missing` | `ontology.py:855` | 匿名批量 reconcile 写 Milvus（昂贵、已知会引发向量漂移/flush 停顿） |
| POST | `/api/v1/ontology/graph/sync-missing` | `:866` | 匿名批量 reconcile 写 Neo4j |
| POST | `/api/v1/ontology/classes/{id}/embedding` | `:873` | 匿名重建/覆写单个类 embedding |

### HIGH — 匿名读内部数据模型 / 治理配置（21 条）

- **本体 schema 全量读**（`ontology.py:94/133/143/196/210/242/291/315/360/421`）：
  物理表名、列名、别名、join 条件、KPI 公式 ⇒ 注入/提权的高价值侦察面
- **DQ 规则读**（`data_quality.py:65/97/126`、`data_quality_rule_params.py:24/43`）：
  目标表/列 + 规则表达式
- **血缘读**（`data_lineage.py:37/54`）：完整数据流图（系统/表/列/转换/owner）
- **Neo4j 图读**（`graph.py:31/50`）：节点转储 + 关系

### MEDIUM / LOW（10 条）

`GET /data-quality/scores`（`data_quality.py:219`）、两条 `next-code`
（`data_quality.py:107`、`data_quality_rule_params.py:33`）、
`GET /system/vectors/embeddings|stats`（`vectors.py:24/61`）、
`GET /wiki/compile/tasks|{taskId}|{taskId}/items`（`wiki_compile.py:65/71/79`，含
`total_cost_usd` / `selected_model_id` / 错误文本）、
`GET /term-dictionary`（`term_dictionary.py:23`）、
`GET /ontology/batch/template`（`ontology.py:667`）、
`GET /ontology/health/joins`（`ontology.py:730`）。

---

## 四、生产部署漂移（同一批次必须闭合）

运行中的容器与工作树的 `app/api/v1/*.py` 有 **5 个文件不一致**
（`chat.py` / `evidences.py` / `reports.py` / `wiki.py` / `wiki_compile.py`）。与鉴权相关的后果：

1. 容器版 `wiki_compile.py` 只在 `create_task` 上挂了 `getCurrentUser`，
   **`PATCH /api/v1/wiki/compile/claims/{claimId}` 在生产是匿名的**
   （工作树已修，`wiki_compile.py:94-100`）⇒ 匿名改写知识库 `claim_text`
2. 容器版 `evidences.py` **缺少 R2 的按会话归属守卫** ⇒ 跨用户 evidence 枚举
   （工作树 `evidences.py:45-58` 已有 `_assertSessionOwnership`，99/126 行调用）

⇒ **改源码是必要条件，不是充分条件**。本批次必须含部署步骤。

---

## 五、修复设计

### 5.1 主修复：router 级依赖（一行/路由，不会漏掉未来新增端点）

在下列 `APIRouter(...)` 上加 `dependencies=[Depends(getCurrentUser)]` 并补齐 import：

| 文件 | 行 | 现状 |
|---|---|---|
| `app/api/v1/data_lineage.py` | 28 | `APIRouter(dependencies=[])` |
| `app/api/v1/data_quality.py` | 45 | `APIRouter(dependencies=[])` |
| `app/api/v1/data_quality.py` | 46 | `APIRouter(dependencies=[])`（`scores_router`） |
| `app/api/v1/data_quality_rule_params.py` | 16 | `APIRouter(prefix=..., tags=...)` |
| `app/api/v1/term_dictionary.py` | 18 | 同上 |
| `app/api/v1/ontology.py` | 65 | 同上 |
| `app/api/v1/graph.py` | 15 | `APIRouter(tags=["system"])` |
| `app/api/v1/vectors.py` | 14 | `APIRouter(tags=["system"])` |
| `app/api/v1/wiki_compile.py` | 23 | 同上 |

**不要动**：`auth.py`（登录/密码策略）、`evaluation_report.py`（`/share/{token}` 必须保持公开）。

已带逐端点 `Depends(getCurrentUser)` 的端点不受影响（FastAPI 按请求缓存依赖）。

### 5.2 结构性守卫：防复发

本次事故的根因是「规则存在但无强制」。守卫测试须**枚举全应用路由 + 走依赖图**，
断言「未鉴权路由集合 ⊆ 白名单」。

**关键设计约束（否则守卫是假守卫）**：

1. **必须展开 `_IncludedRouter`**——`original_router` + `include_context.prefix` +
   `include_context.dependencies`。朴素的 `isinstance(r, APIRoute)` 会只枚举到 1 条。
2. **必须断言枚举下限**（如 `len(routes) >= 300`）——否则 FastAPI 升级导致展开失效时，
   守卫会**空集通过**（vacuous pass），静默失效。
3. **必须双向测**：坏输入（无鉴权路由）被拦 **且** 正确输入（有鉴权路由）不被拦。

### 5.3 前端风险：无

`frontend/src/api/*.ts` 全部走共享 `httpClient`（`client.ts` 请求拦截器注入
`Authorization: Bearer`）；非 `httpClient` 路径（`postForm` 上传、blob 下载）走
`authHeaders()`，同样注入 Bearer。受影响的页面**全部在登录后**。

### 5.4 测试风险：低

测试跑 `AUTH_MODE=stub`（config 默认）+ `AUTH_STUB_ENABLED=true`；
无头请求解析为 `anonymous`（roles `("user","admin")`），不抛异常
⇒ 无头 fixture（`conftest.py:103`、`_pg_support.py:118`）继续通过。
无服务按「有无 actor」分支 ⇒ 无语义变化。

---

## 六、明确不做

- **不改 `DataLineage.owner` 的取值来源**。审计将其定性为 mass-assignment，
  但经核实 `DataLineage.owner` **不参与任何鉴权/ACL**（全局 grep 无命中），
  只是 ≤100 字符的描述性元数据；加了鉴权后，登录用户设置该字段正是它的设计用途。
  **不据此造任务。**
- 不引入全局鉴权中间件（router 级依赖已足够，且中间件会波及 `/health` 等公开端点）
- 不为这 46 条路由补速率限制（独立议题，避免范围蔓延）
- 不做 `/share/{token}` 的 token 熵/有效期审查（独立议题）
- 不修 `chat.py` / `wiki.py` / `reports.py` 的非鉴权漂移（功能性，非安全）

---

## 七、验收

1. 守卫测试在修复前**红**（列出 46 条），修复后**绿**
2. 守卫在「展开失效」时**必须红**（下限断言生效）——须有测试证明
3. 双向断言生效（无鉴权被拦 + 有鉴权不被拦）
4. 全量 integration 基线前后对账，报出数字
5. 部署后实测：46 条路由匿名请求返回 401/403；4 条公开路由仍 200
