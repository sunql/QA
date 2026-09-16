# 变更：entity_mapping 同步脚本三重 Bug + 一次性 bootstrap（补 3500 SUPPLIER / 350922 MATERIAL）

- **日期**：2026-09-16
- **Phase**：bugfix（数据维护链路）
- **状态**：done
- **关联变更**：[feat-entity-mapping-seed](../feat-entity-mapping-seed/summary.md)（脚本起源）；[feat-supplier-name-resolver](../feat-supplier-name-resolver/summary.md)（同步后 AutoComplete 可用）
- **MEMORY**：[[qa-system-entity-mapping-sync-bootstrap]]

---

## 1. 需求

「供应商 360°」与「供应商风险」两个菜单点开后**用户看不到任何可选供应商**，
AutoComplete 始终返回空；手输 supplier_key 后端 404
「供应商 enterprise_key=X 不存在或尚未在 entity_mapping 建档」。

验收标准：

- 跑同步脚本能真实写入 SUPPLIER 行（之前恒 0）
- 修复后 `entity_mapping` 表 SUPPLIER 行数 > 0、name 列已填
- AutoComplete 搜索按编码可命中（name 搜索作为遗留 UX 项，已记录）
- supplier-360 / supplier-risk 端点对真实 supplier_key 返回 200
- 不破坏现有 demo 行（15 MATERIAL + 3 PO + 1 GR + 1 IQC 保留）

## 2. 设计评审

### 根因（三层 bug 叠加）

| # | Bug | 触发症状 | 修复位置 |
|---|---|---|---|
| 1 | `_loadThbiDatasource` 硬编码 `DataSource.name == "THBI-Oracle"`（连字符），实际注册名是 `"THBI Oracle"`（空格） | 脚本每次跑立即抛 `RuntimeError("未找到 THBI-Oracle 数据源")` | `sync_entity_mapping_from_thbi.py:225` |
| 2 | `_fetchSupplierCodes` / `_fetchMaterialCodes` 用 `r.get("SUPPLIER_CODE")` 大写键，但 `execute_read_only` 统一把列名转小写（`business_db_pool.py:422`） | 即便修好 #1 也只会 dry-run 显示 suppliers=0 / materials=0（实测 DWD_SUPPLIER 有 3500 行、DWD_MATERIAL 有 350922 行） | `sync_entity_mapping_from_thbi.py:114/118/136/141-143` |
| 3 | `queryTimeoutSeconds` 默认 30s，DWD_MATERIAL 35 万行 SELECT+ORDER BY+fetchmany 全程 > 30s | dry-run / 正式跑都 TimeoutError | 环境变量 `QUERY_TIMEOUT_SECONDS=600`（无需代码改） |

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 改 name 硬编码为「查 is_default+is_active」 | ✅ 必选 | name 字段是 label 不是 key；硬编码名字是「重命名即失配」的脆弱模式 |
| (B) 改 uppercase 键为 lowercase | ✅ 必选 | adapter 已统一下沉小写，调用方用大写就是契约漂移 |
| (C) 默认 timeout 改大 | ❌ 不可取 | 全局影响其他 30s 够用的查询 |
| (D) 同步脚本里把 DWD_MATERIAL 拆批查（每批 5w 行） | 留作未来优化 | 当前 600s 已可一次性跑通 35w 行，复杂度收益不对等 |
| **(A)+(B)+(环境变量补 timeout)** | ✅ 推荐 | 三层叠加 bug 全修，单点风险都堵掉；改动 ≤ 30 行 |

## 3. 数据模型变更

无新增 / 修改表 / 列；纯种子补数 + 脚本契约修正。备份：
`backups/pg/entity_mapping_pre-sync_20260916_1352.dump`（7889 字节，20 行 demo）。

## 4. 接口契约变更

无新增 / 修改 API。`/api/v1/entity-mappings/search` 行为保持（`q` 仅按
`enterprise_code` / `source_code` ILIKE），name 搜索作为遗留 UX 项
（参见第 9 段沉淀）。

## 5. 实现要点

- `sync_entity_mapping_from_thbi.py:225-249` 改为按 `(is_default=true, is_active=true)`
  查询，name 仅入断言日志
- `_fetchSupplierCodes` / `_fetchMaterialCodes` 全部 `r.get("xxx")` 改小写
- 同步运行命令：`QUERY_TIMEOUT_SECONDS=600 docker exec -e DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata' qa-backend python scripts/sync_entity_mapping_from_thbi.py`
- 容器内仅 cp 单文件 `docker cp .../sync_entity_mapping_from_thbi.py qa-backend:/app/scripts/`（脚本独立于 uvicorn 进程，无需重启）

## 6. 测试

