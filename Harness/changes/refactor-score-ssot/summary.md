# 变更：refactor-score-ssot

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 检索与评分（`app/services/vector_similarity.py` + 四个消费方：`embedding_service.py` / `ontology_service.py` / `wiki_vector_service.py` / `rag_service.py`）
- **状态**：done
- **关联变更**：[fix-plan-drop-observability](../fix-plan-drop-observability/summary.md)（同批次）、[chore-chart-service-deadcode](../chore-chart-service-deadcode/summary.md)（同批次）、[fix-sql-guard-side-channel-and-reject-feedback](../fix-sql-guard-side-channel-and-reject-feedback/summary.md)（同批次的上一批）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.2 **H6** + §3 P1 第 8 项（评估日期 2026-09-25）
- **commit**：`82be772`

---

## 1. 需求

`1 / (1 + distance)` 这条「Milvus L2 距离 → 相似度」的换算规则，在四处各写了一遍，**而且口径并不一致**：

| 位置 | `max(0)` | `round(,4)` | 后果 |
|---|---|---|---|
| `embedding_service.py:29-31`（私有 `_distanceToSimilarity`） | 有 | 有 | 基准口径 |
| `ontology_service.py:1198` | 有 | 有 | 与基准一致 |
| `wiki_vector_service.py:264` | 有 | **无** | 同一篇知识条目的 score 精度与其它页面不同（`0.666666…`） |
| `rag_service.py:295-300` | **无** | **无** | **负距离（异常数据）算出 score > 1**；注释还把公式复述了一遍（注释 rot 源） |

用户视角的影响：①文档检索的相似度百分比可能 > 100%（`DocumentsPage` 直接读 `score` 渲染）；②同一向量在知识库页面与文档页面显示不同的相似度；③缺 `distance` 键时直接 `KeyError`，报错位置与语义无关。

**验收标准**：公式只剩一处实现；四处调用点复用同一函数；负距离被 clamp 到 [0,1]；边界值（`0 / 1 / 3 / 负值 / Decimal`）有断言；有源码级守卫禁止再出现内联公式。

## 2. 设计评审

### 候选方案

| # | 方案 | 优点 | 否决理由 |
|---|---|---|---|
| A | 保留四处实现，就地逐处修正（补 `max(0)` / 补 `round`） | diff 最小 | **正是本缺陷的成因**。四份拷贝已经漂移出三种口径，修完仍然会继续漂移；且「补了没有」无法被机器检查 —— 只能靠人读，而这次的漂移正是人读漏的 |
| B | **新增 `app/services/vector_similarity.py` 单源函数，四处调用点复用**（选定） | 单点可测、可被源码断言守卫、口径变更只改一处 | 多一个文件（16 行），可接受 |
| C | 在 `milvus_client` 出口统一换算，让 hit 直接带 `score` | 消费方彻底不需要知道公式 | **距离是 Milvus 的原始事实，score 是产品口径**。把口径塞进 infrastructure 会让「想换公式」变成改基础设施契约；且 `distance` 本身仍被消费（wiki/ontology 的前端展示与对账门禁、「缺 distance 即契约违背」的判断），换算下沉反而要同时保留两者 |
| D | 缺 `distance` 时 `.get("distance", 0.0)` 兜底 | 不会抛错 | **否决（用户口径）**：`0.0` 距离会被换算成 `score = 1.0`，即「完美命中」，静默污染排序 —— 比抛错更糟。缺失是上游契约违背，保持 `h["distance"]` 硬下标**快速失败** |

### 最终决定

方案 B + D 的硬下标口径。实现体直接沿用 `embedding_service.py:29-31` 那份**已在生产运行**的措辞与口径（不重新发明），私有 `_distanceToSimilarity` 删除，改为公共函数。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本（`迁移版本：无`）。

## 4. 接口契约变更

字段名与类型不变，只有**数值口径**变化两处：

| 端点/字段 | 变更 | 影响 |
|---|---|---|
| wiki 语义检索（`GET /api/v1/wiki/pages/semantic-search` → `score`） | 增加 4 位小数归一：`0.666666…` → `0.6667` | 前端按 `toFixed(1) * 100` 渲染百分比 ⇒ **显示完全不变**（无前端改动） |
| 文档检索（`GET /api/v1/documents/search` → `score`） | 增加负距离 clamp 与 4 位归一 | 正常数据（距离 ≥ 0）下**逐位相同**；仅在异常负距离时由「> 1」变为 `1.0`（消除 >100% 的展示） |
| 本体语义检索（`ontology_service` 内部 `score=`） | 等价替换（原口径即基准） | 无变化 |

**未变的契约**：`distance` 仍是 Milvus 原始距离；缺 `distance` 键仍然抛 `KeyError`（有意）。

## 5. 实现要点

新增 `app/services/vector_similarity.py`（全文件 16 行）：

```python
def distanceToSimilarity(distance: float) -> float:
    """Milvus L2 距离 → [0,1] 相似度（距离越小越相似；负数按 0 处理防除零）。"""
    return round(1.0 / (1.0 + max(float(distance), 0.0)), 4)
```

