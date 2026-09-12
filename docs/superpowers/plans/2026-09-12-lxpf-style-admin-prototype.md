# LXPF 风格企业级后台原型实现计划

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在 `~/Projects/lxpf-style-admin-prototype/` 产出一套 22 个文件的 LXPF 风格企业级后台管理 HTML 静态原型（PC 8 页 + H5 5 页 + 总览 2 页 + 共享样式 6 文件 + README），零依赖、零构建、双击即开。

**Architecture:** 纯 HTML + 纯 CSS + 极少量原生 JS。样式通过 CSS 变量（design token）统一管理；每页 head 引用 4 个 CSS（base → tokens → components → layout-*）。无 JS 框架、无构建工具、无后端。

**Tech Stack:** HTML5 · CSS3（变量、Flex/Grid、@media）· 原生 JS（每页 < 30 行）· 系统字体栈 · 内联 SVG 图标

**Spec:** `docs/superpowers/specs/2026-09-12-lxpf-style-admin-prototype-design.md`

---

## Global Constraints

- 项目根路径：`~/Projects/lxpf-style-admin-prototype/`（git init 独立仓库）
- 零依赖：禁止 package.json / node_modules / 任何 CDN / 任何构建工具
- 零 lorem ipsum：所有 mockup 用真实中文示例（订单号、客户名、商品名等）
- 图表占位：dashboard 图表用 SVG 框 + ECharts 占位注释
- CSS 加载顺序固定：base → tokens → components → layout-*
- 每文件 ≤ 600 行（超出则拆分）
- 每任务结束必须 git commit
- 每页验证：Chrome 打开 + F12 0 报错 + 视觉对照 spec §5 线框
- 配色：主色 `#0958d9`、文字主 `rgba(0,0,0,0.88)`、容器 `#ffffff`、底色 `#f5f7fa`
- 断点：H5 在 375/414/768 排版正常；PC 在 1024/1280/1920 排版正常

---

## Phase 1：项目骨架与样式基础（Tasks 1-6）

### Task 1：初始化项目仓库与 README

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/README.md`
- Create: `~/Projects/lxpf-style-admin-prototype/.gitignore`
- Create: `~/Projects/lxpf-style-admin-prototype/index.html`（占位骨架）

- [ ] Step 1：创建目录并初始化 git

```bash
mkdir -p ~/Projects/lxpf-style-admin-prototype/{assets,pc,h5}
cd ~/Projects/lxpf-style-admin-prototype
git init
git config user.email "lxpf-style@prototype.local"
git config user.name "LXPF Prototype Author"
```

- [ ] Step 2：写 .gitignore

```
.DS_Store
*.swp
.vscode/
.idea/
```

- [ ] Step 3：写 README.md（项目说明 + 预览方式 + 目录结构 + 扩展指南）

README 完整内容大纲：
- 项目简介（1 段：LXPF 风格企业级后台静态原型）
- 预览方式（3 种：双击 index.html / `python3 -m http.server` / VSCode Live Server）
- 目录结构（直接照搬 spec §2）
- 设计 Token 速查（复制 spec §3 关键表）
- 组件清单（复制 spec §4 组件表前 10 行）
- 扩展新页面步骤（4 步：复制模板 + 引用 4 个 CSS + 替换内容 + 验证）
- 已知限制（无后端、无真实数据、图标为内联 SVG）

- [ ] Step 4：写 index.html 占位骨架

```html
<!doctype html>
<html lang="zh"><head><meta charset="UTF-8"><title>LXPF 风格原型 - 总览</title>
<link rel="stylesheet" href="assets/base.css">
<link rel="stylesheet" href="assets/tokens.css">
<link rel="stylesheet" href="assets/components.css">
</head><body>
<main style="padding:48px;text-align:center">
<h1>LXPF 风格原型 - 建设中</h1>
<p>详见 README.md</p>
</main></body></html>
```

- [ ] Step 5：浏览器打开 index.html 验证（F12 0 报错、样式生效）

- [ ] Step 6：commit

```bash
cd ~/Projects/lxpf-style-admin-prototype
git add .gitignore README.md index.html
git commit -m "feat: initialize project skeleton with README"
```

### Task 2：tokens.css（设计 Token 全部 CSS 变量）

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/tokens.css`

