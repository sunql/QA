# 召回数据源过滤 + 关键词锚定 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 召回候选集按当前数据源过滤（治跨源幽灵表 ORA-00942）+ 问句关键词/别名锚定（治「问法敏感漏选」），披露面走 ClassRecallInfo + 前端提示。

**Architecture:** 新增两个小模块（`recall_datasource_filter.py` / `recall_keyword_anchor.py`），在 `chat_recall.py` 与 `chat_service.py` 两处候选集源头接线；表名归一复用 `_validateOntologyAgainstSchema` 既有口径（裸名+owner 限定名、`.upper()` 大小写不敏感）；无需数据库迁移（判定数据来自既有 `schema_cache` 表）。

**Tech Stack:** FastAPI + SQLAlchemy async + Pydantic v2（CamelModel）/ pytest（真实 PG）/ React + antd + i18next。

**Spec（SSOT）:** `Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md`

## Global Constraints

- **测试唯一入口**：`./scripts/run_local_tests.sh <定向路径|pytest 参数>` —— **禁止裸 pytest**（会清线上 Milvus/Neo4j）。集成测试必须并入既有文件（`test_classify_and_recall.py`），unit 新建文件安全。
- **无需新迁移**：不改 `ontology_class` 结构；判定数据只读 `schema_cache`。
- **后端命名**：函数/变量 `camelCase`（项目刻意偏离 PEP 8）；ORM/Pydantic 字段 `snake_case`；常量 `UPPER_SNAKE_CASE`。
- **不可变**：过滤/锚定函数返回新列表，不改动入参（与 `_selectRelevantClasses` 既有约定一致）。
- **文件 < 800 行 / 函数 < 50 行**：`chat_recall.py` 已超限，**新逻辑一律进新模块**，只做最小接线。
- **提交格式**：`feat:|test:|docs:` 前缀，中文正文。
- 行号基于 `origin/main`（已核实目标 7 文件在 main 与采购分支零差异）。
- 本计划所有 pytest 命令都在 worktree 的**仓库根**执行（脚本按根目录定位）。

---

### Task 0: 分支与 worktree 准备

**Files:** 无代码变更（环境准备）。

**Interfaces:**
- Produces: worktree `.claude/worktrees/fix-recall-anchor`，分支 `fix/recall-datasource-anchor`（基于 `origin/main`），后续所有 Task 在该 worktree 内执行。

- [ ] **Step 1: 建 worktree（从 main 切出）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git fetch origin main
git worktree add .claude/worktrees/fix-recall-anchor -b fix/recall-datasource-anchor origin/main
cd .claude/worktrees/fix-recall-anchor/backend && uv sync --frozen --extra dev
```

（记忆约定：新 worktree 必须 `uv sync --frozen --extra dev`，否则依赖缺 dev 工具。）

- [ ] **Step 2: 验证基线定向测试绿**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.claude/worktrees/fix-recall-anchor
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -v
```

Expected: 该文件既有用例全 PASS（若有环境性失败先停下报告，不得带病开工）。

---

### Task 1: ClassRecallInfo 新披露字段（后端契约）

**Files:**
- Modify: `backend/app/domain/schemas.py`（`class ClassRecallInfo`，main 上位于 2181 行附近）
- Test: `backend/app/tests/unit/test_class_recall_info_contract.py`（新建）

**Interfaces:**
- Produces: `ClassRecallInfo` 增三字段（wire camelCase）：`datasourceFiltered: int = 0`、`datasourceFilterSkipped: str | None = None`、`anchoredCount: int = 0`。Task 4/5/6 与前端 Task 7 依赖这三个名字。

- [ ] **Step 1: 写失败测试**

```python
"""ClassRecallInfo 披露字段契约（2026-11-19 数据源过滤+锚定批次）。

默认值保证向后兼容：旧响应（无这些字段）反序列化不受影响；wire 格式
camelCase（CamelModel），前端按 datasourceFiltered / datasourceFilterSkipped /
anchoredCount 读。
"""

from app.domain.schemas import ClassRecallInfo


def test_new_disclosure_fields_default_neutral() -> None:
    info = ClassRecallInfo(mode="recall")
    assert info.datasourceFiltered == 0
    assert info.datasourceFilterSkipped is None
    assert info.anchoredCount == 0


def test_new_disclosure_fields_serialize_camel_case() -> None:
    info = ClassRecallInfo(
        mode="expanded", datasourceFiltered=3,
        datasourceFilterSkipped="no_cache", anchoredCount=2,
    )
    payload = info.model_dump(by_alias=True)
    assert payload["datasourceFiltered"] == 3
    assert payload["datasourceFilterSkipped"] == "no_cache"
    assert payload["anchoredCount"] == 2
    # 旧字段不受影响
    assert payload["mode"] == "expanded"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_class_recall_info_contract.py -v
```

Expected: FAIL —— `ClassRecallInfo` 无 `datasourceFiltered` 字段（ValidationError）。

- [ ] **Step 3: 最小实现**

`backend/app/domain/schemas.py` 的 `ClassRecallInfo` 末尾（`truncated: bool = False` 之后）追加，并同步类 docstring：

