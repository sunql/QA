# 供应商名称 + 编码双路解析（Phase 6.5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户用供应商中文名（如「济南吉利汽车有限公司」）或 enterprise_code（如 10105）都能触发 supplier_360 / supplier_risk / graph_traverse 分析；名字歧义时返回候选列表。

**Architecture:** 顶层预解析——新增 `SupplierNameResolver`，在 `AgentRuntimeService.run` 与 `ChatService.processMessage` / `processMessageStream` 入口把 message 中的名字替换为 enterprise_code（immutable `model_copy`），下游 arg_extractor / 意图分类 / NL2SQL 流水线零改动。失败路径：Agent Runtime REST → 422 + `details.candidates`；chat 非流式 → 友好 answer；chat 流式 → SSE error 事件。

**Tech Stack:** FastAPI + SQLAlchemy async + PostgreSQL（真实库测试，端口 5433）+ pytest。

**Spec:** `docs/superpowers/specs/2026-09-01-supplier-name-resolver-design.md`

## Global Constraints

- 测试数据库（每个后端测试命令都要带）：
  `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`
- 不可变数据：不原地修改 dto；用 `model_copy(update={...})` 生成新对象。
- 命名：Python 侧 snake_case（`resolved_by`、`original_name`）；API 契约 camelCase（CamelModel 自动转换）。
- 无 Alembic 迁移（`entity_mapping.name` 列已存在）；无前端改动。
- 文件 < 800 行、函数 < 50 行。
- 每个 Task 完成即 commit（conventional commits，无 attribution）。

---

### Task 1: ValidationError.details 扩展 + 全局 handler 透传 + 错误消息常量

**Files:**
- Modify: `backend/app/domain/exceptions.py`（ValidationError，line 32-33）
- Modify: `backend/app/domain/schemas.py`（ErrorResponse，line 1447-1450）
- Modify: `backend/app/main.py`（handleDomainError，line 271-278）
- Modify: `backend/app/domain/error_messages.py`（文件末尾追加）
- Test: `backend/app/tests/unit/test_validation_error_details.py`（新）

**Interfaces:**
- Produces: `ValidationError(message, *, detail: str | None = None, details: dict | None = None)`；`ErrorResponse.details` 字段；3 个错误消息常量 `MSG_SUPPLIER_NAME_NOT_FOUND` / `MSG_SUPPLIER_NAME_AMBIGUOUS` / `MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT`（Task 2/3 使用）。

- [ ] **Step 1: 写失败测试**

```python
"""ValidationError.details 扩展单测（Phase 6.5 Task 1）。

DomainError.detail（str）与 ValidationError.details（dict）是两个通道：
- detail  → 既有折叠展示用（前端 client.ts 读 detail 字符串）
- details → 结构化数据（candidates 列表等），仅 ValidationError 支持
"""

from __future__ import annotations

from app.domain.exceptions import ValidationError


class TestValidationErrorDetails:
    def test_details_kwarg_stored_and_defaults_none(self):
        err = ValidationError("m", details={"candidates": [["10105", "x"]]})
        assert err.details == {"candidates": [["10105", "x"]]}
        assert err.message == "m"
        assert err.detail is None  # 既有通道不受影响

    def test_details_defaults_to_none(self):
        err = ValidationError("m")
        assert err.details is None

    def test_detail_and_details_coexist(self):
        err = ValidationError("m", detail="str", details={"n": 1})
        assert err.detail == "str"
        assert err.details == {"n": 1}

    def test_other_domain_errors_have_no_details_attr_semantics(self):
        # 非 ValidationError 的 DomainError 不受影响（details 属性不存在语义）
        from app.domain.exceptions import ConflictError

        err = ConflictError("m")
        assert not hasattr(err, "details")
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_validation_error_details.py -q
# 期望：FAIL（TypeError: ValidationError() got an unexpected keyword 'details' 或 assert hasattr 失败）
```

- [ ] **Step 3: 实现**

`backend/app/domain/exceptions.py` —— 替换 ValidationError（line 32-33）：

```python
class ValidationError(DomainError):
    """输入校验失败（领域规则层面）。

    Phase 6.5 扩展：details 携带结构化数据（如供应商名歧义候选列表），
    由全局 handler 透传到 422 响应体。与 detail（str，折叠展示用）互不影响。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.details = details
```

`backend/app/domain/schemas.py` —— ErrorResponse（line 1447-1450）加一行：

```python
class ErrorResponse(CamelModel):
    success: bool = False
    error: str
    detail: str | None = None
    # Phase 6.5：结构化错误数据（如供应商名歧义候选列表）；仅部分 ValidationError 携带
    details: dict | None = None
```

`backend/app/main.py` —— handleDomainError（line 275-278）改为：

```python
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=exc.message,
                detail=exc.detail,
                details=getattr(exc, "details", None),
            ).model_dump(by_alias=True),
        )
```

`backend/app/domain/error_messages.py` —— 文件末尾追加：

```python
# ===== Phase 6.5：供应商名称解析（SupplierNameResolver） =====
MSG_SUPPLIER_NAME_NOT_FOUND = (
    "未在主数据中找到名为 '{name}' 的供应商。"
    "请用 enterprise_code（如 10105）重试，或检查名称拼写"
)
MSG_SUPPLIER_NAME_AMBIGUOUS = (
    "供应商名 '{name}' 匹配 {n} 条候选，请用 enterprise_code 精确指定：{candidates}"
)
MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT = (
    "供应商名 '{name}' 命中候选数过多（≥ {limit} 条），"
    "请用更具体的关键词缩小范围，或直接用 enterprise_code 精确指定"
)
```

