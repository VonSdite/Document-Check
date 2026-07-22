import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from .db import get_bool_setting, get_db, get_setting, now_text
from .documents import DocumentReadError, extract_text, format_document_text
from .file_cleanup import (
    describe_failures,
    remove_empty_directory as cleanup_remove_empty_directory,
    remove_file,
)
from .images import (
    default_image_folder,
    image_items_from_meta,
    image_path_from_item,
    image_to_data_url,
    page_numbers_from_image_item,
    page_sections_from_document_text,
)
from .llm import DEFAULT_ISSUE_OUTPUT_LIMIT, LLMError, run_check, run_multimodal_document_check
from .long_documents import (
    LONG_DOCUMENT_FACT_LIMIT_PER_CHUNK,
    align_language_chunks,
    build_consistency_candidates,
    build_chunk_search_index,
    build_document_outline,
    build_evidence_comparison_input,
    build_language_alignment_input,
    consistency_candidate_batches,
    extract_consistency_facts,
    grouped_document_limits,
    long_document_limits,
    merge_chunk_reports,
    parse_consistency_documents,
    parse_language_consistency_documents,
    rank_relevant_chunks,
    split_grouped_document,
    split_long_document,
)
from .network import outbound_network_config
from .sensitive_terms import (
    SENSITIVE_TERMS_CHECK_CODE,
    build_sensitive_terms_invalid_report,
    build_sensitive_terms_missing_report,
    build_sensitive_terms_report,
    find_sensitive_terms_file,
    format_sensitive_terms_report,
    load_sensitive_terms,
    sensitive_terms_file_candidates,
)
from .task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
)


class TaskCanceled(Exception):
    pass


class TaskArtifactCleanupError(RuntimeError):
    pass


DEFAULT_CHECK_ITEM_CONCURRENCY = 1
DEFAULT_IMAGE_CHECK_BATCH_SIZE = 4
MAX_IMAGE_CHECK_BATCH_SIZE = 4
IMAGE_CONTEXT_NEIGHBOR_PAGES = 1
IMAGE_DOCUMENT_CONTEXT_MAX_CHARS = 20000
DEFAULT_REPORT_RETENTION_DAYS = 0
REPORT_CLEANUP_INTERVAL_SECONDS = 3600
REPORT_CLEANUP_BATCH_SIZE = 100
IMAGE_PAGE_CHECK_CODES = {
    "image-text-correspondence",
    "image-ui-step-consistency",
    "image-figure-table-title-standard",
    "image-integrity-clarity",
}
IMAGE_RESOURCE_CHECK_CODES = {
    "image-small-language-text",
    "image-device-installation",
    "image-wiring",
    "image-drawing-standard",
}
IMAGE_CHECK_TARGET_LABELS = {
    "page": "页面级检查",
    "resource": "图片资源检查",
}
LONG_DOCUMENT_CONSISTENCY_CODE = "consistency"
LONG_DOCUMENT_COMPLIANCE_CODE = "compliance"
LONG_DOCUMENT_FACT_EXTRACTION_PROMPT = """你正在为超长技术文档建立全文一致性事实索引，不要在本次请求中判断是否存在问题。
请从当前分段提取适合跨章节比较的明确事实，包括但不限于：产品和部件属性、型号版本、技术参数、数值单位、范围阈值、操作要求、禁止事项、前置条件、安全等级、步骤顺序、术语定义和引用关系。

输出映射要求：
1. 每个事实输出为一个 items 元素，status 固定为 non_issue，severity 固定为 low。
2. category 填规范化后的对象或实体名称，例如“设备输入电压”。同一对象在不同分段必须尽量使用相同名称。
3. description 填属性或要求类型，例如“额定值”“允许范围”“强制要求”。
4. impact 填规范化值，必须保留数字、单位、否定词和强制程度，例如“220 V”“禁止带电插拔”“必须接地”。
5. suggestion 填适用条件、场景或例外；没有则填空字符串。
6. location 和 excerpt 必须引用当前分段中的明确位置和原文。
7. 不提取纯叙述、版权声明或无法跨位置比较的内容；单个分段最多提取最重要的 120 条事实。"""
LONG_DOCUMENT_CONSISTENCY_VERIFY_PROMPT = """下面内容不是完整文档，而是从全文事实索引中筛选出的潜在冲突证据对。
请逐个候选核对两处原文、适用条件、单位和强制程度：只有在同一对象、同一属性、适用条件相同或重叠且表述确实不兼容时才报告为 issue；不同型号、不同场景、不同阶段或单位可等价换算时不要误报。
每条确认的问题必须同时引用证据A和证据B的位置及原文。证据不足时填 suggestion，并明确需要确认的条件。不要报告候选列表之外的问题。"""
LONG_MULTI_DOCUMENT_FORWARD_PROMPT = """本次是超长多文档对照中的“资料分段 -> 素材证据”批次。
当前资料分段是主要检查对象，后面只提供系统检索到的最相关素材分段。请检查资料表述是否与这些素材证据冲突、偏离或缺少依据；只报告当前批次有明确双边证据的问题。
不要因为本批次没有展示全部素材就断言素材中不存在依据，也不要在这个方向判断素材要求是否被资料全文遗漏。每条结果必须同时引用资料位置和素材证据位置。"""
LONG_MULTI_DOCUMENT_REVERSE_PROMPT = """本次是超长多文档对照中的“素材分段 -> 资料覆盖”反向批次，用于发现资料全文可能遗漏的关键素材要求。
请核对当前素材分段中的关键事实、约束、步骤或风险提示，是否在检索到的资料证据中得到实质覆盖。只有当前素材内容明确重要且候选资料确实未覆盖时才报告；检索候选有限，证据不足时必须标为 suggestion/需人工确认，不能武断认定全文遗漏。
每条结果必须引用素材位置，并说明已核对的资料证据位置。"""
LONG_LANGUAGE_PAIRED_PROMPT = """本次是超长跨语种文档的一组对齐分段。系统按章节编号、数字/型号/单位等硬线索和相对顺序完成对齐。
请只比较当前文档A、文档B分段中的实质内容，识别缺失、增补、误译、约束强弱变化和关键事实差异。每条结果必须同时引用两侧位置和原文；不要根据未提供的其他分段推测全文差异。"""
LONG_LANGUAGE_COVERAGE_PROMPT = """本次是超长跨语种文档的未匹配分段覆盖复核。unmatched_side 指出系统未找到可靠对齐的一侧，另一侧内容只是检索到的邻近或相关候选。
请判断未匹配分段是否确实在另一文档中缺失，还是仅因章节调整、合并拆分或合理本地化而未直接对齐。只有证据充分时才报告 issue；证据不足时标为 suggestion/需人工确认。"""


