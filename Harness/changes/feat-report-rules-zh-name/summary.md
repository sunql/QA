# 变更：评估报告规则明细 UI 中文化 + Rule 列改用规则名

- **日期**：2026-09-15
- **作者**：Claude
- **Phase**：Phase 9 评估报告（UI 中文化 + 字段升级）
- **状态**：done
- **关联变更**：无 predecessor
- **迁移版本**：无
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/report-rule-table-zh-name.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/report-rule-table-zh-name.md)

---

## 1. 需求

进 `/data-quality/reports/:id` → 「规则明细」Table：

- 现状 1：列标题全英文化硬编码（`Rule / Type / Target / Severity / Pass Rate / Status`），未走 i18n。
- 现状 2：Type 列直接展示 enum（`COMPLETENESS / VALIDITY` 等英文），无中文标签。
- 现状 3：Severity 列永远是 `—`（snapshot 里写死 `"severity": None`，evaluator 从没透传 severity）。
- 现状 4：**关键 UX 错误**——Rule 列展示 `rule_code`（系统唯一码如 `PO_QTY_NON_NEG`），无业务含义；用户期望展示 `rule_name`（业务可读名如「采购订单数量非负」）。

**验收**：
- 表头中文化（zh-CN）；en-US 同样走 i18n key
- Type 列 enum → `ruleTypeLabels` 中文（COMPLETENESS → 完整性 等）
- Severity 列 enum → `severityLabels`（HIGH → 高 等），不再永远 `—`
- Rule 列展示 `rule_name`，缺省时降级到 `rule_code`（兼容旧 snapshot）

## 2. 设计评审

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 后端 snapshot 加 `rule_name` + `severity`，前端读取并映射 | 数据一次落库、查表时不用 JOIN 性能好 | **选** |
| B. 前端拿 rule_id 再单独请求 rule 详情 N+1 拼装 | 不动 snapshot | 拒：snapshot 是历史数据，重看老报告也要能展示 |
| C. 前端硬编码中英映射表（写死 enum → 中文） | 不动后端 | 拒：i18n key 已存在（`ruleTypeLabels` / `severityLabels`），复用即可 |

最终：**A**。

设计点：

1. **`EvaluationResult` schema 加 2 个 optional 字段**：`rule_name: str | None = None` / `severity: str | None = None`（向后兼容旧 client）。
2. **`_evaluateOne` / `_errorResult` / except 分支**：全部从 `rule` ORM 读 `rule_name` / `severity` 填进 `EvaluationResult`。
3. **`evaluation_report_service.py:_buildSnapshot`**：把 `r.rule_name` / `r.severity` 写入 snapshot.rules[] dict。
4. **前端 `RuleRow` interface**：加 `rule_name: string | null`，从 snapshot 读（trim 后空串 → null，触发降级）。
5. **前端 `ruleColumns` 表头全走 i18n**：`t("dataQuality.reports.detail.ruleTable.{rule,type,target,severity,passRate,status}")`。
6. **Type / Severity 列 render 函数**：用 `t(..., { returnObjects: true }) as unknown as Record<string, string>` 拿到 labels 字典并映射；缺省 fallback 到原 enum 字符串（防御性）。
7. **Rule 列 render 函数**：`(v, row) => v || row.rule_code` —— `rule_name` 缺省时降级显示 `rule_code`，不破表。
8. **i18n 新增**：`dataQuality.reports.detail.ruleTable.{rule,type,target,severity,passRate,status}`（zh-CN + en-US）。

## 3. 数据模型变更

无 DB 迁移。

snapshot JSONB 结构新增 2 个字段（向后兼容老 snapshot 无这俩字段时不报错，前端 null-safe）：

```json
{
  "tables": [{
    "rules": [{
      "rule_id": 1,
      "rule_code": "PO_QTY_NON_NEG",
      "rule_name": "采购订单数量非负",   // ← 新增
      "rule_type": "COMPLETENESS",
      "severity": "HIGH",                  // ← 新增（之前永远是 null）
      "target_table": "PORDER",
      ...
    }]
  }]
}
```

## 4. 接口契约变更

| 文件 | 改动 |
|------|------|
| `backend/app/domain/schemas.py` | `EvaluationResult` 加 `rule_name: str \| None = None` / `severity: str \| None = None`（都 optional） |
| `backend/app/services/data_quality_evaluator.py` | `_evaluateOne` / except 分支 / `_errorResult` 三处构造 `EvaluationResult` 都填这俩字段（从 `rule` ORM `getattr` 读，rule 缺字段时 None） |
| `backend/app/services/evaluation_report_service.py` | `_buildSnapshot` 写 snapshot.rules[] 时加 `rule_name` / `severity` 两键 |

