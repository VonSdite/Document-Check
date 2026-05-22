import unittest
from unittest.mock import patch

from app import model_discovery


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self._data

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, self.trust_env))
        return self.responses.pop(0)


class ModelDiscoveryTest(unittest.TestCase):
    def test_builds_model_endpoint_candidates_from_chat_completions_url(self):
        self.assertEqual(
            model_discovery._build_model_endpoint_candidates("https://example.test/proxy/v1/chat/completions"),
            [
                "https://example.test/proxy/v1/models",
                "https://example.test/proxy/models",
            ],
        )

    def test_fetch_models_extracts_ids_and_sends_api_key(self):
        fake_session = FakeSession(
            [
                FakeResponse({"data": [{"id": "model-a"}, {"id": "model-b"}]}),
            ]
        )

        with patch.object(model_discovery.requests, "Session", return_value=fake_session):
            models = model_discovery.fetch_models(
                api_base="https://example.test/v1/chat/completions",
                api_key="secret",
                ssl_verify=True,
                request_timeout=10,
            )

        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(fake_session.calls[0][0], "https://example.test/v1/models")
        self.assertEqual(fake_session.calls[0][1]["headers"]["Authorization"], "Bearer secret")
        self.assertTrue(fake_session.calls[0][1]["verify"])
        self.assertFalse(fake_session.calls[0][2])


if __name__ == "__main__":
    unittest.main()
