# Research Report R4：让显示名翻译在真实数据上真正生效

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把研究报告里「列名/值是英文 code」这个用户可见缺口从「代码已写但真实数据上完全无效」修成「真实数据上逐字出现中文」。

**Architecture:** R3 建了两层翻译（`resolveValueName` 值字典 / `resolveColumnName` 本体别名），但两层的输入 `sourceTable` 从未被写入 `finding.supporting_data`，且本体 `property_alias` 在真实库中 3699 行里只有 3 行有值。R4 从**读侧**补齐：用 `supporting_sql` 的 `FROM` 子句派生来源表（纯函数、可回溯修复历史会话），给本体别名之外补一层代码内列名字典，并把大小写归一化；再把翻译接到 data 段、表格段、LLM 摘要与图表的标签上。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy async（asyncpg）+ PostgreSQL 15 / pytest（真实 PG）/ React 18 + ECharts（本计划不改前端）

## Global Constraints

- **不可变数据**：`FindingView` / `FindingStats` 等 frozen dataclass 一律构造新对象，禁止原地改字段。
- **TDD 强制**：每个 task 先写失败测试（RED）→ 最小实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%。
- **真实数据库测试**：后端测试用真实 PostgreSQL + 完整链路，禁 sqlite 内存库。命令必须带 `TEST_DATABASE_URL` **和** `TEST_NEO4J_URI`（缺后者 fail-fast）。
- **小文件**：200–400 行为宜，硬上限 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- **命名**：函数/变量 `camelCase`，类型 `PascalCase`，常量 `UPPER_SNAKE_CASE`；ORM/Pydantic 字段名 `snake_case`。测试函数名 `snake_case`（与既有测试一致）。
- **数字免疫契约**：finding 的原始行数据**永不进 LLM prompt**；prompt 只收摘要字符串。R4 只把摘要里的**标签**中文化，不改这条。
- **不改** chat 侧任何文件；不改 A1+A2 方言守卫；不改 R1 schema reflection；不动 `raw/`。
- **不改** 前端（R3-B 已把 ECharts 渲染接好；R4 只让服务端 `echartsOption` 里的标签变中文，前端契约不变）。
- **不做 alembic 迁移**：字典先放代码内（与 R3 决策一致）。
- 测试环境：`TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test`、`TEST_NEO4J_URI=bolt://localhost:7688`。

---

## Context：为什么 R4-1 不是「接一根线」那么简单

R3 交付后，对**真实 e2e 会话**（dev 库 `qa_metadata`，session `4a45e267-179d-472b-84b1-985012ceaa5f`）做了核查，四条实测事实决定了 R4 的形态：

| # | 事实 | 验证方式 |
|---|---|---|
| F1 | `finding.supporting_data` 的键**只有** `rows` / `error` / `stats` / `rowCount`，**没有 `sourceTable`** | `select jsonb_object_keys(supporting_data) from research_finding where session_id='4a45e267-…'` |
| F2 | 但 `finding.supporting_sql` 里**有**表名：`… FROM DWD_RECONCILIATION_BILL_DTL GROUP BY …`，与 `ontology_class.source_table` 逐字同大小写 | 同表 `left(supporting_sql,300)` |
| F3 | `ontology_property.property_alias` 只有 **3 / 3699** 行有值；`ontology_class.class_alias` **6 / 53**；`business_aliases` **0 / 3699**；`property_name` 无一行含中文 | `count(*) filter (where …)` 三条聚合 |
| F4 | 行数据键是**小写**（`site_code`），SQL 列与本体 `source_column` 是**大写**（`SITE_CODE`）；`DISPLAY_VALUE_DICT` 的表键用的是**小写** | 对比 `supporting_data->'rows'` 与 `information_schema` |

**推论（这就是 R4 的全部动机）：**

- R3 的 `_formatStatsBullet` 只在 `sourceTable` 非空时才翻译值 ⇒ `resolveValueName(None, …)` 恒回原值 ⇒ **值翻译一条都没生效**（F1）。
- R3 的 `resolveColumnName` **零调用点**（只定义了没接线）⇒ 列名翻译从未运行。
- 即便接上线，本体别名层在真实库也几乎无数据（F3），所以必须补一层代码内字典。
- 大小写不归一 ⇒ 即使 `sourceTable` 写进来，dict 也用表键查不中（F4）。

**因此 R4-1 的真实形态 = 派生 sourceTable（读侧）+ 代码内列名字典 + 大小写归一 + 接线到四处表面。**

## Open Decision（已定，如需改请告知）

| 决策 | 采纳 | 理由 / 备选 |
|---|---|---|
| 中文列名从哪来 | **代码内 `DISPLAY_COLUMN_DICT`**（本轮）+ 本体别名层保留为主层 | 与 R3 的 `DISPLAY_VALUE_DICT` 同款、零迁移；备选「播种 `ontology_property.property_alias`」是**数据治理任务**（3699 行），代码无需改即会自动生效，列为后续项 |
| `sourceTable` 写侧还是读侧补 | **读侧**从 `supporting_sql` 派生 | 唯一能修复**历史会话**（F2 的 SQL 已在库）的方式；不碰 research 状态机 / checkpoint 恢复逻辑 |
| 派生要不要查本体白名单 | **不查**，纯函数取第一个 `FROM`/`JOIN` | 下游 dict/DB 查询都是精确匹配，误判（如 CTE 名）自然落空 → 降级原样，无副作用 |
| 值是聚合别名（`bill_cnt`/`amt`）怎么办 | 一并进 `DISPLAY_COLUMN_DICT` | 它们是 LLM 的 `COUNT(*) AS bill_cnt`，本体里没有，但字典按 `(表, 列)` 精确匹配即可命中 |
| 图表 stats hint（R3-B1 遗留） | **不做**（YAGNI） | 决策引擎已从数据形状+问句派生；只有真机回归显示选错图型时才加 |
| `report_planner.py` 790 行 | **先拆出 `report_renderer.py`**（Task 1） | Tasks 2–4 预计新增 ~110 行，不拆会越 800 行硬上限 |

---

## File Structure

| 文件 | 职责 | 本计划动作 |
|---|---|---|
| `backend/app/services/report_renderer.py` | 报告**表示层**：`ReportBlock`/`ReportSection`/`SourceRef` 数据类 + payload→dict 序列化 + MD 渲染 | **新建**（自 `report_planner.py` 移出 ~130 行） |
| `backend/app/services/report_planner.py` | 报告**装配**：取数、标签、LLM 文本、章节编排 | 缩到 ~660 行后接收 Tasks 2–4 的新增 |
| `backend/app/services/display_name_resolver.py` | 显示名解析：`deriveSourceTable`（新，纯函数）+ 列名字典（新）+ 值字典（归一化） | 改造 |
| `backend/app/services/chart_integration.py` | 报告侧 chart 构建（复用 chat 链路） | 加 `aliasOverrides` 参数 |
| `backend/app/services/chart_spec_builder.py` | `buildSpec`：图表结构 → `ChartSpec` | 加可选 `aliasOverrides`（默认 `None` ⇒ 行为不变，chat 侧零影响） |
| `backend/app/tests/unit/test_display_name_resolver.py` | 解析服务单测（真实 PG） | 追加 `deriveSourceTable` + 归一化用例 |
| `backend/app/tests/unit/test_report_planner.py` | 报告装配单测（真实 PG） | 追加列名中文化 / 图表 alias 用例 + 1 fixture |
| `backend/app/tests/unit/test_chart_spec_builder.py`（若不存在则新建） | `buildSpec` 单测 | 追加 `aliasOverrides` 用例 |

