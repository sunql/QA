"""seed_feature_rules 幂等 upsert + 与 seed_agent_tool_configs 模式一致。"""
from sqlalchemy import select

from app.domain.models import FeatureRule
from scripts.seed_feature_rules import seedFeatureRules


async def test_seed_creates_4_rules(dbSession) -> None:
    n = await seedFeatureRules(dbSession)
    await dbSession.commit()
    assert n == 4
    rows = (await dbSession.execute(select(FeatureRule))).scalars().all()
    codes = {r.code for r in rows}
    assert codes == {
        "supplier_risk_score_main", "supplier_otd_high_risk",
        "supplier_defect_high_risk", "supplier_price_var_high_risk",
    }


async def test_seed_is_idempotent(dbSession) -> None:
    n1 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    n2 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    assert n1 == 4
    assert n2 == 0  # 第二次无变更


async def test_seed_risk_score_has_3_tiers(dbSession) -> None:
    await seedFeatureRules(dbSession)
    await dbSession.commit()
    rule = (await dbSession.execute(
        select(FeatureRule).where(FeatureRule.code == "supplier_risk_score_main")
    )).scalar_one()
    sevs = sorted([t.severity for t in rule.thresholds])
    assert sevs == ["HIGH", "LOW", "MEDIUM"]  # 3 tier RISK_SCORE
