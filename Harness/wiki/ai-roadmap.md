# 采购域 AI-Ready 数据底座路线图

> **本文件为系统级路线图**。详细计划见 `/Users/sunql/.claude/plans/mighty-mixing-sutherland.md`。
> **关联评估**：`docs/data-knowledge/系统差距评估报告.md`。

## 1. 战略目标

把 qa-system 从「L3 数据治理上探、L4 AI Ready 下沿」推进到「L3 治理完整化 + L4 AI Ready 三件套完整 + L5 AI Native 起步」，最终落地采购域 **Supplier 360° + Procurement AI Copilot** 样板。

## 2. 现状与目标

| 维度 | 当前 | 目标 |
|---|---|---|
| L3 数据治理 | ⚠️ 元数据 ✓，主数据部分，**质量 ✗ 血缘 ✗ 编码映射 ✗** | **完整化** |
| L4 AI Ready | ⚠️ 语义层 ✓（TermDictionary），**特征层 ✗ 知识层 ✗** | **三件套完整** |
| L5 AI Native | ❌ 无 Agent 平台 | 起步（Agent Registry + 知识图谱推理） |

## 3. 用户决策（2026-08-29）

| 决策点 | 选择 | 影响 |
|---|---|---|
| 起点 | **L3 治理完整化**（Phase 1-3） | 数据质量 / 血缘 / 编码映射 + 业务对象补齐 |
| 调度基础设施 | **暂不引入**（一次性脚本 + 手动触发） | 调度器后续独立 change |
| 多租户 | **延后 Phase 5+** | 本期不实现 |
| RAG/知识库 | **Phase 5 重点**（延后但要做） | Supplier 360° 同 Phase 落地 |

## 4. 6 阶段路线图

```
Phase 1 (L3-质量)   Phase 2 (L3-血缘)   Phase 3 (L3-编码+对象)   Phase 4 (L4-语义+特征)
[2个月]               [1.5个月]             [1个月]                  [3个月]
   │                    │                    │                       │
DQ 规则 + 评分      血缘表 + 字段级      EntityMapping +         KPI Catalog +
AI 可信度集成        自动解析              缺失对象建模              AI Feature Layer

                    Phase 5 (L4-知识+RAG)    Phase 6 (L5-Agent+图推理)
                    [3个月]                   [3个月]
                       │                        │
                  Document + RAG            Agent Registry +
                  Supplier 360° ADS          语义关系 + 图遍历
```

### Phase 1 — L3 数据治理：数据质量（P0）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-data-quality-rule-model` | 1.5-2 周 | 完成（2026-08-29） |
| `feat-data-quality-evaluator` | 1.5-2 周 | 完成（2026-08-30） |
| `feat-data-quality-score-model` | 0.5 周 | 完成（2026-08-30） |
| `feat-nl2sql-quality-integration` | 1 周 | 待启动 |

### Phase 2 — L3 数据治理：数据血缘（P0）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-data-lineage-model` | 0.5-1 周 | 待启动 |
| `feat-lineage-auto-extract` | 2 周 | 待启动 |
| `feat-lineage-visualization` | 1-1.5 周 | 待启动 |

### Phase 3 — L3 数据治理：跨系统编码 + 缺失对象（P0）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-entity-mapping-model` | 0.5-1 周 | 待启动 |
| `feat-entity-mapping-seed` | 0.5 周 | 待启动 |
| `feat-missing-business-objects` | 1.5-2 周 | 待启动 |
| `feat-ontology-governance-fields` | 0.5-1 周 | 待启动 |

### Phase 4 — L4 AI Ready：KPI + Feature（P1）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-kpi-catalog-governance` | 1.5-2 周 | 待启动 |
| `feat-kpi-catalog-seed` | 0.5-1 周 | 待启动 |
| `feat-ai-feature-model` | 2-2.5 周 | 待启动 |
| `feat-feature-store-api` | 1-1.5 周 | 待启动 |

### Phase 5 — L4 AI Ready：知识库 + Supplier 360°（P1+P2）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-document-catalog-model` | 1-1.5 周 | 待启动 |
| `feat-rag-pipeline` | 2-2.5 周 | 待启动 |
| `feat-supplier-360-ads` | 2-3 周 | 待启动 |
| `feat-supplier-risk-agent-mini` | 1-2 周 | 待启动 |

### Phase 6 — L5 AI Native：Agent + 图推理（P2）

| Change | 工时 | 状态 |
|---|---|---|
| `feat-agent-registry` | 1.5-2 周 | ✅ done（2026-08-31） |
| `feat-semantic-relations` | 2 周 | ✅ done（2026-08-31） |
| `feat-graph-traversal-api` | 1.5-2 周 | 待启动 |
| `feat-agent-runtime-mvp` | 2-3 周 | 待启动 |

## 5. 复用的基础设施

| 能力 | 路径 |
|---|---|
| Alembic 异步迁移 | `backend/alembic/versions/` |
| ORM + Pydantic | `backend/app/domain/{models,schemas}.py` |
| 三处同步（PG + Neo4j + Milvus） | `backend/app/services/ontology_service.py` |
| SQL 只读护栏 | `backend/app/infrastructure/business_db_pool.py` |
| Schema 自动发现 | `backend/app/services/schema_introspection_service.py` |
| LocalImport 流水线 | `backend/app/services/local_import_service.py` |
| 前端四 tab 模式 | `frontend/src/components/ontology/` |
| i18n（zh-CN / en-US） | `frontend/src/i18n/` |

## 6. 关键约束

- 每个 Phase 独立验收，每 change 有完整 10 段 summary.md
- 覆盖率 ≥ 80%，真实 PG + 完整 API 链路测试
- 不可让 Neo4j/Milvus 失败阻断 PG
- 不动 `id`（Phase 6 原地 update 约束）
- seed_ontology.py 同步更新，新加列后必须重跑

## 7. 风险

| 风险 | 缓解 |
|---|---|
| DQ 评估 SQL 注入 | code-reviewer 重点审查，强制参数化 |
| Neo4j 白名单扩大影响本体 | 增量扩，不动既有路径 |
| Milvus 多 collection 性能 | 每个 collection 自有索引 |
| 8 个对象建模 seed 数据缺失 | 部分用占位，与 Sage X3 顾问协作 |

## 8. 当前进度

- ✅ **Phase 1.1 `feat-data-quality-rule-model`** 进行中
- ⏳ 其余 23 个 change 待启动

## 9. 参考资料

- `docs/data-knowledge/采购域.md` — 采购域 9 项能力蓝图
- `docs/data-knowledge/系统差距评估报告.md` — 当前 vs 目标差距分析
- `docs/data-knowledge/《企业 AI-Ready 数据标准体系》.md` — 标准体系
- `/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` — 完整实施计划