---

### Task 1: 拆出 `report_renderer.py`（纯搬迁，为后续腾空间）

**理由**：`report_planner.py` 现 790 行，Tasks 2–4 会再加 ~110 行，越 800 硬上限。本 task 是**纯搬迁**——零行为变化，测试是唯一判据。先做，后续 task 的改动才落在有空间的文件里。

**Files:**
- Create: `backend/app/services/report_renderer.py`
- Modify: `backend/app/services/report_planner.py`（删除被移走的定义；`renderMarkdown` 改为 re-export）
- Test: 既有 `backend/app/tests/unit/test_report_planner.py`（不改，作为搬迁的回归网）

**Interfaces:**
- Consumes: 无
- Produces（`report_renderer` 的公开接口，后续 task 与外部只依赖这些）:
  - `class SourceRef` / `class ReportBlock` / `class ReportSection`（frozen dataclass，字段与现在逐字一致）
  - `blockToDict(block) -> dict[str, Any]`、`sectionToDict(section) -> dict[str, Any]`
  - `renderMarkdown(payload: dict[str, Any]) -> str`
  - 常量：`BLOCK_TEXT` / `BLOCK_BULLET` / `BLOCK_TABLE` / `BLOCK_CHART`、`REF_WIKI` / `REF_FINDING`、`CHART_HINT`、`DEFAULT_TITLE`、`_SECTION_TITLES`（`report_planner` 从本模块 import 回来）