- [ ] **Step 4: 运行确认通过 + 既有回归**

```bash
cd backend && uv run pytest app/tests/unit/test_validation_error_details.py \
  app/tests/unit/test_rate_limit.py -q
# 期望：全部 PASS（test_rate_limit 验证既有 handler 行为不回归）
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/exceptions.py backend/app/domain/schemas.py \
  backend/app/main.py backend/app/domain/error_messages.py \
  backend/app/tests/unit/test_validation_error_details.py
git commit -m "feat(domain): ValidationError.details structured channel + 3 supplier name messages"
```

---

### Task 2: SupplierNameResolver 核心 + 单测

**Files:**
- Create: `backend/app/services/supplier_name_resolver.py`
- Test: `backend/app/tests/unit/test_supplier_name_resolver.py`（新）

**Interfaces:**
- Consumes: `extractSupplierAnyKey`（`app.services.intent_service`）；`EntityMapping` / `EntityType`（`app.domain.models` / `app.domain.enums`）；Task 1 的 3 个 MSG 常量 + `ValidationError.details`。
- Produces:
  - `ResolvedKey` NamedTuple：`key: str`、`resolved_by: Literal["code_regex","name_exact","name_like"]`、`original_name: str | None`
  - `SupplierNameResolver().resolve(message: str, session: AsyncSession) -> ResolvedKey | None`（失败 raise ValidationError；无关键词返回 None）
  - `SupplierNameResolver().apply(message: str, resolved: ResolvedKey | None) -> str`
  - 模块级 `_CANDIDATE_DISPLAY_LIMIT = 50`、`_format_candidates(rows)`、`_rows_to_pairs(rows)`

- [ ] **Step 1: 写失败测试**

```python
"""SupplierNameResolver 单测（Phase 6.5 Task 2）。

Fake session 注入预设查询结果（不触真实 DB）；覆盖 resolve() 全部分支
与 apply() 替换语义。
"""

from __future__ import annotations

import pytest

from app.domain.error_messages import (
    MSG_SUPPLIER_NAME_AMBIGUOUS,
    MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT,
    MSG_SUPPLIER_NAME_NOT_FOUND,
)
from app.domain.exceptions import ValidationError
from app.services.supplier_name_resolver import (
    _CANDIDATE_DISPLAY_LIMIT,
    _format_candidates,
    _rows_to_pairs,
    ResolvedKey,
    SupplierNameResolver,
)


class _FakeResult:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """按 WHERE 子句顺序返回预设行：每次 execute 弹出队列首元素。

    resolve() 的查询顺序：exact（== name）→ like（ilike %name%）。
    """

    def __init__(self, exact_rows: list[tuple], like_rows: list[tuple]):
        self._queue = [_FakeResult(exact_rows), _FakeResult(like_rows)]
        self.executeCount = 0

    async def execute(self, *_args, **_kwargs):
        self.executeCount += 1
        return self._queue.pop(0)


class TestResolve:
    def test_numeric_code_short_circuits_without_db(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("评估供应商 10105 的风险", session))
        assert resolved == ResolvedKey(
            key="10105", resolved_by="code_regex", original_name=None
        )
        assert session.executeCount == 0  # 数字路径零 DB 查询

    def test_no_supplier_keyword_returns_none(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("今天天气如何", session))
        assert resolved is None
        assert session.executeCount == 0

    def test_exact_single_match(self):
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve(
                "供应商 济南吉利汽车有限公司 的 360° 视图", session
            )
        )
        assert resolved == ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_exact_multiple_raises_ambiguous_with_candidates(self):
        rows = [("10105", "吉利一厂"), ("10106", "吉利二厂")]
        session = _FakeSession(exact_rows=rows, like_rows=[])
        with pytest.raises(ValidationError) as exc_info:
            _run(
                SupplierNameResolver().resolve("供应商 吉利一厂", session)
            )
        assert exc_info.value.details == {
            "candidates": [["10105", "吉利一厂"], ["10106", "吉利二厂"]]
        }
        assert "10105" in exc_info.value.message
        assert "2" in exc_info.value.message

    def test_like_unique_match(self):
        session = _FakeSession(
            exact_rows=[], like_rows=[("10111", "宁波泰鸿机电有限公司")]
        )
        resolved = _run(SupplierNameResolver().resolve("供应商 泰鸿机电", session))
        assert resolved == ResolvedKey(
            key="10111", resolved_by="name_like", original_name="泰鸿机电"
        )

    def test_like_multiple_raises_ambiguous(self):
        rows = [("10105", "济南吉利汽车有限公司"), ("10118", "宁波吉利汽车研究开发有限公司")]
        session = _FakeSession(exact_rows=[], like_rows=rows)
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 吉利汽车", session))
        assert len(exc_info.value.details["candidates"]) == 2
        assert MSG_SUPPLIER_NAME_AMBIGUOUS.split("{name}")[0] in exc_info.value.message

    def test_like_over_limit_raises_over_limit_without_listing(self):
        rows = [(str(10000 + i), f"汽车供应商{i}") for i in range(_CANDIDATE_DISPLAY_LIMIT)]
        session = _FakeSession(exact_rows=[], like_rows=rows)
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 汽车", session))
        assert exc_info.value.details == {
            "candidate_count": _CANDIDATE_DISPLAY_LIMIT, "name": "汽车"
        }
        assert "过多" in exc_info.value.message
        assert "10105" not in exc_info.value.message  # 不列全量

    def test_not_found_raises(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 不存在的公司", session))
        assert MSG_SUPPLIER_NAME_NOT_FOUND.split("{name}")[0] in exc_info.value.message
        assert exc_info.value.details is None


class TestApply:
    def test_none_resolved_returns_original(self):
        r = SupplierNameResolver()
        assert r.apply("评估供应商 10105", None) == "评估供应商 10105"

    def test_code_regex_returns_original(self):
        r = SupplierNameResolver()
        resolved = ResolvedKey(key="10105", resolved_by="code_regex", original_name=None)
        assert r.apply("评估供应商 10105 的风险", resolved) == "评估供应商 10105 的风险"

    def test_name_path_replaces_once(self):
        r = SupplierNameResolver()
        resolved = ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )
        assert (
            r.apply("评估供应商 济南吉利汽车有限公司 的风险", resolved)
            == "评估供应商 10105 的风险"
        )


class TestHelpers:
    def test_format_candidates(self):
        assert (
            _format_candidates([("10105", "甲公司"), ("10106", "乙公司")])
            == "10105 甲公司 | 10106 乙公司"
        )

    def test_rows_to_pairs(self):
        assert _rows_to_pairs([("10105", "甲公司")]) == [["10105", "甲公司"]]


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_supplier_name_resolver.py -q
# 期望：全部 ERROR（ModuleNotFoundError: app.services.supplier_name_resolver）
```

