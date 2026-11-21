# 变更：采购平台 V0.3 · M0.2 开关落地（A2）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W1 轨 A（A2：功能开关两态）
- **状态**：done（**已合入 main**，分批合入 1/4；**生产未部署**）
- **关联变更**：[2026-11-18-procurement-m0-foundation](../2026-11-18-procurement-m0-foundation/summary.md)（A1，本批的前置）
- **迁移版本**：无（本批无 DDL / 无数据迁移）
- **验收包**：[docs/update/1118/M0/evidence/switch-matrix.md](../../../docs/update/1118/M0/evidence/switch-matrix.md)
- **计划**：[并行轨与汇合点 §3 轨 A · A2](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板的三条前提（2026-11-18）：**生产暂不部署**；**功能需要全部完成**；
> **本地全绿后统一部署**。本批只动本地栈与测试，不碰生产配置。

---

## 1. 需求

**用户视角**：采购平台 V0.3 的 M0 判据 3 要求「**6** 个灰开关全部被打开并跑通，且每个都有开/关两态行为用例」。
2026-11-18 实测发现两侧不对称：`qa-backend` **一个 `ENABLE_*` env 都没有**（6 个全默认关），
而 `qa-workflow-worker` 已有 `ENABLE_WORKFLOW=true` ⇒「worker 开」不等于「backend 开」。

**验收标准（可测量）**：

1. **6 个开关都有开态用例**（每个 ≥1 条）；
2. **6 个开关都有关态用例**，且关态断言**行为回退到预期形态**（不是 500、不是「不报错」）；
3. **至少一个能力在开态下被真实业务问题跑通**（端到端）；
4. **backend 与 workflow worker 两侧的开关态一致**；
5. 证据落 `docs/update/1118/M0/evidence/switch-matrix.md`（开关 / backend 值 / worker 值 / 开态断言 / 关态断言 / 用例 ID）。

---

## 2. 设计评审

### 2.1 两态证据放在哪

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| **A. 沿用既有散落用例** | 在 `test_workflow_list_api` / `test_semantic_release_config` 等文件里各补一点 | ❌ 两态证据**不齐**：`ENABLE_SEMANTIC_RELEASE` / `ENABLE_KNOWLEDGE_FACADE` 只有「Settings 解析」两态，**没有**「路由真的注册/不注册」；新增开关时最容易漏证据 | 否决 |
| **B. 建一张可执行的 6×2 统一表（本批采用）** | 新增 `test_feature_flags_two_state.py`：4 个路由闸门断言「整组路由注册/不注册」，2 个服务闸门断言「闸门读到的值」，另加 1 条**清单守卫**（`config.py` 每个 `ENABLE_*` 必须登记） | 需要对 FastAPI 0.141 的路由包装做展平；服务闸门的行为回退仍引用既有行为用例 | **采用** |

### 2.2 本地开关怎么开

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| **A. 依赖 config 默认值** | 不改 env，靠代码默认 | ❌ 默认全关（这正是事故点）；「显式开」才是判据要的证据 | 否决 |
| **B. 写进 `docker/docker-compose.yml` 的 backend `environment:`** | 每个服务硬编码 | ⚠️ 生产 overlay 会继承 ⇒ 等于生产也全开，违背「生产保持默认关」 | 否决 |
| **C. 写进 `docker/.env` + `.env.example`（本批采用）** | compose 的 `env_file: .env` 读取；生产用自己的 `.env`，模板只作注释与占位 | 本地 `.env` 被 gitignore，证据靠 `.env.example` + 容器实测 env | **采用** |

**C 的安全细则（安全审查后补）**：`.env.example` 里 6 个开关保持**注释态**（模板默认关）。
模板自带 `AUTH_MODE=stub` 且 backend 发布在 `0.0.0.0:8000`；照抄模板即全开会让任意客户端用
`X-User-*` 伪造 admin 推进**审批（写入口）**。判据栈用的是本地未入库 `.env`：全开 + `AUTH_MODE=real`。

### 2.3 最终决定

- **两态证据**：方案 B。路由闸门用「最终路由集合」断言（闸门作用点就是 `include_router`），
  HTTP 级 404 由既有用例覆盖（`test_workflow_list_api`、`test_agent_orchestration_config_api`）。
- **本地开关**：方案 C，6 个全部显式 `true`（本地 `.env`），并在 `.env.example` 写清 worker 侧差异与
  「为什么模板默认关」。
- **顺带修正假告警**：开关全开后启动日志刷 5 条「可伪造身份」ERROR，但 `AUTH_MODE=real` 且
  `ALLOW_STUB_WHEN_REAL=false` 时 stub 头在解析前就被拒 ⇒ 告警判据错位。把判据收敛到纯函数
  `_stubHeadersUnreachable()`，其判定与应用侧实际分支**逐字一致**：

  ```
  stub 分支可达 ⇔ (AUTH_MODE != "real" 或 ALLOW_STUB_WHEN_REAL) 且 AUTH_STUB_ENABLED
  ```

  首轮实现写成 `authMode == "real" and not allowStubWhenReal` 漏了「`real` + `allowStub=true` +
  `stubEnabled=false`」这一态（此时 stub 分支同样不可达）；已按上述公式修正并补用例。

---

## 3. 数据模型变更

**无**。本批不新增/修改表、列、索引、CHECK 约束，也没有迁移文件。

---

## 4. 接口契约变更

**无新增/修改/删除任何 API、DTO 字段或状态机。**

- 唯一对外可见的变化是**开关状态**：本地栈 6 个 `ENABLE_*` 从「未设置（默认关）」变为显式 `true` ⇒
  对应路由组从「不注册（404）」变为「注册（过鉴权后才轮到业务逻辑）」。
- `scripts/acceptance/p5_smoke.sh` 的预检**重写**：原文用「未鉴权 `POST /workflow/instances` 必须 405」
  判定开关已开。该断言前提是错的——`/instances` 同时注册了 `GET`（列表）与 `POST`，
  未鉴权 `GET` 在 `AUTH_MODE=stub` 下返回 200、`real` 下返回 403，**永远不会是 405**；
  且鉴权依赖先于方法匹配执行，状态码判据随鉴权模式漂移。
  改为读 **`/openapi.json` 的路径成员**判定「路由是否注册」（与鉴权模式无关），
  并加一条**正向对照**：未知路径 `/api/v1/definitely-not-a-route` 必须 404，
  否则「路径缺失 ⇒ 已关闭」失去区分力。关态仍由单元层的路由集合断言覆盖。

---

## 5. 实现要点

| 文件 | 作用 | 关键点 |
|---|---|---|
| `backend/app/tests/unit/test_feature_flags_two_state.py`（新增） | 6 个开关的**同一张两态表** | 4 个路由闸门（`SEMANTIC_RELEASE` / `KNOWLEDGE_FACADE` / `WORKFLOW` / `AGENT_ORCHESTRATION`）+ 2 个服务闸门（`SEMANTIC_REGISTRY_NL2SQL` / `SEMANTIC_REGISTRY_RAG`）+ 1 条清单守卫；`_flagEnv()` 进出两侧 `getSettings.cache_clear()`（`@lru_cache` 假红防护） |
| 同上 | FastAPI 0.141 路由展平 | `include_router()` 结果被包成 `_IncludedRouter`，`app.routes` 不再 flat ⇒ 用 `fastapi.routing.iter_route_contexts` 展平；`_MIN_ENUMERATED_PATHS = 200` 空集下限守卫（展平失效时红，而不是空集恒真） |
| `docker/.env.example` | 本地开关模板 | 6 个 `ENABLE_*` **保持注释态**（安全默认，理由见 §2.2 C）+ `ALLOW_STUB_WHEN_REAL=false` 及其说明；注明 **5 个**开关有启动告警、`ENABLE_AGENT_ORCHESTRATION` 暂无（M3 前评估）；注明该开关只开 backend 路由面，agent worker 仍须 `--profile agent-orchestration` |
| `docker/.env`（本地，gitignored） | 本地栈实际值 | 6 个 `true` + `AUTH_MODE=real` + `ALLOW_STUB_WHEN_REAL=false`；重建 `qa-backend` 后实测 6 个 env 均存在 |
| `backend/app/config.py` | 5 个 `*AuthMisconfiguration` 判据收敛 | 新增 `_stubHeadersUnreachable(settings)`，判定与应用侧分支逐字一致（见 §2.3 公式）；告警文本同时打印 `ALLOW_STUB_WHEN_REAL={值}`，并说明「`AUTH_STUB_ENABLED=true` 单独不会放行 stub 头」。`productionAuthMisconfiguration` 的 docstring 改述为**策略闸**（比暴露面更严，刻意不同口径） |
| `backend/app/tests/unit/test_semantic_release_config.py`、`test_knowledge_facade_auth_config.py`、`test_semantic_registry_rag_config.py` | 假告警修正的两态单测 | 三个文件各拆成「real + stubEnabled=true + allowStub=false ⇒ 不告警」与「real + allowStub=true ⇒ 必须告警」两态（rag_config 首轮漏改，代码审查 HIGH 后补齐） |
| `backend/app/tests/unit/test_production_auth_guard.py` | 生产策略闸口径说明 | `test_production_real_mode_with_stub_headers_is_flagged` 的 docstring 明确这是策略闸，与 `_stubHeadersUnreachable` 的暴露面口径不同 |
| `scripts/acceptance/p5_smoke.sh` | 端到端预检 | 改判据为 `/openapi.json` 路径成员 + 未知路径 404 正向对照（理由见 §4）；另修正 `$CODE）` 全角括号吞变量（改 `${CODE}）`） |

**依赖注入点**：无 FastAPI `Depends` 变更；测试直接构造 `buildTestApp`，不连库、不发 HTTP。

---

## 6. 测试

### 6.1 新增 / 修改用例

| 文件 | 用例 | 断言要点 |
|---|---|---|
| `app/tests/unit/test_feature_flags_two_state.py` | `test_开关关闭时整组路由不注册[4 个开关]` | 关 ⇒ 前缀下**一条路由都没有**（对外 404） |
| 同上 | `test_开关打开时整组路由注册[4 个开关]` | 开 ⇒ 前缀下**至少一条**路由注册 |
| 同上 | `test_服务闸门随开关两态切换[2 个开关]` | 闸门**每次**读当前设置（不能 import 期固化） |
| 同上 | `test_开关清单与config一致` | `config.py` 每个 `ENABLE_*` 别名必须在两态表里，漏登记直接红 |
| `app/tests/unit/test_semantic_release_config.py` | `test_real_auth_with_stub_enabled_but_headers_denied_is_clean`、`test_real_auth_allowing_stub_headers_still_warns` | 告警判据与实际鉴权分支一致 |
| `app/tests/unit/test_knowledge_facade_auth_config.py` | `test_开关开且鉴权为real但不放行stub头时无告警`、`test_开关开且鉴权为real但放行stub头时仍告警` | 同上 |
| `app/tests/unit/test_semantic_registry_rag_config.py` | `test_开关开且鉴权为real但不放行stub头时无告警`、既有告警用例补 `ALLOW_STUB_WHEN_REAL=true` | 第三个开关的同一两态（首轮遗漏，审查 HIGH） |
| `app/tests/unit/test_production_auth_guard.py` | `test_production_real_mode_with_stub_headers_is_flagged` | 策略闸：real + `AUTH_STUB_ENABLED=true` 仍 flagged（与暴露面口径不同，刻意更严） |

**行为回退证据（关态不是「不报错」）**：

- `test_semantic_schema_gate.py::Test开关::test_开关关闭时输出与直读渲染逐字节一致`、`test_开关关闭时连库都不碰`；
- `test_semantic_registry_rag_gate.py::Test开关::test_开关关闭时读v1且不带过滤`（`calls == ["v1:None"]`）、
  `test_开关关闭且调用方声明了作用域时fail_closed`（`degradedReason == "switch_off_denied"`，**不回落 v1**）；
- HTTP 级 404：`test_workflow_list_api.py::test_开关关闭时三个列表接口均为404`、
  `test_workflow_definition_admin_api.py::test_开关关闭时管理面404`、
  `test_agent_orchestration_config_api.py::test_disabled_feature_returns_404`。

### 6.2 实测结果（2026-11-18）

| 批次 | 结果 |
|---|---|
| A2 全量批（**10** 个文件：两态 + 两个服务闸门行为 + 两个 workflow 路由 + agent 编排 404 + 三个 auth-config + 生产策略闸） | **122 passed / 0 failed**（80.24s） |
| 单跑两态表 | **11 passed** |
| 端到端 `scripts/acceptance/p5_smoke.sh` | **30 项通过 / 0 失败**（实例 完成=30 取消=32 自审批=33） |
| 容器 env 实测 | `qa-backend` 6 个 `ENABLE_*` 全 `true`；`qa-workflow-worker` `ENABLE_WORKFLOW=true`；`qa-agent-orchestration-worker` `ENABLE_AGENT_ORCHESTRATION=true` |
| 启动日志 | 5 条假 auth ERROR 消失；仅剩 1 条**真实数据告警**（RAG v2 集合为空 ⇒ fail-closed，见验收包 §4） |

复跑命令见验收包 §5。

### 6.3 覆盖率

本批新增的是**测试与配置**，生产代码只改了一个纯函数判据与其调用点；
`_stubHeadersUnreachable` 的三个分支（可达/不可达 × 两条实现路径）被三个 auth-config 文件覆盖
（clean / warn 两态），生产策略闸由 `test_production_auth_guard.py` 覆盖。

---

## 7. 安全审查

两轮审查（`code-reviewer` + `security-reviewer`）**0 CRITICAL**；1 HIGH + 3 MEDIUM 已全部修复并复测，
4 条 LOW 中 2 条已修、2 条记档延后。

### 7.1 已修复

| 级别 | 问题 | 处置 |
|---|---|---|
| **HIGH** | `test_semantic_registry_rag_config.py` 被漏改：其既有用例在 `AUTH_MODE=real + AUTH_STUB_ENABLED=true + ALLOW_STUB_WHEN_REAL=false` 下断言「必须告警」，与新判据冲突 ⇒ 全量批会红 | 补 `ALLOW_STUB_WHEN_REAL="true"` 使其真的走到可达分支，并新增「不放行 ⇒ 不告警」用例（三个开关的覆盖才对齐） |
| **MEDIUM** | `docker/.env.example` 6 个开关全开 + 模板自带 `AUTH_MODE=stub` + backend 绑 `0.0.0.0:8000` ⇒ 照抄模板者把**审批写入口**暴露给任意客户端（`X-User-*` 可伪造 admin） | 6 个开关改注释态（模板默认关），写明判据栈的安全组合（全开 + real）与「为什么不能照抄」 |
| **MEDIUM** | `p5_smoke.sh` 预检的 405 判据前提错误（`/instances` 有 GET，且鉴权先于方法匹配）⇒ 断言永远不可能成立 | 改 `/openapi.json` 路径成员判定 + 未知路径 404 正向对照，见 §4 |
| **MEDIUM** | `productionAuthMisconfiguration`（策略闸）与 `_stubHeadersUnreachable`（暴露面）两套口径并存且无文字说明 ⇒ 后维护者会以为其中一条是 bug | 该函数 docstring 与 `test_production_auth_guard.py` 用例 docstring 都明确「策略闸刻意比暴露面更严」：生产不允许保留 stub 可用的开关状态 |
| **LOW** | `_realAuthEnforced()` 漏了「`real` + `ALLOW_STUB_WHEN_REAL=true` + `AUTH_STUB_ENABLED=false`」分支（此时 stub 分支同样不可达） | 按应用侧原式改写为 `_stubHeadersUnreachable()`（§2.3 公式），并补用例 |
| **LOW** | `.env.example` 注释称 6 个开关都会在 stub 下告警，实际只有 **5** 个（`ENABLE_AGENT_ORCHESTRATION` 无该告警） | 更正为 5，并标注 M3 前评估是否补齐 |

### 7.2 记档延后

| 级别 | 问题 | 去向 |
|---|---|---|
| LOW | `.env.example` 的 `JWT_SECRET=replace_with_jwt_secret_min_32_bytes` 长 36 字节，不在 `validateProduction()` / `jwtSecretInsecurityReason()` 的拒绝名单 ⇒ 照抄模板不会 fail-fast | **M7 配置卫生**（既有问题，非本批引入） |
| LOW | `ENABLE_AGENT_ORCHESTRATION` 无同款鉴权误配告警（其余 5 个有） | **M3** 编排域落地时评估（当前编排端点另有 ACL） |

### 7.3 安全结论

- 本地判据栈 `AUTH_MODE=real` + `ALLOW_STUB_WHEN_REAL=false`：stub 头在解析前被拒，身份不可伪造；
  6 个开关打开后**所有写入口仍走真实 JWT + 角色校验**（p5 冒烟中「非审批人决策 ⇒ 404」「自审批 ⇒ 403」
  两条负向断言同时通过）。
- 本批未向仓库写入任何口令/密钥：`wf_submitter` 的临时口令只在执行本机内存态使用；
  admin 口令对齐 seed 默认值（比对 bcrypt 哈希，未做 HTTP 猜测）。
- `p5_smoke.sh` 保留默认拒绝非本地 `BASE_URL` 的闸门（需显式 `ALLOW_NON_LOCAL=1`）。

---

## 8. 部署验证

**本批不部署**（用户 2026-11-18 决策：生产暂不部署，功能全完成后统一发版）。

### 8.1 本地冒烟

| 检查 | 结果 |
|---|---|
| 6 个开关两态（单元 / 行为 / HTTP 404 / 误配告警） | ✅ 122 passed（80.24s） |
| 开态端到端（P5 工作流） | ✅ 30/30（`p5_smoke.sh`） |
| backend / workflow worker 两侧一致 | ✅ 有 worker 消费方的 2 个开关两侧均 true；其余 4 个无 worker 消费方 |
| 启动日志 | ✅ 假 auth 告警清零；RAG v2 空集合告警保留并记档 |

### 8.2 真实数据验证报告

- **跑的是什么**：真实本地 compose 栈（`qa-backend` 重建后）+ 真实 PG/Neo4j/Milvus；
  `p5_smoke.sh` 走真实 HTTP + 真实登录（JWT）+ 真实落库断言。
- **未触碰**：生产任何配置；Milvus/Neo4j 数据面本批未写入。
- **已知限制**：本地 RAG v2 集合为空 ⇒ 带域过滤的召回 fail-closed；回填
  `scripts/backfill_registry_v2.py` 归 M2/M3 的 RAG 内容准备，不作为 M0 判据。

---

## 9. 关联

- **计划**：[并行轨与汇合点 §3 轨 A · A2](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)、
  [M0–M7 计划 0.2 开关策略](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md)
- **判据**：[本地全绿判据书 §判据 3](../../../docs/update/1118/M0/本地全绿判据书.md)
- **验收包**：[docs/update/1118/M0/evidence/switch-matrix.md](../../../docs/update/1118/M0/evidence/switch-matrix.md)
- **前序变更**：[2026-11-18-procurement-m0-foundation](../2026-11-18-procurement-m0-foundation/summary.md)（A1 本地栈同构）
- **Memory**：`qa-system-procurement-v03-landing-program.md`（主）、`qa-system-a1-local-stack-isomorphism.md`（前置）
- **Successor**：A4（渲染顺序统一，先跑 S2 评估）→ A10（知识四路接线，依赖 A9 + A2）
