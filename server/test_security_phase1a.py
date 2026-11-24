#!/usr/bin/env python3
"""
セキュリティ Phase 1a (Issue #49) の単体・HTTP テスト:
- Host ヘッダ検証 (可変ポート対応)
- Origin: null の特権剥奪 (/api/auth/token, obs_password, 状態変更 POST)
- POST の Content-Type: application/json 検証
- クリックジャッキング / リファラ対策ヘッダ
- jimakuChan CORS の https 限定
- save_twitch_config のホワイトリスト化
- voicevox_url の SSRF 検証 (既定ループバック、opt-in プライベート IP、リンクローカル/外部拒否)
- youtube_handle の検証 (https + YouTube ドメインのみ)
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402
from server import voicevox as server_voicevox  # noqa: E402
from server import youtube_chat as server_youtube  # noqa: E402


def _raw_http_request(port, method, path, body=b"", headers=None, host_header=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        req_headers = dict(headers or {})
        if host_header is not None:
            req_headers["Host"] = host_header
        conn.putrequest(method, path, skip_host=(host_header is not None))
        for k, v in req_headers.items():
            conn.putheader(k, v)
        if body is not None and "Content-Length" not in {k.title() for k in req_headers}:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or b"")
        resp = conn.getresponse()
        raw_body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw_body
    finally:
        conn.close()


class TestSecurityPhase1aHttp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmpdir.name, "twitch_config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "obs_password": "secret-obs-password",
                    "api_token": "secret-api-token",
                    "current_ticker": "initial ticker",
                    "youtube_handle": "@initial_channel",
                    "viewer_settings": {"voicevox_url": "http://127.0.0.1:50021"},
                },
                f,
            )
        self.patch_cfg = mock.patch.object(server_config, "CONFIG_FILE", self.cfg_path)
        self.patch_cfg.start()
        server_config._CONFIG_CACHE["key"] = None
        server_config._CONFIG_CACHE["value"] = None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.patch_cfg.stop()
        server_config._CONFIG_CACHE["key"] = None
        server_config._CONFIG_CACHE["value"] = None
        self.tmpdir.cleanup()

    def _read_config_file(self):
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 1. Host header validation
    def test_host_header_allows_loopback_with_actual_port_and_rejects_others(self):
        for valid_host in (
            f"localhost:{self.port}",
            f"127.0.0.1:{self.port}",
            f"[::1]:{self.port}",
            f"LOCALHOST:{self.port}",
        ):
            status, _, _ = _raw_http_request(
                self.port, "GET", "/api/config", host_header=valid_host
            )
            self.assertEqual(status, 200, f"Host {valid_host!r} should be allowed")

        for bad_host in (
            f"evil.example:{self.port}",
            "localhost:8080" if self.port != 8080 else "localhost:9999",
            "localhost",
            "",
        ):
            for method in ("GET", "HEAD", "POST", "OPTIONS"):
                status, _, body = _raw_http_request(
                    self.port,
                    method,
                    "/api/config" if method != "POST" else "/api/twitch/save-config",
                    body=b"{}" if method == "POST" else b"",
                    headers={"Content-Type": "application/json"} if method == "POST" else {},
                    host_header=bad_host,
                )
                self.assertEqual(status, 403, f"{method} with Host {bad_host!r} should be 403")
                if method == "HEAD":
                    self.assertEqual(body, b"")

    # 2. Origin: null privilege revocation
    def test_origin_null_denied_for_auth_token_obs_password_and_state_changing_posts(self):
        # GET /api/auth/token -> 403, no ACAO
        status, hdrs, body = _raw_http_request(
            self.port, "GET", "/api/auth/token", headers={"Origin": "null"}
        )
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", hdrs)
        self.assertNotIn("secret-api-token", body.decode("utf-8"))

        # GET /api/config -> obs_password is empty, no ACAO
        status, hdrs, body = _raw_http_request(
            self.port, "GET", "/api/config", headers={"Origin": "null"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", hdrs)
        self.assertEqual(json.loads(body.decode("utf-8"))["obs_password"], "")

        # GET /api/status -> obs_password is empty, no ACAO
        with mock.patch.object(
            server_handler,
            "validate_twitch_token",
            return_value={"valid": False, "scopes": [], "missing_scopes": []},
        ):
            status, hdrs, body = _raw_http_request(
                self.port, "GET", "/api/status", headers={"Origin": "null"}
            )
            self.assertEqual(status, 200)
            self.assertNotIn("access-control-allow-origin", hdrs)
            self.assertEqual(json.loads(body.decode("utf-8"))["obs_password"], "")

        # State-changing POST with Origin: null -> 403 and no side effects
        before_cfg = self._read_config_file()
        for ep, payload in (
            ("/api/twitch/save-config", {"current_ticker": "hacked"}),
            ("/api/youtube/reconnect", {"youtube_handle": "@hacked"}),
            ("/api/voicevox/tts", {"text": "hello"}),
        ):
            status, hdrs, _ = _raw_http_request(
                self.port,
                "POST",
                ep,
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "null"},
            )
            self.assertEqual(status, 403, f"POST {ep} with Origin: null should be 403")
            self.assertNotIn("access-control-allow-origin", hdrs)
        self.assertEqual(self._read_config_file(), before_cfg)

    # 3. POST Content-Type validation
    def test_post_rejects_non_json_content_type_without_side_effects(self):
        before_cfg = self._read_config_file()
        for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            for ep, payload in (
                ("/api/twitch/save-config", {"current_ticker": "csrf"}),
                ("/api/youtube/reconnect", {"youtube_handle": "@csrf"}),
                ("/api/voicevox/tts", {"text": "csrf"}),
                ("/api/voice-command/utterance", {"text": "csrf"}),
            ):
                hdrs = {"Content-Type": ct} if ct else {}
                status, _, _ = _raw_http_request(
                    self.port,
                    "POST",
                    ep,
                    body=json.dumps(payload).encode("utf-8"),
                    headers=hdrs,
                )
                self.assertEqual(status, 415, f"POST {ep} with Content-Type={ct!r} should be 415")
        self.assertEqual(self._read_config_file(), before_cfg)

        # Valid Content-Type with charset parameter succeeds
        status, _, _ = _raw_http_request(
            self.port,
            "POST",
            "/api/twitch/save-config",
            body=json.dumps({"current_ticker": "updated"}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self._read_config_file()["current_ticker"], "updated")

    # 4. Security headers
    def test_responses_include_clickjacking_and_referrer_headers(self):
        for method, path in (("GET", "/api/config"), ("OPTIONS", "/api/config")):
            _, hdrs, _ = _raw_http_request(self.port, method, path)
            self.assertEqual(hdrs.get("x-frame-options"), "SAMEORIGIN")
            self.assertEqual(hdrs.get("content-security-policy"), "frame-ancestors 'self'")
            self.assertEqual(hdrs.get("referrer-policy"), "no-referrer")

    # 5. jimakuChan CORS https-only
    def test_cors_allows_https_sayonari_and_blocks_http_sayonari(self):
        for origin in ("https://www.sayonari.com", "https://sayonari.github.io"):
            _, hdrs, _ = _raw_http_request(
                self.port, "GET", "/api/subtitle/lang", headers={"Origin": origin}
            )
            self.assertEqual(hdrs.get("access-control-allow-origin"), origin)

        for origin in (
            "http://www.sayonari.com",
            "http://sayonari.github.io",
            "https://evil.example",
        ):
            _, hdrs, _ = _raw_http_request(
                self.port, "GET", "/api/subtitle/lang", headers={"Origin": origin}
            )
            self.assertNotIn("access-control-allow-origin", hdrs, f"{origin} must not get ACAO")

    # 6. save_twitch_config whitelist
    def test_save_config_rejects_obs_ws_host_and_unknown_keys_without_saving(self):
        before_cfg = self._read_config_file()
        for bad_payload in (
            {"obs_ws_host": "evil.example"},
            {"obs_ws_port": 9999},
            {"api_token": "attacker-token"},
            {"unknown_key": "value"},
            {"current_ticker": "ok", "obs_ws_host": "evil.example"},
        ):
            status, _, body = _raw_http_request(
                self.port,
                "POST",
                "/api/twitch/save-config",
                body=json.dumps(bad_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400, f"payload {bad_payload!r} should be rejected")
            self.assertIn("error", json.loads(body.decode("utf-8")))
            self.assertEqual(self._read_config_file(), before_cfg)

        # Direct save_twitch_config with allowed_keys also drops disallowed keys
        saved = server_config.save_twitch_config(
            {"current_ticker": "direct-ok", "obs_ws_host": "evil.example"},
            allowed_keys=server_config.CLIENT_WRITABLE_CONFIG_KEYS,
        )
        self.assertEqual(saved["current_ticker"], "direct-ok")
        self.assertNotIn("obs_ws_host", saved)
        self.assertNotIn("obs_ws_host", self._read_config_file())

    # 7. voicevox_url SSRF validation (HTTP + unit)
    def test_voicevox_url_rejects_ssrf_targets_and_blocks_network_calls(self):
        before_cfg = self._read_config_file()
        bad_urls = (
            "file:///etc/passwd",
            "ftp://127.0.0.1:50021",
            "http://169.254.169.254",
            "http://169.254.169.254:80",
            "http://[fe80::1]:50021",
            "http://8.8.8.8:50021",
            "http://evil.example:50021",
            "http://127.0.0.1:50021/extra/path",
            "http://127.0.0.1:50021?q=1",
            "http://user:pass@127.0.0.1:50021",
            f"http://{'.'.join(('10', '0', '0', '50'))}:50021",
        )
        for bad_url in bad_urls:
            status, _, _ = _raw_http_request(
                self.port,
                "POST",
                "/api/twitch/save-config",
                body=json.dumps({"viewer_settings": {"voicevox_url": bad_url}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400, f"voicevox_url {bad_url!r} should be 400")
            self.assertEqual(self._read_config_file(), before_cfg)

            with mock.patch("urllib.request.urlopen") as mock_urlopen:
                res, code = server_voicevox.fetch_voicevox_speakers(
                    {"viewer_settings": {"voicevox_url": bad_url}}
                )
                self.assertEqual(code, 200)
                self.assertFalse(res["running"])
                mock_urlopen.assert_not_called()

                with self.assertRaises(ValueError):
                    server_voicevox.synthesize_voicevox_wav(
                        "hello",
                        config={"viewer_settings": {"voicevox_url": bad_url}},
                    )
                mock_urlopen.assert_not_called()

        # Private IP allowed ONLY with explicit env opt-in, link-local still blocked
        private_host = ".".join(("10", "0", "0", "50"))
        with mock.patch.dict(os.environ, {server_voicevox.ENV_ALLOW_PRIVATE_VOICEVOX_URL: "1"}):
            self.assertEqual(
                server_voicevox.validate_voicevox_url(f"http://{private_host}:50021/"),
                f"http://{private_host}:50021",
            )
            with self.assertRaises(ValueError):
                server_voicevox.validate_voicevox_url("http://169.254.169.254")
            with self.assertRaises(ValueError):
                server_voicevox.validate_voicevox_url("http://8.8.8.8:50021")

        # Loopback URLs work by default
        for ok_url in (
            "http://127.0.0.1:50021",
            "http://localhost:50021/",
            "https://[::1]:50021",
        ):
            self.assertTrue(server_voicevox.validate_voicevox_url(ok_url).startswith("http"))

    # 8. youtube_handle validation (HTTP + unit)
    def test_youtube_handle_rejects_non_youtube_or_http_and_blocks_polling(self):
        before_cfg = self._read_config_file()
        for bad_handle in (
            "http://www.youtube.com/@handle",
            "http://169.254.169.254/latest/meta-data",
            "https://evil.example/@handle",
            "file:///etc/passwd",
            "https://www.youtube.com" + "@evil.example/live",
            "javascript:alert(1)",
        ):
            for ep in ("/api/youtube/reconnect", "/api/twitch/save-config"):
                status, _, _ = _raw_http_request(
                    self.port,
                    "POST",
                    ep,
                    body=json.dumps({"youtube_handle": bad_handle}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, f"POST {ep} with {bad_handle!r} should be 400")
                self.assertEqual(self._read_config_file(), before_cfg)

            with self.assertRaises(ValueError):
                server_youtube.build_youtube_live_url(bad_handle)

            with mock.patch("urllib.request.urlopen") as mock_urlopen:
                ok = server_youtube.YOUTUBE_MANAGER._connect_live_chat(
                    {"youtube_handle": bad_handle, "youtube_video_id": ""}
                )
                self.assertFalse(ok)
                mock_urlopen.assert_not_called()

        # Allowed YouTube handles and HTTPS URLs
        for ok_handle in (
            "@your_channel",
            "UC12345678901234567890",
            "https://www.youtube.com/@your_channel",
            "https://youtube.com/@your_channel/live",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
        ):
            url, _ = server_youtube.build_youtube_live_url(ok_handle)
            self.assertTrue(url.startswith("https://"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