- [ ] **Step 3: 实现**

创建 `backend/app/services/supplier_name_resolver.py`：

```python
"""供应商名称 → enterprise_code 预解析（Phase 6.5）。

ChatService / AgentRuntimeService 顶层共用：在数字正则未命中时，
把用户消息里的中文供应商名（entity_mapping.name）解析为 enterprise_code
并替换回消息文本，下游 arg_extractor / 意图分类 / NL2SQL 零感知。

解析顺序：
1. 数字正则（extractSupplierAnyKey）→ 命中即返回（零 DB 开销）
2. 中文名提取（「供应商/supplier」后跟 2-30 字符中文段）
3. entity_mapping.name 精确匹配（==）
4. entity_mapping.name 模糊匹配（ilike %name%）

失败语义（均 raise ValidationError）：
- not_found    — 0 命中
- ambiguous    - (1, 50) 条命中 → details.candidates 列全量
- over_limit   — ≥ 50 条命中 → details.candidate_count（不列全量，防响应过大）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EntityType
from app.domain.error_messages import (
    MSG_SUPPLIER_NAME_AMBIGUOUS,
    MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT,
    MSG_SUPPLIER_NAME_NOT_FOUND,
)
from app.domain.exceptions import ValidationError
from app.domain.models import EntityMapping
from app.services.intent_service import extractSupplierAnyKey

logger = logging.getLogger(__name__)

# 候选展示上限：≥ 此值视为「过宽」，错误消息只给数量不列全量
# （真实业务 SUPPLIER 约 3500+，LIKE '%汽车%' 易命中数百条）
_CANDIDATE_DISPLAY_LIMIT = 50

# 「供应商/supplier」后跟中文段（至少 1 汉字，总长 2-30；支持 · - 数字字母空格）
_NAME_EXTRACT_RE = re.compile(
    r"(?:供应商|supplier)\s*[:：]?\s*(?P<name>[一-龥][一-龥A-Za-z0-9·\-\s]{1,29})"
)


class ResolvedKey(NamedTuple):
    """成功路径的解析结果；失败路径由 resolve() 抛 ValidationError。"""

    key: str  # enterprise_code
    resolved_by: Literal["code_regex", "name_exact", "name_like"]
    original_name: str | None  # apply() 用作 replace 源；code_regex 路径为 None


def _format_candidates(rows: Sequence[tuple[object, str]]) -> str:
    """(code, name) 行 → '10105 济南吉利汽车有限公司 | 10106 ...'（错误消息用）。"""
    return " | ".join(f"{code} {name}" for code, name in rows)


def _rows_to_pairs(rows: Sequence[tuple[object, str]]) -> list[list[str]]:
    """(code, name) 行 → [[code, name], ...]（ValidationError.details.candidates 用）。"""
    return [[str(code), str(name)] for code, name in rows]


class SupplierNameResolver:
    """顶层名字→编码预解析（详见模块 docstring）。"""

    async def resolve(
        self, message: str, session: AsyncSession
    ) -> ResolvedKey | None:
        """成功 → ResolvedKey；失败 → ValidationError；无关键词 → None。"""
        # Pass 0：数字正则（复用既有意图抽取器，保持「供应商 10105」判定一致）
        numeric = extractSupplierAnyKey(message)
        if numeric is not None:
            return ResolvedKey(key=numeric, resolved_by="code_regex", original_name=None)

        # Pass 1：中文名提取；无关键词 → 让下游 pipeline 自行处理
        match = _NAME_EXTRACT_RE.search(message)
        if match is None:
            return None
        name = match.group("name").strip()

        # Pass 2：精确匹配（理论上 (entity_type, name) 唯一；多条属脏数据，按歧义处理）
        exact_rows = (
            (
                await session.execute(
                    select(EntityMapping.enterprise_code, EntityMapping.name)
                    .where(
                        EntityMapping.entity_type == EntityType.SUPPLIER,
                        EntityMapping.name == name,
                    )
                    .limit(2)
                )
            )
            .all()
        )
        if len(exact_rows) == 1:
            return ResolvedKey(
                key=str(exact_rows[0][0]), resolved_by="name_exact", original_name=name
            )
        if len(exact_rows) > 1:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name, n=len(exact_rows),
                    candidates=_format_candidates(exact_rows),
                ),
                details={"candidates": _rows_to_pairs(exact_rows)},
            )

        # Pass 3：LIKE 模糊匹配（无 trigram index，3500 行顺序扫 O(ms)，暂不引 pg_trgm）
        like_rows = (
            (
                await session.execute(
                    select(EntityMapping.enterprise_code, EntityMapping.name).where(
                        EntityMapping.entity_type == EntityType.SUPPLIER,
                        EntityMapping.name.ilike(f"%{name}%"),
                    )
                )
            )
            .all()
        )
        n = len(like_rows)
        if n == 1:
            return ResolvedKey(
                key=str(like_rows[0][0]), resolved_by="name_like", original_name=name
            )
        if 1 < n < _CANDIDATE_DISPLAY_LIMIT:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name=name, n=n, candidates=_format_candidates(like_rows),
                ),
                details={"candidates": _rows_to_pairs(like_rows)},
            )
        if n >= _CANDIDATE_DISPLAY_LIMIT:
            raise ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT.format(
                    name=name, limit=_CANDIDATE_DISPLAY_LIMIT
                ),
                details={"candidate_count": n, "name": name},
            )

        # 0 命中
        logger.info("SupplierNameResolver 未命中 name=%s", name)
        raise ValidationError(MSG_SUPPLIER_NAME_NOT_FOUND.format(name=name))

    def apply(self, message: str, resolved: ResolvedKey | None) -> str:
        """把 message 中 original_name 替换为 key（仅 name 路径；immutable 字符串返回）。"""
        if resolved is None or resolved.original_name is None:
            return message
        return message.replace(resolved.original_name, resolved.key, 1)
```

