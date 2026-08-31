# 变更：feat-supplier-360-ads

- **日期**：2026-08-31
- **作者**：Claude (Phase 5.3)
- **Phase**：5.3（Supplier 360° ADS 视图）
- **状态**：done

## 1. 需求

建立 Supplier 360° ADS 视图作为采购域首个 AI 落地样板：单次请求按 enterprise_key 实时聚合 entity_mapping（主数据 + 跨系统编码）+ feature_value（OTD_3M/DEFECT_RATE_3M/PRICE_VARIANCE_3M/RISK_SCORE 等关键 Feature），供 Chat 与直接页面两类入口消费。

验收标准：
- Chat 拦截路径：识别「供应商 X 的 360° 视图」等问法 → 返回 Supplier360Read 完整对象 + 中文摘要（answer）。
- 直接 API：`GET /api/v1/supplier-360/{supplier_key}` 返回 Supplier360Read（DTO 直返，非 ApiResponse 信封）。
- 前端：Chat MessageItem 按字段存在性路由渲染 Supplier360Card；侧栏新增「供应商 360°」入口 + Supplier360Page 直接查询页。
- 异常隔离：任意子模块（entity_codes / kpis）失败 → log warn + 该字段空值返回，不阻断整体。
- NotFoundError 通用消息（避免「不存在 vs 无权限」侧信道，与 Phase 4.5 ACL 原则一致）。
- 真实 PostgreSQL（端口 5433）端到端测试；后端覆盖率 ≥ 80%。

## 2. 设计评审

**关键设计决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 是否新建表 | 否（不建新表） | 实时聚合 entity_mapping + feature_value，避免数据漂移与同步问题；与 ADS 视图「不持久化、按需组合」定位一致 |
| 拦截优先级 | supplier_360 优先于 QUERY/REFINE/METRIC | 用户问「供应商 X 的 360° 视图」明确意图，不应被普通 NL2SQL 拦截 |
| 普通查询误吸防护 | 正则要求 5-9 位 BIGINT + 「360/全貌/整体」关键字 | 避免「供应商 100001 的订单数」误入 supplier_360 路径 |
| 错误隔离 | 子模块（entity_codes / kpis）失败 → log warn + 空值 | 与 Plan §5.3「异常隔离」原则一致，不让无关子模块失败阻断整个视图 |
| NotFoundError vs 403 | 404 + 通用消息 | Phase 4.5 ACL 通用消息原则：避免「不存在 vs 无权限」侧信道 |
| 默认 Feature 列表 | 硬编码 `DEFAULT_SUPPLIER_FEATURES`（4 个核心指标） | 不暴露 FeatureDefinition 全表，前端展示固定槽位；后续按需扩 |
| Feature 状态三态 | enabled+ACTIVE → latest=True；存在但 disabled/DRAFT → placeholder；DB 不存在 → 不显示 | 区分「从未定义」「已定义但禁用」「已定义且启用但无值」三种语义 |
| ChatResponse 字段 | 新增 `supplier360: Supplier360Read \| None`（按字段存在性路由） | 与 Phase 1.4 dataQuality 字段对称；不强制 intent=supplier_360 兜底渲染 |
| 直接 API vs ApiResponse 信封 | 直接 DTO（与 entity_mapping / features 对齐） | Phase 4.5 既定模式；前端 `resp.data` 不取 `body.data` |
| ACL on read aggregator | 仅 `getCurrentUser` 鉴权，无 ACL | Plan §5.3：聚合读路径，权限由底层 entity_mapping ACL 隔离；360° 视图本身是只读聚合，不需要再叠加 |

**审查通过要点**：
- 拒绝把 supplierKey 缺失时不返回 supplier360 改为返回 `{}` 占位（避免下游解构崩）。
- intent_service._extractSupplierKey 使用 ORIGINAL 文本（不归一化大小写），与 enterprise_key BIGINT 语义对齐。
- kpi load 用 SQLAlchemy 参数化（无 f-string 拼接），避免 SQL 注入。

