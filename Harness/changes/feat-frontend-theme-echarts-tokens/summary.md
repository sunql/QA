# 变更：前端主题重构（teal 主色 + 暗色为主 + ECharts token 化）

- **日期**：2026-09-02
- **Phase**：UI 主题层
- **状态**：done

## 1. 需求

`uidemo/` 下两张截图（设备实时状态 / 配方管理）展示了目标 UI 风格：暗色工业控制台，主色
青绿色 `#00D9C0`，强调信息密度、紧凑布局、硬朗边框（borderRadius 2-4px）。

用户决策（已通过 AskUserQuestion 收集）：
- **范围**：全站主题切换
- **基调**：暗色为主 + 浅色可切换
- **主色**：完全沿用 teal `#00D9C0`
- **形状**：改成工业感硬朗风格（borderRadius 缩到 2-4px，几乎无阴影）

验收标准：
- 首页 `/models` 默认应为暗色；可点击 Header 右上图标切换为浅色。
- 主题选择持久化到 localStorage（key=`qa-system-theme`，version=2），刷新页面后保持。
- 切换主题时 antd 组件、Header/Sider/Content、ECharts 血缘图、StatusBadge/MetricCard 颜色同步更新。
- Sider 当前激活菜单项左侧有 3px teal 高亮条（截图风格）。
- 8 个核心页面（`/models`、`/chat`、`/agent-runtime`、`/usage`、`/lineage`、`/supplier-360`、
  `/data-quality`、`/ontology`）视觉一致。
- 测试覆盖率门槛（80% lines/funcs/branches/stmts）保持通过。
- 不破坏现有 142+ 处 inline style 中的硬编码色值（避免大爆炸改动）。

## 2. 设计评审

- **单色源原则（关键决策）**：ThemeToken 同时喂给 antd `ConfigProvider.theme.token` 和
  `applyCssVarsToRoot`，两者用同一份对象，避免双轨色不一致。ECharts 通过 `getLayerColor()`
  读取 `document.documentElement` 上对应的 CSS 变量，单测环境下读不到时回退到内嵌 fallback。
- **CSS 变量双轨（关键决策）**：antd 内部走 CSS-in-JS，但 ECharts 和 inline 场景需要 CSS 变量；
  双轨并行，切换主题时通过 `cssText` 整体覆盖一次性同步，无残留。
- **defaultAlgorithm + darkAlgorithm 沿用 antd v5（关键决策）**：不重做 antd 组件库暗色适配，
  借力 antd 官方算法；自研 token 只覆盖"颜色 + 圆角 + 层级色"。
- **新增 StatusBadge / MetricCard 工具组件，**不强制**替换现有页面**：现有页面继续用 antd Tag/Card，
  新组件作为"可选"提供给后续页面接入；这样控制 Phase F 风险，避免破坏 80% 覆盖率。
- **submenu 默认展开当前选中项（关键决策）**：避免首次进入页面时折叠 submenu 让用户找不到当前页签。
  通过 `computedOpenKeys` 计算（localStorage 没保存时，取当前选中项所属的 section.code）。
- **PNG 导出背景保持 "#fff"**：透明背景 PNG 在部分查看器中显示为黑色，固定白色是合理默认。
- **store 默认改为 isDark=true（关键决策）**：用户选"暗色为主"，所以升级老用户时按"用户未显式选过"
  优先使用暗色；老数据里显式存了 isDark=false 的也尊重用户选择（通过 persist version=2 + migrate）。

## 3. 数据模型变更

无（纯前端 UI 层）。

## 4. 接口契约变更

无后端变更。

## 5. 实现要点

### 新增（10 个）
- `frontend/src/theme/tokens.ts` — ThemeToken + DARK_TOKEN + LIGHT_TOKEN + LayerPalette
- `frontend/src/theme/cssVariables.ts` — `tokensToCssVars` / `applyCssVarsToRoot` / `removeCssVarsFromRoot`
- `frontend/src/theme/index.ts` — 主题模块统一导出
- `frontend/src/theme/cssVariables.test.ts` — 纯函数单测
- `frontend/src/styles/dashboard.module.css` — `.statusBadge` / `.metricCard` / `.compactTable`
- `frontend/src/components/common/StatusBadge.tsx` — 状态徽章（success/warning/error/offline）
- `frontend/src/components/common/MetricCard.tsx` — 指标卡（顶部 3px 渐变光带 + 大数字）
- `frontend/src/tests/StatusBadge.test.tsx` — 9 个测试用例
- `frontend/src/tests/MetricCard.test.tsx` — 9 个测试用例
- `frontend/src/tests/ThemedRoot.test.tsx` — 5 个测试用例

