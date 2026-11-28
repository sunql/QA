# 变更：2026-11-27-chase-export（催货单 Excel/Word 导出 + 菜单入口 + 创建后跳转链接）

- **日期**：2026-11-27（功能）/ 2026-11-28（终审修复）
- **作者**：Claude
- **Phase**：采购 · 催货单闭环（feat/delivery-chase-closed-loop 之上）
- **状态**：**代码完成，待发版**（无 DDL / 无迁移；菜单行与抬头键随发版自动播种）
- **关联变更**：
  [2026-11-27-release-1127-chase01](../2026-11-27-release-1127-chase01/summary.md)（催货单闭环首发）、
  [feat/delivery-chase-closed-loop](../feat/delivery-chase-closed-loop/summary.md)
- **迁移版本**：**无**（只读导出 + 新端点 + 菜单 seed 一项；`system_config` 是既有表）
- **设计稿**：[docs/superpowers/specs/2026-11-27-chase-export-design.md](../../../docs/superpowers/specs/2026-11-27-chase-export-design.md)

---

## 1. 功能概述

**做什么**：让催货单（采购 Copilot 到货跟踪 → 按供应商创建）**看得见、拿得走**。

| 能力 | 落地 |
|---|---|
| 创建后可见 | 采购 Copilot 结果卡创建成功后，提示里带「查看催货单」跳转链接 → `/procurement/chase` |
| 菜单入口 | 新增菜单项 `item.procurementChase`（sort_order 2750）→ `/procurement/chase`（此前路由已注册但导航里找不到） |
| 下载 | 催货单页每行 `[Excel] [Word]` ⇒ `GET /procurement/chase/drafts/{id}/export?format=xlsx\|docx` |

**产物结构**

- **Excel**（openpyxl）两个 Sheet：
  - 「催货单信息」：催货单号 / 供应商 / 回复期限 / 状态 / 创建人 / 创建时间 / 备注；
  - 「催货明细」：**表头固定第 1 行**、数据自第 2 行起（表头位置成为**可被程序化读取的量化契约**，下游解析不必猜表头行号）。
- **Word**（python-docx）正式催货函版式：抬头 → 催货说明 → 明细表 → 落款。

**架构**：沿用 `evaluation_report_export_service` 范式 —— **导出服务只接收 immutable payload，不接触 DB/ORM/FastAPI**；API 层从 ORM 拼好 payload 再调用。

## 2. 决策要点

| 决策点 | 结论 | 理由 |
|---|---|---|
| 列表呈现位置 | **跳转链接到催货单页**（`/procurement/chase`） | 该页已有完整列表/状态色/详情/决策 ⇒ 不重复造列表卡 |
| 下载粒度 / 时机 | **单张**；**任何状态可下载** | 催货函是过程物料，催的时候就要能发出 |
| 供应商名来源 | MDM 目录查询（`loadMdmDirectory`），查不到**回落编码**、**不阻塞导出** | 催货函仍要能发出；MDM 挂了不该卡住对外动作 |
| 下载权限 | **统一 `requireAdmin`** | 与其它催货端点同源同口径，不做第二套权限体系 |
| 抬头公司名 | **系统配置键** `chase.letter.company_name`；**未配置 ⇒ 整行不渲染** | 写死一个「看起来像公司名」的值比留空更危险（抬头错误的催货函更糟） |
| 抬头种子范式 | `CHASE_LETTER_CONFIG_SEEDS`（**不是** `CHART_CONFIG_SEEDS`） | 后者冲突路径是 DO UPDATE set value ⇒ 会**覆盖 admin 调优**；抬头用内容型通道（value 一个字都不动） |
| 对外产物范围 | 含 供应商/单号/期限/明细/落款/`notes`；**不含** `decisions` | 审批轨迹是内部治理留痕，外发会泄露审批过程 |

### 2.1 对外材料的三道安全/正确性护栏（2026-11-28 终审）

这批文件是**转发给供应商**的对外材料 ⇒ 对外正确性与安全是硬要求。