- [ ] **Step 1: 记录搬迁前基线（不改代码，先跑绿）**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_report_planner.py app/tests/unit/test_display_name_resolver.py -q
```
Expected: `25 passed`（已实测确认；这是搬迁回归的对照基线）。

- [ ] **Step 2: 建 `report_renderer.py`，把表示层整体移入**

新建文件，内容为**逐字搬运**（改的只有名字前缀下划线去掉 + 模块 docstring）：

```python
"""报告表示层：块/章节数据类 + payload 序列化 + MD 渲染（纯字符串模板，零 LLM）。

自 `report_planner.py` 拆出（R4 Task 1）：装配（取数 / 标签 / LLM 文本）留在
`report_planner`，本模块只负责**把已装配好的 payload 渲染成人和前端都能读的形态**。
搬迁是纯移动，零行为变化 —— `report_planner` 以 re-export 保持既有导入路径可用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCK_BULLET",
    "BLOCK_CHART",
    "BLOCK_TABLE",
    "BLOCK_TEXT",
    "CHART_HINT",
    "DEFAULT_TITLE",
    "REF_FINDING",
    "REF_WIKI",
    "ReportBlock",
    "ReportSection",
    "SourceRef",
    "blockToDict",
    "renderMarkdown",
    "sectionToDict",
]

# --- 常量：从 report_planner 原样搬入（值不改） -----------------------------
# BLOCK_TEXT / BLOCK_BULLET / BLOCK_TABLE / BLOCK_CHART
# REF_WIKI / REF_FINDING
# CHART_HINT / DEFAULT_TITLE / _SECTION_TITLES（后者仍是私有名，planner 侧 import 使用）
# 以上常量的**字面值请从 report_planner.py 第 79–100 行原样复制**，勿重打。
```

> 实现者注意：常量块的字面值必须以 `report_planner.py` 现有文本为准原样搬运，不要凭记忆重写。`_SECTION_TITLES` 保持同名（带下划线），`report_planner` 用 `from app.services.report_renderer import _SECTION_TITLES` 取回。

- [ ] **Step 3: 迁入数据类与序列化**

把 `SourceRef` / `ReportBlock` / `ReportSection` 三个 dataclass（现 `report_planner.py:130-155`）与 `_blockToDict` / `_sectionToDict`（现 `193-209`）搬入新文件，函数去下划线改名 `blockToDict` / `sectionToDict`（模块私有属性消失，公开化更诚实）。字段、默认值、`__all__` 之外的私有 helper 引用全部保持。

- [ ] **Step 4: 迁入 MD 渲染**

把 `_cell` → `escapeCell`、`_mdTable` → `mdTable`、`_renderBlock` → `renderBlock`、`renderMarkdown`（现 `401-455`）搬入并改名（`renderMarkdown` 保持公开名）。`renderMarkdown` 内对 `_renderBlock` 的调用改为 `renderBlock`。

- [ ] **Step 5: `report_planner.py` 改为 re-export，删除已迁走的定义**

在 `report_planner.py` 顶部把 `chart_integration` 导入附近的 import 块换成：

```python
from app.services.report_renderer import (
    BLOCK_BULLET,
    BLOCK_CHART,
    BLOCK_TABLE,
    BLOCK_TEXT,
    CHART_HINT,
    DEFAULT_TITLE,
    REF_FINDING,
    REF_WIKI,
    ReportBlock,
    ReportSection,
    SourceRef,
    _SECTION_TITLES,
    blockToDict,
    renderMarkdown,
    sectionToDict,
)
```

并把 `__all__` 保持为 `["ReportBlock", "ReportPlanner", "ReportSection", "SourceRef", "renderMarkdown"]`（对外契约不变）。随后**删除**已迁走的那些定义。

- [ ] **Step 6: 全量回归（搬迁零行为变化的判据）**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_report_planner.py app/tests/unit/test_display_name_resolver.py -q
```
Expected: `25 passed`（与 Step 1 逐字相同）。

- [ ] **Step 7: 确认行数下降**

Run: `wc -l backend/app/services/report_planner.py backend/app/services/report_renderer.py`
Expected: `report_planner.py` ≤ 680；`report_renderer.py` ≤ 160；两个文件都 < 800。

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/report_planner.py backend/app/services/report_renderer.py
git commit -m "refactor(research): [R4-1] 拆出 report_renderer 表示层，report_planner 腾出空间"
```

---

### Task 2: 列名/值翻译真正落地（派生 sourceTable + 代码内列名字典 + 归一化 + 接线）

这是 R4 的**核心交付**：真实数据上，data 段的 stats bullet 与表格段的表头/贡献者列出现中文。

**Files:**
- Modify: `backend/app/services/display_name_resolver.py`
- Modify: `backend/app/services/report_planner.py`
- Test: `backend/app/tests/unit/test_display_name_resolver.py`, `backend/app/tests/unit/test_report_planner.py`

**Interfaces:**
- Consumes: 无（Task 1 只搬到 `report_renderer`，本 task 未触及表示层）
- Produces（后续 task 依赖）:
  - `deriveSourceTable(sql: str | None) -> str | None`（纯函数，返**大写**表名或 `None`）
  - `DISPLAY_COLUMN_DICT: dict[tuple[str, str], str]`（键 `(大写表名, 大写列名)`）
  - `resolveColumnName(db, sourceTable, sourceColumn) -> str`（签名不变；`db=None` 时跳过本体层）
  - `resolveValueName(sourceTable, sourceColumn, value) -> str`（签名不变；查表前把表/列归一为大写）
  - `report_planner._findingView` 的 `sourceTable` 现在会由 SQL 派生（字段已存在，不改类型）
  - `report_planner._formatStatsBullet(stats, *, sourceTable=None, db=None) -> list[str]` 变 **async**

- [ ] **Step 1: 写失败测试 —— `deriveSourceTable`（纯函数，无需 DB）**

追加到 `backend/app/tests/unit/test_display_name_resolver.py`：

```python
# --- R4 Task 2：从支撑 SQL 派生来源表 ----------------------------------------


def test_deriveSourceTable_reads_first_from() -> None:
    """最简 FROM → 表名大写返回."""
    sql = "SELECT SITE_CODE, COUNT(*) AS bill_cnt FROM DWD_RECONCILIATION_BILL_DTL GROUP BY SITE_CODE"
    assert deriveSourceTable(sql) == "DWD_RECONCILIATION_BILL_DTL"


def test_deriveSourceTable_strips_schema_prefix_and_quotes() -> None:
    """schema 前缀 + 双引号（Oracle/ANSI 引号标识符）都要剥掉."""
    assert deriveSourceTable('SELECT a FROM "DW"."DWD_X"') == "DWD_X"
    assert deriveSourceTable("select a from dw.dwd_x") == "DWD_X"


def test_deriveSourceTable_reads_inside_cte_body() -> None:
    """WITH 里的 FROM 出现在外层 FROM 之前 ⇒ 取到的是**真实业务表**，不是 CTE 名."""
    sql = "WITH monthly AS (SELECT PERIOD_MONTH, SUM(AMT) FROM DWD_RECONCILIATION_BILL_DTL GROUP BY 1) SELECT * FROM monthly"
    assert deriveSourceTable(sql) == "DWD_RECONCILIATION_BILL_DTL"


def test_deriveSourceTable_returns_none_without_from() -> None:
    """无 FROM / 空串 / None 一律 None（翻译整体降级为原样）."""
    assert deriveSourceTable("SELECT 1") is None
    assert deriveSourceTable("") is None
    assert deriveSourceTable(None) is None
```

在同文件的 import 块加入 `deriveSourceTable`：

```python
from app.services.display_name_resolver import (
    DISPLAY_COLUMN_DICT,
    DISPLAY_VALUE_DICT,
    deriveSourceTable,
    resolveColumnName,
    resolveValueName,
)
```

- [ ] **Step 2: 跑测试确认 RED**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_display_name_resolver.py -q
```
Expected: FAIL —— `ImportError: cannot import name 'deriveSourceTable'`。

- [ ] **Step 3: 写失败测试 —— 列名字典 + 大小写归一**

追加到同一文件：

```python
@pytest.mark.asyncio
async def test_resolveColumnName_uses_code_dict_when_ontology_has_no_alias(
    db_session: AsyncSession,
) -> None:
    """本体无 alias（真实库常态：3699 行里仅 3 行有）⇒ 落到代码内列名字典."""
    # 该表在测试库无 OntologyClass 行 ⇒ 本体层必然落空
    assert (
        await resolveColumnName(db_session, "DWD_RECONCILIATION_BILL_DTL", "SITE_CODE")
        == "基地"
    )


@pytest.mark.asyncio
async def test_resolveColumnName_normalizes_case(db_session: AsyncSession) -> None:
    """行数据键是小写、本体列是大写 ⇒ 大小写归一后仍命中字典."""
    assert (
        await resolveColumnName(db_session, "dwd_reconciliation_bill_dtl", "site_code")
        == "基地"
    )


@pytest.mark.asyncio
async def test_resolveColumnName_ontology_alias_beats_code_dict(
    db_session: AsyncSession,
) -> None:
    """本体 alias 是主层：有值时**压过**代码内字典."""
    cls = OntologyClass(class_name="dwd_x", source_table="dwd_x")
    db_session.add(cls)
    await db_session.flush()
    db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="site_code",
            data_type="string",
            source_column="SITE_CODE",
            property_alias="本体基地名",
        )
    )
    await db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="site_code_dup",
            data_type="string",
            source_column="SITE_CODE",
            property_alias="本体基地名",
        )
    )
    await db_session.commit()
    assert await resolveColumnName(db_session, "DWD_X", "site_code") == "本体基地名"


@pytest.mark.asyncio
async def test_resolveColumnName_works_without_db() -> None:
    """db=None（旧夹具直接调 / 降级路径）⇒ 只走代码内字典，不抛."""
    assert await resolveColumnName(None, "DWD_RECONCILIATION_BILL_DTL", "SITE_CODE") == "基地"
    assert await resolveColumnName(None, "DWD_RECONCILIATION_BILL_DTL", "NO_SUCH") == "NO_SUCH"


def test_resolveValueName_normalizes_table_case() -> None:
    """值字典查表前归一为大写（R3 的 dict 键原是小写 ⇒ F4 的 bug）."""
    assert resolveValueName("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "QTY") == "数量确认"
    assert resolveValueName("dwd_reconciliation_bill_dtl", "confirm_stage", "QTY") == "数量确认"
```

- [ ] **Step 4: 跑测试确认 RED**

Run: 同 Step 2 的命令。
Expected: FAIL —— 上述新增用例失败（字典不存在 / 大小写查不中 / `db=None` 抛 `AttributeError`）。

- [ ] **Step 5: 实现 `display_name_resolver.py`**

把文件改为（保留模块 docstring，更新「数据源」段）：

```python
"""显示名解析：列名/值 → 中文标签。

数据源（列名，按优先级）：
1. OntologyProperty.property_alias（DB 查；真实库填充率低，但一旦治理即自动生效）
2. DISPLAY_COLUMN_DICT 代码内字典（键 = (大写表名, 大写列名)）
3. 原列名

数据源（值）：DISPLAY_VALUE_DICT 代码内字典（键 = (大写表名, 大写列名, 原值)）。

失败全部降级：DB 不可达 / property 缺失 / 不在 dict → 返回原 column/value。

派生：`deriveSourceTable` 从 finding 的支撑 SQL 的 FROM/JOIN 子句派生来源表 ——
R3 从未把 sourceTable 写进 supporting_data（实测：真实会话的 supporting_data 只有
rows/error/stats/rowCount），故列名翻译在真实数据上完全无效；读侧派生是唯一能同时
修复历史会话的方式。