- [ ] Step 1：按 spec §3 完整定义 4 类 token

文件必须包含：
- :root 内 16 个颜色变量（主色 3 阶、辅助、4 个状态色、4 个中性色、4 个文字色）
- 8 个字号、3 个字重、8 个间距、5 个圆角、3 个阴影、4 个断点
- 3 个字体栈
- @media prefers-color-scheme dark 覆盖所有变量
- [data-theme="dark"] 覆盖所有变量（与 @media 块值相同）

完整 token 列表必须照搬 spec §3.1-3.9，不增不减。

- [ ] Step 2：在 index.html 引入并验证

修改 index.html head 增加 `<link rel="stylesheet" href="assets/tokens.css">`，Chrome 打开，DevTools → Elements → :root 看到所有变量已注册。

- [ ] Step 3：commit

```bash
git add assets/tokens.css index.html
git commit -m "feat(assets): design tokens with light/dark mode"
```

### Task 3：base.css（reset + 全局样式）

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/base.css`

- [ ] Step 1：写 base.css

必须包含：
- 通用 reset（box-sizing、margin、padding、字体继承）
- html / body 基础（字体栈、字号 14px、背景色、文字色、line-height 1.5）
- 链接样式（color primary，无下划线，hover 下划线）
- 图片 / 视频 / svg 响应式（max-width 100%、display block）
- 滚动条样式（webkit + firefox）
- 选中文本样式（::selection 背景 primary-50）
- 通用工具类（.visually-hidden 用于无障碍）

- [ ] Step 2：index.html 引用并验证

- [ ] Step 3：commit

```bash
git add assets/base.css index.html
git commit -m "feat(assets): base reset and global styles"
```

### Task 4：components.css（20 个组件基础样式）

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/components.css`

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/components.css`（组件 1-10）
- Create: `~/Projects/lxpf-style-admin-prototype/assets/components-extra.css`（组件 11-20）
- Modify: `~/Projects/lxpf-style-admin-prototype/index.html`（追加 components-extra.css link）

> **2026-09-12 修订**：20 组件超 600 行上限，经用户裁决拆为 2 文件。CSS 加载顺序：`base → tokens → components → components-extra → layout-*`

- [ ] Step 1：写 20 个组件样式

按 spec §4 表格实现，组件顺序：
1. Button（6 type × 3 size × 5 state = 90 个变体用属性选择器简写）
2. Icon（.icon { width:24; height:24; fill: currentColor }）
3. Input（text/password/number/textarea/search，3 size，5 state）
4. Select（用 details/summary 原生实现）
5. DatePicker（输入框 + 占位日历浮层）
6. Table（基础表格 + 斑马纹 + 排序箭头 + 批量工具栏 + 分页器）
7. Card（标题 + 操作 + 内容）
8. Tag（6 色 × 2 形态）
9. Badge（数字 + 状态点）
10. Tooltip / Popover（details 模拟）
11. Drawer（右侧滑出 + 遮罩）
12. Modal（居中弹层 + 遮罩）
13. Message（顶部悬浮 + 4 状态 + 3 秒渐隐动画）
14. Pagination（总数 + 页码 + 每页 + 跳转）
15. Breadcrumb（> 分隔 + 当前页灰）
16. Tabs（line + card 两风格）
17. Progress（线形 + 圆环）
18. Avatar（圆形 + 文字 fallback + 状态点）
19. Empty（SVG 插画 + 文案 + 按钮）
20. Skeleton（段落 + 圆形 + 表格 + 卡片骨架）

- [ ] Step 2：在 design-system.html 临时占位中逐个验证（不需写 design-system.html 全量，先用临时页面调样式）

- [ ] Step 3：commit

```bash
git add assets/components.css
git commit -m "feat(assets): 20 component styles with variants and states"
```

### Task 5：layout-pc.css 与 layout-h5.css

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/layout-pc.css`
- Create: `~/Projects/lxpf-style-admin-prototype/assets/layout-h5.css`

