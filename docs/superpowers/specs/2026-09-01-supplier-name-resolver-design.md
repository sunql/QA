# 供应商名称 + 编码双路解析（Phase 6.5）

> 日期：2026-09-01
> 类型：纯后端（agent_runtime / chat 顶层预解析）
> 关联：`Harness/changes/feat-supplier-name-resolver/`（执行期创建）

## Context（为什么做）

`Agent Runtime` 与 `Chat` 链路当前仅接受供应商 **enterprise_code**（如 `10105`）作为查询入口——所有 `arg_extractor` / intent extractor 都用 `\d{5,9}` 正则匹配用户输入里的数字编码。

**真实业务诉求**：业务用户查询时记不住数字编码，更习惯用供应商名称（如「评估供应商 济南吉利汽车有限公司 的风险」）。

**机会**：DB 层 `entity_mapping.name` 字段已落地真实中文供应商名（参见 models.py:1002 注释），但当前仅用于展示，未参与查询路径。

**目标**：让用户输入中文供应商名也能路由到正确的 `enterprise_code`，透明接入现有 pipeline；数字编码路径完全保持原行为不变。

## 目标

1. **数字 + 名字 双路**：用户输入「评估供应商 济南吉利汽车有限公司 的风险」与「评估供应商 10105 的风险」语义等价，agent 行为一致。
2. **零侵入**：现有 3 个 supplier 工具的 `arg_extractor`（同步正则）完全不改；ChatService 五类意图分类与 NL2SQL 流水线不改。
3. **歧义显式化**：多个 enterprise 匹配同一名字片段 → 422 + 候选列表，让用户精确指定。
4. **透明替换**：把名字解析成 code 后，原样替换 message 文本喂给下游；下游对「数字 vs 名字」零感知。
5. **真实 DB 测试**：所有集成测试走 PostgreSQL（端口 5433，库 `qa_metadata_test`），与既有约定一致。

## 非目标（YAGNI）

1. **本期不做 Supplier Name AutoComplete API**：不暴露 `GET /suppliers/search?q=` 端点；用户先用企业码或复制粘贴名字。
2. **本期不缓存 name → code 映射**：当前 SUPPLIER 量级小（8 条），LIKE 查询 O(ms)，后续按需扩展。
3. **本期不直接改 SupplierRiskService / GraphTraversalService**：顶层预解析后，下游 service 完全不变；如未来要直连，改动会很小。
4. **本期不引入模糊匹配算法**（如 Levenshtein / 拼音相似度）：LIKE '%X%' 已足够业务场景。
5. **本期不改 `extractSupplierKey` / `extractSupplierRiskKey` / `extractSupplierGraphKey`** 三个正则：它们仍是数字主路径。
6. **本期不写新前端页面**：422 ambiguous 由前端 chat 组件按既有 ValidationError handler 展示（沿用 MSG_AGENT_RUN_BAD_INPUT 的 422 渲染路径）。

## 架构

```
                  ┌────────────────────────────────────────────┐
                  │ ChatService / AgentRuntimeService 入口     │
                  └────────────────────────────────────────────┘
                                       │
                  ┌──────────────────── ▼ ────────────────────┐
                  │ SupplierNameResolver.resolve(message, session) │
                  │                                              │
                  │  1. 数字正则 (extractSupplierAnyKey)        │
                  │       └─ 命中 → ResolvedKey(key=digits)    │
                  │                                              │
                  │  2. 中文名提取 (NAME_EXTRACT_RE)            │
                  │       └─ 未提取到 → resolve() 返回 None     │
                  │                                              │
                  │  3. entity_mapping.name 精确匹配 (==)        │
                  │       ├─ 1 条 → ResolvedKey(key=code)       │
                  │       ├─ 0 条 → step 4                     │
                  │       └─ N 条 (数据不应, 但兜底) → ambiguous │
                  │                                              │
                  │  4. entity_mapping.name LIKE %name%         │
                  │       ├─ 1 条 → ResolvedKey(key=code)       │
                  │       ├─ 0 条 → ValidationError(422, nf)   │
                  │       └─ N 条 → ValidationError(422, ambig) │
                  └────────────────────────────────────────────┘
                                       │
                  ┌──────────────────── ▼ ────────────────────┐
                  │ SupplierNameResolver.apply(message, resolved) │
                  │   └─ 把 message 中 "济南吉利..." 替换为 "10105" │
                  └────────────────────────────────────────────┘
                                       │
                  ┌──────────────────── ▼ ────────────────────┐
                  │ 替换后的 message 喂给现有 pipeline          │
                  │ (arg_extractor / 意图分类 / NL2SQL 完全不变) │
                  └────────────────────────────────────────────┘
```

