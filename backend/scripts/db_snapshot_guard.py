"""写库前备份闸门：备份 / 打码 / 目标库复核的**共享**原语。

为什么单独成模块
----------------
两个脚本都要「写正式库前先备份 + 失败必须打码 + 目标库必须复核」：

- ``scripts/kpi_metric_backfill.py``（KPI 指标回填，早期实现）
- ``scripts/semantic_publish_snapshot.py``（语义发布快照，P2 / D12）

各写一份必然漂移 —— 语义发布脚本初版就**丢了**其中七条硬约束
（安全评审 H1 / H3 / M1–M7）。本模块是这两处的**唯一实现**。

七条硬约束（每条都有对应的评审条目）
------------------------------------
1. **口令永不进 argv**：`pg_dump` 的口令走 `PGPASSWORD` 环境变量（FIX-I2）。
2. **备份文件名只允许 ``[A-Za-z0-9_.-]``**：revision 未经清洗会拼出路径穿越
   （H3）。``..`` 与前导点一并折叠/去掉。
3. **备份临时文件用 ``O_CREAT|O_EXCL|O_NOFOLLOW`` + 0600**：绝不跟随符号链接、
   绝不覆盖既有文件（M1）。
4. **备份文件是符号链接 ⇒ 拒绝复用**（M2）：否则「复用当日备份」可以被一个
   预置的软链骗过，写库时就没有真正的回滚点。
5. **备份不完整 ⇒ 拒绝写正式库**（M3）：``pg_dump`` 说「no matching tables」
   时那张表根本没被备份，不能当作备份成功。
6. **目标库必须用 ``SELECT current_database()`` 复核**（H1）：DSN 的 query string
   能覆盖驱动**实际连接**的 host / database（asyncpg 会把 ``url.query`` 合并进
   连接参数），路径上解析出来的库名不作数。
7. **任何错误输出都要打码 DSN / 口令**（M6 / M7）：驱动与 ``pg_dump`` 的报错
   可能回显连接串。

第八条（安全评审 HIGH）：**备份目标与写入目标必须同源**
--------------------------------------------------------
旧实现里 ``dumpTarget`` 手搓 ``pg_dump -h/-p/-U/-d``（只读 DSN **路径**上的
host/port），而驱动把 DSN 的 **query string** 合并进连接参数 ⇒

    postgresql+asyncpg://u:pw@localhost:5433/qa_metadata?host=evil.internal&port=6432

驱动**实际连的是** ``evil.internal:6432``，而 ``pg_dump`` 备份的是
``localhost:5433`` ⇒ **备份的是 A 库、写入的是 B 库** ⇒ 正式库写入**没有
回滚点**。而且 ``current_database()`` 看不出来 —— 两台机器上库名相同。

⇒ 两处同时修：

- `dumpTarget` 的目标**从驱动自己的 connect kwargs 取**（同一个来源 ⇒ 同值）；
- `assertNoTargetOverride` **拒绝**会改目标的 query 参数（写路径 fail-closed）。
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 备份目录 / 文件权限：目录 0700（仅本人可进），文件 0600（仅本人可读）。
BACKUP_DIR_MODE = 0o700
BACKUP_FILE_MODE = 0o600

# `pg_dump` 的硬超时（秒）。没有超时的话，DSN 里没口令而服务端要口令时，
# 进程会挂在交互式提示上（stdin 被继承）—— 整个流水线静默卡死。
PG_DUMP_TIMEOUT_SECONDS = 60

# `pg_dump` 报错文本的截断长度：只保留尾部（真正的失败原因在最后一行），
# 同时避免把整段 stderr 灌进日志。
PG_DUMP_STDERR_TAIL_CHARS = 500

# 口令长度 ≥ 该值时才做**全局**替换；更短的口令只在凭据语境里替换
# （见 `scrubSecrets`）—— 全局替换会把 `pg_dump` 这类正常词一起打码。
MIN_GLOBAL_SCRUB_PASSWORD_LEN = 4

# DSN query string 的**白名单**：只允许「不改连接目标、不改身份」的参数。
# 任何会改 host / port / database / 用户的参数都在拒绝名单里（见
# ``assertNoTargetOverride``）—— 目标必须写在 DSN 路径上，一眼可见。
#
# ⚠️ 白名单里的每个 key 都必须是 **asyncpg.connect() 真的认**的参数
# （否则连接时 `TypeError: connect() got an unexpected keyword argument ...`）。
# 实测 asyncpg 0.31 的签名里**没有** `sslmode` / `application_name` /
# `connect_timeout` —— 而 SQLAlchemy 的 asyncpg 方言把 `url.query` **原样**
# 并进 connect kwargs ⇒ 白名单里放这三个 key 就会在连接时炸，且这个
# `TypeError` 不是 `DBAPIError` ⇒ 会**逃出**所有错误处理 ⇒ 裸 traceback
# 取代「打码 DSN 的干净退出」。
# ⇒ 只留「语义上认可」的 key，再由 `_normalizeQuery` 显式翻译成驱动参数名。
ALLOWED_QUERY_KEYS = frozenset(
    {
        "sslmode",
        "ssl",
        "connect_timeout",
        "command_timeout",
    }
)

# query key → asyncpg.connect() 的参数名。**显式翻译，不直传**。
# `sslmode` 是 libpq 的叫法，驱动叫 `ssl`；`connect_timeout` 驱动叫 `timeout`。
QUERY_KEY_TO_DRIVER_KEY = {
    "sslmode": "ssl",
    "ssl": "ssl",
    "connect_timeout": "timeout",
    "command_timeout": "command_timeout",
}

# asyncpg 的 SSL 模式（`SSLMode.parse` 只认这几个；`ssl=true` 会抛
# ClientConfigurationError ⇒ 必须在闸门处按字面量校验后拒绝）。
ALLOWED_SSL_MODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

# 秒数类参数：必须是正整数。
ALLOWED_TIMEOUT_KEYS = frozenset({"connect_timeout", "command_timeout"})

# 打码时按「值替换」处理的 query 参数（口令 / 令牌类）。
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "sslpassword",
        "secret",
        "token",
    }
)


def safeRevision(revision: str) -> str:
    """revision 只允许 ``[A-Za-z0-9_.-]``（防 ``/``、``..`` 拼进路径）。

    单个 ``.`` 可保留；``..`` 与前导点会构成上跳 / 隐藏文件语义 ⇒ 折叠、去掉。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", revision or "")
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(".")
    return cleaned or "unknown"