大小写：行数据键是小写（site_code），本体列/字典键是大写（SITE_CODE）——查表前
一律 `.upper()` 归一。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from sqlalchemy import select

from app.domain.models import OntologyClass, OntologyProperty

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_COLUMN_DICT",
    "DISPLAY_VALUE_DICT",
    "deriveSourceTable",
    "resolveColumnName",
    "resolveValueName",
]

# (大写 source_table, 大写 source_column) → 中文列名
# 初始集 = R2 真机 e2e 报告里出现的全部结果集列名（含 LLM 的聚合别名 bill_cnt/amt）。
DISPLAY_COLUMN_DICT: Final[dict[tuple[str, str], str]] = {
    ("DWD_RECONCILIATION_BILL_DTL", "SITE_CODE"): "基地",
    ("DWD_RECONCILIATION_BILL_DTL", "PERIOD_MONTH"): "月份",
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE"): "确认阶段",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE"): "账单状态",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG"): "供应商标记",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_CODE"): "供应商编码",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_NAME"): "供应商名称",
    ("DWD_RECONCILIATION_BILL_DTL", "TOTAL_AMOUNT"): "含税金额",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_CNT"): "账单数",
    ("DWD_RECONCILIATION_BILL_DTL", "AMT"): "金额",
    ("DWD_RECONCILIATION_BILL_DTL", "PAYABLE_AMOUNT"): "应付金额",
}

# (大写 source_table, 大写 source_column, raw_value) → 中文值
DISPLAY_VALUE_DICT: Final[dict[tuple[str, str, str], str]] = {
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "QTY"): "数量确认",
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "PRICE"): "价格确认",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE", "20"): "已确认",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE", "10"): "未确认",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG", "UNCONFIRMED"): "未确认供应商",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG", "CONFIRMED"): "已确认供应商",
    # 基地值（C1 / T1 / T7 …）需 site 主数据才能译名，暂不做 —— 列名「基地」已让
    # 「基地=C1」可读。
}

# FROM / JOIN 后的标识符：可选 schema 前缀、可带 ANSI 双引号。
_TABLE_REF_RE: Final = re.compile(
    r"\b(?:FROM|JOIN)\s+("
    r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
    r'(?:\.(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*))*'
    r")",
    re.IGNORECASE,
)


def deriveSourceTable(sql: str | None) -> str | None:
    """从支撑 SQL 派生来源表名（大写）。

    只取**第一个** FROM/JOIN 的标识符：报告翻译不做多表消歧，而事实表在 LLM 生成的
    聚合 SQL 里总是首个 FROM（CTE 体内也算 —— 那正是真实业务表）。取不到 → None，
    调用方据此把翻译整体降级为原样（与 R3 行为一致，不会更糟）。

    刻意**不查本体白名单**：下游 dict / DB 查询都是精确匹配，误判（如 CTE 名）自然
    落空 → 降级原样，无副作用；而查库会让本函数从纯函数退化。
    """
    if not sql:
        return None
    match = _TABLE_REF_RE.search(sql)
    if match is None:
        return None
    ident = match.group(1).split(".")[-1]
    ident = ident.strip().strip('"').strip()
    return ident.upper() or None


async def resolveColumnName(
    db: Any,
    sourceTable: str | None,
    sourceColumn: str,
) -> str:
    """列名 → 显示名。

    本体 `property_alias` 优先；落空则查 `DISPLAY_COLUMN_DICT`；再落空返原列名。
    `db=None`（降级路径）跳过本体层。DB 异常只 warning，不影响主流程。
    """
    if not sourceColumn:
        return sourceColumn
    table = (sourceTable or "").upper()
    column = str(sourceColumn).upper()
    if db is not None and table:
        try:
            result = await db.execute(
                select(OntologyProperty.property_alias)
                .join(OntologyClass, OntologyProperty.class_id == OntologyClass.id)
                .where(OntologyClass.source_table == table)
                .where(OntologyProperty.source_column == column)
                .limit(1)
            )
            alias = result.scalar_one_or_none()
            if alias:
                return str(alias)
        except Exception as exc:  # noqa: BLE001 —— 翻译只是显示装饰，读失败不该炸报告
            log.warning("resolveColumnName 本体别名查询失败 %s.%s: %s", table, column, exc)
    return DISPLAY_COLUMN_DICT.get((table, column), sourceColumn)


def resolveValueName(
    sourceTable: str | None,
    sourceColumn: str | None,
    value: str | int | float | None,
) -> str:
    """值 → 显示名。值不在 dict → 返回原 str(value)。None → '—'。

    表/列归一为大写后查表；**值本身不做大小写归一**（值是数据，大小写敏感）。
    """
    if value is None:
        return "—"
    raw = str(value)
    if sourceTable and sourceColumn:
        key = (sourceTable.upper(), sourceColumn.upper(), raw)
        if key in DISPLAY_VALUE_DICT:
            return DISPLAY_VALUE_DICT[key]
    return raw
```

- [ ] **Step 6: 跑测试确认 GREEN**

Run: 同 Step 2 的命令。
Expected: PASS（既有 5 个用例 + 新增 8 个）。

- [ ] **Step 7: 写失败测试 —— 报告 data 段/表格段中文化**

追加到 `backend/app/tests/unit/test_report_planner.py`。先加 fixture（**只写 SQL 不写 sourceTable**，复刻真实数据形态 F1+F2）：

```python
@pytest.fixture
async def fakeReportDepsWithSqlOnly(db_session):
    """复刻真实数据的 finding：supporting_data **无 sourceTable**，表名只在 SQL 里.

    这是 R4 的核心场景 —— R3 的翻译层在这个形态下完全无效（F1）。
    """
    svc = ResearchSessionService()
    s = await svc.createSession(db_session, userId=1, question="7 月对账确认情况", mode="research")
    turn = await svc.appendTurn(db_session, sessionId=s.id, role="user", content={"q": "x"})
    await svc.saveFinding(
        db_session,
        sessionId=s.id,
        turnId=turn.id,
        claimText="7 月对账确认情况",
        supportingSql=(
            "SELECT SITE_CODE, CONFIRM_STAGE, COUNT(*) AS bill_cnt "
            "FROM DWD_RECONCILIATION_BILL_DTL GROUP BY SITE_CODE, CONFIRM_STAGE"
        ),
        supportingData={
            "rowCount": 2,
            "error": None,
            "rows": [
                {"site_code": "C1", "confirm_stage": "QTY", "bill_cnt": 10},
                {"site_code": "C1", "confirm_stage": "PRICE", "bill_cnt": 5},
            ],
            "stats": {
                "topContributors": [{"dim": "site_code", "value": "C1", "count": 2, "sharePct": 100.0}],
                "distribution": {"bill_cnt": {"min": 5.0, "max": 10.0, "mean": 7.5, "std": 3.5}},
                "mom": [],
                "anomalies": [],
                "numericColumns": ["bill_cnt"],
                "dimensionColumns": ["site_code", "confirm_stage"],
                "rowCount": 2,
            },
        },
        confidence=0.9,
    )
    await db_session.commit()
    fake = _FakeLlm()
    planner = ReportPlanner(sessionFactory=lambda: None, llmClientFactory=lambda: fake)
    return planner, s.id, turn.id
