import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.infrastructure.network import outbound_network_config
from app.models.client import MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK
from app.persistence.connection import get_db
from app.tasks.runtime.artifacts import _task_image_folder
from app.tasks.runtime.common import (
    STREAM_SNAPSHOT_INTERVAL_SECONDS,
    STREAM_SNAPSHOT_MIN_CHAR_GROWTH,
    _issue_output_limit,
    _merge_check_results,
    _ordered_results,
    _task_value,
)
from app.tasks.runtime.multimodal_common import (
    _check_item_groups,
    _document_text_for_image_batch,
    _format_multimodal_image_check_result,
    _image_batches,
    _image_check_batch_size,
    _multimodal_image_inputs,
    _split_checkable_image_items,
)
from app.tasks.runtime.multimodal_protocol import (
    _run_combined_multimodal_check_with_repair,
    _split_combined_check_output,
    _unresolved_check_codes,
)
from app.tasks.runtime.state import (
    TaskCanceled,
    _cancel_requested,
    _progress_heartbeat,
    _save_intermediate_results,
    _task_claim_token,
    _task_flag,
    _update_progress,
)

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
    cancel_event: threading.Event | None = None,
    base_results: list[dict] | None = None,
) -> list[dict]:
    task_id = task["id"]
    claim_token = _task_claim_token(task)
    total = len(check_items)
    groups = _image_check_groups(
        check_items, image_items, page_image_items, document_meta or {}
    )
    if not groups:
        raise RuntimeError("没有可检查的 PDF 页面截图或图片资源")
    total_units = max(1, sum(max(1, len(group["batches"])) for group in groups))
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
        name=f"task-image-heartbeat-{task_id}",
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

    def run_group(group_index: int, group: dict):
        with app.app_context():
            db = get_db()

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

            ensure_active()

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
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

                with result_lock:
                    sections = (
                        _split_combined_check_output(content, items, fill_missing=False)
                        if content
                        else {}
                    )
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
                last_stream_chars = len(content)
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
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled
                multimodal_images = _multimodal_image_inputs(image_folder, batch)
                batch_document_text = _document_text_for_image_batch(
                    document_text, batch
                )
                current_batch = {
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "images": batch,
                    "target_label": target_label,
                    "target_kind": group["target_kind"],
                }

                sections = _run_combined_multimodal_check_with_repair(
                    app,
                    check_items=items,
                    prompt_builder=lambda selected_items: _combined_image_check_prompt(
                        selected_items,
                        group["target_kind"],
                        target_label,
                    ),
                    check_name=f"{target_label}合并检查",
                    error_label=target_label,
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
                        "document_text": batch_document_text,
                        "image_items": multimodal_images,
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "issue_output_limit": issue_output_limit,
                        "output_contract": MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK,
                        "on_content": lambda content, current=current_batch: (
                            save_partial(
                                current,
                                content,
                                f"正在进行{target_label}：批次 {current['batch_index']}/{current['batch_count']}",
                            )
                        ),
                        "task_id": task_id,
                        "stream_trace_enabled": stream_trace_enabled,
                        "cancel_event": cancel_event,
                        "check_canceled": ensure_active,
                    },
                )
                with result_lock:
                    incomplete_codes.update(_unresolved_check_codes(sections, items))
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

    executor = ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, len(groups))),
        thread_name_prefix=f"task-image-check-{task_id}",
    )
    futures = []
    try:
        futures = [
            executor.submit(run_group, index, group)
            for index, group in enumerate(groups, start=1)
        ]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分图文检查项未完成")
        if incomplete_codes:
            raise RuntimeError(
                f"部分图片检查项补偿后仍未返回有效结果：{','.join(sorted(incomplete_codes))}"
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
        manual_notes = _image_check_manual_notes(
            target_kind, document_meta, fallback_used
        )
        for item_group in _check_item_groups(items):
            groups.append(
                {
                    "target_kind": target_kind,
                    "label": IMAGE_CHECK_TARGET_LABELS[target_kind],
                    "items": item_group,
                    "checkable_images": checkable_images,
                    "skipped_images": skipped_images,
                    "manual_notes": manual_notes,
                    "batches": _image_batches(
                        checkable_images, _image_check_batch_size()
                    ),
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


def _image_check_manual_notes(
    target_kind: str, document_meta: dict, fallback_used: bool
) -> list[str]:
    notes = []
    if target_kind == "page":
        selection = (
            document_meta.get("page_selection")
            if isinstance(document_meta, dict)
            else None
        )
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
        image_error = (
            str(document_meta.get("image_extraction_error") or "").strip()
            if isinstance(document_meta, dict)
            else ""
        )
        if image_error:
            notes.append(
                f"PDF 内嵌图片提取异常，图片资源类检查可能未覆盖全部原始图片：{image_error}"
            )
    if fallback_used:
        notes.append(
            "当前检查对象缺少首选图片源，已回退使用另一类 PDF 图像；细节判断需要人工确认。"
        )
    return notes


def _combined_image_check_prompt(
    check_items: list[dict], target_kind: str, target_label: str
) -> str:
    target_instruction = (
        "本组检查对象是 PDF 整页截图。请重点观察页面中的正文、图、表、标题、页眉页脚、遮挡、裁切、版式和上下文对应关系。"
        if target_kind == "page"
        else "本组检查对象是从 PDF 中提取的内嵌图片资源。请重点观察图片自身的文字、接线、图形元素、标注和可读性。"
    )
    item_blocks = []
    for item in check_items:
        item_blocks.append(
            f"- code: {item['code']}\n"
            f"  name: {item['name']}\n"
            f"  专项要求: {str(item.get('prompt') or '').strip()}"
        )
    return (
        f"本次执行{target_label}，一次请求中合并 {len(check_items)} 个检查项。请对每个检查项独立判断，"
        "不要把其他检查项的结论混入当前检查项。\n\n"
        f"{target_instruction}\n\n"
        "定位要求：引用图片时优先使用 PDF 页码（例如“第12页”），同一页有多张图时再补充图片编号/位置；无法识别页码时使用图片编号或原始位置。\n"
        "汇总约束：status=issue 只放已确认异常；证据不足放 status=suggestion；正常、未发现、符合、一致、清晰、完整等结论不得标记为 issue。\n"
        "证据约束：只能依据本次提供的 PDF 页面/图片和文档上下文；看不清、证据不足、跨页缺上下文时放入“需人工确认”，不要编造不可见内容。\n\n"
        "完整性要求：results 必须包含下列每个 code，且每个 code 只出现一次。\n\n"
        "检查项定义：\n"
        f"{chr(10).join(item_blocks)}"
    )