- [ ] **Step 4: 运行确认通过**

```bash
cd backend && uv run pytest app/tests/unit/test_supplier_name_resolver.py -q
# 期望：全部 PASS
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/supplier_name_resolver.py \
  backend/app/tests/unit/test_supplier_name_resolver.py
git commit -m "feat(supplier): SupplierNameResolver name-to-code resolution with candidate details"
```

---

### Task 3: AgentRuntimeService 集成 + 单测扩展

**Files:**
- Modify: `backend/app/services/agent_runtime_service.py`（run 方法，arg_extractor 调用在 line 114）
- Test: `backend/app/tests/unit/test_agent_runtime_service.py`（扩展）

**Interfaces:**
- Consumes: Task 2 的 `SupplierNameResolver` / `ResolvedKey`。
- Produces: `AgentRuntimeService(registry=…, agentService=…, resolver=…)` 新可选参数（默认 `SupplierNameResolver()`）。

- [ ] **Step 1: 写失败测试**（追加到 `test_agent_runtime_service.py` 的既有 class 之后，新增一个 class；`_run` / `_fakeTool` / `_agent` / `_runtime` 复用既有 helper）

```python
class _FakeResolver:
    """按预设返回/抛错的 resolver 替身（单测不触 DB）。"""

    def __init__(
        self,
        resolved: object = None,
        error: Exception | None = None,
    ) -> None:
        self._resolved = resolved
        self._error = error

    async def resolve(self, message, session):  # noqa: ARG001
        if self._error is not None:
            raise self._error
        return self._resolved

    def apply(self, message, resolved):  # noqa: ARG001
        if resolved is None or getattr(resolved, "original_name", None) is None:
            return message
        return message.replace(resolved.original_name, resolved.key, 1)


class TestRunSupplierNamePreResolve:
    """Phase 6.5：run() 在 arg_extractor 前做名字→编码预解析。"""

    def _runtimeWith(self, resolver, entity=None):
        registry = AgentToolRegistry()
        registry.register(_fakeTool("supplier_risk"))
        return AgentRuntimeService(
            registry=registry,
            agentService=_FakeAgentService(entity or _agent("SUPPLIER_RISK_AGENT")),
            resolver=resolver,
        )

    def test_name_resolved_to_code_before_extractor(self):
        resolver = _FakeResolver(
            resolved=ResolvedKey(
                key="10105", resolved_by="name_exact",
                original_name="济南吉利汽车有限公司",
            )
        )
        service = self._runtimeWith(resolver)
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 济南吉利汽车有限公司 的风险",
            )
        )
        # fakeTool 的 extractor 是 lambda raw: {"key": "100001"}——不足以验证替换；
        # 用专门 extractor 断言 arg_extractor 收到的已是被替换文本
        assert run.tool == "supplier_risk"

    def test_replaced_text_reaches_extractor(self):
        captured: dict = {}

        def extractor(raw: str):
            captured["raw"] = raw
            return {"key": "10105"}

        registry = AgentToolRegistry()
        registry.register(_fakeTool("supplier_risk", extractor=extractor))
        service = AgentRuntimeService(
            registry=registry,
            agentService=_FakeAgentService(_agent("SUPPLIER_RISK_AGENT")),
            resolver=_FakeResolver(
                resolved=ResolvedKey(
                    key="10105", resolved_by="name_exact",
                    original_name="济南吉利汽车有限公司",
                )
            ),
        )
        _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 济南吉利汽车有限公司 的风险",
            )
        )
        assert captured["raw"] == "评估供应商 10105 的风险"

    def test_resolver_validation_error_propagates(self):
        from app.domain.error_messages import MSG_SUPPLIER_NAME_AMBIGUOUS

        resolver = _FakeResolver(
            error=ValidationError(
                MSG_SUPPLIER_NAME_AMBIGUOUS.format(
                    name="吉利", n=2, candidates="10105 甲 | 10106 乙"
                ),
                details={"candidates": [["10105", "甲"], ["10106", "乙"]]},
            )
        )
        service = self._runtimeWith(resolver)
        with pytest.raises(ValidationError) as exc_info:
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="供应商 吉利",
                )
            )
        assert exc_info.value.details["candidates"][0] == ["10105", "甲"]

    def test_numeric_input_bypasses_resolver_db(self):
        """数字输入 → resolver 返回 code_regex → apply 原样 → 既有行为回归保护。"""
        resolver = _FakeResolver(
            resolved=ResolvedKey(
                key="10105", resolved_by="code_regex", original_name=None
            )
        )
        service = self._runtimeWith(resolver)
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 10105 的风险",
            )
        )
        assert run.tool == "supplier_risk"
```

