---
created: 2026-09-02
updated: 2026-09-02
---

# Frontend Theme System

> 前端主题系统（feat-frontend-theme-echarts-tokens）。
>
> 设计目标：参考 `uidemo/` 截图风格——暗色工业控制台，主色青绿色 `#00D9C0`，紧凑硬朗。
> 实现路径：CSS 变量 + antd v5 Token 双轨，单色源驱动。

## 1. 三大原则

1. **单色源（Single Source of Truth）**
   - `ThemeToken` 同时喂给 antd `ConfigProvider.theme.token` 和 CSS 变量轨道。
   - 任何颜色变更都改 `tokens.ts`，下游消费方自动跟随。
2. **暗色为主 + 浅色可切换**
   - `useThemeStore.isDark` 默认 `true`（暗色为新用户默认；老用户保留显式选择）。
   - 切换走 antd `darkAlgorithm` / `defaultAlgorithm` 双轨 + CSS 变量整体替换。
3. **硬朗形状（borderRadius 2-4px）**
   - 全局 `borderRadius=2`、`borderRadiusLG=4`，几乎无阴影（详见 `index.css` 覆盖）。

## 2. 文件清单

| 文件 | 角色 |
|---|---|
| `frontend/src/theme/tokens.ts` | ThemeToken 类型 + DARK_TOKEN + LIGHT_TOKEN + LayerPalette |
| `frontend/src/theme/cssVariables.ts` | `tokensToCssVars` / `applyCssVarsToRoot` / `removeCssVarsFromRoot` |
| `frontend/src/theme/index.ts` | 主题模块统一导出 |
| `frontend/src/components/common/ThemedRoot.tsx` | ConfigProvider 唯一入口 + CSS 变量同步 |
| `frontend/src/components/common/AppLayout.tsx` | Sider + Header + Content 布局 |
| `frontend/src/stores/themeStore.ts` | Zustand store + persist v2 |
| `frontend/src/index.css` | Sider 高亮条 / Content 圆角 / 暗色 Sider 配色 |
| `frontend/src/styles/dashboard.module.css` | `.statusBadge*` / `.metricCard*` / `.compactTable` |
| `frontend/src/components/common/StatusBadge.tsx` | 状态徽章（success / warning / error / offline） |
| `frontend/src/components/common/MetricCard.tsx` | 指标卡（顶部 3px 渐变光带 + 大数字） |
| `frontend/src/components/lineage/LineageGraph.tsx` | `getLayerColor()` 读 CSS 变量 |

## 3. ThemeToken 色板（暗色 / DARK_TOKEN）

| Token | HEX | 用途 |
|---|---|---|
| `colorPrimary` | `#00D9C0` | 主色（按钮、链接、激活态、Sider 高亮条） |
| `colorBgBase` | `#0f1e2e` | 页面底色 |
| `colorBgContainer` | `#152838` | 卡片 / 容器 / Header |
| `colorBorder` | `#1f3a52` | 边框 |
| `colorSuccess` | `#00D9C0` | 生产中、合格 |
| `colorWarning` | `#FF8C42` | 停机、能耗告警 |
| `colorError` | `#F56C6C` | 断网、失败 |
| `colorText` | `#FFFFFF` | 主文字 |
| `colorTextSecondary` | `#cbd5e1` | 次文字 |
| `colorTextTertiary` | `#94a3b8` | 辅助文字 |
| `borderRadius` | `2` | 全局基础圆角 |
| `borderRadiusLG` | `4` | 较大圆角（大卡片 / 容器） |
| `layers.layerSourceSystem` | `#1677ff` | 血缘层 SOURCE_SYSTEM |
| `layers.layerOds` | `#13c2c2` | 血缘层 ODS |
| `layers.layerDwd` | `#52c41a` | 血缘层 DWD |
| `layers.layerDws` | `#722ed1` | 血缘层 DWS |
| `layers.layerAds` | `#fa8c16` | 血缘层 ADS |
| `layers.layerKpi` | `#f5222d` | 血缘层 KPI |
| `layers.layerAi` | `#eb2f96` | 血缘层 AI |

亮色（`LIGHT_TOKEN`）值见 `tokens.ts`，主色为 teal `#00B8A9`（深一档以保证对比度）。

## 4. CSS 变量（注入到 `:root`）

`applyCssVarsToRoot(token)` 在 `ThemedRoot` 挂载时调用 `document.documentElement.style.cssText`
整体覆盖，**不留残留**。卸载时 `removeCssVarsFromRoot()` 清空已知变量（防 SSR 或下次挂载残留）。

