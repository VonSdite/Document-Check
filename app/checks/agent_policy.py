"""检查项的内置执行策略。"""

from app.contracts.agent import AgentOptions
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
)

POLICY_KEY = "check_agent:"
DEFAULT_AGENT_PARALLELISM = 3
LOCAL_CODES = {"common-terms", "sensitive-terms", "hyperlink-validity"}
TEXT_TASKS = {DOCUMENT_TASK_TYPE, CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}


def check_agent_options(code: str, task_type: str) -> dict | None:
    if task_type not in TEXT_TASKS or code in LOCAL_CODES:
        return None
    return AgentOptions().model_dump(mode="json")


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
        "typo": "local",
        "understandability": "local",
    }.get(code, "auto")
