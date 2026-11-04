"""将排版空白归一化后的引用映射回连续原文，保留数字和词语边界。"""

import re


def _cjk(character):
    return (
        "\u3400" <= character <= "\u9fff" or "\U00020000" <= character <= "\U0003134f"
    )


def quotation_text(text):
    """返回用于定位的文本及每个字符对应的原文区间。"""
    characters, spans = [], []
    for match in re.finditer(r"\s+|\S", text):
        value = match.group()
        if value.isspace():
            left = text[match.start() - 1] if match.start() else ""
            right = text[match.end()] if match.end() < len(text) else ""
            if not left or not right or _cjk(left) or _cjk(right):
                continue
            value = " "
        characters.append(value)
        spans.append((match.start(), match.end()))
    return "".join(characters), spans


def locate_quotation(text, quote):
    """仅接受逐字引用或唯一的排版空白匹配，返回原文而非改写后的引用。"""
    if not isinstance(quote, str) or not quote.strip():
        return None
    if quote in text:
        return quote
    normalized, spans = quotation_text(text)
    target, _ = quotation_text(quote)
    start = normalized.find(target) if target else -1
    if start < 0 or normalized.find(target, start + 1) >= 0:
        return None
    end = start + len(target)
    # 不能把两个数字、单词或表格单元格合并成另一个值。
    for position in (start, end):
        if 0 < position < len(normalized):
            left, right = normalized[position - 1 : position + 1]
            if (
                left.isascii()
                and right.isascii()
                and left.isalnum()
                and right.isalnum()
            ):
                return None
    return text[spans[start][0] : spans[end - 1][1]]