class TaskScheduler:
    def __init__(self, app):
        self.app = app
        self._stop_event = threading.Event()
        self._launcher = threading.Thread(target=self._loop, daemon=True, name="task-launcher")
        self._last_report_cleanup = 0.0

    def start(self):
        self._launcher.start()

    def stop(self):
        self._stop_event.set()
        self._launcher.join(timeout=3)

    def _loop(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    progress = 0,
                    result_json = NULL,
                    summary = NULL,
                    error = NULL,
                    updated_at = ?,
                    started_at = NULL
                WHERE status = 'running'
                """,
                (now_text(),),
            )
            db.commit()

        while not self._stop_event.is_set():
            try:
                with self.app.app_context():
                    self._cleanup_reports_if_due()
                    self._launch_available_tasks()
            except Exception:
                self.app.logger.exception("任务调度循环异常")
            self._stop_event.wait(2)

    def _cleanup_reports_if_due(self):
        now = time.monotonic()
        if now - self._last_report_cleanup < REPORT_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_report_cleanup = now
        cleanup_expired_task_reports(self.app)

    def _launch_available_tasks(self):
        db = get_db()
        global_limit = max(1, _int_setting("global_concurrency", 3))
        user_limit = max(1, _int_setting("user_concurrency", 1))

        running_total = db.execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'"
        ).fetchone()["total"]
        slots = global_limit - running_total
        if slots <= 0:
            return

        queued = db.execute(
            """
            SELECT id, ip, COALESCE(owner_subject, 'ip:' || ip) AS owner_subject
            FROM tasks
            WHERE status = 'queued'
            ORDER BY created_at ASC, id ASC
            LIMIT 50
            """
        ).fetchall()

        launched = 0
        for task in queued:
            if launched >= slots:
                break
            running_for_user = db.execute(
                """
                SELECT COUNT(*) AS total
                FROM tasks
                WHERE status = 'running' AND COALESCE(owner_subject, 'ip:' || ip) = ?
                """,
                (task["owner_subject"],),
            ).fetchone()["total"]
            if running_for_user >= user_limit:
                continue

            db.execute(
                """
                UPDATE tasks
                SET status = 'running', progress = 1, started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_text(), now_text(), task["id"]),
            )
            db.commit()
            worker = threading.Thread(
                target=self._run_task,
                args=(task["id"],),
                daemon=True,
                name=f"task-worker-{task['id']}",
            )
            worker.start()
            launched += 1

    def _run_task(self, task_id: int):
        with self.app.app_context():
            db = get_db()
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return

            results = []
            try:
                self.app.logger.info(
                    "任务开始 task_id=%s owner=%s ip=%s file=%s model=%s/%s",
                    task_id,
                    task["owner_subject"] if "owner_subject" in task.keys() and task["owner_subject"] else f"ip:{task['ip']}",
                    task["ip"],
                    task["original_filename"],
                    task["provider_name"],
                    task["model_name"],
                )
                if task["cancel_requested"]:
                    _mark_canceled(db, task_id)
                    return

                task_type = task["task_type"] or DOCUMENT_TASK_TYPE
                max_workers = max(
                    1,
                    _int_setting("check_item_concurrency", DEFAULT_CHECK_ITEM_CONCURRENCY),
                )
                if task_type == IMAGE_TASK_TYPE:
                    document_meta_raw = _task_value(task, "document_meta_json")
                    image_items = image_items_from_meta(document_meta_raw)
                    page_image_items = image_items_from_meta(document_meta_raw, "page_images")
                    if not image_items and not page_image_items:
                        raise RuntimeError("未能从 PDF 中生成可检查页面截图或提取到可检查图片")
                    document_text = _task_value(task, "document_text") or ""
                    if not document_text:
                        document_text = (
                            f"file: {task['original_filename']}\n\n"
                            f"extracted_images: {len(image_items)}\n"
                            f"page_screenshots: {len(page_image_items)}"
                        )
                    check_items = _task_check_items(db, task, IMAGE_TASK_TYPE)
                    if not check_items:
                        raise RuntimeError("没有可执行的图片检查项")
                    results = _run_image_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        image_items,
                        page_image_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                    )
                elif task_type == VIDEO_TASK_TYPE:
                    document_meta_raw = _task_value(task, "document_meta_json")
                    frame_items = image_items_from_meta(document_meta_raw, "frames")
                    if not frame_items:
                        raise RuntimeError("未能从视频中抽取到可检查画面")
                    document_text = _task_value(task, "document_text") or ""
                    if not document_text:
                        document_text = (
                            f"file: {task['original_filename']}\n\n"
                            f"video_frames: {len(frame_items)}"
                        )
                    check_items = _task_check_items(db, task, VIDEO_TASK_TYPE)
                    if not check_items:
                        raise RuntimeError("没有可执行的视频检查项")
                    results = _run_video_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        frame_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                    )
                else:
                    if task_type in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
                        document_text = _task_value(task, "document_text") or _extract_consistency_document_text(self.app, task)
                        check_items = _task_check_items(db, task, task_type)
                    else:
                        document_text = _task_value(task, "document_text")
                        if not document_text:
                            upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
                            document_text = format_document_text(
                                task["original_filename"],
                                extract_text(upload_path, task["file_type"]),
                            )
                        check_items = _document_check_items(db, task)

                    if not document_text:
                        raise RuntimeError("未能从文档中提取到可检查文本")
                    if not check_items:
                        raise RuntimeError("没有可执行的检查项")

                    results = _run_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        document_text,
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                    )
                if _cancel_requested(db, task_id):
                    raise TaskCanceled

                summary = _build_summary(results)
                db.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        progress = 100,
                        result_json = ?,
                        summary = ?,
                        error = NULL,
                        updated_at = ?,
                        finished_at = ?
                    WHERE id = ? AND status = 'running' AND cancel_requested = 0
                    """,
                    (json.dumps(results, ensure_ascii=False), summary, now_text(), now_text(), task_id),
                )
                db.commit()
                self.app.logger.info("任务完成 task_id=%s checks=%s", task_id, len(results))
            except TaskCanceled:
                self.app.logger.info("任务取消 task_id=%s", task_id)
                _mark_canceled(db, task_id)
            except (DocumentReadError, LLMError, RuntimeError) as exc:
                self.app.logger.warning("任务失败 task_id=%s error=%s", task_id, exc)
                _mark_failed(db, task_id, str(exc), results)
            except Exception as exc:
                self.app.logger.exception("任务执行异常：%s", task_id)
                _mark_failed(db, task_id, f"任务执行异常：{exc}", results)


def _extract_consistency_document_text(app, task) -> str:
    groups = document_groups_from_meta(task["document_meta_json"])
    if not groups:
        raise RuntimeError("多文档对照检查缺少文档组信息")

    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    sections = []
    for group in groups:
        label = group["label"]
        group_parts = [f"# {label}"]
        for index, file_info in enumerate(group["files"], start=1):
            stored_filename = Path(str(file_info.get("stored_filename") or "")).name
            file_type = str(file_info.get("file_type") or "").lower()
            original_filename = str(file_info.get("original_filename") or stored_filename or f"文档{index}")
            if not stored_filename or not file_type:
                raise RuntimeError(f"{label}第 {index} 个文档信息不完整")
            upload_path = upload_folder / stored_filename
            if not upload_path.is_file():
                raise RuntimeError(f"{label}“{original_filename}”已删除，无法检查")
            text = extract_text(upload_path, file_type).strip()
            if not text:
                raise RuntimeError(f"{label}“{original_filename}”未能提取到可检查文本")
            group_parts.append(f"## {label}{index}：{original_filename}\n{text}")
        sections.append("\n\n".join(group_parts))
    return "\n\n".join(sections).strip()


def _document_check_items(db, task) -> list[dict]:
    return _task_check_items(db, task, DOCUMENT_TASK_TYPE)


def _task_check_items(db, task, task_type: str) -> list[dict]:
    snapshot = _check_items_from_snapshot(_task_value(task, "checks_snapshot_json"))
    if snapshot:
        return snapshot

    try:
        check_values = json.loads(task["checks_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("检查项数据无效") from exc
    if not isinstance(check_values, list) or not check_values:
        return []

    check_ids = [int(value) for value in check_values if isinstance(value, int)]
    check_codes = [
        str(value).strip()
        for value in check_values
        if isinstance(value, str) and str(value).strip()
    ]
    clauses = []
    params = []
    if check_ids:
        clauses.append(f"id IN ({','.join('?' for _ in check_ids)})")
        params.extend(check_ids)
    if check_codes:
        clauses.append(f"code IN ({','.join('?' for _ in check_codes)})")
        params.extend(check_codes)
    if not clauses:
        return []
    params.append(task_type)
    return [
        dict(row)
        for row in db.execute(
            f"""
            SELECT *
            FROM check_items
            WHERE ({' OR '.join(clauses)}) AND task_type = ? AND enabled = 1
            ORDER BY sort_order ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
    ]


