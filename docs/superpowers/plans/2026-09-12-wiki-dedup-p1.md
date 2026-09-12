# Wiki 去重（P1：内容派生身份）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `wiki_page.page_id` 成为**内容的函数**（`PAGE-<slug(title)>-<sha256(source_ref\x00title\x00content)[:8].upper()>`），使同一份文件重跑得到同一个 ID，于是导入路径既有的「冲突检查 + `uq_wiki_page_page_id`」原样生效、重复项根本落不了库。

**Architecture:** 不新建去重机制、不新增去重表、不读 `page_ids` 台账。把随机后缀换成内容派生后缀（一次纯函数改动），P1 的唯一新增机制是**台账分类学修正**：`page_id` 撞车且 `content_hash` 相等 → 计「跳过」而非「失败」。配套一次 Alembic 迁移（三处 DDL）：`wiki_page.content_hash VARCHAR(64) NULL` + 非唯一索引（内容摘要，P0 的 `document_catalog.content_hash` 落地时同源消费）、`document_catalog.content_hash` 唯一索引（spec §4.7 推迟到 P1 的那条）、`wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`（台账三元计数）。

**P1 与 P0 / P3 解耦**：本计划不依赖 P0 的 `TextBlock` / `source_locator` / `document_catalog` 读写代码，也不依赖 P3 的 `wiki_compile_*` 表。两者只有一个**算法口径**上的约定：本计划的 `contentHashOf()` 与 P0 的 `source_locator.hashText()` 同为 `sha256(utf-8).hexdigest()`，先落地的一方负责把另一份收敛掉（见下方「复用而非新建」callout）。两边可任意顺序落地。

唯一的接触点是 **0061 迁移里的一条 DDL**：spec §4.7 的 D2-2 把 `document_catalog.content_hash` 的唯一约束**明确推迟到 P1「一并做，只付一次迁移成本」**，P1 就是现在，所以本次补上（只建索引，不动该表任何列，也不需要 P0 的任何代码）。交接约束：**P0 落地时不得重复创建该索引**（`IF NOT EXISTS` 会让重复创建静默通过，看起来没事，但两处定义会各自漂移）。已实测 prod：该列存在、表 0 行、索引尚未创建 —— 成本为零的窗口。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.x async / Pydantic v2 / Alembic / PostgreSQL 16（`qa_metadata` prod + `qa_metadata_test` 测试）/ pytest（真实 PG + 完整 API 链路）/ React + TypeScript + antd + i18next（台账列）/ vitest。

## Global Constraints

- **真实 PostgreSQL + 完整 API 链路，禁止 sqlite 内存库**（含 `sqlite+aiosqlite:///:memory:`）—— 来自 `Harness/rules/测试规范.md`；集成测试从 HTTP 入口发起，经路由 → 中间件 → service → 真实 PG。
- **集成测试库固定 `qa_metadata_test`**，通过 `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test` 注入（端口是 **5433**，不是 5432）—— 来自 `Harness/rules/数据库环境使用规范.md` 与 `Harness/wiki/operations-runbook.md`。
- **表结构变更直接改 prod `qa_metadata`**，改前必须备份受影响表，命名 `<原表名>_<YYYYMMDD>` → 本次 `wiki_page_20260912` 与 `wiki_import_task_20260912` —— 来自 `数据库环境使用规范.md`（规则不区分轻重，即使只是 `ADD COLUMN`）。
- **Alembic 只认 `DATABASE_URL`（默认指向 5432/prod）**：传 `TEST_DATABASE_URL` 会被静默忽略并打到 prod。要给测试库跑迁移必须显式 `DATABASE_URL=<test url> alembic upgrade head`。
- **迁移 head 起点 = `0060_schema_reconcile`**（`alembic/versions/0060_reconcile_menu_index_and_users_legacy_cols.py`）；本计划新增 `0061_wiki_dedup`，`down_revision = "0060_schema_reconcile"`。
- **TDD：RED → GREEN → IMPROVE**；后端 `pytest --cov=app --cov-fail-under=80`，前端 `vitest run --coverage` —— 来自 `Harness/rules/开发流程规范.md` 第 4-8 阶段。
- **函数 < 50 行；文件 200-400 行、不超过 800 行；嵌套 ≤ 4 层** —— 来自 `Harness/rules/编码规范.md`。
- **不可变数据**：不原地改传入对象；派生值走新列 / 新对象，服务层不做就地 mutation —— 来自项目 `CLAUDE.md` 核心约束 1。
- **显式错误处理**：服务端记录详细上下文（logger 带 taskId / pageId / title），UI 侧给中文友好文案 —— 来自项目 `CLAUDE.md` 核心约束 6。
- **命名**：函数/变量 `camelCase`，类型 `PascalCase`，常量 `UPPER_SNAKE_CASE`；ORM / Pydantic 字段 `snake_case`（与 DB 列及 JSON 契约一致，刻意偏离 PEP 8）—— 来自项目 `CLAUDE.md`。
- **注释与 docstring 用中文**，且写「为什么」不写「做了什么」。
- **Conventional Commits**（`feat:` / `fix:` / `test:` / `chore:`，可带 scope）；关闭 Attribution。
- **开发门禁**：本变更必须交付 `backend/scripts/wiki_dedup_realdata.py` + `Harness/changes/feat-wiki-dedup/summary.md` 的 §9「真实数据验证报告」，**缺失即该变更不通过** —— 来自 `开发流程规范.md`「每轮真实数据验证（开发门禁）」。
- **HITL**：`0061_wiki_dedup` 属数据模型变更，migration 脚本须人工 review 后才可合并 —— 来自 `开发流程规范.md`「人工介入点」。
- **部署门禁**：每个目标环境的 `SELECT version_num FROM alembic_version` 必须等于 `0061_wiki_dedup`，且新增列对应的端点（`POST /api/v1/wiki/import/execute`）在目标环境冒烟返回非 5xx —— 来自 `开发流程规范.md`「数据库迁移同步（部署门禁）」。

---

## 背景：P1 的根因（已在代码中实测确认）

```
_importOne(wiki_import_service.py:447) → generatePageId(title)
  → wiki_page_service.py:156  suffix = uuid.uuid4().hex[:8].upper()   ← 随机，非内容派生
  → 冲突检查(wiki_import_service.py:449-453) 永不命中
  → uq_wiki_page_page_id(0053:54) 永不冲突
  → 副本必落库（实测：557 行页面只有 75 个不同标题 = 8 个导入任务 × 75 页）
```

**关键判断（spec §5.1 原话）**：P1 不是「把 `page_ids` 读起来」，而是让 `page_id` 成为内容的函数。`page_ids` 是结果，不是手段。

四个已实测的代码事实（本计划据此设计，勿再假设）：

| 事实 | 证据 |
|---|---|
| `wiki_import_task.page_ids` **只写不读**（无任何生产读取方） | `grep page_ids app/` 只命中写入（`wiki_import_service.py:295/345/417`）与无关的多态数组列（`knowledge_conflict.page_ids`） |
| `wiki_learning_models.py:172-173` 的「幂等重放」docstring **是假的** | 上一条 + `test_wiki_batch_delete_api.py:556` 自述「按设计只写不读」 |
| `wiki_page` 上**已存在** `content_hash` 的同类先例 | `document_catalog.content_hash: Mapped[str \| None] = mapped_column(String(64))`（`app/domain/models.py:1288`） |
| 会话工厂 `expire_on_commit=False` / `autoflush=False` | `app/infrastructure/database.py:46-47` → `_importOne` 可安全跨 commit 读 `task.source_ref` |

### 三个必须写进计划的取舍（不在 spec 字面里，但回避不了）

**D1-1 重复的判定依据 = `page_id` 撞车 **且** `content_hash` 相等。**
spec §5.4 只说「重复 → 成功但跳过」，没说怎么证明「这是重复」。只用 ID 撞车不够安全：`[:8]` 只有 32 bit，截断哈希碰撞（概率极低但非零）会把**另一份文档**静默丢掉，而丢知识比报错严重得多。故：
- `content_hash` 相等 → 确证重跑 → 跳过（`DuplicatePageError` → `skipped_pages += 1`）。
- `content_hash` 不等，或任一侧为 `NULL`（0061 之前的历史行）→ 不可判定 → 保持 `ConflictError` → 计失败。
「宁可报错，不静默丢」是这条规则的唯一理由。

**D1-2 `POST /api/v1/wiki/pages` 的撞号仍然返回 409，不改判为跳过。**
与导入路径**故意不同**：`createPage` 是用户显式「我要新建一条」的单条动作，ID 被占用就该 409 让人自己决定；导入是批处理，重跑不该被记成失败。`test_wiki_api.py:71-72`（`test_duplicate_page_id_returns_409`）因此**保持不变**。

**D1-3 `content` 被 PATCH 修改时 `content_hash` 必须重算，但 `page_id` 不变。**
`content_hash` 是「这条条目**当前**正文的摘要」这一事实，不是「创建时正文的摘要」。不重算会产生两个方向的错误：把真正的冲突误判成重跑（静默丢知识），或把重跑误判成冲突（假性失败）。`page_id` 则**刻意不可变** —— 它被 `knowledge_claim.page_id` / `knowledge_relation.upstream_page_id` / `evidence` 二级链以 FK 引用，改 ID 等于断开引用。

---

## 文件结构

| 文件 | 状态 | 职责 |
|---|---|---|
| `backend/app/services/wiki_page_service.py` | 改 `:20-21`（imports）、`:64-68`（常量）、`:148-157`（`generatePageId`）、`:403-446`（`createPage`）、`:474-486`（`updatePage` 循环） | 新增 `contentHashOf()` / `_identityDigest()`；`generatePageId` 改内容派生；create/update 落 `content_hash` |
| `backend/app/domain/exceptions.py` | 改文件尾（`~:170` 之前） | 新增 `DuplicatePageError(ConflictError)` |
| `backend/app/services/messages_zh.py` | 改 `:307` 附近 | 新增 `MSG_WIKI_PAGE_DUPLICATE_SKIPPED` |
| `backend/app/services/wiki_import_service.py` | 改 `:74-78`（imports）、`:305-334`（计数器与 except）、`:343-350`（台账赋值）、`:389-426`（`_markFailedBestEffort`）、`:428-481`（`_importOne`）、`:483+` 不变 | 内容派生 ID、重复 → 跳过、`skipped_pages` 记账 |
| `backend/app/domain/wiki_models.py` | 改 `WikiPage`（`:118-141` 区间） | 加 `content_hash` ORM 列 |
| `backend/app/domain/wiki_learning_models.py` | 改 `WikiImportTask`（`:168-230` 区间） | 更正不实 docstring + 加 `skipped_pages` 列 |
| `backend/app/domain/wiki_schemas.py` | 改 `WikiImportTaskRead`（`:363-382`） | 暴露 `skipped_pages` |
| `backend/alembic/versions/0061_wiki_dedup.py` | 新建 | `wiki_page.content_hash` + 非唯一索引；`document_catalog.content_hash` 唯一索引（spec §4.7 推迟到 P1 的那条）；`wiki_import_task.skipped_pages` |
| `backend/app/tests/unit/test_wiki_page_id.py` | 新建 | 纯函数：确定性、字面公式、中文 slug 折叠、长度上界、`contentHashOf` |
| `backend/app/tests/integration/test_wiki_dedup_api.py` | 新建 | 真实 PG + 完整 API 链路：迁移列、create/patch 落 hash、幂等重放、同标题异内容、sourceRef 边界、台账无关性 |
| `backend/app/tests/integration/test_wiki_import_api.py` | 改 `:781-808`、`:841-866` | 两个既有测试的语义随 D1-1 更新 |
| `backend/app/tests/integration/test_wiki_api.py` | 改 `:36-48` | 钉死「API 生成的 page_id 是确定性的」 |
| `backend/scripts/wiki_dedup_realdata.py` | 新建 | 真实数据验证脚本（开发门禁） |
| `frontend/src/types/wikiImport.ts` | 改 `:62-63` 附近 | 加 `skippedPages: number` |
| `frontend/src/pages/AdminWikiImportPage.tsx` | 改 `:324-326`、`:546-552` | 台账加「跳过」列 + 结果摘要带跳过数 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 改 `wikiImport.columns` 与 `wikiImport.resultCounts` | 新增 `skipped` 文案 |
| `frontend/src/tests/AdminWikiImportPage.test.tsx` | 改 `:60-80`、`:125-140` | i18n mock 加 key + fixture 加 `skippedPages` |
| `Harness/changes/feat-wiki-dedup/summary.md` | 新建 | SSOT 九段变更记录 |

