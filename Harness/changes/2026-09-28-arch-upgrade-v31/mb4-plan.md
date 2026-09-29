# MB4 重新界定方案（待评估）

> 起草：2026-09-30，MB3 批次收尾后
> 状态：**待负责人评估**，未启动
> 前置事实：`main` = `epic/v31-upgrade` = `82947cb`，已推送

---

## 一、为什么 MB4 需要重新界定

蓝图 §23.3 原本定义：

| 里程碑 | 包含 | 验证标准 |
|---|---|---|
| MB1 Semantic Foundation | M0 + M1 + M8 | 三库 ID 一致；Claim→Evidence 链可追溯 |
| MB2 Agent Convergence | M2 + M3 + M6 | 单步任务 ≤1 LLM；多步 ≤3 LLM；Confidence 4 级生效 |
| MB3 Report MVP | M4 + M7 | 3 个模板上线；Hypothesis 可选触发 |
| **MB4 Governance** | **M5** | **Authority 字段可用；治理文档评审通过** |

**MB4 的全部内容 = M5 Authority**，而 M5 已作为 MB3 的启动项交付（`550c968`：`authority_department` + 0102 + 11 部门枚举 + `docs/governance-authority.md` 476 行）。

⇒ **MB4 按原定义已无内容**。同时 MB3 完成意味着蓝图 §23.1 的 M0–M8 **九项全部落地**。

**结论**：MB4 需要从「蓝图既定里程碑」改为「**v3.1 剩余项的收口里程碑**」。下面先盘点真实剩余量，再给候选方案。

---

## 二、现状盘点（已交付 vs 剩余）

### 2.1 已交付（M0–M8，逐项有 commit 佐证）

| M | 内容 | 优先级 | 交付 |
|---|---|---|---|
| M0 | 统一 ID 体系（P0.1–P0.4） | P0 | ✅ `41e8449..72e8274`，三库一致 |
| M1 | Claim + Evidence MVP（3 类型） | P1 | ✅ MB1 + MR，§4.12 三类型写入路径全齐 |
| M2 | Agent 收敛（Semantic 合并 / Planner ≤5 / SQL 自动 Evidence） | P1 | ✅ 通道 1 = A7 `classifyAndRecall`；≤5 步 = `05d3412`；SQL Evidence = B2 |
| M3 | Confidence 4 级 | P1 | ✅ B4 `confidence_service.py` |
| M4 | Report 模板 | P2 | 🟡 **2/3 模板**（monthly-ops-v1 + supplier-360-v1） |
| M5 | Authority 字段 + 治理文档 | P2 | ✅ MB3 首项 |
| M6 | 三层 Memory Phase A | P1 | ✅ B5 |
| M7 | Hypothesis Hook | P2 | ✅ B6 |
| M8 | Knowledge Compiler 3 产物 | P1 | ✅ MB1 `knowledge_compiler_service.py` |

### 2.2 剩余（逐项已核实，非估计）

| # | 项 | 蓝图出处 | 现状佐证 | 优先级 |
|---|---|---|---|---|
| R1 | **Intent 通道 2**（LLM Semantic 兜底） | §5.2 现状栏「合并 Agent ⚪」 | A7 只做了通道 1（单入口收口），通道 2 未做 | P1 |
| R2 | **Planner 分级路由**（中间两档） | §5.3「v3.1 强制分级」表 | 只做了「>5 拒收 / ≤5 放行」；「2-3 步模板化」「4-5 步 LLM+模板校验」未做 | P1 |
| R3 | **Evidence 用户面**（点击展开） | §4.12 + §6.1 U1 | 后端 3 类型全齐，**前端无 EvidenceCard** | P1（用户可见缺口） |
| R4 | **Row/Col Security** | §13 / §22.2 | 列级脱敏 + 行级 filter 注入均未做 | P2 |
| R5 | **sales-decline-v1 模板** | §23.1 M4 | 第 3 个模板未做 | P2 |
| R6 | **技术债 TD-1..TD-15** | 本目录 `tech-debt.md` | 含 2 项 P1（TD-1 PLAN_EMPTY 噪声、TD-5 路由无鉴权） | P1/P2/P3 |

**已确认不属于剩余**（避免误排期）：
- §5.4 Result 合并 → 蓝图现状栏已是「✅」
- §15.3 Memory Phase B → B5 的 `inheritance_snapshot`（alembic 0101）已落地
- §15.4 Memory Phase C / Phase 3b/3c / Knowledge Compiler 8 产物 / Analysis DSL / Report DSL / Authority 完整模型 / Evidence 5+ 类型 → 蓝图 §22.4 明确标 **📦 v3.x**，且各有**触发条件**（如「复杂分析场景 ≥10/周」），**不应现在做**

---

## 三、候选方案

### 方案 A（推荐）· MB4「v3.1 收口」

**目标**：把蓝图 P1/P2 的剩余项清零，使 **v3.1 可正式宣告交付**；同时先清掉两项 P1 债务，让后续所有验证跑在干净基线上。

