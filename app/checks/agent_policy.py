"""检查策略与配置快照。"""

from app.contracts.agent import AgentOptions
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
)
from app.persistence.settings import get_setting

POLICY_KEY = "check_agent:"
LOCAL_CODES = {"common-terms", "sensitive-terms", "hyperlink-validity"}
TEXT_TASKS = {DOCUMENT_TASK_TYPE, CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}


def check_agent_options(code: str, task_type: str) -> dict | None:
    if task_type not in TEXT_TASKS or code in LOCAL_CODES:
        return None
    return AgentOptions.model_validate(get_setting(POLICY_KEY + code, {})).model_dump(
        mode="json"
    )


def strategy_for(code: str, task_type: str, configured: str) -> str:
    if configured != "auto":
        return configured
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return "cross_language"
    if task_type == CONSISTENCY_TASK_TYPE:
        return "cross_document"
    return {
        "consistency": "consistency",
        "completeness": "completeness",
        "compliance": "compliance",
    }.get(code, "local")


def agent_options_from_form(form, current: dict) -> dict:
    values = dict(current)
    values.update(
        enabled=form.get("agent_enabled") == "on",
        strategy=form.get("agent_strategy", "auto"),
        protocol=form.get("agent_protocol", "auto"),
    )
    for key in (
        "chunk_chars",
        "max_calls",
        "max_tokens",
        "max_seconds",
        "max_output_tokens",
        "max_review_rounds",
        "review_batch_size",
        "max_candidates",
    ):
        values[key] = int(form.get("agent_" + key, values[key]))
    for key in ("input_price", "output_price"):
        raw = form.get("agent_" + key, "").strip()
        values[key] = float(raw) if raw else None
    values["currency"] = form.get("agent_currency", "CNY").strip()
    standards = []
    names, versions, texts = (
        form.getlist("standard_" + key) for key in ("name", "version", "text")
    )
    if not len(names) == len(versions) == len(texts):
        raise ValueError("参考标准的名称、版本和内容需要完整提供")
    for name, version, text in zip(names, versions, texts):
        if any(value.strip() for value in (name, version, text)):
            if not all(value.strip() for value in (name, version, text)):
                raise ValueError("每条参考标准都需要名称、版本和原文")
            standards.append(
                {"name": name.strip(), "version": version.strip(), "text": text.strip()}
            )
    values["standards"] = standards
    return AgentOptions.model_validate(values).model_dump(mode="json")
