# 治理写入口收口（词条字典 + 本体指标）实施计划

- 分支：`fix/governance-write-gate`（worktree `/private/tmp/qa-governance-write-gate`）
- 底座：`main@4777bfba`（语义发布 P1b 合并提交）
- **零 DDL、零生产写、不部署**
- 计划粒度：天（每任务 = 1 天，独立可验收、独立提交）

## 一、背景与根因（已实测，非推测）

### 1.1 词条字典：任何登录用户可写，且映射不校验存活本体

`backend/app/api/v1/term_dictionary.py` 的路由只有 `Depends(getCurrentUser)`：

- `POST /api/v1/term-dictionary` —— 任何登录用户可写入；
- `DELETE /api/v1/term-dictionary/{id}` —— 任何登录用户可删除。

而写入内容会**原样进入 NL2SQL 计划阶段的 system prompt**（`renderDictionaryText`），
属于治理数据。前端 `TermDictionaryButton` / `TermDictionaryManager` 对全部用户可见。

`TermDictionaryService.createTerm`（`backend/app/services/term_dictionary_service.py:51`）
只查 `term` 重名，**完全不校验** `mapped_class_name` / `mapped_property_name`：

- 类名是裸字符串（`TermDictionary` 模型注释：class_name 版本化不唯一 ⇒ 无 FK）；
- 本体 tombstone / 拆表后词条不会跟着走。

**已发生的真实事故**（MEMORY `qa-system-term-dictionary-drift`）：「供货量」词条曾映射到
已 tombstone 的 `DWD_GOODS_RECEIPT_LINE`，同一问题因问法不同落到不同表（第三套结果）。

### 1.2 本体指标：写入口无任何角色门禁（2026-11-05 N3 遗留）

`backend/app/api/v1/ontology.py` 路由级依赖只有 `getCurrentUser`：

| 端点 | 当前守卫 | 说明 |
|---|---|---|
| `POST /ontology/metrics` | 无 | 任何人可创建指标 |
| `PUT /ontology/metrics/{id}` | 无 | 任何人可改指标（含发布状态） |
| `DELETE /ontology/metrics/{id}` | 无 | 任何人可删指标 |

对比：`ontology_class` 的 PUT/DELETE/restore 已走 owner-based ACL（`AclService.assertCanModify`），
但 `ontology_metric` **没有 `object_owner` 列**，无法套用同一套 ACL ⇒ 本批用 admin 门禁。

指标驱动全部 NL2SQL 口径，且 `PUT` 可改 `status`（发布闸门落点）⇒ 越权面比类更大。

### 1.3 不在本批范围（明确边界）

- 不收紧 ontology_class / property / join / relation 的写入口（类已有 owner ACL，其余留待单独评估）。
- 不改 stub 鉴权 fail-soft 策略（跨特性治理，MEMORY 已记）。
- 不补「按域版本唯一性」库侧约束、不做语义快照运行期消费（P1c 单独立项）。
- 不改前端菜单权限模型（本批只在组件层按 `roles.includes("admin")` 收敛写操作入口）。

## 二、设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 词条写入口用 `requireAdmin`（与 id_mapping、semantic release 同口径） | 仓库既有的治理写入口模式；不引入新权限模型 |
| D2 | 引用完整性在 **service 层** 校验，不在 router | 任何调用方（含未来脚本/内部服务）都必须受同一约束；router 只管鉴权 |
| D3 | `mapped_class_name` 非空 ⇒ 必须存在**且存活**（`valid_to IS NULL`）；`mapped_property_name` 非空 ⇒ 必须属于该类 | 事故形态正是「死类 + 属性不存在」 |
| D4 | 只给 `mapped_property_name` 不给类 ⇒ 422 | 属性挂在类上，脱离类无法校验；静默放行等于回到旧缺陷 |
| D5 | 两个字段都为空 ⇒ 允许（自由文本词条） | 术语词典既有用途包含「只解释业务习惯用语」，不能一刀切 |
| D6 | 类不存在与类已下线给**不同**错误文案 | 运维要能区分「拼错类名」与「类被下线」（后者需要补映射，不是改拼写） |
| D7 | 指标写入口用 admin 门禁，读接口不动 | 无 `object_owner` 列，owner ACL 不可用；读路径（NL2SQL 召回）不受影响 |
| D8 | 前端只在**写操作入口**收敛，列表/查看保留 | 非 admin 仍可看词典与指标；只是不能改 |