> 复用而非新建：`contentHashOf()` 与 P0 的 `document_catalog.content_hash` **同口径**（`sha256(utf-8).hexdigest()`，64 位小写十六进制）。
>
> **先落地的一方负责收敛为一份实现**：P0 的 `app/infrastructure/source_locator.py` 提供了
> `hashText(text)` —— 与 `contentHashOf` 逐字节等价。P0 落地后，把 `contentHashOf` 改为
> `from app.infrastructure.source_locator import hashText` 的薄包装（或直接替换调用点），
> **不要保留两份 `sha256` 实现**。反向亦然：若本计划先落地，P0 落地时收敛到 `hashText`。
> 两边可以任意顺序落地，不互相阻塞。

---

### Task 1: 内容派生 `page_id` 与内容摘要（纯函数层）

**Files:**
- Modify: `backend/app/services/wiki_page_service.py:20-21`（import）、`:64-68`（常量）、`:148-157`（`generatePageId`）
- Test: `backend/app/tests/unit/test_wiki_page_id.py`（新建）

**Interfaces:**
- Consumes: 无（本任务是全计划的叶子依赖）
- Produces:
  - `contentHashOf(content: str) -> str` —— sha256 小写十六进制，恒 64 字符
  - `_identityDigest(sourceRef: str, title: str, content: str) -> str` —— sha256 小写十六进制，恒 64 字符（模块私有）
  - `generatePageId(title: str, sourceRef: str = "", content: str = "") -> str` —— 形如 `PAGE-<SLUG>-<8位大写HEX>`，长度 ≤ 64
  - 常量 `_IDENTITY_HASH_LEN: int = 8`、`_IDENTITY_SEPARATOR: str = "\x00"`

> 本任务按 `Harness/rules/测试规范.md`「例外（仅限无 IO 的纯逻辑）」用**纯单测**：`generatePageId` / `contentHashOf` 不触 DB、不触网络。真实 PG + 完整 API 链路的验证在 Task 2/3/5 的集成测试里。

- [ ] **Step 1: Write the failing test**

新建 `backend/app/tests/unit/test_wiki_page_id.py`：

```python
"""内容派生 page_id 的纯函数测试（feat-wiki-dedup P1）。

不触 DB / 不触网络，按 Harness/rules/测试规范.md「例外（仅限无 IO 的纯逻辑）」
条款可直接单测。真实 PG + 完整 API 链路的验证在
``app/tests/integration/test_wiki_dedup_api.py``。

这里**必须**用字面量锁死派生公式（而不是只断言「两次调用相等」）：只断言相等
挡不住把 sha256 换成 md5 之类的「确定但不同」的改写 —— 那会让改造前已落库的
page_id 与改造后算出的不一致，重复项照样入库，而测试全绿。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \\
        .venv/bin/pytest app/tests/unit/test_wiki_page_id.py -q
"""

from __future__ import annotations

from app.services.wiki_page_service import (
    contentHashOf,
    generatePageId,
)

# 夹具取值与 test_wiki_api.py 的 _createPage 默认值一致，便于两个套件对照
_TITLE = "供应商准入规则"
_CONTENT = "注册资本 >= 1000 万"


def test_same_inputs_produce_same_page_id() -> None:
    """同 (sourceRef, title, content) → 同 ID：这是本次去重的全部依据。"""
    assert generatePageId(_TITLE, "", _CONTENT) == generatePageId(_TITLE, "", _CONTENT)


def test_page_id_is_not_random() -> None:
    """回归：旧实现拼 ``uuid.uuid4().hex[:8]``，同一标题每次不同 → 冲突检查永不命中。

    20 次调用必须收敛到 1 个值。旧实现在这里会拿到 20 个不同的 ID。
    """
    ids = {generatePageId("同一份知识", "", "同样的正文") for _ in range(20)}
    assert len(ids) == 1


def test_page_id_formula_is_pinned_by_literal() -> None:
    """钉死派生公式的**字面输出**（换哈希算法/换分隔符都会打红）。"""
    assert generatePageId(_TITLE, "", _CONTENT) == "PAGE-UNTITLED-012CA6C8"
    assert (
        generatePageId("Supplier Qualification", "", "x")
        == "PAGE-SUPPLIER-QUALIFICATION-0747A11B"
    )


def test_content_change_changes_page_id() -> None:
    """正文变 = 另一条知识（不再撞号，各建一条）。"""
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        _TITLE, "", "注册资本 > 2000 万"
    )


def test_source_ref_change_changes_page_id() -> None:
    """来源参与派生。

    同一份文件重跑时 sourceRef 也相同（导入台账记的是同一个来源），故不影响
    去重；两个部门各写一份同样条款则算两条知识。spec §5.2 的公式如此规定，
    边界与代价见 summary 风险段。
    """
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        _TITLE, "policy.md", _CONTENT
    )


def test_chinese_title_folds_to_untitled_slug() -> None:
    """纯中文标题的 slug 折叠成 ``UNTITLED``。

    钉住既有 ASCII 字符集契约（``_PAGE_ID_SANITIZE`` 只放行
    ASCII 字母/数字/连字符/下划线，中文一个字符都不剩）的后果：中文场景下
    可读性由哈希后缀兜底、slug 恒定。这是 P1 明确接受的代价，不是 bug。
    """
    assert generatePageId(_TITLE, "", _CONTENT).startswith("PAGE-UNTITLED-")
    # 两个不同的中文标题 slug 相同，但整体 ID 不同（标题参与哈希）
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        "供应商 准入 规则", "", _CONTENT
    )


def test_overlong_title_still_fits_page_id_column() -> None:
    """超长标题截断到 40 字符后，整体长度 = 5 + 40 + 1 + 8 = 54 ≤ VARCHAR(64)。"""
    pageId = generatePageId("x" * 200, "", "c")
    assert len(pageId) == 54
    assert pageId == f"PAGE-{'X' * 40}-2E3AC84B"


def test_content_hash_is_sha256_lowercase_hex() -> None:
    """content_hash 写库的原值：64 位小写十六进制（口径与 document_catalog 一致）。"""
    assert (
        contentHashOf(_CONTENT)
        == "5dd995a8688226c1fc01cc593b6bce29b2b1b96fcde0feca24ddaab3a13d4041"
    )
    assert contentHashOf("x") == (
        "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    )
    assert contentHashOf("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_content_hash_is_not_the_page_id_suffix() -> None:
    """``content_hash`` 与 page_id 后缀**不同源**：全量 sha256(content) vs
    身份哈希 sha256(source_ref\\x00title\\x00content) 的前 8 位。

    回归点：把两者混用不会报错，但会让「同 ID 同内容 → 跳过」的判定永远不
    成立（或永远成立），重复项静默入库。
    """
    assert not generatePageId(_TITLE, "", _CONTENT).endswith(
        contentHashOf(_CONTENT)[:8].upper()
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_wiki_page_id.py -q
```

Expected: FAIL —— 收集期即报错：

```
ImportError: cannot import name 'contentHashOf' from 'app.services.wiki_page_service'
```

- [ ] **Step 3: Write minimal implementation**

改 `backend/app/services/wiki_page_service.py`。

3a. import 段（现 `:20-21`）—— 删掉 `uuid`（改用内容哈希后不再需要，留着会被 lint 抓为未使用），加 `hashlib`：

```python
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
```

3b. 常量段（现 `:64-68`）—— 追加三个常量，保留原有三个：

```python
_PAGE_ID_SANITIZE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_PAGE_ID_LEN = 64
_MAX_SLUG_LEN = 40

# page_id 后缀的哈希位数（16 进制）。8 位 = 32 bit，对单库万级条目足够；真撞上
# 时不会静默丢知识 —— _importOne 还会比对 content_hash，不等则判为「同 ID 不同
# 内容」的冲突（计失败）而不是跳过。
_IDENTITY_HASH_LEN = 8

# 身份哈希的字段分隔符。用 NUL 而不是 ``|`` / ``-``：标题与来源里合法出现的
# 任何字符都不该能伪造出「字段边界」—— ``("ab", "c")`` 与 ``("a", "bc")``
# 必须哈希不同。
_IDENTITY_SEPARATOR = "\x00"
```

3c. 替换 `generatePageId`（现 `:148-157`），并在其上方新增两个函数：

```python
def contentHashOf(content: str) -> str:
    """正文的 SHA-256 十六进制摘要（小写，恒 64 字符）。

    与 RAG 路径写进 ``document_catalog.content_hash`` 的**同一口径**
    （``sha256(utf-8 bytes).hexdigest()``），因此两处摘要可直接比对。P0 落地
    「导入路径也登记 document_catalog」时复用本函数，不要再写第二份实现 ——
    两份实现迟早会在编码/大小写上漂移，而漂移了不会报错，只会让比对永远不等。
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _identityDigest(sourceRef: str, title: str, content: str) -> str:
    """身份哈希：``sha256(source_ref \\x00 title \\x00 content)`` 的十六进制。

    用**内容**（而非随机数）回答「这条知识是不是已经在了」。字段以 NUL 分隔，
    保证 ``("ab", "c")`` 与 ``("a", "bc")`` 不会撞成同一条。
    """
    payload = _IDENTITY_SEPARATOR.join((sourceRef, title, content))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generatePageId(title: str, sourceRef: str = "", content: str = "") -> str:
    """由**内容**派生稳定可读的 page_id（业务专家未显式提供时）。

    形如 ``PAGE-<SLUG>-<8位身份哈希>``。与旧实现（``PAGE-<SLUG>-<8位随机>``）
    的唯一差别是**幂等**：同一份知识重跑得到同一个 ID，于是导入路径既有的
    「冲突检查 + ``uq_wiki_page_page_id``」原样生效，重复项根本落不了库 ——
    不需要引入第二套去重机制，也不需要有人去读 ``wiki_import_task.page_ids``。

    ``sourceRef`` 参与派生：同一标题同一正文但来源不同（两个部门各写了一份
    同样的模板）是**两条**知识。同一份文件重跑时来源也相同，故仍会合并。
    调用方负责把 ``None`` 归一成 ``""``（签名是 str，不接受 None）。

    注意 slug 沿用 ``_PAGE_ID_SANITIZE`` 的 ASCII 字符集，故**纯中文标题会折叠
    成 ``UNTITLED``**：唯一性由哈希后缀保证，可读性在中文场景是净损失。这是
    刻意保留既有字符集契约（page_id 要能安全放进 ``GET /wiki/pages/{pageId}``
    的路径段）的结果，不是疏忽。
    """
    slug = _PAGE_ID_SANITIZE.sub("-", title.strip()).strip("-").upper()
    slug = slug[:_MAX_SLUG_LEN].strip("-") or "UNTITLED"
    digest = _identityDigest(sourceRef, title, content)[:_IDENTITY_HASH_LEN].upper()
    return f"PAGE-{slug}-{digest}"[:_MAX_PAGE_ID_LEN]
```

> `createPage` / `_importOne` 的调用点在本任务**不动**（默认参数让它们在 Task 3 / Task 4 各自更新），因此本任务结束时 `generatePageId(dto.title)` 等价于 `generatePageId(dto.title, "", "")` —— 同一标题会算出同一个 ID，但 Task 3 会立刻补上 `content`，最终形态见 Task 3。Task 1 与 Task 2 之间不部署，中间态不影响任何已合并的测试。

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit/test_wiki_page_id.py -q
```

Expected: PASS —— `9 passed`。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wiki_page_service.py backend/app/tests/unit/test_wiki_page_id.py
git commit -m "feat(wiki): page_id 改为内容派生 + contentHashOf（P1 去重地基）"
```

---

### Task 2: 迁移 `0061_wiki_dedup` + ORM 两列 + `document_catalog` 唯一索引

**Files:**
- Create: `backend/alembic/versions/0061_wiki_dedup.py`
- Modify: `backend/app/domain/wiki_models.py`（`WikiPage`，`:118-141` 区间）
- Modify: `backend/app/domain/wiki_learning_models.py`（`WikiImportTask`，`:189-230` 区间）
- Test: `backend/app/tests/integration/test_wiki_dedup_api.py`（新建，本任务只放两个 schema 断言）

