"""本体后台向量同步「请求回滚存活」真实数据验证脚本。

对应生产事故（2026-11-16）：``ontology_service.py:2392`` 抛
``sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called``。

本脚本站在**修复之后**的下游，用**真实 PostgreSQL + 真实 Milvus + 真实 Neo4j**
重演生产故障形状，并给出**自证**（不是假绿）：

  步骤 0  预检：写库闸门 / PG 版本 / Milvus 集合 / Neo4j 连通 / embedding 模式
  步骤 1  清上一轮残留（只删本脚本前缀的行）
  步骤 2  建类 + 建属性（真实 service ⇒ 真实 PG + 真实 Neo4j；后台向量任务登记）
  步骤 3  复刻请求失败链路：读一次实体（开事务）→ ``await session.rollback()``
          ⇒ SQLAlchemy ``_restore_snapshot`` 把 identity map 全量 expire
          ⇒ **断言实体确实已 expired**（复现忠实性的前置条件，不满足即 FAIL）
  步骤 4  放行后台任务 ⇒ 断言**零异常**（修复前此处抛 MissingGreenlet）
  步骤 5  **旧写法的对照实验**：任务捕获请求级 ORM 实例，rollback 后再取属性
          ⇒ 断言**必须抛 MissingGreenlet**
          （证明「步骤 4 的绿」不是因为场景没复现，而是因为写法换了）
  步骤 6  真实 Milvus 核对：类/属性向量确实落库（ontology_id 对得上）
  步骤 7  实体已删 ⇒ 后台同步**跳过**（不写回孤儿向量）
  步骤 8  清理：本脚本产生的 PG 行 / Neo4j 节点 / Milvus 向量

为什么必须走真链路：事故的触发条件横跨三层——PG 事务边界（identity map expire）、
asyncio 任务调度、Milvus 写入。只调 service 或 mock Milvus 都验证不到「后台任务
真的把向量写进了真 Milvus」这一段，也无法证明对照实验抛的是同一个
MissingGreenlet。

embedding 说明：本地 ollama（:11434）未启动 ⇒ 真实 embedding 服务不可用，脚本注入
**确定性 embedding 桩**（bge-m3 同为 1024 维，桩也产出 1024 维以匹配 Milvus 集合
schema）。PG 读取、Milvus 写入、Neo4j 写入**全部为真实**；embedding 只影响向量
内容，不影响本脚本要验证的「会话解耦 + 真实落库」结论。

隔离闸门（三道，缺一即拒绝运行，脚本会先读后删）：

1. **PG**：库名必须匹配 ``^qa_metadata_test($|_)``（生产库 ``qa_metadata`` 曾发生
   整库误删事故）。Alembic/settings 只认 ``DATABASE_URL``，``TEST_DATABASE_URL``
   会被静默忽略 ⇒ 运行命令必须显式传 ``DATABASE_URL``。
2. **Milvus**：``MILVUS_DB_NAME`` 必须以 ``qa_test`` 开头。默认值是 ``default``
   （= 生产向量库）；漏配时按 id 删除会误删生产向量，``refreshCollection`` 还会
   release+load 生产集合。
3. **Neo4j**：URI 端口不得是生产默认 ``7687``（测试实例为 ``7688``）。
   2026-10-03 生产图谱曾因测试连上 7687 被清空。

运行（宿主机；先确保目标库已 ``alembic upgrade head``）：

    cd backend && \\
    DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test_mg2' \\
    NEO4J_URI='bolt://localhost:7688' \\
    MILVUS_URI='http://localhost:19530' \\
    MILVUS_DB_NAME='qa_test' \\
    .venv/bin/python -m scripts.ontology_background_sync_realdata
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.config import getSettings  # noqa: E402
from app.dependencies import CurrentUser  # noqa: E402
from app.domain.schemas import OntologyClassCreate, OntologyPropertyCreate  # noqa: E402
from app.infrastructure import milvus_client as milvus  # noqa: E402
from app.infrastructure import neo4j_client as neo4j  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services import ontology_service as ontMod  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ALLOWED_DB_PATTERN = "qa_metadata_test"
_ALLOWED_DB_RE = re.compile(r"^qa_metadata_test($|_)")
ALLOWED_MILVUS_DB_PREFIX = "qa_test"
PROD_NEO4J_PORT = 7687
TEST_NEO4J_PORT = 7688
CLASS_NAME_PREFIX = "REALDATA_MGREENLET"
PROPERTY_NAME = "REALDATA_MGREENLET_AMT"
EMBEDDING_DIM = 1024  # 与 milvus_client._DIM（bge-m3）一致
ACTOR = CurrentUser(userId="realdata-admin", roles=(ADMIN_ROLE,), departments=())

_FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> bool:
    """打印一条步骤结果并登记失败。"""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""), flush=True)
    if not ok:
        _FAILURES.append(label)
    return ok


def _step(title: str) -> None:
    print(f"\n=== STEP {title} ===", flush=True)


class _StubEmbedding:
    """确定性 embedding 桩（本地真实 embedding 服务不可用，见模块 docstring）。

    闸门 ``gate`` 用于把后台任务停在 embedding 这一步，由脚本决定何时放行——
    这样「请求 rollback」与「后台任务继续」之间的**先后次序是确定的**，
    不依赖 sleep 抖动。
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def generateEmbedding(self, text_: str) -> list[float]:
        await self._gate.wait()
        rnd = random.Random(zlib.crc32(text_.encode("utf-8")))
        return [rnd.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]


