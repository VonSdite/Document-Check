import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from flask import Flask

from app.identity import cookie_session as cache


class CookieSessionTest(unittest.TestCase):
    def setUp(self):
        cache._cache.clear()
        cache._inflight.clear()
        cache._next_cleanup = 0
        self.config = {
            "userinfo_url": "https://userinfo.example.test/user",
            "field_mapping": {"user_id": "uuid", "username": "name"},
            "cache_ttl": 600,
            "cache_grace": 600,
            "ssl_verify": True,
        }
        self.info = {
            "user_id": "user-1",
            "username": "张三",
            "employee_number": "",
            "avatar": "",
        }
        self.clock = patch.object(cache.time, "monotonic", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(cache._cache.clear)
        self.addCleanup(cache._inflight.clear)

    def test_fresh_cache_and_grace_retry_backoff(self):
        with patch.object(
            cache, "_fetch_userinfo", return_value=(self.info, None)
        ) as fetch:
            self.assertEqual(
                cache.resolve_userinfo(self.config, "ticket=a"), (self.info, None)
            )
            cache.resolve_userinfo(self.config, "ticket=a")
            self.assertEqual(fetch.call_count, 1)
            self.now.return_value = 701
            fetch.return_value = (None, cache.CookieSessionTransient("timeout"))
            info, error = cache.resolve_userinfo(self.config, "ticket=a")
            self.assertEqual(info, self.info)
            self.assertIsInstance(error, cache.CookieSessionTransient)
            for _ in range(3):
                cache.resolve_userinfo(self.config, "ticket=a")
            self.assertEqual(fetch.call_count, 2)
            self.now.return_value = 707
            cache.resolve_userinfo(self.config, "ticket=a")
            self.assertEqual(fetch.call_count, 3)
            self.now.return_value = 1301
            info, error = cache.resolve_userinfo(self.config, "ticket=a")
            self.assertIsNone(info)
            self.assertIsInstance(error, cache.CookieSessionNoCache)

    def test_expired_session_does_not_use_grace_cache(self):
        with patch.object(
            cache, "_fetch_userinfo", return_value=(self.info, None)
        ) as fetch:
            cache.resolve_userinfo(self.config, "ticket=a")
            self.now.return_value = 701
            fetch.return_value = (None, cache.CookieSessionExpired("401"))
            for _ in range(2):
                info, error = cache.resolve_userinfo(self.config, "ticket=a")
                self.assertIsNone(info)
                self.assertIsInstance(error, cache.CookieSessionExpired)
            self.assertEqual(fetch.call_count, 2)

    def test_cache_is_bounded_and_expired_entries_are_reclaimed(self):
        with (
            patch.object(cache, "_CACHE_LIMIT", 2),
            patch.object(cache, "_fetch_userinfo", return_value=(self.info, None)),
        ):
            for cookie in ("a", "b", "c"):
                cache.resolve_userinfo(self.config, cookie)
            self.assertEqual(len(cache._cache), 2)
            self.assertNotIn(cache._cache_key("a"), cache._cache)
            self.now.return_value = 1301
            cache.resolve_userinfo(self.config, "d")
            self.assertEqual(list(cache._cache), [cache._cache_key("d")])

    def test_concurrent_queries_share_one_upstream_request(self):
        started, waiting, release = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiting.set()
                return super().result(timeout)

        def fetch(*_args):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test query not released")
            return self.info, None

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(cache, "Future", ObservedFuture),
            patch.object(cache, "_fetch_userinfo", side_effect=fetch) as upstream,
        ):
            first = pool.submit(cache.resolve_userinfo, self.config, "ticket=a")
            try:
                self.assertTrue(started.wait(3))
                second = pool.submit(cache.resolve_userinfo, self.config, "ticket=a")
                self.assertTrue(waiting.wait(3))
            finally:
                release.set()
            self.assertEqual(first.result(3), second.result(3))
            self.assertEqual(upstream.call_count, 1)
        self.assertFalse(cache._inflight)

    def test_clear_fences_late_success_after_a_new_401(self):
        started, release = threading.Event(), threading.Event()

        def fetch(*_args):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test query not released")
            return self.info, None

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(cache, "_fetch_userinfo", side_effect=fetch) as upstream,
        ):
            first = pool.submit(cache.resolve_userinfo, self.config, "ticket=a")
            try:
                self.assertTrue(started.wait(3))
                cache.clear_cache_for_cookie("ticket=a")
                upstream.side_effect = None
                upstream.return_value = (None, cache.CookieSessionExpired("401"))
                self.assertIsNone(cache.resolve_userinfo(self.config, "ticket=a")[0])
            finally:
                release.set()
            first.result(3)
            info, error = cache.resolve_userinfo(self.config, "ticket=a")
            self.assertIsNone(info)
            self.assertIsInstance(error, cache.CookieSessionExpired)
            self.assertEqual(upstream.call_count, 2)

    def test_only_successful_http_responses_can_authenticate(self):
        for status in (200, 302, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                response = MagicMock(status_code=status)
                response.json.return_value = {"uuid": "user-1"}
                info, error = cache._parse_response(response, self.config)
                self.assertEqual(info is not None, status == 200)
                if status in (401, 403):
                    self.assertIsInstance(error, cache.CookieSessionExpired)

    def test_mapping_and_wrapped_response(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "status": "success",
            "data": {"uuid": "user-1", "name": "张三", "number": "123"},
        }
        config = {
            **self.config,
            "field_mapping": {
                **self.config["field_mapping"],
                "employee_number": "number",
            },
            "avatar_field": "number",
            "avatar_url_template": "https://avatar.test/{user_id}",
        }
        info, error = cache._parse_response(response, config)
        self.assertIsNone(error)
        self.assertEqual(
            info,
            {
                **self.info,
                "employee_number": "123",
                "avatar": "https://avatar.test/123",
            },
        )
        response.json.return_value["data"].update(name="李四", number="456")
        updated, error = cache._parse_response(response, config)
        self.assertIsNone(error)
        self.assertEqual(
            updated,
            {
                **self.info,
                "username": "李四",
                "employee_number": "456",
                "avatar": "https://avatar.test/456",
            },
        )

    def test_avatar_field_is_independent_of_employee_number(self):
        config = {
            **self.config,
            "avatar_field": "number",
            "avatar_url_template": "/avatar/{user_id}",
        }
        info = cache._apply_mapping({"uuid": "user-1", "number": "00123"}, config)
        self.assertEqual(info["employee_number"], "")
        self.assertEqual(info["avatar"], "/avatar/00123")

    def test_outbound_header_and_network_settings(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"uuid": "user-1", "name": "张三"}
        http_session = MagicMock()
        http_session.__enter__.return_value = http_session
        http_session.get.return_value = response
        app = Flask(__name__)
        app.config["NETWORK"] = {
            "proxy_mode": "custom",
            "proxy": "http://proxy.test:8080",
        }
        self.config["cookie_header_name"] = "X-Upstream-Cookie"
        with (
            app.app_context(),
            patch.object(cache.requests, "Session", return_value=http_session),
            patch.object(cache.time, "time_ns", return_value=12345),
        ):
            info, error = cache.resolve_userinfo(self.config, "ticket=a")
        self.assertEqual(info, {**self.info, "_profile_version": 12345})
        self.assertIsNone(error)
        self.assertFalse(http_session.trust_env)
        http_session.proxies.update.assert_called_once_with(
            {
                "http": "http://proxy.test:8080",
                "https": "http://proxy.test:8080",
            }
        )
        http_session.get.assert_called_once_with(
            self.config["userinfo_url"],
            headers={"X-Upstream-Cookie": "ticket=a"},
            timeout=3,
            verify=True,
            allow_redirects=False,
        )

    def test_slow_older_query_keeps_its_start_version(self):
        started, release = threading.Event(), threading.Event()

        def session():
            http = MagicMock()
            http.__enter__.return_value = http

            def get(_url, *, headers, **_kwargs):
                if headers["cookie"] == "ticket=old":
                    started.set()
                    if not release.wait(3):
                        raise TimeoutError("test query not released")
                response = MagicMock(status_code=200)
                response.json.return_value = {"uuid": "user-1", "name": "张三"}
                return response

            http.get.side_effect = get
            return http

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(cache, "_build_http_session", side_effect=session),
            patch.object(cache.time, "time_ns", side_effect=[10, 20]),
        ):
            first = pool.submit(cache.resolve_userinfo, self.config, "ticket=old")
            try:
                self.assertTrue(started.wait(3))
                latest, error = cache.resolve_userinfo(self.config, "ticket=new")
                self.assertIsNone(error)
                self.assertEqual(latest["_profile_version"], 20)
            finally:
                release.set()
            self.assertEqual(first.result(3)[0]["_profile_version"], 10)
