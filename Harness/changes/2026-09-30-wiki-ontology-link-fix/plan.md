# Wiki ↔ Ontology 链接修复（A/B/C）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 property/metric 类型的 wiki 链接在生产召回链路中真正生效（A），统一 Wiki 链接管理页与本体管理页的标签与对象名展示口径（B），并把 `metric` 类型放开到全链路（C）。

**Architecture:** A 反转 `_collectWikiBlock` 的收集顺序 —— 先查「库里配了哪些类型的链接」，再决定对哪些类型做语义召回，从而做到「没配就零成本、配了才付费」。B 把前端硬编码标签收进 i18n，并让后端在链接列表里带上本体对象名。C 沿 A 已铺好的按需召回通道，只放开 6 处类型枚举 + 一条 CHECK 约束迁移。

**Tech Stack:** FastAPI + SQLAlchemy(async) + Alembic + PostgreSQL 16 / pytest(asyncio_mode=auto) / React 18 + TypeScript + antd v5 + i18next / vitest + Testing Library

## Global Constraints

- **永远不要修改 `raw/`**
- **仅在用户要求时 commit 或 push** —— 本计划**不含任何 commit 步骤**。每个任务结束时改动留在工作区（当前分支 `feat/chat-session-restore`，非默认分支，本次不新建分支）
- **TDD**：RED → GREEN → REFACTOR；覆盖率 ≥80%
- **小文件**：200–400 行为宜，≤800；**函数 <50 行**；**嵌套 ≤4 层**
- **不可变模式**：不改原对象，返回新副本
- **显式错误处理**：每层显式处理，UI 友好提示，服务端记详细上下文，**绝不静默吞错**
- **后端测试**：真实 PostgreSQL + 完整 API 链路。**禁 sqlite 内存库**。库 `qa_metadata_test`（容器 `qa-pg-a1`，端口 **5434**）**串行**跑；**绝不能用 5433（prod）**；**不要在同一进程混跑 unit + services + integration 三个套件**
- **前端测试**：`cd frontend` 后跑 vitest（**别从仓库根跑**，会误选 `.worktrees/` 副本）
- **禁止硬编码密钥** —— 测试口令只经命令替换注入，不落盘、不回显
- ORM / Pydantic 字段名用 `snake_case`（与 DB 列及 JSON 契约一致，刻意偏离 PEP 8）
- 回复用中文

### 测试运行前缀（每个后端任务复用，只在此处定义一次）

```bash
cd backend
export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:$(docker exec qa-pg-a1 printenv POSTGRES_PASSWORD)@localhost:5434/qa_metadata_test"
```

口令经命令替换注入，**不写入任何文件、不回显**。之后所有 `pytest` 命令都假定该变量已导出。

---

## File Structure

| 文件 | 职责 | 涉及任务 |
|---|---|---|
| `backend/app/services/wiki_link_service.py` | 新增 `listConfiguredOntologyTypes`；C 放开 `_VALID_ONTOLOGY_TYPES` 与 `listLinkableTargets` | 1, 7 |
| `backend/app/services/chat_service.py` | `_collectWikiBlock` 拆出 `_classRecallScores` / `_recallExtraOntologyScores` / `_getWikiExtraRecallTopK` | 2 |
| `backend/app/api/v1/admin_wiki_links.py` | 列表响应补 `ontology_name` / `ontology_alias`；C 放开 type pattern | 4, 7 |
| `backend/app/domain/schemas.py` | `WikiLinkOut` 加两字段 | 4 |
| `backend/alembic/versions/0106_wiki_link_metric_type.py` | CHECK 约束放开 metric | 6 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | `wikiLinks.tabs.*` | 3, 8 |
| `frontend/src/pages/WikiLinksPage.tsx` | 标签走 `t()`；列表显示对象名 | 3, 5 |
| `frontend/src/utils/ontologyLabel.ts` | 新增：本体对象名展示单源 helper | 5 |
| `frontend/src/components/ontology/classOptions.ts` | 改为复用该 helper | 5 |
| `frontend/src/types/wikiLink.ts` | `WikiLinkType` 加 `"metric"`；`WikiLink` 加两字段 | 5, 8 |
| `frontend/src/api/adminWikiLinks.ts` | `listLinkableTargets` 签名放开 | 8 |

**任务依赖**：T1 → T2（T2 调 T1 的新方法）；T3 → T5（T5 复用 T3 的 i18n 键）；T4 → T5（T5 消费 T4 的响应字段）；T2 → T7 → T8（C 复用 A 的召回通道）；T6 独立但必须在 T7 之前（先有约束后有用例）。

---

## Task 1: `WikiLinkService.listConfiguredOntologyTypes`

**Files:**
- Modify: `backend/app/services/wiki_link_service.py`（在 `listAllLinks` 之后、`listLinkableTargets` 之前插入）
- Test: `backend/app/tests/unit/test_wiki_link_service.py`（追加，复用已有的 `_seed_page` / `_actor` 辅助）

**Interfaces:**
- Consumes: `WikiOntologyLink` ORM（`app/domain/models.py`）
- Produces: `WikiLinkService.listConfiguredOntologyTypes(self, session: AsyncSession) -> set[str]` —— 返回「存在未撤销链接」的 `ontology_type` 集合（如 `{"class"}` / `{"class","property"}` / `set()`）

- [ ] **Step 1: 写失败测试**

追加到 `backend/app/tests/unit/test_wiki_link_service.py` 末尾：

```python
async def test_list_configured_ontology_types_excludes_revoked(dbSession):
    """只统计**未撤销**的链接类型。

    构造要点（这一条写错整个测试就是假的）：被撤销的那条必须用一个
    **只出现在被撤销行里**的类型，否则删掉实现里的 ``revoked_time IS NULL``
    过滤时结果集不变、测试照样绿 —— 那就成了对核心契约零保护的假测试。
    """
    await _seed_page(dbSession)
    actor = await _actor(42)
    await _svc.createLink(
        dbSession, page_id="p001", chunk_id=None, ontology_type="property",
        ontology_id=13, weight=Decimal("1.00"), note=None, actor=actor,
    )
    revoked = await _svc.createLink(
        dbSession, page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None, actor=actor,
    )
    await _svc.revokeLink(dbSession, revoked.id, actor=actor)

    # class 只以「已撤销」的身份出现 ⇒ 漏掉 revoked 过滤会得到 {"class","property"}
    assert await _svc.listConfiguredOntologyTypes(dbSession) == {"property"}


async def test_list_configured_ontology_types_empty_when_no_links(dbSession):
    """空表返回空集 —— 这是「没配就零成本」的前提。"""
    assert await _svc.listConfiguredOntologyTypes(dbSession) == set()
```

> **不要在测试里断言「去重」**：返回类型是 `set[str]`，Python 的集合推导本身就折叠重复，
> SQL 里的 `.distinct()` 在公开 API 层面**不可观测** —— 它是省流量的优化，不是契约。
> 为它写断言只会得到一个删掉 `distinct()` 也不红的假测试。

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/unit/test_wiki_link_service.py -k configured_ontology_types -v
```

Expected: FAIL —— `AttributeError: 'WikiLinkService' object has no attribute 'listConfiguredOntologyTypes'`

- [ ] **Step 3: 写最小实现**

在 `backend/app/services/wiki_link_service.py` 的 `listAllLinks` 之后插入：

```python
    async def listConfiguredOntologyTypes(self, session: AsyncSession) -> set[str]:
        """返回「当前存在未撤销链接」的 ontology_type 集合。

        供注入侧判断是否值得为某类型做语义召回：某类型一条链接都没有时，召回它的
        结果必然在 ``WikiInjector.collectAndScore`` 的 recallIndex 命中检查处被丢弃
        （``wiki_injector.py:103``），纯属白花一次 embedding + 一次 Milvus 检索。
        空表返回空集，调用方零额外成本。

        走 ix_wol_ontology 部分索引（``WHERE revoked_time IS NULL``）。
        """
        stmt = (
            select(WikiOntologyLink.ontology_type)
            .where(WikiOntologyLink.revoked_time.is_(None))
            .distinct()
        )
        result = await session.execute(stmt)
        return {row[0] for row in result.all()}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/unit/test_wiki_link_service.py -v
```

Expected: 全部 PASS。**用例总数以实际 pytest 输出为准**（原文件既有 13 个 + 本次新增 2 个），
不要为凑数字去改测试。

---

## Task 2: `_collectWikiBlock` 按需召回 property/metric

**Files:**
- Modify: `backend/app/services/chat_service.py:1095-1188`（`_collectWikiBlock` 及其后新增三个私有成员）
- Test: `backend/app/tests/integration/test_wiki_link_injector_e2e.py`（追加 fixture 与 4 个用例；修 `:327` 的恒真断言）

**Interfaces:**
- Consumes: `WikiLinkService.listConfiguredOntologyTypes(session) -> set[str]`（Task 1）；`self._ontology.searchByKeyword(query, topK=..., typeFilter=...) -> list[OntologySearchResult]`（`OntologySearchResult` 有 `.id` / `.score` / `.type`）；`ScoredOntology(type: str, id: int, recall_score: float)`
- Produces: 无新公开接口；行为变更为「`recallIndex` 中现在可能出现 `("property", id)` / `("metric", id)` 键」

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/integration/test_wiki_link_injector_e2e.py` 中：

(a) 文件顶部 import 块（对照现行 import，**保持原样即可**）：

```python
from app.domain.models import (
    DataSource,
    LlmConfig,
    Nl2sqlWikiTrace,
    OntologyClass,
    WikiOntologyLink,
    WikiPage,
)
```