- [ ] Step 1：写 layout-pc.css

必须包含：
- .app-shell（grid 布局：sidebar 220px | main 1fr）
- .topbar（高 56px，flex 横排，logo + search + actions + user）
- .sidebar（高 100vh，padding 16px，菜单项 40px 高，hover/active 态）
- .sidebar.collapsed（宽 64px）
- .breadcrumb（横排 padding 16px 24px，背景白，border-bottom）
- .page-tabs（38px 高，白底，标签可关闭）
- .main-content（padding 24px）
- .page-header（flex 横排，左标题右操作）
- .card-section（白底 + 圆角 6 + shadow-sm + padding 24）
- 响应式：@media (max-width:1024px) 自动折叠 sidebar

- [ ] Step 2：写 layout-h5.css

必须包含：
- .h5-shell（flex column，min-height 100vh，max-width 750px 居中）
- .statusbar（高 24px，模拟手机状态栏）
- .navbar（高 44px，白底，shadow-sm，左返回 + 中标题 + 右操作）
- .content（flex 1，padding 16px，背景 bg-base）
- .tabbar（高 56px，白底，border-top，flex 横排 4 等分）
- .tabbar-item（垂直布局，图标 + 文字，active 态 primary 色）
- .pull-refresh 占位（顶部 60px 灰色条）
- 响应式：@media (min-width:768px) 内容区 max-width 750px 居中

- [ ] Step 3：临时 index.html 引入两个 CSS 验证基本布局不报错

- [ ] Step 4：commit

```bash
git add assets/layout-pc.css assets/layout-h5.css
git commit -m "feat(assets): pc and h5 layout shells"
```

### Task 6：icons.svg（图标雪碧图）

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/assets/icons.svg`

- [ ] Step 1：编写 SVG 雪碧图

文件结构：
```xml
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
  <defs>
    <symbol id="i-home" viewBox="0 0 24 24"><path d="..."/></symbol>
    <symbol id="i-user" viewBox="0 0 24 24"><path d="..."/></symbol>
    <!-- 共 30+ symbol -->
  </defs>
</svg>
```

必须包含 30+ symbol（id 命名 `i-{name}`）：
i-home, i-user, i-setting, i-bell, i-search, i-filter, i-plus, i-edit, i-delete, i-eye, i-download, i-upload, i-check, i-close, i-menu, i-logout, i-chart, i-team, i-file, i-tag, i-calendar, i-location, i-message, i-star, i-lock, i-arrow-left, i-arrow-right, i-arrow-up, i-arrow-down, i-more, i-cart, i-wallet, i-document, i-folder, i-refresh

每个 symbol 路径 24x24 viewBox 线性 1.5px stroke 风格，currentColor 描边。

- [ ] Step 2：临时写测试页验证 5 个图标能通过 `<svg><use href="assets/icons.svg#i-home"/></svg>` 显示

- [ ] Step 3：commit

```bash
git add assets/icons.svg
git commit -m "feat(assets): inline svg icon sprite (30+ icons)"
```

---

## Phase 2：总览页（Tasks 7-8）

### Task 7：index.html 总览导航

**Files:**
- Modify: `~/Projects/lxpf-style-admin-prototype/index.html`

- [ ] Step 1：实现总览页

页面结构：
- header：左 logo + 标题「LXPF 风格企业级后台管理系统原型」+ 右链接（design-system）
- 主体：4 个分组卡片网格
  - 设计系统（1 张卡）：design-system.html
  - PC 端（8 张卡）：链接到 pc/*.html
  - H5 端（5 张卡）：链接到 h5/*.html
  - 说明（1 张卡）：README.md 链接
- 每张卡：图标 + 标题 + 一句话描述 + 「打开 →」链接

- [ ] Step 2：Chrome 打开验证：所有链接可点、无 404、卡片网格在 1280px 排 4 列、1024px 排 2 列、768px 排 1 列

- [ ] Step 3：commit

```bash
git add index.html
git commit -m "feat: overview navigation page with all mockup links"
```

### Task 8：design-system.html 设计系统总览

**Files:**
- Create: `~/Projects/lxpf-style-admin-prototype/design-system.html`

- [ ] Step 1：实现设计系统 showcase 页

页面结构（单列长页）：
- 标题 + 一句话说明
- Section 1：色板（16 个色块网格 + token 名 + 用途）
- Section 2：字号（8 个 size 实际渲染示例）
- Section 3：字重（3 个 weight 实际渲染示例）
- Section 4：间距（8 个 size 横向条形可视化）
- Section 5：圆角（5 个 size 矩形预览）
- Section 6：阴影（3 个 size 卡片预览）
- Section 7：组件 showcase（20 个组件每组件一段标题 + 实际渲染示例 + 变体切换）
- Section 8：图标库（30+ 图标网格）
- 右侧悬浮目录锚点导航（position sticky）

- [ ] Step 2：Chrome 打开验证：所有 token 与组件完整呈现、目录锚点跳转正常、F12 0 报错

- [ ] Step 3：commit

```bash
git add design-system.html
git commit -m "feat: design system showcase page (tokens + components + icons)"
```

---

## Phase 3：PC 端 8 个页面（Tasks 9-16）

每页通用结构：
- head 引用 4 个 CSS（base → tokens → components → layout-pc）
- body 用 .app-shell > (.topbar + .sidebar) + main
- 视觉对照 spec §5.X 线框
- 真实中文示例数据
- Chrome 1280×800 验证无横向滚动条

### Task 9：pc/login.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/login.html`

