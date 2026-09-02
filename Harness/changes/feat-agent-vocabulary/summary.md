# feat-agent-vocabulary

> 日期：2026-09-02 | 状态：done | Spec: docs/superpowers/specs/2026-09-02-agent-vocabulary-design.md
> Plan: docs/superpowers/plans/2026-09-02-agent-vocabulary.md
> SDD ledger: .superpowers/sdd/2026-09-02-agent-vocabulary/progress.md

## 目标

锁定 `AgentDefinition.data_domains` / `data_layers` 词表（SSOT 在代码常量，零 DB 迁移），
前端从自由输入改成约束多选 + 列表页按域过滤。

## 实现

- `app/domain/agent_vocabulary.py`（新）：`AGENT_DATA_DOMAINS`、`AGENT_DATA_LAYERS` 常量 + `normalizeAgentDomain`
- `AgentDefinitionCreate/Update` 加 `field_validator`：归一化（strip+upper）+ 词表校验 + 去重保序
- 两条 `MSG_AGENT_*_NOT_IN_VOCAB` 错误消息（`error_messages.py` Phase 6.5 段后追加）
- `GET /api/v1/agents/options` 新端点（路由必须在 `/{agent_code}` 之前注册——集成测试守护）
- `listAgents` 扩展 `data_layer`（JSONB contains）+ `data_domains: list[str]`（JSONB overlap OR 语义）
- 前端 `getAgentOptions()` API client + `useAgentOptions()` hook（模块级 cache + inflight dedup）
- AgentRegistryPage 4 处 Select 改造（Create/Edit Modal × 2 fields + Policy sub-table 1 field 单值）
- 列表页加 `filterDomains` 多选下拉 → `?dataDomain=A&dataDomain=B`
- i18n: zh-CN / en-US 新增 6 keys（agentOptions + agentRegistry.filter + agentRegistry.fields）
- 前端依赖：`qs@^6.11.0` + `@types/qs@^6.11.0`（显式 paramsSerializer 保证 `?dataDomain=A&dataDomain=B` 序列化）

## 验证

### 单测
- `backend/app/tests/unit/test_agent_vocabulary.py`：14 用例（10 来自 brief + 2 TestConstants + 2 bonus dedup/normalize）
- `frontend/src/tests/useAgentOptions.test.tsx`：2 用例（初次拉取 / 失败文案）

### 集成
- `backend/app/tests/integration/test_agent_options_api.py`：2 用例（含路由顺序守护 `/options` 不被 `/{agent_code}` 吞）
- `backend/app/tests/integration/test_agent_registry_api.py`：3 新过滤用例（dataLayer 单值、dataDomain 多值、status+layer 组合） + 13 既有用例全绿

### 全量
- backend: `pytest app/tests/ --cov=app --cov-fail-under=80` ≥ 80% 通过
- frontend: `npx tsc --noEmit` + `npx vitest run` 427 用例全绿

### e2e
- `GET /agents/options` 返回 `{"domains":[...],"layers":[...]}` 200
- 合法域写入 201
- 非法域写入 422 + 合法集错误消息
- `GET /agents?dataDomain=A&dataDomain=B` 多值过滤生效（OR 语义）

## 关键决策

- **词表走代码常量**（域 3 个、层 3 个，规模未到需要字典表，YAGNI）
- **严格 422 拒绝**非法值；不引入新 RequestValidationError handler（YAGNI，与项目错误信封不一致的取舍）
- **不迁移历史不规范行**——继续读、继续展示；只新写入校验（可独立发清理脚本）
- **`dataDomain` 同名多值绑定**（FastAPI Query 重复同名聚合为 `list[str]`），不新增参数名
- **`AgentOptionsRead` 字段名用 `domains`/`layers`**（顶层 options，无 `data_` 前缀）——Task 1 实施时短暂误用 `dataDomains/dataLayers`，
  已在 fix commit `b957294` 回退到 plan 契约
- **JSONB overlap unavailable** — SQLAlchemy 2.0.50 JSONB comparator 无 `.overlap()`，改用 `or_(*[contains([d]) for d in data_domains])`
  实现等价 OR 语义（flat-string-array 下与 `&&` 完全一致）
- **`qs` 显式 paramsSerializer** — axios 默认序列化虽正确，但显式 `arrayFormat: "repeat"` 避免未来 axios 版本漂移风险

## 与大改动（Agent 工具绑定可配置化）的边界

- 本期**不暴露**工具列表；`useAgentOptions()` 形态可演化为 `useAgentMetadata()`，接口预留 `tools` 字段
- 工具绑定仍硬编码于 `AGENT_TOOLS`（`agent_tools.py:287`），大改动把 `Agent→Tool` 关系入 DB
- 工具 `handler/data_object/data_layers` 定义留代码（安全关键，不入 DB）
- 两改动解耦：无单向依赖，大改动可独立排期

## 已知遗留

- **Agent `data_layer` 策略粒度未生效**（来自 `Harness/changes/feat-agent-runtime-mvp/summary.md §10` LOW#3）：
  `AgentAccessPolicy.data_layer` 字段已存在但运行时未校验。独立 change 修复（参见
  `Harness/changes/feat-agent-runtime-mvp/summary.md` 后续优化）。本期词表治理为该修复铺路
  （`AGENT_DATA_LAYERS` 词表与 tool 声明的 `data_layers` 已强制对齐）。
- **`stale docstring`**：agent_registry_service.py:91 docstring 仍写 `JSONB && 重叠（OR 语义）`，
  实际实现是 `contains + OR`——trivial 后续清理项。
- **`refresh` useCallback dep array** 省略 `domains`/`layers`（vocab 静态 + cache 稳定，无害）。

## Commits

```
b957294 fix(agent): align AgentOptionsRead field names to plan (domains/layers)
0f47fda feat(agent): extend listAgents with dataLayer + multi-value dataDomain
f375f73 feat(frontend): Agent Registry page uses vocabulary-bounded Selects
da84e7d feat(frontend): agentOptions API client + useAgentOptions hook
c5f2b6c feat(agent): GET /agents/options endpoint
2fdb5e2 feat(agent): vocabulary SSOT + DTO write validation
bc20c6d docs(spec): Agent 词表治理（feat-agent-vocabulary）设计方案
```
