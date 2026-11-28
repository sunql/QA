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
| 列表呈现位置 | **跳转链接到催货单页**（`/procurement/chase`）——该页已有完整列表/状态色/详情/决策，**不重复造列表卡** |
| 下载粒度 | **单张下载** |
| 下载时机 | **任何状态可下载** |
| 供应商名称来源 | 与到货跟踪同款：**MDM 目录查询**（`loadMdmDirectory`） |
| 下载权限 | **统一 requireAdmin**（与其它催货端点一致） |
| 催货函抬头 | **系统配置键维护**（admin 在后台维护法定公司名，导出时读取） |

### 入口缺失（2026-11-28 实测）

`menu_config` 采购段有 6 项（Rfq / Bindings / Knowledge / Copilot / Agents /
PendingDelivery），**唯独没有催货单** ⇒ 路由 `/procurement/chase` 虽已注册，
导航里却找不到 ⇒ 「创建完想看列表」无路可循。

⇒ 本批补两件事：
1. **菜单项**：`seed_menu_config.py` 加 `item.procurementChase` → `/procurement/chase`
   （+ 对应 i18n label）。**该 code 是全新的** ⇒ 菜单 seed 的
   `on_conflict_do_nothing` 对新 code 有效 ⇒ 跑一次 `python -m scripts.seed_menu_config`
   即**新增**该行（不像场景 seed 是给已有行补字段、需要定向补丁机制）。
2. **创建后给跳转链接**：到货跟踪结果卡「创建催货单」成功后，toast/提示里带
   「查看催货单」入口 ⇒ 跳 `/procurement/chase`。

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

**方案**：新增 system_config 键 `chase.letter.company_name`，admin 在后台维护。

**落地链路（2026-11-28 实施补齐，重要）**：`PUT /admin/system-config/{key}` 对**不存在的 key**
抛 NotFoundError ⇒ admin **只能改已有行、不能新建** ⇒ 该键必须由 seed **先种出行**，
否则 admin 无法维护。

- 种子范式照 `backend/scripts/seed_system_config.py` 的 **`BRAND_CONFIG_SEEDS`**（**不是**
  `CHART_CONFIG_SEEDS`——后者的冲突路径是 DO UPDATE set value ⇒ 会**覆盖 admin 调优**）：
  - value 播种为 **None（未配置）**、description 说明用途；
  - 冲突路径 `on_conflict_do_update(index_elements=["key"], set_={"description": ...})`
    ⇒ **value 一个字都不动**（admin 改过的抬头不能被发版重置）。
- 若 `seed_system_config` 不在 `main.py` 启动链 ⇒ 列入**上线必做动作**。
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
   成功提示 + 「查看催货单」跳转链接 → /procurement/chase
        │  （菜单新增 item.procurementChase，之前导航里找不到）
        ↓
   催货单页列表每行：[Excel] [Word]
        ↓
   GET /procurement/chase/drafts/{chaseDraftId}/export?format=xlsx|docx
        ↓
   导出服务：draft + lines + decisions + supplierName → bytes → 流式下载
```

**关键点**

1. **不复刻列表卡**：催货单页 `GET /chase/drafts` 已有完整列表（状态色 / 详情 /
   决策时间线）⇒ 只补「菜单入口」+「创建后跳转链接」。
2. **导出在后端做**：Excel/Word 需要行明细（`ChaseLineRead`）与决策时间线
   （`ChaseDecisionRead`）；沿用 `evaluation_report_export_service` 的范式——
   **导出服务只接收 immutable payload，不接触 DB/ORM/FastAPI**。
3. **催货单页是唯一下载入口**。

## 后端

### 新增 `backend/app/services/procurement_chase_export_service.py`

| 符号 | 职责 |
|---|---|
| `ChaseExportPayload`（frozen dataclass） | 不可变导出输入：单号 / 状态 / 供应商码 / 供应商名 / 回复期限 / 备注 / 创建人 / 行列表 / 决策时间线 |
| `buildChaseExportPayload(*, draft, lines, decisions, supplierName) -> ChaseExportPayload` | 纯拼装（API 层从 ORM 取数据后调它） |
| `renderChaseExcel(payload) -> bytes` | openpyxl |
| `renderChaseDocx(payload) -> bytes` | python-docx |

**Excel 结构**

- **信息 Sheet「催货单信息」**：催货单号、供应商（码 + 名）、回复期限、状态、创建人、
  创建时间、备注
- **明细 Sheet「催货明细」**：表头**固定第 1 行**、数据自第 2 行起；列：订单号 / 行号 /
  物料编码 / 物料描述 / 订购量 / 已收量 / **欠交量** / 期望到货日 / 逾期天数 / 脏数据标记
- 依赖已在 `backend/pyproject.toml`：`openpyxl>=3.1.5`

> **为什么两个 Sheet**（2026-11-28 实施裁决）：抬头与明细放同一 Sheet 时，
> 「表头在第 1 行」与「抬头在前」不可兼得 ⇒ 拆两 Sheet，让**表头位置成为可被程序化
> 读取的量化契约**（下游解析不必猜表头行号）。

**Word 结构**（正式催货函版式）

1. 抬头：**公司法定名称**（读 system_config 键 `chase.letter.company_name`；**未配置则该行
   整行不渲染** + 服务端记 warning，**不拿产品品牌名冒充法人名**）+ 供应商全名 + 催货单号 +
   回复期限
2. 催货说明段：**「本次催货单共 N 行订单明细，合计欠交 X。」**（事实陈述）
   + 回复期限要求。⚠️ 不用「欠交总行数」措辞——它与「合计欠交 X」在
   **数量未知（`open_qty` 为 NULL）** 时会自相矛盾（2026-11-28 实施发现）。
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
  404 → 催货单不存在（含非法单号 ⇒ 404 而非 500）
  400 → format 非法（只允许 xlsx / docx）
  401 → **无效/过期 token**（全仓口径：401=token 无效，403=身份有效但无权限）
  403 → 非 admin（**requireAdmin**，与其它催货端点同口径）
  ⚠️ 「完全无 Authorization 头」在本仓不可断言 401：real 模式无头 ⇒ 403；
     stub 模式无头 ⇒ 匿名拿到默认角色（含 admin）⇒ 放行。与**全部**既有催货端点同源。
```

