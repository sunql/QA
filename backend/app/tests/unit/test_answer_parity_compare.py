"""A5：答案级评估的**比较层**单测 —— 退出码语义、L3 硬红线、抖动归因、L2/L4/L5。

为什么这些必须钉死
------------------
1. **退出码是契约**：0/2/3/4/5/6 各有明确含义。若「口径丢了」被算成 0，
   开闸判据就失效（这正是 D17 实测到的那类漂移）。
2. **L3 是硬红线**：不接受「差异率 < 5% 就算过」这类阈值 —— 阈值会把口径丢失稀释掉。
3. **同态抖动 J > 0 ⇒ 6**：同态都复现不了时，任何跨态差异都不能归因到开关。
4. **假绿防线**：别名前缀「整段删掉」会让不同表上的同名列碰巧相等；
   这里必须证明别名解析保留了表身份。

阳性对照（`runSelfTest`）本身也要有反向守卫：把比较器改坏，自检必须变红 ——
否则「15/15 通过」可能只是断言空集（本仓反复踩过的坑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import answer_parity_cases as cases
from scripts import answer_parity_compare as parity
from scripts import diag_semantic_switchover as diag
from scripts import eval_semantic_answer_parity as evalModule

_QUESTIONS_PATH = evalModule._DEFAULT_QUESTIONS


# ---------------------------------------------------------------------------
# 契约与版本化
# ---------------------------------------------------------------------------


class Test合同:
    def test_退出码契约与D17一致(self) -> None:
        assert parity.EXIT_OK == diag.EXIT_OK
        assert parity.EXIT_NOT_EQUIVALENT == diag.EXIT_NOT_EQUIVALENT
        assert parity.EXIT_ANSWER_MISMATCH == diag.EXIT_REPLAY_MISMATCH
        assert parity.EXIT_PRECONDITION == diag.EXIT_PRECONDITION
        assert parity.EXIT_GUARD_BROKEN == diag.EXIT_GUARD_BROKEN
        assert parity.EXIT_INCONCLUSIVE == diag.EXIT_INCONCLUSIVE

    def test_问题集摘要可校验(self) -> None:
        payload = evalModule.loadQuestions(_QUESTIONS_PATH)
        assert payload["version"] == "v1"
        assert payload["sha256"] == parity.questionsDigest(payload)
        assert len(payload["questions"]) == 7
        assert [item["id"] for item in evalModule._applicableQuestions(payload)] == [
            "Q01",
            "Q02",
            "Q03",
            "Q05",
        ]
        # v1 的核心改动：题目必须带可执行的实体编码（v0 的「指定物料」让模型拒答）
        byId = {item["id"]: item for item in payload["questions"]}
        assert "A0001" in byId["Q01"]["question"]
        assert "S0001" in byId["Q02"]["question"]
        assert "S0001" in byId["Q05"]["question"]

    def test_改题不重算摘要会被拒绝(self, tmp_path: Path) -> None:
        payload = json.loads(_QUESTIONS_PATH.read_text(encoding="utf-8"))
        payload["questions"][0]["question"] = "被篡改的问题"
        tampered = tmp_path / "questions.json"
        tampered.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="摘要不符"):
            evalModule.loadQuestions(tampered)

    def test_未知层直接拒绝(self) -> None:
        assert evalModule._parseLayers("L1, L3") == ("L1", "L3")
        with pytest.raises(ValueError, match="未知层"):
            evalModule._parseLayers("L1,L9")


# ---------------------------------------------------------------------------
# L1 / L3：SQL 规范化与口径集合
# ---------------------------------------------------------------------------


class TestSql规范化:
    def test_别名命名不同但表身份相同则规范化一致(self) -> None:
        left = parity.canonicalSql(cases._OFF_SQL)
        right = parity.canonicalSql(cases._ON_SQL_RENAMED_ALIASES)
        assert left == right, "别名名可以任意，但规范化后的表身份不能丢"

    def test_单值IN与等号归一(self) -> None:
        inForm = "SELECT 1 FROM T WHERE X IN ('A')"
        eqForm = "SELECT 1 FROM T WHERE X = 'A'"
        assert parity.predicateSet(inForm) == parity.predicateSet(eqForm)

    def test_不同表上的同名列不能碰巧相等(self) -> None:
        """假绿防线：把别名整段删掉会让 `a.NAME` / `b.NAME` 都变成 `NAME`。"""
        left = "SELECT 1 FROM T1 a JOIN T2 b ON a.X = b.Y WHERE a.NAME = 'x'"
        right = "SELECT 1 FROM T1 a JOIN T2 b ON a.X = b.Y WHERE b.NAME = 'x'"
        difference = parity.coverageDiff(left, right)
        assert not difference.isEmpty
        assert difference.offOnly == ("T1.NAME = 'x'",)
        assert difference.onOnly == ("T2.NAME = 'x'",)

    def test_schema限定不同的同名表必须被发现(self) -> None:
        difference = parity.coverageDiff("SELECT X FROM ODS.T", "SELECT X FROM DWD.T")
        assert difference.offOnly == ("FROM:ODS.T",)
        assert difference.onOnly == ("FROM:DWD.T",)

    def test_Oracle分页与分页时的排序键都必须被发现(self) -> None:
        base = "SELECT X FROM T ORDER BY X FETCH FIRST 10 ROWS ONLY"
        assert not parity.coverageDiff(
            base, "SELECT X FROM T ORDER BY X FETCH FIRST 20 ROWS ONLY"
        ).isEmpty
        assert not parity.coverageDiff(
            base, "SELECT X FROM T ORDER BY Y FETCH FIRST 10 ROWS ONLY"
        ).isEmpty
        # 无分页时排序只是展示顺序，不影响多重集
        assert parity.coverageDiff(
            "SELECT X FROM T ORDER BY X", "SELECT X FROM T ORDER BY Y"
        ).isEmpty


class TestL3口径覆盖:
    def test_谓词重排不算差异(self) -> None:
        reordered = cases._OFF_SQL.replace(
            "WHERE d.INTER_COM_CODE = '1' AND s.ENAFLG_0 = 2",
            "WHERE s.ENAFLG_0 = 2 AND d.INTER_COM_CODE = '1'",
        )
        assert parity.coverageDiff(cases._OFF_SQL, reordered).isEmpty

    def test_投影列序与别名不算口径差异(self) -> None:
        assert parity.coverageDiff(
            cases._OFF_SQL, cases._ON_SQL_PROJECTION_ONLY
        ).isEmpty

    def test_丢谓词被量化(self) -> None:
        difference = parity.coverageDiff(cases._OFF_SQL, cases._ON_SQL_DROP_PREDICATE)
        assert difference.offOnly == ("DIM_SUPPLIER.ENAFLG_0 = 2",)
        assert difference.rate == pytest.approx(1 / 10)

    def test_丢JOIN与改JOIN类型都被抓到(self) -> None:
        assert not parity.coverageDiff(cases._OFF_SQL, cases._ON_SQL_DROP_JOIN).isEmpty
        assert not parity.coverageDiff(cases._OFF_SQL, cases._ON_SQL_LEFT_JOIN).isEmpty

    def test_改分组键被抓到(self) -> None:
        difference = parity.coverageDiff(cases._OFF_SQL, cases._ON_SQL_GROUP_BY_CHANGED)
        assert not difference.isEmpty
        assert any("GROUPBY" in element for element in difference.onOnly)


# ---------------------------------------------------------------------------
# L2 / L4 / L5
# ---------------------------------------------------------------------------


class TestL2结果集:
    def test_行序与列序无关(self) -> None:
        left = [{"A": 1, "B": 2}, {"A": 3, "B": 4}]
        right = [{"B": 4, "A": 3}, {"B": 2, "A": 1}]
        comparison = parity.compareResultSets(left, right)
        assert comparison is not None and comparison.equal

    def test_数值容差内相等(self) -> None:
        comparison = parity.compareResultSets([{"V": 0.30000000004}], [{"V": 0.3}])
        assert comparison is not None and comparison.equal

    def test_缺一侧则不可判(self) -> None:
        assert parity.compareResultSets(None, [{"A": 1}]) is None

    def test_字符串编码不能被当数字抹平(self) -> None:
        comparison = parity.compareResultSets([{"CODE": "0001"}], [{"CODE": "1"}])
        assert comparison is not None and not comparison.equal


class TestL4数值:
    def test_千分位金额不被拆开(self) -> None:
        tokens = parity.extractNumbers("合计 1,234.56 元")
        assert [str(token) for token in tokens] == ["amount:1234.56"]

    def test_金额容差0_01元(self) -> None:
        assert parity.compareNumbers("合计 1234.56 元", "合计 1234.57 元").equal
        assert not parity.compareNumbers("合计 1234.56 元", "合计 1234.58 元").equal

    def test_比率容差0_1个百分点(self) -> None:
        assert parity.compareNumbers("准时率 92.3%", "准时率 92.4%").equal
        assert not parity.compareNumbers("准时率 92.3%", "准时率 92.5%").equal

    def test_计数必须精确(self) -> None:
        assert not parity.compareNumbers("共 10 条", "共 11 条").equal

    def test_两侧都没有数值则不可判(self) -> None:
        """空集通过防线：纯文本答案不同也判「差异 0」就是假绿。"""
        assert parity.compareNumbers("供应商A可供应", "供应商B可供应") is None


class TestL5答案:
    def test_一侧明确未接入则不等价(self) -> None:
        assert parity.answersEquivalent("合计 10 元", "该数据域未接入") is False

    def test_措辞不同但结论与数值相同则等价(self) -> None:
        assert parity.answersEquivalent("共 10 条记录", "查询结果共 10 条") is True

    def test_两侧都没有数值则判不了(self) -> None:
        assert parity.answersEquivalent("供应商A可供应", "供应商B可供应") is None


# ---------------------------------------------------------------------------
# 逐题判定：退出码语义
# ---------------------------------------------------------------------------


class Test判定顺序:
    def test_L3不一致时不再比L2(self) -> None:
        """查的东西都不一样时，L2 数值接近也不能算过。"""
        evaluation = parity.evaluateQuestion(
            cases._OFF_SQL,
            cases._OFF_SQL,
            cases._ON_SQL_DROP_PREDICATE,
            cases._ON_SQL_DROP_PREDICATE,
            requiredLayers=("L1", "L2", "L3"),
            offRows1=[{"CNT": 10}],
            onRows1=[{"CNT": 10}],
        )
        assert evaluation.exitCode == parity.EXIT_ANSWER_MISMATCH
        assert not any(verdict.layer == "L2" for verdict in evaluation.verdicts)

    def test_同态抖动优先于跨态比较(self) -> None:
        evaluation = parity.evaluateQuestion(
            cases._OFF_SQL,
            cases._JITTER_SQL,
            cases._ON_SQL_DROP_PREDICATE,
            cases._ON_SQL_DROP_PREDICATE,
        )
        assert evaluation.exitCode == parity.EXIT_INCONCLUSIVE
        assert "抖动" in evaluation.reason

    def test_汇总退出码_3优先于6(self) -> None:
        assert (
            parity.aggregateExitCodes(
                [parity.EXIT_OK, parity.EXIT_INCONCLUSIVE, parity.EXIT_ANSWER_MISMATCH]
            )
            == parity.EXIT_ANSWER_MISMATCH
        )
        assert (
            parity.aggregateExitCodes([parity.EXIT_OK, parity.EXIT_INCONCLUSIVE])
            == parity.EXIT_INCONCLUSIVE
        )
        assert parity.aggregateExitCodes([parity.EXIT_OK, parity.EXIT_OK]) == parity.EXIT_OK

    def test_汇总退出码_评估无效压过不通过(self) -> None:
        assert (
            parity.aggregateExitCodes([parity.EXIT_ANSWER_MISMATCH, parity.EXIT_PRECONDITION])
            == parity.EXIT_PRECONDITION
        )
        assert parity.aggregateExitCodes([]) == parity.EXIT_PRECONDITION


# ---------------------------------------------------------------------------
# 阳性对照自检本身
# ---------------------------------------------------------------------------


class Test自检:
    def test_自检全部通过(self) -> None:
        results = cases.runSelfTest()
        assert all(case.passed for case in results), cases.formatSelfTest(results)
        assert cases.selfTestExitCode(results) == parity.EXIT_OK

    def test_比较器坏掉时自检必须变红(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向守卫：把口径集合恒置为空 ⇒ 「丢谓词」「丢 JOIN」用例必须对不上。"""
        from scripts import answer_parity_sql as sqlLayer

        # 两处都要坏：`coverageDiff` 在 sql 层解析，抖动基线在 compare 层解析。
        monkeypatch.setattr(sqlLayer, "predicateSet", lambda sql: frozenset())
        monkeypatch.setattr(parity, "predicateSet", lambda sql: frozenset())
        results = cases.runSelfTest()
        failed = [case.name for case in results if not case.passed]
        assert failed, "比较器被改坏后自检仍然全绿 ⇒ 自检是空集通过"
        assert cases.selfTestExitCode(results) == parity.EXIT_PRECONDITION


