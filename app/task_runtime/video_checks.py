import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..db import get_db
from ..llm import MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK
from ..network import outbound_network_config
from .artifacts import _task_image_folder
from .common import (
    STREAM_SNAPSHOT_INTERVAL_SECONDS,
    STREAM_SNAPSHOT_MIN_CHAR_GROWTH,
    _issue_output_limit,
    _merge_check_results,
    _ordered_results,
    _task_value,
)
from .multimodal_common import (
    IMAGE_DOCUMENT_CONTEXT_MAX_CHARS,
    _check_item_groups,
    _format_multimodal_image_check_result,
    _image_batches,
    _image_check_batch_size,
    _multimodal_image_inputs,
    _split_checkable_image_items,
    _trim_document_context,
)
from .multimodal_protocol import (
    _format_combined_json_result,
    _run_combined_multimodal_check_with_repair,
    _split_combined_check_output,
    _unresolved_check_codes,
)
from .state import (
    TaskCanceled,
    _cancel_requested,
    _progress_heartbeat,
    _save_intermediate_results,
    _task_claim_token,
    _task_flag,
    _update_progress,
)


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
    cancel_event: threading.Event | None = None,
    base_results: list[dict] | None = None,
) -> list[dict]:
    task_id = task["id"]
    claim_token = _task_claim_token(task)
    total = len(check_items)
    checkable_frames, skipped_frames = _split_checkable_image_items(frame_items)
    if not checkable_frames and not skipped_frames:
        raise RuntimeError("没有可检查的视频抽帧画面")

    batches = _image_batches(checkable_frames, _image_check_batch_size())
    check_groups = _check_item_groups(check_items)
    total_units = max(1, len(batches) * max(1, len(check_groups)))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    base_results = list(base_results or [])
    incomplete_codes: set[str] = set()
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = cancel_event or threading.Event()
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
            claim_token,
        ),
        daemon=True,
        name=f"task-video-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        if base_results:
            _save_intermediate_results(
                db,
                task_id,
                base_results,
                f"正在重试 {total} 个失败检查项。",
                5,
                claim_token,
            )
        else:
            _update_progress(db, task_id, 5, claim_token)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            current_results = _ordered_results(
                check_items, completed_by_code, partial_by_code
            )
            snapshot = _merge_check_results(base_results, current_results)
        with save_lock:
            _save_intermediate_results(
                db, task_id, snapshot, summary, progress, claim_token
            )

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

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

            ensure_active()

            app.logger.info(
                "任务视频多模态检查开始 task_id=%s checks=%s groups=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(check_groups),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )
            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not batches:
                progress = mark_unit_completed()
                with result_lock:
                    for item in check_items:
                        structured_report = _merge_video_batch_reports(
                            item["code"],
                            [],
                            skipped_frames=skipped_frames,
                        )
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                [],
                                skipped_images=skipped_frames,
                            ),
                            "structured_report": structured_report,
                            "batch_reports": [],
                            "issue_output_limit": issue_output_limit,
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个视频检查项。",
                    progress,
                )
                return

            for group_index, items in enumerate(check_groups, start=1):
                batch_results_by_code = {item["code"]: [] for item in items}
                last_stream_write = 0.0
                last_stream_chars = 0

                def save_partial(
                    current_batch: dict | None,
                    content: str,
                    summary: str,
                    *,
                    force: bool = False,
                ):
                    nonlocal last_stream_write, last_stream_chars
                    content = content.strip()
                    now = time.monotonic()
                    if not force and content and last_stream_write:
                        if now - last_stream_write < STREAM_SNAPSHOT_INTERVAL_SECONDS:
                            return
                        if (
                            abs(len(content) - last_stream_chars)
                            < STREAM_SNAPSHOT_MIN_CHAR_GROWTH
                        ):
                            return
                    if cancel_event.is_set() or _cancel_requested(
                        db, task_id, claim_token
                    ):
                        raise TaskCanceled

                    with result_lock:
                        sections = (
                            _split_combined_check_output(
                                content, items, fill_missing=False
                            )
                            if content
                            else {}
                        )
                        for item in items:
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
                    last_stream_chars = len(content)
                    save_snapshot(db, summary, current_progress())

                app.logger.info(
                    "任务视频多模态检查组开始 task_id=%s group=%s/%s checks=%s",
                    task_id,
                    group_index,
                    len(check_groups),
                    len(items),
                )
                for batch_index, batch in enumerate(batches, start=1):
                    if cancel_event.is_set() or _cancel_requested(
                        db, task_id, claim_token
                    ):
                        raise TaskCanceled
                    multimodal_images = _multimodal_image_inputs(image_folder, batch)
                    current_batch = {
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "images": batch,
                        "target_label": "视频帧检查",
                        "target_kind": "video_frame",
                    }
                    sections = _run_combined_multimodal_check_with_repair(
                        app,
                        check_items=items,
                        prompt_builder=_combined_video_check_prompt,
                        check_name="视频帧检查合并检查",
                        error_label="视频",
                        preserve_structured=True,
                        run_kwargs={
                            "api_base": task["api_base"],
                            "api_key": task["api_key"],
                            "proxy_mode": network["proxy_mode"],
                            "proxy": network["proxy"],
                            "ssl_verify": network["ssl_verify"],
                            "request_timeout": task["request_timeout"],
                            "model_name": task["model_name"],
                            "reasoning_effort": _task_value(task, "reasoning_effort"),
                            "force_disable_thinking": _task_flag(
                                task, "force_disable_thinking"
                            ),
                            "document_text": _document_text_for_video_batch(
                                document_text, batch, document_meta or {}
                            ),
                            "image_items": multimodal_images,
                            "batch_index": batch_index,
                            "batch_count": len(batches),
                            "issue_output_limit": issue_output_limit,
                            "output_contract": MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK,
                            "on_content": lambda content, current=current_batch: (
                                save_partial(
                                    current,
                                    content,
                                    f"正在进行视频帧检查：批次 {current['batch_index']}/{current['batch_count']}",
                                )
                            ),
                            "task_id": task_id,
                            "stream_trace_enabled": stream_trace_enabled,
                            "cancel_event": cancel_event,
                            "check_canceled": ensure_active,
                        },
                    )
                    with result_lock:
                        incomplete_codes.update(
                            _unresolved_check_codes(sections, items)
                        )
                    for item in items:
                        structured_report = sections.get(item["code"], {})
                        batch_results_by_code[item["code"]].append(
                            {
                                "batch_index": batch_index,
                                "batch_count": len(batches),
                                "images": batch,
                                "target_label": "视频帧检查",
                                "target_kind": "video_frame",
                                "content": _format_combined_json_result(
                                    item, structured_report
                                ),
                                "structured_report": structured_report,
                            }
                        )
                    progress = mark_unit_completed()
                    save_partial(
                        None,
                        "",
                        f"已完成视频帧检查：检查组 {group_index}/{len(check_groups)}，批次 {batch_index}/{len(batches)}",
                        force=True,
                    )
                    save_snapshot(
                        db,
                        f"正在进行视频检查，已完成 {completed_units}/{total_units} 个检查组批次。",
                        progress,
                    )

                with result_lock:
                    for item in items:
                        batch_results = batch_results_by_code[item["code"]]
                        structured_report = _merge_video_batch_reports(
                            item["code"],
                            batch_results,
                            skipped_frames=skipped_frames,
                        )
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                batch_results,
                                skipped_images=skipped_frames,
                            ),
                            "structured_report": structured_report,
                            "batch_reports": _video_batch_report_snapshots(
                                batch_results
                            ),
                            "issue_output_limit": issue_output_limit,
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个视频检查项。",
                    current_progress(),
                )
                app.logger.info(
                    "任务视频多模态检查组完成 task_id=%s group=%s/%s checks=%s",
                    task_id,
                    group_index,
                    len(check_groups),
                    len(items),
                )

            app.logger.info(
                "任务视频多模态检查完成 task_id=%s checks=%s groups=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(check_groups),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )

    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"task-video-check-{task_id}"
    )
    futures = []
    try:
        futures = [executor.submit(run_checks)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分视频检查项未完成")
        if incomplete_codes:
            raise RuntimeError(
                f"部分视频检查项补偿后仍未返回有效结果：{','.join(sorted(incomplete_codes))}"
            )
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
            f"- code: {item['code']}\n"
            f"  name: {item['name']}\n"
            f"  专项要求: {str(item.get('prompt') or '').strip()}"
        )
    return (
        f"本次执行硬件产品安装调测视频质检，一次请求中合并 {len(check_items)} 个检查项。"
        "请对每个检查项独立判断，不要把其他检查项的结论混入当前检查项。\n\n"
        "检查对象说明：系统只提供从视频时间轴均匀抽取的采样帧，模型不能直接观看完整连续视频。"
        "请重点观察安装顺序、接线端子、安全防护、调测界面/参数、视频清晰度和关键步骤完整性。\n\n"
        "定位要求：引用画面时优先使用视频时间点（例如 00:12.300），必要时补充帧文件名；不要使用 PDF 页码定位。\n"
        "证据约束：只依据当前提供的采样帧和视频上下文判断；看不清、被遮挡、动作连续性证据不足或缺少产品图纸依据时放入“需人工确认”，不要编造不可见内容。\n"
        "汇总约束：status=issue 只放已确认异常；证据不足放 status=suggestion；正常、未发现、符合、清晰、完整、无需修改等结论不得标记为 issue。\n"
        "完整性要求：results 必须包含下列每个 code，且每个 code 只出现一次。\n\n"
        "检查项定义：\n"
        f"{chr(10).join(item_blocks)}"
    )