**供应商名获取**：API 层用
`loadMdmDirectory(session, entityType="SUPPLIER", codes=[draft.supplier_code])`
（与到货跟踪同款，`entity_mapping_names.loadMdmDirectory`）。**查不到时回落
`supplier_code`，不 fail-closed**——催货函仍要能发出。

**文件名单**：含中文时用 RFC 5987 的 `filename*=UTF-8''…` 形式编码（避免中文文件名在
部分浏览器乱码），同时保留 ASCII 回退名。

## 前端

### 改动 `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx`

创建成功分支：现在只有 `message.success` ⇒ 改为**成功提示 + 「查看催货单」跳转链接**
（跳 `/procurement/chase`）。「部分未解析供应商」的 warning 语义保留。

### 改动 `frontend/src/pages/ProcurementChasePage.tsx`

列表每行加 `[Excel] [Word]` 下载按钮（催货单页是**唯一**下载入口）。

### 改动 `backend/scripts/seed_menu_config.py` + i18n

加菜单项 `item.procurementChase` → `/procurement/chase`（采购段排序位在
PendingDelivery 之后），label 走 i18n（zh-CN / en-US）。

### 下载实现

`httpClient` 走 **Bearer token**，`<a download>` 无法带 header ⇒ 用 axios
`responseType: "blob"` + `URL.createObjectURL` + 触发 `<a>` 点击 + `revokeObjectURL`
（与仓库其它导出保持一致）。

## 错误处理

| 场景 | 行为 |
|---|---|
| 催货单不存在 | 404 → 前端友好提示「催货单不存在或已删除」 |
| `format` 非法 | 400 —— **走 `HTTPException` ⇒ `{"detail": "..."}`**；而 404 走统一错误包络（`{"success": false, "error": ...}`）⇒ **前端要兼容这两种形状** |
| 导出服务异常 | 500 → 统一错误包络；前端提示「导出失败，请稍后重试」 |
| 催货单无明细行 | **仍可导出**（只有抬头信息，明细表为空） |
| MDM 查不到供应商名 | 回落 `supplier_code`，不阻塞导出 |

## 测试

**后端**
- 导出服务单测：Excel 行数与列名正确、欠交量取值正确、空明细也能出文件；
  Word 含关键文本（供应商名 / 单号 / 欠交量 / 回复期限）
- 端点集成测：200 + `Content-Disposition` 文件名正确、404（不存在 + 非法单号）、
  400（format 非法）、**无效 token 401**、**非 admin 登录 403**（确认 requireAdmin 生效）

**前端**
- 下载按钮触发：调用导出端点且 format 正确
- 创建成功 → 出现「查看催货单」跳转链接
- 菜单项：`seed_menu_config.py` 声明了 `item.procurementChase` → `/procurement/chase`

## 外发材料的保密口径（2026-11-28 裁决）

| 内容 | 进外发产物？ | 理由 |
|---|---|---|
| 供应商 / 单号 / 回复期限 / 明细 / 落款 | ✅ | 催货函的正文 |
| `notes`（催货单备注） | ✅ | spec 正文要素之一；**当前 UI 无输入口**（Copilot 结果卡创建时不暴露该字段）⇒ 暂无实际内容。将来若开放 UI，**须在输入处明确标注「对供应商可见」** |
| `decisions`（内部审批轨迹） | ❌ | 内部治理留痕，外发会泄露审批过程 |
| 状态枚举（`PENDING_APPROVAL` 等） | ✅ | 与系统内一致，便于对账 |

> payload 仍携带 `decisions`（供后续内部附件/审计用），只是**不渲染进文件**。

## 影响面与部署
- **无 DDL / 无迁移**（只读导出 + 新端点 + 菜单项）
- 后端新增 1 个 service 文件 + 1 个端点 + 菜单 seed 一项；前端 2 处接入（结果卡跳转链接 + 催货单页下载按钮）+ i18n
- **上线必做**：跑一次 `python -m scripts.seed_menu_config`（催货单菜单 code 是**全新**的 ⇒
  菜单 seed 的 `on_conflict_do_nothing` 对新 code 有效 ⇒ 直接**新增**该行；
  注意与场景 seed 不同，这里**不需要**定向补丁机制）
- **抬头键无需手工动作**：`chase.letter.company_name` 已接入 `main.py` 启动链
  （`seed_brand_config` 之后）⇒ **随发版自动播种**（value 播种为未配置，admin 随后可在
  后台填写）
- 需重建 backend + frontend 镜像；worker 同镜像须一并 `--force-recreate`
  （见 `Harness/skills/thdeploy/SKILL.md` §worker 同步铁律）

### 已知遗留项（本批未治）

- **既有催货端点对非法 UUID 单号仍打成 500**：`GET /drafts/{chaseDraftId}` 等旧端点未做
  单号规范化（本批只修了新导出端点）⇒ 建议下一批统一。

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
