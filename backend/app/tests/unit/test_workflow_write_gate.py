"""P5 G6 反向守卫：**写操作全部经 Command**（源码级）。

为什么需要这道守卫：G6 判据的第一条是「写操作全部经 Command」。这只是一句
**口号**——只要有任何一个服务层绕过存储层直接改 ``workflow_*`` 表，
「唯一写入口」就名存实亡（后门 = 半生效的治理）。仓内的既有先例
（``test_semantic_schema_gate.py::Test生产侧不许绕过闸门``）证明这类断言
必须**结构性**落地，否则下一个人加一行 ``session.add(...)`` 就把口径破了。

守卫口径（机械可判定）：

1. **写入口收敛到存储层**：``app/`` 下（除测试）**只有**
   ``app/services/workflow_store.py`` 允许触碰 P5 表的 ORM 实体 ——
   其余模块（服务层 / API 层 / Worker）一律不得直接构造 / update / delete
   P5 的 ORM 实体。
2. **ORM 定义点唯一**：P5 表的 ``__tablename__`` 只允许出现在
   ``app/domain/workflow_models.py``（否则「写入口收敛」会漏掉一处定义）。
3. **原始 SQL 不直写**：不得用 ``text("... INSERT/UPDATE/DELETE workflow_...")``
   绕过 ORM。
4. **Agent 仍只读**：Agent 工具注册表里**不得**出现工作流 / 审批 / 命令类写工具。
5. **默认关闭**：``ENABLE_WORKFLOW`` 默认 ``false``（关 ⇒ 路由 404、Worker 不轮询）。

⚠️ 断言口径：断**「源码里是否出现违规调用」**，用**合成样本**反向验证扫描器本身
（扫描器写错 ⇒ 守卫空转 ⇒ 必须能被抓出来）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# 允许直接触碰 P5 表的模块（**唯一**写入口）。
_ALLOWED_WRITERS = {"workflow_store.py"}

# P5 表名前缀（用于「ORM 定义点唯一」这条守卫）。
_P5_TABLE_PREFIXES = ("workflow_",)
_P5_TABLE_NAMES = frozenset({"business_command", "approval_task"})

# 存储层之外的模块一旦出现这些调用 ⇒ 绕过写入口。
_WRITE_CALL_NAMES = frozenset({"update", "delete", "insert", "merge"})

_APP_DIR = pathlib.Path(__file__).resolve().parents[2]


def _p5ModelPairs() -> tuple[frozenset[str], frozenset[str]]:
    """(ORM 类名集合, 表名集合)（从 SSOT 模块派生 ⇒ 新增模型自动纳入守卫）。"""

    from app.domain import workflow_models

    classNames: set[str] = set()
    tableNames: set[str] = set()
    for obj in vars(workflow_models).values():
        if not isinstance(obj, type):
            continue
        table = getattr(obj, "__tablename__", None)
        if isinstance(table, str) and (
            table.startswith(_P5_TABLE_PREFIXES) or table in _P5_TABLE_NAMES
        ):
            classNames.add(obj.__name__)
            tableNames.add(table)
    return frozenset(classNames), frozenset(tableNames)


def _p5OrmClassNames() -> frozenset[str]:
    return _p5ModelPairs()[0]


def _isOrmName(node: ast.AST, ormNames: frozenset[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in ormNames


def _hasOrmConstructor(node: ast.AST, ormNames: frozenset[str]) -> bool:
    """表达式里是否出现 ORM 构造调用（``WorkflowInstance(...)``）。"""

    return any(
        isinstance(sub, ast.Call) and _isOrmName(sub.func, ormNames) for sub in ast.walk(node)
    )


def _rawWriteKeywords(line: str) -> bool:
    upper = line.upper()
    return any(kw in upper for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM"))


def findWriteOffenders(
    source: str,
    fileName: str,
    ormNames: frozenset[str],
    tableNames: frozenset[str] | None = None,
) -> list[str]:
    """在**单份源码**里找出「绕过存储层写 P5 表」的调用点。

    返回 ``["行号: 说明", ...]``；空列表 = 干净。
    """

    if tableNames is None:
        tableNames = _p5ModelPairs()[1]

    offenders: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover —— 解析失败要暴露而不是放过
        return [f"{fileName}:0: 源码无法解析（SyntaxError: {exc}）"]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        # ① update(...) / delete(...) / insert(...) / merge(...) 直接作用于 ORM 类
        if (
            isinstance(func, ast.Name)
            and func.id in _WRITE_CALL_NAMES
            and node.args
            and _isOrmName(node.args[0], ormNames)
        ):
            offenders.append(f"{fileName}:{node.lineno}: {func.id}(<P5 ORM 实体>)")

        # ② ORM 实体构造（``WorkflowInstance(...)``）= 潜在写入
        if _isOrmName(func, ormNames):
            offenders.append(f"{fileName}:{node.lineno}: 直接构造 P5 ORM 实体 {func.id}(...)")

        # ③ session.add / session.add_all 传入 P5 ORM 实体
        if isinstance(func, ast.Attribute) and func.attr in {"add", "add_all"}:
            for arg in node.args:
                if _hasOrmConstructor(arg, ormNames):
                    offenders.append(f"{fileName}:{node.lineno}: session.{func.attr}(<P5 ORM 实体>)")

        # ④ 原始 SQL 直写 P5 表（表名可能在语句中部：``UPDATE workflow_instance SET …``）
        if isinstance(func, ast.Name) and func.id == "text":
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _rawWriteKeywords(arg.value)
                    and any(table in arg.value for table in tableNames)
                ):
                    offenders.append(f"{fileName}:{node.lineno}: text() 原始 SQL 直写 P5 表")

    return offenders


def _sourceFiles() -> list[pathlib.Path]:
    """被扫描的源码文件（``app/`` 下、排除测试目录）。"""

    return [
        path
        for path in sorted(_APP_DIR.rglob("*.py"))
        if "tests" not in path.relative_to(_APP_DIR).parts
    ]


# =============================================================================
# 扫描器自检（合成样本）——扫描器写错 ⇒ 守卫空转 ⇒ 必须能被抓出来
# =============================================================================


def test_扫描器能抓出直接构造ORM实体():
    source = """