- [ ] Step 1：照 spec §5.1 实现

页面要点：
- 不使用 .app-shell，自定义布局：50/50 网格
- 左半：CSS 渐变（linear-gradient 135deg, #0958d9 → #13c2c2）+ 居中品牌区（logo SVG + 大字「LXPF 快速开发平台」+ 一句话口号 + © 版权）
- 右半：垂直居中表单（max-width 360px）
  - 标题「欢迎登录 LXPF 后台」+ 副标题
  - 账号 input（prefix user icon）
  - 密码 input（type=password + suffix eye icon）
  - checkbox 7天免登录 + 忘记密码链接
  - primary 按钮「登 录」全宽
  - 分割线「─── 或 ───」
  - 第三方登录：企业微信 + 钉钉 + 飞书 三个图标按钮横排
  - 底部：还没有账号？立即注册

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/login.html
git commit -m "feat(pc): login page with brand area and third-party login"
```

### Task 10：pc/workbench.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/workbench.html`

- [ ] Step 1：照 spec §5.2 实现

页面要点：
- 标准 .app-shell（顶栏 + 侧边栏 + 主区）
- 顶栏：logo + 搜索框 + 帮助 + 通知（badge 3）+ 头像下拉
- 侧边栏：6 个一级菜单（工作台 active、任务、看板、客户、订单、财务、设置）
- 主区：面包屑「首页 / 工作台」
- 4 个区块：
  - 待办事项（卡片，列表 4 条，右侧「查看全部 →」）
  - 快捷入口（卡片，2x2 网格：新建订单/添加客户/生成报表/导入数据）
  - 数据概览（卡片，3 个 KPI：订单数 1234 / 客户数 567 / GMV 89万）
  - 本月趋势（卡片，标题 + 占位折线图 SVG 框）

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/workbench.html
git commit -m "feat(pc): workbench with todos, quick actions, KPIs"
```

### Task 11：pc/list.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/list.html`

- [ ] Step 1：照 spec §5.3 实现

页面要点：
- .app-shell + 面包屑「订单管理 / 订单列表」
- 顶部操作区：左标题「订单列表」+ 右 [⬇ 导出] [+ 新建] 两个按钮
- 筛选区：4 个 input/select 横排（订单号/客户/状态/时间）+ 重置 + 搜索按钮
- 表格（20 行示例数据）：
  - 列：复选框 / 序号 / 订单号 / 客户 / 金额 / 状态(Tag) / 操作(查看···)
  - 状态 Tag 颜色映射：待发货=warning / 已发货=primary / 已完成=success / 已取消=default
  - 斑马纹 + 行 hover
