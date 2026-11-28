# 催货单：创建后可见列表 + Excel/Word 下载 —— 设计

## 背景

采购 Copilot 的「到货跟踪」结果卡提供「创建催货单」动作。创建成功后**当前只有一句 toast**
（`message.success`：张数 + 供应商数），**没有任何产物输出**——用户在对话里看不到刚创建的
催货单，也没有下载入口。

催货单本身的数据是齐的（`procurement_chase_draft` + `procurement_chase_line` +
`procurement_chase_decision`），后端 7 个端点里**也没有导出端点**。

## 目标

1. 创建催货单后，**在对话流里能看见催货单列表**（不是只有一句 toast）。
2. 催货单支持**下载**：**Excel + Word 两种格式**，单张下载，任何状态可下载。

## 非目标

- 不做批量导出（只做单张）。
- 不做催货函的模板配置化（版式硬编码在导出服务里，后续再抽）。
- 不改催货单的状态机与审批流程。
- 不做「发送」通道（`SENDING`/`SENT` 状态维持现状，本设计只管产出物）。

## 决策记录（用户确认）

| 决策点 | 结论 |
|---|---|
| 列表呈现位置 | **对话流内嵌列表卡**（不是跳转链接） |
| 下载粒度 | **单张下载** |
| 下载时机 | **任何状态可下载** |
| 供应商名称来源 | 与到货跟踪同款：**MDM 目录查询**（`loadMdmDirectory`） |
| 下载权限 | **统一 requireAdmin**（与其它催货端点一致） |
| 催货函抬头 | **系统配置键维护**（admin 在后台维护法定公司名，导出时读取） |

### 权限取舍（已定：统一 requireAdmin）

催货的其它端点（from-pending / validate / approve / reject / cancel / send）全是
`requireAdmin`，导出内容与它们**同源**（供应商 / 订单号 / 欠交量 / 逾期天数）。
若做成「登录即可」＝普通登录用户拿到 admin 才能看的同一批采购数据 ⇒ **口径统一到
requireAdmin**，不做第二套权限体系。

> 注：用户原话是「下载不控制权限」，经澄清其含义是「不要再单独加一套下载权限体系」，
> 而非「放开给所有人」。

### 抬头来源（已定：系统配置键）

催货函抬头的**公司法定名称**不能取 `BRAND_NAME`（那是产品品牌「泰鸿智脑」，不是法人名），
系统里目前也没有「公司主体名」字段。

**方案**：新增 system_config 键（如 `chase.letter.company_name`），admin 在后台维护；
导出服务读取该键。

- **不写死任何默认公司名** —— 写死一个「看起来像公司名」的默认值比留空更危险：
  用户没配却导出一封抬头错误的催货函，比抬头留空更糟。
- **未配置 ⇒ 整行不渲染**（不是留一行空白）—— 避免「以为填了其实是空」的歧义；
  同时服务端记 warning，**不阻塞导出**。
- system_config 是既有表 ⇒ **无迁移**。

## 架构

```
采购 Copilot 结果卡 → [创建催货单]
   └→ POST /procurement/chase/from-pending  ← 响应已含 drafts[]
        ↓
   对话流追加「催货单列表卡」（前端组件，无新端点）
        │  每行：[Excel] [Word]
        ↓
   GET /procurement/chase/drafts/{chaseDraftId}/export?format=xlsx|docx
        ↓
   导出服务：draft + lines + decisions + supplierName → bytes → 流式下载
```

**关键点**

1. **列表卡不需要新端点**：`POST /from-pending` 的响应 `ChaseFromPendingResultRead`
   已返回 `drafts: list[ChaseDraftRead]`，字段含 `chase_draft_id` / `status` /
   `supplier_code` / `line_count` / `expected_reply_deadline` / `notes` / `created_time`
   —— 足够渲染列表卡。
2. **导出在后端做**：Excel/Word 需要行明细（`ChaseLineRead`）与决策时间线
   （`ChaseDecisionRead`）；沿用 `evaluation_report_export_service` 的范式——
   **导出服务只接收 immutable payload，不接触 DB/ORM/FastAPI**。
3. **两个下载入口共用一个端点**：对话流列表卡 + 催货单页（`/procurement/chase`）列表。

## 后端

### 新增 `backend/app/services/procurement_chase_export_service.py`

| 符号 | 职责 |
|---|---|
| `ChaseExportPayload`（frozen dataclass） | 不可变导出输入：单号 / 状态 / 供应商码 / 供应商名 / 回复期限 / 备注 / 创建人 / 行列表 / 决策时间线 |
| `buildChaseExportPayload(*, draft, lines, decisions, supplierName) -> ChaseExportPayload` | 纯拼装（API 层从 ORM 取数据后调它） |
| `renderChaseExcel(payload) -> bytes` | openpyxl |
| `renderChaseDocx(payload) -> bytes` | python-docx |

**Excel 结构**

- 顶部信息区（合并单元格）：催货单号、供应商（码 + 名）、回复期限、状态、创建人、创建时间
- 明细 Sheet「催货明细」列：订单号 / 行号 / 物料编码 / 物料描述 / 订购量 / 已收量 /
  **欠交量** / 期望到货日 / 逾期天数 / 脏数据标记
- 依赖已在 `backend/pyproject.toml`：`openpyxl>=3.1.5`

**Word 结构**（正式催货函版式）

1. 抬头：**公司法定名称**（读 system_config 键 `chase.letter.company_name`；**未配置则该行
   整行不渲染** + 服务端记 warning，**不拿产品品牌名冒充法人名**）+ 供应商全名 + 催货单号 +
   回复期限