测试文件顶部 import 区追加：

```python
from app.services.supplier_name_resolver import ResolvedKey
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_agent_runtime_service.py -q
# 期望：TestRunSupplierNamePreResolve 4 个用例 FAIL（TypeError: unexpected keyword 'resolver'）
```

- [ ] **Step 3: 实现**

`backend/app/services/agent_runtime_service.py`：

3a. import 区（line 43-49 agent_tools import 之后）追加：

```python
from app.services.supplier_name_resolver import SupplierNameResolver
```

3b. `__init__`（line 73-80）加参数：

```python
    def __init__(
        self,
        *,
        registry: AgentToolRegistry | None = None,
        agentService: AgentRegistryService | None = None,
        resolver: SupplierNameResolver | None = None,  # Phase 6.5：名字→编码预解析
    ) -> None:
        self._registry = registry or agent_tool_registry
        self._agents = agentService or AgentRegistryService()
        self._resolver = resolver or SupplierNameResolver()
```

3c. `run()` 内（line 112-118），把

```python
        tool = self._resolveTool(tool_names[0])
        self._enforcePolicies(entity, tool)

        args = tool.arg_extractor(input_text)
```

替换为：

```python
        tool = self._resolveTool(tool_names[0])
        self._enforcePolicies(entity, tool)

        # Phase 6.5：供应商名→编码预解析（数字未命中时查 entity_mapping.name；
        # 失败 raise ValidationError → 全局 handler 422 + details.candidates）
        resolved = await self._resolver.resolve(input_text, session)
        input_text = self._resolver.apply(input_text, resolved)

        args = tool.arg_extractor(input_text)
```

- [ ] **Step 4: 运行确认通过 + 既有回归**

```bash
cd backend && uv run pytest app/tests/unit/test_agent_runtime_service.py -q
# 期望：全部 PASS（既有 25 个 + 新增 4 个；数字输入用例不受影响）
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_runtime_service.py \
  backend/app/tests/unit/test_agent_runtime_service.py
git commit -m "feat(agent-runtime): pre-resolve supplier name to code before arg_extractor"
```

---

### Task 4: ChatService 集成（非流式 + 流式两个入口）

**Files:**
- Modify: `backend/app/services/chat_service.py`（__init__ line 319-340、processMessage line 366-382、processMessageStream line 1576-1595）

**Interfaces:**
- Consumes: Task 2 的 `SupplierNameResolver`；Task 1 的 `ValidationError`；既有 `_storeSessionMessages` / `classifyResult` / `StreamEvent` / `EVENT_ERROR` / `ErrorType`。
- Produces: `ChatService(..., supplierNameResolver=…)` 新可选参数；私有 `_prepareSupplierQuestion(session, dto) -> ChatRequest`。

- [ ] **Step 1: 写失败测试**（新建 `backend/app/tests/integration/test_chat_supplier_name.py`，结构沿用 `test_chat_supplier_360.py`；集成测试真实 PG + fakes——见 Task 5 完整代码，此处先写非流式两条核心用例占位驱动实现）

> 注：ChatService 无独立单测基建（既有 chat 测试全部走真实 PG 集成），本 Task 的测试就是 Task 5 的集成测试文件。先写其中两条（TDD RED），实现后 GREEN，Task 5 再补全其余用例。

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_chat_supplier_name.py -q
# 期望：FAIL（chat 对名字输入返回 supplierKey 缺失引导，而非按名字解析）
```

- [ ] **Step 3: 实现**

`backend/app/services/chat_service.py`：

3a. import 区追加（与其他 app.services import 同区）：

```python
from app.services.supplier_name_resolver import SupplierNameResolver
```

3b. `__init__` 签名（line 339 `agentRuntimeService` 之后）加参数：

```python
        supplierNameResolver: SupplierNameResolver | None = None,  # Phase 6.5：名字预解析