调用点替换（四处，全部改为 `similarity=`/`score=distanceToSimilarity(h["distance"])`）：

| 文件 | 位置 | 改动 |
|---|---|---|
| `services/embedding_service.py` | 定义 `:29-31` → 删除；调用 `:131` | 私有函数删除，改 import |
| `services/ontology_service.py` | `:1199` | 内联公式 → 函数调用 |
| `services/wiki_vector_service.py` | `:265` | 内联公式（无 round）→ 函数调用；注释改为指向单源文件 |
| `services/rag_service.py` | `:298` | 内联公式（无 clamp / 无 round）+ 复述公式的长注释 → 函数调用 + 一行指针注释 |

依赖注入点：无（纯函数，无 DI 变更）。

## 6. 测试

`app/tests/unit/test_vector_similarity.py`（新增，6 个测试函数；边界 9 组 + 非有限 3 组参数化 ⇒ 收集 16 例）：

- 边界参数化：`(0, 1.0) (0.0, 1.0) (1.0, 0.5) (-0.5, 1.0) (-1.0, 1.0) (3.0, 0.25) (Decimal("3"), 0.25) (9.0, 0.1) (1000.0, 0.001)`；
- 4 位小数归一断言（`1/3 → 0.3333`）；
- **非有限输入**（`nan` / `inf` / `-inf` → `0.0`，审查整改项 3，见 §7）；
- **源码契约三条（反复发闸门）**：四个调用点文件必须出现 `distanceToSimilarity`；四个调用点不得再出现 `"1.0 / (1.0 +"` 内联公式；`embedding_service` 不得再有 `_distanceToSimilarity`。

回归：`test_embedding_service` / `test_rag_service` / `test_wiki_vector_service` / `test_ontology_*` 全绿；集成切片（本体语义检索、wiki 语义检索、文档检索、chat 状态）除一条**预存 Milvus 可见性 flake**（见 §8）外全绿。

覆盖率：新增文件为纯函数，分支被参数化用例全覆盖（含非有限分支）；本批不涉及未覆盖新增分支。

## 7. 安全审查

**触发条件**：不涉及认证、密钥、SQL、用户输入解析、文件操作、加解密、支付；本批是**数值口径收敛**，故不触发 security-reviewer 的强制项。仍按流程送审（与 M3/M6 同批），结论与整改见下。

### 审查结论与整改（2026-09-26，两位审查独立送审）

| 审查 | 结论 | 本批处置 |
|---|---|---|
| code-reviewer | APPROVE（0 CRITICAL / 0 HIGH / 2 MEDIUM / 4 LOW） | 本文件相关：H6 正确性**无问题**（四个调用点口径收敛无误、无残留内联公式、无下游 score 阈值逻辑、`int/float/Decimal` 均正确、相关测试用 `pytest.approx` 不会因 round 断裂） |
| security-reviewer | 无 CRITICAL / 无 HIGH；2 MEDIUM | **MEDIUM-2（NaN/±inf 穿透）当场修**（见下），其余 MEDIUM 属 M3，见 `../fix-plan-drop-observability/summary.md` |

**整改：非有限输入不再穿透值域（security MEDIUM-2）**

原实现 `max(nan, 0.0)` 在 Python 中返回 `nan`（`max` 依比较顺序返回第一个参数）⇒ 文档宣称的「映射到 [0,1]」被 `NaN` 违约：前端渲染 `NaN%`，且含 `NaN` 的排序顺序不确定（可能把不相关文档排到前排）；`-inf` 则被算成 `1.0`，即把「无限远」当成「完全相同」（假完美命中）。原评估把它记为「当前不可达的已知边界」，但**单源函数正是加防的最佳位置**（四个调用点从此一次性获得保护），故按 TDD 整改：

- **RED**：新增 `test_non_finite_distance_maps_to_least_similar`（3 组参数化）→ `nan` / `-inf` 两例转红（`inf` 本就算出 `0.0`）；
- **GREEN**：入口 `if not math.isfinite(value): return 0.0` —— 取「最不相似」，既不猜一个"更好"的值，也不抛错中断整条检索；
- 模块 docstring 同步声明该口径。

**保留项（有意，非遗漏）**

- 缺 `distance` 键保持 `KeyError`：`0.0` 距离会被换算成 `score = 1.0`（假完美命中）并静默污染排序，比抛错更糟。缺失是上游契约违背，故硬下标**快速失败**（见 §2 方案 D，用户口径）。容器内探针已实证仍为 `KeyError`。

## 8. 部署验证（2026-09-26）