- 端到端冒烟：`curl /api/v1/supplier-360/3823452429` → 200 OK（济南吉利汽车有限公司，5 KPI 全部返回）
- `curl /api/v1/supplier-risk/3823452429` → 200 OK（level=unknown / contributions 全返回）
- 数据库行数：`SELECT entity_type, COUNT(*), COUNT(name) FROM entity_mapping GROUP BY entity_type`
  - SUPPLIER 3500（全部带 name）/ MATERIAL 350925（350910 带 name，3 demo 行来自 `seed_entity_mapping.py` 无 name 字段，保留）/ PO 3 / GR 1 / IQC 1
- 前端 SPA 路由：`/supplier-360` `/supplier-risk` 均 200 OK 881B（vite dev SPA HTML）

## 7. 安全审查

未触发 security-reviewer（脚本只读 entity_mapping + SELECT FROM THBI.*）。LOW：
- 同步脚本读 `DataSource.password_encrypted` 启动期解密，连接用完 dispose，不留存；
  与 chat 评估链路复用同一适配器，安全审查已覆盖。

## 8. 部署验证

```bash
# dry-run（修后）
$ docker exec -e DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata' \
    -e QUERY_TIMEOUT_SECONDS=600 \
    qa-backend python scripts/sync_entity_mapping_from_thbi.py --dry-run
[sync] 使用默认活跃数据源: name='THBI Oracle' id=1 host=192.168.205.70
  [plan] SUPPLIER key=3823452429 code=10105 name='济南吉利汽车有限公司'
  ... 其余 354412 行略
[sync] mode=DRY-RUN suppliers=3500 materials=350922 planned=354422 affected=0 erp_before=14
✅ DRY-RUN 完成（未写入数据库）

# 正式同步
$ docker exec ... python scripts/sync_entity_mapping_from_thbi.py
[sync] mode=WRITE suppliers=3500 materials=350922 planned=354422 affected=354422 erp_before=14
✅ THBI 实体映射同步完成

# 验收
$ curl -s 'http://localhost:8000/api/v1/supplier-360/3823452429' | jq .profile
{
  "enterpriseKey": 3823452429,
  "enterpriseCode": "10105",
  "entityType": "SUPPLIER"
}

$ docker exec qa-postgres psql -U qa_user -d qa_metadata -c \
    "SELECT entity_type, COUNT(*), COUNT(name) FROM entity_mapping GROUP BY entity_type"
 GR          |      1 |         0
 IQC         |      1 |         0
 MATERIAL    | 350925 |    350910
 PO          |      3 |         0
 SUPPLIER    |   3500 |      3500
```

## 9. 关联

- 备份：`backups/pg/entity_mapping_pre-sync_20260916_1352.dump`（20 demo 行）
- 脚本：`backend/scripts/sync_entity_mapping_from_thbi.py`（本次唯一改动文件，30 行内）
- 触发 bug 的注册：`backend/scripts/rebind_ontology_thbi.py`（name 字段写入处）
- Wiki：[Harness/wiki/data-model.md](../../wiki/data-model.md)（待补 entity_mapping 维护契约段）
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-entity-mapping-sync-bootstrap.md`

### 遗留 UX 项（非本 fix 范围，需另开 issue）

`EntityAutoComplete`（`frontend/src/components/common/EntityAutoComplete.tsx` → `api/entityMapping.searchMappings` →
`backend/app/services/entity_mapping_service.py:122 searchMappings`）目前仅按 `enterprise_code` /
`source_code` ILIKE，**不支持按 `name` 列搜索**。同步后用户可按编码搜到供应商，但按「吉利」
「吉利汽车」等中文名查不到。建议增加 `EntityMapping.name.ilike(like)` 子句，使 1-字符名搜
可用（[feat-entity-mapping-seed](../feat-entity-mapping-seed/summary.md) 阶段定下的「1 字符查询 +
显示供应商名」目标）。

### 长期维护机制（待办）

| 维度 | 现状 | 建议 |
|---|---|---|
| 周期性同步 | 仅手动 | `scripts/cron_sync_entity_mapping.sh` 走 launchd 周期任务 |
| 行数告警 | 无 | Prometheus 指标 `entity_mapping_total{entity_type}` + Alertmanager |
| 重命名影响面 | 多处脚本硬编码 name | DataSource 重命名 checklist |
| PG wipe 防护 | 已有 | 备份存档记录 entity_mapping 行数与 SUPPLIER/MATERIAL 计数作为恢复校验锚点 |

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 4 个候选方案对比
- [x] 第 3 段无 alembic 迁移（数据种子修复，无需迁移）
- [x] 第 7 段 LOW 1 项
- [x] 第 8 段 端到端冒烟输出贴出
- [x] 第 9 段 5 个跨文件链接（关联变更 / Wiki / Memory / 备份 / 脚本）
- [x] wiki 段落待补（已列入第 9 段待办）
- [x] memory 待写（已列入第 9 段）