2. 催货说明段：欠交总行数 / 欠交总量 + 回复期限要求
3. 明细表：订单号 / 行号 / 物料描述 / 欠交量 / 期望到货日 / 逾期天数
4. 落款：催货人（`created_by`）+ 日期

中文字体：docx 侧用 `w:eastAsia` 设置（`pdf_fonts.ensureCjkFont()` 是 PDF 专用，不复用）。
依赖已在 `backend/pyproject.toml`：`python-docx>=1.0.0`。

**system_config 键**：既有表 ⇒ 无迁移。读法沿用仓内既有 system_config 读取模式
（如 `config.py` 里 `SCHEMA_MAX_TABLES` 之类配置的读法），键名 `chase.letter.company_name`。

### 新增端点（挂 `backend/app/api/v1/procurement_chase.py` 的现有 router）

```
GET /procurement/chase/drafts/{chaseDraftId}/export?format=xlsx|docx
  200 → 文件流，Content-Disposition: attachment; filename="催货单_{单号}.{ext}"
  404 → 催货单不存在
  400 → format 非法（只允许 xlsx / docx）
  401/403 → 未登录 / 非 admin（**requireAdmin**，与其它催货端点同口径）
```

**供应商名获取**：API 层用
`loadMdmDirectory(session, entityType="SUPPLIER", codes=[draft.supplier_code])`
（与到货跟踪同款，`entity_mapping_names.loadMdmDirectory`）。**查不到时回落
`supplier_code`，不 fail-closed**——催货函仍要能发出。

**文件名单**：含中文时用 RFC 5987 的 `filename*=UTF-8''…` 形式编码（避免中文文件名在
部分浏览器乱码），同时保留 ASCII 回退名。

## 前端

### 新增 `frontend/src/components/copilot/ChaseDraftListCard.tsx`

- 每行：催货单号 / 供应商码 / 行数 / 状态标签 / 回复期限 / `[Excel] [Word]`
- 状态标签颜色按状态机映射（`DRAFT` / `VALIDATING` / `PENDING_APPROVAL` /
  `READY_TO_SEND` / `SENDING` / `SENT` / `CLOSED` / `REJECTED` / `CANCELLED`）
- 空列表（有 `unresolvedSuppliers` / `skippedLines`）→ 显示提示而非空卡

### 改动 `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx`

创建成功分支：现在只有 `message.success` ⇒ 改为**追加一条列表卡消息**
（`ChaseDraftListCard`），toast 保留（保留「部分未解析供应商」的 warning 语义）。

### 改动 `frontend/src/pages/ProcurementChasePage.tsx`

列表每行加 `[Excel] [Word]` 下载按钮，与列表卡共用同一端点。

### 下载实现

`httpClient` 走 **Bearer token**，`<a download>` 无法带 header ⇒ 用 axios
`responseType: "blob"` + `URL.createObjectURL` + 触发 `<a>` 点击 + `revokeObjectURL`
（与仓库其它导出保持一致）。

## 错误处理

| 场景 | 行为 |
|---|---|
| 催货单不存在 | 404 → 前端友好提示「催货单不存在或已删除」 |
| `format` 非法 | 400（前端只传两个固定值，正常不会触发） |
| 导出服务异常 | 500 → 统一错误包络；前端提示「导出失败，请稍后重试」 |
| 催货单无明细行 | **仍可导出**（只有抬头信息，明细表为空） |
| MDM 查不到供应商名 | 回落 `supplier_code`，不阻塞导出 |

## 测试

**后端**
- 导出服务单测：Excel 行数与列名正确、欠交量取值正确、空明细也能出文件；
  Word 含关键文本（供应商名 / 单号 / 欠交量 / 回复期限）
- 端点集成测：200 + `Content-Disposition` 文件名正确、404（不存在）、400（format 非法）、
  **匿名访问 401**、**非 admin 登录 403**（确认 requireAdmin 生效）

**前端**
- 列表卡渲染：收到 `drafts` → 行数/供应商/状态正确显示
- 下载按钮触发：调用导出端点且 format 正确
- 创建成功 → 列表卡出现在对话流

## 影响面与部署

- **无 DDL / 无迁移**（只读导出 + 新端点）
- 后端新增 1 个 service 文件 + 1 个端点；前端新增 1 个组件 + 2 处接入
- 需重建 backend + frontend 镜像；worker 同镜像须一并 `--force-recreate`
  （见 `Harness/skills/thdeploy/SKILL.md` §worker 同步铁律）

### 生产可用性（写清楚，免得生产验收误判）

生产 DMZ 到业务 Oracle `192.168.205.70:1521` **本就隔离**（2026-09-30 已有先例，见
`Harness/changes/2026-11-27-release-1127-chase01/summary.md`）。

- 催货单的**真实业务数据**（待到货回查、欠交量）在生产跑不通 ⇒ 功能正确性只能在本地 dev 栈验；
- **导出功能本身在生产可用**：它只读**已落库的催货单**（`procurement_chase_draft` /
  `_line` / `_decision`），**不直连业务库** ⇒ 前提是催货单能被创建出来。

⇒ 发版 runbook 的验收探针要写明这一点，别让生产验收把「业务库不可达」误判成导出功能坏了。

## 关联

- 现有催货单能力：`Harness/changes/feat/delivery-chase-closed-loop/`
- 到货跟踪（MDM 目录查询范式）：`backend/app/services/delivery_tracking_query.py`
- 导出服务范式：`backend/app/services/evaluation_report_export_service.py`
- 发版纪律：`Harness/skills/thdeploy/SKILL.md`
