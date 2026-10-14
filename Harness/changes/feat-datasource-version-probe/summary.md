# 变更：feat-datasource-version-probe

- **日期**：2026-10-02
- **作者**：Claude / 启琳
- **Phase**：Phase 12 NL2SQL 引擎（方言版本分发）
- **状态**：done
- **关联变更**：[../fix-datasource-type-failfast/summary.md](../fix-datasource-type-failfast/summary.md)（同批方言加固的缺口 #2）
- **迁移版本**：无
- **提交**：`4ce0fbf`
- **MEMORY**：[qa-system-datasource-version-probe.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-datasource-version-probe.md)

---

## 1. 需求

方言架构核实后的缺口 #2：`oracle_version` 字段已存在且参与方言分发（11g 用 ROWNUM / 12c+ 用 FETCH FIRST），但**没有任何自动填充路径**——只在手工编辑数据源时才有机会填。用户问「是否包含各版本方言 / 以后还会不会不兼容」，评估结论是：版本粒度上 Oracle 只分 11g vs 12c+（12c/19c/21c 共用），MySQL/PG 无版本维度；最低成本的加固是**连接时探测服务端版本**，让 11g 老库不再依赖人工填版本。

验收标准：
1. 创建数据源不填版本 → 自动探测并落库；探测失败不阻断创建。
2. 用户显式给的版本永不被探测覆盖。
3. 更新数据源时，版本为空或连接参数变更 → best-effort 重新探测。
4. `/datasources/test` 端点顺带返回 `server_version`（诊断用）。
5. 点分版本（如 `11.2.0.1.0`）能被方言分发正确识别为 11g。

## 2. 设计评审

| # | 候选方案 | 取舍 | 决定 |
|---|---|---|---|
| A | **复用 test 连接探测**：`adapter.test()` 第三元返回服务端版本原文，create/update best-effort 调 `_probeServerVersion` | 版本共享测试连接（零额外连接成本）；fail-fast 语义与既有「探测不阻断」一致；协议改三元是内部接口，调用方全在本仓 | ✅ 采用 |
| B | 每次 NL2SQL 生成时实时探测 | 多一次网络往返在热路径上；方言只吃 `oracle_version` 静态字段，没必要 | ❌ 拒绝 |
| C | 前端填表单时调 /test 自动带出版本 | 只覆盖 UI 入口，API/seed 直建的数据源不受益；且 UI 交互变重 | ❌ 拒绝（可作为后续 UX 增强） |

版本归一原则：**best-effort，绝不阻断**——探测是增强（新功能），连接失败/驱动不支持都只记 warning 返回 None。

## 3. 数据模型变更

无迁移。`data_source.oracle_version` 列已存在（0033），本变更只是补上自动写入路径。

## 4. 接口契约变更

- `POST /api/v1/datasources/test` 响应新增 `server_version: str | None = None`（向后兼容，None 缺省）。
- `POST /api/v1/datasources` / `PUT /{id}`：`oracleVersion` 现在真的会被持久化——**顺带修了一个既有 bug**：`DataSourceCreate.oracle_version` 此前从未进 `DataSource(...)` 构造参数，用户填了也丢。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/infrastructure/business_db_pool.py` | `test()` Protocol 改三元 `(bool, str, version原文)`：`_SqlaAdapter` 返回 `conn.dialect.server_version_info`（tuple），`_OracleAdapter` 返回 `conn.version`（str）；版本共享测试连接，无额外 connect |
| `app/services/datasource_service.py` | `_normalizeServerVersion`（tuple→点分、40 字符截断防 banner 进字段）；`_probeServerVersion`（建临时适配器 → test() → dispose，三层 try 各自兜底）；`create()` 用户值优先、空则探测落库；`update()` 未显式给版本且（连接参数变更或版本为空）时用**更新后的最终参数**重探 |
| `app/domain/schemas.py` | `DataSourceTestResponse.server_version` |
| `app/services/nl2sql_dialects.py` | 点分版本修复：`version.startswith(("9","10","11"))` —— 否则探测落库的 `11.2.0.1.0` 三个规则都不命中，会落 12c 分支给 11g 库生成 FETCH FIRST |
| `app/services/messages_zh.py` | （无新增；探测失败仅 logger.warning） |

## 6. 测试

| 用例 | 断言 |
|---|---|
| `test_datasource_service_probe.py`（新，~20 例） | 归一化（tuple→"8.0.46"、None→None、40 字符截断）；create 填充/用户值胜出/探测失败不阻断；update 重探时机（`called == []` 双向断言）；/test 返回版本 |
| `test_nl2sql_service.py::TestResolveDialectDottedVersion` | `11g`/`11.2.0.1.0`/`9.2.0`/`10.2.0` → ROWNUM；`12.1.0.2`/`19.0.0.0.0`/`19c`/`None` → FETCH FIRST |
| `test_datasource_pool.py` / `test_datasource_api.py` | fake adapter 协议同步为三元；成功断言 `server_version`，失败断言 None |

回归：probe + 方言版本 **20/20 绿**；全量 unit 3 failed（与基线 3 条逐条一致，均为既有红）/ 312 passed；integration `test_datasource_api.py` 15 passed。

## 7. 安全审查

未触发 security-reviewer：探测走的连接参数与 test_connection 完全同源（白名单、Fernet 加密存储均不变）；版本字符串来自数据库服务端 banner——`_normalizeServerVersion` 截断 40 字符且只进 DTO JSON 通道，无 HTML/SQL 拼接点。

## 8. 部署验证

`./scripts/deploy_backend.sh` 成功。真机端到端（2026-10-02）：

1. 建探针数据源（PG → qa-pg-a1，不填版本）→ 响应 `oracle_version = "16.14"`（自动探测落库）✅
2. `POST /datasources/test` → `serverVersion = "16.14"` ✅
3. `PUT` 显式填 `11g` → 保持 `11g`（用户值不被覆盖）✅
4. `DELETE` 探针 → 204，列表回 5 ✅

存量数据现状：THBI `19c`（用户已手工填）；其余 PG/MySQL 数据源版本为空——无版本维度影响（仅 Oracle 分叉），下次编辑保存时会自动补探。

## 9. 已知边界与后续

- MySQL 5.7 方言（无 CTE/窗口函数）仍是 YAGNI 缓议——一旦真出现 5.x 数据源，探测会把 `5.7.x` 落进 `oracle_version`，届时按此信号加 MySQL 版本分支。
- 探测只发生在 create/update 时点；DB 版本升级不回填（需重存一次数据源）——低频事件，接受。
