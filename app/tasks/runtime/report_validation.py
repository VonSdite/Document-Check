"""校验本次模型报告的完整结构，保留完整文本报告的兼容入口。"""

import json
import re

from app.models.client import LLMIncompleteResponseError


def parse_check_report(
    content: str, *, allow_plain_text: bool = False, label: str = "本轮"
) -> dict | None:
    candidate = content.strip()
    fence = re.search(r"(?im)^\s*```json[ \t]*(?:\n|$)", candidate)
    if fence:
        candidate = candidate[fence.start() :].strip()
    json_fence = False
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        json_fence = lines[0].strip().lower() in {"```", "```json"}
        if json_fence and len(lines) > 1 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
        elif json_fence:
            raise _invalid_report(content, f"{label}未返回完整的结构化报告")
    if allow_plain_text and candidate and not json_fence:
        if not candidate.startswith(("{", "[")):
            report_start = re.search(r'\{\s*"(?:summary|items)"\s*:', candidate)
            if report_start is None:
                return None
            candidate = candidate[report_start.start() :]
    try:
        report = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise _invalid_report(content, f"{label}未返回完整的结构化报告") from exc
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("items"), list)
        or any(not isinstance(item, dict) for item in report["items"])
    ):
        raise _invalid_report(content, f"{label}报告缺少有效的问题列表")
    if any(
        not isinstance(item.get("description"), str)
        or not item["description"].strip()
        or item.get("status") not in {"issue", "suggestion"}
        for item in report["items"]
    ):
        raise _invalid_report(content, f"{label}报告的问题条目缺少描述或有效判定")
    return report


def _invalid_report(content: str, message: str) -> LLMIncompleteResponseError:
    return LLMIncompleteResponseError(
        f"模型输出不完整：{message}。已保留部分输出，可重试该检查项。", content
    )
