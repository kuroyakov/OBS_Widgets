#!/usr/bin/env python3
"""
セキュリティ Phase 2a (Issue #51) の単体・HTTP テスト:
- server.config.read_env_local_obs_password のフォールバック読み取りとキャッシュ
- GET /api/config・GET /api/status の経路別アクセス制御 (localhost / トークンあり / LAN 未認証 / Origin: null)
  - env-config.local.js からの obs_password フォールバック
  - /api/status の未認証時レスポンス最小化と 60 秒 TTL キャッシュ
- GET /api/pngtuber/config の未認証時 obs_input_name / matched_input_name マスク
- shared/api-token.js の authFetch オリジンガード (Node.js による検証)
"""

import http.client
import json
import os
import shutil
import subprocess
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


def _reset_status_cache():
    with server_handler._STATUS_VALIDATION_CACHE_LOCK:
        server_handler._STATUS_VALIDATION_CACHE["key"] = None
        server_handler._STATUS_VALIDATION_CACHE["expires_at"] = 0.0
        server_handler._STATUS_VALIDATION_CACHE["token_info"] = None
        server_handler._STATUS_VALIDATION_CACHE["channel_info"] = None
        server_handler._STATUS_VALIDATION_CACHE["broadcaster_id"] = ""


class TestEnvLocalPasswordReader(unittest.TestCase):
    def test_read_env_local_obs_password_and_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "env-config.local.js")
            cache = {"key": None, "password": ""}

            # 不在時は空文字
            self.assertEqual(
                server_config.read_env_local_obs_password(env_file=env_path, cache=cache),
                "",
            )

            # エスケープ含むパスワードの読み取り
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'my\\'secret\\\\pw' };\n")
            self.assertEqual(
                server_config.read_env_local_obs_password(env_file=env_path, cache=cache),
                "my'secret\\pw",
            )

            # YOUR_OBS_PASSWORD プレースホルダーは無視される
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'YOUR_OBS_PASSWORD' };\n")
            cache["key"] = None
            self.assertEqual(
                server_config.read_env_local_obs_password(env_file=env_path, cache=cache),
                "",
            )