## 三、任务分解

### Task 1（第 1 天）—— 词条字典写入契约：service 校验 + admin 门禁

**Files**
- Modify: `backend/app/services/messages_zh.py`（+4 条中文错误）
- Modify: `backend/app/services/term_dictionary_service.py`
- Modify: `backend/app/api/v1/term_dictionary.py`
- Test: `backend/app/tests/integration/test_term_dictionary_write_gate.py`（新建）
- Modify: `backend/app/tests/integration/test_term_dictionary_api.py`（改用 admin 头 + 种活类）
- Modify: `backend/app/tests/integration/test_term_dictionary_supply_qty.py`（补 `DIM_SUPPLIER` 种子）

**Interfaces**
- Consumes: `OntologyClass`（`class_name`, `valid_to`）、`OntologyProperty`（`class_id`, `property_name`）
- Produces: `TermDictionaryService.createTerm` 新增前置校验（无签名变化）；
  新消息常量 `MSG_TERM_DICT_CLASS_NOT_FOUND` / `MSG_TERM_DICT_CLASS_EXPIRED` /
  `MSG_TERM_DICT_PROPERTY_NOT_FOUND` / `MSG_TERM_DICT_PROPERTY_REQUIRES_CLASS`

**Step 1 — RED：新建 `test_term_dictionary_write_gate.py`**

覆盖（真实 PG `qa_metadata_test` @5434 + 完整 API 链路）：

```python
"""词条字典写入口：admin 门禁 + 映射引用完整性（真实 PG + HTTP 链路）。

背景：POST/DELETE 原先只有 getCurrentUser；createTerm 不校验映射类是否存活
（「供货量」曾映射到 tombstone 类 DWD_GOODS_RECEIPT_LINE）。本文件钉住两者。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}


async def _seedClass(
    session: AsyncSession,
    className: str,
    *,
    expired: bool = False,
    propertyNames: tuple[str, ...] = (),
) -> int:
    from datetime import datetime, timezone

    cls = OntologyClass(
        class_name=className,
        source_table=f"T_{className}",
        valid_to=datetime(2020, 1, 1, tzinfo=timezone.utc) if expired else None,
        properties=[
            OntologyProperty(property_name=name, data_type="STRING") for name in propertyNames
        ],
    )
    session.add(cls)
    await session.commit()
    await session.refresh(cls)
    return cls.id


def _payload(**overrides: object) -> dict:
    base = {"term": "占比", "definition": "某值占总量的比例"}
    return {**base, **overrides}


async def test_non_admin_cannot_create_term(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/term-dictionary", json=_payload(), headers=USER_HEADERS)
    assert resp.status_code == 403


async def test_non_admin_cannot_delete_term(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/term-dictionary", json=_payload(), headers=ADMIN_HEADERS
    )
    assert created.status_code == 201
    resp = await client.delete(
        f"/api/v1/term-dictionary/{created.json()['id']}", headers=USER_HEADERS
    )
    assert resp.status_code == 403


async def test_admin_can_create_and_delete_without_mapping(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/term-dictionary", json=_payload(), headers=ADMIN_HEADERS
    )
    assert created.status_code == 201
    resp = await client.delete(
        f"/api/v1/term-dictionary/{created.json()['id']}", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 204


async def test_mapping_to_missing_class_is_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="NOT_EXIST_CLASS"),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422
    assert "不存在" in resp.json()["error"]


async def test_mapping_to_expired_class_is_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "DEAD_CLASS", expired=True)
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="DEAD_CLASS"),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422
    assert "已下线" in resp.json()["error"]


async def test_mapping_to_live_class_is_accepted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "LIVE_CLASS", propertyNames=("收货数量",))
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="LIVE_CLASS", mappedPropertyName="收货数量"),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201


async def test_property_not_on_class_is_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "LIVE_CLASS2", propertyNames=("其他属性",))
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="LIVE_CLASS2", mappedPropertyName="不存在的属性"),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422
    assert "不属于" in resp.json()["error"]


async def test_property_without_class_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedPropertyName="收货数量"),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422
    assert "同时指定" in resp.json()["error"]
```