async def _legacyTask(entity: Any) -> Any:
    """**修复前**的后台任务形状：捕获请求级 ORM 实例，事后再取它的属性。

    事故栈里就是这个形状：``entity.id`` / ``entity.property_name`` 触发同步懒加载
    ⇒ async 上下文里做 IO ⇒ MissingGreenlet。
    """
    await asyncio.sleep(0)  # 让出事件循环 ⇒ 与生产一致地「在另一个 task 里」
    return entity.id


def _tryDeleteMilvus(ontologyId: int, type_: str) -> None:
    try:
        milvus.deleteByOntologyIdDual(ontologyId, type_)
    except Exception as exc:  # noqa: BLE001
        print(f"    (清理 Milvus {type_} 向量 id={ontologyId} 失败，忽略：{exc})")


def _tryDeleteNode(label: str, unifiedId: str) -> None:
    try:
        neo4j.deleteNode(label, unifiedId)
    except Exception as exc:  # noqa: BLE001
        print(f"    (清理 Neo4j {label} {unifiedId} 失败，忽略：{exc})")


async def _cleanupLeftovers(session) -> int:
    """清掉上一轮残留：只按本脚本的类名前缀圈定，绝不 TRUNCATE。

    **对称清理**：类、属性两级的 PG 行 / id_mapping / Neo4j 节点 / Milvus 向量
    全部按本前缀圈定的 id 删除 ⇒ 反复运行时不会在 Neo4j / Milvus 里堆孤儿。
    """
    rows = await session.execute(
        text(
            "SELECT id FROM ontology_class WHERE class_name LIKE :p ORDER BY id"
        ),
        {"p": f"{CLASS_NAME_PREFIX}%"},
    )
    ids = [int(r[0]) for r in rows.fetchall()]
    for cid in ids:
        props = await session.execute(
            text("SELECT id FROM ontology_property WHERE class_id = :cid"),
            {"cid": cid},
        )
        pids = [int(r[0]) for r in props.fetchall()]
        for pid in pids:
            # 先取 unified_id（随后要删 id_mapping 行）
            uids = await session.execute(
                text(
                    "SELECT unified_id FROM id_mapping "
                    "WHERE business_object='PROPERTY' AND pg_id = :pid"
                ),
                {"pid": str(pid)},
            )
            for (uid,) in uids.fetchall():
                _tryDeleteNode("Property", str(uid))
            await session.execute(
                text("DELETE FROM id_mapping WHERE business_object='PROPERTY' AND pg_id = :pid"),
                {"pid": str(pid)},
            )
            await session.execute(
                text("DELETE FROM ontology_property WHERE id = :pid"), {"pid": pid}
            )
            _tryDeleteMilvus(pid, "property")
        clsUids = await session.execute(
            text(
                "SELECT unified_id FROM id_mapping "
                "WHERE business_object='CLASS' AND pg_id = :cid"
            ),
            {"cid": str(cid)},
        )
        for (uid,) in clsUids.fetchall():
            _tryDeleteNode("Class", str(uid))
        await session.execute(
            text("DELETE FROM id_mapping WHERE business_object='CLASS' AND pg_id = :cid"),
            {"cid": str(cid)},
        )
        await session.execute(
            text("DELETE FROM ontology_class WHERE id = :cid"), {"cid": cid}
        )
        _tryDeleteMilvus(cid, "class")
    if ids:
        await session.commit()
    return len(ids)