> **不 import `OntologyProperty`**（初稿写错了，2026-09-30 实读代码后订正）：`wiki_injector.collectAndScore`
> 只校验 `_VALID_TYPES` + `recallIndex` 命中，**从不查本体对象是否存在**（`wiki_injector.py:99-108`），
> `wiki_ontology_link.ontology_id` 也没有指向 `ontology_property` 的 FK。为 property 链接单独 seed 一条
> `ontology_property` 行是**没有任何消费者的死代码** —— 对应的 `ontology_property_seed` fixture 一并删除。

(b) 加一个记录型本体替身（放在 `_StubEmbeddingService` 之后）：

```python
class _RecordingOntology:
    """包住真实 OntologyService，只拦截 searchByKeyword 并记录 typeFilter。

    类召回继续走真实实现（测试库只剩 seed 出来的类，会走 _fallbackRecall 拿到它），
    只有 property / metric 这两个新增的按需召回被替换成可控结果。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str | None] = []
        self.hits: dict[str, list[Any]] = {}
        self.raiseOn: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None):
        self.calls.append(typeFilter)
        if typeFilter in self.raiseOn:
            raise RuntimeError("recall boom")
        if typeFilter in self.hits:
            return list(self.hits[typeFilter])
        return await self._inner.searchByKeyword(query, topK=topK, typeFilter=typeFilter)
```

(c) 加 fixture（放在 `wiki_link_seed` 之后）：

```python
@pytest.fixture
async def property_link_seed(
    dbSession: AsyncSession,
    wiki_page_seed: None,  # noqa: ARG001 —— 显式声明 FK 前置：link.page_id → wiki_page
) -> None:
    """Seed a *property* wiki link —— 证明 property 类型真的会生效（修复前它死链）。"""
    row = (await dbSession.execute(
        select(WikiOntologyLink).where(
            WikiOntologyLink.page_id == "wp001",
            WikiOntologyLink.ontology_type == "property",
            WikiOntologyLink.ontology_id == 401,
        )
    )).scalar_one_or_none()
    if row is None:
        dbSession.add(WikiOntologyLink(
            page_id="wp001", chunk_id=None, ontology_type="property",
            ontology_id=401, weight=Decimal("1.0"), note="属性口径", created_by=1,
        ))
    await dbSession.commit()


@pytest.fixture
async def recording_ontology(
    client_with_fakes: AsyncClient,  # noqa: ARG001 —— 触发 _service 装配
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingOntology:
    """把 _service._ontology 换成记录型替身。

    **必须走 monkeypatch**（与同文件 `client_with_fakes` 的 `_modelRouter` / `_embedding`
    一致）：裸赋值 `chatModule._service._ontology = recorder` 不会还原，`hits` / `raiseOn`
    会泄漏到后续用例 —— 尤其 `test_extra_recall_skipped_when_no_extra_links` 会被前一个
    用例留下的 `hits["property"]` 假命中，成本守卫静默失效。
    """
    import app.api.v1.chat as chatModule
    recorder = _RecordingOntology(chatModule._service._ontology)
    monkeypatch.setattr(chatModule._service, "_ontology", recorder)
    return recorder
```

(d) 追加 4 个用例：

```python
async def test_extra_recall_skipped_when_no_extra_links(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    dbSession: AsyncSession,  # noqa: ARG001 —— 由 client fixture 触发 truncate
) -> None:
    """成本回归守卫：库里没有 property/metric 链接时，绝不发起额外语义召回。

    这一条守的是「按需付费」这个设计本身。少了它，将来有人把
    listConfiguredOntologyTypes 的判断删掉，每轮 chat 都会白花一次 embedding +
    一次 Milvus 检索，而其它用例全是绿的。
    """
    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-cost-guard"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    assert "property" not in recording_ontology.calls
    assert "metric" not in recording_ontology.calls


async def test_property_link_injected_when_property_recalled(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """property 链接在其属性被召回时必须注入（修复前：恒不注入）。"""
    recording_ontology.hits["property"] = [
        SimpleNamespace(id=401, score=0.9),
    ]

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    assert recording_ontology.calls.count("property") == 1

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property",
            Nl2sqlWikiTrace.ontology_type == "property",
        )
    )).scalars().all()
    assert len(traces) == 1
    assert traces[0].ontology_id == 401
    assert traces[0].page_id == "wp001"


async def test_property_link_skipped_when_property_not_recalled(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """配了属性链接、但该属性没被召回 → 不注入（不能退化成「配了就无脑注入」）。"""
    recording_ontology.hits["property"] = [SimpleNamespace(id=999, score=0.9)]

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property-miss"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property-miss",
            Nl2sqlWikiTrace.ontology_type == "property",
        )
    )).scalars().all()
    assert traces == []


async def test_property_recall_failure_does_not_block_class_injection(
    client_with_fakes: AsyncClient,
    recording_ontology: _RecordingOntology,
    property_link_seed: None,
    dbSession: AsyncSession,
) -> None:
    """属性召回抛异常 → 只跳过 property，class 链接照常注入，且不抛到调用方。

    失败隔离的双向断言：坏输入（抛异常的 property）被降级处理，
    正确输入（class 链接）不受牵连。
    """
    recording_ontology.raiseOn = {"property"}

    resp = await client_with_fakes.post(
        "/api/v1/chat",
        json={"question": "上个月供应商准时交付率", "datasourceId": 1,
              "sessionId": "s-wiki-property-fail"},
        headers={"X-User-Id": "1", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

    class_traces = (await dbSession.execute(
        select(Nl2sqlWikiTrace).where(
            Nl2sqlWikiTrace.session_id == "s-wiki-property-fail",
            Nl2sqlWikiTrace.ontology_type == "class",
        )
    )).scalars().all()
    assert len(class_traces) >= 1
    assert class_traces[0].ontology_id == 201
```

(e) 文件顶部 import 补 `SimpleNamespace`：`from types import SimpleNamespace`

(f) **修恒真断言**（`:327`）：把

```python
    assert t.ontology_type in ("class", "property")
```

改为

```python
    # 绑的是 class 链接（wiki_link_seed），trace 必须记下同一个类型 ——
    # 原断言 `in ("class","property")` 由 DB CHECK 约束保证恒真，等于没测。
    assert t.ontology_type == "class"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/integration/test_wiki_link_injector_e2e.py -v
```

Expected:
- `test_extra_recall_skipped_when_no_extra_links` → PASS（当前实现本来就不召回）—— 这是守卫，预期一开始就绿
- `test_property_link_injected_when_property_recalled` → **FAIL**：`recording_ontology.calls.count("property") == 1` 实为 0（当前实现从不召回 property）
- 其余两个 → PASS（当前实现同样不注入）
- `test_wiki_trace_records_correct_fields` → PASS（改后的断言对当前实现也成立）

> 只有注入用例是 RED —— 这正是缺陷的形状：**该生效的不生效**。用 `-k property_link_injected` 先确认这一条红。

- [ ] **Step 3: 写最小实现**

把 `backend/app/services/chat_service.py` 的 `_collectWikiBlock` 开头（`:1095`–`:1136`）替换为：

```python
    async def _collectWikiBlock(
        self,
        session: AsyncSession,
        question: str,
        classes: list[Any],
    ) -> tuple[str, list[dict]]:
        """调 WikiLinkService + WikiChunkLoader + WikiInjector → (prompt_block, trace_data)。

        失败返回 ("", [])，由调用方统一做 log warning + 不阻断流水线。

        收集顺序是**先看配了哪些类型的链接，再决定召回哪些类型**：某类型一条链接都
        没有时，召回它的结果必然在 WikiInjector 的 recallIndex 命中检查处被丢弃，
        白花一次 embedding + 一次 Milvus 检索（见 listConfiguredOntologyTypes）。
        """
        from app.services.wiki_injector import WikiInjector
        from app.services.wiki_link_service import WikiLinkService
        from app.services.wiki_chunk_loader import WikiChunkLoader

        service = WikiLinkService()

        # Step 1：class 分数来自 _selectRelevantClasses 附加的 _recall_score。
        scored_ontology = self._classRecallScores(classes)

        # Step 2：property / metric 按需召回 —— 只有真的配了该类型的链接才召回。
        try:
            configuredTypes = await service.listConfiguredOntologyTypes(session)
        except Exception as e:
            # 查不出来只降级到「class 链接仍工作」，不阻断；不能静默 —— 记 warning。
            logger.warning("listConfiguredOntologyTypes failed: %s", e)
            configuredTypes = set()
        extraTypes = {
            t for t in _WIKI_EXTRA_RECALL_TYPES if t in configuredTypes
        }
        scored_ontology.extend(
            await self._recallExtraOntologyScores(session, question, extraTypes)
        )

        if not scored_ontology:
            return "", []

        # Step 3：查询 wiki-link（根据 ontology type/id 对）。
        pairs = [(o.type, o.id) for o in scored_ontology]
        try:
            link_rows = await service.getLinksByOntology(session, pairs)
        except Exception as e:
            logger.warning("getLinksByOntology failed: %s", e)
            return "", []
        if not link_rows:
            return "", []
```