def build():
    return WorkflowInstance(id=1, status="RUNNING")
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("直接构造 P5 ORM 实体" in item for item in offenders), offenders


def test_扫描器能抓出update语句():
    source = """
async def run(session):
    await session.execute(update(BusinessCommand).where(BusinessCommand.id == 1))
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("update(<P5 ORM 实体>)" in item for item in offenders), offenders


def test_扫描器能抓出session_add():
    source = """
async def run(session):
    session.add(ApprovalTask(instance_id=1, matrix_code="m"))
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("session.add" in item for item in offenders), offenders


def test_扫描器能抓出原始SQL直写():
    source = """
from sqlalchemy import text

SQL = text("UPDATE workflow_instance SET status = 'FAILED'")
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("原始 SQL" in item for item in offenders), offenders


def test_扫描器不误报只读查询():
    """只读路径（select / 属性访问 / 类型注解）**不得**被误判。"""

    source = """
from sqlalchemy import select

async def read(session, instanceId: int) -> WorkflowInstance:
    row = await session.execute(
        select(WorkflowInstance).where(WorkflowInstance.id == instanceId)
    )
    return row.scalar_one_or_none()


def label(task: ApprovalTask) -> str:
    return f"{task.matrix_code}:{task.status}"
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert offenders == [], offenders


# =============================================================================
# 守卫本体（扫描真实源码）
# =============================================================================


