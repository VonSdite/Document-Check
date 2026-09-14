"""生成宽表和稀疏 PDF 表格，测量解析耗时与输出摘要。"""

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import fitz
from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_documents(root: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "参数表"
    for row in range(1, 201):
        sheet.append([row * column for column in range(1, 513)])
        for column in range(1, 11):
            sheet.cell(
                row, column
            ).hyperlink = f"https://example.invalid/{row}/{column}"
    workbook.save(root / "wide.xlsx")
    workbook.close()
    with fitz.open() as document:
        for _ in range(3):
            page = document.new_page(width=1000, height=1000)
            for row in range(31):
                page.draw_line((30, 30 + row * 28), (930, 30 + row * 28))
            for column in range(13):
                page.draw_line((30 + column * 75, 30), (30 + column * 75, 870))
            for row in range(30):
                for column in range(12):
                    if column % 3 == 0:
                        page.insert_text(
                            (35 + column * 75, 48 + row * 28),
                            f"R{row}C{column}",
                            fontsize=9,
                        )
        document.save(root / "tables.pdf")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from app.documents.extraction import extract_document

    results = {}
    with tempfile.TemporaryDirectory(
        prefix="documentcheck-doc-benchmark-"
    ) as directory:
        root = Path(directory)
        create_documents(root)
        for filename, file_type in (("wide.xlsx", "xlsx"), ("tables.pdf", "pdf")):
            started = time.perf_counter()
            text, links = extract_document(root / filename, file_type)
            elapsed = time.perf_counter() - started
            digest = hashlib.sha256(
                json.dumps([text, links], ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            results[filename] = {
                "seconds": round(elapsed, 4),
                "text_chars": len(text),
                "hyperlinks": len(links),
                "sha256": digest,
            }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