def _check_items_from_snapshot(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []

    items = []
    seen_codes = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not code or not name or not prompt or code in seen_codes:
            continue
        seen_codes.add(code)
        items.append({"code": code, "name": name, "prompt": prompt})
    return items


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _document_meta(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _long_document_execution_plan(document_text: str, max_input_chars: int) -> tuple[list, str, int]:
    direct_limit, chunk_limit = long_document_limits(max_input_chars)
    if len(document_text) <= direct_limit:
        return [], "", chunk_limit
    chunks = split_long_document(document_text, max_chars=chunk_limit)
    outline_limit = max(800, min(8_000, chunk_limit // 5))
    return chunks, build_document_outline(document_text, max_chars=outline_limit), chunk_limit


def _long_document_chunk_input(chunk, outline: str) -> str:
    if not outline:
        return chunk.text
    return (
        "# 全文结构线索\n"
        f"{outline}\n\n"
        "说明：全文结构线索只用于定位和理解章节关系，具体问题必须以当前分段原文为证据。\n\n"
        f"# 当前检查分段\n{chunk.text}"
    )


def _long_document_chunk_issue_limit(issue_output_limit: int) -> int:
    limit = max(0, int(issue_output_limit or 0))
    if limit <= 0:
        return 10
    return max(3, min(10, limit))


def _run_long_document_chunk_check(
    *,
    task,
    item: dict,
    chunks: list,
    outline: str,
    issue_output_limit: int,
    save_partial,
    ensure_active,
    stream_trace_enabled: bool,
) -> str:
    reports = []
    network = outbound_network_config()
    chunk_limit = _long_document_chunk_issue_limit(issue_output_limit)
    for chunk in chunks:
        ensure_active()
        content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"{item['name']}（长文档分段 {chunk.index}/{chunk.total}）",
            prompt=(
                f"{item['prompt']}\n\n"
                "长文档分段规则：只报告当前分段内有明确证据的问题；不要因为缺少其他分段内容而断言全文缺失。"
            ),
            document_text=_long_document_chunk_input(chunk, outline),
            issue_output_limit=chunk_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        reports.append((chunk.label, content))
        merged = merge_chunk_reports(
            reports,
            issue_output_limit=issue_output_limit,
            summary_prefix=f"{item['name']}长文档分段检查进行中",
        )
        save_partial(merged, f"正在检查长文档：{item['name']}，分段 {chunk.index}/{chunk.total}", force=True)

    if item["code"] == LONG_DOCUMENT_COMPLIANCE_CODE and outline:
        ensure_active()
        outline_content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"{item['name']}（全文结构检查）",
            prompt=(
                f"{item['prompt']}\n\n"
                "本次只检查全文结构线索中可直接确认的章节编号、层级、重复标题和明显引用规范问题；"
                "不要根据结构线索推测正文内容缺失。"
            ),
            document_text=f"file: 长文档\n\n# 全文结构线索\n{outline}",
            issue_output_limit=chunk_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        reports.append(("全文结构线索", outline_content))

    return merge_chunk_reports(
        reports,
        issue_output_limit=issue_output_limit,
        summary_prefix=f"{item['name']}长文档分段检查完成",
    )


def _run_long_document_consistency_check(
    *,
    task,
    item: dict,
    chunks: list,
    outline: str,
    chunk_chars: int,
    issue_output_limit: int,
    save_partial,
    ensure_active,
    stream_trace_enabled: bool,
) -> str:
    reports = []
    facts = []
    fact_failures = []
    network = outbound_network_config()
    chunk_limit = _long_document_chunk_issue_limit(issue_output_limit)
    for chunk in chunks:
        ensure_active()
        chunk_text = _long_document_chunk_input(chunk, outline)
        local_content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"{item['name']}（分段内部检查 {chunk.index}/{chunk.total}）",
            prompt=(
                f"{item['prompt']}\n\n"
                "本次先检查当前分段内部可直接对照的矛盾；不要因为其他分段未提供而判断全文缺失。"
            ),
            document_text=chunk_text,
            issue_output_limit=chunk_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        reports.append((f"{chunk.label}内部检查", local_content))
        save_partial(
            merge_chunk_reports(
                reports,
                issue_output_limit=issue_output_limit,
                summary_prefix="全文一致性分段内部检查进行中",
            ),
            f"正在检查全文一致性：分段 {chunk.index}/{chunk.total}",
            force=True,
        )

        ensure_active()
        fact_content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"全文一致性事实提取（{chunk.index}/{chunk.total}）",
            prompt=LONG_DOCUMENT_FACT_EXTRACTION_PROMPT,
            document_text=chunk_text,
            issue_output_limit=LONG_DOCUMENT_FACT_LIMIT_PER_CHUNK,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        chunk_facts = extract_consistency_facts(fact_content, chunk.label)
        if chunk_facts:
            facts.extend(chunk_facts)
        else:
            fact_failures.append(chunk.label)

    candidates = build_consistency_candidates(facts)
    candidate_batches = consistency_candidate_batches(candidates, max_chars=chunk_chars)
    for batch_index, candidate_text in enumerate(candidate_batches, start=1):
        ensure_active()
        verified = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"全文一致性跨分段证据复核（{batch_index}/{len(candidate_batches)}）",
            prompt=f"{item['prompt']}\n\n{LONG_DOCUMENT_CONSISTENCY_VERIFY_PROMPT}",
            document_text=candidate_text,
            issue_output_limit=chunk_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        reports.append((f"跨分段证据复核 {batch_index}/{len(candidate_batches)}", verified))
        save_partial(
            merge_chunk_reports(
                reports,
                issue_output_limit=issue_output_limit,
                summary_prefix="全文一致性跨分段复核进行中",
            ),
            f"正在复核全文一致性候选：{batch_index}/{len(candidate_batches)}",
            force=True,
        )

    if fact_failures:
        warning = json.dumps(
            {
                "summary": "部分事实提取批次未形成可解析结果。",
                "items": [
                    {
                        "status": "suggestion",
                        "severity": "low",
                        "confidence": "high",
                        "category": "全文一致性检查完整性",
                        "location": "、".join(fact_failures[:12]),
                        "excerpt": "",
                        "description": "部分分段未能建立事实索引，跨分段冲突覆盖可能不完整。",
                        "impact": "这些分段与其他章节之间的远距离矛盾可能被遗漏。",
                        "suggestion": "建议重试任务或人工复核所列分段。",
                    }
                ],
            },
            ensure_ascii=False,
        )
        reports.append(("事实索引完整性", warning))

    return merge_chunk_reports(
        reports,
        issue_output_limit=issue_output_limit,
        summary_prefix=(
            f"全文一致性长文档检查完成：提取 {len(facts)} 条事实，"
            f"复核 {len(candidates)} 组潜在冲突"
        ),
    )


def _run_long_multi_document_check(
    *,
    task,
    item: dict,
    document_text: str,
    issue_output_limit: int,
    save_partial,
    ensure_active,
    stream_trace_enabled: bool,
) -> str:
    grouped = parse_consistency_documents(document_text)
    if not grouped["master"] or not grouped["related"]:
        raise RuntimeError("超长多文档对照无法识别素材文档或资料边界")

    request_chars, chunk_chars = grouped_document_limits(task["max_input_chars"])
    master_chunks = [
        chunk
        for document in grouped["master"]
        for chunk in split_grouped_document(document, max_chars=chunk_chars)
    ]
    related_chunks = [
        chunk
        for document in grouped["related"]
        for chunk in split_grouped_document(document, max_chars=chunk_chars)
    ]
    master_index = build_chunk_search_index(master_chunks)
    related_index = build_chunk_search_index(related_chunks)
    reports = []
    network = outbound_network_config()
    batch_issue_limit = _long_document_chunk_issue_limit(issue_output_limit)
    total_batches = len(related_chunks) + len(master_chunks)
    completed_batches = 0

    def run_batch(*, query, evidence, direction: str, prompt: str, check_label: str):
        nonlocal completed_batches
        ensure_active()
        content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"{item['name']}（{check_label}）",
            prompt=f"{item['prompt']}\n\n{prompt}",
            document_text=build_evidence_comparison_input(
                query,
                evidence,
                direction=direction,
                max_chars=request_chars,
            ),
            issue_output_limit=batch_issue_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        completed_batches += 1
        reports.append((check_label, content))
        save_partial(
            merge_chunk_reports(
                reports,
                issue_output_limit=issue_output_limit,
                summary_prefix="多文档长文档对照进行中",
            ),
            f"正在执行多文档长文档对照：{completed_batches}/{total_batches}",
            force=True,
        )

    for index, query in enumerate(related_chunks, start=1):
        run_batch(
            query=query,
            evidence=rank_relevant_chunks(query, master_index),
            direction="related_to_master",
            prompt=LONG_MULTI_DOCUMENT_FORWARD_PROMPT,
            check_label=f"资料对照素材 {index}/{len(related_chunks)}：{query.label}",
        )

    for index, query in enumerate(master_chunks, start=1):
        run_batch(
            query=query,
            evidence=rank_relevant_chunks(query, related_index),
            direction="master_to_related",
            prompt=LONG_MULTI_DOCUMENT_REVERSE_PROMPT,
            check_label=f"素材反向覆盖 {index}/{len(master_chunks)}：{query.label}",
        )

    return merge_chunk_reports(
        reports,
        issue_output_limit=issue_output_limit,
        summary_prefix=(
            f"多文档长文档对照完成：资料检查 {len(related_chunks)} 个分段，"
            f"素材反向覆盖 {len(master_chunks)} 个分段"
        ),
    )


def _run_long_language_consistency_check(
    *,
    task,
    item: dict,
    document_text: str,
    issue_output_limit: int,
    save_partial,
    ensure_active,
    stream_trace_enabled: bool,
) -> str:
    static_precheck, document_a, document_b = parse_language_consistency_documents(document_text)
    if document_a is None or document_b is None:
        raise RuntimeError("超长跨语种检查无法识别文档A或文档B边界")

    request_chars, chunk_chars = grouped_document_limits(task["max_input_chars"])
    left_chunks = split_grouped_document(document_a, max_chars=chunk_chars)
    right_chunks = split_grouped_document(document_b, max_chars=chunk_chars)
    alignments, unmatched_left, unmatched_right = align_language_chunks(left_chunks, right_chunks)
    left_index = build_chunk_search_index(left_chunks)
    right_index = build_chunk_search_index(right_chunks)
    reports = []
    network = outbound_network_config()
    batch_issue_limit = _long_document_chunk_issue_limit(issue_output_limit)
    total_batches = len(alignments) + len(unmatched_left) + len(unmatched_right)
    completed_batches = 0

    def run_batch(*, left, right, coverage_side: str, check_label: str):
        nonlocal completed_batches
        ensure_active()
        content = run_check(
            api_base=task["api_base"],
            api_key=task["api_key"],
            proxy_mode=network["proxy_mode"],
            proxy=network["proxy"],
            ssl_verify=network["ssl_verify"],
            request_timeout=task["request_timeout"],
            model_name=task["model_name"],
            force_disable_thinking=_task_flag(task, "force_disable_thinking"),
            check_name=f"{item['name']}（{check_label}）",
            prompt=(
                f"{item['prompt']}\n\n"
                f"{LONG_LANGUAGE_COVERAGE_PROMPT if coverage_side else LONG_LANGUAGE_PAIRED_PROMPT}"
            ),
            document_text=build_language_alignment_input(
                left,
                right,
                static_precheck=static_precheck,
                max_chars=request_chars,
                coverage_side=coverage_side,
            ),
            issue_output_limit=batch_issue_limit,
            task_id=task["id"],
            stream_trace_enabled=stream_trace_enabled,
        )
        completed_batches += 1
        reports.append((check_label, content))
        save_partial(
            merge_chunk_reports(
                reports,
                issue_output_limit=issue_output_limit,
                summary_prefix="跨语种长文档检查进行中",
            ),
            f"正在执行跨语种长文档检查：{completed_batches}/{total_batches}",
            force=True,
        )

    for index, alignment in enumerate(alignments, start=1):
        run_batch(
            left=alignment.left,
            right=alignment.right,
            coverage_side="",
            check_label=f"对齐分段 {index}/{len(alignments)}：{alignment.left.label} ↔ {alignment.right.label}",
        )

    for index, chunk in enumerate(unmatched_left, start=1):
        evidence = rank_relevant_chunks(chunk, right_index, top_k=1)
        run_batch(
            left=chunk,
            right=evidence[0] if evidence else None,
            coverage_side="document_a",
            check_label=f"文档A未匹配覆盖 {index}/{len(unmatched_left)}：{chunk.label}",
        )

    for index, chunk in enumerate(unmatched_right, start=1):
        evidence = rank_relevant_chunks(chunk, left_index, top_k=1)
        if not evidence:
            continue
        run_batch(
            left=evidence[0],
            right=chunk,
            coverage_side="document_b",
            check_label=f"文档B未匹配覆盖 {index}/{len(unmatched_right)}：{chunk.label}",
        )

    return merge_chunk_reports(
        reports,
        issue_output_limit=issue_output_limit,
        summary_prefix=(
            f"跨语种长文档检查完成：对齐 {len(alignments)} 组分段，"
            f"复核 {len(unmatched_left) + len(unmatched_right)} 个未匹配分段"
        ),
    )


def _run_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    document_text: str,
    *,
    max_workers: int,
    stream_trace_enabled: bool,
) -> list[dict]:
    task_id = task["id"]
    total = len(check_items)
    total_units = max(1, total)
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
        ),
        daemon=True,
        name=f"task-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        _update_progress(db, task_id, 5)
    heartbeat.start()
    task_type = _task_value(task, "task_type") or DOCUMENT_TASK_TYPE
    direct_limit, _ = long_document_limits(task["max_input_chars"])
    grouped_long_mode = (
        task_type in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}
        and len(document_text) > direct_limit
    )
    if any(item.get("code") != SENSITIVE_TERMS_CHECK_CODE for item in check_items):
        if grouped_long_mode:
            long_chunks, document_outline, long_chunk_chars = [], "", 0
        else:
            long_chunks, document_outline, long_chunk_chars = _long_document_execution_plan(
                document_text,
                task["max_input_chars"],
            )
    else:
        long_chunks, document_outline, long_chunk_chars = [], "", 0
    if grouped_long_mode:
        app.logger.info(
            "分组长文档模式 task_id=%s task_type=%s document_chars=%s",
            task_id,
            task_type,
            len(document_text),
        )
    elif long_chunks:
        app.logger.info(
            "单文档长文档模式 task_id=%s document_chars=%s chunks=%s chunk_chars=%s outline_chars=%s",
            task_id,
            len(document_text),
            len(long_chunks),
            long_chunk_chars,
            len(document_outline),
        )

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            snapshot = _ordered_results(check_items, completed_by_code, partial_by_code)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()

    def run_item(index: int, item: dict) -> dict:
        with app.app_context():
            db = get_db()

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled

            ensure_active()

            app.logger.info(
                "任务检查项开始 task_id=%s item=%s index=%s/%s",
                task_id,
                item["name"],
                index,
                total,
            )
            last_stream_write = 0.0
            def save_partial(content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                content = content.strip()
                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled
                with result_lock:
                    had_partial = item["code"] in partial_by_code
                    if content:
                        partial_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": content,
                        }
                    else:
                        partial_by_code.pop(item["code"], None)

                last_stream_write = now
                if not content and not had_partial:
                    return
                save_snapshot(db, summary, current_progress())

            structured_report = None
            if item["code"] == SENSITIVE_TERMS_CHECK_CODE:
                structured_report = _run_sensitive_terms_check(app, document_text, issue_output_limit)
                content = format_sensitive_terms_report(structured_report)
            elif grouped_long_mode and task_type == CONSISTENCY_TASK_TYPE:
                content = _run_long_multi_document_check(
                    task=task,
                    item=item,
                    document_text=document_text,
                    issue_output_limit=issue_output_limit,
                    save_partial=save_partial,
                    ensure_active=ensure_active,
                    stream_trace_enabled=stream_trace_enabled,
                )
            elif grouped_long_mode and task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
                content = _run_long_language_consistency_check(
                    task=task,
                    item=item,
                    document_text=document_text,
                    issue_output_limit=issue_output_limit,
                    save_partial=save_partial,
                    ensure_active=ensure_active,
                    stream_trace_enabled=stream_trace_enabled,
                )
            elif long_chunks:
                if item["code"] == LONG_DOCUMENT_CONSISTENCY_CODE:
                    content = _run_long_document_consistency_check(
                        task=task,
                        item=item,
                        chunks=long_chunks,
                        outline=document_outline,
                        chunk_chars=long_chunk_chars,
                        issue_output_limit=issue_output_limit,
                        save_partial=save_partial,
                        ensure_active=ensure_active,
                        stream_trace_enabled=stream_trace_enabled,
                    )
                else:
                    content = _run_long_document_chunk_check(
                        task=task,
                        item=item,
                        chunks=long_chunks,
                        outline=document_outline,
                        issue_output_limit=issue_output_limit,
                        save_partial=save_partial,
                        ensure_active=ensure_active,
                        stream_trace_enabled=stream_trace_enabled,
                    )
            else:
                network = outbound_network_config()
                content = run_check(
                    api_base=task["api_base"],
                    api_key=task["api_key"],
                    proxy_mode=network["proxy_mode"],
                    proxy=network["proxy"],
                    ssl_verify=network["ssl_verify"],
                    request_timeout=task["request_timeout"],
                    model_name=task["model_name"],
                    force_disable_thinking=_task_flag(task, "force_disable_thinking"),
                    check_name=item["name"],
                    prompt=item["prompt"],
                    document_text=document_text,
                    issue_output_limit=issue_output_limit,
                    on_content=lambda content: save_partial(content, f"正在并发检查：{item['name']}"),
                    task_id=task_id,
                    stream_trace_enabled=stream_trace_enabled,
                )
            progress = mark_unit_completed()

            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            result = {
                "code": item["code"],
                "name": item["name"],
                "result": content,
                "issue_output_limit": issue_output_limit,
            }
            if structured_report is not None:
                result["structured_report"] = structured_report
            with result_lock:
                completed_by_code[item["code"]] = result
                partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个检查项，继续检查中。",
                progress,
            )
            app.logger.info(
                "任务检查项完成 task_id=%s item=%s output_chars=%s",
                task_id,
                item["name"],
                len(content),
            )
            return result

    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, total)), thread_name_prefix=f"task-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_item, index, item) for index, item in enumerate(check_items, start=1)]
        results = []
        for future in as_completed(futures):
            results.append(future.result())
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分检查项未完成")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _run_sensitive_terms_check(app, document_text: str, issue_output_limit: int) -> dict:
    root_dir = Path(app.config.get("ROOT_DIR") or Path(app.instance_path).parent)
    instance_dir = Path(app.instance_path)
    configured_path = app.config.get("SENSITIVE_TERMS_PATH")
    candidates = sensitive_terms_file_candidates(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    terms_path = find_sensitive_terms_file(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    if terms_path is None:
        return build_sensitive_terms_missing_report(candidates=candidates)

    try:
        rules = load_sensitive_terms(terms_path)
    except Exception as exc:
        return build_sensitive_terms_invalid_report(source_path=terms_path, error=str(exc))
    if not rules:
        return build_sensitive_terms_invalid_report(
            source_path=terms_path,
            error="未读取到有效敏感词规则，请确认表头包含“不规范用语”和“规范用语”两列。",
        )
    return build_sensitive_terms_report(
        document_text,
        rules,
        source_path=terms_path,
        issue_limit=issue_output_limit,
    )


def _run_image_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    image_items: list[dict],
    page_image_items: list[dict],
    document_text: str,
    *,
    document_meta: dict | None,
    max_workers: int,
    stream_trace_enabled: bool,
) -> list[dict]:
    task_id = task["id"]
    total = len(check_items)
    groups = _image_check_groups(check_items, image_items, page_image_items, document_meta or {})
    if not groups:
        raise RuntimeError("没有可检查的 PDF 页面截图或图片资源")
    total_units = max(1, sum(max(1, len(group["batches"])) for group in groups))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
        ),
        daemon=True,
        name=f"task-image-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        _update_progress(db, task_id, 5)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            snapshot = _ordered_results(check_items, completed_by_code, partial_by_code)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()

    def run_group(group_index: int, group: dict):
        with app.app_context():
            db = get_db()
            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            items = group["items"]
            batches = group["batches"]
            batch_count = len(batches)
            skipped_images = group["skipped_images"]
            manual_notes = group["manual_notes"]
            target_label = group["label"]
            app.logger.info(
                "任务图文联合检查组开始 task_id=%s target=%s group=%s/%s checks=%s images=%s skipped_images=%s batches=%s",
                task_id,
                target_label,
                group_index,
                len(groups),
                len(items),
                len(group["checkable_images"]),
                len(skipped_images),
                batch_count,
            )
            prompt = _combined_image_check_prompt(items, group["target_kind"], target_label)
            batch_results_by_code = {item["code"]: [] for item in items}
            last_stream_write = 0.0

            def save_partial(current_batch: dict | None, content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                content = content.strip()
                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled

                with result_lock:
                    sections = _split_combined_check_output(content, items) if content else {}
                    for item in items:
                        item_content = sections.get(item["code"], "")
                        result_text = _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            current_batch=current_batch if item_content else None,
                            current_content=item_content,
                            skipped_images=skipped_images,
                            manual_notes=manual_notes,
                        )
                        if result_text:
                            partial_by_code[item["code"]] = {
                                "code": item["code"],
                                "name": item["name"],
                                "result": result_text,
                            }
                        elif not batch_results_by_code[item["code"]]:
                            partial_by_code.pop(item["code"], None)

                last_stream_write = now
                save_snapshot(db, summary, current_progress())

            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not batches:
                progress = mark_unit_completed()
                with result_lock:
                    for item in items:
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                [],
                                skipped_images=skipped_images,
                                manual_notes=manual_notes,
                            ),
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个图片检查项，继续检查中。",
                    progress,
                )
                app.logger.info(
                    "任务图文联合检查组跳过 task_id=%s target=%s skipped_images=%s",
                    task_id,
                    target_label,
                    len(skipped_images),
                )
                return

            for batch_index, batch in enumerate(batches, start=1):
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled
                multimodal_images = _multimodal_image_inputs(image_folder, batch)
                batch_document_text = _document_text_for_image_batch(document_text, batch)
                current_batch = {
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "images": batch,
                    "target_label": target_label,
                    "target_kind": group["target_kind"],
                }

                content = run_multimodal_document_check(
                    api_base=task["api_base"],
                    api_key=task["api_key"],
                    proxy_mode=network["proxy_mode"],
                    proxy=network["proxy"],
                    ssl_verify=network["ssl_verify"],
                    request_timeout=task["request_timeout"],
                    model_name=task["model_name"],
                    force_disable_thinking=_task_flag(task, "force_disable_thinking"),
                    check_name=f"{target_label}合并检查（{len(items)}项）",
                    prompt=prompt,
                    document_text=batch_document_text,
                    image_items=multimodal_images,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    issue_output_limit=issue_output_limit,
                    on_content=lambda content, current=current_batch: save_partial(
                        current,
                        content,
                        f"正在进行{target_label}：批次 {current['batch_index']}/{current['batch_count']}",
                    ),
                    task_id=task_id,
                    stream_trace_enabled=stream_trace_enabled,
                )
                sections = _split_combined_check_output(content, items)
                for item in items:
                    batch_results_by_code[item["code"]].append(
                        {
                            "batch_index": batch_index,
                            "batch_count": batch_count,
                            "images": batch,
                            "target_label": target_label,
                            "target_kind": group["target_kind"],
                            "content": sections.get(item["code"], ""),
                        }
                    )
                progress = mark_unit_completed()
                save_partial(
                    None,
                    "",
                    f"已完成 {target_label}：{batch_index}/{batch_count} 个图文批次",
                    force=True,
                )
                save_snapshot(
                    db,
                    f"正在进行图片检查，已完成 {completed_units}/{total_units} 个图文批次。",
                    progress,
                )

            with result_lock:
                for item in items:
                    completed_by_code[item["code"]] = {
                        "code": item["code"],
                        "name": item["name"],
                        "result": _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            skipped_images=skipped_images,
                            manual_notes=manual_notes,
                        ),
                    }
                    partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个图片检查项，继续检查中。",
                current_progress(),
            )
            app.logger.info(
                "任务图文联合检查组完成 task_id=%s target=%s checks=%s images=%s skipped_images=%s batches=%s",
                task_id,
                target_label,
                len(items),
                len(group["checkable_images"]),
                len(skipped_images),
                batch_count,
            )
            return

    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(groups))), thread_name_prefix=f"task-image-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_group, index, group) for index, group in enumerate(groups, start=1)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分图文检查项未完成")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _run_video_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    frame_items: list[dict],
    document_text: str,
    *,
    document_meta: dict | None,
    max_workers: int,
    stream_trace_enabled: bool,
) -> list[dict]:
    task_id = task["id"]
    total = len(check_items)
    checkable_frames, skipped_frames = _split_checkable_image_items(frame_items)
    if not checkable_frames and not skipped_frames:
        raise RuntimeError("没有可检查的视频抽帧画面")

    batches = _image_batches(checkable_frames, _image_check_batch_size())
    total_units = max(1, len(batches))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
        ),
        daemon=True,
        name=f"task-video-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        _update_progress(db, task_id, 5)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            snapshot = _ordered_results(check_items, completed_by_code, partial_by_code)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()

    def run_checks():
        with app.app_context():
            db = get_db()
            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            app.logger.info(
                "任务视频多模态检查开始 task_id=%s checks=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )
            prompt = _combined_video_check_prompt(check_items)
            batch_results_by_code = {item["code"]: [] for item in check_items}
            last_stream_write = 0.0

            def save_partial(current_batch: dict | None, content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                content = content.strip()
                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled

                with result_lock:
                    sections = _split_combined_check_output(content, check_items) if content else {}
                    for item in check_items:
                        item_content = sections.get(item["code"], "")
                        result_text = _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            current_batch=current_batch if item_content else None,
                            current_content=item_content,
                            skipped_images=skipped_frames,
                        )
                        if result_text:
                            partial_by_code[item["code"]] = {
                                "code": item["code"],
                                "name": item["name"],
                                "result": result_text,
                            }
                        elif not batch_results_by_code[item["code"]]:
                            partial_by_code.pop(item["code"], None)

                last_stream_write = now
                save_snapshot(db, summary, current_progress())

            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not batches:
                progress = mark_unit_completed()
                with result_lock:
                    for item in check_items:
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                [],
                                skipped_images=skipped_frames,
                            ),
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个视频检查项。",
                    progress,
                )
                return

            for batch_index, batch in enumerate(batches, start=1):
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled
                multimodal_images = _multimodal_image_inputs(image_folder, batch)
                current_batch = {
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "images": batch,
                    "target_label": "视频帧检查",
                    "target_kind": "video_frame",
                }
                content = run_multimodal_document_check(
                    api_base=task["api_base"],
                    api_key=task["api_key"],
                    proxy_mode=network["proxy_mode"],
                    proxy=network["proxy"],
                    ssl_verify=network["ssl_verify"],
                    request_timeout=task["request_timeout"],
                    model_name=task["model_name"],
                    force_disable_thinking=_task_flag(task, "force_disable_thinking"),
                    check_name=f"视频帧检查合并检查（{len(check_items)}项）",
                    prompt=prompt,
                    document_text=_document_text_for_video_batch(document_text, batch, document_meta or {}),
                    image_items=multimodal_images,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    issue_output_limit=issue_output_limit,
                    on_content=lambda content, current=current_batch: save_partial(
                        current,
                        content,
                        f"正在进行视频帧检查：批次 {current['batch_index']}/{current['batch_count']}",
                    ),
                    task_id=task_id,
                    stream_trace_enabled=stream_trace_enabled,
                )
                sections = _split_combined_check_output(content, check_items)
                for item in check_items:
                    batch_results_by_code[item["code"]].append(
                        {
                            "batch_index": batch_index,
                            "batch_count": len(batches),
                            "images": batch,
                            "target_label": "视频帧检查",
                            "target_kind": "video_frame",
                            "content": sections.get(item["code"], ""),
                        }
                    )
                progress = mark_unit_completed()
                save_partial(
                    None,
                    "",
                    f"已完成视频帧检查：{batch_index}/{len(batches)} 个批次",
                    force=True,
                )
                save_snapshot(
                    db,
                    f"正在进行视频检查，已完成 {completed_units}/{total_units} 个视频批次。",
                    progress,
                )

            with result_lock:
                for item in check_items:
                    completed_by_code[item["code"]] = {
                        "code": item["code"],
                        "name": item["name"],
                        "result": _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            skipped_images=skipped_frames,
                        ),
                    }
                    partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个视频检查项。",
                current_progress(),
            )
            app.logger.info(
                "任务视频多模态检查完成 task_id=%s checks=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"task-video-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_checks)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分视频检查项未完成")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _combined_video_check_prompt(check_items: list[dict]) -> str:
    item_blocks = []
    for item in check_items:
        item_blocks.append(
            f"### 检查项：{item['code']}｜{item['name']}\n"
            f"{str(item.get('prompt') or '').strip()}"
        )
    required_sections = "\n".join(
        (
            f"### 检查项：{item['code']}｜{item['name']}\n"
            "#### 总体判断\n"
            "说明是否发现明确问题；没有明确问题时只写“未发现明确问题”。\n"
            "#### 明确问题\n"
            "- 只列能够从当前采样帧直接判定的异常，每条必须以视频时间点或帧文件名开头；没有明确问题时只写“未发现明确问题”，不要列正常项。\n"
            "#### 需人工确认\n"
            "- 只列证据不足、看不清、需要连续片段或业务资料确认的对象；没有需人工确认项时只写“未发现需人工确认项”。\n"
            "#### 未发现问题\n"
            "- 可简述正常项；这些内容不要写入“明确问题”。"
        )
        for item in check_items
    )
    return (
        f"本次执行硬件产品安装调测视频质检，一次请求中合并 {len(check_items)} 个检查项。"
        "请对每个检查项独立判断，不要把其他检查项的结论混入当前检查项。\n\n"
        "检查对象说明：系统只提供从视频时间轴均匀抽取的采样帧，模型不能直接观看完整连续视频。"
        "请重点观察安装顺序、接线端子、安全防护、调测界面/参数、视频清晰度和关键步骤完整性。\n\n"
        "定位要求：引用画面时优先使用视频时间点（例如 00:12.300），必要时补充帧文件名；不要使用 PDF 页码定位。\n"
        "证据约束：只依据当前提供的采样帧和视频上下文判断；看不清、被遮挡、动作连续性证据不足或缺少产品图纸依据时放入“需人工确认”，不要编造不可见内容。\n"
        "汇总约束：明确问题只放已确认异常；正常、未发现、符合、清晰、完整、无实质影响、无需修改、无需处理等无问题结论不得放入“明确问题”。\n\n"
        "输出必须严格使用以下结构，并保留每个检查项的 code：\n"
        f"{required_sections}\n\n"
        "检查项提示词如下：\n\n"
        f"{chr(10).join(item_blocks)}"
    )