**Step 2 — 跑 RED**（预期：门禁 2 条、引用 4 条失败；无映射 1 条通过）

```bash
cd /private/tmp/qa-governance-write-gate/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_term_dictionary_write_gate.py -q
```

**Step 3 — GREEN**

`messages_zh.py`（紧跟现有 `MSG_TERM_DICT_*`）：

```python
MSG_TERM_DICT_CLASS_NOT_FOUND = "术语映射的本体类不存在：{className}"
MSG_TERM_DICT_CLASS_EXPIRED = "术语映射的本体类已下线（valid_to 非空）：{className}"
MSG_TERM_DICT_PROPERTY_NOT_FOUND = "术语映射的属性「{propertyName}」不属于本体类 {className}"
MSG_TERM_DICT_PROPERTY_REQUIRES_CLASS = "映射属性时必须同时指定映射类（mappedClassName）"
```

`term_dictionary_service.py`：

```python
from app.domain.models import OntologyClass, OntologyProperty, TermDictionary

async def _validateMappings(self, session: AsyncSession, dto: TermDictionaryCreate) -> None:
    """写入前校验映射指向的类/属性存活（D2/D3/D4/D5）。"""
    className = dto.mapped_class_name
    propertyName = dto.mapped_property_name
    if not className:
        if propertyName:
            raise ValidationError(MSG_TERM_DICT_PROPERTY_REQUIRES_CLASS)
        return
    liveIds = list(
        (
            await session.execute(
                select(OntologyClass.id).where(
                    OntologyClass.class_name == className,
                    OntologyClass.valid_to.is_(None),
                )
            )
        ).scalars()
    )
    if not liveIds:
        anyRow = (
            await session.execute(
                select(OntologyClass.id).where(OntologyClass.class_name == className)
            )
        ).first()
        raise ValidationError(
            (MSG_TERM_DICT_CLASS_EXPIRED if anyRow else MSG_TERM_DICT_CLASS_NOT_FOUND).format(
                className=className
            )
        )
    if not propertyName:
        return
    matched = (
        await session.execute(
            select(OntologyProperty.id).where(
                OntologyProperty.class_id.in_(liveIds),
                OntologyProperty.property_name == propertyName,
            )
        )
    ).first()
    if matched is None:
        raise ValidationError(
            MSG_TERM_DICT_PROPERTY_NOT_FOUND.format(
                propertyName=propertyName, className=className
            )
        )
```

`createTerm` 首行（重名检查之前）调用 `await self._validateMappings(session, dto)`。

`term_dictionary.py`：

```python
from app.dependencies import CurrentUser, getCurrentUser, getDb, requireAdmin

@router.post(...)
async def createTerm(
    dto: TermDictionaryCreate,
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> TermDictionaryRead: ...

@router.delete("/{id}", ...)
async def deleteTerm(
    id: int,
    _user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> None: ...
```

**Step 4 — 修既有用例**

- `test_term_dictionary_api.py`：所有 POST/DELETE 改带 `ADMIN_HEADERS`；
  类 `PRECEIPTD` 与属性 `收货数量` 在用例内先经 DB 种子（`dbSession`）或改用无映射 payload。
  **优先**：把映射断言改为「先种子活类 → 再建词条」，保留原断言强度。
- `test_term_dictionary_supply_qty.py`：`_seed` 补 `DIM_SUPPLIER`（含 `BPSNUM_0` 属性）——
  现有 TERMS 里有 `DIM_SUPPLIER.BPSNUM_0` 映射，若不补种子会在种词条时 422。
- `test_term_dictionary_inject.py`：已种 `SupplierPriceDetail` 且属性名匹配（`单价`），
  预期无需改动；若属性名不匹配，按实际种子修正。

**Step 5 — 验证**

```bash
pytest app/tests/integration/test_term_dictionary_write_gate.py \
       app/tests/integration/test_term_dictionary_api.py \
       app/tests/integration/test_term_dictionary_inject.py \
       app/tests/integration/test_term_dictionary_supply_qty.py \
       app/tests/unit/test_term_dictionary.py -q
```

