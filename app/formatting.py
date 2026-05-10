import re

import mistune
from markupsafe import Markup


_markdown = mistune.create_markdown(
    escape=True,
    hard_wrap=True,
    plugins=["strikethrough", "table"],
)


def render_markdown(value) -> Markup:
    text = "" if value is None else str(value)
    text = _normalize_markdown(text)
    return Markup(_markdown(text))


def _normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\*\*([^*\n]*?\S)[ \t\u3000]+\*\*", r"**\1**", text)