```python
    truncated: bool = False
    # 2026-11-19 数据源过滤+锚定披露：跨源类被滤数量 / 跳过原因(no_datasource|no_cache)
    # / 锚定纳入数量。默认值 = 未启用，wire 向后兼容。
    datasourceFiltered: int = 0
    datasourceFilterSkipped: str | None = None
    anchoredCount: int = 0
```

docstring 追加一行：

```python
    datasourceFiltered/datasourceFilterSkipped/anchoredCount：见
    Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md。
```

- [ ] **Step 4: 跑测试确认通过**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_class_recall_info_contract.py -v
```

Expected: 2 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/schemas.py backend/app/tests/unit/test_class_recall_info_contract.py
git commit -m "feat: ClassRecallInfo 增加数据源过滤/锚定披露字段（默认值向后兼容）"
```

---

### Task 2: recall_datasource_filter 模块（数据源过滤）

**Files:**
- Create: `backend/app/services/recall_datasource_filter.py`
- Test: `backend/app/tests/unit/test_recall_datasource_filter.py`（新建）

**Interfaces:**
- Consumes: `app.domain.models.SchemaCache`（`schema_data: list[dict]`，元素形如 `{"table_name", "owner", "columns"}`）、`sqlalchemy.ext.asyncio.AsyncSession`。
- Produces（Task 4/5 依赖，签名逐字）:
  - `loadDatasourceTableSet(session, datasourceId: int) -> set[str] | None`（None = 缓存缺失/空/读失败）
  - `filterClassesByDatasource(classes: list, tableSet: set[str]) -> tuple[list, list[str]]`（保留类, 被滤 source_table 原文）
  - `applyDatasourceRecallFilter(session, classes: list, *, datasourceId: int | None) -> tuple[list, dict[str, Any]]`（三态入口，dict 恒含 `datasourceFiltered`/`datasourceFilterSkipped` 两键）

- [ ] **Step 1: 写失败测试**

```python
"""recall_datasource_filter 纯函数单测（DB 路径的集成覆盖在
test_classify_and_recall.py；本文件只测无 DB 依赖的归一与三态降级）。"""

from types import SimpleNamespace

from app.services.recall_datasource_filter import (
    applyDatasourceRecallFilter,
    filterClassesByDatasource,
)


def _cls(name: str, table: str | None) -> SimpleNamespace:
    return SimpleNamespace(class_name=name, source_table=table)


def test_filter_matches_bare_and_qualified_names_case_insensitive() -> None:
    tableSet = {"DIM_SUPPLIER", "THBI.MAIN_DATA_FOO"}
    kept, dropped = filterClassesByDatasource(
        [
            _cls("A", "dim_supplier"),          # 大小写归一命中
            _cls("B", "THBI.main_data_foo"),    # owner 限定名命中
            _cls("C", "MAIN_DATA_SUPPLIER"),    # 不在目录 ⇒ 被滤
            _cls("D", None),                    # 空 source_table ⇒ 保留
        ],
        tableSet,
    )
    assert [c.class_name for c in kept] == ["A", "B", "D"]
    assert dropped == ["MAIN_DATA_SUPPLIER"]


def test_filter_returns_new_list_without_mutation() -> None:
    classes = [_cls("A", "X1"), _cls("B", "X2")]
    kept, _ = filterClassesByDatasource(classes, {"X1"})
    assert [c.class_name for c in kept] == ["A"]
    assert [c.class_name for c in classes] == ["A", "B"]  # 入参不变


async def test_apply_skips_without_datasource() -> None:
    classes = [_cls("A", "WHATEVER")]
    kept, disclosure = await applyDatasourceRecallFilter(None, classes, datasourceId=None)
    assert [c.class_name for c in kept] == ["A"]  # 不过滤
    assert disclosure == {"datasourceFiltered": 0, "datasourceFilterSkipped": "no_datasource"}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_recall_datasource_filter.py -v
```

Expected: FAIL —— `ModuleNotFoundError: app.services.recall_datasource_filter`。

- [ ] **Step 3: 最小实现**