async def _cleanupCurrent(session, classId: int, propId: int) -> None:
    """清理本轮产生的 Neo4j 节点 / PG 行 / Milvus 向量。"""
    # Neo4j：按 id_mapping 里的 unified_id 删节点
    rows = await session.execute(
        text(
            "SELECT business_object, unified_id FROM id_mapping "
            "WHERE (business_object='CLASS' AND pg_id = :cid) "
            "   OR (business_object='PROPERTY' AND pg_id = :pid)"
        ),
        {"cid": str(classId), "pid": str(propId)},
    )
    labelByObject = {"CLASS": "Class", "PROPERTY": "Property"}
    for businessObject, unifiedId in rows.fetchall():
        label = labelByObject.get(str(businessObject))
        if not label:
            continue
        try:
            deleted = neo4j.deleteNode(label, str(unifiedId))
            print(f"    Neo4j 删除 {label} {unifiedId} → {deleted} 个节点")
        except Exception as exc:  # noqa: BLE001
            print(f"    (Neo4j 删除失败，忽略：{exc})")

    # Milvus
    for oid, type_ in ((classId, "class"), (propId, "property")):
        try:
            milvus.deleteByOntologyIdDual(oid, type_)
        except Exception as exc:  # noqa: BLE001
            print(f"    (Milvus 删除 {type_} 向量失败，忽略：{exc})")

    # PG：先 id_mapping，再属性，再类（FK RESTRICT 顺序）
    await session.execute(
        text("DELETE FROM id_mapping WHERE business_object='PROPERTY' AND pg_id = :pid"),
        {"pid": str(propId)},
    )
    await session.execute(
        text("DELETE FROM id_mapping WHERE business_object='CLASS' AND pg_id = :cid"),
        {"cid": str(classId)},
    )
    await session.execute(
        text("DELETE FROM ontology_property WHERE id = :pid"), {"pid": propId}
    )
    await session.execute(
        text("DELETE FROM ontology_class WHERE id = :cid"), {"cid": classId}
    )
    await session.commit()


def _milvusRows(type_: str) -> list[dict[str, Any]]:
    if type_ == "property":
        return milvus.queryPropertyEmbeddings()
    if type_ == "class":
        return milvus.queryClassEmbeddings()
    return milvus.queryMetricEmbeddings()


def _rowsOf(type_: str, ontologyId: int) -> list[dict[str, Any]]:
    return [r for r in _milvusRows(type_) if int(r.get("ontology_id", -1)) == ontologyId]


def _collectionOf(type_: str) -> Any:
    if type_ == "property":
        return milvus.ensurePropertyCollection()
    if type_ == "class":
        return milvus.ensureClassCollection()
    return milvus.ensureMetricCollection()