预期全绿；再用变异取证（M1：删掉 `_validateMappings` 调用 ⇒ 引用 4 条红；
M2：删掉 `requireAdmin` ⇒ 门禁 2 条红），取证后还原。

**Step 6 — 提交**

```
fix: 词条字典写入口加 admin 门禁 + 映射引用完整性校验
```

---

### Task 2（第 2 天）—— 本体指标写入口 admin 门禁

**Files**
- Modify: `backend/app/api/v1/ontology.py`（metrics 三条写路由）
- Test: `backend/app/tests/integration/test_ontology_metric_write_gate.py`（新建）

**Step 1 — RED**

```python
"""本体指标写入口 admin 门禁（2026-11-05 N3 遗留）。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}
_METRIC = {"metricName": "gated_metric", "formula": "COUNT(id)", "aggFunction": "COUNT"}


async def test_non_admin_cannot_create_metric(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/ontology/metrics", json=_METRIC, headers=USER_HEADERS)
    assert resp.status_code == 403


async def test_non_admin_cannot_update_metric(client: AsyncClient) -> None:
    created = await client.post("/api/v1/ontology/metrics", json=_METRIC, headers=ADMIN_HEADERS)
    assert created.status_code == 201
    resp = await client.put(
        f"/api/v1/ontology/metrics/{created.json()['id']}",
        json={"metricAlias": "改名"},
        headers=USER_HEADERS,
    )
    assert resp.status_code == 403


async def test_non_admin_cannot_delete_metric(client: AsyncClient) -> None:
    created = await client.post("/api/v1/ontology/metrics", json=_METRIC, headers=ADMIN_HEADERS)
    assert created.status_code == 201
    resp = await client.delete(
        f"/api/v1/ontology/metrics/{created.json()['id']}", headers=USER_HEADERS
    )
    assert resp.status_code == 403


async def test_non_admin_can_read_metrics(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ontology/metrics", headers=USER_HEADERS)
    assert resp.status_code == 200
```

**Step 2 — 跑 RED**（3 条写用例失败，读用例通过）

**Step 3 — GREEN**：`ontology.py` 三条写路由把 `Depends(getCurrentUser)` 换成
`Depends(requireAdmin)`（保留 `user` 变量名，审计字段继续取 `user.userId`）。

**Step 4 — 验证**：新文件全绿 + 既有指标相关套件（`test_ontology_api.py`、
`test_ontology_audit.py`、`test_kpi_metric_publish_gate_http.py`、
`test_ontology_unified_id.py`）零新增失败（stub 默认角色含 admin，预期无需改）。

**Step 5 — 提交**

```
fix: 本体指标写入口加 admin 门禁（N3 越权面）
```

---

### Task 3（第 3 天）—— 前端写操作入口按角色收敛

**Files**
- Modify: `frontend/src/components/chat/TermDictionaryButton.tsx`
- Modify: `frontend/src/components/chat/TermDictionaryManager.tsx`
- Modify: `frontend/src/components/ontology/MetricTab.tsx`
- Test: `frontend/src/tests/TermDictionaryButton.test.tsx`（补非 admin 用例）
- Test: `frontend/src/tests/TermDictionaryManager.test.tsx`（新建）
- Test: `frontend/src/tests/OntologyPage.test.tsx` 或新建 `MetricTab.test.tsx`（补非 admin 用例）

**Step 1 — RED**：写用例 —— 非 admin 时：
- `TermDictionaryButton` 不渲染「添加到术语词典」；
- `TermDictionaryManager` 删除列不渲染（列表仍渲染）；
- `MetricTab` 新建/编辑/删除按钮不渲染（列表仍渲染）。

**Step 2 — GREEN**：三处统一用

```ts
const user = useAuthStore((s) => s.user);
const isAdmin = (user?.roles ?? []).includes("admin");
```

- `TermDictionaryButton`：`if (!isAdmin) return null;`
- `TermDictionaryManager`：`columns` 里删除列按 `isAdmin ? [...] : []` 构造；
- `MetricTab`：写操作按钮按 `isAdmin` 条件渲染。

**Step 3 — 验证**

