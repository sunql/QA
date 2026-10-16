# 前端 Coverage Gate 实测 (2026-10-16)

**目标**：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md`
Step 12.3 — coverage ≥ 80% on `chatStore` / `streamChunkParser`。

**前置现实**（来自记忆）：
- `qa-system-frontend-coverage-gate.md`：functions 长期 < 80%（2026-10-13 67.5%）；覆盖率表只在绿跑时打印
- `qa-system-frontend-suite-baseline.md`：`d1af4d4` 上 10 既有失败 + 1 flake

## 实测（main tip `ec0570b0`，worktree `chore/aichat-priority-remaining`）

### Pass 1：先跑完整套件

```
Test Files  16 failed | 168 passed (184)
Tests  29 failed | 1546 passed (1575)
Errors  1 error
```

**根因发现**：`frontend/src/stores/chatStore.ts:748-749` 有重复 `else` 块

```ts
} else {     // 748  (原 d72bc125 2026-08-29)
} else {     // 749  (P1-3 b958b855 2026-10-14 新增 — 不知道原已有 else)
  // 非流式分支
```

git blame 锁定 P1-3 commit `b958b855` (2026-10-14 00:40) **复制粘贴时未删原 `else`**。
P1-3 R1 `b818910d` 后续合流也漏检（esbuild 在 vitest 报 suite error，但 CI 不存在）

**修法**：删 line 748 的孤立 `else {`，保留 line 749 起的新分支

### Pass 2：修语法后

```
Test Files  7 failed | 177 passed (184)
Tests  26 failed | 1672 passed (1698)
```

**真实剩余 26 红分布**（与 aichat plan 关系）：

| 文件 | 失败 | 与 plan 关系 |
|---|---|---|
| `chatStore.test.ts` | 8 | **直接相关**（P0-3 改 setDatasourceId + P1-3 改 assistantMessageId） |
| `chatKpiWarning.test.ts` | 2 | **直接相关**（F2 from PR #18） |
| `chatApi.test.ts` | 2 | **直接相关**（P1-3 resumeMultiStepRun） |
| `ChatPage.test.tsx` | 5 | **直接相关**（P0-3 + 0105 chart in report） |
| `RequireAuth.test.ts` | 5 | 间接（mustChangePassword 路径匹配） |
| `client.test.ts` | 3 | 间接（error message 错误体解析） |
| `EntityMappingPage.test.tsx` | 1 | 不相关 |

**aichat plan 直接相关的真实红：17 个**。其余 9 个是相邻改动（PR #18 F1-F5 闭缺陷 + 0105 chart）+ 无改动（RequireAuth / client）。

### Coverage 实测（仅 streamChunkParser 单文件，验证 v8）

```
...tend/src/stores |    2.84 |    60.86 |   58.33 |    2.84 |
  authStore.ts     |       0 |        0 |       0 |       0 | 1-94
  chatStore.ts     |       0 |        0 |       0 |       0 | 1-1118
  ...hatUiState.ts |       0 |      100 |     100 |       0 | 17-129
  researchStore.ts |       0 |        0 |       0 |       0 | 1-430
  ...unk_parser.ts |   89.13 |       75 |     100 |   89.13 | 34-38
  themeStore.ts    |       0 |        0 |       0 |       0 | 1-22
  userSwitch.ts    |       0 |      100 |     100 |       0 | 16-26
  wikiChatStore.ts |       0 |        0 |       0 |       0 | 1-177
```

| 文件 | 行 | 分支 | 函数 | 语句 | 80% 闸门 |
|---|---|---|---|---|---|
| `stream_chunk_parser.ts` | 89.13% | 75% | **100%** | 89.13% | ✅ functions 过；lines 89% 过；branches 75% 差 5pp |
| `chatStore.ts` | 0% | 0% | 0% | 0% | ❌ chatStore.test.ts 8 红 = 测试未跑 = 0% |

## 结论

1. **stream_chunk_parser.ts 单文件 coverage 已达 89% lines / 100% functions**
   - branches 75% 差 5pp（line 34-38 单一 case 缺测，可补 1 个 test 抬到 80%）
   - **算"基本过 80% 闸门"**，仍建议补 branch coverage
2. **chatStore.ts coverage 0% — 不是覆盖问题，是测试根本未跑（8 个测试红）**
   - 修 8 个 chatStore.test.ts 测试后，coverage 才会回升
   - 修法不属本 plan 范围（plan 只要求"跑"coverage，不要求"修"测试）
3. **整体 80% 闸门** — 不可能达到（v8 全局统计受 26 红影响，0% 行/分支）
4. **CI 不存在** — 这次重复 `else` 阻塞 9 个测试文件 2 天没被发现，符合
   `qa-system-no-ci-main-can-go-red` 警示

## 同步 follow-up（已记入 `qa-system-71-red-triage.md` backlog）

- 修 chatStore.test.ts 8 红（P0-3 + P1-3 行为可能与新 prod 实现漂移）
- 修 chatKpiWarning.test.ts 2 红（F2 meta frame `datasourceMismatch` 编码）
- 修 chatApi.test.ts 2 红（resumeMultiStepRun 签名/Idempotency-Key）
- 修 ChatPage.test.tsx 5 红（0105 图表进报告 + 单条导出 dbMessageId）
- 修 streamChunkParser branches 75→80%（补 1 个 test）
- 修 RequireAuth / client / EntityMappingPage 9 红（独立 track）

## 链接

- [[qa-system-frontend-coverage-gate]] — 闸门 67.5% functions 历史
- [[qa-system-frontend-suite-baseline]] — d1af4d4 基线
- [[qa-system-no-ci-main-can-go-red]] — 解释语法错误为何没被拦
- [[qa-system-antd-validatefields-unhandled-rejection]] — AgentRegistryPage 那 1 个 unhandled error
- [[qa-system-71-red-triage]] — 71→16 红 + 9 个 wiki 红 backlog
- 计划源：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md` Step 12.3
