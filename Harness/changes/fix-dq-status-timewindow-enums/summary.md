# 变更：DQ 域 status / time_window_type 字面字符串改枚举派生

- **日期**：2026-09-16
- **Phase**：refactor（消除硬编码字面常量，引入枚举 SSOT）
- **状态**：done
- **触发**：用户请求「检查后台 aichat、DQ、本体管理相关的代码还有没有硬编码的逻辑」
- **MEMORY**：（本会话内一并写入）

---

## 1. 需求

消除三处业务运行时残留字面字符串，让枚举成为 SSOT。后续添加 `ReportStatus` / `ReportTimeWindowType` 新成员时，**只需一处（enum 类）改**即可让 schema 校验 / ORM CheckConstraint / scheduler service 三处同时生效。

验收标准：

- 三处全部走枚举派生 / 强类型参数
- IN 子句字符串内容与原版**字面相同**（避免 alembic 检测到 schema 漂移、误推 migration）
- 集成测试 8/9 通过（1 fail 是预存 Oracle mock 缺失，与本次无关）
- 现有端点 200 OK 无回归

## 2. 设计评审

### 根因（三处独立的字面字符串残留）

| # | 位置 | 字面 | 现有 SSOT |
|---|---|---|---|
| 1 | `evaluation_report_scheduler_service.py:267` | `status="PUBLISHED"` | `ReportStatus.PUBLISHED.value`（enums.py:472） |
| 2 | `evaluation_report_scheduler_service.py:32-34` | `_WINDOW_OFFSETS` 用 `"LAST_7D"` / `"LAST_30D"` 作 key | `ReportTimeWindowType`（enums.py:488） |
| 2' | `evaluation_report_scheduler_service.py:46-51` | `_resolve_window` 三处 `"LAST_RUN"` / `"LAST_7D"` 字面比较 | `ReportTimeWindowType` |
| 3 | `schemas.py:3359` | `if v not in {"LAST_7D", "LAST_30D", "LAST_RUN"}` 字符串集合 | `ReportTimeWindowType` |
| 3' | `models.py:1905` | `CheckConstraint("time_window_type IN ('LAST_7D','LAST_30D','LAST_RUN')", ...)` 裸 SQL | 枚举 `.value` 派生 |

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| **(A) 三处全部改枚举派生 + 强类型参数 + pydantic 双向 coerce** | ✅ 推荐 | 一次性收敛 5 处字面，加新值时只改 enum |
| (B) 仅把 scheduler 字面改枚举，schema/models 不动 | ❌ 不彻底 | 加第 4 个枚举值时仍要改 schema + models 两处 |
| (C) 把枚举搬到 system_config 表 | ❌ 不可取 | 枚举是**类型契约**（ORM CheckConstraint / pydantic 字段类型），不是配置项 |

### 关键设计点

- `_WINDOW_OFFSETS` 改为 `dict[ReportTimeWindowType, timedelta]` 强类型 key，避免运行时拼字符串 key 的 typo
- `_resolve_window` 入参改 `ReportTimeWindowType` 强类型；调用方已传 ORM Enum 实例（`schedule.time_window_type`），无需 coerce
- `EvaluationReportScheduleCreate.time_window_type` 改 `ReportTimeWindowType` 类型；校验器改 `mode="before"` + `try: ReportTimeWindowType(v)`，**同时接受字符串（API 入参）和枚举实例（测试代码）**
- CheckConstraint SQL 改为模块加载时字符串拼接：`(t.value for t in ReportTimeWindowType)`；拼接结果 `time_window_type IN ('LAST_7D','LAST_30D','LAST_RUN')` 与原 SQL **字面完全相同**，alembic 不会检测到漂移
- IN 子句生成时机：**模块导入期**（CheckConstraint 是 `mapped_column`/`__table_args__` 的实例化参数），必须在 import-time 可见，避免产生运行时 SQL 拼接

## 3. 数据模型变更

无 alembic 迁移。CheckConstraint SQL 字面值与 0074 一致，PG 不感知变化。

## 4. 接口契约变更

无新接口。`EvaluationReportScheduleCreate.time_window_type` 字段类型从 `str` 改为 `ReportTimeWindowType`（pydantic 序列化时仍输出字符串，wire-compat 保持）。

## 5. 实现要点

