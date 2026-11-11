# KPI 匹配器 v2（包含式 + 歧义澄清 + 50 条回归集）实施计划 — P1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 KPI 匹配从「Jaccard 取 top1」换成「精确 code → 归一化包含 → 最长短语胜出 → 等长判歧义并反问用户」，
并用 50 条真实问句回归集把命中率与误命中钉死。

**Architecture:** 匹配算法全部落在 `KpiSemanticMatchService.matchRoute()`（纯函数式，无 DB、无 LLM）；
缓存补 `metric_alias` 作为第三个短语源；`_runL1AndL4Preamble` 增加一条「歧义 ⇒ 澄清」短路，
沿用既有 `intent=clarify` 线上契约（前端无需改动）；运行时四个入口（chat preamble、Tier B 契约块、
B3 注入、admin 诊断）统一按 `KPI_MATCH_CONTAINMENT_ENABLED` 灰度开关在 v1/v2 之间切换，开关默认关
（fail-closed），生产行为在显式置 true 前保持不变。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / PostgreSQL 16 / pytest + httpx AsyncClient。
**不升级任何依赖版本**（用户约束）。

---

## Global Constraints（逐条来自仓库规则与用户指令，每个任务隐含适用）

1. **不可变数据**：匹配结果与缓存索引一律「新建对象整体替换」，禁止原地改 list/dict 元素。
2. **TDD**：每个任务 RED（先看它失败）→ GREEN（最小实现）→ REFACTOR；新增代码覆盖率 ≥ 80%。
3. **真实数据库测试**：集成测试必须真实 PostgreSQL + 完整 HTTP 链路，禁止 sqlite 内存库或直接调 service。
   测试库白名单 `^qa_metadata_test($|_)`；本批固定用 5434 的 `qa_metadata_test`。
4. **不部署、不做生产写**：本批只改代码与测试；生产仍在 alembic `0140` 跑旧代码。
5. **不新增 Alembic 迁移**（零 DDL）：本批无 schema 变更。
6. **小文件**：单文件 ≤ 800 行，函数 < 50 行；新文件按职责拆分，不往 `chat_service.py` 继续堆。
7. **显式错误处理**：v2 路径的每个降级点都要 `logger.warning(..., exc_info=True)` 并回落旧路径，
   不静默吞；诊断接口保持「故障即 500」（诊断不能骗人）。
8. **计划以天为最小管理单位**：任务 = 1 个工作日，当天可独立提交、可独立评审。
9. **合并判据 = CI 失败集合对差净新增 0**，不是「CI 全绿」；`gh` 与 git remote 走
   `HTTPS_PROXY=http://127.0.0.1:12450`。
10. **禁止后端覆盖用户 modelId**；本批不触碰模型选择。
11. **口径来源**：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`
    §5（匹配规则）、§10（P1 范围）、§12（三个开关）、§13（M1 验收）、§14（已决事项）。

## 测试环境（每个任务的测试命令都先导出这些）

```bash
cd /private/tmp/qa-kpi-p1/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
export MILVUS_DB_NAME=qa_test
```

> 停 pytest 用 SIGINT；**不要**跑后端全量 unit 套件当门禁（27min/28 红已知基线），只跑定向文件。

---

## 关键设计决定（实施前先读，避免走偏）

### D-1 匹配顺序与判定（设计 §5.1/§5.2）

```
1. 精确 code（KPI_SUPPLIER_OTD 这种大写下划线 token）→ confidence 1.0，单命中
2. 归一化包含：短语源 = kpi_name + semantic_keywords + metric_alias
   - 归一化：NFKC → lower → 非字母数字转空格 → 折叠空白 → strip
   - 含 CJK 的短语：子串即命中
   - 纯 ASCII 短语：必须满足词边界 (?<![a-z0-9])phrase(?![a-z0-9])，否则 "OTD" 会命中 "notdone"
   - 短语门槛：CJK ≥ 2 字，或 ASCII 字母数字 ≥ 3
3. 每个 KPI 取自己命中的**最长**短语；全局比较，**最长者胜出**
4. 最长长度并列且分属不同 KPI ⇒ ambiguous（**不得**取 top1），候选按 code 升序
5. 无命中 ⇒ none（回落 LLM 流水线）
```

### D-2 澄清复用什么（设计 §14）

- 复用既有 `intent="clarify"`（前端 `KNOWN_INTENTS` 已含），`data=[{code,name}]`，`tokensUsed=0`，`cost=0.0`。
- 前端**零改动**：`data` 只在设置 `chartType` 时渲染，clarify 不带图 ⇒ 只显示 answer 正文。
- 插入点在 `_runL1AndL4Preamble`（意图分类**之前**）——放 CLARIFY 域处理器（概念解释）里会走到 LLM。

### D-3 开关语义（与 `KPI_CONTRACT_INJECTION_ENABLED` 完全同构）

| 层 | 默认 | 作用 |
|---|---|---|
| env `KPI_MATCH_CONTAINMENT_ENABLED` | `true` | 进程级紧急杀闸：置 false ⇒ 一律走 v1 |
| `system_config` 行 | 行缺失 ⇒ **false** | 灰度旋钮：显式 `true` 才启用 v2 |

⇒ **合入本批后生产照旧**（没有 system_config 行 = v1），开灰度是一次单独的数据操作。

### D-4 歧义时的注入行为

`ambiguous` ⇒ `codes=[]`，**不建契约块**。原因：把两个互相矛盾的口径同时塞进 prompt，
等于让模型替用户选一个指标——这正是「不得猜」要禁止的事。chat 已经会先反问，不会执行 SQL。

### D-5 不动的面（本批明确不覆盖）

- `/search` 端点、研究线 ESL 指标臂、B4 分母警告：仍是 v1 `matchAll`（诊断/发现类界面，不是执行面）。
  变更记录里写明。
- 不删 Jaccard：设计 §14 已决「保留给诊断」，`matchAll` 继续用它。

---

## 文件结构（先定边界，再写任务）

| 文件 | 职责 | 动作 |
|---|---|---|
| `app/services/kpi_semantic_match_service.py` | 匹配算法（v1 + v2 共存） | +归一化/包含/路由 + `matchRoute` |
| `app/services/kpi_match_cache.py` | PUBLISHED KPI 只读索引 | +`metric_alias` 外连接 + `metricAliasOf` |
| `app/services/kpi_metric_switches.py` | 灰度开关读取 SSOT | +`SWITCH_MATCH_CONTAINMENT` + `isKpiMatchContainmentEnabled` |
| `app/config.py` | env 配置 | +`kpiMatchContainmentEnabled` |
| `app/services/chat_constants.py` | wire 常量 SSOT | +`ROUTING_LAYER_KPI_CLARIFY` / `META_INTENT_CLARIFY` / `metaIntentOf` |
| `app/services/chat_stream_output.py` | L1/L4 preamble（普通+流式共用） | +`_routeKpi` / `_buildKpiClarifyPreamble` |
| `app/services/chat_stream_entry.py` | SSE 入口 | meta/done 帧改用 `metaIntentOf` 与澄清层 |
| `app/services/chat_service.py` | 非流式入口 | 澄清层持久化 + 日志 |
| `app/domain/schemas.py` | `ChatResponse` | +`match_method` |
| `app/services/kpi_contract_builder.py` | Tier B 契约块匹配 | v2 切换 |
| `app/services/kpi_contract_reconcile.py` | B3 注入 | v2 切换 |
| `app/api/v1/kpi_diagnose.py` | admin 诊断 | v2 切换 + `ambiguous` 字段 |
| 测试 | 见各任务 Files | 新建/追加 |

---

### Task D1（第 1 天）：匹配算法 v2 + 单元测试

**Files:**
- Modify: `app/services/kpi_semantic_match_service.py`
- Test: `app/tests/unit/test_kpi_match_route.py`（新建）

**Interfaces:**
- Produces:
  - `normalizeKpiText(text: str) -> str`
  - `isEligiblePhrase(phrase: str) -> bool`
  - `phraseMatchesQuestion(phrase: str, normalizedQuestion: str) -> bool`
  - `@dataclass(frozen=True) KpiMatchRoute(kind: Literal["hit","ambiguous","none"], matches: tuple[KpiMatchResult, ...] = ())`
  - `KpiMatchResult.matched_phrase: str | None = None`（新字段）
  - `KpiSemanticMatchService.matchRoute(question: str) -> KpiMatchRoute`
  - `buildKpiClarifyAnswer(question: str, matches: Sequence[KpiMatchResult]) -> str`
  - 常量 `CONTAINMENT_CONFIDENCE = 0.9`
  - 抽象缓存基类新增 `is_warmUp()` / `getAll()` / `metricAliasOf(code)` 声明

- [ ] **Step 1 — 写失败测试** `app/tests/unit/test_kpi_match_route.py`

```python
"""P1 匹配器 v2：归一化包含 + 最长短语胜出 + 等长歧义（设计 §5.1/§5.2）。

纯算法，无 DB：`_RouteCache` 是内存桩，只实现 matchRoute 需要的三个方法。
每条用例都对应一个可以通过变异让它变红的判别点（D5 有变异取证清单）。
"""

from __future__ import annotations

import pytest

from app.services.kpi_semantic_match_service import (
    KpiMatchRoute,
    KpiSemanticMatchService,
    buildKpiClarifyAnswer,
    isEligiblePhrase,
    normalizeKpiText,
    phraseMatchesQuestion,
)


# ---------------------------------------------------------------------------
# 桩：只实现 matchRoute 用到的三个方法（duck typing，不继承 KpiMatchCache）
# ---------------------------------------------------------------------------

class _Kpi:
    def __init__(self, code: str, name: str, keywords: list[str] | None) -> None:
        self.kpi_code = code
        self.kpi_name = name
        self.semantic_keywords = keywords


class _RouteCache:
    def __init__(self, kpis: list[_Kpi], aliases: dict[str, str] | None = None) -> None:
        self._kpis = list(kpis)
        self._aliases = dict(aliases or {})
        self._warm = True

    def is_warmUp(self) -> bool:
        return self._warm

    def getAll(self) -> list[_Kpi]:
        return list(self._kpis)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._kpis)

    def metricAliasOf(self, code: str) -> str | None:
        return self._aliases.get(code)