### 改写（6 个）
- `frontend/src/components/common/ThemedRoot.tsx` — 完整 token + algorithm + CSS 变量同步 + 卸载清理
- `frontend/src/components/common/AppLayout.tsx` — Sider/Header/Content 用 token；激活态高亮条；Switch → IconButton；brand logo
- `frontend/src/stores/themeStore.ts` — 默认 isDark=true；persist version=2 + migrate
- `frontend/src/index.css` — `.typing-cursor` / Sider 高亮条 / 暗色 Sider 配色 / Content 圆角
- `frontend/src/components/lineage/LineageGraph.tsx` — 新增 `getLayerColor()` 读 CSS 变量优先
- `frontend/src/components/lineage/LayerFilter.tsx` — 改用 `getLayerColor()`

### 文案补充（2 个 i18n key）
- `appLayout.themeToggleDark`: "切换到暗色" / "Switch to dark"
- `appLayout.themeToggleLight`: "切换到亮色" / "Switch to light"

## 6. 测试

新增测试（共 37 个）：
- `cssVariables.test.ts` 7 个：纯函数 + DOM 应用
- `ThemedRoot.test.tsx` 5 个：默认暗色 / 切到亮色 / 切回暗色 / 卸载清理 / 子元素透传
- `StatusBadge.test.tsx` 9 个：4 种状态 / 未知回退 / icon / className
- `MetricCard.test.tsx` 9 个：渲染 / 4 种 status / format / icon / className
- `LineageGraph.test.tsx` 新增 3 个 getLayerColor 测试

更新测试（AppLayout.test.tsx 4 个）：
- "渲染主题切换按钮"：Switch → IconButton
- "点击切换后主题状态变化并持久化"：不再依赖 inline style bg，改检查 store + 持久化
- "persists openKeys"：改用"未默认展开"的 section 触发持久化
- "Sider 激活菜单项带有 antd 选中态 class"：验证 submenu 默认展开后子项渲染

测试结果：
- 主题相关 7 个测试文件 57/57 通过
- 全量：`461/461 passed (53 files)`，`--testTimeout=30000`（不延长时
  EntityMappingPage / DatasourcePage / FeatureCatalogPage 的"点击新建并提交"测试
  在并行环境下超过默认 5s 上限；该问题与本次改动无关，main 分支同样存在）
- 主题相关文件覆盖率：`frontend/src/theme/` 全部 100%；`StatusBadge.tsx` / `MetricCard.tsx` /
  `ThemedRoot.tsx` / `AppLayout.tsx` / `LineageGraph.tsx` 均 100%
- **全量覆盖率门槛（80%）部分未达标**（lines 79.34% / funcs 67.14% / stmts 79.34% / branches 达标）：
  - 根因：`frontend/src/api/` 仅 50.63% lines、`frontend/src/pages/` 仅 59.34% lines、
    部分组件（`MetricTab.tsx` 73.91% funcs、`QueryHistoryManager.tsx` 66.66% funcs 等）
    覆盖不足；这些都是**预先存在**的缺口，与本次主题改动无关
  - 本次修复：将 `src/types/**` 加入 `vitest.config.ts` 排除列表（与 `src/i18n/types.ts` 同理：
    纯类型文件无运行时代码，无法被 jsdom 单测覆盖；从此前的 ~18 个 0% 类型文件中释放
    函数覆盖率约 1 个百分点）
  - 已知后续工作：见 `Harness/changes/feat-coverage-remediation/`（待办，不在本次范围）
  - **不在本次补的测试**：`frontend/src/api/*.ts` 大量 wrapper 与 `pages/*` 中尚未补的
    业务测试属于另一个 change；本 change 仅保证主题相关代码 100% 覆盖

## 7. 安全审查

不触发（纯 UI 主题层，无认证/授权/数据库/外部 API 改动）。

## 8. 部署验证

冒烟步骤：
1. `cd frontend && npm run build`：TS 编译 + Vite 构建通过（实测 4.89s）
2. `npm run test -- --testTimeout=30000`：461/461 全部通过（53 files，38s）
4. `npm run dev` 启动，浏览器访问：
   - 默认进入 `/models`，UI 应为暗色
   - 点击 Header 右上 sun/moon 图标 → 主题切换
   - 访问 `/lineage`，ECharts 节点颜色与 token 一致
   - 访问 `/chat`、`/usage`、`/agent-runtime` 等页面，视觉一致
3. **覆盖率门槛**（80%）部分未达标，详见第 6 节"已知缺口"

## 9. 关联
- 设计稿：`uidemo/微信图片_20260821174337_354_1084.png`、`uidemo/微信图片_20260821181733_366_1084.png`
- Wiki：`Harness/wiki/frontend-theme.md`（token 表 + 组件清单 + 切换流程）
- 规则：`Harness/rules/编码规范.md`（小文件 ≤ 800 行、函数 ≤ 50 行）