```

`__init__` body（line 362 `self._agentRuntime = …` 之后）加：

```python
        # Phase 6.5：supplier name → code 预解析（非流式/流式两入口共用）
        self._supplierNameResolver = supplierNameResolver or SupplierNameResolver()
```

3c. 新私有方法（放在 `_classifyMessage` line 554 之前）：

```python
    async def _prepareSupplierQuestion(
        self, session: AsyncSession, dto: ChatRequest
    ) -> ChatRequest:
        """Phase 6.5：supplier name → code 预解析（immutable replace，下游零感知）。

        成功 → 返回替换后的新 dto（model_copy，不原地修改）；
        无关键词 / 纯数字编码 → 原样返回 dto（零 DB 开销）；
        解析失败（not_found / ambiguous / over_limit）→ 抛 ValidationError，
        由两个入口分别转为友好 answer / error 事件（chat 惯例，见 processMessage）。
        """
        resolved = await self._supplierNameResolver.resolve(dto.question, session)
        if resolved is None or resolved.original_name is None:
            return dto
        return dto.model_copy(
            update={"question": self._supplierNameResolver.apply(dto.question, resolved)}
        )
```

3d. `processMessage`（line 382 `result, state = await self._classifyMessage(...)` 之前）插入：

```python
        # Phase 6.5：supplier name → code 预解析（immutable replace）
        try:
            dto = await self._prepareSupplierQuestion(session, dto)
        except ValidationError as exc:
            # chat 惯例（与 _handleSupplier360 NotFoundError 同模式）：返回友好
            # answer（含候选）而非 422；4-4：本轮也持久化消息，历史链不断。
            preResult = self._intent.classifyResult(dto.question)
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, exc.message, None
            )
            return ChatResponse(answer=exc.message, intent=preResult.intent.value)

        result, state = await self._classifyMessage(session, dto)
```

3e. `processMessageStream`（line 1595 `result = self._intent.classifyResult(dto.question)` 之前）插入：

```python
        # Phase 6.5：supplier name → code 预解析（同 processMessage；流式入口覆盖）
        try:
            dto = await self._prepareSupplierQuestion(session, dto)
        except ValidationError as exc:
            # 流式惯例（与下方 DomainError 分支同型）：结构化 error 事件
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, exc.message, None
            )
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": exc.message,
                    "errorType": ErrorType.DOMAIN.value,
                    "detail": exc.detail,
                },
            )
            return

        result = self._intent.classifyResult(dto.question)
```

（确认 `ValidationError` 已在 chat_service.py 的 import 中；若无需加入 `from app.domain.exceptions import … ValidationError`。）

- [ ] **Step 4: 运行确认通过**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_chat_supplier_name.py -q
# 期望：PASS
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_service.py backend/app/tests/integration/test_chat_supplier_name.py
git commit -m "feat(chat): supplier name pre-resolution on both chat entries with friendly error surface"
```

---

### Task 5: 集成测试补全（Agent Runtime REST + chat 全场景）

**Files:**
- Test: `backend/app/tests/integration/test_agent_runtime_supplier_name.py`（新）
- Test: `backend/app/tests/integration/test_chat_supplier_name.py`（Task 4 已建，补全）

**Interfaces:**
- Consumes: 既有 `client` / `dbSession` fixture（`app/tests/integration/conftest.py`）；`_seedAgent` 模式（`test_agent_runtime_api.py:77`）；chat fakes 模式（`test_chat_supplier_360.py:112-189`）。

- [ ] **Step 1: 写 test_chat_supplier_name.py 完整版**