## 组件设计

### `SupplierNameResolver` 新 service

`backend/app/services/supplier_name_resolver.py`（新文件）

```python
from collections.abc import Sequence
from typing import Any, NamedTuple, Literal
from sqlalchemy.ext.asyncio import AsyncSession

# 候选展示上限：超过此值视为「过宽」，错误消息提示用户用 enterprise_code
# 精确指定而非列全量（真实业务 SUPPLIER 量级约 3500+，LIKE %X% 易命中数百）
_CANDIDATE_DISPLAY_LIMIT = 50


def _format_candidates(rows: Sequence[tuple[Any, str]]) -> str:
    """把 (code, name) 行渲染为 '10105 济南吉利汽车有限公司 | 10120 ...' 形式。"""
    return " | ".join(f"{code} {name}" for code, name in rows)


def _rows_to_pairs(rows: Sequence[tuple[Any, str]]) -> list[list[str]]:
    """把 (code, name) 行序列化为 [[code, name], ...] 透传到 ValidationError.details。"""
    return [[str(code), str(name)] for code, name in rows]


class ResolvedKey(NamedTuple):
    """成功路径的解析结果；失败路径由 resolve() 抛 ValidationError。

    key            — enterprise_code（digits 或解析出的 code）
    resolved_by    — 解析来源（用于日志/审计与 apply 判定）
    original_name  — 解析出的中文名（apply 用作 replace source）；code_regex 路径为 None
    """
    key: str
    resolved_by: Literal["code_regex", "name_exact", "name_like"]
    original_name: str | None


class SupplierNameResolver:
    """ChatService / AgentRuntimeService 顶层名字→编码预解析（Phase 6.5）。

    设计原则:
    - arg_extractor 保持同步 (零侵入)
    - 仅在数字正则未命中时触发名字解析 (避免已知 code 多一次 DB 查询)
    - 替换 message 中的名字为 code, 下游完全透明
    - 候选「过宽」判断：≥ _CANDIDATE_DISPLAY_LIMIT 视为模糊（"吉利"命中 200+），
      不列全量（前端无意义），错误消息引导用户精确指定
    """

    # 中文/英文 2-30 字符，至少 1 个汉字；支持中间点 · 与连字符 -
    _NAME_EXTRACT_RE = re.compile(
        r"(?:供应商|supplier)\s*[:：]?\s*(?P<name>[一-龥][一-龥A-Za-z0-9·\-\s]{1,29})"
    )

    async def resolve(
        self, message: str, session: AsyncSession
    ) -> ResolvedKey | None:
        """成功 → ResolvedKey；失败 → ValidationError；无关键词 → None。"""
        # Pass 0: 数字正则先试 (extractSupplierAnyKey from intent_service)
        numeric = extractSupplierAnyKey(message)
        if numeric is not None:
            return ResolvedKey(key=numeric, resolved_by="code_regex",
                               original_name=None)

        # Pass 1: 抽取中文名；无关键词 → 让下游 pipeline 自己处理
        match = self._NAME_EXTRACT_RE.search(message)
        if match is None:
            return None

        name = match.group("name").strip()

        # Pass 2: 精确匹配 entity_mapping.name
        exact = await session.execute(
            select(EntityMapping.enterprise_code, EntityMapping.name)
            .where(EntityMapping.entity_type == EntityType.SUPPLIER,
                   EntityMapping.name == name)
            .limit(2)
        )
        exact_rows = exact.all()
        if len(exact_rows) == 1:
            return ResolvedKey(key=str(exact_rows[0][0]),
                               resolved_by="name_exact", original_name=name)
        if len(exact_rows) > 1:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name,
                    n=len(exact_rows),
                    candidates=_format_candidates(exact_rows),
                ),
                details={"candidates": _rows_to_pairs(exact_rows)},
            )

        # Pass 3: LIKE 模糊匹配（依赖 PG 自然全量扫；不设硬 LIMIT）
        # 注：当前 entity_mapping.name 无 trigram index，LIKE %X% 走顺序扫；
        # 3500 条规模下 O(ms)，暂不引 pg_trgm 扩展。后续按需索引。
        like = await session.execute(
            select(EntityMapping.enterprise_code, EntityMapping.name)
            .where(EntityMapping.entity_type == EntityType.SUPPLIER,
                   EntityMapping.name.ilike(f"%{name}%"))
        )
        like_rows = like.all()
        n = len(like_rows)
        if n == 1:
            return ResolvedKey(key=str(like_rows[0][0]),
                               resolved_by="name_like", original_name=name)
        if 1 < n < _CANDIDATE_DISPLAY_LIMIT:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name,
                    n=n,
                    candidates=_format_candidates(like_rows),
                ),
                details={"candidates": _rows_to_pairs(like_rows)},
            )
        if n >= _CANDIDATE_DISPLAY_LIMIT:
            # 过宽：列内容无意义（前端无法展示），仅给数量 + 引导
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT.format(
                    name=name, limit=_CANDIDATE_DISPLAY_LIMIT
                ),
                details={"candidate_count": n, "name": name},
            )

        # 0 命中
        raise ValidationError(MSG_SUPPLIER_NAME_NOT_FOUND.format(name=name))

    def apply(self, message: str, resolved: ResolvedKey | None) -> str:
        """把 message 中 original_name 替换为 resolved.key（仅 name 路径生效）。

        resolved is None（无关键词）或 original_name is None（code_regex 路径）
        → 原样返回，不修改。
        """
        if resolved is None or resolved.original_name is None:
            return message
        return message.replace(resolved.original_name, resolved.key, 1)
```

