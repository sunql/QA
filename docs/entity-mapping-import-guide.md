# entity_mapping 批量导入使用说明

> 一份给业务用户 / 运营人员看的「怎么往系统灌跨系统编码映射」操作手册。

---

## 1. 这是什么？

`entity_mapping` 表是企业统一代理键（`enterprise_key` / `enterprise_code`）与各业务系统原始编码（`source_system` + `source_code`）的**映射字典**。

典型场景：

- SAP（ERP）里的供应商 `V000001` 在我们统一供应商目录里叫什么？答案是 `SUP000001`。
- Sage X3（ERP）里的物料 `M-1001` 在物料目录里叫什么？答案是 `MAT000001`。

**没有这张表的映射，供应商 360° / 供应商风险 / NL2SQL 跨系统 join 全跑不通**。

---

## 2. 三种灌数据方式

| 方式 | 适用 | 操作入口 | 何时用 |
|---|---|---|---|
| **运维脚本** | 一次性同步 THBI 数仓主数据 | `scripts/sync_entity_mapping_from_thbi.py`（直连 Oracle） | 新接入 THBI 全量供应商/物料时 |
| **API 单条** | 程序化集成 / 调试 | `POST /api/v1/entity-mappings` | 业务系统实时推 1-2 条 |
| **批量导入（本文）** | 业务用户日常维护 | `/entity-mapping` 页面 → 批量导入 | 新接入 SRM / QMS / MDM 等系统，要批量映射几十~几千条 |

---

## 3. 怎么批量导入？

### 步骤

1. **下载模板**：`docs/entity-mapping-template.csv`（仓库根目录）
2. **填数据**：按列头约定填，每行一条映射（详见模板文件内注释）
3. **打开页面**：浏览器访问 `/entity-mapping`
4. **点「批量导入」**：弹窗出现
5. **粘贴内容**：把 CSV / TSV 内容粘贴到输入框
   - 自动识别 Tab 分隔 vs 逗号分隔（看哪个解析的行数最多）
6. **点「校验」**：实时显示每行的解析结果
   - ✅ 绿色 = 该行能入库
   - ❌ 红色 = 该行有问题，hover 看具体原因
7. **点「导入」**：开始批量入库
8. **查看结果**：弹窗底部表格显示每行的实际处理结果（insert / update / skip / error）

### 模板字段速查

| 列 | 必填 | 取值范围 | 示例 |
|---|---|---|---|
| `entity_type` | ✅ | SUPPLIER / MATERIAL / PO / GR / IQC / NCR | `SUPPLIER` |
| `enterprise_code` | ✅ | 1-100 字符 | `SUP000001` |
| `source_system` | ✅ | ERP / SRM / QMS / MDM / PLM | `SRM` |
| `source_code` | ✅ | 1-100 字符 | `SRM-2024-001` |
| `match_rule` | 选填，默认 `MAPPING` | MAPPING / MDM_MASTER / BUSINESS_KEY | `MDM_MASTER` |
| `name` | 选填，≤200 字符 | 自由文本 | `北京XX有限公司` |
| `effective_date` | 选填 | YYYY-MM-DD | `2026-01-01` |
| `expiry_date` | 选填 | YYYY-MM-DD；空=长期 | `2026-12-31` |

**`enterprise_key` 不需要手填**——后端按 `(entity_type, enterprise_code)` 自动派生，同一 code 永远派生同一 key（SHA-256 + 偏移量）。

---

## 4. 后端会怎么处理？

### 导入结果返回（每行一条）

```json
{
  "total": 4,
  "inserted": 3,
  "updated": 1,
  "skipped": 0,
  "failed": 0,
  "results": [
    {
      "row": 2,
      "status": "inserted",
      "entity_type": "SUPPLIER",
      "enterprise_code": "SUP000001",
      "id": 12345
    },
    {
      "row": 3,
      "status": "updated",
      "entity_type": "SUPPLIER",
      "enterprise_code": "SUP000002",
      "id": 12346,
      "changed_fields": ["name"]
    },
    {
      "row": 4,
      "status": "skipped",
      "entity_type": "SUPPLIER",
      "enterprise_code": "SUP000003",
      "reason": "与现存行完全一致，无变化"
    },
    {
      "row": 5,
      "status": "failed",
      "entity_type": "MATERIAL",
      "enterprise_code": "MAT000001",
      "error": "source_system 不在合法枚举（ERP/SRM/QMS/MDM/PLM）：'WRONG'"
    }
  ]
}
```

