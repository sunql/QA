# LXPF WEB_MES Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 12 PCCP/standard-pipe MES prototype pages to `~/Projects/lxpf-style-admin-prototype/pc-mes/`, reusing the existing design system (tokens.css, components.css, components-extra.css, layout-pc.css) and minimal per-page inline SVG sprite from `assets/icons.svg`.

**Architecture:** Independent pages in `pc-mes/` directory; each page links CSS from `../assets/` and inlines only the symbols it actually uses (2-12 per page) — same pattern that the LXPF plan just shipped. No JS, no build step. Style consistency is enforced by reusing the LXPF CSS layer (not redefining tokens).

**Tech Stack:** HTML5 + CSS3 (static), inline SVG sprite, zero JS, zero npm.

## Global Constraints

1. **Per-file ≤ 600 lines** (hard cap, project-wide).
2. **Do NOT modify** any existing file outside `pc-mes/` and `.superpowers/sdd/2026-09-13-lxpf-web-mes-prototype/` (no edits to `pc/`, `h5/`, `assets/`, `README.md`, `progress.md`, etc.). Existing LXPF plan is shipped — touch nothing.
3. **Reuse CSS via relative links**: each `pc-mes/*.html` uses `href="../assets/tokens.css"`, `href="../assets/base.css"`, `href="../assets/components.css"`, `href="../assets/components-extra.css"`, `href="../assets/layout-pc.css"`. Do NOT link `assets/icons.svg` (broken — see prior final-review). Do NOT inline the full sprite.
4. **Per-page minimal inline sprite**: extract only the `<symbol id="i-xxx">` blocks that page actually references; wrap in `<svg style="display:none" aria-hidden="true"><defs>...</defs></svg>` immediately before `</body>`. Verifiable: `comm -23 <(grep -oE '<use href="#i-[a-z-]+"' "$f" | sort -u) <(grep -oE 'id="i-[a-z-]+"' "$f" | sort -u)` must be empty.
5. **Class-name SSOT**: `app-shell`, `topbar`, `topbar-logo`, `topbar-actions`, `sidebar`, `sidebar-nav-item`, `breadcrumb`, `page-tabs`, `main-content`, `card-section`, `btn`, `btn-primary`/`.btn-default`/`.btn-dashed`/`.btn-danger`, `icon`, `input`, `tag`, `badge`, `modal`, `drawer`. Do not invent new classes.
6. **Color tokens** (existing in `tokens.css`): `--color-primary`, `--color-success`, `--color-danger`, `--color-warning`, `--color-text`, `--color-text-secondary`, `--bg-container`, `--bg-hover`, `--divider-color`. No new color tokens.
7. **HTML/CSS-only**: no JS, no `<script>`, no external resources beyond CSS + inline SVG.
8. **Domain vocabulary**: PCCP / 标准管 / 钢筋混凝土管 / 承口 / 插口 / 防腐 / 水压试验 / 批次 / 工序 / 班次 / 报工 / 派工. Tables use Chinese column headers.
9. **Independent root menu entry**: add a single 13th index file `pc-mes/index.html` (≤ 200 lines) linking to all 12 pages. Do NOT modify `index.html` (root). Each card is a `<a>` with class `card`.
10. **Each task self-verifies**: HTML parse via `python3 -c "from html.parser import HTMLParser; HTMLParser().feed(open('file').read())"`; per-page missing-icon audit; line count ≤ 600.

---

## File Structure

```
pc-mes/
├── index.html              # 12-card overview
├── login.html              # 1. 登录
├── workbench.html          # 2. 工作台
├── order-list.html         # 3. 工单列表
├── order-detail.html       # 4. 派工单详情
├── work-report.html        # 5. 报工记录
├── quality-trace.html      # 6. 质量追溯
├── dashboard.html          # 7. 生产看板
├── material.html           # 8. 物料管理
├── equipment.html          # 9. 设备管理
├── routing.html            # 10. 工艺路线
├── exception.html          # 11. 异常处理
└── finish-warehousing.html # 12. 成品入库
```

---

## Task Right-Sizing

13 tasks total: Task 0 (setup: index.html + sprite inventory) + Task 1-12 (one per page). Each task = one HTML file + one commit. Each task's implementer reads THIS plan, builds the file, runs the 3 self-checks, commits, writes report.

---

### Task 0: pc-mes/index.html + sprite inventory

**Files:**
- Create: `pc-mes/index.html`
- Read (no modify): `assets/icons.svg` (43 symbols)

**Steps:**

- [ ] **Step 1: Build icon inventory**