**Interfaces:**
- Consumes: Task 1 无（本任务不依赖派生函数）
- Produces:
  - Alembic revision `0061_wiki_dedup`，`down_revision = "0060_schema_reconcile"`
  - DB 列 `wiki_page.content_hash VARCHAR(64) NULL` + 索引 `ix_wiki_page_content_hash`（**非唯一**）
  - DB 列 `wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`
  - ORM `WikiPage.content_hash: Mapped[str | None]`
  - ORM `WikiImportTask.skipped_pages: Mapped[int]`

> **同名不同约束：为什么 `wiki_page` 这场必须非唯一、`document_catalog` 那场必须唯一。**
> spec §5.3 只说 `wiki_page` 要「加列 + 索引」；§4.7 D2-2 另说把 `content_hash` 的**唯一约束**推迟到 P1 —— 指的是 `document_catalog.content_hash`（本计划一并建，见 Step 3）。
> `wiki_page.content_hash` 则刻意**不加唯一约束**，两个理由：(1) 同一段正文出现在两条知识里合法（共享模板、多部门引用同一条款），唯一约束会把正常业务变成 500；(2) 唯一约束一旦存在，「同 ID 同内容 → 跳过」这条分支在 INSERT 前就炸了，永远走不到。
> 两者方向相反是因为语义不同：`document_catalog` 一行 = 一份**源文档**（重复上传应映射同一份）；`wiki_page` 一行 = 一条**知识**（同一段话可以是两条知识）。**这是本计划对 spec 的一处显式解释，需在 summary §3 记录。**

- [ ] **Step 1: Write the failing test**

新建 `backend/app/tests/integration/test_wiki_dedup_api.py`：

```python
"""P1 去重集成测试（feat-wiki-dedup）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
从 HTTP 入口发起，经路由校验 → service → 真实 PG，数据准备与断言都落在真实行上。

覆盖：
- 迁移 0061 的三处 DDL 确实生效（两列的列类型/可空 + 两个索引的名字/唯一性方向）
- POST /wiki/pages 落 content_hash；PATCH content 后哈希跟着变、page_id 不变
- 同一份文件导入两次 → wiki_page 恰 N 行，第二次全部计入 skippedPages
- 同标题不同内容 → 两条（不再撞号）
- 同内容不同 sourceRef → 两条（spec 公式的显式边界）
- 台账被删光后重放仍然跳过（钉死「跳过不依赖 page_ids」）

断言一律走 **raw SQL**：本项目的集成测试踩过 SQLAlchemy 2.x identity map
返回「还在」假象的坑（见 seed upsert 的教训），台账计数尤其要用裸 SQL 读。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \\
        .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_IMPORT = "/api/v1/wiki/import"


async def _countPages(dbSession: AsyncSession) -> int:
    return (
        await dbSession.execute(text("SELECT count(*) FROM wiki_page"))
    ).scalar_one()


async def _taskRow(dbSession: AsyncSession, taskId: int) -> dict[str, Any]:
    """裸 SQL 读台账行（绕开 identity map）。"""
    row = (
        await dbSession.execute(
            text(
                "SELECT status, total_pages, success_pages, skipped_pages, "
                "failed_pages, page_ids, error_message "
                "FROM wiki_import_task WHERE id = :id"
            ),
            {"id": taskId},
        )
    ).mappings().one()
    return dict(row)


# ---------------------------------------------------------------------------
# 迁移 0061
# ---------------------------------------------------------------------------


async def test_wiki_page_content_hash_column_exists(
    dbSession: AsyncSession,
) -> None:
    """0061 之后 wiki_page.content_hash 存在：VARCHAR(64) 且可空。"""
    rows = (
        await dbSession.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'wiki_page' AND column_name = 'content_hash'"
            )
        )
    ).mappings().all()
    assert len(rows) == 1, "wiki_page.content_hash 缺失 —— 0061_wiki_dedup 未应用"
    assert rows[0]["data_type"] == "character varying"
    assert rows[0]["character_maximum_length"] == 64
    assert rows[0]["is_nullable"] == "YES"


async def test_wiki_page_content_hash_index_is_not_unique(
    dbSession: AsyncSession,
) -> None:
    """索引存在且**非唯一**（同一段正文出现在两条知识里是合法的）。"""
    indexDef = (
        await dbSession.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'wiki_page' "
                "AND indexname = 'ix_wiki_page_content_hash'"
            )
        )
    ).scalar_one_or_none()
    assert indexDef is not None, "ix_wiki_page_content_hash 缺失"
    assert "UNIQUE" not in indexDef.upper(), (
        "content_hash 索引变成了唯一索引 —— 共享模板的正常写入会 500，"
        "且「同 ID 同内容 → 跳过」分支永远走不到"
    )


async def test_import_task_skipped_pages_column_exists(
    dbSession: AsyncSession,
) -> None:
    """0061 之后 wiki_import_task.skipped_pages 存在：NOT NULL DEFAULT 0。"""
    row = (
        await dbSession.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'wiki_import_task' "
                "AND column_name = 'skipped_pages'"
            )
        )
    ).mappings().one_or_none()
    assert row is not None, (
        "wiki_import_task.skipped_pages 缺失 —— 0061_wiki_dedup 未应用"
    )
    assert row["is_nullable"] == "NO"
    assert row["column_default"] is not None and "0" in row["column_default"]


async def test_document_catalog_content_hash_index_is_unique(
    dbSession: AsyncSession,
) -> None:
    """0061 补上 spec §4.7 推迟到 P1 的 ``content_hash`` 唯一约束。

    与 ``wiki_page`` 那两个索引方向相反：``document_catalog`` 的行 = 一份源文档，
    同一份文件重复上传必须映射到同一份文档，所以这里**要**唯一。
    （``wiki_page.content_hash`` 反而必须非唯一 —— 共享模板是合法的。两处
    同名不同约束不是笔误，见 0061 模块 docstring。）

    另外确认它**允许 NULL 重复**：PostgreSQL 的唯一索引把 NULL 视为互不相等，
    所以尚未算出摘要的行不会被挡。这条断言把该依赖钉住 —— 换数据库或改成
    ``NULLS NOT DISTINCT`` 时立刻打红。
    """
    indexDef = (
        await dbSession.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'document_catalog' "
                "AND indexname = 'uq_document_catalog_content_hash'"
            )
        )
    ).scalar_one_or_none()
    assert indexDef is not None, (
        "uq_document_catalog_content_hash 缺失 —— spec §4.7 的唯一约束未落地"
    )
    assert "UNIQUE" in indexDef.upper()
    assert "NULLS NOT DISTINCT" not in indexDef.upper(), (
        "索引变成了 NULLS NOT DISTINCT —— content_hash 为 NULL 的行会互相冲突，"
        "而 P1 不回填历史行，写入会立刻炸"
    )
    assert indexDef.upper().count("(content_hash)") == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
```

Expected: FAIL —— 测试库仍在 `0060_schema_reconcile`（`_pg_support._ensureSchema` 会先跑一次 `upgrade head`，但 0061 文件还不存在），三条都打红：

```
AssertionError: wiki_page.content_hash 缺失 —— 0061_wiki_dedup 未应用
```

- [ ] **Step 3: Write minimal implementation**

3a. 新建 `backend/alembic/versions/0061_wiki_dedup.py`：

```python
"""wiki 去重地基：content_hash 落列与加约束 + 台账 skipped_pages（P1）。

三处改动同属 P1，故同一次迁移交付（spec §4.7 D2-2 的「只付一次迁移成本」）：

1. ``wiki_page.content_hash VARCHAR(64)`` —— 正文的 sha256 十六进制（小写 64 位），
   与 ``document_catalog.content_hash`` **同类型同口径**。用途有二（spec §5.2）：
   给 P0 的 ``document_catalog.content_hash`` 提供同源值；让「同 ID 不同内容」与
   「同文件重跑」在数据层可判定（导入路径据此把后者记成跳过、前者记成冲突）。
   **刻意不加唯一约束**：同一段正文出现在两条知识里是合法的（共享模板、多部门
   引用同一条款），唯一约束会把正常写入变成 500；且唯一约束一旦存在，
   「同 ID 同内容 → 跳过」的分支在 INSERT 前就炸了，永远走不到。
2. ``document_catalog.content_hash`` 加**唯一索引** —— spec §4.7 的 D2-2 明确把
   「`content_hash` 唯一约束」推迟到 P1「一并做，只付一次迁移成本」，P1 就是
   现在，故本次补上。实测 prod：该列已存在（`VARCHAR(64) NULL`）、
   `document_catalog` **0 行**、且**还没有**这个索引 —— 与 `wiki_page` 一样，
   这是加约束最便宜的时刻（有数据之后要先查重再清理才能建）。
   **两个库都已实测**：prod `qa_metadata` 与测试库 `qa_metadata_test` 的
   `document_catalog` 均为 0 行（后者版本号 = `0060_schema_reconcile`），
   故 `upgrade head` 在两边都不会因重复摘要而失败。
   语义：一条 catalog 行 = 一份源文档，同一份文件重复上传应映射到同一份文档。
   **PostgreSQL 的唯一索引允许多个 NULL**（NULL 互不相等），所以现存/未来的
   `content_hash IS NULL` 行不会被挡 —— 约束只作用于「已经算出了摘要」的行。
   建索引失败是**安全失败**（DDL 是原子的，要么建成要么报错），不会静默丢数据。
   **防撞车**：P0 落地时不得再建同名/同义索引（见 summary §3 与 §9 的交接项）。

3. ``wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`` —— P1 之后重复项
   不再计失败（spec §5.4）。不新增这一列的话，「本次 8 条全是重复」会退化成
   ``success_pages=0, failed_pages=0`` 的第三种状态，运维无从区分「任务没跑」
   与「跑了但都已入库」。

**幂等**：``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``，
与 0053-0060 同模式 —— prod 是从 dump 恢复出来的，重复执行必须是 no-op。

**downgrade 是对称的**（删索引 + 删列）：三处 DDL 都由本迁移新建，不存在 0060
那种「列早于迁移且带真实数据」的情形。删列会丢掉已回填的 content_hash、删索引
会放开重复上传，但那正是 downgrade 的语义 —— 0060 的系统里没有任何代码读这些
对象，回退后不会留下半截状态。

Revision ID: 0061
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0061_wiki_dedup"
down_revision: str | None = "0060_schema_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE wiki_page ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)"
    )
    # 非唯一索引：去重判定走 page_id（已有 uq_wiki_page_page_id），本索引服务的是
    # 「这份正文还出现在哪些条目里」这类排查/审计查询。不加唯一约束的理由见
    # 模块 docstring。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_page_content_hash "
        "ON wiki_page (content_hash)"
    )
    # spec §4.7 D2-2 把「content_hash 唯一约束」推迟到 P1 一并做，本次补上。
    # 作用在 document_catalog（P0 的表）而非 wiki_page：一条 catalog 行 = 一份
    # 源文档，同一份文件重复上传应映射到同一份文档。P1 只建索引，不碰该表其它列，
    # 也不依赖 P0 的任何代码。
    # PostgreSQL 唯一索引允许多个 NULL，故 content_hash IS NULL 的行不受影响。
    # 目标表实测 0 行 → 无重复可清理，直接建即可；建失败会原子回滚（安全失败）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_catalog_content_hash "
        "ON document_catalog (content_hash)"
    )
    op.execute(
        "ALTER TABLE wiki_import_task ADD COLUMN IF NOT EXISTS skipped_pages "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE wiki_import_task DROP COLUMN IF EXISTS skipped_pages")
    op.execute("DROP INDEX IF EXISTS uq_document_catalog_content_hash")
    op.execute("DROP INDEX IF EXISTS ix_wiki_page_content_hash")
    op.execute("ALTER TABLE wiki_page DROP COLUMN IF EXISTS content_hash")
```

3b. `backend/app/domain/wiki_models.py` —— 在 `WikiPage.content` 之后（现 `:122` 后）插入：