变量名与 token 一一对应（kebab-case）：
- `--color-primary`、`--color-bg-base`、`--color-bg-container`、`--color-border`
- `--color-success`、`--color-warning`、`--color-error`
- `--color-text`、`--color-text-secondary`、`--color-text-tertiary`
- `--border-radius`、`--border-radius-lg`
- `--color-layer-source-system`、`--color-layer-ods`、`--color-layer-dwd`、
  `--color-layer-dws`、`--color-layer-ads`、`--color-layer-kpi`、`--color-layer-ai`

## 5. 主题切换流程

```
[User clicks ThemeToggle in Header]
       ↓
useThemeStore.toggleTheme()  → isDark = !isDark
       ↓
ThemedRoot re-render
       ↓
┌────────────────────────────────────────┐
│ 1. ConfigProvider.theme.token = new token
│ 2. ConfigProvider.theme.algorithm = darkAlgorithm | defaultAlgorithm
│ 3. useEffect → applyCssVarsToRoot(token)  // CSS 变量整体替换
└────────────────────────────────────────┘
       ↓
antd 全组件 / ECharts / inline style / StatusBadge / MetricCard 同步更新
       ↓
persist middleware → localStorage["qa-system-theme"]  (version=2)
```

## 6. 组件清单（新增）

### StatusBadge
```tsx
<StatusBadge status="success">生产中</StatusBadge>
<StatusBadge status="warning" icon={false}>停机</StatusBadge>
```
- 4 种 status：`success` / `warning` / `error` / `offline`
- 未知 status 健壮回退到 `offline`
- 默认带 icon，`icon={false}` 关闭

### MetricCard
```tsx
<MetricCard value={4506} label="总冲次数" unit="万冲" />
<MetricCard value={3} label="停机" status="warning" icon={<WarningIcon />} />
```
- 默认 status=`success`（中性青色顶部光带）
- 数字默认千分位格式化（`format={false}` 关闭）
- 顶部 3px 渐变光带颜色随 status 变化

## 7. 兼容性策略

- **现有 142+ 处 inline 硬编码色值不动**：本主题改造只新增 token + 新组件，不强制清理历史代码。
  后续页面可分批把硬编码色替换为 `var(--color-xxx)` 或 token。
- **老用户 localStorage 兼容**：persist `version=2`，老数据里显式存了 `isDark=false` 仍尊重用户选择；
  老数据里没存过的视为新用户，默认暗色。
- **ECharts 单测兼容**：`getLayerColor()` 在 jsdom 下读不到 CSS 变量，自动回退到
  `LAYER_COLORS` 内嵌值，不破坏现有 `LineageGraph.test.tsx`。

## 8. 验证清单（部署前）

- [ ] `cd frontend && npm run build` 通过（实测 4.88s）
- [x] `npx vitest run --testTimeout=30000` 461/461 全部通过（53 files，~38s）
- [x] `npx vitest run --coverage` 主题相关文件 100%；全量门槛部分未达标
  - 当前：lines 79.34% / funcs 67.14% / stmts 79.34% / branches 67%+
  - 已知缺口：`src/api/` 50.63% lines、`src/pages/` 59.34% lines、部分组件 funcs 偏低
  - 根因：预先存在，与本次主题改动无关（main 分支同样不达标）
  - 本次修复：`src/types/**` 加入 `vitest.config.ts` 排除列表（与 `src/i18n/types.ts` 同理，
    纯类型文件无运行时代码，无法被 jsdom 单测覆盖）
  - 后续工作：见 `Harness/changes/feat-frontend-theme-echarts-tokens/summary.md` §6
- [ ] `npm run dev` 启动，访问 `/models` 默认暗色
- [ ] 点击 Header 右上图标切换主题，刷新页面（F5）保持
- [ ] 访问 `/lineage`，ECharts 节点颜色与 token 一致
- [ ] Sider 激活项左侧有 3px teal 高亮条
- [ ] 浏览器 DevTools Lighthouse / axe 抽检 3 个核心页面 WCAG AA 对比度

## 9. 相关文档

- 变更 SSOT：`Harness/changes/feat-frontend-theme-echarts-tokens/summary.md`
- 设计稿参考：`uidemo/微信图片_20260821174337_354_1084.png`、`uidemo/微信图片_20260821181733_366_1084.png`
- 编码规范：`Harness/rules/编码规范.md`（小文件、不可变、显式错误处理）
- 测试规范：`Harness/rules/测试规范.md`（80% 覆盖率门槛）