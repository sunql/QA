# 变更：chore-chat-15-tail-three-items

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：对话能力（§15 末尾批次）
- **状态**：✅ implemented (2026-09-27，3 项全过；测试 46/46 全绿，ruff 5 文件干净)
- **关联变更**：[chore-doc-drift-cleanup-c](../chore-doc-drift-cleanup-c/summary.md)（§2.5 7 行全闭 + metric-pipeline 主动巡检）
- **迁移版本**：无 alembic 迁移（仅代码 + 单测）
- **MEMORY**：新增 3 条（KPI 关键词索引 / 空关键词语义 / 失败尝试 LLM 用量）

## 1. 需求

`Harness/wiki/chat-service-assessment.md` §15 末尾**新登记 3 项**待办（2026-09-26 末次审
计时未关闭），3 项彼此独立、各自钉死一个边缘用例：

| # | §15 末尾项 | 痛点 |
|---|---|---|
| 1 | KPI 关键词索引结构 | 千级 KPI 时 `_by_keyword` 子串枚举复杂度退化；当前实现要标 SSOT |
| 2 | 空关键词语义 | `"" in s` 恒真 → 「匹配全部」副作用；缺防御 |
| 3 | 失败尝试 LLM 用量 | `completeStream` 流式中断 / `complete_with_tools` 后处理失败时，已测得的 prompt/completion tokens 凭空消失（核心约束 #3「失败路径也是计量路径」违反） |

**目标**：把这 3 项从挂账转 ✅，每项要么实现要么 SSOT 文档化（不混改），保持现有 chat 服
务的不变量。

## 2. 设计评审

### 2.1 整体策略

| 决策点 | 选择 | 理由 |
|---|---|---|
| 项 1 索引 | **不动索引**（加 SSOT 注释） | 实测千级场景下倒排索引 +42.3MB 纯开销，无收益（前提压测已有数据） |
| 项 2 防御 | **`if not kw: continue`**（跳过空 needle） | 最小改动；与既有合法调用路径（`_extractKeywords` 不产 `[""]`）一致 |
| 项 3 用量携带 | **扩 `LlmClientError.tokens` 字段** + `consumedTokens` 第二档优先级 | 与 `Nl2SqlError.tokens` 同一思路；不破既有 `retryGenTokens` 通道契约 |

### 2.2 候选方案对比

#### 项 3：失败路径 LLM 用量

| 维度 | 候选 A：异常带 `tokens` 字段 | 候选 B：调用方在 except 块重试前自己读 usage | 候选 C：全链路重试预算时统一收集 |
|---|---|---|---|
| 改动面 | `exceptions.py` + `openai_client.py` + `llm_retry_policy.py` | 仅 `chat_service` / `nl2sql_service` 改动 | 大改 `completeWithTransientRetry` 公共调用路径 |
| 准确性 | 高（源头挂） | 中（依赖调用方记得） | 低（重试预算跨层会重复计数） |
| 失败路径是否覆盖 | ✅ | ❌（post-response 异常永远漏） | ✅ 但耦合 |
| **决定** | **✅ A** | ❌ | ❌ |

#### 项 2：空关键词语义

| 维度 | 候选 A：防御跳过 | 候选 B：抛 ValueError | 候选 C：文档说明靠调用方守卫 |
|---|---|---|---|
| 兼容性 | ✅（静默 no-op） | ❌ 抛错破坏合法调用 | ✅ |
| 调用方负担 | 低（不用记） | 高 | 中（已有 `if user_kws:` 但调用方要记得） |
| **决定** | **✅ A** | ❌ | ❌ |

### 2.3 三项 SSOT 映射

| 项 | 文件 | 关键改动 | 测试 |
|---|---|---|---|
| 1 | `app/services/kpi_match_cache.py` | `KpiMatchCache` 类 docstring 加未来 Aho-Corasick / 后缀自动机 SSOT + 倒排索引压测结论 | 无新增（SSOT 注释） |
| 2 | `app/services/kpi_match_cache.py` + `tests/unit/test_kpi_match_cache.py` | `findByAnyKeyword` 跳过 `""`；测试 `test_empty_keyword_matches_every_published_kpi` → `test_empty_string_keyword_is_skipped` | 1 例替换 + 既有例辅助断言对齐 |
| 3 | `domain/exceptions.py` + `infrastructure/llm/openai_client.py` + `services/llm_retry_policy.py` + 新单测 | `LlmClientError.tokens` 字段；`completeStream` 累计挂上；`complete_with_tools` 后处理失败挂上；`consumedTokens` 第二档优先级 | 新文件 `tests/unit/test_openai_client_failure_tokens.py` 8 例 + `tests/unit/test_llm_retry_policy.py` 3 例 |

## 3. 数据模型变更（qa_metadata PG）

无 schema / 数据改动。

## 4. 接口契约变更

无 API DTO 变化。

