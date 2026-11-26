"""待到货统计路由的双接线守卫（main + _testapp 漏一边 ⇒ 那一侧 404）。"""

from __future__ import annotations

from pathlib import Path


def test_pending_delivery_summary_registered_in_both_wirings() -> None:
    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_analytics" in code, f"{rel} 未导入 analytics 路由模块"
        assert "procurement_analytics.router" in code, (
            f"{rel} 未注册 pending-delivery/summary（漏一边 ⇒ 那一侧 404）"
        )