```python
"""Chat 供应商名称解析集成测试（Phase 6.5）。

覆盖 processMessage / processMessageStream 两个入口的名字→编码预解析：
- 精确名：问「供应商 济南吉利汽车有限公司 的 360° 视图」→ 等价于问 10105
- LIKE 歧义：问「供应商 济南吉利」→ 200 + answer 含候选编码
- not_found：问「供应商 不存在的名字」→ 200 + answer 含引导文案
- 数字回归：问「供应商 10105 的 360° 视图」→ 原行为不变
- 流式：同名消息 → SSE 结构化事件序列正常结束
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}

_NAME_SUPPLIER_KEY = 910505
_NAME_SUPPLIER_CODE = "910505"
_NAME_AMBIGUOUS_KEY = 910506
_NAME_AMBIGUOUS_CODE = "910506"


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9301,
        name="ds-chat-name",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


async def _seedNamedSupplier(
    dbSession: AsyncSession, key: int, code: str, name: str
) -> None:
    dbSession.add(
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
            name=name,
        )
    )
    await dbSession.commit()


async def _seedOtdFeature(dbSession: AsyncSession, code: str) -> None:
    dbSession.add(
        FeatureDefinition(
            id=9302,
            feature_name="SUPPLIER_OTD_3M",
            feature_alias="SUPPLIER_OTD_3M",
            feature_definition="auto",
            entity_type=EntityType.SUPPLIER,
            calculation_logic="SELECT 1",
            window_size="3M",
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit="%",
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=9301,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=9302,
            entity_key=code,
            value=92.5,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )
    await dbSession.commit()


@pytest.mark.asyncio
async def test_chat_name_exact_resolves_to_code(
    client: AsyncClient, dbSession: AsyncSession
):
    """精确名 → 替换为 code → 走既有 supplier_360 拦截 → 返回 360 视图。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名精确供应商甲"
    )
    await _seedOtdFeature(dbSession, _NAME_SUPPLIER_CODE)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-1",
            "question": "供应商 测试名精确供应商甲 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert _NAME_SUPPLIER_CODE in body["answer"]
    assert body["supplier360"] is not None
    assert body["supplier360"]["profile"]["enterpriseCode"] == _NAME_SUPPLIER_CODE


@pytest.mark.asyncio
async def test_chat_name_ambiguous_returns_candidates_in_answer(
    client: AsyncClient, dbSession: AsyncSession
):
    """LIKE 命中 2 条 → 200 + answer 列出候选编码（chat 惯例：不抛 422）。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名歧义供应商甲"
    )
    await _seedNamedSupplier(
        dbSession, _NAME_AMBIGUOUS_KEY, _NAME_AMBIGUOUS_CODE, "测试名歧义供应商乙"
    )

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-2",
            "question": "供应商 测试名歧义 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("supplier360") is None
    assert _NAME_SUPPLIER_CODE in body["answer"]
    assert _NAME_AMBIGUOUS_CODE in body["answer"]
    assert "候选" in body["answer"]


@pytest.mark.asyncio
async def test_chat_name_not_found_returns_guidance(
    client: AsyncClient, dbSession: AsyncSession
):
    """0 命中 → 200 + answer 引导用 enterprise_code 重试。"""
    await _seedDatasource(dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-3",
            "question": "供应商 测试名不存在的公司 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "10105" in body["answer"]  # 引导文案含示例编码
    assert "拼写" in body["answer"] or "重试" in body["answer"]


@pytest.mark.asyncio
async def test_chat_numeric_code_unchanged(
    client: AsyncClient, dbSession: AsyncSession
):
    """数字编码回归保护：原行为完全不变。"""
    await _seedDatasource(dbSession)
    await _seedNamedSupplier(
        dbSession, _NAME_SUPPLIER_KEY, _NAME_SUPPLIER_CODE, "测试名精确供应商甲"
    )
    await _seedOtdFeature(dbSession, _NAME_SUPPLIER_CODE)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-name-4",
            "question": f"供应商 {_NAME_SUPPLIER_CODE} 的 360° 视图",
            "datasourceId": 9301,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert body["supplier360"]["profile"]["enterpriseCode"] == _NAME_SUPPLIER_CODE
```

- [ ] **Step 2: 写 test_agent_runtime_supplier_name.py**

```python
"""Agent Runtime 供应商名称解析集成测试（Phase 6.5）。

覆盖 POST /api/v1/agents/{code}/run 的名字→编码预解析（真实 PG + 完整 API 链路）：
- 精确名 → 200（等价于数字编码输入）
- LIKE 歧义 → 422 + details.candidates
- 0 命中 → 422 + 引导文案
- 数字回归 → 原行为不变
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EntityType, MatchRule, SourceSystem
from app.domain.models import EntityMapping

from app.tests.integration.test_agent_runtime_api import _seedAgent

AUTH_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_RT_SUPPLIER_KEY = 920505
_RT_SUPPLIER_CODE = "920505"
_RT_AMBIGUOUS_KEY = 920506
_RT_AMBIGUOUS_CODE = "920506"


async def _seedNamedSupplier(
    dbSession: AsyncSession, key: int, code: str, name: str
) -> None:
    dbSession.add(
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
            name=name,
        )
    )
    await dbSession.commit()


async def _runAgent(client: AsyncClient, inputText: str):
    return await client.post(
        "/api/v1/agents/SUPPLIER_360_AGENT/run",
        headers=AUTH_HEADERS,
        json={"input": inputText},
    )


@pytest.mark.asyncio
async def test_runtime_name_exact_returns_200(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时供应商甲"
    )
    resp = await _runAgent(client, "查询供应商 测试运行时供应商甲 的 360° 视图")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agentCode"] == "SUPPLIER_360_AGENT"
    # 解析路径：profile.enterprise_code 命中替换后的编码
    assert body["result"]["profile"]["enterpriseCode"] == _RT_SUPPLIER_CODE


@pytest.mark.asyncio
async def test_runtime_name_ambiguous_returns_422_with_candidates(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时歧义甲"
    )
    await _seedNamedSupplier(
        dbSession, _RT_AMBIGUOUS_KEY, _RT_AMBIGUOUS_CODE, "测试运行时歧义乙"
    )
    resp = await _runAgent(client, "查询供应商 测试运行时歧义 的情况")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    candidates = body["details"]["candidates"]
    assert [c[0] for c in candidates] == [_RT_SUPPLIER_CODE, _RT_AMBIGUOUS_CODE]


@pytest.mark.asyncio
async def test_runtime_name_not_found_returns_422(
    client: AsyncClient, dbSession: AsyncSession
):
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    resp = await _runAgent(client, "查询供应商 测试运行时不存在 的情况")
    assert resp.status_code == 422, resp.text
    assert "未在主数据中找到" in resp.json()["error"]


@pytest.mark.asyncio
async def test_runtime_numeric_regression(
    client: AsyncClient, dbSession: AsyncSession
):
    """数字编码输入回归保护：与既有 test_agent_runtime_api 同语义。"""
    await _seedAgent(dbSession, "SUPPLIER_360_AGENT")
    await _seedNamedSupplier(
        dbSession, _RT_SUPPLIER_KEY, _RT_SUPPLIER_CODE, "测试运行时供应商甲"
    )
    resp = await _runAgent(client, f"查询供应商 {_RT_SUPPLIER_CODE} 的 360° 视图")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["profile"]["enterpriseCode"] == _RT_SUPPLIER_CODE
```