> 原 `Step 3/4/5/6` 的编号顺延为 `Step 4/5/6/7`。
>
> ⚠️ **订正（2026-09-30，实现期发现）**：初稿此处写「函数体其余部分**逐字不动**」是**错的**，
> 会造成第三次静默丢弃。原因：`WikiInjector.collectAndScore` 按 `(page_id, chunk_id or "")` 分组
> （`wiki_injector.py:107`）并把同组所有链接放进 `applied_to`（`:129`）—— 同一 page/chunk 上同时
> 绑了 class 与 property 时，两者**合并成同一个 ScoredChunk**。而 Step 6 的 trace 循环原本是
> `ontology_type, ontology_id = c.applied_to[0]`，**只写第一条** ⇒ property 的 trace 被丢掉。
> 这不是可选项：不改它，本任务自己新增的 `test_property_link_injected_when_property_recalled`
> 就永远红。
>
> **改法**：trace 循环改为遍历 `c.applied_to` 全部条目（每个 applied ontology 一条 trace），
> 并删掉随之冗余的 `if not c.applied_to: continue`（空列表自然零次迭代）。
> 这与 `Nl2sqlWikiTrace` 的 ORM docstring（`app/models.py:2227`）自述的
> 「**一行 = 某条 ontology 业务规则被注入 prompt 的事实**」一致 —— 按 ontology 计行本就是
> schema 声明的契约，旧代码与自己的契约矛盾。
>
> **无下游风险**：该表**只有写入方**（`chat_service.py:1291`），全仓无任何读取方；唯一索引
> `ix_nlwt_session` 非唯一约束，一个 chunk 写多行不会冲突。

在 `_collectWikiBlock` 之后新增三个成员：

```python
    @staticmethod
    def _classRecallScores(classes: list[Any]) -> list[Any]:
        """把 _selectRelevantClasses 产出的 class 列表转成 ScoredOntology(type="class")。

        分数取自 chat_recall 在召回时附加的 _recall_score（fallback 路径为 0.0）。
        """
        from app.services.wiki_injector import ScoredOntology

        out: list[ScoredOntology] = []
        for cls in classes:
            oid = getattr(cls, "id", None)
            if oid is None:
                continue
            out.append(ScoredOntology(
                type="class",
                id=oid,
                recall_score=float(getattr(cls, "_recall_score", 0.0) or 0.0),
            ))
        return out

    async def _recallExtraOntologyScores(
        self,
        session: AsyncSession,
        question: str,
        types: set[str],
    ) -> list[Any]:
        """对 property / metric 做按需语义召回，返回 ScoredOntology 列表。

        每个类型一次 embedding + 一次 Milvus 检索，故只在**确实配了该类链接**时调用。
        单类型失败只影响该类型（warning + 跳过），不影响 class 与其他类型 ——
        注入是增强而非硬依赖。
        """
        if not types:
            return []
        from app.services.wiki_injector import ScoredOntology

        topK = await self._getWikiExtraRecallTopK(session)
        scored: list[ScoredOntology] = []
        for ontologyType in sorted(types):
            try:
                hits = await self._ontology.searchByKeyword(
                    question, topK=topK, typeFilter=ontologyType,
                )
            except Exception:
                logger.warning(
                    "wiki 注入：%s 类型召回失败，跳过该类型", ontologyType,
                    exc_info=True,
                )
                continue
            scored.extend(
                ScoredOntology(
                    type=ontologyType,
                    id=hit.id,
                    recall_score=float(getattr(hit, "score", 0.0) or 0.0),
                )
                for hit in hits
            )
        return scored

    async def _getWikiExtraRecallTopK(self, session: AsyncSession) -> int:
        """读 WIKI_LINK_EXTRA_RECALL_TOPK；缺失或非法 → 默认 10。"""
        try:
            from sqlalchemy import select
            from app.models.system_config import SystemConfig

            stmt = select(SystemConfig).where(
                SystemConfig.key == "WIKI_LINK_EXTRA_RECALL_TOPK"
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return _WIKI_EXTRA_RECALL_TOPK_DEFAULT
            value = int(row.value)
            return value if value > 0 else _WIKI_EXTRA_RECALL_TOPK_DEFAULT
        except Exception:
            # 配置读取失败 → 用默认值，不阻断
            return _WIKI_EXTRA_RECALL_TOPK_DEFAULT
```

模块级常量放到 `chat_service.py` 顶部常量区：

```python
# wiki 注入：property / metric 的按需语义召回窗口。
# class 走 CLASS_FILTER_TOPK（类集合小）；属性向量 3164 条、指标待建，窗口先小后调。
_WIKI_EXTRA_RECALL_TOPK_DEFAULT = 10
_WIKI_EXTRA_RECALL_TYPES = ("property", "metric")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/integration/test_wiki_link_injector_e2e.py -v
```

Expected: 全部 PASS。**用例总数以实际 pytest 输出为准**（该文件既有 **6** 个 + 本次新增 4 个），
不要为凑数字改测试 —— 初稿写的「原 8」是计划作者的算术错误。

- [ ] **Step 5: 回归**

```bash
pytest app/tests/integration/test_wiki_link_injector_e2e.py app/tests/unit/test_wiki_injector.py app/tests/unit/test_wiki_link_service.py -v
```

Expected: 全绿。若 `test_wiki_injector.py` 出现红，说明改动越界到了注入器 —— 检查是否误改了 `wiki_injector.py`（本任务不应改它）。

---

## Task 3: Wiki 链接管理页 Tab 标签走 i18n

**Files:**
- Modify: `frontend/src/i18n/zh-CN.ts:2480`（`wikiLinks` 块内加 `tabs`）、`frontend/src/i18n/en-US.ts:2466`（同）
- Modify: `frontend/src/pages/WikiLinksPage.tsx:262-269`（Tabs `label`）、`:298-300`（类型 Tag）
- Test: `frontend/src/pages/__tests__/WikiLinksPage.test.tsx`

**Interfaces:**
- Consumes: `useTranslation()` 已返回的 `t`
- Produces: i18n 键 `wikiLinks.tabs.class` / `wikiLinks.tabs.property`（Task 5、Task 8 复用；`tabs.metric` 由 Task 8 加）

- [ ] **Step 1: 写失败测试**

**① 先改既有用例**（`WikiLinksPage.test.tsx:230-234`）—— 它现在断言的正是我们要删掉的硬编码英文，
改动后**必然变红**（初稿漏了这条，2026-09-30 实读代码后补上）：

```tsx
  it("renders class and property tabs", async () => {
    // 标签走 i18n（与本体管理同词）：zh 下显示「类」「属性」
    await i18n.changeLanguage("zh-CN");
    renderPage();
    expect(await screen.findByRole("tab", { name: "类" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "属性" })).toBeInTheDocument();
  });
```

（原内容 `expect(screen.getByText("Class"))` / `getByText("Property")` 删除 ——
它把「标签是英文」这个待修行为钉成了契约。）

**② 追加切语言用例**到文件末尾（文件顶部 :23 已 `import { i18n } from "../../i18n"`）：

```tsx
describe("标签本地化", () => {
  // 语言是模块级全局状态：用 afterEach 复位，避免英文用例中途失败时把 en 泄漏给后续用例
  afterEach(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  it("切到英文后 Tab 标签变成 Class / Property", async () => {
    await i18n.changeLanguage("en-US");
    renderPage();
    expect(await screen.findByRole("tab", { name: "Class" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Property" })).toBeInTheDocument();
  });
});
```

> `renderPage()` 是该文件已有的渲染辅助（`:144`，内部做 `I18nextProvider` + `ConfigProvider` 包装）。
> 顶部 import 需补 `afterEach`（现为 `import { describe, it, expect, vi, beforeEach } from "vitest";`）。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run src/pages/__tests__/WikiLinksPage.test.tsx
```

Expected: **至少改后的 `renders class and property tabs` 必须红** —— 它找 `role="tab"` 名为「类」的元素，
而当前渲染的是硬编码 `Class`（`WikiLinksPage.tsx:265-268`）。英文用例此时**应当通过**（切换语言对硬编码
标签无效，`Class`/`Property` 恒在）。**以实际 vitest 输出为准，如实记录；不要为迎合预期改测试。**

- [ ] **Step 3: 写最小实现**

`frontend/src/i18n/zh-CN.ts` 的 `wikiLinks` 块内（`title` 之后）加：

```ts
    tabs: {
      // 与本体管理 forms.ontology.tabs.* 同词（类/属性/指标），保证两页标签一致
      class: "类",
      property: "属性",
    },
```

`frontend/src/i18n/en-US.ts` 同位置加：

```ts
    tabs: {
      class: "Class",
      property: "Property",
    },
```

`frontend/src/pages/WikiLinksPage.tsx` 的 Tabs 改为：

```tsx
          <Tabs
            activeKey={linkType}
            onChange={(k) => setLinkType(k as WikiLinkType)}
            items={[
              { key: "class", label: t("wikiLinks.tabs.class") },
              { key: "property", label: t("wikiLinks.tabs.property") },
            ]}
          />
```

同文件 `:298-300` 的类型 Tag 改为：

```tsx
                <Tag color={link.ontology_type === "class" ? "blue" : "green"}>
                  {t(`wikiLinks.tabs.${link.ontology_type}`)}
                </Tag>
```

> `t()` 的键类型是 `NestedKeyOf<typeof zhCN>`，模板串拼接不会被类型系统接受。改为映射表：
> ```tsx
> const TYPE_LABEL_KEY: Record<WikiLinkType, string> = {
>   class: "wikiLinks.tabs.class",
>   property: "wikiLinks.tabs.property",
> };
> ```
> 然后 `{t(TYPE_LABEL_KEY[link.ontology_type])}`。
>
> ⚠️ **Task 8 必须同步给这张表加 `metric` 一行** —— `Record<WikiLinkType, string>` 要求键齐全，
> 而 Task 8 会把 `WikiLinkType` 扩成三值。**此处不要提前写 `metric`**：Task 3 时
> `WikiLinkType` 还是两值，多写一个键会直接 TS 报错。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npx vitest run src/pages/__tests__/WikiLinksPage.test.tsx
```

