"""按用户与完整模型连接配置保存图片识别测试结果。"""

import base64
import hashlib
import json
import secrets

from app.contracts.task_types import IMAGE_TASK_TYPE, VIDEO_TASK_TYPE
from app.models.client import normalize_reasoning_effort
from app.models.endpoints import normalize_chat_completions_endpoint
from app.persistence.connection import get_db, now_text

IMAGE_INPUT_TASK_TYPES = frozenset({IMAGE_TASK_TYPE, VIDEO_TASK_TYPE})
IMAGE_INPUT_REQUIRED_MESSAGE = (
    "图片和视频检查需要能识别图片的模型。"
    "请到“模型管理”对所用模型点击“测试图片识别”，通过后再提交或重试。"
)


def image_model_signature(model) -> str:
    model = dict(model)
    values = [
        normalize_chat_completions_endpoint(model.get("api_base") or ""),
        model.get("api_key") or "",
        str(model.get("model_name") or "").strip(),
        bool(model.get("force_disable_thinking")),
        normalize_reasoning_effort(model.get("reasoning_effort")) or "",
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def image_capabilities(owner_subject: str) -> dict[str, str]:
    return {
        row["model_signature"]: "verified" if row["verified"] else "failed"
        for row in get_db().execute(
            "SELECT model_signature, verified FROM model_image_capabilities WHERE owner_subject=?",
            (owner_subject,),
        )
    }


def image_model_status(model, capabilities: dict[str, str]) -> str:
    try:
        return capabilities.get(image_model_signature(model), "unknown")
    except ValueError:
        return "unknown"


def record_image_capability(owner_subject: str, model: dict, *, verified: bool) -> None:
    get_db().execute(
        "INSERT INTO model_image_capabilities(owner_subject, model_signature, verified, checked_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(owner_subject, model_signature) DO UPDATE SET "
        "verified=excluded.verified, checked_at=excluded.checked_at",
        (owner_subject, image_model_signature(model), int(verified), now_text()),
    )
    get_db().commit()


def image_model_error(model, owner_subject: str) -> str:
    try:
        signature = image_model_signature(model)
    except ValueError:
        return IMAGE_INPUT_REQUIRED_MESSAGE
    row = (
        get_db()
        .execute(
            "SELECT verified FROM model_image_capabilities WHERE owner_subject=? AND model_signature=?",
            (owner_subject, signature),
        )
        .fetchone()
    )
    return "" if row and row["verified"] else IMAGE_INPUT_REQUIRED_MESSAGE


def image_task_model_error(task) -> str:
    task = dict(task)
    if task.get("task_type") not in IMAGE_INPUT_TASK_TYPES:
        return ""
    return image_model_error(task, task.get("owner_subject") or "")


def image_test_challenge() -> tuple[str, str]:
    """生成仅包含在图片像素中的随机数字，验证模型实际读取图片。"""
    import fitz

    expected = "".join(secrets.choice("0123456789") for _ in range(8))
    with fitz.open() as document:
        page = document.new_page(width=480, height=140)
        page.insert_text((48, 96), expected, fontname="cour", fontsize=72)
        png = page.get_pixmap(alpha=False).tobytes("png")
    return "data:image/png;base64," + base64.b64encode(png).decode(), expected
