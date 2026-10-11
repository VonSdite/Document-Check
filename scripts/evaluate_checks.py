"""在同一人工标注样本上执行整文检查和证据审查。"""

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from app.checks.evaluation import annotation_template, score_runs
from app.checks.guardrails import (
    build_pdf_table_evidence_index,
    sanitize_text_check_result,
)
from app.checks.language_consistency import compose_language_consistency_text
from app.contracts.agent import AgentOptions
from app.documents.extraction import extract_document, format_document_text
from app.documents.extraction.common import DocumentReadError
from app.documents.index import DocumentIndex
from app.models.client import LLMError, run_check
from app.persistence.checkpoints import MemoryCheckpointStore
from app.reporting.service import (
    _normalize_report_acceptance,
    _normalize_structured_report_payload,
    _result_report_items,
    _result_structured_report,
)
from app.tasks.runtime.evidence_agent import EvidenceAgent
from app.tasks.runtime.preprocessing import (
    _format_consistency_document_text,
    _source_document,
)


def read_rows(path):
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("评测输入需要 JSON 数组或 JSONL 对象")
    return rows


def write_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_documents(case, case_folder):
    documents = []
    for item in case["documents"]:
        if item.get("path"):
            path = (case_folder / item["path"]).resolve()
            blocks = []
            text, _ = extract_document(
                path, path.suffix.lstrip(".").lower(), blocks=blocks
            )
            documents.append(
                _source_document(
                    item.get("filename", path.name),
                    path.suffix.lstrip("."),
                    text,
                    blocks,
                    item.get("role", "document"),
                )
            )
        else:
            documents.append(
                {
                    "filename": item["filename"],
                    "role": item.get("role", "document"),
                    "text": item["text"],
                    "blocks": item.get("blocks", []),
                    "text_complete": item.get("text_complete", True),
                    "limitations": item.get("limitations", []),
                }
            )
    return documents


def validate_cases(cases):
    ids = [case.get("id") for case in cases]
    if any(
        not isinstance(case_id, str) or not case_id.strip() for case_id in ids
    ) or len(set(ids)) != len(ids):
        raise ValueError("评测样本需要唯一的非空 ID")
    for case in cases:
        if not case.get("documents") or not isinstance(case.get("check"), dict):
            raise ValueError(f"样本 {case['id']} 需要文档列表和检查项")
        if not all(
            isinstance(case["check"].get(key), str) and case["check"][key].strip()
            for key in ("code", "name", "prompt")
        ):
            raise ValueError(f"样本 {case['id']} 的检查项需要 code、name、prompt")


def baseline_text(documents, task_type):
    if task_type == "language_consistency_check":
        by_role = {d.get("role"): d for d in documents}
        if set(by_role) != {"document_a", "document_b"} or len(documents) != 2:
            raise ValueError("跨语种样本需要文档A与文档B")
        files = [
            {
                **by_role[role],
                "original_filename": by_role[role]["filename"],
                "file_type": Path(by_role[role]["filename"]).suffix.lstrip(".")
                or "txt",
            }
            for role in ("document_a", "document_b")
        ]
        return compose_language_consistency_text(*files)[0]
    if task_type == "consistency_check":
        groups = []
        for role, label in (("master", "素材文档"), ("related", "资料文档")):
            files = [
                {**d, "original_filename": d["filename"]}
                for d in documents
                if d.get("role") == role
            ]
            if not files:
                raise ValueError("多文档样本需要素材文档和资料文档")
            groups.append({"label": label, "files": files})
        return _format_consistency_document_text(groups)
    return "\n\n".join(
        format_document_text(doc["filename"], doc["text"]) for doc in documents
    )