def targetIdentity(databaseUrl: str) -> str:
    """从 DSN 派生 ``host_port_db``，清洗成 ``[A-Za-z0-9_.-]``。

    dev 与 prod 同名 ``qa_metadata`` ⇒ 备份文件名必须带 host/port 才能区分
    目标库，否则「复用当日备份」会把 dev 的备份当成 prod 的。

    端口取**实际端口**：DSN 省略端口时 libpq 与 asyncpg 都回落到
    ``$PGPORT``，再回落 5432 ⇒ 身份必须按同一规则算。写成 "no-port" 的话，
    ``$PGPORT`` 一变身份却不变 ⇒ 今天的备份被当成另一台机器的备份复用
    （回滚点是错的）。
    """
    try:
        url = make_url(databaseUrl)
    except (ArgumentError, ValueError):
        return "unknown-target"
    raw = f"{url.host or 'no-host'}_{_effectivePort(url)}_{url.database or 'unknown'}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def _effectivePort(url: Any) -> str:
    """DSN 省略端口时的**实际**端口：``$PGPORT``，再回落 5432。

    与 libpq / asyncpg 的回落规则一致（两者都读 ``$PGPORT``，都没有时用 5432）。
    """
    if url.port:
        return str(url.port)
    raw = os.environ.get("PGPORT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return raw
    return "5432"


def ensureBackupDir(directory: Path) -> Path:
    """创建（或复用）**私有**备份目录；权限只收紧不放宽。"""
    if directory.is_symlink():
        raise SystemExit(f"备份目录是符号链接 ⇒ 拒绝使用：{directory}")
    directory.mkdir(mode=BACKUP_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(directory, BACKUP_DIR_MODE)
    return directory


def backupPath(directory: Path, table: str, revision: str, databaseUrl: str) -> Path:
    """备份文件名带日期 + **目标库身份** + revision（跨库 / 旧版本可辨识）。"""
    name = (
        f"{table}_{date.today():%Y%m%d}_"
        f"{targetIdentity(databaseUrl)}_{safeRevision(revision)}.sql"
    )
    return ensureBackupDir(directory) / name


def openExclusive(path: Path) -> BinaryIO:
    """``O_EXCL | O_NOFOLLOW`` 打开文件：已存在 / 符号链接 ⇒ 失败，不覆盖。"""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            BACKUP_FILE_MODE,
        )
    except OSError as exc:
        # O_NOFOLLOW 命中符号链接时 POSIX 报 ELOOP（不是 FileExistsError），
        # 与 O_EXCL 的「已存在」一并按失败处理：绝不跟随 / 覆盖。
        raise SystemExit(
            f"备份临时文件已存在或不可安全创建：{path}"
            "（可能是符号链接 —— 拒绝跟随；也可能是上次运行残留的 .tmp 文件"
            " —— 请人工确认后清理再重跑）。"
        ) from exc
    return os.fdopen(fd, "wb")


def _parsedUrl(databaseUrl: str):
    """解析 DSN；不可解析 ⇒ 直接拒绝（不猜、不放过）。"""
    try:
        return make_url(databaseUrl)
    except (ArgumentError, ValueError) as exc:
        raise SystemExit(
            f"目标库 DSN 不可解析：{maskDatabaseUrl(databaseUrl)}"
        ) from exc


def _singleValue(value: Any) -> Any:
    """asyncpg 的 ``host`` 可以是列表 ⇒ 取第一个（``pg_dump`` 只认一个目标）。"""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _effectiveConnectArgs(databaseUrl: str) -> dict[str, Any]:
    """驱动**实际**使用的连接参数（host / port / database / user / password）。

    为什么必须走驱动：asyncpg 方言把 ``url.query`` 合并进 connect kwargs ⇒
    路径上的 host/port 可能不是真正连的那台机器。备份目标只有和这里**同源**，
    才谈得上「备份的就是要写的那一个」。
    """
    url = _parsedUrl(databaseUrl)
    try:
        dialect = url.get_dialect()()
        _, kwargs = dialect.create_connect_args(url)
    except (ArgumentError, ValueError, NoSuchModuleError, KeyError, TypeError) as exc:
        raise SystemExit(
            f"无法解析目标库的连接参数：{maskDatabaseUrl(databaseUrl)}"
        ) from exc
    return dict(kwargs)


def _pgEnvFromQuery(url) -> dict[str, str]:
    """把「不改连接目标」的 query 参数翻译成 ``pg_dump`` 认识的环境变量。

    不翻译的话：驱动要求 SSL（``?sslmode=require``）而 ``pg_dump`` 不要求
    ⇒ 备份与写入走的不是同一条连接路径（极端情况下备份连不上 ⇒ 没有回滚点）。

    归一化后的 DSN 里是**驱动参数名**（``ssl`` / ``timeout``），这里同时认
    libpq 的原名（``sslmode`` / ``connect_timeout``），两种形态都能翻译。
    """
    mapping = {
        "sslmode": "PGSSLMODE",
        "ssl": "PGSSLMODE",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "timeout": "PGCONNECT_TIMEOUT",
    }
    result: dict[str, str] = {}
    for key, value in dict(url.query).items():
        envName = mapping.get(str(key).lower())
        if envName and value is not None:
            result[envName] = str(value)
    return result


def _validateQueryValue(key: str, value: str) -> None:
    """校验白名单参数**取值**（只放行驱动真认的合法值）。

    为什么连取值也要校验：``?ssl=true`` 会让 asyncpg 抛
    ``ClientConfigurationError``，``?command_timeout=abc`` 会抛 ``ValueError``
    —— 两者都不是 ``DBAPIError`` ⇒ 会逃出打码出口 ⇒ 裸 traceback。
    ⇒ 非法取值在闸门处直接拒绝（fail-closed），错误信息说清合法值。
    """
    normalized = key.lower()
    if normalized in ALLOWED_TIMEOUT_KEYS:
        if not str(value).isdigit() or int(str(value)) <= 0:
            raise SystemExit(
                f"拒绝执行：DSN 的 query 参数 {key} 必须是正整数秒数，当前为 {value!r}。"
            )
        return
    if (
        normalized in {"ssl", "sslmode"}
        and str(value).lower() not in ALLOWED_SSL_MODES
    ):
        raise SystemExit(
            f"拒绝执行：DSN 的 query 参数 {key} 的取值非法：{value!r}。"
            f"只允许 {', '.join(sorted(ALLOWED_SSL_MODES))}。"
        )


def _normalizeQuery(databaseUrl: str) -> str:
    """把 query 参数**翻译成驱动参数名**（``sslmode``→``ssl`` 等）。

    为什么必须翻译而不是原样传给引擎：SQLAlchemy 的 asyncpg 方言把
    ``url.query`` 原样并进 ``connect()`` 的 kwargs，而 ``connect()`` 的签名里
    没有 ``sslmode`` / ``connect_timeout`` ⇒ 连接时 ``TypeError`` ⇒ 逃出
    错误处理 ⇒ 裸 traceback（且这个 TypeError 发生在**打码出口之外**）。
    ⇒ 白名单只认「语义」，这里负责把语义翻译成驱动真认的参数名。
    """
    url = _parsedUrl(databaseUrl)
    if not url.query:
        return databaseUrl
    translated: dict[str, str] = {}
    for key, value in dict(url.query).items():
        normalized = str(key).lower()
        driverKey = QUERY_KEY_TO_DRIVER_KEY.get(normalized)
        if driverKey is None:
            continue
        translated[driverKey] = str(value)
    return url.set(query=translated).render_as_string(hide_password=False)


def assertNoTargetOverride(databaseUrl: str) -> None:
    """拒绝会**改变连接目标**的 DSN query 参数（写路径 fail-closed）。

    为什么必须拦：asyncpg 方言把 ``url.query`` 合并进连接参数 ⇒
    ``?host=evil.internal&port=6432`` 让驱动连到**另一台机器**，而 DSN
    **路径**上的 host/port 不变 ⇒ 备份（读路径）与写入（读 query）不是同一个
    库 ⇒ 正式库写入**没有回滚点**，且 ``current_database()`` 看不出来
    （两台机器上库名相同）。

    ⇒ 只放行「不改目标也不改身份」且**驱动真认**的参数（TLS / 超时）；
    目标写在 DSN 路径上（一眼可见），口令写在 userinfo 或环境变量里。
    """
    url = _parsedUrl(databaseUrl)
    offending = sorted(
        str(key) for key in dict(url.query) if str(key).lower() not in ALLOWED_QUERY_KEYS
    )
    if offending:
        raise SystemExit(
            "拒绝执行：DSN 的 query 参数不被允许："
            f"{', '.join(offending)}。"
            f"只允许 {', '.join(sorted(ALLOWED_QUERY_KEYS))}"
            "（不改连接目标、且驱动真正支持的参数）。"
            "目标（host / port / database）请写进 DSN 路径，口令请写进 userinfo "
            "或环境变量。"
        )
    for key, value in dict(url.query).items():
        _validateQueryValue(str(key), str(value))


def assertExplicitTarget(databaseUrl: str) -> None:
    """DSN 必须**显式**写清目标主机（fail-closed）。

    为什么：DSN 省略 host 时，asyncpg 会回落到 ``$PGHOST`` / ``$PGPORT`` /
    unix socket ⇒ **DSN 文本上看不出会连到哪台机器**。而备份闸门要在
    「同一个目标」上做备份 ⇒ 目标必须是**看得见、写得出来**的那一个。
    ⇒ 目标写在 DSN 路径上（host/port/database），一眼可见、可复核。

    port 可以省略：省略时 libpq 与 asyncpg 都回落到 ``$PGPORT`` / 5432，
    两侧规则一致 ⇒ 不构成歧义。
    """
    url = _parsedUrl(databaseUrl)
    if not url.host:
        raise SystemExit(
            "拒绝执行：DSN 没有显式写主机（host）⇒ 实际连接目标会由 "
            "$PGHOST / $PGPORT / unix socket 决定，看不出会连到哪台机器。"
            "请把目标写进 DSN 路径（如 postgresql+asyncpg://用户@主机:5432/库名）。"
            f"当前为：{maskDatabaseUrl(databaseUrl)}"
        )


def assertNoPasswordInArgv(databaseUrl: str, *, source: str) -> None:
    """显式参数里的 DSN 不许带口令。

    口令进 argv ⇒ 进 ``ps`` 输出 / shell 历史 / CI 日志 ⇒ 换一个终端就能看到。
    ⇒ 显式参数只放「目标」，口令走环境变量（``--database-url-env NAME``）。
    """
    url = _parsedUrl(databaseUrl)
    if url.password:
        raise SystemExit(
            f"拒绝执行：{source} 里的 DSN 含口令 ⇒ 口令会进 argv"
            "（ps / shell 历史 / CI 日志）。请改用 --database-url-env 指定环境变量名。"
        )


def maskDatabaseUrl(dsn: str) -> str:
    """打码 DSN 的**全部**凭据：userinfo 口令 + query 里的口令类参数。

    为什么不用 ``scripts.kpi_metric_db_guard.maskDsn``：它按**最后一个** ``@``
    切 userinfo ⇒ query 里带 ``@`` 时会把 query 误当 host；而 query 恰恰是
    最容易藏口令的地方（``?password=…``）。
    """
    if not dsn or "://" not in dsn:
        return "***"
    scheme, rest = dsn.split("://", 1)
    head, separator, query = rest.partition("?")
    userinfo, at, hostPart = head.rpartition("@")
    if not at:
        # 与 maskDsn 同口径：没有凭据 ⇒ 整体打码。
        return "***"
    user, colon, _password = userinfo.partition(":")
    maskedUserinfo = f"{user}:***" if colon else "***"
    maskedHead = f"{maskedUserinfo}@{hostPart}"
    if not separator:
        return f"{scheme}://{maskedHead}"
    maskedQuery = "&".join(_maskQueryChunk(chunk) for chunk in query.split("&"))
    return f"{scheme}://{maskedHead}?{maskedQuery}"


def _maskQueryChunk(chunk: str) -> str:
    """query 里的口令类参数按值打码；其余原样保留（便于排查连接参数）。"""
    key, separator, _value = chunk.partition("=")
    if separator and key.lower() in SENSITIVE_QUERY_KEYS:
        return f"{key}=***"
    return chunk


def dumpTarget(databaseUrl: str) -> tuple[list[str], dict[str, str]]:
    """把 DSN 拆成 argv 前缀 + 环境变量（口令永不进 argv）。

    ⚠️ **目标从驱动自己的 connect kwargs 取**（不是手搓 DSN 路径）：
    asyncpg 方言把 DSN 的 query string 合并进连接参数 ⇒ 路径上的 host/port
    可能不是真正连的那个 ⇒ 备份 A 库、写入 B 库 ⇒ 正式库写入**没有回滚点**。
    ⇒ 备份目标与写入目标必须**同一个来源**。

    ⚠️ **绝不替驱动「补默认值」**：DSN 省略 host/port 时，asyncpg 会回落到
    ``$PGHOST`` / ``$PGPORT``（或 unix socket）⇒ 若这里兜底成
    ``localhost:5432``，备份的是 A 库、写入的是 B 库 ⇒ **同一类不变量被
    换个门绕过**（安全评审 H-1 实测复现）。
    ⇒ 取不到就不下发 ``-h`` / ``-p`` / ``-U``，让 libpq 与 asyncpg 用
    **同一套**环境变量 / socket 解析规则。
    """
    try:
        url = make_url(databaseUrl)
    except (ArgumentError, ValueError) as exc:
        raise SystemExit(
            f"备份要求可解析的 PostgreSQL DSN，当前为：{maskDatabaseUrl(databaseUrl)}"
        ) from exc
    if not url.drivername.startswith("postgresql") or not url.database:
        raise SystemExit(f"备份要求 PostgreSQL DSN，当前为：{maskDatabaseUrl(databaseUrl)}")
    kwargs = _effectiveConnectArgs(databaseUrl)
    host = _singleValue(kwargs.get("host"))
    port = _singleValue(kwargs.get("port"))
    database = _singleValue(kwargs.get("database")) or url.database
    user = _singleValue(kwargs.get("user"))
    password = _singleValue(kwargs.get("password")) or url.password
    command = ["pg_dump"]
    for flag, value in (("-h", host), ("-p", port), ("-U", user), ("-d", database)):
        if value is None:
            continue
        command.extend([flag, str(value)])
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = str(password)
    env.update(_pgEnvFromQuery(url))
    return command, env


def pgDump(
    databaseUrl: str,
    table: str,
    path: Path,
    *,
    timeout: int = PG_DUMP_TIMEOUT_SECONDS,
) -> bool:
    """单表备份：先写 ``.tmp`` 再原子替换。返回「是否真的备份到了」。

    半截 dump 不会被当成合法备份（``.tmp`` + 原子替换）。
    """
    command, env = dumpTarget(databaseUrl)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with openExclusive(tmp) as handle:
            result = subprocess.run(
                [*command, "-t", table],
                stdout=handle,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
    except FileNotFoundError as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit("备份失败：找不到 pg_dump（写正式库必须先备份）。") from exc
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"备份失败（{table}）：pg_dump 超时 {timeout}s，已中止。") from exc
    if result.returncode == 0:
        os.replace(tmp, path)
        return True
    tmp.unlink(missing_ok=True)
    # ⚠️ **绝不删除最终 ``path``**：本函数只写 ``.tmp`` 再 ``os.replace`` 原子替换
    # ⇒ ``path`` 此时要么不存在，要么是**另一个进程刚写好的完整有效备份**。
    # 在这里 ``path.unlink()`` 会把别人的回滚点删掉。
    stderr = result.stderr.decode("utf-8", "replace")
    if "no matching tables" in stderr:
        print(f"⚠️ 备份跳过 {table}：目标库尚无该表（迁移未应用）")
        return False
    # stderr 可能回显 DSN / 口令 ⇒ 必须打码
    raise SystemExit(
        scrubSecrets(
            f"备份失败（{table}）：{stderr[-PG_DUMP_STDERR_TAIL_CHARS:]}",
            databaseUrl,
        )
    )


def ensureBackups(
    databaseUrl: str,
    tables: tuple[str, ...],
    revision: str,
    directory: Path,
) -> tuple[Path, ...]:
    """写正式库前的备份闸：备份齐了才返回，否则拒绝继续。

    - **先过 DSN 闸**（`assertNoTargetOverride`）：备份目标与写入目标必须同源，
      备份闸自己就是写路径的一部分 ⇒ 不依赖调用方记得先过闸；
    - 备份文件是符号链接 ⇒ 拒绝复用（软链能骗过「复用当日备份」）；
    - 备份缺失或为空 ⇒ 现场 ``pg_dump``；
    - 仍拿不到非空备份 ⇒ **拒绝写正式库**（没有回滚点就不能写）。
    """
    assertNoTargetOverride(databaseUrl)
    assertExplicitTarget(databaseUrl)
    paths: list[Path] = []
    for table in tables:
        path = backupPath(directory, table, revision, databaseUrl)
        if path.is_symlink():
            raise SystemExit(f"备份文件是符号链接 ⇒ 拒绝复用/覆盖：{path}")
        if not path.exists() or path.stat().st_size == 0:
            pgDump(databaseUrl, table, path)
        if path.exists() and path.stat().st_size > 0:
            paths.append(path)
    if len(paths) != len(tables):
        raise SystemExit(
            f"备份不完整（{len(paths)}/{len(tables)} 张表）⇒ 拒绝写正式库。"
        )
    return tuple(paths)


def scrubSecrets(message: str, databaseUrl: str) -> str:
    """错误文本里的 DSN / 口令一律打码（日志泄密防护）。

    三层：

    1. **整个 DSN** ⇒ 整体替换成打码版（驱动与 ``pg_dump`` 的报错最常回显连接串）；
    2. **裸口令**（长度 ≥ 4）⇒ 全局替换；
    3. **短口令**（长度 < 4）⇒ 只在**凭据语境**（``password=`` / ``:pwd@``）里
       替换 —— 全局替换会把 ``pg_dump`` 这类正常词一起打码，报错信息就没法看了。

    口令来源有**两处**：userinfo（``u:pw@host``）与 query（``?password=pw``）。
    两处都要取出来单独打码 —— 「整体打码 DSN」只遮得住 DSN **原文**，
    驱动若只回显口令值（``authentication failed for <口令>``）就遮不住。
    """
    result = message
    if databaseUrl and databaseUrl in result:
        result = result.replace(databaseUrl, maskDatabaseUrl(databaseUrl))
    for secret in _secretsIn(databaseUrl):
        result = _scrubOneSecret(result, secret)
    return result


def _secretsIn(databaseUrl: str) -> list[str]:
    """DSN 里的全部口令候选：userinfo 口令 + query 里的口令类参数。"""
    secrets: list[str] = []
    try:
        url = make_url(databaseUrl)
    except (ArgumentError, ValueError):
        return secrets
    if url.password:
        secrets.append(str(url.password))
    for key, value in url.query.items():
        if key.lower() not in SENSITIVE_QUERY_KEYS:
            continue
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if item:
                secrets.append(str(item))
    return secrets


def _scrubOneSecret(result: str, secret: str) -> str:
    """把**一个**口令从文本里去掉（长口令全局替换，短口令只在凭据语境替换）。"""
    if len(secret) >= MIN_GLOBAL_SCRUB_PASSWORD_LEN:
        return result.replace(secret, "***")
    result = re.sub(
        rf"(?i)(password|passwd|pwd|secret|token)(\s*[=:]\s*){re.escape(secret)}\b",
        r"\1\2***",
        result,
    )
    return result.replace(f":{secret}@", ":***@")


def connectionErrorExit(exc: BaseException, databaseUrl: str) -> SystemExit:
    """连接类失败统一转 SystemExit（不吐 traceback、不带口令）。"""
    root = getattr(exc, "orig", None) or exc
    detail = scrubSecrets(str(root), databaseUrl)
    return SystemExit(f"❌ 连接目标库失败（{maskDatabaseUrl(databaseUrl)}）：{detail}")


def buildSessionFactory(databaseUrl: str) -> async_sessionmaker[AsyncSession]:
    """按**守卫放行的那个 DSN** 建会话工厂（DSN 单一来源）。

    `echo=False`：默认 ``APP_ENV=development`` 时 SQLAlchemy 会把每条 SQL 连同
    参数回显到日志 —— 发布脚本的参数里有 ~1MB 的 manifest JSONB。
    """
    engine = create_async_engine(databaseUrl, echo=False, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def currentDatabaseName(session: AsyncSession) -> str:
    """会话**真正连上的**库名（不信 DSN 路径上解析出来的那个）。

    为什么必须查库而不是信 DSN：asyncpg 方言会把 DSN 的 query string 合并进
    连接参数（``?database=postgres&host=evil.example.com`` 会覆盖路径上的
    host/database），守卫按路径解析出来的库名会与实际写入的库**不是同一个**。
    """
    return str((await session.execute(text("SELECT current_database()"))).scalar_one())


async def assertSameDatabase(
    session: AsyncSession,
    allowedDbName: str,
    databaseUrl: str,
) -> None:
    """守卫放行的库名必须等于会话**真正连的**库（不一致 ⇒ 拒绝落库）。"""
    actual = await currentDatabaseName(session)
    if actual != allowedDbName:
        raise SystemExit(
            f"落库目标与守卫放行目标不一致：实际 {actual!r} != 放行 {allowedDbName!r}"
            f"（{maskDatabaseUrl(databaseUrl)}）"
        )


def resolveDatabaseUrl(
    explicit: str | None,
    envName: str | None,
    *,
    allowInlinePassword: bool = False,
) -> str:
    """解析目标 DSN：显式参数优先，其次环境变量名。

    两道闸挂在这里（**所有**走本解析器的脚本都绕不过去**）：

    - **argv 不许带口令**（仅显式参数）：口令进 argv 会出现在 ``ps`` / shell 历史 /
      CI 日志里 ⇒ 显式参数里带口令 ⇒ 拒绝，请改用环境变量；
    - **DSN 不许用 query 改连接目标**：见 `assertNoTargetOverride`；
    - **DSN 必须显式写主机**：见 `assertExplicitTarget`。

    返回的是**归一化后**的 DSN（query 参数已翻译成驱动参数名）——
    引擎拿到的必须是驱动真认的参数，否则连接时 ``TypeError`` 会逃出打码出口。

    **口令可以只出现在环境变量里**（不进 argv）。``envName`` 指向的变量必须存在
    且非空。

    ``allowInlinePassword=True`` 只放开**第一道闸**（显式参数里的口令），
    给「运维既有契约允许 `--database-url` 带口令」的脚本用 —— 它们不该
    自己抄一份 DSN 解析。目标相关的两道闸**不因这个开关而放松**。
    """
    if explicit:
        if not allowInlinePassword:
            assertNoPasswordInArgv(explicit, source="--database-url")
        databaseUrl = explicit
    elif envName:
        value = os.environ.get(envName, "").strip()
        if not value:
            raise SystemExit(f"环境变量 {envName} 为空 ⇒ 无法确定目标库 DSN。")
        databaseUrl = value
    else:
        raise SystemExit("必须显式给出目标库 DSN（--database-url 或 --database-url-env）。")
    assertNoTargetOverride(databaseUrl)
    assertExplicitTarget(databaseUrl)
    return _normalizeQuery(databaseUrl)
