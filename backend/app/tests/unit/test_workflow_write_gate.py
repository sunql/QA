"""P5 G6 反向守卫：**写操作全部经 Command**（源码级）。

为什么需要这道守卫：G6 判据的第一条是「写操作全部经 Command」。这只是一句
**口号**——只要有任何一个服务层绕过存储层直接改 ``workflow_*`` 表，
「唯一写入口」就名存实亡（后门 = 半生效的治理）。仓内的既有先例
（``test_semantic_schema_gate.py::Test生产侧不许绕过闸门``）证明这类断言
必须**结构性**落地，否则下一个人加一行 ``session.add(...)`` 就把口径破了。

守卫口径（机械可判定）：

1. **写入口收敛到存储层**：``app/`` + ``scripts/`` 下（除测试）**只有**
   ``app/services/workflow_store.py`` 允许触碰 P5 表的 ORM 实体 ——
   其余模块（服务层 / API 层 / Worker / 运维脚本）一律不得直接构造 /
   update / delete / **改属性** P5 的 ORM 实体。
2. **ORM 定义点唯一**：P5 表的 ``__tablename__`` 只允许出现在
   ``app/domain/workflow_models.py``（否则「写入口收敛」会漏掉一处定义）。
3. **原始 SQL 不直写**：不得用 ``text("... INSERT/UPDATE/DELETE workflow_...")``
   绕过 ORM，**也不得**用裸字符串（``session.execute("UPDATE workflow_...")``）。
4. **适配器注册点收口**：注册表是**开放**的 ⇒ 任何模块都能 ``register`` 一个直写
   外部系统的 handler ⇒ 注册点只允许在**组装根**（``app/workers/workflow_worker.py``）。
5. **Agent 仍只读**：Agent 工具注册表里**不得**出现工作流 / 审批 / 命令类写工具。
6. **默认关闭**：``ENABLE_WORKFLOW`` 默认 ``false``（关 ⇒ 路由 404、Worker 不轮询）。

⚠️ 断言口径：断**「源码里是否出现违规调用」**，用**合成样本**反向验证扫描器本身
（扫描器写错 ⇒ 守卫空转 ⇒ 必须能被抓出来）。

⚠️ **守卫的已知边界（别当成事实）**：

- 只管**库表**写入，**管不了**「外部系统写」⇒「所有外部写都经 Command」是
  **约定 + 评审**，不是守卫能证明的事实（注册点已收口到组装根作为补偿手段）。
- 实体追踪是**保守**的启发式（识别 ORM 类引用 / 形参标注 / 经存储层取回）⇒
  极端变形（``getattr`` 反射拿实体再改属性）**可能漏报**。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# 允许直接触碰 P5 表的模块（**唯一**写入口 = 存储层）——按**路径**白名单。
# ⚠️ 不能按文件名放行：新建一个 ``scripts/workflow_store.py`` 就能整体豁免
#    ⇒ 白名单形同虚设。
# ⚠️ 存储层按职责域拆成了 4 个文件（底座 / 实例+步骤 / 命令 / 审批）⇒ 白名单
#    跟着拆；新增存储层文件必须**显式**加进来（⇒ 走评审）。
_ALLOWED_WRITERS = frozenset(
    {
        "app/services/workflow_store_base.py",
        "app/services/workflow_instance_store.py",
        "app/services/workflow_command_store.py",
        "app/services/workflow_approval_store.py",
    }
)

# 允许注册 Command 适配器的模块（**唯一**注册点 = 组装根）。
_ALLOWED_REGISTRARS = frozenset({"app/workers/workflow_worker.py"})

# P5 表名前缀（用于「ORM 定义点唯一」这条守卫）。
_P5_TABLE_PREFIXES = ("workflow_",)
_P5_TABLE_NAMES = frozenset({"business_command", "approval_task"})

# 存储层之外的模块一旦出现这些调用 ⇒ 绕过写入口。
_WRITE_CALL_NAMES = frozenset({"update", "delete", "insert", "merge"})

# ``session.<方法>`` 形态的写入口（属性赋值是另一种形态，见 ``findWriteOffenders``）。
_SESSION_WRITE_METHODS = frozenset({"add", "add_all", "delete", "merge"})

# 硬规则（项目 CLAUDE.md）：单文件不超过 800 行。
_MAX_MODULE_LINES = 800

_APP_DIR = pathlib.Path(__file__).resolve().parents[2]
_BACKEND_DIR = _APP_DIR.parent
_SCRIPTS_DIR = _BACKEND_DIR / "scripts"
_TESTS_DIR = _APP_DIR / "tests" / "unit"


def _isAllowedWriter(relPath: str) -> bool:
    """该模块是否在**写入口白名单**里（按相对 ``backend/`` 的 posix 路径判定）。"""

    return relPath in _ALLOWED_WRITERS


def _isAllowedRegistrar(relPath: str) -> bool:
    """该模块是否是**唯一**的适配器注册点（组装根）。"""

    return relPath in _ALLOWED_REGISTRARS


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


def _mentionsOrmClass(node: ast.AST, ormNames: frozenset[str]) -> bool:
    """表达式 / 注解里是否**引用**了 P5 ORM 类名。"""

    return any(isinstance(sub, ast.Name) and sub.id in ormNames for sub in ast.walk(node))


def _rootName(node: ast.AST) -> str | None:
    """``a.b.c`` ⇒ ``"a"``（属性赋值的归属根）。"""

    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _mentionsStoreCall(node: ast.AST) -> bool:
    """表达式里是否是「经存储层拿到的实体」（``self._store.getInstance(...)``）。"""

    return any(
        (isinstance(sub, ast.Attribute) and sub.attr in {"_store", "store"})
        or (isinstance(sub, ast.Name) and sub.id in {"_store", "store"})
        for sub in ast.walk(node)
    )


def _trackedEntityNames(tree: ast.AST, ormNames: frozenset[str]) -> set[str]:
    """追踪「**持有 P5 ORM 实例**」的变量名。

    为什么要追踪：``task.status = "APPROVED"`` 这种**属性赋值**既不走 ``add`` /
    ``update()`` / 原始 SQL ⇒ 只盯「调用形态」的扫描器会漏掉它 ⇒ 必须先把
    「哪些变量是 P5 实体」认出来，才能把属性赋值判成违规。

    认法（保守，宁可漏报也不误报）：

    - 赋值语句的**右值**引用了 ORM 类名 ⇒ 目标变量就是实体
      （``task = (await session.execute(select(ApprovalTask))).scalar_one()``）；
    - 函数**形参**标注了 ORM 类 ⇒ 该形参就是实体
      （``def decide(task: ApprovalTask)``）。
    """

    names: set[str] = set()

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        annotation: ast.expr | None = None

        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
            annotation = node.annotation
        else:
            continue

        if value is not None:
            mentions = _mentionsOrmClass(value, ormNames) or _mentionsStoreCall(value)
        else:
            # 只有注解（``x: ApprovalTask``）⇒ 注解说了算。
            mentions = annotation is not None and _mentionsOrmClass(annotation, ormNames)
        if not mentions:
            continue

        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)

    # 形参标注 ⇒ 形参名即实体名。
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is not None and _mentionsOrmClass(arg.annotation, ormNames):
                names.add(arg.arg)

    return names


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

    tracked = _trackedEntityNames(tree, ormNames)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        callName = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else "")
        )
        # ① update(...) / delete(...) / insert(...) / merge(...) 直接作用于 ORM 类
        #    （含 ``sa.update(...)`` 这种带命名空间的写法）。
        if (
            callName in _WRITE_CALL_NAMES
            and node.args
            and _isOrmName(node.args[0], ormNames)
        ):
            offenders.append(f"{fileName}:{node.lineno}: {callName}(<P5 ORM 实体>)")

        # ② ORM 实体构造（``WorkflowInstance(...)``）= 潜在写入
        if _isOrmName(func, ormNames):
            offenders.append(f"{fileName}:{node.lineno}: 直接构造 P5 ORM 实体 {func.id}(...)")

        # ③ session.add / add_all / delete / merge 传入 P5 ORM 实体
        #    （实体既可以是**就地构造**，也可以是**已跟踪的变量**）。
        if isinstance(func, ast.Attribute) and func.attr in _SESSION_WRITE_METHODS:
            for arg in node.args:
                if _hasOrmConstructor(arg, ormNames) or _rootName(arg) in tracked:
                    offenders.append(
                        f"{fileName}:{node.lineno}: session.{func.attr}(<P5 ORM 实体>)"
                    )

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

        # ⑤ 裸字符串 execute（``session.execute("UPDATE workflow_instance …")``）：
        #    不在 ``text()`` 包装里 ⇒ 早期扫描器放行 ⇒ 后门。
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _rawWriteKeywords(arg.value)
                    and any(table in arg.value for table in tableNames)
                ):
                    offenders.append(f"{fileName}:{node.lineno}: 裸字符串 execute 直写 P5 表")

    # ⑥ 属性赋值（``task.status = "APPROVED"``）= ORM 的另一种写入口。
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            root = _rootName(target)
            if root in tracked:
                offenders.append(
                    f"{fileName}:{node.lineno}: 属性赋值绕过存储层 {root}.{target.attr} = ..."
                )

    return offenders


def findRegistryOffenders(source: str, fileName: str) -> list[str]:
    """在**单份源码**里找出「注册 Command 适配器」的调用点。

    为什么要单独一道：适配器注册表是**开放**的（任何模块都能 ``register`` 一个
    直写外部系统的 handler）⇒ 「写操作全部经 Command」会被悄悄绕过 ⇒ 注册点
    必须收口在**组装根**（Worker 启动处）。

    只在**导入了注册表类**的文件里查（``CommandHandlerRegistry``）⇒ 不会把
    仓内其它同名 ``register``（``IdMappingService.register`` 等）误判成命令适配器注册。
    """

    if "CommandHandlerRegistry" not in source:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover —— 解析失败要暴露而不是放过
        return [f"{fileName}:0: 源码无法解析（SyntaxError: {exc}）"]

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "register":
            offenders.append(f"{fileName}:{node.lineno}: 注册适配器 {func.attr}(...)")

    return offenders


def _sourceFiles() -> list[tuple[pathlib.Path, str]]:
    """被扫描的源码文件。

    返回 ``[(路径, 相对 backend/ 的 posix 路径), ...]``。

    范围 = ``app/``（排除测试目录）**+** ``scripts/``：
    脚本是运维侧的第二个写入口（回填 / 修复脚本），它若绕过存储层直写 P5 表，
    只扫 ``app/`` 的守卫就漏了一处。
    """

    roots = ((_APP_DIR, True), (_SCRIPTS_DIR, False))
    files: list[tuple[pathlib.Path, str]] = []
    for root, skipTests in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if skipTests and "tests" in path.relative_to(root).parts:
                continue
            files.append((path, path.relative_to(_BACKEND_DIR).as_posix()))
    return files


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


def test_扫描器能抓出实体属性赋值():
    """绕过存储层的**属性赋值**（``task.status = "APPROVED"``）也必须被抓出来。

    为什么补这条：属性赋值是 ORM 的**另一种**写入口——它不经过 ``add`` /
    ``update()`` / 原始 SQL ⇒ 只盯「调用形态」的扫描器会漏掉它。
    """

    source = """