| 护栏 | 缺陷 | 修法 |
|---|---|---|
| **Excel 公式注入** | 外部数据（物料描述/供应商名/备注/单号）以 `=` 开头 ⇒ openpyxl **写入时**置 `data_type='f'` ⇒ 变成**活公式**（`=HYPERLINK(...)` 外链、`=cmd\|...`）⇒ 供应商在 Excel 里打开即执行 | 写入**之后**强制 `cell.data_type = "s"`（内容原样，Excel 按文本存储）。⚠️ 判定发生在**写入时** ⇒ 必须 `cell.value = ...` **之后**再覆盖；**只处理字符串**（数字/日期/布尔不动） |
| **对外时间口径** | `expected_reply_deadline` 是 timestamptz（asyncpg 返回 UTC aware）⇒ 直接渲染 UTC 墙钟 ⇒ 对外承诺期限比页面口径（`toLocaleString()`）**早 8 小时**，且函上**无时区说明** ⇒ 供应商无从判断（落款日期同源，可能差一天） | 一律按**业务时区（固定 +08:00）**渲染并**标注时区**（`（北京时间 UTC+8）`）。⚠️ **不用 `ZoneInfo("Asia/Shanghai")`** —— 容器（python:3.12-slim）可能没装 tzdata ⇒ `ZoneInfoNotFoundError`；中国无夏令时 ⇒ 固定 `timezone(timedelta(hours=8))` 完全正确 |
| **合计失实陈述** | 全部行 `open_qty` 为 NULL ⇒ 函上白纸黑字写「合计欠交 0」⇒ **等于向供应商断言没有欠交**（明细单元格层面已正确区分「未知」与「0」，**合计层没跟上**） | 三形态：全部已知「合计欠交 X。」／部分未知「合计欠交 X（其中 M 行数量待确认，未计入）。」／**全部未知「欠交数量待确认（全部 N 行数量未知，未计入合计）。」**（绝不能写「合计欠交 0」） |

### 2.2 其它口径统一

- **供应商抬头**：Excel 与 Word **同一口径** —— 有名字 ⇒ `名字（编码）`；无名字 ⇒ **只显示编码**（此前 Excel 是 `F109（F109）`、Word 是 `供应商：F109`，两产物不一致且前者冗余）。
- **错误包络**：400 ⇒ `{"detail": "..."}`；403（`PermissionDenied`）⇒ `{"detail": {"code","message"}}`；404 ⇒ `{"success": false, "error": "..."}` ⇒ **前端三种形状都要能读出文案**。

## 3. 数据模型变更

**无**。导出只读既有 `procurement_chase_draft` / `_line` / `_decision`；抬头配置键落在既有表 `system_config`（无 DDL、无迁移）。

## 4. 接口契约变更

新增端点（挂既有 `backend/app/api/v1/procurement_chase.py` 的 router）：

```
GET /procurement/chase/drafts/{chaseDraftId}/export?format=xlsx|docx
  200 → 文件流，Content-Disposition: attachment; filename="催货单_{单号8位}.{ext}"
        （RFC 5987：ASCII 回退名 + filename*=UTF-8''…）
  400 → format 非法（HTTPException ⇒ {"detail": "..."}）
  401 → 无效/过期 token
  403 → 非 admin（requireAdmin ⇒ {"detail": {"code","message"}}）
  404 → 催货单不存在（**含非法 UUID 单号 ⇒ 404 而非 500**；两条 404 走同一异常类型 ⇒ 响应体形状一致）
```

