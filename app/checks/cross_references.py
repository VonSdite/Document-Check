"""用检查范围内的图表题核对模型提出的编号引用缺失结论。"""

import re
import unicodedata

from app.checks.text_content import document_body_view

Reference = tuple[str, str]
CrossReferenceIndex = frozenset[Reference]

_SPACE = r"[ \t]*(?:\n[ \t]*)?"
_NUMBER = rf"(?:[A-Za-z]{_SPACE}[-.]{_SPACE})?\d+(?:{_SPACE}[-.]{_SPACE}\d+)*"
_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<kind>图|表|figure|fig|table|tab)"
    rf"\.?{_SPACE}(?P<number>{_NUMBER})(?![A-Za-z0-9]|{_SPACE}[-.]\s*\d)",
    re.I,
)
_CAPTION_PREFIX = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?\*{0,2}")
_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—−﹘﹣－"})
_TOC_HEADING = re.compile(
    r"(?mi)^\s*(?:#{1,6}\s*)?(?:目\s*录|(?:插图|图表|表格|图|表)目录|"
    r"(?:Table\s+of\s+)?Contents|List\s+of\s+(?:Figures|Tables))\s*$"
)
_TOC_ENTRY = re.compile(r"(?:\.{2,}|…+|[·•]{2,}|\t)\s*(?:\d+|[ivxlcdm]+)\s*$", re.I)
_REFERENCE_TAIL = re.compile(
    r"^(?:所示|可见|参见|参考|见|请|的|中|为|是|在|和|及|与|或|至|到|均|都|"
    r"显示|展示|说明了|介绍了|给出|用于|不存在|缺失|"
    r"(?:shows?|illustrates?|depicts?|is|are|was|were|and|or|to)\b)",
    re.I,
)
_NARRATIVE = re.compile(r"所示|参见|不存在|缺失|未找到|[。！？!?；;]|[|]")
_CLAIM_FIELDS = ("description", "issue", "problem", "finding", "问题描述")
_EVIDENCE_FIELDS = (
    "location",
    "位置",
    "excerpt",
    "quote",
    "original",
    "evidence",
    "原文摘录",
    "原文",
    "证据",
    "文档线索",
)
_MISSING = re.compile(
    r"缺失|缺少|遗漏|不存在|未定义|悬空|找不到|"
    r"未(?:能)?(?:找到|发现|提供|出现|给出|收录|见(?:到)?)|"
    r"未检索到|没有(?:找到|发现|提供|出现)|无(?:法找到|法定位|对应)|检索不到|"
    r"\b(?:missing|not\s+found|not\s+defined|does\s+not\s+exist|"
    r"cannot\s+be\s+(?:found|located))\b",
    re.I,
)
_REFERENCE_SUBJECT = re.compile(
    r"图号|图题|表号|表题|编号|引用|"
    r"(?:该|此|对应|上述|引用的|所引)[^，。；\n]{0,6}(?:图|表)|"
    r"\b(?:figure|table|reference|caption)\b",
    re.I,
)
_OTHER_PROBLEM = re.compile(
    r"不一致|不匹配|不符|错误|有误|重复|冲突|矛盾|歧义|模糊|不清|混淆|"
    r"错引|错位|跳号|漏标|颠倒|错别字|拼写|语法|不准确|不完整|"
    r"应为|应(?:指向|引用)|实际指向|"
    r"未(?:被)?引用|没有引用|\b(?:mismatch|incorrect|wrong|duplicate|inconsistent)\b",
    re.I,
)
_DETAIL = (
    r"(?:单位|字段|数据|参数|数值|步骤|说明|图例|标注|标记|表头|注释|"
    r"列名|行名|页码|内容|文字|\b(?:units?|fields?|data|values?|parameters?|"
    r"steps?|legends?|labels?|notes?|columns?|rows?|content)\b)"
)
_MISSING_DETAIL = re.compile(
    rf"{_DETAIL}[^，。；\n]{{0,20}}(?:{_MISSING.pattern}|未说明)|"
    rf"(?:缺失|缺少|未提供|未说明|\bmissing\b)[^，。；\n]{{0,20}}{_DETAIL}|"
    r"(?:标题|图题|表题)[^，。；\n]{0,8}(?:缺少|缺失|遗漏)编号",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"\bTODO\b|\bTBD\b|\bXXX\b|待补充|待插入|此处插入|后续提供|占位",
    re.I,
)
_UNCERTAIN_TARGET = re.compile(
    r"《|外部(?:文档|文件|资料)|其他(?:文档|文件)|另一(?:份|个)(?:文档|文件)|"
    r"\.(?:pdf|docx?)(?![A-Za-z0-9])",
    re.I,
)
_NEGATED = re.compile(r"并非|不是|不应|不得|不能|无法(?:确认|判断)|并不缺")
_ABBREVIATED_LIST = re.compile(
    r"^\s*(?:(?:、|,|和|与|及|至|到|[-~/])\s*[A-Za-z]?\d|"
    r"(?:至|到|[-~])\s*(?:图|表|figure|fig|table|tab))",
    re.I,
)
_SUBFIGURE = re.compile(r"^\s*\([A-Za-z0-9]+\)")
_REFERENCE_CONTEXT = re.compile(
    r"引用|参见|提及|提到|标注|\b(?:refer|references?|cites?)\b", re.I
)


