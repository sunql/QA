"""seed_feature_rules 幂等 upsert + 与 seed_agent_tool_configs 模式一致。"""
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.domain.models import FeatureRule
from scripts.seed_feature_rules import seedFeatureRules


async def test_seed_creates_4_rules(dbSession) -> None:
    # conftest pre-seeds; truncate to test seed function in isolation
    await dbSession.execute(text(
        "TRUNCATE TABLE feature_rule_threshold, feature_rule RESTART IDENTITY CASCADE"
    ))
    await dbSession.commit()
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
    # conftest pre-seeds; truncate to test seed function in isolation
    await dbSession.execute(text(
        "TRUNCATE TABLE feature_rule_threshold, feature_rule RESTART IDENTITY CASCADE"
    ))
    await dbSession.commit()
    n1 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    n2 = await seedFeatureRules(dbSession)
    await dbSession.commit()
    assert n1 == 4
    assert n2 == 0  # 第二次无变更


async def test_seed_risk_score_has_3_tiers(dbSession) -> None:
    await seedFeatureRules(dbSession)
    await dbSession.commit()
    # 必须 eager-load：`Session` 的 identity map 是弱引用，clean 实例可能被 GC
    # 回收；届时再次 SELECT 会造出 thresholds 未加载的新实例，而 async 下的同步
    # 懒加载会抛 MissingGreenlet（对象存活与否取决于分配时机 ⇒ 用例时红时绿）。
    rule = (await dbSession.execute(
        select(FeatureRule)
        .options(selectinload(FeatureRule.thresholds))
        .where(FeatureRule.code == "supplier_risk_score_main")
    )).scalar_one()
    sevs = sorted([t.severity for t in rule.thresholds])
    assert sevs == ["HIGH", "LOW", "MEDIUM"]  # 3 tier RISK_SCORE