Expected: 全部 PASS

---

## Task 4: 链接列表带本体对象名

**Files:**
- Modify: `backend/app/domain/schemas.py:3611-3620`（`WikiLinkOut`）
- Modify: `backend/app/api/v1/admin_wiki_links.py`（`_row_to_out` → 异步批量解析）
- Test: `backend/app/tests/integration/test_wiki_link_admin_api.py`

**Interfaces:**
- Consumes: `OntologyClass` / `OntologyProperty` / `OntologyMetric` ORM
- Produces: `WikiLinkOut` 新增 `ontology_name: str | None`、`ontology_alias: str | None`；解析不到的 id → 两个字段均为 `None`

- [ ] **Step 1: 写失败测试**

追加到 `backend/app/tests/integration/test_wiki_link_admin_api.py`：

```python
async def test_list_links_includes_ontology_name_and_alias(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """列表必须带本体对象名 —— 否则界面只能显示裸 ID，与本体管理对不上。"""
    from datetime import datetime

    from app.domain.models import OntologyClass

    dbSession.add(OntologyClass(
        id=701, class_name="DWD_ARRIVAL_ORDER_DTL", class_alias="到货单",
        version=1, valid_from=datetime.now(),
    ))
    await dbSession.commit()

    created = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class",
              "ontology_id": 701, "weight": 1.0},
    )
    assert created.status_code == 201, f"Got {created.status_code}: {created.text}"

    resp = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["ontology_name"] == "DWD_ARRIVAL_ORDER_DTL"
    assert rows[0]["ontology_alias"] == "到货单"


async def test_list_links_missing_ontology_yields_none(client: AsyncClient) -> None:
    """对象不存在（已删）→ 两个字段为 None，接口不 500。"""
    created = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "class",
              "ontology_id": 999999, "weight": 1.0},
    )
    assert created.status_code == 201, f"Got {created.status_code}: {created.text}"

    resp = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    rows = resp.json()
    assert rows[0]["ontology_name"] is None
    assert rows[0]["ontology_alias"] is None
```

> 该文件已有：`AUTH_HEADERS = {"X-User-Id": "admin", "X-User-Roles": "admin"}`（`:10`）、autouse fixture `seed_wiki_page` 种下 `wiki_page.page_id = "p001"` 并跑 RBAC baseline。**没有** `_seedPage` 辅助 —— 页面用现成的 `p001`，链接通过 public API `POST` 建立（与文件既有风格一致）。`createLink` 不校验本体对象是否存在（无 FK），故 `ontology_id=999999` 能建成功，正好用来测「解析不到」这一支。

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/integration/test_wiki_link_admin_api.py -k ontology_name -v
```

Expected: FAIL —— `KeyError: 'ontology_name'`（响应里没有该字段）

- [ ] **Step 3: 写最小实现**

`backend/app/domain/schemas.py` 的 `WikiLinkOut` 加两个字段：

```python
class WikiLinkOut(BaseModel):
    id: int
    page_id: str
    chunk_id: str | None
    ontology_type: str
    ontology_id: int
    # 本体对象名（列表展示用）。对象已删/不存在时为 None，前端回退显示 ID。
    ontology_name: str | None = None
    ontology_alias: str | None = None
    weight: float
    note: str | None
    created_by: int
    revoked_time: datetime | None
```

`backend/app/api/v1/admin_wiki_links.py`：把 `_row_to_out` 换成「解析表 + 异步出口」两个函数：

```python
_ONTOLOGY_NAME_FIELDS: dict[str, tuple[Any, str, str]] = {
    "class": (OntologyClass, "class_name", "class_alias"),
    "property": (OntologyProperty, "property_name", "property_alias"),
    "metric": (OntologyMetric, "metric_name", "metric_alias"),
}


