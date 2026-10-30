# AIChat 优先级修复 — 执行路线图

> **配套 plan 文档**：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md`（技术细节：每个 Task 的 RED → GREEN → step-by-step）
>
> **本文件焦点**：派发策略、时间线、Gate、协调点、回滚预案。

**Goal:** 按"P0 + P1 → P2"两阶段派发 P0 (4 任务) + P1 (5 任务) + P2 (1 任务) 共 10 个任务，每个任务 TDD 闭环。

**Strategy:** P0 同分支串行合入；P1 五项独立分支并行友好；P2 在 P0+P1 全部合入后启动。

---

## 全局时间线（推荐工期估算）

```
Week 1       Week 2-3           Week 4-5          Week 6-7         Week 8
  │             │                  │                  │               │
  ├─ Phase A ──┤── Phase B (P0) ──┤── Phase C (P1) ──┤── Phase C 续 →─┤── Phase D (P2) ─┤
  │  准备      │  4 任务串行      │  5 任务并行      │  整合 + 回归   │  1 任务          │
  │  分支/协调 │  一个 PR | p0    │  5 个 PR | p1-*  │               │  一个 PR | p2    │
```

**工期估算依据**：
- P0 4 任务每个 ~2-3 工作日（含 alembic 迁移 / 前后端联动 / 回归）
- P1 5 任务每个 ~3-5 工作日（部分任务如 P1-3 / P1-4 含前端 / 多步架构变更）
- P2 1 任务 ~2-3 工作日

**关键路径**：
- P0-3 alembic 迁移（必须先于其他 P0 任务的回归）
- P1-3 + P1-4 + P1-5 含前端 / 多步 / 假设三个独立模块，可并行
- P2 依赖 P0 + P1 全部合入后才能稳态运行

---

## Phase A — 准备阶段（Week 1）

### A.1 协调 & 占号

**目标:** 弄清同期进行中的工作、避免 alembic 冲突、与 Person A/B 协调。

- [ ] **A.1.1 — 盘点同期活动**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
# 1. 检查最近 7 天的 alembic 提交
git log --oneline --all --since="7 days ago" -- backend/alembic/versions/ | head -20
# 2. 检查 Person A/B 正在做的事
gh issue list --state open --label "in-progress" 2>/dev/null || echo "no gh access"
# 3. 检查 worktrees
git worktree list
ls .worktrees/
```

- [ ] **A.1.2 — 锁定 alembic 0118 占号**

```bash
ls backend/alembic/versions/ | sort -r | head -10
# 找到当前 head revision（如 0117_*）
# 本计划 P0-3 用 0118；如有冲突按既有的 renumber 流程处理（参考 qa-system-alembic-targets-prod）
```

- [ ] **A.1.3 — 同步信息给 Person A/B**

如 alembic 占号冲突 / 同时跑 Plan，需要在 conversation 中沟通：
- 是否需要合并 Person A 的 id_mapping 占号（参考 evidence-extension-b1 plan Task 5 协调）
- 与 Person B 是否有任何 race 风险

### A.2 切 P0 分支

- [ ] **A.2.1 — 切 feat/aichat-priority-p0**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git checkout main
git pull --ff-only origin main 2>/dev/null || echo "no upstream yet"
git checkout -b feat/aichat-priority-p0
git push -u origin feat/aichat-priority-p0
```

- [ ] **A.2.2 — 基线状态确认**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/ -v -m "not integration" --tb=no -q 2>&1 | tail -20
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/ -v -m integration --tb=no -q 2>&1 | tail -20
cd ../frontend && npm test -- --silent 2>&1 | tail -10
```

记录基线测试通过数。

---

## Phase B — P0 派发与实施（Week 2-3）

### B.0 派发策略

P0 4 任务在**同一分支** `feat/aichat-priority-p0` 上**串行**实施：

```
P0-1 (T2) → P0-2 (T3) → P0-3 (T4) → P0-4 (T5) → Final Verify (T12.1-12.5) → PR
```

**为何串行**：
- 减少 T3 → T4 → T5 之间 merge 冲突
- P0-3 alembic 迁移需最早做（其他 P0 任务回归要基于新 schema）
- 单个 PR 便于 code-review / 整体验证

**派发方式**：每任务用 1 个 sub-agent（`subagent-driven-development`），顺序执行。

### B.1 派发 Task 2 (P0-1) — 流式 KPI/L4 + KPI 用 dto

