# 变更：服务状态监控（Service Status Monitor）

- **日期**：2026-08-14
- **Phase**：Phase 5 扩展（运维可观测性）
- **状态**：done

## 1. 需求

QA System 依赖多项外部基础设施：PostgreSQL 元数据库、Neo4j 图库、Milvus 向量库、
Embedding 服务。既有 `/api/v1/health` 存活探针只返回 `ok`，**不探测任何依赖**，运维
无法快速判断「哪个依赖挂了」。

目标：新增只读的「服务状态」页面 + 后端状态探测端点，逐一做真实连通性探测，展示每项
依赖的 up / down / not_configured 状态、延迟、端点与错误详情。

用户已确认范围：
- **只监控核心 4 项**：PostgreSQL + Neo4j + Milvus + Embedding（不做 Redis/LLM 推理等）。
- **手动刷新按钮**，不做自动轮询。

## 2. 设计评审

- 探测方式按各基础设施 SDK 选最轻量的连通性操作（见 §5），统一 5s 超时。
- 并发：`asyncio.gather` 一次并发探测 4 项；单服务挂（含超时）不拖垮整体——每个探测
  函数自行 catch 异常并返回 `ServiceStatus`（`up/down/not_configured`），gather 永不 raise。
- 状态枚举：`up | down | not_configured`（仅 Embedding 可能出现 not_configured：无激活
  provider 且无环境变量 base_url）。
- 同步 SDK（Neo4j / Milvus）在 async 探测内用 `asyncio.to_thread` 包裹，避免阻塞事件循环。
- 端点脱敏：PG 用 `render_as_string(hide_password=True)`，Neo4j 复用既有 `_sanitizeUri`，
  响应中不出现任何密码/密钥。
- ⚠️ 顶层**不出现** `success` 字段：前端 `client.ts` 拦截器按 `"success" in body` 解包
  信封，`ServiceStatusResponse` 若含 `success` 会被误解析（与 `api/datasource.ts` 既有规避
  注释同一约束）。

## 3. 数据模型变更

无。只读特性，不新增表/列。

## 4. 接口契约变更

新增 `GET /api/v1/system/status`（前缀挂载于 `main.py` / `_testapp.py`，`system` tag，
复用 `health` 的 tag）。认证：依赖 `getCurrentUser`。

响应 `ServiceStatusResponse`（camelCase）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `services` | `list[ServiceStatus]` | 4 项依赖状态 |
| `checkedAt` | datetime | 探测完成时间 |

`ServiceStatus`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | `postgresql \| neo4j \| milvus \| embedding` |
| `status` | str | `up \| down \| not_configured` |
| `latencyMs` | int \| null | 探测往返耗时 |
| `endpoint` | str \| null | 已脱敏端点 |
| `detail` | str \| null | down 时的错误信息（超时固定为「探测超时」） |

## 5. 实现要点

- `app/services/service_status_service.py`（新）：`SERVICE_CHECK_TIMEOUT_SECONDS = 5`；
  4 个模块级探测函数 `_checkPostgres/_checkNeo4j/_checkMilvus/_checkEmbedding`（各自 catch
  异常返回 `ServiceStatus`，`TimeoutError` → detail「探测超时」，其余 `Exception` →
  `_checkFailedDetail`：完整异常写服务端日志，响应返回通用脱敏提示）；
  `ServiceStatusService.checkAll()` 用 `asyncio.gather` 并发组装响应。
- 展示脱敏：`_sanitizeEndpoint` 对 Milvus / Embedding 端点剥除 userinfo 与查询参数；
  PG `render_as_string(hide_password=True)`、Neo4j `_sanitizeUri`（经 `_neo4jEndpoint`
  安全解析，畸形 URI 返回 None）。
- 各依赖探测：
  - PostgreSQL：`async with getEngine().connect() as conn: await conn.execute(text("SELECT 1"))`，
    `asyncio.wait_for` 包超时；endpoint 用 `getEngine().url.render_as_string(hide_password=True)`。
  - Neo4j：`_probeInThread(_pingNeo4j)`（`verify_connectivity`，专用线程池内执行）。
  - Milvus：`_probeInThread(milvus_client.checkHealth)`；`milvus_client.checkHealth()`
    （新公开方法）：`_connect()` + `utility.list_collections`。
  - Embedding：`asyncio.wait_for(embedding_provider_factory.getActiveEmbeddingClient(), timeout)`
    （解析本身也受 5s 约束）；`client.apiBase` 为空 → `not_configured`，否则
    `await client.checkHealth()`（新方法：`GET {base}/models`，httpx 5s 超时，复用
    `buildHttpClient()` 绕过系统代理；`apiBase` 新属性只读暴露 base_url）。
- 阻塞式探测（Neo4j/Milvus）用**专用** `_PROBE_EXECUTOR`（`ThreadPoolExecutor(max_workers=4)`）
  + `_probeInThread`：超时只中断 await、杀不死线程，独立小池避免挂死的 SDK 调用占满应用
  默认线程池导致其他 to_thread 饿死。
- `app/api/v1/system.py`（新）：`GET /status` → `ServiceStatusService().checkAll()`。
- 路由挂载两处（缺一不可）：`main.py` + `tests/_testapp.py`；**不**在 legacy `router.py` 注册
  （避免 FastAPI 0.141 prefix 叠加 bug）。
