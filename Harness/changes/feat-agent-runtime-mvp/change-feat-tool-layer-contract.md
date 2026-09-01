# 变更：feat-agent-tool-layer-contract

- **日期**：2026-09-01
- **阶段**：Phase 7 G6（Agent Runtime 遗留缺口）
- **类型**：功能补强（数据契约 / 防漂移）
- **提交**：`feat: agent tool data_layers contract validation`

---

## 1. 问题

`AgentTool.data_layers` 是人工注解，运行时 `_enforcePolicies` 用它与 `AgentAccessPolicy.data_layer` 做精确比较。缺少防漂移机制：

- 新增工具时可能写错大小写（如 `feature` / `Dim`），导致授权静默 fail-closed（403）或漏判；
- 工具声明与实际读取的数据源不一致无人发现（如 supplier_risk 开始读 FEATURE 但只声明 DIM）；
- `data_object` 同理，坏格式会破坏 deny-by-default 授权语义。

`feat-agent-runtime-mvp` §10 列为已知缺口；Phase 7 gap plan G6（P1）。

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 校验时机 | 注册期（`AgentToolRegistry.register`）校验，构建完成后 `validate()` 兜底 | 尽早报错阻止坏注册；内置注册表构建时自检一次，无每请求开销 |
| 校验规则 | `data_object` 非空全大写；`data_layers` 元素非空全大写 | 运行时与归一化后的策略层做精确比较，大小写不一致即授权失真 |
| 层无关工具 | `data_layers=()` 合法（回退对象粒度） | 与 Phase 6.4 旧行为完全兼容 |
| 契约测试 | 单测（格式 + 声明契约 + 图实体分类）+ 集成（SQL 追踪实际触达表） | 集成用真实 PG + `before_cursor_execute` 追踪，验证「声明层 == 实际读取层」 |
| 启动自检 | 不在 `AgentRuntimeService.__init__` 重复校验 | register 已保证内置 registry 不变式，每请求再校验纯属浪费 |

## 3. 数据模型变更

无 DB 迁移、无 schema 变更。

## 4. 关键代码

### 后端修改

- `app/services/agent_tools.py`：
  - `AgentToolRegistry.register` 调 `_validate`：`data_object` / `data_layers` 元素非空全大写，违规抛 `ValueError`；
  - 新增 `validate()` 全量自检（防御外部直接改 `_tools`）；
  - `_buildRegistry()` 末尾 `registry.validate()` 构建自检。
- `app/tests/unit/test_agent_tool_layer_contract.py`（NEW）：
  - 格式校验 9 例（小写/混合/空/空白 data_object 或 layer 拒绝；合法大写通过；层无关空 tuple 通过；validate 兜底）；
  - 内置注册表声明契约（supplier_360/risk → DIM+FEATURE，graph_traverse → DIM+DWD）；
  - 图实体分类：`BUSINESS_ENTITY_LABELS` 的 Supplier/Material（DIM）∪ 单据类（DWD）== 全集。
- `app/tests/integration/test_agent_tool_layer_contract.py`（NEW，真实 PG）：
  - `before_cursor_execute` 追踪 handler 实际触达的表 → 映射层，断言与声明一致；
  - supplier_360 / supplier_risk 均触达 `entity_mapping` + `feature_definition` + `feature_value` → {DIM, FEATURE}；
  - 两工具声明层集合必须一致（risk 复用 get360）。

### 偏离计划点

- 计划把契约测试放 `tests/unit/`；SQL 追踪需要真实 PG，按项目测试规范放入 `tests/integration/`（单测只保留格式 + 声明契约 + 图分类）。
- 未实现「`data_layers=()` 需附带 `# layer-agnostic:` 注释的 CI lint 扫描」——运行时无法校验注释，lint 扫描收益低，留作约定而非强制。

## 5. 测试

| 层 | 文件 | 结果 |
|---|---|---|
| 单元 | `test_agent_tool_layer_contract.py` | 11 passed |
| 集成 | `test_agent_tool_layer_contract.py` | 3 passed |
| 回归 | `test_agent_tool_registry.py` / `test_agent_runtime_service.py` / `test_agent_runtime_api.py` | 全部通过 |
| 全量后端 | `app/tests/ --cov=app` | 1861 passed, 1 skipped，覆盖率 93.22%（G2 时基线；G6 为纯增量） |

## 6. 安全审查

`code-reviewer` + `security-reviewer` 双审，无 CRITICAL / HIGH：

- **安全正向**（security-reviewer）：`data_object` / `data_layers` 大写校验是 fail-closed，
  并实际关闭一个 fail-open 缺口——工具侧小写 layer 会绕过 `FORBIDDEN` 精确匹配
  （策略侧已归一化大写，工具侧未归一化），被通配 READ 放行；注册期强制大写消除该路径。
- **MEDIUM 修复**（code-reviewer）：集成契约测试原用 `if t in _TABLE_LAYER` 过滤触达表，
  会静默丢弃未映射新表（「开始读新表但声明没跟上」检测失效）；改为
  `assert set(tables) <= set(_TABLE_LAYER)` 强校验无未声明表。
- `_enforcePolicies` 行为零变化（格式合规的现有 3 工具不受影响）；
- 无新增依赖、无外部调用、无密钥。

## 7. 验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_agent_tool_layer_contract.py \
    app/tests/integration/test_agent_tool_layer_contract.py \
    app/tests/unit/test_agent_tool_registry.py \
    app/tests/unit/test_agent_runtime_service.py \
    app/tests/integration/test_agent_runtime_api.py -v
```

期望：15 契约用例 + 既有 agent 用例全绿。

## 8. 关键文件清单

- `backend/app/services/agent_tools.py`
- `backend/app/tests/unit/test_agent_tool_layer_contract.py`
- `backend/app/tests/integration/test_agent_tool_layer_contract.py`
