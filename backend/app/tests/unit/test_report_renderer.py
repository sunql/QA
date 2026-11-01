"""研究报告 MD 渲染：中文表头与末尾字段治理提示。"""


def test_render_markdown_uses_column_labels_in_table_header():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "table",
                        "content": {
                            "columns": ["rcv_qty"],
                            "rows": [{"rcv_qty": 1}],
                            "columnLabels": {"rcv_qty": "收货数量（件）"},
                        },
                    }
                ],
            }
        ],
    }
    markdown = renderMarkdown(payload)
    assert "| 收货数量（件） |" in markdown


def test_render_markdown_appends_field_hints():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "chart",
                        "content": {
                            "columns": ["a"],
                            "data": [{"a": 1}],
                            "chartType": "bar",
                            "fieldHints": {
                                "missingLabels": ["a"],
                                "missingUnits": [],
                            },
                        },
                    }
                ],
            }
        ],
    }
    markdown = renderMarkdown(payload)
    assert "字段治理提示" in markdown
    # 提示必须收尾（不是夹在正文中间）
    lastLine = [line for line in markdown.splitlines() if line.strip()][-1]
    assert lastLine.startswith("以下字段缺少中文含义")


def test_render_markdown_hint_uses_label_from_chart_block():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "chart",
                        "content": {
                            "columns": ["a"],
                            "data": [{"a": 1}],
                            "chartType": "bar",
                            "fieldHints": {"missingLabels": ["a"], "missingUnits": []},
                            "columnLabels": {"a": "收货数量"},
                        },
                    }
                ],
            }
        ],
    }
    hintText = renderMarkdown(payload).split("字段治理提示", 1)[1]
    assert "收货数量" in hintText
    assert "补充别名：a" not in hintText


def test_render_markdown_hint_uses_label_from_table_block():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "table",
                        "content": {
                            "columns": ["a"],
                            "rows": [{"a": 1}],
                            "columnLabels": {"a": "收货数量"},
                        },
                    },
                    {
                        "type": "chart",
                        "content": {
                            "columns": ["a"],
                            "data": [{"a": 1}],
                            "chartType": "bar",
                            "fieldHints": {"missingLabels": ["a"], "missingUnits": []},
                        },
                    },
                ],
            }
        ],
    }
    hintText = renderMarkdown(payload).split("字段治理提示", 1)[1]
    assert "收货数量" in hintText
    assert "补充别名：a" not in hintText


def test_render_markdown_golden_without_labels_or_hints():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "销售报告",
        "mode": "research",
        "question": "本月销量？",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {"type": "text", "content": "一句话结论。"},
                    {
                        "type": "table",
                        "content": {
                            "columns": ["a", "b"],
                            "rows": [{"a": 1, "b": "x"}],
                        },
                    },
                ],
            }
        ],
    }
    expected = (
        "# 销售报告\n"
        "\n"
        "> 模式：research｜问题：本月销量？\n"
        "\n"
        "## 数据\n"
        "\n"
        "一句话结论。\n"
        "\n"
        "| a | b |\n"
        "| --- | --- |\n"
        "| 1 | x |\n"
    )
    assert renderMarkdown(payload) == expected