```

> 实现者注意：`_FakeLlm` / `ReportPlanner(...)` 的构造与既有 fixture 保持同一写法（照抄 `fakeReportDepsWithStats` 的构造段，勿臆造参数名）。

```python
@pytest.mark.asyncio
async def test_stats_bullet_translates_column_names(fakeReportDepsWithSqlOnly) -> None:
    """列名中文化：site_code → 基地，bill_cnt → 账单数（走 SQL 派生的 sourceTable）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    bullet = next(b for b in data["blocks"] if b["type"] == "bullet_list"
                  and any("维度 topN" in item for item in b["content"]))
    joined = "\n".join(bullet["content"])
    assert "基地=C1" in joined          # dim 译中文 + 值原样（C1 无值字典）
    assert "site_code" not in joined    # 原始列名不再出现
    assert "bill_cnt" not in joined     # 数值分布列名也译中文


@pytest.mark.asyncio
async def test_stats_bullet_translates_values(fakeReportDepsWithSqlOnly) -> None:
    """值中文化：confirm_stage 的 QTY/PRICE → 数量确认/价格确认（R3 值字典在真实数据上是死的）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    bullet = next(b for b in data["blocks"] if b["type"] == "bullet_list"
                  and any("维度 topN" in item for item in b["content"]))
    joined = "\n".join(bullet["content"])
    assert "数量确认" in joined or "价格确认" in joined
```

- [ ] **Step 8: 跑测试确认 RED**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_report_planner.py -q
```
Expected: FAIL —— `site_code` 仍原样出现在 bullet 里。

- [ ] **Step 9: 接线 `report_planner.py`**

(a) import 块补 `deriveSourceTable` 与 `resolveColumnName`：

```python
from app.services.display_name_resolver import (
    deriveSourceTable,
    resolveColumnName,
    resolveValueName,
)
```

(b) `_findingView` 里 `sourceTable` 改为「先取持久化值，缺失则由 SQL 派生」：

```python
        # R4：R3 从未写入 supporting_data["sourceTable"]（真实会话实测只有
        # rows/error/stats/rowCount），故缺失时从支撑 SQL 的 FROM 子句派生 ——
        # 这是唯一能同时修复历史会话与新增会话的读侧方案。
        sourceTable=data.get("sourceTable") or deriveSourceTable(row.supporting_sql),
```

(c) `_formatStatsBullet` 变 async 并翻译全部列名：

```python
async def _formatStatsBullet(
    stats: dict[str, Any],
    *,
    sourceTable: str | None = None,
    db: Any = None,
) -> list[str]:
    """stats → bullet 行（确定性、不经 LLM）。

    顺序：异常 → topN → 分布 → 环比。
    列名走 resolveColumnName（本体别名 → 代码内列名字典 → 原列名），
    值走 resolveValueName（代码内值字典）。R4 前两者在真实数据上都是死的。
    """
    lines: list[str] = []
    anomalies = stats.get("anomalies") or []
    if anomalies:
        first = anomalies[0]
        dim = await resolveColumnName(db, sourceTable, str(first.get("dim") or ""))
        lines.append(f"- 异常：{dim} z={float(first.get('zScore', 0)):.2f}")
    top = stats.get("topContributors") or []
    if top:
        parts: list[str] = []
        for item in top[:3]:
            rawDim = str(item.get("dim") or "")
            dim = await resolveColumnName(db, sourceTable, rawDim)
            value = resolveValueName(sourceTable, rawDim, item.get("value"))
            parts.append(f"{dim}={value} ({float(item.get('sharePct') or 0):.1f}%)")
        lines.append(f"- 维度 topN：{', '.join(parts)}")
    dist = stats.get("distribution") or {}
    if dist:
        firstCol, firstStats = next(iter(dist.items()), (None, None))
        if firstCol and firstStats:
            col = await resolveColumnName(db, sourceTable, firstCol)
            lines.append(
                f"- 数值分布：{col} "
                f"min={firstStats.get('min', 0):g}, "
                f"max={firstStats.get('max', 0):g}, "
                f"mean={firstStats.get('mean', 0):.1f}, "
                f"std={firstStats.get('std', 0):.1f}"
            )
    mom = stats.get("mom") or []
    if mom:
        firstMom = mom[0]
        lines.append(
            f"- 环比：{firstMom.get('period', '')}→{mom[1].get('period', '')} "
            f"delta={firstMom.get('deltaPct', 0):+.1f}%" if len(mom) >= 2
            else f"- 环比：{firstMom.get('period', '')}（仅 1 期）"
        )
    return lines
```

(d) `_topContributorLabel` / `_numericRangeLabel` 同样 async 化并接 `db`：

```python
async def _topContributorLabel(
    stats: dict[str, Any] | None, *, sourceTable: str | None = None, db: Any = None
) -> str:
    """top1 贡献者标签：「基地=C1 (60.0%)」；无 stats → 「—」。"""
    if not stats:
        return "—"
    top = stats.get("topContributors") or []
    if not top:
        return "—"
    top1 = top[0]
    rawDim = str(top1.get("dim") or "")
    dim = await resolveColumnName(db, sourceTable, rawDim)
    value = resolveValueName(sourceTable, rawDim, top1.get("value"))
    return f"{dim}={value} ({float(top1.get('sharePct') or 0):.1f}%)"


async def _numericRangeLabel(
    stats: dict[str, Any] | None, *, sourceTable: str | None = None, db: Any = None
) -> str:
    """第一个数值列的 min–max：「10–100」；无 stats → 「—」。"""
    if not stats:
        return "—"
    dist = stats.get("distribution") or {}
    firstCol = next(iter(dist), None)
    if firstCol is None:
        return "—"
    d = dist[firstCol]
    lo, hi = float(d.get("min", 0)), float(d.get("max", 0))
    col = await resolveColumnName(db, sourceTable, firstCol)
    return f"{col}：{lo:g}–{hi:g}"
```

> 若现有 `_numericRangeLabel` 的返回串不含列名（实现者先读现状再改），保持其现有格式，仅把第一个数值列名过 `resolveColumnName`；不要顺手改格式（那是另一件事）。

(e) 三处调用点补 `await` 与 `db=session`、`sourceTable=`：

```python
            if view.stats:
                statsLines = await _formatStatsBullet(
                    view.stats, sourceTable=view.sourceTable, db=session
                )
```
```python
                "主要贡献者": await _topContributorLabel(
                    view.stats, sourceTable=view.sourceTable, db=session
                ),
```
```python
                "数值范围": await _numericRangeLabel(
                    view.stats, sourceTable=view.sourceTable, db=session
                ),
```

- [ ] **Step 10: 跑测试确认 GREEN**

Run: 同 Step 8 的命令。
Expected: PASS（既有 25 个全绿 + 新增 2 个）。

