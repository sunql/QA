# 判据 3 · 开关两态矩阵（6 个开关 × 开/关）

> **状态**：已完成（2026-11-18，本地栈） ｜ **归属**：M0.2 / A2 ｜ **判据**：[本地全绿判据书 §判据 3](../本地全绿判据书.md)
>
> 本表是判据 3 的**唯一证据源**。每个开关必须同时有「开态」和「关态」用例；
> 关态必须是**行为回退到预期形态**，不是「关掉不报错」。

---

## 1 · 两侧 env 实际值（2026-11-18 实测）

证据命令：

```bash
docker exec qa-backend env | grep -E '^ENABLE_' | sort
docker exec qa-workflow-worker env | grep -E '^ENABLE_' | sort
docker exec qa-agent-orchestration-worker env | grep -E '^ENABLE_' | sort
```

| 开关 | backend（`qa-backend`） | workflow worker（`qa-workflow-worker`） | agent worker（`qa-agent-orchestration-worker`） | 有 worker 消费方 |
|---|---|---|---|---|
| `ENABLE_SEMANTIC_RELEASE` | `true` | `true`（继承，不读） | `true`（继承，不读） | 否（纯 API 面） |
| `ENABLE_SEMANTIC_REGISTRY_NL2SQL` | `true` | `true`（继承，不读） | `true`（继承，不读） | 否（backend 问答链路） |
| `ENABLE_SEMANTIC_REGISTRY_RAG` | `true` | `true`（继承，不读） | `true`（继承，不读） | 否（backend 召回链路） |
| `ENABLE_KNOWLEDGE_FACADE` | `true` | `true`（继承，不读） | `true`（继承，不读） | 否（纯 API 面） |
| `ENABLE_WORKFLOW` | `true` | **`true`**（显式 `-e` + env-file） | `true`（继承） | **是**（`qa-workflow-worker`） |
| `ENABLE_AGENT_ORCHESTRATION` | `true` | `true`（继承） | **`true`**（compose profile 显式覆盖） | **是**（agent worker） |

**两侧一致性结论**：

- 6 个开关在 **backend 与两个 worker 上全部为 `true`**（worker 都通过 `--env-file docker/.env` 继承）；
- 真正**消费**开关的 worker 是 2 个：`qa-workflow-worker`（只读 `ENABLE_WORKFLOW`）与
  `qa-agent-orchestration-worker`（读 `ENABLE_AGENT_ORCHESTRATION`）；其余 4 个开关在 worker 侧只是继承、不参与判定；
- `qa-workflow-worker` 是**手工 `docker run` 容器**（不是 compose service，见 `docs/update/1117/P5功能验收手册.md`）：
  `--env-file docker/.env -e ENABLE_WORKFLOW=true`；
- `qa-agent-orchestration-worker` 是 compose profile 服务，`environment:` 里显式覆盖
  `ENABLE_AGENT_ORCHESTRATION: "true"`（`docker/docker-compose.yml` 201–226）。

> ⚠️ 2026-11-18 之前 `qa-backend` **一个 `ENABLE_*` 都没有**（6 个全默认关），
> 而 worker 已开 `ENABLE_WORKFLOW`——这就是「worker 开 ≠ backend 开」的两侧不对称。
> A2 已在 `docker/.env`（本地）与 `docker/.env.example`（模板）显式补齐 6 个开关。

---

## 2 · 6 × 2 用例矩阵

**统一用例文件**：`backend/app/tests/unit/test_feature_flags_two_state.py`
（11 条：4 个路由闸门 × 2 态 + 2 个服务闸门 × 2 态 + 1 条开关清单守卫）

