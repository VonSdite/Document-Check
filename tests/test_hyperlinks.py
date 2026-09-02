import unittest
from unittest.mock import patch

from app.hyperlinks import (
    HYPERLINK_CHECK_CODE,
    UnsafeHyperlinkTarget,
    _ensure_safe_http_destination,
    build_hyperlink_report,
    probe_http_url,
)


class HyperlinkRuleTest(unittest.TestCase):
    def test_build_report_deduplicates_links_and_merges_locations(self):
        calls = []

        def fake_probe(target, network):
            calls.append(target)
            return {
                "state": "valid",
                "http_status": 200,
                "final_url": target,
            }

        report = build_hyperlink_report(
            [
                {"target": "https://docs.example.com/guide", "display_text": "指南", "location": "第1章"},
                {"target": "https://docs.example.com/guide", "display_text": "操作指南", "location": "第2章"},
            ],
            issue_limit=30,
            probe=fake_probe,
        )

        self.assertEqual(calls, ["https://docs.example.com/guide"])
        self.assertIn("共检查 1 个唯一链接；有效 1 个", report["summary"])
        self.assertEqual(report["items"], [])

    def test_build_report_marks_invalid_scheme_without_network(self):
        probe_called = []

        report = build_hyperlink_report(
            [{"target": "javascript:alert(1)", "display_text": "点击", "location": "第1段"}],
            issue_limit=30,
            probe=lambda *_args: probe_called.append(True),
        )

        self.assertFalse(probe_called)
        self.assertEqual(report["items"][0]["type"], "issue")
        self.assertEqual(report["items"][0]["category"], "超链接格式错误")

    def test_probe_blocks_private_destination_before_request(self):
        with patch("app.hyperlinks.requests.Session.request") as request:
            result = probe_http_url("http://127.0.0.1:8080")

        request.assert_not_called()
        self.assertEqual(result["state"], "suggestion")
        self.assertEqual(result["category"], "安全策略未检测")

    def test_probe_marks_404_as_definite_issue(self):
        class FakeResponse:
            status_code = 404
            headers = {}

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.request_count = 0

            def request(self, *_args, **_kwargs):
                self.request_count += 1
                return FakeResponse()

            def close(self):
                pass

        session = FakeSession()
        with (
            patch("app.hyperlinks._ensure_safe_http_destination"),
            patch("app.hyperlinks._http_session", return_value=session),
        ):
            result = probe_http_url("https://docs.example.com/missing")

        self.assertEqual(result["state"], "issue")
        self.assertEqual(result["category"], "外部链接失效")
        self.assertEqual(result["http_status"], 404)
        self.assertEqual(session.request_count, 2)

    def test_probe_rechecks_redirect_destination_before_following(self):
        class RedirectResponse:
            status_code = 302
            headers = {"Location": "http://127.0.0.1/private"}

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.requests = []

            def request(self, method, target, **_kwargs):
                self.requests.append((method, target))
                return RedirectResponse()

            def close(self):
                pass

        session = FakeSession()
        with (
            patch(
                "app.hyperlinks._ensure_safe_http_destination",
                side_effect=[
                    None,
                    UnsafeHyperlinkTarget("重定向指向内网，系统未访问。"),
                ],
            ) as safety_check,
            patch("app.hyperlinks._http_session", return_value=session),
        ):
            result = probe_http_url("https://docs.example.com/redirect")

        self.assertEqual(safety_check.call_count, 2)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(result["category"], "安全策略未检测")

    def test_report_redacts_sensitive_query_values(self):
        def fake_probe(target, _network):
            return {
                "state": "suggestion",
                "category": "人工确认",
                "description": "需要确认",
                "impact": "未知",
                "suggestion": "人工打开",
                "confidence": "low",
            }

        report = build_hyperlink_report(
            [
                {
                    "target": "https://docs.example.com/guide?token=secret-value&lang=zh",
                    "display_text": "指南",
                    "location": "第1段",
                }
            ],
            issue_limit=30,
            probe=fake_probe,
        )

        excerpt = report["items"][0]["excerpt"]
        self.assertNotIn("secret-value", excerpt)
        self.assertIn("token=%2A%2A%2A", excerpt)
        self.assertIn("lang=zh", excerpt)

    def test_safe_destination_rejects_non_global_dns_answer(self):
        with patch(
            "app.hyperlinks.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("10.0.0.5", 443))],
        ):
            with self.assertRaisesRegex(ValueError, "内网"):
                _ensure_safe_http_destination("https://docs.example.com/guide")

    def test_rule_code_is_stable(self):
        self.assertEqual(HYPERLINK_CHECK_CODE, "hyperlink-validity")


if __name__ == "__main__":
    unittest.main()