def _document_text_for_video_batch(
    document_text: str, frame_items: list[dict], document_meta: dict
) -> str:
    text = _trim_document_context(
        str(document_text or "").strip(), IMAGE_DOCUMENT_CONTEXT_MAX_CHARS
    )
    selection = (
        document_meta.get("frame_selection") if isinstance(document_meta, dict) else {}
    )
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
        skipped_frame_count = int(selection.get("skipped_frame_count") or 0)
        if skipped_frame_count:
            selection_lines.append(f"- 已跳过无法解码的采样点：{skipped_frame_count}")
        if selection.get("strategy"):
            selection_lines.append(f"- 抽帧策略：{selection.get('strategy')}")

    parts = []
    if text:
        parts.append(text)
    if selection_lines:
        parts.append("video_sampling:\n" + "\n".join(selection_lines))
    parts.append(
        "current_batch_video_frames:\n"
        + ("\n".join(frame_lines) if frame_lines else "- 未记录视频帧")
    )
    return "\n\n".join(parts).strip()


def _merge_video_batch_reports(
    result_code: str,
    batch_results: list[dict],
    *,
    skipped_frames: list[dict] | None = None,
) -> dict:
    merged_items: list[dict] = []
    items_by_key: dict[str, dict] = {}
    for batch in batch_results:
        report = batch.get("structured_report")
        if not isinstance(report, dict):
            continue
        raw_items = report.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            item = _normalize_video_report_item(raw_item)
            if not item or _video_report_item_is_generic_non_issue(item):
                continue
            inferred_refs = _video_evidence_refs_for_item(item, batch)
            item["evidence_refs"] = _merge_video_evidence_refs(
                item.get("evidence_refs"),
                inferred_refs,
            )
            evidence_location = _video_evidence_location(item["evidence_refs"])
            if evidence_location:
                item["location"] = evidence_location
            key = _video_report_item_merge_key(item)
            existing = items_by_key.get(key)
            if existing is None:
                items_by_key[key] = item
                merged_items.append(item)
            else:
                _merge_video_report_item(existing, item)

    for frame in skipped_frames or []:
        item = _skipped_video_frame_report_item(frame)
        key = _video_report_item_merge_key(item)
        if key not in items_by_key:
            items_by_key[key] = item
            merged_items.append(item)

    for item in merged_items:
        evidence_location = _video_evidence_location(item.get("evidence_refs"))
        if evidence_location:
            item["location"] = evidence_location
        item["id"] = _video_report_item_id(result_code, item)
    merged_items.sort(key=_video_report_item_priority)

    issue_count = sum(1 for item in merged_items if item.get("status") == "issue")
    suggestion_count = sum(
        1 for item in merged_items if item.get("status") == "suggestion"
    )
    if issue_count or suggestion_count:
        summary = f"视频检查形成 {issue_count} 个明确问题、{suggestion_count} 个需人工确认项。"
    else:
        summary = "未发现明确问题或需人工确认项。"
    return {"summary": summary, "items": merged_items}