### 状态语义

- `inserted`：新行入库（之前不存在）
- `updated`：行已存在但有字段变化（name/effective_date/expiry_date 等被更新；唯一键三列不可变）
- `skipped`：行已存在且完全一致，无副作用
- `failed`：该行有问题，**不影响其它行入库**

### 事务策略

- **行级隔离**：某行失败不阻塞其它行
- **单事务**：一次 HTTP 请求 = 一次 DB 事务；中途失败，已成功的行**不回滚**
- **重试友好**：失败行修好后重跑，未变化的行自动 `skipped`，幂等

---

## 5. 常见错误

### 5.1 `entity_type` 报「不存在的业务对象」

```
row 3, entity_type=MATERIAL_X: 不存在的业务对象 code（仅支持 SUPPLIER/MATERIAL/PO/GR/IQC/NCR）
```

**原因**：你的企业没有注册 `MATERIAL_X` 这类实体。

**修法**：到 `/business-objects` 页面先注册新的业务对象 code，再回 `/entity-mapping` 灌数据。

### 5.2 `enterprise_code` 同一 entity_type 下重名

```
row 5, enterprise_code=SUP000001: 与现存行冲突（已属于 SUPPLIER/SR 系统）
```

**原因**：你尝试给同一个 `enterprise_code` 在同一 entity_type 下挂多个 source_system。

**修法**：检查你的数据源是不是真的希望「同一企业码跨多系统」（如果是，改用不同 enterprise_code，比如 `SUP000001-ERP` vs `SUP000001-SRM`）。

### 5.3 `source_system` 大小写错

```
row 2, source_system=erp: 不在合法枚举（ERP/SRM/QMS/MDM/PLM）
```

**原因**：枚举严格区分大小写，必须全大写。

**修法**：全表替换为 `ERP`。

### 5.4 行解析失败（列数不对）

```
row 4: 列数不符（期望 6 列，实际 5 列）："SUPPLIER,SUP000004,SRM,X1,MDM_MASTER"
```

**原因**：该行少了一列（或多了一列）；通常是 CSV 里有未转义的逗号。

**修法**：含逗号字段用双引号包裹，如 `"北京, 朝阳, XX公司"`。

### 5.5 整批报「文件解析失败」

通常是文件编码不对。**务必 UTF-8 无 BOM**（Windows 记事本默认会带 BOM）。

---

## 6. 权限

- **导入**：需要登录态（`getCurrentUser` 拦截）
- **owner 字段**：批量导入时，`owner` 默认置为当前用户 `userId`（行级 ACL 隔离）
- **跨用户**：用户 A 导入的行，用户 B 在列表里看不到（被 ACL 过滤）；要共享需走 owner 调整工单

---

## 7. 性能

- 单批上限：**1000 行 / 请求**（避免单事务过大阻塞 PG）
- 超过 1000 行 → 切多批；前端弹窗会自动按 1000 行 chunk
- 典型时延：1000 行 / 1 个事务 ≈ 200-500ms（PG 本地实例）

---

## 8. 与其它特性的关系

- **NL2SQL 召回**：chatService 在 join 时会查这张表把 source_code 转 enterprise_key；映射缺失 → 跨系统 join 出空
- **供应商 360° / 风险**：完全依赖这张表（之前 2026-09-16 修复的「供应商 360° 不可用」根因就是 entity_mapping 0 行）
- **本体召回**：`source_table` 反解本体类时，如果本体里有这张表的引用，也靠 entity_mapping 做关联
- **Milvus 向量**：与 entity_mapping 无关，但有 `name` 字段时向量召回质量更高（重名消歧）

---

## 9. 相关文档

- 模板：`docs/entity-mapping-template.csv`
- 后端 API：`GET/POST/PUT/DELETE /api/v1/entity-mappings`（单条）
- 后端 Bulk API：`POST /api/v1/entity-mappings/bulk`（批量，本文主语）
- 运维同步脚本：`scripts/sync_entity_mapping_from_thbi.py`（直连 Oracle）
- 数据模型：`Harness/wiki/data-model.md` → entity_mapping 段
- 相关 memory：[[qa-system-entity-mapping-sync-bootstrap]]（同步脚本三重 bug 修复）