from sqlalchemy import select

async def run(session):
    task = (await session.execute(select(ApprovalTask))).scalar_one()
    task.status = "APPROVED"
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("属性赋值" in item for item in offenders), offenders


def test_扫描器能抓出session_delete():
    source = """
from sqlalchemy import select

async def run(session):
    task = (await session.execute(select(ApprovalTask))).scalar_one()
    await session.delete(task)
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("session.delete" in item for item in offenders), offenders


def test_扫描器能抓出session_merge():
    source = """
async def run(session):
    await session.merge(WorkflowInstance(id=1))
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("session.merge" in item for item in offenders), offenders


def test_扫描器能抓出已跟踪实体的session_add():
    """``session.add(row)``（row 是查出来的实体）同样绕过存储层。"""

    source = """
from sqlalchemy import select

async def run(session):
    row = (await session.execute(select(BusinessCommand))).scalar_one()
    session.add(row)
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("session.add" in item for item in offenders), offenders


def test_扫描器能抓出裸字符串execute():
    """``session.execute("UPDATE workflow_instance …")``（裸字符串）也必须被抓出来。

    为什么补这条：``text("…")`` 形态已被覆盖，但**裸字符串**（驱动层自己拼的
    SQL）不在 ``text()`` 包装里 ⇒ 早期扫描器放行 ⇒ 后门。
    """

    source = """
