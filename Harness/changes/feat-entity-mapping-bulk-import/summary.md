# 变更：feat-entity-mapping-bulk-import（CSV/TSV 批量导入端点 + 前端弹窗 + 模板）

- **日期**：2026-09-16
- **Phase**：feature（业务用户日常维护场景：从无 → 有）
- **状态**：done
- **触发**：用户问「编码映射关系后续能否通过前台批量创建」+「导入的方式有模版和模版的使用说明吗」
- **MEMORY**：（本会话一并写入）

---

## 1. 需求

业务用户日常需要给 entity_mapping 灌数据（接入新 SRM / QMS / MDM 时批量映射），但当前只能：

- 单条 create（curl/Postman）
- 运维脚本 `scripts/sync_entity_mapping_from_thbi.py`（直连 Oracle，**运维专属**，业务用户无 Oracle 账号）

需要面向业务用户的「批量导入」入口，含模板 + 使用说明。

验收：

- 前端 `/entity-mapping` 页面加「批量导入」按钮 + 弹窗
- 后端 `POST /api/v1/entity-mappings/bulk` 端点，接收 JSON 数组，上限 1000 行
- 行级隔离：单行失败不阻塞其它行
- enterprise_key 可省略（前端不用算 SHA-256 + offset）
- 模板：`docs/entity-mapping-template.csv`（UTF-8 无 BOM，表头注释完整）
- 使用说明：`docs/entity-mapping-import-guide.md`（含 5 类错误示范）

## 3. 数据模型变更

无 alembic 迁移（复用现有 `entity_mapping` 表）。补 **ORM 已存在但 schema 缺**的字段：

- `EntityMappingCreate` 加 `name: str | None`（ORM 列早存在，仅 schema 暴露）
- `EntityMappingRead` 加 `name: str | None`（同上）

新增 `EntityMappingBulkImportItem` schema（与 Create 差异：enterprise_key 可省，默认 0 → 后端派生）。

## 4. 接口契约变更

新增：

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/v1/entity-mappings/bulk` | 批量导入；请求体 `EntityMappingBulkImportItem[]`；上限 1000 行 |

响应 schema：

```typescript
interface EntityMappingBulkResult {
  total: number;
  inserted: number;
  updated: number;
  skipped: number;
  failed: number;
  results: EntityMappingBulkResultRow[];
}
interface EntityMappingBulkResultRow {
  row: number; // 1-based（含表头），数据行从 2 开始
  status: "inserted" | "updated" | "skipped" | "failed";
  entityType: string | null;
  enterpriseCode: string | null;
  id: number | null;
  changedFields?: string[] | null;
  reason?: string | null;
  error?: string | null;
}
```

## 5. 实现要点

### 后端

- **`bulkImportMappings` 服务方法**：
  - 派生 enterprise_key：`SHA-256(enterprise_code)[:8] → uint64 → mod 2³² → + entity_type offset`
  - 与 `scripts/sync_entity_mapping_from_thbi.py` 同 `_stableKey` 算法（SSOT，参数 `offset` 派生 SUPPLIER=800000, MATERIAL=4295767296, 其它通用=8591534592）
  - 单事务；逐行 flush 后捕获 IntegrityError → 标 failed 不阻塞
  - 比对 7 列非身份字段（enterprise_code / source_key / source_code / match_rule / effective_date / expiry_date / name），全一致 → skipped，有变化 → updated
  - owner 由 actor.departments[0] 派生（与单条 create 同模式）
  - 行级日期校验（effective_date <= expiry_date）
  - outbox 入队（updated 事件带 `source: "bulk_import"` 标记，便于审计区分）

### 前端

- **`utils/csvTsvParser.ts`**：自实现 CSV/TSV 解析器（RFC 4180 子集）
  - 支持双引号包裹 + 双引号转义、UTF-8 BOM 自动剥离、自动识别 Tab/逗号分隔符
  - 列数不符 → 警告但保留主流列数的数据
- **`components/entity-mapping/BulkImportModal.tsx`**：
  - 3 个 Tab：粘贴 / 预览 / 结果
  - 前端纯函数校验（不调后端）：日期格式、枚举值、长度上限、日期顺序
  - 行级状态展示：OK / FAIL + 失败详情 hover
  - 后端返回结果按 status 着色（inserted=绿 / updated=蓝 / skipped=灰 / failed=红）

## 6. 测试

| 套件 | 结果 |
|---|---|
| 后端 unit `test_entity_mapping_service` | ✅ pass |
| 后端 unit `test_sync_entity_mapping_from_thbi`（顺手修 fixture 大小写 bug） | ✅ pass |
| 后端 integration `test_entity_mapping_api` 23 用例（含 4 个新 bulk 测试） | ✅ 23/23 pass |
| 前端 vitest `csvTsvParser.test.ts` 14 用例 | ✅ 14/14 pass |
| 前端 vitest `entityMappingApi.test.ts` 7 用例（含 1 个新 bulk API 测试） | ✅ 7/7 pass |
| 前端 tsc `--noEmit` | ✅ 0 errors |
| 端到端 curl 真实 prod 库 | ✅ inserted / skipped / updated 三态全验证 |

### 修复的测试 fixture bug（配对遗漏）

`test_sync_entity_mapping_from_thbi.py` 的 `_FakeAdapter` 重写 `execute_read_only` 返回大写列键（`SUPPLIER_CODE`），但生产 sync 脚本读小写（`supplier_code`）。上一轮 fix sync 脚本时**测试 fixture 漏改**，导致 7 个 sync 测试 fail。本次一并修齐。

## 7. 安全审查

未触发 security-reviewer。

- bulk 端点与单条 create 同 ACL 路径（owner 由 actor.departments[0] 派生）
- 行级上限 1000 防 DoS（PG 长事务）
- 前端 enterpriseKey 传 0 占位 → 后端派生；前端无法伪造 key
- bulkImportItem 不暴露 `owner` / `enterprise_key` 字段（防 client 篡改）

## 8. 部署验证

```bash
# 备份
mkdir -p backups/file-edit/20260916_1800
cp backend/app/domain/schemas.py \
   backend/app/services/entity_mapping_service.py \
   backend/app/api/v1/entity_mapping.py \
   backups/file-edit/20260916_1800/