- 已选工具栏（默认隐藏，mockup 中显示「已选 3 项」状态）：批量发货 / 批量删除 / 批量导出
- 底部分页器：共 1234 条 + 42 页 + 页码 + 每页 20 + 跳转

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/list.html
git commit -m "feat(pc): order list with filter, batch ops, pagination"
```

### Task 12：pc/form-edit.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/form-edit.html`

- [ ] Step 1：照 spec §5.4 实现

页面要点：
- .app-shell + 面包屑「订单管理 / 新建订单」
- 顶部操作区：左标题 + 右 [💾 保存草稿] [✓ 提交] 按钮
- 3 个 card-section：
  - 基础信息（4 个 input/select 横排：订单号禁用/客户类型/客户搜索/业务日期）
  - 商品明细（带 [+ 添加商品] 按钮，3 行明细表：序号/商品搜索/规格/单价/数量/小计/删除）
  - 其他信息（textarea 备注 + 文件上传区）
- 底部操作栏（右对齐）：[取消] [保存并提交]

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/form-edit.html
git commit -m "feat(pc): order form with sections, items table, upload"
```

### Task 13：pc/dashboard.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/dashboard.html`

- [ ] Step 1：照 spec §5.5 实现

页面要点：
- .app-shell + 面包屑「数据看板 / 销售概览」
- 顶部操作区：左标题 + 右 [时间选择器: 近 30 天 ▾] + [🔄 刷新] 按钮
- 4 个 KPI 卡片横排：销售额 89.2万 ▲12.5% / 订单数 1234 ▲8.2% / 客单价 723 ▲4.1% / 转化率 3.42% ▼0.3%
  - ▲ 用绿色、▼ 用红色
- 4 个图表卡片 2x2 网格（全部用占位 SVG 框）：
  - 销售趋势（折线）：标题 + 300x200 SVG 框 + 「ECharts 占位 - 折线」注释
  - 品类占比（饼图）：同上
  - TOP10 商品（柱状）：同上
  - 区域分布（中国地图）：同上

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/dashboard.html
git commit -m "feat(pc): sales dashboard with 4 KPIs and 4 chart placeholders"
```

### Task 14：pc/permission.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/permission.html`

- [ ] Step 1：照 spec §5.6 实现

页面要点：
- .app-shell + 面包屑「系统管理 / 角色权限」
- 主区 2 列 grid：左 280px + 右 1fr
- 左：角色列表卡（搜索框 + + 新建角色按钮 + 5 行角色：超级管理员 active / 销售经理 / 财务主管 / 普通员工 / 访客 disabled）
- 右：权限配置卡（选中角色 = 销售经理）
  - 顶部：角色名 + [✓ 启用] switch
  - 菜单权限 card-section（树状 checkbox，3 级：工作台/订单管理/客户管理/财务管理/系统管理）
  - 数据权限 card-section（3 个 radio：全部/本部门/本人）
  - 操作栏：[取消] [保存]

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/permission.html
git commit -m "feat(pc): role permission with tree and data scope"
```

### Task 15：pc/settings.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/settings.html`

- [ ] Step 1：照 spec §5.7 实现

页面要点：
- .app-shell + 面包屑「系统管理 / 设置中心」
- 顶部 Tabs（line 风格）：基本信息 / 安全设置 / 消息通知 / 集成应用 / 审计日志（5 个 tab，基本信息 active）
- 主体 card-section：基本信息系统表单
  - 系统名称（input）
  - 系统 Logo（上传 + 预览）
  - 主题色（4 个色块单选：深海蓝/翡翠绿/葡萄紫/商务黑）
  - 默认语言（3 个 radio）
  - 时区（select）
  - 日期格式（select）
  - 底部 [取消] [保存]

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/settings.html
git commit -m "feat(pc): settings center with tabs and system form"
```

### Task 16：pc/404.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/pc/404.html`

- [ ] Step 1：照 spec §5.8 实现

页面要点：
- 不使用 .app-shell，全屏居中
- SVG 插画（手绘 404 大字 + 装饰图形，300x200）
- 主标题「页面找不到了」
- 副标题「您访问的页面不存在或已被移除」
- 两个按钮：[< 返回上一页] [🏠 回到首页]