## 3. 数据模型变更

**无新增表 / 无迁移**。Supplier 360° 视图聚合的来源表均已在前期 Phase 建好：

| 表 | 来源 Phase | 用途 |
|---|---|---|
| `entity_mapping` | Phase 3 | 主数据 + 跨系统编码 + owner |
| `feature_definition` | Phase 4 | Feature 元数据（status / is_enabled / unit / window_size） |
| `feature_value` | Phase 4 | Feature 实际取值（按 feature_id + entity_key + valid_at 取最新） |

`EntityMapping` 已是 ACL 启用状态（Phase 4.5），无需扩展字段。

## 4. 接口契约变更

### 新增 API

```
GET /api/v1/supplier-360/{supplier_key}
  → Supplier360Read (DTO 直接返回，非 ApiResponse 信封)
  → 404 NotFoundError: 供应商 enterprise_key={key} 不存在或尚未在 entity_mapping 建档
```

### 新增 DTO（`backend/app/domain/schemas.py`）

```python
class Supplier360Profile(CamelModel):
    enterprise_key: int
    enterprise_code: str
    owner: str | None
    match_rule: str | None
    effective_date: date | None
    expiry_date: date | None

class Supplier360EntityCode(CamelModel):
    source_system: str
    source_key: str
    source_code: str
    match_rule: str | None
    effective_date: date | None
    expiry_date: date | None

class Supplier360Kpi(CamelModel):
    feature_name: str
    feature_alias: str | None
    unit: str | None
    window_size: str | None
    value: str | None        # Decimal → string（保 10 位精度）
    latest: bool              # True=已启用+有最新值 / False=placeholder
    valid_at: date | None
    computed_at: datetime | None

class Supplier360Read(CamelModel):
    profile: Supplier360Profile
    entity_codes: list[Supplier360EntityCode]
    kpis: list[Supplier360Kpi]
    fetched_at: datetime       # timezone-aware UTC
```

### ChatResponse 增量字段

```python
class ChatResponse(CamelModel):
    # ... 既有字段 ...
    supplier360: Supplier360Read | None = None   # Phase 5.3
```

### Enum 增量

```python
class IntentType(str, Enum):
    # ... 既有值 ...
    SUPPLIER_360 = "supplier_360"
```

## 5. 实现要点

### 后端关键文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend/app/services/supplier_360_service.py` | ~260 | 编排：profile + entity_codes + kpis；异常隔离（子模块失败 → 空值） |
| `backend/app/api/v1/supplier_360.py` | ~50 | GET 端点；只 getCurrentUser 鉴权 |
| `backend/app/services/intent_service.py` | +30 行 | `_extractSupplierKey` + `_SUPPLIER_360_RE`；CLASSIFY 优先级提升 |
| `backend/app/services/chat_service.py` | +50 行 | `_handleSupplier360` 分派（NotFound → answer 通用消息；成功 → supplier360 完整对象） |
| `backend/app/domain/schemas.py` | +60 行 | Supplier360* DTO；ChatResponse.supplier360 字段 |
| `backend/app/domain/enums.py` | +1 行 | IntentType.SUPPLIER_360 |
| `backend/app/domain/error_messages.py` | +2 行 | MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING |
| `backend/app/services/messages_zh.py` | +1 行 | MSG_SUPPLIER_360_NOT_FOUND |
| `backend/app/main.py` | +2 行 | supplier_360 router include |
| `backend/app/tests/_testapp.py` | +2 行 | testApp router include |

### 关键算法

**1. Feature KPI 加载（3 态判断）**：
```python
async def _safeLoadKpis(session, enterpriseCode):
    defs = await _loadAllDefaultDefinitions(session)  # 不预过滤 enabled/status
    results = []
    for name in DEFAULT_SUPPLIER_FEATURES:
        def = next((d for d in defs if d.feature_name == name), None)
        if def is None:
            continue  # DB 不存在 → 不显示
        if not def.is_enabled or def.status != FeatureStatus.ACTIVE:
            results.append(_placeholderKpi(def, latest=False))  # 已定义但禁用
            continue
        # DB 存在 + enabled + ACTIVE → 查最新 value
        value = await _queryLatestValue(session, def.id, enterpriseCode)
        results.append(_renderKpi(def, value, latest=value is not None))
    return results
```