| 开关 | 开态断言（用例 ID） | 关态断言（用例 ID） |
|---|---|---|
| `ENABLE_SEMANTIC_RELEASE` | 前缀 `/api/v1/semantic/releases` 下**至少 1 条路由注册**：`test_feature_flags_two_state.py::test_开关打开时整组路由注册[ENABLE_SEMANTIC_RELEASE]` | 同前缀下**一条路由都没有**（对外 404）：`test_开关关闭时整组路由不注册[ENABLE_SEMANTIC_RELEASE]` |
| `ENABLE_KNOWLEDGE_FACADE` | `/api/v1/semantic/knowledge` **和** `/api/v1/procurement/knowledge` 均注册：`test_开关打开时整组路由注册[ENABLE_KNOWLEDGE_FACADE]` | 两个前缀均无路由：`test_开关关闭时整组路由不注册[ENABLE_KNOWLEDGE_FACADE]` |
| `ENABLE_WORKFLOW` | `/api/v1/workflow` 下路由注册：`test_开关打开时整组路由注册[ENABLE_WORKFLOW]`；HTTP 级另有 `test_workflow_definition_admin_api.py::test_开关关闭时管理面404` 的反面 | `test_开关关闭时整组路由不注册[ENABLE_WORKFLOW]`；HTTP 级 **404** 证据：`test_workflow_list_api.py::test_开关关闭时三个列表接口均为404`、`test_workflow_definition_admin_api.py::test_开关关闭时管理面404` |
| `ENABLE_AGENT_ORCHESTRATION` | `/api/v1/agent-orchestrations`、`/api/v1/agent-runs` 均注册：`test_开关打开时整组路由注册[ENABLE_AGENT_ORCHESTRATION]` | 两个前缀均无路由：`test_开关关闭时整组路由不注册[ENABLE_AGENT_ORCHESTRATION]`；HTTP 级 **404** 证据：`test_agent_orchestration_config_api.py::test_disabled_feature_returns_404` |
| `ENABLE_SEMANTIC_REGISTRY_NL2SQL` | 闸门读到 true：`test_服务闸门随开关两态切换[ENABLE_SEMANTIC_REGISTRY_NL2SQL]`；**行为**证据：`test_semantic_schema_gate.py::Test接线点::test_generateSql走闸门_快照新鲜时prompt列序按快照` | 闸门读到 false：同上两态用例；**行为回退**：`test_semantic_schema_gate.py::Test开关::test_开关关闭时输出与直读渲染逐字节一致`、`test_开关关闭时连库都不碰`（`registry.calls == []`） |
| `ENABLE_SEMANTIC_REGISTRY_RAG` | 闸门读到 true：`test_服务闸门随开关两态切换[ENABLE_SEMANTIC_REGISTRY_RAG]`；**行为**证据：`test_semantic_registry_rag_gate.py::Test路由::test_开关开且无过滤器时读v2`、`test_开关开且有过滤器时读v2并带表达式` | 闸门读到 false：同上两态用例；**行为回退**：`test_semantic_registry_rag_gate.py::Test开关::test_开关关闭时读v1且不带过滤`（`calls == ["v1:None"]`）、`test_开关关闭且调用方声明了作用域时fail_closed`（`degradedReason == "switch_off_denied"`，**不回落 v1**） |

**清单完整性守卫**：`test_开关清单与config一致` —— `config.py` 里每个 `ENABLE_*` 别名都必须出现在上表；
新增开关却漏登记 ⇒ 直接红（防止判据静默少一项）。

### 2.1 为什么路由闸门断言「路由集合」而不是发 HTTP

闸门的作用点就是 `create_app` 里的 `include_router` 条件：关 = 整组不注册 ⇒ 对外 404。
直接在最终路由表上断言，等价且不依赖数据库 / 鉴权 / 限流；HTTP 级 404 由上面两个既有用例覆盖。

⚠️ **FastAPI 0.141 陷阱**：`include_router()` 的结果被包成 `_IncludedRouter`，`app.routes` 不再 flat。
直接枚举只能看到直接声明的少数路由 ⇒ 「开关打开也没看到路由」的**空集假绿**（本次实测踩中）。
故用 `fastapi.routing.iter_route_contexts` 展平，并加 `_MIN_ENUMERATED_PATHS = 200` 下限守卫
（展平失效时直接红，而不是断言恒真）。

