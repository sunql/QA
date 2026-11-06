"""backfill_milvus_external_id 单元测试的**共享 support**（假 Milvus + 固定数据）。

背景：回填脚本的测试分成两个关注面 ——
「delete 可见性与幂等契约」（``test_backfill_milvus_delete_visibility.py``）与
「安全护栏与运维可见性」（``test_backfill_milvus_safety_guards.py``）——
两者共用同一批替身。抽到本模块后：

- 依赖关系显式：谁提供什么，一眼可见；
- 两个套件各自只保留自己关心的用例，行数都在 800 行硬上限内
  （合并成一个文件时是 941 行 ⇒ 超限）；
- 与 ``app/tests/unit/_ci_gate_support.py`` 的既有做法一致（跨模块只引用
  **公开**名字，避免私有名被改名后变成收集期 error）。

⚠️ 替身只替换**外部依赖**（Milvus RPC），**PG 数据层必须真实**：
本模块的 ``readIdMapping`` 走真的 ``dbSession``（真实 PostgreSQL）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

import scripts.backfill_milvus_external_id as script
import scripts.milvus_external_id_apply as applyModule
from app.infrastructure import milvus_client

# ---------------------------------------------------------------------------
# 假 Milvus：按集合名共享的存储 + 异步可见性模型
# ---------------------------------------------------------------------------


class FakeMutationResult:
    """pymilvus ``MutationResult`` 的最小替身。"""

    def __init__(self, insertCount: int, primaryKeys: list[int]) -> None:
        self.insert_count = insertCount
        self.primary_keys = primaryKeys


def rowOrderKey(row: dict) -> tuple:
    """按主键升序（Milvus 的 query 返回顺序）；非整数 id 排到最后。"""
    autoId = row.get("id")
    if isinstance(autoId, int) and not isinstance(autoId, bool):
        return (0, autoId, "")
    return (1, 0, str(autoId))


def parseIdsFromDeleteExpr(expr: str) -> list[int]:
    """解析 delete 表达式里的主键列表。

    支持两种形态：``id == N``（逐行）与 ``id in [a, b, c]``（批量）。
    """
    body = expr.strip()
    if " in " in body:
        inner = body[body.index("[") + 1 : body.index("]")]
        return [int(part.strip()) for part in inner.split(",") if part.strip()]
    return [int(body.split("==")[1].strip())]


class FakeStore:
    """假 Milvus 存储；``loaded``/``pendingDeletes`` 复刻 delete 的异步可见性。"""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.pendingDeletes: dict[str, set[int]] = {}
        self.loaded: dict[str, bool] = {}
        self.nextId = 1000
        self.queryCount: dict[str, int] = {}
        self.onQuery = None
        # 跨句柄共享的调用序列：(集合名, 调用名)
        self.calls: list[tuple[str, str]] = []
        # 模拟「刷新了但 delete 仍未生效」：load() 不落 pending delete
        self.applyDeletesOnLoad = True
        # 模拟「insert 只写了一部分」：非 None 时覆盖 insert_count
        self.insertCountOverride: int | None = None
        # 模拟「insert 返回的 primary_keys 数量对不上」：截断返回的主键列表
        self.primaryKeysCountOverride: int | None = None
        # 模拟「insert 的行不可见」：新插入的行不出现在 query 结果里
        self.hideInsertedRows = False
        # 模拟「插入的列内容错位」：新行之间的 external_id 互换
        self.swapInsertedExternalIds = False
        # 模拟「query 撞上限被静默截断」：返回该数量的合成行
        self.truncateTo: int | None = None
        # 记录 wait_for_loading_complete 的调用参数
        self.waitCalls: list[dict] = []
        # 记录每次 query 的参数（expr / consistency_level）
        self.queryCalls: list[dict] = []
        # 记录每次 delete 的表达式（集合名, expr）
        self.deleteExprs: list[tuple[str, str]] = []
        # 模拟「集合 schema 缺字段」⇒ 整批 insert 会失败
        self.missingFields: set[str] = set()
        # 集合里 embedding 字段的维度（脚本按它校验载荷维度）
        self.embeddingDim = 2

    def seed(self, collectionName: str, rows: list[dict]) -> None:
        self.rows[collectionName] = [dict(r) for r in rows]
        self.pendingDeletes[collectionName] = set()
        # 生产语义：集合早已 loaded ⇒ 后续 load() 是 no-op，不会重放 delete。
        self.loaded[collectionName] = True

    def visibleRows(self, collectionName: str) -> list[dict]:
        """当前 query 能看到的行。

        ⚠️ pending 的 delete **仍然可见** —— 这正是 Milvus 的真实语义：
        delete 走三段异步管道，未重装（release+load）之前，query 照样读得到旧行。
        """
        rows = list(self.rows.get(collectionName, []))
        if self.hideInsertedRows:
            seeded = {r["id"] for r in self.rows.get(collectionName, []) if r["id"] <= 1000}
            rows = [r for r in rows if r["id"] in seeded]
        return sorted(rows, key=rowOrderKey)

    def applyPendingDeletes(self, collectionName: str) -> None:
        """release+load 时把 pending 的 delete 真正落到存储上。"""
        pending = self.pendingDeletes.get(collectionName, set())
        if not pending:
            return
        self.rows[collectionName] = [
            r for r in self.rows.get(collectionName, []) if r["id"] not in pending
        ]
        self.pendingDeletes[collectionName] = set()


class FakeCollection:
    """Collection 替身；记录调用顺序，用于断言 delete → insert → flush → release → load。"""

    def __init__(self, store: FakeStore, name: str, using: str | None = None) -> None:
        self._store = store
        self.name = name
        self.using = using

    def load(self, **kwargs) -> None:
        self._store.calls.append((self.name, "load"))
        if self._store.loaded.get(self.name, True):
            return  # 已 loaded ⇒ no-op（真实 Milvus 语义：不会重放 delete buffer）
        self._store.loaded[self.name] = True
        if self._store.applyDeletesOnLoad:
            self._store.applyPendingDeletes(self.name)

    def release(self, **kwargs) -> None:
        self._store.calls.append((self.name, "release"))
        self._store.loaded[self.name] = False

    def delete(self, expr: str, **kwargs) -> None:
        self._store.calls.append((self.name, "delete"))
        self._store.deleteExprs.append((self.name, expr))
        for autoId in parseIdsFromDeleteExpr(expr):
            self._store.pendingDeletes.setdefault(self.name, set()).add(autoId)

    @property
    def schema(self) -> SimpleNamespace:
        """集合 schema（字段名）。

        ``store.missingFields`` 可注入「集合少某个字段」——用来验证脚本在
        **delete 之前**就发现「要写的字段集不在 schema 里」⇒ 整批 insert 会失败
        ⇒ 已删的行会全部丢失。
        """
        names = [f for f in applyModule._COLUMN_FIELDS if f not in self._store.missingFields]
        fields = []
        for name in names:
            params = {"dim": self._store.embeddingDim} if name == "embedding" else {}
            fields.append(SimpleNamespace(name=name, params=params))
        return SimpleNamespace(fields=fields)

    def insert(self, data, **kwargs) -> FakeMutationResult:
        self._store.calls.append((self.name, "insert"))
        columns = list(data)
        rows = self._store.rows.setdefault(self.name, [])
        newIds: list[int] = []
        newRows: list[dict] = []
        for index in range(len(columns[0])):
            row = {
                field: column[index]
                for field, column in zip(applyModule._COLUMN_FIELDS, columns, strict=True)
            }
            self._store.nextId += 1
            row["id"] = self._store.nextId
            newIds.append(row["id"])
            newRows.append(row)
            rows.append(row)
        count = self._store.insertCountOverride
        if count is None:
            count = len(columns[0])
        if self._store.primaryKeysCountOverride is not None:
            # 服务端回的主键数量与实际写入行数对不上 ⇒ 脚本无法逐行校验
            newIds = newIds[: self._store.primaryKeysCountOverride]
        if self._store.swapInsertedExternalIds and len(newRows) > 1:
            swapped = [r["external_id"] for r in newRows]
            swapped.reverse()
            for row, value in zip(newRows, swapped, strict=True):
                row["external_id"] = value
        return FakeMutationResult(count, newIds)

    def flush(self, **kwargs) -> None:
        self._store.calls.append((self.name, "flush"))

    def query(self, expr: str, output_fields=None, limit=None, **kwargs) -> list[dict]:
        self._store.calls.append((self.name, "query"))
        self._store.queryCalls.append(
            {
                "collection": self.name,
                "expr": expr,
                "consistency_level": kwargs.get("consistency_level"),
            }
        )
        count = self._store.queryCount.get(self.name, 0)
        self._store.queryCount[self.name] = count + 1
        if self._store.onQuery is not None:
            self._store.onQuery(self.name, count)
        fields = list(output_fields or [])
        if self._store.truncateTo is not None:
            # 服务端静默截断：返回与上限同量的合成行（内容无关紧要）。
            return [{field: None for field in fields} for _ in range(self._store.truncateTo)]
        return [
            {field: row.get(field) for field in fields}
            for row in self._store.visibleRows(self.name)
        ]


# ---------------------------------------------------------------------------
# 假 Milvus 的装配入口
# ---------------------------------------------------------------------------


def makeFakeMilvus(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """把脚本依赖的 Milvus 入口换成 fake，但**保留真实的 refreshCollection**。

    `refreshCollection` 只做三件事：``release()`` → ``load()`` → 等装载完成。
    前两步打在 fake collection 上（真实语义），第三步用假的 `utility` 顶掉，
    并记录调用参数 ⇒ 「脚本确实用了共享原语、且等到了装载完成」被钉住。

    ⚠️ 这里给的是**普通函数**而不是 fixture：fixture 被 import 进测试模块后，
    它的依赖（另一个 fixture）要靠测试模块的命名空间解析 ⇒ 跨模块再引一个
    fixture 会变成 ``fixture 'store' not found``；而直接 import fixture 名字
    又会被测试函数的同名参数遮蔽（ruff F811）⇒ 由各测试模块自己声明一个
    3 行的 fixture 包一层。
    """
    store = FakeStore()
    monkeypatch.setattr(script, "_connect", lambda: None)
    monkeypatch.setattr(script, "_connAlias", lambda: "fake_alias")
    monkeypatch.setattr(
        script, "Collection", lambda name, using=None: FakeCollection(store, name, using)
    )
    # Milvus 原语模块（scripts.milvus_external_id_apply）**自己**持有一份
    # `Collection` / `_connAlias` 引用 ⇒ 必须在这个命名空间里也换成 fake，
    # 否则扫描/删除/复扫会走真实 pymilvus。
    monkeypatch.setattr(applyModule, "_connAlias", lambda: "fake_alias")
    monkeypatch.setattr(
        applyModule, "Collection", lambda name, using=None: FakeCollection(store, name, using)
    )
    # refreshCollection 定义在 milvus_client 里，用的是它自己的 _connAlias 与 utility。
    monkeypatch.setattr(milvus_client, "_connAlias", lambda: "fake_alias")

    def _fakeWait(collectionName: str, timeout=None, using=None, **kwargs) -> None:
        store.waitCalls.append(
            {"collection": collectionName, "timeout": timeout, "using": using}
        )

    monkeypatch.setattr(
        milvus_client, "utility", SimpleNamespace(wait_for_loading_complete=_fakeWait)
    )
    return store


# ---------------------------------------------------------------------------
# 固定数据
# ---------------------------------------------------------------------------


def seedOneRowPerCollection(store: FakeStore) -> None:
    """三个集合各放一行 ``external_id`` 为空的行。"""
    store.seed(
        script._CLASS_COLLECTION_NAME,
        [{"id": 1, "ontology_id": 5001, "type": "class", "name": "ClassS001",
          "alias": "", "description": "", "embedding": [0.5, 0.5], "external_id": ""}],
    )
    store.seed(
        script._PROPERTY_COLLECTION_NAME,
        [{"id": 2, "ontology_id": 5002, "type": "property", "name": "PropP001",
          "alias": "", "description": "", "embedding": [0.6, 0.6], "external_id": ""}],
    )
    store.seed(
        script._METRIC_COLLECTION_NAME,
        [{"id": 3, "ontology_id": 5003, "type": "metric", "name": "MetricM001",
          "alias": "", "description": "", "embedding": [0.7, 0.7], "external_id": ""}],
    )


async def readIdMapping(dbSession, unifiedId: str) -> tuple | None:
    """从**真实 PG** 读一条 id_mapping（按主键）。"""
    result = await dbSession.execute(
        text("SELECT unified_id, business_object, external_id, milvus_collection, milvus_id "
             "FROM id_mapping WHERE unified_id = :uid"),
        {"uid": unifiedId},
    )
    return result.first()