def _document_text_for_video_batch(document_text: str, frame_items: list[dict], document_meta: dict) -> str:
    text = _trim_document_context(str(document_text or "").strip(), IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)
    selection = document_meta.get("frame_selection") if isinstance(document_meta, dict) else {}
    frame_lines = []
    for frame in frame_items:
        filename = str(frame.get("filename") or frame.get("id") or "视频帧")
        position = str(frame.get("position") or "未标注")
        frame_lines.append(f"- {filename}：视频时间点 {position}")
    selection_lines = []
    if isinstance(selection, dict):
        duration = selection.get("duration_seconds")
        frame_count = selection.get("frame_count")
        if duration is not None:
            selection_lines.append(f"- 视频时长：{duration} 秒")
        if frame_count is not None:
            selection_lines.append(f"- 总抽帧数：{frame_count}")
        if selection.get("strategy"):
            selection_lines.append(f"- 抽帧策略：{selection.get('strategy')}")

    parts = []
    if text:
        parts.append(text)
    if selection_lines:
        parts.append("video_sampling:\n" + "\n".join(selection_lines))
    parts.append("current_batch_video_frames:\n" + ("\n".join(frame_lines) if frame_lines else "- 未记录视频帧"))
    return "\n\n".join(parts).strip()


def _image_check_groups(
    check_items: list[dict],
    image_items: list[dict],
    page_image_items: list[dict],
    document_meta: dict,
) -> list[dict]:
    items_by_target = {"page": [], "resource": []}
    for item in check_items:
        items_by_target[_image_check_target(item)].append(item)

    groups = []
    for target_kind in ("page", "resource"):
        items = items_by_target[target_kind]
        if not items:
            continue
        source_images = page_image_items if target_kind == "page" else image_items
        fallback_used = False
        if not source_images and target_kind == "resource" and page_image_items:
            source_images = page_image_items
            fallback_used = True
        if not source_images and target_kind == "page" and image_items:
            source_images = image_items
            fallback_used = True
        checkable_images, skipped_images = _split_checkable_image_items(source_images)
        manual_notes = _image_check_manual_notes(target_kind, document_meta, fallback_used)
        groups.append(
            {
                "target_kind": target_kind,
                "label": IMAGE_CHECK_TARGET_LABELS[target_kind],
                "items": items,
                "checkable_images": checkable_images,
                "skipped_images": skipped_images,
                "manual_notes": manual_notes,
                "batches": _image_batches(checkable_images, _image_check_batch_size()),
            }
        )
    return groups


