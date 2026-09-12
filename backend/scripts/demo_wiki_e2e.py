"""企业 Wiki 端到端演示 —— feat-wiki-knowledge 验收 §13 的 4 个 demo。

    写规则 → 分类 → dry-run → Agent 直读      （demo 1）
    跨条目关系：候选 → 确认 → Agent 视角切换   （demo 2）
    矛盾检测（确定性三路，不调模型）            （demo 3）
    覆盖度看板 ↔ Agent 数字一致                （demo 4）

跑法（**必须先指定库**，见下面的写库闸）：

    DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \\
        .venv/bin/python -m scripts.demo_wiki_e2e

设计取舍，先说清楚：

1. **走真实 app 的完整 API 链路**（`createApp()` + 真 lifespan + httpx ASGI 传输），
   不是直接调 service。demo 的价值就在于证明「用户点得到的那条路」是通的；
   绕过路由层的演示证明不了这件事。
2. **确定性优先**：所有需要 LLM 的步骤（机制 1 分类抽取、机制 2 关系发现、
   机制 3 矛盾、机制 4 结构抽取）都走它们各自声明的「无模型降级路径」，
   或由脚本直接落库并**就地标注**「生产上这一步由 X 产出」。这样 demo 不依赖
   任何模型 key，也不会因为模型抖动而时绿时红。
3. **写库闸**：本脚本会写数据。默认只允许库名含 `test` 的目标；要打别的库
   得显式加 `--allow-non-test-db`。这条闸是给 `qa_metadata` 那次整库被清
   的事故（见 memory: qa-system-pg-wipe-incident）立的规矩：
   演示脚本不该有「手滑打到生产库」这个选项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import and_, delete, or_, select, text, update  # noqa: E402

from app.domain.wiki_learning_models import (  # noqa: E402
    LearningFeedback,
    WikiRuleExecutable,
)
from app.domain.wiki_models import KnowledgeRelation, WikiPage  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.main import createApp  # noqa: E402

BASE = "http://demo"
API = "/api/v1"

# 演示数据统一前缀 —— 每次运行先按前缀清干净，重复跑不会堆垃圾，也不会
# 因为「第二次跑时 409」而让人以为功能坏了。
DEMO_PREFIX = "DEMO-"
PAGE_RULE = f"{DEMO_PREFIX}RULE-0001"
PAGE_OBJ = f"{DEMO_PREFIX}OBJ-0001"
PAGE_EXPIRED = f"{DEMO_PREFIX}EXPIRED-0001"

AUTH = {"X-User-Id": "demo-admin", "X-User-Roles": "admin"}


# ---------------------------------------------------------------------------
# 输出助手
# ---------------------------------------------------------------------------


def step(n: str, title: str) -> None:
    print(f"\n{'=' * 72}\n[{n}] {title}\n{'=' * 72}")


def say(msg: str) -> None:
    print(f"  · {msg}")


def show(label: str, payload: Any) -> None:
    print(f"  ↳ {label}: {json.dumps(payload, ensure_ascii=False, default=str)}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def silenceSql() -> None:
    """压掉 SQLAlchemy 的 echo（演示要的是读得懂的记录，不是 SQL 流水账）。

    非生产环境 ``create_engine(echo=True)`` 让每条 SQL 打两遍，4 个 demo 的
    结论会被淹没在几百行参数转储里。

    **这里不能用 ``logger.setLevel(WARNING)``** —— 试过了，压不住。
    SQLAlchemy 的 ``InstanceLogger._log``（``sqlalchemy/log.py``）在调用
    ``logger._log`` 之前先算 ``selected_level = _echo_map[echo]``，``echo=True``
    时它直接是 ``INFO``，**根本不会回退去读 logger 自己的级别**
    （只有 ``echo=None`` 才 ``getEffectiveLevel()``）。也就是说设级别这条路对
    echo 是死的，而它连 ``not self.logger.handlers`` 才装 handler 的判断都写在
    构造期，事后摘 handler 也躲不过 root 的兜底 handler。

    ``logging.disable`` 走的是另一条路：``_log`` 开头就查
    ``logger.manager.disable >= level``，命中即 return —— 唯一能挡下 echo 的开关。
    代价是全局的（本进程所有 INFO 都不再输出），对一次性演示脚本正是想要的。
    """
    logging.disable(logging.INFO)


def check(condition: bool, msg: str) -> None:
    """演示里的断言：不成立就抛，让「演示通过」这句话有代价。"""
    if not condition:
        raise AssertionError(f"✗ 演示断言失败：{msg}")
    ok(msg)


# ---------------------------------------------------------------------------
# HTTP 助手
# ---------------------------------------------------------------------------


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    expect: int = 200,
) -> dict:
    resp = await client.request(method, f"{API}{path}", json=body, headers=AUTH)
    if resp.status_code != expect:
        raise AssertionError(
            f"{method} {path} 期望 {expect}，实际 {resp.status_code}：{resp.text}"
        )
    return resp.json() if resp.content else {}


async def runAgent(client: httpx.AsyncClient, agentCode: str, text: str) -> dict:
    return await call(client, "POST", f"/agents/{agentCode}/run", body={"input": text})


def _countsText(counts: dict[str, int]) -> str:
    return (
        f"{counts['pages']} 条目 / {counts['relations']} 关系 / "
        f"{counts['conflicts']} 冲突 / {counts['feedback']} 反馈"
    )


# ---------------------------------------------------------------------------
# 造数 / 清理（只用于「产品确实没有 API」的两处：规则物化、关系候选发现）
# ---------------------------------------------------------------------------


async def _resetDemoData() -> dict[str, int]:
    """按前缀清掉上一次的演示数据，返回各类被删行数。

    **清理 learning_feedback 必须先收集 ID，不能只按前缀匹配。** 三种机制的
    ``entity_id`` 形状不同：
      - ``CLASSIFY`` → Page 的业务键（``"DEMO-RULE-0001"``），前缀匹配有效
      - ``RELATE``   → ``str(relation.id)``，**纯数字**
      - ``CONFLICT`` → ``str(conflict.id)``，**纯数字**
    只按 ``LIKE 'DEMO-%'`` 删，后两类**一条都删不掉**，每次运行静默留 2 行 ——
    而 ``learning_feedback`` 的 ``entity_id`` 是多态业务键，**刻意不建 FK**
    （见 ORM 注释），指望级联也不成立。所以要先把关系/冲突的 ID 查出来，
    再按 ``entity_type + entity_id`` 精确删。
    """
    factory = getSessionFactory()
    async with factory() as session:
        pageIds = list(
            (
                await session.execute(
                    select(WikiPage.page_id).where(
                        WikiPage.page_id.like(f"{DEMO_PREFIX}%")
                    )
                )
            ).scalars().all()
        )
        counts = {"pages": len(pageIds), "relations": 0, "conflicts": 0, "feedback": 0}
        if pageIds:
            relationIds = list(
                (
                    await session.execute(
                        select(KnowledgeRelation.id).where(
                            KnowledgeRelation.upstream_page_id.in_(pageIds)
                        )
                    )
                ).scalars().all()
            )
            # 冲突表按 page_ids 数组匹配（knowledge_conflict.page_ids 是 VARCHAR[]）
            conflictIds = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM knowledge_conflict "
                            "WHERE page_ids && CAST(:ids AS VARCHAR[])"
                        ),
                        {"ids": pageIds},
                    )
                ).scalars().all()
            )

            # 关系/规则有 CASCADE，但显式删一遍更清楚：演示脚本不该依赖级联去解释
            # 「为什么删了条目以后冲突还在」。
            await session.execute(
                delete(KnowledgeRelation).where(
                    KnowledgeRelation.upstream_page_id.in_(pageIds)
                )
            )
            await session.execute(
                delete(WikiRuleExecutable).where(WikiRuleExecutable.page_id.in_(pageIds))
            )

            # 反馈事件（见 docstring：三种机制三种 ID 形状，逐一精确删）
            feedbackWhere = [
                and_(
                    LearningFeedback.entity_type == "WIKI_PAGE",
                    LearningFeedback.entity_id.in_(pageIds),
                )
            ]
            if relationIds:
                feedbackWhere.append(
                    and_(
                        LearningFeedback.entity_type == "RELATION",
                        LearningFeedback.entity_id.in_([str(i) for i in relationIds]),
                    )
                )
            if conflictIds:
                feedbackWhere.append(
                    and_(
                        LearningFeedback.entity_type == "CONFLICT",
                        LearningFeedback.entity_id.in_([str(i) for i in conflictIds]),
                    )
                )
            removed = await session.execute(
                delete(LearningFeedback).where(or_(*feedbackWhere))
            )

            await session.execute(delete(WikiPage).where(WikiPage.page_id.in_(pageIds)))
            await session.execute(
                text(
                    "DELETE FROM knowledge_conflict "
                    "WHERE page_ids && CAST(:ids AS VARCHAR[])"
                ),
                {"ids": pageIds},
            )
            counts["relations"] = len(relationIds)
            counts["conflicts"] = len(conflictIds)
            counts["feedback"] = removed.rowcount or 0
        await session.commit()
        return counts


async def _seedAutoClassification(pageId: str, primary: str, confidence: float) -> None:
    """落一份机制 1 的自动分类结论（`wiki_page.auto_classification`）。

    **生产上这一步由机制 1 产出**（入库时 LLM few-shot 分类）。演示要的是
    「建议已经在了，业务专家怎么处置它」—— 没有这份建议，reclassify 就只是
    改了个字段，`action` 会是 ``None``，学习闭环那一环根本没被触发。
    """
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(
            update(WikiPage)
            .where(WikiPage.page_id == pageId)
            .values(
                auto_classification={
                    "primary": primary,
                    "confidence": confidence,
                    "alternatives": [],
                }
            )
        )
        await session.commit()


async def _countFeedback(mechanism: str, entityId: str) -> int:
    """数一数某个实体上记了几条学习闭环反馈事件。"""
    factory = getSessionFactory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(LearningFeedback.id).where(
                    LearningFeedback.mechanism == mechanism,
                    LearningFeedback.entity_id == entityId,
                )
            )
        ).scalars().all()
        return len(rows)


async def _seedRule(pageId: str, examples: list[dict]) -> None:
    """落一条可执行规则。

    **生产上这一步由机制 4 产出**（`POST /pages/{id}/suggestions` 抽取 →
    `POST /suggestions/{id}/accept` 物化），那条路要真调模型。演示要的是
    「规则被物化之后，dry-run 与 Agent 直读是通的」，所以这里直接落库，
    并且样例就是规则自带的 `dry_run_examples`。
    """
    factory = getSessionFactory()
    async with factory() as session:
        session.add(
            WikiRuleExecutable(
                page_id=pageId,
                rule_kind="THRESHOLD",
                rule_expression={
                    "conditions": [
                        {"field": "注册资本", "operator": ">=", "value": 1000},
                    ],
                    "action": {"type": "REJECT", "reason": "注册资本不足"},
                },
                target_entity="SUPPLIER",
                dry_run_examples=examples,
                authority_chain=["L4", "L2"],
                version="v1.0",
            )
        )
        await session.commit()


async def _seedRelationCandidate(
    upstreamPageId: str,
    downstreamType: str,
    downstreamId: str,
    relationType: str,
) -> int:
    """落一条 `confirmed=false` 的关系候选，返回 id。

    **生产上这一步由机制 2 产出**（`POST /pages/{id}/relations/discover`，
    带 modelId 时调模型）。这里直接落库，为的是把 demo 的重点放在
    「候选与事实的分界是不是真的存在」上。
    """
    factory = getSessionFactory()
    async with factory() as session:
        row = KnowledgeRelation(
            upstream_page_id=upstreamPageId,
            downstream_type=downstreamType,
            downstream_id=downstreamId,
            relation_type=relationType,
            confidence=0.62,
            auto_detected=True,
            confirmed=False,
        )
        session.add(row)
        await session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Demo 1：写规则 → 分类 → dry-run → Agent 直读
# ---------------------------------------------------------------------------


async def demo1(client: httpx.AsyncClient) -> None:
    step("demo 1", "写规则 → 分类 → dry-run → Agent 直读")

    page = await call(
        client,
        "POST",
        "/wiki/pages",
        body={
            "pageId": PAGE_RULE,
            "title": "供应商准入规则（演示）",
            "content": "注册资本不低于 1000 万元方可准入。",
            "dimension": "CONCEPT",  # 先给个错的：下面用 reclassify 纠正
        },
        expect=201,
    )
    say(f"建条目 {page['pageId']}，初始维度 {page['dimension']}")

    # 机制 1 的产物先落上（生产上由入库时的 LLM 分类产出）：机器认为是 CONCEPT。
    await _seedAutoClassification(PAGE_RULE, primary="CONCEPT", confidence=0.71)
    say("机制 1 自动分类已就位：建议 CONCEPT（置信度 0.71）")

    # 机制 1：业务专家**不认同**这个建议，改成 RULE（调整是人的动作，不调模型）
    before = await _countFeedback("CLASSIFY", PAGE_RULE)
    reclass = await call(
        client,
        "POST",
        f"/wiki/pages/{PAGE_RULE}/reclassify",
        body={"dimension": "RULE"},
    )
    show(
        "机制 1 处置",
        {"action": reclass["action"], "dimension": reclass["page"]["dimension"]},
    )
    check(reclass["page"]["dimension"] == "RULE", "分类已调整为 RULE")
    check(
        reclass["action"] == "MODIFY",
        "处置被判定为 MODIFY —— 基准是**建议的** CONCEPT，不是被覆盖前的页面值",
    )
    check(
        await _countFeedback("CLASSIFY", PAGE_RULE) == before + 1,
        "学习闭环落了一条 feedback 事件（回改样本，供后续训练分类器）",
    )

    await _seedRule(
        PAGE_RULE,
        examples=[
            {"input": {"注册资本": 800}, "expectedOutput": {"matched": False}},
            {"input": {"注册资本": 3000}, "expectedOutput": {"matched": False}},
            {"input": {"公司名称": "X"}, "expectedOutput": {"matched": True}},
        ],
    )
    say("规则已物化（生产上由机制 4 的「建议 → 接受」产出）")

    # 6.5 dry-run：三态
    dry = await call(
        client, "POST", f"/wiki/pages/{PAGE_RULE}/rules/dry-run", body={}
    )
    show("dry-run 汇总", dry["summary"])
    check(
        (dry["summary"]["passed"], dry["summary"]["failed"], dry["summary"]["undecidable"])
        == (1, 1, 1),
        "dry-run 三态齐备（通过 1 / 失败 1 / 判不了 1）",
    )

    # Agent 直读同一条规则 —— 走 ACL 门禁 + AgentTool 装配
    agentRun = await runAgent(client, "WIKI_RULE_AGENT", PAGE_RULE)
    check(agentRun["tool"] == "rule_evaluate", "Agent 命中 rule_evaluate 工具")
    check(
        agentRun["result"]["summary"] == dry["summary"],
        "Agent 报的三态与 HTTP dry-run 完全一致",
    )
    check(
        agentRun["tokensUsed"] == 0 and agentRun["cost"] == 0.0,
        "零 token / 零成本（只读工具，没有 LLM 调用）",
    )
    print(f"  ↳ Agent 原话: {agentRun['answer']}")

    readRun = await runAgent(client, "WIKI_READ_AGENT", PAGE_RULE)
    check(
        readRun["result"]["rule"]["ruleKind"] == "THRESHOLD",
        "wiki_read 也看得到这条规则（含 targetEntity / 样例数）",
    )


# ---------------------------------------------------------------------------
# Demo 2：跨条目关系 —— 候选 ≠ 事实
# ---------------------------------------------------------------------------


async def demo2(client: httpx.AsyncClient) -> None:
    step("demo 2", "跨条目关系：候选 → 确认 → Agent 视角切换")

    await call(
        client,
        "POST",
        "/wiki/pages",
        body={
            "pageId": PAGE_OBJ,
            "title": "供应商主数据定义（演示）",
            "content": "供应商 = 与企业发生采购往来的法人主体。",
            "dimension": "OBJECT",
        },
        expect=201,
    )
    relationId = await _seedRelationCandidate(
        PAGE_RULE, "PAGE", PAGE_OBJ, "REFERENCES"
    )
    say(f"关系候选已落库（生产上由机制 2 发现产出）：#{relationId} confirmed=false")

    rows = await call(client, "GET", f"/wiki/pages/{PAGE_RULE}/relations")
    show("管理面 /relations", [{"id": r["id"], "confirmed": r["confirmed"]} for r in rows])
    check(
        any(r["id"] == relationId and r["confirmed"] is False for r in rows),
        "管理面看得到未审核的候选（审核工作台的输入）",
    )

    before = await runAgent(client, "WIKI_READ_AGENT", PAGE_RULE)
    check(
        before["result"]["relations"] == [],
        "Agent 看不到这条候选 —— 机器猜的东西不会绕过审核变成对外答案",
    )

    confirmed = await call(client, "POST", f"/wiki/relations/{relationId}/confirm")
    check(confirmed["confirmed"] is True, "关系已确认（写回学习闭环反馈）")

    after = await runAgent(client, "WIKI_READ_AGENT", PAGE_RULE)
    show("Agent 视角", after["result"]["relations"])
    check(
        [r["downstreamId"] for r in after["result"]["relations"]] == [PAGE_OBJ],
        "确认之后 Agent 立刻看得到（同一次进程，无需重启/刷缓存）",
    )


# ---------------------------------------------------------------------------
# Demo 3：矛盾检测（确定性三路，零 token）
# ---------------------------------------------------------------------------


async def demo3(client: httpx.AsyncClient) -> None:
    step("demo 3", "矛盾检测：确定性三路（不调模型）")

    await call(
        client,
        "POST",
        "/wiki/pages",
        body={
            "pageId": PAGE_EXPIRED,
            "title": "旧的准入办法（演示）",
            "content": "本办法自 2023 年起废止。",
            "dimension": "POLICY",
        },
        expect=201,
    )
    # 让它失效 —— 走 PATCH，而不是直接改库：失效是产品的合法动作
    expired = await call(
        client, "PATCH", f"/wiki/pages/{PAGE_EXPIRED}", body={"status": "EXPIRED"}
    )
    check(expired["status"] == "EXPIRED", f"{PAGE_EXPIRED} 已置为 EXPIRED")

    await _seedRelationCandidate(PAGE_RULE, "PAGE", PAGE_EXPIRED, "REFERENCES")

    detected = await call(client, "POST", f"/wiki/pages/{PAGE_RULE}/conflicts/detect", body={})
    show("检测结果", {"llmStatus": detected["llmStatus"], "total": detected["total"]})
    check(
        detected["llmStatus"] == "SKIPPED",
        "llm_status=SKIPPED：没给模型就是「没要求跑」，不是「跑了没成」",
    )
    types = [c["conflictType"] for c in detected["conflicts"]]
    check("STALENESS" in types, f"检出引用已失效条目的冲突（{types}）")

    board = await call(client, "GET", "/wiki/conflicts")
    show("冲突看板", {"total": board["total"]})
    # 断言「刚才那条具体的冲突」在列表里，而不是「列表非空」—— 后者在库里有
    # 任何历史冲突时都会通过，绑定不到本次 demo 产出的那一行。
    check(
        any(c["id"] == detected["conflicts"][0]["id"] for c in board["rows"]),
        "看板能看到**这条**冲突（按 id 比对，不是「列表非空」）",
    )

    conflictId = detected["conflicts"][0]["id"]
    resolved = await call(
        client, "POST", f"/wiki/conflicts/{conflictId}/resolve", body={"action": "RESOLVED"}
    )
    check(resolved["resolvedAt"] is not None, "冲突已处置（RESOLVED 与 IGNORED 分开记）")


# ---------------------------------------------------------------------------
# Demo 4：覆盖度看板 ↔ Agent 同一份快照
# ---------------------------------------------------------------------------


async def demo4(client: httpx.AsyncClient) -> None:
    step("demo 4", "覆盖度：看板与 Agent 读同一份快照")

    refreshed = await call(client, "POST", "/wiki/coverage/refresh", body={})
    show(
        "刷新结果",
        {
            "cellCount": refreshed["cellCount"],
            "classCount": refreshed["classCount"],
            "removedCount": refreshed["removedCount"],
        },
    )

    board = await call(client, "GET", "/wiki/coverage/overview")
    show("看板 unlinked", board["unlinked"])
    # 断言**本次 demo 的三条**条目都在未挂载清单里（按维度归属核对），而不是
    # 「计数 ≥ 1」—— 后者在测试库里有任何历史孤儿条目时都会通过。三条条目
    # 分属 RULE/OBJECT/POLICY 三个维度，逐维度核对才能证明数的是它们。
    check(
        board["unlinked"]["pageCount"] >= 3
        and {"RULE", "OBJECT", "POLICY"} <= set(board["unlinked"]["dimensions"]),
        "demo 的三条条目（RULE/OBJECT/POLICY）都被认成未挂载 —— "
        "矩阵看不见的那部分被单独报出来了",
    )

    agentRun = await runAgent(client, "WIKI_COVERAGE_AGENT", "现在的知识覆盖度如何")
    check(agentRun["tool"] == "coverage_status", "Agent 命中 coverage_status 工具")
    # 这条是 M7+M8 的接缝：看板与 Agent 必须报同一组数字。工具若现场重算，
    # 两个界面就会各说各话，而「同一份数字被反复引用」正是看板的全部价值。
    check(
        agentRun["result"]["summary"] == board["summary"],
        "Agent 的汇总与 HTTP 看板逐字段一致（读的是同一份 coverage_cell 快照）",
    )
    check(
        agentRun["result"]["unlinked"]["pageCount"] == board["unlinked"]["pageCount"],
        "未挂载条目数也一致",
    )
    print(f"  ↳ Agent 原话: {agentRun['answer']}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def isTestDatabase(dbName: str) -> bool:
    """库名是否明显是测试库。

    按**命名段**判定，不用子串包含：``"test" in name`` 会把 ``contest`` /
    ``latest`` / ``attest`` 一并放行，而那正是这条闸要挡的形状（一个叫
    ``latest_dw`` 的库会被当成测试库写进去）。取 ``_`` 分段，要求 ``test``
    是整个段 —— ``qa_metadata_test`` / ``qa_test`` / ``test`` 都过，
    ``latest_dw`` / ``contest`` 不过。
    """
    return "test" in dbName.lower().split("_")


def _guardDatabase(allowNonTest: bool) -> str:
    """写库闸：默认只允许 test 库。返回库名用于 banner。"""
    from sqlalchemy.engine import make_url

    from app.config import getSettings

    url = make_url(getSettings().databaseUrl)
    dbName = url.database or ""
    if not allowNonTest and not isTestDatabase(dbName):
        raise SystemExit(
            f"✗ 拒绝执行：目标库 `{dbName}` 不像测试库（库名需含 `test` 这一命名段）。"
            "本脚本会**写数据**。\n"
            "  演示请指向 qa_metadata_test；确实要在别的库上跑，加 --allow-non-test-db。"
        )
    return dbName


async def main() -> None:
    parser = argparse.ArgumentParser(description="企业 Wiki 端到端演示")
    parser.add_argument("--allow-non-test-db", action="store_true")
    parser.add_argument("--keep-data", action="store_true", help="跑完不清理演示数据")
    args = parser.parse_args()

    silenceSql()  # 第一件事：后面的每一步都会建引擎，早关早清静
    dbName = _guardDatabase(args.allow_non_test_db)
    print(f"目标库：{dbName}")

    # 1) 注册 Agent（含 4 个知识工具 Agent）。必须在 app 起来之前跑：seedAgents
    #    自带 tool config seed + registry warmUp，独立执行不依赖 lifespan。
    from scripts.seed_agents import main as seedAgentsMain

    step("setup", "注册 Agent（9 个，含 4 个知识工具 Agent）")
    await seedAgentsMain()

    cleaned = await _resetDemoData()
    if cleaned["pages"]:
        say(f"清理上一次的演示数据：{_countsText(cleaned)}")

    app = createApp()
    # 真 lifespan（seed 工具元数据 / 绑定 / registry warmUp / 菜单基线）。
    # lifespan 只由 ASGI server 或 lifespan_context 触发；ASGITransport 不会自己跑，
    # 少了这一步，Agent 运行会因 registry 未 warmUp 抛 RuntimeError。
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            await demo1(client)
            await demo2(client)
            await demo3(client)
            await demo4(client)

    if not args.keep_data:
        cleaned = await _resetDemoData()
        say(f"已清理演示数据：{_countsText(cleaned)}（--keep-data 可保留）")

    print(f"\n{'=' * 72}\n✅ 4 个端到端 demo 全部通过\n{'=' * 72}")


if __name__ == "__main__":
    asyncio.run(main())