class TestSecurityPhase2aHttp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmpdir.name, "twitch_config.json")
        self.env_local_path = os.path.join(self.tmpdir.name, "env-config.local.js")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "client_id": "test-client-id",
                    "access_token": "test-access-token",
                    "broadcaster_id": "12345",
                    "channel_name": "test_channel",
                    "obs_password": "",
                    "api_token": "secret-api-token",
                },
                f,
            )
        with open(self.env_local_path, "w", encoding="utf-8") as f:
            f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'env-local-obs-pw' };\n")

        self.patch_cfg = mock.patch.object(server_config, "CONFIG_FILE", self.cfg_path)
        self.patch_env = mock.patch.object(server_config, "ENV_LOCAL_JS_FILE", self.env_local_path)
        self.patch_cfg.start()
        self.patch_env.start()
        server_config._CONFIG_CACHE["key"] = None
        server_config._CONFIG_CACHE["value"] = None
        server_config._ENV_LOCAL_CACHE["key"] = None
        server_config._ENV_LOCAL_CACHE["password"] = ""
        _reset_status_cache()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.patch_env.stop()
        self.patch_cfg.stop()
        server_config._CONFIG_CACHE["key"] = None
        server_config._CONFIG_CACHE["value"] = None
        server_config._ENV_LOCAL_CACHE["key"] = None
        server_config._ENV_LOCAL_CACHE["password"] = ""
        _reset_status_cache()
        self.tmpdir.cleanup()

    def test_api_config_obs_password_fallback_across_paths(self):
        # 1. localhost: env-config.local.js からフォールバック取得できる
        status, _, raw = _raw_http_request(self.port, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["obs_password"], "env-local-obs-pw")

        # 2. Origin: null: 空文字
        status, _, raw = _raw_http_request(
            self.port, "GET", "/api/config", headers={"Origin": "null"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["obs_password"], "")

        # 3. LAN 未認証 (_client_is_local=False, トークンなし): 空文字
        with mock.patch.object(
            server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
        ):
            status, _, raw = _raw_http_request(self.port, "GET", "/api/config")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["obs_password"], "")

            # 4. LAN 認証済み (X-Widget-Token あり): env-config.local.js から取得できる
            status, _, raw = _raw_http_request(
                self.port,
                "GET",
                "/api/config",
                headers={"X-Widget-Token": "secret-api-token"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["obs_password"], "env-local-obs-pw")

        # 5. twitch_config.json に obs_password がある場合はそちらを優先する
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "client_id": "test-client-id",
                    "access_token": "test-access-token",
                    "obs_password": "twitch-cfg-obs-pw",
                    "api_token": "secret-api-token",
                },
                f,
            )
        status, _, raw = _raw_http_request(self.port, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["obs_password"], "twitch-cfg-obs-pw")

    def test_api_status_exposure_and_cache(self):
        fake_token_info = {
            "valid": True,
            "login": "streamer",
            "user_id": "12345",
            "client_id": "test-client-id",
            "scopes": ["chat:read"],
            "missing_scopes": [],
            "expires_in": 3600,
        }
        fake_channel = ({"data": [{"broadcaster_id": "12345", "title": "Live!"}]}, 200)

        with (
            mock.patch.object(
                server_handler, "validate_twitch_token", return_value=fake_token_info
            ) as mock_validate,
            mock.patch.object(
                server_handler, "twitch_request", return_value=fake_channel
            ) as mock_req,
        ):
            # 1. localhost: 詳細情報が返る
            status, _, raw = _raw_http_request(self.port, "GET", "/api/status")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertEqual(body["obs_password"], "env-local-obs-pw")
            self.assertEqual(body["broadcaster_id"], "12345")
            self.assertEqual(body["client_id"], "test-client-id")
            self.assertEqual(body["channel_info"], {"broadcaster_id": "12345", "title": "Live!"})
            self.assertEqual(body["token_info"]["login"], "streamer")
            self.assertEqual(mock_validate.call_count, 1)
            self.assertEqual(mock_req.call_count, 1)

            # 2. LAN 未認証: 60 秒キャッシュにより validate_twitch_token は再実行されず、最小情報のみ返る
            with mock.patch.object(
                server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
            ):
                status, _, raw = _raw_http_request(self.port, "GET", "/api/status")
                self.assertEqual(status, 200)
                lan_body = json.loads(raw)
                self.assertEqual(lan_body["obs_password"], "")
                self.assertEqual(lan_body["broadcaster_id"], "")
                self.assertEqual(lan_body["client_id"], "")
                self.assertIsNone(lan_body["channel_info"])
                self.assertEqual(lan_body["token_info"], {"valid": True})
                self.assertEqual(mock_validate.call_count, 1)

                # 3. LAN 認証済み (X-Widget-Token): 詳細が返る
                status, _, raw = _raw_http_request(
                    self.port,
                    "GET",
                    "/api/status",
                    headers={"X-Widget-Token": "secret-api-token"},
                )
                self.assertEqual(status, 200)
                auth_body = json.loads(raw)
                self.assertEqual(auth_body["obs_password"], "env-local-obs-pw")
                self.assertEqual(auth_body["broadcaster_id"], "12345")
                self.assertEqual(auth_body["client_id"], "test-client-id")
                self.assertEqual(auth_body["token_info"]["login"], "streamer")
                self.assertEqual(mock_validate.call_count, 1)

            # 4. Origin: null: 最小情報のみ返る
            status, _, raw = _raw_http_request(
                self.port, "GET", "/api/status", headers={"Origin": "null"}
            )
            self.assertEqual(status, 200)
            null_body = json.loads(raw)
            self.assertEqual(null_body["obs_password"], "")
            self.assertEqual(null_body["broadcaster_id"], "")
            self.assertEqual(null_body["client_id"], "")
            self.assertIsNone(null_body["channel_info"])
            self.assertEqual(null_body["token_info"], {"valid": True})

            # 5. 設定変更時はキャッシュキーが変わり再検証される
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "client_id": "test-client-id",
                        "access_token": "rotated-access-token",
                        "broadcaster_id": "12345",
                        "channel_name": "test_channel",
                        "obs_password": "",
                        "api_token": "secret-api-token",
                    },
                    f,
                )
            status, _, _ = _raw_http_request(self.port, "GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertEqual(mock_validate.call_count, 2)

            # 6. TTL 超過後は再検証される
            with server_handler._STATUS_VALIDATION_CACHE_LOCK:
                server_handler._STATUS_VALIDATION_CACHE["expires_at"] = 0.0
            status, _, _ = _raw_http_request(self.port, "GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertEqual(mock_validate.call_count, 3)

    def test_api_pngtuber_config_masks_input_names_when_unauthenticated(self):
        sample_png_cfg = {
            "avatar": "default",
            "avatars": [],
            "images": {},
            "image_labels": {},
            "obs_input_name": "My Secret Mic",
            "matched_input_name": "My Secret Mic (Matched)",
            "open_threshold": 0.05,
        }
        with (
            mock.patch.object(server_handler, "_PNGTUBER_ENABLED", True),
            mock.patch.object(
                server_handler,
                "_get_pngtuber_client_config",
                side_effect=lambda avatar="": dict(sample_png_cfg),
            ),
        ):
            # 1. localhost: そのまま返る
            status, _, raw = _raw_http_request(self.port, "GET", "/api/pngtuber/config")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertEqual(body["obs_input_name"], "My Secret Mic")
            self.assertEqual(body["matched_input_name"], "My Secret Mic (Matched)")

            # 2. Origin: null: マスクされる
            status, _, raw = _raw_http_request(
                self.port, "GET", "/api/pngtuber/config", headers={"Origin": "null"}
            )
            self.assertEqual(status, 200)
            null_body = json.loads(raw)
            self.assertEqual(null_body["obs_input_name"], "")
            self.assertEqual(null_body["matched_input_name"], "")
            self.assertEqual(null_body["open_threshold"], 0.05)

            # 3. LAN 未認証: マスクされる
            with mock.patch.object(
                server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
            ):
                status, _, raw = _raw_http_request(self.port, "GET", "/api/pngtuber/config")
                self.assertEqual(status, 200)
                lan_body = json.loads(raw)
                self.assertEqual(lan_body["obs_input_name"], "")
                self.assertEqual(lan_body["matched_input_name"], "")

                # 4. LAN 認証済み (X-Widget-Token): そのまま返る
                status, _, raw = _raw_http_request(
                    self.port,
                    "GET",
                    "/api/pngtuber/config",
                    headers={"X-Widget-Token": "secret-api-token"},
                )
                self.assertEqual(status, 200)
                auth_body = json.loads(raw)
                self.assertEqual(auth_body["obs_input_name"], "My Secret Mic")
                self.assertEqual(auth_body["matched_input_name"], "My Secret Mic (Matched)")


@unittest.skipUnless(shutil.which("node"), "node is required for JS origin guard test")
class TestApiTokenOriginGuardJs(unittest.TestCase):
    def test_auth_fetch_origin_guard(self):
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync('shared/api-token.js', 'utf8');

async function runCase(locationObj, envObj, targetUrl, expectToken) {
  let capturedHeaders = null;
  const ctx = {
    URL,
    location: locationObj,
    localStorage: {
      getItem: () => 'tok-123',
      setItem: () => {},
      removeItem: () => {},
    },
    fetch: async (url, opts) => {
      capturedHeaders = (opts && opts.headers) || {};
      return { ok: true, json: async () => ({}) };
    },
  };
  ctx.window = ctx;
  if (envObj) ctx.window.OBSWidgetsEnv = envObj;
  vm.runInNewContext(code, ctx);
  await ctx.window.authFetch(targetUrl, {});
  if (expectToken) {
    assert.strictEqual(capturedHeaders['X-Widget-Token'], 'tok-123', `Expected token for ${targetUrl}`);
  } else {
    assert.strictEqual(capturedHeaders['X-Widget-Token'], undefined, `Expected NO token for ${targetUrl}`);
  }
}

(async () => {
  const httpsLoc = {
    protocol: 'https:',
    hostname: '198.51.100.10',
    origin: 'https://198.51.100.10:8443',
    href: 'https://198.51.100.10:8443/WebCanvas/pad.html',
  };
  await runCase(httpsLoc, null, '/api/config', true);
  await runCase(httpsLoc, null, 'https://198.51.100.10:8443/api/pngtuber/inputs', true);
  await runCase(httpsLoc, null, 'https://evil.example/api/config', false);
  await runCase(httpsLoc, null, 'http://198.51.100.10:8080/api/config', false);
  await runCase(httpsLoc, null, 'http://[::invalid', false);

  const fileLoc = {
    protocol: 'file:',
    hostname: '',
    origin: 'null',
    href: 'file:///path/to/OBS_Widgets/WebCanvas/pad.html',
  };
  await runCase(fileLoc, null, 'http://localhost:8080/api/config', true);
  await runCase(fileLoc, null, 'http://127.0.0.1:8080/api/config', true);
  await runCase(fileLoc, null, 'http://[::1]:8080/api/config', true);
  await runCase(fileLoc, { API_BASE: 'https://198.51.100.10:8443' }, 'https://198.51.100.10:8443/api/config', true);
  await runCase(fileLoc, null, 'https://evil.example/api/config', false);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"node failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