- [ ] **Step 11: 反向自检（防假绿）**

临时把 `display_name_resolver.py` 的 `DISPLAY_COLUMN_DICT` 改回空 dict，重跑 Step 8 → 两个新用例必须**转红**；确认后改回。这证明测试真的在测翻译，而不是在测 fixture 里恰好存在的字面量。

- [ ] **Step 12: Commit**

```bash
git add backend/app/services/display_name_resolver.py backend/app/services/report_planner.py \
        backend/app/tests/unit/test_display_name_resolver.py backend/app/tests/unit/test_report_planner.py
git commit -m "feat(research): [R4-2] 列名/值翻译在真实数据上生效（SQL 派生 sourceTable + 列名字典 + 大小写归一）"
```

---

### Task 3: LLM 摘要与备选段中文化

**理由**：`_findingSummary` 是**唯一进 LLM prompt 的数据形态**，现在吐的是 `按 site_code 分，C1 占 100%` —— LLM 于是照着英文列名写叙述。中文化后 LLM 才有机会写「按基地分」。同时在 `_alternativeSection`（验证未过的结论摘要）上一致生效。

**Files:**
- Modify: `backend/app/services/report_planner.py`
- Test: `backend/app/tests/unit/test_report_planner.py`

**Interfaces:**
- Consumes: Task 2 的 `resolveColumnName` / `resolveValueName`、`resolveSourceTable` 派生（经由 `view.sourceTable`）
- Produces（后续 task 依赖）:
  - `_findingSummary(view, *, db=None) -> str`（**async**，签名新增 `db`）
  - `_findingDigest(ctx, *, db=None) -> str`（**async**）
  - `_degradedText(ctx, *, db=None) -> str`（**async**）

- [ ] **Step 1: 写失败测试 —— prompt 摘要中文化**

追加到 `backend/app/tests/unit/test_report_planner.py`：

```python
@pytest.mark.asyncio
async def test_finding_summary_in_llm_prompt_is_translated(fakeReportDepsWithSqlOnly) -> None:
    """喂给 LLM 的摘要里列名/值应是中文（LLM 据此写叙述）——不是 site_code/QTY."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    sent = _promptsOf(planner, NARRATIVE_MARKER)
    joined = "\n".join(sent)
    assert "site_code" not in joined
    assert "基地" in joined
```

> 实现者注意：`_promptsOf(fake, marker)` 是文件里已有的 helper（过滤出命中 marker 的 system prompt 对应的 user content）；本用例的 `planner` 已由 fixture 建好，**不要**重建 fake。若既有 helper 直接接受 fake 而非 planner，按既有用法调整（照抄现有 R3-C 用例的取法）。

- [ ] **Step 2: 跑测试确认 RED**

Run: 同 Task 2 Step 8。
Expected: FAIL —— prompt 里仍有 `site_code`。

- [ ] **Step 3: 实现 —— 三个 helper async 化**

```python
async def _findingSummary(view: FindingView, *, db: Any = None) -> str:
    """结论摘要（喂 LLM 的唯一数据形态：不含任何原始行，但可引用 stats 数字）。

    R4：top1 的列名/值走翻译 —— LLM 看到「按 基地 分，C1 占 100%」才会用中文写叙述。
    """
    state = "已验证" if view.verified else f"未验证：{view.error}"
    base = f"{view.claimText}（置信度 {view.confidence}，{state}，{view.rowCount} 行）"
    if not view.stats or not view.stats.get("topContributors"):
        return base
    top1 = view.stats["topContributors"][0]
    rawDim = str(top1.get("dim") or "")
    dimLabel = await resolveColumnName(db, view.sourceTable, rawDim)
    valueLabel = resolveValueName(view.sourceTable, rawDim, top1.get("value"))
    sharePct = float(top1.get("sharePct") or 0.0)
    return f"{base}；按 {dimLabel} 分，{valueLabel} 占 {sharePct}%"


async def _findingDigest(ctx: _Context, *, db: Any = None) -> str:
    views = ctx.findings[:MAX_REPORT_FINDINGS]
    if not views:
        return EMPTY_FINDING_TEXT
    lines = [f"- {await _findingSummary(view, db=db)}" for view in views]
    return "\n".join(lines)


async def _degradedText(ctx: _Context, *, db: Any = None) -> str:
    """LLM 降级文案：显式说明 + 结构化摘要列表（brief 指定文案）。"""
    digest = await _findingDigest(ctx, db=db)
    return "\n".join([LLM_FALLBACK_NOTICE, *_bulletItems(digest)])
```

- [ ] **Step 4: 更新 5 处调用点（全部已在 async 方法内）**

```python
        # 643 行附近
        digest = await _findingDigest(ctx, db=session)
        text = await self._llmText(
            client,
            system=_NARRATIVE_SYSTEM.format(intent=_NARRATIVE_INTENTS[kind]),
            parts=[ctx.question, digest, semanticCtx],
        )
        return self._textSection(kind, text or await _degradedText(ctx, db=session), refs)
```
```python
        # _commentary（685/687 行附近）
        summary = await _findingSummary(view, db=session)
        text = await self._llmText(
            client,
            system=_COMMENTARY_SYSTEM,
            parts=[ctx.question, summary, semanticCtx],
        )
        return _splitBullets(text) if text else [summary]
```
```python
        # _alternativeSection（767 行附近）
        bullets = [await _findingSummary(view, db=session) for view in pending]
```

- [ ] **Step 5: 跑测试确认 GREEN**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_report_planner.py -q
```
Expected: PASS —— 含 **R2 遗留的 `test_llm_prompt_excludes_raw_rows`**（数字免疫契约：摘要里仍无原始行，只是标签变中文）。

- [ ] **Step 6: 数字免疫反向校验**

Run: `uv run pytest app/tests/unit/test_report_planner.py -q -k "raw_rows or number or prompt"`（若既有用例名不同，先 `-k` 试出正确选择器）。
Expected: PASS —— 若有任何失败，说明中文化把数字带进了 prompt，**必须回退**：只译标签，绝不译数字。

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/report_planner.py backend/app/tests/unit/test_report_planner.py
git commit -m "feat(research): [R4-3] LLM 摘要/备选段列名值中文化（prompt 里不再出现英文列名）"
```

---

### Task 4: 图表标签中文化 + 收紧 chart 类型测试

**理由**：R3-B 把 ECharts 渲染接上了，但 `chart_spec_builder._aliasByField` 只在有 `QueryPlan` 时给中文别名，而报告路径 `plan=None` ⇒ 图表轴/图例全是 `site_code`。同时现有 `test_chart_block_chartType_is_not_none` 断言「落在 11 种类型之一」，等于不断言 —— 收紧到 fixture 形状应得的类型。

**Files:**
- Modify: `backend/app/services/chart_spec_builder.py`
- Modify: `backend/app/services/chart_integration.py`
- Modify: `backend/app/services/report_planner.py`
- Test: `backend/app/tests/unit/test_report_planner.py`, `backend/app/tests/unit/test_chart_spec_builder.py`