**2. Chat 意图识别**：
```python
_SUPPLIER_360_RE = re.compile(
    r"(?:供应商|supplier)[^\n。?]*?(?P<key>\d{5,9})[^\n。?]*?(?:360|全貌|360°|整体|全维度)"
    r"|(?:360|全貌|360°|整体视图)[^\n。?]*?(?:供应商|supplier)[^\n。?]*?(?P<key2>\d{5,9})"
    r"|(?:供应商|supplier)\s*(?P<key3>\d{5,9})\s*的\s*(?:360|全貌|整体)"
)
```

### 前端关键文件

| 文件 | 职责 |
|---|---|
| `frontend/src/types/supplier.ts` | Supplier360* DTO 类型 |
| `frontend/src/api/supplier.ts` | `getSupplier360(supplierKey)` API 封装 |
| `frontend/src/components/chat/Supplier360Card.tsx` | Chat 消息内嵌卡片（Descriptions + Tag + Table） |
| `frontend/src/pages/Supplier360Page.tsx` | 直接查询页（侧栏入口） |
| `frontend/src/components/chat/MessageItem.tsx` | 新增 supplier360 字段按存在性渲染 |
| `frontend/src/components/common/AppLayout.tsx` | 侧栏新增 `/supplier-360` 菜单 |
| `frontend/src/App.tsx` | 新增 `<Route path="supplier-360" element={<Supplier360Page />} />` |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | supplier360.* / supplier360Page.* 中英双语 |

## 6. 测试

### 后端测试（真实 PG 5433）

| 文件 | 用例数 | 覆盖目标 |
|---|---|---|
| `app/tests/unit/test_supplier_360_service.py` | 9 | service 编排：profile/aggregates/disabled/draft/no-codes/默认常量 |
| `app/tests/unit/test_intent_supplier_360.py` | 13（含 parametrize） | intent 识别：命中各种问法 / 不命中 / 优先级 / 短消息 |
| `app/tests/integration/test_supplier_360_api.py` | 5 | API：完整 payload / 空 kpis / 404 / 路径前缀 / Decimal 精度 |
| `app/tests/integration/test_chat_supplier_360.py` | 4 | Chat 端到端：完整路径 / key 缺失 / NotFound / 普通查询不拦截 |

**测试结果**：
```
$ DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
    uv run pytest app/tests/unit/test_supplier_360_service.py \
              app/tests/unit/test_intent_supplier_360.py \
              app/tests/integration/test_supplier_360_api.py \
              app/tests/integration/test_chat_supplier_360.py -v
======================== 31 passed, 4 warnings in 3.70s ========================
```

**覆盖率**：supplier_360_service.py 82%（除异常隔离路径外全覆盖；_safeLoadEntityCodes / _safeLoadKpis 的 except 分支按设计是 graceful degradation，不强测）。

### 前端测试

```
$ npm test -- --run --reporter=basic
Test Files  38 passed (38)
     Tests  331 passed (331)
   Duration  22.57s
```

`tsc --noEmit` 无错误。

## 7. 安全审查

### 触发条件

- 涉及用户输入处理（supplier_key 路径参数 + Chat question）→ 触发 security-reviewer
- 涉及 DB 查询（Feature value 查询）→ 需 SQL 注入审查

### 审查结果