### 错误消息扩展

`backend/app/domain/error_messages.py` 新增：

```python
MSG_SUPPLIER_NAME_NOT_FOUND = (
    "未在主数据中找到名为 '{name}' 的供应商。"
    "请用 enterprise_code（如 10105）重试，或检查名称拼写"
)

MSG_SUPPLIER_NAME_AMBIGUOUS = (
    "供应商名 '{name}' 匹配 {n} 条候选，请用 enterprise_code 精确指定："
    "{candidates}"
)
# candidates 由 service 渲染为 "10105 济南吉利汽车有限公司 | 10120 浙江福林国润汽车零部件"

MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT = (
    "供应商名 '{name}' 命中候选数过多（≥ {limit} 条），请用更具体的关键词缩小范围，"
    "或直接用 enterprise_code 精确指定"
)
```

### ValidationError 扩展（支持附加候选列表）

`backend/app/domain/exceptions.py` 在 `ValidationError` 上加可选字段 `details: dict | None`，422 响应体里 `details.candidates` 由全局 DomainError handler 透传给前端。

## 调用点集成

### `AgentRuntimeService.run`（run 方法 line 82-135）

在 `tool.arg_extractor(input_text)` 之前（line 114）插入：

```python
# Phase 6.5: 供应商名→编码预解析（不命中 → 422）
resolver = SupplierNameResolver()
resolved = await resolver.resolve(input_text, session)
input_text = resolver.apply(input_text, resolved)

args = tool.arg_extractor(input_text)
```

`resolve()` 抛 `ValidationError`（not_found / ambiguous / over_limit 三种）时，由全局 handler 映射 422 + `details.candidates`（或 `details.candidate_count`），下游不再执行。

### `ChatService`（锁定行号）

`backend/app/services/chat_service.py`

**集成点 1 — 依赖注入（line 364 后）**

```python
# Phase 6.5：supplier name → code 预解析（ChatService / AgentRuntimeService 共享）
self._supplierNameResolver = supplierNameResolver or SupplierNameResolver()
```

**集成点 2 — 共享预解析 helper（`_prepareSupplierQuestion`，新私有方法）**