async def run(session):
    await session.execute("UPDATE workflow_instance SET status = 'FAILED'")
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("裸字符串" in item for item in offenders), offenders


def test_扫描器能抓出sa_update形式():
    """``sa.update(BusinessCommand)``（带命名空间的 update）也是后门。"""

    source = """
import sqlalchemy as sa

async def run(session):
    await session.execute(sa.update(BusinessCommand).where(BusinessCommand.id == 1))
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("update(<P5 ORM 实体>)" in item for item in offenders), offenders


def test_扫描器能抓出存储层取回后直接改属性():
    """最真实的绕过形态：从存储层**取回**实体后直接改属性。"""

    source = """
async def run(session, store):
    instance = await store.getInstance(session, 1)
    instance.status = "FAILED"
"""
    offenders = findWriteOffenders(source, "synthetic.py", _p5OrmClassNames())

    assert any("属性赋值" in item for item in offenders), offenders


def test_扫描器能抓出适配器注册():
    """注册 Command 适配器 ⇒ 写入口从「组装根」扩散 ⇒ 必须被抓出来。"""

    source = """
from app.services.workflow_command_ports import CommandHandlerRegistry

registry = CommandHandlerRegistry()
registry.register("po.write", handler)
"""
    offenders = findRegistryOffenders(source, "synthetic.py")

    assert any("注册适配器" in item for item in offenders), offenders


def test_适配器注册点收口在组装根():
    """适配器**只能**在组装根（Worker 启动处）注册。

    为什么：注册表是**开放**的（任何模块都能 ``register`` 一个直写外部系统的
    handler）⇒ 「写操作全部经 Command」就会被悄悄绕过 ⇒ 注册点必须收口，
    新增适配器必须改到组装根 ⇒ 走评审。
    """

    offenders: list[str] = []
    for path, rel in _sourceFiles():
        if _isAllowedRegistrar(rel):
            continue
        offenders.extend(findRegistryOffenders(path.read_text(encoding="utf-8"), rel))

    assert offenders == [], (
        "适配器注册点扩散到组装根之外（写入口被绕过）：\n" + "\n".join(offenders)
    )


def test_组装根确实注册了适配器():
    """注册点收口守卫**不是空转**：组装根必须真的注册了适配器。"""

    source = (
        pathlib.Path(_APP_DIR) / "workers" / "workflow_worker.py"
    ).read_text(encoding="utf-8")

    offenders = findRegistryOffenders(source, "app/workers/workflow_worker.py")

    assert offenders, "组装根没有注册任何适配器 ⇒ 注册点收口守卫形同虚设"


def test_P5模块不超过行数上限():
    """硬规则（项目 CLAUDE.md）：**单文件不超过 800 行**。

    为什么要有守卫：P5 的存储层一度长到 1010 行 ⇒ 违反硬规则 ⇒ 而且这类
    「越滚越大」是**静默**的（没人会在提交时数行数）⇒ 必须**机械**判定。

    ⚠️ **范围**：既扫 ``app/`` 下的 P5 模块，**也**扫 ``app/tests/unit/`` 下的
    P5 单测文件 —— 测试文件同样受硬规则约束（``test_workflow_command_executor.py``
    一度 809 行 ⇒ 补偿用例已拆到 ``test_workflow_compensation.py``）。
    早期版本的守卫只扫 ``app/`` ⇒ 测试文件可以无声地越界 ⇒ 拆分压力被推迟。
    """

    offenders: list[str] = []
    for path, rel in _sourceFiles():
        if not rel.startswith("app/"):
            continue
        if not (
            rel.startswith("app/services/workflow_")
            or rel.startswith("app/api/v1/workflow")
            or rel.startswith("app/workers/workflow_")
            or rel.startswith("app/domain/workflow_")
        ):
            continue
        lineCount = len(path.read_text(encoding="utf-8").splitlines())
        if lineCount > _MAX_MODULE_LINES:
            offenders.append(f"{rel}: {lineCount} 行（上限 {_MAX_MODULE_LINES}）")

    for path in sorted(_TESTS_DIR.glob("test_workflow_*.py")):
        rel = path.relative_to(_BACKEND_DIR).as_posix()
        lineCount = len(path.read_text(encoding="utf-8").splitlines())
        if lineCount > _MAX_MODULE_LINES:
            offenders.append(f"{rel}: {lineCount} 行（上限 {_MAX_MODULE_LINES}）")

    assert offenders == [], "P5 模块超出单文件行数上限（须按职责拆分）：\n" + "\n".join(
        offenders
    )


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
    for path, rel in _sourceFiles():
        if _isAllowedWriter(rel):
            continue
        offenders.extend(
            findWriteOffenders(path.read_text(encoding="utf-8"), rel, ormNames)
        )

    assert offenders == [], (
        "出现了绕过存储层直接写 workflow_* / business_command / approval_task 的调用点"
        "（G6『写操作全部经 Command』名存实亡）：\n" + "\n".join(offenders)
    )


def test_守卫覆盖Worker层():
    """Worker 也是「写入口候选」⇒ 必须落在扫描范围内。

    ⚠️ 别把扫描范围缩到 ``app/services/``：Worker 会 claim / 提交，
    它若绕过存储层直写 P5 表，上面的守卫就漏了一处。
    """

    names = {rel for _path, rel in _sourceFiles()}

    assert any(rel.endswith("workflow_worker.py") for rel in names), (
        "Worker 未被守卫覆盖（扫描范围可能被人为收窄）"
    )


def test_守卫覆盖脚本目录():
    """``backend/scripts/`` 也是写入口候选（回填 / 修复脚本）⇒ 必须落在扫描范围内。

    ⚠️ 别把扫描范围缩到 ``app/``：脚本是运维侧的第二个写入口，它若绕过存储层
    直写 P5 表，上面的守卫就漏了一处。
    """

    rels = {rel for _path, rel in _sourceFiles()}

    assert any(rel.startswith("scripts/") for rel in rels), "scripts/ 未被守卫覆盖"


def test_白名单按路径而非文件名():
    """白名单必须按**路径**放行 ⇒ 任何目录下的同名文件都不能豁免。

    早期按文件名放行（``path.name in {"workflow_store.py"}``）⇒ 新建一个
    ``scripts/workflow_store.py`` 就能整体豁免 ⇒ 白名单形同虚设。
    """

    assert _isAllowedWriter("app/services/workflow_store_base.py") is True
    assert _isAllowedWriter("app/services/nested/workflow_store_base.py") is False
    assert _isAllowedWriter("scripts/workflow_store_base.py") is False
    assert _isAllowedWriter("app/api/v1/workflow.py") is False
    # 门面只**组合**三个子存储、自己不写表 ⇒ 不在白名单（写入口在子存储里）。
    assert _isAllowedWriter("app/services/workflow_store.py") is False


def test_存储层本身确实在写P5表():
    """守卫不是空转：存储层**必须**真的在写 P5 表（否则上面的守卫会永远绿）。"""

    ormNames = _p5OrmClassNames()
    offenders: list[str] = []
    for rel in sorted(_ALLOWED_WRITERS):
        path = pathlib.Path(_BACKEND_DIR) / rel
        assert path.exists(), f"白名单里的存储层文件不存在：{rel}"
        offenders.extend(
            findWriteOffenders(path.read_text(encoding="utf-8"), rel, ormNames)
        )

    assert offenders, "存储层没有写 P5 表 ⇒ 守卫白名单形同虚设"


def test_P5表的ORM定义点唯一():
    """P5 表的 ``__tablename__`` 只允许出现在 ``app/domain/workflow_models.py``。"""

    offenders: list[str] = []
    for path, rel in _sourceFiles():
        if path.name == "workflow_models.py":
            continue
        source = path.read_text(encoding="utf-8")
        for no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if '__tablename__' not in stripped:
                continue
            for table in _P5_TABLE_NAMES:
                if table in stripped:
                    offenders.append(f"{rel}:{no}: {stripped}")
            for prefix in _P5_TABLE_PREFIXES:
                if prefix in stripped:
                    offenders.append(f"{rel}:{no}: {stripped}")
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