_OTD = _Kpi("KPI_SUPPLIER_OTD", "供应商准时交付率", ["准时交付", "OTD", "交付率", "on time delivery"])
_OVERDUE = _Kpi("KPI_SUPPLIER_OVERDUE_RATIO", "供应商逾期率", ["逾期率", "逾期订单"])
_DELAY = _Kpi("KPI_SUPPLIER_DELAY_DAYS", "供应商平均延误天数", ["逾期天数", "延误"])


@pytest.fixture()
def svc() -> KpiSemanticMatchService:
    return KpiSemanticMatchService(_RouteCache([_OTD, _OVERDUE, _DELAY]))


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def test_normalize_fullwidth_and_punctuation_become_spaces() -> None:
    """全角标点／连字符必须变成分隔符：否则 "on-time" 会粘成 "ontime"。"""
    assert normalizeKpiText("Ｏｎ－Time　Delivery！") == "on time delivery"


def test_normalize_collapses_whitespace() -> None:
    assert normalizeKpiText("  供应商   交付率  ") == "供应商 交付率"


def test_normalize_empty_string() -> None:
    assert normalizeKpiText("") == ""


# ---------------------------------------------------------------------------
# 短语门槛
# ---------------------------------------------------------------------------

def test_single_cjk_char_phrase_is_ineligible() -> None:
    """单字中文短语一律不合格（"险" 会命中一半问题）。"""
    assert isEligiblePhrase("险") is False


def test_two_cjk_char_phrase_is_eligible() -> None:
    assert isEligiblePhrase("逾期") is True


def test_two_letter_ascii_phrase_is_ineligible() -> None:
    assert isEligiblePhrase("ok") is False


def test_three_letter_ascii_phrase_is_eligible() -> None:
    assert isEligiblePhrase("OTD") is True


def test_mixed_phrase_eligible_by_ascii_side() -> None:
    """1 个中文 + 3 个字母：ASCII 侧达标即可。"""
    assert isEligiblePhrase("NCR率") is True


# ---------------------------------------------------------------------------
# 包含判定
# ---------------------------------------------------------------------------

def test_cjk_phrase_matches_as_substring() -> None:
    assert phraseMatchesQuestion("逾期率", normalizeKpiText("本月逾期率是多少")) is True


def test_ascii_phrase_requires_token_boundary() -> None:
    """关键判别点：OTD 不能命中 notdone / otdx。"""
    assert phraseMatchesQuestion("OTD", normalizeKpiText("notdone")) is False
    assert phraseMatchesQuestion("OTD", normalizeKpiText("otdx 是多少")) is False
    assert phraseMatchesQuestion("OTD", normalizeKpiText("算一下 OTD 是多少")) is True


def test_ascii_phrase_matches_across_hyphen() -> None:
    """归一化把连字符变空格后，"on time delivery" 能命中 "on-time delivery"。"""
    assert phraseMatchesQuestion("on time delivery", normalizeKpiText("on-time delivery")) is True