def _image_check_target(item: dict) -> str:
    code = str(item.get("code") or "")
    if code in IMAGE_RESOURCE_CHECK_CODES:
        return "resource"
    if code in IMAGE_PAGE_CHECK_CODES:
        return "page"
    return "page"


def _image_check_manual_notes(target_kind: str, document_meta: dict, fallback_used: bool) -> list[str]:
    notes = []
    if target_kind == "page":
        selection = document_meta.get("page_selection") if isinstance(document_meta, dict) else None
        if isinstance(selection, dict) and int(selection.get("omitted_pages") or 0) > 0:
            notes.append(
                "PDF 共 {total} 页，本次页面级检查按长文档策略选取 {selected} 页，未覆盖 {omitted} 页；"
                "未覆盖页需要人工抽查或调高 image_page_check_max_pages 后重跑。".format(
                    total=selection.get("total_pages", "-"),
                    selected=len(selection.get("selected_pages") or []),
                    omitted=selection.get("omitted_pages", "-"),
                )
            )
    if target_kind == "resource":
        image_error = str(document_meta.get("image_extraction_error") or "").strip() if isinstance(document_meta, dict) else ""
        if image_error:
            notes.append(f"PDF 内嵌图片提取异常，图片资源类检查可能未覆盖全部原始图片：{image_error}")
    if fallback_used:
        notes.append("当前检查对象缺少首选图片源，已回退使用另一类 PDF 图像；细节判断需要人工确认。")
    return notes


