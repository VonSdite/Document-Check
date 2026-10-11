import unittest

from app.checks.evaluation import annotation_template, score_runs


class CheckEvaluationTest(unittest.TestCase):
    def test_precision_recall_and_critical_misses_are_separate_from_suggestions(self):
        cases = [
            {
                "id": "a",
                "expected_issues": [
                    {"id": "critical", "severity": "critical"},
                    {"id": "other", "severity": "medium"},
                ],
            }
        ]
        runs = [
            {
                "case_id": "a",
                "variant": "agent",
                "report": {
                    "items": [
                        {"id": "one", "status": "issue", "description": "正确检出"},
                        {"id": "two", "status": "issue", "description": "误报"},
                        {"id": "three", "status": "suggestion", "description": "建议"},
                    ]
                },
                "metadata": {
                    "elapsed_seconds": 10,
                    "usage_complete": True,
                    "known_input_tokens": 100,
                    "known_output_tokens": 20,
                },
            }
        ]
        labels = [
            {
                "case_id": "a",
                "variant": "agent",
                "item_id": "one",
                "label": "true_positive",
                "expected_id": "other",
            },
            {
                "case_id": "a",
                "variant": "agent",
                "item_id": "two",
                "label": "false_positive",
            },
            {
                "case_id": "a",
                "variant": "agent",
                "item_id": "three",
                "label": "suggestion_accepted",
            },
        ]
        score = score_runs(cases, runs, labels)["agent"]
        self.assertEqual(score["precision_on_reviewed"], 0.5)
        self.assertEqual(score["recall_against_gold"], 0.5)
        self.assertEqual(score["critical_recall"], 0)
        self.assertEqual(score["suggestions_accepted"], 1)
        self.assertEqual(score["false_positives_per_fully_reviewed_document"], 1)

    def test_unreviewed_predictions_do_not_produce_false_precision_or_recall(self):
        cases = [
            {"id": "a", "expected_issues": [{"id": "gold", "severity": "critical"}]}
        ]
        runs = [
            {
                "case_id": "a",
                "variant": "agent",
                "report": {
                    "items": [{"id": "one", "status": "issue", "description": "候选"}]
                },
            }
        ]
        score = score_runs(cases, runs, annotation_template(runs))["agent"]
        self.assertIsNone(score["precision_on_reviewed"])
        self.assertIsNone(score["critical_recall"])
        self.assertEqual(score["fully_reviewed_documents"], 0)

    def test_missing_or_failed_runs_do_not_disappear_from_gold_denominator(self):
        cases = [
            {"id": "a", "expected_issues": [{"id": "gold", "severity": "critical"}]},
            {"id": "b", "expected_issues": []},
        ]
        runs = [
            {
                "case_id": "a",
                "variant": "agent",
                "report": {"items": []},
                "error": "模型失败",
            }
        ]
        score = score_runs(cases, runs, [])["agent"]
        self.assertEqual(score["gold_issues"], 1)
        self.assertEqual(score["failed_runs"], 1)
        self.assertEqual(score["missing_runs"], 1)
        self.assertIsNone(score["critical_recall"])

    def test_accepted_suggestion_is_not_counted_as_proven_error(self):
        cases = [{"id": "a", "expected_issues": []}]
        runs = [
            {
                "case_id": "a",
                "variant": "agent",
                "report": {"items": [{"id": "one", "status": "suggestion"}]},
            }
        ]
        with self.assertRaises(ValueError):
            score_runs(
                cases,
                runs,
                [
                    {
                        "case_id": "a",
                        "variant": "agent",
                        "item_id": "one",
                        "label": "true_positive",
                    }
                ],
            )

    def test_pending_candidates_and_incomplete_gold_do_not_inflate_recall(self):
        cases = [
            {
                "id": "a",
                "gold_complete": False,
                "expected_issues": [{"id": "gold", "severity": "critical"}],
            }
        ]
        runs = [
            {
                "case_id": "a",
                "variant": "agent",
                "report": {
                    "items": [],
                    "pending_items": [{"id": "pending", "description": "待核实"}],
                },
            }
        ]
        annotations = [
            {
                "case_id": "a",
                "variant": "agent",
                "item_id": "pending",
                "label": "pending_issue",
                "expected_id": "gold",
            }
        ]
        score = score_runs(cases, runs, annotations)["agent"]
        self.assertEqual(score["pending_candidates"], 1)
        self.assertEqual(score["confirmed_recall_lower_bound"], 0)
        self.assertIsNone(score["recall_against_gold"])
        annotations[0]["label"] = "true_positive"
        with self.assertRaises(ValueError):
            score_runs(cases, runs, annotations)

    def test_different_currencies_are_reported_separately(self):
        cases = [{"id": case_id, "expected_issues": []} for case_id in ("a", "b")]
        runs = [
            {
                "case_id": case_id,
                "variant": "agent",
                "report": {"items": []},
                "metadata": {"cost": 1, "currency": currency},
            }
            for case_id, currency in (("a", "CNY"), ("b", "USD"))
        ]
        score = score_runs(cases, runs, [])["agent"]
        self.assertIsNone(score["known_cost_total"])
        self.assertEqual(score["known_cost_by_currency"], {"CNY": 1, "USD": 1})


if __name__ == "__main__":
    unittest.main()
