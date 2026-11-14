"""写库前备份闸门共享原语（`scripts/db_snapshot_guard.py`）的单测。

这七条硬约束每一条都对应一次评审条目（H1 / H3 / M1–M7），每一条都可能
**静默**失效 ⇒ 必须有测试钉住，不能靠「读代码觉得对」。

关键点：**备份不完整 ⇒ 拒绝写正式库**。没有回滚点就不写，这是唯一能挡住
「备份脚本其实没备份到任何东西」的闸门。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import db_snapshot_guard as guard


class _FakeResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class _FakeSession:
    """最小会话替身：只提供 `SELECT current_database()`。"""

    def __init__(self, actual: str) -> None:
        self._actual = actual
        self.statements: list[str] = []

    async def execute(self, statement) -> _FakeResult:
        self.statements.append(str(statement))
        return _FakeResult(self._actual)


class Test版本号清洗:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("v-p2-d12", "v-p2-d12"),
            ("v1.0.0", "v1.0.0"),
            ("../../etc/passwd", "etcpasswd"),
            ("a/b", "ab"),
            ("..", "unknown"),
            ("", "unknown"),
            (".hidden", "hidden"),
        ],
    )
    def test_只保留安全字符(self, raw: str, expected: str) -> None:
        assert guard.safeRevision(raw) == expected

    def test_清洗结果不含路径分隔符(self) -> None:
        cleaned = guard.safeRevision("../../tmp/x")
        assert "/" not in cleaned
        assert ".." not in cleaned


class Test目标库身份:
    def test_身份里带主机端口与库名(self) -> None:
        identity = guard.targetIdentity(
            "postgresql+asyncpg://u:p@db.example.com:6543/qa_metadata"
        )
        assert identity == "db.example.com_6543_qa_metadata"

    def test_非法DSN退化为unknown而不是抛异常(self) -> None:
        assert guard.targetIdentity("not a dsn") == "unknown-target"

    def test_口令不会出现在身份里(self) -> None:
        identity = guard.targetIdentity(
            "postgresql+asyncpg://u:sup3rsecret@localhost:5432/qa_metadata"
        )
        assert "sup3rsecret" not in identity

    def test_DSN没写端口时按实际端口算身份(self) -> None:
        """身份要反映**实际连到哪台**，不是 DSN 文本长什么样。

        DSN 省略端口 ⇒ libpq / asyncpg 都回落到 ``$PGPORT``，再回落 5432。
        身份若写成 "no-port" ⇒ ``$PGPORT`` 变了身份不变 ⇒ 今天的备份被当成
        另一台机器的备份复用（回滚点是错的）。
        """
        assert guard.targetIdentity("postgresql://u:p@h/qa_metadata") == (
            "h_5432_qa_metadata"
        )

    def test_环境变量里的端口进身份(self, monkeypatch) -> None:
        monkeypatch.setenv("PGPORT", "6543")
        assert guard.targetIdentity("postgresql://u:p@h/qa_metadata") == (
            "h_6543_qa_metadata"
        )

    def test_DSN写了端口时端口优先于环境变量(self, monkeypatch) -> None:
        monkeypatch.setenv("PGPORT", "6543")
        assert guard.targetIdentity("postgresql://u:p@h:5432/qa_metadata") == (
            "h_5432_qa_metadata"
        )


class Test备份文件创建:
    def test_正常创建得到0600文件(self, tmp_path: Path) -> None:
        target = tmp_path / "backup.sql"
        with guard.openExclusive(target) as handle:
            handle.write(b"-- dump")
        assert target.exists()
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_已存在时不覆盖(self, tmp_path: Path) -> None:
        target = tmp_path / "backup.sql"
        target.write_text("原有内容", encoding="utf-8")
        with pytest.raises(SystemExit):
            guard.openExclusive(target)
        assert target.read_text(encoding="utf-8") == "原有内容"

    def test_符号链接绝不跟随(self, tmp_path: Path) -> None:
        """软链能骗过「复用当日备份」⇒ 打开备份文件时必须拒绝跟随。"""
        real = tmp_path / "real.sql"
        real.write_text("原有内容", encoding="utf-8")
        link = tmp_path / "link.sql"
        link.symlink_to(real)
        with pytest.raises(SystemExit):
            guard.openExclusive(link)
        assert real.read_text(encoding="utf-8") == "原有内容"

    def test_残留tmp文件的报错点名路径并提示清理(self, tmp_path: Path) -> None:
        """残留的 .tmp 会永久挡住备份 ⇒ 报错必须点名它，别让操作者猜。"""
        target = tmp_path / "backup.sql.tmp"
        target.write_text("残留", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            guard.openExclusive(target)
        message = str(exc.value)
        assert str(target) in message
        assert "清理" in message


class Test备份目录:
    def test_目录权限收紧到0700(self, tmp_path: Path) -> None:
        directory = tmp_path / "backups"
        directory.mkdir(mode=0o755)
        guard.ensureBackupDir(directory)
        assert oct(directory.stat().st_mode)[-3:] == "700"

    def test_目录是符号链接时拒绝使用(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(SystemExit):
            guard.ensureBackupDir(link)


class Test备份闸门:
    def test_备份不齐时拒绝继续(self, tmp_path: Path, monkeypatch) -> None:
        """pg_dump 说「no matching tables」⇒ 那张表根本没被备份 ⇒ 不许写正式库。"""
        monkeypatch.setattr(guard, "pgDump", lambda *args, **kwargs: False)
        with pytest.raises(SystemExit) as exc:
            guard.ensureBackups(
                "postgresql+asyncpg://u:p@localhost:5432/qa_metadata",
                ("semantic_release",),
                "v-test",
                tmp_path,
            )
        assert "备份不完整" in str(exc.value)

    def test_备份齐全时返回备份路径(self, tmp_path: Path, monkeypatch) -> None:
        def _fakeDump(databaseUrl: str, table: str, path: Path, **kwargs) -> bool:
            path.write_text("-- dump", encoding="utf-8")
            return True

        monkeypatch.setattr(guard, "pgDump", _fakeDump)
        paths = guard.ensureBackups(
            "postgresql+asyncpg://u:p@localhost:5432/qa_metadata",
            ("semantic_release",),
            "v-test",
            tmp_path,
        )
        assert len(paths) == 1
        assert paths[0].exists()

    def test_备份文件是符号链接时拒绝复用(self, tmp_path: Path, monkeypatch) -> None:
        """软链能骗过「复用当日备份」⇒ 拒绝。

        ⚠️ 备份文件名带**当天日期**（`backupPath` 内部用 `date.today()`）⇒
        测试里的文件名必须现算。写死日期会造出一个「根本不在检查路径上」的
        软链 ⇒ 测试是靠 `pg_dump` 连不上而绿，**软链拒绝分支一次都没跑过**
        （假绿）。
        """
        dsn = "postgresql+asyncpg://u:p@localhost:5432/qa_metadata"
        path = guard.backupPath(tmp_path, "semantic_release", "v-test", dsn)
        real = tmp_path / "real.sql"
        real.write_text("-- dump", encoding="utf-8")
        path.symlink_to(real)

        def _failDump(*args, **kwargs):
            raise AssertionError("软链分支必须先于 pg_dump 触发")

        monkeypatch.setattr(guard, "pgDump", _failDump)
        with pytest.raises(SystemExit) as exc:
            guard.ensureBackups(dsn, ("semantic_release",), "v-test", tmp_path)
        assert "符号链接" in str(exc.value)
        assert real.read_text(encoding="utf-8") == "-- dump"


class Test目标必须显式:
    """⚠️ DSN 省略 host ⇒ 实际连接目标由 ``$PGHOST`` / ``$PGPORT`` 决定。

    asyncpg 在 host 为空时读 ``$PGHOST``（实测连 ``prod-bastion.internal``），
    而旧实现把备份目标兜底成 ``localhost:5432`` ⇒ **备份 A 库、写入 B 库**
    —— 与本批修掉的 HIGH 同一类不变量，只是换了个门。

    ⇒ 闸门层拒绝「没有显式 host」的 DSN；``dumpTarget`` 取不到 host/port 时
    **不下发** ``-h`` / ``-p``，让 libpq 与 asyncpg 用同一套解析规则。
    """

    def test_DSN省略host时被拒绝(self) -> None:
        with pytest.raises(SystemExit) as exc:
            guard.resolveDatabaseUrl("postgresql+asyncpg://u@/qa_metadata", None)
        assert "host" in str(exc.value)

    def test_省略host时备份目标不下发_h(self) -> None:
        command, _ = guard.dumpTarget("postgresql+asyncpg://u@/qa_metadata")
        assert "-h" not in command
        assert "-p" not in command

    def test_PGHOST环境变量不改变备份目标(self, monkeypatch) -> None:
        """设了 ``$PGHOST`` 时也不能把备份目标改成 localhost —— 两侧必须
        一起回落到同一个环境变量目标。"""
        monkeypatch.setenv("PGHOST", "prod-bastion.internal")
        monkeypatch.setenv("PGPORT", "6432")
        command, _ = guard.dumpTarget("postgresql+asyncpg://u@/qa_metadata")
        assert "localhost" not in " ".join(command)
        assert "prod-bastion.internal" not in " ".join(command)

    def test_显式host时目标照旧下发(self) -> None:
        command, _ = guard.dumpTarget(
            "postgresql+asyncpg://u:p@db.example.com:6543/qa_metadata"
        )
        assert command[command.index("-h") + 1] == "db.example.com"
        assert command[command.index("-p") + 1] == "6543"

    def test_备份闸自己也拒绝没有显式host的DSN(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            guard.ensureBackups(
                "postgresql+asyncpg://u:p@/qa_metadata",
                ("semantic_release",),
                "v-test",
                tmp_path,
            )


class Test打码:
    def test_口令被替换掉(self) -> None:
        message = guard.scrubSecrets(
            "连接失败 postgresql://u:sup3rsecret@host/db", "postgresql://u:sup3rsecret@host/db"
        )
        assert "sup3rsecret" not in message
        assert "***" in message

    def test_没有口令时原样返回(self) -> None:
        assert guard.scrubSecrets("普通错误", "postgresql://u@host/db") == "普通错误"


class Test连接目标不被query覆盖:
    """⚠️ 本模块最要紧的一条：**DSN 的 query string 能改掉驱动实际连的服务器**。

    asyncpg 方言把 ``url.query`` 合并进 connect kwargs ⇒
    ``?host=evil.internal&port=6432`` 让驱动连到**另一台机器**，而 DSN
    **路径**上的 host/port 不变 ⇒ 备份（``pg_dump`` 读路径）备份的是 A 库、
    写入的是 B 库 ⇒ **正式库写入没有回滚点**，且 ``current_database()``
    看不出来（两台机器上库名相同）。

    ⇒ 写路径必须拒绝会改目标的 query 参数（只留「不改目标」的 TLS / 超时类）。
    """

    @pytest.mark.parametrize(
        "key",
        ["host", "port", "database", "dbname", "service", "user", "password"],
    )
    def test_会改目标的query参数被拒绝(self, key: str) -> None:
        dsn = f"postgresql+asyncpg://u:p@localhost:5433/qa_metadata?{key}=x"
        with pytest.raises(SystemExit) as exc:
            guard.assertNoTargetOverride(dsn)
        assert key in str(exc.value)

    @pytest.mark.parametrize(
        "query",
        ["sslmode=require", "ssl=require", "connect_timeout=5", "command_timeout=7"],
    )
    def test_不改目标的query参数放行(self, query: str) -> None:
        guard.assertNoTargetOverride(
            f"postgresql+asyncpg://u:p@localhost:5433/qa_metadata?{query}"
        )

    def test_驱动不认的query参数被拒绝(self) -> None:
        """``application_name`` 在 asyncpg 的 connect() 签名里**不存在**。

        SQLAlchemy 的 asyncpg 方言把 ``url.query`` 原样并进 connect kwargs ⇒
        放行它 ⇒ 连接时 ``TypeError: connect() got an unexpected keyword
        argument 'application_name'`` ⇒ 该异常不是 DBAPIError ⇒ 逃出所有
        错误处理 ⇒ 裸 traceback 取代「打码 DSN 的干净退出」。
        """
        with pytest.raises(SystemExit) as exc:
            guard.assertNoTargetOverride(
                "postgresql+asyncpg://u:p@localhost:5433/qa_metadata?application_name=qa"
            )
        assert "application_name" in str(exc.value)

    @pytest.mark.parametrize(
        ("query", "expectedKey"),
        [("sslmode=require", "ssl"), ("connect_timeout=5", "timeout")],
    )
    def test_参数名被翻译成驱动认的名字(
        self, query: str, expectedKey: str
    ) -> None:
        """libpq 的叫法（``sslmode`` / ``connect_timeout``）必须翻译成驱动叫法
        （``ssl`` / ``timeout``）—— 直传会 TypeError。"""
        resolved = guard.resolveDatabaseUrl(
            f"postgresql+asyncpg://u@localhost:5433/qa_metadata?{query}", None
        )
        assert f"{expectedKey}=" in resolved
        assert "sslmode" not in resolved
        assert "connect_timeout" not in resolved

    def test_归一化后的连接参数驱动全都认(self) -> None:
        """**不变量**：引擎拿到的每一个 connect kwarg 都必须在 asyncpg 的
        ``connect()`` 签名里 —— 否则连接时炸 TypeError，且逃出打码出口。"""
        import inspect

        import asyncpg
        from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
        from sqlalchemy.engine import make_url

        accepted = set(inspect.signature(asyncpg.connect).parameters)
        for query in (
            "sslmode=require",
            "ssl=require",
            "connect_timeout=5",
            "command_timeout=7",
        ):
            resolved = guard.resolveDatabaseUrl(
                f"postgresql+asyncpg://u@localhost:5433/qa_metadata?{query}", None
            )
            _url, kwargs = PGDialect_asyncpg().create_connect_args(make_url(resolved))
            unknown = sorted(set(kwargs) - accepted)
            assert not unknown, f"{query} ⇒ 驱动不认的参数：{unknown}"

    @pytest.mark.parametrize(
        "query",
        ["ssl=true", "sslmode=whatever", "connect_timeout=abc", "command_timeout=0"],
    )
    def test_取值非法的query参数被拒绝(self, query: str) -> None:
        """``?ssl=true`` 会让 asyncpg 抛 ClientConfigurationError，
        ``?command_timeout=abc`` 会抛 ValueError —— 两者都不是 DBAPIError
        ⇒ 逃出打码出口 ⇒ 必须在闸门处按字面量拒绝。"""
        with pytest.raises(SystemExit):
            guard.assertNoTargetOverride(
                f"postgresql+asyncpg://u:p@localhost:5433/qa_metadata?{query}"
            )

    def test_没有query时放行(self) -> None:
        guard.assertNoTargetOverride(
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata"
        )

    def test_不可解析的DSN被拒绝而不是放过(self) -> None:
        with pytest.raises(SystemExit):
            guard.assertNoTargetOverride("这不是一个DSN")

    def test_显式DSN走解析器时也会被拦(self) -> None:
        """闸门必须挂在**所有** DSN 入口上（解析器）⇒ 绕不过去。"""
        with pytest.raises(SystemExit):
            guard.resolveDatabaseUrl(
                "postgresql+asyncpg://u@host/db?host=evil.internal", None
            )

    def test_环境变量里的DSN同样被拦(self, monkeypatch) -> None:
        monkeypatch.setenv("QA_DSN", "postgresql+asyncpg://u@host/db?host=evil.internal")
        with pytest.raises(SystemExit):
            guard.resolveDatabaseUrl(None, "QA_DSN")


class Test备份目标与写入目标同源:
    """备份目标必须与写入目标**同一个来源** ⇒ 否则没有回滚点。

    旧实现手搓 ``-h/-p/-U/-d``（只读 DSN 路径）⇒ query 覆盖时备份 A、写入 B。
    新实现从**驱动自己的 connect kwargs** 取目标 ⇒ 同源同值。
    """

    def test_普通DSN的备份目标取自路径(self) -> None:
        command, _ = guard.dumpTarget(
            "postgresql+asyncpg://u:p@db.example.com:6543/qa_metadata"
        )
        assert command[command.index("-h") + 1] == "db.example.com"
        assert command[command.index("-p") + 1] == "6543"
        assert command[command.index("-U") + 1] == "u"
        assert command[command.index("-d") + 1] == "qa_metadata"

    def test_query覆盖host_port时备份目标跟着驱动走(self) -> None:
        """即使白名单以后放宽，备份也必须跟着**驱动**走（而不是 DSN 路径）。"""
        command, _ = guard.dumpTarget(
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata"
            "?host=evil.internal&port=6432"
        )
        assert command[command.index("-h") + 1] == "evil.internal"
        assert command[command.index("-p") + 1] == "6432"

    def test_备份目标始终等于驱动实际连的目标(self) -> None:
        """不变量：``pg_dump`` 的目标 == 驱动 connect kwargs 的目标。"""
        from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
        from sqlalchemy.engine import make_url

        for dsn in (
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata",
            "postgresql+asyncpg://u:p@db.example.com:6543/qa_metadata_test_x",
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata"
            "?host=evil.internal&port=6432",
        ):
            _, kwargs = PGDialect_asyncpg().create_connect_args(make_url(dsn))
            command, _ = guard.dumpTarget(dsn)
            assert command[command.index("-h") + 1] == kwargs["host"]
            assert command[command.index("-p") + 1] == str(kwargs["port"])
            assert command[command.index("-d") + 1] == kwargs["database"]
            assert command[command.index("-U") + 1] == kwargs["user"]

    def test_口令只在环境变量里不进argv(self) -> None:
        command, env = guard.dumpTarget(
            "postgresql+asyncpg://u:sup3rsecret@localhost:5433/qa_metadata"
        )
        assert "sup3rsecret" not in " ".join(command)
        assert env["PGPASSWORD"] == "sup3rsecret"

    def test_query里的口令也走PGPASSWORD(self) -> None:
        command, env = guard.dumpTarget(
            "postgresql+asyncpg://u@localhost:5433/qa_metadata?password=sup3rsecret"
        )
        assert "sup3rsecret" not in " ".join(command)
        assert env["PGPASSWORD"] == "sup3rsecret"


class Test打码覆盖query:
    def test_没有凭据时整体打码(self) -> None:
        assert guard.maskDatabaseUrl("postgresql+asyncpg://localhost:5433/db") == "***"

    def test_userinfo口令被打码(self) -> None:
        masked = guard.maskDatabaseUrl(
            "postgresql+asyncpg://qa_user:sup3rsecret@host:5433/db"
        )
        assert "sup3rsecret" not in masked
        assert "qa_user" in masked

    def test_query里的口令被打码(self) -> None:
        masked = guard.maskDatabaseUrl(
            "postgresql+asyncpg://qa_user:pw@host:5433/db?password=sup3rsecret"
        )
        assert "sup3rsecret" not in masked

    def test_query里有at符号时不会把query当host(self) -> None:
        """``maskDsn`` 按最后一个 ``@`` 切 userinfo ⇒ query 带 ``@`` 时会切错。"""
        masked = guard.maskDatabaseUrl(
            "postgresql+asyncpg://qa_user:pw@host:5433/db?options=a@b"
        )
        assert "options=a@b" in masked
        assert "pw@" not in masked

    def test_非DSN整体打码(self) -> None:
        assert guard.maskDatabaseUrl("garbage") == "***"


class Test口令不进argv:
    """口令放在 argv 里会进 ``ps`` / shell 历史 / CI 日志 ⇒ 显式参数不许带口令。"""

    def test_显式DSN带口令时拒绝(self) -> None:
        with pytest.raises(SystemExit) as exc:
            guard.resolveDatabaseUrl("postgresql+asyncpg://u:pw@host/db", None)
        assert "--database-url-env" in str(exc.value)

    def test_环境变量里的DSN可以带口令(self, monkeypatch) -> None:
        monkeypatch.setenv("QA_DSN", "postgresql+asyncpg://u:pw@host/db")
        assert guard.resolveDatabaseUrl(None, "QA_DSN") == (
            "postgresql+asyncpg://u:pw@host/db"
        )

    def test_显式DSN不带口令时放行(self) -> None:
        assert guard.resolveDatabaseUrl(
            "postgresql+asyncpg://u@host/db", None
        ) == "postgresql+asyncpg://u@host/db"

    def test_允许内嵌口令时显式DSN带口令也放行(self) -> None:
        """**运维既有契约**的脚本（KPI 回填）允许 `--database-url` 带口令。

        这类脚本不该自己抄一份 DSN 解析 ⇒ 用同一个解析器 + 一个显式的
        「允许内嵌口令」开关，而不是复制实现。
        """
        assert guard.resolveDatabaseUrl(
            "postgresql+asyncpg://u:pw@host/db", None, allowInlinePassword=True
        ) == "postgresql+asyncpg://u:pw@host/db"

    def test_允许内嵌口令时仍然拒绝改目标的query(self) -> None:
        """放开的是**口令**，不是目标 —— query 改目标在任何脚本里都必须拒绝。"""
        with pytest.raises(SystemExit, match="host"):
            guard.resolveDatabaseUrl(
                "postgresql+asyncpg://u:pw@host/db?host=evil.internal",
                None,
                allowInlinePassword=True,
            )

    def test_允许内嵌口令时仍然要求显式主机(self) -> None:
        with pytest.raises(SystemExit, match="主机"):
            guard.resolveDatabaseUrl(
                "postgresql+asyncpg://u:pw@/db", None, allowInlinePassword=True
            )


class Test错误文本打码:
    def test_整个DSN被打码(self) -> None:
        dsn = "postgresql+asyncpg://u:sup3rsecret@host/db"
        message = guard.scrubSecrets(f"连接失败：{dsn}", dsn)
        assert "sup3rsecret" not in message

    def test_口令在query里时也被打码(self) -> None:
        """口令在 query 里时 ``make_url(...).password`` 是 None ⇒ 只有「整体打码
        DSN」这一条能遮住它。"""
        dsn = "postgresql+asyncpg://qa_user@host/db?password=sup3rsecret"
        message = guard.scrubSecrets(f"连接失败：{dsn}", dsn)
        assert "sup3rsecret" not in message

    def test_长口令单独出现时也被替换(self) -> None:
        message = guard.scrubSecrets(
            "authentication failed for secretpw", "postgresql://u:secretpw@host/db"
        )
        assert "secretpw" not in message

    def test_短口令不做全局替换以免破坏信息(self) -> None:
        """口令 2 个字符时全局替换会把 ``pg_dump`` 也打码 ⇒ 只在凭据语境替换。"""
        message = guard.scrubSecrets(
            "pg_dump failed for user", "postgresql://u:pg@host/db"
        )
        assert "pg_dump" in message

    def test_query里的口令单独出现时也被替换(self) -> None:
        """query 里的口令**单独**出现在报错里时也必须打码。

        「整体打码 DSN」只遮得住 DSN **原文**；驱动若只回显口令值
        （``authentication failed for <口令>``），原文替换就遮不住 ⇒ 泄密。
        """
        message = guard.scrubSecrets(
            "authentication failed for sup3rsecret",
            "postgresql+asyncpg://qa_user@host/db?password=sup3rsecret",
        )
        assert "sup3rsecret" not in message

    def test_query里的短口令只在凭据语境替换(self) -> None:
        """短口令沿用同一策略：全局替换会误伤正常词（``pg_dump``）。"""
        message = guard.scrubSecrets(
            "pg_dump failed for user", "postgresql://u@host/db?password=pg"
        )
        assert "pg_dump" in message

    def test_短口令在凭据语境里仍被打码(self) -> None:
        message = guard.scrubSecrets(
            "auth failed: password=pg rejected", "postgresql://u:pg@host/db"
        )
        assert "password=pg" not in message


class Test目标库复核:
    async def test_实际连的库与放行的库不一致时拒绝(self) -> None:
        session = _FakeSession("postgres")
        with pytest.raises(SystemExit) as exc:
            await guard.assertSameDatabase(
                session, "qa_metadata", "postgresql+asyncpg://u:p@host/qa_metadata"
            )
        assert "不一致" in str(exc.value)

    async def test_一致时放行(self) -> None:
        session = _FakeSession("qa_metadata_test_semantic")
        await guard.assertSameDatabase(
            session,
            "qa_metadata_test_semantic",
            "postgresql+asyncpg://u:p@host/qa_metadata_test_semantic",
        )


class TestDSN解析:
    def test_显式参数优先(self) -> None:
        assert guard.resolveDatabaseUrl("postgresql://a/b", "SOME_ENV") == "postgresql://a/b"

    def test_没有显式参数时读环境变量(self, monkeypatch) -> None:
        monkeypatch.setenv("QA_METADATA_DSN", "postgresql://u:p@host/db")
        assert guard.resolveDatabaseUrl(None, "QA_METADATA_DSN") == "postgresql://u:p@host/db"

    def test_环境变量为空时拒绝(self, monkeypatch) -> None:
        monkeypatch.delenv("QA_METADATA_DSN", raising=False)
        with pytest.raises(SystemExit):
            guard.resolveDatabaseUrl(None, "QA_METADATA_DSN")

    def test_两者都没给时拒绝(self) -> None:
        with pytest.raises(SystemExit):
            guard.resolveDatabaseUrl(None, None)
