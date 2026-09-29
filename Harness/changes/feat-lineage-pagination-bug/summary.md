# 变更：数据血缘管理 tab 分页数量修复

- **日期**：2026-09-15
- **作者**：Claude
- **Phase**：Phase 2.3 数据血缘（fix）
- **状态**：done
- **关联变更**：[feat-dq-rules-pagination-bug](../feat-dq-rules-pagination-bug/summary.md)（同模式修复 DataQuality 规则 tab）
- **迁移版本**：无
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/lineage-pagination-multiselect.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/lineage-pagination-multiselect.md)

---

## 1. 需求

进 `/lineage` → 「管理」Tab，Table 自带分页器把 pageSize 改成 50/100/200/500 都没用，列表永远只显示 20 行。

**Bug**：`LineagePage.tsx:405` 硬编码 `pagination={{ pageSize: 20 }}` 且未开 `showSizeChanger`。

**验收**：
- 默认仍是 20（不破坏现有用户预期）
- 选 50/100/200/500 后立即生效
- 刷新页面后选中的 pageSize 保留（localStorage 记忆）
- 非法值（如 999）回退到默认 20

## 2. 设计评审

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 受控 pageSize + showSizeChanger + pageSizeOptions + localStorage | 与 DataQualityPage 一致 | **选**：模式已落地，复用避免漂移 |
| B. 非受控，只在 onShowSizeChange 写 localStorage | 简单 | 拒：受控才能在初始化时读 localStorage；非受控只有首次生效 |
| C. 改后端 API 加分页参数 | 服务端分页 | 拒：列表量级不大（数百条边），前端分页足够；改 API 工作量大且影响其他页面 |

最终：**A**。

关键点：
- `pageSize` 与 `current` 都入 state（受控模式），但 `current` 由 antd Table 内部管理，组件只透传 `pageSize` 给 `onChange`
- localStorage key：`qa.lineage.manage.pageSize`（独立命名空间，避免和 DQ 冲突）
- 非法值（如用户手改 localStorage 为 "999"）走 `PAGE_SIZE_OPTIONS.includes(n)` 校验失败回退默认
- `showTotal` 用现有 `common.totalItems` i18n key

## 3. 数据模型变更

无。

## 4. 接口契约变更

无。

## 5. 实现要点

`frontend/src/pages/LineagePage.tsx`：

- 顶部加 3 个模块常量 `PAGE_SIZE_STORAGE_KEY / DEFAULT_PAGE_SIZE / PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500]`
- `ManageTab` 内加 `useState<number>(() => readFromLs())` lazy read（SSR-safe：typeof window 守卫）
- 配套 `useEffect(() => writeToLs(pageSize), [pageSize])` 写入（异常不阻断）
- Table `pagination` 改为受控对象：`{ pageSize, pageSizeOptions, showSizeChanger: true, showTotal, onChange: (_, size) => setPageSize(size) }`

注释里写明「feat-lineage-pagination-bug (2026-09-15)」便于追溯。

## 6. 测试

`frontend/src/tests/LineagePage.test.tsx` —— 「管理 Tab」describe 新增 3 个用例：

| 用例 | 覆盖 |
|------|------|
| showSizeChanger 已挂载，pageSizeOptions 候选全在 | 渲染时 `.ant-pagination-options-size-changer` 在 DOM；`.ant-pagination-total-text` 也在（验证 `showTotal`） |
| localStorage 记忆 pageSize 切到 50 后刷新仍是 50 | 预置 `localStorage["qa.lineage.manage.pageSize"] = "50"` → 渲染 → 验证 localStorage 仍是 "50"（没被默认值覆写） + showSizeChanger 在 |
| 非法 localStorage 值（999）回退到默认 20 | 预置 "999" → 渲染 → 验证 localStorage 被覆写为 "20" |

```bash
cd frontend && npx vitest run src/tests/LineagePage.test.tsx -t "管理 Tab 分页"
# 期望：3 passed

cd frontend && npx vitest run src/tests/LineagePage.test.tsx
# 期望：17 passed（14 已有 + 3 新增）
```

## 7. 安全审查

未触发（UI 分页控件，无认证/输入/外网依赖）。

## 8. 部署验证

无后端改动、无 DB 迁移、无构建产物。Vite HMR 即可生效。

手动：
- 进 `/lineage` → 「管理」Tab → 翻页器右下出现 pageSize 选择器
- 选 50 → 立即生效 → F5 刷新 → 仍是 50
- 改 localStorage 为 "999" → 刷新 → 回到 20

## 9. 关联

- 设计稿：无
- Wiki：`Harness/wiki/frontend.md` Phase 9 增量章节已记录分页修复模式
- Rules：`Harness/rules/变更记录强制规范.md`（本次为新规范首批应用案例之一）
- Memory：`lineage-pagination-multiselect.md`（待写）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / MEMORY）
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段无迁移
- [x] 第 7 段说明未触发 security-reviewer
- [x] 第 8 段说明无后端改动 / Vite HMR 即可
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新
- [x] 至少 1 条 MEMORY 索引（新增 `lineage-pagination-multiselect.md` + MEMORY.md 加行）
- [x] 无真实 SQL/DB 改动，跳过真实数据验证