- `enums.py`：无需改动（`ReportStatus` / `ReportTimeWindowType` 已存在）
- `schemas.py`：加 `ReportTimeWindowType` import；`time_window_type` 字段改枚举类型；校验器改 `mode="before"` + 双向 coerce
- `models.py`：加 `ReportTimeWindowType` import；CheckConstraint SQL 改派生拼接
- `scheduler_service.py`：加 `ReportStatus, ReportTimeWindowType` import；`_WINDOW_OFFSETS` key 改枚举；`_resolve_window` 入参类型 + 字面比较全改枚举；`status="PUBLISHED"` → `ReportStatus.PUBLISHED.value`

## 6. 测试

| 套件 | 结果 |
|---|---|
| 静态 import（models / schemas / scheduler） | ✅ 全部 import 成功，CheckConstraint SQL 拼接无错 |
| 行为烟雾（`_resolve_window(LAST_30D, None)` 返回 30 天 timedelta） | ✅ |
| Pydantic 双向 coerce（字符串 `'LAST_7D'` + 枚举 `LAST_RUN` 实例都通过校验） | ✅ |
| Pydantic 拒绝非法值 `LAST_99D` | ✅ raise ValueError("unsupported time_window_type: LAST_99D") |
| `ReportStatus.PUBLISHED.value == 'PUBLISHED'` | ✅（wire-compat） |
| 集成测试 `test_evaluation_report_api.py` 9 用例 | ✅ 8 pass / 1 fail（`test_list_filter_by_class_id` 触发真实 evaluation 链 → 连 THBI Oracle 失败，预存 issue，与本次无关） |
| 端到端 4 端点 | ✅ eval-report list 200 / schedules list 200 / ontology 200 / supplier-360 200 |

## 7. 安全审查

未触发 security-reviewer。改动语义不变（value 字面相同），仅把字面字符串替换为枚举常量。无新输入面。

## 8. 部署验证

```bash
# 备份
mkdir -p backups/file-edit/20260916_1700
cp backend/app/services/evaluation_report_scheduler_service.py \
   backend/app/domain/schemas.py \
   backend/app/domain/models.py \
   backups/file-edit/20260916_1700/

# 部署
docker cp backend/app/services/evaluation_report_scheduler_service.py qa-backend:/app/app/services/
docker cp backend/app/domain/schemas.py qa-backend:/app/app/domain/
docker cp backend/app/domain/models.py qa-backend:/app/app/domain/
docker restart qa-backend

# 启动
docker logs qa-backend 2>&1 | tail -3
# Application startup complete.
# Uvicorn running on http://0.0.0.0:8000

# 验收
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/v1/data-quality/reports?limit=20
# 200
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/v1/data-quality/reports/schedules
# 200
```

容器状态：qa-backend Up，Application startup complete，零异常。

## 9. 关联

- 主修复 3 文件：`backend/app/services/evaluation_report_scheduler_service.py` / `backend/app/domain/schemas.py` / `backend/app/domain/models.py`
- 备份：`backups/file-edit/20260916_1700/`
- 关联变更：[fix-eval-report-zombie-revival](../fix-eval-report-zombie-revival/summary.md)（复活时引入的 4 个 ORM 类，本次同样被 enum 化锁定）
- 触发源头：三域硬编码扫描（chat / DQ / ontology），其中 ontology 与 aichat 已通过架构层校验天然干净，仅 DQ 域有 2 个真实残留点

### 教训

- **「ORM CheckConstraint 用裸 SQL 字符串」是字面重复的温床**：本应在 CheckConstraint 上封装 `_enum_in_clause(EnumCls)` 工厂函数，全项目复用。但本次只 1 处使用，未抽取（YAGNI）；若后续出现第 2 处 CheckConstraint IN 列表字面则应抽工厂
- **「pydantic Field(str) + 集合字面校验」是 schema 校验器的反模式**：应直接用枚举类型 + pydantic 自动校验，零手写代码。本次只改一处，后续若发现同类 pattern 应一并扫
- **「import-time 字符串拼接」是隐藏耦合**：CheckConstraint SQL 在模块加载期就拼好，**新增枚举值必须重启进程**才生效（DDL 不会自动重建）。本项目 alembic baseline 已固化，重启即可；新增值若触发 alembic 漂移需手动管理迁移

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 3 候选方案
- [x] 第 3 段无 alembic 迁移（字面值未变）
- [x] 第 7 段无安全敏感变更
- [x] 第 8 段部署命令 + 验收输出
- [x] 第 9 段 5+ 跨文件链接
- [x] memory 待写（已列入本会话收尾）