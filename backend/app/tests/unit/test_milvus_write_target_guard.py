"""Milvus 写入目标闸门（维护脚本写库前的 deny-by-default 闸）。

与 ``app/tests/milvus_isolation.py::guardIsolatedDatabase()`` **不是**同一件事：
那个是**测试**闸门（直接禁掉 ``default`` 库），这个是**维护脚本**闸门
（默认禁、显式 opt-in 放行）。两者语义不同，故意不合并。
"""
from __future__ import annotations

import pytest

from scripts.milvus_write_target_guard import (
    PROD_MILVUS_DB_NAME,
    assertSafeMilvusTarget,
    describeMilvusTarget,
)


def test_prod_milvus_db_is_rejected_without_explicit_optin() -> None:
    """生产库（default）默认拒绝 —— Milvus 无备份，误写不可逆。"""
    with pytest.raises(SystemExit) as exc:
        assertSafeMilvusTarget(PROD_MILVUS_DB_NAME, allowProdWrite=False)

    assert "拒绝执行" in str(exc.value)
    assert "--allow-prod" in str(exc.value)


def test_prod_milvus_db_is_allowed_with_explicit_optin() -> None:
    """显式 opt-in 后放行，返回归一后的库名。"""
    assert (
        assertSafeMilvusTarget(PROD_MILVUS_DB_NAME, allowProdWrite=True)
        == PROD_MILVUS_DB_NAME
    )


def test_isolated_db_is_allowed_without_optin() -> None:
    """测试库 / 隔离库免 opt-in。"""
    assert assertSafeMilvusTarget("qa_test", allowProdWrite=False) == "qa_test"


def test_empty_db_name_falls_back_to_prod_name_and_is_rejected() -> None:
    """空配置归一到 ``default``（生产）⇒ 仍然必须拒绝，不能当成"没指定=安全"。"""
    with pytest.raises(SystemExit):
        assertSafeMilvusTarget("", allowProdWrite=False)

    with pytest.raises(SystemExit):
        assertSafeMilvusTarget("   ", allowProdWrite=False)


def test_describe_target_shows_host_port_and_db() -> None:
    """目标描述要含 host:port 与库名。"""
    described = describeMilvusTarget("http://milvus-host:19530", "qa_test")

    assert described == "milvus://milvus-host:19530/qa_test"


def test_describe_target_never_leaks_credentials() -> None:
    """URI 里的口令不得出现在目标描述里（否则会进日志 / CI 输出）。"""
    described = describeMilvusTarget(
        "https://someuser:sup3r-s3cret@milvus-host:19530", "qa_test"
    )

    assert "sup3r-s3cret" not in described
    assert "someuser" not in described
    assert "milvus-host:19530" in described


def test_describe_target_defaults_host_and_port() -> None:
    """空 URI ⇒ localhost:19530（与 milvus_client._connect 的兜底一致）。"""
    described = describeMilvusTarget("", "qa_test")

    assert described == "milvus://localhost:19530/qa_test"