```python
async def _prepareSupplierQuestion(
    self, session: AsyncSession, dto: ChatRequest
) -> ChatRequest:
    """Phase 6.5：supplier name → code 预解析（immutable replace，下游零感知）。

    成功 → 返回替换后的新 dto（model_copy，不原地修改）；
    无关键词 / 纯数字编码 → 原样返回 dto（零 DB 开销）；
    解析失败（not_found / ambiguous / over_limit）→ 抛 ValidationError，
    由两个入口分别转为友好 answer / error 事件（chat 惯例，见下）。
    """
    resolved = await self._supplierNameResolver.resolve(dto.question, session)
    if resolved is None or resolved.original_name is None:
        return dto
    return dto.model_copy(update={
        "question": self._supplierNameResolver.apply(dto.question, resolved)
    })
```

**集成点 3 — processMessage 入口（line 382 前，processMessage 在 line 366）**

```python
# Phase 6.5：supplier name → code 预解析（immutable replace，下游零感知）
try:
    dto = await self._prepareSupplierQuestion(session, dto)
except ValidationError as exc:
    # chat 惯例（与 _handleSupplier360 NotFoundError 同模式）：
    # 不给前端抛 422，而是返回 ChatResponse + 友好 answer（含候选列表）。
    # 4-4 惯例：本轮也持久化消息，历史链不断。
    result = self._intent.classifyResult(dto.question)
    await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
    return ChatResponse(answer=exc.message, intent=result.intent.value)

result, state = await self._classifyMessage(session, dto)
# ... 后续不变 ...
```

**集成点 4 — processMessageStream 入口（line 1595 前）**

```python
# Phase 6.5：supplier name → code 预解析（同 processMessage，流式路径覆盖）
try:
    dto = await self._prepareSupplierQuestion(session, dto)
except ValidationError as exc:
    # 流式惯例（与 processMessageStream 的 DomainError 分支同型）：结构化 error 事件
    await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
    yield StreamEvent(
        EVENT_ERROR,
        {"error": exc.message, "errorType": ErrorType.DOMAIN.value, "detail": exc.detail},
    )
    return

result = self._intent.classifyResult(dto.question)
# ... 后续不变 ...
```

**错误面契约（两处入口 + Agent Runtime 共三种）**：
- `processMessage`（非流式 chat）：ValidationError → 200 ChatResponse + `answer=引导文案（含候选）`
- `processMessageStream`（流式 chat）：ValidationError → SSE error 事件（ErrorType.DOMAIN）
- `AgentRuntimeService.run`（REST）：ValidationError → 全局 handler 422 + `details.candidates`

**依据**：chat 端 `_handleSupplier360` / `_handleSupplierRisk` 对 NotFoundError 均转友好 answer（chat_service.py:1235, 1275），422 会破坏 chat UI「永远有 answer」的契约；Agent Runtime 是 REST 工具端点，422 是既有契约（MSG_AGENT_RUN_BAD_INPUT）。

## 测试策略

### Unit: `tests/unit/test_supplier_name_resolver.py`（新）

不依赖 DB，用 fake session。覆盖：
1. 数字正则命中 → ResolvedKey(resolved_by=code_regex)，不查 DB
2. 中文名精确匹配 1 条 → ResolvedKey(resolved_by=name_exact, original_name=name)
3. 中文名 LIKE 唯一命中 → ResolvedKey(resolved_by=name_like)
4. 中文名 LIKE 多条（< 50）→ ValidationError(422, MSG_SUPPLIER_NAME_AMBIGUOUS, details.candidates)
5. 中文名 LIKE ≥ 50 条 → ValidationError(422, MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT, details.candidate_count)
6. 中文名 0 命中 → ValidationError(422, MSG_SUPPLIER_NAME_NOT_FOUND)
7. 无 "供应商/supplier" 关键词 → resolve() 返回 None（不抛错，让 arg_extractor 自己处理）
8. `apply(None, None)` → 原 message
9. `apply(msg, code_regex)` → 原 message（不替换）
10. `apply(msg, name_exact)` → 替换 original_name → key
11. `_format_candidates` / `_rows_to_pairs` 序列化正确

### Unit: `tests/unit/test_agent_runtime_service.py`（扩展）