| 任务 | 内容 | 工期 |
|---|---|---|
| **A1 债务清障** | TD-1（P1）统一刷新 `_PipelineLlm` 陈旧夹具，消掉最大噪声源；TD-5（P1，**安全**）`wiki_compile.run_task` 补鉴权；打包 P3 卫生项（TD-3 / TD-8 / TD-11 / TD-12 / TD-13） | 0.5 周 |
| **A2 Evidence 用户面** | 前端 `EvidenceCard`：答案卡片按 `source_type` 渲染 Document / SQL_QUERY / METRIC_RESULT 三类证据，点击展开（§4.12 的「点击展开证据」） | 0.5 周 |
| **A3 Intent 通道 2** | LLM Semantic 兜底：通道 1 未命中时走一次 LLM 语义判定，接入位已在 `classifyAndRecall` 预留（A7 单入口） | 0.5–1 周 |
| **A4 Planner 分级路由** | §5.3 中间两档：2-3 步走 TOP-N 模板化；4-5 步走 LLM 生成 + 模板校验 | 0.3–0.5 周 |
| | **合计** | **~1.8–2.5 周** |

**验收**：R1/R2/R3 清零 + TD-1/TD-5 关闭；全量 integration 基线重新对账并报出前后数字。

**风险**：
- A3 要动 `classifyAndRecall` 的判定链——A7 红线是「IntentType 13 类冻结」，通道 2 **不得新增 IntentType**，只能影响「走哪条通道」的分派
- A4 的「模板化」需要先确定模板边界；若 MVP 只做 2-3 步模板化、4-5 步保持现状，工期可压到 0.3 周

---

### 方案 B · MB4「治理与安全」

**目标**：补安全纵深，不追求 v3.1 闭环。

| 任务 | 内容 | 工期 |
|---|---|---|
| B1 Row/Col Security | 列级脱敏 + 行级 filter 注入（§13） | 2 周 |
| B2 Confidence 表级 freshness | §12.2 的 freshness 从 claim 级扩到表级 | 0.5 周 |
| B3 Authority 完整模型预研 | §4.13 的 📦 项，触发条件「治理委员会建立后」 | 待定 |
| | 合计 | ~2.5 周+ |

**问题**：R1/R2/R3 悬空 ⇒「v3.1 是否已完成」这个问题继续没有答案；且 Row/Col Security 是 P2，与 v3.1 的核心降级（把能力做小做扎实）无耦合，先做它会打断「收口」的节奏。**B3 的触发条件当前不成立**。

---

### 方案 C · MB4「v3.x 预研」

**目标**：提前启动下一代能力（Phase 3b/3c、Memory Phase C、Knowledge Compiler 8 产物、Analysis DSL、Report DSL）。

**问题**：蓝图 §22.4 为每一项都写了**触发条件**（如 Analysis DSL「复杂分析场景 ≥10/周」、Report DSL「用户自定义报告需求 ≥5/周」），**当前均未成立**。现在做等于用猜测替代信号，与 v3.1「显式降级」的整个思路相反。**不建议**。

---

## 四、推荐与理由

**推荐方案 A**，理由：

1. **它让 v3.1 有终点**——R1/R2/R3 是蓝图里最后三项非 📦 的 P1/P2 缺口，做完即可正式宣告 v3.1 交付，不再有「还差什么」的长期悬置
2. **先清债再干活**——TD-1 是目前最大的噪声源（`test_chat_service_state.py` 9 例假红，已跨三任务反复挂账），TD-5 是**安全**项（路由无鉴权）。不清掉它们，A2–A4 的验证都要在噪声里做
3. **工期最短、价值/成本比最高**（~2 周 vs B 的 2.5 周+）
4. **B/C 在 A 之后基础更好**——A 做完，B 的 Row/Col Security 有干净的测试基线，C 的预研有明确的「v3.1 已完成」起点

---

## 五、明确不做（避免范围蔓延）

- 蓝图 §22.4 全部 📦 v3.x 项（含 Phase 3b/3c、Memory Phase C、Analysis DSL / Report DSL、Authority 完整模型、Evidence 5+ 类型、Knowledge Compiler 8 产物）
- TD-2 / TD-4 / TD-6 / TD-7 / TD-9 / TD-10 / TD-14 / TD-15（P2/P3，除 A1 打包的 P3 卫生项外，其余留待专项批次）
- TD-10 需**先确认设计意图**（L1 命中落两条 evidence 是否为「原始 SQL + 语义指标」成对设计），确认前不动
- 任何对 SQL Guard / planner 执行缝 / nl2sql_engine / business_db_pool 的改动（v3.1 全程红线）

---

## 六、待评估的决策点

1. **是否采用方案 A**？若采用，A1 的 TD-1 是否要与 A2–A4 同批，还是**单独先出一个 TD-1+TD-5 的小批次**（更快见效，且后续任务都在干净基线上）？
2. **A3 通道 2 的边界**——LLM 语义兜底是「只做意图分派」还是「含语义映射（自然语言 → 语义对象）」？后者更接近 §5.2 的终态，但工期翻倍且与 A4 有耦合。
3. **A4 是否全做**——只做「2-3 步模板化」即可（0.3 周），还是连「4-5 步 LLM + 模板校验」一起（0.5 周）？
4. **R4 Row/Col Security 的排期**——若合规/安全有硬要求，应提前到 MB4 内（则会与方案 B 合并）。
5. **分支策略**——MB3 期间是「epic 领跑 + 批次末 FF main」。MB4 是否继续沿用，还是改为「feature 分支直接合 main」？（现 epic 与 main 已同点，继续维护双分支的收益下降）