- **owner**: Backend Agent A
- **工期**: 2-3 工作日
- **plan 文档**: Task 2 (line 142-228)

**执行步骤**（按 plan 文档）：
- 2.1 写 failing integration tests（4 个）
- 2.2 RED 验证
- 2.3 抽 L1L4PreambleMixin
- 2.4 用 mixin 替换 chat_service.py:408-440
- 2.5 用 mixin 插入 chat_stream.py:154
- 2.6 GREEN 验证
- 2.7 回归测试
- 2.8 Commit

**Gate**:
- [ ] 4 个 integration tests 全部 GREEN
- [ ] chat_service.py 普通入口现有测试不回归
- [ ] `_runL1AndL4Preamble` 抽到 L1L4PreambleMixin（不在 ChatService 类体里堆代码）
- [ ] KPI mismatch 路径：datasource 不匹配时**显式** SSE 警告帧（不是静默）
- [ ] commit message 包含 "P0-1"

**回归必查**:
- ⚠️ "你好" 仍走 chitchat（不应触发 KPI/L4）
- ⚠️ 反向断言：mismatch 警告**必须**显式出现（SSE 帧含 `KPI_DATASOURCE_MISMATCH` 字面量）
- ⚠️ chat_service.py 行数：1969 → ~2020（仍在 2000 豁免边缘；下次升级必须拆）

### B.2 派发 Task 3 (P0-2) — 意图分类顺序 + 礼貌前缀

- **owner**: Backend Agent B
- **工期**: 1-2 工作日
- **plan 文档**: Task 3 (line 230-330)

**Gate**:
- [ ] 6 个 unit tests 全部 GREEN
- [ ] `test_pure_thanks_remains_chitchat` 仍 PASS（保持 0 LLM 友好交互）
- [ ] `test_compare_without_history_returns_clarify` 不生成猜测 SQL
- [ ] 现有 intent tests 不回归（用 `pytest app/tests/unit/test_intent_*.py` 跑全量）

**回归必查**:
- ⚠️ **LLM 调用次数**：分类顺序调整可能让一些原本 CHITCHAT 的问题改走 QUERY/REFINE → 监控 LLM 调用计数变化（应在 < 5% 范围）
- ⚠️ 短追问"去年呢"：必走 REFINE（带"时间维度继承"提示）
- ⚠️ "你好，请查询 X"：必剥除前缀（不带"问候"标签）

### B.3 派发 Task 4 (P0-3) — SessionQueryState 三元 + 前端 reset

- **owner**: Backend Agent C + Frontend Agent D（前后端联动）
- **工期**: 3-4 工作日（含 alembic 双烟）
- **plan 文档**: Task 4 (line 332-470)

**这是 Phase B 的关键路径**，因为：
1. alembic 0118 迁移必须双烟
2. 前端 chatStore reset 必须 wire 到 ChatPage
3. SessionQueryState 是 OOP 的关键改动

**执行步骤**（按 plan 文档）：
- 4.1 写 failing integration tests（3 个 backend + 1 个 frontend）
- 4.2 RED
- 4.3 写 alembic 0118 迁移
- 4.4 **双烟迁移**（`alembic upgrade head` → `alembic downgrade -1` → `alembic upgrade head`）
- 4.5 **生产 schema 烟测**（参考 `qa-system-alembic-targets-prod` 记忆）：
  ```bash
  # 先看 prod qa_metadata 表是否已存在新列（如果有 → STOP）
  psql "$DATABASE_URL" -c "\d session_query_state" 2>/dev/null | head -20
  # 否则跑迁移
  cd backend
  alembic upgrade head
  ```
- 4.6-4.9 实现 ORM / load / save / chat.py / 前端 chatStore
- 4.10 GREEN
- 4.11 Commit

**Gate**:
- [ ] alembic 0118 双向升级降级通过
- [ ] 3 个 backend integration tests + 1 个 frontend test 全部 GREEN
- [ ] 反向断言：B 拿 A sessionId + 不同 datasourceId → load 返 None
- [ ] 反向断言：切数据源时 chatStore.messages 清空、sessionId 重新分配、isStreaming=false
- [ ] prod schema 烟测（注意：参考 `qa-system-pg-wipe-incident` 教训，**绝不直接对 prod 跑迁移**——只在测试库验）

