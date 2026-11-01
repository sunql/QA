"""chat 输出中文化端到端（真实 PG + 完整 API 链路）。"""

from __future__ import annotations

from sqlalchemy import update

from app.domain.models import OntologyProperty
from app.tests.integration.test_chat_api import (
    _chat_payload,
    _installFakes,
    _seed,
)


async def _governQty(session, *, alias: str | None, unit: str | None) -> None:
    await session.execute(
        update(OntologyProperty)
        .where(OntologyProperty.property_name == "QTY")
        .values(property_alias=alias, unit=unit)
    )
    await session.commit()


class TestChatFieldLabels:
    async def test_answer_hints_and_column_labels(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        await _governQty(dbSession, alias="收货数量", unit="件")
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # 图例：series 名带中文 + 单位
        assert body["chartOption"]["series"][0]["name"] == "收货数量（件）"
        # 表头映射：只收录治理过的列，未治理列缺键 ⇒ 前端回落原始列名。
        # 本用例是 bar 图：`columnLabels` 挂在明细表负载 `tableOption` 上，
        # 柱状图的 `chartOption` 只有 series/坐标轴，没有 `columnLabels`。
        labels = body["tableOption"]["columnLabels"]
        assert labels["QTY"] == "收货数量（件）"
        assert "NAME" not in labels
        # 治理提示：NAME 缺中文含义；QTY 有单位 ⇒ 不缺单位
        hints = body["chartOption"]["fieldHints"]
        assert hints["missingLabels"] == ["NAME"]
        assert hints["missingUnits"] == []
        assert "字段治理提示" in body["answer"]
        assert "NAME" in body["answer"]

    async def test_no_hint_when_every_field_governed(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        await dbSession.execute(
            update(OntologyProperty).values(property_alias="已治理")
        )
        await _governQty(dbSession, alias="已治理", unit="件")
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "字段治理提示" not in body["answer"]
        assert body["chartOption"]["fieldHints"] == {
            "missingLabels": [],
            "missingUnits": [],
        }
