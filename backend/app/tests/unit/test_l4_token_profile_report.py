"""l4_token_profile_report 聚合/渲染单元测试（feat/l4-token-profiling）。

用合成 profile 数据验证：
- 标定后组件和 == provider 实测 prompt 总和（分母一致性）
- resend tax / 缓存有效计费 / 台账对比
- 按 runId 分组、乱序输入、缺 usage 轮次标记
- 工具结果跨轮持久化计费
- 解析统计 linesSeen/matched/skipped
"""

import pytest

from app.services import l4_token_profile_report as report


def _profile(iteration: int, prompt: int | None, components, *, runId="r1", cached=None):
    est = sum(e for _, e in components)
    toolResults = [
        {"name": n.split(":", 1)[1], "count": 1, "chars": 0, "escapeChars": 0, "estimatedTokens": e}
        for n, e in components
        if n.startswith("tool:")
    ]
    return {
        "event": "iteration",
        "runId": runId,
        "sessionId": "s1",
        "iteration": iteration,
        "modelName": "deepseek-chat",
        "messagesCount": 5,
        "usagePresent": prompt is not None,
        "providerPromptTokens": prompt,
        "providerCompletionTokens": 10,
        "cachedTokens": cached,
        "cacheHitRate": None,
        "estimatedTotalTokens": est,
        "scale": round(prompt / est, 4) if prompt and est else None,
        "components": [{"name": n, "chars": 0, "estimatedTokens": e} for n, e in components],
        "toolResults": toolResults,
    }


def _toolRecord(iteration: int, **overrides):
    record = {
        "event": "tool_result",
        "runId": "r1",
        "sessionId": "s1",
        "iteration": iteration,
        "toolCallId": f"t{iteration}",
        "toolName": "execute_sql",
        "rawChars": 100,
        "sentChars": 160,
        "escapeExpansion": 1.6,
        "rowCount": 5,
    }
    record.update(overrides)
    return record


class TestAggregateRun:
    def test_calibrated_components_sum_to_provider_total(self):
        profiles = [
            _profile(1, 1000, [("user", 400), ("tool:execute_sql", 600)]),
            _profile(2, 2000, [("user", 500), ("tool:execute_sql", 1000)]),
        ]

        run = report.aggregateRun("r1", profiles)

        assert run.totalPromptTokens == 3000
        calibratedSum = sum(c.calibratedTokens for c in run.components)
        assert calibratedSum == pytest.approx(3000, rel=1e-3)

    def test_resend_tax_is_sum_of_previous_prompts(self):
        profiles = [
            _profile(1, 1000, [("user", 400)]),
            _profile(2, 2000, [("user", 500)]),
            _profile(3, 3000, [("user", 600)]),
        ]

        run = report.aggregateRun("r1", profiles)

        # 为旧上下文重复支付：P1 + P2
        assert run.resendTaxTokens == 1000 + 2000

    def test_cache_effective_billed_input(self):
        profiles = [
            _profile(1, 1000, [("user", 400)], cached=800),
            _profile(2, 1000, [("user", 500)], cached=0),
        ]

        run = report.aggregateRun("r1", profiles, cacheMultiplier=0.25)

        # (1000-800) + 800×0.25 + 1000 = 200 + 200 + 1000
        assert run.effectiveBilledInputTokens == 1400
        assert run.totalCachedTokens == 800

    def test_cached_none_excluded_from_cache_totals(self):
        profiles = [
            _profile(1, 1000, [("user", 400)], cached=None),
            _profile(2, 1000, [("user", 500)], cached=500),
        ]

        run = report.aggregateRun("r1", profiles, cacheMultiplier=0.25)

        assert run.totalCachedTokens == 500
        # C=None 的轮按全额计费：1000 + (500 + 500×0.25)
        assert run.effectiveBilledInputTokens == 1625

    def test_unknown_usage_iterations_flagged_not_zeroed(self):
        profiles = [
            _profile(1, None, [("user", 400)]),
            _profile(2, 2000, [("user", 500)]),
        ]

        run = report.aggregateRun("r1", profiles)

        assert run.unknownUsageIterations == (1,)
        assert run.totalPromptTokens == 2000

    def test_out_of_order_iterations_sorted(self):
        profiles = [
            _profile(2, 2000, [("user", 500)]),
            _profile(1, 1000, [("user", 400)]),
        ]

        run = report.aggregateRun("r1", profiles)

        assert run.iterationsUsed == 2
        assert run.resendTaxTokens == 1000

    def test_tool_result_persists_across_iterations(self):
        # 同一工具结果在第 1、2 轮都随 prompt 重发 ⇒ 计费翻倍
        profiles = [
            _profile(1, 1000, [("user", 400), ("tool:execute_sql", 600)]),
            _profile(2, 2000, [("user", 500), ("tool:execute_sql", 1000)]),
        ]
        records = [_toolRecord(1)]

        run = report.aggregateRun("r1", profiles, toolResultRecords=records)

        toolTotal = next(t for t in run.toolTotals if t.name == "execute_sql")
        # 标定后跨轮累计 = 600×1.0 + 1000×(2000/1500)
        assert toolTotal.calibratedTokens == pytest.approx(600 + 2000 * (1000 / 1500), rel=1e-3)
        assert len(run.toolResultRecords) == 1