def _refreshMilvus(type_: str) -> None:
    """release + load：强制 QueryNode 重装 segment + delta binlog，让 delete 立即可见。

    见 ``milvus_client.refreshCollection`` 注释：Milvus 删除走
    ``Proxy → DML channel → DataNode → QueryNode`` 三段异步管道，``flush()``
    只持久化；collection 默认 Bounded（5s 容忍窗口）时，per-request
    ``consistency_level="Strong"`` 在 delete 后 0~5s 窗口内**仍会返回残留行**
    （2026-09-27 实测）。2026-11-16 容器内实测同一现象更极端：单纯轮询 10s 后
    仍见 1 行，直到后续又一次 delete 推进时间戳才消失 ⇒ 定时长没有保证，必须
    用仓库既有的一次性验证写法（refresh）换取确定性。
    """
    milvus.refreshCollection(_collectionOf(type_))


async def _waitMilvusPresence(
    type_: str, ontologyId: int, *, present: bool, timeout: float = 10.0
) -> list[dict[str, Any]]:
    """等到该实体的向量「出现 / 消失」。

    ``present=True``（写入已 ``flush()`` ⇒ 立即可见）直接轮询；
    ``present=False``（delete 可见性滞后）先 ``refresh`` 再轮询，见
    ``_refreshMilvus`` 的实测记录。
    """
    if not present:
        # release + load 是同步阻塞调用（~2-3s）⇒ 移出事件循环
        await asyncio.to_thread(_refreshMilvus, type_)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        # pymilvus 是同步 SDK（load + query 会阻塞）⇒ 移出事件循环
        rows = await asyncio.to_thread(_rowsOf, type_, ontologyId)
        if bool(rows) is present:
            return rows
        if loop.time() >= deadline:
            return rows
        await asyncio.sleep(0.25)