- [ ] Step 2：Chrome 1280×800 验证

- [ ] Step 3：commit

```bash
git add pc/404.html
git commit -m "feat(pc): 404 page with illustration and actions"
```

---

## Phase 4：H5 端 5 个页面（Tasks 17-21）

每页通用结构：
- head 引用 4 个 CSS（base → tokens → components → layout-h5）
- body 用 .h5-shell > (statusbar + navbar + content + tabbar)（除 login 外）
- Chrome devtools 设备模式 375×667 与 414×896 验证
- 真实中文示例数据

### Task 17：h5/login.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/h5/login.html`

- [ ] Step 1：照 spec §5.9 实现

页面要点：
- 不含 tabbar，自定义布局：statusbar + 内容垂直居中
- 顶部 logo + 品牌名
- 标题「欢迎回来」+ 副标题「登录账号，开启高效办公」
- 表单：
  - 手机号 input（prefix 手机 icon）
  - 密码 input（prefix 锁 icon + suffix 眼睛）
  - 记住我 checkbox + 忘记密码链接
  - 主按钮「登 录」全宽
  - 底部「还没有账号？立即注册」
  - 分割线「─── 第三方登录 ───」+ 3 个图标横排

- [ ] Step 2：Chrome devtools 375 + 414 验证

- [ ] Step 3：commit

```bash
git add h5/login.html
git commit -m "feat(h5): login with phone and third-party"
```

### Task 18：h5/home.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/h5/home.html`

- [ ] Step 1：照 spec §5.10 实现

页面要点：
- statusbar + 无 navbar（自定义头部）
- 自定义头部：左「早安，张三 👋」+ 时间副标题 + 右 🔔 badge
- 搜索框（带 placeholder）
- 2 个核心数据卡横排：今日订单 128 / 待办任务 12
- 常用功能：4 个图标 2x2 网格（新建订单/客户/订单/财务）
- 待办列表（4 条）+ 右「→」
- 底部 tabbar（首页 active）

- [ ] Step 2：Chrome devtools 375 + 414 验证

- [ ] Step 3：commit

```bash
git add h5/home.html
git commit -m "feat(h5): home with greeting, KPIs, quick actions, todos"
```

### Task 19：h5/list.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/h5/list.html`

- [ ] Step 1：照 spec §5.11 实现

页面要点：
- navbar：左 [< 返回] + 中「订单列表」+ 右 [🔍] [⋮]
- 筛选条：状态 select + 时间 select
- 列表：每条 card（订单号 + 金额右对齐 / 客户 + 状态 Tag / 时间 + 右箭头 ›）
- 列表 8 条 + 底部「加载更多 ↓」
- 无 tabbar

- [ ] Step 2：Chrome devtools 375 + 414 验证

- [ ] Step 3：commit

```bash
git add h5/list.html
git commit -m "feat(h5): order list with filter, card items, load more"
```

### Task 20：h5/detail.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/h5/detail.html`

- [ ] Step 1：照 spec §5.12 实现

页面要点：
- navbar：左返回 + 中「订单详情」+ 右 [⋮]
- 头部状态卡：订单号大字 + 状态 Tag 大
- 基本信息 card（key-value 列表 6 项）
- 商品明细 card（2 个商品行，名称 + 单价 × 数量 = 小计）
- 底部操作栏（fixed）：[取消订单] secondary + [确认收货] primary

- [ ] Step 2：Chrome devtools 375 + 414 验证

- [ ] Step 3：commit

```bash
git add h5/detail.html
git commit -m "feat(h5): order detail with status, info, items, actions"
```

### Task 21：h5/profile.html

**Files:** Create: `~/Projects/lxpf-style-admin-prototype/h5/profile.html`

- [ ] Step 1：照 spec §5.13 实现

页面要点：
- navbar：左「个人中心」+ 右 [⚙ 设置]
- 头像卡：64x64 圆形头像 + 姓名 + 部门岗位 + 邮箱
- 数据卡：3 列（我的订单 128 / 我的客户 56 / 业绩 89万）
- 菜单列表：6 行（我的待办 12→ / 我创建的订单 128→ / 我的客户 56→ / 业绩报表→ / 消息中心 3→ / 联系客服→ / 关于 LXPF→）
- 退出登录按钮（danger outlined）
- 底部 tabbar（我的 active）

