#!/usr/bin/env python3
"""
Twitch トークン検証・リフレッシュ・EventSub 購読ハンドラのテスト (Issue #14)
実行: python3 server/test_twitch_api.py
"""

import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import handler as server_handler  # noqa: E402
from server import twitch_api  # noqa: E402


class _FakeHttpResponse:
    def __init__(self, payload_dict, status=200):
        self._body = json.dumps(payload_dict).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _http_error(code, msg="error"):
    return urllib.error.HTTPError(
        url="https://id.twitch.tv/oauth2/validate",
        code=code,
        msg=msg,
        hdrs=None,
        fp=io.BytesIO(msg.encode("utf-8")),
    )


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestValidateTwitchTokenUnit(unittest.TestCase):
    def test_validate_refreshes_once_on_401_and_succeeds(self):
        state = {
            "client_id": "dummy-client-id",
            "access_token": "expired-token",
            "refresh_token": "old-refresh",
        }
        calls = []

        def fake_urlopen(req, **_kwargs):
            calls.append(req.headers.get("Authorization"))
            if len(calls) == 1:
                raise _http_error(401, "invalid access token")
            return _FakeHttpResponse({
                "client_id": "dummy-client-id",
                "login": "dummy_user",
                "user_id": "12345",
                "scopes": ["chat:read"],
                "expires_in": 3600,
            })

        def fake_refresh(_cfg):
            state["access_token"] = "fresh-token"
            state["refresh_token"] = "new-refresh"
            return True

        with mock.patch.object(twitch_api.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(twitch_api, "refresh_twitch_token", side_effect=fake_refresh), \
             mock.patch.object(twitch_api, "load_twitch_config", side_effect=lambda: dict(state)):
            res = twitch_api.validate_twitch_token(dict(state))

        self.assertTrue(res["valid"])
        self.assertEqual(res["user_id"], "12345")
        self.assertEqual(calls, ["OAuth expired-token", "OAuth fresh-token"])

    def test_validate_does_not_loop_when_401_persists_after_refresh(self):
        state = {
            "client_id": "dummy-client-id",
            "access_token": "expired-token",
            "refresh_token": "old-refresh",
        }
        urlopen_count = 0
        refresh_count = 0

        def fake_urlopen(_req, **_kwargs):
            nonlocal urlopen_count
            urlopen_count += 1
            raise _http_error(401, "still unauthorized")

        def fake_refresh(_cfg):
            nonlocal refresh_count
            refresh_count += 1
            state["access_token"] = "still-bad-token"
            return True

        with mock.patch.object(twitch_api.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(twitch_api, "refresh_twitch_token", side_effect=fake_refresh), \
             mock.patch.object(twitch_api, "load_twitch_config", side_effect=lambda: dict(state)):
            res = twitch_api.validate_twitch_token(dict(state))

        self.assertFalse(res["valid"])
        self.assertNotIn("error", res)
        self.assertEqual(refresh_count, 1)
        self.assertEqual(urlopen_count, 2)

    def test_validate_marks_network_and_5xx_failures_as_transient_error(self):
        cfg = {"access_token": "some-token", "refresh_token": "some-refresh"}

        with mock.patch.object(
            twitch_api.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            res_net = twitch_api.validate_twitch_token(cfg)
        self.assertFalse(res_net["valid"])
        self.assertIn("error", res_net)

        with mock.patch.object(
            twitch_api.urllib.request,
            "urlopen",
            side_effect=_http_error(503, "service unavailable"),
        ):
            res_503 = twitch_api.validate_twitch_token(cfg)
        self.assertFalse(res_503["valid"])
        self.assertEqual(res_503.get("error"), "HTTP 503")


class TestTwitchSubscribeAndStatusHttp(unittest.TestCase):
    def setUp(self):
        self.state = {
            "client_id": "dummy-client-id",
            "client_secret": "dummy-client-secret",
            "access_token": "expired-token",
            "refresh_token": "old-refresh",
            "broadcaster_id": "99999",
            "channel_name": "dummy_channel",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_subscribe_passes_refreshed_config_after_validate(self):
        captured_configs = []

        def fake_validate(_cfg):
            self.state["access_token"] = "refreshed-token"
            self.state["refresh_token"] = "refreshed-refresh"
            return {"valid": True, "user_id": "99999", "scopes": [], "missing_scopes": []}

        def fake_register(_session_id, _b_id, _user_id, cfg):
            captured_configs.append(dict(cfg))
            return [{"name": "chat", "type": "channel.chat.message", "success": True, "code": 202, "error": None}]

        with mock.patch.object(server_handler, "load_twitch_config", side_effect=lambda: dict(self.state)), \
             mock.patch.object(server_handler, "validate_twitch_token", side_effect=fake_validate), \
             mock.patch.object(server_handler, "register_eventsub_subscriptions", side_effect=fake_register):
            status, body = _http_request(
                f"{self.base_url}/api/twitch/eventsub/subscribe",
                method="POST",
                body=json.dumps({"session_id": "dummy-session"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(len(captured_configs), 1)
        self.assertEqual(captured_configs[0]["access_token"], "refreshed-token")
        self.assertEqual(captured_configs[0]["refresh_token"], "refreshed-refresh")

    def test_subscribe_returns_502_on_transient_error_and_401_on_invalid_token(self):
        with mock.patch.object(server_handler, "load_twitch_config", side_effect=lambda: dict(self.state)), \
             mock.patch.object(
                 server_handler,
                 "validate_twitch_token",
                 return_value={"valid": False, "error": "timed out", "scopes": [], "missing_scopes": []},
             ):
            status_502, _ = _http_request(
                f"{self.base_url}/api/twitch/eventsub/subscribe",
                method="POST",
                body=json.dumps({"session_id": "dummy-session"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status_502, 502)

        with mock.patch.object(server_handler, "load_twitch_config", side_effect=lambda: dict(self.state)), \
             mock.patch.object(
                 server_handler,
                 "validate_twitch_token",
                 return_value={"valid": False, "scopes": [], "missing_scopes": ["chat:read"]},
             ):
            status_401, _ = _http_request(
                f"{self.base_url}/api/twitch/eventsub/subscribe",
                method="POST",
                body=json.dumps({"session_id": "dummy-session"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status_401, 401)

    def test_status_passes_refreshed_config_to_twitch_request(self):
        captured_configs = []

        def fake_validate(_cfg):
            self.state["access_token"] = "refreshed-token"
            return {"valid": True, "user_id": "99999", "scopes": [], "missing_scopes": []}

        def fake_twitch_request(_endpoint, _method="GET", config=None, **_kwargs):
            captured_configs.append(dict(config or {}))
            return {"data": [{"broadcaster_id": "99999"}]}, 200

        with mock.patch.object(server_handler, "load_twitch_config", side_effect=lambda: dict(self.state)), \
             mock.patch.object(server_handler, "validate_twitch_token", side_effect=fake_validate), \
             mock.patch.object(server_handler, "twitch_request", side_effect=fake_twitch_request):
            status, _ = _http_request(f"{self.base_url}/api/status")

        self.assertEqual(status, 200)
        self.assertEqual(len(captured_configs), 1)
        self.assertEqual(captured_configs[0]["access_token"], "refreshed-token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