```bash
cd ~/Projects/lxpf-style-admin-prototype
grep -oE '<symbol id="(i-[a-z-]+)"' assets/icons.svg | sed 's/<symbol id="//;s/"//' | sort -u > /tmp/lxpf-icons.txt
wc -l /tmp/lxpf-icons.txt  # must be 43
```

- [ ] **Step 2: Write pc-mes/index.html (≤ 200 lines)**

Top navigation card grid of 12 pages. Each card: title (Chinese), short description (one line), and an `<svg>` icon using a symbol that's likely to appear in the page it links to.

Structure:
```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>LXPF WEB_MES — 功能原型</title>
  <link rel="stylesheet" href="../assets/base.css">
  <link rel="stylesheet" href="../assets/tokens.css">
  <link rel="stylesheet" href="../assets/components.css">
  <link rel="stylesheet" href="../assets/components-extra.css">
  <style>
    body { padding: 32px; max-width: 1200px; margin: 0 auto; }
    h1 { font-size: var(--font-size-xl); margin-bottom: 8px; }
    .subtitle { color: var(--color-text-secondary); margin-bottom: 32px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
    .card { display: block; padding: 20px; background: var(--bg-container); border: 1px solid var(--divider-color); border-radius: var(--radius-md); transition: border-color .2s; }
    .card:hover { border-color: var(--color-primary); }
    .card-title { font-size: var(--font-size-md); font-weight: var(--font-weight-medium); margin: 12px 0 6px; }
    .card-desc { color: var(--color-text-secondary); font-size: var(--font-size-sm); }
  </style>
</head>
<body>
  <h1>LXPF WEB_MES · 功能原型</h1>
  <p class="subtitle">PCCP / 标准管 MES · 12 个核心页面</p>
  <div class="grid">
    <a class="card" href="login.html">
      <svg width="32" height="32"><use href="#i-user"/></svg>
      <div class="card-title">登录</div>
      <div class="card-desc">工号 + 班次选择</div>
    </a>
    <!-- 11 more cards -->
  </div>
  <!-- inline sprite: cover all 12 pages' expected icons. Easiest: include ALL 43 from icons.svg. Since this is the entry page, full sprite is acceptable here. -->
  <svg style="display:none" aria-hidden="true">
    <defs>
      <!-- all 43 <symbol> blocks from assets/icons.svg -->
    </defs>
  </svg>
</body>
</html>
```

- [ ] **Step 3: Verify**