⚠️ `getSettings()` 带 `@lru_cache`：`_flagEnv()` 在进入和退出两侧都 `cache_clear()`，
否则读到上一个用例的开关值（2026-11-18 实测过同类假红）。

---

## 3 · 端到端：开态下真实业务跑通（≥1 条）

**标准动作**（判据书写的是 `p5_smoke.sh`）：

```bash
# 本地栈：6 个开关已在 docker/.env 显式置 true；admin 用 seed 默认口令，
# 提交人 wf_submitter 由 admin 调 PUT /api/v1/users/773/password 重置临时口令
# （只在本机内存态使用，未写入仓库）。
ADMIN_PASSWORD='<本地 admin 口令>' SUBMITTER_USER=wf_submitter SUBMITTER_PASSWORD='<临时口令>' \
  ./scripts/acceptance/p5_smoke.sh
```

**实测结果（2026-11-18，`qa-backend` 最终重建后）**：

```
▶ 汇总
实例：完成=30 取消=32 自审批=33
通过 30 项，失败 0 项
验收通过
```

覆盖的四条链路（每条同时断言 HTTP 结果**和**落库状态）：

1. 提交 → 第一步执行 → 停在审批门 → 审批通过 → 第二步执行 → `COMPLETED`；
2. 同 `idempotencyKey` 重放 ⇒ 200 + 同一实例 + 库里只有 1 行；
3. 取消 ⇒ `CANCELLED` + 未执行步骤 `SKIPPED` + 待办审批 `CANCELLED`；
4. 四眼：提交人自己批 ⇒ 403。

**A2 对冒烟脚本预检的修正**（原文错在两处）：

- `/api/v1/workflow/instances` 同时注册了 `GET`（列表）与 `POST`，原文「只允许 POST ⇒ 405」是错的；
  未鉴权 GET 在 `AUTH_MODE=stub` 下返回 200、`real` 下返回 403，状态码判据天然不稳。
- 改为读 **`/openapi.json` 的路径成员**判定「路由是否注册」（与鉴权模式无关），
  并加一条**正向对照**：未知路径 `/api/v1/definitely-not-a-route` 必须 404，
  否则「路径缺失 ⇒ 已关闭」失去区分力。关态仍由单元层的路由集合断言覆盖（见 §2）。

---

## 4 · 开态启动日志（安全与数据面）

重建后 `qa-backend` 启动日志只剩 **1 条** ERROR：

```
🚨 语义注册表 RAG 开关已开启，但 v2 集合缺失或为空：
{'class': 0, 'property': 0, 'metric': 0} ⇒ 带过滤的召回会返回空结果（fail-closed）。
请先执行回填 scripts/backfill_registry_v2.py。
```

- 这是**数据面缺回填**，设计上是 fail-closed（带域过滤的召回返回空，而不是放行全量）；
  无过滤的召回仍回退 v1。回填动作归 M2/M3 的 RAG 内容准备，**不作为 M0 判据**。
- 本地栈 `AUTH_MODE=real` 且 `ALLOW_STUB_WHEN_REAL=false` ⇒ stub 头在解析前被拒，身份不可伪造。

**A2 顺带修正的假告警**：此前只要 `AUTH_STUB_ENABLED=true`（默认值）就给 5 个开关各刷一条
「任意客户端可伪造身份」ERROR，而实际鉴权分支在 `AUTH_MODE=real && !ALLOW_STUB_WHEN_REAL`
时根本不会解析 stub 头。已把 5 个 `*AuthMisconfiguration` 的判断收敛到纯函数
`_stubHeadersUnreachable()`（`backend/app/config.py`）——判定与应用实际分支逐字节一致：

