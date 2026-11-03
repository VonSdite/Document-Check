from app.documents.extraction.pdf import _format_pdf_table_html

TABLE_ID = "page075-table001"


def pdf_table_model() -> dict:
    cells = [
        (0, 0, 1, 3, "组合柜", False),
        (1, 0, 2, 1, "告警ID", False),
        (1, 1, 1, 1, "级别", False),
        (1, 2, 1, 1, "", False),
        (2, 1, 1, 1, "", True),
        (2, 2, 1, 1, '温度 ≥ 30℃\n路径 C:\\alarm\t"确认" & <标签>', False),
    ]
    return {
        "id": TABLE_ID,
        "row_count": 3,
        "column_count": 3,
        "confidence": "high",
        "unresolved_positions": [],
        "cells": [
            {
                "row": row,
                "column": column,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": text,
                "has_nontext_content": nontext,
            }
            for row, column, rowspan, colspan, text, nontext in cells
        ],
    }


def normalized_pdf_table() -> str:
    return _format_pdf_table_html(pdf_table_model())