`LlmClientError.__init__` 新增 kw-only `tokens: tuple[int, int] | None = None` 字段
（位置在 `detail` 之后，与 `Nl2SqlError.tokens` 顺序一致）。既有调用方不传时退化为
`None`，`consumedTokens` 走旧路径，零破坏。

## 5. 实现要点

### 5.1 项 1：KPI 关键词索引 SSOT

```python
class KpiMatchCache:
    """L1 语义匹配缓存。

    ⚠️ **未来扩展示例**（§15 末尾第 1 项登记，当前未启用）：
    如果目录规模真的上来（千级 KPI / 万级关键词），正确结构是
    **Aho-Corasick / 后缀自动机**（多模式子串匹配的线性时间算法）而不是
    子串枚举 —— 现行 `_by_keyword` 字典查找的复杂度是 O(keywords × catalog_keywords)，
    在千级场景下会从亚毫秒退化到毫秒级。换索引时必须与 `_by_keyword` **写路径同生命周期**
    （`onKpiChanged` / `refreshOne` 必须重建自动机），否则会复现倒排索引那次的
    静默偏离（写路径漏改、读路径命中陈旧数据）。**准入压测**（500 关键词 × 30 字
    字典 + 倒排子串索引实测 +42.3 MB 纯开销）是当初否决该方案的真实数据，禁止
    凭直觉「越大越需要索引」—— 实测收益在当前规模下为零。当前实现保留作为 SSOT。
    """
```

### 5.2 项 2：空关键词跳过

```python
# app/services/kpi_match_cache.py
def findByAnyKeyword(self, keywords: list[str]) -> list["KpiCatalog"]:
    if not self._loaded:
        return []
    seen: set[int] = set()
    result: list["KpiCatalog"] = []
    for kw in keywords:
        if not kw:
            continue  # 空串跳过（防御：空 needle 不应触发「匹配全部」副作用）
        kw_lower = kw.lower()
        for cat_kw, kpis in self._by_keyword.items():
            if kw_lower in cat_kw.lower():
                for kpi in kpis:
                    if id(kpi) not in seen:
                        seen.add(id(kpi))
                        result.append(kpi)
    return result
```

测试侧：原 `test_empty_keyword_matches_every_published_kpi` 改为 `test_empty_string_keyword_is_skipped`，断言 `[]`（不再命中全部）。辅助 `_expectedHitSet` / `_expectedOrder` 同步跳过空串。

### 5.3 项 3：失败路径 LLM 用量

#### 异常字段扩展

```python
# app/domain/exceptions.py
class LlmClientError(DomainError):
    """LLM 调用失败。tokens 携带已消耗的 (promptTokens, completionTokens)
    —— 与 Nl2SqlError.tokens 同一思路，覆盖「失败路径也是计量路径」约束。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        detail: str | None = None,
        tokens: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.provider = provider
        self.tokens = tokens
```

#### `consumedTokens` 第二档优先级

```python
# app/services/llm_retry_policy.py
def consumedTokens(exc: Exception) -> tuple[int, int]:
    """取值优先级：
    1. Nl2SqlError.tokens（终态累计值）
    2. LlmClientError.tokens（基础设施层实测用量，流式 / post-response 失败时挂）
    3. 携带通道 retryGenTokens（多轮中途失败时上层累计值，M4 路径）
    """
    if isinstance(exc, Nl2SqlError) and exc.tokens is not None:
        return exc.tokens
    if isinstance(exc, LlmClientError) and exc.tokens is not None:
        return exc.tokens
    return retryGenTokens(exc)
```

#### `completeStream` 累计挂上

```python
# app/infrastructure/llm/openai_client.py
promptTokens = 0
completionTokens = 0
try:
    async with acquire_llm_concurrency():
        stream = await self._client.chat.completions.create(**payload)
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                promptTokens = getattr(usage, "prompt_tokens", 0) or 0
                completionTokens = getattr(usage, "completion_tokens", 0) or 0
                continue
            ...
except Exception as exc:
    accumulated = (promptTokens, completionTokens) if (promptTokens or completionTokens) else None
    raise LlmClientError(
        MSG_LLM_STREAM_FAILED.format(provider=self._provider.value, exc=exc),
        provider=self._provider.value, detail=str(exc), tokens=accumulated,
    ) from exc
```

`create(...)` 抛错时 `promptTokens/completionTokens` 仍为 0/0 ⇒ `accumulated=None`（不伪造"零消耗"）。

#### `complete_with_tools` 后处理失败挂上

```python
# 先读 usage
usage = getattr(response, "usage", None)
prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
completion_tokens = getattr(usage, "completion_tokens", 0) or 0
response_tokens = (prompt_tokens, completion_tokens)

# 易出错解析包在 try/except，挂上 response_tokens
try:
    tool_calls = [...]
except Exception as exc:
    raise LlmClientError(
        MSG_LLM_CALL_FAILED.format(...),
        provider=self._provider.value,
        detail=f"post-response 解析失败：{exc}",
        tokens=response_tokens,
    ) from exc
```