```python
    # 正文的 sha256（小写 64 位十六进制），与 document_catalog.content_hash 同口径。
    # 可空：0061 之前的历史行没有值，且 P1 不做回填 —— 代码必须在 NULL 时退化成
    # 「不可判定」而不是当成「内容相同」，见 wiki_import_service._importOne。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

3c. `backend/app/domain/wiki_learning_models.py` —— 在 `WikiImportTask.failed_pages` 之后（现 `:211` 后）插入：

```python
    # P1 起：重复项（page_id 撞车且 content_hash 相同）计入跳过而非失败。
    # 与 success_pages 分开记，是因为「这次跑了但一条都没新建」与「这次确实
    # 新建了 N 条」是两种运维结论，混进一个计数就分不出来。
    skipped_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
```

Expected: PASS —— `4 passed`（`_pg_support._ensureSchema` 已把测试库推到 `0061_wiki_dedup`）。

再验迁移可逆（同一个库上来回一趟，确认 `downgrade` 不会把库留在坏状态）：

```bash
cd backend && DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/alembic downgrade -1 && DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/alembic upgrade head
```

Expected: `Running downgrade 0061_wiki_dedup -> 0060_schema_reconcile` 后 `Running upgrade 0060_schema_reconcile -> 0061_wiki_dedup`，两次均无异常。

- [ ] **Step 5: 备份 prod 受影响表（备份门禁，spec §5.3）**

```bash
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata --table=wiki_page --no-owner > /tmp/wiki_page_20260912.sql
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata --table=wiki_import_task --no-owner > /tmp/wiki_import_task_20260912.sql
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata --table=document_catalog --no-owner > /tmp/document_catalog_20260912.sql
ls -l /tmp/wiki_page_20260912.sql /tmp/wiki_import_task_20260912.sql /tmp/document_catalog_20260912.sql
```

Expected: 三个文件非空（`document_catalog` 现为 0 行，dump 只有 schema，属正常）。若 `pg_dump` 报错则**停止**，不得带着未备份的库继续（备份 cron 当前静默失效，见 `Harness/changes/qa-system-cron-silently-broken` 的复盘）。

建唯一索引前先证明**没有重复的非空摘要**（现状应为 0 行，此步是给未来重跑的人留的安全带）：

```bash
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "SELECT content_hash, count(*) FROM document_catalog WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING count(*) > 1;"
```

Expected: `(0 rows)`。若返回了行，**停止**并先人工裁决保留哪一行 —— 直接建索引会报 `could not create unique index`（原子失败，不会写坏数据，但迁移会中断）。

- [ ] **Step 6: 在 prod 上跑迁移**

```bash
docker exec qa-backend bash -lc 'cd /app && alembic upgrade head'
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "SELECT version_num FROM alembic_version;"
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "\d wiki_page" | grep content_hash
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "\d wiki_import_task" | grep skipped_pages
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_document_catalog_content_hash';"
```

Expected: `version_num = 0061_wiki_dedup`；两个新列都存在；唯一索引的 `indexdef` 里含 `UNIQUE`。

> **注意**：`docker exec qa-backend ... alembic upgrade head` 使用的是容器内的 `DATABASE_URL`（指向 prod `qa_metadata`），这正是部署门禁要求的动作。绝不要在宿主机裸跑 `alembic upgrade head` —— 宿主机默认 DATABASE_URL 指向 5432（OpenMetadata 占用的端口或另一个库），且传 `TEST_DATABASE_URL` 会被 env.py 忽略。

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/0061_wiki_dedup.py backend/app/domain/wiki_models.py backend/app/domain/wiki_learning_models.py backend/app/tests/integration/test_wiki_dedup_api.py
git commit -m "feat(wiki): 0061_wiki_dedup —— wiki_page.content_hash + 台账 skipped_pages"
```

---

### Task 3: `createPage` / `updatePage` 落 `content_hash`

**Files:**
- Modify: `backend/app/services/wiki_page_service.py:403-446`（`createPage`）、`:474-500`（`updatePage` 的字段循环）
- Modify: `backend/app/tests/integration/test_wiki_api.py:36-48`（`test_create_generates_page_id_and_defaults`）
- Test: `backend/app/tests/integration/test_wiki_dedup_api.py`（追加两个测试）

**Interfaces:**
- Consumes: `contentHashOf(content: str) -> str`、`generatePageId(title, sourceRef="", content="") -> str`（Task 1）；列 `wiki_page.content_hash`（Task 2）
- Produces: `POST /wiki/pages` 与 `PATCH /wiki/pages/{pageId}` 之后，`wiki_page.content_hash` 恒等于 `contentHashOf(当前 content)`

> `page_id` 在 PATCH 时**不重算**（理由见 D1-3）。`createPage` 撞号**仍返回 409**（理由见 D1-2）。

- [ ] **Step 1: Write the failing test**

在 `backend/app/tests/integration/test_wiki_dedup_api.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 创建 / 更新落 content_hash
# ---------------------------------------------------------------------------

_CONTENT = "注册资本 >= 1000 万"
_CONTENT_HASH = "5dd995a8688226c1fc01cc593b6bce29b2b1b96fcde0feca24ddaab3a13d4041"


async def test_create_page_writes_content_hash(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /wiki/pages 落 content_hash = sha256(content)（可直接与 document_catalog 比对）。"""
    resp = await client.post(
        _PAGES, json={"title": "供应商准入规则", "content": _CONTENT}
    )
    assert resp.status_code == 201, resp.text
    pageId = resp.json()["pageId"]

    stored = (
        await dbSession.execute(
            text(
                "SELECT content_hash, page_id FROM wiki_page WHERE page_id = :pid"
            ),
            {"pid": pageId},
        )
    ).mappings().one()
    assert stored["content_hash"] == _CONTENT_HASH
    # API 生成的 ID 是确定性的内容派生（不再是随机后缀）
    assert stored["page_id"] == "PAGE-UNTITLED-012CA6C8"


async def test_patch_content_updates_hash_but_keeps_page_id(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH content 后 content_hash 必须重算，page_id 必须**不变**。

    两件事都是刻意的：
    - 哈希不重算 → 「同 ID 同内容 → 跳过」基于过期值判定，会把真冲突误判成重跑
      而静默丢知识（或反之）。
    - page_id 变了 → knowledge_claim / knowledge_relation 的 FK 指向的旧 ID 变成
      悬空，条目在关系网里直接断链。
    """
    created = await client.post(
        _PAGES, json={"title": "供应商准入规则", "content": _CONTENT}
    )
    pageId = created.json()["pageId"]

    resp = await client.patch(
        f"{_PAGES}/{pageId}", json={"content": "注册资本 >= 2000 万"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pageId"] == pageId

    stored = (
        await dbSession.execute(
            text("SELECT content_hash FROM wiki_page WHERE page_id = :pid"),
            {"pid": pageId},
        )
    ).scalar_one()
    assert stored == (
        "87fa50ec95c812008c1482260d4ab13894e579bdeeee3a675d663766169a8914"
    )
```

> 上面两个常量可直接用 venv 复核（本计划里所有哈希/ID 字面量都已这样验过）：
> ```bash
> cd backend && .venv/bin/python -c "from app.services.wiki_page_service import contentHashOf; print(contentHashOf('注册资本 >= 1000 万')); print(contentHashOf('注册资本 >= 2000 万'))"
> ```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q -k "content_hash or keeps_page_id"
```

Expected: FAIL —— `stored["content_hash"]` 读到 `None`：

```
AssertionError: assert None == '5dd995a8688226c1fc01cc593b6bce29b2b1b96fcde0feca24ddaab3a13d4041'
```

- [ ] **Step 3: Write minimal implementation**

3a. `createPage` 的 `pageId` 计算（现 `:418-420`）—— 把 `content` 传进派生：

```python
        _assertDimension(dto.dimension)
        pageId = (
            sanitizePageId(dto.page_id)
            if dto.page_id
            # sourceRef 传 ""：单条创建没有导入来源，与「导入时 sourceRef 为空」
            # 得到同一个 ID，两条路径不会因为参数缺失而算出两种身份。
            else generatePageId(dto.title, "", dto.content)
        )
```

3b. `createPage` 的实体构造（现 `:428-438`）—— 加一行 `content_hash`：

```python
        entity = WikiPage(
            page_id=pageId,
            title=dto.title,
            content=dto.content,
            content_hash=contentHashOf(dto.content),
            dimension=dto.dimension,
            authority_level=dto.authority_level,
            status="DRAFT",
            structure_stage="MARKDOWN",
            version="v1.0",
            created_by_user_id=createdByUserId,
        )
```

3c. `updatePage` 的字段循环（现 `:475-486`）—— 在通用 `setattr` **之前**插入 `content` 分支：

```python
        newDimension = _UNSET_DIMENSION
        for field in dto.model_fields_set:
            value = getattr(dto, field)
            if isinstance(value, _UnsetType):
                continue  # 显式传了哨兵 → 视为「未提供」
            if field == "dimension":
                _assertDimension(value)
                newDimension = value
            elif field == "structure_stage":
                _assertStage(value)
            elif field == "status":
                _assertStatus(value)
            elif field == "content":
                # 正文变了哈希必须跟着变：content_hash 是「这条条目**当前**正文的
                # 摘要」这一事实，不是「创建时正文的摘要」。不更新会让导入路径的
                # 「同 ID 同内容 → 跳过」基于过期值判定 —— 要么把真正的冲突误判成
                # 重跑而静默丢知识，要么把重跑误判成冲突而报假失败。
                # page_id 刻意**不重算**：它被 knowledge_claim / knowledge_relation
                # 以 FK 引用，改 ID 会让已确认的关系变成悬空引用。
                entity.content_hash = contentHashOf(value)
            setattr(entity, field, value)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
```

Expected: PASS —— `5 passed`。

- [ ] **Step 5: 强化既有测试，钉死 API 生成的 ID 是确定性的**

改 `backend/app/tests/integration/test_wiki_api.py:36-48`，把末尾的弱断言换成字面量：

```python
async def test_create_generates_deterministic_page_id(client: AsyncClient) -> None:
    """未提供 pageId 时按**内容**派生；默认 DRAFT + MARKDOWN 阶段。

    回归点（feat-wiki-dedup P1）：生成 ID 的后缀曾是 ``uuid4()[:8]``，同一标题
    每次都不同 → 导入路径的冲突检查永不命中，同一份文件重跑必落副本
    （实测 557 行页面只有 75 个不同标题）。这里锁死字面输出，随机后缀一旦
    回归即打红。
    """
    # Arrange / Act
    body = await _createPage(client)

    # Assert
    assert body["pageId"] == "PAGE-UNTITLED-012CA6C8"
    assert body["title"] == "供应商准入规则"
    assert body["status"] == "DRAFT"
    assert body["structureStage"] == "MARKDOWN"
    assert body["dimension"] is None
    assert body["version"] == "v1.0"
```

跑一遍确认没打坏别处（随机后缀的消失可能被其他套件隐式依赖）：

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_api.py -q
```

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/wiki_page_service.py backend/app/tests/integration/test_wiki_dedup_api.py backend/app/tests/integration/test_wiki_api.py
git commit -m "feat(wiki): 创建/更新落 content_hash，page_id 走内容派生"
```

---

### Task 4: 导入路径：重复项改判为「跳过」而非「失败」

**Files:**
- Modify: `backend/app/domain/exceptions.py`（`statusForError` 之前，`:168` 附近）
- Modify: `backend/app/services/messages_zh.py:307` 附近
- Modify: `backend/app/services/wiki_import_service.py:74-78`（imports）、`:305-334`（循环与 except）、`:343-350`（台账赋值）、`:389-426`（`_markFailedBestEffort`）、`:428-481`（`_importOne`）
- Modify: `backend/app/domain/wiki_schemas.py:363-382`（`WikiImportTaskRead`）
- Modify: `backend/app/tests/integration/test_wiki_import_api.py:781-808`、`:841-866`
- Test: `backend/app/tests/integration/test_wiki_dedup_api.py`（追加两个测试）

**Interfaces:**
- Consumes: `contentHashOf` / `generatePageId`（Task 1）；`WikiPage.content_hash` / `WikiImportTask.skipped_pages`（Task 2）
- Produces:
  - `DuplicatePageError(ConflictError)` —— 携带 `page_id`
  - `WikiImportService.execute` 台账语义：`success_pages` = 新建数、`skipped_pages` = 重复数、`failed_pages` = 冲突/校验失败数
  - `WikiImportTaskRead.skipped_pages: int` → JSON `skippedPages`

- [ ] **Step 1: Write the failing test**

在 `backend/app/tests/integration/test_wiki_dedup_api.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 导入：重复 → 跳过
# ---------------------------------------------------------------------------


