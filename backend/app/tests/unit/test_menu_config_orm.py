"""MenuConfig ORM 基本属性测试。

不依赖 DB session，只验证 dataclass-like 列映射。
"""

from __future__ import annotations


def test_menu_config_tablename_is_menu_config() -> None:
    from app.domain.models import MenuConfig

    assert MenuConfig.__tablename__ == "menu_config"


def test_menu_config_columns_present() -> None:
    from app.domain.models import MenuConfig

    expected = {
        "id",
        "code",
        "parent_id",
        "label_key",
        "path",
        "icon_code",
        "sort_order",
        "permission_code",
        "roles",
        "visible",
        "created_time",
        "updated_time",
    }
    assert set(MenuConfig.__table__.columns.keys()) >= expected