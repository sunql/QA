# 变更：术语词典抽屉增加 admin 新增入口

- **日期**：2026-11-11
- **作者**：Claude
- **状态**：待合并（分支 `feat/term-dictionary-add-entry`，base `main@851d6049`）
- **迁移版本**：无（纯前端、零 DDL、零生产写、**不部署**）

---

## 1. 需求

**用户视角**：admin 在聊天页点「术语词典」打开抽屉，只能看到列表与删除，**找不到新增入口**。
新增入口此前只存在于**失败回答气泡旁**的 `TermDictionaryButton`（`MessageItem` 的 `message.isError`
分支）——正常流程下用户几乎看不到它，等于词条新增是隐藏功能。

**验收标准**：

1. admin 打开抽屉能看到「新增」按钮；非 admin 看不到（后端 `POST /term-dictionary` 已 403 兜底）；
2. 点「新增」弹出与气泡入口相同的表单；创建成功后抽屉列表自动重载；
3. 表单校验失败时**不调用创建接口**，且不产生未处理的 Promise rejection；
4. 既有 `TermDictionaryButton` / `MessageItem` / `ChatPage` 测试无回归；`tsc` 零错误；
5. `merge-gate / failure-set-diff` 净新增 0；不部署。

## 2. 变更清单

| 文件 | 变更 |
|---|---|
| `frontend/src/components/chat/TermDictionaryFormModal.tsx` | 新增。Modal + Form + `createTerm` 从按钮组件抽出；props `open / initialTerm / onCancel / onCreated` |
| `frontend/src/components/chat/TermDictionaryButton.tsx` | 改为复用共用 Modal；预填 / reset / toast / 静默 catch / admin 门禁保持不变 |
| `frontend/src/components/chat/TermDictionaryManager.tsx` | Drawer `extra` 按 admin 渲染「新增」；`onCreated → load()` 重载列表 |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 新增 `termDictionary.add`（新增 / Add） |
| `frontend/src/tests/TermDictionaryManager.test.tsx` | 新增 4 条：admin 可见 / 非 admin 不可见 / 提交调用并重载 / 校验失败不创建 |

顺带修复：原 `handleSubmit` 把 `await form.validateFields()` 放在 try 之外，校验失败会产生
未处理的 rejection（Vitest 会以 exit 1 收场，但失败不归属到用例节点）。抽出时改为
`.catch(() => null)` + 早退。

## 3. 验证证据

- `npx vitest run src/tests/TermDictionaryManager.test.tsx src/tests/TermDictionaryButton.test.tsx`
  → **12 passed**（RED 阶段先是「找不到 `新增` 按钮」的 3 条失败）
- `npx vitest run src/tests/MessageItem.test.tsx src/tests/ChatPage.test.tsx` → **74 passed**
- `npx tsc --noEmit` → **exit 0**
- code-reviewer：0 CRITICAL / 0 HIGH / 0 MEDIUM（3 LOW：重复提交窗口、rejection 断言不自证、成功路径少断言关闭）
- 本地重建 frontend 镜像后经 nginx 冒烟（admin）：`POST /api/v1/term-dictionary` **201** →
  列表可见 → `DELETE` **204**；构建产物包含 `新增` 文案

## 4. CI 闸门取证（退出码 5：净丢失 1 条，判为基线侧 flake）

- 基线 run `42888578074`（head `851d6049`）：失败 **143** 条；
  PR run `42930057960`（head `b180ccf5`）：失败 **142** 条 ⇒ 净新增 **0** / 净丢失 **1**。
- 净丢失用例：`app/tests/integration/test_seed_feature_rules.py::test_seed_risk_score_has_3_tiers`
  （前后端代码在本 PR 中零改动）。三件套证伪：

| 证据 | 结果 |
|---|---|
| ① PR 侧真跑且通过 | 同一 job（`Backend / integration shard 0`）汇总：基线 `34 failed, 694 passed` → PR `33 failed, 695 passed`，不多不少正好这一条由失败转通过 |
| ② 同代码不同结论 | 基线 head `851d6049` 正是本 PR 的 main 底座；`git diff origin/main...HEAD` 只含 4 个前端组件/i18n 文件 + 测试 + 本文档，无任何后端改动 |
| ③ 本批碰不到它 | 基线失败原因是 `MissingGreenlet`（lazy load `rule.thresholds` 在异步会话外触发），属会话态相关的非确定性失败；diff 与该文件、`seedFeatureRules`、lazy-load 机制均无关 |

⇒ 判「基线侧 flake」，非把失败藏起来。

## 5. 明确未做的事

- 未动后端（写路由已是 `requireAdmin`，见 `fix-governance-write-gate`）。
- 未把「新增」入口放到抽屉的工具栏外（如表格上方），未改权限模型。
- 未部署；生产仍在 alembic `0140`。
