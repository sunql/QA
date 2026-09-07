"""一次性 Neo4j label 重命名脚本（Phase 4.4）。

策略：
  - Material → ItemMaster
  - GoodsReceipt → Receipt
  - NCR 节点 → DETACH DELETE（NCR 不再支持入图）

幂等：重复运行结果一致。
  第二次运行时 MATCH (n:Material) 返回 0（已被重命名）→ 0 变更；
  MATCH (n:NCR) 返回 0（已被删除）→ 0 删除。
  SET n:NewLabel REMOVE n:OldLabel 对已转换节点无副作用。
"""
from __future__ import annotations

from neo4j import Driver

# 标签来自受控常量（非用户输入），CQL 拼接防注入在此上下文中不适用
RENAMES: dict[str, str] = {
    "Material": "ItemMaster",
    "GoodsReceipt": "Receipt",
}


def rename_neo4j_labels(driver: Driver) -> dict[str, int]:
    """执行 label 重命名 + NCR 节点删除。返回每个操作的节点数."""
    stats: dict[str, int] = {}
    with driver.session() as session:
        for old, new in RENAMES.items():
            result = session.run(
                f"MATCH (n:{old}) SET n:{new} REMOVE n:{old} RETURN count(n) AS cnt"
            )
            stats[old] = result.single()["cnt"]

        # 删除 NCR 节点
        result = session.run("MATCH (n:NCR) DETACH DELETE n RETURN count(n) AS cnt")
        stats["NCR_deleted"] = result.single()["cnt"]
    return stats


async def main() -> None:
    from app.config import getSettings
    from app.infrastructure.neo4j_client import getDriver

    getSettings()  # 触发 settings 初始化
    driver = getDriver()
    stats = rename_neo4j_labels(driver)
    print(f"[rename_neo4j_labels] 完成: {stats}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