> 注：`_seedAgent` 从 `test_agent_runtime_api.py` import——若该模块有 import 副作用问题（应无，纯函数 + 模块级常量），则把 `_seedAgent` + `_defaultPolicies` 复制到本文件并注明来源。

- [ ] **Step 3: 运行两个集成测试文件**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_chat_supplier_name.py \
    app/tests/integration/test_agent_runtime_supplier_name.py -v
# 期望：全部 PASS
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/tests/integration/test_chat_supplier_name.py \
  backend/app/tests/integration/test_agent_runtime_supplier_name.py
git commit -m "test(supplier): name resolution integration coverage for chat and agent runtime"
```

---

### Task 6: 全量回归 + Harness 变更记录 + 收尾

**Files:**
- Create: `Harness/changes/feat-supplier-name-resolver/summary.md`
- Modify: `Harness/wiki/business-domain.md`（Agent Runtime / Chat 章节各补一句）

- [ ] **Step 1: 全量后端回归 + 覆盖率门槛**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
# 期望：全部 PASS，覆盖率 ≥ 80%
```

> 若出现与本 feature 无关的既有失败，记录在 summary.md 并向 controller 报告，不要静默跳过。

- [ ] **Step 2: 手动 e2e 冒烟（真实数据）**

```bash
docker compose up -d --build backend
curl -s -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "Content-Type: application/json" -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -d '{"input":"查询供应商 济南吉利汽车有限公司 的情况"}' | python3 -m json.tool | head -20
# 期望：200 + result.profile.enterpriseCode == "10105"
curl -s -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "Content-Type: application/json" -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -d '{"input":"查询供应商 吉利 的情况"}' | python3 -m json.tool
# 期望：422 + details.candidates（真实 THBI 数据含多个"吉利"）
```

- [ ] **Step 3: Harness 变更记录**

创建 `Harness/changes/feat-supplier-name-resolver/summary.md`：

```markdown
# feat-supplier-name-resolver

> 日期：2026-09-01 | 状态：done | Spec: docs/superpowers/specs/2026-09-01-supplier-name-resolver-design.md

## 目标

供应商分析支持中文名 + enterprise_code 双路输入。用户说「评估供应商 济南吉利汽车有限公司
的风险」与「评估供应商 10105 的风险」语义等价；名字歧义返回候选列表。

## 实现

- `SupplierNameResolver`（新）：数字正则 → name 精确 → name LIKE 三级解析；
  失败 raise ValidationError（not_found / ambiguous / over_limit，details 携带候选）
- `ValidationError.details`（新通道）+ `ErrorResponse.details` + 全局 handler 透传
- `AgentRuntimeService.run`：arg_extractor 前预解析（REST 422 + details.candidates）
- `ChatService.processMessage / processMessageStream`：classify 前预解析；
  失败转友好 answer（非流式）/ SSE error 事件（流式）——chat 端不抛 422
- 3 个错误消息常量（error_messages.py）

## 验证

- 单测：resolver 13 用例 + runtime 4 用例 + details 4 用例（全绿）
- 集成：chat 4 用例 + runtime 4 用例（真实 PG 5433）
- 全量回归：pytest --cov-fail-under=80 通过
- 手动 e2e：济南吉利汽车有限公司 → 200 (10105)；吉利 → 422 + candidates

## 关键决策

- arg_extractor 保持同步（顶层预解析零侵入，避免改 AgentTool API）
- 候选 ≥ 50 条 → 只给数量不列全量（响应体保护）
- chat 端错误面遵循「永远有 answer」惯例（_handleSupplier360 同模式）；
  422 仅保留给 Agent Runtime REST 端点
```

- [ ] **Step 4: Wiki 补充 + 最终 commit**

`Harness/wiki/business-domain.md` Agent Runtime 章节补一句：

```markdown
- **供应商名称解析（Phase 6.5）**：run 入口支持中文名（entity_mapping.name）与
  enterprise_code 双路输入；名字→编码预解析在 arg_extractor 之前，歧义返回
  422 + 候选列表（chat 端转友好 answer）。
```

```bash
git add Harness/changes/feat-supplier-name-resolver/summary.md Harness/wiki/business-domain.md
git commit -m "docs(harness): supplier name resolver change record + wiki"
```

---

## Self-Review 结论

1. **Spec 覆盖**：spec 的组件（resolver / details 通道 / 3 常量）、两个 runtime 集成点、chat 两个入口、错误面三契约、单测 11 条 + 集成用例全覆盖于 Task 1-5。
2. **无 placeholder**：所有步骤含完整代码。
3. **类型一致**：`ResolvedKey(key, resolved_by, original_name)` 在 Task 2 定义、Task 3 测试引用一致；`ValidationError(message, *, detail, details)` 签名在 Task 1 定义、Task 2/3/4 使用一致；`details.candidates` / `details.candidate_count` 键名全链路一致。
4. **注意**：Task 4 与 Task 5 共享 `test_chat_supplier_name.py`（Task 4 先建核心 2 条，Task 5 补全）——执行时 Task 4 的 Step 1 直接写 Task 5 Step 1 的前两条用例即可。