- 文件含采购敏感数据 ⇒ `Cache-Control: no-store`。
- 权限：与其它催货端点同口径（`requireAdmin`），**不做第二套权限体系**。

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/app/services/procurement_chase_export_service.py` | 纯渲染：`buildChaseExportPayload` / `renderChaseExcel` / `renderChaseDocx`；`_writeExcelCell`（公式注入护栏）、`_toBusinessTz`/`_formatDeadline`/`_formatDate`（时区护栏）、`_totalSummary`（合计护栏） |
| `backend/app/api/v1/procurement_chase.py` | 端点 + 抬头读取（`_companyNameOf`，**savepoint 隔离** ⇒ 读配置失败不打 aborted 事务）+ MDM 供应商名（`_supplierNameOf`） |
| `backend/scripts/seed_menu_config.py` | 菜单项 `item.procurementChase`（sort_order 2750） |
| `backend/scripts/seed_system_config.py` | `CHASE_LETTER_CONFIG_SEEDS`（抬头键，**内容型**通道） |
| `backend/app/main.py` | 启动链：菜单 seed + 抬头种子 ⇒ **随发版自动播种** |
| `frontend/src/api/procurementChase.ts` | `downloadChaseDraft`（blob 下载 + 三种错误包络兼容 + RFC5987 文件名） |
| `frontend/src/pages/ProcurementChasePage.tsx` | 列表每行 `[Excel] [Word]` |
| `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx` | 创建成功 ⇒ 「查看催货单」跳转链接 |

**依赖**：openpyxl ≥ 3.1.5、python-docx ≥ 1.0.0（均已在 `backend/pyproject.toml`）；**未新增依赖**。

## 6. 测试

| 文件 | 覆盖 |
|---|---|
| `backend/app/tests/unit/test_procurement_chase_export_service.py` | payload 拼装 / Excel 结构 / Word 关键文本 / 抬头未配置不渲染 / 决策不外泄 / 数量 0 与未知的区分；**终审新增**：Excel 公式注入（明细列 + 信息 Sheet，数值/布尔 type 不受影响）、业务时区渲染（期限 + 落款）、合计三形态（全部未知**不含**「合计欠交 0」）、供应商抬头两产物一致 |
| `backend/app/tests/integration/test_procurement_chase_export_api.py` | 200 + 附件 / **合法 OOXML（zip 魔数 `PK` + `xl/workbook.xml`）** / 404（不存在 + 非法单号 + 两条 404 形状一致）/ 400 / 401 / 403 / 公司名未配置仍可导出 / MDM 命中与回落 / 配置读失败不被阻塞（savepoint）/ 启动链播种守卫 |
| `frontend/src/tests/ProcurementChaseExport.test.tsx` | 请求形状 / 三种错误包络 / 文件名（RFC5987 + 本地兜底）；菜单 seed ↔ i18n ↔ 路由三方对齐；创建后跳转链接；**终审新增**：催货单页点某行 Excel ⇒ 断言用的是**那一行**的 draftId |
| `frontend/src/tests/chasePageDirtyMarker.test.tsx` | 欠交量异常行红色标识 |

**结果**：后端 37 passed（unit 22 + integration 15）；前端 12 passed；`ruff` 全绿；`tsc --noEmit` 全绿。

## 7. 安全审查

- **触发 `security-reviewer`**（对外材料 + 外部数据写入文件）。
- **CRITICAL（已修）**：Excel 公式注入（外部数据 → 活公式，供应商打开即执行）⇒ 写入后强制 `data_type='s'`。
- **HIGH（已修）**：对外时间无时区标注且用 UTC 墙钟 ⇒ 对外承诺期限比系统口径早 8 小时 ⇒ 业务时区 + 时区标注。
- **HIGH（已修）**：数量未知时「合计欠交 0」⇒ 对供应商的失实陈述 ⇒ 合计层跟上明细层的「未知 vs 0」区分。
- **MEDIUM（已修）**：供应商抬头两产物口径不一致 / `F109（F109）` 冗余 ⇒ 统一为「有名字 `名字（编码）`、无名字只显示编码」。
- **LOW（已修）**：前端注释把 403 描述成统一包络（实为 `{"detail":{"code","message"}}`）⇒ 只改注释，读取逻辑本就正确。
- **NOTE（不做）**：Word 催货函版式美化（标题/居中/字号）—— 属观感优化，本批不做。

## 8. 上线必做

**无需任何手工动作**（2026-11-28 终审修订）：

| 项 | 口径 |
|---|---|
| 菜单行 `item.procurementChase` | **随发版自动播种**（`seed_menu_config` 在 `main.py` 启动链 ⇒ 新 code 走新增）⇒ ~~跑一次 `python -m scripts.seed_menu_config`~~ **已删**（多余步骤） |
| 抬头键 `chase.letter.company_name` | **随发版自动播种**（`seed_chase_letter_config` 在 `main.py` 启动链，`seed_brand_config` 之后）；value 播种为**未配置**，admin 随后在后台填法定公司名 |
| 迁移 | **无 DDL / 无迁移** |
| 镜像 | 需重建 backend + frontend；worker 同镜像须一并 `--force-recreate`（见 `Harness/skills/thdeploy/SKILL.md` §worker 同步铁律） |

## 9. 验收步骤

**本地（真实 PG + 完整 API 链路）**

```bash
cd backend
PGPW=$(docker exec qa-postgres sh -c 'echo $POSTGRES_PASSWORD')
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:${PGPW}@127.0.0.1:5433/qa_metadata_test_sdd" \
TEST_NEO4J_URI=bolt://localhost:7688 \
uv run pytest app/tests/unit/test_procurement_chase_export_service.py \
                 app/tests/integration/test_procurement_chase_export_api.py -q