def run_case(case, variant, connection, case_folder):
    documents = load_documents(case, case_folder)
    index = DocumentIndex(documents)
    item = case["check"]
    started = time.monotonic()
    if variant == "agent":
        options = AgentOptions.model_validate(item.get("agent_options", {}))
        index = index.with_standards(
            [s.model_dump(mode="json") for s in options.standards]
        )
        result = EvidenceAgent(
            index=index,
            item=item,
            task_type=case.get("task_type", "document_check"),
            connection=connection,
            options=options,
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        ).run()
        return {
            "case_id": case["id"],
            "variant": variant,
            "report": result["report"],
            "metadata": result["metadata"],
            "error": result["error"],
        }
    document_text = baseline_text(documents, case.get("task_type", "document_check"))
    options = AgentOptions.model_validate(item.get("agent_options", {}))
    for standard in options.standards:
        document_text += (
            f"\n\n参考标准：{standard.name} / 版本 {standard.version}\n{standard.text}"
        )
    calls = 0
    usages = {}

    def record(event):
        nonlocal calls
        if event["kind"] == "start":
            calls += 1

    def record_usage(usage, request_id, attempt):
        if all(
            isinstance(usage.get(key), int)
            and usage[key] >= 0
            and not isinstance(usage[key], bool)
            for key in ("prompt_tokens", "completion_tokens")
        ):
            usages[(request_id, attempt)] = usage

    error, report = "", {"summary": "检查未完成", "items": []}
    try:
        if len(document_text) > connection["max_input_chars"]:
            raise LLMError("整文检查输入超过模型文本上限")
        content = run_check(
            **{k: v for k, v in connection.items() if k != "max_input_chars"},
            check_name=item["name"],
            prompt=item["prompt"],
            document_text=document_text,
            on_output=record,
            on_usage=record_usage,
            **(
                {"max_completion_tokens": None}
                if case.get("task_type", "document_check") == "document_check"
                else {}
            ),
        )
        content, _ = sanitize_text_check_result(
            content, pdf_table_evidence=build_pdf_table_evidence_index(document_text)
        )
        normalized = _normalize_structured_report_payload(content)
        items = _result_report_items(
            {"code": item["code"], "result": content}, normalized
        )
        report = {
            "summary": normalized.get("summary", "") if normalized else "",
            "items": [
                {**row, "status": row["type"]}
                for row in items
                if row["type"] in {"issue", "suggestion"}
            ],
        }
    except LLMError as exc:
        error = str(exc)
    known_input = sum(usage["prompt_tokens"] for usage in usages.values())
    known_output = sum(usage["completion_tokens"] for usage in usages.values())
    cost = None
    if (
        len(usages) == calls
        and options.input_price is not None
        and options.output_price is not None
    ):
        cost = (
            known_input * options.input_price + known_output * options.output_price
        ) / 1000000
    return {
        "case_id": case["id"],
        "variant": variant,
        "report": report,
        "error": error,
        "metadata": {
            "elapsed_seconds": time.monotonic() - started,
            "model_calls": calls,
            "usage_complete": len(usages) == calls,
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "cost": cost,
            "currency": options.currency,
        },
    }


def run_case_safely(case, variant, connection, case_folder):
    started = time.monotonic()
    try:
        return run_case(case, variant, connection, case_folder)
    except (
        LLMError,
        DocumentReadError,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
    ) as exc:
        return {
            "case_id": case["id"],
            "variant": variant,
            "report": {"summary": "检查未完成", "items": []},
            "metadata": {
                "elapsed_seconds": time.monotonic() - started,
                "usage_complete": False,
            },
            "error": str(exc),
        }


