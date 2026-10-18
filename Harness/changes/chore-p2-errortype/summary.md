# 变更：chore-p2-errortype

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P2 收尾（errorType 端到端）
- **状态**：in-review
- **关联变更**：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P2-1 分类器）、[chore-p2-residual-cleanup](../chore-p2-residual-cleanup/summary.md)（同批 P2 收尾）
- **迁移版本**：无
- **MEMORY**：[qa-system-errortype-end-to-end.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-errortype-end-to-end.md)

---

## 1. 需求

P2-1 让后端 `error` SSE 事件携带细粒度 `errorType`（`permission` / `provider_transient` / `query_timeout` / ...），目的是让前端「差异化降级」。收尾审计发现这条链在**解析层就断了**：

1. `frontend/src/api/chat.ts` 的 `error` 分支只取 `error` 与 `detail`，`d.errorType` 被直接丢弃；`onError` 签名也没有第三参。
2. `chatStore` 的错误消息没有 `errorType` 字段，UI 无从区分「权限不足」与「查询超时」。
3. doc-QA / Wiki 两个 SSE 客户端虽然类型里声明了 `errorType`，但没有任何界面消费它。

**验收标准**：

- `errorType` 从 SSE 帧一路到消息模型，最终在错误气泡里给出**可操作的下一步提示**。
- 只有白名单内的类型才渲染提示；未知类型不渲染、不透传原始枚举。
- 测试断言真实文案，并反向断言不出现 i18n 裸 key。

## 2. 设计评审

### 候选方案

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 解析 → 消息字段 → 白名单提示文案 | 改动集中在解析层 + `MessageItem`；提示是静态文案，无新交互 | **采用**（最小闭环） |
| B. 按类型渲染不同按钮（重连 / 申请权限） | 需要新的用户动作与路由，属于产品决策；重连对 SSE 轮次也无明确语义 | 否（YAGNI） |
| C. 仅在控制台/埋点记录 errorType | 用户无感知，等于没做端到端 | 否 |

### 白名单而非拼接

`errorType` 是系统边界字符串。`ERROR_TYPE_HINT_KEYS` 只列「用户能采取动作」的 7 类：`permission` / `provider_transient` / `provider_protocol` / `query_timeout` / `db_connectivity` / `sql_syntax` / `schema_reference`。`domain` / `llm` / `internal` 的正文本身已面向用户，不再追加提示；未知类型（后端未来新增）静默不渲染 —— 绝不 `t(\`...${errorType}\`)`（i18next `returnNull: false` 会把缺 key 原样打到界面）。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无后端变更。前端内部契约：

- `sendMessageStream` 的 `onError(message, detail?, errorType?)` 新增第三参；缺省为 `undefined`。
- `ChatMessage` 新增 `errorType?: string | null`（系统边界原始字符串）。
- doc-QA / Wiki 的 SSE 客户端已经是 `{...obj, kind}` 展开，`errorType` 运行时本就在事件对象里；本次只补消费端（聊天）。

## 5. 实现要点

- `frontend/src/api/chat.ts`：`error` 分支透传 `d.errorType`（仅 string）。
- `frontend/src/stores/chatStore.ts`：两处 `onError`（首发 / 续跑）把 `errorType` 写入消息；网络异常兜底路径不伪造类型。
- `frontend/src/types/chat.ts`：`ChatMessage.errorType` + 注释说明渲染前必须过白名单。
- `frontend/src/components/chat/MessageItem.tsx`：`ERROR_TYPE_HINT_KEYS` + `errorTypeHintKey()`；错误 Alert 下方渲染 `Text type="secondary"` 提示。
- `frontend/src/i18n/{zh-CN,en-US}.ts`：`messageItem.errorHints.*` 7 条中英文案。

## 6. 测试

- `frontend/src/tests/chatApi.test.ts`：error 帧带 `errorType` ⇒ 第三参透传；缺失 ⇒ 三参 `(msg, undefined, undefined)`。
- `frontend/src/tests/chatStore.test.ts`：错误消息回填 `errorType=db_connectivity`。
- `frontend/src/tests/MessageItem.test.tsx`：已知 `query_timeout` ⇒ 真实提示文案 + 反向断言无裸 key；未知 `some_future_type` ⇒ 无提示且不泄露枚举。
- 结果：3 个文件 **126 passed**；`npx tsc --noEmit` 通过。

## 7. 安全审查

不触发 security-reviewer：纯展示层白名单；唯一外部输入 `errorType` 只用于查表，不参与 HTML、URL、SQL 或状态变更。白名单同时是信息泄露防线（未知枚举不展示）。

## 8. 部署验证

- 纯前端变更；无迁移、无后端契约变更。
- 前端仍需 `docker compose build`（memory `qa-system-frontend-deploy-build-required`）。
- 冒烟：制造一次错误（如把数据源停掉再提问）→ 错误气泡下方出现对应提示（如「数据源连接失败…」），而不是只有原始错误文本。

## 9. 关联

- Wiki：[Harness/wiki/frontend.md](../../wiki/frontend.md)（errorType 端到端与白名单）。
- Rules：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)、[Harness/rules/测试规范.md](../../rules/测试规范.md)。
- Memory：[qa-system-errortype-end-to-end.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-errortype-end-to-end.md)。
- 设计来源：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P2-1 `classifyError` 与 ErrorType 枚举）。
