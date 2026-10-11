"""人工标注驱动的检查质量评估。"""

from collections import Counter
from statistics import mean


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _percentile(values, percentile):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def score_runs(cases: list[dict], runs: list[dict], annotations: list[dict]) -> dict:
    case_map = {case["id"]: case for case in cases}
    if len(case_map) != len(cases):
        raise ValueError("评测样本 ID 重复")
    gold = {
        (case["id"], issue["id"]): issue
        for case in cases
        for issue in case.get("expected_issues", [])
    }
    if len(gold) != sum(len(case.get("expected_issues", [])) for case in cases):
        raise ValueError("人工标注的问题 ID 重复")
    run_map = {(run["case_id"], run["variant"]): run for run in runs}
    if len(run_map) != len(runs) or any(key[0] not in case_map for key in run_map):
        raise ValueError("执行结果包含重复或未知样本")
    for run in runs:
        report = run["report"]
        items = [
            item
            for field in ("items", "pending_items", "withdrawn_items")
            for item in report.get(field, [])
        ]
        if len({item["id"] for item in items}) != len(items):
            raise ValueError("执行结果的条目 ID 重复")
        if any(
            item.get("status") not in {"issue", "suggestion"}
            for item in report.get("items", [])
        ):
            raise ValueError("执行结果中的问题与建议需要明确分类")
    gold_complete = all(
        case.get("gold_complete", "expected_issues" in case) for case in cases
    )
    annotation_map = {}
    for annotation in annotations:
        key = (annotation["case_id"], annotation["variant"], annotation["item_id"])
        if key in annotation_map:
            raise ValueError("同一个输出条目的复核标注重复")
        if (key[0], key[1]) not in run_map:
            raise ValueError("复核标注没有对应的执行结果")
        report = run_map[key[:2]]["report"]
        all_items = (
            report.get("items", [])
            + report.get("withdrawn_items", [])
            + report.get("pending_items", [])
        )
        if key[2] not in {item["id"] for item in all_items}:
            raise ValueError("复核标注引用了未知输出条目")
        annotation_map[key] = annotation
    result = {}
    for variant in sorted({run["variant"] for run in runs}):
        counts, matched, complete_documents, false_by_document = (
            Counter(),
            set(),
            [],
            Counter(),
        )
        durations, tokens = [], []
        cost_documents = 0
        costs = Counter()
        model_calls = 0
        known_input, known_output, usage_complete = 0, 0, True
        for case_id in case_map:
            run = run_map.get((case_id, variant))
            if run is None:
                counts["missing_runs"] += 1
                usage_complete = False
                continue
            if run.get("error"):
                counts["failed_runs"] += 1
            meta = run.get("metadata", {})
            model_calls += meta.get("model_calls") or 0
            if meta.get("elapsed_seconds") is not None:
                durations.append(meta["elapsed_seconds"])
            usage_complete = usage_complete and bool(meta.get("usage_complete", False))
            known_input += meta.get("known_input_tokens") or 0
            known_output += meta.get("known_output_tokens") or 0
            if meta.get("cost") is not None:
                costs[meta.get("currency") or "unknown"] += meta["cost"]
                cost_documents += 1
            if meta.get("budget_tokens") is not None:
                tokens.append(meta["budget_tokens"])
            document_reviewed = True
            for item in run["report"].get("items", []):
                annotation = annotation_map.get((case_id, variant, item["id"]), {})
                label = annotation.get("label", "unreviewed")
                if label not in {
                    "unreviewed",
                    "true_positive",
                    "false_positive",
                    "suggestion_accepted",
                    "suggestion_rejected",
                }:
                    raise ValueError("报告条目的标注类别无效")
                if item.get("status") == "suggestion":
                    counts["suggestions"] += 1
                    if label in {"true_positive", "false_positive"}:
                        raise ValueError("建议的认可不能直接标注为明确问题的真假")
                    if label == "suggestion_accepted":
                        counts["suggestions_accepted"] += 1
                    if label == "suggestion_rejected":
                        counts["suggestions_rejected"] += 1
                    continue
                counts["reported_issues"] += 1
                if label == "unreviewed":
                    document_reviewed = False
                    continue
                if label not in {"true_positive", "false_positive"}:
                    raise ValueError("明确问题需要真假标注")
                counts["reviewed_issues"] += 1
                counts[label] += 1
                if label == "true_positive":
                    key = (case_id, annotation.get("expected_id"))
                    if key not in gold:
                        raise ValueError("正确检出需要对应人工标注的问题 ID")
                    matched.add(key)
                else:
                    false_by_document[case_id] += 1
            if document_reviewed and not run.get("error"):
                complete_documents.append(case_id)
            for item in run["report"].get("withdrawn_items", []):
                label = annotation_map.get((case_id, variant, item["id"]), {}).get(
                    "label", "unreviewed"
                )
                if label not in {
                    "unreviewed",
                    "withdrawn_correct",
                    "withdrawn_incorrect",
                }:
                    raise ValueError("撤回候选的标注类别无效")
                counts["withdrawn_candidates"] += 1
                if label != "unreviewed":
                    counts[label] += 1
            for item in run["report"].get("pending_items", []):
                label = annotation_map.get((case_id, variant, item["id"]), {}).get(
                    "label", "unreviewed"
                )
                if label not in {
                    "unreviewed",
                    "pending_issue",
                    "pending_non_issue",
                    "pending_unresolved",
                }:
                    raise ValueError("待核实候选的标注类别无效")
                counts["pending_candidates"] += 1
                if label != "unreviewed":
                    counts[label] += 1
        review_complete = (
            counts["reviewed_issues"] == counts["reported_issues"]
            and not counts["missing_runs"]
            and gold_complete
        )
        critical = {
            key for key, issue in gold.items() if issue.get("severity") == "critical"
        }
        result[variant] = {
            **dict(counts),
            "documents": len(cases),
            "gold_issues": len(gold),
            "gold_review_complete": gold_complete,
            "run_success_rate": _ratio(
                len(cases) - counts["failed_runs"] - counts["missing_runs"], len(cases)
            ),
            "precision_on_reviewed": _ratio(
                counts["true_positive"], counts["reviewed_issues"]
            ),
            "issue_review_coverage": _ratio(
                counts["reviewed_issues"], counts["reported_issues"]
            ),
            "recall_against_gold": _ratio(len(matched), len(gold))
            if review_complete
            else None,
            "confirmed_recall_lower_bound": _ratio(len(matched), len(gold)),
            "critical_recall": _ratio(len(matched & critical), len(critical))
            if review_complete
            else None,
            "redundant_predictions": counts["true_positive"] - len(matched),
            "fully_reviewed_documents": len(complete_documents),
            "false_positives_per_fully_reviewed_document": _ratio(
                sum(false_by_document[c] for c in complete_documents),
                len(complete_documents),
            ),
            "withdrawal_precision_on_reviewed": _ratio(
                counts["withdrawn_correct"],
                counts["withdrawn_correct"] + counts["withdrawn_incorrect"],
            ),
            "latency_p50_seconds": _percentile(durations, 0.5),
            "latency_p95_seconds": _percentile(durations, 0.95),
            "latency_measurement_coverage": _ratio(len(durations), len(cases)),
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "usage_complete": usage_complete,
            "model_calls": model_calls,
            "mean_budget_tokens": mean(tokens) if tokens else None,
            "known_cost_total": sum(costs.values()) if len(costs) == 1 else None,
            "known_cost_by_currency": dict(costs),
            "cost_complete": cost_documents == len(cases),
        }
    return result


def annotation_template(runs):
    rows = []
    for run in runs:
        for field in ("items", "pending_items", "withdrawn_items"):
            for item in run["report"].get(field, []):
                rows.append(
                    {
                        "case_id": run["case_id"],
                        "variant": run["variant"],
                        "item_id": item["id"],
                        "kind": field,
                        "description": item.get("description", ""),
                        "label": "unreviewed",
                        "expected_id": "",
                        "note": "",
                    }
                )
    return rows
