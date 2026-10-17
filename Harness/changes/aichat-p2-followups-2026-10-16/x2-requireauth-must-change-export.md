# X2 — must_change_password 前端 5 红收尾（PR #38）

> 状态: ✅ 已合（branch: fix/qa-1012-p0-x2, commit 7da6a7f0, PR https://github.com/sunql/QA/pull/38）
> 来源: 1012 cross-refs spec §P0-X2 + aichat-p2-followups SSOT 序 3 第 2 项
> 关联: `docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`

## 问题

QA-1012-01 #3：must_change_password 无服务端限制。

**后端**已在 PR #9 (audit-fx-1.3) 合入 `mustChangePasswordGuard` + `ALLOWED_PATHS_WHEN_MUST_CHANGE`，所有非 auth 路由挂 `Depends(mustChangePasswordGuard)`。

**前端** RequireAuth.tsx 配套函数 `isAllowedWhenMustChange`：
1. 未 export → 5 个单测无法直接导入断言（main baseline 长期 5 红）
2. 边界行为（精确 / 子路径 / 前缀误判 / 不相关）已隐式正确，但**未钉死**：
   `p + "/"` 拼 startsWith 防 `/auth/change-password-v2` 被 `/auth/change-password` 误判

## 修法

### export `isAllowedWhenMustChange` 与 `ALLOWED_WHEN_MUST_CHANGE`

```typescript
// frontend/src/components/common/RequireAuth.tsx
export const ALLOWED_WHEN_MUST_CHANGE = ["/auth/change-password", "/auth/login"];

export function isAllowedWhenMustChange(pathname: string): boolean {
  return ALLOWED_WHEN_MUST_CHANGE.some(
    (p) => pathname === p || pathname.startsWith(p + "/")
  );
}
```

注释钉死前缀边界（`p + "/"` 而非 `p`），audit-fx-1.3 后端镜像。

## 验证

| 测试 | Before | After |
|---|---|---|
| `RequireAuth.test.ts`（5 单测）| 5 红 | 5 绿 |
| `__tests__/RequireAuth.test.tsx`（5 组件测）| 5 绿 | 5 绿（无回归）|

**全仓前端**：
| | Before | After | Delta |
|---|---|---|---|
| 失败文件 | 16 | 15 | -1 |
| 失败测试 | 29 | 24 | -5 |

剩余 15 红文件均为 main baseline rot（chatStore 系列 / ChatPage / authStore / DocumentQaPanel / MessageItem / chatApi / chatKpiWarning / EntityMappingPage / pages / chatStoreHydration / App）—— 后续 PR 处理。

## 风险评估

- 单文件改动，12 增 / 3 删
- 仅 `export` 与注释加详细解释，**零行为变更**（生产代码路径未动）
- 5 个原本红的单测现在绿 —— 验路径匹配逻辑本就正确，只是测不到

## 后续 SSOT 进度

| 序 | 项 | 估时 | 状态 |
|---|---|---|---|
| 1 | Frontend 7 红 + chatStore 8 红 | 4h | ✅ PR #36 |
| 2 | Task 12.5 code-reviewer | 1h | ✅ PR #32 |
| 3.X1 | 模型配置 admin 守卫 | 2h | ✅ PR #37 |
| 3.X2 | must_change_password 前端收尾 | 30m | ✅ **本 PR #38** |
| 3.X3 | 检查点恢复保留研究问题 | 2h | ⏳（依赖 research 线） |
| 3.X4 | 流式 CRLF + EOF 守门 | 2h | ⏳（chat 已修，research 未修） |
| 3.X5 | 会话归属 + 状态互斥 | 2h | ⏳（chat 互斥 ✅，research 只 cover create） |
| 4 | M5 stub 模式回填 | 4h | ⏳ |
| 6 | L1 prompt 注入 | 1d+ | ⏳ |
| 7 | N4 qwen 知识保留 | 独立 | ⏳ |