def _normalized(text: str) -> str:
    return (
        unicodedata.normalize("NFKC", text)
        .translate(_DASHES)
        .replace("\u200b", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _key(match: re.Match) -> Reference:
    kind = match["kind"].lower()
    return (
        "figure" if kind == "图" or kind.startswith("fig") else "table",
        re.sub(r"\s+", "", match["number"]).upper(),
    )


def build_cross_reference_index(body_text: str) -> CrossReferenceIndex:
    """索引已限定章节范围的正文；只有独立图表题能证明目标存在。"""
    if len(re.findall(r"(?m)^file:", body_text)) > 1:
        return frozenset()
    # 表格单元格中的引用和目录条目不能充当图表定义。
    text = re.sub(
        r"<table\b[^>]*>.*?</table>",
        lambda match: (
            "\n"
            + "\n".join(
                re.findall(r"<caption\b[^>]*>(.*?)</caption>", match[0], re.I | re.S)
            )
            + "\n"
        ),
        body_text,
        flags=re.I | re.S,
    )
    definitions = set()
    for page in re.split(r"\[第\d+页\]", text):
        visible = _normalized(document_body_view(page).text)
        if _TOC_HEADING.search(visible):
            continue
        for prefix in _CAPTION_PREFIX.finditer(visible):
            match = _REFERENCE.match(visible, prefix.end())
            if not match:
                continue
            remainder = visible[match.end() : match.end() + 240]
            if _SUBFIGURE.match(remainder):
                continue
            # 图号与图题允许相邻两行，空行边界保留。
            title = re.match(r"[ \t*]*[:：、]?[ \t]*(?:\n[ \t]*)?([^\n]+)", remainder)
            if not title:
                continue
            value = title[1].strip(" *\r")
            if (
                not value
                or value.startswith((",", "、"))
                or len(value) > 160
                or not re.search(r"[A-Za-z\u3400-\u9fff]", value)
                or _TOC_ENTRY.search(value)
                or _REFERENCE_TAIL.search(value)
                or _NARRATIVE.search(value)
                or _PLACEHOLDER.search(value)
                or _REFERENCE.search(value)
            ):
                continue
            definitions.add(_key(match))
    return frozenset(definitions)


def _fields(item: dict, fields: tuple[str, ...]) -> str:
    return _normalized(
        "\n".join(str(item.get(field) or "") for field in fields)
    ).strip()


def missing_reference_targets(item: dict) -> CrossReferenceIndex:
    """识别明确编号的缺失主张；附带的其他问题由后续规则保留。"""
    claim = _fields(item, _CLAIM_FIELDS)
    if not _MISSING.search(claim):
        return frozenset()
    matches = list(_REFERENCE.finditer(claim))
    if not matches and _REFERENCE_SUBJECT.search(claim):
        matches = list(_REFERENCE.finditer(_fields(item, _EVIDENCE_FIELDS)))
    return frozenset(_key(match) for match in matches)


def is_disproved_missing_reference(item: dict, index: CrossReferenceIndex) -> bool:
    """仅在全部明确目标已存在、且条目仅涉及引用缺失时丢弃结论。"""
    if item.get("status", item.get("type")) in {"non_issue", "非问题"}:
        return False
    targets = missing_reference_targets(item)
    if not targets or not targets.issubset(index):
        return False
    claim = _fields(item, _CLAIM_FIELDS)
    evidence = _fields(item, _EVIDENCE_FIELDS)
    if (
        _OTHER_PROBLEM.search(claim)
        or _MISSING_DETAIL.search(claim)
        or _NEGATED.search(claim)
        or _PLACEHOLDER.search(claim + "\n" + evidence)
        or _UNCERTAIN_TARGET.search(claim + "\n" + evidence)
    ):
        return False
    for text in (claim, evidence):
        if any(
            _ABBREVIATED_LIST.match(text[match.end() :])
            or _SUBFIGURE.match(text[match.end() :])
            for match in _REFERENCE.finditer(text)
        ):
            return False
    # 每个分句都需明确描述缺失主张或引用上下文，其他陈述保留供复核。
    for clause in re.split(r"[，,。；;\n]", claim):
        if not clause.strip():
            continue
        if not (
            _MISSING.search(clause)
            and (_REFERENCE.search(clause) or _REFERENCE_SUBJECT.search(clause))
            or _REFERENCE.search(clause)
            and _REFERENCE_CONTEXT.search(clause)
        ):
            return False
    return True