**回归必查**:
- ⚠️ 所有 chat 接口测试仍 PASS（session_message / 多步 / 假设 路径）
- ⚠️ 既有 chat_session_id 在新 schema 下仍可读（nullable 兼容历史）

### B.4 派发 Task 5 (P0-4) — 流式 SSE 编码器保真度 + distinct

- **owner**: Backend Agent E
- **工期**: 1-2 工作日
- **plan 文档**: Task 5 (line 472-590)

**Gate**:
- [ ] 5 个 stream_events_safe 测试 + 3 个 data_summary_distinct 测试 GREEN
- [ ] 反向断言：JSON parse 不会因 NaN/Infinity 抛错（标准 JSON.parse 兼容）
- [ ] Decimal / UUID / date 序列化保真（保留精度 / ISO 字符串）
- [ ] 现有 SSE 路径不回归（用既有 chat 流测试 + 手动 smoke）

### B.5 P0 Final Verification（Plan Task 12.1-12.5）

**owner**: 全员 / Exec

- [ ] **B.5.1 — 覆盖率 gate**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/unit/ -v -m unit \
    --cov=app.services.chat_service \
    --cov=app.services.chat_stream \
    --cov=app.services.intent_service \
    --cov=app.services.stream_events \
    --cov=app.services.data_summary \
    --cov-report=term-missing
```

Expected: ≥ 80% on touched modules.

- [ ] **B.5.2 — 集成测试 gate**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/ -v -m integration
```

- [ ] **B.5.3 — 前端测试 gate**

```bash
cd frontend
npm test -- --coverage
```

- [ ] **B.5.4 — security-reviewer dispatch

派发 security-reviewer agent 审查：
- backend/app/services/chat_service.py
- backend/app/services/chat_stream.py
- backend/app/services/chat_context.py
- backend/app/services/intent_service.py
- backend/app/api/v1/chat.py
- backend/app/services/stream_events.py
- backend/app/domain/models.py
- frontend/src/stores/chatStore.ts

**重点检查**：
- [ ] assertSessionOwnership 守卫在所有 chat 写入路径仍生效（chat.py:74, 99, 193, 261）
- [ ] P0-3 三元 UniqueConstraint 真正生效（不能被 nullable 旁路）
- [ ] P0-2 礼貌前缀剥除后无信息泄露（拒绝注入）
- [ ] P0-4 `_jsonSafe` 不泄露 Decimal 内部表示
- [ ] assistantMessageId 不被未授权用户跨会话访问

- [ ] **B.5.5 — code-reviewer dispatch

派发 code-reviewer agent 审查：
- chat_service.py 行数（2069 行上限检查；超 2000 行强制拆分）
- mixin 顺序正确（L1L4PreambleMixin 在最末）
- immutability（不原地修改 ORM）
- alembic 迁移文件不丢 downgrade

### B.6 P0 PR 合入

- [ ] **B.6.1 — push + 开 PR**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git push origin feat/aichat-priority-p0
gh pr create \
  --base main \
  --head feat/aichat-priority-p0 \
  --title "feat(chat): P0 — AIChat Service 优先级修复" \
  --body-file <(cat <<'EOF'
[Paste PR body from plan 文档 Task 12.6]
EOF
)
```

- [ ] **B.6.2 — 等 CI + code-review**

PR review gate 全部通过后合入 main。

- [ ] **B.6.3 — 部署监控指标**：
  - LLM 调用次数变化（应 < 5% 波动）
  - SSE 错误率（应下降）
  - chat latency p50/p95
  - 跨用户窃取测试（应无）

---

## Phase C — P1 派发与实施（Week 4-5）

### C.0 派发策略

P1 5 任务在**各自独立分支**上**并行友好**：

```
feat/aichat-p1-1 (SQL 改写)    ─┐
feat/aichat-p1-2 (模型路由)    ─┤
feat/aichat-p1-3 (流式守卫)   ─┼── 可并行启动 → 按依赖顺序合入
feat/aichat-p1-4 (多步依赖)   ─┤
feat/aichat-p1-5 (假设验证)   ─┘
```

**为何并行**：
- 5 任务之间无强依赖（除 P1-3 与 P0-3 chatStore 改动有部分重叠但 P0-3 已合 main，P1-3 可独立走）
- sqlglot 依赖已就绪（P1-1）
- 多步依赖是纯算法层（P1-4）
- 假设验证是独立模块（P1-5）

**合入顺序建议**（按风险与耦合度）：
```
P1-1 → P1-2 → P1-3 → P1-4 → P1-5
       ↑
       必须最先合（其他任务可能复用 sqlglot）
