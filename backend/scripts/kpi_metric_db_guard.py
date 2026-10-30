"""KPI 指标工具链的**目标库守卫**（回填脚本与真机验证脚本共用）。

⚠️ 本模块**不 import app.***：真机验证脚本必须在设置 ``DATABASE_URL`` 之后
才能 import ``app.config``（app.config 在导入期读环境变量），因此库名解析
必须留在 app 导入之前 —— 这也是本模块单独成文件的原因。

白名单
------
``qa_metadata``（**正式库 / prod 同源**，含 35 万 entity_mapping 等真实业务数据）
与 ``qa_metadata_test*``（隔离测试库）允许写入；**其它任何库名一律拒绝**。

四道闸（T15-W）
--------------
1. **显式目标，不猜**：调用方必须显式传入 DSN（本模块不做 env 兜底猜测）；
2. **库名白名单**：``^qa_metadata(_test.*)?$``；
3. **prod 写入需显式 opt-in**：目标库是 ``qa_metadata`` 时必须额外传
   ``allowProdWrite=True``（CLI 对应 ``--allow-prod-write``），否则 ``SystemExit``；
4. **prod 写入需备份**：由调用方（回填脚本）在写库前完成当日备份。

⚠️ **dev 与 prod 同名**（都叫 ``qa_metadata``）⇒ 库名白名单在原理上**无法**
区分二者 ⇒ 闸门 3/4 才是真正挡住误写 prod 的那道闸。**不要**把
"白名单挡住了生产库"当成安全保证。
"""

from __future__ import annotations

import re

# 库名白名单：正式库 qa_metadata（prod 同源） + 隔离测试库 qa_metadata_test*。
# ⚠️ 改动时必须与调用方（scripts/kpi_metric_backfill.py）行为保持一致 ——
# 两者共用本模块，不存在第二份正则。
ALLOWED_DB_PATTERN = re.compile(r"^qa_metadata(_test.*)?$")

# 正式库（prod 同源）库名 —— 提成具名常量，不散落字面量。
PROD_DB_NAME = "qa_metadata"


def databaseNameOf(dsn: str) -> str:
    """从 DSN 取库名（去掉查询串 / 参数 / fragment）。

    形如 ``postgresql+asyncpg://user:pwd@host:5434/qa_metadata_test?ssl=require``
    ⇒ ``qa_metadata_test``；没有路径 / 不是 DSN ⇒ 空串（**不抛异常**）。
    """
    if not dsn:
        return ""
    withoutQuery = dsn.split("?", 1)[0].split("#", 1)[0].strip()
    if "://" not in withoutQuery:
        return ""
    hostAndPath = withoutQuery.split("://", 1)[1].rsplit("@", 1)[-1]
    _, separator, path = hostAndPath.partition("/")
    if not separator:
        return ""
    return path.strip("/")


def assertSafeTarget(databaseUrl: str, *, allowProdWrite: bool = False) -> str:
    """从 DSN 取库名并做白名单校验；返回校验通过的库名。

    - 库名不在白名单 ⇒ ``SystemExit``（挡「写到别的库」）；
    - 库名是正式库 ``qa_metadata``（prod 同源）⇒ 必须显式
      ``allowProdWrite=True``（且调用方还要满足备份前置，见 T15-B），
      否则 ``SystemExit``；
    - 测试库（``qa_metadata_test*``）免 opt-in。

    ⚠️ 签名是 Task 16 共享契约，**一字不得改**。
    """
    name = databaseNameOf(databaseUrl)
    if not ALLOWED_DB_PATTERN.match(name):
        raise SystemExit(
            f"拒绝执行：目标库 {name!r} 不在白名单内。"
            "本工具链只允许在 qa_metadata / qa_metadata_test* 库运行。"
        )
    if name == PROD_DB_NAME and not allowProdWrite:
        raise SystemExit(
            f"拒绝执行：目标库 {name!r} 是正式库（prod 同源）。"
            "写正式库必须显式加 --allow-prod-write，且必须先完成当日备份。"
        )
    return name


def maskDsn(dsn: str) -> str:
    """口令一律打码（``user:***@host``）。没有凭据 ⇒ 整体打码。"""
    if "@" not in dsn or "://" not in dsn:
        return "***"
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"
