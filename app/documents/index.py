"""共享文档来源、上下文和解析覆盖信息。"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from itertools import groupby

from app.documents.segmentation import split_source_text

INDEX_VERSION = "source-index-v3"
_PAGE = re.compile(r"^\[第(\d+)页\]$")
_TABLE = re.compile(
    r"\[PDF结构化表格 ([^\s；]+) 开始.*?\[PDF结构化表格 \1 结束\]", re.S
)


def compact_page_blocks(blocks: list[dict]) -> list[dict]:
    """同页同章节的连续正文合并，保留原文顺序和首尾行位置。"""
    result = []
    previous_page = None
    for original in blocks:
        block = dict(original)
        match = re.fullmatch(r"(第\d+页) / (第\d+行)", block.get("location", ""))
        page = match[1] if match and block.get("kind") == "paragraph" else None
        if (
            page
            and page == previous_page
            and result[-1].get("section") == block.get("section")
            and len(result[-1]["text"]) + len(block["text"]) + 1 <= 1600
        ):
            result[-1]["text"] += "\n" + block["text"]
            result[-1]["location"] = (
                result[-1]["location"].split("～")[0] + "～" + match[2]
            )
        else:
            result.append(block)
        previous_page = page
    return result


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

    @property
    def document_id(self):
        return self.id.rsplit("-b", 1)[0]


class DocumentIndex:
    def __init__(self, documents: list[dict]):
        self.blocks: list[SourceBlock] = []
        self.documents = []
        for doc_number, document in enumerate(documents, start=1):
            filename = str(document.get("filename") or f"文档{doc_number}")
            role = str(document.get("role") or "document")
            raw = compact_page_blocks(
                document.get("blocks") or text_blocks(str(document.get("text") or ""))
            )
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
                fragments = split_source_text(
                    text, str(block.get("kind") or "paragraph")
                )
                for fragment, content in enumerate(fragments, start=1):
                    self.blocks.append(
                        SourceBlock(
                            id=f"d{doc_number}-{identity}-b{block_number}-{fragment}",
                            filename=filename,
                            role=role,
                            kind=str(block.get("kind") or "paragraph"),
                            section=str(block.get("section") or ""),
                            location=str(block.get("location") or f"第{block_number}段")
                            + (f" / 片段{fragment}" if len(fragments) > 1 else ""),
                            text=content,
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
        self._index_context()
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
        index._index_context()
        return index

    @staticmethod
    def _table_key(block):
        if block.kind != "table":
            return None
        table = re.search(r"表格\s*([^ /；>]+)", block.location)
        return (block.document_id, table[1] if table else block.id.rsplit("-", 1)[0])

    def _index_context(self):
        self._sections, self._tables = {}, {}
        for _, group in groupby(self.blocks, lambda b: (b.document_id, b.section)):
            section = list(group)
            for block in section:
                self._sections[block.id] = section
        for _, group in groupby(self.blocks, self._table_key):
            table = list(group)
            if table[0].kind == "table":
                for block in table:
                    self._tables[block.id] = table

    def supporting_sources(self, source_ids):
        """提供同章节的适用条件、表格开头和相邻原文，目标来源独立计数。"""
        targets = {source_id for source_id in source_ids if source_id in self.by_id}
        selected = {}

        def add(block):
            if block.id not in targets:
                selected.setdefault(block.id, block.as_dict())

        for block in sorted((self.by_id[s] for s in targets), key=lambda b: b.ordinal):
            section = self._sections[block.id]
            if block.section:
                for source in section[:3]:
                    if source.kind == "table":
                        break
                    add(source)
            table = self._tables.get(block.id)
            if table:
                for source in table[:2]:
                    add(source)
                for ordinal in (table[0].ordinal - 1, table[-1].ordinal + 1):
                    if 0 <= ordinal < len(self.blocks):
                        source = self.blocks[ordinal]
                        if (
                            source.document_id == block.document_id
                            and source.section == block.section
                        ):
                            add(source)
        for block in sorted((self.by_id[s] for s in targets), key=lambda b: b.ordinal):
            for source in self.blocks[max(0, block.ordinal - 1) : block.ordinal + 2]:
                if (
                    source.document_id == block.document_id
                    and source.section == block.section
                ):
                    add(source)
        return list(selected.values())

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
            if b.document_id == block.document_id
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
        blocks = [b for b in self.blocks if b.role != "standard"]
        lengths = {
            b.id: len(json.dumps(b.as_dict(), ensure_ascii=False)) + 2 for b in blocks
        }
        if any(length > max_chars for length in lengths.values()):
            raise RuntimeError("单个来源片段超过检查上下文预算")
        units = []
        for _, group in groupby(blocks, lambda b: (b.document_id, b.section)):
            section = list(group)
            if sum(lengths[b.id] for b in section) <= max_chars:
                units.append(section)
                continue
            for _, group in groupby(
                section, lambda b: self._table_key(b) or b.id.rsplit("-", 1)[0]
            ):
                unit = list(group)
                if sum(lengths[b.id] for b in unit) <= max_chars:
                    units.append(unit)
                else:
                    units.extend([b] for b in unit)
        for unit in units:
            length = sum(lengths[b.id] for b in unit)
            if current and (
                size + length > max_chars
                or current[-1].document_id != unit[0].document_id
            ):
                chunks.append(current)
                current, size = [], 0
            current.extend(unit)
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
