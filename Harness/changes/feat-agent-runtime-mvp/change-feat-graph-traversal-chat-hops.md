# 变更：feat-graph-traversal-chat-hops

- **日期**：2026-09-01
- **阶段**：Phase 7 G3（Agent Runtime 遗留缺口）
- **类型**：功能补强（Chat 图遍历支持 >2-hop）
- **提交**：`feat: chat graph traversal supports >2-hop`

---

## 1. 问题

`GraphTraversalService.traverseForChat` 硬编码 `_CHAT_DEFAULT_HOPS = 2`，
Chat 图推理问法无法按用户意图加深遍历。用户问「供应商 100001 的 3 跳关联」
期望比默认 2 跳更深的链路，当前被强制 2 跳。

`feat-agent-runtime-mvp` §10 列为已知缺口；Phase 7 gap plan G3（P2）。

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 跳数来源 | 问句口语短语（`_GRAPH_HOP_RE`）→ `IntentResult.max_hops` | 复用既有意图管线，不新增 API 参数；chat 口语句式无需查询参数 |
| 跳数范围语义 | 未指名 → None（服务层默认 2）；越界（>5 / ≤0）→ 服务层 clamp 到 [1,5] | 与 API 层 `Query(ge=1, le=5)` 直接拒绝不同，chat 里「10 跳」是用户不了解上限的口语，clamp 比 4xx/500 更友好 |
| 正则独立 | 跳数解析独立于 `_SUPPLIER_GRAPH_RE`，由 GRAPH_REASONING 意图把关 | 图意图关键词（涉及/关联…）仍决定是否走图推理；「深度 5」单独出现是普通查询，不被吸走 |
| clamp 归属 | `resolveChatMaxHops` 放 `graph_traversal_service`（跳数常量同源） | 与 `_CHAT_DEFAULT_HOPS` / `_MAX_TRAVERSAL_HOPS` 相邻，单测直接覆盖 |
| 前端 | 无改动（`GraphTraversalCard` 已渲染 `maxHops` Tag） | 后端返回 `GraphTraversalRead.max_hops` 已透传，卡片展示已存在 |
| Agent tool 路径 | `graph_traverse` 工具保持默认 2 跳（范围外，见 §6） | G3 聚焦 chat 拦截路径；工具路径跳数解析留作后续 |

## 3. 数据模型变更

无 DB 迁移、无 schema 变更。`GraphTraversalRead.max_hops`（既有字段）透传实际跳数。

## 4. 关键代码

### 后端修改

- `app/services/intent_service.py`：
  - 新增 `_GRAPH_HOP_RE`：匹配「N 跳 / N 层 / N 度 / 深度 N / 最多 N 跳」，
    支持阿拉伯数字（1-2 位）与中文数字（一~九）；
  - 新增模块级 `extractGraphMaxHops(message) -> int | None`：未命中返回 None，
    越界值原样返回（clamp 在 service 层）；
  - `IntentResult` 新增 `max_hops: int | None = None`；
  - `classifyResult` 的 GRAPH_REASONING 分支填充 `max_hops=extractGraphMaxHops(original)`。
- `app/services/graph_traversal_service.py`：新增 `resolveChatMaxHops(max_hops)`：
  `None → _CHAT_DEFAULT_HOPS(2)`，越界 `max(1, min(x, _MAX_TRAVERSAL_HOPS))`。
- `app/services/chat_service.py::_handleGraphReasoning`：`traverseForChat(key)` →
  `traverse("Supplier", key, resolveChatMaxHops(result.max_hops))`；
  clamp 后必然通过 `traverse` 的 `1..5` 校验，不会因越界抛 ValueError。

### 偏离计划点

- 计划把跳数捕获组并入 `_SUPPLIER_GRAPH_RE`；实现改为**独立正则 + 意图把关**，
  避免在 3 分支复合正则上叠加捕获组（可读性/回归风险），且天然保证
  「非图问法含 N 跳也不消费」。
- 计划单测「供应商 100001 深度 5 → max_hops=5」；实现验证该句**无图推理关键词**
  实为普通查询（`test_depth_alone_not_hijacked`），改为「供应商 100001 深度 5 的关联」。
  行为不变：跳数短语必须伴随图意图关键词才生效。

## 5. 测试

| 层 | 文件 | 结果 |
|---|---|---|
| 单元 | `test_graph_traversal_service.py`（新增跳数解析 + clamp） | 14 新增全绿（意图 8 + resolveChatMaxHops 5 + 回归 1） |
| 单元 | `test_intent_service.py` 回归 | 通过 |
| 集成 | `test_graph_traversal_api.py`（新增 3-hop chat 用例） | 11 passed |
| 回归 | `test_chat_api.py` / `test_agent_runtime_api.py` | 23 passed |

`resolveChatMaxHops` 覆盖：None→2 / 3→3 / 10→5 / 0→1 / -3→1。
意图覆盖：3 跳→3 / 深度 5 的关联→5 / 最多 3 跳→3 / 三跳→3 / 两跳→2 / 十跳→10
（clamp 在 service）/ 10 跳→原始 10 / 聚合量词兜底（含跳数问法）不被吸走 /
深度单独出现不吸走。

## 6. 安全审查

`code-reviewer` + `security-reviewer` 双审 **APPROVE**（0 CRITICAL / 0 HIGH）：

- **无界遍历**（security）：`resolveChatMaxHops` clamp 到 [1,5] 先于 `traverse` 与
  `neo4j_client` 的双重校验；正则 `\d{1,2}` 上限 2 位，负数/超长不可构造，`int()` 不会抛；
- **ReDoS**：正则无嵌套/无界量词，最坏线性；
- **侧信道**：错误响应语义不变（NotFound 通用消息 / 降级文案），不泄露图结构；
- **注入**：`maxHops` 经 f-string 进 CQL（Neo4j 不支持参数化变长路径上界），
  但入口即保证 int∈[1,5]，原有校验为承重墙；
- **MEDIUM 修复**（code-reviewer）：聚合量词兜底测试原问法无跳数短语，未真正
  验证「跳数解析在兜底之后」的顺序；改为「供应商 100001 的 3 跳关联订单总金额是多少」。
- **LOW 修复**：
  - 丢弃 `至少` 前缀（maxHops 是上限语义，「至少 3 跳」是下限，映射到上限会低估）；
  - 补 `两`→2 / `十`→10 口语数字；
  - 图推理降级日志补 `exc_info=True`（traceback 留服务端，不透出客户端）；
  - 集成测试改名 `..._roundtrips_max_hops`（不断言 seed 依赖的 depth-3 hop，防脆断）。
- `graph_traverse` agent tool 仍固定 2 跳（`traverseForChat` 未改），与 chat 路径
  存在语义差，列为后续工作（工具 arg_extractor 支持跳数 + handler 透传），
  非本期缺陷。

## 7. 验证

```bash
cd backend
uv run pytest app/tests/unit/test_graph_traversal_service.py app/tests/unit/test_intent_service.py -q
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_graph_traversal_api.py \
    app/tests/integration/test_chat_api.py \
    app/tests/integration/test_agent_runtime_api.py -q
```

期望：单元 95 全绿；集成 34 全绿（含新增 3-hop chat 用例）。

## 8. 关键文件清单

- `backend/app/services/intent_service.py`
- `backend/app/services/graph_traversal_service.py`
- `backend/app/services/chat_service.py`
- `backend/app/tests/unit/test_graph_traversal_service.py`
- `backend/app/tests/integration/test_graph_traversal_api.py`