class TestParseProfileLines:
    def test_groups_by_run_and_counts_noise(self):
        import json

        from app.services.l4_token_profile import TOKEN_PROFILE_TAG, TOOL_RESULT_TAG

        def _line(tag, payload):
            return f"2026-10-31 INFO app: {tag} {json.dumps(payload, ensure_ascii=False)}"

        lines = [
            _line(TOKEN_PROFILE_TAG, _profile(1, 100, [("user", 50)], runId="rA")),
            _line(TOKEN_PROFILE_TAG, _profile(1, 200, [("user", 80)], runId="rB")),
            _line(TOKEN_PROFILE_TAG, _profile(2, 300, [("user", 90)], runId="rA")),
            _line(TOOL_RESULT_TAG, _toolRecord(1, runId="rA")),
            "INFO unrelated log line",
            f"{TOKEN_PROFILE_TAG} " + '{"truncat',
        ]

        parsed = report.parseProfileLines(lines)

        assert parsed.linesSeen == 6
        assert parsed.linesMatched == 4
        assert parsed.linesSkipped == 2
        assert set(parsed.runs.keys()) == {"rA", "rB"}
        assert [p["iteration"] for p in parsed.runs["rA"]] == [1, 2]
        assert len(parsed.toolResultRecords) == 1

    def test_handles_docker_json_wrapper(self):
        import json

        from app.services.l4_token_profile import TOKEN_PROFILE_TAG

        inner = f"{TOKEN_PROFILE_TAG} {json.dumps(_profile(1, 100, [('user', 50)]))}"
        wrapper = json.dumps({"log": inner, "stream": "stdout"})

        parsed = report.parseProfileLines([wrapper])

        assert parsed.linesMatched == 1
        assert "r1" in parsed.runs


class TestRenderMarkdown:
    def test_markdown_contains_core_tables_and_totals(self):
        profiles = [
            _profile(1, 1000, [("user", 400), ("tool:execute_sql", 600)], cached=800),
            _profile(2, 2000, [("user", 500), ("tool:execute_sql", 1000)]),
        ]
        run = report.aggregateRun(
            "r1",
            profiles,
            toolResultRecords=[_toolRecord(1)],
            inputPricePer1k=0.0014,
            outputPricePer1k=0.0028,
        )

        md = report.renderMarkdown(run)

        assert "## 逐轮明细" in md
        assert "## 组件构成" in md
        assert "## 工具结果" in md
        assert "## 缓存与成本" in md
        assert "3000" in md  # provider 实测总量

    def test_component_table_sums_to_total(self):
        profiles = [
            _profile(1, 1000, [("user", 400), ("tool:execute_sql", 600)]),
            _profile(2, 2000, [("user", 500), ("tool:execute_sql", 1000)]),
        ]
        run = report.aggregateRun("r1", profiles)

        md = report.renderMarkdown(run)

        # 合计行的标定值 ≈ provider 总量 3000
        assert "3000" in md