```python
"""召回候选集的数据源过滤（2026-11-19 跨源幽灵表根治）。

设计：Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md。
ontology_class 无「类→数据源」绑定列 ⇒ 召回链全局取类，SQL 却在会话选定
数据源执行 ⇒ 登记在 A 源的物理表会在 B 源会话被召回（生产事故
main_data_supplier → ORA-00942）。本模块用 schema_cache（每数据源物理表
清单，多 owner 行）做运行时校验。三态（用户裁决）：
- 有缓存 + 有 datasourceId：硬过滤；
- datasourceId=None：不过滤（披露 no_datasource）；
- 缓存缺失/空/读失败：不过滤 fail-open（披露 no_cache）——目录缺失不把
  真实表的查询打死。
表名归一与 schema_introspection_service._validateOntologyAgainstSchema 同口径：
裸表名 + owner 限定名并集、.upper() 大小写不敏感、空 source_table 保留。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SchemaCache


def _tableKeys(schemaData: list[dict[str, Any]]) -> set[str]:
    """schema_data → 可匹配表名集合（裸名 + owner 限定名，全大写）。"""
    keys: set[str] = set()
    for table in schemaData:
        name = table.get("table_name")
        if not name:
            continue
        keys.add(name.upper())
        owner = table.get("owner")
        if owner:
            keys.add(f"{owner}.{name}".upper())
    return keys


async def loadDatasourceTableSet(
    session: AsyncSession, datasourceId: int
) -> set[str] | None:
    """当前数据源的物理表名集合（多 owner 行取并集）。

    无缓存行 / 清单为空 / 读异常 ⇒ None（调用方按 fail-open 处理）；
    读失败只 warning 不阻断主链路（对齐 _getClassFilterMaxClasses 口径）。
    """
    try:
        rows = (
            await session.execute(
                select(SchemaCache).where(SchemaCache.datasource_id == datasourceId)
            )
        ).scalars().all()
    except Exception:
        logger.warning(
            "schema_cache 读取失败，跨源过滤跳过 datasourceId=%s",
            datasourceId, exc_info=True,
        )
        return None
    keys: set[str] = set()
    for row in rows:
        keys |= _tableKeys(row.schema_data or [])
    return keys or None


def filterClassesByDatasource(
    classes: list[Any], tableSet: set[str]
) -> tuple[list[Any], list[str]]:
    """按表名集合过滤候选类，返回 (保留类, 被滤 source_table 原文列表)。

    匹配键 = source_table 最后一段的 .upper()（兼容 owner 限定名写法）；
    source_table 为空的类保留（无法判定，不误杀）。
    返回新列表，不改动入参。
    """
    kept: list[Any] = []
    dropped: list[str] = []
    for cls in classes:
        table = cls.source_table
        if not table:
            kept.append(cls)
            continue
        if table.rsplit(".", 1)[-1].upper() in tableSet:
            kept.append(cls)
        else:
            dropped.append(table)
    return kept, dropped


async def applyDatasourceRecallFilter(
    session: AsyncSession, classes: list[Any], *, datasourceId: int | None
) -> tuple[list[Any], dict[str, Any]]:
    """三态入口：返回 (过滤后的候选类, ClassRecallInfo 披露字段 dict)。

    dict 恒含 datasourceFiltered / datasourceFilterSkipped 两键，调用方用
    recallInfo.model_copy(update=...) 合并进召回诊断。
    """
    disclosure: dict[str, Any] = {
        "datasourceFiltered": 0,
        "datasourceFilterSkipped": None,
    }
    if datasourceId is None:
        disclosure["datasourceFilterSkipped"] = "no_datasource"
        return list(classes), disclosure
    tableSet = await loadDatasourceTableSet(session, datasourceId)
    if tableSet is None:
        disclosure["datasourceFilterSkipped"] = "no_cache"
        return list(classes), disclosure
    kept, dropped = filterClassesByDatasource(classes, tableSet)
    if dropped:
        logger.warning(
            "召回候选跨源过滤 dropped=%d total=%d droppedTables=%s datasourceId=%s",
            len(dropped), len(classes), dropped[:10], datasourceId,
        )
    disclosure["datasourceFiltered"] = len(dropped)
    return kept, disclosure
```

- [ ] **Step 4: 跑测试确认通过**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_recall_datasource_filter.py -v
```

Expected: 3 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/recall_datasource_filter.py backend/app/tests/unit/test_recall_datasource_filter.py
git commit -m "feat: 召回候选集数据源过滤模块（schema_cache 运行时校验，三态 fail-open）"
```

---

### Task 3: recall_keyword_anchor 模块（关键词锚定）

**Files:**
- Create: `backend/app/services/recall_keyword_anchor.py`
- Test: `backend/app/tests/unit/test_recall_keyword_anchor.py`（新建）

**Interfaces:**
- Produces（Task 6 依赖，签名逐字）: `selectAnchoredClasses(question: str, classes: list, *, maxAnchored: int) -> list`（锚定类，排序后截断，纯函数无 IO）。

- [ ] **Step 1: 写失败测试**