- [ ] Step 2：Chrome devtools 375 + 414 验证

- [ ] Step 3：commit

```bash
git add h5/profile.html
git commit -m "feat(h5): profile with avatar, KPIs, menu list, logout"
```

---

## Phase 5：验收（Task 22）

### Task 22：自检与物证截图

**Files:** Modify: `~/Projects/lxpf-style-admin-prototype/README.md`（追加截图章节）

- [ ] Step 1：文件存在性自检

```bash
cd ~/Projects/lxpf-style-admin-prototype
ls -la index.html design-system.html assets/ pc/ h5/
# 期望：22 个文件全在
```

- [ ] Step 2：HTML 语法快速校验

```bash
for f in index.html design-system.html pc/*.html h5/*.html; do
  echo "=== $f ==="
  python3 -c "from html.parser import HTMLParser
import sys
class P(HTMLParser):
    def error(self, msg): print(f'ERROR: {msg}')
P().feed(open('$f').read())" || echo "parse fail"
done
```

- [ ] Step 3：devtools 截图物证

打开 Chrome，依次截图：
- index.html（1280×800）
- design-system.html（1280×800）
- pc/workbench.html（1280×800）
- pc/dashboard.html（1280×800）
- pc/permission.html（1280×800）
- h5/home.html（devtools 设备 375×667）
- h5/list.html（devtools 设备 375×667）

保存到 `~/Projects/lxpf-style-admin-prototype/screenshots/` 目录。

- [ ] Step 4：README 追加截图章节

在 README 末尾增加：
```markdown
## 预览截图

详见 `screenshots/` 目录。

| 页面 | 视口 | 路径 |
|------|------|------|
| 总览 | 1280×800 | `screenshots/01-overview.png` |
| 设计系统 | 1280×800 | `screenshots/02-design-system.png` |
| PC 工作台 | 1280×800 | `screenshots/03-pc-workbench.png` |
| PC 看板 | 1280×800 | `screenshots/04-pc-dashboard.png` |
| PC 权限 | 1280×800 | `screenshots/05-pc-permission.png` |
| H5 首页 | 375×667 | `screenshots/06-h5-home.png` |
| H5 列表 | 375×667 | `screenshots/07-h5-list.png` |
```

- [ ] Step 5：commit screenshots 与 README 更新

```bash
git add screenshots/ README.md
git commit -m "docs: add screenshots as visual proof"
```

- [ ] Step 6：交付物总览 commit

```bash
git log --oneline | head -25
# 期望看到 22 个 commit + screenshots commit
```

- [ ] Step 7：验收清单逐项打勾（按 spec §7 Done 判定 7 项）

---

## 自检（写作时执行）

- ✅ Spec §3 全部 token 已在 Task 2 实现
- ✅ Spec §4 全部 20 组件已在 Task 4 实现
- ✅ Spec §5 13 页线框全部映射到 Task 9-21
- ✅ Spec §6 22 文件清单：2 + 6 + 13 + 1 = 22
- ✅ Spec §7 验收 7 项已映射到 Task 22
- ✅ 全局约束：零依赖/真实中文/无 lorem/ECharts 占位/每文件 ≤ 600 行/每任务 commit
- ✅ 配色锁定：主色 #0958d9 在 tokens.css 唯一定义
- ✅ CSS 加载顺序：4 个 link 顺序 base → tokens → components → layout-* 贯穿所有页面
- ✅ 类型一致：.app-shell、.h5-shell、.topbar、.navbar、.tabbar、.card-section 等类名在 layout 与页面之间保持一致

---

## 执行选项

计划已完成并保存到 `docs/superpowers/plans/2026-09-12-lxpf-style-admin-prototype.md`。两种执行方式：

1. **Subagent-Driven（推荐）** - 我为每个任务派遣新的子代理，任务之间审阅，快速迭代
2. **Inline Execution** - 在当前会话中用 executing-plans 批量执行，检查点审阅

请告知你倾向哪种执行方式。