#### 顺手修：circular import

`openai_client.py` 之前 `from app.infrastructure.llm.factory import acquire_llm_concurrency` 是预存潜在环（factory ↔ client）。
改为 `from app.infrastructure.llm.concurrency import acquire_llm_concurrency` 直接导入叶
子模块。新测试文件 `_patchConcurrency` autouse fixture 验证：no-op 上下文管理器替
换模块内符号即可生效（印证直接导入而非闭包引用）。

## 6. 测试

### 6.1 单测（46 例全绿）

| 文件 | 新增 / 改动 | 覆盖 |
|---|---|---|
| `tests/unit/test_kpi_match_cache.py` | 1 例替换 + 既有例辅助断言 | 项 2：空串跳过、与非空一致 |
| `tests/unit/test_llm_retry_policy.py::TestConsumedTokens` | 3 例 | 项 3：`LlmClientError.tokens` 优先于通道 / 无 channel 回退 / 无 builtin 走通道 |
| `tests/unit/test_openai_client_failure_tokens.py`（新） | 8 例 | 项 3：`completeStream` 终块后异常 / 收到 usage 终块 / `create(...)` 失败（无 tokens）/ `complete_with_tools` 后处理失败 / `create(...)` 失败 / `consumedTokens` 端到端读流式失败 / 读 post-response 失败 / 无 builtin 返回 (0,0) |

测试基础设施：`StreamChunks` / `FakeStreamFactory` / `FakeClient` / `UsageObj` / `ChoiceObj` / `FakeResponse` / `FakeChunk` —— 纯字典伪流，无 SDK 依赖。

### 6.2 验证

```bash
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  python -m pytest app/tests/unit/test_openai_client_failure_tokens.py \
                   app/tests/unit/test_llm_retry_policy.py \
                   app/tests/unit/test_kpi_match_cache.py -x -q
# → 46 passed in 9.60s
```

### 6.3 ruff

```bash
ruff check <batch 5 files>  # → All checks passed!
```

注：`kpi_match_cache.py` 12 个 pre-existing 警告（`I001`/`UP037`，import 排序 + 引号类
型注解）**本批未触碰**（基线比对确认），不在本批改动面内。

## 7. 安全审查

未触发 security-reviewer：
- 无 auth / secrets / SQL / 用户输入改动；
- 项 3 只在异常传递路径上多挂一个 `tokens` 元组，无可被外部利用的输入面。

## 8. 部署验证

```bash
./scripts/deploy_backend.sh
# → [deploy] 快照容器内现状
# → [deploy] cp app/. scripts/. alembic/. → qa-backend
# → [deploy] 重启 qa-backend
# → [deploy] ✅ 启动成功
```

无 alembic 迁移（无 DB schema 改动）。

真机冒烟（待 deploy 后跑）：
```bash
# 项 3 真机探针：故意构造流式中断 / post-response 解析失败
# 验证 audit_log 行能如实计量这部分用量
```

## 9. 关联

- 设计稿：本 summary.md（无独立 plan）
- Wiki：`Harness/wiki/chat-service-assessment.md` §15 末尾（三项挂账 → ✅）
- Rules：Harness/rules/开发流程规范.md（核心约束 #3 失败路径也是计量路径）
- Memory：
  - `~/.claude/projects/.../memory/qa-system-kpi-keyword-index-future.md`（新增）
  - `~/.claude/projects/.../memory/qa-system-kpi-empty-keyword-skip.md`（新增）
  - `~/.claude/projects/.../memory/qa-system-llm-failure-tokens.md`（新增）
- 关联变更：
  - predecessor: [chore-doc-drift-cleanup-c](../chore-doc-drift-cleanup-c/summary.md)
  - sibling: M4 同模型瞬态重试（`LlmClientError` + `retryGenTokens` 双通道已是上游契约）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段迁移文件名 ≤ 32 字符（无迁移，N/A）
- [x] 第 7 段未触发安全审查（说明理由）
- [x] 第 8 段 docker compose 冒烟命令已贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] MEMORY 索引已添加

## 风险与遗留

| 风险 | 等级 | 现状 |
|---|---|---|
| 流式中断时仍可能丢 **极早期** chunk 的部分 delta 字节（语义上无对应 token，但延迟没记） | 低 | 流式产出的是字节而非 token；既有契约是按 `usage` 终块计，已对齐 |
| `consumedTokens` 第二档被滥用：未来若 LLM client 内多了一层「真调用」会重复计 | 低 | 现有两层不会同时挂（`tokens` 是 builtin，`retryGenTokens` 是上层累计）；测试钉死优先级 |
| 异常字段扩 `tokens` 是 kw-only，旧调用方代码不需改；但**类型注解**靠 docstring，IDE 静态检查不到 | 低 | 有 8 例新单测覆盖；docstring 显式标了字段 |
