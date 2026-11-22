# 《AI 驱动本体发现与持续演进 V1.0（WP-ONT-AI）》与《V0.3 详细设计》能否融合？

> 核对时间：2026-11-22 ｜ 基线 `main@9662aa6b` ｜ 迁移 head 0165
> 结论先行：**能融合，而且代码里已经融合了一部分**（两套表与路由共存、迁移号交叠）。
> 但融合前必须先把 **6 个契约**对齐，否则会在"候选→发布"接缝处打架。

## 一、两稿的定位其实是**上下游**，不是替代关系

| | WP-ONT-AI（V1.0） | V0.3 详细设计 |
|---|---|---|
| 角色 | **候选生产者**：连接器 → 采集 → 剖析 → AI 语义候选 → 变更提案 → 审核 | **发布者 + 消费者**：语义发布（I1）→ 知识服务（I2）→ Agent（I3）→ 业务流程（I4） |
| 数据流位置 | 上游（写发现域自己的表） | 下游（写 `semantic_release`，被 NL2SQL/RAG/Agent 消费） |
| 关键红线 | 不自动改权威主数据；LLM 不得直接写正式本体 | 只读白名单；写操作走独立命令+审批（D-4） |

**两稿在红线与分工上完全一致**，且 WP 稿 §5 自己写明：

> `publish` 不直接写正式语义表，而调用既有本体治理/Compiler/Registry 的受控发布流程

⇒ 设计上本来就是「插接式」：WP 产出候选与提案，V0.3 的发布链负责编译、校验、版本化、回滚。
**这不是两份抢同一块地盘的文档。**

## 二、已经落地的事实（A1，可查）

| 设计项 | 代码现状 | 迁移 |
|---|---|---|
| `discovery_source` | ✅ 表 + `POST/GET /api/v1/ontology-discovery/sources` | **0146** |
| `metadata_snapshot` | ✅ 表（**不可变**触发器）+ `POST/GET /snapshots` | **0146/0147** |
| 与已发布 Manifest 的差异 | ✅ `GET /diff`（截断 500 + `truncated` 标记） | — |
| 审计 | ✅ 复用 `audit_log`（0148 加 `ALTER_SESSION` 之类动作枚举） | 0148 |
| 写入口边界 | ✅ 代码注释即红线：**发现域只写自己的表，不写治理表** | — |

⚠️ 也就是说：**A1 已完成，A2/A3 未做**（候选/提案/审核/评测四类表还没有）。

## 三、必须对齐的 6 个契约（不对齐就会打架）

### 1. 表命名与归属
WP 稿提 9 张表；现状只建了 `discovery_source` / `metadata_snapshot`。

| WP 稿表名 | 建议 | 理由 |
|---|---|---|
| `profile_result` / `ontology_candidate` / `masterdata_finding` / `ontology_change_proposal` / `ontology_review` / `discovery_run` / `evaluation_result` | 统一 **`discovery_*` 前缀**（如 `discovery_candidate`、`discovery_proposal`） | 与本仓既有命名一致（`discovery_source`）；**且必须避开 `ontology_*` 命名空间** —— 那是正式本体表，候选混进去等于"AI 写本体"的红线 |
| `metadata_snapshot`（已建） | 保持（已落地，改名成本高于收益） | 记入偏差即可 |

### 2. 状态机映射（两套 7 态）
- WP：`DISCOVERED → CANDIDATE → PROPOSED → IN_REVIEW → APPROVED → VALIDATED → PUBLISHED`
- V0.3 发布：`DRAFT → ACTIVE → ARCHIVED`（域内单活）

**必须写死映射**，否则"谁算已发布"两边各说各话：

```
APPROVED / VALIDATED  ──►  semantic_release(DRAFT)
activate              ──►  semantic_release(ACTIVE)     ← 只有这一步是"发布"
退回 / 失败            ──►  NEEDS_EVIDENCE / REJECTED（不产生 release）
```

### 3. 审批机制不能造第三套
WP §3 F07 要"四眼原则 + 审计"；V0.3 已有 **P5 `approval_task`**（审批矩阵）；
本仓还有 `semantic_release_check`（校验）。

⇒ 建议：**候选审核不新建审批引擎**，二选一：
- (a) 复用 `approval_task`（需已发布 workflow definition，成本高但统一）；
- (b) 保留轻量 `discovery_review` 表，但**强制携带 `approval_task_id` 外键**（首期可空，二期接上）。

**不建议**自建一套独立审核流（会与 I4 的审批语义重复——这正是 D1 要防的"双写双口径"）。

### 4. 权限与脱敏
WP 稿要求"只读连接、采样白名单、字段级脱敏、敏感数据不出域"；
V0.3 侧已有：数据源 ACL、文档密级（L1/L2/L3）、域可见性。