def export_feedback(database, *, task_ids=None, limit=100):
    """从只读数据库导出复核样本，用户认可状态保留为待复核线索。"""
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    if limit <= 0:
        raise ValueError("导出任务数量需要大于零")
    cases, runs, skipped = [], [], []
    predicates = [
        "task_type IN ('document_check', 'consistency_check', 'language_consistency_check')",
        "status IN ('completed', 'partial', 'failed', 'canceled')",
    ]
    parameters = []
    if task_ids:
        predicates.append("id IN (" + ",".join("?" for _ in task_ids) + ")")
        parameters.extend(task_ids)
    query = (
        "SELECT id, task_type, original_filename, document_meta_json, checks_snapshot_json, result_json "
        "FROM tasks WHERE " + " AND ".join(predicates) + " ORDER BY id DESC LIMIT ?"
    )
    parameters.append(limit)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for task in db.execute(query, parameters):
            try:
                meta = json.loads(task["document_meta_json"] or "{}")
                snapshots = json.loads(task["checks_snapshot_json"] or "[]")
                results = json.loads(task["result_json"] or "[]")
                documents = []
                for document in meta.get("source_documents", []):
                    documents.append(
                        {
                            key: document[key]
                            for key in (
                                "filename",
                                "role",
                                "text",
                                "blocks",
                                "text_complete",
                                "limitations",
                            )
                            if key in document
                        }
                    )
                if not documents or not all(
                    d.get("text", "").strip() for d in documents
                ):
                    skipped.append(
                        {"task_id": task["id"], "reason": "文档来源文本未保留"}
                    )
                    continue
                snapshots_by_code = {item["code"]: item for item in snapshots}
                for result in results:
                    snapshot = snapshots_by_code.get(result["code"])
                    if not snapshot or result["code"] in {
                        "common-terms",
                        "sensitive-terms",
                        "hyperlink-validity",
                    }:
                        continue
                    structured = _result_structured_report(result)
                    items = _result_report_items(result, structured)
                    report = {
                        "summary": structured.get("summary", "") if structured else "",
                        "items": [
                            {**item, "status": item["type"]}
                            for item in items
                            if item["type"] in {"issue", "suggestion"}
                        ],
                        "pending_items": structured.get("pending_items", [])
                        if structured
                        else [],
                        "withdrawn_items": structured.get("withdrawn_items", [])
                        if structured
                        else [],
                    }
                    case_id = f"task-{task['id']}-{result['code']}"
                    cases.append(
                        {
                            "id": case_id,
                            "task_type": task["task_type"],
                            "documents": documents,
                            "check": {
                                key: snapshot[key]
                                for key in ("code", "name", "prompt", "agent_options")
                                if key in snapshot
                            },
                            "expected_issues": [],
                            "gold_complete": False,
                            "provenance": {
                                "task_id": task["id"],
                                "check_code": result["code"],
                            },
                        }
                    )
                    runs.append(
                        {
                            "case_id": case_id,
                            "variant": "production_agent"
                            if result.get("agent")
                            else "production_standard",
                            "report": report,
                            "metadata": {
                                key: result.get("agent", {}).get(key)
                                for key in (
                                    "elapsed_seconds",
                                    "model_calls",
                                    "usage_complete",
                                    "known_input_tokens",
                                    "known_output_tokens",
                                    "budget_tokens",
                                    "cost",
                                    "currency",
                                )
                            },
                            "error": result.get("error", ""),
                            "feedback": {
                                item["id"]: _normalize_report_acceptance(
                                    result.get("item_acceptances", {}).get(item["id"])
                                )
                                for item in report["items"]
                            },
                        }
                    )
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                skipped.append(
                    {"task_id": task["id"], "reason": f"任务来源或结果不完整：{exc}"}
                )
    annotations = annotation_template(runs)
    run_map = {(run["case_id"], run["variant"]): run for run in runs}
    for annotation in annotations:
        feedback = run_map[(annotation["case_id"], annotation["variant"])][
            "feedback"
        ].get(annotation["item_id"])
        if feedback:
            annotation["feedback_hint"] = feedback
    return {
        "cases": cases,
        "runs": runs,
        "annotations": annotations,
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="执行同一组文档的对照检查")
    run_parser.add_argument("--cases", required=True)
    run_parser.add_argument(
        "--variant", choices=("agent", "standard", "both"), default="both"
    )
    run_parser.add_argument("--api-base", required=True)
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--api-key-env", default="DOCUMENTCHECK_EVAL_API_KEY")
    run_parser.add_argument("--max-input-chars", type=int, default=80000)
    run_parser.add_argument("--timeout", type=int, default=3600)
    run_parser.add_argument("--ssl-verify", action="store_true")
    run_parser.add_argument("--output", required=True)
    feedback_parser = commands.add_parser(
        "export-feedback", help="只读导出已保留文档与人工反馈，生成待标注样本"
    )
    feedback_parser.add_argument("--database", required=True)
    feedback_parser.add_argument("--task-id", type=int, action="append")
    feedback_parser.add_argument("--limit", type=int, default=100)
    feedback_parser.add_argument("--output-dir", required=True)
    score_parser = commands.add_parser("score", help="根据人工复核标注计算质量指标")
    for name in ("cases", "runs", "annotations", "output"):
        score_parser.add_argument("--" + name, required=True)
    score_parser.add_argument("--min-precision", type=float)
    score_parser.add_argument("--min-critical-recall", type=float)
    score_parser.add_argument("--max-p95-seconds", type=float)
    args = parser.parse_args()
    if args.command == "export-feedback":
        exported = export_feedback(
            args.database, task_ids=args.task_id, limit=args.limit
        )
        folder = Path(args.output_dir)
        for name, rows in exported.items():
            write_json(folder / f"{name}.json", rows)
        print(
            f"导出 {len(exported['cases'])} 个样本，跳过 {len(exported['skipped'])} 个任务；请复核问题和反馈并完成 gold_complete 标记。"
        )
        return 0
    if args.command == "score":
        for value in (args.min_precision, args.min_critical_recall):
            if value is not None and not 0 <= value <= 1:
                parser.error("准确率和关键问题召回率门槛需要在 0 到 1 之间")
        if args.max_p95_seconds is not None and not 0 < args.max_p95_seconds < float(
            "inf"
        ):
            parser.error("P95 耗时门槛需要是有限的正数")
    if args.command == "run":
        cases = read_rows(args.cases)
        validate_cases(cases)
        if args.max_input_chars <= 0 or args.timeout <= 0:
            parser.error("模型输入上限和超时需要大于零")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        connection = {
            "api_base": args.api_base,
            "api_key": os.environ.get(args.api_key_env),
            "model_name": args.model,
            "max_input_chars": args.max_input_chars,
            "request_timeout": args.timeout,
            "ssl_verify": args.ssl_verify,
        }
        variants = ("standard", "agent") if args.variant == "both" else (args.variant,)
        runs = []
        with output.open("w", encoding="utf-8", newline="\n") as target:
            for case in cases:
                for variant in variants:
                    result = run_case_safely(
                        case, variant, connection, Path(args.cases).resolve().parent
                    )
                    runs.append(result)
                    target.write(json.dumps(result, ensure_ascii=False) + "\n")
                    target.flush()
                    print(
                        f"{case['id']} / {variant}：{'未完成' if result['error'] else '完成'}"
                    )
        write_json(output.with_suffix(".annotations.json"), annotation_template(runs))
        return 0
    scores = score_runs(
        read_rows(args.cases), read_rows(args.runs), read_rows(args.annotations)
    )
    write_json(args.output, scores)
    print(json.dumps(scores, ensure_ascii=False, indent=2))
    agent = scores.get("agent", {})
    if args.max_p95_seconds is not None and (
        agent.get("run_success_rate") != 1
        or agent.get("latency_measurement_coverage") != 1
    ):
        print("验收未通过：P95 耗时需要全部执行成功及完整的耗时记录。")
        return 1
    if any(
        threshold is not None
        for threshold in (args.min_precision, args.min_critical_recall)
    ):
        if (
            not agent.get("gold_review_complete")
            or agent.get("run_success_rate") != 1
            or agent.get("issue_review_coverage") not in (None, 1)
        ):
            print("验收未通过：需要完整人工标注、所有执行成功及全部已报告问题的复核。")
            return 1
    thresholds = (
        ("precision_on_reviewed", args.min_precision, True),
        ("critical_recall", args.min_critical_recall, True),
        ("latency_p95_seconds", args.max_p95_seconds, False),
    )
    for key, threshold, minimum in thresholds:
        if threshold is not None:
            value = agent.get(key)
            if value is None or (value < threshold if minimum else value > threshold):
                print(f"验收未通过：{key}，结果={value}，门槛={threshold}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