def _combined_image_check_prompt(check_items: list[dict], target_kind: str, target_label: str) -> str:
    target_instruction = (
        "本组检查对象是 PDF 整页截图。请重点观察页面中的正文、图、表、标题、页眉页脚、遮挡、裁切、版式和上下文对应关系。"
        if target_kind == "page"
        else "本组检查对象是从 PDF 中提取的内嵌图片资源。请重点观察图片自身的文字、接线、图形元素、标注和可读性。"
    )
    item_blocks = []
    for item in check_items:
        item_blocks.append(
            f"### 检查项：{item['code']}｜{item['name']}\n"
            f"{str(item.get('prompt') or '').strip()}"
        )
    required_sections = "\n".join(
        (
            f"### 检查项：{item['code']}｜{item['name']}\n"
            "#### 总体判断\n"
            "说明是否发现明确问题；没有明确问题时只写“未发现明确问题”。\n"
            "#### 明确问题\n"
            "- 只列能从当前可见图片直接判定的异常，每条必须以“第N页”或图片位置开头；没有明确问题时只写“未发现明确问题”，不要列正常项。\n"
            "#### 需人工确认\n"
            "- 只列证据不足、看不清或需要业务确认的对象，每条必须以“第N页”或图片位置开头；没有需人工确认项时只写“未发现需人工确认项”。\n"
            "#### 未发现问题\n"
            "- 可简述正常项；这些内容不要写入“明确问题”。"
        )
        for item in check_items
    )
    return (
        f"本次执行{target_label}，一次请求中合并 {len(check_items)} 个检查项。请对每个检查项独立判断，"
        "不要把其他检查项的结论混入当前检查项。\n\n"
        f"{target_instruction}\n\n"
        "定位要求：引用图片时优先使用 PDF 页码（例如“第12页”），同一页有多张图时再补充图片编号/位置；无法识别页码时使用图片编号或原始位置。\n"
        "汇总约束：明确问题只放已确认异常；正常、未发现、符合、一致、清晰、完整等无问题结论只能放“未发现问题”，不得放入“明确问题”。\n"
        "证据约束：只能依据本次提供的 PDF 页面/图片和文档上下文；看不清、证据不足、跨页缺上下文时放入“需人工确认”，不要编造不可见内容。\n\n"
        "输出必须严格使用以下结构，并保留每个检查项的 code：\n"
        f"{required_sections}\n\n"
        "检查项提示词如下：\n\n"
        f"{chr(10).join(item_blocks)}"
    )


def _split_combined_check_output(content: str, check_items: list[dict]) -> dict[str, str]:
    text = str(content or "").strip()
    if not text:
        return {item["code"]: _missing_check_section(item, "模型未返回该检查项内容。") for item in check_items}
    if len(check_items) == 1 and "### 检查项：" not in text:
        return {check_items[0]["code"]: text}

    headers = list(re.finditer(r"(?m)^###\s*检查项[:：]\s*(.+?)\s*$", text))
    sections: dict[str, str] = {}
    for index, header in enumerate(headers):
        header_line = header.group(1).strip()
        start = header.start()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        matched_code = _match_check_code_from_header(header_line, check_items)
        if matched_code:
            sections[matched_code] = text[start:end].strip()

    for item in check_items:
        sections.setdefault(item["code"], _missing_check_section(item, "模型未按要求返回该检查项的独立小节。"))
    return sections


def _match_check_code_from_header(header_line: str, check_items: list[dict]) -> str:
    normalized = str(header_line or "").strip()
    for item in check_items:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if code and code in normalized:
            return code
        if name and name in normalized:
            return code
    return ""


def _missing_check_section(item: dict, reason: str) -> str:
    return (
        f"### 检查项：{item.get('code')}｜{item.get('name')}\n\n"
        "#### 总体判断\n"
        "需人工确认。\n\n"
        "#### 明确问题\n"
        "- 未汇总到明确问题。\n\n"
        "#### 需人工确认\n"
        f"- {reason}\n\n"
        "#### 未发现问题\n"
        "- 未形成独立结论。"
    )


def _image_check_batch_size() -> int:
    return max(1, min(MAX_IMAGE_CHECK_BATCH_SIZE, _int_setting("image_check_batch_size", DEFAULT_IMAGE_CHECK_BATCH_SIZE)))


def _image_batches(image_items: list[dict], batch_size: int) -> list[list[dict]]:
    return [image_items[index : index + batch_size] for index in range(0, len(image_items), batch_size)]


def _document_text_for_image_batch(document_text: str, image_items: list[dict]) -> str:
    text = str(document_text or "").strip()
    if not text:
        return ""

    pages = sorted(
        {
            page
            for image in image_items
            for page in page_numbers_from_image_item(image)
        }
    )
    if not pages:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    page_sections = page_sections_from_document_text(text)
    if not page_sections:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    wanted_pages = set()
    for page in pages:
        for value in range(page - IMAGE_CONTEXT_NEIGHBOR_PAGES, page + IMAGE_CONTEXT_NEIGHBOR_PAGES + 1):
            if value > 0:
                wanted_pages.add(value)
    selected = [(page, section) for page, section in page_sections if page in wanted_pages]
    if not selected:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    image_lines = []
    for image in image_items:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        image_lines.append(f"- {filename}（位置：{position}）")

    page_text = "\n\n".join(section.strip() for _, section in selected if section.strip())
    header = _document_header(text)
    scoped = (
        f"{header}\n\n"
        f"document_text_scope: 仅提供当前图片所在页及前后 {IMAGE_CONTEXT_NEIGHBOR_PAGES} 页的文本，避免跨页误配。\n"
        f"current_batch_images:\n{chr(10).join(image_lines)}\n\n"
        f"相关文档文本：\n{page_text}"
    ).strip()
    return _trim_document_context(scoped, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)


