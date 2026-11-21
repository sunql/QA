"""采购源绑定的**纯校验**层单测（M1-1.1）。

被测模块：`app.services.procurement_binding_validation`

为什么单独拆一个纯函数模块：过滤白名单 / 复合键序号 / 角色字面量这三类规则
**不需要数据库**就能判定 ⇒ 放成纯函数才能在 unit 层用真实断言覆盖
（服务层一旦掺进 session，测试就退化成「间接调用 service」——项目规则明令禁止）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import ConflictError, ValidationError
from app.services.procurement_binding_service import _classifyIntegrityError
from app.services.procurement_binding_validation import (
    MAX_ALLOWLIST_ITEM_LENGTH,
    _maskNonAsciiInLiterals,
    extractCallIdentifiers,
    extractIdentifiers,
    extractPlainIdentifiers,
    validateAllowlistItems,
    validateBindingRole,
    validateColumnName,
    validateFilterExpression,
    validateKeyOrdinals,
    validateTransformExpression,
)


class TestExtractIdentifiers:
    def test_extracts_column_and_function_names(self) -> None:
        found = extractIdentifiers("STATUS = UPPER(ORDER_STATUS)")
        assert found == {"STATUS", "UPPER", "ORDER_STATUS"}

    def test_ignores_sql_keywords(self) -> None:
        found = extractIdentifiers("STATUS IS NOT NULL AND AMT > 0")
        assert found == {"STATUS", "AMT"}

    def test_ignores_string_literal_content(self) -> None:
        """字面量里的内容不是标识符：`STATUS = 'ORDER_QTY'` 不得要求放行 ORDER_QTY。"""
        found = extractIdentifiers("STATUS = 'ORDER_QTY'")
        assert found == {"STATUS"}

    def test_ignores_numbers(self) -> None:
        found = extractIdentifiers("AMT >= 100 AND QTY = 2.5")
        assert found == {"AMT", "QTY"}

    def test_quoted_identifier_is_one_token(self) -> None:
        """`"SALARY NET"` 是一个带空格的列名 ⇒ 不能按词切分（切了就会被逐词放行）。"""
        found = extractIdentifiers('"SALARY NET" > 0')
        assert found == {"SALARY NET"}

    def test_quoted_keyword_is_still_an_identifier(self) -> None:
        """`"IN"` 是被引号包住的保留字 ⇒ 关键字豁免只对未加引号的部分生效。"""
        found = extractIdentifiers('"IN" = 1')
        assert found == {"IN"}


class TestValidateFilterExpressionHardening:
    """评审发现的绕过口子（安全审查 MEDIUM-2 / MEDIUM-3）。"""

    def test_rejects_expression_without_any_identifier(self) -> None:
        """`1=1` 零列引用 = 把行级过滤抹成全表读 ⇒ 拒绝。"""
        with pytest.raises(ValidationError, match="列引用"):
            validateFilterExpression("1=1", ["STATUS"])

    def test_rejects_quoted_identifier_outside_allowlist(self) -> None:
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression('"SALARY NET" > 0', ["SALARY", "NET"])

    def test_rejects_quoted_keyword_without_allowlist_entry(self) -> None:
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression('"IN" = 1', ["STATUS"])


class TestValidateAllowlistItems:
    def test_accepts_identifier_items(self) -> None:
        validateAllowlistItems(["STATUS", "ORG_CODE"])

    def test_rejects_non_identifier_item(self) -> None:
        """白名单项会被读路径当列名用 ⇒ 非标识符必须响亮失败。"""
        with pytest.raises(ValidationError, match="列名非法"):
            validateAllowlistItems(["STATUS); DROP TABLE X --"])

    def test_rejects_empty_item(self) -> None:
        with pytest.raises(ValidationError):
            validateAllowlistItems([""])

    def test_accepts_none(self) -> None:
        validateAllowlistItems(None)
        validateAllowlistItems([])


class TestValidateTransformExpression:
    def test_allows_empty(self) -> None:
        validateTransformExpression(None, "ORDER_QTY")
        validateTransformExpression("", "ORDER_QTY")

    def test_allows_own_column(self) -> None:
        validateTransformExpression("ORDER_QTY / 1000", "ORDER_QTY")

    def test_allows_controlled_function(self) -> None:
        validateTransformExpression("TRUNC(ORDER_QTY)", "ORDER_QTY")
        validateTransformExpression("NVL(ORDER_QTY, 0)", "ORDER_QTY")

    def test_rejects_dml(self) -> None:
        """列级变换会被拼进读路径投影 ⇒ 与过滤表达式同级的闸门。"""
        with pytest.raises(ValidationError):
            validateTransformExpression("1); DROP TABLE X --", "ORDER_QTY")

    def test_rejects_other_column(self) -> None:
        """跨列引用必须显式开白名单（本期不做 ⇒ 拒绝）。"""
        with pytest.raises(ValidationError, match="未授权"):
            validateTransformExpression("ORDER_QTY + SECRET_FLAG", "ORDER_QTY")

    def test_rejects_unknown_function(self) -> None:
        with pytest.raises(ValidationError, match="未授权"):
            validateTransformExpression("MY_CUSTOM_FN(ORDER_QTY)", "ORDER_QTY")


class TestValidateFilterExpression:
    def test_accepts_expression_within_allowlist(self) -> None:
        validateFilterExpression("STATUS <> 'X'", ["STATUS"])

    def test_rejects_identifier_outside_allowlist(self) -> None:
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression("STATUS <> 'X' AND SECRET_FLAG = 1", ["STATUS"])

    def test_rejects_expression_without_allowlist(self) -> None:
        """给了过滤表达式却没给白名单 ⇒ 无法判定是否越权 ⇒ 拒绝（不能默认放行）。"""
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression("STATUS <> 'X'", None)

    def test_rejects_empty_allowlist_with_expression(self) -> None:
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression("STATUS <> 'X'", [])

    def test_allows_empty_expression(self) -> None:
        """没有过滤表达式就没有过滤标识符 ⇒ 不需要白名单。"""
        validateFilterExpression(None, None)
        validateFilterExpression("", [])

    def test_rejects_injection_keyword(self) -> None:
        with pytest.raises(ValidationError):
            validateFilterExpression("STATUS = 'X' UNION SELECT 1", ["STATUS"])

    def test_allowlist_is_case_insensitive(self) -> None:
        validateFilterExpression("status = 'X'", ["STATUS"])

    def test_reports_all_missing_identifiers(self) -> None:
        """错误信息要一次列全缺失项，避免用户来回试。"""
        with pytest.raises(ValidationError) as excinfo:
            validateFilterExpression("A = 1 AND B = 2 AND C = 3", ["A"])
        message = str(excinfo.value)
        assert "B" in message and "C" in message


class TestValidateKeyOrdinals:
    def test_accepts_contiguous_ordinals(self) -> None:
        validateKeyOrdinals([1, 2, 3])

    def test_accepts_ordinals_with_none_entries(self) -> None:
        """NULL = 非键列 ⇒ 忽略。"""
        validateKeyOrdinals([1, None, 2])

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError, match="重复"):
            validateKeyOrdinals([1, 1])

    def test_rejects_non_contiguous(self) -> None:
        with pytest.raises(ValidationError, match="连续"):
            validateKeyOrdinals([1, 3])

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            validateKeyOrdinals([0, 1])

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            validateKeyOrdinals([-1])

    def test_accepts_empty(self) -> None:
        validateKeyOrdinals([])


class TestValidateBindingRole:
    @pytest.mark.parametrize("role", ["BASE", "NUMERATOR", "DENOMINATOR", "FILTER"])
    def test_accepts_known_roles(self, role: str) -> None:
        validateBindingRole(role)

    def test_rejects_unknown_role(self) -> None:
        with pytest.raises(ValidationError, match="bindingRole"):
            validateBindingRole("SOURCE")


class TestValidateColumnName:
    def test_accepts_normal_column(self) -> None:
        validateColumnName("ORDER_QTY")

    def test_rejects_sql_injection_in_column_name(self) -> None:
        with pytest.raises(ValidationError):
            validateColumnName("QTY; DROP TABLE X")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            validateColumnName("")

    def test_rejects_trailing_newline(self) -> None:
        """`re.match` + `$` 会放行尾随换行（Python 的 `$` 语义）⇒ 必须 fullmatch。"""
        with pytest.raises(ValidationError):
            validateColumnName("ORDER_QTY\n")


class TestExtractCallIdentifiers:
    """函数调用形态的标识符必须与普通列名**分开**（函数是代码，列是数据）。"""

    def test_detects_function_call(self) -> None:
        assert extractCallIdentifiers("UPPER(STATUS)") == {"UPPER"}

    def test_detects_multiple_calls(self) -> None:
        found = extractCallIdentifiers("ROUND(NVL(ORDER_QTY, 0), 2)")
        assert found == {"ROUND", "NVL"}

    def test_ignores_plain_columns(self) -> None:
        assert extractCallIdentifiers("ORDER_QTY / 1000") == set()

    def test_ignores_sql_keyword_used_as_call(self) -> None:
        """`IN (` 是 SQL 语法，不是函数调用 ⇒ 不该进受控函数清单。"""
        assert extractCallIdentifiers("STATUS IN ('A', 'B')") == set()

    def test_ignores_string_literal_content(self) -> None:
        """字面量里的 `(` 不是调用形态。"""
        assert extractCallIdentifiers("STATUS = 'X('") == set()

    def test_plain_identifiers_exclude_function_names(self) -> None:
        plain = extractPlainIdentifiers("UPPER(ORDER_STATUS)")
        assert plain == {"ORDER_STATUS"}


class TestValidateFilterExpressionOrBan:
    """HIGH-1：`STATUS = 'x' OR 1=1` 恒真 ⇒ 行级过滤被抹平成全表读。"""

    def test_rejects_top_level_or(self) -> None:
        with pytest.raises(ValidationError, match="OR"):
            validateFilterExpression("STATUS = 'x' OR 1=1", ["STATUS"])

    def test_rejects_nested_or(self) -> None:
        with pytest.raises(ValidationError, match="OR"):
            validateFilterExpression("STATUS = 'x' AND (AMT > 1 OR AMT < 0)", ["STATUS"])

    def test_rejects_or_with_allowlisted_columns_only(self) -> None:
        """两个列都在白名单里也一样拒 —— 危险的不是列，是恒真。"""
        with pytest.raises(ValidationError, match="OR"):
            validateFilterExpression("STATUS = 'x' OR AMT > 0", ["STATUS", "AMT"])

    def test_allows_or_inside_string_literal(self) -> None:
        """字面量里的 OR 是数据，不是逻辑或 ⇒ 不误杀。"""
        validateFilterExpression("STATUS = 'A OR B'", ["STATUS"])

    def test_allows_column_named_order(self) -> None:
        """`ORDER_QTY` 里的 OR 不是独立单词 ⇒ 不误杀。"""
        validateFilterExpression("ORDER_QTY > 0", ["ORDER_QTY"])


class TestValidateFilterExpressionFunctionGate:
    """HIGH-2：函数是代码 ⇒ 走受控清单，不许绑定白名单自报。"""

    def test_rejects_unknown_function(self) -> None:
        with pytest.raises(ValidationError, match="未授权的函数"):
            validateFilterExpression("PG_SLEEP(10) > 0", ["PG_SLEEP"])

    def test_rejects_unknown_function_even_if_allowlisted(self) -> None:
        """自报进白名单也不行 —— 白名单授权的是**列**，不是函数。"""
        with pytest.raises(ValidationError, match="未授权的函数"):
            validateFilterExpression("PG_SLEEP(10) > 0", ["STATUS", "PG_SLEEP"])

    def test_rejects_qualified_function(self) -> None:
        with pytest.raises(ValidationError, match="未授权的函数"):
            validateFilterExpression("DBMS_LOCK.SLEEP(10) > 0", ["DBMS_LOCK"])

    def test_allows_controlled_function_with_column_comparison(self) -> None:
        """受控函数可以出现，但过滤里必须**另有一处裸列比较**（见恒真闸门）。"""
        validateFilterExpression("STATUS = 'X' AND UPPER(NAME) = 'Y'", ["STATUS", "NAME"])


class TestFilterExpressionConstantTrue:
    """HIGH-2（第三轮）：恒真谓词会把行级过滤抹平成全表读。

    禁 `OR` 只堵住恒真的**一种**写法 —— 这一组钉住其余写法。
    """

    def test_rejects_case_always_true(self) -> None:
        """`CASE ... ELSE 1 END = 1` 恒真（NULL 也走 ELSE）。"""
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression(
                "CASE WHEN STATUS = 'x' THEN 1 ELSE 1 END = 1", ["STATUS"]
            )

    def test_rejects_function_of_column_compared_to_constant(self) -> None:
        """`ABS(A) >= 0` 对非空行恒真 ⇒ 引用合法 ⇒ 白名单闸门放行 ⇒ 必须另设闸门。"""
        with pytest.raises(ValidationError, match="列 与 常量"):
            validateFilterExpression("ABS(STATUS) >= 0", ["STATUS"])

    def test_rejects_column_compared_to_itself(self) -> None:
        """`A = A` 恒真（列与列比较 ⇒ 两侧都不是常量）。"""
        with pytest.raises(ValidationError, match="列 与 常量"):
            validateFilterExpression("STATUS = STATUS", ["STATUS"])

    def test_rejects_function_only_comparison(self) -> None:
        """只有函数比较 ⇒ 整条过滤与行无关 ⇒ 恒真。"""
        with pytest.raises(ValidationError, match="列 与 常量"):
            validateFilterExpression("UPPER(STATUS) = 'X'", ["STATUS"])

    def test_rejects_keyword_as_column(self) -> None:
        """**关键字不是列**：`END` 是 `CASE` 的收尾关键字，不能满足「裸列↔常量」闸门。

        否则 `CASE ... END = 1` 能靠 `END` 满足这条闸门 ⇒ 恒真谓词从侧门进来。
        """
        with pytest.raises(ValidationError, match="列 与 常量"):
            validateFilterExpression("STATUS AND END = 1", ["STATUS"])

    def test_allows_is_not_null(self) -> None:
        validateFilterExpression("STATUS IS NOT NULL", ["STATUS"])

    def test_allows_in_list_of_constants(self) -> None:
        validateFilterExpression("STATUS IN ('A', 'B')", ["STATUS"])

    def test_allows_between_constants(self) -> None:
        validateFilterExpression("STATUS BETWEEN 1 AND 10", ["STATUS"])

    def test_allows_negated_comparison(self) -> None:
        validateFilterExpression("NOT (STATUS = 'x')", ["STATUS"])

    def test_allows_quoted_column_against_constant(self) -> None:
        validateFilterExpression('"SALARY NET" > 0', ["SALARY NET"])


class TestFilterExpressionNonAsciiLiterals:
    """业务数据是中文 ⇒ 字面量必须能带中文（共享校验器只认 ASCII ⇒ 先掩码）。"""

    def test_allows_chinese_string_literal(self) -> None:
        validateFilterExpression("STATUS = '有效'", ["STATUS"])

    def test_allows_chinese_literal_with_function_comparison(self) -> None:
        validateFilterExpression("STATUS = '有效' AND UPPER(NAME) = 'X'", ["STATUS", "NAME"])

    def test_rejects_non_ascii_outside_literal(self) -> None:
        """掩码只覆盖**字面量内部** ⇒ 引号外的中文仍然被拒。"""
        with pytest.raises(ValidationError, match="非法字符"):
            validateFilterExpression("STATUS = '有效' AND 备注 <> ''", ["STATUS"])

    def test_masked_expression_keeps_non_ascii_outside_literal(self) -> None:
        masked = _maskNonAsciiInLiterals("STATUS = '有效' AND 备注 = 'x'")
        assert "有效" not in masked
        assert "备注" in masked

    def test_masked_expression_keeps_ascii_literal_unchanged(self) -> None:
        assert _maskNonAsciiInLiterals("STATUS = 'ACTIVE'") == "STATUS = 'ACTIVE'"


class TestFilterExpressionTableKeyword:
    """第三轮 HIGH-1：`EXISTS (TABLE t)` 不出现 SELECT/FROM 也能跨表读。"""

    def test_rejects_table_relation_expression(self) -> None:
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression("EXISTS (TABLE OTHER_TBL)", ["OTHER_TBL"])

    def test_rejects_exists_without_select(self) -> None:
        """`EXISTS` 之前被当成 SQL 关键字 ⇒ 禁令是死代码 ⇒ 现在必须能被看见。"""
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression("EXISTS (TABLE OTHER_TBL)", ["EXISTS", "OTHER_TBL"])

    def test_rejects_table_in_allowlist(self) -> None:
        """白名单项也不能是 `TABLE`（它是结构词）。"""
        with pytest.raises(ValidationError, match="保留字"):
            validateAllowlistItems(["OTHER_TBL", "TABLE"])

    def test_rejects_lateral(self) -> None:
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression("STATUS = 'x' AND LATERAL OTHER_TBL = 1", ["STATUS"])


class TestValidateFilterExpressionReservedWords:
    """子查询 / 多语句的结构词不许出现（白名单自报也不能放行）。"""

    def test_rejects_select(self) -> None:
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression("EXISTS (SELECT 1 FROM X)", ["EXISTS", "SELECT", "FROM", "X"])

    def test_rejects_from(self) -> None:
        with pytest.raises(ValidationError, match="结构词"):
            validateFilterExpression("STATUS IN (SELECT STATUS FROM OTHER_TBL)", ["STATUS"])

    def test_rejects_union(self) -> None:
        with pytest.raises(ValidationError):
            validateFilterExpression("STATUS = 'X' UNION SELECT 1 FROM Y", ["STATUS"])


class TestDollarIdentifier:
    """MEDIUM-1：抽取必须认 `$`，否则 `status$1` 会被切成 `status` 放行。"""

    def test_dollar_identifier_is_extracted_whole(self) -> None:
        found = extractIdentifiers("status$1 > 0")
        assert found == {"status$1"}

    def test_dollar_identifier_rejected_by_filter(self) -> None:
        with pytest.raises(ValidationError, match="白名单"):
            validateFilterExpression("status$1 > 0", ["STATUS"])

    def test_dollar_identifier_cannot_be_allowlisted(self) -> None:
        """`$` 列名永远进不了合法白名单 ⇒ 不存在「合法放行 status$1」的口子。"""
        with pytest.raises(ValidationError, match="列名非法"):
            validateAllowlistItems(["status$1"])


class TestValidateAllowlistItemsReserved:
    def test_rejects_sql_keyword_item(self) -> None:
        with pytest.raises(ValidationError, match="保留字"):
            validateAllowlistItems(["IN"])

    def test_rejects_structure_word_item(self) -> None:
        with pytest.raises(ValidationError, match="保留字"):
            validateAllowlistItems(["SELECT"])

    def test_rejects_dml_word_item(self) -> None:
        with pytest.raises(ValidationError, match="保留字"):
            validateAllowlistItems(["DROP"])


class TestValidateTransformExpressionHardening:
    def test_rejects_or(self) -> None:
        with pytest.raises(ValidationError, match="OR"):
            validateTransformExpression(
                "NVL(ORDER_QTY, 0) > 0 OR ORDER_QTY < 0", "ORDER_QTY"
            )

    def test_rejects_unknown_function(self) -> None:
        with pytest.raises(ValidationError, match="未授权的函数"):
            validateTransformExpression("PG_SLEEP(ORDER_QTY)", "ORDER_QTY")

    def test_rejects_reserved_word(self) -> None:
        with pytest.raises(ValidationError, match="结构词"):
            validateTransformExpression("ORDER_QTY FROM OTHER", "ORDER_QTY")

    def test_rejects_cross_column(self) -> None:
        with pytest.raises(ValidationError, match="未授权"):
            validateTransformExpression("ORDER_QTY + OTHER_QTY", "ORDER_QTY")

    def test_allows_case_projection(self) -> None:
        """变换表达式里 `CASE` 只是常量投影 ⇒ **不受**过滤表达式的 CASE 禁令影响。"""
        validateTransformExpression(
            "CASE WHEN ORDER_QTY > 0 THEN 1 ELSE 0 END", "ORDER_QTY"
        )


class TestValidateAllowlistItemsLength:
    """MEDIUM-1：PG 对数组元素超长是**静默截断** ⇒ 长度闸门必须住在校验层。

    DTO 层的 `max_length` 挡不住不经 HTTP 的写入方（导入作业 / 脚本 / 内部服务）。
    """

    def test_rejects_too_long_item(self) -> None:
        with pytest.raises(ValidationError, match="过长"):
            validateAllowlistItems(["B" * (MAX_ALLOWLIST_ITEM_LENGTH + 1)])

    def test_accepts_item_at_limit(self) -> None:
        validateAllowlistItems(["B" * MAX_ALLOWLIST_ITEM_LENGTH])

    def test_limit_matches_library_column_width(self) -> None:
        """上限必须是 100（库列 `ARRAY(String(100))`）⇒ 改动要同步库迁移。"""
        assert MAX_ALLOWLIST_ITEM_LENGTH == 100


class TestClassifyIntegrityError:
    """MEDIUM-2：DB 异常翻译的两条分支都要有断言。

    为什么重要：集成测试里**唯一可达**的 IntegrityError 是唯一索引冲突 ⇒
    「CHECK / 外键 / 非空 ⇒ 422」这条分支在服务层被前置校验挡住 ⇒
    不补单测的话，把 `isUnique` 改成恒真（全部报 409）**不会有任何测试变红**。
    """

    @staticmethod
    def _error(constraintName: str = "", sqlstate: str = "") -> IntegrityError:
        class _Orig(Exception):
            def __init__(self) -> None:
                super().__init__("duplicate key value violates unique constraint")
                self.constraint_name = constraintName
                self.sqlstate = sqlstate

        return IntegrityError("INSERT ...", {}, _Orig())

    def test_unique_constraint_name_is_conflict(self) -> None:
        error = self._error("uq_procurement_source_binding_active", "23505")
        assert isinstance(_classifyIntegrityError(error, "写入失败"), ConflictError)

    def test_unique_sqlstate_is_conflict(self) -> None:
        """没有约束名时靠 SQLSTATE 判定。"""
        error = self._error("", "23505")
        assert isinstance(_classifyIntegrityError(error, "写入失败"), ConflictError)

    def test_check_constraint_is_validation_error(self) -> None:
        error = self._error("ck_procurement_source_binding_role", "23514")
        assert isinstance(_classifyIntegrityError(error, "写入失败"), ValidationError)

    def test_foreign_key_is_validation_error(self) -> None:
        error = self._error("fk_procurement_property_binding_source", "23503")
        assert isinstance(_classifyIntegrityError(error, "写入失败"), ValidationError)

    def test_not_null_sqlstate_is_validation_error(self) -> None:
        error = self._error("", "23502")
        assert isinstance(_classifyIntegrityError(error, "写入失败"), ValidationError)

    def test_constraint_name_not_leaked_to_user(self) -> None:
        """约束名只进服务端日志 ⇒ 不得出现在给用户的文案里。"""
        error = self._error("ck_procurement_source_binding_secret_name", "23514")
        message = str(_classifyIntegrityError(error, "写入失败"))
        assert "ck_procurement_source_binding_secret_name" not in message

