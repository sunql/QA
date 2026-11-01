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
