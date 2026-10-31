"""回填 CLI 的写库目标守卫（A1 ③）。

两道闸都要测：库名白名单（deny-by-default，复用 kpi_metric_db_guard）与环境名。
只测其中一道会让另一道悄悄失效 —— 例如白名单挡住 `scratch`，但同名 dev/prod
都叫 `qa_metadata`，只有 APP_ENV 那一路能区分。
"""
from __future__ import annotations

import pytest

from scripts.backfill_id_mapping import assertSafeWriteTarget

TEST_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/qa_metadata_test"
SOLO_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/qa_metadata_test_solo"
PROD_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5433/qa_metadata"
SCRATCH_URL = "postgresql+asyncpg://qa_user:pw@127.0.0.1:5434/scratch"


@pytest.mark.parametrize(
    "databaseUrl, appEnv, allowProdWrite, expectedName",
    [
        (TEST_URL, "development", False, "qa_metadata_test"),
        (SOLO_URL, "development", False, "qa_metadata_test_solo"),
        (PROD_URL, "development", True, "qa_metadata"),
        (TEST_URL, "PROD", True, "qa_metadata_test"),
    ],
)
def test_accepts_safe_write_targets(
    databaseUrl: str, appEnv: str, allowProdWrite: bool, expectedName: str
) -> None:
    assert (
        assertSafeWriteTarget(
            appEnv=appEnv, databaseUrl=databaseUrl, allowProdWrite=allowProdWrite
        )
        == expectedName
    )


@pytest.mark.parametrize(
    "databaseUrl, appEnv, allowProdWrite, reason",
    [
        (PROD_URL, "development", False, "--allow-prod"),  # 库名 = 正式库
        (TEST_URL, "production", False, "生产环境"),  # APP_ENV 说生产
        (SOLO_URL, "prod", False, "生产环境"),
        (SCRATCH_URL, "development", True, "白名单"),  # 白名单外，opt-in 也不放行
        ("not-a-dsn", "development", True, "白名单"),  # 解析不出库名 ⇒ 一律拒绝
    ],
)
def test_refuses_unsafe_write_targets(
    databaseUrl: str, appEnv: str, allowProdWrite: bool, reason: str
) -> None:
    with pytest.raises(SystemExit) as excInfo:
        assertSafeWriteTarget(
            appEnv=appEnv, databaseUrl=databaseUrl, allowProdWrite=allowProdWrite
        )
    assert reason in str(excInfo.value)


def test_refusal_names_this_scripts_flag_not_the_kpi_one() -> None:
    """守卫模块的文案是 KPI 工具链的开关名；本脚本必须说 --allow-prod。"""
    with pytest.raises(SystemExit) as excInfo:
        assertSafeWriteTarget(
            appEnv="development", databaseUrl=PROD_URL, allowProdWrite=False
        )
    message = str(excInfo.value)
    assert "--allow-prod" in message
    assert "--allow-prod-write" not in message