```python
"""recall_keyword_anchor 纯函数单测：字面包含匹配兜向量措辞敏感的底。

生产事故对照：问「公司目前有多少供应商」时向量漏 DIM_SUPPLIER（别名
「供应商主数据」），字面锚定必须把它捞回来。
"""

from types import SimpleNamespace

from app.services.recall_keyword_anchor import selectAnchoredClasses


def _cls(cid: int, name: str, alias: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=cid, class_name=name, class_alias=alias)


def test_alias_containment_anchors_missed_class() -> None:
    dim = _cls(12, "DIM_SUPPLIER", "供应商主数据")
    other = _cls(30, "SUPPLY_ACCESS", "供应商准入")
    anchored = selectAnchoredClasses("公司目前有多少供应商", [other, dim], maxAnchored=5)
    # 两个类都被「供应商」片段命中；精确/更长片段优先，id 兜底确定性
    assert [c.id for c in anchored][:2] == [12, 30]  # 供应商主数据(片段更长)在前


def test_exact_match_beats_containment() -> None:
    exact = _cls(1, "SUPPLIER", "供应商")
    partial = _cls(2, "DIM_SUPPLIER", "供应商主数据")
    anchored = selectAnchoredClasses("供应商", [partial, exact], maxAnchored=5)
    assert [c.id for c in anchored] == [1, 2]


def test_cap_limits_anchored_count() -> None:
    classes = [_cls(i, f"TABLE_{i}", f"供应商{i}号档案") for i in range(10)]
    anchored = selectAnchoredClasses("供应商", classes, maxAnchored=5)
    assert len(anchored) == 5


def test_no_match_returns_empty() -> None:
    anchored = selectAnchoredClasses(
        "完全无关的问题", [_cls(1, "DIM_SUPPLIER", "供应商主数据")], maxAnchored=5,
    )
    assert anchored == []


def test_zero_cap_or_blank_question_is_noop() -> None:
    dim = _cls(12, "DIM_SUPPLIER", "供应商主数据")
    assert selectAnchoredClasses("供应商", [dim], maxAnchored=0) == []
    assert selectAnchoredClasses("   ", [dim], maxAnchored=5) == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_recall_keyword_anchor.py -v
```

Expected: FAIL —— `ModuleNotFoundError: app.services.recall_keyword_anchor`。

- [ ] **Step 3: 最小实现**

```python
"""问句关键词/别名锚定（2026-11-19「问法敏感漏选」根治）。

设计：Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md。
向量召回对措辞敏感：「公司目前有多少供应商」的「目前」即可把 DIM_SUPPLIER
（别名「供应商主数据」）挤出 topK。字面包含关系对措辞不敏感 ⇒ 问句切 2–6 字
连续片段（n-gram，无分词器）与类名/别名做包含匹配，命中类强制纳入候选集、
排在向量命中之前（不被 MAX_CLASSES 截断挤掉）。上限 ANCHOR_MAX_CLASSES 由
调用方从 system_config 读后经 maxAnchored 传入。纯增量：不淘汰任何类。
"""

from __future__ import annotations

from typing import Any

_QUESTION_NGRAM_MIN = 2
_QUESTION_NGRAM_MAX = 6


def _questionFragments(question: str) -> list[str]:
    """问句 2–6 字连续片段（去空白；长片段先入列，供「更长优先」语义）。"""
    compact = "".join(question.split())
    fragments: list[str] = []
    seen: set[str] = set()
    for size in range(_QUESTION_NGRAM_MAX, _QUESTION_NGRAM_MIN - 1, -1):
        for start in range(0, max(0, len(compact) - size + 1)):
            frag = compact[start:start + size]
            if frag not in seen:
                seen.add(frag)
                fragments.append(frag)
    return fragments


def selectAnchoredClasses(
    question: str, classes: list[Any], *, maxAnchored: int
) -> list[Any]:
    """问句片段与类名/别名包含匹配，返回锚定类（新列表，不改动入参）。

    排序：精确等值（片段 == 类名/别名）优先 > 匹配片段更长优先 > 类 id 升序
    （确定性兜底）。同一类多片段命中取最高档计分。
    """
    if maxAnchored <= 0 or not question.strip() or not classes:
        return []
    fragments = _questionFragments(question)
    scored: list[tuple[int, int, int, Any]] = []
    for cls in classes:
        if cls.id is None:
            continue
        best: tuple[int, int] | None = None
        for target in (cls.class_alias, cls.class_name):
            if not target:
                continue
            normalized = "".join(target.split()).upper()
            for frag in fragments:
                fu = frag.upper()
                if fu == normalized:
                    candidate = (1, len(fu))
                elif fu in normalized:
                    candidate = (0, len(fu))
                else:
                    continue
                if best is None or candidate > best:
                    best = candidate
        if best is not None:
            scored.append((best[0], best[1], cls.id, cls))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored[:maxAnchored]]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
./scripts/run_local_tests.sh app/tests/unit/test_recall_keyword_anchor.py -v
```

Expected: 5 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/recall_keyword_anchor.py backend/app/tests/unit/test_recall_keyword_anchor.py
git commit -m "feat: 问句关键词/别名锚定模块（n-gram 包含匹配，纯增量兜向量漏选）"
```

---

### Task 4: 接线 chat_recall.classifyAndRecall（数据源过滤 + 披露合并）

**Files:**
- Modify: `backend/app/services/chat_recall.py`（`classifyAndRecall` 内 `allClasses` 计算，约 223-231 行）
- Test: `backend/app/tests/integration/test_classify_and_recall.py`（**追加用例，不新建文件**）

**Interfaces:**
- Consumes: Task 2 的 `applyDatasourceRecallFilter`；Task 1 的 `ClassRecallInfo` 新字段。

- [ ] **Step 1: 追加失败集成测试**

在 `test_classify_and_recall.py` 末尾追加（复用文件内既有 `_FakeOntology` / `_datasource()` helper；hit 用 `SimpleNamespace(id=..., score=...)`，与本文件首个召回用例同构）：

```python
# ---------------------------------------------------------------------------
# 2026-11-19 数据源过滤（Harness/changes/2026-11-19-recall-datasource-filter-anchor）：
# 跨源幽灵类不得进候选集；三态披露合并进 recallInfo。
# ---------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402  （文件末追加段统一置顶导入）

