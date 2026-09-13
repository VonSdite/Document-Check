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
    if task_type == CONSISTENCY_TASK_TYPE:
        return "admin_consistency" if admin_created else "user_consistency"
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return (
            "admin_language_consistency"
            if admin_created
            else "user_language_consistency"
        )
    if task_type == IMAGE_TASK_TYPE:
        return "admin_images" if admin_created else "user_images"
    if task_type == VIDEO_TASK_TYPE:
        return "admin_videos" if admin_created else "user_videos"
    return "admin_tasks" if admin_created else "user_tasks"


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
