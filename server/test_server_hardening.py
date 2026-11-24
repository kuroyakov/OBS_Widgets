#!/usr/bin/env python3
"""
サーバー堅牢化（静的配信ガード正規化・POST JSON 型検証・設定ファイル破損保護）のテスト (Issue #16)
実行: python3 server/test_server_hardening.py
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestConfigNonDictBackupUnit(unittest.TestCase):
    def test_non_dict_root_json_is_backed_up_as_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "twitch_config.json")
            raw_content = '["unexpected", "list"]'
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(raw_content)

            try:
                with mock.patch.object(server_config, "CONFIG_FILE", cfg_path):
                    server_config._CONFIG_CACHE["key"] = None
                    server_config._CONFIG_CACHE["value"] = None
                    loaded = server_config.load_twitch_config()
            finally:
                server_config._CONFIG_CACHE["key"] = None
                server_config._CONFIG_CACHE["value"] = None

            self.assertIsInstance(loaded, dict)
            self.assertEqual(loaded["client_id"], "")
            corrupt_path = cfg_path + ".corrupt"
            self.assertTrue(os.path.isfile(corrupt_path))
            with open(corrupt_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), raw_content)


class TestServerHardeningHttp(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_static_guard_blocks_normalized_traversal_and_twitch_tmp_files(self):
        blocked_paths = (
            "/a/../logs/dummy.txt",
            "//a/../logs/dummy.txt",
            "/a/../tools/privacy_scan_allowlist.txt",
            "/a/../server/README.md",
            "/twitch_config.json.tmp.9999",
            "/twitch_config.json.corrupt",
        )
        for path in blocked_paths:
            status_get, _ = _http_request(f"{self.base_url}{path}", method="GET")
            self.assertEqual(status_get, 403, f"GET {path} should be 403")
            status_head, _ = _http_request(f"{self.base_url}{path}", method="HEAD")
            self.assertEqual(status_head, 403, f"HEAD {path} should be 403")

    def test_json_post_endpoints_reject_non_dict_and_invalid_json_with_400(self):
        endpoints = (
            "/api/feed-log",
            "/api/twitch/save-config",
            "/api/twitch/eventsub/subscribe",
            "/api/translate",
            "/api/subtitle/lang",
            "/api/youtube/reconnect",
            "/api/youtube/test",
            "/api/launcher/action",
            "/api/voicevox/tts",
            "/api/voicevox/synthesis",
        )
        for ep in endpoints:
            for raw_body in (b"[]", b'"string"', b"{not-json"):
                status, body = _http_request(
                    f"{self.base_url}{ep}",
                    method="POST",
                    body=raw_body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, f"POST {ep} with {raw_body!r} should be 400")
                data = json.loads(body.decode("utf-8"))
                self.assertIn("error", data)

    def test_cubase_bridge_clear_requires_local_or_token(self):
        called = []

        def fake_clear():
            called.append(True)
            return {"ok": True}

        with (
            mock.patch.object(server_handler, "_CUBASE_BRIDGE_ENABLED", True),
            mock.patch.object(server_handler, "_clear_cubase_display", fake_clear),
            mock.patch.object(
                server_handler.UnifiedWidgetHandler,
                "_client_is_local",
                return_value=False,
            ),
        ):
            status, body = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(called, [])
            self.assertIn("error", json.loads(body.decode("utf-8")))

            token = server_handler.get_or_create_api_token()
            status_ok, body_ok = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json", "X-Widget-Token": token},
            )
            self.assertEqual(status_ok, 200)
            self.assertEqual(called, [True])
            self.assertEqual(json.loads(body_ok.decode("utf-8")), {"ok": True})

    def test_cubase_bridge_clear_returns_404_when_disabled_or_unimplemented(self):
        with (
            mock.patch.object(server_handler, "_CUBASE_BRIDGE_ENABLED", False),
            mock.patch.object(server_handler, "_clear_cubase_display", lambda: {"ok": True}),
        ):
            status, body = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body.decode("utf-8")), {"error": "Not Found"})

        with (
            mock.patch.object(server_handler, "_CUBASE_BRIDGE_ENABLED", True),
            mock.patch.object(server_handler, "_clear_cubase_display", None),
        ):
            status, body = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body.decode("utf-8")), {"error": "Not Found"})

    def test_cubase_bridge_clear_calls_hook_and_returns_200(self):
        called = []

        def fake_clear_dict():
            called.append("dict")
            return {"ok": True, "cleared": True}

        with (
            mock.patch.object(server_handler, "_CUBASE_BRIDGE_ENABLED", True),
            mock.patch.object(server_handler, "_clear_cubase_display", fake_clear_dict),
        ):
            status, body = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True, "cleared": True})

        def fake_clear_none():
            called.append("none")
            return None

        with (
            mock.patch.object(server_handler, "_CUBASE_BRIDGE_ENABLED", True),
            mock.patch.object(server_handler, "_clear_cubase_display", fake_clear_none),
        ):
            status, body = _http_request(
                f"{self.base_url}/api/cubase-bridge/clear",
                method="POST",
                body=b"",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True})

        self.assertEqual(called, ["dict", "none"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