# ---------------------------------------------------------------------------
# 档案回放（不连库、不调模型）：实跑与回放共用的判定入口
# ---------------------------------------------------------------------------


_ON_RELEASE_ID = 7


def _writeRun(
    runDir: Path,
    sqlByState: dict[str, str | None],
    *,
    rows: list[dict] | None = None,
    answer: str | None = None,
) -> None:
    """写一份档案；**ON 态必须带发布 id**，否则回放证明不了「ON 真走了快照」。"""
    runDir.mkdir(parents=True, exist_ok=True)
    for name, sql in sqlByState.items():
        record = {
            "questionId": "Q01",
            "sql": sql,
            "semanticReleaseId": None if name.startswith("off") else _ON_RELEASE_ID,
        }
        if rows is not None:
            record["rows"] = rows
        if answer is not None:
            record["answer"] = answer
        (runDir / f"{name}.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
        )


class Test档案回放:
    def _questions(self) -> list[dict]:
        return [{"id": "Q01", "question": "哪些供应商可以供应指定物料？"}]

    def test_四份档案一致则退出0(self, tmp_path: Path) -> None:
        _writeRun(
            tmp_path,
            {"off-1": cases._OFF_SQL, "off-2": cases._OFF_SQL,
             "on-1": cases._OFF_SQL, "on-2": cases._OFF_SQL},
        )
        exitCode, reports = evalModule.evaluateRunDirectory(
            tmp_path, self._questions(), ("L1", "L3")
        )
        assert exitCode == parity.EXIT_OK
        assert reports[0].evaluation.verdicts[1].layer == "L3"
        assert reports[0].evaluation.verdicts[1].status == "PASS"

    def test_丢口径则退出3(self, tmp_path: Path) -> None:
        _writeRun(
            tmp_path,
            {"off-1": cases._OFF_SQL, "off-2": cases._OFF_SQL,
             "on-1": cases._ON_SQL_DROP_PREDICATE, "on-2": cases._ON_SQL_DROP_PREDICATE},
        )
        exitCode, _reports = evalModule.evaluateRunDirectory(
            tmp_path, self._questions(), ("L1", "L3")
        )
        assert exitCode == parity.EXIT_ANSWER_MISMATCH

    def test_缺一份档案则不可判定(self, tmp_path: Path) -> None:
        _writeRun(
            tmp_path,
            {"off-1": cases._OFF_SQL, "off-2": cases._OFF_SQL, "on-1": None},
        )
        exitCode, _reports = evalModule.evaluateRunDirectory(
            tmp_path, self._questions(), ("L1", "L3")
        )
        assert exitCode == parity.EXIT_INCONCLUSIVE

    def test_ON缺发布id视为无效证据(self, tmp_path: Path) -> None:
        """OFF vs OFF 会被判「一致」⇒ 回放必须能证明 ON 真走了快照。"""
        _writeRun(
            tmp_path,
            {"off-1": cases._OFF_SQL, "off-2": cases._OFF_SQL,
             "on-1": cases._OFF_SQL},
        )
        # 把 ON 的 release id 抹成 null
        path = tmp_path / "on-1.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["semanticReleaseId"] = None
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="快照"):
            evalModule.evaluateRunDirectory(tmp_path, self._questions(), ("L1", "L3"))

    def test_回放可比L2且列名参与(self, tmp_path: Path) -> None:
        _writeRun(
            tmp_path,
            {"off-1": cases._OFF_SQL, "off-2": cases._OFF_SQL,
             "on-1": cases._OFF_SQL, "on-2": cases._OFF_SQL},
            rows=[{"SUPPLIER_CODE": "S1", "CNT": 10}],
        )
        # ON 列名不同、值相同 ⇒ L2 必须判差异（不是 0）
        for name in ("on-1", "on-2"):
            (tmp_path / f"{name}.jsonl").write_text(
                json.dumps(
                    {
                        "questionId": "Q01",
                        "sql": cases._OFF_SQL,
                        "semanticReleaseId": _ON_RELEASE_ID,
                        "rows": [{"SUPPLIER": "S1", "CNT": 10}],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        exitCode, reports = evalModule.evaluateRunDirectory(
            tmp_path, self._questions(), ("L1", "L2", "L3")
        )
        assert exitCode == parity.EXIT_ANSWER_MISMATCH
        l2 = [verdict for verdict in reports[0].evaluation.verdicts if verdict.layer == "L2"]
        assert l2 and l2[0].status == "DIFF"


class Test命令行:
    def test_环境变量名只认名字不认值(self, capsys: pytest.CaptureFixture[str]) -> None:
        secret = "postgres://qa_user:super-secret@10.0.0.1:5432/qa_metadata"
        exitCode = evalModule.main(["--database-url-env", secret])
        assert exitCode == parity.EXIT_PRECONDITION
        captured = capsys.readouterr()
        assert "super-secret" not in captured.err + captured.out
        assert "环境变量名" in captured.err

    def test_空层列表被拒绝(self) -> None:
        assert evalModule.main(["--layers", " , "]) == parity.EXIT_PRECONDITION

    def test_回放目录不存在返回4(self, tmp_path: Path) -> None:
        exitCode = evalModule.main(
            ["--replay-dir", str(tmp_path / "nope"), "--layers", "L1,L3"]
        )
        assert exitCode == parity.EXIT_PRECONDITION

    def test_实跑不支持L2至L5(self) -> None:
        """实跑不执行 SQL ⇒ 要求 L2 必须被显式拒绝，不能静默判「通过」。"""
        exitCode = evalModule.main(["--layers", "L1,L2", "--precheck-only"])
        assert exitCode == parity.EXIT_PRECONDITION