```

**派发方式**：每任务 1 个 sub-agent，并行启动（参考 superpowers:dispatching-parallel-agents）。

### C.1 派发 Task 6 (P1-1) — SQL 改写 AST 化

- **owner**: Backend Agent F
- **分支**: `feat/aichat-p1-1`
- **工期**: 2-3 工作日
- **plan 文档**: Task 6 (line 592-870)

**Gate**:
- [ ] 5 个 quote-aware tests GREEN
- [ ] 反向断言：`note = 'LIMIT 10'` 不被改（quote-aware 必生效）
- [ ] 嵌套 SQL / CTE / window 自动 fallback（不瞎改）
- [ ] 现有 nl2sql_refine tests 不回归
- [ ] 既有 ANSI / Oracle / MySQL / PostgreSQL 方言兼容（dialect registry 已就绪）

**风险**：
- ⚠️ sqlglot 对一些边界 SQL 解析失败 → 返 None 让两阶段生成接手（已有 fallback）

### C.2 派发 Task 7 (P1-2) — 模型能力路由 + usage 双收

- **owner**: Backend Agent G
- **分支**: `feat/aichat-p1-2`
- **工期**: 2-3 工作日
- **plan 文档**: Task 7 (line 872-1010)

**Gate**:
- [ ] 3 个 capability filter 测试 GREEN
- [ ] 反向断言：tools 任务不路由到 Ollama（前置过滤掉非兼容）
- [ ] usage+content 同块双收（流式文本与 token 同时累加）
- [ ] cost_source 显式分类（不允许 0 = 免费）

**风险**：
- ⚠️ selectModel 改签名 → 所有调用方需同步；grep `selectModel` 确认

### C.3 派发 Task 8 (P1-3) — 流式 assistantMessageId + CRLF

- **owner**: Frontend Agent H
- **分支**: `feat/aichat-p1-3`
- **工期**: 3-4 工作日
- **plan 文档**: Task 8 (line 1012-1140)

**Gate**:
- [ ] 3 个 chatStore tests + 2 个 streamChunkParser tests GREEN
- [ ] 反向断言：旧 assistantMessageId 的事件被丢弃（不污染新会话）
- [ ] CRLF 跨块保留（`\r` 在 chunk N、`\n` 在 chunk N+1 → token 不丢）
- [ ] EOF 无 done/error → interrupted=true（不再误判成功）

**风险**：
- ⚠️ 与 P0-3 chatStore reset 改动有连锁（但 P0-3 已合 main，P1-3 可独立走）
- ⚠️ 事件路由按 `(runId, assistantMessageId)` 丢弃：需保证 type 系统一致（参考 `qa-system-resolutioninflight-page-wiring`）

### C.4 派发 Task 9 (P1-4) — 多步依赖显式

- **owner**: Backend Agent I
- **分支**: `feat/aichat-p1-4`
- **工期**: 3-4 工作日
- **plan 文档**: Task 9 (line 1142-1210)

**Gate**:
- [ ] 多步依赖测试 GREEN（至少 6 个）
- [ ] 反向断言：依赖失败下游 mark `skipped`（不是 `failed`）
- [ ] result_set_refs 跨步复用（prompt 不含大集合）
- [ ] 同一 AsyncSession 不并发（独立并发会污染事务）

**风险**：
- ⚠️ multi_step_plan.py 是 chat_service.py 最大的依赖文件之一；改 dataclass 字段需 grep 所有调用点
- ⚠️ `entity_set_ref` 跨步传递：需保证执行层能做集合操作（不依赖 LLM）

### C.5 派发 Task 10 (P1-5) — 假设验证 + 证据回放

- **owner**: Backend Agent J
- **分支**: `feat/aichat-p1-5`
- **工期**: 3-4 工作日
- **plan 文档**: Task 10 (line 1212-1320)

**Gate**:
- [ ] 3 个 verify 测试 GREEN
- [ ] 反向断言：support_count=0 不被标 '已验证'（最严重的"假绿"修复）
- [ ] 验证 SQL 失败 → execution_failed（与 inconclusive 区分）
- [ ] TurnResult 持久化 + schema 字段（result_ref / chart_config_ref / step_results / evidence_refs / schema_version）
- [ ] 历史回放按 result_ref 拉数据完整（图表/数据/步骤）

**风险**：
- ⚠️ hypothesis_service.py 与多步链路耦合（hypothesis 是多步后的可选步骤）；改 verify 流程需 grep 多步链路
- ⚠️ TurnResult 表若新增 → 又一次 alembic 迁移（占号 0119）

### C.6 P1 Final Verification（Plan Task 12.1-12.5 应用到 P1）

**owner**: 全员 / Exec

每个 P1 PR 合入前：
- [ ] 该任务的 unit + integration tests 全 GREEN
- [ ] 覆盖率 ≥ 80% on touched modules
- [ ] security-reviewer 审查（重点：API 兼容、不在 P0-3 三元约束上开新窗口）
- [ ] code-reviewer 审查（重点：immutability、backward compat）

### C.7 P1 PR 合入

按顺序合入（推荐 P1-1 → P1-2 → P1-3 → P1-4 → P1-5）：

- [ ] **C.7.1 — 每个 PR 的合并流程**

```bash
# 每个 PR：
git push origin feat/aichat-p1-{n}
gh pr create --base main --head feat/aichat-p1-{n} --title "..." --body "..."
# 等 CI + review 全过
gh pr merge feat/aichat-p1-{n} --squash
```

- [ ] **C.7.2 — 每合一个跑回归**

```bash
# 合入后立即跑：
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/ -v -m "not integration"
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/ -v -m integration
cd frontend && npm test
```

- [ ] **C.7.3 — 部署监控指标**：
  - LLM 调用次数（应变化不大）
  - 多步成功率（应上升）
  - 假设支持/反驳判定正确率（应有 metrics 监控）
  - 流式 interrupted 比例（应下降）

---

## Phase E — P0 + P1 整合验证（Week 7）

### E.1 端到端集成验证

P0 + P1 全部合入后，跑完整业务场景套件：

- [ ] **E.1.1 — 业务场景回归**（参考 plan 文档建议的固定业务验证集）：
  - 单表 / 多表查询
  - 绝对和相对时间
  - 中文 / 英文 / 混合词
  - Top N / 逐组 Top N
  - 空结果
  - JSON / 数组 / Decimal / 日期
  - 同名实体
  - 不同数据源 / 模型
  - 短追问
  - 部分失败
  - 取消和并发
  - 断连和恢复

- [ ] **E.1.2 — 跟踪指标采集基线**：
  - 答案正确率（业务口径，不是 API 200）
  - 子目标覆盖率
  - 范围继承正确率
  - 流式 / 普通一致率
  - 未支持能力的误路由率
  - 断连识别率
  - 首事件 / 首答案 / 整体延迟
  - 每轮真实成本
  - unknown 用量比例

### E.2 chat_service.py 行数 gate

- [ ] **E.2.1 — 行数检查**

```bash
wc -l backend/app/services/chat_service.py
```

Expected: ≤ 2000 行（豁免边缘）；若超 → 立即触发 N1 拆分任务

---

## Phase D — P2 派发与实施（Week 8）

### D.0 派发策略

P2 1 任务在**独立分支** `feat/aichat-priority-p2` 上实施：

```
P2-1 (T11) → Final Verify → PR
```

**派发方式**：1 个 sub-agent。

### D.1 派发 Task 11 (P2-1) — 错误分类 + 整轮 deadline

- **owner**: Backend Agent K
- **分支**: `feat/aichat-priority-p2`
- **工期**: 2-3 工作日
- **plan 文档**: Task 11 (line 1322-1390)

**前置条件**：P0 + P1 全部合入 main，且 Phase E 整合验证通过。

**Gate**:
- [ ] 7 个 error category tests GREEN
- [ ] deadline propagation tests 7 个 GREEN
- [ ] 现有错误处理路径不回归（既有 chat tests + smoke）

### D.2 P2 Final Verification

- [ ] **D.2.1 — 覆盖率 gate**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/unit/ -v -m unit \
    --cov=app.services.stream_events \
    --cov=app.services.chat_stream_output \
    --cov=app.services.chat_service \
    --cov=app.services.chat_usage \
    --cov-report=term-missing
```

