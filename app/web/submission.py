from flask import flash, redirect, request, url_for

from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.identity.models import UserIdentity
from app.tasks.submission import (
    SubmissionResult,
    TaskSubmission,
    submit_consistency_task,
    submit_document_task,
    submit_image_task,
    submit_language_consistency_task,
    submit_video_task,
)
from app.web.common import _task_endpoint


def _submission_input() -> TaskSubmission:
    return TaskSubmission(
        files={name: request.files.getlist(name) for name in request.files},
        check_ids=[
            int(value) for value in request.form.getlist("checks") if value.isdigit()
        ],
        model_id=request.form.get("model_id", ""),
        submission_token=request.form.get("submission_token", ""),
    )


def _submission_response(result: SubmissionResult, *, admin_created: bool):
    for message, category in result.messages:
        flash(message, category)
    return redirect(url_for(_task_list_endpoint(admin_created, result.task_type)))


def _task_list_endpoint(
    admin_created: bool, task_type: str | None = DOCUMENT_TASK_TYPE
) -> str:
    name = {
        CONSISTENCY_TASK_TYPE: "consistency",
        LANGUAGE_CONSISTENCY_TASK_TYPE: "language_consistency",
        IMAGE_TASK_TYPE: "images",
        VIDEO_TASK_TYPE: "videos",
    }.get(task_type, "tasks")
    return _task_endpoint(name) if admin_created else f"user_{name}"


def create_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    return _submission_response(
        submit_document_task(identity, _submission_input()),
        admin_created=admin_created,
    )


def create_image_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    return _submission_response(
        submit_image_task(identity, _submission_input()),
        admin_created=admin_created,
    )


def create_video_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    return _submission_response(
        submit_video_task(identity, _submission_input()),
        admin_created=admin_created,
    )


def create_consistency_task_for_identity(
    identity: UserIdentity, *, admin_created: bool
):
    return _submission_response(
        submit_consistency_task(identity, _submission_input()),
        admin_created=admin_created,
    )


def create_language_consistency_task_for_identity(
    identity: UserIdentity, *, admin_created: bool
):
    return _submission_response(
        submit_language_consistency_task(identity, _submission_input()),
        admin_created=admin_created,
    )