def test_只有存储层能写P5表():
    """G6 第一条：写操作全部经存储层 ⇒ 其余模块一律不许直接碰 P5 实体。"""

    ormNames = _p5OrmClassNames()
    assert ormNames, "P5 ORM 实体集合为空（守卫会空转 ⇒ 必须先确认模型定义）"

    offenders: list[str] = []
    for path in _sourceFiles():
        if path.name in _ALLOWED_WRITERS:
            continue
        offenders.extend(
            findWriteOffenders(path.read_text(encoding="utf-8"), path.name, ormNames)
        )

    assert offenders == [], (
        "出现了绕过存储层直接写 workflow_* / business_command / approval_task 的调用点"
        "（G6『写操作全部经 Command』名存实亡）：\n" + "\n".join(offenders)
    )


def test_存储层本身确实在写P5表():
    """守卫不是空转：存储层**必须**真的在写 P5 表（否则上面的守卫会永远绿）。"""

    source = (
        pathlib.Path(_APP_DIR) / "services" / "workflow_store.py"
    ).read_text(encoding="utf-8")

    offenders = findWriteOffenders(source, "workflow_store.py", _p5OrmClassNames())

    assert offenders, "存储层没有写 P5 表 ⇒ 守卫白名单形同虚设"


def test_P5表的ORM定义点唯一():
    """P5 表的 ``__tablename__`` 只允许出现在 ``app/domain/workflow_models.py``。"""

    offenders: list[str] = []
    for path in _sourceFiles():
        if path.name == "workflow_models.py":
            continue
        source = path.read_text(encoding="utf-8")
        for no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if '__tablename__' not in stripped:
                continue
            for table in _P5_TABLE_NAMES:
                if table in stripped:
                    offenders.append(f"{path.name}:{no}: {stripped}")
            for prefix in _P5_TABLE_PREFIXES:
                if prefix in stripped:
                    offenders.append(f"{path.name}:{no}: {stripped}")
                    break

    assert offenders == [], (
        "P5 表在别处也有 ORM 定义 ⇒ 写入口收敛守卫会漏掉一处：\n" + "\n".join(offenders)
    )


# =============================================================================
# G6 其余判据
# =============================================================================


def test_Agent工具层不暴露工作流写入口():
    """G6 第二条：**Agent 仍只读** —— 工具注册表里不得有工作流 / 审批 / 命令写工具。"""

    from app.services.agent_tools import BUILTIN_HANDLERS, NL2SQL_HANDLERS

    forbidden = ("workflow", "approval", "command", "compensate")
    offenders = [
        name
        for name in list(BUILTIN_HANDLERS) + list(NL2SQL_HANDLERS)
        if any(token in name.lower() for token in forbidden)
    ]

    assert offenders == [], (
        "Agent 工具层出现了工作流/审批/命令类写工具（Agent 必须仍只读）：" + ", ".join(offenders)
    )


def test_工作流开关默认关闭():
    """G6 第三条：默认关闭可上线。"""

    from app.config import Settings

    settings = Settings()

    assert settings.enableWorkflow is False


def test_开关关闭时不注册路由():
    """开关关闭 ⇒ 整组路由不注册（对外完全不存在）。"""

    import os

    from app.config import getSettings

    previous = os.environ.get("ENABLE_WORKFLOW")
    os.environ.pop("ENABLE_WORKFLOW", None)
    getSettings.cache_clear()
    try:
        from app.main import createApp

        app = createApp()
        paths = {
            route.path
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/v1/workflow")
        }
    finally:
        if previous is not None:
            os.environ["ENABLE_WORKFLOW"] = previous
        getSettings.cache_clear()

    assert paths == set(), f"开关关闭时仍注册了工作流路由：{sorted(paths)}"


@pytest.mark.parametrize("token", ["workflow", "approval"])
def test_审批与命令写入口不在Agent可调用面(token: str):
    """Agent 工具的 **handler 来源**里不得出现审批 / 工作流模块。"""

    from app.services import agent_tools

    offenders = [
        name
        for name, handler in list(agent_tools.BUILTIN_HANDLERS.items())
        + list(agent_tools.NL2SQL_HANDLERS.items())
        if token in getattr(handler, "__module__", "").lower()
    ]

    assert offenders == [], (
        f"Agent 工具的 handler 来自含「{token}」的模块（Agent 必须仍只读）："
        + ", ".join(offenders)
    )