```
stub 分支可达 ⇔ (AUTH_MODE != "real" 或 ALLOW_STUB_WHEN_REAL) 且 AUTH_STUB_ENABLED
```

三份 auth-config 单测（semantic release / knowledge facade / registry RAG）都覆盖了
「real + stubEnabled → 不可达 ⇒ 不告警」与「real + ALLOW_STUB_WHEN_REAL → 可达 ⇒ 告警」两态。

**模板安全默认**：`docker/.env.example` 里 6 个开关保持**注释态**（默认关）。
原因：模板的 `AUTH_MODE=stub` 且 backend 发布在 `0.0.0.0:8000`；照抄模板即全开会让
任意客户端用 `X-User-*` 伪造 admin 推进审批（写入口）。本地判据栈的真实 `.env`（未入库）
是全开 + `AUTH_MODE=real` 的组合，容器实测见 §1。

---

## 5 · 复跑命令与结果

```bash
# A2 全量批：两态 + 服务闸门行为 + HTTP 级 404 + 误配告警 + 生产策略闸
./scripts/run_local_tests.sh \
  app/tests/unit/test_feature_flags_two_state.py \
  app/tests/unit/test_semantic_schema_gate.py \
  app/tests/unit/test_semantic_registry_rag_gate.py \
  app/tests/unit/test_workflow_list_api.py \
  app/tests/unit/test_workflow_definition_admin_api.py \
  app/tests/integration/test_agent_orchestration_config_api.py \
  app/tests/unit/test_semantic_release_config.py \
  app/tests/unit/test_knowledge_facade_auth_config.py \
  app/tests/unit/test_semantic_registry_rag_config.py \
  app/tests/unit/test_production_auth_guard.py
# 2026-11-18 实测：122 passed / 0 failed（80.24s）

# 单元级两态 + 开关清单守卫（单跑）
./scripts/run_local_tests.sh app/tests/unit/test_feature_flags_two_state.py
# 11 passed

# 行为级：服务闸门回退
./scripts/run_local_tests.sh app/tests/unit/test_semantic_schema_gate.py \
  app/tests/unit/test_semantic_registry_rag_gate.py

# HTTP 级 404（关态）
./scripts/run_local_tests.sh app/tests/unit/test_workflow_list_api.py \
  app/tests/unit/test_workflow_definition_admin_api.py \
  app/tests/integration/test_agent_orchestration_config_api.py
```

---

## 6 · 遗留与说明

| 项 | 状态 | 说明 |
|---|---|---|
| RAG v2 本地集合为空 | 已知，不阻塞 | fail-closed 设计；回填 `scripts/backfill_registry_v2.py` 归 M2/M3 |
| `qa-workflow-worker` 非 compose 服务 | 已知 | 按 `docs/update/1117/P5功能验收手册.md` 手工 `docker run`；`ENABLE_WORKFLOW=true` 显式传入 |
| `ENABLE_AGENT_ORCHESTRATION` 无 auth 误配告警 | 已知，待评估 | M3 前评估是否需要与其余 5 个开关同款告警（当前编排端点另有 ACL） |
| 本地 `AUTH_STUB_ENABLED` 仍为默认 true | 有意保留 | `ALLOW_STUB_WHEN_REAL=false` ⇒ 不生效；生产由 `validateProduction()` fail-fast |
| `docker/.env.example` 的 6 个开关是注释态 | 有意为之 | 模板 `AUTH_MODE=stub` + backend 绑 `0.0.0.0:8000` ⇒ 照抄全开即写入口可伪造 admin（见 §4）。判据栈用本地未入库 `.env`（全开 + real） |
| `JWT_SECRET` 占位值不在 `validateProduction()` 拒绝名单 | 已知（既有问题） | 照抄模板的 `replace_with_jwt_secret_min_32_bytes` 长 36 字节，不会被长度校验拦下 ⇒ 归 M7 配置卫生批次 |
