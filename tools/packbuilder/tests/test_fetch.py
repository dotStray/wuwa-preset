"""The builder's one way to the internet, against a server on this machine (audit P3, P5, P6, P10)."""

from __future__ import annotations

import http.server
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from packbuilder import http as fetch
from packbuilder.http import Fetcher, FetchError


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        route = type(self).routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        route(self)


def _send(handler, status=200, body=b"ok", headers=None, length=None):
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body) if length is None else length))
    handler.end_headers()
    handler.wfile.write(body)


class FetcherTest(unittest.TestCase):
    def setUp(self):
        _Handler.routes = {}
        _Handler.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        # No real waiting between tries in a test.
        patcher = mock.patch.object(fetch.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def fetcher(self, **options) -> Fetcher:
        return Fetcher(pathlib.Path(self.temp.name), delay=0, **options)

    def paths(self, path):
        return [seen for seen, _ in _Handler.seen if seen == path]

    def test_a_hiccup_is_tried_again(self):
        answers = iter([500, 200])
        _Handler.routes["/x"] = lambda h: _send(h, next(answers), b"body")
        self.assertEqual(self.fetcher().get(f"{self.base}/x"), b"body")
        self.assertEqual(len(self.paths("/x")), 2)

    def test_a_body_cut_short_is_a_fetch_error_not_a_crash(self):
        # IncompleteRead is http.client's own; it used to stop every game's build (P3).
        _Handler.routes["/x"] = lambda h: _send(h, 200, b"short", length=1000)
        with self.assertRaises(FetchError):
            self.fetcher().get(f"{self.base}/x")

    def test_offline_reads_only_the_cache(self):
        _Handler.routes["/x"] = lambda h: _send(h, 200, b"first")
        online = self.fetcher()
        online.get(f"{self.base}/x")
        offline = self.fetcher(offline=True)
        self.assertEqual(offline.get(f"{self.base}/x"), b"first")
        with self.assertRaises(FetchError):
            offline.get(f"{self.base}/not-cached")
        self.assertEqual(len(self.paths("/x")), 1)

    def test_the_token_goes_to_its_host_and_never_through_a_redirect(self):
        _Handler.routes["/api"] = lambda h: _send(h, 302, b"", headers={"Location": "/elsewhere"})
        _Handler.routes["/elsewhere"] = lambda h: _send(h, 200, b"there")
        self.fetcher(token="secret", token_hosts=(self.host,)).get(f"{self.base}/api")
        auth = dict(_Handler.seen)
        self.assertEqual(auth["/api"], "Bearer secret")
        self.assertIsNone(auth["/elsewhere"], "a redirect does not carry the token (P5)")

    def test_a_path_robots_txt_disallows_is_not_fetched(self):
        _Handler.routes["/robots.txt"] = lambda h: _send(
            h, 200, b"User-agent: *\nDisallow: /private\n", headers={"Content-Type": "text/plain"})
        _Handler.routes["/public"] = lambda h: _send(h, 200, b"fine")
        _Handler.routes["/private"] = lambda h: _send(h, 200, b"secret")
        fetcher = self.fetcher()
        self.assertEqual(fetcher.get(f"{self.base}/public"), b"fine")
        with self.assertRaises(FetchError):
            fetcher.get(f"{self.base}/private")
        self.assertEqual(self.paths("/private"), [])
        self.assertEqual(len(self.paths("/robots.txt")), 1, "read once per host")

    def test_a_site_without_robots_txt_is_read(self):
        _Handler.routes["/x"] = lambda h: _send(h, 200, b"ok")
        self.assertEqual(self.fetcher().get(f"{self.base}/x"), b"ok")

    def test_a_short_retry_after_is_waited_out_and_a_long_one_fails(self):
        answers = iter([(429, {"Retry-After": "2"}), (200, {})])
        _Handler.routes["/x"] = lambda h: _answer(h, answers)
        self.assertEqual(self.fetcher().get(f"{self.base}/x"), b"ok")
        _Handler.routes["/y"] = lambda h: _send(h, 429, b"", headers={"Retry-After": "3600"})
        with self.assertRaises(FetchError) as caught:
            self.fetcher().get(f"{self.base}/y")
        self.assertIn("3600", str(caught.exception))
        self.assertEqual(len(self.paths("/y")), 1, "not asked again sooner than it said")

    def test_an_answer_over_the_limit_is_not_kept(self):
        _Handler.routes["/x"] = lambda h: _send(h, 200, b"x" * 2048)
        with mock.patch.object(fetch, "MAX_BYTES", 1024):
            with self.assertRaises(FetchError):
                self.fetcher().get(f"{self.base}/x")
        cached = pathlib.Path(self.temp.name, "http")
        self.assertEqual([p for p in cached.rglob("*") if p.is_file()] if cached.exists() else [], [])


def _answer(handler, answers):
    status, headers = next(answers)
    _send(handler, status, b"ok" if status == 200 else b"", headers=headers)


if __name__ == "__main__":
    unittest.main()