Expected: ≥ 80% on touched modules.

- [ ] **D.2.2 — security-reviewer + code-reviewer dispatch

- [ ] **D.2.3 — PR 合入**

### D.3 部署监控指标

- 错误分类命中率（按 7 类应有分布）
- deadline 超时比例（应有上界）
- retry 次数下降（应比修前少）

---

## Gate 汇总表

| Phase | Gate | 验证方式 | 通过条件 |
|---|---|---|---|
| A | 分支干净 + alembic 占号 | git status / `ls alembic/` | 无冲突占号 |
| B.1 | P0-1 KPI/L4 流式 | integration test | 4 个绿灯 ✓ |
| B.2 | P0-2 意图顺序 | unit test | 6 个绿灯 + LLM 调用次数 < 5% 波动 |
| B.3 | P0-3 SessionQueryState | integration + alembic 双烟 | 3+1 绿灯 + 双向升级降级 ✓ |
| B.4 | P0-4 序列化 | unit test | 5+3 绿灯 + JSON parse 不抛错 |
| B.5 | P0 整合回归 | coverage + security + review | ≥ 80% + 无 CRITICAL/HIGH |
| B.6 | P0 PR 合入 | CI + review | 合入成功 + 部署指标正常 |
| C | P1 5 任务并行 | 每任务 unit/integration + 回归 | 每任务绿灯 + 合入后无回归 |
| E | P0+P1 整合 | 业务场景套件 + 指标基线 | 全部场景通过 + 指标采集完成 |
| D | P2 错误分类 | unit + review | 7+7 绿灯 + 合入 PR |