⇒ 融合口径：**`discovery_source.access_policy` 必须落到既有 ACL 上**（datasource 的可见范围 + 列级脱敏），
而不是新造一套策略字段；发现域的读取也要走 `ensureDatasourceActive` / 密级闸。

### 5. 证据契约
WP 要求候选带 `evidence_ids` 且"不以 LLM 自述为唯一依据"；
本仓 Evidence 契约已成熟（`evidenceId` **内容确定**、`sourceRef` 全字段、`confidence`）。

⇒ 候选的 `evidence_refs` 应**直接复用 `KnowledgeEvidenceRead`**，不要自定义一套 id 规则
（否则审核界面与知识检索会各有一套"证据编号"，无法互相引用）。

### 6. 计量与评测
- **计量**：V0.3 强制"每次 LLM 调用记 token/成本" ⇒ WP 的 `discovery_run` 必须带
  `prompt_tokens` / `completion_tokens` / `cost` / `model_version` / `prompt_version`（可复现性也靠它）。
- **评测**：WP §8 要"候选准确率/召回率/误合并率"；V0.3 有 P95 与答案一致性（A5）。
  ⇒ 两者共用**同一评测集与报告格式**，否则"完成度"无法在同一张表里比较。

## 四、范围与 WBS 的关系（这条最容易被忽略）

WP 稿开头就写明：**"本工作包为建议新增范围，需经过变更评审，不默认计入原有工期与工作量"**。

⇒ 融合的正确表述是：**把 WP-ONT-AI 作为 V0.3 的「前置可选阶段 I-1」**，而**不是**把 A/B/C 三阶段
塞进 I0–I5 的验收口径。否则 V0.3 的"首期 94%"会突然变成分母变大的 60 几%，验收标准被稀释。

建议写法：

```
I0 — I1 — I2 — I3 — I4 — I5      ← 原验收口径不变
        ↑
   I-1 本体发现（可选，独立工作包，产出候选与提案，由 I1 发布）
```

## 五、建议的三步融合路径

| 步 | 做什么 | 产出/验收 |
|---|---|---|
| **F1 契约对齐（文档 + 守卫）** | 写死上面 6 条契约；加**守卫测试**：候选表不得写入 `ontology_*`、`discovery_review` 必带 `approval_task_id`（二期）、`discovery_run` 必带计量字段 | 守卫用例红→绿；两稿各自补一节"接口对齐" |
| **F2 A2：候选与提案** | 建 `discovery_candidate`（含证据引用）、`discovery_finding`、`discovery_proposal`（`diff_json` 必须能编译成 **manifest v2**）；提案页走审核 | 一个真实表 → 产出候选 → 审核通过 → 生成 release DRAFT |
| **F3 A3：增量与发布联动** | `/diff` 驱动提案；publish 调既有 `activate`；补 `evaluation_result` 与回滚演练 | 端到端演练：发现→提案→审核→发布→回滚；误合并率 0 |

## 六、需要改哪份文档的哪一节

| 文档 | 改动 |
|---|---|
| WP-ONT-AI V1.0 | §4 表名统一 `discovery_*`（除已建的 `metadata_snapshot`）；§6 加"与 semantic_release 的状态映射"；§7 加"计量字段"；§5 `publish` 明确指向 `/semantic/releases/{id}/activate` |
| V0.3 详细设计 | 新增小节「I-1 本体发现（可选，独立工作包）」+ 一句边界声明：**发现域只产出候选，发布仍由 I1 承担** |
| 本仓 Harness | 新增 `Harness/changes/<date>-wp-ont-ai-fusion/design.md` 记录 6 条契约 |

## 七、融合的风险清单

| 风险 | 说明 | 缓解 |
|---|---|---|
| **AI 写本体** | 候选直接落 `ontology_*` 即破红线 | 表名前缀 + 守卫测试 |
| **第三套审批** | 与 P5 `approval_task` 重复 | 复用或强制外键 |
| **证据双口径** | 候选证据与知识证据两套 id | 复用 `KnowledgeEvidenceRead` |
| **成本失控** | 发现任务批量调 LLM | `discovery_run` 计量 + 预算上限 + 限流 |
| **验收稀释** | 把 A/B/C 计入首期 | 作为 I-1 独立口径 |

## 八、一句话回答

**能融合，且已经是"半融合"状态**（A1 的源/快照/差异已在代码里，并与语义发布共用迁移链）。
真正要做的不是"把两份文档合成一份"，而是**把 6 个接缝写死**：表名、状态映射、审批复用、
权限落地、证据契约、计量评测。做完这 6 条，WP-ONT-AI 就是 V0.3 的 **I-1**（可选前置阶段），
而不是另一套并行系统。
