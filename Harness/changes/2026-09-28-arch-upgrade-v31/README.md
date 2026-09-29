# 架构升级 v3.1 实施工程 · 分工总览

> **蓝图**：`docs/系统架构优化思路0928-v3.1.md`
> **日期**：2026-09-28
> **人力**：2 人（甲 = 数据/基建，乙 = 服务/链路）
> **周期**：W1-W12（MB2 完成于 W8，MB3 完成于 W12）

## 1. 决策记录（已与负责人确认）

| 决策点 | 结论 |
|---|---|
| M0 统一 ID | **全量改造**（按 v3.1 §17.1 P0.1-P0.4），非映射表渐进 |
| Claim 类型 | **保留现有 4 类型**（FACT/DEFINITION/RULE/STATISTIC），INFERENCE 新增 |
| 首期优先级 | **MB2 Agent 收敛 + 可信性** 先于 MB3 报告模板 |
| Evidence 策略 | 复用现有 `evidence` 表扩展 2 种 source_type，不建新表 |

## 2. 分支策略

```
main
 └── epic/v31-upgrade                 # 共享 epic 分支（每周同步点）
      ├── feat/m0-unified-id          # 甲 W1-W3
      ├── feat/evidence-sql-metric    # 乙 W1-W2
      ├── feat/knowledge-compiler     # 甲乙 W4-W5 协作
      ├── feat/planner-step-limit     # 甲 W5
      ├── feat/intent-semantic-merge  # 甲 W6
      ├── feat/confidence-level       # 乙 W5-W6
      ├── feat/memory-phase-a         # 乙 W6-W7
      ├── feat/hypothesis-hook        # 乙 W9-W10
      └── feat/report-templates       # 甲 W9-W12
```

规则：
1. 每人从 `epic/v31-upgrade` 切任务分支，完成后 PR 回 epic，**不直接进 main**
2. **每周五 rebase 对齐**：两人各自 rebase epic 最新，跑一遍对方模块的冒烟测试
3. alembic 版本号冲突时，后合并者负责 renumber（先合并者优先占用 0095/0096）

## 3. 代码边界（防止撞车）

| 区域 | 负责人 | 说明 |
|---|---|---|
| `alembic/versions/0095_*`（id_mapping） | 甲 | |
| `alembic/versions/0096_*`（evidence 扩展） | 乙 | |
| `app/domain/models.py`（id_mapping ORM） | 甲 | |
| `app/domain/wiki_models.py`（Evidence 扩展） | 乙 | |
| `ontology_service.py` / `graph_traversal_service.py` / `graph_relation_service.py` | 甲 | M0 全程独占 |
| Milvus / embedding 相关 infra | 甲 | |
| `business_db_pool.py`（execute_read_only） | 乙 | 只加记录钩子，不动护栏逻辑 |
| `chat_service.py` | **乙主甲辅** | 乙改执行/evidence/confidence 链路；甲改 planner/intent 入口；**同文件改动必须分日提交** |
| `step_query_planner.py` / `intent_service.py` / `chat_recall.py` | 甲 | |
| `chat_context.py` / `chat_helpers.py` | 乙 | |
| `wiki_page_service.py`（claim 查询） | 乙（confidence 派生） | 甲不动 |
| 新增 `knowledge_compiler_service.py` | 甲乙共建，甲主导 | |

**禁区**：两人都不得修改 `raw/`、seed 数据的存量语义、`SQL Guard` 三集合口径（SQL Guard 侧信道修复是已收口的高危区）。

## 4. 里程碑与验收

| 里程碑 | 周 | 验收标准 | 负责 |
|---|---|---|---|
| MB1 Semantic Foundation | W1-W5 | 三库统一 ID 对账一致；Evidence 3 类型齐；Compiler 门面 + 对账任务上线 | 甲主导 |
| MB2 Agent Convergence | W5-W8 | 单步 ≤1 次 LLM / 多步 ≤3 次；Confidence 4 级真实链路可见；追问字段继承补全 | 乙主导 |
| MB3 Report MVP | W9-W12 | supplier-360-v1 + monthly-ops-v1 模板上线；Hypothesis Hook 触发可用 | 甲 M4 / 乙 M7 |
| MB4 Governance | 穿插 | governance-authority.md 评审通过；冲突裁定落库闭环 | 乙 |

全局红线（来自项目 CLAUDE.md + 历史事故）：
- TDD：先写测试（真实 PG + 完整 API 链路，禁 sqlite 内存库），覆盖率 ≥ 80%
- 不可变数据：新对象不原地改
- M0 每步执行前备份到 `backups/<日期>_m0/`（pg_dump + neo4j-admin dump + Milvus 备份）
- Token 计量：新增 LLM 调用必须走 `token_usage_service` 收口

## 5. 风险登记

| 风险 | 概率 | 缓解 | 责任人 |
|---|---|---|---|
| M0 全量改造引发三库漂移 | 高 | 分批迁移 + 每批 count(*) 对账 + 双写窗口 | 甲 |
| Intent/Semantic 合并误伤业务路由 | 中 | 路由枚举冻结，只合并 LLM 调用层；SUPPLIER_360/AGENT_RUN 单独回归 | 甲 |
| 两人同改 chat_service.py 冲突 | 中 | 分日提交 + 周五 rebase + 文件内按方法区划分工 | 双方 |
| Confidence freshness 缺数据源 | 中 | v1 只对 claim 级生效，表级 freshness 借 DQ 时效字段 | 乙 |
| alembic 版本号撞车 | 低 | 先合并优先占号，后合并 renumber | 后合并者 |

## 6. 详细计划

- 甲：[plan-person-a.md](./plan-person-a.md)
- 乙：[plan-person-b.md](./plan-person-b.md)