**Interfaces:**
- Consumes: Task 2 的 `resolveColumnName`
- Produces:
  - `buildSpec(decision, signals, data, plan=None, *, aliasOverrides: Mapping[str, str] | None = None) -> ChartSpec`（新参数默认 `None` ⇒ chat 侧行为**逐字不变**）
  - `buildReportChart(session, *, rows, columns, question="", plan=None, aliasOverrides=None)`（新参数默认 `None`）
  - `report_planner._chartContent(view, *, session)` 内部自建 `aliasOverrides`

- [ ] **Step 1: 写失败测试 —— `buildSpec` 接受 `aliasOverrides`**

追加到 `backend/app/tests/unit/test_chart_spec_builder.py`（文件不存在则新建，import 与既有 chart 测试同款）：

```python
def test_buildSpec_aliasOverrides_rename_dimension_and_measure_labels() -> None:
    """aliasOverrides 直接给出「结果集列名 → 标签」；plan=None（报告路径）时也生效."""
    decision = decideChartKind(buildChartSignals(None, ("site_code", "bill_cnt"), ROWS, "按基地分"), DEFAULT_THRESHOLDS)
    spec = buildSpec(
        decision, buildChartSignals(None, ("site_code", "bill_cnt"), ROWS, "按基地分"),
        ROWS, None, aliasOverrides={"site_code": "基地", "bill_cnt": "账单数"},
    )
    labels = {d.field: d.label for d in spec.dimensions} | {m.field: m.label for m in spec.measures}
    assert labels.get("site_code") == "基地"
    assert labels.get("bill_cnt") == "账单数"
```

> 实现者注意：`ROWS` / `DEFAULT_THRESHOLDS` / `decideChartKind` 的实参形状照抄该测试文件（或 chat 侧）现有 `buildSpec` 用例，勿臆造。若既有用例用 `loadChartThresholds` 的 fixture，沿用。

