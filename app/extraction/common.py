import re


class DocumentReadError(Exception):
    pass


def _format_hyperlink_text(label, target) -> str:
    label = str(label or "").strip()
    target = _clean_hyperlink_target(target)
    if not target:
        return label
    if label == target:
        return label
    if not label:
        return f"超链接：{target}"
    return f"{label}（超链接：{target}）"


def _clean_hyperlink_target(target) -> str:
    if isinstance(target, bytes):
        target = target.decode("utf-8", errors="replace")
    value = re.sub(r"[\r\n\t]+", " ", str(target or "")).strip()
    return value[:4096]


def _record_hyperlink(
    hyperlinks: list[dict] | None,
    display_text,
    target,
    location: str,
    *,
    internal_target_exists: bool | None = None,
) -> None:
    if hyperlinks is None:
        return
    item = {
        "display_text": str(display_text or "").strip(),
        "target": _clean_hyperlink_target(target),
        "location": str(location or "").strip(),
    }
    if isinstance(internal_target_exists, bool):
        item["internal_target_exists"] = internal_target_exists
    hyperlinks.append(item)