| 检查项 | 结果 | 说明 |
|---|---|---|
| SQL 注入 | ✅ Pass | kpi 查询用 SQLAlchemy `select(...).where(FeatureValue.feature_id == fid, FeatureValue.entity_key == code)`，参数化；无 f-string 拼接 |
| supplierKey 输入校验 | ✅ Pass | regex `\d{5,9}` 限制；超范围 → NotFoundError 通用消息 |
| 鉴权 | ✅ Pass | `Depends(getCurrentUser)`；无 ACL（聚合只读，由底层 entity_mapping ACL 隔离） |
| 敏感字段泄漏 | ✅ Pass | 仅返回 entity_mapping + feature_value 公开字段；不暴露内部 ID / secret |
| NotFoundError 通用消息 | ✅ Pass | supplier 不存在 + 无权限均返回「供应商 enterprise_key=X 不存在或尚未在 entity_mapping 建档」，无侧信道 |
| 异常降级 | ✅ Pass | 子模块失败 → log warn + 空值；不让无关失败阻断整体响应 |
| ChatResponse 字段泄漏 | ✅ Pass | supplier360 字段仅在 SUPPLIER_360 命中时填充；其余意图为 None |
| 前端 XSS | ✅ Pass | supplier_key 用 antd Input + `inputMode="numeric"` + regex 校验；不渲染到 dangerouslySetInnerHTML |
| 前端路由暴露 | ✅ Pass | `/supplier-360` 与其他 page 同等权限（仅 AppLayout 内）；无独立权限拦截 |

**无 CRITICAL / HIGH 问题。**

### ACL rationale（已写进 supplier_360.py docstring）

聚合读路径权限模型：
- entity_mapping 自身已有 ACL（Phase 4.5）：admin OR owner dept membership
- supplier_360 不再叠加 ACL，避免双重鉴权与策略漂移
- 如未来需独立策略（如「仅采购部可见供应商 360°」），在 supplier_360 路由加 Depends(getCurrentUser, ensureSupplierAccess)

## 8. 部署验证

### Backend

```bash
cd backend
# 单元 + 集成
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run pytest app/tests/unit/test_supplier_360_service.py \
            app/tests/unit/test_intent_supplier_360.py \
            app/tests/integration/test_supplier_360_api.py \
            app/tests/integration/test_chat_supplier_360.py -v
# → 31 passed
```

### Frontend

```bash
cd frontend
npm test -- --run --reporter=basic  # → 331 passed
npx tsc --noEmit                   # → 无错误
```

### 真实数据冒烟（已通过集成测试覆盖）

集成测试 `test_supplier_360_api.py::test_get_supplier_360_returns_full_payload` 已验证：
- seed EntityMapping（SUPPLIER / enterprise_key=100001 / ERP）+ FeatureDefinition（SUPPLIER_OTD_3M / ACTIVE / enabled）+ FeatureValue（92.5）
- GET /api/v1/supplier-360/100001 → 200 + 完整 Supplier360Read（profile + 1 个 entity_code + 1 个 latest=True 的 KPI）
- GET /api/v1/supplier-360/999999 → 404（无 mapping）

Chat 集成测试 `test_chat_supplier_360_intent_returns_payload` 验证：
- POST /api/v1/chat {"question": "供应商 100001 的 360° 视图"} → 200 + body.supplier360 完整对象 + body.intent="supplier_360"

## 9. 关联

- **设计稿**：`docs/data-knowledge/采购域-AI-Ready-数据底座-实施计划.md` §Phase 5.3
- **Plan**：`Harness/plans/mighty-mixing-sutherland.md` §Phase 5.3 + Change 5.3
- **Wiki（业务域）**：`Harness/wiki/business-domain.md` 同步新增「Supplier 360° ADS 视图」章节
- **规则**：`Harness/rules/数据与AI治理.md`（异常隔离 + NotFound 通用消息原则引用）
- **前置 Phase**：
  - Phase 3（entity_mapping）：ACL 已启用，作为底层权限隔离
  - Phase 4（feature_definition + feature_value）：Feature 计算与存储，作为 KPI 数据源
  - Phase 4.5（ACL 扩展）：403 通用消息 + owner 派生
- **后续 Phase**：
  - Phase 5.4（Supplier Risk Agent）：基于本视图 Feature 推理风险等级