from app.domain.models import SchemaCache  # noqa: E402
from app.services.recall_datasource_filter import (  # noqa: E402, F401（下批接线用）
    applyDatasourceRecallFilter,
)


def _hit(cls) -> SimpleNamespace:
    return SimpleNamespace(id=cls.id, score=0.9)


@pytest.mark.asyncio
async def test_recall_filters_cross_source_class_and_discloses(self, dbSession) -> None:
    # Arrange：schema_cache 只登记 DIM_SUPPLIER；本体里另有跨源幽灵类
    ds = _datasource()
    ds.id = 7
    ds.name = "THBI"
    dbSession.add(ds)
    dbSession.add(SchemaCache(
        datasource_id=7, schema_name="THBI",
        schema_data=[{"table_name": "DIM_SUPPLIER", "owner": "THBI", "columns": []}],
        schema_version="t1",
    ))
    await dbSession.flush()
    ghost = OntologyClass(
        class_name="MAIN_DATA_SUPPLIER", source_table="main_data_supplier")
    real = OntologyClass(
        class_name="DIM_SUPPLIER", class_alias="供应商主数据", source_table="DIM_SUPPLIER")
    for c in (ghost, real):
        dbSession.add(c)
    await dbSession.flush()
    fakeOntology = _FakeOntology(hits=[_hit(real)], allClasses=[ghost, real])
    service = _recallService(fakeOntology)

    # Act
    result = await service.classifyAndRecall(
        dbSession, "公司目前有多少供应商", sessionId=None,
        needRecall=True, datasourceId=7,
    )

    # Assert：幽灵类被滤、真类保留、披露计数正确
    assert [c.class_name for c in result.classes] == ["DIM_SUPPLIER"]
    assert result.recallInfo.datasourceFiltered == 1
    assert result.recallInfo.datasourceFilterSkipped is None


@pytest.mark.asyncio
async def test_recall_fail_open_when_schema_cache_missing(self, dbSession) -> None:
    # Arrange：datasourceId 有值但无任何 schema_cache 行 ⇒ fail-open 不过滤
    ds = _datasource()
    ds.id = 9
    ds.name = "NO_CACHE"
    dbSession.add(ds)
    await dbSession.flush()
    ghost = OntologyClass(
        class_name="MAIN_DATA_SUPPLIER", source_table="main_data_supplier")
    fakeOntology = _FakeOntology(hits=[], allClasses=[ghost])
    service = _recallService(fakeOntology)

    result = await service.classifyAndRecall(
        dbSession, "公司目前有多少供应商", sessionId=None,
        needRecall=True, datasourceId=9,
    )

    assert [c.class_name for c in result.classes] == ["MAIN_DATA_SUPPLIER"]
    assert result.recallInfo.datasourceFiltered == 0
    assert result.recallInfo.datasourceFilterSkipped == "no_cache"


@pytest.mark.asyncio
async def test_recall_skips_filter_without_datasource(self, dbSession) -> None:
    ghost = OntologyClass(
        class_name="MAIN_DATA_SUPPLIER", source_table="main_data_supplier")
    fakeOntology = _FakeOntology(hits=[], allClasses=[ghost])
    service = _recallService(fakeOntology)

    result = await service.classifyAndRecall(
        dbSession, "公司目前有多少供应商", sessionId=None, needRecall=True,
    )

    assert [c.class_name for c in result.classes] == ["MAIN_DATA_SUPPLIER"]
    assert result.recallInfo.datasourceFilterSkipped == "no_datasource"
```

> 注：① `_recallService(fakeOntology)` 不是既有 helper —— 参照本文件首个召回用例（`test_need_recall_path_returns_classes_and_recall_info`）构造 `ChatService` 的方式，把「构造 service」提成文件级小 helper 供三个新用例共用；若该用例构造参数里有本用例不需要的依赖，按同款 fake 传入即可。② `ClassifyAndRecallResult` 的字段名（`classes`/`recallInfo`）以文件内既有断言为准。③ 若 `OntologyClass` 建行有非空必填列，按本文件既有 `OntologyClass` 构造处补齐同款字段。

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -k "cross_source or fail_open or skips_filter" -v
```

Expected: FAIL —— `datasourceFiltered` 恒为 0（过滤未接线，幽灵类仍在 classes 里）。

- [ ] **Step 3: 最小实现**

`chat_recall.py` 顶部 import 区追加：

```python
from app.services.recall_datasource_filter import applyDatasourceRecallFilter
```

`classifyAndRecall` 中（原 223-231 行）改为：

```python
        allClasses, dsDisclosure = await applyDatasourceRecallFilter(
            session,
            list(view.classes) if view is not None
            else await self._ontology.listClasses(session),
            datasourceId=datasourceId,
        )
        classes, recallInfo = await self._selectRelevantClasses(
            session, question, allClasses, view=view
        )
        recallInfo = recallInfo.model_copy(update=dsDisclosure)
```

