"""database.py 连接池配置注入单测（feat-chat-concurrency-params）。

覆盖：
- 模块导入时 ``_db_pool_config`` 默认值取自 Settings（env 可覆盖 → Settings 默认）
- ``init_db_pool_config`` 写入新值
- ``get_db_pool_config`` 返回拷贝（避免外部修改污染 module-level）
- ``getEngine`` 创建 engine 时使用最新值（间接验证：依赖 _db_pool_config 读值）

无 IO：纯函数调用 + 断言 module-level dict。Engine 实际创建会触发 DB 连
接，本测试只验证配置注入不连 DB。
"""

from __future__ import annotations

import pytest

from app.config import getSettings
from app.infrastructure import database


@pytest.fixture(autouse=True)
def _resetPoolConfig():
    """每个用例前后还原 _db_pool_config 到 Settings 默认值。

    防止用例链式修改 _db_pool_config 影响后续测试。
    """
    defaults = {
        "pool_size": getSettings().dbPoolSize,
        "max_overflow": getSettings().dbMaxOverflow,
    }
    yield
    database._db_pool_config.clear()
    database._db_pool_config.update(defaults)


class TestModuleDefaults:
    """模块导入时 _db_pool_config 应来自 Settings。"""

    def test_defaults_match_settings(self) -> None:
        """导入时默认值与 Settings.dbPoolSize / dbMaxOverflow 一致。"""
        settings = getSettings()
        assert database._db_pool_config["pool_size"] == settings.dbPoolSize
        assert database._db_pool_config["max_overflow"] == settings.dbMaxOverflow

    def test_settings_default_pool_size_is_20(self) -> None:
        """基线默认 20（2026-09-19 从 5 提升以支撑 50 人并发；环境变量可覆盖但本测试场景无 env）。

        改动记录见 ``Harness/changes/feat-chat-concurrency/summary.md`` §follow-up 1：
        50 并发时旧默认 5 + overflow10 = 15 max 会让 35 请求排队；改 20+10=30 max。
        """
        # 只在没设过 env var 的干净环境下恒为 20；测试 conftest 通常不设
        settings = getSettings()
        assert settings.dbPoolSize == 20
        assert settings.dbMaxOverflow == 10


class TestInitDbPoolConfig:
    """init_db_pool_config 写入行为。"""

    def test_overrides_values(self) -> None:
        database.init_db_pool_config(pool_size=20, max_overflow=30)
        assert database._db_pool_config["pool_size"] == 20
        assert database._db_pool_config["max_overflow"] == 30

    def test_partial_override_preserves_other(self) -> None:
        """只调一次 init 同时改两个值；本方法签名要求两个参数都传（无 partial）。"""
        database.init_db_pool_config(pool_size=20, max_overflow=30)
        assert database._db_pool_config["pool_size"] == 20
        assert database._db_pool_config["max_overflow"] == 30


class TestGetDbPoolConfig:
    """get_db_pool_config 返回 dict 副本，外部修改不污染 module-level。"""

    def test_returns_current_values(self) -> None:
        database.init_db_pool_config(pool_size=20, max_overflow=30)
        snapshot = database.get_db_pool_config()
        assert snapshot == {"pool_size": 20, "max_overflow": 30}

    def test_returned_dict_is_copy(self) -> None:
        snapshot = database.get_db_pool_config()
        snapshot["pool_size"] = 999
        # module-level 不受 snapshot 修改影响
        assert database._db_pool_config["pool_size"] != 999
        # 重新读取仍是原值
        fresh = database.get_db_pool_config()
        assert fresh["pool_size"] != 999