import re
from io import BytesIO
from pathlib import Path

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.reporting.constants import (
    REPORT_EXPORT_MIMETYPE,
    REPORT_IMPORT_MAX_BYTES,
    ReportExcelImportError,
)
from app.reporting.excel import _load_report_excel_reviews, build_report_workbook
from app.reporting.service import (
    _report_item_fields_for_task,
    _report_item_totals,
    _task_document_groups,
    _task_results,
    _uses_compact_media_report,
    update_report_item_review,
)
from app.tasks.submission import _consistency_task_title


def _report_download_name(task, extension: str) -> str:
    task_type = task["task_type"] or DOCUMENT_TASK_TYPE
    if task_type == CONSISTENCY_TASK_TYPE:
        name = _consistency_task_title(task)
    else:
        name = str(task["original_filename"] or "")
        if task_type != LANGUAGE_CONSISTENCY_TASK_TYPE:
            name = Path(name.replace("\\", "/")).stem
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", name).strip(" ._")
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" ._")
    return f"{name or '文档'}_报告_{task['id']}.{extension}"


def _import_task_report_excel(task, detail_endpoint: str):
    redirect_response = redirect(url_for(detail_endpoint, task_id=task["id"]))
    if task["status"] not in {"completed", "partial"}:
        flash("任务尚未完成，暂不能回填报告标注。", "error")
        return redirect_response

    upload = request.files.get("report_excel")
    filename = str(upload.filename or "").strip() if upload is not None else ""
    if upload is None or not filename:
        flash("请选择需要回填的 Excel 报告。", "error")
        return redirect_response
    if Path(filename).suffix.lower() != ".xlsx":
        flash("回填文件仅支持系统导出的 xlsx 格式报告。", "error")
        return redirect_response

    payload = upload.stream.read(REPORT_IMPORT_MAX_BYTES + 1)
    if len(payload) > REPORT_IMPORT_MAX_BYTES:
        flash("回填文件不能超过 10MB。", "error")
        return redirect_response

    try:
        imported_count = _load_report_excel_reviews(task, payload)
    except ReportExcelImportError as exc:
        flash(str(exc), "error")
        return redirect_response

    flash(f"已从 Excel 回填 {imported_count} 条报告标注。", "success")
    return redirect_response


def _export_task_report(task):
    static_folder = current_app.static_folder
    if not static_folder:
        raise RuntimeError("静态资源目录未配置，无法导出报告。")
    app_css = (Path(static_folder) / "app.css").read_text(encoding="utf-8")
    table_resize_js = (Path(static_folder) / "table-resize.js").read_text(
        encoding="utf-8"
    )
    results = _task_results(task)
    html = render_template(
        "task_report_export.html",
        task=task,
        results=results,
        report_totals=_report_item_totals(results),
        report_item_fields=_report_item_fields_for_task(task["task_type"]),
        media_report=_uses_compact_media_report(task["task_type"]),
        video_report=(task["task_type"] or DOCUMENT_TASK_TYPE) == VIDEO_TASK_TYPE,
        video_stream_url="",
        document_groups=_task_document_groups(task),
        app_css=app_css,
        table_resize_js=table_resize_js,
    )
    return send_file(
        BytesIO(html.encode("utf-8")),
        as_attachment=True,
        download_name=_report_download_name(task, "html"),
        mimetype="text/html",
    )


def _update_report_item_type(task):
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        data = request.form
    return update_report_item_review(task, data)


def _export_task_report_excel(task):
    return send_file(
        build_report_workbook(task),
        as_attachment=True,
        download_name=_report_download_name(task, "xlsx"),
        mimetype=REPORT_EXPORT_MIMETYPE,
    )
