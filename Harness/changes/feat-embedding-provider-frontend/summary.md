# 变更：Embedding 服务管理页（前端）

- **日期**：2026-08-13
- **Phase**：Phase 5 扩展
- **状态**：done

## 1. 需求

后端已完成 `embedding_provider` 注册表 CRUD API（列表/创建/更新/激活/软删除），支持单活互斥切换，
但切换只能通过 `curl` 调 API，系统界面没有入口。用户需求：把「切换 embedding 服务」的能力放进系统，
新增一个管理页，与现有「模型配置」页同级——表格展示各服务与激活状态、一键启用/停用切换、新增/编辑。

验收标准：
- 侧边栏新增「Embedding 服务」菜单，进入后表格列出所有 provider 及当前激活状态。
- 非激活行可「启用」（走 `POST /{id}/activate`），激活行可「停用」（Popconfirm 后走 DELETE）。
- 新增/编辑服务表单（名称/类型/接入地址/模型名/维度/API Key），维度默认 1024。
- 单活互斥由后端保证，前端不重复实现。

## 2. 设计评审

- **零后端改动（关键决策）**：后端 Read DTO 已是 camelCase
  （`id/name/providerType/baseUrl/modelName/dimension/isActive/createdTime/updatedTime`，不含 `apiKey`），
  且 `httpClient` 不做 snake↔camel 转换，前端类型直接 camelCase 对齐即可消费。
- **复刻 ModelConfigPage（关键决策）**：页面复用 antd Table + Modal 表单 + Popconfirm 模式，
  而非自建组件——交互一致、心智负担最小；API client 复用 `httpClient.get/post/put/delete` 封装。
- **单活互斥下沉后端（关键决策）**：「启用」按钮只调 `activateEmbeddingProvider(id)`，顶替逻辑
  （DB 部分唯一索引 + `_clearOtherActives`）全部在后端，前端无互斥状态。
- **apiKey 最小暴露**：`EmbeddingProviderUpdate.apiKey?: string | null`；编辑时表单留空则
  `delete payload.apiKey`（不传=不修改，与 ModelConfigPage 一致），Read DTO 永不回显 key。

## 3. 数据模型变更

无（后端 `embedding_provider` 表已在 `feat-embedding-provider-registry` 建立）。

## 4. 接口契约变更

无后端变更。前端新增 5 个封装函数（`frontend/src/api/embeddingProviders.ts`，`BASE=/embedding-providers`）：

| 函数 | HTTP | 说明 |
|---|---|---|
| `listEmbeddingProviders()` | GET `/` | 列表 |
| `createEmbeddingProvider(payload)` | POST `/` | 创建 |
| `updateEmbeddingProvider(id, payload)` | PUT `/{id}` | 部分更新 |
| `activateEmbeddingProvider(id)` | POST `/{id}/activate` | 设为唯一激活 |
| `deactivateEmbeddingProvider(id)` | DELETE `/{id}` | 软删除（取消激活） |

## 5. 实现要点

新增：
- `frontend/src/types/embeddingProvider.ts`：`EmbeddingProviderType`（ollama/omlx/openai_compatible）、
  `EmbeddingProvider`（Read，camelCase）、`EmbeddingProviderCreate`、`EmbeddingProviderUpdate`
  （`apiKey?: string | null` 支持清除）、`PROVIDER_TYPE_OPTIONS`（label 用技术名词原文，不翻译）。
- `frontend/src/api/embeddingProviders.ts`：上述 5 个函数。
- `frontend/src/pages/EmbeddingProvidersPage.tsx`：Table（ID/名称/类型/接入地址/模型名/维度/状态 Tag/
  操作）+ 顶部刷新+新增 + Modal 表单（name/baseUrl/modelName 必填，providerType Select，
  dimension InputNumber 默认 1024，apiKey Input.Password）；`handleSubmit` 编辑时空 apiKey 不传。
- `frontend/src/tests/EmbeddingProvidersPage.test.tsx`（7 例）与 `src/tests/embeddingProvidersApi.test.ts`（5 例）。

修改：
- `frontend/src/App.tsx`：路由 `embeddings` → `<EmbeddingProvidersPage />`（`models` 之后）。
- `frontend/src/components/common/AppLayout.tsx`：`NAV_KEYS` 新增 `{ key: "/embeddings", labelKey: "appLayout.menu.embeddings" }`。
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`：`appLayout.menu.embeddings`、`pages.embeddings`、
  `forms.embeddingProviders`（columns/addButton/deactivateConfirm/addModalTitle/editModalTitle/
  labels 含 dimensionHint「须与 Milvus 集合维度一致（当前 1024）」/placeholders）。

## 6. 测试

- `embeddingProvidersApi.test.ts`（5 例）：list/create/update/activate/deactivate 的 URL 与 payload 断言。
- `EmbeddingProvidersPage.test.tsx`（7 例）：渲染标题/表格/加载列表、激活行有停用无启用、非激活行有启用、
  点击启用调 `activateEmbeddingProvider(id)`、停用 Popconfirm 确认后调 `deactivateEmbeddingProvider(id)`、
  新增提交校验 payload（dimension=1024）、编辑提交 `apiKey` 留空为 undefined。
- 全量：`npm test` **21 文件 172 passed**（含新增 12，不破坏现有回归）；`npm run build`
  （`tsc -b && vite build`）通过。

## 7. 安全审查

- `apiKey` 仅经表单 Input.Password 明文提交，后端 Fernet 加密落库；编辑留空表示不修改，不清除已有 key。
- Read DTO 不返回 `apiKey`/`apiKeyEncrypted`，前端类型中 `EmbeddingProvider` 亦无该字段，杜绝回显。
- 本地免鉴权服务（Ollama）apiKey 留空即可；未在源码/文档中落任何真实 key。

## 8. 部署验证

- 后端 `:8000` 状态核对：`GET /api/v1/embedding-providers` → 3 条（oMLX FP16/8bit 非激活、
  Ollama 激活）；`/active` → `{id:3, name:"Ollama bge-m3", ...}`，与页面预期展示一致。
- 开发服务器 `:5173` 运行中（vite HMR 已加载新页面），页面加载/列表渲染正常；
  浏览器交互冒烟（启用切换单活互斥、编辑保存后刷新、与 `/active` 一致性）待用户手动点验。

## 9. 关联

- 任务：#61（types + api client）、#62（EmbeddingProvidersPage）、#63（路由/导航/i18n）、#64（测试+验证）
- 前置：`Harness/changes/feat-embedding-provider-registry/`（后端注册表 + 单活互斥 + Docker host override）
- Wiki：`Harness/wiki/`（NL2SQL 引擎、模型路由）
