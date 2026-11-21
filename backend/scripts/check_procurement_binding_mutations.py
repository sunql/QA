#!/usr/bin/env python3
"""采购源绑定**测试套件**的变异反证（mutation proof）。

用途：证明 `backend/app/tests/integration/test_procurement_binding_api.py`
（以及纯校验层的 unit 套件）**不是「永远绿」** —— 即 M1-1.1 代码评审 / 安全审查
要求修的那批不变量，确实被测试钉住了。

做法：
1. 先断言**原始代码必须让目标测试全绿**（钉基线）；
2. 逐个注入一个已知的错误变异（改**实现**，不改测试）；
3. 要求**每一个**变异都让对应测试**变红**，**而且红在预期的那条断言上**；
4. 变异落地失败 / 测试工具自身报错 / 红了但红得不是地方 ⇒ **工具侧故障**
   （退出码 2），本轮结论不可用。

为什么要有「原始必须全绿」这一步：若原始代码本身就红，那么「每个变异都让测试变红」
毫无意义 —— 红的原因是本来就红。

**为什么必须判「红的原因」（第三轮 MEDIUM-3）**：只判「测试变红」会把**红在别处**
也算成「守得住」。变异可能让测试因为**导入失败 / 语法错 / 夹具炸 / 另一条断言**
而变红 —— 那与被测的那条不变量无关 ⇒ 「守得住」的结论是假的。
⇒ 每条变异带一个 `expect`：**预期失败的那条断言的源码文本**（pytest 会在失败
回溯里打出这一行）⇒ 输出里**必须**出现其中之一，否则判「工具侧故障」。

**为什么工具侧故障必须与「漏网」分开（第三轮 MEDIUM-4）**：工具侧崩溃
（超时 / OS 错误 / 收集失败）如果被当成「漏网」，退出码 1 会被下游读成
「测试存在假绿」⇒ 结论的方向就反了。⇒ 工具侧故障一律退出码 **2**，
与「漏网」（退出码 1）、「全捕获」（退出码 0）严格区分。

**已知结构性不可观测项**：`M9`（生效视图不校验「父必须未失效」）。父失效时子绑定由
**级联失效**一并作废 ⇒ 删掉查询里的这条冗余谓词，行为不变 ⇒ 变异必然漏网。
这是**预期结果**（冗余防御，不是缺陷），脚本把它单列而不是混进「漏网」。
（对照：`M7`「父必须已确认」**是**可观测的 —— 子已确认但父仍是候选时会漏出。）

**变异覆盖 25 条**：
M1–M10 = 代码评审 + 安全审查第一轮的不变量；
M11–M16 = 安全审查第二轮新增的三道表达式闸门（OR / 受控函数 / 结构词）、
`$` 标识符抽取、生效视图的属性条数口径、建时键序号前缀闸门；
M17–M25 = 第三轮新增：`CASE` 恒真闸门、裸列↔常量比较闸门、`TABLE` 结构词、
`EXISTS` 死代码修复、白名单长度闸门、中文字面量掩码、父子有效期嵌套、
`IntegrityError` 分流、关键字不算列。

用法::

    python3 backend/scripts/check_procurement_binding_mutations.py

退出码：0 = 全部可观测变异都被捕获；1 = 有变异漏网（测试存在假绿）；
2 = **工具侧故障**（变异没落地 / 测试工具自身报错 / 红得不是地方）⇒ 结论不可用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
SERVICE = BACKEND_DIR / "app/services/procurement_binding_service.py"
VALIDATION = BACKEND_DIR / "app/services/procurement_binding_validation.py"
TEST_FILE = "app/tests/integration/test_procurement_binding_api.py"
UNIT_TEST_FILE = "app/tests/unit/test_procurement_binding_validation.py"

#: 变异锚点未命中 / 测试目标缺失 ⇒ 工具侧故障（不是「漏网」）。
TOOL_ERROR = "tool-error"
ESCAPED = "escaped"
CAUGHT = "caught"

#: 结构性不可观测的变异（冗余防御 ⇒ 变异不改变行为 ⇒ 必然漏网）。
UNOBSERVABLE = {"M9"}


#: 生效视图里的四条谓词（整块作为锚点 ⇒ 唯一 ⇒ 不会误命中别处的同名片段）。
#:
#: ⚠️ 教训（2026-11-18 实测）：先前用「`ProcurementSourceBinding.is_candidate`
#: + `valid_to`」两行做锚点，结果它**先命中**了 `listSourceBindings` 里
#: `effectiveOnly` 的同名两行 ⇒ 变异落在了错误的位置 ⇒ 目标测试当然不变红 ⇒
#: 脚本却报「漏网」⇒ **结论是假的**。⇒ 锚点必须**全文唯一**，且脚本要断言唯一性。
_EFFECTIVE_PARENT_PREDICATES = """                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
                ProcurementSourceBinding.is_candidate.is_(False),
                ProcurementSourceBinding.valid_to.is_(None),"""

#: 过滤表达式的闸门块（**整块**做锚点 ⇒ 唯一）。
#:
#: ⚠️ 必须带上下文（`if not plain:` 那行）才唯一：变换表达式的版本结构相似，
#: 只有「零标识符 ⇒ 拒绝」那一行不同 ⇒ 用整块可保证锚点只命中过滤表达式。
_FILTER_GATE_BLOCK = """    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertNoFilterOnlyWords(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)
    if not plain:"""

_FILTER_GATE_BLOCK_NO_OR = """    _validateExpressionSafety(expression)
    _assertNoFilterOnlyWords(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)
    if not plain:"""

_FILTER_GATE_BLOCK_NO_FUNCTION = """    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertNoFilterOnlyWords(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)
    if not plain:"""

_FILTER_GATE_BLOCK_NO_RESERVED = """    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertNoFilterOnlyWords(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    if not plain:"""

_FILTER_GATE_BLOCK_NO_CASE = """    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)
    if not plain:"""


@dataclass(frozen=True)
class Mutation:
    """一条变异：把目标文件里的一段实现替换成另一段，期望对应测试变红。

    ``expect`` = **预期失败的那条断言的源码文本**（pytest 失败回溯会打出这一行）。
    输出里出现其中之一才算「红在预期的地方」；一条都不出现 ⇒ **工具侧故障**
    （红得不是地方 ⇒ 本轮结论不可用）。
    """

    code: str
    name: str
    path: Path
    old: str
    new: str
    test: str
    expect: tuple[str, ...] = ()
    test_file: str = field(default=TEST_FILE)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        code="M1",
        name="去掉「父已确认⇒子集合冻结」检查",
        path=SERVICE,
        old="    if not source.is_candidate:\n        raise ConflictError(",
        new="    if False:\n        raise ConflictError(",
        test="test_add_property_to_confirmed_source_rejected",
        expect=("assert resp.status_code == 409",),
    ),
    Mutation(
        code="M2",
        name="键序号连续性按「含已失效绑定」计算",
        path=SERVICE,
        old="        return [item for item in properties if item.valid_to is None]",
        new="        return list(properties)",
        test="test_confirm_property_revalidates_key_ordinals",
        expect=("assert resp.status_code == 422",),
    ),
    Mutation(
        code="M3",
        name="去掉「所有属性绑定须已确认」检查",
        path=SERVICE,
        old="        pending = [item for item in active if item.is_candidate]",
        new="        pending = []",
        test="test_confirm_source_requires_confirmed_properties",
        expect=("assert resp.status_code == 422",),
    ),
    Mutation(
        code="M4",
        name="去掉 actor 长度边界检查（改由库层兜底 ⇒ 文案变 ⇒ 假绿会被抓）",
        path=SERVICE,
        old='    if len(actor or "") > _MAX_ACTOR_LENGTH:',
        new="    if False:",
        test="test_long_user_id_rejected_not_500",
        expect=(
            "assert resp.status_code == 422",
            'assert "操作用户标识" in resp.text',
        ),
    ),
    Mutation(
        code="M5",
        name="去掉 transform 表达式闸门",
        path=SERVICE,
        old="        validateTransformExpression(dto.transform_expression, dto.source_column)",
        new="        pass",
        test="test_transform_expression_injection_rejected",
        expect=("assert resp.status_code == 422",),
    ),
    Mutation(
        code="M6",
        name="级联失效不写子行审计",
        path=SERVICE,
        old="""            item.valid_to = max(validTo, _expireAt(item.valid_from))
            await self._audit.record(
                session,
                entity_type=PROPERTY_ENTITY,
                entity_id=item.id,
                action="UPDATE",
                actor=actor,
                before=before,
                after=_propertySnapshot(item),
            )""",
        new="            item.valid_to = max(validTo, _expireAt(item.valid_from))",
        test="test_cascade_expire_audits_children",
        expect=('assert actions == ["CREATE", "UPDATE", "UPDATE"], (',),
    ),
    Mutation(
        code="M7",
        name="生效视图不校验「父必须已确认」（子已确认但父仍是候选时会漏出）",
        path=SERVICE,
        old=_EFFECTIVE_PARENT_PREDICATES,
        new="""                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
                ProcurementSourceBinding.valid_to.is_(None),""",
        test="test_effective_properties_excluded_while_parent_candidate",
        expect=("assert after.json() == []",),
    ),
    Mutation(
        code="M9",
        name="生效视图不校验「父必须未失效」（**冗余防御** ⇒ 级联失效已保证 ⇒ 不可观测）",
        path=SERVICE,
        old=_EFFECTIVE_PARENT_PREDICATES,
        new="""                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
                ProcurementSourceBinding.is_candidate.is_(False),""",
        test="test_effective_properties_empty_when_parent_expired",
        expect=("assert after.json() == []",),
    ),
    Mutation(
        code="M8",
        name="属性确认不校验归属父 id（跨父确认）",
        path=SERVICE,
        old="""        owner = await session.get(ProcurementPropertyBinding, id)
        if owner is None or owner.source_binding_id != sourceBindingId:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ValidationError(f"属性绑定 id={id} 已失效，不能确认")""",
        new="""        owner = await session.get(ProcurementPropertyBinding, id)
        if False:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ValidationError(f"属性绑定 id={id} 已失效，不能确认")""",
        test="test_confirm_property_rejects_foreign_source_id",
        expect=("assert resp.status_code == 404", 'assert "不属于" in resp.text'),
    ),
    Mutation(
        code="M10",
        name="属性失效不校验归属父 id（跨父失效）",
        path=SERVICE,
        old="""        owner = await session.get(ProcurementPropertyBinding, id)
        if owner is None or owner.source_binding_id != sourceBindingId:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ConflictError(f"属性绑定 id={id} 已失效，不能重复失效")""",
        new="""        owner = await session.get(ProcurementPropertyBinding, id)
        if False:
            raise NotFoundError(
                f"属性绑定 id={id} 不存在或不属于源绑定 id={sourceBindingId}"
            )

        source = await self._lockSourceBinding(session, sourceBindingId)
        _assertParentEditable(source)
        binding = await self._lockPropertyBinding(session, id)
        if binding.valid_to is not None:
            raise ConflictError(f"属性绑定 id={id} 已失效，不能重复失效")""",
        test="test_expire_property_rejects_foreign_source_id",
        expect=("assert resp.status_code == 404", 'assert "不属于" in resp.text'),
    ),
    # ------------------------------------------------------------------
    # 第二轮（安全审查 HIGH-1 / HIGH-2 / MEDIUM-1/3 + 建时闸门）
    # ------------------------------------------------------------------
    Mutation(
        code="M11",
        name="过滤表达式不禁 OR（恒真 ⇒ 行级过滤失效）",
        path=VALIDATION,
        old=_FILTER_GATE_BLOCK,
        new=_FILTER_GATE_BLOCK_NO_OR,
        test="test_filter_expression_with_or_rejected",
        expect=("assert resp.status_code == 422", 'assert "OR" in resp.text'),
    ),
    Mutation(
        code="M12",
        name="过滤表达式不受控函数清单约束（白名单自报即可调任意函数）",
        path=VALIDATION,
        old=_FILTER_GATE_BLOCK,
        new=_FILTER_GATE_BLOCK_NO_FUNCTION,
        test="test_filter_expression_with_unknown_function_rejected",
        expect=("assert resp.status_code == 422", 'assert "函数" in resp.text'),
    ),
    Mutation(
        code="M13",
        name="过滤表达式不禁结构词（可塞子查询跨表读）",
        path=VALIDATION,
        old=_FILTER_GATE_BLOCK,
        new=_FILTER_GATE_BLOCK_NO_RESERVED,
        test="test_filter_expression_with_subquery_rejected",
        expect=("assert resp.status_code == 422", 'assert "结构词" in resp.text'),
    ),
    Mutation(
        code="M14",
        name="标识符抽取不认 `$`（`status$1` 被切成 `status` ⇒ 白名单绕过）",
        path=VALIDATION,
        old='_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")',
        new='_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")',
        test="test_dollar_column_cannot_bypass_allowlist",
        expect=("assert resp.status_code == 422",),
    ),
    Mutation(
        code="M15",
        name="生效视图的属性条数不过滤（候选/已失效行从侧面泄漏）",
        path=SERVICE,
        old="""        if onlyEffective:
            stmt = stmt.where(
                ProcurementPropertyBinding.is_candidate.is_(False),
                ProcurementPropertyBinding.valid_to.is_(None),
            )""",
        new="""        if onlyEffective:
            pass""",
        test="test_effective_view_property_count_excludes_expired",
        expect=('assert rows[0]["propertyCount"] == 0',),
    ),
    Mutation(
        code="M16",
        name="建属性绑定时不校验键序号前缀（缺位 ⇒ 永远无法确认）",
        path=SERVICE,
        old="        missing = [number for number in range(1, keyOrdinal) if number not in present]",
        new="        missing = []",
        test="test_non_contiguous_key_ordinal_rejected_at_create",
        expect=("assert prop.status_code == 422", 'assert "连续" in prop.text'),
    ),
    # ------------------------------------------------------------------
    # 第三轮（代码评审 HIGH-1 / HIGH-2 + MEDIUM-1/2/3/4 + LOW-1/2）
    # ------------------------------------------------------------------
    Mutation(
        code="M17",
        name="过滤表达式不禁 CASE（`CASE ... ELSE 1 END = 1` 恒真）",
        path=VALIDATION,
        old=_FILTER_GATE_BLOCK,
        new=_FILTER_GATE_BLOCK_NO_CASE,
        test="test_filter_expression_with_case_constant_true_rejected",
        expect=(
            "assert resp.status_code == 422",
            'assert "结构词" in resp.text',
        ),
    ),
    Mutation(
        code="M18",
        name="过滤表达式不要求「裸列↔常量」比较（恒真族可达）",
        path=VALIDATION,
        old="""    _assertHasColumnConstantComparison(expression)""",
        new="""    pass""",
        test="test_filter_expression_self_comparison_rejected",
        expect=("assert resp.status_code == 422", 'assert "列 与 常量" in resp.text'),
    ),
    Mutation(
        code="M19",
        name="结构词禁表漏 `TABLE`（`EXISTS (TABLE other_tbl)` 跨表读绕过）",
        path=VALIDATION,
        old='        "TABLE",\n        "LATERAL",',
        new='        "LATERAL",',
        test="test_filter_expression_with_table_keyword_alone_rejected",
        expect=('assert "结构词" in resp.text',),
    ),
    Mutation(
        code="M20",
        name="`EXISTS` 被算作 SQL 关键字（⇒ 结构词禁令成了死代码）",
        path=VALIDATION,
        old='        "WHEN",\n    }\n)',
        new='        "WHEN",\n        "EXISTS",\n    }\n)',
        test="test_filter_expression_with_exists_keyword_alone_rejected",
        expect=('assert "结构词" in resp.text',),
    ),
    Mutation(
        code="M21",
        name="白名单长度闸门被去掉（PG 数组元素超长是静默截断）",
        path=VALIDATION,
        old="""        if len(item) > MAX_ALLOWLIST_ITEM_LENGTH:
            raise ValidationError(
                f"白名单条目过长（{len(item)} > {MAX_ALLOWLIST_ITEM_LENGTH} 字符）：{item!r}"
            )""",
        new="        pass",
        test="test_rejects_too_long_item",
        expect=("DID NOT RAISE", "白名单条目过长"),
        test_file=UNIT_TEST_FILE,
    ),
    Mutation(
        code="M22",
        name="中文字面量掩码失效（业务数据是中文 ⇒ 过滤表达式不可用）",
        path=VALIDATION,
        old="""    return _STRING_LITERAL_RE.sub(
        lambda m: "".join("x" if not ch.isascii() else ch for ch in m.group()),
        expression,
    )""",
        new="    return expression",
        test="test_allows_chinese_string_literal",
        expect=("非法字符",),
        test_file=UNIT_TEST_FILE,
    ),
    Mutation(
        code="M23",
        name="级联失效不后移父行失效时刻（父子有效期窗口不嵌套）",
        path=SERVICE,
        old="        return effectiveValidTo",
        new="        return validTo",
        test="test_cascade_expire_nests_child_validity_within_parent",
        expect=("assert datetime.fromisoformat(childValidTo) <= datetime.fromisoformat(parentValidTo)",),
    ),
    Mutation(
        code="M25",
        name="「裸列」判定把 SQL 关键字也算成列（恒真谓词可从侧门进来）",
        path=VALIDATION,
        old="""    if kind == "ident" and text.upper() in _SQL_KEYWORDS:
        return False""",
        new="""    if False:
        return False""",
        test="test_rejects_keyword_as_column",
        expect=("DID NOT RAISE", "列 与 常量"),
        test_file=UNIT_TEST_FILE,
    ),
    Mutation(
        code="M24",
        name="IntegrityError 一律当冲突（CHECK/外键 ⇒ 409 ⇒ 重试提示误导）",
        path=SERVICE,
        old='    isUnique = constraintName.startswith("uq_") or sqlstate == "23505"',
        new="    isUnique = True",
        test="test_check_constraint_is_validation_error",
        expect=("ConflictError", "DID NOT RAISE"),
        test_file=UNIT_TEST_FILE,
    ),
)


def _env() -> dict[str, str]:
    """测试运行环境（与 `scripts/run_local_tests.sh` 同款接线）。

    ⚠️ 口令不写死在脚本里：优先取环境变量，缺失则从 `backend/.env` /
    `docker/.env` 读本地开发口令（两个文件都没有 ⇒ 工具侧故障，不猜）。
    """
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL", "")
    if not env["TEST_DATABASE_URL"]:
        password = _readLocalSetting(env, "POSTGRES_PASSWORD")
        if password is None:
            return {}
        env["TEST_DATABASE_URL"] = (
            f"postgresql+asyncpg://qa_user:{password}"
            "@localhost:5434/qa_metadata_test"
        )
    env.setdefault("TEST_NEO4J_URI", "")
    if not env["TEST_NEO4J_URI"]:
        neo4jPassword = _readLocalSetting(env, "NEO4J_PASSWORD")
        if neo4jPassword is None:
            return {}
        # 测试实例固定 7688（7687 是本地开发实例，生产实例更在远端）
        env["TEST_NEO4J_URI"] = f"bolt://neo4j:{neo4jPassword}@localhost:7688"
    return env


def _readLocalSetting(env: dict[str, str], key: str) -> str | None:
    """从 `backend/.env` / `docker/.env` 读本地开发设置值（口令等）。"""
    for candidate in (
        BACKEND_DIR / ".env",
        REPO_ROOT / "docker/.env",
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _runPytest(
    testFile: str, testName: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str] | None:
    """跑一个测试；**工具自身崩了**（超时 / OS 错误）⇒ 返回 ``None``。

    ⚠️ 不能让异常冒出去：`subprocess.TimeoutExpired` 会让脚本以 Python 的
    退出码 1 结束 ⇒ 而 1 在本脚本的契约里表示「有变异漏网（测试存在假绿）」
    ⇒ **工具故障会被读成相反的结论**（第三轮 MEDIUM-4）。
    """
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                testFile,
                "-k",
                testName,
                "-x",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _redForRightReason(output: str, expect: tuple[str, ...]) -> bool:
    """测试是不是**红在预期的那条断言上**。

    pytest 的失败回溯会打出失败断言的**源码行** ⇒ 用它当指纹 ⇒
    「红了但红在别的断言上」会被判为工具侧故障，而不是「守住了」。
    """
    if not expect:
        return True
    return any(token in output for token in expect)


def main() -> int:
    env = _env()
    if not env.get("TEST_DATABASE_URL"):
        print("工具侧故障：拿不到 TEST_DATABASE_URL（读不到本地口令）⇒ 本轮结论不可用。")
        return 2

    print("== 基线：不注入任何变异，目标测试必须全绿 ==")
    baselineTargets = sorted({(item.test_file, item.test) for item in MUTATIONS})
    for testFile, target in baselineTargets:
        result = _runPytest(testFile, target, env)
        if result is None:
            print(f"工具侧故障：基线测试 {target} 运行失败（超时 / OS 错误）⇒ 结论不可用。")
            return 2
        if result.returncode != 0:
            print(f"工具侧故障：基线测试 {target} 本来就不绿 ⇒ 结论不可用。")
            print(_output(result)[-2000:])
            return 2
    print(f"基线通过（{len(baselineTargets)} 个目标测试）。\n")

    escaped: list[str] = []
    toolErrors: list[str] = []
    for mutation in MUTATIONS:
        source = mutation.path.read_text()
        occurrences = source.count(mutation.old)
        if occurrences != 1:
            print(
                f"⚠️ {mutation.code} {mutation.name} —— 锚点命中 {occurrences} 处"
                "（要求恰好 1 处 ⇒ 变异会落错位置）"
            )
            toolErrors.append(f"{mutation.code} {mutation.name}（锚点命中 {occurrences} 处）")
            continue
        mutation.path.write_text(source.replace(mutation.old, mutation.new, 1))
        try:
            result = _runPytest(mutation.test_file, mutation.test, env)
        finally:
            mutation.path.write_text(source)

        if result is None:
            print(f"⚠️ {mutation.code} {mutation.name} —— 测试工具自身崩了（超时 / OS 错误）")
            toolErrors.append(f"{mutation.code} {mutation.name}（工具侧崩了）")
            continue
        output = _output(result)
        if result.returncode == 0:
            if mutation.code in UNOBSERVABLE:
                print(
                    f"ℹ️  {mutation.code} {mutation.name} —— 漏网"
                    "（**预期**：冗余防御不可观测）"
                )
                continue
            print(f"❌ {mutation.code} {mutation.name} —— 漏网（测试没抓住）")
            escaped.append(f"{mutation.code} {mutation.name}")
            continue
        if "no tests ran" in output or "ERROR: not found" in output:
            print(f"⚠️ {mutation.code} {mutation.name} —— 测试目标缺失")
            toolErrors.append(f"{mutation.code} {mutation.name}（测试目标缺失）")
            continue
        if not _redForRightReason(output, mutation.expect):
            print(
                f"⚠️ {mutation.code} {mutation.name} —— 红了但**红得不是地方**"
                "（预期的断言没出现 ⇒ 结论不可用）"
            )
            toolErrors.append(f"{mutation.code} {mutation.name}（红得不是地方）")
            continue
        print(f"✅ {mutation.code} {mutation.name}")

    if toolErrors:
        print(f"\n{len(toolErrors)} 个变异**工具侧故障** ⇒ 本轮结论不可用：")
        for item in toolErrors:
            print(f"   - {item}")
        return 2
    if escaped:
        print(f"\n{len(escaped)} 个变异漏网 ⇒ 测试存在假绿：")
        for item in escaped:
            print(f"   - {item}")
        return 1
    observable = len(MUTATIONS) - len(UNOBSERVABLE)
    print(f"\n全部可观测变异都被捕获（{observable} 条），测试不是「永远绿」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