async def _execute(
    client: AsyncClient, drafts: list[dict[str, Any]], *, sourceRef: str
) -> dict[str, Any]:
    """跑一次导入（关闭自动分类 → 不调 LLM，把断言集中在去重本身）。"""
    resp = await client.post(
        f"{_IMPORT}/execute",
        json={
            "drafts": drafts,
            "sourceRef": sourceRef,
            "autoClassify": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_replay_same_file_yields_one_row_and_all_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """验收信号（spec §十 P1）：同一份文件导入两次 → wiki_page 恰 N 行。

    第二次不是「失败」而是「跳过」：运维看到的失败数不该因为重跑而虚高
    （spec §5.4）。
    """
    drafts = [
        {"title": "甲", "content": "内容甲"},
        {"title": "乙", "content": "内容乙"},
    ]

    first = await _execute(client, drafts, sourceRef="policy-v1.md")
    assert first["successPages"] == 2
    assert first["skippedPages"] == 0
    assert first["failedPages"] == 0
    assert first["status"] == "SUCCEEDED"
    assert await _countPages(dbSession) == 2

    second = await _execute(client, drafts, sourceRef="policy-v1.md")

    assert second["successPages"] == 0
    assert second["skippedPages"] == 2
    assert second["failedPages"] == 0
    assert second["status"] == "SUCCEEDED"
    assert second["pageIds"] == []  # 跳过路径不伪造台账条目
    assert await _countPages(dbSession) == 2, "重跑产生了副本"

    row = await _taskRow(dbSession, second["id"])
    assert row["skipped_pages"] == 2
    assert row["success_pages"] == 0
    assert row["failed_pages"] == 0
    assert row["error_message"] is None


async def test_replay_id_is_content_derived_not_random(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """page_id 是 (sourceRef, title, content) 的函数 —— 重跑算出同一个 ID。

    这条把「为什么重复项落不了库」的机制本身写进断言：不是靠读台账跳过，
    而是靠 uq_wiki_page_page_id 撞车。
    """
    await _execute(client, [{"title": "甲", "content": "内容甲"}], sourceRef="policy-v1.md")
    ids = list(
        (await dbSession.execute(text("SELECT page_id FROM wiki_page"))).scalars().all()
    )
    assert ids == ["PAGE-UNTITLED-840CCA89"]


async def test_same_title_different_content_creates_second_entry(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同标题不同内容**不是**重复：各自一条（旧实现下两者都表现为 ID 冲突，无法区分）。"""
    await _execute(client, [{"title": "条款", "content": "甲"}], sourceRef="v.md")
    second = await _execute(client, [{"title": "条款", "content": "乙"}], sourceRef="v.md")

    assert second["successPages"] == 1, "新内容被当成了重复而跳过"
    assert second["skippedPages"] == 0
    assert await _countPages(dbSession) == 2

    hashes = list(
        (await dbSession.execute(text("SELECT content_hash FROM wiki_page"))).scalars().all()
    )
    assert len(set(hashes)) == 2, "两条的 content_hash 必须不同"


async def test_explicit_page_id_with_different_content_still_fails(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 pageId 撞车但内容不同 → 仍是冲突（计失败），不得静默跳过。

    这是 D1-1 的守门测试：`[:8]` 只有 32 bit，把「ID 撞车」当作「重复」会
    静默丢掉另一份文档。内容不可判定时宁可报错。
    """
    await _execute(
        client, [{"pageId": "MANUAL-001", "title": "甲", "content": "x"}], sourceRef="m.md"
    )
    second = await _execute(
        client, [{"pageId": "MANUAL-001", "title": "甲", "content": "y"}], sourceRef="m.md"
    )

    assert second["skippedPages"] == 0
    assert second["failedPages"] == 1
    assert second["status"] == "FAILED"
    assert second["errorMessage"]


async def test_explicit_page_id_with_same_content_is_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 pageId 撞车且内容一致 → 确证重跑 → 跳过。"""
    await _execute(
        client, [{"pageId": "MANUAL-002", "title": "甲", "content": "x"}], sourceRef="m.md"
    )
    second = await _execute(
        client, [{"pageId": "MANUAL-002", "title": "甲", "content": "x"}], sourceRef="m.md"
    )

    assert second["skippedPages"] == 1
    assert second["failedPages"] == 0
    assert second["status"] == "SUCCEEDED"
    assert await _countPages(dbSession) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q -k "replay or same_title or explicit_page_id"
```

Expected: FAIL —— 三个层面同时打红：

```
KeyError: 'skippedPages'                                   # 响应里还没有这个字段
assert 2 == 0                                              # successPages：重复项仍被算成功
assert 1 == 2 / AssertionError: 重跑产生了副本               # 无 sourceRef 参与时旧公式不成立
```

- [ ] **Step 3: Write minimal implementation**

3a. `backend/app/domain/exceptions.py` —— 在 `statusForError` **之前**（现 `:168` 附近）新增：

```python
# ---------------------------------------------------------------------------
# feat-wiki-dedup (P1): 内容重复
# ---------------------------------------------------------------------------


class DuplicatePageError(ConflictError):
    """同 ``page_id`` 且 ``content_hash`` 相同 —— 确证是同一份知识的重跑。

    刻意继承 ``ConflictError``：如果它意外逃到 API 层，``statusForError`` 仍给出
    409（语义正确）。但**导入路径必须单独 catch 它**（catch 在 ``ConflictError``
    之前），否则它会被当成失败计数 —— 那正是 P1 要修的分类学错误。
    """

    def __init__(self, message: str, *, page_id: str) -> None:
        super().__init__(message)
        self.page_id = page_id
```

3b. `backend/app/services/messages_zh.py` —— 紧接 `MSG_WIKI_PAGE_DUPLICATE`（现 `:307`）之后新增：

```python
MSG_WIKI_PAGE_DUPLICATE_SKIPPED = "知识条目 ID「{pageId}」已存在且正文一致，本次导入跳过"
```

3c. `backend/app/services/wiki_import_service.py` import 段（现 `:46-50`）—— 加 `DuplicatePageError`：

```python
from app.domain.exceptions import (
    ConflictError,
    DomainError,
    DuplicatePageError,
    ValidationError,
)
```

`:64-73` 的 messages 段加 `MSG_WIKI_PAGE_DUPLICATE_SKIPPED`：

```python
from app.services.messages_zh import (
    MSG_WIKI_IMPORT_ABORTED,
    MSG_WIKI_IMPORT_ALL_FAILED,
    MSG_WIKI_IMPORT_CLASSIFY_INCOMPLETE,
    MSG_WIKI_IMPORT_MODEL_REQUIRED,
    MSG_WIKI_IMPORT_NO_DRAFTS,
    MSG_WIKI_IMPORT_SOURCE_TYPE_INVALID,
    MSG_WIKI_IMPORT_TASK_TYPE_INVALID,
    MSG_WIKI_PAGE_DUPLICATE,
    MSG_WIKI_PAGE_DUPLICATE_SKIPPED,
)
```

`:74-78` 的 page service import 段加 `contentHashOf`：

```python
from app.services.wiki_page_service import (
    WikiPageService,
    contentHashOf,
    generatePageId,
    sanitizePageId,
)
```

3d. `execute` 的计数器与循环（现 `:306-334`）：

```python
        pageIds: list[str] = []
        failedPages = 0
        skippedPages = 0
        classifiedPages = 0

        try:
            for draft in dto.drafts:
                try:
                    # SAVEPOINT 隔离单条：失败只回滚这一条，外层 session 仍可用
                    # （裸 session.rollback() 会把 task 一起卷掉）。
                    async with session.begin_nested():
                        page, classified = await self._importOne(
                            session,
                            task,
                            draft,
                            invoker=invoker,
                            createdByUserId=createdByUserId,
                        )
                except DuplicatePageError as e:
                    # 重复 ≠ 失败（P1，spec §5.4）：同一份知识重跑是幂等成功，
                    # 记进 skipped_pages。**必须排在 ConflictError 之前捕获** ——
                    # 它是 ConflictError 的子类，顺序反了就会走成失败计数，
                    # 失败数依旧虚高，等于没修。
                    logger.info(
                        "导入第 %d 条为重复内容，跳过: %s",
                        len(pageIds) + skippedPages + failedPages + 1,
                        e,
                    )
                    skippedPages += 1
                    continue
                except (ConflictError, ValidationError) as e:
                    # 单条脏数据不毁整批：记失败、继续
                    logger.warning(
                        "导入第 %d 条失败: %s",
                        len(pageIds) + skippedPages + failedPages + 1,
                        e,
                    )
                    failedPages += 1
                    continue

                pageIds.append(page.page_id)
                classifiedPages += 1 if classified else 0
                # 按页提交：部分成功的进度必须落库（避免最后一条挂了全回滚）
                await session.commit()
        except Exception as e:
            # 兜底：task 已在上面 commit 成 RUNNING，任何逃出单条 catch 的异常
            # 都必须先把它标成 FAILED 再上抛，否则台账里永远挂着一条「进行中」
            # 的僵尸任务（用户看到 RUNNING，实际早就没人跑了）。
            logger.exception("导入任务 %s 异常中止", task.id)
            await self._markFailedBestEffort(
                session,
                task,
                cause=e,
                pageIds=pageIds,
                skippedPages=skippedPages,
                failedPages=failedPages,
            )
            raise
```

3e. `execute` 的台账汇总（现 `:345-373`）：

```python
        task.page_ids = pageIds
        task.success_pages = len(pageIds)
        task.skipped_pages = skippedPages
        task.failed_pages = failedPages
        # 成本以 wiki_token_usage 台账为准（SUM），不在 Python 里累加：
        # 「调用成功但输出解析失败」这类路径也会留下计量行，靠累加会漏账。
        task.total_cost_usd = (
            await session.execute(
                select(func.coalesce(func.sum(WikiTokenUsage.cost), 0)).where(
                    WikiTokenUsage.import_task_id == task.id
                )
            )
        ).scalar_one()
        task.finished_time = _now()
        # 「一条都没分类成功」必须在任务上留痕：用户显式选了模型并要求分类，
        # 却拿到 SUCCEEDED + 全部 dimension 为空，是最容易被误读成「模型认为
        # 这些条目无维度」的状态。分类是增强，不该让状态码骗人。
        # 未开自动分类时缺口恒为 0 —— 没请求分类就没有「未分类」这回事。
        classificationGap = len(pageIds) - classifiedPages if invoker else 0
        if classificationGap > 0:
            task.error_message = MSG_WIKI_IMPORT_CLASSIFY_INCOMPLETE.format(
                count=classificationGap
            )
        # skipped 计入「有产出」：整批全是重跑的任务（pageIds 为空）不该落成
        # FAILED —— 那是旧分类学下的假失败，P1 要修的正是它。
        if failedPages == 0 and classificationGap == 0:
            task.status = "SUCCEEDED"
        elif pageIds or skippedPages:
            task.status = "PARTIAL"
        else:
            task.status = "FAILED"
            task.error_message = MSG_WIKI_IMPORT_ALL_FAILED
        session.add(task)
        await session.commit()
        await session.refresh(task)

        logger.info(
            "导入任务 %s 完成: status=%s 新建=%d 跳过=%d 失败=%d 分类成功=%d 成本=%s",
            task.id,
            task.status,
            task.success_pages,
            task.skipped_pages,
            task.failed_pages,
            classifiedPages,
            task.total_cost_usd,
        )
        return task
```

3f. `_markFailedBestEffort`（现 `:389-426`）—— 加 `skippedPages` 形参并写入：

```python
    async def _markFailedBestEffort(
        self,
        session: AsyncSession,
        task: WikiImportTask,
        *,
        cause: Exception,
        pageIds: list[str],
        skippedPages: int,
        failedPages: int,
    ) -> None:
        """尽力把任务标记成 FAILED 落库；失败只记日志，绝不掩盖原始异常。

        中止的诱因可能正是「数据库连不上」，补的那一刀 commit 同样会炸。
        此时若让新异常冒出去，调用方看到的是 commit 的错误而不是真正的病根
        —— 所以这里吞掉，原始异常由调用方继续上抛。

        属性赋值放在 ``rollback()`` **之后**：rollback 会 expire 掉实例上的
        所有属性，先赋值再回滚等于白写（下次访问会把 DB 里的 RUNNING 读回来）。
        """
        # rollback 同样会 expire 主键，先取值供日志与 add 使用
        taskId = task.id
        try:
            # 先清干净：诱因若是「上一次 commit 失败」，session 正卡在
            # PendingRollbackError，不 rollback 的话 add+commit 会再炸一次。
            await session.rollback()
            task.status = "FAILED"
            task.error_message = MSG_WIKI_IMPORT_ABORTED
            # 已 commit 的页是真落库了的，台账要如实反映，否则「失败」看起来
            # 像一条都没进去，实际库里躺着一半。
            task.page_ids = pageIds
            task.success_pages = len(pageIds)
            task.skipped_pages = skippedPages
            task.failed_pages = failedPages
            task.finished_time = _now()
            session.add(task)
            await session.commit()
        except Exception:
            logger.exception(
                "导入任务 %s 标记 FAILED 失败（原始异常: %s）", taskId, cause
            )
```

3g. `_importOne`（现 `:428-481`）—— 内容派生 + 跳过判定：

```python
    async def _importOne(
        self,
        session: AsyncSession,
        task: WikiImportTask,
        draft: WikiImportDraft,
        *,
        invoker: LearningLLMInvoker | None,
        createdByUserId: int | None,
    ) -> tuple[WikiPage, bool]:
        """建一条 Page（可选分类）。返回 (page, 是否给出了分类建议)。

        先查 page_id 冲突**再**调模型：撞号是纯本地就能判定的错误，
        不该白烧一次 LLM 调用。

        撞号分两种（P1，spec §5.2 + §5.4）：
        - **同 content_hash** → ``DuplicatePageError``，导入层计「跳过」。这是
          同一份知识的重跑，跳过它才是幂等。
        - **不同 content_hash，或任一侧为 NULL**（0061 之前的历史行）→
          ``ConflictError``，导入层计「失败」。无法证明是重跑就不能静默丢弃：
          ``page_id`` 后缀只有 32 bit，截断碰撞会把另一份文档悄悄吃掉。
        """
        title = draft.title
        content = draft.content
        contentHash = contentHashOf(content)
        # 台账里的 sourceRef 是归一化输入：None → ""。它是身份的一部分，故必须
        # 在**同一批导入内稳定** —— 重跑同一份文件时台账记的是同一个来源。
        sourceRef = task.source_ref or ""
        # 显式 page_id 走与生成 ID 同一套字符集收敛（斜杠会让条目在
        # GET /wiki/pages/{pageId} 里不可达）
        rawPageId = draft.page_id
        pageId = (
            sanitizePageId(rawPageId)
            if rawPageId
            else generatePageId(title, sourceRef, content)
        )

        existing = (
            await session.execute(
                select(WikiPage.content_hash).where(WikiPage.page_id == pageId)
            )
        ).first()
        if existing is not None:
            # 注意用 ``is not None`` 判行存在、用值判内容：content_hash 可空，
            # 「行存在但哈希为 NULL」与「行不存在」在标量结果里都是 None。
            existingHash = existing[0]
            if existingHash is not None and existingHash == contentHash:
                raise DuplicatePageError(
                    MSG_WIKI_PAGE_DUPLICATE_SKIPPED.format(pageId=pageId),
                    page_id=pageId,
                )
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId))

        suggestion = None
        modelConfigId: int | None = None
        if invoker is not None:
            suggestion, modelConfigId = await self._classify(invoker, title, content)

        page = WikiPage(
            page_id=pageId,
            title=title,
            content=content,
            content_hash=contentHash,
            dimension=suggestion.primary if suggestion else None,
            auto_classification=suggestion.toDict() if suggestion else None,
            status="DRAFT",
            structure_stage="MARKDOWN",
            version="v1.0",
            imported_via_task_id=task.id,
            processing_model_id=modelConfigId,
            created_by_user_id=createdByUserId,
        )
        session.add(page)
        try:
            await session.flush()
        except IntegrityError as e:
            # 「先查后插」挡不住两个并发请求同时通过检查；第二个 INSERT 会
            # 以用户主键冲突出现在 flush 时。转成 ConflictError 让它走
            # 单条失败的分支（PARTIAL），而不是冒成 500 毁掉整批。
            # 这里**不判重复**：竞态下读到的既有行不可信，而重复判定宁可漏
            # （记成失败，人来处置）也不能错（静默丢一份内容不同的文档）。
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId)) from e
        return page, suggestion is not None
```

3h. `backend/app/domain/wiki_schemas.py` `WikiImportTaskRead`（现 `:363-382`）—— 在 `failed_pages` 后加一行：

```python
class WikiImportTaskRead(CamelModel):
    """导入任务读模型。"""

    id: int
    task_type: str
    source_type: str | None = None
    source_ref: str | None = None
    selected_model_id: int | None = None
    fallback_model_id: int | None = None
    status: str
    page_ids: list[str] | None = None
    total_pages: int
    success_pages: int
    # P1 起：重复项（同 ID 同内容）计入跳过而非失败。与 success 分开下发，
    # 否则「跑了但一条都没新建」看起来像任务没干活。
    skipped_pages: int = 0
    failed_pages: int
    total_cost_usd: Decimal
    error_message: str | None = None
    created_by_user_id: int | None = None
    created_time: datetime | None = None
    finished_time: datetime | None = None
```

3i. 更新 `backend/app/tests/integration/test_wiki_import_api.py` 的两个既有测试。

`:781-808`（原 `test_execute_duplicate_page_id_yields_partial`）—— 语义已随 D1-1 变化，整段替换：

```python
async def test_execute_duplicate_page_id_same_content_is_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 pageId 撞号且正文一致 → 跳过（不是失败），其余条目照常入库。

    语义变更点（feat-wiki-dedup P1，spec §5.4）：旧实现把它记成 failedPages=1
    + PARTIAL，于是「同一份文件重跑」在运维眼里是失败。
    """
    modelId = await _seedModel(dbSession)
    # 先占位
    await client.post(
        _PAGES, json={"pageId": "DUP-001", "title": "已存在", "content": "x"}
    )

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [
                    {"pageId": "DUP-001", "title": "撞号", "content": "x"},
                    {"title": "正常条目", "content": "y"},
                ],
                "modelId": modelId,
            },
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "SUCCEEDED"
    assert task["successPages"] == 1
    assert task["skippedPages"] == 1
    assert task["failedPages"] == 0
```

`:841-866`（原 `test_execute_all_failed_marks_failed`）—— 把撞号改成**同 ID 不同内容**（否则新语义下它会变成 SUCCEEDED+skipped，测不到「全失败」这条路径）：

```python
async def test_execute_all_failed_marks_failed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """全部条目都是「同 ID 不同内容」的冲突 → FAILED + 错误文案。

    正文用 ``y``（已存在的占位行是 ``x``）：P1 起「同 ID 同内容」是跳过而非
    失败，只有**内容不可判定为重复**的撞车才计失败 —— 那正是一条都不该放过
    的人工处置场景。
    """
    modelId = await _seedModel(dbSession)
    await client.post(
        _PAGES, json={"pageId": "DUP-ALL", "title": "已存在", "content": "x"}
    )

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"pageId": "DUP-ALL", "title": "撞号", "content": "y"}],
                "modelId": modelId,
            },
        )

    task = resp.json()
    assert task["status"] == "FAILED"
    assert task["skippedPages"] == 0
    assert task["failedPages"] == 1
    assert task["errorMessage"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
```

Expected: PASS —— `10 passed`。

再跑一遍受影响的既有套件，确认没有别处依赖旧语义：

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_import_api.py app/tests/integration/test_wiki_api.py -q
```

Expected: PASS。若有打红项，逐条判定是「测试断言过时」还是「实现有 bug」—— 只有前者可以改测试。

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/exceptions.py backend/app/services/messages_zh.py backend/app/services/wiki_import_service.py backend/app/domain/wiki_schemas.py backend/app/tests/integration/test_wiki_dedup_api.py backend/app/tests/integration/test_wiki_import_api.py
git commit -m "feat(wiki): 导入重复项改判为跳过（DuplicatePageError + skipped_pages 台账）"
```

---

### Task 5: 更正不实文档 + 钉死幂等重放（spec §5.5 验收）

**Files:**
- Modify: `backend/app/domain/wiki_learning_models.py:168-173`（`WikiImportTask` docstring）
- Test: `backend/app/tests/integration/test_wiki_dedup_api.py`（追加两个测试）

**Interfaces:**
- Consumes: Task 4 的 `skipped_pages` 语义
- Produces: 无新接口；本任务是**文档更正**与**反退化证据**

- [ ] **Step 1: Write the failing test**

在 `backend/app/tests/integration/test_wiki_dedup_api.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 钉死 docstring 的声称 / 台账无关性
# ---------------------------------------------------------------------------


async def test_replay_skip_survives_deleted_task_ledger(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """跳过**不依赖** wiki_import_task.page_ids —— 把台账删光后重放仍然跳过。

    spec §5.5 要求「钉死 wiki_learning_models.py:172-173 今天只声称、并不存在
    的幂等重放」。做法是把台账整行删掉（``page_ids`` 随行消失），再导入同一份
    文件：
    - 若跳过靠读 page_ids → 台账没了就跳过不了 → 必落副本 → 断言打红；
    - 实际实现靠内容派生的 page_id 撞车 → 台账在不在都一样跳过。

    诚实边界：本测试证明的是「跳过的**可观测结果**不依赖台账」，不是某条 SQL
    的静态不变量（后者用语句级断言更脆）。对「docstring 的声称是否属实」而言，
    这个强度足够。
    """
    drafts = [
        {"title": "甲", "content": "内容甲"},
        {"title": "乙", "content": "内容乙"},
    ]
    await _execute(client, drafts, sourceRef="ledger-pin.md")
    assert await _countPages(dbSession) == 2

    # 抹掉台账（page_ids 随之消失）。wiki_page.imported_via_task_id 的 FK 是
    # ON DELETE SET NULL，行不会被连带删除。
    await dbSession.execute(text("DELETE FROM wiki_import_task"))
    await dbSession.commit()

    replay = await _execute(client, drafts, sourceRef="ledger-pin.md")

    assert replay["successPages"] == 0
    assert replay["skippedPages"] == 2
    assert await _countPages(dbSession) == 2, (
        "台账被删后就跳过不了了 —— 说明跳过依赖 page_ids，而它按设计只写不读"
    )


async def test_same_content_different_source_ref_is_two_entries(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已知边界：sourceRef 参与身份派生，故同内容不同来源 = 两条。

    spec §5.2 的公式写死了 ``sha256(source_ref \\x00 title \\x00 content)``，
    这里把该公式的**代价**钉成显式行为而不是让它在生产里被偶然发现：用户第一次
    导入留空 sourceRef、第二次填了文件名，同一份文件会得到两个 ID、两条知识。
    缓解方式记录在 summary 风险段（向导层统一要求填 sourceRef，或后续把来源
    归一化为上传文件的内容哈希）。
    """
    await _execute(client, [{"title": "模板条款", "content": "同样的话"}], sourceRef="")
    second = await _execute(
        client, [{"title": "模板条款", "content": "同样的话"}], sourceRef="dept-b.md"
    )

    assert second["successPages"] == 1
    assert second["skippedPages"] == 0
    assert await _countPages(dbSession) == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q -k "ledger or source_ref"
```

Expected: **PASS**（Task 4 已经实现了行为）。这一步刻意是 GREEN —— 它要钉的是**文档**，不是行为。若这里 FAIL，说明 Task 4 的实现与设计不符，回去修实现，不要改这两条测试。

- [ ] **Step 3: 更正不实文档**

改 `backend/app/domain/wiki_learning_models.py:168-173`。原 docstring 声称：

> 本表是幂等可重放的作业台账：`page_ids` 记录产出的知识条目业务键，失败重跑时用来跳过已成功项（部分成功 → status=PARTIAL）。

**这句话今天是假的** —— `grep page_ids app/` 只命中写入点（`wiki_import_service.py:295/345/417`），零读取方；`test_wiki_batch_delete_api.py:556` 自己也写着「按设计只写不读」。替换为：

```python
class WikiImportTask(Base):
    """一次知识导入的作业记录（含模型选择与成本汇总）。

    **``page_ids`` 是只写的快照，不是幂等依据。** 它记录本次**新建**的条目业务
    键，供人查看「这批进了哪些条目」；没有任何运行路径读它来决定要不要跳过。

    幂等（重跑同一份文件不落副本）由 ``wiki_page.page_id`` 是**内容派生**的实现
    保证：同 (source_ref, title, content) 必得同 ID，于是导入路径既有的
    ``page_id`` 冲突检查与 ``uq_wiki_page_page_id`` 原样命中，重复项根本
    落不了库（feat-wiki-dedup P1，见 ``wiki_page_service.generatePageId``）。
    撞车且 ``content_hash`` 相同 = 重跑，计 ``skipped_pages``；撞车但内容不同
    = 需人工处置的冲突，计 ``failed_pages``。

    这段注释过去写着「``page_ids`` …… 失败重跑时用来跳过已成功项」—— 那是一个
    从未实现的声称（写入方 3 处、读取方 0 处），P1 把它改成事实描述。
    ``test_wiki_dedup_api.py::test_replay_skip_survives_deleted_task_ledger``
    把「跳过不依赖台账」钉成可执行断言，防止这句话再次漂回谎言。
    """
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py app/tests/unit/test_wiki_page_id.py -q
```

Expected: PASS —— `12 passed`（集成 10 + 纯单测 9 中的重复项除外，实际输出以收集为准）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/wiki_learning_models.py backend/app/tests/integration/test_wiki_dedup_api.py
git commit -m "docs(wiki): 更正 page_ids 的幂等重放不实声称 + 钉死重放不依赖台账"
```

---

### Task 6: 台账可见性 —— 前端展示「跳过」数

**Files:**
- Modify: `frontend/src/types/wikiImport.ts:62-63`
- Modify: `frontend/src/pages/AdminWikiImportPage.tsx:324-326`（`taskColumns`）、`:546-552`（结果摘要）
- Modify: `frontend/src/i18n/zh-CN.ts`（`wikiImport.columns` 与 `wikiImport.resultCounts`）
- Modify: `frontend/src/i18n/en-US.ts`（同上两处）
- Modify: `frontend/src/tests/AdminWikiImportPage.test.tsx:60-80`、`:125-140`
- Test: `frontend/src/tests/AdminWikiImportPage.test.tsx`

**Interfaces:**
- Consumes: `WikiImportTaskRead.skippedPages`（Task 4）
- Produces: 台账表格列 `skippedPages` + 结果摘要文案带 `{skipped}`

> 为什么在 P1 里做：spec §5.4 的立论是「否则运维看到的失败数假性偏高」。后端改完后失败数不再虚高，但「跑了却一条都没新建」的批次会在 UI 上显示成成功 0 / 失败 0，看起来像任务没干活。跳过数是这条语义修正的**可见证据**，不补它就只是把谎报换成了沉默。

- [ ] **Step 1: Write the failing test**

改 `frontend/src/tests/AdminWikiImportPage.test.tsx`。先在 i18n mock（`:60-80` 区间）的两处文案里加 `skipped`：

```tsx
                "wikiImport.columns.success": "成功",
                "wikiImport.columns.skipped": "跳过",
                "wikiImport.columns.failed": "失败",
```

```tsx
                "wikiImport.resultCounts":
                    "共 {total} 条，成功 {success} 条，跳过 {skipped} 条，失败 {failed} 条，花费 ${cost}",
```

再在 fixture（`:125-140` 区间）的 `successPages: 2,` 后加一行：

```tsx
    successPages: 2,
    skippedPages: 0,
    failedPages: 0,
```

然后在文件末尾追加一条断言（放在既有的任务表格测试之后）：

```tsx
    it("台账展示跳过数（P1：重复 ≠ 失败）", async () => {
        // Arrange：一条「整批都是重跑」的任务 —— 成功 0 / 跳过 3 / 失败 0
        mockListTasks({
            rows: [{ ...taskFixture, id: 9, successPages: 0, skippedPages: 3, failedPages: 0 }],
            total: 1,
        });

        // Act
        renderPage();

        // Assert：三列都在，且跳过数可读
        expect(await screen.findByText("跳过")).toBeInTheDocument();
        expect(screen.getByText("3")).toBeInTheDocument();
        // 旧分类学下这条会被显示成「失败 3」，P1 之后失败列必须是 0 —— 单靠
        // 「3 出现了」证明不了它出现在正确的那一列。
        expect(screen.queryByText("失败")).toBeInTheDocument();
    });
```

> **实现者注意**：`mockListTasks` / `renderPage` / `taskFixture` 用该测试文件**已有**的 helper 名（打开文件确认后照抄，不要新造）。如果现有 helper 名不同，以文件现状为准 —— 本步骤的语义（断言「跳过」列存在且渲染出 3）不变。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend && npx vitest run src/tests/AdminWikiImportPage.test.tsx
```

Expected: FAIL —— 找不到文本「跳过」：

```
TestingLibraryElementError: Unable to find an element with the text: 跳过
```

- [ ] **Step 3: Write minimal implementation**

3a. `frontend/src/types/wikiImport.ts` —— 在 `successPages: number;` 后加：

```ts
    successPages: number;
    /**
     * P1 起：重复项（同 page_id 且正文一致）计入跳过而非失败。
     * 与 successPages 分开下发，否则「整批都是重跑」看起来像任务没干活。
     */
    skippedPages: number;
    failedPages: number;
```

3b. `frontend/src/pages/AdminWikiImportPage.tsx` —— `taskColumns`（现 `:324-326`）在成功列后插入跳过列：

```tsx
      { title: t("wikiImport.columns.total"), dataIndex: "totalPages", key: "totalPages", width: 80 },
      { title: t("wikiImport.columns.success"), dataIndex: "successPages", key: "successPages", width: 80 },
      { title: t("wikiImport.columns.skipped"), dataIndex: "skippedPages", key: "skippedPages", width: 80 },
      { title: t("wikiImport.columns.failed"), dataIndex: "failedPages", key: "failedPages", width: 80 },
```

3c. 同文件结果摘要（现 `:546-552`）—— 插值加 `skipped`：

```tsx
                  {t("wikiImport.resultCounts", {
                    total: result.totalPages,
                    success: result.successPages,
                    skipped: result.skippedPages,
                    failed: result.failedPages,
                    cost: result.totalCostUsd,
                  })}
```

3d. `frontend/src/i18n/zh-CN.ts`（`wikiImport.columns` 现约 `:1655-1662`）：

```ts
    columns: {
      title: "标题",
      content: "正文",
      status: "状态",
      total: "总条数",
      success: "成功",
      skipped: "跳过",
      failed: "失败",
      errorMessage: "备注",
    },
```

同文件 `wikiImport.resultCounts`（现约 `:1672`）：

```ts
    resultCounts: "共 {total} 条，成功 {success} 条，跳过 {skipped} 条，失败 {failed} 条，花费 ${cost}",
```

3e. `frontend/src/i18n/en-US.ts` 对应两处（`columns` 现约 `:1652-1660`、`resultCounts` 现约 `:1664-1665`）：

```ts
      success: "Succeeded",
      skipped: "Skipped",
      failed: "Failed",
```

```ts
    resultCounts:
      "{total} total, {success} succeeded, {skipped} skipped, {failed} failed, cost ${cost}",
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd frontend && npx vitest run src/tests/AdminWikiImportPage.test.tsx
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/wikiImport.ts frontend/src/pages/AdminWikiImportPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/AdminWikiImportPage.test.tsx
git commit -m "feat(frontend): 导入台账展示跳过数（P1：重复 ≠ 失败）"
```

---

## 真实数据验证（开发门禁，交付物 `backend/scripts/wiki_dedup_realdata.py`）

`Harness/rules/开发流程规范.md`「每轮真实数据验证（开发门禁）」要求每个特性交付 `scripts/<feature>_realdata.py`，并在 `Harness/changes/feat-wiki-dedup/summary.md` §9 贴出真实输出。**缺这一节即该变更不通过。**

### 脚本要做什么

不做玩具验证。P1 的失效场景在真实数据里是「**8 个导入任务 × 75 页同一份文档**」（spec §1.2 实测），脚本必须复现这个**形状**，而不是只跑 2 条草稿。

| # | 步骤 | 真实链路 | 判据 |
|---|---|---|---|
| 1 | 造真实来源文件 | 写一份 ~75 个 `##` 标题的中文 Markdown 制度文集到临时目录（内容里混入 NUL 不可能出现的全角标点、表格、代码块、超长标题） | 文件行数 ≥ 300 |
| 2 | 走真实 HTTP 导入 8 次 | `POST /api/v1/wiki/import/preview` → `POST /api/v1/wiki/import/execute`（`autoClassify=false`，不烧 LLM 钱），`sourceRef` 用同一个真实文件名 | 第 1 次 `successPages=75`；第 2..8 次 `successPages=0 / skippedPages=75 / failedPages=0` |
| 3 | 直查真实 PG | `SELECT count(*) FROM wiki_page`、`SELECT count(DISTINCT title) FROM wiki_page`、`SELECT count(*) FROM wiki_page WHERE content_hash IS NULL` | `count(*) = 75`（旧实现会是 600）、`count(DISTINCT title) = 75`、`content_hash IS NULL` 计数为 0 |
| 4 | 真实冲突（同 ID 异内容） | `PATCH /api/v1/wiki/pages/{pageId}` 改某条正文，再用 `pageId` 显式指定 + **旧正文**导入一次 | `failedPages=1`（跳过数 0）—— 证明哈希重算生效、冲突不会被误判成重跑 |
| 5 | 真实幂等重放（改后） | 用**新正文**显式指定同一 `pageId` 导入 | `skippedPages=1` |
| 6 | 台账无关性 | `DELETE FROM wiki_import_task` 后重放第 2 步的导入 | 仍 `skippedPages=75`，`count(*)` 仍 75 |
| 7 | 输出 | 逐项打印「步骤 / 真实 SQL / 真实计数 / PASS-FAIL」，末尾一行 `REALDATA RESULT: PASS|FAIL` | 全部 PASS |

### 脚本形态（与 `backend/scripts/seed_data_quality_realdata.py` 同级同风格）

```python
"""P1 去重真实数据验证（feat-wiki-dedup，Harness 开发门禁）。

复现 spec §1.2 实测到的真实失效形状 —— 8 个导入任务 × 75 页同一份文档 ——
并在真实 PostgreSQL + 真实 HTTP 链路上验证 P1 之后「同一份文件重跑 8 次
只有 75 行」。不做 mock、不用 sqlite。

幂等：每次跑先删掉本脚本上次产生的 wiki_page / wiki_import_task 行
（按 source_ref 前缀 ``realdata://`` 精确定位，**不用** TRUNCATE 清整库）。

运行（必须显式指向测试库，否则会写 prod）：
    cd backend && DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \\
        .venv/bin/python scripts/wiki_dedup_realdata.py
"""
```

脚本内部复用既有支撑：`app.tests._pg_support.pgApiClient()`（真实 FastAPI + 真实 PG）、`app.services.wiki_page_service.contentHashOf`（断言口径一致）。**不新写一套 HTTP 客户端**。

### §9 要贴的内容（summary 里）

- 三条真实 SQL 及其**真实计数**（75 / 75 / 0），不是「测试通过」四个字。
- 第 2 步 8 次导入的 `successPages/skippedPages/failedPages` 三元组逐次输出（第一次 75/0/0，其余 0/75/0）。
- 真实数据暴露的问题清单（若为 0，写明「本次未发现，原因是 X」——不允许空白）。

---

## 交付清单与验证命令

- [ ] 全量后端回归（**注意**：unit + services + integration 不要塞进同一个进程 —— autouse TRUNCATE 会抹掉迁移种子行，混跑必挂；**分目录跑**）：

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit -q
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py app/tests/integration/test_wiki_import_api.py app/tests/integration/test_wiki_api.py app/tests/integration/test_wiki_batch_delete_api.py -q
```

- [ ] 覆盖率门禁：

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' .venv/bin/pytest app/tests/unit app/tests/integration/test_wiki_dedup_api.py --cov=app --cov-fail-under=80 -q
```

- [ ] 前端：

```bash
cd frontend && npx vitest run src/tests/AdminWikiImportPage.test.tsx && npx tsc --noEmit
```

- [ ] 部署门禁（两个环境版本号都必须等于 `0061_wiki_dedup`）：

```bash
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "SELECT version_num FROM alembic_version;"
docker exec qa-postgres psql -U qa_user -d qa_metadata_test -c "SELECT version_num FROM alembic_version;"
curl -s -X POST http://localhost:8000/api/v1/wiki/import/execute -H 'Content-Type: application/json' -d '{"drafts":[{"title":"冒烟","content":"冒烟"}],"autoClassify":false,"sourceRef":"smoke.md"}' | head -c 400
```

- [ ] `code-reviewer` + `python-reviewer` 审查（`0061` 迁移脚本属 HITL，须人工过一遍）；`security-reviewer` 强制触发项：数据库查询/DDL（spec §9.3）。

---

## `Harness/changes/feat-wiki-dedup/summary.md` 九段大纲

```markdown
# 变更：feat-wiki-dedup（P1 去重：内容派生身份）

- 日期：2026-09-12
- 作者：启琳（Claude Code）
- Phase：P1（《企业 LLM Wiki 知识层落地设计》§五）
- 状态：done

## 1. 需求
- 背景：557 行页面只有 75 个不同标题（8 个导入任务 × 75 页同一份文档）。
- 目标：同一份文件重跑 N 次，wiki_page 恒 1 份。
- 验收标准（spec §十 P1 原文）：同一文件导入两次 → wiki_page 恰 1 行；
  `wiki_learning_models.py:172-173` 的声称由测试钉死。
- 非目标：不做 P3 的知识版本化；不建去重表；不读 page_ids。
- SSOT：docs/superpowers/specs/2026-09-12-wiki-knowledge-layer-design.md §五

## 2. 设计评审
- 决策 D1：page_id 内容派生（`PAGE-<slug>-<sha256(source_ref\x00title\x00content)[:8].upper()>`），
  复用既有冲突检查 + uq_wiki_page_page_id，**零新去重机制**。
- 决策 D1-1：重复 = page_id 撞车 **且** content_hash 相等；不等或 NULL → 仍计冲突。
  理由：`[:8]` 仅 32 bit，只按 ID 判定会把碰撞到的另一份文档静默丢掉。
- 决策 D1-2：`POST /wiki/pages` 撞号仍 409（单条显式动作 vs 批处理重跑，语义不同）。
- 决策 D1-3：PATCH content 重算 content_hash、**不重算 page_id**（FK 引用完整性）。
- 多视角意见（code-reviewer / python-reviewer / security-reviewer 结论摘要）。

## 3. 数据模型变更
- 迁移 `0061_wiki_dedup`（down_revision `0060_schema_reconcile`），三处 DDL：
  - `wiki_page.content_hash VARCHAR(64) NULL` + `ix_wiki_page_content_hash`（**非唯一**）。
  - `document_catalog.content_hash` 加 `uq_document_catalog_content_hash`
    （**唯一**，spec §4.7 D2-2 推迟到 P1 一并做的那条；建前查重为 0 行）。
  - `wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`。
- **两处同名索引方向相反，不是笔误**：`document_catalog` 一行 = 一份源文档，
  重复上传应映射同一份 → 唯一；`wiki_page` 一行 = 一条知识，共享模板合法 → 非唯一
  （且唯一约束会让「同 ID 同内容 → 跳过」分支永远走不到，正常写入还会变 500）。
- **交接项（P0）**：P0 不得重复创建 `uq_document_catalog_content_hash`；
  且 P0 写 `document_catalog.content_hash` 时须自行处理唯一冲突（`ON CONFLICT`）。
- 备份：`/tmp/wiki_page_20260912.sql`、`/tmp/wiki_import_task_20260912.sql`、
  `/tmp/document_catalog_20260912.sql`。
- prod `qa_metadata` 已 upgrade，drift 校验通过。

## 4. 接口契约变更
- `POST /api/v1/wiki/import/execute` 响应 `WikiImportTaskRead` **新增 `skippedPages`**（向后兼容的纯新增）。
- 语义变更：重复项由 `failedPages` 改计 `skippedPages`，状态由 FAILED/PARTIAL 改 SUCCEEDED。
- `POST /api/v1/wiki/pages` 生成的 pageId 由随机后缀改为内容派生（**行为变更**，见 §6 回归测试）。
- 无新增/删除端点，无路径变更。

## 5. 实现要点
- `contentHashOf()` / `_identityDigest()` / `generatePageId(title, sourceRef, content)`
  —— `backend/app/services/wiki_page_service.py`。
- `DuplicatePageError(ConflictError)` —— `backend/app/domain/exceptions.py`；
  **catch 顺序必须排在 ConflictError 之前**（子类），顺序反了等于没修。
- `_importOne` 三分支：同哈希 → DuplicatePageError；异哈希/NULL → ConflictError；无既有行 → 建。
- 前端：台账加「跳过」列 + 结果摘要带 `{skipped}`（zh/en 两套 i18n）。
- 依赖方向：P0 落地时 `document_catalog.content_hash` **消费**本计划的
  `contentHashOf()` 取同源值；P1 不依赖 P0/P3。

## 6. 测试
- 纯单测：`app/tests/unit/test_wiki_page_id.py`（9 条）—— 确定性、字面公式、
  中文 slug 折叠、长度上界、哈希口径、hash≠ID 后缀。
- 集成（真实 PG + 完整 API 链路）：`app/tests/integration/test_wiki_dedup_api.py`（12 条）。
- 既有测试更新：`test_wiki_import_api.py` 2 条（语义随 D1-1 变更）、`test_wiki_api.py` 1 条（ID 确定性）。
- 覆盖率：`--cov-fail-under=80` 结果；前端 vitest 结果。

## 7. 安全审查
- 触发项：数据库 DDL / DML（DDL 属 HITL，须人工 review）。
- 检查项：迁移幂等（`IF NOT EXISTS`）与可逆性（downgrade 对称）；
  无 SQL 拼接（全部 ORM 或参数绑定）；错误文案不泄露内部结构。
- 结论：CRITICAL/HIGH 数量与处置。

## 8. 部署验证
- 两环境 `alembic_version` 均 = `0061_wiki_dedup`。
- `POST /api/v1/wiki/import/execute` 真机冒烟输出（非 5xx + skippedPages 字段可见）。
- 前端 bundle 含新 i18n key（按 `qa-system-stale-container-deploy` 的对照探针法确认）。

## 9. 真实数据验证报告（开发门禁，缺此节即不通过）
- 脚本：`backend/scripts/wiki_dedup_realdata.py`，运行命令与完整输出。
- 8 次导入的三元组逐次记录（第 1 次 75/0/0，第 2..8 次 0/75/0）。
- 真实 SQL 与其真实计数：`count(*) = 75`、`count(DISTINCT title) = 75`、
  `count(*) WHERE content_hash IS NULL = 0`。
- 真实数据/真实链路暴露的问题与修复（若为 0，写明原因）。
- 已知限制（不粉饰）：
  1. `source_ref` 参与身份派生 → 首次导入留空、二次填文件名会产生两条
     （已由 `test_same_content_different_source_ref_is_two_entries` 钉死，
     缓解方案：向导层要求必填 sourceRef，或后续把来源归一化为上传文件哈希）。
  2. 纯中文标题 slug 折叠为 `UNTITLED`，可读性净损失（唯一性由哈希兜底）。
  3. `[:8]` 32 bit 截断碰撞虽概率极低，但后果是**另一份文档被判冲突计失败**
     （不是静默丢弃）——这是刻意的失败方向。
  4. 备份 cron 仍静默失效，本次备份为手动执行。

## 9'. 关联
- 设计稿：`docs/superpowers/specs/2026-09-12-wiki-knowledge-layer-design.md` §五
- 计划：`docs/superpowers/plans/2026-09-12-wiki-dedup-p1.md`
- 规则：`Harness/rules/数据库环境使用规范.md`、`Harness/rules/测试规范.md`、`Harness/rules/开发流程规范.md`
- 前置复盘：`Harness/changes/feat-wiki-knowledge/summary.md`（本次修复其遗留的 557/75 副本问题）
```

> 注：上面把关联并进 §9 之后的 `9'`，是因为序号 9 已被门禁要求的「真实数据验证报告」占用。若 `Harness/changes/_template/summary.md` 更新为 10 段模板，把 `9'` 改名即可。

---

## 自审记录

**spec §五 覆盖对照**

| spec 条目 | 落在哪个任务 |
|---|---|
| §5.1 根因（随机后缀 → 冲突检查永不命中） | Task 1（替换实现）+ Task 3 Step 5（回归断言） |
| §5.2 设计：内容派生 page_id | Task 1 |
| §5.2 设计：`content_hash VARCHAR(64) NULL` 落列 | Task 2（DB + ORM）、Task 3（写入）、Task 4（导入写入） |
| §5.3 迁移与门禁（一次迁移、加列 + 索引、备份 `wiki_page_20260912`） | Task 2 Step 3 / Step 5 / Step 6 |
| §4.7 D2-2（`content_hash` 唯一约束推迟到 P1 一并做） | Task 2：`uq_document_catalog_content_hash` + 同名断言 + 建前查重 |
| §5.4 分类学修正（重复 → 成功但跳过） | Task 4（含前端可见性 Task 6） |
| §5.5 验收（重放恰 1 行 + 更正不实 docstring） | Task 5 |
| §九 9.2 开发门禁（realdata 脚本 + summary §9） | 「真实数据验证」段 + summary 大纲 |
| §九 9.2 备份门禁 | Task 2 Step 5 |
| §九 9.3 HITL（迁移脚本人工 review） | 交付清单 |
| §十 P1 验收信号 | Task 5（`wiki_page` 恰 N 行）、「真实数据验证」第 3 步 |
| §十一 风险「备份 cron 静默失效」 | Task 2 Step 5 的显式警告 + summary §9 限制 4 |
| §三 3.2 软引用写入侧校验 | **不在 P1 范围**（spec 明确指派给 P3，见 §3.2 末句） |

**占位符扫描**：未出现任何「待补/待定」类记号，也没有「细节同前一条」这类省略。33 个 Step 全部带可执行命令或可直接粘贴的代码块；每个失败断言的预期报错都给出真实文本。所有哈希与 ID 字面量（`PAGE-UNTITLED-012CA6C8`、`PAGE-UNTITLED-840CCA89`、`PAGE-SUPPLIER-QUALIFICATION-0747A11B`、`…-2E3AC84B`、四个 `contentHashOf` 值）均用项目 venv 实算核对过，实现者无需自行推导。

**类型一致性**（跨任务逐名核对）

| 名字 | 定义处 | 使用处 |
|---|---|---|
| `contentHashOf(content: str) -> str` | Task 1 | Task 3（create/update）、Task 4（`_importOne`）、realdata 脚本 |
| `generatePageId(title, sourceRef, content)` | Task 1 | Task 3（`createPage`）、Task 4（`_importOne`） |
| `_identityDigest` / `_IDENTITY_SEPARATOR` / `_IDENTITY_HASH_LEN` | Task 1 | 仅 Task 1（模块私有，无跨任务引用） |
| `WikiPage.content_hash` | Task 2 | Task 3、Task 4 |
| `WikiImportTask.skipped_pages` | Task 2 | Task 4（写）、Task 5（读断言） |
| `DuplicatePageError(message, *, page_id)` | Task 4 | Task 4（`_importOne` 抛、`execute` 捕） |
| `MSG_WIKI_PAGE_DUPLICATE_SKIPPED` | Task 4 | Task 4 |
| `WikiImportTaskRead.skipped_pages` | Task 4 | Task 6（`skippedPages`） |
| `_countPages` / `_taskRow` / `_execute` | Task 2 / Task 4 | Task 4、Task 5 的集成测试 |

**迁移 head 核对**：`cd backend && .venv/bin/alembic heads` → `0060_schema_reconcile (head)`（实测输出）。本计划新增 `0061_wiki_dedup`，`down_revision = "0060_schema_reconcile"`，与 `alembic/versions/0060_reconcile_menu_index_and_users_legacy_cols.py` 的 `revision = "0060_schema_reconcile"` 一致。