（docstring 的「P0-3」段补一句：datasourceId 自 2026-11-19 起同时用于候选集跨源过滤，三态见 recall_datasource_filter 模块注释。）

- [ ] **Step 4: 跑测试确认通过（含既有用例无回归）**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -v
```

Expected: 新 3 用例 PASS + 文件既有用例全 PASS（recallInfo 加字段不影响既有断言）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_recall.py backend/app/tests/integration/test_classify_and_recall.py
git commit -m "feat: classifyAndRecall 接入跨源候选过滤并合并三态披露"
```

---

### Task 5: 接线 chat_service 第二调用点（plan/schema 链）

**Files:**
- Modify: `backend/app/services/chat_service.py`（单步 QUERY 链，`allClasses = await self._ontology.listClasses(session)` 约 996 行）
- Test: `backend/app/tests/integration/test_classify_and_recall.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `applyDatasourceRecallFilter`。

- [ ] **Step 1: 追加失败集成测试**

沿用文件内既有单步 QUERY 用例（`test_single_step_query_makes_two_llm_calls_plan_and_sql`，258 行起）的 `_PipelineLlm` / `service_saveState` 脚手架，追加：

```python
@pytest.mark.asyncio
async def test_single_step_query_schema_excludes_cross_source_class(self, dbSession) -> None:
    # Arrange：本用例在既有单步 QUERY 脚手架上加 schema_cache 与幽灵类
    # （DataSource 复用 _datasource()，id 与 saveState 的 datasourceId 一致；
    #   OntologyClass 造 MAIN_DATA_SUPPLIER 幽灵 + 既有 PRECEIPT 真类）
    #   —— 具体落点照 258 行用例 Arrange 段逐行对齐，仅多出下列三行：
    #   dbSession.add(SchemaCache(datasource_id=<同 saveState>, schema_name="ZJTH",
    #       schema_data=[{"table_name": "PRECEIPT", "owner": "ZJTH", "columns": []}],
    #       schema_version="t1"))
    #   + ghost OntologyClass(class_name="MAIN_DATA_SUPPLIER",
    #       source_table="main_data_supplier") 进 allClasses 注入处
    ...
    # Act：走同一 service.run/saveState 入口发起单步 QUERY
    ...
    # Assert：plan 阶段 LLM 收到的 schema 文本不含幽灵表、含真表
    planUser = llm.calls[0][1][1].content
    assert "MAIN_DATA_SUPPLIER" not in planUser.upper()
    assert "PRECEIPT" in planUser.upper()
```

> 注：`...` 处**不是占位**——实施者打开 258 行用例，把其 Arrange/Act 原样复制为本用例主体，再按注释加入 SchemaCache 行与 ghost 类，断言段用上面三行。llm 变量即该用例的 `_PipelineLlm` 实例。

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -k "schema_excludes_cross_source" -v
```

Expected: FAIL —— plan prompt 里仍含 `MAIN_DATA_SUPPLIER`（第二调用点未过滤）。

- [ ] **Step 3: 最小实现**

`chat_service.py` import 区追加 `from app.services.recall_datasource_filter import applyDatasourceRecallFilter`；原 996 行起改为：

```python
        allClasses, dsDisclosure = await applyDatasourceRecallFilter(
            session, await self._ontology.listClasses(session),
            datasourceId=dto.datasourceId,
        )
```

并在同方法内 `classes, recallInfo = await self._selectRelevantClasses(...)` 之后紧跟：

```python
        recallInfo = recallInfo.model_copy(update=dsDisclosure)
```

- [ ] **Step 4: 跑测试确认通过（含既有用例无回归）**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -v
```

Expected: 新用例 PASS + 既有全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_service.py backend/app/tests/integration/test_classify_and_recall.py
git commit -m "feat: 单步 QUERY 链候选集接入跨源过滤（第二调用点）"
```

---

### Task 6: 接线关键词锚定（`_selectRelevantClasses`）