```bash
cd frontend
npx tsc -b
npx vitest run src/tests/TermDictionaryButton.test.tsx src/tests/TermDictionaryManager.test.tsx src/tests/OntologyPage.test.tsx
```

**Step 4 — 提交**

```
feat: 词条与指标写操作入口按 admin 角色收敛
```

---

### Task 4（第 4 天）—— 生产装配守卫 + 文档 + 全量回归 + PR

**Files**
- Test: `backend/app/tests/integration/test_governance_write_route_auth.py`（新建）
- Create: `Harness/changes/fix-governance-write-gate/summary.md`
- Modify: `Harness/rules/权限安全.md`（若存在对应条目，补一行「治理写入口清单」）

**Step 1 — 生产 app 路由守卫**

直接构建 `app.main.createApp()`，复用 D11 守卫的树展开逻辑（`_IncludedRouter` +
`inspect.signature` 间接依赖），断言：

| 路由 | 必须声明 |
|---|---|
| `POST /api/v1/ontology/metrics` | `requireAdmin` |
| `PUT /api/v1/ontology/metrics/{id}` | `requireAdmin` |
| `DELETE /api/v1/ontology/metrics/{id}` | `requireAdmin` |
| `POST /api/v1/term-dictionary` | `requireAdmin` |
| `DELETE /api/v1/term-dictionary/{id}` | `requireAdmin` |
| `GET /api/v1/ontology/metrics` | **不得**声明 `requireAdmin`（读路径契约） |
| `GET /api/v1/term-dictionary` | **不得**声明 `requireAdmin` |

反向断言 + 正向对照（include-context 依赖可见性）不得省。
（D11 已把「抽公共路由守卫工具」立项，本批**再新增一份**会加重重复；如时间允许，
优先抽 `app/tests/_route_auth_support.py` 并让三处共用；否则在 summary 的遗留项里
再次点名，并说明第三份重复的理由。）

**Step 2 — 定向回归**

```bash
pytest app/tests/integration/test_term_dictionary*.py \
       app/tests/integration/test_ontology_*.py \
       app/tests/integration/test_kpi_metric_publish_gate_http.py \
       app/tests/integration/test_governance_extension_acl_api.py \
       app/tests/integration/test_ontology_audit.py \
       app/tests/unit/test_term_dictionary.py -q
```

记录 passed/failed 基线；与 main 同套件对差（净新增失败 0 才算通过）。

**Step 3 — 文档**：`summary.md` 写背景、根因、变更清单、评审处置、测试证据、
变异取证、明确不做的事、CI 失败集合对差。

**Step 4 — PR**：`HTTPS_PROXY=http://127.0.0.1:12450 gh pr create`；
合并判据 = 闸门失败集合净新增 0（不是 CI 全绿）；**不部署**。

---

## 四、风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| 既有测试因 admin 门禁大面积红 | 中 | 先跑定向套件取基线；stub 默认角色含 admin ⇒ 无头请求仍通过；只有显式 `X-User-Roles: user` 的用例受影响 |
| 引用校验误伤合法自由文本词条 | 低 | D5：两字段为空一律放行；只有显式声明映射才校验 |
| 生产已有悬空映射词条 | —— | 校验只管写；存量由后续「词条体检」单独立项（本批不做） |
| 非 admin 用户仍能通过前端旧缓存调用接口 | 低 | 后端 403 是最终裁决；前端收敛只改善体验 |
| 指标写入口收紧影响部门用户日常 | 中 | 这是产品语义变更，已在计划中显式声明；如产品要求按部门授权，需先补 `object_owner` 列（DDL，另立项） |
| 回滚 | —— | 纯代码改动，`git revert` 即可；无迁移、无数据变更 |

## 五、验收口径

1. 非 admin 调用 5 条写路由（词条 2 + 指标 3）全部 403；
2. 词条映射到不存在/已下线类、或属性不属于该类 ⇒ 422 且文案可区分；
3. 词条两字段为空 ⇒ 201（自由文本）；
4. 既有词条/指标/本体套件净新增失败 0；
5. 前端非 admin 看不到写入口，admin 功能不变；
6. `merge-gate / failure-set-diff` 净新增 0；不部署。