- 前端：`types/serviceStatus.ts`（camelCase 类型）、`api/serviceStatus.ts`（`GET /system/status`）、
  `pages/ServiceStatusPage.tsx`（antd 卡片网格 + 手动刷新 `Button`）、`App.tsx` 路由
  `path="status"`、`AppLayout.tsx` NAV_KEYS 追加「服务状态」。
- i18n：`zh-CN.ts` 与 `en-US.ts` 同步新增 `appLayout.menu.status` / `pages.status` /
  `forms.serviceStatus.*`（键名一致）。

## 6. 测试

- `app/tests/unit/test_service_status_service.py`（18 测试）：monkeypatch 4 个探测函数与
  `getActiveEmbeddingClient` / `milvus_client.checkHealth`，覆盖 up / down / not_configured /
  超时（含 embedding resolve 超时）四条路径、并发组装逻辑，以及 `_sanitizeEndpoint`
  脱敏（带/无 scheme userinfo、查询参数、凭据不泄漏）与失败 detail 通用化。
- `app/tests/integration/test_service_status_api.py`：真实 PG fixture（`TEST_DATABASE_URL`
  强制，fail-fast 禁 sqlite），完整 HTTP 链路 `GET /api/v1/system/status`；真实 PG 探测返回
  up，Neo4j/Milvus/Embedding monkeypatch 掉；断言 200 + camelCase JSON 形状 + `services`
  长度 4。
- `frontend/src/tests/ServiceStatusPage.test.tsx`（2 测试）：`vi.hoisted` + `vi.mock` 模式，
  断言 4 卡片中文标签 / up×2·down·not_configured 状态 / 延迟 / 端点 / detail / 探测时间，
  及刷新按钮触发重新请求。
- 回归：后端 **817 passed**（覆盖率 92.87%，> 80% 门槛）；前端 **232 tests** 全绿；
  `npm run build`（tsc + vite）通过。

## 7. 安全审查

已触发 `security-reviewer` 与 `code-reviewer`（health 端点涉及外部服务探测）。

### security-reviewer（初始 BLOCK → 本特性范围修复）

根因是 **HIGH-1**：`getCurrentUser` 是应用全局的 no-op 桩（读取 `X-User-Id` 头，不校验
token），**所有**路由共享——这是既有「网关层鉴权」架构设计，非本特性引入。据此推导的
**HIGH-2**（embedding `base_url` 可写导致盲 SSRF）同样依赖 HIGH-1 且 base_url 出站调用在
本特性之前已存在（`embed()` 正常路径）。**两项均超出只读状态看板范围，不改，记录为既有
系统性缺口**，建议后续统一补真实鉴权。

本特性新代码内的问题已修复：

| 级别 | 问题 | 修复 |
|---|---|---|
| MEDIUM-3 | Milvus / Embedding 端点原样返回，未脱敏（PG/Neo4j 已脱敏，不一致） | `_sanitizeEndpoint`：剥除 userinfo（带/无 scheme）与查询/片段参数，4 项统一脱敏 |
| MEDIUM-4 | `detail=str(exc)` 泄漏用户名/内网主机/URL 给调用方 | `_checkFailedDetail`：完整异常只写服务端日志（`logger.error(exc_info=True)`），响应返回通用 `MSG_SERVICE_CHECK_FAILED` |

### code-reviewer（APPROVE：0 CRITICAL / 0 HIGH）

| 级别 | 问题 | 处置 |
|---|---|---|
| MEDIUM | `asyncio.to_thread` 超时无法杀死线程，挂死的 SDK 探测会占满应用默认线程池 | 新增专用 `_PROBE_EXECUTOR`（`ThreadPoolExecutor(max_workers=4)`）+ `_probeInThread`，隔离到状态页内，不饿死应用其他 to_thread |
| MEDIUM | `getActiveEmbeddingClient()` 未套超时，元数据库挂起时整个 gather 可拖到 ~60s | 包 `asyncio.wait_for`，超时映射为「探测超时」 |
| LOW | `_checkNeo4j` 的 endpoint 计算在 try 外，畸形 URI 会 500 | `_neo4jEndpoint()` 安全解析，失败返回 None |
| LOW | 集成测试未断言顶层无 `success` 字段 | 已补 `assert "success" not in body` |
| LOW | 未使用的 logger | 已被 `_checkFailedDetail` 使用，无需处理 |

- 预置防护（未变）：PG `hide_password=True`、Neo4j `_sanitizeUri`；路由依赖
  `getCurrentUser`（详见 HIGH-1 说明）+ SlowAPI 全局限流；每项 5s 超时。
- 已知限制：`_PROBE_EXECUTOR` 内挂死的线程仍会跑完 SDK 自身超时（无法杀死），但受
  「手动刷新 + 限流」约束，可接受。

## 8. 部署验证

- 后端全量测试 + 集成（真实 PG）通过；前端构建通过。
- 端到端冒烟（可选）：`docker compose up`（PG/Neo4j/Milvus）+ 后端 + `npm run dev`，
  访问 `/status` 页确认 4 项状态正确渲染、刷新按钮生效。

## 9. 关联

- 设计：`Harness/changes/feat-服务状态监控/`（本文件）
- Wiki：`Harness/wiki/`（架构、数据模型）
- 规则：`Harness/rules/测试规范.md`（真实 DB 测试约束）、`开发流程规范.md`
