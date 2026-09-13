from pathlib import Path

from flask import (
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.contracts.task_types import DOCUMENT_TASK_TYPE, VIDEO_TASK_TYPE
from app.reporting.constants import (
    REPORT_EXPORT_MIMETYPE,
    REPORT_IMPORT_MAX_BYTES,
    REPORT_ITEM_TYPES,
    ReportExcelImportError,
)
from app.reporting.excel import _load_report_excel_reviews, build_report_workbook
from app.reporting.service import (
    _report_item_fields_for_task,
    _report_item_totals,
    _task_document_groups,
    _task_results,
    _uses_compact_media_report,
    update_report_item_type,
)


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
        report_item_types=REPORT_ITEM_TYPES,
        report_item_fields=_report_item_fields_for_task(task["task_type"]),
        media_report=_uses_compact_media_report(task["task_type"]),
        video_report=(task["task_type"] or DOCUMENT_TASK_TYPE) == VIDEO_TASK_TYPE,
        video_stream_url="",
        document_groups=_task_document_groups(task),
        app_css=app_css,
        table_resize_js=table_resize_js,
    )
    filename = f"document-check-report-{task['id']}.html"
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _update_report_item_type(task):
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        data = request.form
    return update_report_item_type(task, data)


def _export_task_report_excel(task):
    return send_file(
        build_report_workbook(task),
        as_attachment=True,
        download_name=f"document-check-report-{task['id']}.xlsx",
        mimetype=REPORT_EXPORT_MIMETYPE,
    )