def test_empty_phrase_never_matches() -> None:
    assert phraseMatchesQuestion("", normalizeKpiText("任何问题")) is False


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@pytest.mark.asyncio()
async def test_exact_code_wins_with_confidence_one(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("KPI_SUPPLIER_OTD 最近怎么样")
    assert route.kind == "hit"
    assert len(route.matches) == 1
    assert route.matches[0].code == "KPI_SUPPLIER_OTD"
    assert route.matches[0].confidence == 1.0
    assert route.matches[0].method == "exact_code"


@pytest.mark.asyncio()
async def test_single_containment_hit(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("哪些供应商的交付率低")
    assert route.kind == "hit"
    assert [m.code for m in route.matches] == ["KPI_SUPPLIER_OTD"]
    assert route.matches[0].method == "containment"
    assert route.matches[0].matched_phrase == "交付率"


@pytest.mark.asyncio()
async def test_longest_phrase_wins(svc: KpiSemanticMatchService) -> None:
    """「逾期天数」4 字属于 DELAY，OVERDUE 只有 2 字「逾期」⇒ DELAY 胜出。"""
    route = await svc.matchRoute("逾期天数排行")
    assert route.kind == "hit"
    assert [m.code for m in route.matches] == ["KPI_SUPPLIER_DELAY_DAYS"]
    assert route.matches[0].matched_phrase == "逾期天数"


@pytest.mark.asyncio()
async def test_equal_length_cross_kpi_is_ambiguous_sorted_by_code(
    svc: KpiSemanticMatchService,
) -> None:
    """3+3 并列且分属两个 KPI ⇒ ambiguous，候选按 code 升序（确定性）。"""
    route = await svc.matchRoute("供应商交付率和逾期率分别是多少")
    assert route.kind == "ambiguous"
    assert [m.code for m in route.matches] == [
        "KPI_SUPPLIER_OTD",
        "KPI_SUPPLIER_OVERDUE_RATIO",
    ]


@pytest.mark.asyncio()
async def test_no_match_returns_none(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("供应商一共有多少家")
    assert route.kind == "none"
    assert route.matches == ()


@pytest.mark.asyncio()
async def test_empty_question_returns_none(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("   ")
    assert route.kind == "none"


@pytest.mark.asyncio()
async def test_cold_cache_returns_none() -> None:
    cache = _RouteCache([_OTD])
    cache._warm = False
    route = await KpiSemanticMatchService(cache).matchRoute("供应商交付率")
    assert route.kind == "none"


@pytest.mark.asyncio()
async def test_metric_alias_is_a_phrase_source() -> None:
    """KPI 挂靠 metric 的别名参与匹配（设计 §5.1 第三个短语源）。"""
    cache = _RouteCache([_Kpi("KPI_X", "X 指标", [])], aliases={"KPI_X": "准时交货"})
    route = await KpiSemanticMatchService(cache).matchRoute("这个月准时交货情况")
    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_X"
    assert route.matches[0].matched_phrase == "准时交货"


def test_route_is_frozen() -> None:
    route = KpiMatchRoute(kind="none")
    with pytest.raises(Exception):
        route.kind = "hit"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 澄清文案
# ---------------------------------------------------------------------------

def test_clarify_answer_lists_every_candidate() -> None:
    from app.services.kpi_semantic_match_service import KpiMatchResult

    text = buildKpiClarifyAnswer(
        "供应商交付率和逾期率分别是多少",
        (
            KpiMatchResult(code="KPI_SUPPLIER_OTD", confidence=0.9, kpi_name="供应商准时交付率"),
            KpiMatchResult(code="KPI_SUPPLIER_OVERDUE_RATIO", confidence=0.9, kpi_name="供应商逾期率"),
        ),
    )
    assert "供应商准时交付率" in text
    assert "KPI_SUPPLIER_OVERDUE_RATIO" in text
    assert "供应商交付率和逾期率分别是多少" in text
    # 必须明确要求用户确认，不能直接给数字
    assert "确认" in text
```

- [ ] **Step 2 — 跑 RED**：`pytest app/tests/unit/test_kpi_match_route.py -q`
  预期 `ImportError: cannot import name 'KpiMatchRoute'`（算法整块还不存在）。

- [ ] **Step 3 — 写最小实现**（`app/services/kpi_semantic_match_service.py`）

模块顶部补 import：

```python
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal
```

`KpiMatchResult` 增加字段（放在 `method` 之后）：

```python
    matched_phrase: str | None = None
```

新增常量与工具函数（放在 `jaccard` 之后）：

```python
# 包含式命中的置信度：规则确定，但与「用户直接点名 code」的身份级命中不同，
# 故低于 1.0。误命中兜底靠最长匹配 + 等长澄清 + 50 条回归集（设计 §5.2）。
CONTAINMENT_CONFIDENCE = 0.9

_CJK_START = "一"
_CJK_END = "鿿"


def _hasCjk(text: str) -> bool:
    return any(_CJK_START <= ch <= _CJK_END for ch in text)


def _cjkCount(text: str) -> int:
    return sum(1 for ch in text if _CJK_START <= ch <= _CJK_END)


def _asciiAlnumCount(text: str) -> int:
    return sum(1 for ch in text if ch.isascii() and ch.isalnum())


def normalizeKpiText(text: str) -> str:
    """匹配归一化：NFKC（全角→半角）→ 小写 → 非字母数字转空格 → 折叠空白。

    标点转**空格**而不是删除：英文短语（"on-time delivery"）与中文分句都需要
    一个分隔符；删除会把 "on-time" 粘成 "ontime"，也会让跨分句的伪短语成立
    （"交付率，不良率" 删掉逗号后可能出现本不存在的长短语）。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    spaced = "".join(ch if ch.isalnum() else " " for ch in normalized)
    return " ".join(spaced.split())


def isEligiblePhrase(phrase: str) -> bool:
    """命中短语最低长度：CJK ≥ 2 字，或 ASCII 字母数字 ≥ 3（设计 §5.1）。"""
    normalized = normalizeKpiText(phrase)
    return _cjkCount(normalized) >= 2 or _asciiAlnumCount(normalized) >= 3


def phraseMatchesQuestion(phrase: str, normalizedQuestion: str) -> bool:
    """完整包含判定（大小写/全半角已由 normalizeKpiText 处理）。

    - 含 CJK 的短语：子串即命中（中文没有词边界）。
    - 纯 ASCII 短语：左右不得与其他字母/数字相连 —— 否则 "otd" 会命中
      "notdone"，"iqc" 会命中 "biqc" 这类标识符片段。
    """
    normalizedPhrase = normalizeKpiText(phrase)
    if not normalizedPhrase:
        return False
    if _hasCjk(normalizedPhrase):
        return normalizedPhrase in normalizedQuestion
    pattern = rf"(?<![a-z0-9]){re.escape(normalizedPhrase)}(?![a-z0-9])"
    return re.search(pattern, normalizedQuestion) is not None


@dataclass(frozen=True)
class KpiMatchRoute:
    """包含式匹配的路由判定（v2）。

    kind 三态（设计 §5.2）：
    - ``hit``：唯一命中最长短语 ⇒ 直接执行，matches 恒为 1 条
    - ``ambiguous``：最长长度并列且分属不同 KPI ⇒ **必须澄清**，不得取 top1
    - ``none``：无命中 ⇒ 回落 LLM 流水线

    matches 恒按 code 升序 —— 歧义集合必须确定性排序，否则同一问题两次
    回答的候选顺序会不同。
    """

    kind: Literal["hit", "ambiguous", "none"]
    matches: tuple[KpiMatchResult, ...] = ()


def _phrasesOf(kpi: "KpiCatalog", alias: str | None) -> tuple[str, ...]:
    """一个 KPI 的全部匹配短语源：名称 + 语义关键词 + 挂靠 metric 别名。"""
    phrases = [kpi.kpi_name] if kpi.kpi_name else []
    phrases.extend(kpi.semantic_keywords or [])
    if alias:
        phrases.append(alias)
    return tuple(phrases)


def buildKpiClarifyAnswer(
    question: str, matches: Sequence[KpiMatchResult]
) -> str:
    """歧义澄清文案：列出全部候选并请用户确认（不猜、不执行）。"""
    lines = [f"「{question}」可能对应多个指标，请确认您想查询的是哪一个："]
    for index, match in enumerate(matches, start=1):
        lines.append(f"{index}. {match.kpi_name or match.code}（{match.code}）")
    lines.append("请回复指标名称或 KPI 编码，我再按该口径回答。")
    return "\n".join(lines)
```

抽象缓存基类补三个声明（`KpiMatchCache` 类里，`findByAnyKeyword` 之后）：

```python
    def is_warmUp(self) -> bool:
        """缓存是否已预热（matchRoute 冷缓存时直接返回 none）。"""
        raise NotImplementedError

    def getAll(self) -> list["KpiCatalog"]:
        """全部 PUBLISHED KPI（包含式匹配需要遍历全量短语）。"""
        raise NotImplementedError

    def metricAliasOf(self, code: str) -> str | None:
        """KPI 挂靠 metric 的别名（第三个短语源）；无别名/冷缓存返 None。"""
        raise NotImplementedError
```

`KpiSemanticMatchService` 增加 `matchRoute`（放在 `matchAll` 之后）：

```python
    async def matchRoute(self, question: str) -> KpiMatchRoute:
        """v2 匹配：精确 code → 归一化包含 → 最长短语胜出 → 等长判歧义。

        与 ``match()``/``matchAll()``（Jaccard，保留给诊断与 /search）并存：
        调用方按 ``KPI_MATCH_CONTAINMENT_ENABLED`` 灰度开关选择走哪条。
        ```
        """
        if not question or not question.strip():
            return KpiMatchRoute(kind="none")

        exact = self._matchExactAlias(question)
        if exact is not None:
            return KpiMatchRoute(kind="hit", matches=(exact,))

        if not self._cache.is_warmUp():
            return KpiMatchRoute(kind="none")

        normalizedQuestion = normalizeKpiText(question)
        if not normalizedQuestion:
            return KpiMatchRoute(kind="none")

        # (最长短语长度, 命中结果)
        hits: list[tuple[int, KpiMatchResult]] = []
        for kpi in self._cache.getAll():
            bestLength = 0
            bestPhrase = ""
            for phrase in _phrasesOf(kpi, self._cache.metricAliasOf(kpi.kpi_code)):
                if not isEligiblePhrase(phrase):
                    continue
                if not phraseMatchesQuestion(phrase, normalizedQuestion):
                    continue
                length = len(normalizeKpiText(phrase))
                if length > bestLength:
                    bestLength = length
                    bestPhrase = phrase
            if bestLength == 0:
                continue
            hits.append(
                (
                    bestLength,
                    KpiMatchResult(
                        code=kpi.kpi_code,
                        confidence=CONTAINMENT_CONFIDENCE,
                        kpi_name=kpi.kpi_name,
                        method="containment",
                        matched_phrase=bestPhrase,
                    ),
                )
            )

        if not hits:
            return KpiMatchRoute(kind="none")

        hits.sort(key=lambda item: (-item[0], item[1].code))
        topLength = hits[0][0]
        top = tuple(match for length, match in hits if length == topLength)
        if len(top) == 1:
            return KpiMatchRoute(kind="hit", matches=top)
        # 等长跨 KPI 并列：不猜，交给澄清。hits 已按 code 升序。
        return KpiMatchRoute(kind="ambiguous", matches=top)
```

> 删掉 docstring 里误写的 ``` 行的笔误（上面示意里的围栏只是引用标记，落地时不要带）。

- [ ] **Step 4 — GREEN**：重跑 Step 2，全绿。
- [ ] **Step 5 — 回归**：`pytest app/tests/unit/test_kpi_semantic_match_service.py app/tests/unit/test_kpi_match_route.py -q`
  ⇒ v1 的 20 个用例必须保持全绿（`match`/`matchAll` 未改动）。
- [ ] **Step 6 — 提交**

```bash
git add app/services/kpi_semantic_match_service.py app/tests/unit/test_kpi_match_route.py
git commit -m "feat(kpi): 匹配器 v2 包含式路由（最长短语胜出 + 等长歧义）"
```

---

### Task D2（第 2 天）：缓存补 metric_alias + 灰度开关

**Files:**
- Modify: `app/services/kpi_match_cache.py`
- Modify: `app/services/kpi_metric_switches.py`
- Modify: `app/config.py`
- Test: `app/tests/unit/test_kpi_match_cache.py`（追加，真实 PG）
- Test: `app/tests/unit/test_kpi_metric_switches.py`（追加，若无此文件则新建）

**Interfaces:**
- Consumes: `KpiCatalog.metric_id`、`OntologyMetric.metric_alias`（`String(100)` nullable）
- Produces:
  - `KpiMatchCache.metricAliasOf(code: str) -> str | None`
  - `SWITCH_MATCH_CONTAINMENT = "KPI_MATCH_CONTAINMENT_ENABLED"`
  - `async isKpiMatchContainmentEnabled(session: AsyncSession) -> bool`
  - `Settings.kpiMatchContainmentEnabled: bool`（env alias 同名，默认 True）

**为什么必须外连接**：`KpiCatalog` 没有 `metric_alias` 列（别名在 `OntologyMetric`）。
`warmUp` 目前 `select(KpiCatalog)` 单表，拿不到别名；改成 `select(KpiCatalog, OntologyMetric.metric_alias)`
outer join。**必须 outer**：种子/引导 KPI 的 `metric_id` 为 NULL，inner join 会把它们整批漏出缓存。

- [ ] **Step 1 — 写失败测试**（追加到 `app/tests/unit/test_kpi_match_cache.py`）

```python
async def test_metricAliasOf_returns_alias_of_linked_metric(dbSession: AsyncSession) -> None:
    """KPI 挂靠 metric 后，metric_alias 必须能从缓存读到（v2 第三短语源）。"""
    from app.domain.models import KpiCatalog, OntologyMetric

    metric = OntologyMetric(
        metric_name="cache-alias-metric",
        metric_alias="准时交货",
        formula="1",
        agg_function="SUM",
        metric_type="MEASURE",
        created_by="p1-test",
    )
    dbSession.add(metric)
    await dbSession.commit()

    dbSession.add(
        KpiCatalog(
            kpi_code="KPI_P1_ALIAS",
            kpi_name="别名测试指标",
            status="PUBLISHED",
            metric_id=metric.id,
            semantic_keywords=[],
            match_threshold=Decimal("0.75"),
        )
    )
    await dbSession.commit()

    cache = KpiMatchCache()
    await cache.warmUp(dbSession)
    assert cache.metricAliasOf("KPI_P1_ALIAS") == "准时交货"


async def test_metricAliasOf_is_none_for_unlinked_kpi(dbSession: AsyncSession) -> None:
    """metric_id 为 NULL 的引导 KPI 仍必须进缓存（outer join，不是 inner）。"""
    from app.domain.models import KpiCatalog

    dbSession.add(
        KpiCatalog(
            kpi_code="KPI_P1_NO_METRIC",
            kpi_name="无 metric 指标",
            status="PUBLISHED",
            metric_id=None,
            semantic_keywords=[],
            match_threshold=Decimal("0.75"),
        )
    )
    await dbSession.commit()

    cache = KpiMatchCache()
    await cache.warmUp(dbSession)
    assert cache.metricAliasOf("KPI_P1_NO_METRIC") is None
    # 反证：整行没有被 inner join 掉
    assert "KPI_P1_NO_METRIC" in {kpi.kpi_code for kpi in cache.getAll()}


async def test_metricAliasOf_is_none_when_cold() -> None:
    """冷缓存不做隐式加载，返 None（调用方由 ensureFresh 负责预热）。"""
    assert KpiMatchCache().metricAliasOf("ANY") is None


async def test_refreshOne_updates_alias(dbSession: AsyncSession) -> None:
    """改挂 metric / 改别名后定向刷新要跟上（否则缓存长期停在旧别名）。"""
    from app.domain.models import KpiCatalog, OntologyMetric

    metricA = OntologyMetric(metric_name="alias-a", metric_alias="旧别名", formula="1",
                             agg_function="SUM", metric_type="MEASURE", created_by="p1-test")
    metricB = OntologyMetric(metric_name="alias-b", metric_alias="新别名", formula="1",
                             agg_function="SUM", metric_type="MEASURE", created_by="p1-test")
    dbSession.add_all([metricA, metricB])
    await dbSession.commit()

    kpi = KpiCatalog(kpi_code="KPI_P1_REFRESH", kpi_name="刷新指标", status="PUBLISHED",
                     metric_id=metricA.id, semantic_keywords=[], match_threshold=Decimal("0.75"))
    dbSession.add(kpi)
    await dbSession.commit()

    cache = KpiMatchCache()
    await cache.warmUp(dbSession)
    assert cache.metricAliasOf("KPI_P1_REFRESH") == "旧别名"

    kpi.metric_id = metricB.id
    await dbSession.commit()
    await cache.refreshOne(dbSession, "KPI_P1_REFRESH", kpiId=kpi.id)
    assert cache.metricAliasOf("KPI_P1_REFRESH") == "新别名"
```

开关测试（新建 `app/tests/unit/test_kpi_metric_switches.py` 或追加到既有文件）：

```python
async def test_match_containment_switch_defaults_off_when_row_missing(
    dbSession: AsyncSession,
) -> None:
    """灰度旋钮 fail-closed：没有 system_config 行 ⇒ False（生产行为不变）。"""
    from app.services.kpi_metric_switches import isKpiMatchContainmentEnabled

    assert await isKpiMatchContainmentEnabled(dbSession) is False


async def test_match_containment_switch_on_when_row_true(dbSession: AsyncSession) -> None:
    from app.models.system_config import SystemConfig
    from app.services.kpi_metric_switches import (
        SWITCH_MATCH_CONTAINMENT,
        isKpiMatchContainmentEnabled,
    )

    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    assert await isKpiMatchContainmentEnabled(dbSession) is True


async def test_match_containment_env_kill_switch_wins(dbSession: AsyncSession, monkeypatch) -> None:
    """env 杀闸优先：行=true 也压不过 env=false。"""
    from app.models.system_config import SystemConfig
    from app.services.kpi_metric_switches import (
        SWITCH_MATCH_CONTAINMENT,
        isKpiMatchContainmentEnabled,
    )

    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    monkeypatch.setenv("KPI_MATCH_CONTAINMENT_ENABLED", "false")
    from app.config import getSettings

    getSettings.cache_clear()  # 若 getSettings 用了 lru_cache；若无则删掉这一行
    try:
        assert await isKpiMatchContainmentEnabled(dbSession) is False
    finally:
        # 还原：缓存清理，避免污染同进程后续用例
        getSettings.cache_clear()
```

> 落地前先看 `app/config.py` 的 `getSettings` 是否带缓存：带 `lru_cache` 就用 `cache_clear()`，
> 不带就直接 `monkeypatch.setenv` 后重新读（实现里每次调用 `getSettings()`，天然生效）。
> 两种形态任选其一，但**必须在 `finally` 里还原**。

- [ ] **Step 2 — 跑 RED**

```bash
pytest app/tests/unit/test_kpi_match_cache.py app/tests/unit/test_kpi_metric_switches.py -q
```
预期：`AttributeError: 'KpiMatchCache' object has no attribute 'metricAliasOf'` + `ImportError: SWITCH_MATCH_CONTAINMENT`。

- [ ] **Step 3 — 实现缓存侧**

`__init__` 增加第三个索引：

```python
        # code → 挂靠 metric 的别名（v2 第三短语源）；与 _by_code 同生命周期重建。
        self._metricAliasByCode: dict[str, str] = {}
```

`getAll` 之后新增：

```python
    def metricAliasOf(self, code: str) -> str | None:
        """KPI 挂靠 metric 的别名；冷缓存或无别名返 None（不隐式加载）。"""
        if not self._loaded:
            return None
        return self._metricAliasByCode.get(code)
```

`warmUp` 改成外连接并填充别名索引：

```python
        from app.domain.models import KpiCatalog, OntologyMetric

        stmt = (
            select(KpiCatalog, OntologyMetric.metric_alias)
            .outerjoin(OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id)
            .where(KpiCatalog.status == KpiStatus.PUBLISHED.value)
        )
        if not _hasPendingKpiChanges(session):
            stmt = stmt.execution_options(populate_existing=True)
        rows = await session.execute(stmt)

        by_code: dict[str, "KpiCatalog"] = {}
        by_keyword: dict[str, list["KpiCatalog"]] = {}
        alias_by_code: dict[str, str] = {}

        for kpi, metric_alias in rows.all():
            if kpi.kpi_code:
                by_code[kpi.kpi_code] = kpi
                if metric_alias:
                    alias_by_code[kpi.kpi_code] = metric_alias

            kws = kpi.semantic_keywords
            if kws:
                for kw in kws:
                    if kw not in by_keyword:
                        by_keyword[kw] = []
                    by_keyword[kw].append(kpi)

        self._by_code = by_code
        self._by_keyword = by_keyword
        self._metricAliasByCode = alias_by_code
```

`onKpiChanged` 全量失效分支补 `self._metricAliasByCode = {}`。

`_removeEntries` 先算出被移除的 code 集合，再据此裁别名索引：

```python
        removedCodes = {
            code
            for code, kpi in self._by_code.items()
            if code == kpi_code or (kpiId is not None and kpi.id == kpiId)
        }
        ...
        self._metricAliasByCode = {
            code: alias
            for code, alias in self._metricAliasByCode.items()
            if code not in removedCodes
        }
```

`refreshOne` 改成同样带别名的单行查询：

```python
        from app.domain.models import KpiCatalog, OntologyMetric

        row = await session.execute(
            select(KpiCatalog, OntologyMetric.metric_alias)
            .outerjoin(OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id)
            .where(KpiCatalog.kpi_code == kpi_code)
        )
        found = row.first()
        if found is None:
            return
        kpi, metric_alias = found
```

并把 PUBLISHED 分支里的 `self._by_code = byCode` 附近补：

```python
            aliasByCode = {
                code: alias
                for code, alias in self._metricAliasByCode.items()
                if code != kpi_code
            }
            if metric_alias:
                aliasByCode[kpi_code] = metric_alias
            self._metricAliasByCode = aliasByCode
```

- [ ] **Step 4 — 实现开关侧**

`app/config.py`（紧跟 `kpiContractInjectionEnabled`）：

```python
    # P1 匹配器 v2 灰度开关：包含式匹配 + 歧义澄清（关闭 = 行为与 P0 完全一致）
    kpiMatchContainmentEnabled: bool = Field(
        default=True, alias="KPI_MATCH_CONTAINMENT_ENABLED"
    )
```

`app/services/kpi_metric_switches.py`：

```python
SWITCH_MATCH_CONTAINMENT = "KPI_MATCH_CONTAINMENT_ENABLED"


async def isKpiMatchContainmentEnabled(session: AsyncSession) -> bool:
    """KPI 匹配器 v2 灰度开关（env 杀闸 + system_config 灰度，fail-closed）。

    与 ``isKpiContractInjectionEnabled`` 完全同构：

    1. env ``KPI_MATCH_CONTAINMENT_ENABLED=false`` ⇒ 一律走 v1 关键字路径
       （进程级、需重启；默认 true，只作「一键止血」）；
    2. system_config 行值为真才启用 v2；**行缺失 / 非法 / 读取异常 ⇒ 关**。

    ⇒ 本批合入后生产行为不变，开 v2 是一次显式的数据操作（设计 §12）。
    """
    if not getSettings().kpiMatchContainmentEnabled:
        logger.debug("KPI 包含式匹配 env 紧急杀闸关闭，回落 v1 关键字路径")
        return False
    return await readKpiMetricSwitch(session, SWITCH_MATCH_CONTAINMENT, False)
```

- [ ] **Step 5 — GREEN**：重跑 Step 2，全绿；再跑
  `pytest app/tests/unit/test_kpi_semantic_match_service.py app/tests/unit/test_kpi_match_route.py -q` 确认无连带。
- [ ] **Step 6 — 提交**

```bash
git add app/services/kpi_match_cache.py app/services/kpi_metric_switches.py app/config.py \
        app/tests/unit/test_kpi_match_cache.py app/tests/unit/test_kpi_metric_switches.py
git commit -m "feat(kpi): 缓存补 metric_alias + 匹配器 v2 灰度开关"
```

---

### Task D3（第 3 天）：preamble 歧义短路 + wire 契约

**Files:**
- Modify: `app/services/chat_constants.py`
- Modify: `app/services/chat_stream_output.py`
- Modify: `app/services/chat_stream_entry.py`
- Modify: `app/services/chat_service.py`
- Modify: `app/domain/schemas.py`
- Test: `app/tests/unit/test_chat_stream_meta_constants.py`（改：功能性重写，不弱化）
- Test: `app/tests/unit/test_chat_preamble_kpi_route.py`（新建）

**Interfaces:**
- Consumes: `matchRoute`、`isKpiMatchContainmentEnabled`、`buildKpiClarifyAnswer`
- Produces:
  - `ROUTING_LAYER_KPI_CLARIFY = "L1C"`（落 `session_message.routing_layer VARCHAR(10)`，3 字符安全）
  - `META_INTENT_CLARIFY = "clarify"`（**是** `IntentType.CLARIFY` 的值，与 KPI/AGENT_RUN 不同）
  - `metaIntentOf(routingLayer: str) -> str`（路由层 → meta 帧 intent 的唯一映射）
  - `PreambleResult.routing_layer: Literal["L1","L4","L1C"]`
  - `ChatResponse.match_method: str | None`

- [ ] **Step 1 — 先改常量与守卫测试**

`chat_constants.py`：

```python
ROUTING_LAYER_L1 = "L1"
...
# KPI 多命中歧义：不执行，返回澄清候选（设计 §5.2）。长度 3，safe for VARCHAR(10)。
ROUTING_LAYER_KPI_CLARIFY = "L1C"
```

meta intent 区追加：

```python
META_INTENT_CLARIFY = "clarify"  # L1C 歧义澄清：复用 IntentType.CLARIFY 的值（前端已知意图）


def metaIntentOf(routingLayer: str) -> str:
    """路由层 → meta 帧 ``intent`` 的唯一映射（SSE wire 契约）。

    新增路由层**必须**在这里显式登记：未登记的值会落到 ``META_INTENT_L4``，
    前端会把它当 Agent 运行来渲染（图表卡/轨迹位全空）。这条兜底是刻意的
    fail-safe，不是「默认值随便挑」。
    """
    if routingLayer == ROUTING_LAYER_KPI_CLARIFY:
        return META_INTENT_CLARIFY
    if routingLayer == ROUTING_LAYER_L1:
        return META_INTENT_L1
    return META_INTENT_L4
```

> 模块 docstring 首句「本模块不承载运行时行为」改成「本模块只承载常量与一个无副作用的纯映射函数
> （`metaIntentOf`）」，避免自相矛盾。

**守卫测试改造**（`test_chat_stream_meta_constants.py`）——把「正则抓三元表达式」换成**更强的功能断言**：

删除 `test_chat_stream_uses_meta_intent_constants_for_l1` /
`..._l1_constant_in_ternary` / `..._l4_constant_in_ternary` /
`test_chat_stream_uses_routing_layer_constant_for_l1_comparison`，替换为：

```python
def test_meta_intent_of_maps_every_routing_layer() -> None:
    """功能守卫：三个路由层的 wire 映射逐条锁定（比正则抓三元表达式更硬）。"""
    assert metaIntentOf(ROUTING_LAYER_L1) == "KPI"
    assert metaIntentOf(ROUTING_LAYER_KPI_CLARIFY) == "clarify"
    assert metaIntentOf(ROUTING_LAYER_L4) == "AGENT_RUN"
    # 未登记的路由层兜底到 AGENT_RUN（fail-safe，不是静默丢帧）
    assert metaIntentOf("L9") == "AGENT_RUN"


def test_meta_intent_of_delegates_to_constants() -> None:
    """映射函数本身不得写死字面量：改常量必须连带改行为。"""
    import app.services.chat_constants as constants

    assert constants.metaIntentOf(constants.ROUTING_LAYER_L1) == constants.META_INTENT_L1
    assert constants.metaIntentOf(constants.ROUTING_LAYER_KPI_CLARIFY) == constants.META_INTENT_CLARIFY
    assert constants.metaIntentOf(constants.ROUTING_LAYER_L4) == constants.META_INTENT_L4


def test_meta_intent_clarify_is_a_real_intent_type_value() -> None:
    """澄清是**真实意图**（与 KPI/AGENT_RUN 这种纯 wire 值不同），前端 KNOWN_INTENTS 已含。"""
    from app.domain.enums import IntentType

    assert META_INTENT_CLARIFY == IntentType.CLARIFY.value == "clarify"


def test_chat_stream_entry_calls_meta_intent_helper() -> None:
    """入口必须走 helper，不得再手写三元表达式。"""
    source = _read_chat_stream_source()
    assert "metaIntentOf(preamble.routing_layer)" in source
    assert "META_INTENT_L1 if" not in source


def test_chat_stream_done_frame_zero_cost_covers_clarify() -> None:
    """done 帧 tokenCostUsd 必须对 L1C 同样记 0（澄清也是 0 LLM 消耗）。"""
    source = _read_chat_stream_source()
    assert "ROUTING_LAYER_KPI_CLARIFY" in source
```

保留原有的 `test_meta_intent_l1_is_legacy_uppercase_kpi` 等常量值断言与
`test_chat_stream_no_raw_uppercase_kpi_string_outside_documentation`。

- [ ] **Step 2 — 写 preamble 失败测试** `app/tests/unit/test_chat_preamble_kpi_route.py`

```python
"""L1 preamble 的 KPI 路由：v1/v2 切换 + 歧义澄清短路（0 LLM）。

用真实 KpiSemanticMatchService + 内存缓存桩，只替换 mixin 的其余依赖
（_buildL1Response / _handleNl2SqlAgent），验证 preamble 的分支选择。
"""

from __future__ import annotations

import pytest

from app.domain.schemas import ChatRequest
from app.services.chat_constants import ROUTING_LAYER_KPI_CLARIFY
from app.services.chat_stream_output import L1L4PreambleMixin
from app.services.kpi_semantic_match_service import KpiSemanticMatchService


class _Kpi:
    def __init__(self, code: str, name: str, keywords: list[str]) -> None:
        self.kpi_code = code
        self.kpi_name = name
        self.semantic_keywords = keywords


class _Cache:
    def __init__(self, kpis: list[_Kpi]) -> None:
        self._kpis = kpis

    def is_warmUp(self) -> bool:
        return True

    def getAll(self) -> list[_Kpi]:
        return list(self._kpis)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._kpis)

    def metricAliasOf(self, code: str) -> str | None:
        return None

    def findByAnyKeyword(self, keywords: list[str]) -> list[_Kpi]:
        return []


class _Preamble(L1L4PreambleMixin):
    def __init__(self, matcher: KpiSemanticMatchService, *, containment: bool) -> None:
        self._kpiMatcher = matcher
        self._containment = containment
        self.l4Called = False

    async def _containmentEnabled(self, session) -> bool:  # 供 _routeKpi 覆写
        return self._containment

    async def _buildL1Response(self, match, session):  # type: ignore[override]
        class _R:
            answer = f"L1:{match.code}"
            chartType = None
            chartOption = None
            visual_rationale = None
            kpi_name = "n"
            kpi_code = match.code
            confidence = match.confidence
        return _R()

    async def _resolveKpiCatalog(self, code, session):  # type: ignore[override]
        return None

    async def _handleNl2SqlAgent(self, session, dto, user):  # type: ignore[override]
        self.l4Called = True
        return None


_KPIS = [
    _Kpi("KPI_SUPPLIER_OTD", "供应商准时交付率", ["准时交付", "交付率"]),
    _Kpi("KPI_SUPPLIER_OVERDUE_RATIO", "供应商逾期率", ["逾期率"]),
]


@pytest.fixture()
def matcher() -> KpiSemanticMatchService:
    return KpiSemanticMatchService(_Cache(_KPIS))


def _dto(question: str) -> ChatRequest:
    return ChatRequest(question=question, sessionId="p1-session", datasourceId=1)


@pytest.mark.asyncio()
async def test_ambiguous_question_returns_clarify_without_l4(matcher) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(_dto("供应商交付率和逾期率分别是多少"), None, None)
    assert preamble is not None
    assert preamble.routing_layer == ROUTING_LAYER_KPI_CLARIFY
    assert "请确认" in preamble.answer
    assert mixin.l4Called is False  # 必须短路，不能落到 Agent Loop


@pytest.mark.asyncio()
async def test_clarify_response_has_zero_tokens_and_candidate_data(matcher) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(_dto("供应商交付率和逾期率分别是多少"), None, None)
    response = preamble.full_response
    assert response.intent == "clarify"
    assert response.tokensUsed == 0
    assert response.cost == 0.0
    assert [item["code"] for item in response.data] == [
        "KPI_SUPPLIER_OTD",
        "KPI_SUPPLIER_OVERDUE_RATIO",
    ]


@pytest.mark.asyncio()
async def test_unique_containment_hit_goes_to_l1(matcher) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(_dto("供应商准时交付率多少"), None, None)
    assert preamble.routing_layer == "L1"


@pytest.mark.asyncio()
async def test_containment_off_uses_legacy_match(matcher) -> None:
    """关灰度 ⇒ 走 v1：歧义问题不澄清（这是生产当前行为，必须可回退）。"""
    mixin = _Preamble(matcher, containment=False)
    preamble = await mixin._runL1AndL4Preamble(_dto("供应商交付率和逾期率分别是多少"), None, None)
    assert preamble is None or preamble.routing_layer != ROUTING_LAYER_KPI_CLARIFY
```

> `_Preamble` 覆写 `_containmentEnabled` 是测试钩子：实现里 `_routeKpi` 调用
> `await self._containmentEnabled(session)`（默认实现读开关）。这样纯单元测试不需要 DB
> 也能覆盖 v1/v2 两条分支。

- [ ] **Step 3 — 跑 RED**：预期 `ImportError: ROUTING_LAYER_KPI_CLARIFY` / `AttributeError: metaIntentOf` /
  preamble 无 `L1C` 分支（走 L4 或 None）。

- [ ] **Step 4 — 实现 preamble**

`chat_stream_output.py` 顶部 import 补：

```python
from app.services.chat_constants import (
    ROUTING_LAYER_KPI_CLARIFY,
    ROUTING_LAYER_L1,
    USAGE_PURPOSE_ANSWER_STREAM_FAILED,
    USAGE_PURPOSE_FALLBACK_ANSWER,
)
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_switches import isKpiMatchContainmentEnabled
from app.services.kpi_semantic_match_service import (
    KpiMatchRoute,
    KpiMatchResult,
    buildKpiClarifyAnswer,
)
```

`PreambleResult.routing_layer`：

```python
    # "L1C" = KPI 多命中歧义澄清（见 ROUTING_LAYER_KPI_CLARIFY）。Literal 不能用常量，
    # 故写字面量；str 值由 chat_constants 单点定义，测试锁定两者一致。
    routing_layer: Literal["L1", "L4", "L1C"]
```

`_runL1AndL4Preamble` 第 1 段整体替换（原 `try/except match = await self._kpiMatcher.match(...)` 那块）：

```python
        # 1. KPI 路由：v2（包含式）开启走 matchRoute；否则回落 v1 关键字匹配。
        #    任何故障（含缓存预热失败）都降级到「无匹配」，不阻断 LLM 流水线。
        route: KpiMatchRoute | None = None
        try:
            route = await self._routeKpi(dto.question, session)
        except Exception:  # noqa: BLE001 — 降级到原 LLM 流水线
            logger.warning("L1 KPI match failed, falling back to LLM", exc_info=True)
            route = None

        if route is not None and route.kind == "ambiguous":
            return self._buildKpiClarifyPreamble(dto.question, route.matches)

        match = route.matches[0] if route is not None and route.kind == "hit" else None
        if match is not None:
            ...（保持原有 L1 构建逻辑不变）
```

新增两个方法（放在 `_runL1AndL4Preamble` 之前）：

```python
    async def _containmentEnabled(self, session: AsyncSession) -> bool:
        """v2 灰度开关判定（单点，测试可覆写以避免 DB 依赖）。"""
        return await isKpiMatchContainmentEnabled(session)

    async def _routeKpi(
        self, question: str, session: AsyncSession
    ) -> KpiMatchRoute:
        """KPI 匹配路由：v2 走 matchRoute（先 ensureFresh），v1 走旧 match()。"""
        if not await self._containmentEnabled(session):
            match = await self._kpiMatcher.match(question)
            if match is None:
                return KpiMatchRoute(kind="none")
            return KpiMatchRoute(kind="hit", matches=(match,))
        # savepoint：ensureFresh 可能写库（TTL 重建），失败不能把 chat 主流程的
        # 事务打成 failed 态（后续语句全 PendingRollbackError）。
        async with session.begin_nested():
            await get_kpi_match_cache().ensureFresh(session)
        return await self._kpiMatcher.matchRoute(question)

    def _buildKpiClarifyPreamble(
        self, question: str, matches: tuple[KpiMatchResult, ...]
    ) -> PreambleResult:
        """多命中 ⇒ 澄清候选（0 LLM、无图表；前端按 intent=clarify 渲染正文）。

        ChatResponse 在这里**延迟导入**：本模块在 ChatResponse 之前被导入，
        模块级 import 会形成循环依赖（与 full_response 的 Any 注解同源问题）。
        """
        from app.domain.enums import IntentType
        from app.domain.schemas import ChatResponse

        answer = buildKpiClarifyAnswer(question, matches)
        response = ChatResponse(
            answer=answer,
            intent=IntentType.CLARIFY.value,
            data=[{"code": m.code, "name": m.kpi_name or m.code} for m in matches],
            tokensUsed=0,
            cost=0.0,
        )
        return PreambleResult(
            routing_layer=ROUTING_LAYER_KPI_CLARIFY,
            answer=answer,
            full_response=response,
        )
```

> `self._kpiMatcher.match` 现在必须 `await`（原实现已 await，保持一致）。

- [ ] **Step 5 — 实现流式入口**

`chat_stream_entry.py`：

```python
from app.services.chat_constants import (
    META_INTENT_L1,
    ROUTING_LAYER_KPI_CLARIFY,
    ROUTING_LAYER_L1,
    metaIntentOf,
)
```
（`META_INTENT_L4` 不再需要直接引用；若别处仍用则保留。）

meta 帧与 done 帧：

```python
            meta_frame = {
                # 路由层→wire intent 的唯一映射（L1→KPI / L1C→clarify / 其余→AGENT_RUN）
                "intent": metaIntentOf(preamble.routing_layer),
                "routingLayer": preamble.routing_layer,
                "datasourceMismatch": preamble.datasource_mismatch,
            }
...
            yield StreamEvent(EVENT_DONE, {
                "routingLayer": preamble.routing_layer,
                # L1 与 L1C 都是 0 LLM 消耗；L4 才由用量审计回填
                "tokenCostUsd": (
                    0.0
                    if preamble.routing_layer in (ROUTING_LAYER_L1, ROUTING_LAYER_KPI_CLARIFY)
                    else None
                ),
            })
```

- [ ] **Step 6 — 实现非流式入口**

`chat_service.py`：

```python
            if preamble.routing_layer in (ROUTING_LAYER_L1, ROUTING_LAYER_KPI_CLARIFY):
                if preamble.routing_layer == ROUTING_LAYER_L1:
                    logger.info(
                        "L1 KPI hit: code=%s confidence=%s",
                        preamble.kpi_code, preamble.confidence,
                    )
                else:
                    logger.info("KPI 匹配歧义：返回澄清候选，未执行 SQL")
                _t0 = time.monotonic()
                await self._storeSessionMessages(
                    session, dto.sessionId, dto.question, preamble.answer, None,
                    routing_layer=preamble.routing_layer,
                    latency_ms=int((time.monotonic() - _t0) * 1000),
                    token_cost_usd=0.0,
                    chart_type=preamble.chart_type,
                    chart_option=preamble.chart_option,
                    visual_rationale=preamble.visual_rationale,
                )
                return requireFullResponse(preamble, preamble.routing_layer)
```

顶部 import 补 `ROUTING_LAYER_KPI_CLARIFY`（与 `ROUTING_LAYER_L1` 同一处）。

- [ ] **Step 7 — `ChatResponse.match_method`**

`app/domain/schemas.py`（紧跟 `confidence`）：

```python
    match_method: str | None = Field(
        default=None,
        description="L1 命中路径：exact_code | keyword | containment（诊断可解释性）",
    )
```

`chat_service._wrapChatResponse` 的 `ChatResponse(...)` 构造补 `match_method=match.method`。

- [ ] **Step 8 — GREEN + 回归**

```bash
pytest app/tests/unit/test_chat_stream_meta_constants.py \
       app/tests/unit/test_chat_preamble_kpi_route.py \
       app/tests/unit/test_kpi_match_route.py -q
pytest app/tests/unit/test_chat_service.py -q          # 既有 L1 用例不能红
```
预期：全绿。`test_chat_service.py` 的 `_FakeSession` 没有 `begin_nested` ⇒ 开关读取异常
⇒ `readKpiMetricSwitch` 返回 False ⇒ 走 v1，行为与今天一致。

- [ ] **Step 9 — 提交**

```bash
git add app/services/chat_constants.py app/services/chat_stream_output.py \
        app/services/chat_stream_entry.py app/services/chat_service.py app/domain/schemas.py \
        app/tests/unit/test_chat_stream_meta_constants.py app/tests/unit/test_chat_preamble_kpi_route.py
git commit -m "feat(chat): KPI 歧义澄清短路（L1C）+ meta wire 映射收敛到 metaIntentOf"
```

---

### Task D4（第 4 天）：运行时 + 诊断统一切到 v2

**Files:**
- Modify: `app/services/kpi_contract_builder.py`（`_matchKpiCodes`）
- Modify: `app/services/kpi_contract_reconcile.py`（B3 `buildKpiContractBlock`）
- Modify: `app/api/v1/kpi_diagnose.py`
- Test: `app/tests/integration/test_kpi_diagnose_api.py`（追加）
- Test: `app/tests/unit/test_kpi_contract_builder.py`（追加，若存在；否则并入诊断集成测试）

**Interfaces:**
- Consumes: `isKpiMatchContainmentEnabled`、`matchRoute`
- Produces: `KpiDiagnoseRead.ambiguous: bool = False`；`KpiDiagnoseMatch.match_method` 新增取值 `"containment"`

**一致性要求**：诊断接口与真实注入路径必须**同一个决策**（P0 D2 的既有不变量）。
v2 开启后，`ambiguous` 必须同时表现为：诊断 `ambiguous=true` + `codes=[]` + 块为空。

- [ ] **Step 1 — 写失败测试**

追加到 `app/tests/integration/test_kpi_diagnose_api.py`：

```python
async def test_diagnose_reports_ambiguous_and_builds_no_block(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """v2 开启 + 等长跨 KPI ⇒ ambiguous=true，不建契约块（不替用户选指标）。"""
    from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT

    await _enableInjectionSwitch(dbSession)
    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    await _seedKpi(dbSession, "KPI_P1_DIAG_A", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["交付率"])
    await _seedKpi(dbSession, "KPI_P1_DIAG_B", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["逾期率"])
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "交付率和逾期率分别是多少"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ambiguous"] is True
    assert sorted(m["kpiCode"] for m in body["matches"]) == ["KPI_P1_DIAG_A", "KPI_P1_DIAG_B"]
    assert body["injectionBlockEmpty"] is True


async def test_diagnose_containment_hit_reports_method(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """唯一包含式命中 ⇒ matchMethod=containment，与 chat 同一路径。"""
    from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT

    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    await _seedKpi(dbSession, "KPI_P1_DIAG_C", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["采购周期"])
    await _warmCache(dbSession)

    resp = await client.get(_DIAGNOSE_URL, params={"question": "采购周期多长"},
                            headers=_ADMIN_HEADERS)
    body = resp.json()
    assert body["ambiguous"] is False
    assert [m["kpiCode"] for m in body["matches"]] == ["KPI_P1_DIAG_C"]
    assert body["matches"][0]["matchMethod"] == "containment"


async def test_diagnose_legacy_when_switch_off(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """灰度关闭 ⇒ 永不澄清（生产当前行为可回退）。"""
    await _seedKpi(dbSession, "KPI_P1_DIAG_D", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["交付率"])
    await _seedKpi(dbSession, "KPI_P1_DIAG_E", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["逾期率"])
    await _warmCache(dbSession)

    resp = await client.get(_DIAGNOSE_URL, params={"question": "交付率和逾期率分别是多少"},
                            headers=_ADMIN_HEADERS)
    assert resp.json()["ambiguous"] is False
```

- [ ] **Step 2 — 跑 RED**：预期 `KeyError: 'ambiguous'` 或 `assert False is True`。

- [ ] **Step 3 — 实现三处切换**

`kpi_contract_builder._matchKpiCodes` 的两个分支合并为一个内部函数（DRY），保持 strict/非 strict 的差异只在异常处理：

```python
async def _matchKpiCodes(
    session: AsyncSession, question: str, limit: int, *, strict: bool = False
) -> list[str]:
    """动态匹配 KPI（v1 Jaccard / v2 包含式，由灰度开关选择）。

    歧义（v2 等长并列）⇒ 返回空列表：契约块会写进 plan prompt，把两个
    互相矛盾的口径同时注入等于让模型替用户选指标 —— 设计 §5.2 明令禁止。
    """
    if not question or not question.strip():
        return []

    async def _match() -> list[str]:
        await get_kpi_match_cache().ensureFresh(session)
        matcher = getKpiSemanticMatchService()
        if await isKpiMatchContainmentEnabled(session):
            route = await matcher.matchRoute(question)
            return [m.code for m in route.matches] if route.kind == "hit" else []
        results = await matcher.matchAll(question, limit=limit)
        return [r.code for r in results]

    if strict:
        async with session.begin_nested():
            return await _match()
    try:
        # savepoint：ensureFresh 可能写库；失败不回流会把调用方事务打成 failed 态。
        async with session.begin_nested():
            return await _match()
    except Exception:
        logger.warning("KPI 语义匹配失败，跳过契约注入", exc_info=True)
        return []
```

`kpi_contract_reconcile.buildKpiContractBlock`（替换 `matchAll` 那三行）：

```python
    matcher = getKpiSemanticMatchService()
    if await isKpiMatchContainmentEnabled(session):
        route = await matcher.matchRoute(question.strip())
        if route.kind != "hit":
            # none 与 ambiguous 都不注入：歧义时 chat 会先反问用户，不会执行 SQL。
            return None
        matches = list(route.matches)
    else:
        matches = await matcher.matchAll(
            question.strip(), limit=_KPI_CONTRACT_MAX_KPIS
        )
    if not matches:
        return None
```
（import 处补 `isKpiMatchContainmentEnabled`。）

`kpi_diagnose.diagnoseKpi`（替换 `matches = await ...matchAll(...)` 那段）：

```python
    matcher = getKpiSemanticMatchService()
    ambiguous = False
    if await isKpiMatchContainmentEnabled(db):
        route = await matcher.matchRoute(question)
        matches = list(route.matches)
        ambiguous = route.kind == "ambiguous"
        # 歧义时不建块：matches 仍全量返回（管理员要看候选），但不据此注入。
        codes = [m.code for m in matches] if route.kind == "hit" else []
    else:
        matches = await matcher.matchAll(question, limit=KPI_CONTRACT_MAX_KPIS)
        codes = [m.code for m in matches]
```

`buildKpiContractBlockWithCodes(..., codes=codes, ...)`；
`KpiDiagnoseRead` 增加字段并回填：

```python
    ambiguous: bool = False
```
```python
    return KpiDiagnoseRead(
        question=question,
        injection_enabled=injectionEnabled,
        injection_block_empty=not block.strip(),
        ambiguous=ambiguous,
        matches=diagnosticMatches,
    )
```
模块 docstring 的「匹配：matchAll 的 code / confidence / 命中路径」改成
「匹配：v2 的 matchRoute（唯一命中 / 歧义候选 / 无命中）或 v1 的 matchAll」。

- [ ] **Step 4 — GREEN + 回归**

```bash
pytest app/tests/integration/test_kpi_diagnose_api.py -q
pytest app/tests/unit/test_kpi_semantic_match_service.py app/tests/unit/test_kpi_match_route.py \
       app/tests/unit/test_kpi_match_cache.py -q
```
预期全绿；`test_kpi_diagnose_api.py` 的 6 条 P0 既有用例不能红。

- [ ] **Step 5 — 提交**

```bash
git add app/services/kpi_contract_builder.py app/services/kpi_contract_reconcile.py \
        app/api/v1/kpi_diagnose.py app/tests/integration/test_kpi_diagnose_api.py
git commit -m "feat(kpi): 运行时与诊断统一按灰度开关切到匹配器 v2"
```

---

### Task D5（第 5 天）：50 条问句回归集（验收闸门）

**Files:**
- Test: `app/tests/integration/test_kpi_match_corpus.py`（新建）

**Interfaces:**
- Consumes: `seedKpiCatalog` / `seedKpiSemanticIndex`（`backend/scripts/`，测试可导入）、
  admin 诊断接口、`KpiMatchCache`
- Produces: 验收证据（写进 D6 的 summary）

**为什么走诊断接口而不是直接调 service**：仓库规则要求「真实 PG + 完整 API 链路」；
诊断接口内部就是运行时同一个 `matchRoute`（D4 已统一），所以链路保真且不重复造判定。

- [ ] **Step 1 — 写 50 条语料与测试**

```python
"""P1 验收：50 条真实问句回归集（设计 §5.3 / §13 M1 前两条）。

真实 PostgreSQL + 完整 HTTP 链路：seed 12 条 PUBLISHED KPI 与语义关键词，
打开 KPI_MATCH_CONTAINMENT_ENABLED 灰度行，重建缓存，再逐条调 admin 诊断接口
（运行时同一个 matchRoute 决策），断言唯一命中 / 歧义集合 / 零命中。

验收口径：
- 正向 36 条：命中率 ≥ 90%（≥ 33 条）；
- 误命中（负向被命中 + 正向命中错 KPI + 歧义集合不符或未判歧义）合计 ≤ 2 条；
- 4 条真实歧义必须 ambiguous 且候选集合精确等于期望（§5.2「不得直接执行」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_switches import (
    SWITCH_CONTRACT_INJECTION,
    SWITCH_MATCH_CONTAINMENT,
)
from scripts.seed_kpi_catalog import seedKpiCatalog
from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

pytestmark = pytest.mark.asyncio

_DIAGNOSE_URL = "/api/v1/admin/kpi/diagnose"
_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_MIN_POSITIVE_HITS = 33   # 36 × 90% 向下取整
_MAX_FALSE_HITS = 2


@dataclass(frozen=True)
class _Case:
    question: str
    expected: tuple[str, ...]  # 唯一命中=1 个 code；歧义=候选全集；负向=()
    kind: str                  # positive | ambiguous | negative


# 12 个 KPI code 常量（与 scripts/seed_kpi_catalog.py 一致）
_OTD = "KPI_SUPPLIER_OTD"
_OVD = "KPI_SUPPLIER_OVERDUE_RATIO"
_CYC = "KPI_PURCHASE_CYCLE_TIME"
_DLY = "KPI_SUPPLIER_DELAY_DAYS"
_DFC = "KPI_SUPPLIER_DEFECT_RATE"
_FPY = "KPI_SUPPLIER_FPY"
_NCR = "KPI_SUPPLIER_NCR_RATE"
_PRV = "KPI_PURCHASE_PRICE_VARIANCE"
_CST = "KPI_COST_SAVING"
_INV = "KPI_INVOICE_MATCH_RATE"
_PAY = "KPI_PAYMENT_ON_TIME"
_MNT = "KPI_SUPPLIER_MAINTENANCE_RATIO"

_CORPUS: tuple[_Case, ...] = (
    # ── 正向 36：唯一命中 ──────────────────────────────────────────────
    _Case("上个月供应商的准时交付率是多少", (_OTD,), "positive"),
    _Case("KPI_SUPPLIER_OTD 这个指标最近三个月怎么样", (_OTD,), "positive"),
    _Case("帮我算一下 OTD", (_OTD,), "positive"),
    _Case("哪些供应商的按时交付做得不好", (_OTD,), "positive"),
    _Case("on time delivery 排名", (_OTD,), "positive"),
    _Case("逾期率最高的五个供应商", (_OVD,), "positive"),
    _Case("本月逾期订单有多少", (_OVD,), "positive"),
    _Case("overdue 订单清单", (_OVD,), "positive"),
    _Case("采购周期平均是多少天", (_CYC,), "positive"),
    _Case("供应商的 lead time 分布", (_CYC,), "positive"),
    _Case("平均延误天数", (_DLY,), "positive"),
    _Case("delay days 是多少", (_DLY,), "positive"),
    _Case("供应商来料不良率趋势", (_DFC,), "positive"),
    _Case("iqc 合格情况", (_DFC,), "positive"),
    _Case("一次合格率", (_FPY,), "positive"),
    _Case("first pass yield 怎么算", (_FPY,), "positive"),
    _Case("NCR 数量", (_NCR,), "positive"),
    _Case("不合格报告的比例", (_NCR,), "positive"),
    _Case("采购价格偏差率", (_PRV,), "positive"),
    _Case("price variance 是多少", (_PRV,), "positive"),
    _Case("今年采购成本节约了多少", (_CST,), "positive"),
    _Case("cost saving 趋势", (_CST,), "positive"),
    _Case("三单匹配率", (_INV,), "positive"),
    _Case("invoice match 失败的原因", (_INV,), "positive"),
    _Case("付款及时率怎么样", (_PAY,), "positive"),
    _Case("账期内的按时付款比例", (_PAY,), "positive"),
    _Case("供应商资质合规率", (_MNT,), "positive"),
    _Case("有多少供应商 certificate 过期", (_MNT,), "positive"),
    _Case("哪些供应商的交付率低", (_OTD,), "positive"),
    _Case("采购交期太长", (_CYC,), "positive"),
    _Case("来料不良的批次数", (_DFC,), "positive"),
    _Case("供应商的缺陷率高不高", (_DFC,), "positive"),
    _Case("逾期天数排行", (_DLY,), "positive"),
    _Case("价格波动的幅度", (_PRV,), "positive"),
    _Case("节支金额", (_CST,), "positive"),
    _Case("供应商合规率", (_MNT,), "positive"),
    # ── 歧义 4：等长跨 KPI，必须反问 ──────────────────────────────────
    _Case("供应商交付率和逾期率分别是多少", (_OTD, _OVD), "ambiguous"),
    _Case("价格偏差和成本节约哪个高", (_PRV, _CST), "ambiguous"),
    _Case("来料不良和来料异常的数量", (_DFC, _NCR), "ambiguous"),
    _Case("交付率、不良率和合规率分别是多少", (_OTD, _DFC, _MNT), "ambiguous"),
    # ── 负向 10：一个都不能命中 ───────────────────────────────────────
    _Case("供应商一共有多少家", (), "negative"),
    _Case("查询本月采购订单数量", (), "negative"),
    _Case("上个月的总采购金额是多少", (), "negative"),
    _Case("物料主数据有多少条", (), "negative"),
    _Case("工厂的产能利用率", (), "negative"),
    _Case("本月新增供应商名单", (), "negative"),
    _Case("订单金额排名前十的供应商", (), "negative"),
    _Case("各工厂的库存周转率", (), "negative"),
    _Case("采购员张三负责哪些订单", (), "negative"),
    _Case("系统里有哪些数据源", (), "negative"),
)


async def _enableSwitch(session: AsyncSession, key: str) -> None:
    session.add(SystemConfig(key=key, value="true"))
    await session.commit()


async def _warmCache(session: AsyncSession) -> None:
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


async def test_corpus_has_fifty_cases() -> None:
    """语料规模守卫：设计 §5.3 要求 50 条，改动语料时不许悄悄缩水。"""
    assert len(_CORPUS) == 50
    assert sum(1 for c in _CORPUS if c.kind == "positive") == 36
    assert sum(1 for c in _CORPUS if c.kind == "ambiguous") == 4
    assert sum(1 for c in _CORPUS if c.kind == "negative") == 10


async def test_fifty_question_corpus_meets_acceptance(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await seedKpiCatalog(dbSession)
    await seedKpiSemanticIndex(dbSession)
    await _enableSwitch(dbSession, SWITCH_MATCH_CONTAINMENT)
    await _warmCache(dbSession)

    positiveHits = 0
    falseHits = 0
    failures: list[str] = []

    for case in _CORPUS:
        resp = await client.get(
            _DIAGNOSE_URL, params={"question": case.question}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200, f"{case.question} → HTTP {resp.status_code}"
        body = resp.json()
        codes = tuple(sorted(m["kpiCode"] for m in body["matches"]))
        expected = tuple(sorted(case.expected))
        isAmbiguous = body["ambiguous"]
        ok = codes == expected and isAmbiguous == (case.kind == "ambiguous")

        if ok and case.kind == "positive":
            positiveHits += 1
        if not ok:
            failures.append(
                f"[{case.kind}] {case.question} → 实际 {codes} ambiguous={isAmbiguous}，"
                f"期望 {expected} ambiguous={case.kind == 'ambiguous'}"
            )
            if case.kind != "positive" or codes:
                # 负向被命中 / 歧义判错 / 正向命中错 KPI 都算误命中；
                # 正向完全没命中只拉低命中率，不算误命中。
                falseHits += 1

    report = (
        f"正向命中 {positiveHits}/36（要求 ≥{_MIN_POSITIVE_HITS}），"
        f"误命中 {falseHits}（要求 ≤{_MAX_FALSE_HITS}）\n" + "\n".join(failures)
    )
    assert positiveHits >= _MIN_POSITIVE_HITS, report
    assert falseHits <= _MAX_FALSE_HITS, report
    assert not failures, report


async def test_kpi_code_question_yields_non_empty_contract_block(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """M1 验收第三条：问句里直接给 KPI code ⇒ 契约块非空。

    这条需要真实挂靠的 metric（seed 的 12 条 metric_id 均为 NULL，契约块必空），
    所以单独构造一条 pass 的 KPI。
    """
    metric = OntologyMetric(
        metric_name="M1-OTD-metric",
        formula=(
            "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) "
            "/ NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) "
            "FROM THBI.DWD_PURCHASE_ORDER_LINE O "
            "JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO"
        ),
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source="ESCAPE_HATCH",
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        created_by="p1-m1",
    )
    dbSession.add(metric)
    await dbSession.commit()
    dbSession.add(
        KpiCatalog(
            kpi_code="KPI_M1_OTD",
            kpi_name="M1 准时交付率",
            status="PUBLISHED",
            metric_id=metric.id,
            numerator_columns=["RECEIPT_DATE"],
            denominator_columns=["PROMISED_RECEIPT_DATE"],
            grain_keys=["SUPPLIER_CODE"],
            semantic_keywords=["准时交付"],
            match_threshold=Decimal("0.75"),
        )
    )
    await dbSession.commit()
    await _enableSwitch(dbSession, SWITCH_CONTRACT_INJECTION)
    await _enableSwitch(dbSession, SWITCH_MATCH_CONTAINMENT)
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_M1_OTD 这个指标怎么样"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"][0]["kpiCode"] == "KPI_M1_OTD"
    assert body["matches"][0]["matchMethod"] == "exact_code"
    assert body["injectionBlockEmpty"] is False
```

- [ ] **Step 2 — 跑 RED**：先只跑语料规模守卫（应通过），再跑验收用例 —— 在 D1–D4 全部落地后它应当直接通过；
  **若整批尚未实现就跑，它必须红**（证明闸门真的会拦）。实施顺序上这一步是「先确认它会红，再确认它转绿」：
  在 D1 之前先跑一次留证据，D4 完成后复跑。

- [ ] **Step 3 — 若个别用例不过**：按失败明细判断是「语料期望错」还是「算法缺陷」。
  禁止为了让用例变绿而改期望值 —— 只允许两种情况改动语料：
  (a) 期望值与 `scripts/seed_kpi_semantic_index.py` 的**实际关键词**不符（抄写错误）；
  (b) 该问句本身就存在真实歧义，且算法判定是对的（此时把 kind 改成 ambiguous 并写清理由）。
  任何改动都要在提交信息里写明原因。

- [ ] **Step 4 — 变异取证（防假绿，必做）**

逐条施加下列变异，**确认对应用例变红**，再还原（用 `git diff` 核对树干净）：

| # | 变异 | 必须变红的用例 |
|---|---|---|
| M1 | `phraseMatchesQuestion` 去掉 ASCII 边界，改成纯 `in` | `test_ascii_phrase_requires_token_boundary` |
| M2 | `matchRoute` 的等长分支改成永远取 `top[0]` | `test_equal_length_cross_kpi_is_ambiguous_sorted_by_code` |
| M3 | `matchRoute` 的 `length > bestLength` 改成 `length <`（最短胜出） | `test_longest_phrase_wins` |
| M4 | `isEligiblePhrase` 的 CJK 阈值 2 改成 1 | `test_single_cjk_char_phrase_is_ineligible` |
| M5 | `_phrasesOf` 不传 alias（只用名称+关键词） | `test_metric_alias_is_a_phrase_source` |
| M6 | 诊断接口 `ambiguous` 恒写 `False` | `test_diagnose_reports_ambiguous_and_builds_no_block` |
| M7 | `warmUp` 的 `outerjoin` 改成 `join` | `test_metricAliasOf_is_none_for_unlinked_kpi` |

把 7 条「变异 → 红」的 node id 与输出摘要贴进 D6 的 `summary.md`。

- [ ] **Step 5 — 提交**

```bash
git add app/tests/integration/test_kpi_match_corpus.py
git commit -m "test(kpi): 50 条问句回归集验收（命中 ≥90%、误命中 ≤2、4 条歧义必反问）"
```

---

### Task D6（第 6 天）：文档、评审与合并闸门

**Files:**
- Create: `Harness/changes/2026-11-11-kpi-match-v2/summary.md`
- Modify: `Harness/wiki/nl2sql-engine.md`（KPI 匹配章节补 v2）
- Modify: `Harness/wiki/kpi-metric-technical-form.md`（匹配链路一节补 v2 + 开关）
- Modify: `Harness/wiki/metric-pipeline.md`（若它描述匹配顺序）

- [ ] **Step 1 — 代码评审（强制）**

```
code-reviewer  → 关注：不可变性、错误处理、文件长度、DRY
security-reviewer → 关注：新增输入路径（用户问句直接进匹配）、无 secrets、无 SQL 注入面
```
把 CRITICAL/HIGH 全部修掉；MEDIUM 记入 summary 的「已知遗留」。

- [ ] **Step 2 — 定向回归（净新增失败 0 的取证）**

```bash
pytest app/tests/unit/test_kpi_match_route.py \
       app/tests/unit/test_kpi_semantic_match_service.py \
       app/tests/unit/test_kpi_match_cache.py \
       app/tests/unit/test_kpi_metric_switches.py \
       app/tests/unit/test_chat_stream_meta_constants.py \
       app/tests/unit/test_chat_preamble_kpi_route.py \
       app/tests/unit/test_chat_service.py \
       app/tests/integration/test_kpi_diagnose_api.py \
       app/tests/integration/test_kpi_match_corpus.py \
       app/tests/integration/test_chat_l1_routing.py -q
```
对 `main`（`33d292f8`）同集合跑一次基线，**失败集合对差必须净新增 0**（不是「全绿」）。

- [ ] **Step 3 — 写变更记录** `summary.md`，必含：
  背景与目标 / 匹配规则表（D-1）/ 开关语义表（D-3）/ 文件清单 /
  50 条语料验收实测数字（正向命中 x/36、误命中 y）/ 7 条变异取证 /
  M1 三条验收逐条对照 / **明确未做**（未部署、未开灰度行、生产仍 v1、
  `/search` 与研究线仍 v1、不删 Jaccard）/ 已知遗留（MEDIUM 清单）。

- [ ] **Step 4 — Wiki 更新**：`nl2sql-engine.md` 的 KPI 匹配小节追加「v2 包含式 + 歧义澄清 + 开关」；
  `kpi-metric-technical-form.md` 的链路图补 `matchRoute` 分支。**只写与代码一致的事实**，
  数字取自 Step 2 实测，不抄设计稿。

- [ ] **Step 5 — 推送与合并闸门**

```bash
HTTPS_PROXY=http://127.0.0.1:12450 git push -u origin feat/kpi-match-v2
HTTPS_PROXY=http://127.0.0.1:12450 gh pr create --base main --title "feat: KPI 匹配器 v2（包含式 + 歧义澄清 + 50 条回归集）" --body-file /tmp/pr-kpi-v2.md
```
合并判据：`merge-gate / failure-set-diff` 通过（失败集合对差净新增 0）。
退出码 5（NEEDS_REVIEW）时按既有仲裁流程（owner `--admin` 合并）处理，并在 PR 里写明仲裁理由。

- [ ] **Step 6 — 合入后不部署**：生产保持 v1（灰度行不存在即 fail-closed）。
  开灰度是单独的一次 `system_config` 数据操作，需要用户事先批准（本批不做）。

---

## 风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| v2 误命中导致 KPI 直答给错指标 | 中 | 最长短语 + 等长澄清 + 50 条语料（误命中 ≤2）；灰度开关默认关，可随时 `false` 全量回退 |
| 缓存 outer join 把冷缓存/待落库改动搞坏 | 中 | `_hasPendingKpiChanges` 条件化逻辑保持不动；新增 `test_metricAliasOf_is_none_for_unlinked_kpi` 反证 inner join 会丢行 |
| clarify 帧让前端出现空白气泡 | 低 | clarify 带 answer 正文，前端渲染走 markdown；`data` 无 `chartType` 不渲染图表（已核 `MessageItem.tsx`） |
| `metaIntentOf` 兜底把新路由层归到 AGENT_RUN | 低 | 函数 docstring 写明必须登记；`test_meta_intent_of_maps_every_routing_layer` 锁三态 |
| 诊断与运行时判定漂移 | 中 | D4 三处共用同一 `matchRoute` + 开关；诊断用例同时断言 `ambiguous` 与块为空 |
| 回归集「调期望值」式假绿 | 中 | D5 Step 3 只允许两类改动且必须写理由；D5 Step 4 的 7 条变异取证是硬闸门 |
| 回滚 | — | `git revert` 本批 5 个提交即可；不涉及 schema，无数据迁移 |

## 完成定义（DoD）

- [ ] D1–D5 全部提交，D6 的评审 CRITICAL/HIGH 清零
- [ ] 50 条语料：正向命中 ≥ 33/36，误命中 ≤ 2，4 条歧义全部 `ambiguous`
- [ ] 7 条变异全部「变红」有取证
- [ ] 定向回归失败集合对差净新增 0
- [ ] `summary.md` 与两份 wiki 更新落库
- [ ] 未部署、未写生产、未开灰度行