- [ ] **Step 2: 跑测试确认 RED**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit/test_chart_spec_builder.py -q
```
Expected: FAIL —— `TypeError: buildSpec() got an unexpected keyword argument 'aliasOverrides'`。

- [ ] **Step 3: 实现 `buildSpec` 的 `aliasOverrides`**

在 `chart_spec_builder.py`：给 `_aliasByField` 加 `overrides` 形参并让 override 优先：

```python
def _aliasByField(
    plan: QueryPlan | None,
    columns: tuple[str, ...],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """「结果集列名 → 中文别名」映射（overrides 最优先，其次 plan 聚合别名）.

    `overrides` 供**无 QueryPlan** 的路径（研究报告）注入标签 —— 否则
    `aliases.get("site_code")` 永远落空，图表轴/图例只能是英文列名。
    """
    aliases: dict[str, str] = dict(overrides or {})
    if plan is None:
        return aliases
    for aggregation in plan.aggregations:
        if not aggregation.alias:
            continue
        column = _resolveColumn(aggregation.property, columns)
        if column is not None:
            aliases.setdefault(column, aggregation.alias)
    return aliases
```

`buildSpec` 签名与调用处：

```python
def buildSpec(
    decision: ChartDecision,
    signals: ChartSignals,
    data: list[dict[str, Any]],
    plan: QueryPlan | None = None,
    *,
    aliasOverrides: Mapping[str, str] | None = None,
) -> ChartSpec:
```
```python
    aliases = _aliasByField(plan, signals.columns, aliasOverrides)
```

（`Mapping` 从 `collections.abc` import。）

- [ ] **Step 4: 跑测试确认 GREEN**

Run: 同 Step 2。
Expected: PASS。**并且** chat 侧既有 chart 测试全绿（默认 `None` ⇒ 行为不变）：
```bash
uv run pytest app/tests/unit -q -k "chart"
```

- [ ] **Step 5: 写失败测试 —— 报告图表标签中文化 + 收紧类型断言**

在 `backend/app/tests/unit/test_report_planner.py` 里**改写**既有 `test_chart_block_chartType_is_not_none` 的结尾断言（把「落在 11 种之一」换成按 fixture 形状断言），并追加：

```python
@pytest.mark.asyncio
async def test_chart_labels_are_translated(fakeReportDepsWithSqlOnly) -> None:
    """图表轴/图例标签中文化（报告无 QueryPlan，只能靠 aliasOverrides）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart = next(b for s in payload["sections"] if s["kind"] == "data"
                 for b in s["blocks"] if b["type"] == "chart")
    option = chart["content"]["echartsOption"]
    dumped = json.dumps(option, ensure_ascii=False)
    assert "基地" in dumped
    assert "site_code" not in dumped
```

（文件顶部若无 `import json`，加上。）

- [ ] **Step 6: 跑测试确认 RED**

Run: 同 Task 2 Step 8。
Expected: FAIL —— `site_code` 仍在 echartsOption 里。

- [ ] **Step 7: 实现 —— `chart_integration` 透传 + `report_planner` 自建 aliasMap**

`chart_integration.py`：

```python
from collections.abc import Mapping

async def buildReportChart(
    session: AsyncSession,
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    question: str = "",
    plan: QueryPlan | None = None,
    aliasOverrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    ...
            spec: ChartSpec = buildSpec(decision, signals, rows, plan, aliasOverrides=aliasOverrides)
```

（其余不动：失败仍 `except Exception` 降级 TABLE。）

`report_planner._chartContent`：

```python
async def _chartContent(view: FindingView, *, session: AsyncSession) -> dict[str, Any]:
    """chart 块 content：行数据逐字来自 finding.supporting_data["rows"].

    R4：报告无 QueryPlan ⇒ 自建 aliasOverrides（列名 → 中文），否则图表轴/图例
    只能是英文列名。失败降级为 TABLE，绝不抛错。
    """
    if not view.rows:
        return {
            "title": view.claimText or "（无数据）",
            "chartType": "table",
            "columns": list(view.rows[0].keys()) if view.rows else [],
            "data": [],
            "echartsOption": None,
        }
    columns = list(view.rows[0].keys())
    aliasOverrides = {
        column: await resolveColumnName(session, view.sourceTable, column)
        for column in columns
    }
    return await buildReportChart(
        session,
        rows=view.rows,
        columns=columns,
        question=view.claimText or "",
        aliasOverrides=aliasOverrides,
    )
```

> 注：自建 aliasMap 时**不过滤未命中项**（未命中的列名恰好等于原名，`_aliasByField` 里等同无 alias），避免多一层分支。

- [ ] **Step 8: 跑测试确认 GREEN**

Run: 同 Task 2 Step 8。
Expected: PASS。

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/chart_spec_builder.py backend/app/services/chart_integration.py \
        backend/app/services/report_planner.py backend/app/tests/unit/test_report_planner.py \
        backend/app/tests/unit/test_chart_spec_builder.py
git commit -m "feat(research): [R4-4] 图表轴/图例中文化（aliasOverrides 注入，chat 侧行为不变）"
```

---

### Task 5: 真机回归（验收，非代码变更）

**Files:** 无（产出是一份回归记录，追加到 R4 的 SDD workspace `task-5-report.md`）

**Interfaces:**
- Consumes: Tasks 1–4 的全部改动
- Produces: 回归结论（通过 / 缺口清单）

- [ ] **Step 1: 重建并重启后端容器**

```bash
cd docker && docker compose build backend && docker compose up -d --force-recreate --no-deps backend
docker compose logs --tail=40 backend    # 确认无启动异常（历史坑：CMD 现装依赖 130s 超时）
```

- [ ] **Step 2: 重跑 R2 那个研究会话的同一问题**

用与 session `4a45e267-179d-472b-84b1-985012ceaa5f` 相同的问题（dev 前端发起或直连 API），等报告归档。

- [ ] **Step 3: 核验 renderedMd / payload 的 5 条断言**

```bash
docker exec qa-postgres psql -U qa_user -d qa_metadata -c \
"select rendered_md from research_report where session_id='<新会话id>' order by version desc limit 1;"
```

| # | 断言 | 判据 |
|---|---|---|
| 1 | data 段 stats bullet 列名中文 | 出现「基地」/「账单数」/「确认阶段」，且不再出现 `site_code`/`bill_cnt` |
| 2 | 值中文 | 出现「数量确认」/「价格确认」/「已确认」 |
| 3 | 图表标签中文 | payload 的 `echartsOption` 序列化串里含「基地」、不含 `site_code` |
| 4 | 表格段表头/贡献者列中文 | `hypothesis_table` 的「主要贡献者」是 `基地=C1 (…)` |
| 5 | 列名整体残量下降 | `rendered_md` 里 `site_code` 出现次数 **= 0**（`支撑 SQL` 列除外，那是原始 SQL，**应当**保留英文） |

- [ ] **Step 4: 若 5 条全过 → 记录结论；若有缺口 → 记入 `task-5-report.md` 并评估是否回 Task 2/4 补**

- [ ] **Step 5: 提交回归记录**

```bash
git add docs/superpowers/plans/2026-10-09-research-report-r4.md
git commit -m "docs(research): [R4-5] 真机回归记录"
```

---

## Verification

### Unit（全部改完后一次跑齐）

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/unit -q
```

预期：全绿，含 R2/R3 遗留 + R4 新增（本次规划基线 = `test_report_planner.py` + `test_display_name_resolver.py` 共 **25 passed**，R4 预计新增 ~14 个用例）。

### Integration

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  uv run pytest app/tests/integration/test_research_agent_service.py app/tests/integration/test_research_api.py -q
```

预期：与 R4 前**同样的**红灯集合（R3 已确认存在 3 个 pre-existing Neo4j 相关红灯，与本次改动无关）；**不得新增红灯**。若新增，是 R4 bug。

### 覆盖率

```bash
cd backend && TEST_DATABASE_URL=... TEST_NEO4J_URI=... \
  uv run pytest app/tests/unit -q --cov=app.services.display_name_resolver --cov=app.services.report_planner --cov-report=term-missing
```

预期：两个模块行覆盖 ≥ 80%。

### 数字免疫契约（R2 铁律，R4 必须继续成立）

`test_llm_prompt_excludes_raw_rows`（或等价）必须 PASS：LLM prompt 中**没有任何原始行数据**。R4 只把摘要的**标签**中文化，数字仍只出现在 chart/table 的 content 里。

---

## Out of Scope（明确不做，附理由）

- **不播种 `ontology_property.property_alias`**（3699 行）：那是数据治理任务；代码路径已就绪（本体 alias 是翻译链的第一优先级），**治理完成后零代码改动即自动生效**。本轮用代码内 `DISPLAY_COLUMN_DICT` 覆盖 demo 所需列。
- **不记录写侧 `sourceTable`**（`research_agent_phases.findingData`）：读侧派生已覆盖新旧会话；改状态机会牵动 checkpoint 恢复，风险收益不成比例。
- **不做多表消歧**：`deriveSourceTable` 只取首个 FROM。JOIN 场景下列名可能来自第二张表 → 落回原列名（降级，不报错）。
- **不做图表 stats hint**（R3-B1 遗留）：决策引擎已按数据形状+问句派生；仅当真机回归显示选错图型才加。
- **不译 base/site 值**（`C1`/`T1`/`T7`）：需要 site 主数据；列名「基地」已让「基地=C1」可读。
- **不改前端 / chat 侧 / 方言守卫 / schema reflection / 不建迁移**。

---

## Self-Review

**1. Spec coverage**（对照用户可见缺口）：

| 缺口 | 覆盖 | 判据 |
|---|---|---|
| 列名是 code（`site_code`） | Task 2 + Task 3 + Task 4 | data 段 / 表格段 / LLM prompt / 图表轴四处均中文 |
| 值是 code（`QTY`/`20`） | Task 2 | `resolveValueName` 因 sourceTable 派生而**首次真正生效** |
| 报告没有图 | R3-B 已交付；R4 补标签 | Task 4 图表标签中文，R3 已验 `chartType ≠ None` + 前端渲染 |
| 分析内容太少 | R3-C 已交付；R4 让 prompt 用中文标签 | Task 3 使 LLM 看到「按 基地 分」 |

**2. Placeholder scan**：无 TBD/TODO；每个代码步骤都给了完整可抄的实现。三处「实现者注意」是**求证指令**（以既有文件为准），不是占位符。

**3. Type consistency**：

- `deriveSourceTable(sql)` 返回**大写**表名 ⇒ `DISPLAY_COLUMN_DICT` / `DISPLAY_VALUE_DICT` 键大写 ⇒ `resolveColumnName`/`resolveValueName` 内 `.upper()` 归一 ⇒ 与本体 `source_column`（实测大写）一致。全链一致。
- `_formatStatsBullet` / `_topContributorLabel` / `_numericRangeLabel` / `_findingSummary` / `_findingDigest` / `_degradedText` 六个 helper 在 Tasks 2–3 里统一 async 化，且全部调用点在同一 task 内更新（已逐处列出），无「Task 2 改了签名、Task 3 还在同步调」的悬空。
- `buildSpec` 新增的 `aliasOverrides` 关键字参数默认 `None` ⇒ chat 侧调用点（不传）行为逐字不变。

**4. 风险点（实现者须先验证，勿假设）**：

- `_FakeLlm` / `_promptsOf` / `_numericRangeLabel` 的**现有确切形态**以文件为准；本计划的代码片段若与现状有出入，**以现状为准并保持行为等价**。
- `test_chart_spec_builder.py` 可能不存在 ⇒ 按 Step 1 注新建，构造参数照抄既有 chart 用例。
- `resolveColumnName(db=None)` 是新能力（降级路径）；`_findingSummary(..., db=None)` 在旧夹具直接调本方法的路径上必须不抛 —— Task 2 Step 1 的 `test_resolveColumnName_works_without_db` 钉住它。