**Files:**
- Modify: `backend/app/services/chat_recall.py`（`_selectRelevantClasses` 正常路径，`_rankByLayer` 之后截断之前，约 622-628 行；及 `_getAnchorMaxClasses` 新方法）
- Test: `backend/app/tests/integration/test_classify_and_recall.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `selectAnchoredClasses`；`system_config` 键 `ANCHOR_MAX_CLASSES`（缺席/非法 ⇒ 默认 5，读法照抄 `_getClassFilterMaxClasses`）。

- [ ] **Step 1: 追加失败集成测试**

```python
@pytest.mark.asyncio
async def test_anchor_forces_missed_class_into_candidates(self, dbSession) -> None:
    # Arrange：向量只命中 supply_access，DIM_SUPPLIER 漏选；
    # 问句含「供应商」⇒ 两个类的别名都包含该片段，但 DIM_SUPPLIER
    # 未在向量命中里 ⇒ 必须被锚定强制纳入且排在命中之前。
    real = OntologyClass(
        class_name="DIM_SUPPLIER", class_alias="供应商主数据", source_table="DIM_SUPPLIER")
    other = OntologyClass(
        class_name="SUPPLY_ACCESS", class_alias="供应商准入", source_table="SUPPLY_ACCESS")
    fakeOntology = _FakeOntology(hits=[_hit(other)], allClasses=[real, other])
    service = _recallService(fakeOntology)

    result = await service.classifyAndRecall(
        dbSession, "公司目前有多少供应商", sessionId=None, needRecall=True,
    )

    names = [c.class_name for c in result.classes]
    assert "DIM_SUPPLIER" in names                       # 漏选类被锚定捞回
    assert names.index("DIM_SUPPLIER") < names.index("SUPPLY_ACCESS")  # 锚定在前
    assert result.recallInfo.anchoredCount == 1          # 只计「新增」锚定
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -k "anchor_forces" -v
```

Expected: FAIL —— `DIM_SUPPLIER` 不在 classes（锚定未接线）。

- [ ] **Step 3: 最小实现**

`chat_recall.py` import 区追加 `from app.services.recall_keyword_anchor import selectAnchoredClasses`。

新增 `_getAnchorMaxClasses`（放在 `_getClassFilterMaxClasses` 之后，读法照抄该方法：`SELECT value FROM system_config WHERE key = 'ANCHOR_MAX_CLASSES'`，缺席/格式错/非正 ⇒ 返回 `_ANCHOR_MAX_CLASSES_DEFAULT = 5`，常量放文件顶部 `_CLASS_FILTER_*` 常量旁，docstring 注明「上限是锚定强纳入的唯一闸门，0/负数视为非法而非『不锚定』」）。

`_selectRelevantClasses` 正常路径，`_rankByLayer`（622-624 行）之后、截取 top K（627 行）之前插入：

```python
        # 关键词锚定（2026-11-19）：字面包含匹配兜向量措辞敏感性底。
        # 锚定类排在向量命中之前 ⇒ MAX_CLASSES 截断保锚；与命中去重，
        # anchoredCount 只计「向量没捞到、靠锚定新增」的类。
        anchorMax = await self._getAnchorMaxClasses(session)
        anchored = selectAnchoredClasses(question, allClasses, maxAnchored=anchorMax)
        rankedIds = {cls.id for cls in relevant}
        newAnchored = [cls for cls in anchored if cls.id not in rankedIds]
        anchoredCount = len(newAnchored)
        relevant = newAnchored + relevant
```

召回诊断构造处（原 640-645 行）追加字段：

```python
        recall = ClassRecallInfo(
            mode="expanded" if len(expanded) > len(relevant) else "recall",
            hitCount=len(relevant),
            classCount=len(expanded),
            truncated=truncated,
            anchoredCount=anchoredCount,
        )
```

> 范围纪律：`_fallbackRecall` **不加**锚定（spec 明确本批只在向量正常路径兜底；fallback 是纯增量缺失场景，另批再议）。

- [ ] **Step 4: 跑测试确认通过（含既有用例无回归）**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py -v
```

Expected: 新用例 PASS + 既有全 PASS（既有用例问句若意外锚入新类导致断言红，按「披露面扩大」惯例逐条核对是锚定语义正确后更新断言，并在 commit 正文列明改动原因）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_recall.py backend/app/tests/integration/test_classify_and_recall.py
git commit -m "feat: 类召回接入关键词锚定（锚定类前置保截断，anchoredCount 披露）"
```

---

### Task 7: 前端披露（类型 + 提示 + i18n + 测试）

**Files:**
- Modify: `frontend/src/types/chat.ts`（`interface ClassRecallInfo`，282 行）
- Modify: `frontend/src/components/chat/MessageItem.tsx`（类召回 Alert 区，约 160-172 行）
- Modify: `frontend/src/i18n/zh-CN.ts`（429 行区）/ `frontend/src/i18n/en-US.ts`（424 行区）
- Test: `frontend/src/tests/MessageItem.test.tsx`（追加）

**Interfaces:**
- Consumes: Task 1 的 wire 字段 `datasourceFiltered` / `datasourceFilterSkipped` / `anchoredCount`。

- [ ] **Step 1: 追加失败渲染测试**

在 `MessageItem.test.tsx` 追加（沿用文件内既有 message/classRecall 造数方式）：

```tsx
it("renders datasource filter disclosure when tables were filtered", () => {
  // message 造数照本文件既有 classRecall 用例，classRecall 改为：
  // { mode: "recall", hitCount: 1, classCount: 1, truncated: false,
  //   datasourceFiltered: 3 }
  // 断言屏幕出现「已按当前数据源过滤 3 张跨源数据表」
});

it("renders skip disclosure when filter was skipped", () => {
  // classRecall: { ..., datasourceFilterSkipped: "no_cache" }
  // 断言屏幕出现「未选择数据源或数据源目录缺失」
});

it("renders anchored count disclosure", () => {
  // classRecall: { ..., anchoredCount: 2 }
  // 断言屏幕出现「已按关键词锚定 2 张数据表」
});
```

> 注：注释不是占位——实施者把注释所述 classRecall 造数与断言写成真实代码，断言文案与 Step 3 的 i18n key 逐字一致；造数结构照本文件既有 classRecall 测试。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run src/tests/MessageItem.test.tsx
```