# 部署
docker cp backend/app/domain/schemas.py qa-backend:/app/app/domain/
docker cp backend/app/services/entity_mapping_service.py qa-backend:/app/app/services/
docker cp backend/app/api/v1/entity_mapping.py qa-backend:/app/app/api/v1/
docker restart qa-backend

# 启动
docker logs qa-backend | grep "Application startup"
# Application startup complete.

# 端到端（prod 库）
curl -X POST http://localhost:8000/api/v1/entity-mappings/bulk \
  -H "Content-Type: application/json" -H "X-User-Id: admin" \
  -d '[
    {"entityType":"SUPPLIER","enterpriseCode":"BULK_LIVE_001",
     "sourceSystem":"ERP","sourceKey":"L1","sourceCode":"L1",
     "matchRule":"MDM_MASTER","name":"live测试供应商1"}
  ]'
# → inserted=1, id=354444

# 重跑同样 → skipped=1
# name 变更重跑 → updated=1, changedFields=[name]

# 测试数据清理
DELETE FROM entity_mapping WHERE enterprise_code LIKE 'BULK_%';
```

## 9. 关联

- 模板：`docs/entity-mapping-template.csv`（CSV 字段约定 + 示例）
- 使用说明：`docs/entity-mapping-import-guide.md`（9 段：错误示范 5 类、幂等、权限、性能、关联特性）
- 后端：
  - `app/services/entity_mapping_service.py`（+~150 行：bulkImportMappings + _stableKey + _deriveKey）
  - `app/api/v1/entity_mapping.py`（+ bulk 路由，必须在 /{mappingId} 前）
  - `app/domain/schemas.py`（+ EntityMappingBulkImportItem / BulkResult / BulkResultRow；+ name 字段暴露）
  - `app/domain/error_messages.py`（+ MSG_SCHEMA_ENTITY_MAPPING_NAME）
  - `app/services/messages_zh.py`（+ MSG_ENTITY_MAPPING_BULK_TOO_LARGE / _EMPTY）
- 前端：
  - `components/entity-mapping/BulkImportModal.tsx`（新）
  - `pages/EntityMappingPage.tsx`（+ 批量导入按钮 + 弹窗挂载）
  - `utils/csvTsvParser.ts`（新，RFC 4180 子集）
  - `api/entityMapping.ts`（+ bulkImportMappings）
  - `types/entityMapping.ts`（+ name 字段 + BulkResult / Row 类型）
  - `i18n/zh-CN.ts` / `en-US.ts`（+ 8 个 bulkImport 命名空间 key）
- 测试：
  - `tests/integration/test_entity_mapping_api.py`（+ 4 个 bulk 用例）
  - `tests/unit/test_sync_entity_mapping_from_thbi.py`（修 fixture 大小写）
  - `tests/entityMappingApi.test.ts`（+ 1 个 bulk API 用例）
  - `tests/csvTsvParser.test.ts`（新，14 用例）

### 关联变更

- [fix-entity-mapping-sync-bootstrap](../fix-entity-mapping-sync-bootstrap/summary.md) — 同步脚本 bug 修复（本次顺手修测试 fixture 与之配对）
- [fix-dq-status-timewindow-enums](../fix-dq-status-timewindow-enums/summary.md) — 同会话三域硬编码扫描的 DQ 部分

### 元教训

- **「dto entity_type 是 str 还是 enum」**：Pydantic 接收 wire JSON 时，string-typed 字段在 DTO 里**仍是 str**（不是 enum 实例），所以 `dto.entity_type.value` 会抛 `AttributeError`。bulk service 第 1 次调用就踩了；测试通过后端到端再发现`gt=0`约束挡 wire 入参。最终通过拆 `EntityMappingBulkImportItem`（enterprise_key 可省）+ service 内部容错（`isinstance str`）解决。
- **「前置 fix 漏改测试 fixture」**：上一轮修 sync 脚本三重 bug 时（hardcode name / 大小写键 / 30s 超时），只改了生产代码 + 加新内存，未覆盖到测试 fixture。本次集成测试跑全量才发现 7 个 sync 测试 fail。下次 sync 类 fix 应**同时改生产代码 + 改所有 fixture**，避免类似遗留。
- **「ORM 列存在但 schema 缺」**：entity_mapping.name 列早在 2026 年初就加过 ORM（Phase 6.x），但 EntityMappingCreate/Read schema 一直没暴露。bulk 导入强制需要 name 字段（业务用户填「北京XX有限公司」），趁本次一并补齐；前端类型 / 列表展示 / 后端响应都跟着亮起来。

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 3 段含 alembic 变更说明（无迁移）
- [x] 第 6 段所有测试套件结果
- [x] 第 7 段无安全敏感变更
- [x] 第 8 段部署命令 + 端到端输出
- [x] 第 9 段 5+ 跨文件链接
- [x] memory 待写