async def main() -> int:
    settings = getSettings()
    dbUrl = make_url(settings.databaseUrl)
    dbName = dbUrl.database or ""

    # ---------------- STEP 0 预检 ----------------
    _step("0 预检（写库闸门 / PG / Milvus / Neo4j）")
    if not _ALLOWED_DB_RE.match(dbName):
        print(f"  [FAIL] 写库闸门：库名 {dbName!r} 不匹配 ^{ALLOWED_DB_PATTERN}($|_) ⇒ 拒绝运行")
        print("\nREALDATA RESULT: FAIL")
        return 1
    _check("写库闸门：库名合规", True, f"{dbName} @ {dbUrl.host}:{dbUrl.port}")

    # 闸门 2：Milvus 必须指向**测试库**（`qa_test*`）。production 用 default 库；
    # 漏配时 `deleteByOntologyIdDual` 会按 id 误删生产向量，且 refreshCollection
    # 会 release+load 生产集合（检索短暂不可用）。
    milvusDb = settings.milvusDbName or "default"
    if not milvusDb.startswith(ALLOWED_MILVUS_DB_PREFIX):
        print(
            f"  [FAIL] Milvus 隔离闸门：MILVUS_DB_NAME={milvusDb!r} 不是测试库"
            f"（需前缀 {ALLOWED_MILVUS_DB_PREFIX!r}）⇒ 拒绝运行"
        )
        print("\nREALDATA RESULT: FAIL")
        return 1
    _check("Milvus 隔离闸门：测试库", True, f"db={milvusDb} uri={settings.milvusUri}")

    # 闸门 3：Neo4j 不得指向生产端口 7687（2026-10-03 生产图谱被测试清空的同款风险）。
    neo4jPort = urlparse(settings.neo4jUri).port
    if neo4jPort == PROD_NEO4J_PORT:
        print(
            f"  [FAIL] Neo4j 隔离闸门：{settings.neo4jUri!r} 指向生产端口 "
            f"{PROD_NEO4J_PORT} ⇒ 拒绝运行（用测试实例 :{TEST_NEO4J_PORT}）"
        )
        print("\nREALDATA RESULT: FAIL")
        return 1
    _check("Neo4j 隔离闸门：非生产端口", True, f"uri={settings.neo4jUri}")
    print("  info embedding：本地 ollama(:11434) 未启动 ⇒ 用确定性 1024 维桩"
          "（PG/Milvus/Neo4j 均为真实写入）")

    factory = getSessionFactory()
    async with factory() as session:
        ver = await session.execute(text("SELECT version_num FROM alembic_version"))
        _check("PG 可连 + alembic 版本可读", True, f"version={ver.fetchone()[0]}")
        try:
            neo4j.getClassIds()
            _check("Neo4j 可连", True)
        except Exception as exc:  # noqa: BLE001
            _check("Neo4j 可连", False, str(exc))

        # ---------------- STEP 1 清残留 ----------------
        _step("1 清理上一轮残留")
        removed = await _cleanupLeftovers(session)
        _check("残留清理完成", True, f"删除 {removed} 个历史类")

        # ---------------- STEP 2 建类 + 建属性 ----------------
        _step("2 建类 + 建属性（真实 PG + 真实 Neo4j）")
        gate = asyncio.Event()
        service = OntologyService(embeddingService=_StubEmbedding(gate))  # type: ignore[arg-type]
        beforeTasks = set(ontMod._PENDING_SYNC_TASKS)

        className = f"{CLASS_NAME_PREFIX}_C"
        cls = await service.createClass(
            session,
            OntologyClassCreate(class_name=className, source_table="ZJTH.REALDATA_MG"),
            actor=ACTOR.userId,
            actor_departments=None,
            sync_embedding=True,
        )
        prop = await service.createProperty(
            session,
            OntologyPropertyCreate(
                class_id=cls.id,
                property_name=PROPERTY_NAME,
                data_type="DECIMAL",
                source_column="AMT_REALDATA",
            ),
            actor=ACTOR.userId,
            actor_departments=None,
        )
        classId, propId = cls.id, prop.id
        tasks = set(ontMod._PENDING_SYNC_TASKS) - beforeTasks
        _check("类已入库", True, f"class_id={classId} name={className}")
        _check("属性已入库", True, f"property_id={propId} name={PROPERTY_NAME}")
        _check("后台向量任务已登记", len(tasks) >= 2, f"{len(tasks)} 个任务")

        # ---------------- STEP 3 请求失败链路 ----------------
        _step("3 复刻请求失败链路（读实体 → session.rollback() → identity map 全量 expire）")
        # 请求收尾读一次实体（真实请求会在这里组装响应）⇒ 事务处于开启状态，
        # rollback 才会触发 SQLAlchemy SessionTransaction._restore_snapshot。
        await session.execute(text("SELECT 1"))
        await session.rollback()
        expiredState = inspect(prop)
        _check(
            "rollback 后实体确实 expired（复现忠实性前置条件）",
            bool(expiredState.expired),
            f"expired={expiredState.expired} expired_attrs={expiredState.expired_attributes}",
        )

        # ---------------- STEP 4 放行后台任务 ----------------
        _step("4 放行后台向量同步任务（修复后的写法）")
        gate.set()
        errored: list[BaseException] = []
        for task in tasks:
            try:
                await task
            except BaseException as exc:  # noqa: BLE001
                errored.append(exc)
        _check(
            "后台任务零异常",
            not errored,
            "; ".join(f"{type(e).__name__}: {e}" for e in errored) or "无",
        )

        # ---------------- STEP 5 旧写法对照 ----------------
        _step("5 对照实验：修复前的写法在同一场景下必须抛 MissingGreenlet")
        legacyError: BaseException | None = None
        try:
            await _legacyTask(prop)
        except BaseException as exc:  # noqa: BLE001
            legacyError = exc
        _check(
            "旧写法复现 MissingGreenlet",
            legacyError is not None and type(legacyError).__name__ == "MissingGreenlet",
            f"{type(legacyError).__name__ if legacyError else 'None'}: {legacyError}",
        )
        _check(
            "对照实验说明：步骤 4 的绿来自写法而非场景未复现",
            legacyError is not None,
            "同一 session、同一 rollback 后，请求级 ORM 实例的属性不可再安全读取",
        )

        # ---------------- STEP 6 真实 Milvus 核对 ----------------
        _step("6 真实 Milvus 核对（向量是否真的落库）")
        propRows = await _waitMilvusPresence("property", propId, present=True)
        classRows = await _waitMilvusPresence("class", classId, present=True)
        _check("属性向量已落 Milvus", len(propRows) == 1, f"{len(propRows)} 行")
        _check("类向量已落 Milvus", len(classRows) == 1, f"{len(classRows)} 行")
        if propRows:
            row = propRows[0]
            # 读投影刻意不含 1024 维 embedding（见 milvus_query_helpers 的行数/字段说明）
            # ⇒ 这里核对「这条向量属于谁、叫什么」，维数由 schema 保证。
            _check(
                "属性向量内容自洽（type / name）",
                row.get("type") == "property" and row.get("name") == PROPERTY_NAME,
                f"type={row.get('type')} name={row.get('name')}",
            )

        # ---------------- STEP 7 实体已删 ⇒ 跳过 ----------------
        _step("7 实体已删除 ⇒ 后台同步跳过（不写回孤儿向量）")
        await service.deleteProperty(session, propId, actor=ACTOR.userId)
        await service.deleteClass(session, classId, actor=ACTOR)
        await session.commit()
        skippedError: BaseException | None = None
        try:
            await service._syncPropertyEmbeddingNow(propId)
            await service._syncClassEmbeddingNow(classId)
        except BaseException as exc:  # noqa: BLE001
            skippedError = exc
        _check("已删实体的后台同步不抛异常", skippedError is None, str(skippedError or ""))
        propAfter = await _waitMilvusPresence("property", propId, present=False)
        classAfter = await _waitMilvusPresence("class", classId, present=False)
        _check("已删属性不写回向量", len(propAfter) == 0, f"{len(propAfter)} 行")
        _check("已删类（墓碑）不写回向量", len(classAfter) == 0, f"{len(classAfter)} 行")

        # ---------------- STEP 8 清理 ----------------
        _step("8 清理本轮数据")
        await _cleanupCurrent(session, classId, propId)
        leftClass = await session.execute(
            text("SELECT count(*) FROM ontology_class WHERE id = :cid"), {"cid": classId}
        )
        leftProp = await session.execute(
            text("SELECT count(*) FROM ontology_property WHERE id = :pid"), {"pid": propId}
        )
        leftMap = await session.execute(
            text(
                "SELECT count(*) FROM id_mapping WHERE (business_object='CLASS' AND pg_id=:cid) "
                "OR (business_object='PROPERTY' AND pg_id=:pid)"
            ),
            {"cid": str(classId), "pid": str(propId)},
        )
        leftCounts = {
            "class": leftClass.fetchone()[0],
            "property": leftProp.fetchone()[0],
            "id_mapping": leftMap.fetchone()[0],
        }
        _check(
            "PG 清理干净",
            leftCounts["class"] == 0 and leftCounts["property"] == 0,
            f"class={leftCounts['class']} property={leftCounts['property']}",
        )
        _check(
            "id_mapping 清理干净",
            leftCounts["id_mapping"] == 0,
            f"id_mapping={leftCounts['id_mapping']}",
        )
        finalProp = await _waitMilvusPresence("property", propId, present=False)
        finalClass = await _waitMilvusPresence("class", classId, present=False)
        _check("Milvus 清理干净", not finalProp and not finalClass,
               f"property={len(finalProp)} class={len(finalClass)}")

    print("\n=== 汇总 ===")
    if _FAILURES:
        print(f"  失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print(f"    - {item}")
        print("\nREALDATA RESULT: FAIL")
        return 1
    print("  全部通过")
    print("\nREALDATA RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