uv run ruff check app/services/procurement_chase_export_service.py
```

```bash
cd frontend
npx vitest run src/tests/ProcurementChaseExport.test.tsx
npx tsc --noEmit
```

**人工验收（浏览器 / 端点）**

1. 采购 Copilot → 到货跟踪 → 勾选未到货行 → 「按供应商创建催货单」⇒ 提示里出现「查看催货单」链接 ⇒ 点击跳 `/procurement/chase`；
2. 左侧导航「采购」段出现「催货单」⇒ 点进去看到列表；
3. 某行点 `[Excel]` ⇒ 下载 `催货单_{单号8位}.xlsx` ⇒ 两个 Sheet 齐全，**明细表头在第 1 行**；
4. 同行点 `[Word]` ⇒ 下载 `.docx` ⇒ 抬头 / 催货说明 / 明细表 / 落款齐全，**回复期限带「（北京时间 UTC+8）」**；
5. **安全抽查**：物料描述写成 `=HYPERLINK("http://evil","click")` 的那一行 ⇒ Excel 里该单元格**显示为文本**（不变成可点链接），Excel 公式栏里也不是公式；
6. **失实陈述抽查**：把某行 `open_qty` 置 NULL ⇒ 催货函写「…行数量待确认，未计入…」，**不是**「合计欠交 0」；
7. 403 / 404 的提示文案能正常弹出（前端两种错误包络都兼容）。

**生产**

- ⚠️ 生产 DMZ 到业务 Oracle `192.168.205.70:1521` **本就隔离**（2026-09-30 已有先例）⇒ 催货单的**真实业务数据**在生产跑不通；
- **导出功能本身在生产可用**：它只读**已落库的催货单**，**不直连业务库** ⇒ 前提是催货单能被创建出来；
- ⇒ 发版 runbook 的验收探针要写明这一点，别让生产验收把「业务库不可达」误判成导出功能坏了。

## 10. 已知遗留

| # | 遗留 | 处置 |
|---|---|---|
| 1 | **既有催货端点对非法 UUID 单号仍打成 500**：`GET /drafts/{chaseDraftId}` 等旧端点未做单号规范化（本批只修了新导出端点） | 下一批统一 |
| 2 | **Word 催货函版式朴素**（无标题居中/字号层级） | 观感优化，本批明确不做 |
| 3 | **催货单真实业务数据在生产跑不通**（业务 Oracle 不可达） | 产品/运维决策（开只读网络策略 / Copilot 部署到业务网侧 / 数据同步） |
| 4 | **`notes` 当前无 UI 输入口**（Copilot 结果卡创建时不暴露该字段）⇒ 实际为空 | 将来若开放 UI，**须在输入处明确标注「对供应商可见」** |

## 11. 关联

- 设计稿：[docs/superpowers/specs/2026-11-27-chase-export-design.md](../../../docs/superpowers/specs/2026-11-27-chase-export-design.md)
- 发版：[2026-11-27-release-1127-chase01](../2026-11-27-release-1127-chase01/summary.md)
- 催货单闭环：[feat/delivery-chase-closed-loop](../feat/delivery-chase-closed-loop/summary.md)
- 导出服务范式：`backend/app/services/evaluation_report_export_service.py`
- 到货跟踪（MDM 目录查询范式）：`backend/app/services/delivery_tracking_query.py`
- Rules：[变更记录强制规范](../../rules/变更记录强制规范.md)、[测试规范](../../rules/测试规范.md)
- 发版纪律：[thdeploy 技能](../../skills/thdeploy/SKILL.md)