## 5. 实现要点

### 后端

- `EvaluationResult` 字段顺序：放在 `rule_code` 后、`rule_type` 前 —— 同业务相关字段相邻
- `getattr(rule, "rule_name", None)` 而不是 `rule.rule_name` 直接访问：rule 是 SQLAlchemy ORM 对象，部分场景（rule 缺失）会传 None 进来；getattr 兜底
- error 分支同步填：以前 error 时只有 `rule_code=""`，现在也填 `rule_name=None / severity=None`

### 前端

- `rule_name: string | null` 读取逻辑：`((r.rule_name as string | null | undefined) ?? "").trim() || null` —— 把空字符串、null、undefined 都归一为 null，让 render 函数能简单 `v || row.rule_code` 降级
- i18n render 函数 `t(..., { returnObjects: true })` 返回 `string` 类型但实际是 object；用 `as unknown as Record<string, string>` 二次断言（TS 编译过）
- `useMemo([t])` 依赖加 `t`：t 是闭包变量，理论上稳定，但 lint 规则要求显式声明

## 6. 测试

### 后端（pytest，3 个文件 60+ 个 case）

```bash
docker exec qa-backend bash -c 'cd /app && \
  TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata_test" \
  python3 -m pytest app/tests/unit/test_data_quality_evaluator_dispatcher.py \
            app/tests/unit/test_data_quality_evaluator_time_window.py \
            app/tests/unit/test_data_quality_evaluators.py -q'
# 期望：60 passed
```

注：`EvaluationResult` 加 optional 字段不破坏现有契约，旧测试构造 `EvaluationResult(rule_id=..., rule_code=..., ...)` 仍合法（缺省值生效）。

### 前端（vitest，新增 1 个文件 2 个 case）

`frontend/src/tests/DataQualityReportDetailPage.ruleTable.test.tsx`：

| 用例 | 覆盖 |
|------|------|
| 表头中文化 + Rule 列显示 rule_name + Type 列显示中文标签 | 全链路：snapshot → ruleRows 解析 → ruleColumns 渲染 → i18n 映射 |
| Rule 列在 rule_name 缺失时降级显示 rule_code（兼容旧 snapshot） | 旧数据兼容性 |

```bash
cd frontend && npx vitest run src/tests/DataQualityReportDetailPage.ruleTable.test.tsx
# 期望：2 passed
```

## 7. 安全审查

未触发（无认证/输入/外网依赖）。Severity / rule_name 来自服务端 SQLAlchemy ORM 读取，不经用户输入。

## 8. 部署验证

无后端迁移、无 alembic 变更、无 API 路径变更；后端镜像 cp 文件 + restart 即可。

```bash
docker cp backend/app/domain/schemas.py qa-backend:/app/app/domain/schemas.py
docker cp backend/app/services/data_quality_evaluator.py qa-backend:/app/app/services/data_quality_evaluator.py
docker cp backend/app/services/evaluation_report_service.py qa-backend:/app/app/services/evaluation_report_service.py
docker restart qa-backend
```

前端 Vite HMR 即可。

手动：
- 进 `/data-quality/reports/:id` → 规则明细表头全中文
- Rule 列展示「采购订单数量非负」等业务可读名
- Type 列展示「完整性」等中文标签
- Severity 列展示「高」等中文标签（之前永远是 `—`）
- 老 snapshot 数据（无 `rule_name`）：Rule 列降级显示 rule_code

## 9. 关联

- 设计稿：无
- Wiki：`Harness/wiki/api-reference.md` Phase 9 增量章节（EvaluationResult 字段补 rule_name/severity）
- Rules：`Harness/rules/变更记录强制规范.md`（新规范应用案例）
- Memory：`report-rule-table-zh-name.md`（待写）
- 相关变更：[feat-dq-evaluation-report](../feat-dq-evaluation-report/summary.md)（前置：本特性是其 UI 升级）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段无 DB 迁移（仅 snapshot 字段增）
- [x] 第 7 段说明未触发 security-reviewer
- [x] 第 8 段 docker compose 部署命令贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新
- [x] 至少 1 条 MEMORY 索引（新增 `report-rule-table-zh-name.md` + MEMORY.md 加行）
- [x] 无真实 SQL/DB 改动，跳过真实数据验证
