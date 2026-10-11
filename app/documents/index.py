"""共享文档来源、上下文和解析覆盖信息。"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

INDEX_VERSION = "source-index-v1"
_PAGE = re.compile(r"^\[第(\d+)页\]$")
_TABLE = re.compile(
    r"\[PDF结构化表格 ([^\s；]+) 开始.*?\[PDF结构化表格 \1 结束\]", re.S
)


@dataclass(frozen=True)
class SourceBlock:
    id: str
    filename: str
    role: str
    kind: str
    section: str
    location: str
    text: str
    ordinal: int

    def as_dict(self):
        return asdict(self)


class DocumentIndex:
    def __init__(self, documents: list[dict]):
        self.blocks: list[SourceBlock] = []
        self.documents = []
        for doc_number, document in enumerate(documents, start=1):
            filename = str(document.get("filename") or f"文档{doc_number}")
            role = str(document.get("role") or "document")
            raw = document.get("blocks") or text_blocks(str(document.get("text") or ""))
            identity = hashlib.sha256(
                json.dumps(
                    [INDEX_VERSION, doc_number, filename, role, raw],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:16]
            for block_number, block in enumerate(raw, start=1):
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                for fragment, offset in enumerate(range(0, len(text), 4000), start=1):
                    self.blocks.append(
                        SourceBlock(
                            id=f"d{doc_number}-{identity}-b{block_number}-{fragment}",
                            filename=filename,
                            role=role,
                            kind=str(block.get("kind") or "paragraph"),
                            section=str(block.get("section") or ""),
                            location=str(block.get("location") or f"第{block_number}段")
                            + (f" / 片段{fragment}" if len(text) > 4000 else ""),
                            text=text[offset : offset + 4000],
                            ordinal=len(self.blocks),
                        )
                    )
            self.documents.append(
                {
                    "filename": filename,
                    "role": role,
                    "text_complete": bool(document.get("text_complete", False)),
                    "limitations": list(document.get("limitations") or []),
                }
            )
        self.by_id = {block.id: block for block in self.blocks}
        self.signature = hashlib.sha256(
            json.dumps(
                [INDEX_VERSION, self.documents, [b.as_dict() for b in self.blocks]],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_task(cls, text: str, meta: dict, filename: str):
        documents = meta.get("source_documents")
        if not isinstance(documents, list) or not documents:
            documents = [
                {
                    "filename": filename,
                    "text": text,
                    "text_complete": False,
                    "limitations": ["解析覆盖信息未提供"],
                }
            ]
        return cls(documents)

    def with_standards(self, standards: list[dict]):
        index = object.__new__(DocumentIndex)
        index.blocks = list(self.blocks)
        index.by_id = dict(self.by_id)
        index.documents = self.documents
        index.signature = self.signature
        documents = [
            {
                "filename": s["name"],
                "role": "standard",
                "text": s["text"],
                "text_complete": True,
                "blocks": [
                    {
                        "text": s["text"],
                        "kind": "standard",
                        "location": f"{s['name']} / 版本 {s['version']}",
                        "section": s["version"],
                    }
                ],
            }
            for s in standards
        ]
        extra = DocumentIndex(documents)
        for b in extra.blocks:
            block = SourceBlock(
                **{**b.as_dict(), "id": "std-" + b.id, "ordinal": len(index.blocks)}
            )
            index.blocks.append(block)
            index.by_id[block.id] = block
        return index

    def verify(self, reference: dict, *, allowed: set[str] | None = None) -> bool:
        source_id = reference.get("source_id")
        block = self.by_id.get(source_id)
        quote = reference.get("quote")
        return bool(
            block
            and (allowed is None or source_id in allowed)
            and isinstance(quote, str)
            and quote.strip()
            and quote in block.text
        )

    def context(self, source_id: str, radius: int = 1):
        block = self.by_id.get(source_id)
        if block is None:
            raise ValueError("来源标识不存在")
        return [
            b.as_dict()
            for b in self.blocks[
                max(0, block.ordinal - radius) : block.ordinal + radius + 1
            ]
            if b.filename == block.filename and b.role == block.role
        ]

    def search(self, query: str, limit: int = 8):
        terms = [t for t in re.findall(r"[\w.-]+", query.lower()) if t]
        for word in re.findall(r"[\u4e00-\u9fff]{3,}", query):
            terms.extend(word[i : i + 2] for i in range(len(word) - 1))
        terms = list(dict.fromkeys(terms))
        if not terms:
            return []
        ranked = []
        for block in self.blocks:
            content = (block.section + " " + block.text).lower()
            score = sum(content.count(t) for t in terms)
            if score:
                ranked.append((score, block.ordinal, block))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2].as_dict() for entry in ranked[:limit]]

    def chunks(self, max_chars: int):
        chunks, current, size = [], [], 0
        for block in self.blocks:
            if block.role == "standard":
                continue
            length = len(json.dumps(block.as_dict(), ensure_ascii=False)) + 2
            if length > max_chars:
                raise RuntimeError("单个来源片段超过检查上下文预算")
            if current and (
                size + length > max_chars or current[-1].filename != block.filename
            ):
                chunks.append(current)
                current, size = [], 0
            current.append(block)
            size += length
        if current:
            chunks.append(current)
        return chunks

    def coverage(self, checked_ids: list[str]):
        ids = {b.id for b in self.blocks if b.role != "standard"}
        checked = ids & set(checked_ids)
        return {
            "total_blocks": len(ids),
            "checked_blocks": len(checked),
            "all_checked": bool(ids) and checked == ids,
            "text_complete": all(d["text_complete"] for d in self.documents),
            "documents": self.documents,
        }


def text_blocks(text: str) -> list[dict]:
    """文本按原始顺序保留章节、页码、行号和完整表格片段。"""
    blocks = []
    section, page = "", ""
    table_by_start = {match.start(): match for match in _TABLE.finditer(text)}
    offset, line_number = 0, 1
    lines = text.splitlines(keepends=True)
    position = 0
    while position < len(lines):
        line = lines[position]
        table = table_by_start.get(offset)
        if table:
            table_text = table.group(0)
            consumed = len(table_text.splitlines())
            blocks.append(
                {
                    "text": table_text,
                    "kind": "table",
                    "section": section,
                    "location": f"{page} / 表格 {table.group(1)}".strip(" / "),
                }
            )
            skipped = lines[position : position + consumed]
            offset += sum(map(len, skipped))
            line_number += len(skipped)
            position += len(skipped)
            continue
        value = line.strip()
        if match := _PAGE.fullmatch(value):
            page = f"第{match.group(1)}页"
        elif value:
            heading = bool(
                re.match(
                    r"^#{1,6}\s+|^第[一二三四五六七八九十\d]+[章节]|^\d+(?:\.\d+){0,3}\s+\S",
                    value,
                )
            )
            if heading:
                section = value.lstrip("# ")
            blocks.append(
                {
                    "text": value,
                    "kind": "heading" if heading else "paragraph",
                    "section": section,
                    "location": f"{page} / 第{line_number}行".strip(" / "),
                }
            )
        offset += len(line)
        position += 1
        line_number += 1
    return blocks