---

## 协调点（必须沟通）

### 与 Person A（id_mapping 链路）

- alembic 0118 占号是否冲突
- chat_session.py / chat_context.py 改动是否会破坏 id_mapping 链路
- 同步时机：在 Phase A.1.3 沟通

### 与 Person B（其他并行工作）

- 是否同时跑 Plan
- 同期是否有 chat 相关回流
- 同步时机：在 Phase A.1.3 沟通

### 与 ops / DBA

- P0-3 alembic 0118 部署窗口
- 双烟验证流程（参考 `qa-system-alembic-targets-prod` 记忆）
- 同步时机：在 Phase B.3 派发前 1 天

---

## 回滚预案

### P0-3 alembic 迁移回滚

```bash
cd backend
alembic downgrade -1  # 回滚 0118
# 若失败：手动 DROP COLUMN (datasource_id / schema_name / user_id / datasource_version)
# 注意：UniqueConstraint 回滚可能丢失索引（grep `uq_session_query_state`）
```

### 单个 P1 任务回滚

```bash
git revert <commit_sha>
# 或
git reset --hard HEAD~1  # 强制回滚（需要 PR review）
```

### 整体回滚（极端情况）

P0 + P1 全部合入后发现严重问题：
1. revert PR 合入顺序的逆序（P1-5 → P1-1 → P0）
2. alembic downgrade -1 链回滚（按合入逆序）
3. chat_service.py 行数豁免撤销（拆分任务）

---

## Self-Review Checklist

**Phase 覆盖:**
- [x] Phase A 准备（占号 + 分支 + 协调） → A.1, A.2
- [x] Phase B P0 串行（4 任务 + 整合 + PR）→ B.1-B.6
- [x] Phase C P1 并行（5 任务 + PR + 回归）→ C.1-C.7
- [x] Phase E P0+P1 整合验证 → E.1, E.2
- [x] Phase D P2 独立（1 任务 + 整合 + PR）→ D.1-D.3

**协调点覆盖:**
- [x] 与 Person A（id_mapping / alembic 占号）→ 协调点 1
- [x] 与 Person B（其他并行工作）→ 协调点 2
- [x] 与 ops/DBA（alembic 部署窗口 + 双烟）→ 协调点 3

**Gate 覆盖:**
- [x] 每个 Phase 都有明确 Gate 任务（不靠描述，靠具体验证命令）
- [x] 每个任务的反向断言都明确（"是谁拒的"必须可观测）

**回滚覆盖:**
- [x] alembic 单点回滚
- [x] 单个 P1 任务回滚（git revert / reset）
- [x] 整体回滚（P0+P1+P2 逆序回滚 + alembic 链回滚）

**风险标注:**
- [x] P0-3 是关键路径（schema 迁移 + 前端连锁）→ 标注
- [x] P1-5 hypothesis_service 与多步链路耦合 → 标注
- [x] LLM 调用次数监控（P0-2 可能上升）→ 标注
- [x] P1-2 selectModel 改签名 → 标注调用方需 grep