```bash
cd ~/Projects/lxpf-style-admin-prototype
wc -l pc-mes/index.html  # ≤ 200
python3 -c "from html.parser import HTMLParser; HTMLParser().feed(open('pc-mes/index.html').read())"  # no errors
grep -ohE 'href="[^"]+"' pc-mes/index.html | grep -oE 'i-[a-z-]+' | sort -u > /tmp/index-used.txt
grep -ohE 'id="i-[a-z-]+"' pc-mes/index.html | sort -u > /tmp/index-defined.txt
comm -23 /tmp/index-used.txt /tmp/index-defined.txt  # must be empty
```

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/lxpf-style-admin-prototype
git add pc-mes/index.html
git commit -m "feat(pc-mes): index overview page with 12 cards + full sprite"
```

**Report contract:** Status / Commit / Self-check 3 outputs / Concerns.

---

### Task 1: pc-mes/login.html

**Files:**
- Create: `pc-mes/login.html`

**Content sketch:**
- Brand area on left (车间实景占位, gray gradient background, "LXPF WEB_MES" logo, slogan "管通天下，智能制造")
- Login form on right (card-section): 工号 (Input with prefix icon `i-user`), 密码 (Input with `i-lock` + show/hide `i-eye`), 班次 select (早/中/晚三班), 记住我 checkbox, 登录 button (`btn-primary` 满宽), 第三方登录 row (微信/钉钉/企业微信 placeholder text)
- Footer: 版本号 v1.0.0, 备案号 placeholder
- Reuse: `../assets/{base,tokens,components,components-extra}.css`
- Inline sprite: minimal — only `i-user`, `i-lock`, `i-eye`

**Self-checks** (3 commands): HTML parse, per-page missing-icons, line count ≤ 600.

**Commit:** `feat(pc-mes): login page with shift selector`

---

### Task 2: pc-mes/workbench.html

**Files:**
- Create: `pc-mes/workbench.html`

**Content sketch:**
- Layout: standard `app-shell` with topbar + sidebar + breadcrumb + main-content
- Topbar: logo "LXPF WEB_MES", 全局搜索 (Input prefix `i-search`), user dropdown (`i-user` + "张工 · 钢筋班")
- Sidebar (5 items): 工作台 (active) / 工单管理 / 物料管理 / 设备管理 / 看板 (`i-home`/`i-file`/`i-package`/`i-setting`/`i-chart`)
- Breadcrumb: 首页 / 工作台
- Main content (4 card-sections in a grid):
  1. 今日生产概览 — 4 KPI cards (今日产量 / 在制工单 / 设备稼动率 / 异常数) with `i-trending-up` `i-file-text` `i-chart` `i-warning`
  2. 待办工单 — table with 5 rows (工单号/规格/计划量/实际量/操作), 状态tag颜色对应 `--color-warning`(待派工) `--color-primary`(进行中) `--color-success`(已完成)
  3. 异常提醒 — list of 3 items, 红色 icon `i-warning` + 异常描述 + 时间 + 处理按钮
  4. 快捷入口 — 6 small cards (派工 / 报工 / 领料 / 检验 / 入库 / 查询) with icons
- Inline sprite: extract matching symbols from `assets/icons.svg`

**Self-checks:** same 3 commands.

**Commit:** `feat(pc-mes): workbench with KPIs, todos, exceptions, quick actions`

---

### Task 3: pc-mes/order-list.html

**Files:**
- Create: `pc-mes/order-list.html`

**Content sketch:**
- Same shell (topbar + sidebar with 工单管理 active)
- Breadcrumb: 首页 / 工单管理 / 工单列表
- Page header: title "工单列表" + "新建工单" button (`btn-primary` with `i-plus`)
- Filter row (Input fields): 工单号 / 规格型号 (select DN800/DN1000/DN1200/DN1400/DN1600) / 状态 (select 待派工/进行中/已完成/已取消) / 计划日期 (date range) / 查询 button + 重置 button (`btn-default`)
- Batch actions toolbar: 批量派工 button (visible when ≥1 selected) + 已选 N 项 + total count
- Table (8 rows): checkbox / 工单号 / 规格 / 计划量(根) / 实际量 / 进度条 / 状态 tag / 派工人 / 计划完成日期 / 操作 (查看 派工 取消) — using `i-search` `i-edit` `i-delete` icons in action column
- Pagination: 共 156 条 / 10条/页 / 1-10 页码 / 跳页
- Inline sprite: minimal matching set

**Commit:** `feat(pc-mes): order list with filters, batch dispatch, pagination`

---

### Task 4: pc-mes/order-detail.html

**Files:**
- Create: `pc-mes/order-detail.html`

**Content sketch:**
- Breadcrumb: 首页 / 工单管理 / 工单列表 / WO2024-0815
- Header card: 工单基本信息 (工单号 / 批次号 / 产品规格 DN1200 PCCP / 计划量 200 根 / 计划开始 / 计划完成 / 客户单位 / 合同号)
- Tabs (3 tabs): 工序分解 / 派工记录 / 工艺参数 — use existing tab pattern if any, otherwise simple tab buttons
- 工序分解 tab content: table of 6 processes (工序名称 / 责任人 / 计划工时 / 实际工时 / 完成度 / 状态) — 示例工序：钢丝骨架制作 / 混凝土浇筑 / 离心成型 / 蒸汽养护 / 水压试验 / 防腐处理
- 派工记录 tab content: timeline list of 4 dispatch events (派工人 / 接收人 / 工序 / 时间)
- Right side: 操作面板 (重新派工 button, 取消工单 button, 导出PDF)
- Inline sprite: minimal

**Commit:** `feat(pc-mes): order detail with processes, dispatch log, operations`

---

### Task 5: pc-mes/work-report.html

**Files:**
- Create: `pc-mes/work-report.html`

**Content sketch:**
- Breadcrumb: 首页 / 生产执行 / 报工记录
- Two side-by-side panels: 个人报工 / 批量报工 (tabs)
- 个人报工 form: 工单号 (Input + 工单选择 dropdown trigger `i-search`) / 工序 (select) / 本次完成量 (Input number) / 工时 (Input decimal) / 合格率 (%) / 备注 (textarea) / 提交 button
- 批量报工 table inline: 10 rows (checkbox / 工单 / 工序 / 完成量 input / 工时 input / 合格率 input) + 底部"全部提交" button
- 历史记录 table below: 8 rows (报工时间 / 工单号 / 工序 / 完成量 / 工时 / 合格率 / 状态 / 操作 撤销) — tag colors: 已提交/已审核/已驳回
- Right sidebar: 本日统计 (4 KPI mini: 已报工 N / 总产量 根 / 平均合格率 % / 工时 h)
- Inline sprite: minimal

**Commit:** `feat(pc-mes): work report (single + batch) with history and daily stats`

---

### Task 6: pc-mes/quality-trace.html

**Files:**
- Create: `pc-mes/quality-trace.html`

**Content sketch:**
- Breadcrumb: 首页 / 质量管理 / 质量追溯
- Search header: 批次号 Input + 扫描 barcode icon button `i-search` + 追溯 button
- Trace chain visualization (horizontal flow with 5 stages): 原料进场 → 钢丝骨架 → 混凝土浇筑 → 离心成型 → 水压试验 → 成品入库 — each as a card with checkmark icon `i-check` and details expandable
- Below the flow: 4 detail sections in tabs (原料检验记录 / 过程检验 / 成品检验 / 不合格处理)
- 原料检验记录 table: 5 rows (原料名称 / 批次 / 供应商 / 检验项目 / 结果 / 检验员)
- 不合格品处理流程 (collapsible): 上报 → 隔离 → 评审 → 处置(返工/让步/报废) → 关闭
- Right sidebar: 批次摘要 (产品规格 / 生产日期 / 客户 / 总产量 / 合格量 / 合格率%)
- Inline sprite: minimal

**Commit:** `feat(pc-mes): quality traceability with batch search and detail tabs`

---

### Task 7: pc-mes/dashboard.html

**Files:**
- Create: `pc-mes/dashboard.html`

**Content sketch:**
- Breadcrumb: 首页 / 数据看板 / 生产看板
- Top: 4 KPI cards (本日产量 187/200 根 / 合格率 98.4% / 设备稼动率 87.2% / 在制品 23 根) — with trend arrows `i-trending-up` or `i-trending-down`
- 4 chart placeholder cards (2x2 grid), each labeled with title and chart-type icon:
  1. 产量趋势 (近30天) — line chart placeholder, `i-chart`
  2. 规格分布 — pie/donut placeholder, `i-pie-chart`
  3. 设备稼动 TOP5 — bar chart placeholder, `i-bar-chart`
  4. 异常分布 — stacked bar placeholder, `i-warning`
- Below: 实时生产 table — 6 lines of equipment (设备名称 / 当前工序 / 操作员 / 状态 tag 运行/待机/故障 / 实时产量 / 今日产量)
- Date range selector top-right: 近7天 / 近30天 / 近90天 / 自定义
- Inline sprite: minimal

**Commit:** `feat(pc-mes): production dashboard with KPIs and 4 chart placeholders`

---

### Task 8: pc-mes/material.html

**Files:**
- Create: `pc-mes/material.html`

**Content sketch:**
- Breadcrumb: 首页 / 物料管理 / 原料库存
- Top tabs: 原料库存 / 收发存台账 / 批次台账 / 超期预警 — 4 tabs
- 原料库存 tab content (default active):
  - Filter row: 物料类别 (水泥/钢筋/钢丝/砂石/外加剂/防腐材料) / 仓库 (select) / 库存预警 checkbox / 查询
  - Table 10 rows: 物料编号 / 名称 / 规格 / 单位 / 当前库存 / 安全库存 / 状态 tag 充足/预警/缺货 / 仓库 / 最近入库 / 操作
  - 状态 tag colors: 充足 `--color-success` / 预警 `--color-warning` / 缺货 `--color-danger`
- 收发存台账 tab: table with 月初/本月入库/本月出库/月末 4 columns × 5 物料 rows
- 批次台账 tab: table with 批次号 / 物料 / 供应商 / 入库日期 / 检验状态 / 使用情况
- 超期预警 tab: warning-styled table with red icons
- Inline sprite: minimal

**Commit:** `feat(pc-mes): material inventory with 4 tabs (stock, ledger, batch, alerts)`

---

### Task 9: pc-mes/equipment.html

**Files:**
- Create: `pc-mes/equipment.html`

**Content sketch:**
- Breadcrumb: 首页 / 设备管理
- Top: 5 KPI mini cards (设备总数 / 运行中 / 待机 / 故障 / 维保中) with `i-cog` icons
- Main table (设备台账): 8 rows (设备编号 / 名称 / 型号 / 安装位置 / 当前状态 tag / 责任人 / 上次维保 / 下次维保 / 操作 查看/维保记录/故障报修) — 状态 colors: 运行 success / 待机 default / 故障 danger / 维保中 warning
- Right panel: 设备详情侧栏 (when row selected, shows: 设备照片 placeholder, 基本信息, 维保记录 timeline 4 items, 故障历史 list 3 items)
- Bottom: 维保计划 table (5 rows: 设备 / 维保类型 月度/季度/年度 / 计划日期 / 责任人 / 状态)
- Inline sprite: minimal

**Commit:** `feat(pc-mes): equipment ledger with status, maintenance plan, fault history`

---

### Task 10: pc-mes/routing.html

**Files:**
- Create: `pc-mes/routing.html`

**Content sketch:**
- Breadcrumb: 首页 / 工艺管理 / 工艺路线
- Left: 产品选择 tree (产品分类: PCCP / 标准管 / 钢套筒 / 3-4 leaf products)
- Right: selected product's工艺路线 visualization as horizontal step flow (6 step boxes connected by arrows):
  1. 钢丝骨架制作 (`i-cog`)
  2. 混凝土浇筑 (`i-package`)
  3. 离心成型 (`i-refresh`)
  4. 蒸汽养护 (`i-trending-up`)
  5. 水压试验 (`i-eye`)
  6. 防腐处理 (`i-edit`)
- Each step box: 工序名 + 标准工时 + 责任人 + checkmark if default
- Below flow: 工序参数 table — 6 rows (工序 / 参数名称 / 标准值 / 实际范围 / 偏差阈值 / 检验方法)
- Right sidebar: 替代工艺 dropdown + 启用按钮 + 工艺版本号 v3.2
- Inline sprite: minimal

**Commit:** `feat(pc-mes): routing with step flow visualization and parameter table`

---

### Task 11: pc-mes/exception.html

**Files:**
- Create: `pc-mes/exception.html`

**Content sketch:**
- Breadcrumb: 首页 / 异常管理
- Top: filter row (异常类型 / 严重程度 / 状态 / 时间范围) + "上报异常" button
- Stats row: 4 KPI cards (未处理 N red / 处理中 N yellow / 已闭环 N green / 今日新增 N)
- Main: split layout
  - Left (60%): 异常列表 table — 8 rows (异常编号 / 类型 设备故障/质量异常/物料异常/工艺异常 / 严重程度 tag 高/中/低 / 关联工单 / 上报人 / 上报时间 / 状态 / 操作)
  - Right (40%): 选中异常的处置面板 — 异常详情 / 处置流程 timeline (4 steps: 上报 → 接收 → 处置 → 闭环) / 处置记录 textarea + 提交 button
- 异常编号 prefix EX + yyyymmdd + 4位序号
- 严重程度 tag colors: 高 danger / 中 warning / 低 default
- Inline sprite: minimal

**Commit:** `feat(pc-mes): exception handling with list, status stats, and triage panel`

---

### Task 12: pc-mes/finish-warehousing.html

**Files:**
- Create: `pc-mes/finish-warehousing.html`

**Content sketch:**
- Breadcrumb: 首页 / 库存管理 / 成品入库
- Top tabs: 入库单 / 库位管理 / 出库衔接 / 入库台账
- 入库单 tab (default):
  - New入库单 button (`btn-primary` with `i-plus`)
  - Filter: 批次号 / 客户 / 库位 / 检验状态 / 日期
  - Table 8 rows: 入库单号 / 批次号 / 产品规格 / 数量(根) / 库位 / 检验状态 tag / 入库日期 / 操作员 / 操作 (查看 / 打印 / 取消)
  - 入库单号格式: RK + yyyymmdd + 4位序号
- 库位管理 tab: visual warehouse grid (10库位 cards, 状态颜色) + 库位表
- 出库衔接 tab: 待出库列表 + 已出库记录
- 入库台账 tab: 月度汇总 table
- Inline sprite: minimal

**Commit:** `feat(pc-mes): finish warehousing with inbound orders, location grid, and outbound link`

---

## Per-Task Workflow

For each task 0-12:

1. **Dispatch implementer subagent** (sonnet) with:
   - This plan file path
   - Task N's "Content sketch" + "Commit" sections (verbatim)
   - Global Constraints (verbatim)
   - Report path: `.superpowers/sdd/2026-09-13-lxpf-web-mes-prototype/task-N-report.md`
   - Self-check commands (3 commands: HTML parse, per-page missing-icons, line count ≤ 600)
2. **After implementer reports DONE**, dispatch scoped re-review (sonnet) with:
   - The file path of the new file
   - 3 verification commands
3. **Adjudicate**: if APPROVED, mark complete in progress.md and move to next task. If FIX_REQUIRED, enter fix loop (max 5 rounds per task).
4. After Task 12: dispatch final code reviewer (opus) on `pc-mes/` only — verify all 12 pages + index consistency.

---

## Done Criteria (per task)

- File exists and parses via Python HTMLParser
- Per-page missing-icons audit returns empty (`comm -23` empty)
- File ≤ 600 lines
- Class names match Global Constraint §5
- No new CSS files added; CSS linked via `../assets/` relative paths only
- Commit message matches the "Commit" line for the task