- **镜像重建**（非 `docker cp`）：`docker compose build backend && docker compose up -d backend`，容器 `Up`、`/api/v1/health` 直连返回 200；
- **「容器在跑新代码」是镜像级证据**（不依赖 cp 残留）：用容器镜像 `sha256:b6694892e89e8350f56cbd77ca3dfaaf111336c2522593ef3cd1989a9ac81cfd` 起一次性容器读取文件——

  ```
  $ docker run --rm --entrypoint sh <image> -c 'ls -l app/services/vector_similarity.py; md5sum app/domain/query_plan.py'
  -rw-r--r-- 1 root root 1430 Sep 26 14:53 app/services/vector_similarity.py
  6225dfff3d96c6cb8f9f41640d755936  app/domain/query_plan.py      # 与仓库 md5 一致
  ```

- **仓库 ↔ 容器逐文件 md5**：本批 12 个文件（含新增 3 个）**12/12 MATCH**；
- **容器内真机探针**（真实模块，非测试替身）**21 项全 PASS**，本变更相关项：

  ```
  PASS  H6 d=-1 -> 1.0 / d=0 -> 1.0 / d=1 -> 0.5 / d=3 -> 0.25
  PASS  H6 nan -> 0.0 / -inf -> 0.0
  PASS  H6 embedding_service|ontology_service|wiki_vector_service|rag_service 复用单源 + 无内联公式（8 项）
  PROBE PASS
  ```

- **网关**：`localhost:8000/api/v1/health` = 200；经 nginx `localhost:5173/api/v1/health` = 200；
- **测试**：
  - 全量 unit（最终 hash `3309a71`）`2 failed, 2411 passed, 1 skipped`；两条失败与 §13 批次记录的**预存失败同名同因**（`test_chat_service.py::TestSearchByKeywordAdsWeighting::test_weight_from_system_config_db_value`、`test_dependencies.py::test_stub_disabled_raises_permission_denied`）⇒ delta=0；
  - 集成切片 94 例 `2 failed / 92 passed`：两条是 Milvus 残留累积，**已用基线判别实验证明非本批回归**（见下）；
  - `ruff` 与基线**同集合**（9 个两版皆存在的文件）：基线 46 → 当前 42（**净 -4**：`chart_service.py` 3→0、`query_plan.py` 1→0），新增 5 文件 `All checks passed`。
- **预存失败的判别实验**（不靠推测）：把这两条用例跑在**本批之前的** `7a8ce7d`（分离 worktree，实测该版本仍含 `_distanceToSimilarity` 私有实现 ⇒ 确认是旧代码）上，**旧代码同样失败**：

  ```
  E  AssertionError: assert 5 == 1        # 计数比本批那一轮（4）又多 1
  1 failed, 1 passed
  ```

  计数按「每跑一次 +1」单调增长（3 → 4 → 5），同一批业务 id 每次生成新自增 id ⇒ 原因是**残留累积 + Milvus 删除可见性滞后 + 用例不自清理**，与 score 公式无关（本批只改 score 数值口径，不写入 Milvus）。
- ⚠️ 前置：全量 unit 会 truncate 测试库，故顺序为**先集成切片、再全量 unit**；如需恢复，`DROP SCHEMA public CASCADE` + `DATABASE_URL=<test 库>` alembic upgrade head（`env.py` 只认 `Settings.databaseUrl`，`.env` 指向 **prod**）。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.2 H6、§3 P1 第 8 项、新增 §14 批次记录（本次标 ✅）
- 机制文档：`Harness/wiki/nl2sql-engine.md`（M3 的计划解析契约，同批次）
- 代码契约：`app/services/vector_similarity.py`（单源）、`app/tests/unit/test_vector_similarity.py`（源码守卫）
- 规则：根 `CLAUDE.md` 核心约束 #5（小文件）、`~/.claude/rules/common/coding-style.md`（DRY / KISS）、`Harness/rules/开发流程规范.md`（TDD + 双审）
- Memory：`qa-system-score-ssot-plan-drop.md`（新增）
- 关联变更：`../fix-plan-drop-observability/summary.md`、`../chore-chart-service-deadcode/summary.md`

## SSOT 校验清单

- [x] 第 1 段 需求 = 四处口径漂移的实测表（含后果）
- [x] 第 2 段 4 个候选方案对比（含方案 D = 用户口径「保持 `KeyError`」，附否决理由）
- [x] 第 3 段 无 alembic 迁移（纯函数收敛，无表变更）
- [x] 第 4 段 接口契约变更两处 + 明确「`distance` 缺失仍抛 `KeyError`」为未变契约
- [x] 第 5 段 实现要点含调用点替换表（4 处 file:line）
- [x] 第 6 段 测试：边界 9 组 + 非有限 3 组 + 源码守卫 3 条（RED/GREEN 留证）
- [x] 第 7 段 安全审查：未触发强制项 + 两位审查结论表 + MEDIUM-2 整改（非有限输入）
- [x] 第 8 段 部署验证：镜像级证据 + 12/12 md5 + 21 项探针 + 网关 200 + 预存失败判别实验
- [x] 第 9 段 跨文件链接（评估文档 §14 / 机制文档 / 代码契约 / 规则 / Memory / 关联变更）
- [x] `Harness/wiki/chat-service-assessment.md` §2.2 H6 标 ✅ + §14 批次记录