Expected: 新 3 用例 FAIL（提示未渲染）。若本仓前端测试入口不是 vitest，看 `frontend/package.json` scripts 用实际命令（**勿用记忆里的旧命令**，以 package.json 为准）。

- [ ] **Step 3: 实现**

`frontend/src/types/chat.ts` 的 `ClassRecallInfo` 追加：

```ts
export interface ClassRecallInfo {
  mode: "recall" | "expanded" | "fallback";
  hitCount: number;
  classCount: number;
  truncated: boolean;
  datasourceFiltered?: number;
  datasourceFilterSkipped?: string | null;
  anchoredCount?: number;
}
```

`MessageItem.tsx` 类召回 Alert 之后（同 `marginTop` 分支内）追加一个 info 级 Alert：

```tsx
{message.classRecall && ((message.classRecall.datasourceFiltered ?? 0) > 0
  || message.classRecall.datasourceFilterSkipped
  || (message.classRecall.anchoredCount ?? 0) > 0) ? (
  <Alert
    type="info"
    showIcon
    style={{ marginBottom: 8 }}
    message={
      (message.classRecall.datasourceFiltered ?? 0) > 0
        ? t("messageItem.classRecallDatasourceFiltered", {
            total: message.classRecall.datasourceFiltered,
          })
        : message.classRecall.datasourceFilterSkipped
          ? t("messageItem.classRecallDatasourceSkipped")
          : t("messageItem.classRecallAnchored", {
              total: message.classRecall.anchoredCount,
            })
    }
  />
) : null}
```

i18n（两文件 `messageItem` 段，紧跟 `classRecallFallback`）：

```ts
// zh-CN.ts
classRecallDatasourceFiltered: "已按当前数据源过滤 {{total}} 张跨源数据表。",
classRecallDatasourceSkipped: "未选择数据源或数据源目录缺失，跨源过滤本次未生效。",
classRecallAnchored: "已按关键词锚定 {{total}} 张数据表。",
// en-US.ts
classRecallDatasourceFiltered: "Filtered {{total}} cross-source tables for the current datasource.",
classRecallDatasourceSkipped: "No datasource selected or datasource catalog missing; cross-source filtering was not applied this turn.",
classRecallAnchored: "Anchored {{total}} tables by keywords.",
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npx vitest run src/tests/MessageItem.test.tsx
```

Expected: 新 3 用例 PASS + 既有 PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/chat.ts frontend/src/components/chat/MessageItem.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/MessageItem.test.tsx
git commit -m "feat: 前端类召回披露跨源过滤/锚定提示（i18n zh+en）"
```

---

### Task 8: 定向回归 + 文档回填

**Files:**
- Modify: `Harness/wiki/nl2sql-engine.md`（「类召回窗口与规模化风险」节）
- Modify: `Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md`（状态行）
- Create: `Harness/changes/2026-11-19-recall-datasource-filter-anchor/summary.md`

- [ ] **Step 1: 后端定向宽回归**

```bash
./scripts/run_local_tests.sh app/tests/integration/test_classify_and_recall.py app/tests/integration/test_chat_pipeline_layer.py app/tests/integration/test_multistep_global_filter.py -v
./scripts/run_local_tests.sh app/tests/unit/test_recall_datasource_filter.py app/tests/unit/test_recall_keyword_anchor.py app/tests/unit/test_class_recall_info_contract.py -v
```

Expected: 全 PASS 或失败集合对 main 基线**净新增 0**（红基线三角定位法；有净新增必须修复后重跑）。

- [ ] **Step 2: 前端定向回归**

```bash
cd frontend && npx vitest run src/tests/MessageItem.test.tsx
```

- [ ] **Step 3: 文档回填**

1. `Harness/wiki/nl2sql-engine.md`「类召回窗口与规模化风险」节追加一段：召回链新增「数据源过滤（schema_cache 运行时校验，三态 fail-open）→ 关键词锚定（n-gram 包含匹配，ANCHOR_MAX_CLASSES 上限）」两级前置/兜底，披露字段 `datasourceFiltered`/`datasourceFilterSkipped`/`anchoredCount`；链接 design.md。
2. `design.md` 状态行改为「已实施（分支 fix/recall-datasource-anchor）」。
3. 新建 `summary.md`：实施结果（任务/提交清单、测试计数、净新增 0 证据）、生产部署仍需后端镜像发版、墓碑 id 270 复活裁决项（过滤上线验证后原地 `valid_to = null`）。

- [ ] **Step 4: Commit**

```bash
git add Harness/wiki/nl2sql-engine.md Harness/changes/2026-11-19-recall-datasource-filter-anchor/
git commit -m "docs: 召回数据源过滤+锚定实施回填（wiki 类召回窗口节 + 批次 summary）"
```

- [ ] **Step 5: 汇报**

向用户汇报：分支/提交清单、测试证据、净新增 0 结论；请示是否发 PR 到 main 与生产镜像发版时机（发版 runbook 另出）。