async def _resolveOntologyLabels(
    session: AsyncSession, rows: list[WikiLinkRow],
) -> dict[tuple[str, int], tuple[str | None, str | None]]:
    """批量解析 (type, id) → (name, alias)。每张本体表一次 IN 查询，避免 N+1。"""
    out: dict[tuple[str, int], tuple[str | None, str | None]] = {}
    byType: dict[str, list[int]] = {}
    for r in rows:
        byType.setdefault(r.ontology_type, []).append(r.ontology_id)

    for ontologyType, ids in byType.items():
        fields = _ONTOLOGY_NAME_FIELDS.get(ontologyType)
        if fields is None:
            continue
        model, nameAttr, aliasAttr = fields
        stmt = select(model.id, getattr(model, nameAttr), getattr(model, aliasAttr)).where(
            model.id.in_(set(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[(ontologyType, row[0])] = (row[1], row[2])
    return out


async def _rowsToOut(
    session: AsyncSession, rows: list[WikiLinkRow],
) -> list[dict]:
    labels = await _resolveOntologyLabels(session, rows)
    out = []
    for r in rows:
        name, alias = labels.get((r.ontology_type, r.ontology_id), (None, None))
        out.append(WikiLinkOut(
            id=r.id, page_id=r.page_id, chunk_id=r.chunk_id,
            ontology_type=r.ontology_type, ontology_id=r.ontology_id,
            ontology_name=name, ontology_alias=alias,
            weight=float(r.weight), note=r.note, created_by=r.created_by,
            revoked_time=r.revoked_time,
        ).model_dump(mode="json"))
    return out
```

四个端点改为 `return await _rowsToOut(session, rows)`（单条场景传 `[row]` 并取 `[0]`）。`list_links` 的两个分支各调一次 `_rowsToOut`。

import 补齐：`from sqlalchemy import select`；`from app.domain.models import OntologyClass, OntologyMetric, OntologyProperty`；`Any`。

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/integration/test_wiki_link_admin_api.py -v
```

Expected: 全部 PASS

---

## Task 5: 前端显示对象名 + 名称顺序随语言

**Files:**
- Create: `frontend/src/utils/ontologyLabel.ts`
- Create: `frontend/src/utils/ontologyLabel.test.ts`
- Create: `frontend/src/components/ontology/classOptions.test.ts`
- Modify: `frontend/src/components/ontology/classOptions.ts`（签名变更）
- Modify: `frontend/src/components/ontology/{JoinTab,MetricTab,PropertyTab,SemanticRelationTab,ClassTab}.tsx`（11 处调用点 + 取 `locale`）
- Modify: `frontend/src/types/wikiLink.ts`（`WikiLink` 加两字段）
- Modify: `frontend/src/pages/WikiLinksPage.tsx`（列表行 + 下拉 label）

**Interfaces:**
- Consumes: Task 3 的 `wikiLinks.tabs.*`；Task 4 的 `ontology_name` / `ontology_alias`
- Produces:
  - `ontologyObjectLabel(name: string, alias: string | null | undefined, locale: string): string` —— zh → `别名（物理名）`、en → `物理名 (alias)`、别名空 → 物理名
  - **签名变更**：`classOptions(classes, locale)` / `classOptionLabel(className, classAlias, locale)` —— 去掉了原来的首参 `t`，`locale` 为必填第三/第三参。**Task 8 不受影响**（它只动 wiki 链接页与类型）

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/utils/ontologyLabel.test.ts`：

```ts
import { describe, it, expect } from "vitest";
import { ontologyObjectLabel } from "./ontologyLabel";

describe("ontologyObjectLabel", () => {
  it("中文下别名在前：别名（物理名）", () => {
    expect(ontologyObjectLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "zh-CN"))
      .toBe("到货单（DWD_ARRIVAL_ORDER_DTL）");
  });

  it("英文下物理名在前：物理名 (alias)", () => {
    expect(ontologyObjectLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "en-US"))
      .toBe("DWD_ARRIVAL_ORDER_DTL (到货单)");
  });

  it("无别名时只显示物理名（两种语言一致）", () => {
    expect(ontologyObjectLabel("DIM_SUPPLIER", null, "zh-CN")).toBe("DIM_SUPPLIER");
    expect(ontologyObjectLabel("DIM_SUPPLIER", undefined, "en-US")).toBe("DIM_SUPPLIER");
  });

  it("空字符串别名按无别名处理", () => {
    expect(ontologyObjectLabel("DIM_SUPPLIER", "", "zh-CN")).toBe("DIM_SUPPLIER");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run src/utils/ontologyLabel.test.ts
```

Expected: FAIL —— 模块不存在（`Failed to resolve import "./ontologyLabel"`）

- [ ] **Step 3: 写最小实现**

新建 `frontend/src/utils/ontologyLabel.ts`：

```ts
/**
 * 本体对象名的展示口径（单源）。
 *
 * 中文（默认）：别名在前 —— 「到货单（DWD_ARRIVAL_ORDER_DTL）」
 * 英文：物理名在前 —— 「DWD_ARRIVAL_ORDER_DTL (到货单)」
 * 无别名（含空串）：只显示物理名，两种语言一致。
 *
 * 本体管理页与 Wiki 链接管理页共用本函数，避免两处各写一份格式化逻辑后漂移。
 * 注意：本体数据的别名目前大量为空（class_alias 0/52、property_alias 0/3642），
 * 所以多数对象会落到「只显示物理名」这一支 —— 与本体管理页表现一致。
 */
export function ontologyObjectLabel(
  name: string,
  alias: string | null | undefined,
  locale: string,
): string {
  if (!alias) {
    return name;
  }
  const isChinese = locale.startsWith("zh");
  return isChinese ? `${alias}（${name}）` : `${name} (${alias})`;
}
```

`frontend/src/components/ontology/classOptions.ts` 改为纯复用（**去掉 `t` 参数、`locale` 变必填**）：

```ts
/** 类下拉选项的 label 计算与构建，供本体管理各 Tab 与 Wiki 链接管理页复用。
 *
 * 文案格式的 SSOT 在 utils/ontologyLabel —— 中文「别名（物理名）」、英文「物理名 (alias)」。
 * 原先的 t("forms.ontology.classOptionLabel") 走 i18n，但那只能换括号样式、换不了顺序；
 * 中文要「中文打头」必须按语言换结构，故收敛到 helper。
 *
 * locale 必填而非默认 "zh-CN"：漏传就在 `tsc -b` 处报错，而不是静默给英文用户塞中文格式。
 */
import { ontologyObjectLabel } from "../../utils/ontologyLabel";
import type { OntologyClass } from "../../types/ontology";

export function classOptionLabel(
  className: string,
  classAlias: string | null | undefined,
  locale: string,
): string {
  return ontologyObjectLabel(className, classAlias, locale);
}

export function classOptions(
  classes: OntologyClass[],
  locale: string,
): { label: string; value: number }[] {
  return classes.map((c) => ({
    label: classOptionLabel(c.className, c.classAlias, locale),
    value: c.id,
  }));
}
```

`t` 与 `TFunc` / `Vars` 的导入一并删除（`t` 已无用处；留着会触发 lint 未使用告警）。

**同步改 6 个组件的 11 处调用点**（漏一处 `tsc -b` 就红，这是刻意的编译期兜底）：

| 文件 | 行 | 改法 |
|---|---|---|
| `JoinTab.tsx` | 101, 296, 319 | `classOptions(t, classes)` → `classOptions(classes, locale)` |
| `MetricTab.tsx` | 81, 314 | 同上 |
| `PropertyTab.tsx` | 121, 299 | 同上 |
| `SemanticRelationTab.tsx` | 248, 260 | 同上 |
| `ClassTab.tsx` | 274 | 同上 |
| —（`classOptions.ts:28` 内部） | — | 已在上面改 |

每个文件里把 `const { t } = useTranslation();` 改为 `const { t, locale } = useTranslation();`（**只加 `locale`，`t` 仍被这些文件其它地方使用**）。改完跑 `npx tsc -b` 确认零错 —— 这是本任务「11 处都改到了」的机械证据。

> ⚠️ `frontend/tsconfig.json:15` 开了 **`noUnusedLocals: true`**。若某个文件里 `t` 除了这几处 `classOptions`
> 之外确实没别的用处，改完 `t` 就成了未使用局部变量、`tsc -b` 会报 TS6133 —— **此时把该文件的
> `const { t, locale } = useTranslation();` 收成 `const { locale } = useTranslation();`**，
> 不要用 `void t` / eslint-disable 之类去掩盖。实测 `classOptions` 的调用点分布：
> `JoinTab.tsx:101/296/319`、`MetricTab.tsx:81/314`、`PropertyTab.tsx:121/299`、
> `SemanticRelationTab.tsx:248/260`、`ClassTab.tsx:274`（共 10 处组件内）+ `classOptions.ts:28`（内部）= 11 处。

**新增用例**（追加到 `frontend/src/utils/ontologyLabel.test.ts` 之外，另建 `frontend/src/components/ontology/classOptions.test.ts`）：

```ts
import { describe, it, expect } from "vitest";
import { classOptions, classOptionLabel } from "./classOptions";

const CLASSES = [
  { id: 1, className: "DWD_ARRIVAL_ORDER_DTL", classAlias: "到货单" },
  { id: 2, className: "DIM_SUPPLIER", classAlias: null },
] as never; // 只用到 className / classAlias / id，其余字段与本用例无关

describe("classOptions", () => {
  it("中文下中文别名前置", () => {
    expect(classOptionLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "zh-CN"))
      .toBe("到货单（DWD_ARRIVAL_ORDER_DTL）");
  });

  it("英文下物理名前置", () => {
    expect(classOptionLabel("DWD_ARRIVAL_ORDER_DTL", "到货单", "en-US"))
      .toBe("DWD_ARRIVAL_ORDER_DTL (到货单)");
  });

  it("无别名的类两种语言都只显示物理名，且 value 仍是数字 id", () => {
    expect(classOptions(CLASSES, "zh-CN")).toEqual([
      { label: "到货单（DWD_ARRIVAL_ORDER_DTL）", value: 1 },
      { label: "DIM_SUPPLIER", value: 2 },
    ]);
  });
});
```

> 若 `as never` 让 lint 不满意，改用 `as unknown as OntologyClass[]` 并补 import —— 意图是只提供本用例真正读到的三个字段。

`frontend/src/types/wikiLink.ts` 的 `WikiLink` 加：

```ts
  ontology_name: string | null;
  ontology_alias: string | null;
```

> 这两个字段是**必填**（`string | null`，不是可选），所以所有 `WikiLink` 字面量夹具都会被 `tsc -b`
> 要求补齐。实测构造 `WikiLink` 字面量的地方只有 `src/pages/__tests__/WikiLinksPage.test.tsx` 与
> `src/pages/WikiLinksPage.tsx`（`src/api/adminWikiLinks.ts` 只转接响应，无需改）——
> 夹具补 `ontology_name: null, ontology_alias: null` 即可，别把字段改成可选来绕过编译。

`frontend/src/pages/WikiLinksPage.tsx`：

- `const { t, locale } = useTranslation();`（`locale` 已由 hook 返回）
- `:301` 的 `ontology_id=` 一行替换为：

```tsx
                <span>
                  {link.ontology_name
                    ? ontologyObjectLabel(link.ontology_name, link.ontology_alias, locale)
                    : `ID:${link.ontology_id}`}
                </span>
```

- 弹窗下拉 label（`:369-372`）改为：

```tsx
              options={linkables.map((tgt) => ({
                value: tgt.id,
                label: ontologyObjectLabel(tgt.name, tgt.alias, locale),
              }))}
```

- 补 import：`import { ontologyObjectLabel } from "../utils/ontologyLabel";`

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npx tsc -b && npx vitest run src/utils/ontologyLabel.test.ts src/components/ontology/classOptions.test.ts src/pages/__tests__/WikiLinksPage.test.tsx
```

Expected: `tsc -b` 零报错（= 11 处调用点都改到了）、vitest 全绿。

> `forms.ontology.classOptionLabel` / `classOptionLabelNoAlias` 两个 i18n 键在本次改动后**不再被使用**（`grep -rn classOptionLabel src/ --include='*.tsx' --include='*.ts'` 应只剩新 helper）。**保留不要删** —— 删键属于另一件事，且 en-US / zh-CN 必须同增同删，本次不做。在 Task 9 的文档里记一笔「已成为死键，待后续清理」。

---

## Task 6: migration 放开 `chk_link_type` 到 metric

**Files:**
- Create: `backend/alembic/versions/0106_wiki_link_metric_type.py`

**Interfaces:**
- Consumes: 现有约束 `chk_link_type`（定义见 migration `0091`/`wiki_ontology_link` 建表）
- Produces: 约束变为 `ontology_type IN ('class','property','metric')`

- [ ] **Step 1: 写迁移**

新建 `backend/alembic/versions/0106_wiki_link_metric_type.py`：

```python
"""wiki_ontology_link 的 ontology_type 放开到 metric（C 档：指标入链，2026-09-30）。

**触发**：Wiki 链接管理只能绑 class / property，本体指标不可入链。调查发现类型被
写死在 6 处，其中 DB 层就是本约束 ``chk_link_type``。

**变更**：drop 后按三个值重建同名约束（PostgreSQL 的 CHECK 不能就地改，必须 drop +
add）。约束名保持不变，避免下游脚本/文档引用失效。

**安全性**：当前生产 ``wiki_ontology_link`` **0 行**，重建约束不会因存量数据违反而失败；
且新集合是旧集合的**超集**，任何存量行都必然满足新约束。

**降级是破坏性的**：downgrade 必须先删除 metric 行才能重建两值约束（否则 ADD
CONSTRAINT 会失败）。沿用 0098 的先例（其 downgrade 亦删除不合规行），此处显式删除
并把后果写在这里 —— 丢失的是「指标链接」这一新类型的配置，class/property 不受影响。

**两库同步**：prod + test 都要 upgrade。

Revision ID: 0106
"""

from __future__ import annotations

from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels = None
depends_on = None

_CONSTRAINT = "chk_link_type"
_OLD_VALUES = ("class", "property")
_NEW_VALUES = ("class", "property", "metric")


def _sql(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"ontology_type IN ({joined})"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "wiki_ontology_link", type_="check")
    op.create_check_constraint(_CONSTRAINT, "wiki_ontology_link", _sql(_NEW_VALUES))


def downgrade() -> None:
    # 先清掉新类型行，否则旧的两值约束加不回去（详 docstring）
    op.execute("DELETE FROM wiki_ontology_link WHERE ontology_type = 'metric'")
    op.drop_constraint(_CONSTRAINT, "wiki_ontology_link", type_="check")
    op.create_check_constraint(_CONSTRAINT, "wiki_ontology_link", _sql(_OLD_VALUES))
```

- [ ] **Step 2: 在测试库上验证 upgrade / downgrade 往返**

```bash
cd backend
export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:$(docker exec qa-pg-a1 printenv POSTGRES_PASSWORD)@localhost:5434/qa_metadata_test"
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
```

Expected: `current` 输出 `0106 (head)`

> ⚠️ **`alembic` 只认 `DATABASE_URL`**（`alembic/env.py`），而 `DATABASE_URL` 默认指向 **prod 5432/5433**。上面显式覆盖为测试库；**验证 prod 迁移前必须再确认一次目标**。

- [ ] **Step 3: 验证约束真的放开**

```bash
docker exec qa-pg-a1 psql -U qa_user -d qa_metadata_test -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'chk_link_type';"
```

Expected: 输出含 `ontology_type = ANY (ARRAY['class'::character varying, 'property'::character varying, 'metric'::character varying])`

- [ ] **Step 4: 验证 downgrade 可回退**

```bash
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic downgrade 0105
docker exec qa-pg-a1 psql -U qa_user -d qa_metadata_test -c "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'chk_link_type';"
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
```

Expected: downgrade 后约束只剩两个值；再 upgrade 回到三个值

---

## Task 7: 后端放开 metric 类型

> ⚠️ **初稿漏了第 6 处之外的又一个闸门（2026-09-30 实读代码后补）**：Task 9 的自审声称
> 「注入器 `_VALID_TYPES` 由 Task 7 一并放开」，但初稿的 Files 里**没有 `wiki_injector.py`**，
> Task 2 的 brief 还明确禁止改它。照初稿执行 ⇒ `wiki_injector.py:100-101` 会把
> **所有 metric 链接静默丢弃**，与 A 档修好的死链是同一个失败模式，而且是一类**测试抓不到**的
> 静默丢弃。故本任务**必须**包含 `wiki_injector.py:67` 的 `_VALID_TYPES`，并配双向单测。

> ⚠️ **还有第 7 处（Task 6 审查后补，2026-09-30）**：`backend/app/domain/models.py` 的 ORM
> **也**声明了同一个 `chk_link_type`，且 docstring 写着「二选一」。Task 6 只改迁移、Files 未含此文件
> （审查者判其**不属 Task 6 交付范围**，与本裁定一致）。**但必须在本任务修**，理由：
> - 真实 PG 与 prod 的 schema 由 alembic 产生 ⇒ 不改它**运行期无害**（SQLAlchemy 的
>   `CheckConstraint` 是纯 DDL，从不做客户端校验）；
> - 它只在 `create_all` 建表处生效，而 `app/tests/conftest.py:70-78` 的 `dbEngine`（sqlite 内存库）
>   正是 `dbSession` 的来源 ⇒ `test_wiki_link_service.py` 里**ORM 声明的约束才是真正生效的约束**。
>   本任务落地后若经该路径插入 metric 链接，会抛 `IntegrityError`（错因看似 DB、实为 ORM）。
> - 现有的结构漂移守卫 `app/infrastructure/schema_drift.py` **只比表/列/索引、不比 CHECK 约束**
>   （其 docstring 自述粒度）⇒ 这条漂移**没有任何测试能抓到**，属 A 档那类「潜伏且全绿」的缺陷。
> - docstring `:2156` 在本任务后成为**事实错误**。

**Files:**
- Modify: `backend/app/services/wiki_link_service.py:27`（`_VALID_ONTOLOGY_TYPES`）、`:44-49`（`LinkableTarget.type` 注释）、`:210-263`（`listLinkableTargets` 加 metric 分支；Task 1 落地后实测，初稿的 `:192-245` 已下移 18 行，以函数名为准）
- Modify: `backend/app/api/v1/admin_wiki_links.py`（`CreateWikiLinkRequest.pattern`）
- Modify: `backend/app/services/wiki_injector.py:67`（`_VALID_TYPES` 加 `"metric"`）
- Modify: `backend/app/domain/models.py:2156`（docstring 改三类型）、`:2186-2189`（`CheckConstraint` 字符串加 `'metric'`）
- Test: `backend/app/tests/unit/test_wiki_link_service.py`、`backend/app/tests/unit/test_wiki_injector.py`、`backend/app/tests/integration/test_wiki_link_admin_api.py`

**Interfaces:**
- Consumes: `OntologyMetric` ORM（`metric_name` / `metric_alias`；**无 `description` 列**）
- Produces: `listLinkableTargets(session, "metric", ...)` 返回 `LinkableTarget(type="metric", ...)`；`createLink` 接受 `ontology_type="metric"`；`WikiInjector.collectAndScore` 接受 `type="metric"`

- [ ] **Step 1: 写失败测试**

(a) `backend/app/tests/unit/test_wiki_link_service.py` 追加（若文件顶部未 import `OntologyMetric`，在 `from app.domain.models import WikiOntologyLink` 一行补上）。

> ⚠️ **该文件里有两条既有用例都拿 `"metric"` 当「非法类型」，两条都必须一并改掉** ——
> 它们断言的正是本次要推翻的行为。初稿只提了第一条（2026-09-30 实读代码后补第二条）：
>
> | 行 | 用例 | 现状 | 改法 |
> |---|---|---|---|
> | `:168-170` | `test_list_linkable_targets_invalid_type_returns_empty` | `listLinkableTargets(dbSession, "metric", ...) == []` | 换 `"unknown"` |
> | `:173-181` | `test_create_link_invalid_type_returns_422` | `createLink(..., ontology_type="metric", ...)` 期待 `ValidationError` | 换 `"unknown"` |
>
> 漏改第二条，它就会在 Step 3 之后**变红**（metric 变合法，不再抛异常）。

```python
async def test_list_linkable_targets_metric_returns_metrics(dbSession):
    """metric 现在必须返回指标，而不是空列表。"""
    dbSession.add(OntologyMetric(
        id=801, metric_name="KPI_SUPPLIER_OTD", metric_alias="供应商准时交付率",
        formula="SUM(a)/SUM(b)", agg_function="SUM",
    ))
    await dbSession.flush()

    targets = await _svc.listLinkableTargets(dbSession, "metric", query=None, limit=10)
    assert [(t.id, t.type, t.name, t.alias) for t in targets] == [
        (801, "metric", "KPI_SUPPLIER_OTD", "供应商准时交付率"),
    ]


async def test_list_linkable_targets_invalid_type_returns_empty(dbSession):
    """非法类型仍返回空列表（不抛异常）—— 反例换成一个真正非法的值。"""
    assert await _svc.listLinkableTargets(dbSession, "unknown", query=None, limit=10) == []
```

（`query=None` + 精确相等成立的前提是 `ontology_metric` 在用例开始时是空表 —— 既有用例
`list_linkable_targets_invalid_type_returns_empty` 断言 `== []` 正是这个前提的现成证据。
若实际输出里冒出别的行，说明该文件没做 truncate，**如实报告**，别把断言改成 `in`。）

同文件再追加一条 —— **它同时是 Step 3 里 `models.py` 那处改动的驱动测试**：

```python
async def test_create_metric_link_persists(dbSession):
    """metric 链接必须能落库（service 层成功路径）。

    与 integration 的 test_create_metric_link_accepted 互补：那条走 HTTP 全链路，
    这条只压 service 层（createLink 的类型校验 + 落库两件事）。

    注意：本用例**不能**用来钉 ORM 的 chk_link_type —— unit/ 目录由
    app/tests/unit/conftest.py 覆盖 dbSession 为真实 PG（表结构由 alembic 产生），
    ORM 元数据不参与建表。app/domain/models.py 那处 CheckConstraint 与迁移 0106
    对齐是为了 ORM↔迁移一致；当前**没有任何测试路径会强制执行它**
    （根 conftest 的 create_all 路径已无消费者）。
    """
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="metric",
        ontology_id=801, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    assert row.ontology_type == "metric"
    assert row.ontology_id == 801
```

> `createLink` **不校验本体目标是否存在**（只校验 type/weight/actor.dbUserId）⇒ 本用例
> **不需要**先 seed `ontology_metric` 行。顺带记录：`models.py:2156` 那句
> 「Service 层在写入时校验目标存在」是**既有的不实描述**，本次只改类型枚举，不顺手改它。

**两条既有用例就地改（不是新增，别追加出重名函数）**：

```python
# :168-170 —— 改前：assert await _svc.listLinkableTargets(dbSession, "metric", ...) == []
async def test_list_linkable_targets_invalid_type_returns_empty(dbSession):
    """非法类型仍返回空列表（不抛异常）—— 反例换成一个真正非法的值。"""
    assert await _svc.listLinkableTargets(dbSession, "unknown", query=None, limit=10) == []
```

```python
# :173-181 —— 改前：ontology_type="metric" 期待 ValidationError
async def test_create_link_invalid_type_returns_422(dbSession):
    await _seed_page(dbSession)
    with pytest.raises(ValidationError):
        await _svc.createLink(
            dbSession,
            page_id="p001", chunk_id=None, ontology_type="unknown",
            ontology_id=12, weight=Decimal("1.00"), note=None,
            actor=await _actor(42),
        )
```

> 这两条是本次**刻意推翻**的旧行为，改动必须在 diff 里看得见。只改 `ontology_type` 字面值，
> 用例名与断言保持原样。

(b) `backend/app/tests/integration/test_wiki_link_admin_api.py` 追加：

```python
async def test_create_metric_link_accepted(
    client: AsyncClient, dbSession: AsyncSession,
) -> None:
    """API 必须接受 ontology_type=metric（修复前被 422 pattern 拒绝）。"""
    from app.domain.models import OntologyMetric

    dbSession.add(OntologyMetric(
        id=802, metric_name="KPI_PURCHASE_CYCLE_TIME", metric_alias="采购周期",
        formula="AVG(x)", agg_function="AVG",
    ))
    await dbSession.commit()

    resp = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "metric",
              "ontology_id": 802, "weight": 1.0},
    )
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    assert resp.json()["ontology_type"] == "metric"

    # 列表必须能解析出指标名（Task 4 的解析表已含 metric）
    listed = await client.get(
        "/api/v1/admin/wiki-links?page_id=p001", headers=AUTH_HEADERS,
    )
    assert listed.json()[0]["ontology_name"] == "KPI_PURCHASE_CYCLE_TIME"


async def test_create_link_rejects_unknown_type(client: AsyncClient) -> None:
    """放开的只有三个值；非法类型仍 422（双向断言）。"""
    resp = await client.post(
        "/api/v1/admin/wiki-links", headers=AUTH_HEADERS,
        json={"page_id": "p001", "chunk_id": None, "ontology_type": "join",
              "ontology_id": 1, "weight": 1.0},
    )
    assert resp.status_code == 422
```

(c) **`backend/app/tests/unit/test_wiki_injector.py` 追加两条**（双向：metric 收进来 + 非法类型仍拦下）。
直接复用该文件既有的 `_rec` / `_lnk` / `_budget` 辅助，追加在文件末尾：

```python
def test_metric_type_is_accepted():
    """metric 必须在 _VALID_TYPES 内 —— 否则 A 档刚修好的链在注入器处又被静默丢弃。

    这是「六处枚举」里最隐蔽的一处：wiki_injector 拦掉之后既无日志也无前端提示，
    表现与本次要修的 A 档缺陷完全一样。
    """
    recalled = [_rec("metric", 8, 0.7)]
    links = [_lnk("p001", None, "metric", 8, "1.0")]
    chunks = {("p001", ""): "指标口径"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert len(out) == 1
    assert out[0].applied_to == [("metric", 8)]


def test_unknown_type_is_dropped():
    """反例：非法类型仍被丢弃 —— 别为了放 metric 把闸门整个拆了。"""
    recalled = [_rec("join", 8, 0.7)]
    links = [_lnk("p001", None, "join", 8, "1.0")]
    chunks = {("p001", ""): "x"}
    assert WikiInjector.collectAndScore(recalled, links, chunks, _budget()) == []
```

> 该文件 `:1-9` 的模块 docstring 自称「纯函数 12 例」，加完这两条计数就过时了 —— **顺手改成实际数字**。

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest app/tests/unit/test_wiki_link_service.py -v
pytest app/tests/unit/test_wiki_injector.py -v
pytest app/tests/integration/test_wiki_link_admin_api.py -v
```

Expected: FAIL ——
- `test_list_linkable_targets_metric_returns_metrics` → 空列表
- `test_create_metric_link_persists` → `ValidationError`（此刻 `_VALID_ONTOLOGY_TYPES` 仍是两值，`createLink` 在校验处就拒了，**还没走到 ORM 的 CHECK**）
- `test_metric_type_is_accepted` → `collectAndScore` 返回 `[]`（metric 被 `_VALID_TYPES` 拦掉）
- `test_create_metric_link_accepted` → 422
- `test_create_link_rejects_unknown_type` → **也红**（`ValidationError` 被 `createLink` 抛出 → 目前会转成什么状态码取决于全局处理器；先记录实际值，Step 3 后应为 422）
- `test_unknown_type_is_dropped` → **一开始就该绿**（反例，守住闸门没被整个拆掉）

> 不要在一个 pytest 进程里同时跑 unit 与 integration（见 Global Constraints）—— 分成三条命令。

- [ ] **Step 3: 写最小实现**

`backend/app/services/wiki_link_service.py`：

```python
_VALID_ONTOLOGY_TYPES = frozenset({"class", "property", "metric"})
```

`backend/app/domain/models.py` —— **同一件事的第 7 处枚举**（迁移 0106 已放开 DB，ORM 声明没跟上）。
理由是 **ORM↔迁移一致性 + docstring 事实正确**，**不是**为了测试：实测该文件在四个测试目录里
被 `unit/conftest.py`、`services/conftest.py`、`integration/conftest.py` 逐层覆盖为真实 PG
（表结构由 alembic 产生），根 conftest 的 sqlite `dbEngine`（`create_all`）**已无任何消费者**
⇒ 这处声明在当前测试路径下不会被强制执行。仍然要改，因为它就是 schema 契约的一部分，
且 docstring 会变成事实错误：

```python
# :2186-2189 —— 原为 "ontology_type IN ('class','property')"
        CheckConstraint(
            "ontology_type IN ('class','property','metric')",
            name="chk_link_type",
        ),
```

```python
# :2156 —— docstring 原文「``ontology_type`` 由 ``chk_link_type`` CHECK 约束为 'class'/'property' 二选一；」
# 改为三类型（迁移 0106 起）
    - ``ontology_type`` 由 ``chk_link_type`` CHECK 约束为 'class'/'property'/'metric' 三选一
      （迁移 0106 放开；与 ``OntologyMetric`` 对应）；仍与 ontology_class.id /
      ontology_property.id / ontology_metric.id 没有 FK（本体类属性可任意修改
      / 重命名，硬 FK 会拖累回滚）。
```

> 顺带核对：该 docstring 紧接着写的「Service 层在写入时校验目标存在」**是不实描述**
> （`createLink` 只校验 type/weight/actor.dbUserId，见 `wiki_link_service.py:69-78`）。
> **本次不顺手改它** —— 与本任务的类型枚举无关，改了会让 diff 混入无关语义变更。

`backend/app/services/wiki_injector.py:67`：

```python
_VALID_TYPES = frozenset({"class", "property", "metric"})
```

import 补 `OntologyMetric`：

```python
from app.domain.models import (
    OntologyClass, OntologyMetric, OntologyProperty, WikiOntologyLink,
)
```

`LinkableTarget.type` 注释改为 `# 'class' | 'property' | 'metric'`。

`listLinkableTargets` 在 property 分支之后、`return []` 之前插入：

```python
        if type == "metric":
            stmt = select(OntologyMetric)
            if query:
                pattern = f"%{query.upper()}%"
                stmt = stmt.where(
                    or_(
                        OntologyMetric.metric_name.ilike(pattern),
                        OntologyMetric.metric_alias.ilike(pattern),
                    )
                )
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                LinkableTarget(
                    id=r.id,
                    type="metric",
                    name=r.metric_name,
                    alias=r.metric_alias,
                    description=None,
                )
                for r in rows
            ]
```

> `OntologyMetric` 无 `description` 列（实测列清单：`metric_name/metric_alias/formula/agg_function/target_class_id/dimension_defaults/created_by`）—— 故 `description=None`。

`backend/app/api/v1/admin_wiki_links.py`：

```python
    ontology_type: str = Field(..., pattern="^(class|property|metric)$")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest app/tests/unit/test_wiki_link_service.py -v
pytest app/tests/unit/test_wiki_injector.py -v
pytest app/tests/integration/test_wiki_link_admin_api.py -v
```

Expected: 三条命令各自全绿

---

## Task 8: 前端加「指标」Tab

**Files:**
- Modify: `frontend/src/types/wikiLink.ts:6`、`frontend/src/api/adminWikiLinks.ts:64`
  （行号经 Task 3/5 落地后实读核过：`listLinkableTargets` 在 `:64`；`WikiLinksPage.tsx` 的 Tabs `items` 在
  `:272-274`、`openAddModal` 在 `:193`）
- Modify: `frontend/src/i18n/zh-CN.ts` / `en-US.ts`（`wikiLinks.tabs.metric`）
- Modify: `frontend/src/pages/WikiLinksPage.tsx`（Tabs 第三项 + `TYPE_LABEL_KEY`）
- Test: `frontend/src/pages/__tests__/WikiLinksPage.test.tsx`

**Interfaces:**
- Consumes: Task 3 的 `wikiLinks.tabs.class/property`；Task 7 的后端
- Produces: `WikiLinkType = "class" | "property" | "metric"`

- [ ] **Step 1: 写失败测试**

追加到 `frontend/src/pages/__tests__/WikiLinksPage.test.tsx`：

```tsx
  it("中文下存在「指标」Tab，切换后用 metric 拉取可链接目标", async () => {
    await i18n.changeLanguage("zh-CN");
    api.listLinkableTargets.mockResolvedValue([]);
    renderPage();

    const metricTab = await screen.findByRole("tab", { name: "指标" });
    await userEvent.click(metricTab);

    // 切 Tab 本身**不**触发拉取 —— `listLinkableTargets` 只在 `openAddModal`
    // （WikiLinksPage.tsx:186-194）里调用，必须先选树节点让按钮可用再点它。
    // 初稿漏了这一步，按原样写这条用例永远不可能变绿。
    await clickTreeNode("供应商准入流程");
    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    await waitFor(() => expect(addBtn).toBeEnabled());
    await userEvent.click(addBtn);

    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("metric");
    });
  });
```

> 复用的是同文件既有辅助：`clickTreeNode(name)`、`renderPage()`，以及既有用例
> `opens the add modal and fetches linkables for the active tab`（`WikiLinksPage.test.tsx:269-292`）
> 的按钮选择器 `/\+\s*添加绑定/`（antd Button 会在汉字间插 U+0020，别用精确串）。
> 若 `findByRole("tab", { name: "指标" })` 因无障碍名空白差异找不到，退成 `{ name: /指标/ }` ——
> 但**不要**改断言去迁就。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run src/pages/__tests__/WikiLinksPage.test.tsx
```

Expected: FAIL —— 找不到名为「指标」的 tab（三值 Tabs 尚未落地；注意此时**中文/英文标签用例仍应是绿的**，
因为 Task 3 已完成，失败点只在「指标」这一项）

- [ ] **Step 3: 写最小实现**

```ts
// frontend/src/types/wikiLink.ts
export type WikiLinkType = "class" | "property" | "metric";
```

```ts
// frontend/src/api/adminWikiLinks.ts
export async function listLinkableTargets(
  type: WikiLinkType,
  query?: string,
): Promise<WikiLinkableTarget[]> {
```

（并把 `import type { WikiLinkType }` 加进该文件的类型 import）

i18n 两文件各加一行（zh `指标` / en `Metric`）。

`frontend/src/pages/WikiLinksPage.tsx` 的 Tabs items 加第三项：

```tsx
              { key: "metric", label: t("wikiLinks.tabs.metric") },
```

并给 Task 3 定义的 `TYPE_LABEL_KEY` 补上第三行（`Record<WikiLinkType, string>` 要求键齐全）：

```tsx
  metric: "wikiLinks.tabs.metric",
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npx vitest run src/pages/__tests__/WikiLinksPage.test.tsx
```

Expected: 全部 PASS

---

## Task 9: 回归对账、文档与部署

**Files:**
- Modify: `Harness/changes/2026-09-30-wiki-ontology-link-fix/summary.md`（状态 + 验收结果）
- Modify: `Harness/wiki/wiki-ontology-link.md`（下表 9 处）+ `Harness/wiki/chat-service-capabilities.md`（若提到类型集合）
- Modify: `wiki/log.md`、`wiki/index.md`（知识库规则）
- Modify（**注释真话化，本轮唯一动到的代码文件**，均为 comment/docstring 单行改动、零运行期影响）：
  `backend/app/services/wiki_injector.py:22`（`ScoredOntology.type` 注释）、
  `backend/app/services/wiki_link_service.py:218`（`listLinkableTargets` docstring）、
  `backend/app/domain/models.py:2149`（类 docstring，与同块 `:2156` 自相矛盾）、
  `backend/app/domain/schemas.py:3628`（`WikiLinkableTargetOut.type` 注释）

**Interfaces:**
- Consumes: 前 8 个任务的全部产物
- Produces: 无代码接口

> ⛔ **本任务的派单只包含 Step 1–3（含注释真话化）。Step 4/5 是一次生产变更，
> 由控制器在用户明确确认后单独执行 —— 实现者不得执行、不得尝试、遇到部署相关内容一律停下报告。**

- [ ] **Step 1: 后端全量回归（分套件跑，不混进程）**

```bash
cd backend
pytest app/tests/unit -q
pytest app/tests/integration -q
```

Expected: 全绿；**报出实际数字**（通过/失败/跳过），不报印象值。基线与本次改动前对比，若有红用例先确认是否为 HEAD 既有红（本会话已知 `EntityMappingPage` 前端红与本次无关）。

- [ ] **Step 2: 前端全量回归**

```bash
cd frontend && npx vitest run src
```

Expected: 基线 = 1322 通过 / 1 失败（`EntityMappingPage` 既有红）。**报出实际数字**。

- [ ] **Step 3: 更新 SSOT 文档**

`Harness/changes/2026-09-30-wiki-ontology-link-fix/summary.md`：
- 顶部 `状态` 改为「已落地并复核（YYYY-MM-DD）」并补复核依据
- §三 的设计若与实现有偏差，按实际改（**不要留虚假描述**）
- 补 §五 验收的实际数字与未验项（如实标注，不冒充已验证）

`Harness/wiki/`：若某条目描述了「wiki 链接只支持 class/property」或类型集合，按实际更新。
`wiki/log.md` 追加一条、`wiki/index.md` 若新增条目则补索引行。

> `Harness/wiki/wiki-ontology-link.md` 的 9 处确切行号已由控制器逐条实读核对（2026-09-30），
> 全部准确 —— 见 `.superpowers/sdd/plan/progress.md` 的 Task 9 节，直接依赖即可。
> 其中 **`:230` 那段代码示例在 A 档改成「按需召回」后已不是当前实现的形状 ⇒ 不要只把
> `"metric"` 追加进去**，应按 `listConfiguredOntologyTypes` + `_recallExtraOntologyScores`
> 的真实口径重写；`:382` 的计数「12」是旧值（Task 1 后为 15）。

- [ ] **Step 3.5: 注释真话化（4 处，均为 comment/docstring）**

类型枚举从「二值」放开到「三值」后，残留的旧注释会**与代码自相矛盾**。逐处按实际改：

| 文件:行 | 现状 | 应改为 |
|---|---|---|
| `wiki_injector.py:22` | `type: str  # 'class' \| 'property'` | 三类型（该 dataclass 是下游消费者被告知要依赖的类型） |
| `wiki_link_service.py:218` | docstring「列 ontology_class 或 ontology_property」 | 加 `ontology_metric` |
| `models.py:2149` | 「说明某个 ontology class/property」 | 三类型（**与同块 `:2156` 自相矛盾**，必须一致） |
| `schemas.py:3628` | `# 'class' \| 'property'` | 三类型 |

> **不改** `models.py:2159` 那句「Service 层在写入时校验目标存在」—— 它**本来就是不实的**
> （`createLink` 只校验 type/weight/actor.dbUserId），与类型枚举无关，改了会污染本任务的语义边界；
> 留给后续独立处理。

- [ ] **Step 4: 部署（**不在本任务派单内；由控制器在用户确认后执行**）**

前端（改前端必须重建镜像，`npm run build` ≠ 容器 bundle）：

```bash
docker compose -f docker/docker-compose.yml build --no-cache frontend
docker compose -f docker/docker-compose.yml up -d frontend
```

> ⚠️ 构建若撞 `EIDLETIMEOUT`/超时，是 npm 默认源太慢（实测 0.47MB/s）—— 换 npmmirror 源重建
> （实测提速 ~21 倍）。本仓踩过。

后端：

```bash
./scripts/deploy_backend.sh
```

迁移（**不可逆操作 —— 必须先确认目标库再执行**）。`alembic/env.py` **只认 `DATABASE_URL`**，
而本机 shell 里该变量是**空的**（实测），未设置时回落到 `backend/.env`（5433/`qa_metadata`）与
`config.py` 默认值（5432/`qa_metadata`）—— **两者都是 prod**。故按顺序执行，**每步看输出**：

```bash
# 1) 确认容器内的目标库确实是 prod，且当前停在 0105（改前状态）
docker exec qa-backend printenv DATABASE_URL
docker exec qa-backend alembic current          # 期望：0105 (head)

# 2) 备份（prod 全表备份；约束是超集、表 0 行，风险低但不省这一步）
#    按仓库既有备份流程执行，产出文件后确认大小非 0

# 3) 迁移 + 立即回读
docker exec qa-backend alembic upgrade head
docker exec qa-backend alembic current          # 期望：0106 (head)

# 4) 约束真的改了（不信 alembic 自述，直接查库）
docker exec qa-postgres psql -U qa_user -d qa_metadata -tAc \
  "select pg_get_constraintdef(oid) from pg_constraint where conname='chk_link_type'"
# 期望：ANY (ARRAY['class','property','metric'])
```

- [ ] **Step 5: 部署后实测**

1. 无鉴权头 `GET /api/v1/admin/wiki-links` → 403（鉴权未回归）
2. `GET /api/v1/admin/wiki-links/linkables?type=metric` → 200 且为 `[]`（表空是已知事实，**不是异常**）
3. 容器代码与工作树 `sha256sum` 逐一对齐（容器内没有 `shasum`，会打印 `OCI` 是假阴性）
4. 界面上：Wiki 链接管理 Tab 显示「类 / 属性 / 指标」；切英文变 `Class / Property / Metric`
5. ⚠️ **后端重建后若 `/api` 全 502，先重启 nginx** —— nginx 会缓存 upstream IP，后端换 IP 后
   旧缓存会让整个前端菜单退回 `FALLBACK_NAV`，看起来像前端崩了（本仓踩过）

---

## 自审记录

**Spec 覆盖**：§三 A → Task 1+2；B 四条 → Task 3（1、2 两条）+ Task 4（第 3 条）+ Task 5（第 4 条）+ 数据不动（无任务，符合「明确不做」）；C 六处 → Task 6（DB）+ Task 7（service `_VALID_ONTOLOGY_TYPES`、pattern、linkables）+ Task 8（前端两处 + Tabs）+ Task 2 的 `_WIKI_EXTRA_RECALL_TYPES` 已含 metric（注入器 `_VALID_TYPES` 由 Task 7 一并放开）。

> **偏差说明**：spec §2.4 列了 5 处硬编码，实现期发现第 **6** 处 —— `wiki_link_service.py:27` 的 `_VALID_ONTOLOGY_TYPES`（它同时 gate `createLink` 的校验与 `getLinksByOntology` 的 pair 过滤）。已并入 Task 7，spec 待 Task 9 一并订正。

**类型一致性**：`listConfiguredOntologyTypes`（Task 1 定义 → Task 2 调用）、`_WIKI_EXTRA_RECALL_TYPES` / `_WIKI_EXTRA_RECALL_TOPK_DEFAULT`（Task 2 内定义与使用）、`ontologyObjectLabel(name, alias, locale)`（Task 5 定义 → Task 5 内两处调用）、`TYPE_LABEL_KEY`（Task 3 定义 → Task 8 使用）、`ontology_name` / `ontology_alias`（Task 4 后端 → Task 5 前端）均已对齐。

**无占位符**：所有步骤含可执行代码或可执行命令；无 TODO / TBD。