def _document_header(document_text: str) -> str:
    lines = []
    for line in str(document_text or "").splitlines():
        if line.startswith("file:"):
            lines.append(line.strip())
            continue
        if lines:
            break
    return "\n".join(lines) if lines else "file: document"


def _trim_document_context(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[文档上下文已按当前批次截断]"


def _split_checkable_image_items(image_items: list[dict]) -> tuple[list[dict], list[dict]]:
    checkable = []
    skipped = []
    for index, image in enumerate(image_items, start=1):
        item = dict(image)
        item["_image_index"] = index
        mime_type = str(item.get("mime_type") or "")
        if mime_type.startswith("image/"):
            checkable.append(item)
            continue
        item["skip_reason"] = "不是可识别的图片格式"
        skipped.append(item)
    return checkable, skipped


def _multimodal_image_inputs(image_folder: Path, image_items: list[dict]) -> list[dict]:
    inputs = []
    for fallback_index, image in enumerate(image_items, start=1):
        image_index = int(image.get("_image_index") or fallback_index)
        image_path = image_path_from_item(image_folder, image)
        if not image_path.is_file():
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”已删除，无法检查")
        mime_type = str(image.get("mime_type") or "")
        if not mime_type.startswith("image/"):
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”不是可识别的图片格式")
        inputs.append(
            {
                "index": image_index,
                "name": str(image.get("filename") or f"image-{image_index:04d}"),
                "position": _image_location_label(image),
                "mime_type": mime_type,
                "data_url": image_to_data_url(image_path, mime_type),
            }
        )
    return inputs


def _image_pdf_page_label(image: dict) -> str:
    try:
        page_number = int(image.get("page_number") or 0)
    except (TypeError, ValueError):
        page_number = 0
    if page_number <= 0:
        pages = page_numbers_from_image_item(image)
        page_number = pages[0] if pages else 0
    if page_number <= 0:
        return ""
    return f"PDF第{page_number}页"


def _image_location_label(image: dict) -> str:
    page_label = _image_pdf_page_label(image)
    position = str(image.get("position") or "").strip()
    if page_label and position:
        return f"{page_label}（{position}）"
    if page_label:
        return page_label
    return position or "未标注"


def _format_multimodal_image_check_result(
    batch_results: list[dict],
    *,
    current_batch: dict | None = None,
    current_content: str = "",
    skipped_images: list[dict] | None = None,
    manual_notes: list[str] | None = None,
) -> str:
    parts = []
    for item in batch_results:
        parts.append(_format_image_batch_result(item, item["content"]))
    if current_batch is not None and current_content:
        parts.append(_format_image_batch_result(current_batch, current_content))
    if skipped_images:
        parts.append(_format_skipped_image_result(skipped_images))
    if manual_notes:
        parts.append(_format_manual_notes(manual_notes))
    if current_batch is None:
        summary = _format_image_check_issue_summary(batch_results, skipped_images or [], manual_notes or [])
        if summary:
            parts.append(summary)
    return "\n\n".join(parts).strip()


def _format_image_batch_result(batch: dict, content: str) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    images = batch.get("images") or []
    target_label = str(batch.get("target_label") or "图文联合检查")
    media_label = "视频帧" if batch.get("target_kind") == "video_frame" else "图片"
    title = f"### {target_label}结果" if batch_count <= 1 else f"### {target_label}结果（批次 {batch_index}/{batch_count}）"
    image_lines = []
    for image in images:
        filename = str(image.get("filename") or image.get("id") or media_label)
        location = _image_location_label(image)
        image_lines.append(f"- {location}：{filename}")
    image_list = "\n".join(image_lines) if image_lines else f"- 未记录{media_label}"
    return f"{title}\n\n覆盖{media_label}：\n{image_list}\n\n{str(content or '').strip()}"


def _format_skipped_image_result(skipped_images: list[dict]) -> str:
    image_lines = []
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        mime_type = str(image.get("mime_type") or "未知格式")
        reason = str(image.get("skip_reason") or "已跳过")
        image_lines.append(f"- {filename}（位置：{position}，格式：{mime_type}，原因：{reason}）")
    return "### 已跳过的图片\n\n以下提取图片不是可识别图片格式，已跳过，不影响其他图片继续检查：\n" + "\n".join(image_lines)


def _format_manual_notes(manual_notes: list[str]) -> str:
    lines = [f"- {note}" for note in _dedupe_limited([str(note).strip() for note in manual_notes], 30) if note]
    if not lines:
        return ""
    return "### 系统需人工确认\n\n" + "\n".join(lines)


def _format_image_check_issue_summary(batch_results: list[dict], skipped_images: list[dict], manual_notes: list[str] | None = None) -> str:
    issues = []
    manual = []
    for batch in batch_results:
        batch_label = _image_batch_label(batch)
        structured_issues, structured_manual = _structured_summary_items(str(batch.get("content") or ""))
        for line in structured_issues:
            issues.append(f"{batch_label} {line}")
        for line in structured_manual:
            manual.append(f"{batch_label} {line}")
        if structured_issues or structured_manual:
            continue
        for line in _summary_candidate_lines(str(batch.get("content") or "")):
            normalized = _normalize_summary_line(line)
            if not normalized or _summary_line_is_negative(normalized) or _summary_line_is_normal(normalized):
                continue
            entry = f"{batch_label} {normalized}"
            if _summary_line_needs_manual(normalized):
                manual.append(entry)
            elif _summary_line_is_issue(normalized):
                issues.append(entry)
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        location = _image_location_label(image)
        manual.append(f"{location}：{filename} 提取后不是可识别图片格式，已跳过，需人工确认是否影响检查。")
    for note in manual_notes or []:
        manual.append(str(note).strip())

    issues = _dedupe_limited(issues, 30)
    manual = _dedupe_limited(manual, 30)
    if not issues and not manual:
        return "### 检查汇总\n\n#### 明确问题\n- 未发现明确问题。\n\n#### 需人工确认\n- 未发现需人工确认项。"

    issue_text = "\n".join(f"- {item}" for item in issues) if issues else "- 未发现明确问题。"
    manual_text = "\n".join(f"- {item}" for item in manual) if manual else "- 未发现需人工确认项。"
    return f"### 检查汇总\n\n#### 明确问题\n{issue_text}\n\n#### 需人工确认\n{manual_text}"


def _image_batch_label(batch: dict) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    page_label = _video_batch_time_label(batch) if batch.get("target_kind") == "video_frame" else _image_batch_pages_label(batch)
    if batch_count > 1:
        prefix = f"批次 {batch_index}/{batch_count}"
    else:
        prefix = "批次 1"
    if page_label:
        return f"{prefix}（{page_label}）："
    return f"{prefix}："


def _video_batch_time_label(batch: dict) -> str:
    positions = []
    seen = set()
    for image in batch.get("images") or []:
        position = str(image.get("position") or "").strip()
        if not position or position in seen:
            continue
        seen.add(position)
        positions.append(position)
    if not positions:
        return ""
    if len(positions) == 1:
        return f"视频时间 {positions[0]}"
    return f"视频时间 {positions[0]}-{positions[-1]}"


def _image_batch_pages_label(batch: dict) -> str:
    pages = []
    seen = set()
    for image in batch.get("images") or []:
        for page in page_numbers_from_image_item(image):
            if page in seen:
                continue
            seen.add(page)
            pages.append(page)
    if not pages:
        return ""
    pages = sorted(pages)
    if len(pages) == 1:
        return f"PDF第{pages[0]}页"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"PDF第{pages[0]}-{pages[-1]}页"
    return "PDF第" + "、".join(str(page) for page in pages) + "页"


def _summary_candidate_lines(content: str) -> list[str]:
    lines = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[-*]?\s*(覆盖图片|覆盖视频帧|图片名称|图片位置|输出要求|详细问题列表)[:：]?$", line):
            continue
        lines.append(line)
    return lines


def _structured_summary_items(content: str) -> tuple[list[str], list[str]]:
    issues = []
    manual = []
    for item in _structured_section_items(content, "明确问题"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        if _summary_line_needs_manual(item):
            manual.append(item)
        elif _summary_line_is_issue(item):
            issues.append(item)
    for item in _structured_section_items(content, "需人工确认"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        manual.append(item)
    return issues, manual


def _structured_section_items(content: str, section_title: str) -> list[str]:
    items = []
    current_section = ""
    for raw_line in str(content or "").splitlines():
        heading, inline_text = _summary_section_heading(raw_line)
        if heading:
            current_section = heading
            if heading == section_title and inline_text:
                line = _normalize_summary_line(inline_text)
                if line:
                    items.append(line)
            continue
        if current_section != section_title:
            continue
        line = _normalize_summary_line(raw_line)
        if line:
            items.append(line)
    return items


def _summary_section_heading(line: str) -> tuple[str, str]:
    value = str(line or "").strip()
    if not value:
        return "", ""
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value).strip()
    value = re.sub(r"^\s*[-*]\s*", "", value).strip()
    value = value.strip("*_` \t")
    value = re.sub(r"^\d+[.)、]\s*", "", value).strip()

    title_aliases = (
        ("总体判断", "总体判断"),
        ("明确问题", "明确问题"),
        ("发现问题", "明确问题"),
        ("发现明确问题", "明确问题"),
        ("明确冲突", "明确问题"),
        ("需人工确认", "需人工确认"),
        ("需要人工确认", "需人工确认"),
        ("未发现问题", "未发现问题"),
    )
    for title, normalized_title in title_aliases:
        if value == title:
            return normalized_title, ""
        match = re.match(rf"^{re.escape(title)}\s*[:：]\s*(.+)$", value)
        if match:
            return normalized_title, match.group(1).strip()
    return "", ""


def _normalize_summary_line(line: str) -> str:
    value = re.sub(r"^\s*[-*]\s*", "", str(line or "").strip())
    value = re.sub(r"^\s*\d+[.)、]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:240].strip()


def _summary_line_is_negative(line: str) -> bool:
    text = str(line or "")
    if any(marker in text for marker in ("未汇总到", "未发现明确问题", "没有明确问题", "无明确问题", "无问题", "没有问题", "未见问题", "无异常", "未见异常")):
        return True
    if any(marker in text for marker in ("未发现需人工确认", "没有需人工确认", "无需人工确认", "无须人工确认", "未见需人工确认")):
        return True
    return bool(
        re.search(
            r"(未发现|没有发现|未见|无明显|不存在明显).{0,12}(问题|异常|风险|冲突|错误|缺失|不一致|不匹配|不符合)",
            text,
        )
    )


def _summary_line_is_normal(line: str) -> bool:
    text = str(line or "")
    if _summary_line_needs_manual(text) or _summary_line_has_issue_marker(text):
        return False
    return any(marker in text for marker in ("正常", "符合要求", "符合规范", "一致", "匹配", "清晰", "完整", "可读", "无误"))


def _summary_line_needs_manual(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "需人工确认",
            "需要人工确认",
            "建议人工",
            "建议复核",
            "无法确认",
            "无法判断",
            "无法辨认",
            "难以辨认",
            "不确定",
            "可能",
            "疑似",
            "证据不足",
            "上下文不足",
            "看不清",
            "文字过小",
            "较小",
            "分辨率不足",
        )
    )


def _summary_line_is_issue(line: str) -> bool:
    if _summary_line_is_negative(line) or _summary_line_is_normal(line) or _summary_line_needs_manual(line):
        return False
    return _summary_line_has_issue_marker(line)


def _summary_line_has_issue_marker(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "问题",
            "风险",
            "不一致",
            "不匹配",
            "冲突",
            "错误",
            "异常",
            "不符合",
            "缺失",
            "缺少",
            "错位",
            "不对应",
            "不完整",
            "不清晰",
            "不规范",
            "模糊",
            "遮挡",
            "裁切",
            "乱码",
            "断裂",
            "变形",
            "过度拉伸",
            "漏标",
            "错标",
            "矛盾",
            "有误",
        )
    )


def _dedupe_limited(items: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= limit:
            break
    return result


def _task_image_folder(app) -> Path:
    configured = app.config.get("IMAGE_FOLDER")
    if configured:
        return Path(configured)
    return default_image_folder(app.config["UPLOAD_FOLDER"])


def cleanup_expired_task_reports(app) -> int:
    retention_days = _report_retention_days()
    if retention_days <= 0:
        return 0

    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    tasks = db.execute(
        """
        SELECT *
        FROM tasks
        WHERE status IN ('completed', 'failed', 'canceled')
          AND COALESCE(finished_at, updated_at, created_at) < ?
        ORDER BY COALESCE(finished_at, updated_at, created_at) ASC, id ASC
        LIMIT ?
        """,
        (cutoff, REPORT_CLEANUP_BATCH_SIZE),
    ).fetchall()
    deleted = 0
    for task in tasks:
        try:
            _remove_task_artifacts(app, task)
            db.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
            deleted += 1
        except TaskArtifactCleanupError as exc:
            app.logger.warning("定期清理检查报告跳过 task_id=%s error=%s", task["id"], exc)
        except Exception:
            app.logger.exception("定期清理检查报告失败 task_id=%s", task["id"])
    if deleted:
        db.commit()
        app.logger.info("定期清理检查报告完成 deleted=%s cutoff=%s retention_days=%s", deleted, cutoff, retention_days)
    return deleted


def _report_retention_days() -> int:
    return max(0, _int_setting("report_retention_days", DEFAULT_REPORT_RETENTION_DAYS))


def _issue_output_limit() -> int:
    return max(0, _int_setting("issue_output_limit", DEFAULT_ISSUE_OUTPUT_LIMIT))


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _remove_task_artifacts(app, task):
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    paths = _task_artifact_paths(app, task)
    image_dirs = {
        path.parent
        for path in paths
        if path.parent.resolve() != image_root.resolve() and _path_is_relative_to(path.parent, image_root)
    }
    failures = []
    for path in paths:
        if not (_path_is_relative_to(path, upload_root) or _path_is_relative_to(path, image_root)):
            app.logger.warning("跳过不在运行目录内的任务文件 task_id=%s path=%s", task["id"], path)
            continue
        if path.exists() and path.is_file():
            ok, error = remove_file(path)
            if not ok:
                failures.append((path, error))
    for image_dir in sorted(image_dirs, key=lambda value: len(value.parts), reverse=True):
        _remove_empty_directory(image_dir)
    if failures:
        raise TaskArtifactCleanupError(
            f"任务文件正被其他程序使用，暂时无法清理：{describe_failures(failures)}。"
            "请关闭正在下载、预览或扫描该文件的程序后稍后重试。"
        )


def _task_artifact_paths(app, task) -> list[Path]:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    raw_meta = _task_value(task, "document_meta_json")
    paths = []
    groups = document_groups_from_meta(raw_meta)
    if groups:
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if stored_filename:
                    paths.append(upload_root / stored_filename)
    else:
        stored_filename = Path(str(_task_value(task, "stored_filename") or "")).name
        if stored_filename:
            paths.append(upload_root / stored_filename)

    for image in image_items_from_meta(raw_meta):
        paths.append(image_path_from_item(image_root, image))
    for image in image_items_from_meta(raw_meta, "page_images"):
        paths.append(image_path_from_item(image_root, image))
    for image in image_items_from_meta(raw_meta, "frames"):
        paths.append(image_path_from_item(image_root, image))
    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_empty_directory(path: Path):
    return cleanup_remove_empty_directory(path)


def _ordered_results(check_items: list[dict], completed: dict[str, dict], partial: dict[str, dict]) -> list[dict]:
    results = []
    for item in check_items:
        result = completed.get(item["code"]) or partial.get(item["code"])
        if result:
            results.append(result)
    return results


def _task_flag(task, key: str) -> bool:
    if hasattr(task, "keys") and key in task.keys():
        return bool(task[key])
    if isinstance(task, dict):
        return bool(task.get(key))
    return False


def _cancel_requested(db, task_id: int) -> bool:
    row = db.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return bool(row and row["cancel_requested"])


def _update_progress(db, task_id: int, progress: int):
    db.execute(
        "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ? AND status = 'running'",
        (progress, now_text(), task_id),
    )
    db.commit()


def _save_intermediate_results(db, task_id: int, results: list[dict], summary: str, progress: int):
    db.execute(
        """
        UPDATE tasks
        SET result_json = ?,
            summary = ?,
            progress = MAX(progress, ?),
            updated_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (json.dumps(results, ensure_ascii=False), summary, progress, now_text(), task_id),
    )
    db.commit()


def _progress_heartbeat(
    app,
    task_id: int,
    stop_event: threading.Event,
    start: int,
    end: int,
    timeout_seconds: int,
):
    if end <= start:
        return
    started_at = time.monotonic()
    climb_seconds = max(60, min(int(timeout_seconds or 300), 300))
    while not stop_event.wait(8):
        elapsed = time.monotonic() - started_at
        ratio = min(1, elapsed / climb_seconds)
        progress = start + int((end - start) * ratio)
        if progress <= start:
            continue
        with app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT status, progress FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            if row["progress"] >= progress:
                continue
            _update_progress(db, task_id, progress)


def _mark_canceled(db, task_id: int):
    db.execute(
        """
        UPDATE tasks
        SET status = 'canceled', progress = 0, updated_at = ?, finished_at = ?
        WHERE id = ?
        """,
        (now_text(), now_text(), task_id),
    )
    db.commit()


def _mark_failed(db, task_id: int, error: str, results: list[dict] | None = None):
    existing = db.execute("SELECT result_json, summary FROM tasks WHERE id = ?", (task_id,)).fetchone()
    result_json = existing["result_json"] if existing else None
    summary = existing["summary"] if existing else None
    if not result_json and results:
        result_json = json.dumps(results, ensure_ascii=False)
        summary = f"已完成 {len(results)} 个检查项，后续检查失败。"
    db.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = ?,
            result_json = ?,
            summary = ?,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (error, result_json, summary, now_text(), now_text(), task_id),
    )
    db.commit()


def _build_summary(results: list[dict]) -> str:
    names = "、".join(item["name"] for item in results)
    return f"已完成 {len(results)} 个检查项：{names}"
