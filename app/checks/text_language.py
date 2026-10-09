import re
import unicodedata

from app.checks.text_content import DocumentBodyView, document_body_view

TEXT_LANGUAGE_CHINESE = "zh"
TEXT_LANGUAGE_MIXED = "mixed"
TEXT_LANGUAGE_LATIN = "latin"
TEXT_LANGUAGE_OTHER = "other"
TEXT_LANGUAGE_UNCERTAIN = "uncertain"
TEXT_LANGUAGE_UNKNOWN = "unknown"

_TEXT_LANGUAGE_LABELS = {
    TEXT_LANGUAGE_CHINESE: "中文为主",
    TEXT_LANGUAGE_MIXED: "中英混合",
    TEXT_LANGUAGE_LATIN: "拉丁字母为主（含英文）",
    TEXT_LANGUAGE_OTHER: "其他语种为主",
    TEXT_LANGUAGE_UNCERTAIN: "语种特征较少，需人工确认",
    TEXT_LANGUAGE_UNKNOWN: "未识别",
}

_ASCII_TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+#-]*")
_SHORT_UPPERCASE_TOKEN_RE = re.compile(r"\b[A-Z]{2,8}\b")
_UPPERCASE_PROSE_WORDS = {
    "A",
    "AN",
    "THE",
    "THIS",
    "THAT",
    "IS",
    "ARE",
    "AND",
    "OR",
    "OF",
    "FOR",
    "TO",
    "WITH",
    "IN",
    "ON",
    "DO",
    "NOT",
    "PLEASE",
    "USE",
}


def estimate_text_language(text: str | DocumentBodyView) -> str:
    body = text if isinstance(text, DocumentBodyView) else document_body_view(text)
    value = body.text
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", value))
    latin_chars = _latin_prose_character_count(value)
    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u31f0-\u31ff]", value))
    hangul_chars = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", value))
    other_letter_chars = sum(
        character.isalpha()
        and not _is_han(character)
        and not _is_latin(character)
        and not _is_japanese(character)
        and not _is_hangul(character)
        for character in value
    )
    return estimate_text_language_from_counts(
        cjk_chars,
        latin_chars,
        japanese_chars=japanese_chars,
        hangul_chars=hangul_chars,
        other_letter_chars=other_letter_chars,
    )


def estimate_text_language_from_counts(
    cjk_chars: int,
    latin_chars: int,
    *,
    japanese_chars: int = 0,
    hangul_chars: int = 0,
    other_letter_chars: int = 0,
) -> str:
    if (
        japanese_chars >= 10
        and japanese_chars / max(cjk_chars + japanese_chars, 1) >= 0.08
        and cjk_chars + japanese_chars >= max(20, latin_chars)
    ):
        return TEXT_LANGUAGE_OTHER
    if hangul_chars >= max(20, cjk_chars, latin_chars):
        return TEXT_LANGUAGE_OTHER
    if other_letter_chars >= max(40, cjk_chars, latin_chars):
        return TEXT_LANGUAGE_OTHER
    if (
        cjk_chars >= 40
        and latin_chars >= 80
        and min(cjk_chars, latin_chars) / max(cjk_chars, latin_chars) >= 0.2
    ):
        return TEXT_LANGUAGE_MIXED
    if cjk_chars >= max(20, latin_chars):
        return TEXT_LANGUAGE_CHINESE
    if latin_chars >= max(40, cjk_chars):
        return TEXT_LANGUAGE_LATIN
    if cjk_chars or latin_chars:
        return TEXT_LANGUAGE_UNCERTAIN
    return TEXT_LANGUAGE_UNKNOWN


def text_language_label(language: str) -> str:
    return _TEXT_LANGUAGE_LABELS.get(
        str(language or ""), _TEXT_LANGUAGE_LABELS[TEXT_LANGUAGE_UNKNOWN]
    )


def _is_han(character: str) -> bool:
    return "\u4e00" <= character <= "\u9fff"


def _is_latin(character: str) -> bool:
    return "LATIN" in unicodedata.name(character, "")


def _latin_prose_character_count(value: str) -> int:
    text_without_identifiers = _ASCII_TECHNICAL_TOKEN_RE.sub(
        lambda match: (
            "" if _is_alphanumeric_identifier(match.group(0)) else match.group(0)
        ),
        value,
    )
    if re.search(r"[a-z\u4e00-\u9fff]", text_without_identifiers):
        text_without_identifiers = "\n".join(
            _without_uppercase_abbreviations(line)
            for line in text_without_identifiers.splitlines()
        )
    return sum(_is_latin(character) for character in text_without_identifiers)


def _without_uppercase_abbreviations(line: str) -> str:
    words = re.findall(r"\b[A-Z]+\b", line)
    if len(words) >= 4 and sum(word in _UPPERCASE_PROSE_WORDS for word in words) >= 2:
        return line
    return _SHORT_UPPERCASE_TOKEN_RE.sub("", line)


def _is_alphanumeric_identifier(token: str) -> bool:
    return any(character.isdigit() for character in token) and any(
        character.isascii() and character.isalpha() for character in token
    )


def _is_japanese(character: str) -> bool:
    return ("\u3040" <= character <= "\u30ff") or ("\u31f0" <= character <= "\u31ff")


def _is_hangul(character: str) -> bool:
    return (
        "\u1100" <= character <= "\u11ff"
        or "\u3130" <= character <= "\u318f"
        or "\uac00" <= character <= "\ud7af"
    )