扩展既有 `_fakeSession` 注入 fake resolver 行为；新增用例：
1. SUPPLIER_360_AGENT.run("供应商 济南吉利汽车有限公司") → mock resolver 返回 '10105' → handler 被以 key='10105' 调用
2. SUPPLIER_360_AGENT.run("供应商 吉利") → mock resolver 抛 ambiguous → 422 + candidates in details
3. SUPPLIER_360_AGENT.run("供应商 不存在") → mock resolver 抛 not_found → 422
4. SUPPLIER_360_AGENT.run("10105") → resolver 跳过 DB 路径（code_regex），handler 直接被以 digits 调用（回归保护）

### Integration: `tests/integration/test_agent_runtime_supplier_name.py`（新）

真实 PostgreSQL（5433 / qa_metadata_test），用既有 seed 工具（`scripts/seed_entity_mapping.py` 或同类）按实际数据驱动测试；具体 fixture 选型由实施期根据既有 seed 工具的覆盖度决定（业务视角：依赖实际效果）。

1. SUPPLIER_360_AGENT.run("评估供应商 济南吉利汽车有限公司") → 200 + view.enterprise_code == '10105'
2. SUPPLIER_360_AGENT.run("供应商 吉利") → 422 + response.details.candidates 长度 ≥ 2（依赖既有 SUPPLIER 数据中 "吉利" 命中数；若既有数据不够则在测试用 fixture 临时插入）
3. SUPPLIER_360_AGENT.run("供应商 不存在的名字") → 422 + message 含 "未在主数据中找到"
4. SUPPLIER_360_AGENT.run("供应商 汽车") → 422 + response.details.candidate_count ≥ 50 + message 含 "命中候选数过多"（依赖既有数据量大；按需 fixture 补充）

### Integration: `tests/integration/test_chat_supplier_name.py`（新）

1. POST /api/v1/chat `{"message": "评估供应商 济南吉利汽车有限公司 的风险"}` → 200 + 内部按 10105 执行（answer 引用含 '10105' 或 enterprise_code）
2. POST /api/v1/chat `{"message": "查询供应商 吉利"}` → 200 ChatResponse + answer 含候选 enterprise_code（chat 惯例：不抛 422，见「错误面契约」）
3. 流式 POST /api/v1/chat/stream 同名消息 → SSE error 事件（ErrorType.DOMAIN）+ error 含候选

## 关键文件清单

- `backend/app/services/supplier_name_resolver.py` (新)
- `backend/app/services/agent_runtime_service.py` (集成 2 行)
- `backend/app/services/chat_service.py` (集成 4 行：resolve + apply，保留原 message)
- `backend/app/domain/error_messages.py` (2 个新常量)
- `backend/app/domain/exceptions.py` (ValidationError 加 details: dict | None)
- `backend/app/main.py`（或全局 error handler 文件，如已有）(ValidationError 422 响应透传 details)
- `backend/app/tests/unit/test_supplier_name_resolver.py` (新)
- `backend/app/tests/unit/test_agent_runtime_service.py` (扩展 ~4 个用例)
- `backend/app/tests/integration/test_agent_runtime_supplier_name.py` (新)
- `backend/app/tests/integration/test_chat_supplier_name.py` (新)
- `Harness/changes/feat-supplier-name-resolver/summary.md` (变更记录)

**无 Alembic 迁移**（entity_mapping.name 列已存在）。  
**无前端改动**（422 响应沿用既有 ValidationError 处理路径）。

## 验证

```bash
cd backend
# 定向（解析器 + agent runtime 集成）
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_supplier_name_resolver.py \
    app/tests/unit/test_agent_runtime_service.py \
    app/tests/integration/test_agent_runtime_supplier_name.py -v

# Chat 集成
TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_chat_supplier_name.py -v

# 全量回归 + 覆盖率门槛 80%
TEST_DATABASE_URL=... uv run pytest app/tests/ --cov=app --cov-fail-under=80

# 手动 e2e: 起 backend, 用真实数据验证
docker compose up -d backend
curl -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "Content-Type: application/json" \
  -d '{"input_text":"查询供应商 济南吉利汽车有限公司 的情况"}'
# 期望 200 + enterprise_code='10105'
```

## 收尾

- `Harness/changes/feat-supplier-name-resolver/summary.md` 记录目标/文件/验证/commit hash
- `Harness/wiki/business-domain.md` Agent Runtime 章节补一句「支持 enterprise_code 与供应商名称双路解析」
- 独立 commit：`feat: supplier name resolver for agent runtime and chat`