def _normalize_video_report_item(raw_item) -> dict:
    if isinstance(raw_item, str):
        raw_item = {"status": "suggestion", "description": raw_item}
    if not isinstance(raw_item, dict):
        return {}
    status = str(raw_item.get("status") or "suggestion").strip().lower()
    if status not in {"issue", "suggestion", "non_issue"}:
        status = "suggestion"
    severity = str(raw_item.get("severity") or "").strip().lower()
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "medium" if status == "issue" else "low"
    confidence = str(raw_item.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium" if status == "issue" else "low"
    item = {
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "category": str(raw_item.get("category") or "").strip(),
        "location": str(raw_item.get("location") or "").strip(),
        "excerpt": str(raw_item.get("excerpt") or "").strip(),
        "description": str(raw_item.get("description") or "").strip(),
        "impact": str(raw_item.get("impact") or "").strip(),
        "suggestion": str(raw_item.get("suggestion") or "").strip(),
        "evidence_refs": _normalize_video_evidence_refs(raw_item.get("evidence_refs")),
    }
    if not any(
        item.get(field)
        for field in (
            "category",
            "location",
            "excerpt",
            "description",
            "impact",
            "suggestion",
        )
    ):
        return {}
    return item


def _video_report_item_is_generic_non_issue(item: dict) -> bool:
    if item.get("status") != "non_issue":
        return False
    text = "".join(
        str(item.get(field) or "")
        for field in ("category", "excerpt", "description", "impact", "suggestion")
    )
    compact = re.sub(r"\s+", "", text)
    return not compact or any(
        marker in compact
        for marker in (
            "未发现",
            "无明显",
            "未见",
            "正常",
            "符合",
            "一致",
            "清晰",
            "完整",
            "无需修改",
            "无异常",
        )
    )


def _video_evidence_refs_for_item(item: dict, batch: dict) -> list[dict]:
    frames = batch.get("images") or []
    searchable = "\n".join(
        str(item.get(field) or "") for field in ("location", "excerpt", "description")
    )
    refs = []
    for frame in frames:
        filename = str(frame.get("filename") or "").strip()
        frame_id = str(frame.get("id") or "").strip()
        position = str(frame.get("position") or "").strip()
        if not any(
            value and value in searchable for value in (filename, frame_id, position)
        ):
            continue
        refs.append(_video_evidence_ref(frame))
    if not refs and len(frames) == 1:
        refs.append(_video_evidence_ref(frames[0]))
    return [ref for ref in refs if ref]


def _video_evidence_ref(frame: dict) -> dict:
    frame_id = str(frame.get("id") or "").strip()
    filename = str(frame.get("filename") or "").strip()
    if not frame_id and not filename:
        return {}
    position = str(frame.get("position") or "").strip()
    timestamp_seconds = _video_timestamp_seconds(
        frame.get("timestamp_seconds"), position
    )
    return {
        "id": frame_id or filename,
        "filename": filename,
        "position": position,
        "timestamp_seconds": timestamp_seconds,
        "relative_path": str(frame.get("relative_path") or "").strip(),
        "mime_type": str(frame.get("mime_type") or "image/jpeg").strip(),
        "kind": "video_frame",
    }


def _normalize_video_evidence_refs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    refs = []
    for raw_ref in value:
        if not isinstance(raw_ref, dict):
            continue
        ref = _video_evidence_ref(raw_ref)
        if ref:
            refs.append(ref)
    return _merge_video_evidence_refs(refs, [])


def _merge_video_evidence_refs(left, right) -> list[dict]:
    merged = []
    seen = set()
    for raw_ref in list(left or []) + list(right or []):
        if not isinstance(raw_ref, dict):
            continue
        ref = _video_evidence_ref(raw_ref)
        if not ref:
            continue
        key = ref.get("id") or ref.get("filename") or ref.get("position")
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
    merged.sort(
        key=lambda ref: (
            ref.get("timestamp_seconds") is None,
            float(ref.get("timestamp_seconds") or 0),
            str(ref.get("filename") or ""),
        )
    )
    return merged


def _video_timestamp_seconds(value, position: str = "") -> float | None:
    if value is not None and not isinstance(value, bool):
        try:
            return round(max(0.0, float(value)), 3)
        except (TypeError, ValueError):
            pass
    match = re.search(
        r"(?:(\d{1,2}):)?(\d{2}):(\d{2})(?:\.(\d{1,3}))?", str(position or "")
    )
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    millis = int((match.group(4) or "0").ljust(3, "0")[:3])
    return round(hours * 3600 + minutes * 60 + seconds + millis / 1000, 3)


def _video_evidence_location(refs) -> str:
    positions = []
    for ref in refs or []:
        position = str(ref.get("position") or "").strip()
        if position and position not in positions:
            positions.append(position)
    if not positions:
        return ""
    return "视频时间 " + "、".join(positions)


def _video_report_item_merge_key(item: dict) -> str:
    category = _normalize_video_issue_key_text(item.get("category"))
    description = _normalize_video_issue_key_text(
        item.get("description")
        or item.get("excerpt")
        or item.get("suggestion")
        or item.get("impact")
        or item.get("location")
    )
    return f"{category}\n{description}"


def _normalize_video_issue_key_text(value) -> str:
    text = str(value or "").lower()
    text = re.sub(r"(?:视频时间\s*)?(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?", "", text)
    text = re.sub(r"\b\d{4}_t\d+\.(?:jpg|jpeg|png)\b", "", text)
    return re.sub(r"[\s，。；、,.!！?？:：;；/\\|()（）【】\[\]\"'“”‘’_-]+", "", text)


def _merge_video_report_item(target: dict, source: dict) -> None:
    status_order = {"issue": 0, "suggestion": 1, "non_issue": 2}
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    if status_order.get(source.get("status"), 9) < status_order.get(
        target.get("status"), 9
    ):
        target["status"] = source.get("status")
    if severity_order.get(source.get("severity"), 9) < severity_order.get(
        target.get("severity"), 9
    ):
        target["severity"] = source.get("severity")
    if confidence_order.get(source.get("confidence"), 9) < confidence_order.get(
        target.get("confidence"), 9
    ):
        target["confidence"] = source.get("confidence")
    for field in ("category", "excerpt", "description", "impact", "suggestion"):
        target[field] = _merge_video_report_field(target.get(field), source.get(field))
    target["evidence_refs"] = _merge_video_evidence_refs(
        target.get("evidence_refs"),
        source.get("evidence_refs"),
    )


def _merge_video_report_field(left, right) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text or right_text == left_text or right_text in left_text:
        return left_text
    if left_text in right_text:
        return right_text
    return f"{left_text}；{right_text}"


def _video_report_item_id(result_code: str, item: dict) -> str:
    source = "\n".join(
        (
            str(result_code or ""),
            _normalize_video_issue_key_text(item.get("category")),
            _normalize_video_issue_key_text(
                item.get("description")
                or item.get("excerpt")
                or item.get("suggestion")
                or item.get("impact")
                or item.get("location")
            ),
        )
    )
    return hashlib.sha1(source.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _video_report_item_priority(item: dict) -> tuple[int, int, int, str]:
    return (
        {"issue": 0, "suggestion": 1, "non_issue": 2}.get(
            str(item.get("status") or ""), 9
        ),
        {"high": 0, "medium": 1, "low": 2}.get(str(item.get("confidence") or ""), 9),
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
            str(item.get("severity") or ""), 9
        ),
        str(item.get("id") or ""),
    )


def _skipped_video_frame_report_item(frame: dict) -> dict:
    ref = _video_evidence_ref(frame)
    filename = str(frame.get("filename") or frame.get("id") or "视频帧")
    position = str(frame.get("position") or "").strip()
    return {
        "status": "suggestion",
        "severity": "low",
        "confidence": "high",
        "category": "视频帧读取",
        "location": f"视频时间 {position}" if position else "",
        "excerpt": filename,
        "description": f"视频帧 {filename} 不是可识别的图片格式，系统已跳过。",
        "impact": "该时间点未参与模型检查，可能影响视频检查完整性。",
        "suggestion": "请人工回看对应时间点，或将视频转换为受支持格式后重新检查。",
        "evidence_refs": [ref] if ref else [],
    }


def _video_batch_report_snapshots(batch_results: list[dict]) -> list[dict]:
    snapshots = []
    for batch in batch_results:
        frames = []
        for frame in batch.get("images") or []:
            ref = _video_evidence_ref(frame)
            if ref:
                frames.append(ref)
        report = batch.get("structured_report")
        snapshots.append(
            {
                "batch_index": int(batch.get("batch_index") or 1),
                "batch_count": int(batch.get("batch_count") or 1),
                "frames": frames,
                "structured_report": report
                if isinstance(report, dict)
                else {"summary": "", "items": []},
            }
        )
    return snapshots
